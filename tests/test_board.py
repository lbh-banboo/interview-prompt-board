"""M11 / M05-T2 / M01-5：board 事件总线、WS 订阅与重放、display_ack、快照、超时停止。"""
import json
import struct
import math
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.asr.mock import MockASR
from app.audio.frame import pack
from app.audio.vad import EnergyVad
from app.domain.turn_detector import TurnEvent
from app.realtime.events import Activity, EventBus
from app.server import create_app
from app.telemetry.metrics import Metrics
from test_api import prepared

OUTLINE = dict(conclusion='先说结论', points=['一', '二', '三'], examples='例子', cautions='注意')


def te(type_, text='你能讲讲分布式事务的区别吗', ver=1, reason='r', **kw):
    return TurnEvent(type_, text, ver, 1000, reason, **kw)


class FakePipe:
    def __init__(self):
        self.turn_events = []
        self.detector = SimpleNamespace(phase='idle', answerer_active=False)
        self._health = {'state': 'ok', 'reason': ''}

    def health(self):
        return self._health


def types(events):
    return [e['type'] for e in events]


# ---------- EventBus ----------
def test_event_seq_monotonic_and_epoch_per_committed():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events += [te('candidate'), te('committed')]
    bus.pull('s', pipe)
    first = bus.replay('s', 0)
    assert [e['event_seq'] for e in first] == list(range(1, len(first) + 1))
    assert first[-1]['epoch'] == 1 and first[-1]['turn_id'] == 't1'
    pipe.turn_events.append(te('committed', text='换个问题：你们怎么做缓存'))
    bus.pull('s', pipe)
    later = bus.replay('s', first[-1]['event_seq'])
    assert 'interrupt' in types(later)  # 前一题未结束：被新题打断
    last = later[-1]
    assert (last['epoch'], last['turn_id']) == (2, 't2')
    seqs = [e['event_seq'] for e in bus.replay('s', 0)]
    assert seqs == sorted(set(seqs))


def test_pull_is_idempotent_by_cursor():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events.append(te('committed'))
    bus.pull('s', pipe)
    n = bus.snapshot('s')['event_seq']
    bus.pull('s', pipe)
    bus.pull('s', pipe)
    assert bus.snapshot('s')['event_seq'] == n


def test_rollback_keeps_turn_and_recommit_bumps_epoch_only():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events += [te('committed', ver=1), te('rolled_back', ver=2, reason='final_changed:x'),
                         te('committed', text='改写后的问题', ver=2)]
    bus.pull('s', pipe)
    evs = bus.replay('s', 0)
    rb = next(e for e in evs if e['type'] == 'turn_rollback')
    assert rb['payload']['from_version'] == 1 and rb['payload']['to_version'] == 2
    snap = bus.snapshot('s')
    assert snap['turn_id'] == 't1' and snap['epoch'] == 2
    assert snap['question']['text'] == '改写后的问题' and snap['question']['corrected'] is True
    assert 'interrupt' not in types(evs)


def test_rollback_then_answer_closed_then_new_question_gets_new_turn():
    """回滚留下的 pending_recommit 不能跨过回答结束：下一题必须是新 turn_id，否则会串题。"""
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events += [te('committed', ver=1), te('rolled_back', ver=2, reason='final_changed:x'),
                         te('answer_closed'), te('committed', text='那你们的缓存策略是什么', ver=1)]
    bus.pull('s', pipe)
    snap = bus.snapshot('s')
    assert (snap['turn_id'], snap['epoch']) == ('t2', 2)
    assert snap['question']['text'] == '那你们的缓存策略是什么'


def test_late_prompt_dropped_by_bus_old_epoch_and_wrong_turn():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events.append(te('committed'))
    bus.pull('s', pipe)
    pipe.turn_events.append(te('committed', text='第二个问题'))
    bus.pull('s', pipe)
    seq = bus.snapshot('s')['event_seq']
    assert bus.publish_prompt_primary('s', 't1', 1, OUTLINE, 'L1', '主库') is None  # 旧题迟到
    assert bus.publish_prompt_primary('s', 't2', 1, OUTLINE, 'L1', '主库') is None  # epoch 不一致
    assert bus.publish_prompt_primary('s', 't1', 2, OUTLINE, 'L1', '主库') is None  # turn_id 不一致
    assert bus.snapshot('s')['event_seq'] == seq and bus.snapshot('s')['primary'] is None
    assert bus.stats('s')['dropped'] == 3
    ok = bus.publish_prompt_primary('s', 't2', 2, OUTLINE, 'L1', '主库')
    assert ok['payload']['prompt_id'] and bus.snapshot('s')['primary']['outline'] == OUTLINE


