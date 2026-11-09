"""M13-1/2 健康监测与能力状态板（SIMULATED：时钟与探测均为注入）。"""
import asyncio
import socket
import ssl

import pytest

from app.health.capabilities import CAPABILITIES, DEGRADE_MATRIX, CapabilityBoard, offline_capabilities
from app.health.monitor import HealthMonitor, classify_error


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    changes = []
    board = CapabilityBoard(clock, on_change=changes.append)
    return clock, board, changes, HealthMonitor(board, clock, **kw)


def test_board_payload_and_since():
    clock, board, changes, _ = make()
    p = board.payload()
    assert set(CAPABILITIES) <= set(p) and p['asr']['state'] == 'ok' and p['notices'] == []
    clock.t = 5
    board.raise_fault('llm_down', reason='x')
    clock.t = 9
    board.raise_fault('llm_down', reason='x')  # 重复登记不改 since 也不重复回调
    assert board.state('llm') == dict(state='down', reason='x', since=5)
    assert len(changes) == 1 and changes[0]['notices'] == ['外部补充不可用']
    board.clear_fault('llm_down')
    board.clear_fault('llm_down')
    assert board.state('llm')['state'] == 'ok'
    assert board.history == [dict(fault='llm_down', source=None, start=5, end=9, reason='x')]


def test_worst_state_wins_and_independent_capabilities():
    _, board, _, _ = make()
    board.raise_fault('uncertain')
    board.raise_fault('voice_unavailable')
    assert board.state('voice')['state'] == 'down' and board.state('role')['state'] == 'degraded'
    assert board.state('retrieval')['state'] == 'ok' and board.state('llm')['state'] == 'ok'
    board.raise_fault('source_lost', 'mic')
    board.raise_fault('source_lost', 'meeting')
    assert board.notices() == ['不确定，继续判断', '角色识别不确定', '麦克风音频已断开', '会议音频已断开']
    board.clear_fault('voice_unavailable')
    assert board.state('role')['state'] == 'degraded' and board.is_active('uncertain')


def test_offline_capabilities_no_overclaim():
    assert offline_capabilities() == dict(recording=True, local_library_hint=True, new_question_recognition=False)
    assert offline_capabilities(True)['new_question_recognition'] is True


def test_matrix_has_seven_rows_with_planning_text():
    assert {v['message'] for v in DEGRADE_MATRIX.values()} == {
        '外部补充不可用', '实时转写不可用', '实时转写停滞', '会议音频已断开', '不确定，继续判断',
        '角色识别不确定', '磁盘空间不足'}


def test_asr_health_event():
    clock, board, _, mon = make()
    mon.on_asr_health({'state': 'reconnecting', 'reason': 'ws closed'})
    assert board.state('asr')['state'] == 'down'
    clock.t = 4
    mon.on_asr_health({'state': 'ok', 'reason': ''})
    assert board.state('asr')['state'] == 'ok'
    assert board.history[0]['fault'] == 'asr_disconnected' and board.history[0]['end'] == 4


def test_silence_30s_no_asr_fault():  # M13-T0
    clock, board, _, mon = make()
    for i in range(300):
        clock.t = i * 0.1
        mon.on_source_frame('meeting', clock.t)
        mon.on_source_frame('mic', clock.t)
        mon.check()
    assert board.state('asr')['state'] == 'ok' and board.notices() == []


def test_asr_stall_after_3s_with_speech_and_recovery():
    clock, board, _, mon = make()
    mon.on_vad_speech('meeting', 1.0)
    mon.on_vad_speech('meeting', 2.0)  # 不刷新起点
    mon.check(3.9)
    assert board.state('asr')['state'] == 'ok'
    mon.check(4.0)
    assert board.state('asr')['state'] == 'degraded' and board.notices() == ['实时转写停滞']
    clock.t = 5
    mon.on_asr_text('meeting', 5)
    assert board.state('asr')['state'] == 'ok'
    mon.check(100)  # 语音已被响应，不再误报
    assert board.state('asr')['state'] == 'ok'


def test_partial_text_keeps_stall_away():
    _, board, _, mon = make()
    t = 0.0
    for _ in range(20):
        mon.on_vad_speech('meeting', t)
        t += 2.0
        mon.on_asr_text('meeting', t)
        mon.check(t)
    assert board.notices() == []


def test_source_lost_within_2s_and_recover():
    clock, board, _, mon = make()
    mon.on_source_frame('meeting', 10.0)
    mon.check(11.9)
    assert board.notices() == []
    mon.check(12.0)
    assert board.notices() == ['会议音频已断开'] and board.state('capture')['state'] == 'degraded'
    mon.on_source_frame('meeting', 20.0)
    assert board.notices() == []


