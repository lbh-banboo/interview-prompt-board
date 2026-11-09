"""WS audio frame: 24-byte header + PCM16 payload (plan §7.2)."""
import struct

MAGIC = b'IA'
VERSION = 1
HEADER = struct.Struct('>2sBBBBIHHI')  # 18 bytes, followed by 48-bit t_client_us
HEADER_SIZE = 24
FRAME_SAMPLES = 320  # 20 ms @ 16 kHz
PAYLOAD_SIZE = FRAME_SAMPLES * 2
SOURCES = {1: 'mic', 2: 'meeting', 3: 'room'}
SOURCE_CODES = {v: k for k, v in SOURCES.items()}
FLAG_REPLAY = 1


class FrameError(ValueError):
    pass


def pack(source, stream_id, generation, seq, pcm, t_client_us=0, replay=False):
    return (HEADER.pack(MAGIC, VERSION, SOURCE_CODES[source], FLAG_REPLAY if replay else 0, 0,
                        stream_id, generation, 0, seq) + t_client_us.to_bytes(6, 'big') + pcm)


def parse(data):
    if len(data) != HEADER_SIZE + PAYLOAD_SIZE:
        raise FrameError('帧长度无效')
    magic, version, source, flags, _, stream_id, generation, _, seq = HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or source not in SOURCES:
        raise FrameError('帧头无效')
    return dict(source=SOURCES[source], replay=bool(flags & FLAG_REPLAY), stream_id=stream_id,
                generation=generation, seq=seq,
                t_client_us=int.from_bytes(data[HEADER.size:HEADER_SIZE], 'big'), pcm=data[HEADER_SIZE:])
