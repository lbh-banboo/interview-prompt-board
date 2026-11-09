"""真实 DeepSeek Search 传输：字段解析容错、错误分类、截断、注入优先、零请求路径。全程 MockTransport，不联网。"""
import asyncio
import json

import httpx
import pytest
from test_answer_routing import BLOOM, Env, Q_BLOOM, SpyGuard

from app.credentials import CredentialStore, MemoryBackend
from app.realtime.answer_ext import Externals
from app.realtime.answer_web import make_search_client, web_items
from app.retrieval.router import PERSONAL_NOTE
from app.search import SearchClient, SearchError
from app.search.transport import DEFAULT_ENDPOINT, make_transport
from app.usage import UsageMeter

KEY = 'sk-fake-search-12345678901234567890'  # pragma: allowlist secret（假 Key）
Q = 'Python asyncio 超时'
# 2026-11-08 真实调用确认的字段结构：results[].{title,site_name,site_icon,authority,rerank_score,date,url,summary,snippet,content}
REAL = [{
    'title': 'Python asyncio 超时处理', 'site_name': '示例站', 'site_icon': 'https://cdn/x.png',
    'authority': 1, 'rerank_score': 0.91, 'date': '2026-02-20T16:00:00+00:00',
    'url': 'https://docs.example.com/asyncio-timeout',
    'summary': 'asyncio 超时可用 wait_for 或 timeout 上下文管理器。',
    'snippet': 'asyncio 超时用 asyncio.wait_for 或 asyncio.timeout 实现。', 'content': '',
}]


class Handler:
    def __init__(self, results=None, status=200, text=None, exc=None):
        self.results, self.status, self.text, self.exc = results if results is not None else REAL, status, text, exc
        self.requests, self.bodies = [], []

    def __call__(self, request):
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        if self.exc:
            raise self.exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json={'results': self.results})


def real(handler, **kw):
    return make_transport(http_transport=httpx.MockTransport(handler), **kw)


def search(query, handler=None, **kw):
    h = handler or Handler()
    hits = asyncio.run(SearchClient(real(h, **kw), api_key=KEY).search(query))
    return hits, h


def test_default_endpoint_and_success_parses_real_fields():
    hits, h = search(Q)
    req, body = h.requests[0], h.bodies[0]
    assert str(req.url) == DEFAULT_ENDPOINT == 'https://api.deepseek.com/v1/tools/search'
    assert req.method == 'POST' and req.headers['authorization'] == f'Bearer {KEY}'
    assert body == {'query': Q, 'count': 5, 'need_content': False}
    assert [(x.title, x.url, x.snippet) for x in hits] == [
        ('Python asyncio 超时处理', 'https://docs.example.com/asyncio-timeout',
         'asyncio 超时用 asyncio.wait_for 或 asyncio.timeout 实现。')]
    assert KEY not in repr(hits)


def test_missing_and_extra_fields_tolerated():
    h = Handler(results=[{'title': '只有标题'}, {}, {'url': 'https://a.com', 'summary': '摘要兜底'}])
    hits, _ = search(Q, h)
    assert [x.snippet for x in hits] == ['', '', '摘要兜底']
    assert [x.url for x in hits] == ['', '', 'https://a.com']


def test_empty_results_and_malformed_response():
    hits, _ = search(Q, Handler(results=[]))
    assert hits == []
    with pytest.raises(SearchError) as e:
        search(Q, Handler(text='{"results": "not-a-list"}'))
    assert e.value.code == 'PROVIDER_ERROR'
    with pytest.raises(SearchError) as e:
        search(Q, Handler(text='not json'))
    assert e.value.code == 'PROVIDER_ERROR'


@pytest.mark.parametrize('status,code', [
    (401, 'INVALID_KEY'), (402, 'INSUFFICIENT_BALANCE'), (403, 'PERMISSION_DENIED'),
    (429, 'RATE_LIMITED'), (500, 'PROVIDER_ERROR'), (503, 'PROVIDER_ERROR'), (418, 'UNKNOWN_ERROR'),
])
def test_http_status_classification_and_no_key_leak(status, code, caplog):
    with pytest.raises(SearchError) as e:
        search(Q, Handler(status=status, text=f'bad key {KEY}'))
    assert e.value.code == code and KEY not in str(e.value) and KEY not in repr(e.value)
    assert e.value.__cause__ is None
    assert KEY not in caplog.text


def test_timeout_and_network_errors_are_short():
    with pytest.raises(SearchError) as e:
        search(Q, Handler(exc=httpx.ReadTimeout('secret ' + KEY)))
    assert e.value.code == 'TIMEOUT' and KEY not in str(e.value)
    with pytest.raises(SearchError) as e:
        search(Q, Handler(exc=httpx.ConnectError('secret ' + KEY)))
    assert e.value.code == 'NETWORK_UNREACHABLE' and KEY not in str(e.value)


