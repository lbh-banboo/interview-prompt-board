"""线上单麦门控：只有 mic 帧时用声纹区分提问方/回答方/unknown，meeting 后到必须切回双通道。

全部合成帧 + MockASR + 假 tracker，不联网、不外发音频、不读墙钟。
"""
import asyncio
import json
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.asr.metered import MeteredASR
from app.asr.mock import MockASR
from app.audio.echo import EchoDetector
from app.audio.enroll import VoiceProfileStore
from app.audio.voiceprint import FakeExtractor
from app.realtime.pipeline import SessionPipeline
from app.server import create_app
from app.telemetry.metrics import Metrics
from test_asr_usage import Inner, ONE_SECOND, asr_rows, guard, wiring
from test_integration_wiring import start_session
from test_pipeline import energy, frame

Q = '你能讲讲分布式事务的区别吗'


class FakeTracker:
    """按脚本给出角色；on_speech_end 给最终角色。与 SpeakerRoleTracker 的鸭子接口一致。"""
    degraded = False
    degraded_reason = None

    def __init__(self, role, resolve_at=30):
        self.role, self.resolve_at, self.n = role, resolve_at, 0
        self.in_answer = False
        self.last_role = None
        self.resets = 0

    def reset(self):
        self.in_answer = False
        self.last_role = None
        self.n = 0
        self.resets += 1

    def on_speech_start(self, t_ms):
        self.t0, self.n = t_ms, 0

    def feed(self, pcm):
        self.n += 1
        if self.n == self.resolve_at:
            return [self.seg(False, self.t0 + 20 * self.n)]
        return []

    def on_speech_end(self, t_ms):
        if self.role != 'unknown':
            self.last_role = self.role
        return self.seg(True, t_ms)

    def seg(self, final, t_end):
        return dict(source='mic', role=self.role, confidence=0.9, t_start=self.t0, t_end=t_end, inherited=False,
                    flags=[], final=final, latency_ms=800)


def make(tracker=None, script=(), *, mic_gate=None, **kw):
    conn = sqlite3.connect(':memory:')
    metrics = Metrics(conn)

    def factory(source):
        return MockASR(script if source in ('mic', 'meeting') else [], source)

    pipe = SessionPipeline('s1', None, factory, metrics, mode='online', vad_factory=energy,
                           room_tracker=tracker, mic_gate=(tracker is not None if mic_gate is None else mic_gate), **kw)
    return pipe, metrics, conn


async def feed(pipe, source, start, stop, speech, stream_id=1):
    for seq in range(start, stop):
        await pipe.on_frame(frame(source, seq, speech(seq) if callable(speech) else speech, stream_id=stream_id))


def committed(pipe):
    return [e.turn_text for e in pipe.turn_events if e.type == 'committed']


@pytest.mark.asyncio
async def test_gate_questioner_segment_commits_and_reports_counts_and_latency():
    gate = []
    pipe, metrics, conn = make(FakeTracker('questioner'), [(900, Q, False, 1000)], on_gate=gate.append)
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert committed(pipe) == [Q]
    assert pipe.mic_gate_stats['counts'] == dict(questioner=1, answerer=0, unknown=0)
    assert gate and gate[-1]['state'] == 'active' and gate[-1]['counts']['questioner'] == 1
    assert gate[-1]['avg_latency_ms'] == 800  # 角色确定耗时（min_window_ms）上报 health
    rows = metrics.rows('s1')
    assert any(r[2] == 'role_latency' for r in rows)
    latency = next(r for r in rows if r[2] == 'role_latency')
    assert json.loads(latency[4])['role'] == 'questioner' and json.loads(latency[4])['latency_ms'] == 800
    conn.close()


