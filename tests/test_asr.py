import asyncio
import dataclasses

import pytest

from app.asr.base import ASRAdapter, ReconnectingASR, TranscriptEvent
from app.asr.mock import MockASR

pytestmark = pytest.mark.asyncio

PCM = b'\0' * 640
SCRIPT = [
    (100, '你好', False, 80),
    (200, '你好请问', False, 180),
    (300, '你好请问Kafka', True, 300),
    (400, '下一句', False, 380),
]


async def collect(adapter, n, timeout=1):
    out = []

    async def run():
        async for ev in adapter.events():
            out.append(ev)
            if len(out) >= n:
                return
    await asyncio.wait_for(run(), timeout)
    return out


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_mock_order_and_time_gating():
    m = MockASR(SCRIPT, clock_ns=lambda: 7)
    await m.start('s')
    await m.push(PCM, 50)
    assert m._queue.empty()
    await m.push(PCM, 250)
    await m.push(PCM, 300)
    evs = await collect(m, 3)
    assert [e.text for e in evs] == ['你好', '你好请问', '你好请问Kafka']
    assert [e.t_audio_end_ms for e in evs] == [100, 200, 300]
    assert all(e.t_audio_start_ms == 100 for e in evs)
    assert [e.is_final for e in evs] == [False, False, True]
    assert evs[0].audio_covered_until_ms == 80 and evs[0].t_received_ns == 7
    assert all(e.simulated and e.source == 'meeting' for e in evs)
    assert m._queue.empty()


async def test_mock_version_monotonic_and_resets_after_final():
    m = MockASR(SCRIPT)
    await m.start('s')
    await m.push(PCM, 1000)
    evs = await collect(m, 4)
    assert [e.text_version for e in evs] == [1, 2, 3, 1]
    assert evs[3].t_audio_start_ms == 400


async def test_event_fields_and_base_not_simulated():
    names = [f.name for f in dataclasses.fields(TranscriptEvent)]
    assert names == ['source', 'text', 'is_final', 'text_version', 'audio_covered_until_ms',
                     't_audio_start_ms', 't_audio_end_ms', 't_received_ns']
    assert TranscriptEvent.simulated is False
    assert MockASR([]).health() == {'state': 'ok', 'reason': ''}


async def test_mock_stall_withholds_then_releases():
    m = MockASR(SCRIPT)
    await m.start('s')
    m.inject_stall()
    assert m.health()['reason'] == 'stalled'
    await m.push(PCM, 250)
    assert m._queue.empty()
    m.inject_stall(False)
    await m.push(PCM, 260)
    assert [e.text for e in await collect(m, 2)] == ['你好', '你好请问']


async def test_mock_disconnect_and_close():
    m = MockASR(SCRIPT)
    await m.start('s')
    m.inject_disconnect('boom')
    assert m.health() == {'state': 'down', 'reason': 'boom'}
    with pytest.raises(ConnectionError):
        await m.push(PCM, 10)
    with pytest.raises(ConnectionError):
        await collect(m, 1)
    m2 = MockASR(SCRIPT)
    await m2.close()
    assert await collect(m2, 1) == []


async def test_abstract():
    with pytest.raises(TypeError):
        ASRAdapter()


def make_wrapper(factory, sleeps):
    async def fake_sleep(d):
        sleeps.append(d)
    return ReconnectingASR(factory, sleep=fake_sleep)


async def test_reconnect_backoff_sequence_then_down_no_events():
    sleeps, made = [], []

    def factory():
        m = MockASR(SCRIPT)
        made.append(m)
        if len(made) > 1:
            async def bad(_):
                raise ConnectionError('refused')
            m.start = bad
        return m

    r = make_wrapper(factory, sleeps)
    await r.start('s')
    made[0].inject_disconnect('lost')
    await asyncio.wait_for(r._task, 1)
    assert sleeps == [0.5, 1.0, 2.0]
    assert r.health()['state'] == 'down' and 'refused' in r.health()['reason']
    await r.push(PCM, 1000)  # down 时静默丢弃
    assert await collect(r, 1) == []
    await r.close()


