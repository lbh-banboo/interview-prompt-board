"""M15-T1/T2/T3、M16-T2/T4 离线部分：响应体/日志/导出/数据库无 Key。"""
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import logging_safe
from app.api import settings
from app.config import ConfigManager
from app.core import Store
from app.credentials import CredentialStore, MemoryBackend, selfcheck
from app.usage import BudgetGuard, UsageMeter

KEY = 'sk-FAKE123456789'  # pragma: allowlist secret（假 Key，尾 8/尾 4 断言依赖，长度 16 触发露尾掩码）
H = {'x-local-token': 'tok'}


def make(tmp_path, backend=None):
    store = Store(tmp_path)
    creds = CredentialStore([backend or MemoryBackend()])
    ctx = SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config=dict(credential_store=creds))
    app = FastAPI()
    settings.register(app, ctx)
    return TestClient(app), store, creds, app


def test_config_and_credential_responses_have_no_key(tmp_path):
    c, store, creds, _ = make(tmp_path)
    r = c.put('/api/credentials/DEEPSEEK_API_KEY', json={'value': KEY}, headers=H)
    assert r.json() == dict(name='DEEPSEEK_API_KEY', status='configured')
    bodies = [r.text, c.get('/api/config').text, c.get('/api/credentials/status').text, c.get('/api/usage').text]
    assert all(KEY not in b and KEY[-8:] not in b for b in bodies)
    st = c.get('/api/credentials/status').json()
    assert st['DEEPSEEK_API_KEY'] == dict(status='configured', mask='****6789', source='memory')
    assert st['SEARCH_API_KEY'] == dict(status='missing', mask='', source=None)
    assert creds.get('DEEPSEEK_API_KEY') == KEY


def test_credential_put_validation_and_token(tmp_path):
    c, *_ = make(tmp_path)
    assert c.put('/api/credentials/DEEPSEEK_API_KEY', json={'value': KEY}).status_code == 403
    assert c.put('/api/credentials/bad', json={'value': KEY}, headers=H).status_code == 400
    r = c.put('/api/credentials/DEEPSEEK_API_KEY', json={'value': 'a\nb'}, headers=H)
    assert r.status_code == 422 and 'a' not in r.json()['detail'].replace('凭证值无效', '')
    r = c.put('/api/credentials/DEEPSEEK_API_KEY', json={}, headers=H)
    assert r.status_code == 422


def test_credential_delete_is_guarded_and_removes_value(tmp_path):
    c, store, creds, _ = make(tmp_path)
    c.put('/api/credentials/DEEPSEEK_API_KEY', json={'value': KEY}, headers=H)
    assert c.delete('/api/credentials/DEEPSEEK_API_KEY').status_code == 403
    r = c.delete('/api/credentials/DEEPSEEK_API_KEY', headers=H)
    assert r.status_code == 200 and r.json() == dict(name='DEEPSEEK_API_KEY', status='missing')
    assert creds.get('DEEPSEEK_API_KEY') is None and KEY not in r.text


def test_credential_backend_failure_is_500_without_key(tmp_path):
    from app.credentials import EnvBackend
    c, *_ = make(tmp_path, backend=EnvBackend({}))
    r = c.put('/api/credentials/DEEPSEEK_API_KEY', json={'value': KEY}, headers=H)
    assert r.status_code == 500 and KEY not in r.text


def test_put_config_next_session_semantics(tmp_path):
    c, store, _, app = make(tmp_path)
    assert c.put('/api/config', json={'turn': {'T_silence': 300}}).status_code == 403
    r = c.put('/api/config', json={'turn': {'T_silence': 300}}, headers=H).json()
    assert r['active_session'] is False and r['config']['turn']['T_silence'] == 300
    lib = store.import_text('x', '**H1-01｜00:01｜问题？** 参考回答：答')['id']
    store.review(lib, 'H1-01', '答', 'approved', True)
    store.start('online', lib)
    snap = app.state.config_manager.snapshot_for_session()
    r = c.put('/api/config', json={'turn': {'T_silence': 350}}, headers=H).json()
    assert r['active_session'] is True and r['effective'] == '下一场生效'
    import json
    assert json.loads(snap)['turn']['T_silence'] == 300  # 已冻结的快照不变
    bad = c.put('/api/config', json={'turn': {'T_silence': 'x'}}, headers=H)
    assert bad.status_code == 422
    assert c.get('/api/config').json()['config']['turn']['T_silence'] == 350


def test_usage_endpoint(tmp_path):
    c, store, _, app = make(tmp_path)
    meter = UsageMeter(store.path, app.state.config_manager.config['prices'])
    meter.record('search', 2)
    r = c.get('/api/usage').json()
    assert r['rows'][0]['calls'] == 1 and r['max_inflight'] == 4 and '估算' in r['note']


def test_startup_without_keys_degrades(tmp_path):
    c, store, creds, _ = make(tmp_path)
    creds.set('DEEPSEEK_API_KEY', KEY)
    creds.delete('DEEPSEEK_API_KEY')
    check = selfcheck(creds)
    assert check['degraded'] and 'DEEPSEEK_API_KEY' in check['missing']
    assert c.get('/api/config').status_code == 200 and c.get('/api/credentials/status').status_code == 200


def test_zero_budget_blocks_with_message(tmp_path):
    c, store, _, app = make(tmp_path)
    c.put('/api/config', json={'budget': {'daily_limit': 0}}, headers=H)
    b = app.state.config_manager.config
    guard = BudgetGuard(UsageMeter(store.path, b['prices']), b['budget']['daily_limit'], b['budget']['session_limit'])
    res = guard.reserve('llm', 500)
    assert not res.ok and '预算' in res.message


def test_logs_and_export_and_db_contain_no_key(tmp_path):
    c, store, creds, app = make(tmp_path)
    creds.set('DEEPSEEK_API_KEY', KEY)
    log = logging.getLogger('privacy_test')
    log.setLevel(logging.DEBUG)
    log.propagate = False
    h = logging_safe.install(tmp_path / 'logs', logger=log)
    try:
        log.info('Authorization: Bearer %s', KEY)
        log.warning('DEEPSEEK_API_KEY=%s', KEY)
        try:
            raise RuntimeError(f'upstream said key {KEY}')
        except RuntimeError:
            log.exception('boom')
    finally:
        log.removeHandler(h)
        h.close()
    assert KEY not in (tmp_path / 'logs' / 'app.log').read_text('utf-8')
    lib = store.import_text('x', '**H1-01｜00:01｜问题？** 参考回答：答')['id']
    store.review(lib, 'H1-01', '答', 'approved', True)
    s = store.start('online', lib)['id']
    store.event(s, 'question_complete', question='你好？')
    store.stop(s)
    c.put('/api/config', json={'model': {'name': 'deepseek-flash'}}, headers=H)
    assert KEY not in store.export(s)
    assert KEY.encode() not in store.path.read_bytes()
    assert KEY not in app.state.config_manager.snapshot_for_session()
    assert KEY not in (tmp_path / 'config.json').read_text('utf-8')
