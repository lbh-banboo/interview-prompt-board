"""VAD 推理耗时基准（M05-1）。结果为本机实测，随机器/负载变化，不可跨机器引用。

用法: .venv/bin/python tools/bench_vad.py [--frames 3000]
冷启动 = 构造(加载模型) + 前 COLD_N 个 20ms 帧的 feed 耗时；热运行 = 预热后的逐帧 feed 耗时。
另报 Silero 单窗口(512 样本)纯模型推理耗时。输入为合成语音/静音交替，不含真实人声。
"""
import argparse
import os
import platform
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.audio.vad import EnergyVad, SileroVad  # noqa: E402

COLD_N = 10
WARMUP = 200


def synth(n_frames, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n_frames * 320) / 16000
    env = (np.sin(2 * np.pi * 0.25 * t) > 0).astype(np.float32)  # 2s 语音 / 2s 静音
    voice = 0.2 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    x = voice * env + 0.003 * rng.standard_normal(len(t))
    return (np.clip(x, -1, 1) * 32767).astype('<i2')


def pct(a, p):
    return float(np.percentile(a, p))


def run(name, factory, pcm, n):
    t0 = time.perf_counter_ns()
    v = factory()
    load_ms = (time.perf_counter_ns() - t0) / 1e6
    cost = []
    for i in range(n):
        chunk = pcm[i * 320:(i + 1) * 320].tobytes()
        t0 = time.perf_counter_ns()
        v.feed(chunk, i * 20)
        cost.append((time.perf_counter_ns() - t0) / 1e6)
    cost = np.array(cost)
    cold, hot = cost[:COLD_N], cost[WARMUP:]
    return name, load_ms, cold, hot


def row(label, a):
    return f'| {label} | {len(a)} | {pct(a, 50):.3f} | {pct(a, 95):.3f} | {a.max():.3f} |'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--frames', type=int, default=3000)
    n = ap.parse_args().frames
    pcm = synth(n)
    print(f'# VAD 耗时基准 -- 本机实测 (REAL_LOCAL_MEASUREMENT, 合成输入)\n{platform.platform()} / {platform.machine()} / python {platform.python_version()}\n')
    results = [run('EnergyVad', lambda: EnergyVad('meeting'), pcm, n)]
    try:
        results.append(run('SileroVad', lambda: SileroVad('meeting'), pcm, n))
    except (ImportError, FileNotFoundError) as e:
        print(f'SileroVad 不可用: {e}\n')
    print('单位 ms / 每个 20ms 帧的 feed() 调用 (Silero 每 32ms 窗口跑一次模型，故部分帧耗时≈0)')
    print('| 实现/阶段 | 帧数 | P50 | P95 | max |\n|---|---|---|---|---|')
    for name, load_ms, cold, hot in results:
        print(row(f'{name} 冷启动(前{COLD_N}帧)', cold))
        print(row(f'{name} 热运行(预热{WARMUP}帧后)', hot))
    print()
    for name, load_ms, _, _ in results:
        print(f'{name} 构造/加载耗时: {load_ms:.1f} ms')
    try:
        v = SileroVad('meeting')
        w = (pcm[:512].astype(np.float32) / 32768)
        cost = []
        for i in range(WARMUP + 2000):
            t0 = time.perf_counter_ns()
            v._is_speech(w)
            cost.append((time.perf_counter_ns() - t0) / 1e6)
        print('\nSilero 单窗口纯模型推理 (ms):')
        print('| 阶段 | 次数 | P50 | P95 | max |\n|---|---|---|---|---|')
        print(row('首次调用(冷)', np.array(cost[:1])))
        print(row('热运行', np.array(cost[WARMUP:])))
    except (ImportError, FileNotFoundError):
        pass


if __name__ == '__main__':
    main()