def test_query_truncated_to_128_chars():
    long_q = '测' * 200
    _, h = search(long_q)
    assert len(h.bodies[0]['query']) == 128
    # 标题/正文再长也不影响请求体
    assert set(h.bodies[0]) == {'query', 'count', 'need_content'}


def test_redaction_happens_before_truncation():
    from app.search.stream import web_supplements

    async def run():
        return [s async for s in web_supplements('小林' + 'Redis 持久化' * 40, ['小林'], SearchClient(real(h), api_key=KEY))]

    h = Handler(results=[])
    asyncio.run(run())
    sent = h.bodies[0]['query']
    assert '小林' not in sent and sent.startswith('Redis') and len(sent) <= 128


def test_count_and_need_content_from_transport_config():
    _, h = search(Q, Handler(), count=50, need_content=True)
    assert h.bodies[0]['count'] == 50 and h.bodies[0]['need_content'] is True
    _, h = search(Q, Handler(), count=999)  # 越界值夹紧，不抛出
    assert h.bodies[0]['count'] == 50


# ---- 接线：注入优先 / Key 回退 / 无 Key 零请求 ----
def ext(**kw):
    base = dict(outbound=lambda: {'web_questions': True}, timeouts=lambda: {'web_first_ms': 1500, 'web_total_ms': 8000},
                creds=CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': KEY})]))
    base.update(kw)
    return Externals(**base)


def test_injected_transport_wins_over_live():
    calls = []

    async def injected(query, *, api_key, timeout_s):
        calls.append(query)
        return []

    client, why = make_search_client(ext(search_transport=injected, search_live=True), 8.0)
    assert why == '' and client._transport is injected
    asyncio.run(client.search('q'))
    assert calls == ['q']


def test_key_prefers_search_key_then_deepseek():
    e = ext(creds=CredentialStore([MemoryBackend({'SEARCH_API_KEY': 'sk-search-000000000000',
                                                   'DEEPSEEK_API_KEY': KEY})]), search_live=True)
    assert make_search_client(e, 8.0)[0]._api_key == 'sk-search-000000000000'
    e2 = ext(search_live=True)
    assert make_search_client(e2, 8.0)[0]._api_key == KEY


def test_no_key_or_live_off_returns_no_client():
    h = Handler()
    client, why = make_search_client(ext(creds=CredentialStore([MemoryBackend()]), search_live=True), 8.0)
    assert client is None and why == 'NO_KEY'
    client, why = make_search_client(ext(search_live=False), 8.0)
    assert client is None and why == 'NO_TRANSPORT'
    assert h.requests == []


@pytest.mark.asyncio
async def test_web_items_no_key_zero_requests_and_no_budget(tmp_path):
    h = Handler()
    e = ext(creds=CredentialStore([MemoryBackend()]), search_live=True, budget=None)
    out = [s async for s in web_items(e, 's', Q, None, asyncio.Event(), lambda: 0.0)]
    assert [s.kind for s in out] == ['unavailable'] and out[0].code == 'NO_KEY'
    assert h.requests == []


# ---- 端到端：零请求与预算 ----
@pytest.mark.asyncio
async def test_outbound_off_zero_network(tmp_path):
    h = Handler()
    env = Env(tmp_path, search=real(h), llm=False, web=False)
    await env.run(Q_BLOOM)
    assert h.requests == [] and '联网检索已关闭' in env.statuses()
    assert env.guard.log == []


@pytest.mark.asyncio
async def test_personal_experience_zero_network(tmp_path):
    h = Handler()
    env = Env(tmp_path, search=real(h), llm=False)
    await env.run('你负责的项目里缓存怎么设计的')
    assert h.requests == [] and env.statuses() == [PERSONAL_NOTE]
    assert env.guard.log == []


@pytest.mark.asyncio
async def test_budget_denied_zero_network(tmp_path):
    h = Handler()
    guard = SpyGuard(UsageMeter(tmp_path / 'u.db', {'asr_per_hour': 1, 'llm_per_1k_tokens': 1, 'search_per_call': 1}),
                     0, 0, 4)
    env = Env(tmp_path, search=real(h), llm=False, budget=guard)
    await env.run(Q_BLOOM)
    assert h.requests == [] and '预算已用尽' in env.statuses()[-1]
    assert guard.count('reserve') == 0 and guard.count('settle') == 0 and guard.inflight == []


@pytest.mark.asyncio
async def test_empty_result_is_no_result_and_settles(tmp_path):
    env = Env(tmp_path, search=real(Handler(results=[])), llm=False)
    await env.run(Q_BLOOM)
    assert env.statuses()[-1] == '暂无可靠的外部参考'
    assert env.guard.count('settle', 'search') == 1 and env.guard.inflight == []
