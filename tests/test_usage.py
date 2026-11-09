import asyncio
from datetime import datetime

import pytest

from app.usage import BudgetGuard, UsageMeter, estimate_cost

PRICES = dict(asr_per_hour=3.6, llm_per_1k_tokens=0.01, search_per_call=0.1)


class Clock:
    t = datetime(2026, 10, 29, 12).timestamp()

    def __call__(self):
        return self.t


def make(tmp_path, daily=10.0, session=10.0, n=4, asr_daily=5.0, asr_session=1.5):
    clock = Clock()
    meter = UsageMeter(tmp_path / 'u.sqlite3', PRICES, clock)
    return BudgetGuard(meter, daily, session, n, asr_daily_limit=asr_daily,
                       asr_session_limit=asr_session), meter, clock


def test_estimate_cost():
    assert estimate_cost(PRICES, 'asr', 1800) == pytest.approx(1.8)
    assert estimate_cost(PRICES, 'llm', 2000) == pytest.approx(0.02)
    assert estimate_cost(PRICES, 'search', 3) == pytest.approx(0.3)
    with pytest.raises(ValueError):
        estimate_cost(PRICES, 'x', 1)


def test_record_aggregates_per_day(tmp_path):
    g, meter, clock = make(tmp_path)
    meter.record('search', 1)
    meter.record('search', 2, calls=2)
    r = meter.report()
    assert r['rows'] == [dict(date='2026-10-29', service='search', calls=3, units=3, est_cost=pytest.approx(0.3))]
    assert '估算' in r['note'] and meter.spent_today() == pytest.approx(0.3)
    clock.t += 86400
    assert meter.spent_today() == 0 and meter.report()['rows'] == []


def test_settle_and_cancel(tmp_path):
    g, meter, _ = make(tmp_path)
    res = g.reserve('search', 1, 's1')
    assert res.ok and len(g.inflight) == 1
    assert g.settle(res, 1) == pytest.approx(0.1) and g.inflight == []
    assert g.settle(res, 1) == 0.0  # 重复核销无效
    r2 = g.reserve('search', 1, 's1')
    g.cancel(r2)
    assert g.inflight == [] and meter.spent_today() == pytest.approx(0.1)


def test_zero_budget_blocks_external_calls(tmp_path):
    g, meter, _ = make(tmp_path, daily=0)
    r = g.reserve('llm', 100)
    assert not r.ok and r.reason == 'daily' and '预算' in r.message and '估算' in r.message
    assert g.inflight == [] and meter.spent_today() == 0
    g2, _, _ = make(tmp_path, session=0)
    assert g2.reserve('llm', 100).reason == 'session'


def test_asr_exhausted_does_not_claim_local_unaffected(tmp_path):
    g, _, _ = make(tmp_path, asr_daily=0)
    msg = g.reserve('asr', 60).message
    assert '不再识别新题' in msg and '不受影响' not in msg


def test_daily_and_session_limits_count_reservations(tmp_path):
    g, meter, _ = make(tmp_path, daily=0.25, session=0.15)
    a = g.reserve('search', 1, 's1')
    assert a.ok
    b = g.reserve('search', 1, 's1')
    assert not b.ok and b.reason == 'session'
    c = g.reserve('search', 1, 's2')
    assert c.ok
    d = g.reserve('search', 1, 's3')
    assert d.reason == 'daily'  # 0.1+0.1 在途 + 0.1 > 0.25
    g.settle(a, 1)
    assert not g.reserve('search', 1, 's1').ok  # 已花 0.1 + 0.1 > 0.15


def test_asr_has_independent_limits(tmp_path):
    """搜索爆发用尽 daily 不影响云 ASR；ASR 自己超限也只拒 ASR。"""
    g, meter, _ = make(tmp_path, daily=0.2, session=10.0)
    assert g.reserve('search', 1, 's1').ok
    assert g.reserve('search', 1, 's1').ok
    assert g.reserve('search', 1, 's1').reason == 'daily'
    assert g.reserve('asr', 60, 's1').ok  # 搜索占满不影响 ASR
    g.settle(g.reserve('asr', 1, 's2'), 60)
    # ASR 独立日限额（默认 5 元）用尽后只拒 ASR
    assert g.reserve('asr', 60 * 200, 's3').reason == 'daily'
    assert g.reserve('llm', 10, 's4').reason == 'daily'  # 非 ASR 仍按共享日限额
    assert g.reserve('asr', 1, 's4').ok  # ASR 不受非 ASR 日限额影响


def test_asr_spend_not_counted_against_shared_daily(tmp_path):
    g, meter, _ = make(tmp_path, daily=0.25, session=0.15, asr_daily=10, asr_session=10)
    res = g.reserve('asr', 60, 's1')
    g.settle(res, 60)  # 0.06 元，不应挤占搜索/模型额度
    assert g.reserve('search', 1, 's1').ok
    assert g.reserve('llm', 1000, 's1').ok


def test_limits_and_prices_read_live_config(tmp_path):
    class Mgr:
        config = {'budget': {'daily_limit': 0.0, 'session_limit': 0.0, 'max_inflight': 4,
                             'asr_daily_limit': 0.0, 'asr_session_limit': 0.0},
                  'prices': dict(asr_per_hour=3.6, llm_per_1k_tokens=0.01, search_per_call=0.1)}

    g, meter, _ = make(tmp_path, daily=99.0, session=99.0)
    g.bind_config(Mgr)
    assert g.reserve('search', 1, 's').reason == 'daily'
    assert g.reserve('asr', 60, 's').reason == 'daily'
    Mgr.config = {'budget': {'daily_limit': 10.0, 'session_limit': 10.0, 'max_inflight': 4,
                             'asr_daily_limit': 10.0, 'asr_session_limit': 10.0},
                  'prices': dict(asr_per_hour=3.6, llm_per_1k_tokens=0.01, search_per_call=0.5)}
    assert g.reserve('search', 1, 's').ok  # 下一场开始前读到新限额
    assert meter.prices['search_per_call'] == 0.5  # 价格同源


def test_prices_provider_used_by_record_and_report(tmp_path):
    g, meter, _ = make(tmp_path)
    prices = dict(PRICES)
    meter.set_prices_provider(lambda: prices)
    assert meter.record('search', 1) == pytest.approx(0.1)
    prices['search_per_call'] = 0.2
    assert meter.record('search', 1) == pytest.approx(0.2)
    assert meter.report()['est_cost_total'] == pytest.approx(0.3)


def test_usage_note_states_units_and_placeholder_prices(tmp_path):
    _, meter, _ = make(tmp_path)
    note = meter.report()['note']
    assert 'ASR=秒' in note and '搜索=次' in note and 'LLM=估算 token' in note and '估算' in note


def test_inflight_cap_async(tmp_path):
    g, _, _ = make(tmp_path, n=4)

    async def call(i, peak):
        res = g.reserve('search', 1, 's')
        if not res.ok:
            return res
        peak.append(len(g.inflight))
        await asyncio.sleep(0.01)
        g.settle(res, 1)
        return res

    async def main():
        peak = []
        out = await asyncio.gather(*(call(i, peak) for i in range(10)))
        return out, peak

    out, peak = asyncio.run(main())
    assert sum(r.ok for r in out) == 4 and max(peak) == 4
    assert all(r.reason == 'inflight' for r in out if not r.ok) and '并发' in next(r for r in out if not r.ok).message
    assert g.inflight == [] and g.reserve('search', 1, 's').ok
