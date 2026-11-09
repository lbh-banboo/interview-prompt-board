"""WS /ws/audio/{session}: binary PCM frames -> continuous WAV, ACK watermark, recording gaps."""
import logging
import secrets
import threading
import time
from contextlib import closing
import uuid
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from ..asr.mock import MockASR
from ..audio.frame import FrameError, parse
from ..audio.recorder import FRAME_MS, FrameRejected, StreamRecorder
from ..audio.vad import make_vad
from ..domain.turn_detector import TurnDetector
from ..telemetry.metrics import Metrics
from .pipeline import SessionPipeline

ACK_EVERY = 10  # frames (200 ms)
MAX_RECORDERS = 16  # 每会话
WAV_SEQUENCE_BASE = 1_000_000  # 旧 MediaRecorder 上传接口的 sequence 上限为 100000
log = logging.getLogger('app')


class _StoreMetrics:
    """每次写入开关一次连接，不持有长连接（避免跨线程与未关闭连接的 ResourceWarning）。"""

    def __init__(self, store, sink=None):
        self.store = store
        self.sink = sink  # (session, turn_id, name, value) -> 额外旁路（迟到归档入记录库）

    def mark(self, *args, **kw):
        if self.sink is not None:
            self.sink(*args[:2], args[2], args[4] if len(args) > 4 else kw.get('value'))
        m = Metrics(self.store.path)
        try:
            m.mark(*args, **kw)
        finally:
            m.conn.close()


class AudioHub:
    def __init__(self, store, slice_frames=None, asr_factory=None, vad_factory=make_vad, trigger='coverage'):
        self.store = store
        self.trigger = trigger
        self.on_pipeline = None  # (session, pipe) -> 管线创建后的挂接钩子（AnswerLoop）
        self.on_closed = None  # async (session) -> 会话停止后的收尾钩子
        self.configure = None  # (session, mode) -> SessionPipeline 额外参数 dict（detector / monitor / asr_factory 等）
        self.on_late_metric = None  # (session, turn_id, name, value_json) -> 旁路 metrics（late_archived 入记录库）
        self.on_progress = None  # (session) -> 每处理完一帧后的钩子（立即拉取轮次事件，不等 100ms 轮询）
        self.slice_frames = slice_frames
        self.asr_factory = asr_factory or (lambda source: MockASR([], source))
        self.vad_factory = vad_factory
        self.recorders = {}  # (session, source, stream_id, generation) -> StreamRecorder
        self.pipelines = {}  # session -> SessionPipeline（懒创建）
        self.finalized = set()  # 已 finalize 的 session，拒收后续帧
        self._capture_started = set()  # 已写过采集起点的 session
        self.lock = threading.Lock()  # ingest 与 finalize 互斥

    def pipeline(self, session):
        pipe = self.pipelines.get(session)
        if pipe is None:
            with closing(self.store.connect()) as db:
                row = db.execute('SELECT mode FROM sessions WHERE id=?', (session,)).fetchone()
            mode = row['mode'] if row else 'online'
            kw = dict(vad_factory=self.vad_factory, detector=TurnDetector(trigger=self.trigger))
            kw.update(self.configure(session, mode) if self.configure else {})
            sink = self.on_late_metric
            pipe = self.pipelines[session] = SessionPipeline(
                session, self.store, kw.pop('asr_factory', self.asr_factory),
                _StoreMetrics(self.store, sink), mode=mode, **kw)
            if self.on_pipeline:
                self.on_pipeline(session, pipe)
        return pipe

    async def aclose(self, session):
        """停止会话：关闭管线（排空 ASR、写完埋点）后 finalize 录音。"""
        pipe = self.pipelines.pop(session, None)
        if pipe is not None:
            await pipe.close(final=True)
        if self.on_closed:
            await self.on_closed(session)
        self.finalize(session)

    def ingest(self, session, frame):
        with self.lock:
            if session in self.finalized:
                raise FrameRejected('会话已结束')
            if not frame.get('replay') and session not in self._capture_started:
                self._capture_started.add(session)
                try:  # 采集起点用于会话页“已用时长”；只写一次，失败不影响收帧
                    with self.store.lock, self.store.connect() as db:
                        db.execute("UPDATE sessions SET capture_started_at=COALESCE(capture_started_at,?) WHERE id=?",
                                   (time.time(), session))
                except Exception:
                    log.warning('记录采集起点失败', exc_info=True)
            key = (session, frame['source'], frame['stream_id'], frame['generation'])
            rec = self.recorders.get(key)
            if rec is None:
                if sum(1 for k in self.recorders if k[0] == session) >= MAX_RECORDERS:
                    raise FrameRejected('录音流数量超限')
                kw = {'slice_frames': self.slice_frames} if self.slice_frames else {}
                name = f"{frame['source']}_{frame['stream_id']}_g{frame['generation']}"
                rec = self.recorders[key] = StreamRecorder(self.store.root / 'audio' / session, name, **kw)
            # replay frames are archive-only: the pipeline drops them before VAD/ASR
            return rec.write(frame['seq'], frame['pcm'])

    def watermark(self, session, frame):
        with self.lock:
            rec = self.recorders.get((session, frame['source'], frame['stream_id'], frame['generation']))
            return rec.watermark if rec else -1

    def record_gap(self, session, frame, reason):
        t = frame['seq'] * FRAME_MS
        with self.store.lock, self.store.connect() as db:
            db.execute('INSERT INTO recording_gaps(session_id,source,capture_generation,t_start_ms,t_end_ms,reason) VALUES(?,?,?,?,?,?)',
                       (session, frame['source'], frame['generation'], t, t + FRAME_MS, reason))

    def finalize(self, session):
        """Close the session's recorders and register files and gaps; idempotent."""
        with self.lock:
            self.finalized.add(session)
            keys = [k for k in self.recorders if k[0] == session]
            with self.store.lock, self.store.connect() as db:
                for key in keys:
                    _, source, stream_id, generation = key
                    rec = self.recorders.pop(key)
                    gaps = rec.gaps()
                    db.execute('INSERT INTO audio_streams(id,session_id,source,stream_id,capture_generation,frames) VALUES(?,?,?,?,?,?)',
                               (uuid.uuid4().hex, session, source, stream_id, generation, len(rec.seen)))
                    for start, end in gaps:
                        db.execute('INSERT INTO recording_gaps(session_id,source,capture_generation,t_start_ms,t_end_ms,reason) VALUES(?,?,?,?,?,?)',
                                   (session, source, generation, start, end, 'missing_frames'))
                    for index, path, _ in rec.close():
                        db.execute('INSERT OR IGNORE INTO recordings VALUES(?,?,?,?,?,?)',
                                   (uuid.uuid4().hex, session, source, 'audio/wav',
                                    WAV_SEQUENCE_BASE + generation * 1000 + index,
                                    str(path.relative_to(self.store.root))))


