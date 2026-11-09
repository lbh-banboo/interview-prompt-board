"""setup 页 E2E 后端：真实 uvicorn + 真实自检路由；自检依赖注入为确定性结果（音源保持 WARN 语义）。"""
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

from app.audio.voiceprint import FakeExtractor  # noqa: E402
from app.server import create_app  # noqa: E402
from app.sessions import profiles as P  # noqa: E402
from app.sessions.selfcheck import WARN  # noqa: E402
from test_core import TEXT  # noqa: E402

DEPS = dict(audio_sources=lambda: (WARN, '音源需在采集页授权后检测'),
            asr_ping=lambda: True,
            llm_ping=lambda: True,
            search_ping=lambda: (WARN, '搜索未配置，联网补充不可用'),
            voice_enrolled=lambda: True,
            disk_free=lambda: 9 * (1 << 30))


def main():
    missing = '--voice-missing' in sys.argv
    deps = {**DEPS, 'voice_enrolled': (lambda: False) if missing else (lambda: True)}
    app = create_app(tempfile.mkdtemp(prefix='setup-e2e-'),
                     config=dict(selfcheck_deps=deps, voice_extractor=FakeExtractor()))
    store = app.state.store
    lib = store.import_text('sample', TEXT)['id']
    store.review(lib, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    P.create_profile(store, 'Setup 测试', ['产品经理'], '', [lib])
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd['op'] == 'quit':
            break
        print(json.dumps({'ok': True}), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
