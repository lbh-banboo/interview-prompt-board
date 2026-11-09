"""导入页 / 向导主库步骤 E2E 后端：真实 create_app + 临时数据目录（不碰 data/）。

用法：python import_e2e_server.py [--mode import|setup|empty]
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
sys.path.insert(0, str(ROOT / 'tests'))

from app.server import create_app  # noqa: E402
from app.sessions import profiles as P  # noqa: E402

FIX = ROOT / 'tests' / 'fixtures' / 'seeds_ok'
LIBS = [
    ('第一库', '**H1-01｜00:01｜第一库问题** 参考回答：第一库答案。', 'H1-01'),
    ('第二库', '**H2-01｜00:02｜第二库问题** 参考回答：第二库答案。', 'H2-01'),
]


def main():
    mode = 'import'
    if '--mode' in sys.argv:
        mode = sys.argv[sys.argv.index('--mode') + 1]
    tmp = Path(tempfile.mkdtemp(prefix='import-e2e-'))
    seeds = tmp / 'seeds'
    shutil.copytree(FIX, seeds)
    app = create_app(tmp / 'data', config={'seeds_dir': str(seeds)})
    store = app.state.store
    if mode in ('setup', 'empty'):
        P.create_profile(store, '导入测试', ['产品经理'], '', [])
    if mode == 'setup':
        for name, text, key in LIBS:
            lib = store.import_text(name, text)['id']
            store.review(lib, key, '这是经确认的练习原型。', 'approved', True)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'mode': mode}), flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
