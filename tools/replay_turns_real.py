"""真人录音轮次级回放评测（单轨近似）：t_end -> committed、漏判、过早、候选撤销、非提问点误 commit。

单轨近似（务必随结论一起报告）：录音是面试官与候选人同轨混音，没有通道分离。
本地 VAD 把整条混音当作"提问方"语音，所以候选人的回答也会被当成提问方发言喂给 TurnDetector；
评测只统计 verified 为"是问题结束点"的题的 t_end -> committed。
为避免把候选人开口误记为"提问方续说"，对已命中标注题的 commit，其后第一个晚于该题 t_end 的 speech_start
按"回答方开口"处理（on_answerer_active + close_turn）；这使用了人工标注的 t_end，仅用于话轮收尾，
不影响 t_committed 的判定时刻。回答结束检测不在本评测范围。
报告类别：build_report 的 kind 枚举不含 REAL_AUDIO_ONE_TRACK，故用 ALGO_REPLAY 并在备注标明"真人单轨近似"。

用法:
  .venv/bin/python tools/replay_turns_real.py --wav eval/audio16k/pdd.wav \
      --events eval/reports/real/pdd.events.jsonl --review eval/annotations/pdd.review.json \
      [--trigger coverage|vad] [--T-silence 200] [--T-long 700] [--T-hard 1500] [--vad silero|energy]
      [--allow-unverified]   # 仅调试流程，输出标 UNVERIFIED-DEBUG，不进任何结论
"""
import argparse
import inspect
import json
import sys
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.audio.vad import EnergyVad, make_vad  # noqa: E402
from app.domain.turn_detector import TurnDetector  # noqa: E402
from app.telemetry.report import MISS_WINDOW_MS, build_report  # noqa: E402

FRAME_MS = 20
FRAME_BYTES = 640
MATCH_MS = 800  # commit 的 t_speech_end_ms 与标注 t_end 相差在此内，视为属于该标注点


@dataclass(frozen=True)
class Tr:  # 鸭子类型的 TranscriptEvent
    text: str
    is_final: bool
    text_version: int
    audio_covered_until_ms: int
    t_audio_start_ms: int
    t_audio_end_ms: int


def read_pcm(path):
    with wave.open(str(path), 'rb') as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError('需要 16k 单声道 16-bit WAV')
        return w.readframes(w.getnframes())


def load_events(path):
    evs = [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]
    return sorted(evs, key=lambda e: e['arr_ms'])


def make_detector(trigger, **kw):
    if trigger != 'coverage':
        if 'trigger' not in inspect.signature(TurnDetector.__init__).parameters:
            sys.exit(f'错误: 当前 TurnDetector 不支持 trigger 参数，无法使用 --trigger {trigger}')
        kw['trigger'] = trigger
    return TurnDetector(**kw)


def simulate(pcm, events, positives, detector, vad_kind='silero'):
    """音频时钟驱动：逐 20ms 帧，先送该帧内到达的 ASR 事件，再送 VAD 事件，最后 tick。返回 detector.history。"""
    vad = EnergyVad('meeting') if vad_kind == 'energy' else make_vad('meeting')
    ends = sorted(p['t_end_ms'] for p in positives)
    ei, n_frames = 0, len(pcm) // FRAME_BYTES
    resolved_te = None  # 已命中标注题的 commit 的 t_speech_end_ms
    seen_commits = 0
    for i in range(n_frames):
        now = (i + 1) * FRAME_MS
        while ei < len(events) and events[ei]['arr_ms'] <= now:
            e = events[ei]
            ei += 1
            detector.on_transcript(Tr(e['text'], e['final'], e['ver'], e['cov'], e['s'], e['e_end']), now_ms=e['arr_ms'])
        for ev in vad.feed(pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES], i * FRAME_MS):
            if ev.kind == 'speech_start' and detector.phase == 'question_committed':
                te = next((h.t_speech_end_ms for h in reversed(detector.history) if h.type == 'committed'), None)
                if te is not None and any(abs(te - t) <= MATCH_MS and ev.t_ms >= t for t in ends):
                    detector.on_answerer_active(True, now)
                    detector.on_vad(ev, now_ms=now)
                    detector.close_turn(ev.t_ms)
                    detector.on_answerer_active(False, now)
                    continue
            detector.on_vad(ev, now_ms=now)
        detector.tick(now)
    return detector.history


def evaluate(history, positives, negatives):
    """返回 (每题记录列表, 误 commit 统计)。"""
    commits = [h for h in history if h.type == 'committed']
    per = []
    for p in sorted(positives, key=lambda x: x['t_end_ms']):
        te = p['t_end_ms']
        hit = next((c for c in commits if te <= c.t_ms <= te + MISS_WINDOW_MS), None)
        rec = dict(id=p['id'], t_end=te, t_committed=None if hit is None else hit.t_ms, premature=0)
        if hit is not None:
            rec['premature'] = sum(1 for h in history if h.type == 'rolled_back' and h.t_ms >= hit.t_ms
                                   and h.t_speech_end_ms == hit.t_speech_end_ms)
        lo = p['vad_seg']['start_ms']
        rec['withdrawn'] = sum(1 for h in history if h.type == 'withdrawn' and lo <= h.t_ms <= te + MISS_WINDOW_MS)
        per.append(rec)
    pos_ends = [p['t_end_ms'] for p in positives]
    neg_ends = [n['t_end_ms'] for n in negatives]
    false_n, unattributed = 0, 0
    for c in commits:
        te = c.t_speech_end_ms
        if te is None or any(abs(te - t) <= MATCH_MS for t in pos_ends):
            continue
        if any(abs(te - t) <= MATCH_MS for t in neg_ends):
            false_n += 1
        else:
            unattributed += 1
    return per, dict(false_commits=false_n, unattributed_commits=unattributed, total_commits=len(commits))


