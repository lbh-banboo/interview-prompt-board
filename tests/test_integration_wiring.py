"""主线接线集成测试：全部临时库 + Mock/注入，不联网、不读 data/。
覆盖：路由与 token、配置快照冻结、WS 音频 -> board、记录与导出、故障注入、线下声纹降级、外发开关、种子岗位别名。
"""
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.asr.metered import MeteredASR
from app.asr.mock import MockASR
from app.core import Store
from app.credentials import CredentialStore, MemoryBackend
from app.realtime.audio_ws import AudioHub
from app.server import create_app
from app.sessions.setup import ensure_sessions_columns
from test_answer_loop import APPROVED, LIB, Q_TX, Run, wire
import test_answer_loop

HEALTH_KEYS = {'capture', 'asr', 'role', 'retrieval', 'llm', 'search', 'voice', 'disk', 'notices'}


def token_of(c):
    return {'X-Local-Token': c.get('/api/status').json()['token']}


def start_session(c, mode='online'):
    h = token_of(c)
    lib = c.post('/api/import', json={'name': 't', 'text': LIB}, headers=h).json()['id']
    for k, ans in APPROVED.items():
        c.post(f'/api/libraries/{lib}/entries/{k}/review',
               json={'status': 'approved', 'answer': ans, 'confirmed': True}, headers=h)
    sid = c.post('/api/sessions', json={'mode': mode, 'library': lib, 'consent': True}, headers=h).json()['id']
    return h, lib, sid


def column(c, sid, name='config_snapshot_json'):
    with closing(sqlite3.connect(c.app.state.store.path)) as db:
        return db.execute(f'SELECT {name} FROM sessions WHERE id=?', (sid,)).fetchone()[0]


# ---------- 1. Store.start 与加列 ----------
def test_store_start_works_after_sessions_columns_added(tmp_path):
    s = Store(tmp_path)
    ensure_sessions_columns(s)
    lib = s.import_text('样例', '**H1-01｜00:01｜你的项目是什么？** 参考回答：我做过一个原型。')['id']
    s.review(lib, 'H1-01', '练习稿', 'approved', True)
    sid = s.start('online', lib)['id']
    assert s.session(sid)['status'] == 'active'
    ensure_sessions_columns(s)  # 幂等


# ---------- 12(a) 路由可达与 token ----------
def test_a_new_routes_reachable_and_token_enforced(tmp_path):
    c = TestClient(create_app(tmp_path))
    h, lib, sid = start_session(c)
    for path in ('/api/config', '/api/credentials/status', '/api/usage', '/api/profiles', '/api/setup/options',
                 '/api/secondary/roles', f'/api/libraries/{lib}/review-queue', f'/api/libraries/{lib}/progress',
                 f'/api/libraries/{lib}/conflicts', f'/api/sessions/{sid}/transcript', f'/api/sessions/{sid}/turns',
                 f'/api/sessions/{sid}/prompts'):
        assert c.get(path, headers=h).status_code == 200, path
    assert c.get('/api/voice/profile', headers=h).json()['enrolled'] is False
    for method, path in (('put', '/api/config'), ('post', '/api/secondary/import'), ('post', '/api/profiles'),
                         ('post', f'/api/libraries/{lib}/batch-approve'), ('post', '/api/voice/enroll'),
                         ('put', '/api/credentials/DEEPSEEK_API_KEY'), ('delete', f'/api/sessions/{sid}'),
                         ('post', '/api/setup/sessions')):
        assert c.request(method.upper(), path, json={}).status_code == 403, path
    assert c.get('/').status_code == 200  # 静态页仍在最后挂载


def test_status_exposes_only_configured_missing_and_budget(tmp_path):
    creds = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': 'sk-abcdefghijklmnop'})])  # pragma: allowlist secret
    c = TestClient(create_app(tmp_path, credentials=creds))
    data = c.get('/api/status').json()
    assert data['credentials'] == {'asr': 'missing', 'llm': 'configured', 'search': 'missing'}
    assert 'sk-abcdefghijklmnop' not in json.dumps(data) and data['budget']['daily_limit'] == 20.0  # pragma: allowlist secret


def test_export_route_conflict_legacy_path_serves_records_export(tmp_path):
    c = TestClient(create_app(tmp_path))
    h, lib, sid = start_session(c)
    text = c.get(f'/api/sessions/{sid}/export').text  # 无 token 的下载链接仍可用
    assert '实际发言' in text and '回答者发言未转写（仅录音）' in text


