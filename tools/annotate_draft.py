"""G4a 标注草稿：能量 VAD 切段，导出 eval/annotations/<audio_id>.draft.json（待人工校对，不做校对）。

用法: .venv/bin/python tools/annotate_draft.py <wav> [--out-dir eval/annotations]
只有 verified:true 的段才进评测分母（见 app/telemetry/report.py）。
"""
import argparse
import json
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 16000
FRAME_MS = 20
MIN_SPEECH_MS = 200
HANGOVER_MS = 300
RMS_FLOOR = 150.0


def load_mono16k(path):
    """返回 (int16 数组@16k, 原采样率, 说明列表)。"""
    with wave.open(str(path), 'rb') as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if width != 2:
            raise ValueError(f'仅支持 16-bit WAV，实际 {width * 8}-bit')
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float64)
    notes = []
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
        notes.append(f'{ch} 声道已平均为单声道')
    if rate != TARGET_RATE:
        if rate > TARGET_RATE:  # 加窗 sinc 低通，截止 0.45*目标采样率，再线性插值
            fc = 0.45 * TARGET_RATE / rate
            t = np.arange(-64, 65)
            h = 2 * fc * np.sinc(2 * fc * t) * np.hamming(129)
            x = np.convolve(x, h / h.sum(), mode='same')
        n_out = int(len(x) * TARGET_RATE / rate)
        x = np.interp(np.arange(n_out) * rate / TARGET_RATE, np.arange(len(x)), x)
        notes.append(f'由 {rate}Hz 重采样到 16000Hz（简单低通+线性插值，仅供切段草稿）')
    return np.clip(np.round(x), -32768, 32767).astype(np.int16), rate, notes


def vad_segments(pcm):
    """能量 VAD，返回 [(start_ms, end_ms)]；阈值=max(floor, 3*噪声底(10% 分位 RMS))。"""
    fl = TARGET_RATE * FRAME_MS // 1000
    n = len(pcm) // fl
    if n == 0:
        return []
    fr = pcm[:n * fl].astype(np.float64).reshape(n, fl)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    thr = max(RMS_FLOOR, 3 * np.percentile(rms, 10))
    hang = HANGOVER_MS // FRAME_MS
    segs, start, last, gap = [], None, None, 0
    for i, v in enumerate(rms):
        if v >= thr:
            if start is None:
                start = i
            last, gap = i, 0
        elif start is not None:
            gap += 1
            if gap >= hang:
                segs.append((start, last))
                start = None
    if start is not None:
        segs.append((start, last))
    return [(s * FRAME_MS, (e + 1) * FRAME_MS) for s, e in segs if (e - s + 1) * FRAME_MS >= MIN_SPEECH_MS]


def annotate(wav_path, out_dir='eval/annotations'):
    wav_path = Path(wav_path)
    pcm, rate, notes = load_mono16k(wav_path)
    audio_id = wav_path.stem
    doc = dict(audio_id=audio_id, source_wav=str(wav_path), original_sample_rate=rate,
               resampled=rate != TARGET_RATE, notes=notes, duration_ms=len(pcm) * 1000 // TARGET_RATE,
               segments=[dict(id=i, start_ms=s, end_ms=e, role=None, t_end=None, is_topic_change=None,
                              verified=False) for i, (s, e) in enumerate(vad_segments(pcm))])
    out = Path(out_dir) / f'{audio_id}.draft.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('wav')
    ap.add_argument('--out-dir', default='eval/annotations')
    args = ap.parse_args()
    print(annotate(args.wav, args.out_dir))