@pytest.mark.asyncio
async def test_gate_answerer_segment_never_feeds_question_detector():
    calls = []
    pipe, _, conn = make(FakeTracker('answerer'), [(900, Q, False, 1000)])
    orig = pipe.detector.on_answerer_active
    pipe.detector.on_answerer_active = lambda active, t: calls.append(active) or orig(active, t)
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert not committed(pipe) and pipe.detector._sent == {}
    assert pipe.mic_gate_stats['counts'] == dict(questioner=0, answerer=1, unknown=0)
    assert calls[0] is True and calls[-1] is False
    conn.close()


@pytest.mark.asyncio
async def test_gate_unknown_segment_is_neither_question_nor_answer_and_is_counted():
    pipe, _, conn = make(FakeTracker('unknown'), [(900, Q, False, 1000)])
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert not committed(pipe) and pipe.detector._sent == {}
    assert pipe.mic_gate_stats['counts'] == dict(questioner=0, answerer=0, unknown=1)
    conn.close()


@pytest.mark.asyncio
async def test_gate_not_enabled_without_voice_profile():
    gate = []
    pipe, _, conn = make(None, [(900, Q, False, 1000)], mic_gate=True, mic_gate_reason='not_enrolled',
                         on_gate=gate.append)
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert not committed(pipe)
    assert pipe._routes['mic'].room is None and pipe._routes['mic'].role == 'answerer'
    assert gate and gate[-1]['state'] == 'unavailable' and gate[-1]['reason'] == 'not_enrolled'
    conn.close()


@pytest.mark.asyncio
async def test_gate_switches_back_to_dual_when_meeting_joins_without_duplicate_commit():
    tracker = FakeTracker('questioner')
    pipe, _, conn = make(tracker, [(900, Q, False, 1000)])
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert committed(pipe) == [Q]  # 单麦门控先提交一次
    await feed(pipe, 'meeting', 0, 30, False, stream_id=2)  # 会议通道后加入
    assert pipe.mic_gate_stats['state'] == 'switched'
    assert 'mic' not in pipe._routes  # 旧门控路已丢弃，下一帧按回答方重建
    assert tracker.resets == 1 and tracker.last_role is None
    await feed(pipe, 'mic', 70, 130, lambda s: s < 100)
    assert pipe._routes['mic'].room is None and pipe._routes['mic'].role == 'answerer'
    assert committed(pipe) == [Q]  # 回答方语音不再提交第二次
    conn.close()


@pytest.mark.asyncio
async def test_gate_config_off_restores_original_mic_behavior():
    pipe, _, conn = make(FakeTracker('questioner'), [(900, Q, False, 1000)], mic_gate=False)
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert not committed(pipe) and pipe._routes['mic'].room is None
    assert pipe._routes['mic'].role == 'answerer'
    conn.close()


@pytest.mark.asyncio
async def test_echo_detector_does_not_swallow_gate_questioner_audio():
    seen = []
    pipe, _, conn = make(FakeTracker('questioner'), [(900, Q, False, 1000)],
                         echo=EchoDetector(), on_echo=seen.append)
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert committed(pipe) == [Q]
    assert seen == [] and len(pipe._echo_pending.get('mic', {})) <= 101
    conn.close()


# ---------- 接线：登记 / 开关 / 计量 / 外发关闭 ----------
def _saved_profile(app):
    VoiceProfileStore(app.state.store.connect, app.state.store.lock, app.state.store.root).save(
        'default', np.array([1, 0, 0], np.float32), dict(effective_speech_s=8.76, snr_db=17.8,
                                                           min_pair_similarity=0.6354))


def test_wiring_online_gate_requires_enrolled_profile(tmp_path):
    app = create_app(tmp_path, config=dict(voice_extractor=FakeExtractor()))
    c = TestClient(app)
    _, _, sid = start_session(c)
    pipe = app.state.audio_hub.pipeline(sid)
    assert pipe.mic_gate is True and pipe.room_tracker is None  # 未登记：不建门控路
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert health['mic_gate']['state'] == 'unavailable' and health['mic_gate']['reason'] == 'not_enrolled'


