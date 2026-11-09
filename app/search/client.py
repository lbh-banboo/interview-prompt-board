"""M10-1 联网检索客户端（DeepSeek Search，同 Key）。只取 snippet+url，不抓全文。

transport 可注入：async (query, *, api_key, timeout_s) -> list[{title,url,snippet}]。
错误归一化思路改写自 MeetingCopilot providerErrors.ts（有序规则表；只输出固定文案，
绝不回显原始异常文本，因此 key 不会进入异常/日志）。
"""
import asyncio
import os
import re
import socket
import ssl
from dataclasses import dataclass

MESSAGES = {
    "DISABLED": "联网检索已关闭",
    "NO_KEY": "未配置搜索凭证",
    "INVALID_KEY": "API Key 无效、已删除或粘贴不完整",
    "PERMISSION_DENIED": "当前 Key 没有访问权限",
    "INSUFFICIENT_BALANCE": "账号余额或额度不足",
    "RATE_LIMITED": "请求过于频繁或达到额度限制",
    "NETWORK_UNREACHABLE": "无法连接服务商，请检查网络",
    "DNS_ERROR": "域名解析失败",
    "TLS_ERROR": "安全连接失败",
    "TIMEOUT": "服务连接超时",
    "PROVIDER_ERROR": "服务商暂时异常",
    "UNKNOWN_ERROR": "外部补充不可用",
}
RETRYABLE = {"RATE_LIMITED", "TIMEOUT", "NETWORK_UNREACHABLE", "DNS_ERROR", "PROVIDER_ERROR"}
_STATUS_CODE = {401: "INVALID_KEY", 402: "INSUFFICIENT_BALANCE", 403: "PERMISSION_DENIED", 429: "RATE_LIMITED"}
_STATUS_IN_TEXT = re.compile(r"\bHTTP\s+(\d{3})\b", re.I)


class SearchError(Exception):
    def __init__(self, code):
        self.code = code
        self.retryable = code in RETRYABLE
        super().__init__(MESSAGES[code])


def normalize_error(exc):
    """任意异常 -> SearchError（有序规则，先传输层后 HTTP 状态）。"""
    if isinstance(exc, SearchError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return SearchError("TIMEOUT")
    if isinstance(exc, ssl.SSLError):
        return SearchError("TLS_ERROR")
    if isinstance(exc, socket.gaierror):
        return SearchError("DNS_ERROR")
    if isinstance(exc, OSError):
        return SearchError("NETWORK_UNREACHABLE")
    status = next((getattr(exc, k) for k in ("status", "status_code") if isinstance(getattr(exc, k, None), int)), None)
    if status is None:
        m = _STATUS_IN_TEXT.search(str(exc))
        status = int(m.group(1)) if m else None
    if status in _STATUS_CODE:
        return SearchError(_STATUS_CODE[status])
    if status is not None and status >= 500:
        return SearchError("PROVIDER_ERROR")
    return SearchError("UNKNOWN_ERROR")


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class Limiter:
    """在途请求上限（默认 4），可整体替换为同样支持 async with 的对象。"""

    def __init__(self, limit=4):
        self._sem = asyncio.Semaphore(limit)

    async def __aenter__(self):
        await self._sem.acquire()

    async def __aexit__(self, *exc):
        self._sem.release()


class SearchClient:
    def __init__(self, transport, *, api_key=None, enabled=True, limiter=None, timeout_s=8.0):
        self._transport = transport
        self._api_key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
        self.enabled = enabled
        self._limiter = limiter or Limiter()
        self._timeout_s = timeout_s

    def __repr__(self):
        return f"SearchClient(enabled={self.enabled}, key={'set' if self._api_key else 'unset'})"

    async def search(self, query):
        if not self.enabled:
            raise SearchError("DISABLED")
        try:
            async with self._limiter:
                raw = await self._transport(query, api_key=self._api_key, timeout_s=self._timeout_s)
        except Exception as e:
            raise normalize_error(e) from None
        return [SearchHit(*(str(r.get(k) or "") for k in ("title", "url", "snippet")))
                for r in raw or [] if isinstance(r, dict)]
