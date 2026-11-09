"""凭证配置 E2E 后端：临时数据目录 + 假 Key + 只写临时 .env，绝不碰本机钥匙串与真实凭证。

stdout 首行 {"port"}；stdin {"op":"quit"} 退出。
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

from app.credentials import CredentialStore, DotenvBackend, EnvBackend  # noqa: E402
from app.server import create_app  # noqa: E402

ENV_KEY = 'sk-fake-env-dashscope-0000'


def main():
    data_dir = tempfile.mkdtemp(prefix='creds-e2e-')
    # 环境变量只注入假值；PUT/DELETE 只落到临时目录的 .env，不触碰钥匙串。
    creds = CredentialStore([EnvBackend({'DASHSCOPE_API_KEY': ENV_KEY}),
                             DotenvBackend(Path(data_dir) / '.env')])
    app = create_app(data_dir, credentials=creds)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port}), flush=True)
    for line in sys.stdin:
        if json.loads(line).get('op') == 'quit':
            break
    server.should_exit = True


if __name__ == '__main__':
    main()
