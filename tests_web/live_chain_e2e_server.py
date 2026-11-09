"""会话页真实采集链路 E2E 后端：setup 会话 + 注入 MockASR（脚本化转写）+ HashEmbedder + 空凭证。

- asr_factory 注入 => AudioHub 不调用 wiring.asr_factory，绝不连阿里云；
- credentials 注入空 MemoryBackend => 没有 DeepSeek / Search Key，绝不外发；
- 主库索引预建（与 /api/setup/{sid}/start 后的中间件一致），AnswerLoop 走真实检索与发布。
stdout 首行 {"port","session","mode"}；之后每条命令回一行 {"ok":true,...}。
命令：metrics / snapshot / streams / status / quit。
"""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from app.asr.mock import MockASR  # noqa: E402
from app.audio.enroll import VoiceProfileStore  # noqa: E402
from app.audio.voiceprint import FakeExtractor  # noqa: E402
from app.credentials import CredentialStore, MemoryBackend  # noqa: E402
from app.retrieval.index import HashEmbedder  # noqa: E402
from app.server import create_app  # noqa: E402
from app.sessions import profiles as P  # noqa: E402
from app.sessions import setup as S  # noqa: E402
from app.telemetry.metrics import Metrics  # noqa: E402
QUESTION = '你能讲讲分布式事务的区别吗'
ANSWER = '先说结论：分布式事务常用两阶段提交和补偿方案。第一，两阶段提交强一致但有阻塞。第二，补偿方案最终一致。'
LIB = f'**H1-01｜00:01｜{QUESTION}** 参考回答：{ANSWER}'
# 单麦门控 E2E：脚本化声纹。模板固定为 [1,0,0,0]，不同角色给不同余弦。
VECTORS = {'answerer': [1.0, 0.0, 0.0, 0.0], 'questioner': [0.0, 1.0, 0.0, 0.0],
           'unknown': [0.45, 0.893, 0.0, 0.0]}


class RoleExtractor:
    def __init__(self, role):
        self.role = role

    def extract(self, pcm):
        return np.asarray(VECTORS[self.role], np.float32)


def arg(name, default=''):
    flag = '--' + name
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main():
    mode = arg('mode', 'online')
    question = arg('q', QUESTION)
    role = arg('role', '')
    voice = bool(role) and '--no-voice' not in sys.argv

    def asr_factory(source):
        # 提问方转写：会议/房间始终有；单麦门控下 mic 也走提问方 ASR。
        script = [(900, question, False, 1000)] if source in ('meeting', 'room') else []
        if source == 'mic' and role:
            script = [(900, question, False, 1000)]
        return MockASR(script, source)

    app = create_app(tempfile.mkdtemp(prefix='live-chain-e2e-'), asr_factory=asr_factory,
                     embedder=HashEmbedder(), credentials=CredentialStore([MemoryBackend()]),
                     config=dict(voice_extractor=RoleExtractor(role) if role else FakeExtractor()))
    store, bus = app.state.store, app.state.bus
    if voice:
        # 模板 = “本人”声纹；脚本化提取器对其他角色返回不同余弦。
        VoiceProfileStore(store.connect, store.lock, store.root).save(
            'default', np.asarray(VECTORS['answerer'], np.float32),
            dict(effective_speech_s=8.76, snr_db=17.8, min_pair_similarity=0.6354))
    lib = store.import_text('sample', LIB)['id']
    store.review(lib, 'H1-01', ANSWER, 'approved', True)
    pid = P.create_profile(store, '会话链路测试', ['产品经理'], '', [lib])['id']
    sid = S.create_session_v2(store, mode, pid, ['产品经理'], [lib], True)['id']
    S.mark_ready(store, sid, dict(items=[], overall='PASS', has_fail=False, can_degrade_start=False,
                                  degraded_banner=None))
    S.start_session(store, sid)
    app.state.wiring.freeze(sid)  # 与生产 /api/setup/{sid}/start 相同：开始时冻结配置
    app.state.index_provider.prepare(lib)  # 与生产相同的会前预建索引
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'session': sid, 'mode': mode}), flush=True)
    for line in sys.stdin:
        cmd = json.loads(line)
        out = {'ok': True}
        if cmd['op'] == 'metrics':
            m = Metrics(store.path)
            rows = [(r[2], json.loads(r[4]) if r[4] and r[4].startswith('{') else r[4])
                    for r in m.rows(sid)]
            m.conn.close()
            out['prompt_sent'] = [v for (n, v) in rows if n == 'prompt_sent']
            out['display_ack'] = len([1 for (n, _) in rows if n == 'display_ack'])
        elif cmd['op'] == 'snapshot':
            out['snapshot'] = bus.snapshot(sid)
        elif cmd['op'] == 'streams':
            with store.connect() as db:
                out['streams'] = [dict(r) for r in db.execute(
                    'SELECT source, stream_id, capture_generation, frames FROM audio_streams WHERE session_id=?', (sid,))]
        elif cmd['op'] == 'status':
            out['asr_backend'] = 'injected' if app.state.audio_hub.asr_factory is not None else None
        elif cmd['op'] == 'quit':
            break
        print(json.dumps(out), flush=True)
    server.should_exit = True


if __name__ == '__main__':
    main()
