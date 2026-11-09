"""提问点校对台 E2E 后端：真实 create_app + annotations 路由；临时目录里的假 wav/json，不读真实录音。

stdout 首行 {"port","ann"}；stdin 收到任意行即退出。
"""
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import annotations  # noqa: E402
from app.server import create_app  # noqa: E402


def fixtures(base):
    ann = base / 'annotations'
    ann.mkdir()
    audio = base / 'audio16k'
    audio.mkdir()
    items = [dict(id=i, t_end_ms=1000 * i, text_final=f'合成句{i}？', guess_role='questioner', guess_kind='question',
                  confidence=0.6, verified=False,
                  human=dict(is_question_end=None, role=None, is_topic_change=None, note='')) for i in (1, 2, 3)]
    (ann / 'demo.review.json').write_text(
        json.dumps(dict(audio_id='demo', duration_ms=8000, items=items), ensure_ascii=False), encoding='utf-8')
    with wave.open(str(audio / 'demo.wav'), 'wb') as w:  # 8 秒静音，够 <audio> 拖动
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\x00\x00' * 16000 * 8)
    return ann, audio


def main():
    base = Path(tempfile.mkdtemp(prefix='annotate-e2e-'))
    ann, audio = fixtures(base)
    app = create_app(base / 'data')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    token = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status'))['token']
    n = len(app.router.routes)
    annotations.register(app, SimpleNamespace(store=None, root=base, token=token, bus=None,
                                              config={'annotations_dir': str(ann), 'audio16k_dir': str(audio)}))
    new = app.router.routes[n:]
    del app.router.routes[n:]
    app.router.routes[0:0] = new  # 新路由须排在 '/' 静态挂载之前
    print(json.dumps({'port': port, 'ann': str(ann)}), flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
