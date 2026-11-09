"""采集页 E2E 后端：起真实 uvicorn，预置一个已开始的 setup 会话（roles 已写入）。"""
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
    app = create_app(tempfile.mkdtemp(prefix='capture-e2e-'))
    store = app.state.store
    lib = store.import_text('sample', TEXT)['id']
    store.review(lib, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    pid = P.create_profile(store, '采集页测试', ['产品经理'], '', [lib])['id']
    sid = S.create_session_v2(store, 'online', pid, ['产品经理'], [lib], True)['id']
    S.mark_ready(store, sid, dict(items=[], overall='PASS', has_fail=False, can_degrade_start=False,
                                  degraded_banner=None))
    S.start_session(store, sid)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'session': sid}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd['op'] == 'quit':
            break
        print(json.dumps({'ok': True}), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
