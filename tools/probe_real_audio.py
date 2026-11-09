"""真人录音 ASR 追赶实测（外发：把录音按真实节拍发给阿里 fun-asr-realtime）。

用法: DASHSCOPE_API_KEY 在环境变量；
  .venv/bin/python tools/probe_real_audio.py eval/audio16k/pdd.wav [--chunk-min 5] [--max-min N]
产出（仅本地，含转写文本=个人数据）: eval/reports/real/<id>.events.jsonl 与 <id>.summary.json
指标口径：本地 VAD 的每个 speech_end（t_end）之后，ASR 事件的 audio_covered_until_ms >= t_end-100 首次满足所需的音频时钟等待；
该指标是“ASR 追赶”，混音单轨，不区分说话人，不等于轮次级 K1。
"""
import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.asr.aliyun import AliyunRealtimeASR  # noqa: E402
from app.audio.vad import make_vad  # noqa: E402

FRAME = 640


def pct(xs, p):
    return float(np.percentile(xs, p)) if xs else None


async def run_chunk(pcm, base_ms, out):
    for attempt in range(4):  # 握手偶发超时：重试，不丢整份数据
        asr = AliyunRealtimeASR(source='meeting')
        try:
            await asr.start('probe')
            break
        except Exception as exc:
            print(f'  握手失败({type(exc).__name__})，第 {attempt + 1} 次', flush=True)
            if attempt == 3:
                out.append(dict(error='handshake_failed', base_ms=base_ms))
                return
            await asyncio.sleep(3)
    pushed = [0]  # frames pushed
    n = len(pcm) // FRAME
    ev_log = []

    async def consume():
        try:
            async for e in asr.events():
                ev_log.append(dict(arr_ms=base_ms + pushed[0] * 20, text=e.text, final=e.is_final,
                                   ver=e.text_version, cov=base_ms + e.audio_covered_until_ms,
                                   s=base_ms + e.t_audio_start_ms, e_end=base_ms + e.t_audio_end_ms))
        except Exception as exc:  # 记录并结束该块
            ev_log.append(dict(error=type(exc).__name__))

    task = asyncio.create_task(consume())
    t0 = time.perf_counter()
    for i in range(n):
        await asr.push(pcm[i * FRAME:(i + 1) * FRAME], i * 20)
        pushed[0] = i + 1
        delay = t0 + (i + 1) * 0.02 - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
    await asr.close()
    await task
    out.extend(ev_log)


def vad_ends(pcm):
    vad = make_vad('meeting')
    ends = []
    for i in range(len(pcm) // FRAME):
        for ev in vad.feed(pcm[i * FRAME:(i + 1) * FRAME], i * 20):
            if ev.kind == 'speech_end':
                ends.append(ev.t_ms)
    return ends


def stats(xs):
    ok = [x for x in xs if x is not None]
    return dict(n=len(xs), none=len(xs) - len(ok), p50=pct(ok, 50), p95=pct(ok, 95), p99=pct(ok, 99),
                within={str(w): sum(1 for x in ok if x <= w) / max(1, len(xs)) for w in (200, 300, 500, 700, 1000)})


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('wav')
    ap.add_argument('--chunk-min', type=float, default=5)
    ap.add_argument('--max-min', type=float, default=None)
    a = ap.parse_args()
    w = wave.open(a.wav)
    assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    raw = w.readframes(w.getnframes())
    if a.max_min:
        raw = raw[:int(a.max_min * 60 * 32000)]
    aid = Path(a.wav).stem
    out_dir = ROOT / 'eval' / 'reports' / 'real'
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f'{aid}.events.jsonl').write_text('')
    step = int(a.chunk_min * 60 * 32000) // FRAME * FRAME
    events, ends = [], []
    for k, off in enumerate(range(0, len(raw), step)):
        chunk = raw[off:off + step]
        base = off // 32
        print(f'{aid} chunk {k} base={base}ms len={len(chunk) // 32}ms', flush=True)
        before = len(events)
        await run_chunk(chunk, base, events)
        ends += [base + t for t in vad_ends(chunk) if t < len(chunk) // 32 - 1500]
        with (out_dir / f'{aid}.events.jsonl').open('a') as f:  # 每块落盘，崩溃不丢已跑数据
            f.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in events[before:])
    errors = sum(1 for e in events if 'error' in e)
    evs = sorted((e for e in events if 'error' not in e), key=lambda e: e['arr_ms'])
    cover_wait, final_wait = [], []
    for t_end in ends:
        c = next((e['arr_ms'] - t_end for e in evs if e['arr_ms'] >= t_end and e['cov'] >= t_end - 100), None)
        f = next((e['arr_ms'] - t_end for e in evs if e['arr_ms'] >= t_end and e['final'] and e['e_end'] >= t_end - 100), None)
        cover_wait.append(c)
        final_wait.append(f)
    summary = dict(category='REAL_AUDIO_ASR_CATCHUP（真人混音单轨；非轮次级 K1）', audio=aid, minutes=len(raw) / 32000 / 60,
                   speech_ends=len(ends), events=len(evs), errors=errors,
                   cover_wait_ms=stats(cover_wait), final_wait_ms=stats(final_wait))
    (out_dir / f'{aid}.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False))


asyncio.run(main())
