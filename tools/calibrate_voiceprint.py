"""声纹阈值标定（M03-5）：对带标注片段求 EER 与建议阈值。

输入目录结构：<dir>/<说话人>/*.wav（16kHz 单声道 16bit）；同一说话人两两为 genuine，跨说话人为 impostor。
数据来源标记：目录下存在 SIMULATED 文件，或没有真实声纹模型（改用 FakeExtractor）-> 报告首行 SIMULATED，否则 REAL。
SIMULATED 报告的阈值不得用于验收；REAL 报告仍须在 M14 评测集（含同性别）上复核。

用法: .venv/bin/python tools/calibrate_voiceprint.py <dir> [--model PATH] [--out report.txt]
"""
import argparse
import itertools
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.audio.voiceprint import DEFAULT_MODEL, FakeExtractor, SherpaExtractor, cosine  # noqa: E402


def compute_eer(genuine, impostor):
    """扫描阈值，返回 (eer, eer_threshold)。FAR = impostor >= thr，FRR = genuine < thr。"""
    g, i = np.asarray(genuine), np.asarray(impostor)
    best = (2.0, 0.0, 0.0)
    for thr in np.unique(np.concatenate([g, i])):
        far, frr = float(np.mean(i >= thr)), float(np.mean(g < thr))
        gap = abs(far - frr)
        if gap < best[0]:
            best = (gap, (far + frr) / 2, float(thr))
    return best[1], best[2]


def suggest_thresholds(genuine, impostor, target=0.01):
    """high：impostor 的 (1-target) 分位（FAR 约 target）；low：genuine 的 target 分位（低于它几乎肯定非本人）。"""
    g, i = np.asarray(genuine), np.asarray(impostor)
    return float(np.percentile(i, 100 * (1 - target))), float(np.percentile(g, 100 * target))


def read_wav(path):
    with wave.open(path) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f'{path}: 需 16kHz 单声道 16bit')
        return w.readframes(w.getnframes())


def load_dataset(root):
    data = {}
    for spk in sorted(os.listdir(root)):
        d = os.path.join(root, spk)
        if os.path.isdir(d):
            data[spk] = [read_wav(os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith('.wav')]
    return data


def score_pairs(data, extractor):
    embs = {s: [extractor.extract(c) for c in clips] for s, clips in data.items()}
    genuine = [cosine(a, b) for e in embs.values() for a, b in itertools.combinations(e, 2)]
    impostor = [cosine(a, b) for (_, e1), (_, e2) in itertools.combinations(embs.items(), 2) for a in e1 for b in e2]
    return genuine, impostor


def report(root, model):
    data = load_dataset(root)
    real_model = os.path.isfile(model)
    simulated = os.path.exists(os.path.join(root, 'SIMULATED')) or not real_model
    extractor = SherpaExtractor(model) if real_model else FakeExtractor()
    g, i = score_pairs(data, extractor)
    if not g or not i:
        raise ValueError('需要至少 2 个说话人且每人至少 2 段')
    eer, thr = compute_eer(g, i)
    high, low = suggest_thresholds(g, i)
    lines = ['SIMULATED' if simulated else 'REAL',
             f'speakers={len(data)} genuine_pairs={len(g)} impostor_pairs={len(i)}',
             f'extractor={type(extractor).__name__}',
             f'EER={eer:.4f} eer_threshold={thr:.4f}',
             f'suggested high(FAR约1%)={high:.4f} low(genuine 1%分位)={low:.4f}']
    if simulated:
        lines.append('SIMULATED 结果不得用于验收或写入默认阈值；待真实数据标定。')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('--model', default=str(DEFAULT_MODEL))
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    text = report(a.dir, a.model)
    print(text)
    if a.out:
        with open(a.out, 'w') as f:
            f.write(text + '\n')


if __name__ == '__main__':
    main()
