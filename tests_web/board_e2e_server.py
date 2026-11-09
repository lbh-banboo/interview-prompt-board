"""E2E 测试模式后端：起真实 uvicorn + 真实 board WS，通过 stdin JSON 行灌入脚本化事件。

stdout 首行 {"port","session"}；之后每条命令回一行 {"ok":true,...}。
命令：{"op":"turn","type":"committed|candidate|...","text":"..","ver":1}
      {"op":"publish","method":"publish_prompt_primary","args":[..]}（会话 id 自动作为第一个参数）
      turn 可带 kind / sub（子问题数组）；type 可为 interrupt / queued / dequeued / answer_end_candidate / answer_closed / text_updated
      {"op":"count_acks"} / {"op":"quit"}
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
from app.server import create_app  # noqa: E402
from app.telemetry.metrics import Metrics  # noqa: E402
from test_core import TEXT  # noqa: E402


class ScriptedPipe:
    detector = type('D', (), {'phase': 'idle', 'answerer_active': False})()

    def __init__(self):
        self.turn_events = []

    def health(self):
        return {'state': 'ok', 'reason': ''}


def main():
    app = create_app(tempfile.mkdtemp(prefix='board-e2e-'))
    store, bus = app.state.store, app.state.bus
    lib = store.import_text('sample', TEXT)['id']
    store.review(lib, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    sid = store.start('online', lib)['id']
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    pipe = ScriptedPipe()
    print(json.dumps({'port': port, 'session': sid}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        out = {'ok': True}
        if cmd['op'] == 'turn':
            pipe.turn_events.append(TurnEvent(cmd['type'], cmd.get('text', ''), cmd.get('ver', 1), 1000, cmd.get('reason', 'scripted'),
                                        kind=cmd.get('kind', ''), sub_questions=tuple(cmd.get('sub', ()))))
            bus.pull(sid, pipe)
        elif cmd['op'] == 'publish':
            ev = getattr(bus, cmd['method'])(sid, *cmd['args'])
            out['published'] = ev is not None
        elif cmd['op'] == 'count_acks':
            m = Metrics(store.path)
            out['acks'] = len([r for r in m.rows(sid) if r[2] == 'display_ack'])
            m.conn.close()
        elif cmd['op'] == 'quit':
            break
        print(json.dumps(out), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
