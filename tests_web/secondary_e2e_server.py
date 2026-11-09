"""次优库审核台 E2E 后端：真实 create_app + 合成种子（临时库，不碰 data/）。

stdout 首行 {"port"}；stdin 收到任意行即退出。
"""
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.library.secondary import SecondaryLibrary  # noqa: E402
from app.server import create_app  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix='secondary-e2e-'))
    seeds = tmp / 'seeds'
    shutil.copytree(ROOT / 'tests' / 'fixtures' / 'seeds_ok', seeds)
    app = create_app(tmp / 'data', config={'seeds_dir': str(seeds)})
    SecondaryLibrary(app.state.store).import_dir(seeds)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port}), flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
