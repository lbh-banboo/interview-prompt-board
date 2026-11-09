"""管线接线：回声 / 健康监测钩子 / 线下角色门控 / 埋点 turn id。合成帧 + MockASR + 假 tracker，不读墙钟。"""
import asyncio
import sqlite3

import numpy as np
import pytest

from app.asr.mock import MockASR
from app.audio.echo import EchoDetector
from app.audio.frame import pack, parse
from app.domain.turn_detector import TurnEvent
from app.health.capabilities import CapabilityBoard
from app.health.monitor import HealthMonitor
from app.realtime.events import EventBus
from app.realtime.pipeline import SessionPipeline
from app.telemetry.metrics import Metrics
from app.wiring import SessionHealth
from test_pipeline import SILENCE, SPEECH, energy, frame

Q = '你能讲讲分布式事务的区别吗'


def make(**kw):
    script = kw.pop('script', ())
    mode = kw.pop('mode', 'online')
    return SessionPipeline('s1', None, lambda source: MockASR(script, source), Metrics(sqlite3.connect(':memory:')),
                           mode=mode, vad_factory=energy, **kw)


def noise_frame(source, seq, pcm):
    return parse(pack(source, 1, 0, seq, pcm, t_client_us=seq * 20000))


def noise(rng):
    return rng.integers(-6000, 6000, 320).astype('<i2').tobytes()


# ---------- 回声 ----------
@pytest.mark.asyncio
async def test_echo_detector_fed_with_paired_frames_and_notice_toggles():
    seen = []
    pipe = make(echo=EchoDetector(), on_echo=seen.append)
    rng = np.random.default_rng(0)
    for seq in range(250):  # 5s：meeting 声音完整回灌进 mic
        pcm = noise(rng)
        await pipe.on_frame(noise_frame('meeting', seq, pcm))
        await pipe.on_frame(noise_frame('mic', seq, pcm))
    assert seen == [True]
    for seq in range(250, 350):  # 2s：两路互不相关
        await pipe.on_frame(noise_frame('meeting', seq, noise(rng)))
        await pipe.on_frame(noise_frame('mic', seq, noise(rng)))
    assert seen == [True, False]
    await pipe.close()


@pytest.mark.asyncio
async def test_echo_pending_buffer_is_bounded_when_one_side_missing():
    pipe = make(echo=EchoDetector())
    for seq in range(500):
        await pipe.on_frame(frame('mic', seq, False))
    assert len(pipe._echo_pending['mic']) <= 101
    await pipe.close()


def test_echo_notice_is_published_in_health_payload():
    bus = EventBus()
    rt = SessionHealth('s', bus, lambda: 0.0)
    rt.set_echo(True)
    assert '可能未戴耳机，建议佩戴耳机' in bus.snapshot('s')['health']['notices']
    rt.set_echo(False)
    assert '可能未戴耳机，建议佩戴耳机' not in bus.snapshot('s')['health']['notices']


# ---------- HealthMonitor 钩子 ----------
class Spy:
    def __init__(self):
        self.calls = []
        self.clock = lambda: 7.0

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


@pytest.mark.asyncio
async def test_monitor_hooks_called_from_pipeline():
    spy = Spy()
    pipe = make(monitor=spy, script=[(100, Q, False, 200)])
    for seq in range(30):
        await pipe.on_frame(frame('meeting', seq, seq < 20))
        await pipe.on_frame(frame('mic', seq, False))
    pipe.detector.waiting_continue = True
    await pipe.on_frame(frame('meeting', 30, False))
    names = [c[0] for c in spy.calls]
    assert {'on_source_frame', 'on_vad_speech', 'on_asr_text', 'on_asr_health', 'on_uncertain'} <= set(names)
    assert ('on_vad_speech', 'meeting', 7.0) in spy.calls and ('on_uncertain', True) in spy.calls
    assert ('on_vad_speech', 'mic', 7.0) not in spy.calls  # mic 不做 ASR，不参与停滞判定
    await pipe.close()
    assert 'on_source_closed' not in [c[0] for c in spy.calls]  # WS 断线式关闭不算“正常结束”
    await pipe.close(final=True)
    await pipe.on_frame(frame('meeting', 31, False))
    await pipe.close(final=True)
    assert ('on_source_closed', 'meeting') in spy.calls


@pytest.mark.asyncio
async def test_real_monitor_flags_source_lost_and_not_after_normal_close():
    now = [0.0]
    board = CapabilityBoard(lambda: now[0])
    mon = HealthMonitor(board, lambda: now[0])
    pipe = make(monitor=mon)
    await pipe.on_frame(frame('meeting', 0, False))
    now[0] = 2.5
    mon.check()
    assert board.is_active('source_lost', 'meeting')
    await pipe.on_frame(frame('meeting', 1, False))
    assert not board.is_active('source_lost', 'meeting')
    await pipe.close(final=True)
    now[0] = 9.0
    mon.check()
    assert not board.is_active('source_lost', 'meeting')


