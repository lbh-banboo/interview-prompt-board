"""VAD 分段（M05-1）：把 PCM16 流转成 speech_start / speech_end 事件。

接口（契约 §2）：``VadSegmenter(source).feed(pcm, t_ms) -> list[VadEvent]``。
t_ms 是本块第一个样本的音频时刻（= seq * 20）；speech_end.t_ms 为最后一个语音窗口的结束时刻，
不含 hangover 静音等待；speech_start.t_ms 为语音起点（已按起始确认回填到第一个语音窗口）。

两种实现共用同一状态机，只替换"单窗口是否语音"的判决：
- EnergyVad：纯 numpy 能量自适应（降级实现，无外部依赖）。
- SileroVad：sherpa-onnx 的 Silero VAD（窗口 512 样本 / 32ms，内部聚合 20ms 传输帧）。

Ported from JWM0203/MeetingCopilot@337973a0a2e34424fb541a158d89533b5c4514cd:electron/asr/vad.ts, Apache-2.0 © 2026 JWM0203.
Modified: TypeScript -> Python/numpy; 输出由"整段音频 segment"改为 speech_start/speech_end 事件（时间轴为音频毫秒，
不读墙钟）；speech_end 回填为最后语音时刻；新增起始确认(min_speech_ms)、重复/重叠块幂等与缺口处理；
抽出 _is_speech 以便替换为 Silero；自适应噪声底(上升 2%/窗口、下降即时)与 hangover、pre-roll 思路保留。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
SAMPLES_PER_MS = SAMPLE_RATE // 1000
DEFAULT_SILERO_MODEL = Path(__file__).resolve().parents[2] / 'models' / 'silero_vad_16k_op15.onnx'


@dataclass(frozen=True)
class VadEvent:
    kind: str  # 'speech_start' | 'speech_end'
    t_ms: int


class VadSegmenter:
    """状态机基类：按 window_samples 聚合输入，逐窗口调 _is_speech，输出事件。

    hangover_ms：语音后连续静音达到该值才发 speech_end（事件时刻仍回填到最后语音时刻）。
    min_speech_ms：连续语音达到该值才发 speech_start（滤掉点击/爆音），事件时刻回填到起点。
    pre_roll_ms：speech_start 时 ``pre_roll`` 属性保存起点之前这么长的音频（供 ASR 补开头）。
    """

    window_samples = 320

    def __init__(self, source: str, hangover_ms: int = 100, min_speech_ms: int = 60, pre_roll_ms: int = 300):
        self.source = source
        self.hangover_ms = hangover_ms
        self.min_speech_ms = min_speech_ms
        self.pre_roll_ms = pre_roll_ms
        self.pre_roll = np.zeros(0, np.float32)
        self.dropped_chunks = 0  # 被当作重复块丢弃的整块数；时间轴重置（新 generation）而未调 reset() 时会累加
        self.reset()

    # ---- 子类实现 ----
    def _is_speech(self, window: np.ndarray) -> bool:
        raise NotImplementedError

    def _reset_model(self) -> None:
        pass

    # ---- 状态 ----
    def reset(self) -> None:
        """时间轴重启（新 generation / 新会话）：清空全部状态，之后的 t_ms 可重新从任意值开始；dropped_chunks 保留。"""
        self._buf = np.zeros(0, np.float32)
        self._buf_start = None  # buf[0] 的绝对样本序号
        self._ring = np.zeros(0, np.float32)  # 最近音频，供 pre-roll
        self._ring_end = 0  # ring 末尾的绝对样本序号
        self._speaking = False
        self._onset_ms = None
        self._silence_ms = 0
        self._last_speech_end_ms = 0
        self._reset_model()

    @property
    def speaking(self) -> bool:
        return self._speaking

    def feed(self, pcm: bytes, t_ms: int) -> list[VadEvent]:
        x = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        start = t_ms * SAMPLES_PER_MS
        events: list[VadEvent] = []
        if self._buf_start is not None:
            expected = self._buf_start + len(self._buf)
            if start + len(x) <= expected:
                self.dropped_chunks += 1
                return events  # 重复块：幂等；时间轴回退须先 reset()，否则整块丢弃并计数
            if start < expected:  # 部分重叠：丢掉已处理部分
                x = x[expected - start:]
                start = expected
            elif start > expected:  # 缺口：当作流中断，收尾并重新对齐
                events += self._close_stream()
                self.reset()
        if self._buf_start is None:
            self._buf_start = start
        self._buf = np.concatenate([self._buf, x])
        w = self.window_samples
        while len(self._buf) >= w:
            window = self._buf[:w]
            ws = self._buf_start
            self._buf = self._buf[w:]
            self._buf_start += w
            self._remember(window, ws)
            events += self._step(self._is_speech(window), ws // SAMPLES_PER_MS, (ws + w) // SAMPLES_PER_MS)
        return events

    def flush(self) -> list[VadEvent]:
        """流结束：若仍在语音中，按最后语音时刻补发 speech_end。"""
        return self._close_stream()

    def _close_stream(self) -> list[VadEvent]:
        if not self._speaking:
            return []
        self._speaking = False
        return [VadEvent('speech_end', self._last_speech_end_ms)]

    def _remember(self, window: np.ndarray, ws: int) -> None:
        keep = (self.pre_roll_ms + self.min_speech_ms + 2 * self.window_samples // SAMPLES_PER_MS) * SAMPLES_PER_MS
        self._ring = np.concatenate([self._ring, window])[-keep:]
        self._ring_end = ws + len(window)

    def _step(self, speech: bool, start_ms: int, end_ms: int) -> list[VadEvent]:
        if not self._speaking:
            if not speech:
                self._onset_ms = None
                return []
            if self._onset_ms is None:
                self._onset_ms = start_ms
            self._last_speech_end_ms = end_ms
            if end_ms - self._onset_ms < self.min_speech_ms:
                return []
            self._speaking = True
            self._silence_ms = 0
            self._snapshot_pre_roll(self._onset_ms)
            return [VadEvent('speech_start', self._onset_ms)]
        if speech:
            self._silence_ms = 0
            self._last_speech_end_ms = end_ms
            return []
        self._silence_ms += end_ms - start_ms
        if self._silence_ms >= self.hangover_ms:
            self._speaking = False
            self._onset_ms = None
            return [VadEvent('speech_end', self._last_speech_end_ms)]
        return []

    def _snapshot_pre_roll(self, onset_ms: int) -> None:
        onset_sample = onset_ms * SAMPLES_PER_MS
        have = len(self._ring)
        ring_start = self._ring_end - have
        a = max(onset_sample - self.pre_roll_ms * SAMPLES_PER_MS, ring_start)
        self.pre_roll = self._ring[a - ring_start:max(onset_sample - ring_start, 0)].copy()


class EnergyVad(VadSegmenter):
    """纯 numpy 能量自适应 VAD：rms > max(abs_floor, 噪声底 * ratio) 即为语音。

    噪声底仅在非语音窗口更新：上升每窗口最多 +2%，下降立即跟随（同 vad.ts），但不低于 abs_floor/2。
    已知限制：噪声底在语音状态下不更新，背景噪声若瞬间抬高到 ratio 倍以上会被持续判为语音。
    """

    window_samples = 320  # 20ms

    def __init__(self, source: str, abs_floor: float = 0.004, noise_ratio: float = 3.0, **kw):
        self.abs_floor = abs_floor
        self.noise_ratio = noise_ratio
        super().__init__(source, **kw)

    def _reset_model(self) -> None:
        self.noise_floor = self.abs_floor

    def _is_speech(self, window: np.ndarray) -> bool:
        level = float(np.sqrt(np.mean(window * window)))
        speechy = level > max(self.abs_floor, self.noise_floor * self.noise_ratio)
        if not speechy:
            lo = self.abs_floor / 2  # 下限：数字静音(0)不能把噪声底压到 0，否则 0*1.02 永远为 0
            self.noise_floor = min(self.noise_floor * 1.02, max(level, lo))
            if level < self.noise_floor:
                self.noise_floor = max(level, lo)
        return speechy


class SileroVad(VadSegmenter):
    """sherpa-onnx Silero VAD。模型窗口 512 样本(32ms)；sherpa 的 v5 模型每次需要 64 样本上下文 + 512 新样本。"""

    window_samples = 512
    CONTEXT = 64

    def __init__(self, source: str, model_path: str | Path = DEFAULT_SILERO_MODEL, threshold: float = 0.5,
                 num_threads: int = 1, **kw):
        import sherpa_onnx  # 缺包时由调用方（make_vad）降级

        if not Path(model_path).is_file():
            raise FileNotFoundError(f'Silero 模型不存在: {model_path}')
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.window_size = self.window_samples
        cfg.silero_vad.threshold = threshold
        # sherpa 默认内置 0.5s 静音平滑，会把 speech_end 推迟约 500ms；时间平滑一律由本类的 hangover/min_speech 负责
        cfg.silero_vad.min_silence_duration = 0.0
        cfg.silero_vad.min_speech_duration = 0.0
        cfg.sample_rate = SAMPLE_RATE
        cfg.num_threads = num_threads
        self._model = sherpa_onnx.VadModel.create(cfg)
        super().__init__(source, **kw)

    def _reset_model(self) -> None:
        self._model.reset()
        self._ctx = np.zeros(self.CONTEXT, np.float32)

    def _is_speech(self, window: np.ndarray) -> bool:
        full = np.concatenate([self._ctx, window])
        self._ctx = window[-self.CONTEXT:]
        return bool(self._model.is_speech(full))


def make_vad(source: str, prefer: str = 'silero', **kw) -> VadSegmenter:
    """优先 Silero，包或模型不可用时降级为 EnergyVad。"""
    if prefer == 'silero':
        try:
            return SileroVad(source, **kw)
        except (ImportError, FileNotFoundError):
            kw = {k: v for k, v in kw.items() if k in ('hangover_ms', 'min_speech_ms', 'pre_roll_ms')}
    return EnergyVad(source, **kw)
