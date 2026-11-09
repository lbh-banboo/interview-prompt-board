"""L7 真实可见端到端：带真实 httpx 传输计时的本地服务包装。

只做两件事：
1. 用独立的临时数据目录启动 app.server:create_app（不碰 data/）。
2. 给 DeepSeek 注入一个透明的 httpx 传输包装，记录每个请求的
   「发出 → 首个 SSE 数据块 → 结束/取消」时间，供 L7 报告统计首 token
   延迟、超时与取消。Key 只从临时目录的 .env 读取，本脚本不打印、不落盘。

用法：L7_DATA_DIR=/tmp/xxx L7_LLM_LOG=/tmp/llm.jsonl .venv/bin/python tools/l7_server.py
stdout 首行：{"port": 12345}
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_DIR = os.environ.get('L7_DATA_DIR')
LLM_LOG = os.environ.get('L7_LLM_LOG')


def _log(record):
    if not LLM_LOG:
        return
    record = dict(record, t=round(time.time(), 6))
    with open(LLM_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


class _TimedStream(httpx.AsyncByteStream):
    def __init__(self, stream, started, question, status):
        self._stream, self._started, self._question, self._status = stream, started, question, status
        self._first = None
        self._bytes = 0

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                if self._first is None and chunk:
                    self._first = time.perf_counter()
                self._bytes += len(chunk)
                yield chunk
        except BaseException as exc:  # 含取消：client 的 3s/8s 超时会取消迭代
            _log(dict(event='stream_abort', question=self._question, status=self._status,
                      first_body_ms=None if self._first is None else round((self._first - self._started) * 1000, 1),
                      elapsed_ms=round((time.perf_counter() - self._started) * 1000, 1),
                      bytes=self._bytes, error=type(exc).__name__))
            raise
        else:
            _log(dict(event='stream_end', question=self._question, status=self._status,
                      first_body_ms=None if self._first is None else round((self._first - self._started) * 1000, 1),
                      elapsed_ms=round((time.perf_counter() - self._started) * 1000, 1),
                      bytes=self._bytes))

    async def aclose(self):
        await self._stream.aclose()


class _TimedTransport(httpx.AsyncBaseTransport):
    """包住真实 AsyncHTTPTransport。不记录请求头（Authorization 永不落盘）。"""

    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        started = time.perf_counter()
        question = ''
        try:
            body = json.loads(request.content or b'{}')
            msgs = body.get('messages') or []
            question = next((str(m.get('content', '')) for m in reversed(msgs)
                             if m.get('role') == 'user'), '')[:120]
        except Exception:
            pass
        try:
            resp = await self._inner.handle_async_request(request)
        except BaseException as exc:
            _log(dict(event='request_error', question=question, error=type(exc).__name__,
                      elapsed_ms=round((time.perf_counter() - started) * 1000, 1)))
            raise
        _log(dict(event='request', question=question, status=resp.status_code,
                  headers_ms=round((time.perf_counter() - started) * 1000, 1)))
        return httpx.Response(resp.status_code, headers=resp.headers, request=request,
                              stream=_TimedStream(resp.stream, started, question, resp.status_code))

    async def aclose(self):  # DeepSeekClient 每次新建 AsyncClient；共享传输不能在这里被关掉
        return None


def main():
    if not DATA_DIR:
        raise SystemExit('缺少 L7_DATA_DIR')
    from app.server import create_app  # 延迟导入：先保证 sys.path
    app = create_app(data_dir=DATA_DIR, config={'llm_transport': _TimedTransport()})
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False))

    import threading
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    print(json.dumps({'port': port, 'data_dir': DATA_DIR}), flush=True)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True


if __name__ == '__main__':
    main()
