"""T_silence 延迟-错误权衡扫描（M05-3 配套）。

!!! SIMULATED：语料、ASR 覆盖延迟、句中停顿均为按固定随机种子合成的脚本化事件，不是真实人声/真实 ASR/真实浏览器采集；
!!! 输出只能说明状态机在这些假设下的行为，不能作为 0.3s 目标的达标证据，也不能写成 PASS。

用法: .venv/bin/python tools/sweep_turn.py [--n 400] [--seed 7]
合成假设（均可在下方常量改）：
- VAD：speech_end 事件在最后语音后 HANGOVER_MS 到达（t_ms=最后语音时刻）；speech_start 在恢复说话后 ONSET_MS 到达（t_ms=恢复时刻）。
- ASR：含完整句的 partial 在 t_end + U(ASR_LAG) 到达且覆盖到 t_end；此前只有覆盖到 t_end-300 的旧 partial。
- 句中停顿：PAUSE_RATE 的问题在中途停顿 U(PAUSE_MS) 后继续，前半句取自 FRAGMENTS。
指标：延迟 = 最后一次 committed 的 t_ms - 真实说完时刻；过早 = rolled_back 次数；漏判 = 真实说完后 3s 内无 committed。
"""
import argparse
import os
import sys
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.domain.turn_detector import TurnDetector  # noqa: E402

HANGOVER_MS, ONSET_MS = 100, 60
ASR_LAG = (60, 300)
PAUSE_RATE = 0.3
PAUSE_MS = (150, 900)
HIGH_Q = ['你能讲讲分布式事务的区别吗', '请你介绍一下你的项目经历', '你们为什么选择Kafka', '缓存雪崩是怎么处理的', '说说你的优缺点', '这个方案和之前的有什么不同']
LOW_Q = ['你的职业规划', '关于这个项目的并发量', '你们团队的规模']
FRAGMENTS = ['我想问一下你在这个项目里', '你先介绍一下项目然后', '你们为什么选择Kafka', '缓存这块你们用的是Redis']
TAILS = ['负责了哪些模块？', '另外消息队列用什么？', '有没有遇到过数据一致性问题？']
MISS_WINDOW_MS = 3000


@dataclass
class Tr:
    text: str
    is_final: bool
    text_version: int
    audio_covered_until_ms: int
    t_audio_end_ms: int
    t_audio_start_ms: int


@dataclass
class V:
    kind: str
    t_ms: int


def make_sample(rng, base):
    """返回 (events, final_end_ms, 类别 high/low/pause)。events: (到达时刻, 类型, 对象)。"""
    ev, ver = [], 0
    segs = []
    if rng.random() < PAUSE_RATE:
        cat = 'pause'
        segs.append(str(rng.choice(FRAGMENTS)))
        segs.append(str(rng.choice(TAILS)))
    else:
        cat = 'high' if rng.random() < 0.6 else 'low'
        segs.append(str(rng.choice(HIGH_Q if cat == 'high' else LOW_Q)))
    t = base
    for i, text in enumerate(segs):
        dur = int(rng.integers(900, 2200))
        start, end = t, t + dur
        ver += 1
        lag = int(rng.integers(*ASR_LAG))
        ev.append((start + ONSET_MS, 'vad', V('speech_start', start)))
        ev.append((end - 300 + 1, 'tr', Tr(text[:-1] or text, False, ver, end - 300, end - 300, start)))  # 旧 partial，覆盖不足
        ev.append((end + HANGOVER_MS, 'vad', V('speech_end', end)))
        ev.append((end + lag, 'tr', Tr(text, False, ver + 1, end, end, start)))
        ver += 1
        if i + 1 < len(segs):
            t = end + int(rng.integers(*PAUSE_MS))
    return ev, end, cat


def run_sample(T_silence, T_long, events, end):
    d = TurnDetector(T_silence=T_silence, T_long=T_long)
    events = sorted(events, key=lambda e: e[0])
    horizon = end + MISS_WINDOW_MS
    i = 0
    for now in range(0, horizon + 1, 20):
        while i < len(events) and events[i][0] <= now:
            t, op, obj = events[i]
            (d.on_vad(obj, now_ms=t) if op == 'vad' else d.on_transcript(obj, now_ms=t))
            i += 1
        d.tick(now)
    commits = [e for e in d.history if e.type == 'committed' and e.t_speech_end_ms == end]
    return d, (commits[-1].t_ms - end if commits else None)


def pct(a, p):
    return float(np.percentile(a, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=400)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--T_long', type=int, default=700)
    args = ap.parse_args()
    print('# SIMULATED -- 合成事件序列，不是真实声音/真实 ASR 结果；不得作为 0.3s 达标依据')
    print(f'n={args.n} seed={args.seed} T_long={args.T_long} 停顿占比={PAUSE_RATE} 停顿={PAUSE_MS}ms ASR覆盖延迟={ASR_LAG}ms hangover={HANGOVER_MS}ms\n')
    print('| T_silence | n | t_end->committed P50 | P95 | P99 | max (ms) | 过早(rolled_back) | 过早率 | 候选撤销 | 漏判(3s内无commit) |')
    print('|---|---|---|---|---|---|---|---|---|---|')
    detail = []
    for T in (150, 200, 250, 300):
        rng = np.random.default_rng(args.seed)
        lat, premature, withdrawn, missed = [], 0, 0, 0
        by_cat = {'high': [], 'low': [], 'pause': []}
        for _ in range(args.n):
            events, end, cat = make_sample(rng, base=500)
            d, latency = run_sample(T, args.T_long, events, end)
            premature += d.premature_count
            withdrawn += d.candidate_withdrawn_count
            if latency is None:
                missed += 1
            else:
                lat.append(latency)
                by_cat[cat].append(latency)
        print(f'| {T} | {args.n} | {pct(lat, 50):.0f} | {pct(lat, 95):.0f} | {pct(lat, 99):.0f} | {max(lat):.0f} | '
              f'{premature} | {premature / args.n:.1%} | {withdrawn} | {missed} |')
        detail.append(f'| {T} | ' + ' | '.join(f'{len(by_cat[c])} / {pct(by_cat[c], 50):.0f} / {pct(by_cat[c], 95):.0f}' for c in by_cat) + ' |')
    print('\n分类延迟 n / P50 / P95 (ms)：high=高完整度问句，low=无疑问标记的名词性提问(靠 T_long 兜底)，pause=含句中停顿(以最后一段说完计)')
    print('| T_silence | high | low | pause |\n|---|---|---|---|')
    print('\n'.join(detail))


if __name__ == '__main__':
    main()
