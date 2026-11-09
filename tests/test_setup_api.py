import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import setup as api
from app.core import Store
from app.sessions import setup as S

H = {'x-local-token': 'tok'}
GB = 1 << 30
GOOD = dict(audio_sources=lambda: True, asr_ping=lambda: True, llm_ping=lambda: True, search_ping=lambda: True,
            voice_enrolled=lambda: True, disk_free=lambda: 9 * GB)


def text(n, tag):
    return '\n'.join(f"**{tag}-{i:02d}｜00:0{i}｜问题{tag}{i}？** 参考回答：答案{i}" for i in range(1, n + 1))


def make(tmp_path, deps=None, config=None):
    store = Store(tmp_path)
    ok = store.import_text('已审核库', text(2, 'H1'))['id']
    zero = store.import_text('未审核库', text(2, 'H2'))['id']
    store.review(ok, 'H1-01', '答案', 'approved', True)
    cfg = dict(selfcheck_deps=GOOD if deps is None else deps, **(config or {}))
    app = FastAPI()
    api.register(app, SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config=cfg))
    c = TestClient(app)
    pid = c.post('/api/profiles', json=dict(alias='甲'), headers=H).json()['id']
    return c, store, ok, zero, pid


def create(c, pid, libs, roles=('管理',), mode='online', consent=True):
    return c.post('/api/setup/sessions', json=dict(mode=mode, profile_id=pid, roles=list(roles), library_ids=libs, consent=consent), headers=H)