def test_wiring_online_gate_enabled_after_enrollment(tmp_path):
    app = create_app(tmp_path, config=dict(voice_extractor=FakeExtractor()))
    _saved_profile(app)
    c = TestClient(app)
    _, _, sid = start_session(c)
    pipe = app.state.audio_hub.pipeline(sid)
    assert pipe.mic_gate is True and pipe.room_tracker is not None and not pipe.room_tracker.degraded
    assert pipe.room_tracker.source == 'mic'


def test_wiring_online_gate_off_by_config(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'turn': {'mic_as_question': False}}), 'utf-8')
    app = create_app(tmp_path, config=dict(voice_extractor=FakeExtractor()))
    _saved_profile(app)
    c = TestClient(app)
    _, _, sid = start_session(c)
    pipe = app.state.audio_hub.pipeline(sid)
    assert pipe.mic_gate is False and pipe.room_tracker is None
    health = c.get(f'/api/sessions/{sid}/snapshot').json()['health']
    assert health['mic_gate']['state'] == 'off'


@pytest.mark.asyncio
async def test_gate_mic_transcription_uses_metered_asr(tmp_path, monkeypatch):
    import app.wiring as w
    (tmp_path / 'config.json').write_text(json.dumps({'outbound': {'asr_answerer': True}}), 'utf-8')
    monkeypatch.setattr(w, 'AliyunRealtimeASR', lambda **kw: Inner())
    g, meter = guard(tmp_path)
    wire, _ = wiring(tmp_path, budget=g)
    asr = wire.asr_factory('s1')('mic')  # 门控下 mic 与 meeting 共用同一工厂
    assert isinstance(asr, MeteredASR) and wire.asr_backend['s1'] == 'aliyun'
    await asr.start('x')
    await asr.push(ONE_SECOND, 0)
    await asr.close()
    assert asr_rows(meter)[0]['units'] == 1.0 and g.inflight == []


def test_gate_mic_uses_cloud_even_when_answerer_transcript_off(tmp_path, monkeypatch):
    """2026-11-09 决定：单麦/线下混音允许上云（质量优先）；asr_answerer 只控制逐字稿入记录。"""
    import app.wiring as w
    monkeypatch.setattr(w, 'AliyunRealtimeASR', lambda **kw: Inner())
    (tmp_path / 'config.json').write_text(json.dumps({'outbound': {'asr_answerer': False}}), 'utf-8')
    g, meter = guard(tmp_path)
    wire, _ = wiring(tmp_path, budget=g)
    assert isinstance(wire.asr_factory('s1')('mic'), MeteredASR)
    assert isinstance(wire.asr_factory('s1')('room'), MeteredASR)
    assert any('已上云转写' in n for n in wire.health('s1').payload()['notices'])


def test_gate_mic_stays_local_when_asr_outbound_disabled(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'outbound': {'asr_audio': False}}), 'utf-8')
    wire, _ = wiring(tmp_path)
    asr = wire.asr_factory('s1')('mic')
    assert not isinstance(asr, MeteredASR) and wire.asr_backend['s1'] == 'none'


# ---------- P0-B：单麦连续独白门控 ----------
@pytest.mark.asyncio
async def test_monologue_pauses_prompting_after_repeated_questioner_segments():
    qs = [f'你能讲讲分布式事务的第{i}个区别吗' for i in range(1, 7)]
    script = [(900 + i * 2400, q, True, 1000 + i * 2400) for i, q in enumerate(qs)]
    gate = []
    pipe, _, conn = make(FakeTracker('questioner'), script, on_gate=gate.append,
                         monologue_segments=3, T_monologue=10000)

    def speech(seq):
        phase = (seq * 20) % 2400
        return phase < 1000

    await feed(pipe, 'mic', 0, 660, speech)  # 13.2 秒，6 段提问方
    assert pipe._monologue is True
    assert gate and gate[-1]['monologue'] is True
    assert pipe.mic_gate_stats['counts']['questioner'] == 6
    # 独白后不再提交新题：提交次数从“每段一题”降到前几段
    assert 1 <= len(committed(pipe)) <= 5
    assert pipe.turn_events[-1].type != 'committed' or pipe.turn_events[-1].t_ms < 11000
    conn.close()