def test_primary_locked_supplements_append_and_closed_turn_rejects():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events.append(te('committed'))
    bus.pull('s', pipe)
    assert bus.publish_prompt_primary('s', 't1', 1, OUTLINE, 'L1', 'a')
    assert bus.publish_prompt_primary('s', 't1', 1, dict(OUTLINE, conclusion='改了'), 'L1', 'b') is None
    assert bus.snapshot('s')['primary']['outline']['conclusion'] == '先说结论'
    for i in range(3):
        assert bus.publish_supplement('s', 't1', 1, '模型补充', f'补充{i}')
    assert [x['content'] for x in bus.snapshot('s')['supplements']] == ['补充0', '补充1', '补充2']
    bus.close_turn('s')
    assert bus.publish_supplement('s', 't1', 1, '模型补充', '迟到') is None
    assert bus.stats('s')['locked_dropped'] == 1


def test_history_keeps_last_n_only():
    bus = EventBus(keep=5)
    for i in range(12):
        bus.publish_queue('s', [str(i)])
    assert [e['event_seq'] for e in bus.replay('s', 0)] == [8, 9, 10, 11, 12]


def test_answering_phase_and_health_and_source_state():
    clock = [100.0]
    act = Activity(lambda: clock[0])
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events.append(te('committed'))
    act.touch('s', 'meeting')
    bus.pull('s', pipe, act)
    pipe.detector.phase = 'answering'
    pipe._health = {'state': 'reconnecting', 'reason': 'x'}
    clock[0] += 5
    bus.pull('s', pipe, act)
    snap = bus.snapshot('s')
    assert snap['status'] == 'answering'
    assert snap['health']['asr']['state'] == 'reconnecting'
    assert snap['sources']['meeting']['state'] == 'lost'


# ---------- WS / 快照 / ack ----------
@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def url(c, sid, since=None):
    t = c.get('/api/status').json()['token']
    return f'/ws/board/{sid}?token={t}' + ('' if since is None else f'&since={since}')


def commit(c, sid, text='你能讲讲分布式事务的区别吗'):
    pipe = FakePipe()
    pipe.turn_events.append(te('committed', text=text))
    c.app.state.bus.pull(sid, pipe)


def test_ws_replays_since_and_streams_live(client):
    h, lib, sid = prepared(client)
    bus = client.app.state.bus
    commit(client, sid)
    first = bus.snapshot(sid)['event_seq']
    bus.publish_prompt_primary(sid, 't1', 1, OUTLINE, 'L1', '主库')
    with client.websocket_connect(url(client, sid, since=first)) as ws:
        ev = ws.receive_json()
        assert ev['type'] == 'prompt_primary' and ev['event_seq'] == first + 1
        assert set(ev) == {'type', 'session', 'turn_id', 'epoch', 'event_seq', 'text_version', 't_server', 'payload'}
        bus.publish_supplement(sid, 't1', 1, '联网', '实时补充', url='https://example.org', elapsed_ms=800)
        live = ws.receive_json()
        assert live['type'] == 'prompt_supplement' and live['event_seq'] == first + 2


def test_ws_without_since_does_not_replay_history(client):
    h, lib, sid = prepared(client)
    bus = client.app.state.bus
    commit(client, sid)
    with client.websocket_connect(url(client, sid)) as ws:
        bus.publish_queue(sid, ['下一题'])
        assert ws.receive_json()['type'] == 'queue_update'


