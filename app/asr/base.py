"""ASR 适配器基类与重连/降级包装器（M04-1 / M04-5，与具体云服务无关）。接口以 docs/S1a_模块接口契约.md §1 为准。"""
import asyncio
import contextlib
import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Callable, ClassVar

BACKOFFS_S = (0.5, 1.0, 2.0)


class ASRFatalError(Exception):
    """不可重连的失败（如云 ASR 预算用尽且无本地回退）：该路转 down，不进入退避重连。"""


class ASRRetryableError(Exception):
    """可重试的失败（如并发名额占满）：ReconnectingASR 按普通异常退避重连，不得当作致命错误。"""


@dataclass(frozen=True)
class TranscriptEvent:
    source: str
    text: str
    is_final: bool
    text_version: int            # 同一句内单调递增
    audio_covered_until_ms: int  # 该文本已覆盖到的音频时刻
    t_audio_start_ms: int
    t_audio_end_ms: int
    t_received_ns: int           # 后端收到时刻（perf_counter_ns），仅埋点
    # 非数据字段：模拟来源（Mock）置 True，报告层据此标 SIMULATED
    simulated: ClassVar[bool] = False


class ASRAdapter(ABC):
    @abstractmethod
    async def start(self, stream_id: str) -> None: ...

    @abstractmethod
    async def push(self, pcm: bytes, t_ms: int) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptEvent]:
        """断连时抛异常，或在未 close 的情况下结束迭代；close() 后必须结束迭代。"""

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def health(self) -> dict:
        """{"state": "ok|reconnecting|down", "reason": str}"""


class ReconnectingASR(ASRAdapter):
    """包住任意适配器工厂：断连后按 backoffs 退避重连，全部失败则 down。

    重连/down 期间 push 的音频直接丢弃（录音由别处继续），绝不伪造转写事件。
    """

    def __init__(self, factory: Callable[[], ASRAdapter], backoffs=BACKOFFS_S, sleep=asyncio.sleep):
        self._factory = factory
        self._backoffs = tuple(backoffs)
        self._sleep = sleep
        self._inner = None
        self._stream_id = None
        self._queue = asyncio.Queue()
        self._task = None
        self._closed = False
        self._state = 'ok'
        self._reason = ''
        self._last_version = 0
        self._floor = 0
        self._fails = 0  # 连续未稳定（未产出事件）的重连次数
        self._fatal = False  # 预算用尽等不可重连失败

    async def start(self, stream_id):
        self._stream_id = stream_id
        self._inner = self._factory()
        await self._inner.start(stream_id)
        self._task = asyncio.create_task(self._pump())

    async def push(self, pcm, t_ms):
        if self._state != 'ok':
            return
        try:
            await self._inner.push(pcm, t_ms)
        except ASRFatalError as e:
            self._fatal = True
            self._state, self._reason = 'down', str(e)
            await self._safe_close(self._inner)  # 让 pump 看到迭代结束并停止
        except Exception as e:
            self._state, self._reason = 'reconnecting', str(e)
            await self._safe_close(self._inner)  # 让 pump 看到迭代结束并进入重连

    async def events(self):
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._safe_close(self._inner)
        self._queue.put_nowait(None)

    def health(self):
        return {'state': self._state, 'reason': self._reason}

    async def _pump(self):
        while True:
            reason = '转写流意外结束'
            try:
                async for ev in self._inner.events():
                    self._fails = 0  # 产出过事件才算连接稳定
                    self._queue.put_nowait(self._monotonic(ev))
            except ASRFatalError as e:
                self._fatal = True
                self._state, self._reason = 'down', str(e)
                self._queue.put_nowait(None)
                return
            except Exception as e:
                reason = str(e)
            if not await self._recover(reason):
                self._queue.put_nowait(None)
                return

    def _monotonic(self, ev):
        v = max(ev.text_version + self._floor, self._last_version + 1)
        if ev.is_final:
            self._last_version = self._floor = 0
        else:
            self._last_version = v
        return dataclasses.replace(ev, text_version=v)

    async def _recover(self, reason):
        if self._fatal:
            return False
        self._state, self._reason = 'reconnecting', reason
        await self._safe_close(self._inner)
        while self._fails < len(self._backoffs):
            await self._sleep(self._backoffs[self._fails])
            self._fails += 1
            try:
                inner = self._factory()
                await inner.start(self._stream_id)
            except Exception as e:
                reason = str(e)
                continue
            self._inner = inner
            self._floor = self._last_version  # 新连接的版本号接在已发出的最大版本之后
            self._state, self._reason = 'ok', ''
            return True
        self._state, self._reason = 'down', f'重连 {len(self._backoffs)} 次失败: {reason}'
        return False

    @staticmethod
    async def _safe_close(adapter):
        with contextlib.suppress(Exception):
            await adapter.close()
