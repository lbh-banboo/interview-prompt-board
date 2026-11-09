"""L3 联网：脱敏词表、预算租约、传输缺失短路；产出 app.search.stream.Supplement 序列。

没有注入 transport 时直接给 unavailable，不构造 SearchClient，不发起任何请求。
个人经历 / 行为情景由 Router 保证不联网，调用方再过滤一次。
"""
import re

from ..search.client import SearchClient
from ..search.stream import STATUS_TEXT, Supplement, web_supplements
from ..search.transport import make_transport
from .answer_ext import take

_TERM = re.compile(r'([一-鿿A-Za-z0-9]{2})(项目|公司|集团|银行|大学|科技)')
_GENERIC = set('个该本此的了过某每整各新做搞发与责导带是在和及为这那')


def build_vocab(entries, names=()):
    """entries：主库已审核条目 dict{id: entry}。项目/公司/机构名 -> terms（命中即不外发）；回答者姓名/代号 -> names（删除）。"""
    terms = set()
    for e in entries.values():
        for m in _TERM.finditer(f"{e['question']} {e['approved_answer']}"):
            if m.group(1)[-1] not in _GENERIC:
                terms.add(m.group(0))
    return dict(names=sorted({n for n in names if n}), terms=sorted(terms))


def _status(kind, code=''):
    return Supplement(kind, 0, text=STATUS_TEXT[kind], code=code)


def make_search_client(ext, timeout_s):
    """返回 (client, reason)；注入的 transport 优先（不要求 Key），其次 search.live 的真实传输。"""
    transport = ext.search_transport
    key = ext.key('SEARCH_API_KEY', 'DEEPSEEK_API_KEY')  # 未单独配置搜索 Key 时复用 DeepSeek Key
    if transport is None:
        if not ext.search_live:
            return None, 'NO_TRANSPORT'
        if not key:
            return None, 'NO_KEY'
        transport = make_transport(ext.search_endpoint, count=ext.search_count,
                                   need_content=ext.search_need_content)
    return SearchClient(transport, api_key=key or '', timeout_s=timeout_s), ''


async def web_items(ext, session, query, vocab, cancel, clock):
    sw = ext.switches()
    if not sw.get('web_questions'):
        yield _status('disabled')
        return
    first_ms, total_ms = ext.web_timeouts()
    client, why = make_search_client(ext, total_ms / 1000)
    if client is None:
        yield _status('unavailable', why)
        return
    lease = take(ext.budget, 'search', 1, session)
    if not lease.ok:
        yield Supplement('budget', 0, text=lease.message)
        return
    called = False
    try:
        async for s in web_supplements(query, vocab, client, first_deadline_ms=first_ms, total_timeout_s=total_ms / 1000,
                                       cancel_event=cancel, clock=clock):
            called = called or s.kind in ('point', 'no_result')
            yield s
    finally:
        if called:
            lease.settle(1)
        else:
            lease.cancel()
