"""真人录音 ASR 追赶实测（本地 sherpa-onnx 流式，不外发任何音频/文本）。

用法: .venv/bin/python tools/probe_real_audio_local.py eval/audio16k/pdd.wav [--max-min N] [--lookahead-ms 600] [--rule2 1.2] [--tag X]
口径同 probe_real_audio.py：本地 VAD 的每个 speech_end(t_end) 之后，事件 audio_covered_until_ms >= t_end-100 首次满足所需的音频时钟等待。
本地没有网络/计费，不按墙钟节拍；但每推一帧(20ms)后立即 drain 事件，arr_ms = 已推音频时刻，与实时节拍等价
（前提 RTF<1 且单次 push 耗时 <20ms，summary 里给出 RTF 与 push 耗时分位以便核对）。
产出（含转写文本=个人数据，只留本地）: eval/reports/real/<id>.local[.tag].events.jsonl 与 .summary.json，
并读取同目录 <id>.summary.json / <id>.events.jsonl（阿里）做并排对照。终端不打印转写原文。
"""
import argparse
import asyncio
import difflib
import json
import random
import re
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.asr.sherpa import DEFAULT_MODEL_DIR, SherpaStreamingASR  # noqa: E402
from app.audio.vad import make_vad  # noqa: E402

FRAME = 640
WINDOWS = (200, 300, 500, 700, 1000)
MODEL = 'sherpa-onnx-streaming-paraformer-bilingual-zh-en (int8)'


def pct(xs, p):
    return float(np.percentile(xs, p)) if len(xs) else None


def stats(xs):
    ok = [x for x in xs if x is not None]
    return dict(n=len(xs), none=len(xs) - len(ok), p50=pct(ok, 50), p95=pct(ok, 95), p99=pct(ok, 99),
                within={str(w): sum(1 for x in ok if x <= w) / max(1, len(xs)) for w in WINDOWS})


def vad_ends(pcm):
    vad = make_vad('meeting')
    return [ev.t_ms for i in range(len(pcm) // FRAME)
            for ev in vad.feed(pcm[i * FRAME:(i + 1) * FRAME], i * 20) if ev.kind == 'speech_end']


async def run_asr(raw, lookahead_ms, rule2):
    asr = SherpaStreamingASR(source='meeting', lookahead_ms=lookahead_ms, rule2=rule2)
    await asr.start('probe')
    log, pushed = [], [0]

    async def consume():
        async for e in asr.events():
            log.append(dict(arr_ms=pushed[0] * 20, text=e.text, final=e.is_final, ver=e.text_version,
                            cov=e.audio_covered_until_ms, s=e.t_audio_start_ms, e_end=e.t_audio_end_ms))

    task = asyncio.create_task(consume())
    n = len(raw) // FRAME
    wall0 = time.perf_counter()
    for i in range(n):
        pushed[0] = i + 1
        await asr.push(raw[i * FRAME:(i + 1) * FRAME], i * 20)
        for _ in range(3):  # 让消费者在下一帧之前取走本帧产生的事件
            await asyncio.sleep(0)
        assert len(log) == asr.n_events, 'drain 落后于推送，arr_ms 不可信'
    await asr.close()
    await task
    return log, asr.infer_ms, time.perf_counter() - wall0


def norm(t):
    return re.sub(r'[\W_]+', '', t.lower())


def text_quality(local, aliyun):
    """按时间把本地 final 与阿里 final 对齐（阿里 final 中点落在本地片段 [s, arr_ms] 内），只输出字符数与相似度。"""
    ali = [((e['s'] + e['e_end']) / 2, e['text']) for e in aliyun if e.get('final')]
    rows = []
    for e in (x for x in local if x['final']):
        a = ''.join(t for mid, t in ali if e['s'] <= mid < e['arr_ms'])
        l = e['text']
        if norm(l) and norm(a):
            rows.append((len(norm(l)), len(norm(a)), difflib.SequenceMatcher(None, norm(l), norm(a)).ratio()))
    if not rows:
        return {}
    r = np.array(rows)
    random.seed(0)
    pick = random.sample(range(len(rows)), min(5, len(rows)))
    return dict(aligned_sentences=len(rows), local_chars=int(r[:, 0].sum()), aliyun_chars=int(r[:, 1].sum()),
                mean_similarity=float(r[:, 2].mean()), median_similarity=float(np.median(r[:, 2])),
                sample5=[dict(local_chars=int(rows[i][0]), aliyun_chars=int(rows[i][1]), similarity=round(rows[i][2], 3))
                         for i in pick])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('wav')
    ap.add_argument('--max-min', type=float, default=None)
    ap.add_argument('--lookahead-ms', type=int, default=600)
    ap.add_argument('--rule2', type=float, default=1.2)
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    w = wave.open(a.wav)
    assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    raw = w.readframes(w.getnframes())
    if a.max_min:
        raw = raw[:int(a.max_min * 60 * 32000)]
    aid = Path(a.wav).stem
    out = ROOT / 'eval' / 'reports' / 'real'
    suffix = f'.local.{a.tag}' if a.tag else '.local'
    log, infer, wall = await run_asr(raw, a.lookahead_ms, a.rule2)
    (out / f'{aid}{suffix}.events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in log))
    dur = len(raw) / 32000
    ends = [t for t in vad_ends(raw) if t < len(raw) // 32 - 1500]
    cover_wait, final_wait = [], []
    for t_end in ends:
        cover_wait.append(next((e['arr_ms'] - t_end for e in log if e['arr_ms'] >= t_end and e['cov'] >= t_end - 100), None))
        final_wait.append(next((e['arr_ms'] - t_end for e in log
                                if e['arr_ms'] >= t_end and e['final'] and e['e_end'] >= t_end - 100), None))
    inf = np.array(infer)
    summary = dict(category='REAL_AUDIO_ASR_CATCHUP_LOCAL（真人混音单轨；非轮次级 K1）', model=MODEL, audio=aid,
                   minutes=len(raw) / 32000 / 60, lookahead_ms=a.lookahead_ms, rule2=a.rule2,
                   speech_ends=len(ends), events=len(log), errors=0,
                   cover_wait_ms=stats(cover_wait), final_wait_ms=stats(final_wait),
                   infer=dict(total_s=float(inf.sum() / 1000), audio_s=dur, rtf=float(inf.sum() / 1000 / dur),
                              push_ms_p50=pct(inf, 50), push_ms_p95=pct(inf, 95), push_ms_p99=pct(inf, 99),
                              push_ms_max=float(inf.max()), frames_over_20ms=int((inf > 20).sum()), wall_s=wall))
    ali_path = out / f'{aid}.summary.json'
    if ali_path.exists() and not a.max_min:
        ali = json.loads(ali_path.read_text())
        summary['vs_aliyun'] = {k: dict(local=summary[k]['within'], aliyun=ali[k]['within'],
                                        local_p=[summary[k][p] for p in ('p50', 'p95', 'p99')],
                                        aliyun_p=[ali[k][p] for p in ('p50', 'p95', 'p99')])
                                for k in ('cover_wait_ms', 'final_wait_ms')}
        ali_ev = [json.loads(l) for l in (out / f'{aid}.events.jsonl').read_text().splitlines() if l]
        summary['text_quality'] = text_quality(log, [e for e in ali_ev if 'error' not in e])
    (out / f'{aid}{suffix}.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


asyncio.run(main())
