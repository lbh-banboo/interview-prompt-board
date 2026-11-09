"""M10 联网检索：客户端、相关度、流式追加、延迟评测。全程 Mock 传输，不联网。"""
import asyncio
import socket
import ssl
import sys
from pathlib import Path

import pytest

from app.search import Limiter, SearchClient, SearchError, normalize_error, select_points, web_supplements
from app.search.relevance import (evaluate, evaluate_detail, is_authoritative, overlap_score,
                                  reset_stats, stats_snapshot)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import search_latency_eval as ev  # noqa: E402

KEY = "sk-SECRET-1234567890"
Q = "Redis 持久化有哪几种方式"
GOOD = [
    {"title": "Redis 持久化 RDB 与 AOF", "url": "https://redis.io/docs/persistence", "snippet": "Redis 持久化有 RDB 和 AOF 两种方式，可组合使用。"},
    {"title": "Redis 持久化方式对比", "url": "https://blog.example.com/a", "snippet": "Redis 持久化方式包括 RDB 快照与 AOF 日志，各有取舍。"},
]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Transport:
    def __init__(self, result=None, delay=0.0, clock=None, exc=None):
        self.calls, self.result, self.delay, self.clock, self.exc = [], result if result is not None else GOOD, delay, clock, exc

    async def __call__(self, query, *, api_key, timeout_s):
        self.calls.append((query, api_key))
        if self.exc:
            raise self.exc
        if self.clock:
            self.clock.t += self.delay
        return self.result


def collect(q, vocab, client, **kw):
    async def run():
        return [s async for s in web_supplements(q, vocab, client, **kw)]
    return asyncio.run(run())


# ---- client ----
def test_client_returns_hits_and_passes_key():
    t = Transport(GOOD + ["junk", {"title": None}])
    hits = asyncio.run(SearchClient(t, api_key=KEY).search("q"))
    assert len(hits) == 3 and hits[0].url.startswith("https://redis.io") and hits[2].url == ""
    assert t.calls == [("q", KEY)]


def test_client_empty_raw():
    assert asyncio.run(SearchClient(Transport([]), api_key=KEY).search("q")) == []
    assert asyncio.run(SearchClient(Transport(None), api_key=KEY).search("q")) != []


