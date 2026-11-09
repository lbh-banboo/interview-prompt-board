"""云 ASR 按音频时长计入 BudgetGuard（M15-3）。

- 首个分片由调用方 reserve；之后每满一个分片按实际时长 settle 并 reserve 下一片。
- 时长按已推送 PCM 计（16k / 16bit / 单声道 = 32000 B/s）。
- 并发占满（inflight）可重试：退避后重试，仍不成功抛 ASRRetryableError（ReconnectingASR 会重连），
  绝不当作 ASRFatalError。
- 预算用尽分两种：有本地回退（fallback）时无缝切到本地转写且不计量金额；没有才抛 ASRFatalError。
- guard 为 None 时不应包装：调用方直接使用原适配器。
"""
import asyncio
import contextlib

from .base import ASRAdapter, ASRFatalError, ASRRetryableError

BYTES_PER_SECOND = 32000
CHUNK_S = 60.0
FALLBACK_NOTICE = '云转写预算用尽，已切本地转写'


class MeteredASR(ASRAdapter):
    def __init__(self, inner, guard, session, lease, *, chunk_s=CHUNK_S,
                 bytes_per_second=BYTES_PER_SECOND, on_exhausted=None, fallback=None, on_fallback=None,
                 retry_delay=2.0, retries=3, sleep=asyncio.sleep):
        self._inner, self._guard, self._session = inner, guard, session
        self._chunk_s, self._bps, self._on_exhausted = chunk_s, bytes_per_second, on_exhausted
        self._fallback, self._on_fallback = fallback, on_fallback
        self._retry_delay, self._retries, self._sleep = retry_delay, retries, sleep
        self._lease, self._acc, self._stopped = lease, 0.0, False
        self._stream_id = None
        self._swapped = False
        self._closed = False

    async def start(self, stream_id):
        self._stream_id = stream_id
        try:
            await self._inner.start(stream_id)
        except BaseException:
            self._cancel_tail()
            raise

    async def push(self, pcm, t_ms):
        if self._stopped:
            return
        await self._inner.push(pcm, t_ms)
        self._acc += len(pcm) / self._bps
        if self._acc < self._chunk_s:
            return
        if self._swapped:  # 本地回退：不再计量/预留
            self._acc = 0.0
            return
        consumed, had_lease = self._acc, self._lease is not None
        self._settle_tail()
        if not had_lease:  # 起始时并发占满、尚无预留：为本片补预留并立即核销
            res = await self._reserve()
            if not res.ok:
                if await self._denied(res, pcm, t_ms):
                    return
            else:
                self._guard.settle(res, consumed)
        res = await self._reserve()
        if not res.ok:
            if await self._denied(res, pcm, t_ms):
                return
        self._lease = res

    async def _denied(self, res, pcm, t_ms):
        """预算/并发拒绝的统一处理：返回 True 表示已回退本地并可继续；否则抛错。"""
        if res.reason == 'inflight':  # 重试后仍占满：可重试错误，交给 ReconnectingASR
            raise ASRRetryableError(res.message)
        if await self._degrade(pcm, t_ms):
            return True
        message, reason = res.message, res.reason
        self._stopped = True
        if self._on_exhausted is not None:
            self._on_exhausted(message, reason)
        raise ASRFatalError(message)

    async def _reserve(self):
        """预算拒绝直接返回；inflight 退避重试，全部失败后返回最后一次拒绝。"""
        res = None
        for attempt in range(self._retries + 1):
            res = self._guard.reserve('asr', self._chunk_s, self._session)
            if res.ok or res.reason != 'inflight':
                return res
            if attempt < self._retries:
                await self._sleep(self._retry_delay)
        return res

    async def _degrade(self, pcm, t_ms):
        if self._fallback is None:
            return False
        make = self._fallback
        try:
            local = make() if callable(make) else make
        except Exception:
            return False
        if local is None:
            return False
        with contextlib.suppress(Exception):
            await self._inner.close()
        try:
            await local.start(self._stream_id)
        except Exception:
            with contextlib.suppress(Exception):
                await local.close()
            return False
        self._inner, self._swapped = local, True
        with contextlib.suppress(Exception):
            await local.push(pcm, t_ms)
        if self._on_fallback is not None:
            self._on_fallback(FALLBACK_NOTICE)
        return True

    def events(self):
        return self._events()

    async def _events(self):
        while True:
            inner = self._inner
            try:
                async for ev in inner.events():
                    yield ev
            except ASRFatalError:
                if self._closed or inner is self._inner:
                    raise
            if self._closed or inner is self._inner:
                return

    async def close(self):
        self._closed = True
        self._stopped = True
        self._settle_tail()
        await self._inner.close()

    def health(self):
        return self._inner.health()

    def _settle_tail(self):
        if self._lease is None:
            self._acc = 0.0
            return
        if self._acc > 0:
            self._guard.settle(self._lease, self._acc)
        else:
            self._guard.cancel(self._lease)
        self._lease, self._acc = None, 0.0

    def _cancel_tail(self):
        if self._lease is not None:
            self._guard.cancel(self._lease)
        self._lease, self._acc = None, 0.0