def test_display_ack_writes_metric(client):
    h, lib, sid = prepared(client)
    bus = client.app.state.bus
    commit(client, sid)
    ev = bus.publish_prompt_primary(sid, 't1', 1, OUTLINE, 'L1', '主库')
    with client.websocket_connect(url(client, sid, since=ev['event_seq'] - 1)) as ws:
        got = ws.receive_json()
        ws.send_json({'type': 'display_ack', 'prompt_id': got['payload']['prompt_id'], 't_visible': 1234.5})
        ws.send_json({'type': 'display_ack', 'prompt_id': 'unknown', 't_visible': 1})
        ws.send_text('not json')
        bus.publish_queue(sid, [])
        ws.receive_json()  # 往返一次，保证 ack 已处理
    store = client.app.state.store
    m = Metrics(store.path)
    rows = [r for r in m.rows(sid) if r[2] == 'display_ack']
    m.conn.close()
    assert len(rows) == 1 and rows[0][1] == 't1'
    assert '"t_visible": 1234.5' in rows[0][4] and got['payload']['prompt_id'] in rows[0][4]


def test_status_and_supplement_prompts_are_ackable(client):
    """status / supplement 也要带 prompt_id，ACK 能回到正确轮次；快照保留 id 供刷新后复用。"""
    h, lib, sid = prepared(client)
    bus = client.app.state.bus
    commit(client, sid)
    st = bus.publish_prompt_status(sid, 't1', 1, '外部补充不可用')
    sp = bus.publish_supplement(sid, 't1', 1, '联网', '补充一条')
    assert st['payload']['prompt_id'].startswith('p') and sp['payload']['prompt_id'].startswith('p')
    assert st['payload']['prompt_id'] != sp['payload']['prompt_id']
    with client.websocket_connect(url(client, sid, since=st['event_seq'] - 1)) as ws:
        a, b = ws.receive_json(), ws.receive_json()
        assert a['type'] == 'prompt_status' and b['type'] == 'prompt_supplement'
        ws.send_json({'type': 'display_ack', 'prompt_id': a['payload']['prompt_id'], 't_visible': 111.0})
        ws.send_json({'type': 'display_ack', 'prompt_id': b['payload']['prompt_id'], 't_visible': 222.0})
        bus.publish_queue(sid, [])
        ws.receive_json()
    m = Metrics(client.app.state.store.path)
    rows = [r for r in m.rows(sid) if r[2] == 'display_ack']
    m.conn.close()
    got = {json.loads(r[4])['prompt_id']: r[1] for r in rows}
    assert got == {a['payload']['prompt_id']: 't1', b['payload']['prompt_id']: 't1'}
    snap = bus.snapshot(sid)
    assert snap['prompt_status_id'] == a['payload']['prompt_id']
    assert snap['supplements'][-1]['prompt_id'] == b['payload']['prompt_id']
    client.app.state.wiring.on_pull(sid, None)  # 记录库 pull 一次
    rec = client.app.state.record_store
    got = {r['block']: r for r in rec.rows('SELECT block,state,display_ack_at FROM prompts WHERE session_id=?', (sid,))}
    assert got['status']['state'] == 'acked' and got['supplement']['state'] == 'acked'
    assert got['status']['display_ack_at'] > 0 and got['supplement']['display_ack_at'] > 0


def test_snapshot_contents_and_resume_without_replaying_old_events(client):
    h, lib, sid = prepared(client)
    bus = client.app.state.bus
    commit(client, sid)
    bus.publish_prompt_primary(sid, 't1', 1, OUTLINE, 'L1', '主库')
    bus.publish_supplement(sid, 't1', 1, '模型补充', '追加一条')
    bus.publish_queue(sid, ['后续问题'])
    snap = client.get(f'/api/sessions/{sid}/snapshot').json()
    assert {'session_id', 'turn_id', 'epoch', 'event_seq', 'status', 'primary', 'supplements', 'queue', 'health'} <= set(snap)
    assert (snap['session_id'], snap['turn_id'], snap['epoch']) == (sid, 't1', 1)
    assert snap['status'] == 'question_committed' and snap['queue'] == ['后续问题']
    assert snap['primary']['outline'] == OUTLINE and snap['supplements'][0]['content'] == '追加一条'
    assert snap['question']['text'] == '你能讲讲分布式事务的区别吗' and snap['session_status'] == 'active'
    # 刷新后：以快照 event_seq 订阅，只收到快照之后的事件
    bus.publish_supplement(sid, 't1', 1, '联网', '快照之后')
    with client.websocket_connect(url(client, sid, since=snap['event_seq'])) as ws:
        ev = ws.receive_json()
    assert ev['event_seq'] == snap['event_seq'] + 1 and ev['payload']['content'] == '快照之后'
    assert client.get('/api/sessions/nope/snapshot').status_code == 404