def test_profiles_http_crud_and_token(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    assert c.post('/api/profiles', json=dict(alias='x')).status_code == 403
    assert c.get(f'/api/profiles/{pid}').json()['alias'] == '甲'
    r = c.put(f'/api/profiles/{pid}', json=dict(alias='乙', roles=['运营'], library_ids=[ok]), headers=H)
    assert r.json()['library_ids'] == [ok]
    assert len(c.get('/api/profiles').json()) == 1
    assert c.put('/api/profiles/zz', json=dict(alias='x'), headers=H).status_code == 404
    assert c.delete(f'/api/profiles/{pid}', headers=H).json() == dict(ok=True)
    assert c.get(f'/api/profiles/{pid}').status_code == 404


def test_options_lists_roles_and_gray_libraries_T2(tmp_path):
    c, store, ok, zero, pid = make(tmp_path, config=dict(extra_roles=['销售']))
    o = c.get('/api/setup/options').json()
    assert o['roles'] == ['技术开发', '产品经理', '运营', '管理', '销售'] and o['modes'] == ['online', 'offline']
    libs = {x['id']: x for x in o['libraries']}
    assert libs[ok]['selectable'] and libs[ok]['approved'] == 1
    assert not libs[zero]['selectable'] and libs[zero]['approved'] == 0


def test_unapproved_library_rejected_T2(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    r = create(c, pid, [zero])
    assert r.status_code == 400 and '启用 0 条' in r.json()['detail']
    assert create(c, pid, ['nope']).status_code == 404


def test_create_validation(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    assert create(c, pid, [ok], consent=False).status_code == 400
    assert create(c, pid, [ok], mode='x').status_code == 400
    assert create(c, pid, [ok], roles=[]).status_code == 400
    assert create(c, pid, [ok], roles=['外星人']).status_code == 400
    assert create(c, 'nope', [ok]).status_code == 404
    assert c.post('/api/setup/sessions', json=dict(mode='online', profile_id=pid, roles=['管理'], library_ids=[ok], consent=True)).status_code == 403


def test_cross_role_union_kept_in_order_and_snapshot(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    s = create(c, pid, [ok], roles=['技术开发', '管理', '技术开发']).json()
    assert s['status'] == 'draft' and s['roles'] == ['技术开发', '管理']
    assert s['config']['roles'] == ['技术开发', '管理'] and s['config']['library_ids'] == [ok]
    assert s['library_id'] == ok and s['profile_id'] == pid


def test_secondary_only_flag_saved_for_empty_library(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    body = dict(mode='online', profile_id=pid, roles=['管理'], library_ids=[], consent=True, secondary_only=True)
    s = c.post('/api/setup/sessions', json=body, headers=H).json()
    assert s['library_id'] == '' and s['config']['secondary_only'] is True
    body['secondary_only'] = False
    s2 = c.post('/api/setup/sessions', json=body, headers=H).json()
    assert s2['config']['secondary_only'] is False


def test_single_active_409_T1(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    sid = create(c, pid, [ok]).json()['id']
    assert c.post(f'/api/setup/{sid}/selfcheck', headers=H).status_code == 200
    assert c.post(f'/api/setup/{sid}/start', json={}, headers=H).json()['status'] == 'active'
    r = create(c, pid, [ok])
    assert r.status_code == 409 and '先结束' in r.json()['detail']
    assert c.post(f'/api/setup/{sid}/stop', headers=H).json()['status'] == 'stopped'
    assert create(c, pid, [ok]).status_code == 200


def test_start_conflict_when_other_session_active(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    a = create(c, pid, [ok]).json()['id']
    b = create(c, pid, [ok]).json()['id']
    for s in (a, b):
        c.post(f'/api/setup/{s}/selfcheck', headers=H)
    assert c.post(f'/api/setup/{a}/start', json={}, headers=H).status_code == 200
    assert c.post(f'/api/setup/{b}/start', json={}, headers=H).status_code == 409


def test_asr_fail_degraded_start_T3(tmp_path):
    c, store, ok, zero, pid = make(tmp_path, deps={**GOOD, 'asr_ping': None})
    sid = create(c, pid, [ok]).json()['id']
    r = c.post(f'/api/setup/{sid}/selfcheck', headers=H).json()
    assert [i for i in r['items'] if i['name'] == 'asr_connect'][0]['status'] == 'FAIL'
    assert c.get(f'/api/setup/{sid}').json()['status'] == 'ready'
    refuse = c.post(f'/api/setup/{sid}/start', json=dict(degraded=False), headers=H)
    assert refuse.status_code == 409 and '降级开始' in refuse.json()['detail']
    s = c.post(f'/api/setup/{sid}/start', json=dict(degraded=True), headers=H).json()
    assert s['status'] == 'active' and s['degraded_banner'] == '实时转写不可用' and s['started_at'] is not None


def test_start_before_selfcheck_and_bad_states(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    sid = create(c, pid, [ok]).json()['id']
    r = c.post(f'/api/setup/{sid}/start', json={}, headers=H)
    assert r.status_code == 409 and '自检' in r.json()['detail']
    c.post(f'/api/setup/{sid}/selfcheck', headers=H)
    c.post(f'/api/setup/{sid}/start', json={}, headers=H)
    assert c.post(f'/api/setup/{sid}/selfcheck', headers=H).status_code == 409
    assert c.post(f'/api/setup/{sid}/start', json={}, headers=H).status_code == 409
    c.post(f'/api/setup/{sid}/stop', headers=H)
    assert c.post(f'/api/setup/{sid}/stop', headers=H).status_code == 409
    assert c.get('/api/setup/none').status_code == 404


def test_cannot_switch_profile_or_roles_while_active(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    pid2 = c.post('/api/profiles', json=dict(alias='乙'), headers=H).json()['id']
    sid = create(c, pid, [ok]).json()['id']
    r = c.put(f'/api/setup/{sid}', json=dict(profile_id=pid2, roles=['运营']), headers=H)
    assert r.status_code == 200 and r.json()['roles'] == ['运营'] and r.json()['config']['profile']['id'] == pid2
    assert c.put(f'/api/setup/{sid}', json=dict(profile_id='nope'), headers=H).status_code == 404
    assert c.put(f'/api/setup/{sid}', json=dict(roles=['x']), headers=H).status_code == 400
    c.post(f'/api/setup/{sid}/selfcheck', headers=H)
    assert c.put(f'/api/setup/{sid}', json=dict(roles=['管理']), headers=H).json()['status'] == 'draft'
    c.post(f'/api/setup/{sid}/selfcheck', headers=H)
    c.post(f'/api/setup/{sid}/start', json={}, headers=H)
    r = c.put(f'/api/setup/{sid}', json=dict(profile_id=pid2), headers=H)
    assert r.status_code == 409 and '不能切换' in r.json()['detail']
    c.post(f'/api/setup/{sid}/stop', headers=H)
    assert c.put(f'/api/setup/{sid}', json=dict(roles=['管理']), headers=H).status_code == 409


def test_empty_library_selfcheck_warns_and_default_disk(tmp_path):
    c, store, ok, zero, pid = make(tmp_path, deps={k: v for k, v in GOOD.items() if k != 'disk_free'})
    sid = create(c, pid, []).json()['id']
    r = c.post(f'/api/setup/{sid}/selfcheck', headers=H).json()
    s = {i['name']: i for i in r['items']}
    assert s['library_approved_count']['status'] == 'WARN' and s['disk_space']['status'] in ('PASS', 'WARN', 'FAIL')
    assert c.get(f'/api/setup/{sid}').json()['library_id'] == ''


def test_default_probes_used_when_not_injected(tmp_path):
    """生产默认依赖兜底：测试环境无 Key、无模型时也要给出诚实结果，而不是全部“未配置”式 FAIL。"""
    c, store, ok, zero, pid = make(tmp_path, deps={})
    sid = create(c, pid, [ok]).json()['id']
    s = {i['name']: i for i in c.post(f'/api/setup/{sid}/selfcheck', headers=H).json()['items']}
    assert s['audio_sources']['status'] == 'WARN' and '授权' in s['audio_sources']['detail']
    assert s['search_connect']['status'] == 'WARN' and '搜索未配置' in s['search_connect']['detail']
    assert s['llm_connect']['status'] == 'FAIL' and '未配置 DeepSeek 凭证' in s['llm_connect']['detail']
    assert s['asr_connect']['status'] == 'FAIL' and '本地模型缺失' in s['asr_connect']['detail']


def test_endpoint_timeout_config(tmp_path):
    c, store, ok, zero, pid = make(tmp_path, deps={**GOOD, 'asr_ping': lambda: time.sleep(0.5) or True},
                                   config=dict(selfcheck_item_timeout=0.1))
    sid = create(c, pid, [ok]).json()['id']
    r = c.post(f'/api/setup/{sid}/selfcheck', headers=H).json()
    assert [i for i in r['items'] if i['name'] == 'asr_connect'][0]['status'] == 'FAIL'


def test_ended_status_maps_to_stopped_and_columns_idempotent(tmp_path):
    c, store, ok, zero, pid = make(tmp_path)
    S.ensure_sessions_columns(store)
    S.ensure_sessions_columns(store)
    cols = {r['name'] for r in store.connect().execute('PRAGMA table_info(sessions)')}
    assert {n for n, _ in S.NEW_COLUMNS} <= cols
    with store.connect() as db:
        db.execute("INSERT INTO sessions(id,mode,status,library_id) VALUES('old','online','ended',?)", (ok,))
    v = c.get('/api/setup/old').json()
    assert v['status'] == 'stopped' and v['config'] is None
    assert c.post('/api/setup/old/stop', headers=H).status_code == 409
