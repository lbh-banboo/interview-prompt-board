"""M10-3 联网补充流：脱敏 -> Search -> 首条有效要点立即产出 -> 逐条追加；8s 超时/取消即停。

产出 Supplement.kind：point（有来源的外部参考）、not_suitable、disabled、unavailable、
no_result、timeout。仅 point 算答案，其余是状态提示。cancel_event 触发后不再产出任何项
（迟到结果由调用方归档）。blocked 路径不会调用 client。
"""
import asyncio
import time
from dataclasses import dataclass, field

from app.privacy.redact import redact

from .client import SearchError
from .relevance import DEFAULT_THRESHOLD, SOURCE_TAG, overlap_score, select_points

STATUS_TEXT = {
    "not_suitable": "该问题不适合联网",
    "disabled": "联网检索已关闭",
    "unavailable": "外部补充不可用",
    "no_result": "暂无可靠的外部参考",
    "timeout": "外部补充超时",
}


@dataclass
class Supplement:
    kind: str
    elapsed_ms: int
    text: str = ""
    title: str = ""
    url: str = ""
    snippet: str = ""
    source_tag: str = ""
    within_first_deadline: bool = None  # 仅首条 point 有值
    code: str = ""
    filtered: int = 0  # 被相关度规则过滤的条数（health/metrics 计数口径）
    filtered_reasons: dict = field(default_factory=dict)


def _status(kind, ms, code="", stats=None):
    out = Supplement(kind, ms, text=STATUS_TEXT[kind], code=code)
    if stats:
        out.filtered = stats.get("dropped", 0)
        out.filtered_reasons = dict(stats.get("reasons") or {})
    return out


def _filter_code(stats):
    """全部因实体不匹配被丢弃时给调用方一个可操作的诊断码。"""
    reasons = (stats or {}).get("reasons") or {}
    return "ENTITY_MISMATCH" if reasons and set(reasons) == {"entity_mismatch"} else ""


async def web_supplements(question, vocab, client, *, first_deadline_ms=1500, total_timeout_s=8,
                          cancel_event=None, clock=time.monotonic, threshold=DEFAULT_THRESHOLD,
                          scorer=overlap_score):
    cancel_event = cancel_event or asyncio.Event()
    start = clock()
    ms = lambda: int((clock() - start) * 1000)

    r = redact(question, vocab)
    if r.blocked:
        yield _status("not_suitable", ms())
        return
    if not client.enabled:
        yield _status("disabled", ms())
        return
    if cancel_event.is_set():
        return

    search = asyncio.ensure_future(client.search(r.text))
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({search, waiter}, timeout=total_timeout_s, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        if not search.done():
            search.cancel()
    if cancel_event.is_set():
        return
    if not search.done() or search.cancelled() or ms() > total_timeout_s * 1000:
        yield _status("timeout", ms())
        return
    try:
        hits = search.result()
    except SearchError as e:
        yield _status("unavailable", ms(), e.code)
        return

    stats = {}
    points = select_points(r.text, hits, threshold=threshold, scorer=scorer, stats=stats)
    if not points:
        yield _status("no_result", ms(), _filter_code(stats), stats=stats)
        return
    for i, p in enumerate(points):
        if cancel_event.is_set():
            return
        if ms() > total_timeout_s * 1000:
            yield _status("timeout", ms())
            return
        t = ms()
        yield Supplement("point", t, text=p.snippet, title=p.title, url=p.url, snippet=p.snippet,
                         source_tag=SOURCE_TAG, within_first_deadline=(t <= first_deadline_ms) if i == 0 else None,
                         filtered=stats.get("dropped", 0) if i == 0 else 0,
                         filtered_reasons=dict(stats.get("reasons") or {}) if i == 0 else {})