def test_answerer_asr_switch_defaults_off_and_can_be_enabled(tmp_path):
    off = TestClient(create_app(tmp_path / 'off'))
    _, _, sid = start_session(off)
    pipe = off.app.state.audio_hub.pipeline(sid)
    assert pipe.answerer_asr is False
    assert '回答者发言未转写（仅录音）' in off.get(f'/api/sessions/{sid}/export').text

    on = TestClient(create_app(tmp_path / 'on'))
    h = token_of(on)
    assert on.put('/api/config', json={'outbound': {'asr_answerer': True}}, headers=h).status_code == 200
    _, _, sid = start_session(on)
    pipe = on.app.state.audio_hub.pipeline(sid)
    assert pipe.answerer_asr is True
    assert '回答者发言未转写（仅录音）' not in on.get(f'/api/sessions/{sid}/export').text


# ---------- 12(b) 配置快照 ----------
def test_b_snapshot_written_and_frozen_against_config_change(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    h, lib, sid = start_session(c)
    snap = json.loads(column(c, sid))
    assert snap == app.state.config_manager.config and snap['turn']['T_silence'] == 200
    r = c.put('/api/config', json={'turn': {'T_silence': 300}, 'session': {'idle_timeout_s': 600}}, headers=h)
    assert r.status_code == 200 and c.get('/api/config').json()['config']['turn']['T_silence'] == 300
    assert json.loads(column(c, sid)) == snap  # 运行中快照不变
    pipe = app.state.audio_hub.pipeline(sid)
    assert (pipe.detector.T_silence, pipe.detector.T_long) == (200, 700)
    assert app.state.wiring.idle_timeout(sid) == 7200


def test_snapshot_applies_turn_params_and_next_session_uses_new_config(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    h = token_of(c)
    c.put('/api/config', json={'turn': {'T_silence': 300, 'T_long': 800, 'T_hard': 1600, 'T_answer_silence': 2000}},
          headers=h)
    h, lib, sid = start_session(c)
    d = app.state.audio_hub.pipeline(sid).detector
    assert (d.T_silence, d.T_long, d.T_hard, d.T_answer_silence) == (300, 800, 1600, 2000)


def test_setup_flow_start_freezes_config_into_setup_snapshot(tmp_path):
    app = create_app(tmp_path)
    c = TestClient(app)
    h = token_of(c)
    lib = c.post('/api/import', json={'name': 't', 'text': LIB}, headers=h).json()['id']
    for k, ans in APPROVED.items():
        c.post(f'/api/libraries/{lib}/entries/{k}/review', json={'status': 'approved', 'answer': ans, 'confirmed': True}, headers=h)
    pid = c.post('/api/profiles', json={'alias': '我', 'roles': []}, headers=h).json()['id']
    sid = c.post('/api/setup/sessions', json={'mode': 'online', 'profile_id': pid, 'roles': ['技术开发'],
                                              'library_ids': [lib], 'consent': True}, headers=h).json()['id']
    assert c.post(f'/api/setup/{sid}/selfcheck', headers=h).status_code == 200
    r = c.post(f'/api/setup/{sid}/start', json={'degraded': True}, headers=h)
    assert r.status_code == 200, r.text
    snap = json.loads(column(c, sid))
    assert snap['turn']['T_silence'] == 200 and snap['roles'] == ['技术开发']  # 并入，不覆盖 setup 的内容
    c.put('/api/config', json={'turn': {'T_silence': 400}}, headers=h)
    assert json.loads(column(c, sid))['turn']['T_silence'] == 200


# ---------- 12(c)(d) WS 音频 -> board、记录、导出 ----------
def test_c_audio_ws_to_board_turn_state_and_health_then_d_records_export(tmp_path):
    run = Run(tmp_path, Q_TX)
    events = run.play(lambda e: e['type'] == 'prompt_primary')
    types = {e['type'] for e in events}
    assert {'turn_state', 'health', 'prompt_primary'} <= types
    health = [e['payload'] for e in events if e['type'] == 'health'][-1]
    assert HEALTH_KEYS <= set(health) and health['asr']['state'] in ('ok', 'degraded', 'down')
    assert all({'state', 'reason', 'since'} <= set(health[k]) for k in HEALTH_KEYS - {'notices'})
    assert health['llm']['state'] == 'down' and health['llm']['reason'] == '配置缺失'  # 无 Key 不发请求
    run.stop()
    rs = run.app.state.record_store
    assert rs.rows('SELECT id, state FROM turns WHERE session_id=?', (run.sid,))[0]['id'] == 't1'
    prompts = rs.rows('SELECT * FROM prompts WHERE session_id=?', (run.sid,))
    assert prompts and prompts[0]['block'] == 'primary' and prompts[0]['turn_id'] == 't1'
    md = run.client.get(f'/api/sessions/{run.sid}/export').text
    parts = {p.split('\n', 1)[0]: p for p in md.split('\n## ')}
    transcript = parts['现场实际发言（逐字稿）']
    suggestions = parts['系统建议（系统生成，不等于实际发言）']
    assert Q_TX in transcript and '我的口径' not in transcript and '回答者发言未转写' in transcript
    assert '我的口径' in suggestions and Q_TX not in suggestions
    metrics_turns = {r[0] for r in test_answer_loop.metric_rows(run.app.state.store, run.sid)}
    assert metrics_turns == {'t1'}  # 埋点 turn id 与 board 一致


def test_d_export_has_no_key_even_if_spoken(tmp_path):
    key = 'sk-abcdefghijklmnopq'  # pragma: allowlist secret
    run = Run(tmp_path, f'{Q_TX} {key}')
    run.play(lambda e: e['type'] == 'transcript')
    run.stop()
    md = run.client.get(f'/api/sessions/{run.sid}/export').text
    assert key not in md and 'sk-***' in md
    assert key not in run.app.state.store.export(run.sid)


def test_late_prompts_archived_in_records(tmp_path):
    run = Run(tmp_path, Q_TX)
    run.play(lambda e: e['type'] == 'prompt_primary')
    bus = run.app.state.bus
    assert bus.publish_prompt_primary(run.sid, 't1', 0, {'conclusion': '旧'}, 'L1', 'x') is None  # 旧 epoch
    run.stop()
    rows = run.app.state.record_store.rows("SELECT * FROM prompts WHERE state='late_archived'")
    assert rows and '旧' in rows[0]['content_json']
    assert '未展示（迟到归档）' in run.client.get(f'/api/sessions/{run.sid}/export').text


# ---------- 12(e) 故障注入：ASR down / 停滞 ----------
def test_e_asr_down_shows_unavailable_notice_on_board(tmp_path):
    def broken(source):
        asr = MockASR([], source)
        asr._broken = 'injected'
        return asr
    now = [100.0]
    app = create_app(tmp_path, asr_factory=broken, clock=lambda: now[0])
    c = TestClient(app)
    c.app.state.audio_hub.vad_factory = test_answer_loop.EnergyVad
    h, lib, sid = start_session(c)
    with c.websocket_connect(f'/ws/audio/{sid}?token={h["X-Local-Token"]}') as audio:
        for seq in range(10):
            audio.send_bytes(wire(seq, True))
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert now[0] == 100.0  # 假时钟未前进：由事件即时触发，不依赖 3s 超时
    assert health['asr']['state'] == 'down' and '实时转写不可用' in health['notices']


def test_e_asr_stall_noticed_within_3_5s_fake_clock(tmp_path):
    now = [100.0]
    app = create_app(tmp_path, asr_factory=lambda s: MockASR([], s), clock=lambda: now[0])
    c = TestClient(app)
    c.app.state.audio_hub.vad_factory = test_answer_loop.EnergyVad
    h, lib, sid = start_session(c)
    with c.websocket_connect(f'/ws/audio/{sid}?token={h["X-Local-Token"]}') as audio:
        for seq in range(20):
            audio.send_bytes(wire(seq, True))
        monitor = app.state.wiring.sessions[sid].monitor
        now[0] += 3.5
        monitor.check()
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert health['asr']['state'] == 'degraded' and '实时转写停滞' in health['notices']


def test_no_asr_backend_marks_asr_down_with_mock(tmp_path):
    app = create_app(tmp_path)  # conftest 已让本机 sherpa 模型不可见，且无 Key
    c = TestClient(app)
    h, lib, sid = start_session(c)
    app.state.audio_hub.pipeline(sid)
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert health['asr']['state'] == 'down' and '实时转写不可用' in health['notices']
    assert app.state.wiring.asr_backend[sid] == 'none' and c.get('/api/status').json()['asr_backend'] == 'none'


# ---------- 12(f) 线下无声纹模型 ----------
def test_f_offline_without_voice_model_marks_voice_degraded(tmp_path):
    app = create_app(tmp_path, asr_factory=lambda s: MockASR([], s),
                     config={'voice_model': tmp_path / 'missing.onnx'})
    c = TestClient(app)
    h, lib, sid = start_session(c, 'offline')
    pipe = app.state.audio_hub.pipeline(sid)
    assert pipe.room_tracker.degraded
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert health['voice']['state'] == 'down' and health['role']['state'] == 'degraded'
    assert '角色识别不确定' in health['notices']


def test_offline_degraded_tracker_still_runs_question_flow(tmp_path):
    script = [(900, Q_TX, False, 1000), (1000, Q_TX, True, 1000)]
    app = create_app(tmp_path, asr_factory=lambda s: MockASR(script, s),
                     config={'voice_model': tmp_path / 'missing.onnx'})
    app.state.audio_hub.vad_factory = test_answer_loop.EnergyVad
    c = TestClient(app)
    h, lib, sid = start_session(c, 'offline')
    with c.websocket_connect(f'/ws/audio/{sid}?token={h["X-Local-Token"]}') as audio:
        for seq in range(70):
            audio.send_bytes(wire_room(seq, seq < 50))
        for _ in range(7):
            audio.receive_json()  # 等 ACK = 帧已处理完
    pipe = app.state.audio_hub.pipelines[sid]
    assert 'committed' in [e.type for e in pipe.turn_events]
    assert pipe.role_events and pipe.role_events[-1]['role'] == 'questioner' and 'degraded' in pipe.role_events[-1]['flags']
    rows = app.state.record_store.rows("SELECT type FROM turn_events WHERE session_id=? AND type='speaker_segment'", (sid,))
    assert rows


def wire_room(seq, speech):
    from app.audio.frame import pack
    from test_answer_loop import SILENCE, SPEECH
    return pack('room', 1, 0, seq, SPEECH if speech else SILENCE, t_client_us=seq * 20000)


# ---------- 12(g) 外发开关 / ASR 选择 ----------
class SpyAliyun:
    made = []

    def __init__(self, **kw):
        self.kw = kw
        SpyAliyun.made.append(self)


@pytest.fixture
def spy(monkeypatch):
    SpyAliyun.made = []
    import app.wiring
    monkeypatch.setattr(app.wiring, 'AliyunRealtimeASR', SpyAliyun)
    return SpyAliyun


def with_key(tmp_path):
    return create_app(tmp_path, credentials=CredentialStore([MemoryBackend({'DASHSCOPE_API_KEY': 'sk-fake-not-real-0000'})]))


def test_cloud_asr_used_with_key_and_hotwords_from_library(tmp_path, spy):
    app = with_key(tmp_path)
    c = TestClient(app)
    h, lib, sid = start_session(c)
    factory = app.state.wiring.asr_factory(sid)
    asr = factory('meeting')
    inner = asr._inner if isinstance(asr, MeteredASR) else asr
    assert isinstance(inner, SpyAliyun) and inner.kw['source'] == 'meeting'
    assert inner.kw['hotwords'] and any('Redis' in w for w in inner.kw['hotwords'])
    assert app.state.wiring.asr_backend[sid] == 'aliyun'


def test_g_outbound_asr_audio_false_never_creates_cloud_asr(tmp_path, spy):
    app = with_key(tmp_path)
    c = TestClient(app)
    h = token_of(c)
    assert c.put('/api/config', json={'outbound': {'asr_audio': False}}, headers=h).status_code == 200
    h, lib, sid = start_session(c)
    factory = app.state.wiring.asr_factory(sid)
    assert not isinstance(factory('meeting'), SpyAliyun) and spy.made == []
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert any('外发开关' in n for n in health['notices']) and health['asr']['state'] == 'down'  # 本机无本地模型 -> Mock + down
    assert app.state.wiring.asr_backend[sid] == 'none'
    assert json.loads(column(c, sid))['outbound']['asr_audio'] is False
    assert app.state.wiring.snapshot(sid)['outbound'] == {'asr_audio': False, 'asr_answerer': False,
                                                          'llm_snippets': True, 'web_questions': True}


def test_outbound_flag_change_mid_session_does_not_enable_cloud(tmp_path, spy):
    app = with_key(tmp_path)
    c = TestClient(app)
    h = token_of(c)
    c.put('/api/config', json={'outbound': {'asr_audio': False}}, headers=h)
    h, lib, sid = start_session(c)
    c.put('/api/config', json={'outbound': {'asr_audio': True}}, headers=h)
    app.state.wiring.asr_factory(sid)('meeting')
    assert spy.made == []


def test_ctx_exposes_outbound_flags_for_answer_loop(tmp_path):
    ctx = create_app(tmp_path).state.ctx
    assert ctx.config['outbound'] == {'asr_audio': True, 'asr_answerer': False, 'llm_snippets': True,
                                       'web_questions': True}
    assert ctx.root == ctx.store.root and ctx.bus is not None and ctx.token


def test_live_llm_config_and_key_reach_answer_loop(tmp_path):
    creds = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': 'sk-' + 'x' * 20})])
    app = create_app(tmp_path, credentials=creds, asr_factory=lambda s: MockASR([], s))
    c = TestClient(app)
    h = token_of(c)
    assert c.put('/api/config', json={'model': {'base_url': 'https://api.example.test/v1'},
                                      'timeouts': {'llm_first_ms': 1000, 'llm_total_ms': 4000}}, headers=h).status_code == 200
    h, lib, sid = start_session(c)
    app.state.audio_hub.pipeline(sid)
    ext = app.state.answer_loops[sid].ext
    assert ext.llm_live and ext.llm_transport is None  # 无注入传输时走 httpx 默认真实传输
    assert ext.llm_base_url == 'https://api.example.test/v1'
    assert ext.llm_first_timeout_s == 1.0 and ext.llm_timeout_s == 4.0


# ---------- 11. 种子岗位别名 ----------
def test_secondary_pm_and_product_are_same_role(tmp_path):
    c = TestClient(create_app(tmp_path))
    h = token_of(c)
    assert c.post('/api/secondary/import', headers=h).status_code == 200
    from app.library.secondary import SecondaryLibrary
    lib = SecondaryLibrary(c.app.state.store)
    assert {r['role'] if isinstance(r, dict) else r for r in lib.roles()} >= {'pm', 'product'}
    as_ids = lambda roles: {e['id'] for e in lib.candidates(roles, include_pending=True)}  # noqa: E731
    assert as_ids(['pm']) == as_ids(['product']) and as_ids(['pm'])


# ---------- 其它接线 ----------
def test_probes_only_exist_when_keys_present_and_missing_keys_mark_down(tmp_path):
    app = create_app(tmp_path)
    assert app.state.wiring._probes() == {}
    keyed = create_app(tmp_path / 'k', credentials=CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': 'sk-abcdefghijklmnop'})]))  # pragma: allowlist secret
    assert set(keyed.state.wiring._probes()) == {'llm', 'search'}
    rt = keyed.state.wiring.health('sid')  # 有 Key：不预先标 down
    assert rt.board.state('llm')['state'] == 'ok' and rt.board.state('search')['state'] == 'ok'


def test_module_level_app_is_lazy(monkeypatch):
    import app.server as server
    monkeypatch.delitem(vars(server), 'app', raising=False)
    monkeypatch.setattr(server, 'create_app', lambda: 'sentinel')
    assert server.app == 'sentinel'
    monkeypatch.delitem(vars(server), 'app', raising=False)


def test_record_view_withdrawn_clears_pending_recommit():
    """P1-B：committed Q1 -> rolled_back -> withdrawn -> committed Q2 必须记到 t2。"""
    from app.wiring import Wiring

    w = Wiring.__new__(Wiring)
    w._turn_state = {}
    ev = lambda t, p: dict(type=t, payload=p)  # noqa: E731
    w._record_view('s', ev('turn_state', dict(state='question_committed')))
    w._record_view('s', ev('turn_rollback', {}))
    w._record_view('s', ev('candidate', dict(withdrawn=True)))
    assert w._turn_state['s'] == [1, False]
    w._record_view('s', ev('turn_state', dict(state='question_committed')))
    assert w._turn_state['s'] == [2, False]
