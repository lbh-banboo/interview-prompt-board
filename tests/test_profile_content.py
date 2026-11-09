"""回答者资料自带内容 → 生成主库；更新走 reimport；单选关联；冻结拒绝；5MB 上限。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import setup as api
from app.core import Store
from app.sessions import profiles as P
from app.sessions import setup as S

from types import SimpleNamespace

H = {'x-local-token': 'tok'}


def row(n, answer):
    return f"**H1-{n:02d}｜00:0{n}｜经历{n}？** 参考回答：{answer}"


def md(rows):
    return "# 个人资料\n\n## H1 项目\n" + "\n".join(rows) + "\n"


BASE = [row(1, '我负责“星河”项目，团队 8 人。'), row(2, '答案二。')]


def make(tmp_path):
    store = Store(tmp_path)
    return store


def test_create_with_content_generates_pending_library_and_binds(tmp_path):
    store = make(tmp_path)
    content = md(BASE)
    p = P.create_profile(store, '甲', ['管理'], '备注', content=content)
    assert p['has_content'] is True
    lib = p['content_library_id']
    assert lib and p['library_ids'] == [lib]
    entries = store.entries(lib)
    assert len(entries) == 2 and all(e['status'] == 'pending' for e in entries), '新条目不得自动启用'
    assert p['library_stats']['total'] == 2 and p['library_stats']['approved'] == 0
    assert p['library_stats']['name'] == '资料：甲'
    # 详情带正文；列表不带正文，避免一次拉全部大文本
    assert P.get_profile(store, p['id'])['content'] == content
    assert 'content' not in P.list_profiles(store)[0]
    assert P.list_profiles(store)[0]['has_content'] is True


def test_update_content_inherits_unchanged_and_returns_stats(tmp_path):
    store = make(tmp_path)
    p = P.create_profile(store, '甲', ['管理'], '', content=md(BASE))
    lib = p['content_library_id']
    store.review(lib, 'H1-01', '确认答案', 'approved', True)

    updated = md([row(1, '我负责“星河”项目，团队 8 人。'), row(2, '答案二已修改。'), row(3, '新增答案三。')])
    result = P.set_profile_content(store, p['id'], updated)
    assert set(result['stats']['added']) == {'H1-03'}
    assert set(result['stats']['changed']) == {'H1-02'}
    assert result['stats']['deleted'] == []
    statuses = {e['id']: e['status'] for e in store.entries(lib)}
    assert statuses == {'H1-01': 'approved', 'H1-02': 'pending', 'H1-03': 'pending'}
    assert store.entries(lib)[0]['approved_answer'] == '确认答案'
    assert P.get_profile(store, p['id'])['content'] == updated
    assert result['profile']['library_stats']['total'] == 3


def test_update_content_reports_deleted(tmp_path):
    store = make(tmp_path)
    p = P.create_profile(store, '甲', ['管理'], '', content=md(BASE))
    result = P.set_profile_content(store, p['id'], md([row(1, '我负责“星河”项目，团队 8 人。')]))
    assert result['stats']['deleted'] == ['H1-02']
    assert result['stats']['added'] == [] and result['stats']['changed'] == []


def test_update_rejected_while_active_session_uses_library(tmp_path):
    store = make(tmp_path)
    p = P.create_profile(store, '甲', ['管理'], '', content=md(BASE))
    lib = p['content_library_id']
    store.review(lib, 'H1-01', '确认答案', 'approved', True)
    sid = S.create_session_v2(store, 'online', p['id'], ['管理'], [lib], True)['id']
    S.mark_ready(store, sid, dict(has_fail=False, degraded_banner=None))
    S.start_session(store, sid)
    with pytest.raises(P.ProfileError) as e:
        P.set_profile_content(store, p['id'], md([row(1, '改。'), row(2, '答案二。')]))
    assert e.value.status == 400 and '冻结' in e.value.message


def test_single_library_link_only(tmp_path):
    store = make(tmp_path)
    a = store.import_text('库A', md([row(1, '答。')]))['id']
    b = store.import_text('库B', md([row(2, '答。')]))['id']
    with pytest.raises(P.ProfileError) as e:
        P.create_profile(store, '甲', library_ids=[a, b])
    assert e.value.status == 400 and '最多关联一个主库' in e.value.message
    p = P.create_profile(store, '乙', library_ids=[a])
    with pytest.raises(P.ProfileError) as e:
        P.update_profile(store, p['id'], '乙', library_ids=[a, b])
    assert e.value.status == 400


def test_content_size_and_format_limits(tmp_path):
    store = make(tmp_path)
    with pytest.raises(P.ProfileError) as e:
        P.create_profile(store, '甲', content='x' * 5_000_001)
    assert e.value.status == 400 and '5MB' in e.value.message
    with pytest.raises(P.ProfileError) as e:
        P.create_profile(store, '甲', content='没有问答格式')
    assert e.value.status == 400 and '附录问答格式' in e.value.message
    p = P.create_profile(store, '乙')
    with pytest.raises(P.ProfileError) as e:
        P.set_profile_content(store, p['id'], '')
    assert e.value.status == 400


def test_content_http_routes(tmp_path):
    store = make(tmp_path)
    app = FastAPI()
    api.register(app, SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config={}))
    c = TestClient(app)
    p = c.post('/api/profiles', json=dict(alias='甲', roles=['管理'], content=md(BASE)), headers=H).json()
    assert p['library_ids'] and p['content_library_id'] == p['library_ids'][0]
    detail = c.get(f"/api/profiles/{p['id']}").json()
    assert detail['content'] == md(BASE)
    lib = p['content_library_id']
    # 启用一条后更新：统计与审核继承
    store.review(lib, 'H1-01', '确认答案', 'approved', True)
    r = c.post(f"/api/profiles/{p['id']}/content", json=dict(text=md([row(1, '我负责“星河”项目，团队 8 人。'), row(3, '新。')])), headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['stats']['added'] == ['H1-03'] and body['stats']['deleted'] == ['H1-02']
    assert body['profile']['library_stats']['total'] == 2
    # 关联库至多 1 个
    other = store.import_text('另一个', md([row(9, '答。')]))['id']
    bad = c.put(f"/api/profiles/{p['id']}", json=dict(alias='甲', library_ids=[lib, other]), headers=H)
    assert bad.status_code == 400 and '最多关联一个主库' in bad.json()['detail']
    assert c.post(f"/api/profiles/{p['id']}/content", json=dict(text='x'), headers=H).status_code == 400
