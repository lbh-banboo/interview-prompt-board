"""M10-1b DeepSeek Search 真实 HTTP 传输（POST /v1/tools/search，与 Chat 同 Key）。

协议按 2026-11-08 真实调用核对：响应 {"results": [{title, url, snippet, summary, ...}]}；
只取 title/url/snippet（snippet 为空时用 summary 兜底并截短），不请求正文（need_content=false）。
- 查询先脱敏后截断到 128 字符；错误只抛固定 SearchError，不回显请求体/响应体，因此 Key 不会外泄。
- http_transport 仅供测试注入（httpx.MockTransport）；本模块自身不做联网动作，除非调用方调用返回的传输。
"""
import httpx

from .client import SearchError

DEFAULT_ENDPOINT = 'https://api.deepseek.com/v1/tools/search'
MAX_QUERY_CHARS = 128
MAX_COUNT = 50
MAX_SNIPPET_CHARS = 300


class _HTTPStatus(Exception):
    """让 client.normalize_error 按 status 归类；消息不含响应体。"""

    def __init__(self, status):
        self.status = status
        super().__init__(f'HTTP {status}')


def _clamp(count):
    return max(1, min(MAX_COUNT, int(count)))


def make_transport(endpoint=DEFAULT_ENDPOINT, *, count=5, need_content=False, http_transport=None):
    async def transport(query, *, api_key, timeout_s):
        if not api_key:
            raise SearchError('NO_KEY')
        payload = {'query': str(query or '').strip()[:MAX_QUERY_CHARS], 'count': _clamp(count),
                   'need_content': bool(need_content)}
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(transport=http_transport, timeout=timeout_s) as http:
                resp = await http.post(endpoint, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise SearchError('TIMEOUT') from None
        except httpx.TransportError:
            raise SearchError('NETWORK_UNREACHABLE') from None
        if resp.status_code != 200:
            raise _HTTPStatus(resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise SearchError('PROVIDER_ERROR') from None
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise SearchError('PROVIDER_ERROR')
        return [{'title': str(r.get('title') or ''), 'url': str(r.get('url') or ''),
                 'snippet': str(r.get('snippet') or r.get('summary') or '')[:MAX_SNIPPET_CHARS]}
                for r in results if isinstance(r, dict)]

    return transport
