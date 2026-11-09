"""参数标定（M05-7）：网格扫描 T_silence / T_long / T_answer_silence，输出延迟-错误权衡表。

!!! SIMULATED：语料、ASR 覆盖延迟、回答方 VAD 均为按固定随机种子合成的脚本化事件，不是真实人声 / 真实 ASR；
!!! 输出只说明状态机在这些假设下的行为，不能作为达标证据，也不能写成 PASS。

用法: .venv/bin/python tools/calibrate_turn.py [--n 60] [--seed 7] [--step 40]
合成场景（每个网格点跑同一批样本）：
- next_q   回答方答完后静音 U(GAP_MS)，面试官问下一题 -> 期望 answer_closed + 新题 committed（漏关闭 = 6s 内没有）
- think    回答方中途停顿 U(THINK_MS)（含 >3s、4s）后继续答 -> 期望不关闭、不入队
- queue    回答方仍在说时面试官问了无终止语的新题 -> 期望 queued，回答方答完静音后才 queue_pop 关闭
- stop     “先停一下，换个问题”+ 新题 -> 期望 interrupted + 新题 committed
- back / follow  嗯/好的 与 “那…呢/具体说说” -> 期望不关闭、不入队
指标：Q1 t_end->committed P50/P95、过早三类（resume/semantic/late）、漏判（Q1 3s 内无 committed）、
误关闭（本不该关闭的场景在回答真实结束前出现 answer_closed/interrupted）、漏关闭、误入队（本不该入队的场景出现 queued）、漏入队，
以及 next_q / stop 的 新题 t_end->committed P50/P95。
"""
import argparse
import itertools
import os
import sys
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.domain.turn_detector import TurnDetector  # noqa: E402

HANGOVER_MS, ONSET_MS = 100, 60
ASR_LAG = (60, 300)
GAP_MS = (300, 3500)
THINK_MS = (1500, 5000)
CLOSE_WINDOW_MS = 6000
MISS_WINDOW_MS = 3000
Q1S = ['你能讲讲分布式事务的区别吗', '请你介绍一下你的项目经历', '缓存雪崩是怎么处理的', '说说你的优缺点']
Q2S = ['MySQL的索引怎么优化', '消息队列怎么保证不丢消息', '你们的接口限流是怎么做的']
FOLLOWS = ['那缓存呢？', '具体说说细节', '能展开讲讲吗']
BACKS = ['嗯', '好的', '对']
SCENARIOS = ('next_q', 'think', 'queue', 'stop', 'back', 'follow')
EXPECT_SWITCH = ('next_q', 'stop')
EXPECT_QUEUE = ('queue',)


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


def utter(ev, rng, start, dur, text):
    """一段提问方发言；返回说完时刻。VAD speech_end 在 end+HANGOVER 到达，完整 partial 在 end+lag 到达。"""
    end = start + dur
    ev.append((start + ONSET_MS, 'vad', V('speech_start', start)))
    ev.append((end - 300 + 1, 'tr', Tr(text[:-1] or text, False, 1, end - 300, end - 300, start)))
    ev.append((end + HANGOVER_MS, 'vad', V('speech_end', end)))
    ev.append((end + int(rng.integers(*ASR_LAG)), 'tr', Tr(text, False, 2, end, end, start)))
    return end


def make_sample(rng, scenario):
    """返回 (events, meta)。meta: q1_end / ans_end（回答真实结束）/ q2_end（第二段发言说完，可无）。"""
    ev = []
    q1_end = utter(ev, rng, 500, int(rng.integers(900, 2200)), str(rng.choice(Q1S)))
    a0 = q1_end + int(rng.integers(300, 900))
    ev.append((a0, 'ans', (True, a0)))
    meta = {'scenario': scenario, 'q1_end': q1_end, 'q2_end': None}
    if scenario == 'think':
        pause_at = a0 + int(rng.integers(2000, 4000))
        resume = pause_at + int(rng.integers(*THINK_MS))
        ev += [(pause_at, 'ans', (False, pause_at)), (resume, 'ans', (True, resume))]
        ans_end = resume + int(rng.integers(1500, 3000))
    elif scenario == 'queue':
        q2_start = a0 + int(rng.integers(2000, 3500))
        meta['q2_end'] = utter(ev, rng, q2_start, int(rng.integers(900, 1800)), str(rng.choice(Q2S)))
        ans_end = meta['q2_end'] + int(rng.integers(1500, 3000))
    elif scenario in ('back', 'follow'):
        s = a0 + int(rng.integers(2000, 3500))
        pool, dur = (BACKS, 500) if scenario == 'back' else (FOLLOWS, 1200)
        utter(ev, rng, s, dur, str(rng.choice(pool)))
        ans_end = s + dur + int(rng.integers(1500, 3000))
    else:  # next_q / stop
        ans_end = a0 + int(rng.integers(4000, 7000))
    ev.append((ans_end, 'ans', (False, ans_end)))
    meta['ans_end'] = ans_end
    if scenario in EXPECT_SWITCH:
        q2_start = ans_end + int(rng.integers(*GAP_MS)) if scenario == 'next_q' else a0 + int(rng.integers(2000, 3500))
        text = str(rng.choice(Q2S))
        meta['q2_end'] = utter(ev, rng, q2_start, int(rng.integers(900, 1800)), text if scenario == 'next_q' else '先停一下，换个问题，' + text)
    meta['horizon'] = max(ans_end, meta['q2_end'] or 0) + CLOSE_WINDOW_MS
    return sorted(ev, key=lambda e: e[0]), meta


