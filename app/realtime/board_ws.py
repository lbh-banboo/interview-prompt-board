"""WS /ws/board/{session}：下行 board 事件，上行 display_ack；会话无音频超时停止（M01-5）。

事件来源：SessionPipeline 没有回调，这里每 POLL_S 秒调用 bus.pull() 读取其新增 turn_events（只读）。
握手校验与 audio_ws 一致：先 accept 再 close(4403/4404)，Origin 与 token 都要过。
"""
import asyncio
import json
import secrets
from time import perf_counter_ns
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from ..telemetry.metrics import Metrics

POLL_S = 0.1
SWEEP_EVERY = 10  # 每 N 次轮询检查一次空闲超时
IDLE_TIMEOUT_S = 2 * 3600


def mark_metric(store, session, turn_id, name, value):
    m = Metrics(store.path)
    try:
        m.mark(session, turn_id, name, perf_counter_ns(), json.dumps(value, ensure_ascii=False))
    finally:
        m.conn.close()


async def expire_idle(store, hub, bus, activity, timeout_s):
    """把无音频帧超过 timeout_s（秒数，或 session -> 秒数 的函数）的 active 会话置为结束（store.stop 的 'ended'），返回被停止的会话 id 列表。"""
    with store.connect() as db:
        active = [r['id'] for r in db.execute("SELECT id FROM sessions WHERE status='active'")]
    stopped = []
    for sid in active:
        if activity.idle_s(sid) > (timeout_s(sid) if callable(timeout_s) else timeout_s):
            pipe = hub.pipelines.get(sid)
            store.stop(sid)
            await hub.aclose(sid)
            bus.pull(sid, pipe)
            bus.end(sid)
            stopped.append(sid)
    return stopped


def register(app, store, token, hub, bus, activity, idle_timeout_s=IDLE_TIMEOUT_S, records=None):
    @app.websocket('/ws/board/{session}')
    async def board_ws(ws: WebSocket, session: str):
        await ws.accept()  # 与音频 WS 一致：accept 之后再 close，浏览器才能收到 4403/4404
        origin = ws.headers.get('origin')
        if (origin and urlsplit(origin).netloc != ws.headers.get('host')) \
                or not secrets.compare_digest(ws.query_params.get('token', '').encode(), token.encode()):
            await ws.close(code=4403)
            return
        with store.connect() as db:
            row = db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone()
        if not row:
            await ws.close(code=4404)
            return
        since = ws.query_params.get('since', '')
        q = bus.subscribe(session)
        sent = int(since) if since.isdigit() else None
        backlog = bus.replay(session, sent) if sent is not None else []
        if sent is None:  # 没带 since：不重放历史，只发订阅之后的新事件（历史由快照提供）
            sent = bus.snapshot(session)['event_seq']

        async def pump():
            nonlocal sent
            for ev in backlog:
                await ws.send_json(ev)
                sent = ev['event_seq']
            ticks = 0
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), POLL_S)
                except asyncio.TimeoutError:
                    ticks += 1
                    bus.pull(session, hub.pipelines.get(session), activity)
                    if ticks % SWEEP_EVERY == 0:
                        await expire_idle(store, hub, bus, activity, idle_timeout_s)
                    continue
                if ev['event_seq'] > sent:
                    await ws.send_json(ev)
                    sent = ev['event_seq']

        task = asyncio.create_task(pump())
        try:
            while True:
                message = await ws.receive()
                if message['type'] == 'websocket.disconnect' or task.done():
                    break
                try:
                    data = json.loads(message.get('text') or '')
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get('type') == 'display_ack':
                    prompt_id = data.get('prompt_id')
                    owner = bus.prompt_turn(session, prompt_id) if isinstance(prompt_id, str) else None
                    if owner:
                        mark_metric(store, session, owner[0], 'display_ack',
                                    dict(prompt_id=prompt_id, t_visible=data.get('t_visible'), epoch=owner[1]))
                        if records is not None:
                            records.mark_ack(session, prompt_id, data.get('t_visible'))
                elif isinstance(data, dict) and data.get('type') == 'candidate_accept':
                    # VAD 候选卡片：用户按 Enter 采用，升级为主答案（未确认的候选在轮次关闭时由总线清理）
                    bus.accept_candidate(session, data.get('candidate_id'))
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
            bus.unsubscribe(session, q)
