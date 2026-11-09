"""Continuous per-stream WAV writer (F-2). Frames are fixed 20 ms, so seq maps to a file offset:
replayed frames fill holes in place, duplicates are dropped, and unfilled holes are the recording gaps."""
import wave
from pathlib import Path

from .frame import PAYLOAD_SIZE

WAV_HEADER = 44
SLICE_FRAMES = 30000  # 10 minutes
FRAME_MS = 20


class FrameRejected(ValueError):
    pass


MAX_SEQ = 360000      # 2 小时
MAX_JUMP = 6000       # 相对已见最大 seq 的前向跳变上限（2 分钟）
HEADER_EVERY = 50     # 每 50 个新写帧刷新一次 WAV 头


class StreamRecorder:
    def __init__(self, directory, name, slice_frames=SLICE_FRAMES):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.slice_frames = slice_frames
        self.files = {}   # slice index -> open file
        self.counts = {}  # slice index -> frames covered (highest frame + 1)
        self.fresh = {}   # slice index -> frames written since last header update
        self.seen = set()
        self.top = -1
        self.watermark = -1  # 从 0 起连续已落盘的最大 seq
        self.rejected = 0
        self.closed = False

    def path(self, index):
        return self.dir / f'{self.name}_{index:03d}.wav'

    def _open(self, index):
        path = self.path(index)
        if path.exists():  # 绝不用 'wb' 清空已有录音
            f = path.open('r+b')
            self.counts[index] = max(0, (path.stat().st_size - WAV_HEADER) // PAYLOAD_SIZE)
        else:
            f = path.open('wb')
            self.counts[index] = 0
        self.files[index] = f
        self.fresh[index] = 0
        self._header(index)
        return f

    def write(self, seq, pcm):
        """Return True when the frame was newly stored."""
        if len(pcm) != PAYLOAD_SIZE:
            raise ValueError('PCM 帧长度无效')
        if self.closed:
            raise FrameRejected('录音已结束')
        if seq in self.seen:
            return False
        if not 0 <= seq <= MAX_SEQ or seq - self.top > MAX_JUMP:
            self.rejected += 1
            raise FrameRejected('seq 超出允许范围')
        index, pos = divmod(seq, self.slice_frames)
        f = self.files.get(index) or self._open(index)
        f.seek(WAV_HEADER + pos * PAYLOAD_SIZE)
        f.write(pcm)
        self.seen.add(seq)
        self.top = max(self.top, seq)
        while self.watermark + 1 in self.seen:
            self.watermark += 1
        self.counts[index] = max(self.counts[index], pos + 1)
        self.fresh[index] += 1
        if self.fresh[index] >= HEADER_EVERY:
            self.fresh[index] = 0
            self._header(index)
        f.flush()
        return True

    def _header(self, index):
        f, size = self.files[index], self.counts[index] * PAYLOAD_SIZE
        f.seek(0)
        f.write(b'RIFF' + (36 + size).to_bytes(4, 'little') + b'WAVEfmt ' + (16).to_bytes(4, 'little')
                + (1).to_bytes(2, 'little') + (1).to_bytes(2, 'little') + (16000).to_bytes(4, 'little')
                + (32000).to_bytes(4, 'little') + (2).to_bytes(2, 'little') + (16).to_bytes(2, 'little')
                + b'data' + size.to_bytes(4, 'little'))

    def gaps(self):
        """Missing frame ranges below the highest received seq, as (t_start_ms, t_end_ms)."""
        out, prev = [], -1
        for seq in sorted(self.seen):  # 只遍历已收到的帧，不随 top 线性增长
            if seq > prev + 1:
                out.append(((prev + 1) * FRAME_MS, seq * FRAME_MS))
            prev = seq
        return out

    def close(self):
        """Flush headers, close files; return [(slice index, path, seconds)]."""
        self.closed = True
        result = []
        for index, f in sorted(self.files.items()):
            self._header(index)
            f.flush()
            f.close()
            result.append((index, self.path(index), self.counts[index] * FRAME_MS / 1000))
        self.files = {}
        return result


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()
