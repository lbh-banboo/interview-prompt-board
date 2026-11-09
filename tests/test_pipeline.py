"""M05-4 会话编排管线：VAD -> ASR -> TurnDetector -> 埋点。全部合成帧 + MockASR，不读墙钟。"""
import asyncio
from types import SimpleNamespace
from contextlib import closing
import math
import sqlite3
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from app.asr.mock import MockASR
from app.audio.frame import pack, parse
from app.audio.vad import EnergyVad
from app.realtime.events import EventBus
from app.realtime.pipeline import SessionPipeline
from app.server import create_app
from app.telemetry.metrics import Metrics
from test_api import prepared

HIGH = '你能讲讲分布式事务的区别吗'
LOW = '我想问一下你在这个项目里'
SPEECH = struct.pack('<320h', *[int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(320)])
SILENCE = b'\x00' * 640


def wire(source, seq, speech, generation=0, replay=False, stream_id=1):
    return pack(source, stream_id, generation, seq, SPEECH if speech else SILENCE, t_client_us=seq * 20000, replay=replay)


def frame(source, seq, speech, **kw):
    return parse(wire(source, seq, speech, **kw))


def energy(source):
    return EnergyVad(source)


class Env:
    def __init__(self, script=(), mic_script=(), asr_factory=None, backoffs=(), answerer_asr=False,
                 mic_asr_cls=None):
        self.conn = sqlite3.connect(':memory:')
        self.metrics = Metrics(self.conn)
        self.created = []

        def factory(source):
            cls = mic_asr_cls if source == 'mic' and mic_asr_cls else MockASR
            asr = cls(mic_script if source == 'mic' else script, source)
            self.created.append(asr)
            return asr
        self.pipe = SessionPipeline('s1', None, asr_factory or factory, self.metrics,
                                    vad_factory=energy, reconnect_backoffs=backoffs,
                                    answerer_asr=answerer_asr)

    async def feed(self, source, start, stop, speech, **kw):
        for seq in range(start, stop):
            await self.pipe.on_frame(frame(source, seq, speech(seq) if callable(speech) else speech, **kw))

    async def finish(self):
        await self.pipe.close()
        rows = self.metrics.rows('s1')
        self.conn.close()
        return rows


async def question_run(script=((900, HIGH, False, 1000),), silence_frames=15):
    """meeting 说话 0~1000ms（50 帧），随后静音。"""
    env = Env(script)
    await env.feed('meeting', 0, 50, True)
    await env.feed('meeting', 50, 50 + silence_frames, False)
    rows = await env.finish()
    return env, rows


@pytest.mark.asyncio
async def test_question_then_silence_commits_with_exact_latency():
    env, rows = await question_run()
    assert [e.type for e in env.pipe.turn_events] == ['candidate', 'committed']
    cand, com = env.pipe.turn_events
    # VAD 最后语音窗口结束于 1000；静音 >= T_silence(200) 后的第一帧边界 now=1200 提交
    assert (com.t_speech_end_ms, com.t_ms, com.reason) == (1000, 1200, 'high_completeness')
    assert cand.t_ms == 1200 and com.turn_text == HIGH
    names = [r[2] for r in rows]
    assert names == ['candidate', 'committed']
    import json
    assert json.loads(rows[1][4])['latency_ms'] == 200 and rows[1][1] == 't1'
    assert all(r[3] > 0 for r in rows)  # perf_counter_ns


@pytest.mark.asyncio
async def test_replay_frames_do_not_touch_vad_asr_or_turns():
    env = Env(((900, HIGH, False, 1000),))
    await env.feed('meeting', 0, 50, True, replay=True)
    await env.feed('meeting', 50, 65, False, replay=True)
    assert env.created == [] and env.pipe.turn_events == [] and env.pipe._routes == {}
    rows = await env.finish()
    assert rows == []