def split_items(items, allow_unverified):
    if allow_unverified:
        return list(items), [], [], 0
    pos = [i for i in items if i.get('verified') is True and i['human'].get('is_question_end') is True]
    neg = [i for i in items if i.get('verified') is True and i['human'].get('is_question_end') is False]
    unv = [i for i in items if i.get('verified') is not True]
    return pos, neg, unv, len(unv)


def run(wav, events_path, review_path, trigger='coverage', vad_kind='silero', allow_unverified=False,
        out_dir=ROOT / 'eval/reports/real', det_kw=None):
    review = json.loads(Path(review_path).read_text(encoding='utf-8'))
    pos, neg, unv, n_unv = split_items(review['items'], allow_unverified)
    det = make_detector(trigger, **(det_kw or {}))
    history = simulate(read_pcm(wav), load_events(events_path), pos, det, vad_kind)
    per, fc = evaluate(history, pos, neg)
    audio_id = review.get('audio_id', Path(wav).stem)
    tag = f'UNVERIFIED-DEBUG.{trigger}' if allow_unverified else trigger
    rows = [dict(sample_id=f'{audio_id}#{r["id"]}', audio_path=str(wav), log_path=str(events_path), verified=True,
                 mode='offline', t_end=r['t_end'], t_committed=r['t_committed'], premature=r['premature'],
                 withdrawn=r['withdrawn']) for r in per]
    rows += [dict(sample_id=f'{audio_id}#{i["id"]}', audio_path=str(wav), log_path=str(events_path), verified=False,
                  mode='offline') for i in unv]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f'{audio_id}.replay.{tag}'
    text = build_report(rows, 'ALGO_REPLAY', mode='offline', csv_path=str(base) + '.csv')
    head = []
    if allow_unverified:
        head += ['> **UNVERIFIED-DEBUG**: 以下用未校对的预填候选当作提问点，仅验证流程通畅，不进任何结论，不得引用数字。', '']
    head += [f'> 备注: 真人单轨近似（REAL_AUDIO_ONE_TRACK；build_report 无此枚举，故类别栏显示 ALGO_REPLAY）。'
             f'面试官与候选人同轨，VAD 把候选人回答也当作提问方语音；只统计 verified 提问点的 t_end->committed。',
             f'> trigger={trigger}，VAD={vad_kind}，检测器参数={det_kw or {}}，命中窗口 {MISS_WINDOW_MS}ms。'
             f'下方「角色识别」章节在单轨下不适用，请忽略。', '']
    tail = ['', '## 误触发（单轨近似）',
            f'- 全部 committed 事件数: {fc["total_commits"]}',
            f'- 落在已校对「不是问题结束点」上的误 commit: {fc["false_commits"]}（已核实负例数 {len(neg)}）',
            f'- 不属于任何已校对点的 commit（含候选人回答被当作提问方，未核实，不算误触发）: {fc["unattributed_commits"]}',
            f'- 每题候选撤销数合计: {sum(r["withdrawn"] for r in per)}',
            f'- 未校对条目排除: {n_unv}']
    Path(str(base) + '.md').write_text('\n'.join(head) + text + '\n'.join(tail) + '\n', encoding='utf-8')
    return per, fc, base


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--wav', required=True)
    ap.add_argument('--events', required=True)
    ap.add_argument('--review', required=True)
    ap.add_argument('--trigger', choices=['coverage', 'vad'], default='coverage')
    ap.add_argument('--vad', choices=['silero', 'energy'], default='silero')
    ap.add_argument('--T-silence', type=int)
    ap.add_argument('--T-long', type=int)
    ap.add_argument('--T-hard', type=int)
    ap.add_argument('--allow-unverified', action='store_true')
    ap.add_argument('--out-dir', default=str(ROOT / 'eval/reports/real'))
    a = ap.parse_args()
    kw = {k: v for k, v in (('T_silence', a.T_silence), ('T_long', a.T_long), ('T_hard', a.T_hard)) if v is not None}
    per, fc, base = run(a.wav, a.events, a.review, a.trigger, a.vad, a.allow_unverified, a.out_dir, kw)
    hit = [r for r in per if r['t_committed'] is not None and r['t_committed'] - r['t_end'] <= MISS_WINDOW_MS]
    print(('UNVERIFIED-DEBUG ' if a.allow_unverified else '') + f'题数 {len(per)}，命中 {len(hit)}，漏判 {len(per) - len(hit)}，'
          f'过早合计 {sum(r["premature"] for r in per)}，误commit {fc["false_commits"]}；报告 {base}.md')
    print(Counter(r['premature'] for r in per))
