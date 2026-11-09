"""MockASR：按脚本确定性回放，不读墙钟；所有事件 simulated=True（报告层标 SIMULATED）。"""
import asyncio
import time
from dataclasses import dataclass

from app.asr.base import ASRAdapter, TranscriptEvent


@dataclass(frozen=True)
class MockTranscriptEvent(TranscriptEvent):
    simulated = True


class MockASR(ASRAdapter):
    """script: [(音频时刻ms, text, is_final, covered_until_ms)]，按时刻升序。

    push(pcm, t_ms) 推进音频时钟，t_ms >= 脚本时刻的条目依次吐出。
    text_version 在一句内从 1 递增，final 之后下一句重新从 1 开始。
    """

    def __init__(self, script, source='meeting', clock_ns=time.perf_counter_ns):
        self._script = list(script)
        self._source = source
        self._clock_ns = clock_ns
        self._i = 0
        self._version = 0
        self._sent_start = None
        self._queue = asyncio.Queue()
        self._stalled = False
        self._broken = None
        self._closed = False

    async def start(self, stream_id):
        self.stream_id = stream_id

    async def push(self, pcm, t_ms):
        if self._broken:
            raise ConnectionError(self._broken)
        self._now = t_ms
        self._release()

    def _release(self):
        while not self._stalled and self._i < len(self._script) and self._script[self._i][0] <= self._now:
            t, text, is_final, covered = self._script[self._i]
            self._i += 1
            self._version += 1
            if self._sent_start is None:
                self._sent_start = t
            self._queue.put_nowait(MockTranscriptEvent(
                self._source, text, is_final, self._version, covered, self._sent_start, t, self._clock_ns()))
            if is_final:
                self._version, self._sent_start = 0, None

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self):
        self._closed = True
        self._queue.put_nowait(None)

    def health(self):
        if self._broken:
            return {'state': 'down', 'reason': self._broken}
        return {'state': 'ok', 'reason': 'stalled' if self._stalled else ''}

    # ---- 故障注入 ----
    def inject_disconnect(self, reason='injected disconnect'):
        self._broken = reason
        self._queue.put_nowait(ConnectionError(reason))

    def inject_stall(self, on=True):
        """停滞：到点的事件被扣住；解除后在下一次 push 时按原顺序补出。"""
        self._stalled = on
