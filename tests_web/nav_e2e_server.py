"""导航 E2E 后端：真实 uvicorn + 静态页面；通过 stdin 控制是否有进行中的会话。

stdout 首行 {"port","session"}；命令：
  {"op":"start"}   把预置会话置为 active，返回 {"ok":true,"session":"..."}
  {"op":"stop"}    结束该会话
  {"op":"status"}  返回 {"ok":true,"active": "id"|null}
  {"op":"quit"}
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

from app.server import create_app  # noqa: E402
from app.sessions import profiles as P  # noqa: E402
from app.sessions import setup as S  # noqa: E402
from test_core import TEXT  # noqa: E402


def main():
    app = create_app(tempfile.mkdtemp(prefix='nav-e2e-'))
    store, bus = app.state.store, app.state.bus
    bus.set_listener = lambda *a, **k: None  # 本测试不需要真实 AnswerLoop
    lib = store.import_text('sample', TEXT)['id']
    store.review(lib, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    pid = P.create_profile(store, '导航测试', ['产品经理'], '', [lib])['id']
    sid = S.create_session_v2(store, 'online', pid, ['产品经理'], [lib], True)['id']
    S.mark_ready(store, sid, dict(items=[], overall='PASS', has_fail=False,
                                  can_degrade_start=False, degraded_banner=None))
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'session': sid}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        out = {'ok': True}
        if cmd['op'] == 'start':
            S.start_session(store, sid)
            app.state.wiring.freeze(sid)
            out['session'] = sid
        elif cmd['op'] == 'stop':
            S.stop_session(store, sid)
        elif cmd['op'] == 'status':
            with store.connect() as db:
                row = db.execute("SELECT id FROM sessions WHERE status='active'").fetchone()
            out['active'] = row['id'] if row else None
        elif cmd['op'] == 'quit':
            break
        print(json.dumps(out), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