def test_source_closed_not_flagged():
    _, board, _, mon = make()
    mon.on_source_frame('mic', 0)
    mon.on_vad_speech('mic', 0)
    mon.check(5)
    assert board.is_active('source_lost', 'mic') and board.is_active('asr_stalled', 'mic')
    mon.on_source_closed('mic')
    assert board.notices() == []
    mon.check(50)
    assert board.notices() == []


def test_uncertain_and_voice():
    _, board, _, mon = make()
    mon.on_uncertain(True)
    mon.on_voice_health(False, '加载失败')
    assert board.state('voice')['reason'] == '加载失败' and board.state('role')['state'] == 'degraded'
    mon.on_uncertain(False)
    mon.on_voice_health(True)
    assert board.notices() == []


def test_disk_thresholds():
    free = [5 << 30]
    _, board, _, mon = make(disk_probe=lambda: free[0])
    mon.check_disk()
    assert board.state('disk')['state'] == 'ok'
    free[0] = 500 << 20
    mon.check_disk()
    assert board.state('disk')['state'] == 'degraded' and board.notices() == ['磁盘空间不足']
    free[0] = 100 << 20
    mon.check_disk()
    assert board.state('disk')['state'] == 'down'
    free[0] = 5 << 30
    mon.check_disk()
    assert board.state('disk')['state'] == 'ok'


def test_disk_probe_error_ignored_and_default_probe():
    def boom():
        raise OSError

    _, board, _, mon = make(disk_probe=boom)
    mon.check_disk()
    assert board.notices() == []
    _, _, _, real = make(disk_path='.')
    assert isinstance(real.disk_probe(), int)


class E(Exception):
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize('exc,code', [
    (E(401), 'INVALID_KEY'), (E(403), 'PERMISSION_DENIED'), (E(402), 'INSUFFICIENT_BALANCE'),
    (E(429), 'RATE_LIMITED'), (E(404), 'MODEL_NOT_FOUND'), (E(503), 'PROVIDER_ERROR'),
    (asyncio.TimeoutError(), 'TIMEOUT'), (socket.gaierror(), 'DNS_ERROR'), (ssl.SSLError(), 'TLS_ERROR'),
    (ConnectionError(), 'NETWORK_UNREACHABLE'), (E(400), 'UNKNOWN_ERROR'), (ValueError('sk-secret'), 'UNKNOWN_ERROR')])
def test_classify_error(exc, code):
    assert classify_error(exc) == code


@pytest.mark.asyncio
async def test_probe_once_failure_timeout_and_recovery():
    state = {'mode': 'ok'}

    async def llm():
        if state['mode'] == 'hang':
            await asyncio.sleep(60)
        if state['mode'] == 'fail':
            raise E(401)
        if state['mode'] == 'false':
            return False
        return True

    clock, board, _, mon = make(probes={'llm': llm}, probe_timeout=0.05)
    await mon.probe_once('llm')
    assert board.state('llm')['state'] == 'ok'
    state['mode'] = 'fail'
    await mon.probe_once('llm')
    assert board.state('llm')['reason'] == 'API Key 无效、已删除或粘贴不完整'
    assert 'sk-' not in str(board.payload())
    state['mode'] = 'ok'
    await mon.probe_once('llm')
    state['mode'] = 'hang'
    await mon.probe_once('llm')
    assert board.state('llm')['reason'] == '服务连接超时'
    state['mode'] = 'false'
    await mon.probe_once('llm')
    assert board.state('llm')['reason'] == '未知错误'


@pytest.mark.asyncio
async def test_probe_cancel_propagates():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)

    _, board, _, mon = make(probes={'search': slow}, probe_timeout=30)
    task = asyncio.create_task(mon.probe_once('search'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert board.state('search')['state'] == 'ok'


@pytest.mark.asyncio
async def test_start_stop_periodic_tasks_recover_within_10s():  # M13-T3
    clock = Clock()
    board = CapabilityBoard(clock)
    ok = {'v': False}
    sleeps = []

    async def probe():
        if not ok['v']:
            raise ConnectionError

    async def fake_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)

    mon = HealthMonitor(board, clock, probes={'llm': probe, 'search': probe}, disk_probe=lambda: 10 << 30,
                        sleep=fake_sleep)
    mon.start()
    mon.start()  # 幂等
    for _ in range(5):
        await asyncio.sleep(0)
    assert board.state('llm')['state'] == 'down' and board.state('search')['state'] == 'down'
    assert 10.0 in sleeps and mon.tick_s in sleeps
    ok['v'] = True
    for _ in range(5):
        await asyncio.sleep(0)
    assert board.state('llm')['state'] == 'ok'
    await mon.stop()
    assert mon._tasks == []