async def test_reconnecting_state_visible_and_success_keeps_version():
    sleeps, made, gate = [], [], asyncio.Event()

    def factory():
        m = MockASR(SCRIPT)
        made.append(m)
        return m

    async def slow_sleep(d):
        sleeps.append(d)
        await gate.wait()

    r = ReconnectingASR(factory, sleep=slow_sleep)
    await r.start('s')
    await r.push(PCM, 200)
    first = await collect(r, 2)
    assert [e.text_version for e in first] == [1, 2]
    made[0].inject_disconnect('lost')
    await settle()
    assert r.health() == {'state': 'reconnecting', 'reason': 'lost'}
    await r.push(PCM, 300)  # 重连期间丢弃
    gate.set()
    await settle()
    assert r.health()['state'] == 'ok' and sleeps == [0.5] and len(made) == 2
    await r.push(PCM, 100)
    await r.push(PCM, 200)
    after = await collect(r, 2)
    assert [e.text for e in after] == ['你好', '你好请问']
    assert [e.text_version for e in after] == [3, 4]  # 不倒退
    await r.push(PCM, 300)
    fin = (await collect(r, 1))[0]
    assert fin.is_final and fin.text_version == 5 and fin.simulated
    await r.push(PCM, 400)
    nxt = (await collect(r, 1))[0]
    assert nxt.text_version == 1  # final 后新句重新计数
    await r.close()


async def test_push_failure_triggers_reconnect_and_retry_then_success():
    sleeps, made = [], []

    def factory():
        m = MockASR(SCRIPT)
        made.append(m)
        if len(made) == 2:
            async def bad(_):
                raise ConnectionError('x')
            m.start = bad
        return m

    r = make_wrapper(factory, sleeps)
    await r.start('s')
    made[0]._broken = 'push dead'  # push 时抛错
    await r.push(PCM, 100)
    await settle()
    assert sleeps == [0.5, 1.0] and r.health()['state'] == 'ok' and len(made) == 3
    await r.close()


async def test_stream_ends_without_close_is_disconnect():
    sleeps, made = [], []

    def factory():
        m = MockASR(SCRIPT)
        made.append(m)
        return m

    r = make_wrapper(factory, sleeps)
    await r.start('s')
    await made[0].close()
    await settle()
    assert sleeps == [0.5] and len(made) == 2
    await r.close()


async def test_stall_through_wrapper_emits_nothing():
    r = make_wrapper(lambda: MockASR(SCRIPT), [])
    await r.start('s')
    r._inner.inject_stall()
    await r.push(PCM, 1000)
    with pytest.raises(asyncio.TimeoutError):
        await collect(r, 1, timeout=0.05)
    assert r.health()['state'] == 'ok'
    await r.close()


async def test_close_before_start():
    r = make_wrapper(lambda: MockASR([]), [])
    r._inner = MockASR([])
    await r.close()
    assert await collect(r, 1) == []


class _EndingASR(ASRAdapter):
    """start 成功但 events() 立即结束（或抛错）的适配器。"""

    def __init__(self, error=None, script=None):
        self._error, self._script = error, script or []

    async def start(self, stream_id):
        pass

    async def push(self, pcm, t_ms):
        pass

    async def events(self):
        for ev in self._script:
            yield ev
        if self._error:
            raise self._error

    async def close(self):
        pass

    def health(self):
        return {'state': 'ok', 'reason': ''}


@pytest.mark.parametrize('error', [None, ConnectionError('boom')])
async def test_immediately_ending_connections_accumulate_then_down(error):
    sleeps, made = [], []

    def factory():
        made.append(1)
        return _EndingASR(error)

    r = make_wrapper(factory, sleeps)
    await r.start('s')
    await asyncio.wait_for(r._task, 1)
    assert sleeps == [0.5, 1.0, 2.0] and len(made) == 4
    assert r.health()['state'] == 'down'
    assert await collect(r, 1) == []
    await r.close()


async def test_event_produced_resets_failure_count():
    sleeps, made = [], []
    ev = TranscriptEvent('s', 'x', False, 1, 0, 0, 0, 0)

    def factory():
        made.append(1)
        # 第 1、3 个连接产出事件后断开；其余立即结束
        return _EndingASR(script=[ev] if len(made) in (1, 3) else [])

    r = make_wrapper(factory, sleeps)
    await r.start('s')
    await asyncio.wait_for(r._task, 1)
    # conn1 产出事件 -> 0.5(conn2 立即断) -> 1.0(conn3 产出事件, 清零) -> 0.5, 1.0, 2.0 -> down
    assert sleeps == [0.5, 1.0, 0.5, 1.0, 2.0] and len(made) == 6
    assert r.health()['state'] == 'down'
    await r.close()
