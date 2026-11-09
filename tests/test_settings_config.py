"""设置页/会话快照：asr.backend 校验与选择、本场 outbound 覆盖以快照为准、默认值下发。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigManager, load_config, validate_patch
from app.credentials import CredentialStore, MemoryBackend
from app.server import create_app
from app.sessions import profiles as P
from app.sessions import setup as S
from test_core import TEXT

H = {'x-local-token': 'tok'}
FAKE_KEY = 'sk-fake-not-real-0000'  # pragma: allowlist secret（假 Key）


def _sherpa(tmp_path, monkeypatch, files=('encoder.int8.onnx',)):
    import app.wiring
    d = tmp_path / 'sherpa'
    d.mkdir()
    for name in files:
        (d / name).write_bytes(b'0')
    monkeypatch.setattr(app.wiring, 'SHERPA_DIR', d)
    return d


def make(tmp_path, creds=None):
    app = create_app(tmp_path, credentials=creds or CredentialStore([MemoryBackend()]))
    store = app.state.store
    lib = store.import_text('库', TEXT)['id']
    store.review(lib, 'H1-01', '答案', 'approved', True)
    pid = P.create_profile(store, '甲', ['管理'], '', [lib])['id']
    return app, store, lib, pid


def new_session(app, store, lib, pid, outbound=None):
    sid = S.create_session_v2(store, 'online', pid, ['管理'], [lib], True, outbound=outbound)['id']
    app.state.wiring.freeze(sid)  # 与实际启动一样冻结快照（同进程多场，不起 active）
    return sid


def test_asr_backend_schema_and_persist(tmp_path):
    for value in ('auto', 'aliyun', 'sherpa'):
        good, errors = validate_patch({'asr': {'backend': value}})
        assert errors == [] and good['asr.backend'] == value
    good, errors = validate_patch({'asr': {'backend': 'whisper'}})
    assert not good and 'asr.backend' in errors[0]
    m = ConfigManager(tmp_path / 'config.json')
    assert m.config['asr']['backend'] == 'auto'
    m.update({'asr': {'backend': 'sherpa'}})
    assert ConfigManager(tmp_path / 'config.json').config['asr']['backend'] == 'sherpa'


def test_removed_tencent_backend_in_user_config_falls_back_without_error(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'asr': {'backend': 'tencent'}}), 'utf-8')
    m = ConfigManager(tmp_path / 'config.json')
    assert m.config['asr']['backend'] == 'auto'  # 不阻止启动，回落默认
    assert any('asr.backend' in e for e in m.errors)


def test_search_config_defaults_and_validation(tmp_path):
    cfg, errors = load_config()
    assert errors == [] and cfg['search'] == {
        'live': True, 'endpoint': 'https://api.deepseek.com/v1/tools/search', 'count': 5, 'need_content': False}
    good, errors = validate_patch({'search': {'live': False, 'count': 10, 'need_content': True,
                                             'endpoint': 'https://api.deepseek.com/v1/tools/search'}})
    assert errors == [] and good['search.count'] == 10 and good['search.live'] is False
    for patch in ({'search': {'count': 0}}, {'search': {'count': 51}}, {'search': {'live': 'yes'}},
                  {'search': {'endpoint': ''}}, {'search': {'need_content': 'no'}}):
        good, errors = validate_patch(patch)
        assert not good and errors


def test_asr_backend_unknown_rejected_by_api(tmp_path):
    app, store, lib, pid = make(tmp_path)
    c = TestClient(app)
    h = {'X-Local-Token': c.get('/api/status').json()['token']}
    r = c.put('/api/config', json={'asr': {'backend': 'whisper'}}, headers=h)
    assert r.status_code == 422 and 'asr.backend' in r.json()['detail']
    ok = c.put('/api/config', json={'asr': {'backend': 'tencent'}}, headers=h)
    assert ok.status_code == 422 and 'asr.backend' in ok.json()['detail']
    ok = c.put('/api/config', json={'asr': {'backend': 'sherpa'}}, headers=h)
    assert ok.status_code == 200 and ok.json()['config']['asr']['backend'] == 'sherpa'


def test_get_config_exposes_defaults_without_secrets(tmp_path):
    app, *_ = make(tmp_path)
    body = TestClient(app).get('/api/config').json()
    assert body['defaults']['asr']['backend'] == 'auto'
    assert body['defaults']['turn']['T_silence'] == 200
    assert 'sk-' not in json.dumps(body)


def test_outbound_override_saved_in_snapshot_and_wins_over_global(tmp_path):
    app, store, lib, pid = make(tmp_path)
    manager = app.state.config_manager
    assert manager.config['outbound']['llm_snippets'] is True
    sid = new_session(app, store, lib, pid, outbound={'llm_snippets': False})
    snap = app.state.wiring.snapshot(sid)
    assert snap['outbound']['llm_snippets'] is False
    assert snap['outbound']['asr_audio'] is True and snap['outbound']['web_questions'] is True
    assert snap['roles'] == ['管理'] and snap['turn']['T_silence'] == 200  # 其余快照字段保留
    manager.update({'outbound': {'llm_snippets': True}})  # 全局改动不影响已冻结快照
    assert app.state.wiring.snapshot(sid)['outbound']['llm_snippets'] is False


def test_outbound_override_only_allows_closing_known_flags(tmp_path):
    app, store, lib, pid = make(tmp_path)
    with pytest.raises(S.SetupError) as e:
        S.create_session_v2(store, 'online', pid, ['管理'], [lib], True, outbound={'nope': False})
    assert e.value.status == 400 and '未知外发开关' in e.value.message
    with pytest.raises(S.SetupError) as e:
        S.create_session_v2(store, 'online', pid, ['管理'], [lib], True, outbound={'asr_audio': True})
    assert e.value.status == 400 and '只能关闭' in e.value.message
    with pytest.raises(S.SetupError) as e:
        S.create_session_v2(store, 'online', pid, ['管理'], [lib], True, outbound={'asr_audio': 'no'})
    assert e.value.status == 400 and '布尔' in e.value.message


def test_create_api_rejects_bad_outbound(tmp_path):
    app, store, lib, pid = make(tmp_path)
    c = TestClient(app)
    h = {'X-Local-Token': c.get('/api/status').json()['token']}
    base = dict(mode='online', profile_id=pid, roles=['管理'], library_ids=[lib], consent=True)
    assert c.post('/api/setup/sessions', json={**base, 'outbound': {'web_questions': True}}, headers=h).status_code == 400
    assert c.post('/api/setup/sessions', json={**base, 'outbound': {'nope': False}}, headers=h).status_code == 400
    ok = c.post('/api/setup/sessions', json={**base, 'outbound': {'web_questions': False}}, headers=h).json()
    assert ok['config']['outbound'] == {'web_questions': False}


def test_asr_factory_honours_backend_and_outbound(tmp_path, monkeypatch):
    _sherpa(tmp_path, monkeypatch)
    creds = CredentialStore([MemoryBackend({'DASHSCOPE_API_KEY': FAKE_KEY})])
    app, store, lib, pid = make(tmp_path, creds)
    manager = app.state.config_manager
    wire = app.state.wiring

    sid = new_session(app, store, lib, pid)
    wire.asr_factory(sid)
    assert wire.asr_backend[sid] == 'aliyun'  # auto：有 Key 且外发开

    manager.update({'asr': {'backend': 'sherpa'}})
    sid2 = new_session(app, store, lib, pid)
    wire.asr_factory(sid2)
    assert wire.asr_backend[sid2] == 'sherpa'

    manager.update({'asr': {'backend': 'aliyun'}})
    sid3 = new_session(app, store, lib, pid)
    wire.asr_factory(sid3)
    assert wire.asr_backend[sid3] == 'aliyun'

    # 外发关闭时显式选择云后端也不建云连接，降级本地
    sid4 = new_session(app, store, lib, pid, outbound={'asr_audio': False})
    wire.asr_factory(sid4)
    assert wire.asr_backend[sid4] == 'sherpa'


def test_asr_factory_aliyun_without_key_falls_back(tmp_path, monkeypatch):
    _sherpa(tmp_path, monkeypatch)
    app, store, lib, pid = make(tmp_path)
    app.state.config_manager.update({'asr': {'backend': 'aliyun'}})
    sid = new_session(app, store, lib, pid)
    app.state.wiring.asr_factory(sid)
    assert app.state.wiring.asr_backend[sid] == 'sherpa'
    assert any('未配置 DASHSCOPE_API_KEY' in n for n in app.state.wiring.health(sid).payload()['notices'])


def test_search_config_frozen_into_session_snapshot(tmp_path):
    app, store, lib, pid = make(tmp_path)
    sid = new_session(app, store, lib, pid)
    assert app.state.wiring.snapshot(sid)['search']['live'] is True
    app.state.config_manager.update({'search': {'live': False, 'count': 8}})
    snap = app.state.wiring.snapshot(sid)
    assert snap['search']['live'] is True and snap['search']['count'] == 5  # 本场以快照为准
    sid2 = new_session(app, store, lib, pid)
    assert app.state.wiring.snapshot(sid2)['search']['live'] is False


def test_config_api_exposes_threshold_source(tmp_path):
    app, *_ = make(tmp_path)
    c = TestClient(app)
    body = c.get('/api/config').json()
    assert body['thresholds_source'] == 'default'
    h = {'X-Local-Token': c.get('/api/status').json()['token']}
    r = c.put('/api/config', json={'retrieval_thresholds': {'tau_cos': 0.66}}, headers=h)
    assert r.status_code == 200 and r.json()['thresholds_source'] == 'snapshot'


def test_usage_api_exposes_service_limits_and_units(tmp_path):
    app, *_ = make(tmp_path)
    data = TestClient(app).get('/api/usage').json()
    assert data['asr_daily_limit'] == 5.0 and data['asr_session_limit'] == 1.5
    assert 'ASR=秒' in data['note'] and 'LLM=估算 token' in data['note']


def test_probes_honour_session_outbound_override(tmp_path):
    from app.sessions import probes
    from app.sessions.selfcheck import WARN
    creds = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': FAKE_KEY})])
    app, store, lib, pid = make(tmp_path, creds)
    ctx = app.state.ctx
    assert probes.session_outbound(ctx, None) is None

    sid = new_session(app, store, lib, pid, outbound={'llm_snippets': False})
    s = S.session_view(store, sid)
    merged = probes.session_outbound(ctx, s['config'])
    assert merged['outbound']['llm_snippets'] is False
    assert merged['outbound']['asr_audio'] is True  # 未覆盖的开关沿用全局

    assert probes.llm_ping(creds, merged)() == (WARN, '外发已关闭')
    deps = probes.default_deps(ctx, config=merged)
    assert set(deps) == {'audio_sources', 'asr_ping', 'llm_ping', 'search_ping', 'voice_enrolled'}
