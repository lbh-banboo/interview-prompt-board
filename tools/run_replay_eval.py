"""离线评测：WAV 回放 -> SessionPipeline(meeting 通道, MockASR 脚本) -> TurnEvent -> report.build_report。

因为 ASR 是 Mock，报告类别固定 SIMULATED，结论不构成真实通过。

脚本 JSON：{"truth_end_ms": [每题真值结束时刻...], "script": [[音频时刻ms, 文本, is_final, 覆盖到ms], ...]}
用法：
  python tools/run_replay_eval.py --wav a.wav --script a.json [--name NAME] [--vad energy|silero]
  python tools/run_replay_eval.py --synth [--seed 0] [--questions 5]   # 合成带停顿的"语音"WAV + 脚本后评测
"""
import argparse
import asyncio
import json
import sqlite3
import struct
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.asr.mock import MockASR  # noqa: E402
from app.audio.frame import parse  # noqa: E402
from app.audio.vad import EnergyVad, make_vad  # noqa: E402
from app.realtime.pipeline import SessionPipeline  # noqa: E402
from app.telemetry.metrics import Metrics  # noqa: E402
from app.telemetry.replay import replay_wav  # noqa: E402
from app.telemetry.report import build_report  # noqa: E402

QUESTIONS = ['你能讲讲分布式事务的区别吗', '请介绍一下你在这个项目里负责的部分', '为什么选择这个方案而不是别的方案呢',
             '你怎么保证缓存和数据库的一致性', '讲一讲你遇到过的最难的线上问题吧']
MATCH_MS = 400  # 事件的 t_speech_end_ms 与真值结束时刻相差在此内才算同一题


def synth(wav_path, script_path, seed=0, n=5, rate=16000):
    """每题：前导静音 -> 200Hz 载波 + 4Hz 包络的"语音" -> 约 1.5s 停顿。脚本在语音结束前 100ms 给出覆盖末尾的完整问句 partial。"""
    rng = np.random.default_rng(seed)
    pcm, script, truth, t = [], [], [], 0
    for i in range(n):
        lead, speech = int(rng.integers(3, 6)) * 100, int(rng.integers(12, 26)) * 100
        pause = 1500
        x = np.arange(rate * speech // 1000) / rate
        tone = 9000 * np.sin(2 * np.pi * 200 * x) * (0.7 + 0.3 * np.sin(2 * np.pi * 4 * x))
        pcm += [np.zeros(rate * lead // 1000), tone, np.zeros(rate * pause // 1000)]
        end = t + lead + speech
        text = QUESTIONS[i % len(QUESTIONS)]
        script += [[end - 100, text, False, end], [end + 100, text, True, end]]
        truth.append(end)
        t = end + pause
    data = np.concatenate(pcm).astype('<i2').tobytes()
    with wave.open(str(wav_path), 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(data)
    Path(script_path).write_text(json.dumps({'truth_end_ms': truth, 'script': script}, ensure_ascii=False, indent=1),
                                 encoding='utf-8')


async def run(wav_path, spec, vad, close_on_commit=False):
    conn = sqlite3.connect(':memory:')
    metrics = Metrics(conn)
    script = [tuple(x) for x in spec['script']]
    vad_factory = (lambda s: EnergyVad(s)) if vad == 'energy' else make_vad
    pipe = SessionPipeline('eval', None, lambda source: MockASR(script, source), metrics, vad_factory=vad_factory)
    closed = 0
    for data in replay_wav(wav_path, 'meeting', realtime=False, live=True):
        frame = parse(data)
        await pipe.on_frame(frame)
        if close_on_commit and pipe.detector.committed_count > closed:
            closed = pipe.detector.committed_count
            pipe.detector.close_turn((frame['seq'] + 1) * 20)  # 模拟"回答瞬间结束"，M05-5 尚未实现
    await pipe.close()
    conn.close()
    return pipe.turn_events


def build_rows(events, truth, wav_path, script_path):
    rows = []
    for i, end in enumerate(truth):
        mine = [e for e in events if e.t_speech_end_ms is not None and abs(e.t_speech_end_ms - end) <= MATCH_MS]
        commits = [e for e in mine if e.type == 'committed']
        rows.append(dict(
            sample_id=f'q{i + 1}', audio_path=str(wav_path), log_path=str(script_path), verified=True, mode='online',
            t_end=end, t_committed=commits[0].t_ms if commits else None,
            premature=sum(e.type == 'rolled_back' for e in mine), withdrawn=sum(e.type == 'withdrawn' for e in mine),
            role_ok=True, role_unknown=False, overlap_truth=False))
    return rows


NOTE = ('\n## 备注（评测工具追加）\n'
        '- ASR 为 MockASR 脚本，VAD 为本地实现；t_end 取自合成/标注脚本的真值，t_committed 取自管线 committed 事件的音频时刻。\n'
        '- 单通道 meeting 回放，未评测角色识别：上方"角色识别"一节的数值是占位（role_ok 固定为真），无意义。\n'
        '- 回放帧由 replay_wav(live=True) 生成，不带 replay 标志，按实时流喂入管线。\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--wav')
    ap.add_argument('--script')
    ap.add_argument('--synth', action='store_true')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--questions', type=int, default=5)
    ap.add_argument('--vad', choices=('energy', 'silero'), default='energy')
    ap.add_argument('--close-on-commit', action='store_true',
                    help='提交后立即 close_turn（模拟回答结束；否则连续提问会被记为 speech_resumed_after_commit 回滚）')
    ap.add_argument('--name')
    ap.add_argument('--out-dir', default=str(ROOT / 'eval' / 'reports'))
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if a.synth:
        name = a.name or f'synthetic_seed{a.seed}'
        wav, script = out / f'{name}.wav', out / f'{name}.script.json'
        synth(wav, script, a.seed, a.questions)
    else:
        if not (a.wav and a.script):
            ap.error('需要 --wav 和 --script，或使用 --synth')
        wav, script = Path(a.wav), Path(a.script)
        name = a.name or wav.stem
    spec = json.loads(script.read_text(encoding='utf-8'))
    events = asyncio.run(run(wav, spec, a.vad, a.close_on_commit))
    rows = build_rows(events, spec['truth_end_ms'], wav, script)
    note = NOTE + ('- 已启用 --close-on-commit：提交后立即 close_turn，模拟回答瞬间结束（回答结束判定属 M05-5，未实现）。\n'
                   if a.close_on_commit else '- 未启用 --close-on-commit：TurnDetector 没有回答结束信号，后一题开口会被记为对前一题的回滚（过早），这是缺少 M05-5 的产物。\n')
    text = build_report(rows, 'SIMULATED', 'online', csv_path=out / f'{name}.csv') + note
    (out / f'{name}.md').write_text(text, encoding='utf-8')
    print(text)
    return text


if __name__ == '__main__':
    main()
