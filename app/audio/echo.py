"""线上模式回声检测（M02-5）：mic 与 meeting 的滑动窗口归一化互相关。

外放且未戴耳机时，meeting 声音经扬声器回灌进 mic，两路高度相关（带 ≤200ms 延迟）。
相关系数 >0.6 持续 ≥hold_s 秒则提示“可能未戴耳机”。纯 numpy；时间只来自样本数，不读墙钟。
echo_energy_ratio 供 domain.roles.echo_dominant(mic_rms, meeting_rms) 的调用方使用。
"""
import numpy as np

SAMPLE_RATE = 16000
SILENCE_RMS = 0.002  # 归一化到 [-1,1] 后低于此值视为静音，不参与相关
HINT = '可能未戴耳机，建议佩戴耳机'


def to_float(pcm):
    if isinstance(pcm, (bytes, bytearray)):
        pcm = np.frombuffer(pcm, dtype='<i2')
    a = np.asarray(pcm)
    return a.astype(np.float64) / 32768.0 if a.dtype.kind == 'i' else a.astype(np.float64)


def rms(pcm):
    a = to_float(pcm)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def echo_energy_ratio(mic, meeting):
    """mic 与 meeting 的 RMS 比；meeting 无能量而 mic 有能量时为 inf，两者皆静音为 0。"""
    m, g = rms(mic), rms(meeting)
    if g == 0.0:
        return float('inf') if m > 0.0 else 0.0
    return m / g


def max_norm_xcorr(a, b, max_lag):
    """a、b 等长；返回 (峰值相关系数 |r|, lag 样本数)。lag>0 表示 a[t]≈b[t-lag]（a 晚于 b）。
    每个 lag 按各自重叠段能量归一化。"""
    a, b = to_float(a), to_float(b)
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    max_lag = min(max_lag, n - 1)
    size = 1 << (2 * n - 1).bit_length()
    cc = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    ca = np.concatenate(([0.0], np.cumsum(a * a)))
    cb = np.concatenate(([0.0], np.cumsum(b * b)))
    best, best_lag = 0.0, 0
    for lag in range(-max_lag, max_lag + 1):
        k = abs(lag)
        if lag >= 0:  # a[lag:] 对 b[:n-lag]
            ea, eb = ca[n] - ca[k], cb[n - k]
        else:  # a[:n-k] 对 b[k:]
            ea, eb = ca[n - k], cb[n] - cb[k]
        denom = np.sqrt(ea * eb)
        if denom < 1e-12:
            continue
        r = abs(cc[lag % size]) / denom
        if r > best:
            best, best_lag = float(r), lag
    return min(best, 1.0), best_lag


class EchoDetector:
    """喂入等长的 mic/meeting PCM 块；每攒满一个窗口评估一次，连续相关 ≥hold_s 秒触发一次提示。"""

    def __init__(self, sample_rate=SAMPLE_RATE, window_s=1.0, max_lag_ms=200, threshold=0.6, hold_s=3.0,
                 silence_rms=SILENCE_RMS):
        self.sample_rate = sample_rate
        self.window = int(sample_rate * window_s)
        self.window_s = window_s
        self.max_lag = int(sample_rate * max_lag_ms / 1000)
        self.threshold = threshold
        self.hold_s = hold_s
        self.silence_rms = silence_rms
        self._mic = np.zeros(0)
        self._meeting = np.zeros(0)
        self._run_s = 0.0
        self.active = False
        self.last_corr = 0.0
        self.last_lag = 0

    def feed(self, mic, meeting):
        """返回 None 或 dict(type='echo_suspected'|'echo_cleared', ...)，仅在状态翻转时返回。"""
        self._mic = np.concatenate((self._mic, to_float(mic)))
        self._meeting = np.concatenate((self._meeting, to_float(meeting)))
        event = None
        while min(self._mic.size, self._meeting.size) >= self.window:
            m, g = self._mic[:self.window], self._meeting[:self.window]
            self._mic, self._meeting = self._mic[self.window:], self._meeting[self.window:]
            event = self._evaluate(m, g) or event
        return event

    def _evaluate(self, m, g):
        if rms(m) < self.silence_rms or rms(g) < self.silence_rms:
            self.last_corr, self.last_lag = 0.0, 0
        else:
            self.last_corr, self.last_lag = max_norm_xcorr(m, g, self.max_lag)
        if self.last_corr > self.threshold:
            self._run_s += self.window_s
            if self._run_s >= self.hold_s and not self.active:
                self.active = True
                return dict(type='echo_suspected', corr=self.last_corr,
                            lag_ms=self.last_lag * 1000 // self.sample_rate, message=HINT)
            return None
        self._run_s = 0.0
        if self.active:
            self.active = False
            return dict(type='echo_cleared')
        return None