@pytest.mark.asyncio
async def test_session_health_monitor_starts_in_loop_and_stops():
    bus = EventBus()
    rt = SessionHealth('s', bus, lambda: 0.0)
    rt.start()
    assert rt.monitor._tasks
    await rt.stop()
    assert not rt.monitor._tasks


# ---------- 线下角色门控 ----------
class FakeTracker:
    """第 resolve_at 次 feed 之后给出角色；speech_end 给最终角色。"""
    degraded, degraded_reason, in_answer = False, None, False

    def __init__(self, role, resolve_at=None):
        self.role, self.resolve_at, self.n = role, resolve_at, 0

    def on_speech_start(self, t_ms):
        self.t0, self.n = t_ms, 0

    def feed(self, pcm):
        self.n += 1
        if self.n == self.resolve_at:
            return [self.seg(False, self.t0 + 20 * self.n)]
        return []

    def on_speech_end(self, t_ms):
        return self.seg(True, t_ms)

    def seg(self, final, t_end):
        return dict(source='room', role=self.role, confidence=0.9, t_start=self.t0, t_end=t_end, inherited=False,
                    flags=[], final=final, latency_ms=None)


async def run_room(tracker, script):
    pipe = make(mode='offline', room_tracker=tracker, script=script)
    for seq in range(70):
        await pipe.on_frame(frame('room', seq, seq < 50))
    await pipe.close()
    return pipe


@pytest.mark.asyncio
async def test_offline_questioner_segment_commits_and_held_transcripts_are_replayed():
    # 转写早于角色确定（第 30 帧 = 600ms）到达：先保留，确定为提问方后补喂
    pipe = await run_room(FakeTracker('questioner', resolve_at=30), [(300, Q, False, 1000), (500, Q, True, 1000)])
    assert 'committed' in [e.type for e in pipe.turn_events]
    assert [s['role'] for s in pipe.role_events][:1] == ['questioner'] and pipe.role_events[-1]['final']


@pytest.mark.asyncio
async def test_offline_answerer_segment_never_feeds_question_detector():
    calls = []
    pipe = make(mode='offline', room_tracker=FakeTracker('answerer', resolve_at=30), script=[(900, Q, True, 1000)])
    orig = pipe.detector.on_answerer_active
    pipe.detector.on_answerer_active = lambda active, t: calls.append((active, t)) or orig(active, t)
    for seq in range(70):
        await pipe.on_frame(frame('room', seq, seq < 50))
    await pipe.close()
    assert calls[0][0] is True and calls[-1][0] is False
    assert not [e for e in pipe.turn_events if e.type in ('candidate', 'committed')]
    assert pipe.detector.history == [] or all(e.type != 'committed' for e in pipe.detector.history)


@pytest.mark.asyncio
async def test_offline_unknown_role_forwards_nothing_and_degraded_reported_once():
    tracker = FakeTracker('unknown')
    tracker.degraded, tracker.degraded_reason = True, 'model_unavailable'
    reports = []
    pipe = make(mode='offline', room_tracker=tracker, on_voice=lambda ok, why: reports.append((ok, why)),
                script=[(900, Q, True, 1000)])
    for seq in range(70):
        await pipe.on_frame(frame('room', seq, seq < 50))
    await pipe.close()
    assert pipe.turn_events == [] and reports == [(False, 'model_unavailable')]
    assert pipe.role_events and pipe.role_events[-1]['role'] == 'unknown'


@pytest.mark.asyncio
async def test_offline_without_tracker_still_ignores_everything():
    pipe = make(mode='offline', script=[(900, Q, True, 1000)])
    await pipe.on_frame(frame('room', 0, True))
    assert pipe._routes == {}


# ---------- 埋点 turn id ----------
def test_metric_turn_ids_follow_board_numbering():
    pipe = make()
    ev = lambda t: TurnEvent(t, 'x', 1, 0, 'r')  # noqa: E731
    seq = ['candidate', 'committed', 'text_updated', 'rolled_back', 'candidate', 'committed', 'answer_closed',
           'candidate', 'withdrawn', 'candidate', 'committed', 'interrupted', 'committed']
    assert [pipe._metric_turn(ev(t)) for t in seq] == [
        't1', 't1', 't1', 't1', 't1', 't1', 't1', 't2', 't2', 't2', 't2', 't2', 't3']
