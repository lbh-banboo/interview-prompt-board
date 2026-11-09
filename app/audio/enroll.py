"""声音登记（M03-3）：固定朗读文本、登记质量检查、模板持久化。

模板仅存本机 SQLite（voice_profiles），数据目录权限 700；本模块不含任何网络代码，声纹绝不外发。
质量阈值默认值均为【待真实数据标定】，不是验收结论。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .voiceprint import SAMPLE_RATE, cosine, mean_template, to_float

# 固定朗读文本（约 12~15 秒语速）；内容为中性通用文本，不含任何个人信息。
ENROLL_TEXT = ('今天我来参加这次面试，很高兴有机会和各位老师交流。'
               '接下来我会用平常的语速，清晰地朗读这段文字，'
               '请保持安静的环境，并和麦克风保持适中的距离。')
SEGMENTS_MIN = 1
SEGMENTS_MAX = 3   # 1 段为连续录音默认；3 段仅兼容旧客户端
SIM_CHUNKS = 3     # 单段（或 2 段）时按有效语音等分为 3 个子段做相似度检查
MIN_CHUNK_S = 0.3  # 子段短于 0.3 秒则无法可靠比对，跳过并标注
FRAME = SAMPLE_RATE // 50  # 20ms

DEFAULTS = dict(
    min_speech_s=8.0,       # 三段合计有效语音下限（规划：≥8s）
    min_snr_db=12.0,        # 每段 SNR 下限；待标定
    min_pair_sim=0.6,       # 三段两两余弦相似度下限；待标定
    abs_floor=0.004,        # 与 EnergyVad 一致的绝对能量下限
)


class Reason(str, Enum):
    NEED_SEGMENT = 'need_segment'
    TOO_SHORT_SPEECH = 'too_short_speech'
    LOW_SNR = 'low_snr'
    INCONSISTENT = 'inconsistent_segments'
    MODEL_UNAVAILABLE = 'model_unavailable'


MESSAGES = {
    Reason.NEED_SEGMENT: '需要录制至少一段朗读；请点“开始录音”，完整读完全文后再点“停止录音”。',
    Reason.TOO_SHORT_SPEECH: '有效语音 {effective_speech_s} 秒，不足 {min_speech_s:g} 秒：'
                              '请完整朗读全文、语速平常，录满约 15 秒再停止。',
    Reason.LOW_SNR: '环境噪声偏大或声音太小，请到安静处、靠近麦克风后重录。',
    Reason.INCONSISTENT: '录音内部声音差异过大（可能有他人说话或中途换了设备），请重录。',
    Reason.MODEL_UNAVAILABLE: '声纹模型不可用，暂时无法登记；角色识别将降级为能量加上下文推断。',
}


@dataclass
class EnrollResult:
    ok: bool
    reason: Reason | None = None
    message: str = '登记成功'
    quality: dict = field(default_factory=dict)
    template: np.ndarray | None = None

    def public(self) -> dict:
        """对外字段，不含 embedding。"""
        return dict(ok=self.ok, reason=self.reason.value if self.reason else None, message=self.message,
                    quality=self.quality)


def analyze_segment(x: np.ndarray, abs_floor: float = DEFAULTS['abs_floor']):
    """返回 (有效语音样本, 有效语音秒数, SNR dB)。噪声底取帧能量 10 分位，语音帧 = 能量 > max(下限, 3 倍噪声底)。

    连续朗读可能几乎没有停顿，10 分位会取到语音本身、把整段判成 0 秒；此时退回绝对能量下限，
    噪声底也用同一绝对值估计 SNR（仅在该回退路径生效，正常停顿下行为不变）。
    """
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, np.float32), 0.0, 0.0
    frames = x[:n * FRAME].reshape(n, FRAME)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    noise = max(float(np.percentile(rms, 10)), 1e-5)
    mask = rms > max(abs_floor, noise * 3)
    if not mask.any():
        mask = rms > abs_floor
        if not mask.any():
            return np.zeros(0, np.float32), 0.0, 0.0
        noise = abs_floor
    speech_rms = float(np.sqrt(np.mean(rms[mask] ** 2)))
    return frames[mask].reshape(-1), float(mask.sum()) * 0.02, 20 * float(np.log10(speech_rms / noise))


def _split_equal(x: np.ndarray, k: int) -> list[np.ndarray]:
    n = len(x)
    return [x[i * n // k:(i + 1) * n // k] for i in range(k)]


def _similarity_embeddings(stats, extractor):
    """返回用于两两相似度的 embedding 列表；无法可靠切分时返回 None。

    3 段（旧客户端）保持原语义：每段一个 embedding。
    1~2 段（新的连续录音）：把每段的有效语音等分为 3 个子段；任一子段短于 0.3 秒则放弃比对。
    """
    if len(stats) == SIM_CHUNKS:
        return [extractor.extract(s[0]) for s in stats]
    min_chunk = int(MIN_CHUNK_S * SAMPLE_RATE)
    chunks = []
    for s in stats:
        parts = _split_equal(s[0], SIM_CHUNKS)
        if min(len(p) for p in parts) < min_chunk:
            return None
        chunks.extend(parts)
    return [extractor.extract(c) for c in chunks]


def check_enrollment(segments, extractor, **overrides) -> EnrollResult:
    """segments：1~3 段 PCM16（bytes / 数组）。按 段数 -> 有效语音 -> SNR -> 内部相似度 顺序检查，首个失败即返回。"""
    p = {**DEFAULTS, **overrides}
    if not SEGMENTS_MIN <= len(segments) <= SEGMENTS_MAX:
        return _fail(Reason.NEED_SEGMENT, dict(segments=len(segments)))
    stats = [analyze_segment(to_float(s), p['abs_floor']) for s in segments]
    speech_s = sum(s[1] for s in stats)
    snr = min(s[2] for s in stats)
    q = dict(effective_speech_s=round(speech_s, 2), snr_db=round(snr, 1))
    if speech_s < p['min_speech_s']:
        return _fail(Reason.TOO_SHORT_SPEECH, q, effective_speech_s=q['effective_speech_s'],
                     min_speech_s=p['min_speech_s'])
    if snr < p['min_snr_db']:
        return _fail(Reason.LOW_SNR, q)
    embs = _similarity_embeddings(stats, extractor)
    if embs is None:
        # 有效语音太短、切不出 3 个 0.3 秒子段：不假造相似度，只标注跳过。
        q['similarity_skipped'] = True
        return EnrollResult(True, None, quality=q, template=mean_template([extractor.extract(s[0]) for s in stats]))
    sims = [cosine(embs[i], embs[j]) for i in range(len(embs)) for j in range(i + 1, len(embs))]
    q['min_pair_similarity'] = round(min(sims), 4)
    if min(sims) < p['min_pair_sim']:
        return _fail(Reason.INCONSISTENT, q)
    return EnrollResult(True, None, quality=q, template=mean_template(embs))


def _fail(reason: Reason, quality: dict, **fmt) -> EnrollResult:
    message = MESSAGES[reason]
    if fmt:
        message = message.format(**fmt)
    return EnrollResult(False, reason, message, quality)


class VoiceProfileStore:
    """voice_profiles 表。connect/lock 取自 ctx.store（connect() 返回 sqlite3 连接，lock 为 RLock）；root 为数据根目录。"""

    def __init__(self, connect, lock, root, clock=time.time):
        self.connect, self.lock, self.clock = connect, lock, clock
        os.chmod(root, 0o700)
        with self.lock, self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS voice_profiles('
                       'id TEXT PRIMARY KEY, profile_id TEXT, embedding BLOB, quality_json TEXT, created_at REAL)')

    def save(self, profile_id: str, template: np.ndarray, quality: dict) -> None:
        """同一 profile_id 只保留最新一份（重录即覆盖）。"""
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM voice_profiles WHERE profile_id=?', (profile_id,))
            db.execute('INSERT INTO voice_profiles VALUES(?,?,?,?,?)',
                       (uuid.uuid4().hex, profile_id, np.asarray(template, np.float32).tobytes(),
                        json.dumps(quality), self.clock()))

    def load(self, profile_id: str):
        """返回 (template, quality, created_at)，不存在返回 None。"""
        with self.lock, self.connect() as db:
            row = db.execute('SELECT embedding, quality_json, created_at FROM voice_profiles WHERE profile_id=?',
                             (profile_id,)).fetchone()
        if row is None:
            return None
        return np.frombuffer(row[0], np.float32).copy(), json.loads(row[1]), row[2]

    def delete(self, profile_id: str) -> bool:
        with self.lock, self.connect() as db:
            return db.execute('DELETE FROM voice_profiles WHERE profile_id=?', (profile_id,)).rowcount > 0
