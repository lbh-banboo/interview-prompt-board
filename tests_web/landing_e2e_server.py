"""欢迎首页 E2E 后端：真实 create_app + 合成主库/次优库/会话，让统计数字有真实来源。

stdout 首行 {"port","counts":{"primary","secondary","sessions","asr"}}；stdin 收到任意行即退出。
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

from app.credentials import CredentialStore, MemoryBackend  # noqa: E402
from app.library.secondary import SecondaryLibrary  # noqa: E402
from app.server import create_app  # noqa: E402
from test_core import TEXT  # noqa: E402

MORE = '**H2-01｜00:01｜问题一？** 参考回答：要点一。\n**H2-02｜00:02｜问题二？** 参考回答：要点二。'
T0 = 1_700_000_000.0


def main():
    root = Path(tempfile.mkdtemp(prefix='landing-e2e-'))
    # 空凭证后端：不读环境变量里的真实 Key，转写方式标签才可预期（也无泄漏风险）
    app = create_app(root, credentials=CredentialStore([MemoryBackend({})]))
    store = app.state.store
    secondary = SecondaryLibrary(store)

    lib1 = store.import_text('合成主库A', TEXT)['id']
    lib2 = store.import_text('合成主库B', MORE)['id']
    store.review(lib1, 'H1-01', '这是经确认的练习原型。', 'approved', True)
    store.review(lib2, 'H2-01', '要点一。', 'approved', True)
    store.review(lib2, 'H2-02', '要点二。', 'approved', True)

    pending = [row for row in secondary.entries() if row['status'] == 'pending']
    for row in pending[:2]:
        secondary.review(row['id'], 'approved', True)

    stopped = store.start('online', lib1)['id']
    store.stop(stopped)
    running = store.start('offline', lib1)['id']
    # store.start 不写 started_at（由采集页负责）；给合成会话补上真实时间，最近会话才有时间可显示
    with store.connect() as db:
        db.execute('UPDATE sessions SET started_at=? WHERE id=?', (T0, stopped))
        db.execute('UPDATE sessions SET started_at=? WHERE id=?', (T0 + 3600, running))

    primary = sum((x['approved'] or 0) for x in store.libraries())
    approved_secondary = sum(1 for row in secondary.entries() if row['status'] == 'approved')
    with store.connect() as db:
        total = db.execute('SELECT COUNT(*) AS n FROM sessions').fetchone()['n']

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'stopped': stopped, 'running': running,
                      'counts': {'primary': primary, 'secondary': approved_secondary, 'sessions': total,
                                 'asr': '本地转写'}}), flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
