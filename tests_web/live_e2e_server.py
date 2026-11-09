"""会话页 E2E 后端：真实 uvicorn + setup 已开始会话 + 真实 board/audio WS。

stdout 首行 {"port","session"}；之后每条命令回一行 {"ok":true,...}。
命令：turn / publish / count_acks / streams / quit（与 board_e2e_server 一致，便于脚本化提示事件）。
"""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from app.domain.turn_detector import TurnEvent  # noqa: E402
from app.audio.voiceprint import FakeExtractor  # noqa: E402
from app.server import create_app  # noqa: E402
from app.sessions import profiles as P  # noqa: E402
from app.sessions import setup as S  # noqa: E402
from app.telemetry.metrics import Metrics  # noqa: E402
from test_core import TEXT  # noqa: E402


class ScriptedPipe:
    detector = type('D', (), {'phase': 'idle', 'answerer_active': False})()

    def __init__(self):
        self.turn_events = []

    def health(self):
        return {'state': 'ok', 'reason': ''}


def main():
    mode = 'offline' if '--offline' in sys.argv else 'online'
    app = create_app(tempfile.mkdtemp(prefix='live-e2e-'), config=dict(voice_extractor=FakeExtractor()))
    store, bus = app.state.store, app.state.bus
    bus.set_listener = lambda *a, **k: None  # 脚本化事件不需要真实 AnswerLoop（它需要事件循环）
    lib = store.import_text('sample', TEXT)['id']
    store.review(lib, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    pid = P.create_profile(store, '会话页测试', ['产品经理'], '', [lib])['id']
    sid = S.create_session_v2(store, mode, pid, ['产品经理'], [lib], True)['id']
    S.mark_ready(store, sid, dict(items=[], overall='PASS', has_fail=False, can_degrade_start=False,
                                  degraded_banner=None))
    S.start_session(store, sid)
    app.state.wiring.freeze(sid)  # 与生产 /api/setup/{sid}/start 相同：开始时冻结配置
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    pipe = ScriptedPipe()
    print(json.dumps({'port': port, 'session': sid, 'mode': mode}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        out = {'ok': True}
        if cmd['op'] == 'turn':
            # 脚本化事件不触发真实 AnswerLoop（它需要事件循环）；本测试只验证提示板渲染与 ACK。
            bus.set_listener(sid, None)
            pipe.turn_events.append(TurnEvent(cmd['type'], cmd.get('text', ''), cmd.get('ver', 1), 1000,
                                              cmd.get('reason', 'scripted'), kind=cmd.get('kind', ''),
                                              sub_questions=tuple(cmd.get('sub', ()))))
            bus.pull(sid, pipe)
        elif cmd['op'] == 'publish':
            out['published'] = getattr(bus, cmd['method'])(sid, *cmd['args']) is not None
        elif cmd['op'] == 'count_acks':
            m = Metrics(store.path)
            out['acks'] = len([r for r in m.rows(sid) if r[2] == 'display_ack'])
            m.conn.close()
        elif cmd['op'] == 'streams':
            with store.connect() as db:
                out['streams'] = [dict(r) for r in db.execute(
                    'SELECT source, stream_id, capture_generation, frames FROM audio_streams WHERE session_id=?', (sid,))]
        elif cmd['op'] == 'quit':
            break
        print(json.dumps(out), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
