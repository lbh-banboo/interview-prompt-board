"""M14-2 回放器：16kHz/单声道/16-bit WAV -> pack() 帧，按实时节拍发出。"""
import asyncio
import time
import wave

from app.audio.frame import PAYLOAD_SIZE, pack

FRAME_MS = 20
CHUNK_MS = (20, 100, 200)


class WavFormatError(ValueError):
    pass


def read_wav(path):
    with wave.open(str(path), 'rb') as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if (ch, width, rate) != (1, 2, 16000):
            raise WavFormatError(f'需要 16kHz/单声道/16-bit，实际 {rate}Hz/{ch}ch/{width * 8}bit（不做静默重采样）')
        return w.readframes(w.getnframes())


def replay_wav(path, source, chunk_ms=20, realtime=True, clock=time.monotonic, sleep=time.sleep,
               stream_id=1, generation=0, live=False):
    """返回帧生成器；格式错误在调用时即抛出。

    第 i 帧(从 0 起)的 t_client_us = i*20000（音频时刻）。realtime 时第 i 帧在 start+(i+1)*20ms 发出
    （该帧音频采完的时刻）；chunk_ms=100/200 时仍按 20ms 成帧，每批 5/10 帧按"批内最后一帧采完的时刻"整批一起发出
    （先 sleep 到批尾时刻，再连续 yield 该批全部帧；末尾不足一批的按最后一帧时刻发）。
    M14-T1 的延迟误差口径：chunk_ms=20 时 <=20ms；批次模式下帧的发送时刻最多比其采完时刻晚 chunk_ms-20ms，
    故延迟测量误差上界为 chunk_ms（判定按批内帧顺序处理，批尾之前的帧被推迟到批尾）。
    live=False（默认）时帧带 replay 标志，服务端按回放帧处理（不进 VAD/ASR）；live=True 时不带，用于评测把回放当实时流喂管线。
    """
    if chunk_ms not in CHUNK_MS:
        raise ValueError(f'chunk_ms 只能是 {CHUNK_MS}')
    pcm = read_wav(path)
    return _frames(pcm, source, chunk_ms // FRAME_MS, realtime, clock, sleep, stream_id, generation,
                   live)


def _frames(pcm, source, batch, realtime, clock, sleep, stream_id, generation, live):
    pcm += b'\x00' * (-len(pcm) % PAYLOAD_SIZE)
    n = len(pcm) // PAYLOAD_SIZE
    start = clock()
    pending = []
    for i in range(n):
        pending.append(pack(source, stream_id, generation, i, pcm[i * PAYLOAD_SIZE:(i + 1) * PAYLOAD_SIZE],
                            t_client_us=i * FRAME_MS * 1000, replay=not live))
        if (i + 1) % batch == 0 or i == n - 1:
            if realtime:
                wait = start + (i + 1) * FRAME_MS / 1000 - clock()
                if wait > 0:
                    sleep(wait)
            yield from pending
            pending = []


async def send_to_ws(url, frames):
    """把帧(可含阻塞节拍的生成器)逐帧发到 WS；返回发送帧数。"""
    import websockets
    it = iter(frames)
    sent = 0
    async with websockets.connect(url) as ws:
        while (frame := await asyncio.to_thread(next, it, None)) is not None:
            await ws.send(frame)
            sent += 1
    return sent