@pytest.mark.asyncio
async def test_answerer_segment_exits_monologue_and_resumes_prompting():
    qs = [f'你能讲讲分布式事务的第{i}个区别吗' for i in range(1, 5)]
    script = [(900 + i * 2400, q, True, 1000 + i * 2400) for i, q in enumerate(qs)]

    tracker = FakeTracker('questioner')
    pipe, _, conn = make(tracker, script, monologue_segments=2, T_monologue=0)

    def speech(seq):
        return (seq * 20) % 2400 < 1000

    await feed(pipe, 'mic', 0, 360, speech)  # 7.2 秒：进入独白
    assert pipe._monologue is True
    n = len(committed(pipe))
    tracker.role = 'answerer'  # 回答方开口：退出独白
    await feed(pipe, 'mic', 360, 480, speech)

    def speech2(seq):
        return (seq * 20) % 2400 < 1000

    tracker.role = 'questioner'
    await feed(pipe, 'mic', 480, 600, speech2)
    assert pipe._monologue is False
    assert len(committed(pipe)) <= n + 2
    conn.close()


def test_wiring_mic_gate_monologue_sets_health_notice(tmp_path):
    from app.realtime.events import EventBus
    from app.wiring import SessionHealth

    bus = EventBus()
    rt = SessionHealth('s1', bus, lambda: 100.0)
    rt.set_mic_gate(dict(state='active', counts=dict(questioner=3, answerer=0, unknown=0), monologue=True))
    assert '检测到连续独白，已暂停提词' in rt.payload()['notices']
    rt.set_mic_gate(dict(state='active', counts=dict(questioner=0, answerer=1, unknown=0), monologue=False))
    assert '检测到连续独白，已暂停提词' not in rt.payload()['notices']


@pytest.mark.asyncio
async def test_81s_monologue_scenario_commits_drop_to_single_digits():
    """81 秒同类对白：修复前每段都提交（22 次）；独白门控 + 轮次滚动后降到个位数。"""
    cycle, speech_ms = 2800, 1200
    base = ['你能讲讲分布式事务的区别吗', '请介绍一下缓存方案', '你们为什么选择Kafka', '说说你们的消息队列设计',
            '缓存击穿怎么处理', '数据库分库分表怎么做', '怎样保证接口幂等', '消息丢失怎么排查',
            '服务雪崩如何避免', '你们怎么做限流']
    qs = [base[i % len(base)] for i in range(29)]
    script = [(600 + i * cycle, q, True, 600 + i * cycle) for i, q in enumerate(qs)]
    pipe, _, conn = make(FakeTracker('questioner'), script)

    def speech(seq):
        return (seq * 20) % cycle < speech_ms

    await feed(pipe, 'mic', 0, 4050, speech)  # 81 秒
    assert pipe._monologue is True
    assert 3 <= len(committed(pipe)) <= 9  # 修复前 22 次；独白后个位数
    assert all(len(t) <= 20 for t in committed(pipe))  # 不累积
    conn.close()


@pytest.mark.asyncio
async def test_single_mic_transcript_source_is_mic_not_meeting():
    pipe, _, conn = make(FakeTracker('questioner'), [(900, Q, False, 1000)])
    await feed(pipe, 'mic', 0, 70, lambda s: s < 50)
    assert pipe.question_source == 'mic'
    from app.realtime.events import EventBus
    bus = EventBus()
    bus.pull('s1', pipe)
    tr = [e for e in bus.replay('s1', 0) if e['type'] == 'transcript']
    assert tr and tr[0]['payload']['source'] == 'mic'
    conn.close()
