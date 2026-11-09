"""声纹 embedding 提取（M03-2）：Extractor 抽象 + sherpa-onnx 实现 + 可注入的 FakeExtractor。

输入统一为 16kHz 单声道 PCM16（bytes 或 int16 ndarray）。模板 = 多段 embedding 的 L2 归一化均值。
声纹与模板只在本机内存/本机 SQLite 中流转，本模块不含任何网络代码。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
DEFAULT_MODEL = Path(__file__).resolve().parents[2] / 'models' / 'speaker' / '3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx'


def to_float(pcm) -> np.ndarray:
    """PCM16（bytes / int16 数组）-> float32 [-1, 1]；float 数组原样返回。"""
    if isinstance(pcm, (bytes, bytearray)):
        return np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    x = np.asarray(pcm)
    if x.dtype == np.int16:
        return x.astype(np.float32) / 32768.0
    return x.astype(np.float32)


def cosine(a, b) -> float:
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def mean_template(embeddings) -> np.ndarray:
    """各段先 L2 归一化再取均值并归一化，避免某一段幅值主导。"""
    vs = [np.asarray(e, np.float64) / (np.linalg.norm(e) or 1.0) for e in embeddings]
    m = np.mean(vs, axis=0)
    return (m / (np.linalg.norm(m) or 1.0)).astype(np.float32)


class Extractor:
    def extract(self, pcm) -> np.ndarray:
        raise NotImplementedError


class SherpaExtractor(Extractor):
    """sherpa-onnx SpeakerEmbeddingExtractor。模型缺失/加载失败抛 FileNotFoundError / ImportError / RuntimeError。"""

    def __init__(self, model_path=DEFAULT_MODEL, num_threads: int = 1):
        import sherpa_onnx

        if not Path(model_path).is_file():
            raise FileNotFoundError(f'声纹模型不存在: {model_path}')
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path), num_threads=num_threads)
        if not cfg.validate():
            raise RuntimeError('声纹模型配置无效')
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dim = self._ex.dim

    def extract(self, pcm) -> np.ndarray:
        s = self._ex.create_stream()
        s.accept_waveform(SAMPLE_RATE, to_float(pcm))
        s.input_finished()
        return np.asarray(self._ex.compute(s), np.float32)


class FakeExtractor(Extractor):
    """SIMULATED：确定性假提取器，供离线单测与阈值标定脚本使用。

    默认 fn 取 64 个对数间隔频带的幅度谱（不同基频的合成音得到不同 embedding）；可注入任意 fn(float32)->向量。
    ``calls`` 记录每次输入长度（样本数），便于断言滚动提取节奏。
    """

    def __init__(self, fn=None):
        self.fn = fn or self._spectrum
        self.calls: list[int] = []

    @staticmethod
    def _spectrum(x: np.ndarray) -> np.ndarray:
        mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        edges = np.unique(np.geomspace(2, len(mag) - 1, 65).astype(int))
        bands = np.array([mag[a:b].sum() for a, b in zip(edges[:-1], edges[1:])], np.float32)
        return bands

    def extract(self, pcm) -> np.ndarray:
        x = to_float(pcm)
        self.calls.append(len(x))
        return np.asarray(self.fn(x), np.float32)


def load_extractor(model_path=DEFAULT_MODEL, num_threads: int = 1) -> Extractor | None:
    """加载真实提取器；不可用返回 None（调用方据此降级并暴露 degraded）。"""
    try:
        return SherpaExtractor(model_path, num_threads)
    except (ImportError, FileNotFoundError, RuntimeError):
        return None


def _pct(values, q):
    return float(np.percentile(values, q))


def bench_extract(factory, pcm, runs: int = 20, clock=time.perf_counter) -> dict:
    """冷/热单段提取耗时（毫秒）。冷 = 加载模型 + 首次提取；热 = 之后 runs 次提取的 P50/P95。clock 可注入。"""
    t0 = clock()
    ex = factory()
    ex.extract(pcm)
    cold_ms = (clock() - t0) * 1000
    hot = []
    for _ in range(runs):
        t = clock()
        ex.extract(pcm)
        hot.append((clock() - t) * 1000)
    return dict(cold_ms=cold_ms, hot_p50_ms=_pct(hot, 50), hot_p95_ms=_pct(hot, 95), runs=runs)
