"""M13-3 故障注入与 M13-T1/T2（SIMULATED：全部依赖为注入的 Mock）。"""
import asyncio
import time

import pytest

from app.health.capabilities import CapabilityBoard
from app.health.faults import FaultInjector
from app.health.monitor import HealthMonitor


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def rig(inj, **kw):
    clock = Clock()
    board = CapabilityBoard(clock)

    async def ok():
        return True

    mon = HealthMonitor(board, clock, probes={'llm': inj.wrap_probe('llm', ok), 'search': inj.wrap_probe('search', ok)},
                        disk_probe=lambda: inj.disk_free(10 << 30), probe_timeout=0.05, **kw)
    return clock, board, mon


async def drive(inj, mon, clock, until, step=0.25):
    """按时钟推进，模拟事件源：ASR 健康、音源帧、VAD 语音、ASR 文本。"""
    while clock.t < until:
        clock.t += step
        mon.on_asr_health(inj.asr_health())
        mon.on_voice_health(inj.voice_ok(), '注入')
        mon.on_vad_speech('meeting', clock.t)
        if not inj.drop_asr_text() and not inj.is_on('asr', 'ws', 'network'):
            mon.on_asr_text('meeting', clock.t)
        if not inj.is_on('source'):
            mon.on_source_frame('meeting', clock.t)
        mon.check()
        mon.check_disk()


def test_unknown_fault_rejected():
    with pytest.raises(ValueError):
        FaultInjector().inject('nope')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,cap,msg,limit_s', [
    ('network', 'asr', '实时转写不可用', 3.0),
    ('ws', 'asr', '实时转写不可用', 3.0),
    ('search', 'search', '外部补充不可用', 12.0),
    ('llm', 'llm', '外部补充不可用', 12.0),
    ('voice', 'voice', '角色识别不确定', 3.0),
    ('asr_stall', 'asr', '实时转写停滞', 3.5),  # 3s 判定窗 + 1 个 tick
    ('disk', 'disk', '磁盘空间不足', 12.0),
])
async def test_t1_inject_each_fault(fault, cap, msg, limit_s):
    inj = FaultInjector()
    clock, board, mon = rig(inj)
    await drive(inj, mon, clock, 2)
    assert board.notices() == []
    t0 = clock.t
    inj.inject(fault)
    detected = None
    while clock.t < t0 + 15 and detected is None:
        await drive(inj, mon, clock, clock.t + 0.25)
        if fault in ('search', 'llm'):
            await mon.probe_once(fault)  # 外部服务按 10s 周期探测，这里等价于下一次探测到来
            detected = clock.t if board.state(cap)['state'] != 'ok' else None
        elif board.state(cap)['state'] != 'ok':
            detected = clock.t
    assert detected is not None and msg in board.notices()
    if fault not in ('search', 'llm'):
        assert detected - t0 <= limit_s
    # 其它能力不受牵连
    assert board.state('retrieval')['state'] == 'ok'
    inj.clear(fault)
    t1 = clock.t
    await drive(inj, mon, clock, t1 + 4)
    await mon.probe_once('llm')
    await mon.probe_once('search')
    assert board.notices() == [] and board.state(cap)['state'] == 'ok'  # 恢复无需重启（M13-T3）
    assert board.history and clock.t - t1 <= 10


@pytest.mark.asyncio
async def test_t1_source_lost_and_hang_probe_timeout():
    inj = FaultInjector()
    clock, board, mon = rig(inj)
    await drive(inj, mon, clock, 1)
    inj.inject('source')
    t0 = clock.t
    while board.state('capture')['state'] == 'ok':
        mon.check(clock.t)
        clock.t += 0.25
    assert clock.t - t0 <= 2.5 and board.notices() == ['会议音频已断开']
    inj.clear()
    inj.inject('llm', 'hang')
    start = time.perf_counter()
    await mon.probe_once('llm')
    assert time.perf_counter() - start < 1 and board.state('llm')['reason'] == '服务连接超时'


def test_disk_free_modes():
    inj = FaultInjector()
    assert inj.disk_free(123) == 123
    inj.inject('disk', 150 << 20)
    assert inj.disk_free(123) == 150 << 20
    inj.inject('disk')
    assert inj.disk_free(123) == 0
    inj.clear()
    assert not inj.is_on('disk') and inj.voice_ok() and not inj.drop_asr_text()


def test_asr_health_passthrough():
    inj = FaultInjector()
    assert inj.asr_health() == {'state': 'ok', 'reason': ''}
    assert inj.asr_health(lambda: {'state': 'reconnecting', 'reason': 'r'})['state'] == 'reconnecting'
    inj.inject('network')
    assert inj.asr_health(lambda: {'state': 'ok'}) == {'state': 'down', 'reason': 'injected:network'}


@pytest.mark.asyncio
async def test_t2_offline_local_hint_unaffected():
    """断网期间：主库命中提示不依赖外网能力，仍立即可用；外部补充按能力降级。"""
    inj = FaultInjector()
    clock, board, mon = rig(inj)
    inj.inject('network')
    await mon.probe_once('llm')
    await mon.probe_once('search')
    mon.on_asr_health(inj.asr_health())
    assert board.state('llm')['state'] == 'down' and board.state('search')['state'] == 'down'
    assert board.state('asr')['state'] == 'down'  # 云 ASR：不得宣称可识别新题

    library = {'q1': '主库答案'}
    external_calls = []

    async def external(q):
        external_calls.append(q)

    async def hint(q):
        answer = library.get(q)  # 本地路径，不经过任何外网能力
        if board.state('llm')['state'] == 'ok':
            await external(q)
        return answer

    for _ in range(60):  # 模拟 60 次/秒的命中，每次 <500ms
        s = time.perf_counter()
        assert await asyncio.wait_for(hint('q1'), 0.5) == '主库答案'
        assert time.perf_counter() - s < 0.5
    assert external_calls == []
    assert board.state('retrieval')['state'] == 'ok' and board.state('capture')['state'] == 'ok'
