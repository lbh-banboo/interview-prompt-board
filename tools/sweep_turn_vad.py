"""trigger='coverage' vs 'vad' 对比扫描（G6 配套）。

!!! SIMULATED：语料、ASR 滞后、句中停顿均为固定随机种子合成的脚本化事件，不是真实人声/真实 ASR；
!!! 结果只说明状态机在这些假设下的行为，不能作为 0.3s 目标的达标证据，也不能写成 PASS。

用法: .venv/bin/python tools/sweep_turn_vad.py [--n 400] [--seed 7]
过早口径拆分（vad）：resume=提问方真的重新开口的回滚，semantic=ASR 追赶但语义要素改变的回滚，late=其余；
catchup=ASR 追赶且语义不变（text_updated，不计过早）。过早 = resume+semantic+late。
注意：按字数线性截取 partial 会放大 stale（提早 commit 的文本过短），故另给 U(60,400) 对照看结论对该假设的敏感度。
ASR 模型：每个样本一个滞后 L~U(60,900)ms；到达时刻 a 的 partial 只含音频 <= a-L 对应的文本前缀（按字数线性比例），
audio_covered_until = a-L；a=end+L 时发 final 全文。VAD / 句中停顿 / 语料沿用 tools/sweep_turn.py 的假设。
指标：延迟 = 最后一次 committed(针对真实说完时刻) 的 t_ms - 说完时刻；过早 = rolled_back 次数（text_updated 不计）；漏判 = 说完后 3s 内无 committed。
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sweep_turn import (FRAGMENTS, HANGOVER_MS, HIGH_Q, LOW_Q, MISS_WINDOW_MS, ONSET_MS, PAUSE_MS, PAUSE_RATE, TAILS,  # noqa: E402
                        Tr, V, pct)
from app.domain.turn_detector import TurnDetector  # noqa: E402

LAGS = ((60, 900), (60, 400))  # 主模型 U(60,900)；对照 U(60,400)
PARTIAL_EVERY_MS = 100


def make_sample(rng, base, lag_range):
    segs = []
    if rng.random() < PAUSE_RATE:
        segs += [str(rng.choice(FRAGMENTS)), str(rng.choice(TAILS))]
    else:
        segs.append(str(rng.choice(HIGH_Q if rng.random() < 0.6 else LOW_Q)))
    lag = int(rng.integers(*lag_range))
    ev, t, ver = [], base, 0
    for i, text in enumerate(segs):
        start, end = t, t + int(rng.integers(900, 2200))
        ev.append((start + ONSET_MS, 'vad', V('speech_start', start)))
        ev.append((end + HANGOVER_MS, 'vad', V('speech_end', end)))
        for a in range(start + lag + PARTIAL_EVERY_MS, end + lag, PARTIAL_EVERY_MS):
            cov = a - lag
            ver += 1
            n = max(1, int(len(text) * (cov - start) / (end - start)))
            ev.append((a, 'tr', Tr(text[:n], False, ver, min(cov, end), min(cov, end), start)))
        ver += 1
        ev.append((end + lag, 'tr', Tr(text, True, ver, end, end, start)))
        if i + 1 < len(segs):
            t = end + int(rng.integers(*PAUSE_MS))
    return ev, end


def run_sample(trigger, T_silence, events, end):
    d = TurnDetector(T_silence=T_silence, trigger=trigger)
    events = sorted(events, key=lambda e: e[0])
    i = 0
    for now in range(0, end + MISS_WINDOW_MS + 1, 20):
        while i < len(events) and events[i][0] <= now:
            t, op, obj = events[i]
            (d.on_vad(obj, now_ms=t) if op == 'vad' else d.on_transcript(obj, now_ms=t))
            i += 1
        d.tick(now)
    commits = [e for e in d.history if e.type == 'committed' and e.t_speech_end_ms == end]
    return d, (commits[-1].t_ms - end if commits else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=400)
    ap.add_argument('--seed', type=int, default=7)
    args = ap.parse_args()
    print('# SIMULATED -- 合成事件序列，不是真实声音/真实 ASR 结果；不得作为 0.3s 达标依据，也不是 PASS')
    for lag_range in LAGS:
        print(f'\n## ASR滞后=U{lag_range}ms n={args.n} seed={args.seed} 停顿占比={PAUSE_RATE} 停顿={PAUSE_MS}ms hangover={HANGOVER_MS}ms')
        print('| trigger | T_silence | P50 | P95 | P99 (ms) | 过早 | 过早率 | resume | semantic | late | catchup(不计) | 候选撤销 | 漏判 | stale |')
        print('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')
        for trigger in ('coverage', 'vad'):
            for T in (150, 200, 250, 300):
                rng = np.random.default_rng(args.seed)
                lat, tot = [], dict(prem=0, res=0, sem=0, late=0, cat=0, wd=0, miss=0, st=0)
                for _ in range(args.n):
                    events, end = make_sample(rng, 500, lag_range)
                    d, latency = run_sample(trigger, T, events, end)
                    tot['prem'] += d.premature_count
                    tot['res'] += d.resume_rollbacks
                    tot['sem'] += d.semantic_rollbacks
                    tot['late'] += d.late_rollbacks
                    tot['cat'] += d.catchup_updates
                    tot['wd'] += d.candidate_withdrawn_count
                    tot['st'] += d.stale_commits
                    if latency is None:
                        tot['miss'] += 1
                    else:
                        lat.append(latency)
                print(f'| {trigger} | {T} | {pct(lat, 50):.0f} | {pct(lat, 95):.0f} | {pct(lat, 99):.0f} | {tot["prem"]} | '
                      f'{tot["prem"] / args.n:.1%} | {tot["res"]} | {tot["sem"]} | {tot["late"]} | {tot["cat"]} | '
                      f'{tot["wd"]} | {tot["miss"]} | {tot["st"]} |')


if __name__ == '__main__':
    main()