def test_key_from_env_and_not_in_repr(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    t = Transport()
    c = SearchClient(t)
    asyncio.run(c.search("q"))
    assert t.calls[0][1] == KEY and KEY not in repr(c) and "set" in repr(c)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert "unset" in repr(SearchClient(t))


def test_disabled_raises_without_transport_call():
    t = Transport()
    with pytest.raises(SearchError) as e:
        asyncio.run(SearchClient(t, api_key=KEY, enabled=False).search("q"))
    assert e.value.code == "DISABLED" and t.calls == []


class _Http(Exception):
    def __init__(self, status=None, msg=""):
        super().__init__(msg)
        if status is not None:
            self.status = status


@pytest.mark.parametrize("exc,code,retry", [
    (asyncio.TimeoutError(), "TIMEOUT", True),
    (ssl.SSLError("x"), "TLS_ERROR", False),
    (socket.gaierror("x"), "DNS_ERROR", True),
    (ConnectionRefusedError("x"), "NETWORK_UNREACHABLE", True),
    (_Http(401), "INVALID_KEY", False),
    (_Http(402), "INSUFFICIENT_BALANCE", False),
    (_Http(403), "PERMISSION_DENIED", False),
    (_Http(429), "RATE_LIMITED", True),
    (_Http(503), "PROVIDER_ERROR", True),
    (_Http(None, "Search HTTP 401: bad"), "INVALID_KEY", False),
    (_Http(418), "UNKNOWN_ERROR", False),
    (ValueError("boom"), "UNKNOWN_ERROR", False),
])
def test_error_normalization(exc, code, retry):
    e = normalize_error(exc)
    assert e.code == code and e.retryable == retry
    assert normalize_error(e) is e


def test_error_never_leaks_key():
    t = Transport(exc=_Http(401, f"HTTP 401 Authorization: Bearer {KEY}"))
    with pytest.raises(SearchError) as e:
        asyncio.run(SearchClient(t, api_key=KEY).search("q"))
    assert KEY not in str(e.value) and KEY not in repr(e.value) and e.value.__cause__ is None


def test_limiter_caps_inflight():
    active = peak = 0

    async def t(query, *, api_key, timeout_s):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    async def run():
        c = SearchClient(t, api_key=KEY, limiter=Limiter(2))
        await asyncio.gather(*(c.search(str(i)) for i in range(6)))
    asyncio.run(run())
    assert peak == 2


# ---- relevance ----
def test_relevance_filters():
    ok = {"title": "Redis 持久化", "url": "https://redis.io/a", "snippet": "Redis 持久化有 RDB 和 AOF 两种方式。"}
    assert evaluate(Q, ok)
    assert evaluate(Q, {**ok, "url": ""}) is None                       # 无来源 URL
    assert evaluate(Q, {**ok, "url": "ftp://x.com/a"}) is None
    assert evaluate(Q, {**ok, "snippet": "  "}) is None                  # 空内容
    assert evaluate(Q, {**ok, "snippet": "Redis 持久化广告推广，立即购买"}) is None  # 广告
    assert evaluate(Q, {**ok, "title": "天气", "snippet": "今天北京天气晴朗，适合出行游玩。"}) is None  # 不相关
    assert overlap_score("", "abc") == 0.0


def test_select_merge_domain_and_authority_first():
    hits = [
        {"title": "Redis 持久化博客", "url": "https://blog.example.com/1", "snippet": "Redis 持久化有哪几种方式：RDB、AOF、混合持久化。"},
        {"title": "Redis 持久化博客二", "url": "https://www.blog.example.com/2", "snippet": "Redis 持久化的方式。"},
        {"title": "Redis 持久化", "url": "https://redis.io/docs/p", "snippet": "Redis 持久化有哪几种方式？RDB 与 AOF。"},
    ]
    pts = select_points(Q, hits)
    assert [p.domain for p in pts] == ["redis.io", "blog.example.com"]
    assert pts[0].authoritative and not pts[1].authoritative and pts[0].source_tag == "外部参考"
    assert pts[1].url.endswith("/1")
    assert select_points(Q, hits, threshold=0.99, scorer=lambda q, t: 1.0)[0]


def test_authority_whitelist_configurable():
    assert is_authoritative("zh.wikipedia.org") and is_authoritative("docs.foo.com")
    assert not is_authoritative("foo.com") and is_authoritative("foo.com", ("foo.com",))


# ---- relevance: 实体误配 ----
SASA_CORP = {"title": "莎莎国际（美妆零售）公布季度财报", "url": "https://news.example.com/sasa",
             "snippet": "莎莎国际控股有限公司今日公布季度营收，门店客流回升。"}


def test_short_person_name_does_not_match_longer_entity():
    stats = {}
    assert select_points("莎莎", [SASA_CORP], stats=stats) == []
    assert stats["dropped"] == 1 and stats["reasons"] == {"entity_mismatch": 1}
    assert evaluate("莎莎", SASA_CORP) is None
    assert evaluate_detail("莎莎", SASA_CORP) == (None, "entity_mismatch")


def test_short_person_name_matches_when_standalone():
    hit = {"title": "莎莎：Redis 持久化怎么理解", "url": "https://blog.example.com/x",
           "snippet": "莎莎 在分享中说，Redis 持久化有 RDB 与 AOF 两种方式。"}
    assert select_points("莎莎", [hit])[0].url == hit["url"]


def test_short_entity_allows_company_suffix_only_for_long_names():
    corp = {"title": "莎莎国际控股发布公告", "url": "https://news.example.com/a",
            "snippet": "莎莎国际控股有限公司公告称季度营收同比上升。"}
    assert select_points("莎莎国际", [corp])  # 4 字实体 + 公司后缀算同一实体
    assert select_points("莎莎", [corp]) == []  # 2 字人名不得借后缀误配
    long_ok = {"title": "莎莎国际控股有限公司公告", "url": "https://news.example.com/b",
               "snippet": "莎莎国际控股有限公司公告称季度营收同比上升。"}
    assert select_points("莎莎国际控股", [long_ok])  # 5 字查询不走短专名规则，退回通用相关度


def test_mixed_entity_knowledge_query_requires_both():
    q = "张三丰 Redis 持久化"
    good = {"title": "张三丰讲 Redis 持久化", "url": "https://blog.example.com/1",
            "snippet": "张三丰 介绍了 Redis 持久化 的 RDB 与 AOF 两种方式。"}
    no_entity = {"title": "Redis 持久化详解", "url": "https://blog.example.com/2",
                 "snippet": "Redis 持久化有 RDB 与 AOF 两种方式，可组合使用。"}
    stats = {}
    got = select_points(q, [good, no_entity], stats=stats)
    assert [p.url for p in got] == [good["url"]]
    assert stats["reasons"].get("entity_mismatch") == 1


def test_mixed_entity_query_requires_knowledge_hit():
    hit = {"title": "莎莎：个人简介", "url": "https://bio.example.com/x",
           "snippet": "莎莎 是一名演员，曾出演多部作品。"}
    stats = {}
    assert select_points("莎莎 商业模式", [hit], stats=stats) == []
    assert stats["reasons"].get("knowledge_mismatch") == 1


def test_filter_stats_snapshot_for_health_metrics():
    reset_stats()
    select_points("莎莎", [SASA_CORP])
    snap = stats_snapshot()
    assert snap["evaluated"] == 1 and snap["dropped"] == 1
    assert snap["reasons"] == {"entity_mismatch": 1}
    reset_stats()
    assert stats_snapshot()["evaluated"] == 0


# ---- stream ----
def test_first_point_immediate_with_source_tag():
    clk = FakeClock()
    t = Transport(delay=0.8, clock=clk)
    out = collect(Q, None, SearchClient(t, api_key=KEY), clock=clk)
    assert [s.kind for s in out] == ["point", "point"]
    first = out[0]
    assert first.source_tag == "外部参考" and first.url.startswith("https://") and first.elapsed_ms == 800
    assert first.within_first_deadline is True and out[1].within_first_deadline is None


def test_first_deadline_missed_flag():
    clk = FakeClock()
    out = collect(Q, None, SearchClient(Transport(delay=2.0, clock=clk), api_key=KEY), clock=clk)
    assert out[0].within_first_deadline is False


def test_blocked_makes_no_transport_call():
    t = Transport()
    for q, v in [("你负责的项目怎么做缓存", None), ("内部灰度网关怎么限流", {"terms": ["内部灰度网关"]})]:
        out = collect(q, v, SearchClient(t, api_key=KEY))
        assert [s.kind for s in out] == ["not_suitable"] and out[0].text == "该问题不适合联网"
    assert t.calls == []


def test_query_sent_is_redacted():
    t = Transport()
    collect("张三丰问：Redis 持久化有哪几种方式 13812345678", ["张三丰"], SearchClient(t, api_key=KEY))
    sent = t.calls[0][0]
    assert "张三丰" not in sent and "13812345678" not in sent and "Redis" in sent


def test_disabled_status_no_call():
    t = Transport()
    out = collect(Q, None, SearchClient(t, api_key=KEY, enabled=False))
    assert [s.kind for s in out] == ["disabled"] and t.calls == []


def test_search_failure_unavailable():
    out = collect(Q, None, SearchClient(Transport(exc=_Http(500)), api_key=KEY))
    assert out[0].kind == "unavailable" and out[0].text == "外部补充不可用" and out[0].code == "PROVIDER_ERROR"


def test_no_source_not_shown_as_external_reference():
    bad = [{"title": "Redis 持久化", "url": "", "snippet": "Redis 持久化有 RDB 和 AOF 两种方式。"}]
    out = collect(Q, None, SearchClient(Transport(bad), api_key=KEY))
    assert [s.kind for s in out] == ["no_result"]
    assert all(s.source_tag == "" and s.url == "" for s in out)


def test_entity_mismatch_no_result_carries_filter_stats():
    out = collect("莎莎", None, SearchClient(Transport([SASA_CORP]), api_key=KEY))
    assert [s.kind for s in out] == ["no_result"]
    assert out[0].source_tag == "" and out[0].url == ""
    assert out[0].code == "ENTITY_MISMATCH"
    assert out[0].filtered == 1 and out[0].filtered_reasons == {"entity_mismatch": 1}


def test_point_carries_filtered_count_for_health_metrics():
    good = {"title": "莎莎：Redis 持久化怎么理解", "url": "https://blog.example.com/x",
            "snippet": "莎莎 在分享中说，Redis 持久化有 RDB 与 AOF 两种方式。"}
    out = collect("莎莎", None, SearchClient(Transport([good, SASA_CORP]), api_key=KEY))
    assert out[0].kind == "point" and out[0].filtered == 1
    assert out[0].filtered_reasons == {"entity_mismatch": 1}


def test_timeout_after_slow_search_fake_clock():
    clk = FakeClock()
    out = collect(Q, None, SearchClient(Transport(delay=9.0, clock=clk), api_key=KEY), clock=clk)
    assert [s.kind for s in out] == ["timeout"] and out[0].elapsed_ms >= 8000


def test_timeout_hanging_transport_cancelled():
    cancelled = []

    async def hang(query, *, api_key, timeout_s):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
    out = collect(Q, None, SearchClient(hang, api_key=KEY), total_timeout_s=0.05)
    assert [s.kind for s in out] == ["timeout"] and cancelled


def test_timeout_between_points():
    clk = FakeClock()

    async def run():
        got = []
        async for s in web_supplements(Q, None, SearchClient(Transport(delay=0.5, clock=clk), api_key=KEY), clock=clk):
            got.append(s.kind)
            clk.t += 9
        return got
    assert asyncio.run(run()) == ["point", "timeout"]


def test_cancel_before_start_yields_nothing():
    ev_ = asyncio.Event()
    ev_.set()
    t = Transport()
    assert collect(Q, None, SearchClient(t, api_key=KEY), cancel_event=ev_) == []
    assert t.calls == []


def test_cancel_during_search_no_late_output():
    started = asyncio.Event()
    cancelled = []

    async def slow(query, *, api_key, timeout_s):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    async def run():
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        return [s async for s in web_supplements(Q, None, SearchClient(slow, api_key=KEY), cancel_event=cancel)]
    assert asyncio.run(run()) == [] and cancelled


def test_cancel_between_points_stops_output():
    async def run():
        cancel = asyncio.Event()
        got = []
        async for s in web_supplements(Q, None, SearchClient(Transport(), api_key=KEY), cancel_event=cancel):
            got.append(s)
            cancel.set()
        return got
    assert len(asyncio.run(run())) == 1


def test_cancel_set_before_second_point_checked_on_resume():
    async def run():
        cancel = asyncio.Event()
        agen = web_supplements(Q, None, SearchClient(Transport(), api_key=KEY), cancel_event=cancel)
        first = await agen.__anext__()
        cancel.set()
        rest = [s async for s in agen]
        return first, rest
    first, rest = asyncio.run(run())
    assert first.kind == "point" and rest == []


# ---- latency eval ----
def test_eval_simulated_report():
    qs = ev.synthetic_questions(50)
    assert len(qs) == 50
    clk = ev.FakeClock()
    tr = ev.mock_transport(clk, seed=1)
    rep = asyncio.run(ev.run_eval(qs, lambda: SearchClient(tr, api_key=None), clk))
    assert rep["n"] == 50 and 0 <= rep["per_run_pass_rate"] <= 1
    assert rep["first_p50_ms"] <= rep["first_p95_ms"] and rep["timeouts"] >= 0
    assert ev.pct([], 50) is None and ev.pct([5], 95) == 5


def test_eval_main_labels(capsys, monkeypatch):
    assert ev.main(["-n", "5"]) == 0
    assert capsys.readouterr().out.startswith("[SIMULATED]")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert ev.main(["--real"]) == 2
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    assert ev.main(["--real"]) == 2


def test_eval_real_uses_injected_transport(capsys, monkeypatch):
    mod = type(sys)("fake_real_tp")

    async def tp(query, *, api_key, timeout_s):
        return GOOD
    mod.factory = lambda: tp
    monkeypatch.setitem(sys.modules, "fake_real_tp", mod)
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    assert ev.main(["--real", "--transport", "fake_real_tp:factory", "-n", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[REAL]") and KEY not in out
