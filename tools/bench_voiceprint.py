"""声纹单段提取耗时基准（M03-2）。结果为本机实测，随机器/负载变化，不可跨机器引用。

用法: .venv/bin/python tools/bench_voiceprint.py [--seconds 3] [--runs 30] [--threads 1]
冷 = 加载模型 + 首次提取；热 = 之后每次提取的 P50/P95。输入为合成噪声/谐波，不含真实人声。模型缺失则输出 NOT TESTED。
"""
import argparse
import os
import platform
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.audio.voiceprint import DEFAULT_MODEL, SAMPLE_RATE, SherpaExtractor, bench_extract  # noqa: E402


def synth(seconds, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    x = 0.2 * np.sin(2 * np.pi * 150 * t) + 0.1 * np.sin(2 * np.pi * 450 * t) + 0.01 * rng.standard_normal(len(t))
    return (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=3.0)
    ap.add_argument('--runs', type=int, default=30)
    ap.add_argument('--threads', type=int, default=1)
    ap.add_argument('--model', default=str(DEFAULT_MODEL))
    a = ap.parse_args(argv)
    if not os.path.isfile(a.model):
        print(f'NOT TESTED: 声纹模型不存在 {a.model}')
        return 1
    r = bench_extract(lambda: SherpaExtractor(a.model, a.threads), synth(a.seconds), a.runs)
    print(f'platform={platform.platform()} python={platform.python_version()} threads={a.threads} '
          f'window={a.seconds}s runs={r["runs"]}')
    print(f'cold_ms={r["cold_ms"]:.1f} hot_p50_ms={r["hot_p50_ms"]:.1f} hot_p95_ms={r["hot_p95_ms"]:.1f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