def run_sample(cfg, events, meta, step):
    d = TurnDetector(T_silence=cfg[0], T_long=cfg[1], T_answer_silence=cfg[2])
    i = 0
    for now in range(0, meta['horizon'] + 1, step):
        while i < len(events) and events[i][0] <= now:
            t, op, obj = events[i]
            if op == 'vad':
                d.on_vad(obj, now_ms=t)
            elif op == 'tr':
                d.on_transcript(obj, now_ms=t)
            else:
                d.on_answerer_active(obj[0], obj[1])
            i += 1
        d.tick(now)
    return d


def pct(a, p):
    return f'{float(np.percentile(a, p)):.0f}' if a else '-'


def evaluate(cfg, samples, step):
    q1_lat, sw_lat = [], []
    c = dict(premature=0, resume=0, semantic=0, late=0, missed=0, false_close=0, missed_close=0, false_queue=0, missed_queue=0)
    n_noswitch = n_nonqueue = 0
    for events, meta in samples:
        d = run_sample(cfg, events, meta, step)
        commits = [e for e in d.history if e.type == 'committed']
        q1 = [e for e in commits if e.t_speech_end_ms == meta['q1_end']]
        if q1:
            q1_lat.append(q1[-1].t_ms - meta['q1_end'])
        else:
            c['missed'] += 1
        c['premature'] += d.premature_count
        c['resume'] += d.resume_rollbacks
        c['semantic'] += d.semantic_rollbacks
        c['late'] += d.late_rollbacks
        closes = [e for e in d.history if e.type in ('answer_closed', 'interrupted')]
        queued = [e for e in d.history if e.type == 'queued']
        sc = meta['scenario']
        if sc in EXPECT_SWITCH:
            hit = [e for e in commits if meta['q2_end'] is not None and e.t_speech_end_ms == meta['q2_end']]
            if hit:
                sw_lat.append(hit[-1].t_ms - meta['q2_end'])
            else:
                c['missed_close'] += 1
        else:
            n_noswitch += 1
            c['false_close'] += any(e.t_ms < meta['ans_end'] for e in closes)
        if sc in EXPECT_QUEUE:
            c['missed_queue'] += not queued
        else:
            n_nonqueue += 1
            c['false_queue'] += bool(queued)
    return q1_lat, sw_lat, c, n_noswitch, n_nonqueue


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=60, help='每个场景的样本数')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--step', type=int, default=40, help='tick 步长 ms')
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    samples = [make_sample(rng, sc) for sc in SCENARIOS for _ in range(args.n)]
    lines = ['# SIMULATED -- 合成事件序列，不是真实声音/真实 ASR 结果；不得作为达标依据',
             f'n={len(samples)}（{len(SCENARIOS)} 场景 x {args.n}） seed={args.seed} step={args.step}ms '
             f'ASR覆盖延迟={ASR_LAG}ms hangover={HANGOVER_MS}ms 下一题间隔={GAP_MS}ms 思考停顿={THINK_MS}ms',
             '', '| T_silence | T_long | T_answer_silence | Q1 P50 | Q1 P95 | 过早(总/续说/改义/迟到) | 漏判 | 新题切换 P50 | P95 | 误关闭率 | 漏关闭 | 误入队 | 漏入队 |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for cfg in itertools.product((200, 300), (700, 1000), (1000, 1500, 2000, 3000)):
        q1, sw, c, n_ns, n_nq = evaluate(cfg, samples, args.step)
        lines.append(f'| {cfg[0]} | {cfg[1]} | {cfg[2]} | {pct(q1, 50)} | {pct(q1, 95)} | '
                     f'{c["premature"]}/{c["resume"]}/{c["semantic"]}/{c["late"]} | {c["missed"]} | {pct(sw, 50)} | {pct(sw, 95)} | '
                     f'{c["false_close"] / n_ns:.1%} | {c["missed_close"]} | {c["false_queue"]}/{n_nq} | {c["missed_queue"]} |')
    text = '\n'.join(lines)
    print(text)
    return text


if __name__ == '__main__':
    main()