def test_replay_frames_still_recorded_over_ws(tmp_path):
    made = []
    app = create_app(tmp_path, asr_factory=lambda s: made.append(s) or MockASR([], s))
    app.state.audio_hub.vad_factory = energy
    client = TestClient(app)
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        for seq in range(20):
            ws.send_bytes(wire('meeting', seq, True, replay=True))
        assert [ws.receive_json()['ack_seq'] for _ in range(2)] == [9, 19]  # 等服务端处理完再断开
    client.post(f'/api/sessions/{sid}/stop', json={}, headers=h)
    assert made == []
    rec = client.get(f'/api/sessions/{sid}/recordings').json()
    assert len(rec) == 1
    assert len(client.get(f'/api/recordings/{rec[0]["id"]}').content) == 44 + 20 * 640
    with closing(app.state.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='metrics'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_new_generation_resets_route_state():
    env = Env(((900, HIGH, False, 1000),))
    await env.feed('meeting', 0, 50, True)  # gen0 仍在说话
    first_vad = env.pipe._routes['meeting'].vad
    assert first_vad.speaking
    await env.feed('meeting', 0, 3, False, generation=1)  # 刷新后 seq 归零
    route = env.pipe._routes['meeting']
    assert route.vad is not first_vad and not route.vad.speaking and route.key == (1, 1)
    assert len(env.created) == 2 and env.created[0]._closed and not env.created[1]._closed
    assert route.base_ms == 1000  # TurnDetector 时间轴保持单调
    # 旧 generation 迟到 live 帧被丢弃，不污染新路由
    await env.feed('meeting', 3, 6, True, generation=0)
    assert env.pipe._routes['meeting'] is route and not route.vad.speaking
    # 旧路由被 flush：speech_end 送到 TurnDetector（t_end=1000）
    assert env.pipe.detector._t_end == 1000
    await env.finish()


@pytest.mark.asyncio
async def test_transcript_sentence_key_stable_within_sentence():
    """TurnDetector 以 t_audio_start_ms 作句子 key：同一句的所有 partial/final 必须相同（含经 ReconnectingASR）。"""
    script = [(900, '你能讲讲', False, 900), (1000, '你能讲讲分布式事务', False, 1000),
              (1100, HIGH, True, 1100), (1500, '第二句话', False, 1500)]
    env = Env(script)
    await env.feed('meeting', 0, 80, True)
    keys = {}
    for ev in env.pipe.detector._sent:
        keys[ev] = True
    assert sorted(keys) == [900, 1500]  # 第一句只有一个 key（= 该句首个 partial 的脚本时刻）
    assert env.pipe.detector._sent[900]['text'] == HIGH and env.pipe.detector._sent[900]['final']
    await env.finish()


@pytest.mark.asyncio
async def test_asr_down_never_fabricates_committed_and_reports_health():
    def broken(source):
        asr = MockASR([(900, HIGH, False, 1000)], source)
        asr.inject_disconnect('boom')
        return asr
    env = Env(asr_factory=broken)
    await env.feed('meeting', 0, 50, True)
    await env.feed('meeting', 50, 120, False)
    assert env.pipe.health()['state'] == 'down' and 'boom' in env.pipe.health()['reason']
    assert [e for e in env.pipe.turn_events if e.type in ('committed', 'candidate')] == []
    assert await env.finish() == []


@pytest.mark.asyncio
async def test_answerer_speech_on_mic_commits_pending_question():
    env = Env(((900, LOW, False, 1000),))
    for seq in range(80):
        await env.pipe.on_frame(frame('meeting', seq, seq < 50))
        await env.pipe.on_frame(frame('mic', seq, 65 <= seq < 80, stream_id=2))
    evs = env.pipe.turn_events
    assert [e.type for e in evs] == ['candidate', 'committed']
    assert evs[0].reason.startswith('silence>=T_silence')  # 低完整度：先只有候选，未提交
    assert evs[1].reason == 'answerer_active' and evs[1].t_ms == 1360
    assert env.pipe.detector.phase == 'answering'
    assert [r[2] for r in await env.finish()] == ['candidate', 'committed']
    assert len(env.created) == 1  # mic 通道不做 ASR


@pytest.mark.asyncio
async def test_answerer_asr_transcript_only_for_export(tmp_path):
    env = Env(script=((900, HIGH, True, 1000),), mic_script=((1300, '我负责后端', True, 1320),),
              answerer_asr=True)
    for seq in range(70):
        await env.pipe.on_frame(frame('meeting', seq, seq < 50))
        await env.pipe.on_frame(frame('mic', seq, 60 <= seq < 70, stream_id=2))
    assert [e.type for e in env.pipe.turn_events] == ['candidate', 'committed']
    assert all(e.turn_text != '我负责后端' for e in env.pipe.turn_events)
    assert 1300 not in env.pipe.detector._sent  # 回答者转写绝不喂 TurnDetector
    await env.pipe.close()
    assert [(e.text, e.is_final, e.source) for e in env.pipe.answerer_events] == [('我负责后端', True, 'mic')]
    assert len(env.created) == 2  # 两路各自一条 ASR 流
    bus = EventBus()
    bus.pull('s1', env.pipe)
    got = [e['payload'] for e in bus.replay('s1', 0) if e['type'] == 'transcript' and e['payload'].get('role') == 'answerer']
    assert got == [dict(source='mic', role='answerer', text='我负责后端', is_final=True)]


@pytest.mark.asyncio
async def test_answerer_asr_push_does_not_block_questioner_path():
    gate = asyncio.Event()

    class Gated(MockASR):
        async def push(self, pcm, t_ms):
            await gate.wait()
            await super().push(pcm, t_ms)

    env = Env(script=((900, HIGH, True, 1000),), mic_script=((100, '稍等', True, 100),),
              answerer_asr=True, mic_asr_cls=Gated)
    for seq in range(70):
        await env.pipe.on_frame(frame('meeting', seq, seq < 50))
        await env.pipe.on_frame(frame('mic', seq, 60 <= seq < 70, stream_id=2))
    assert [e.type for e in env.pipe.turn_events] == ['candidate', 'committed']  # 面试官通道未被慢 ASR 阻塞
    gate.set()
    await env.pipe.close()
    assert [e.text for e in env.pipe.answerer_events] == ['稍等']


@pytest.mark.asyncio
async def test_answerer_asr_default_off_keeps_single_asr(tmp_path):
    env = Env(script=((900, HIGH, True, 1000),))
    await env.feed('meeting', 0, 50, True)
    await env.feed('mic', 60, 70, True, stream_id=2)
    await env.pipe.close()
    assert len(env.created) == 1 and env.pipe.answerer_events == []


@pytest.mark.asyncio
async def test_same_input_same_output():
    a, rows_a = await question_run()
    b, rows_b = await question_run()
    assert a.pipe.turn_events == b.pipe.turn_events
    assert [r[2] for r in rows_a] == [r[2] for r in rows_b]
    assert [r[4] for r in rows_a] == [r[4] for r in rows_b]


def test_metric_turn_restarts_after_answer_closed_even_after_rollback():
    """回滚后的 pending 重提交不能跨过回答结束：指标表中的下一题必须是新 turn_id。"""
    pipe = SessionPipeline.__new__(SessionPipeline)
    pipe._turn_n, pipe._recommit = 0, False
    ev = lambda t: SimpleNamespace(type=t)
    assert pipe._metric_turn(ev('committed')) == 't1'
    assert pipe._metric_turn(ev('rolled_back')) == 't1'
    assert pipe._metric_turn(ev('answer_closed')) == 't1'
    assert pipe._metric_turn(ev('committed')) == 't2'
    assert pipe._metric_turn(ev('interrupted')) == 't2'
    assert pipe._metric_turn(ev('committed')) == 't3'


def test_ws_end_to_end_committed_in_metrics(tmp_path):
    app = create_app(tmp_path, asr_factory=lambda s: MockASR([(900, HIGH, False, 1000)], s))
    app.state.audio_hub.vad_factory = energy
    client = TestClient(app)
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        for seq in range(70):
            ws.send_bytes(wire('meeting', seq, seq < 50))
        assert [ws.receive_json()['ack_seq'] for _ in range(7)][-1] == 69  # TestClient 退出会取消服务端任务，先等处理完
    assert client.post(f'/api/sessions/{sid}/stop', json={}, headers=h).status_code == 200
    with closing(app.state.store.connect()) as db:
        rows = db.execute('SELECT name, value FROM metrics WHERE session_id=? ORDER BY rowid', (sid,)).fetchall()
    rows = [r for r in rows if r[0] != 'prompt_sent']  # S1b：committed 之后答案环会追加 prompt_sent
    assert [r[0] for r in rows] == ['candidate', 'committed']
    assert '"latency_ms": 200' in rows[1][1]
    assert len(client.get(f'/api/sessions/{sid}/recordings').json()) == 1
    assert app.state.audio_hub.pipelines == {}


@pytest.mark.asyncio
async def test_close_leaves_no_tasks():
    before = asyncio.all_tasks()
    env, _ = await question_run()
    assert asyncio.all_tasks() == before


def test_replay_eval_tool_report_is_simulated(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
    import run_replay_eval
    text = run_replay_eval.main(['--synth', '--close-on-commit', '--out-dir', str(tmp_path), '--name', 't'])
    assert text.splitlines()[0] == '类别: SIMULATED' and 'PASS' not in text
    assert '5/5 = 100.00%' in text and (tmp_path / 't.csv').exists() and (tmp_path / 't.md').exists()


def test_record_marks_text_updated_with_coverage_stale():
    import json
    from app.domain.turn_detector import TurnEvent
    env = Env()
    env.pipe._record([TurnEvent('committed', HIGH, 1, 1200, 'vad_primary', t_speech_end_ms=1000, coverage_stale=True),
                      TurnEvent('text_updated', HIGH + '吗', 2, 1500, 'catchup', t_speech_end_ms=1000)])
    rows = env.metrics.rows('s1')
    assert [r[2] for r in rows] == ['committed', 'text_updated']
    v0, v1 = json.loads(rows[0][4]), json.loads(rows[1][4])
    assert v0['coverage_stale'] is True and v1['coverage_stale'] is None
    assert v1['latency_ms'] == 500 and v1['reason'] == 'catchup' and v1['text_version'] == 2
    assert [e.type for e in env.pipe.turn_events] == ['committed', 'text_updated']
    env.conn.close()


@pytest.mark.asyncio
async def test_answer_closed_closes_turn_so_next_question_is_not_a_rollback():
    """已知缺口回归：回答结束后面试官问下一题，此前无人 close_turn，后续提问被当作回滚/丢失。"""
    q2 = '请你介绍一下消息队列的可靠性'
    env = Env(((900, HIGH, True, 1000), (4900, q2, True, 5000)))
    for seq in range(300):
        await env.pipe.on_frame(frame('meeting', seq, seq < 50 or 200 <= seq < 250))
        await env.pipe.on_frame(frame('mic', seq, 65 <= seq < 150, stream_id=2))
    evs = env.pipe.turn_events
    kinds = [e.type for e in evs]
    assert kinds.count('committed') == 2 and 'rolled_back' not in kinds and env.pipe.detector.premature_count == 0
    assert 'answer_end_candidate' in kinds and kinds.index('answer_closed') < len(kinds) - 1
    assert [e.turn_text for e in evs if e.type == 'committed'] == [HIGH, q2]
    closed = next(e for e in evs if e.type == 'answer_closed')
    assert closed.reason == 'new_question' and evs[kinds.index('answer_closed') + 1].type == 'committed'
    rows = await env.finish()
    by_turn = {(r[1], r[2]) for r in rows}  # (turn_id, name)
    assert ('t1', 'committed') in by_turn and ('t1', 'answer_closed') in by_turn and ('t2', 'committed') in by_turn


@pytest.mark.asyncio
async def test_pipeline_replay_of_answer_phase_is_deterministic():
    async def run():
        env = Env(((900, HIGH, True, 1000), (4900, '请你介绍一下消息队列的可靠性', True, 5000)))
        for seq in range(300):
            await env.pipe.on_frame(frame('meeting', seq, seq < 50 or 200 <= seq < 250))
            await env.pipe.on_frame(frame('mic', seq, 65 <= seq < 150, stream_id=2))
        await env.finish()
        return env.pipe.turn_events
    assert await run() == await run()


@pytest.mark.asyncio
async def test_four_unanswered_questions_become_t1_to_t4_without_accumulation():
    """81 秒同类场景的缩样：连续无人回答的提问必须逐题换 t{n}，文本不累积。"""
    qs = ['你能讲讲分布式事务的区别吗', '请介绍一下缓存方案', '你们为什么选择Kafka', '说说你们的消息队列设计']
    script = [(900 + i * 2400, q, True, 1000 + i * 2400) for i, q in enumerate(qs)]
    env = Env(script)
    await env.feed('meeting', 0, 480, lambda seq: (seq * 20) % 2400 < 1000)  # 9.6s，4 段提问
    bus = EventBus()
    bus.pull('s1', env.pipe)
    commits = [e for e in env.pipe.turn_events if e.type == 'committed']
    assert [e.turn_text for e in commits] == qs
    assert [e.type for e in env.pipe.turn_events if e.type == 'rolled_back'] == []
    transcripts = [e for e in bus.replay('s1', 0) if e['type'] == 'transcript']
    assert [e['turn_id'] for e in transcripts] == ['t1', 't2', 't3', 't4']
    assert [e['payload']['text'] for e in transcripts] == qs
    assert [e['payload']['source'] for e in transcripts] == ['meeting'] * 4
    await env.finish()
