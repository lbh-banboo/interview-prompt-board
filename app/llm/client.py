"""DeepSeek 流式客户端（httpx 异步 + SSE；OpenAI 兼容 chat/completions）。

错误分类改写自 JWM0203/MeetingCopilot@337973a0a2e34424fb541a158d89533b5c4514cd:electron/providerErrors.ts, Apache-2.0 © 2026 JWM0203.
Modified: 只保留 DeepSeek 相关的 HTTP 状态/传输层分类，改为 Python，错误文案只含中文短句。

- transport 可注入（测试用 httpx.MockTransport）；本模块自身不做任何联网动作，除非调用方给了真实 transport 并调用 stream()。
- API key 只从构造参数 / 环境变量 DEEPSEEK_API_KEY 读取；不进 repr、日志、异常。
- 3s 无首 token 放弃；整体超时；CancelledError 原样向上传播。
"""
import asyncio
import json
import os
import re

import httpx

from ..logging_safe import redact as safe_redact

_SECRET = re.compile(r'sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+', re.I)

MESSAGES = {
    'NO_KEY': '未配置 DeepSeek 凭证',
    'INVALID_KEY': 'API Key 无效或已删除',
    'INSUFFICIENT_BALANCE': '账号余额不足',
    'PERMISSION_DENIED': '没有访问该模型的权限',
    'MODEL_NOT_FOUND': '模型不可用或名称已变化',
    'BAD_REQUEST': '请求参数有误',
    'RATE_LIMITED': '请求过于频繁',
    'PROVIDER_ERROR': '服务商暂时异常',
    'TIMEOUT': '服务响应超时',
    'NETWORK_UNREACHABLE': '无法连接服务商',
    'UNKNOWN_ERROR': '未知错误',
}
_STATUS = {400: 'BAD_REQUEST', 422: 'BAD_REQUEST', 401: 'INVALID_KEY', 402: 'INSUFFICIENT_BALANCE',
           403: 'PERMISSION_DENIED', 404: 'MODEL_NOT_FOUND', 429: 'RATE_LIMITED'}
_RETRYABLE = {'RATE_LIMITED', 'TIMEOUT', 'NETWORK_UNREACHABLE', 'PROVIDER_ERROR'}


class LLMError(Exception):
    def __init__(self, code, detail=''):
        self.code = code
        self.retryable = code in _RETRYABLE
        self.detail = safe_redact(redact(detail))[:200]
        super().__init__(MESSAGES[code])


def redact(text, key=None):
    text = _SECRET.sub('[REDACTED]', text or '')
    return text.replace(key, '[REDACTED]') if key else text


def normalize_error(exc=None, status=None, body=''):
    """任意失败 -> LLMError（鉴权/余额/限流/超时/网络/服务端）。"""
    if isinstance(exc, LLMError):
        return exc
    if status is not None:
        code = _STATUS.get(status) or ('PROVIDER_ERROR' if status >= 500 else 'UNKNOWN_ERROR')
        return LLMError(code, body)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return LLMError('TIMEOUT')
    if isinstance(exc, httpx.TransportError):
        return LLMError('NETWORK_UNREACHABLE')
    return LLMError('UNKNOWN_ERROR', type(exc).__name__)


class DeepSeekClient:
    def __init__(self, api_key=None, *, base_url='https://api.deepseek.com', model='deepseek-flash',
                 transport=None, first_token_timeout=3.0, total_timeout=20.0):
        self._api_key = api_key or os.environ.get('DEEPSEEK_API_KEY') or ''
        self.base_url, self.model, self.transport = base_url.rstrip('/'), model, transport
        self.first_token_timeout, self.total_timeout = first_token_timeout, total_timeout

    def __repr__(self):
        return f'DeepSeekClient(model={self.model!r}, has_key={bool(self._api_key)})'

    @property
    def has_key(self):
        return bool(self._api_key)

    async def stream(self, messages, *, temperature=0.2, max_tokens=800):
        """异步生成器：逐段产出 delta 文本。失败统一抛 LLMError。"""
        if not self._api_key:
            raise LLMError('NO_KEY')
        payload = {'model': self.model, 'messages': messages, 'stream': True, 'temperature': temperature,
                   'max_tokens': max_tokens, 'thinking': {'type': 'disabled'}}
        headers = {'Authorization': f'Bearer {self._api_key}', 'Content-Type': 'application/json'}
        try:
            async with asyncio.timeout(self.total_timeout):
                async with httpx.AsyncClient(transport=self.transport, timeout=self.total_timeout) as http:
                    async with http.stream('POST', f'{self.base_url}/chat/completions',
                                           json=payload, headers=headers) as resp:
                        if resp.status_code != 200:
                            body = (await resp.aread()).decode('utf-8', 'replace')
                            raise normalize_error(status=resp.status_code, body=redact(body, self._api_key))
                        deltas = _deltas(resp)
                        first = True
                        while True:
                            try:
                                d = await (asyncio.wait_for(anext(deltas), self.first_token_timeout)
                                           if first else anext(deltas))
                            except StopAsyncIteration:
                                return
                            first = False
                            yield d
        except asyncio.CancelledError:
            raise
        except LLMError:
            raise
        except Exception as e:
            raise normalize_error(e) from None

    async def complete(self, messages, **kw):
        return ''.join([d async for d in self.stream(messages, **kw)])


async def _deltas(resp):
    async for line in resp.aiter_lines():
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            return
        try:
            text = json.loads(data)['choices'][0]['delta'].get('content')
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if text:
            yield text
