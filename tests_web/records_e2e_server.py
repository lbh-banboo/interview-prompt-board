"""记录库 E2E 后端：真实 create_app + 合成 3 场会话（含逐字稿/轮次/提示/录音/缺口）。

stdout 首行 {"port","sessions":{"full","no_answerer","active"}}；stdin 收到任意行即退出。
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
from test_core import TEXT  # noqa: E402

WAV = b'RIFF\x24\x00\x00\x00WAVEfmt ' + b'\x00' * 40
T0 = 1_700_000_000.0


def main():
    root = Path(tempfile.mkdtemp(prefix='records-e2e-'))
    app = create_app(root)
    store, rec = app.state.store, app.state.record_store

    lib = store.import_text('合成主库', TEXT)['id']
    pid = P.create_profile(store, '合成候选人B', ['技术开发'], '', [lib])['id']
    cfg = json.dumps(dict(mode='online', profile=dict(id='', alias='合成候选人A'), roles=['产品经理'],
                          library_ids=[lib], libraries={lib: '合成主库'}), ensure_ascii=False)
    wav = root / 'synth_meeting.wav'
    wav.write_bytes(WAV)
    with store.connect() as db:
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,roles_json,config_snapshot_json,"
                   "created_at,started_at,stopped_at,degraded_banner) VALUES(?,?,?,?,NULL,?,?,?,?,?,?)",
                   ('full', 'online', 'stopped', lib, json.dumps(['产品经理'], ensure_ascii=False), cfg,
                    T0 - 100, T0, T0 + 600, '自检有失败项：ASR 未配置（降级开始）'))
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,profile_id,roles_json,"
                   "created_at,started_at,stopped_at) VALUES(?,?,?,?,NULL,?,?,?,?,?)",
                   ('no_answerer', 'offline', 'stopped', lib, pid, json.dumps(['技术开发'], ensure_ascii=False),
                    T0 + 900, T0 + 1000, T0 + 1080))
        db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id,created_at,started_at) "
                   "VALUES('running','online','active',?,NULL,?,?)", (lib, T0 + 1900, T0 + 2000))
        db.execute("INSERT INTO recordings VALUES('rec1','full','meeting','audio/wav',0,?)", (str(wav),))
        db.execute("INSERT INTO recording_gaps(session_id,source,t_start_ms,t_end_ms,reason) "
                   "VALUES('full','meeting',1000,2000,'ws_buffer_full')")

    rec.append_segment('full', 'meeting', 'questioner', '请介绍一个你负责的合成项目', True, T0 + 10, T0 + 12)
    rec.append_segment('full', 'mic', 'answerer', '我负责过合成项目', True, T0 + 14, T0 + 16)
    rec.append_segment('full', 'meeting', 'questioner', '后来怎么复盘', True, T0 + 20, T0 + 22)
    rec.record_turn_event('full', 'committed', 't1', 1, t=T0 + 11, text='请介绍一个你负责的合成项目', kind='question')
    rec.record_turn_event('full', 'committed', 't2', 1, t=T0 + 21, text='后来怎么复盘', kind='followup')
    rec.record_turn_event('full', 'rolled_back', 't2', 1, t=T0 + 22.5)
    with rec.lock:
        rec.conn.execute('UPDATE turns SET t_visible=? WHERE session_id=? AND id=?', (T0 + 11.8, 'full', 't1'))
        rec.conn.commit()
    rec.record_prompt('full', 't1', 1, 'primary', ['结论：先讲目标', '要点：拆分约束'], 'k-full-1', '主库', level='L1')
    rec.record_prompt('full', 't1', 1, 'supplement', '合成联网补充内容', 'k-full-2', 'web',
                      ['https://example.com/synth'], level='L3')
    rec.record_prompt('full', 't2', 1, 'supplement', '迟到补充内容', 'k-full-3', 'web', state='late_archived')

    rec.append_segment('no_answerer', 'meeting', 'questioner', '只转写提问方的问题', True, T0 + 1001, T0 + 1003)
    rec.record_turn_event('no_answerer', 'committed', 't9', 1, t=T0 + 1001, text='只转写提问方的问题', kind='question')
    rec.record_prompt('no_answerer', 't9', 1, 'supplement', '合成追问补充', 'k-na-1', '追问', level='L2')

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    print(json.dumps({'port': port, 'sessions': {'full': 'full', 'no_answerer': 'no_answerer', 'active': 'running'}}),
          flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