def register(app, store, token, hub):
    @app.websocket('/ws/audio/{session}')
    async def audio_ws(ws: WebSocket, session: str):
        await ws.accept()  # accept 之前 close 会被浏览器看成 HTTP 403 / 1006，客户端收不到 4403/4404
        origin = ws.headers.get('origin')
        if (origin and urlsplit(origin).netloc != ws.headers.get('host')) \
                or not secrets.compare_digest(ws.query_params.get('token', '').encode(), token.encode()):
            await ws.close(code=4403)
            return
        with store.connect() as db:
            row = db.execute("SELECT 1 FROM sessions WHERE id=? AND status='active'", (session,)).fetchone()
        if not row:
            await ws.close(code=4404)
            return
        counts, acked = {}, {}
        try:
            while True:
                message = await ws.receive()
                if message['type'] == 'websocket.disconnect':
                    break
                data = message.get('bytes')
                if data is None:
                    await ws.send_json({'error': 'bad_frame'})
                    continue
                try:
                    frame = parse(data)
                except FrameError:
                    await ws.send_json({'error': 'bad_frame'})
                    continue
                if session in hub.finalized:
                    await ws.close(code=4404)
                    return
                try:
                    hub.ingest(session, frame)
                except FrameRejected:
                    await ws.send_json({'error': 'rejected'})
                    continue
                except OSError:
                    hub.record_gap(session, frame, 'disk_error')
                    await ws.send_json({'error': 'disk_error'})
                    continue
                await hub.pipeline(session).on_frame(frame)
                if hub.on_progress:
                    hub.on_progress(session)
                k = (frame['source'], frame['stream_id'], frame['generation'])
                counts[k] = counts.get(k, 0) + 1
                if counts[k] % ACK_EVERY == 0:
                    mark = hub.watermark(session, frame)  # 无洞前缀；写盘时已 flush
                    if mark >= 0 and mark != acked.get(k):
                        acked[k] = mark
                        await ws.send_json({'ack_seq': mark, 'source': frame['source'],
                                            'stream_id': frame['stream_id'], 'generation': frame['generation']})
        finally:
            pipe = hub.pipelines.get(session)
            if pipe is not None:
                await pipe.close()  # 轮次状态保留，重连后路由重建