def test_snapshot_after_stop_is_ended(client):
    h, lib, sid = prepared(client)
    commit(client, sid)
    client.post(f'/api/sessions/{sid}/stop', json={}, headers=h)
    snap = client.get(f'/api/sessions/{sid}/snapshot').json()
    assert snap['status'] == 'ended' and snap['session_status'] == 'ended'
    assert client.app.state.bus.publish_prompt_primary(sid, 't1', 1, OUTLINE, 'L1', 'x') is None


def _frame(source, seq, speech):
    pcm = struct.pack('<320h', *[int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(320)]) if speech else b'\0' * 640
    return pack(source, 1, 0, seq, pcm, t_client_us=seq * 20000)


def test_real_pipeline_frames_become_board_events(tmp_path):
    script = [(900, '你能讲讲分布式事务的区别吗', False, 1000), (1000, '你能讲讲分布式事务的区别吗', True, 1000)]
    c = TestClient(create_app(tmp_path, asr_factory=lambda source: MockASR(script, source)))
    c.app.state.audio_hub.vad_factory = EnergyVad  # 合成正弦波用能量 VAD
    h, lib, sid = prepared(c)
    token = h['X-Local-Token']
    with c.websocket_connect(f'/ws/board/{sid}?token={token}&since=0') as board, \
            c.websocket_connect(f'/ws/audio/{sid}?token={token}') as audio:
        for seq in range(65):
            audio.send_bytes(_frame('meeting', seq, seq < 50))
        seen = []
        while 'transcript' not in seen:
            seen.append(board.receive_json()['type'])
    assert 'turn_state' in seen
    snap = c.get(f'/api/sessions/{sid}/snapshot').json()
    assert snap['epoch'] == 1 and snap['status'] == 'question_committed'
    assert snap['sources']['meeting']['state'] == 'ok'


# ---------- M01-5 超时停止 ----------
def test_idle_timeout_stops_session_with_injected_clock(tmp_path):
    now = [1000.0]
    c = TestClient(create_app(tmp_path, clock=lambda: now[0], idle_timeout_s=7200))
    h, lib, sid = prepared(c)
    token = h['X-Local-Token']
    with c.websocket_connect(f'/ws/audio/{sid}?token={token}') as audio:
        audio.send_bytes(_frame('mic', 0, False))
        audio.send_bytes(_frame('mic', 1, False))
    now[0] += 7000  # 刷新页面：快照 + status，会话仍 active
    assert c.get(f'/api/sessions/{sid}/snapshot').json()['session_status'] == 'active'
    assert c.get('/api/status').json()['active_session']['id'] == sid
    with c.websocket_connect(f'/ws/audio/{sid}?token={token}') as audio:
        audio.send_bytes(_frame('mic', 2, False))  # 新的帧刷新计时
    now[0] += 7000
    assert c.get('/api/status').json()['active_session']['id'] == sid
    now[0] += 201  # 距最近一帧 > 7200s
    assert c.get('/api/status').json()['active_session'] is None
    snap = c.get(f'/api/sessions/{sid}/snapshot').json()
    assert snap['session_status'] == 'ended' and snap['status'] == 'ended'
    assert c.app.state.bus.replay(sid, 0)[-1]['payload']['state'] == 'ended'


def test_text_updated_becomes_non_final_transcript_same_turn():
    bus, pipe = EventBus(), FakePipe()
    pipe.turn_events.append(te('committed', text='你能讲讲分布式', ver=1))
    bus.pull('s', pipe)
    snap = bus.snapshot('s')
    pipe.turn_events.append(te('text_updated', text='你能讲讲分布式事务', ver=2, reason='catchup', kind='request'))
    bus.pull('s', pipe)
    ev = bus.replay('s', snap['event_seq'])
    assert types(ev) == ['transcript']
    e = ev[0]
    assert e['turn_id'] == snap['turn_id'] and e['epoch'] == snap['epoch'] and e['text_version'] == 2
    assert e['payload']['is_final'] is False and e['payload']['text'] == '你能讲讲分布式事务'
    assert e['payload']['role'] == 'questioner' and e['payload']['kind'] == 'request'
    assert bus.snapshot('s')['question']['text'] == '你能讲讲分布式'  # 非 final 不覆盖已提交问题
