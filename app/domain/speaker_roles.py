"""线下 1:1 声纹验证与三态归属（M03-4）。

用法（时间轴为音频毫秒，不读墙钟）：
    on_speech_start(t_ms) -> feed(pcm) ... -> on_speech_end(t_ms)
feed 在说话进行中每 hop_ms 滚动提取一次（累计语音 >= min_window_ms 后开始），角色一旦确定或翻转即返回更新；
on_speech_end 返回最终 speaker_segment。所有阈值均为【待真实数据标定】的默认值（M14 / tools/calibrate_voiceprint.py）。

输出 speaker_segment：{source, role, confidence, t_start, t_end, inherited, flags, final, latency_ms}
role: answerer | questioner | unknown。“非本人”不等于提问方：仅当能量正常（近场、非噪声背景）才标 questioner。
flags: overlap / backchannel_candidate / short / noise_background / degraded。
"""
from __future__ import annotations

import numpy as np

from ..audio.voiceprint import SAMPLE_RATE, cosine, to_float

SPM = SAMPLE_RATE // 1000

DEFAULTS = dict(
    high=0.55,              # >= 回答方
    low=0.35,               # <= 非本人
    hop_ms=500,             # 滚动提取间隔
    min_window_ms=800,      # 提取窗口下限；短于此的语音不提取
    max_window_ms=3000,     # 提取窗口上限（取最近这么长）
    min_rms=0.01,           # 低于此视为远场/噪声，非本人也不判提问方
    min_snr_ratio=2.0,      # 提供 noise_rms 时，语音 rms 至少为噪声的倍数
    backchannel_rms=0.02,   # 短语音且能量低于此 -> backchannel_candidate
    context_gap_ms=3000,    # 上下文继承允许的与上一说话段的最大间隔
)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class SpeakerRoleTracker:
    def __init__(self, template=None, extractor=None, source: str = 'room', **params):
        self.p = {**DEFAULTS, **params}
        self.source = source
        self.template = None if template is None else np.asarray(template, np.float32)
        self.extractor = extractor
        self.degraded_reason = None
        if extractor is None:
            self.degraded_reason = 'model_unavailable'
        elif self.template is None:
            self.degraded_reason = 'no_template'
        self.in_answer = False          # 由 M05 状态机设置：是否处于回答中
        self.last_role = None           # 上一段（非 unknown）说话方
        self.last_end_ms = None
        self._active = False

    @property
    def degraded(self) -> bool:
        return self.degraded_reason is not None

    def reset(self) -> None:
        """清空会话内的角色状态（单麦门控切回双通道时调用，避免旧段继承/上下文重复触发）。"""
        self.in_answer = False
        self.last_role = None
        self.last_end_ms = None
        self._active = False
        self._buf = np.zeros(0, np.float32)
        self._sims = []
        self._role = None

    # ---- 说话段生命周期 ----
    def on_speech_start(self, t_ms: int, noise_rms: float | None = None) -> None:
        self._active = True
        self._t0 = t_ms
        self._noise = noise_rms
        self._buf = np.zeros(0, np.float32)
        self._sims: list[float] = []
        self._next_step_ms = self.p['min_window_ms']
        self._role = None
        self._latency = None
        self._overlap = False

    def feed(self, pcm, overlap: bool = False) -> list[dict]:
        """喂入本语音段的 PCM16；overlap=True 表示调用方判定此刻有他人同时说话（如两路 VAD 同时活动）。"""
        if not self._active:
            return []
        self._buf = np.concatenate([self._buf, to_float(pcm)])
        self._overlap = self._overlap or overlap
        dur = len(self._buf) // SPM
        if self.degraded or dur < self._next_step_ms:
            return []
        self._next_step_ms = dur + self.p['hop_ms']
        window = self._buf[-self.p['max_window_ms'] * SPM:]
        try:
            sim = cosine(self.extractor.extract(window), self.template)
        except Exception:  # 运行期提取失败：整体降级，之后按能量+上下文推断
            self.degraded_reason = 'extract_failed'
            return []
        self._sims.append(sim)
        if self._latency is None:
            self._latency = dur
        role = self._decide()[0]
        if role != self._role:
            self._role = role
            return [self._segment(self._t0 + dur, final=False)]
        return []

    def on_speech_end(self, t_ms: int) -> dict | None:
        if not self._active:
            return None
        self._active = False
        seg = self._segment(t_ms, final=True)
        if seg['role'] != 'unknown':
            self.last_role = seg['role']
        self.last_end_ms = t_ms
        return seg

    # ---- 判定 ----
    def _energy_ok(self) -> bool:
        level = _rms(self._buf)
        if level < self.p['min_rms']:
            return False
        return self._noise is None or level >= self._noise * self.p['min_snr_ratio']

    def _decide(self):
        """滚动相似度均值 -> (role, confidence, flags)。均值而非末窗，使咳嗽/笑/“嗯”不致翻转角色。"""
        avg = float(np.mean(self._sims))
        hi, lo = self.p['high'], self.p['low']
        if self._overlap or (max(self._sims) >= hi and min(self._sims) <= lo):
            return 'unknown', 0.0, ['overlap']  # 同段内既像本人又像他人：重叠，不强行归属
        if avg >= hi:
            return 'answerer', 0.5 + 0.5 * min(1.0, (avg - hi) / (1 - hi)), []
        if avg <= lo:
            if not self._energy_ok():
                return 'unknown', 0.0, ['noise_background']
            return 'questioner', 0.5 + 0.5 * min(1.0, (lo - avg) / (lo + 1)), []
        return 'unknown', 0.5 * (1 - abs(avg - (hi + lo) / 2) / ((hi - lo) / 2)), []

    def _context_role(self, t_start: int):
        """无滚动结果时按上下文推断 -> (role, confidence, inherited)。不单凭上下文强判提问方以外的情形。"""
        recent = self.last_end_ms is not None and t_start - self.last_end_ms <= self.p['context_gap_ms']
        if not recent or not self._energy_ok():
            return 'unknown', 0.0, False
        if self.in_answer and self.last_role == 'answerer':
            return 'answerer', 0.4, True
        if not self.in_answer and self.last_role == 'questioner':
            return 'questioner', 0.4, True
        return 'unknown', 0.0, False

    def _segment(self, t_end: int, final: bool) -> dict:
        dur = len(self._buf) // SPM
        inherited = False
        if self.degraded:
            role, conf, inherited = self._degraded_role()
            flags = ['degraded']
        elif self._sims:
            role, conf, flags = self._decide()
        else:
            role, conf, inherited = self._context_role(self._t0)
            flags = ['short']
            if self._overlap:
                role, conf, inherited = 'unknown', 0.0, False
        if self._overlap and 'overlap' not in flags:
            flags.append('overlap')
        if dur < self.p['min_window_ms'] and _rms(self._buf) < self.p['backchannel_rms']:
            flags.append('backchannel_candidate')
        return dict(source=self.source, role=role, confidence=round(conf, 3), t_start=self._t0, t_end=t_end,
                    inherited=inherited, flags=flags, final=final, latency_ms=self._latency)

    def _degraded_role(self):
        """声纹不可用：能量 + 上下文。回答中且有声 -> answerer；非回答中且有声 -> questioner；置信恒低。"""
        if self._overlap or not self._energy_ok():
            return 'unknown', 0.0, False
        return ('answerer' if self.in_answer else 'questioner'), 0.3, True
