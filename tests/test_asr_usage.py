"""云 ASR 时长计量与预算降级：全部离线，Mock 适配器，不联网。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.asr.base import ASRFatalError, ReconnectingASR
from app.asr.metered import BYTES_PER_SECOND, MeteredASR
from app.asr.mock import MockASR
from app.config import ConfigManager
from app.core import Store
from app.credentials import CredentialStore, MemoryBackend
from app.realtime.events import EventBus
from app.server import create_app
from app.sessions.setup import ensure_sessions_columns
from app.usage import BudgetGuard, UsageMeter
from app.wiring import Wiring

PRICES = dict(asr_per_hour=3.6, llm_per_1k_tokens=0.01, search_per_call=0.1)
ONE_SECOND = b'\x00' * BYTES_PER_SECOND


class Inner:
    """最简 ASR 适配器；events 在 close 后结束。"""

    def __init__(self):
        self.started, self.closed, self.pushes = False, False, []

    async def start(self, stream_id):
        self.started = True

    async def push(self, pcm, t_ms):
        self.pushes.append(len(pcm))

    async def events(self):
        while not self.closed:
            await asyncio.sleep(0)
        return
        yield  # noqa: unreachable —— 保持异步生成器

    async def close(self):
        self.closed = True

    def health(self):
        return {'state': 'ok', 'reason': ''}


def guard(tmp_path, daily=10.0, session=10.0, n=4, asr_daily=10.0, asr_session=10.0):
    meter = UsageMeter(tmp_path / 'usage.sqlite3', PRICES)
    return BudgetGuard(meter, daily, session, n, asr_daily_limit=asr_daily,
                       asr_session_limit=asr_session), meter


def asr_rows(meter):
    return [r for r in meter.report()['rows'] if r['service'] == 'asr']


@pytest.mark.asyncio
async def test_metered_asr_settles_actual_seconds_on_close(tmp_path):
    g, meter = guard(tmp_path)
    inner = Inner()
    m = MeteredASR(inner, g, 's1', g.reserve('asr', 60, 's1'), chunk_s=60)
    await m.start('x')
    await m.push(ONE_SECOND, 0)
    assert asr_rows(meter) == []  # 未关闭前不结算
    await m.close()
    rows = asr_rows(meter)
    assert inner.closed and g.inflight == []
    assert rows == [dict(date=meter.today(), service='asr', calls=1, units=1.0,
                         est_cost=pytest.approx(3.6 / 3600))]


@pytest.mark.asyncio
async def test_budget_exhausted_mid_stream_stops_and_reports_fatal(tmp_path):
    g, meter = guard(tmp_path, asr_daily=0.0015)  # 预留 1s 后只剩半秒额度
    messages = []
    inner = Inner()
    m = MeteredASR(inner, g, 's1', g.reserve('asr', 1, 's1'), chunk_s=1, bytes_per_second=BYTES_PER_SECOND,
                   on_exhausted=lambda message, reason: messages.append(message))
    await m.start('x')
    with pytest.raises(ASRFatalError):
        await m.push(ONE_SECOND, 0)
    assert '预算' in messages[0] and g.inflight == []
    assert [r['units'] for r in asr_rows(meter)] == [1.0]
    await m.push(ONE_SECOND, 20)  # 已停止：不再转发
    assert inner.pushes == [len(ONE_SECOND)]


@pytest.mark.asyncio
async def test_metered_asr_delegates_health_and_events_and_cancels_empty_lease(tmp_path):
    g, meter = guard(tmp_path)
    m = MeteredASR(Inner(), g, 's1', g.reserve('asr', 60, 's1'), chunk_s=60)
    assert m.health()['state'] == 'ok'
    events = m.events()
    await m.start('x')
    await m.close()
    assert [e async for e in events] == []
    assert g.inflight == [] and asr_rows(meter) == []


@pytest.mark.asyncio
async def test_exhausted_without_callback_still_stops(tmp_path):
    g, _ = guard(tmp_path, asr_daily=0.0015)
    m = MeteredASR(Inner(), g, 's1', g.reserve('asr', 1, 's1'), chunk_s=1)
    await m.start('x')
    with pytest.raises(ASRFatalError):
        await m.push(ONE_SECOND, 0)
    await m.close()  # 预算已耗尽：无在途预留可结算


@pytest.mark.asyncio
async def test_start_failure_releases_reservation(tmp_path):
    class Bad(Inner):
        async def start(self, stream_id):
            raise ConnectionError('boom')

    g, meter = guard(tmp_path)
    m = MeteredASR(Bad(), g, 's1', g.reserve('asr', 60, 's1'))
    with pytest.raises(ConnectionError):
        await m.start('x')
    assert g.inflight == [] and asr_rows(meter) == []
    with pytest.raises(ConnectionError):
        await MeteredASR(Bad(), g, 's1', None).start('x')


@pytest.mark.asyncio
async def test_reconnecting_asr_does_not_retry_fatal_budget_error():
    made = []

    class Fatal(Inner):
        async def push(self, pcm, t_ms):
            raise ASRFatalError('云 ASR 预算已用尽')

    def factory():
        made.append(1)
        return Fatal()

    r = ReconnectingASR(factory, backoffs=(0, 0), sleep=lambda _s: asyncio.sleep(0))
    await r.start('x')
    await r.push(b'', 0)
    for _ in range(3):
        await asyncio.sleep(0)
    assert r.health() == {'state': 'down', 'reason': '云 ASR 预算已用尽'} and made == [1]


def wiring(tmp_path, budget=None, key='sk-fake-not-real-0000'):
    store = Store(tmp_path)
    ensure_sessions_columns(store)
    creds = CredentialStore([MemoryBackend({'DASHSCOPE_API_KEY': key} if key else {})])
    return Wiring(store, ConfigManager(store.root / 'config.json'), creds, EventBus(), budget=budget), store


def test_wiring_refuses_cloud_asr_when_budget_denied(tmp_path, monkeypatch):
    import app.wiring as w

    made = []
    monkeypatch.setattr(w, 'AliyunRealtimeASR', lambda **kw: made.append(kw))
    g, _ = guard(tmp_path, asr_daily=0)
    wire, _ = wiring(tmp_path, budget=g)
    asr = wire.asr_factory('s1')('meeting')
    payload = wire.health('s1').payload()
    assert isinstance(asr, MockASR) and made == []  # 本机无 sherpa 模型：不能回退
    assert payload['asr']['state'] == 'down' and any('云 ASR 预算已用尽' in n for n in payload['notices'])
    assert wire.asr_backend['s1'] == 'none' and g.inflight == []


def _sherpa(tmp_path, monkeypatch):
    import app.wiring as w
    d = tmp_path / 'sherpa'
    d.mkdir()
    (d / 'encoder.int8.onnx').write_bytes(b'0')
    monkeypatch.setattr(w, 'SHERPA_DIR', d)
    monkeypatch.setattr(w, 'SherpaStreamingASR', lambda source, **kw: Inner())
    return d


def test_wiring_falls_back_to_local_when_budget_denied_at_start(tmp_path, monkeypatch):
    _sherpa(tmp_path, monkeypatch)
    g, _ = guard(tmp_path, asr_daily=0)
    wire, _ = wiring(tmp_path, budget=g)
    asr = wire.asr_factory('s1')('meeting')
    assert isinstance(asr, Inner) and wire.asr_backend['s1'] == 'sherpa'
    payload = wire.health('s1').payload()
    assert payload['asr']['state'] == 'ok'
    assert '云转写预算用尽，已切本地转写' in payload['notices']


@pytest.mark.asyncio
async def test_budget_exhausted_mid_stream_switches_to_local_without_fatal(tmp_path):
    g, meter = guard(tmp_path, asr_daily=0.07)  # 首片 0.06 元，第二片即超
    inner, local = Inner(), Inner()
    notices = []
    m = MeteredASR(inner, g, 's1', g.reserve('asr', 60, 's1'), chunk_s=60,
                   bytes_per_second=BYTES_PER_SECOND, fallback=lambda: local,
                   on_fallback=notices.append,
                   on_exhausted=lambda message, reason: notices.append('fatal:' + message))
    await m.start('x')
    await m.push(ONE_SECOND * 60, 0)  # 到分片边界：结算首片后预算不足 -> 本地
    assert notices == ['云转写预算用尽，已切本地转写']
    assert local.started and local.pushes == [len(ONE_SECOND) * 60]
    assert [r['units'] for r in asr_rows(meter)] == [60.0]  # 本地回退不计量
    await m.push(ONE_SECOND, 1000)
    assert len(local.pushes) == 2 and inner.pushes == [len(ONE_SECOND) * 60]
    await m.close()
    assert asr_rows(meter)[0]['units'] == 60.0 and g.inflight == []


@pytest.mark.asyncio
async def test_inflight_retries_without_fatal_and_succeeds_when_released(tmp_path):
    g, meter = guard(tmp_path, n=1)
    hold = g.reserve('search', 1, 'other')
    slept, released = [], []

    async def sleep(delay):
        slept.append(delay)
        if not released:
            released.append(1)
            g.settle(hold, 1)  # 第一次退避后并发让出

    m = MeteredASR(Inner(), g, 's1', None, chunk_s=1, bytes_per_second=BYTES_PER_SECOND, sleep=sleep)
    await m.start('x')
    await m.push(ONE_SECOND, 0)
    assert slept == [2.0] and len(g.inflight) == 1  # 重试成功：本片已核销，预留下片
    assert asr_rows(meter) == [dict(
        date=meter.today(), service='asr', calls=1, units=1.0, est_cost=pytest.approx(3.6 / 3600))]
    await m.close()
    assert g.inflight == []


@pytest.mark.asyncio
async def test_inflight_gives_up_with_retryable_not_fatal(tmp_path):
    from app.asr.base import ASRRetryableError

    g, meter = guard(tmp_path, n=1)
    hold = g.reserve('search', 1, 'other')
    slept = []

    async def sleep(delay):
        slept.append(delay)

    m = MeteredASR(Inner(), g, 's1', None, chunk_s=1, bytes_per_second=BYTES_PER_SECOND, sleep=sleep)
    await m.start('x')
    with pytest.raises(ASRRetryableError):
        await m.push(ONE_SECOND, 0)
    assert slept == [2.0, 2.0, 2.0]  # 最多重试 3 次
    g.cancel(hold)
    assert g.inflight == []


@pytest.mark.asyncio
async def test_wiring_meters_cloud_asr_seconds_into_usage(tmp_path, monkeypatch):
    import app.wiring as w

    monkeypatch.setattr(w, 'AliyunRealtimeASR', lambda **kw: Inner())
    g, meter = guard(tmp_path)
    wire, _ = wiring(tmp_path, budget=g)
    asr = wire.asr_factory('s1')('meeting')
    assert isinstance(asr, MeteredASR) and wire.asr_backend['s1'] == 'aliyun'
    await asr.start('x')
    await asr.push(ONE_SECOND, 0)
    await asr.close()
    assert asr_rows(meter)[0]['units'] == 1.0 and g.inflight == []


def test_budget_denial_reports_top_level_health_notice(tmp_path):
    g, _ = guard(tmp_path, daily=0)
    wire, _ = wiring(tmp_path, budget=g)
    res = g.reserve('search', 1, 's1')
    assert not res.ok
    notices = wire.health('s1').payload()['notices']
    assert any('预算已用尽' in n for n in notices)


def test_usage_api_exposes_asr_seconds_and_price_note(tmp_path):
    app = create_app(tmp_path)
    app.state.usage_meter.record('asr', 120)
    data = TestClient(app).get('/api/usage').json()
    row = next(r for r in data['rows'] if r['service'] == 'asr')
    assert row['units'] == 120 and row['calls'] == 1
    assert '待填写真实单价' in data['asr_price_note']
    assert data['asr_price_note'] == app.state.config_manager.config['prices']['asr_note']
