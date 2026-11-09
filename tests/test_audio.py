import wave

import pytest
from fastapi.testclient import TestClient

from app.audio.frame import FrameError, PAYLOAD_SIZE, pack, parse
from app.audio.recorder import StreamRecorder, wav_seconds
from app.server import create_app
from test_api import headers, prepared

PCM = b'\x01\x00' * 320


def frame(seq, replay=False, source='mic', generation=0, pcm=PCM):
    return pack(source, 7, generation, seq, pcm, t_client_us=seq * 20000, replay=replay)


def test_frame_roundtrip_and_header_size():
    data = frame(5, replay=True, source='meeting')
    assert len(data) == 24 + PAYLOAD_SIZE
    f = parse(data)
    assert (f['source'], f['seq'], f['replay'], f['stream_id'], f['t_client_us']) == ('meeting', 5, True, 7, 100000)
    assert f['pcm'] == PCM


@pytest.mark.parametrize('bad', [b'', frame(0)[:-2], b'XX' + frame(0)[2:], frame(0)[:2] + b'\x09' + frame(0)[3:]])
def test_bad_frames_rejected(bad):
    with pytest.raises(FrameError):
        parse(bad)


def test_recorder_continuous_dedupe_and_gap(tmp_path):
    r = StreamRecorder(tmp_path, 's')
    for seq in (0, 1, 2, 5, 6):
        assert r.write(seq, PCM)
    assert not r.write(1, PCM)  # duplicate dropped
    assert r.gaps() == [(60, 100)]  # frames 3,4 missing
    assert r.write(3, PCM) and r.write(4, PCM)  # replay fills the hole in place
    assert r.gaps() == []
    (idx, path, secs), = r.close()
    assert secs == pytest.approx(0.14) and wav_seconds(path) == pytest.approx(0.14)
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)


def test_recorder_slices_without_gap(tmp_path):
    r = StreamRecorder(tmp_path, 's', slice_frames=10)
    for seq in range(25):
        r.write(seq, PCM)
    slices = r.close()
    assert [round(s[2], 2) for s in slices] == [0.2, 0.2, 0.1]
    assert r.gaps() == []


def test_recorder_rejects_wrong_size(tmp_path):
    with pytest.raises(ValueError):
        StreamRecorder(tmp_path, 's').write(0, b'abc')


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_ws_records_acks_and_finalizes(client):
    h, lib, sid = prepared(client)
    token = h['X-Local-Token']
    with client.websocket_connect(f'/ws/audio/{sid}?token={token}') as ws:
        for seq in list(range(10)) + [12, 13, 14, 15, 16, 17, 18, 19]:
            ws.send_bytes(frame(seq))
        ws.send_bytes(b'garbage')
        assert ws.receive_json()['ack_seq'] == 9  # 10th stored frame
        assert ws.receive_json() == {'error': 'bad_frame'}
    # reconnect, replay the hole, then stop
    with client.websocket_connect(f'/ws/audio/{sid}?token={token}') as ws:
        ws.send_bytes(frame(10, replay=True))
        ws.send_bytes(frame(11, replay=True))
    assert client.post(f'/api/sessions/{sid}/stop', json={}, headers=h).status_code == 200
    rec = client.get(f'/api/sessions/{sid}/recordings').json()
    assert len(rec) == 1
    audio = client.get(f'/api/recordings/{rec[0]["id"]}')
    assert audio.headers['content-type'] == 'audio/wav' and len(audio.content) == 44 + 20 * 640
    with client.app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recording_gaps').fetchone()[0] == 0


def test_ws_records_gap_when_not_replayed(client):
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        for seq in (0, 1, 4):
            ws.send_bytes(frame(seq))
    client.post(f'/api/sessions/{sid}/stop', json={}, headers=h)
    with client.app.state.store.connect() as db:
        row = db.execute('SELECT t_start_ms,t_end_ms FROM recording_gaps').fetchone()
    assert (row[0], row[1]) == (40, 80)


def test_ws_rejects_bad_token_origin_and_inactive_session(client):
    h, lib, sid = prepared(client)
    for url, headers_ in ((f'/ws/audio/{sid}?token=bad', {}),
                          (f"/ws/audio/{sid}?token={h['X-Local-Token']}", {'origin': 'https://evil.example'}),
                          (f"/ws/audio/missing?token={h['X-Local-Token']}", {})):
        with pytest.raises(Exception):
            with client.websocket_connect(url, headers=headers_) as ws:
                ws.receive_json()


# ---- 评审缺陷回归 ----
import struct
from starlette.websockets import WebSocketDisconnect
from app.audio.recorder import FrameRejected


def _wav_frames(path):
    with wave.open(str(path)) as w:
        data = w.readframes(w.getnframes())
    return len(data) // PAYLOAD_SIZE, data


def test_s1_frame_after_stop_does_not_wipe_wav(client):
    h, lib, sid = prepared(client)
    token = h['X-Local-Token']
    with client.websocket_connect(f'/ws/audio/{sid}?token={token}') as ws:
        for seq in range(5):
            ws.send_bytes(frame(seq, generation=3))
        assert client.post(f'/api/sessions/{sid}/stop', json={}, headers=h).status_code == 200
        ws.send_bytes(frame(5, generation=3))
    path = client.app.state.store.root / 'audio' / sid / 'mic_7_g3_000.wav'
    n, data = _wav_frames(path)
    assert n == 5 and data == PCM * 5


def test_s1_ws_closed_when_frame_arrives_after_stop(client):
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        ws.send_bytes(frame(0))
        client.post(f'/api/sessions/{sid}/stop', json={}, headers=h)
        ws.send_bytes(frame(1))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4404


def test_s1_recorder_closed_rejects_and_reopen_keeps_data(tmp_path):
    r = StreamRecorder(tmp_path, 's')
    for seq in range(5):
        r.write(seq, PCM)
    r.close()
    with pytest.raises(FrameRejected):
        r.write(5, PCM)
    r2 = StreamRecorder(tmp_path, 's')  # 已存在的文件不得被 wb 清空
    r2.write(5, PCM)
    r2.close()
    n, data = _wav_frames(tmp_path / 's_000.wav')
    assert n == 6 and data == PCM * 6


def test_s3_ack_is_contiguous_watermark_and_flushed(client):
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        for seq in list(range(10)) + list(range(12, 20)):
            ws.send_bytes(frame(seq))
        ws.send_bytes(frame(10)); ws.send_bytes(frame(11))  # 第 20 帧：水位应跨过已补的洞
        a1, a2 = ws.receive_json(), ws.receive_json()
    assert a1['ack_seq'] == 9
    assert a2['ack_seq'] == 19


def test_s3_ack_never_passes_hole(client):
    h, lib, sid = prepared(client)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        for seq in list(range(10)) + list(range(12, 22)):
            ws.send_bytes(frame(seq))
        ws.send_bytes(b'garbage')
        assert ws.receive_json()['ack_seq'] == 9
        assert ws.receive_json() == {'error': 'bad_frame'}  # 第 20 帧后不能 ACK 19/21


def test_s3_recorder_flushes_each_frame(tmp_path):
    r = StreamRecorder(tmp_path, 's')
    r.write(1, PCM)
    assert (tmp_path / 's_000.wav').stat().st_size == 44 + 2 * PAYLOAD_SIZE  # 未 close 也已到 OS


def test_s3_disk_error_keeps_connection(client, monkeypatch):
    h, lib, sid = prepared(client)

    def boom(self, seq, pcm):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(StreamRecorder, 'write', boom)
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        ws.send_bytes(frame(0))
        assert ws.receive_json() == {'error': 'disk_error'}
        ws.send_bytes(b'garbage')
        assert ws.receive_json() == {'error': 'bad_frame'}  # 连接仍存活
    with client.app.state.store.connect() as db:
        rows = db.execute("SELECT reason FROM recording_gaps").fetchall()
    assert [r[0] for r in rows] == ['disk_error']


def test_s3_non_ascii_token_and_text_frame(client):
    h, lib, sid = prepared(client)
    with client.websocket_connect(f'/ws/audio/{sid}?token=%C3%A9') as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4403
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        ws.send_text('hello')
        assert ws.receive_json() == {'error': 'bad_frame'}
        ws.send_bytes(frame(0))


def test_s2_rejections_are_closed_after_accept_with_code(client):
    h, lib, sid = prepared(client)
    for url, code in ((f'/ws/audio/{sid}?token=bad', 4403), (f"/ws/audio/missing?token={h['X-Local-Token']}", 4404)):
        with client.websocket_connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
        assert e.value.code == code


def test_s3_seq_bounds(tmp_path):
    r = StreamRecorder(tmp_path, 's')
    with pytest.raises(FrameRejected):
        r.write(2 ** 32 - 1, PCM)
    assert not list(tmp_path.glob('*.wav')) and r.rejected == 1
    r.write(0, PCM)
    with pytest.raises(FrameRejected):
        r.write(6002, PCM)
    assert r.write(6000, PCM) and r.rejected == 2
    assert r.gaps() == [(20, 6000 * 20)]


def test_s3_recorder_count_cap(client):
    h, lib, sid = prepared(client)
    hub = client.app.state.audio_hub
    for g in range(16):
        hub.ingest(sid, parse(frame(0, generation=g)))
    with pytest.raises(FrameRejected):
        hub.ingest(sid, parse(frame(0, generation=16)))
    assert len([k for k in hub.recorders if k[0] == sid]) == 16


def test_capture_started_at_written_on_first_live_frame_only(client):
    """会话页“已用时长”从首个 live 帧算：replay 不写、重复不覆盖。"""
    h, lib, sid = prepared(client)
    hub = client.app.state.audio_hub
    store = client.app.state.store
    assert store.session(sid)['capture_started_at'] is None
    hub.ingest(sid, parse(frame(0, replay=True)))
    assert store.session(sid)['capture_started_at'] is None, 'replay 帧不算开始采集'
    hub.ingest(sid, parse(frame(1, generation=0)))
    first = store.session(sid)['capture_started_at']
    assert isinstance(first, float)
    hub.ingest(sid, parse(frame(2, generation=0)))
    assert store.session(sid)['capture_started_at'] == first, '重复帧不覆盖起点'


def test_s3_header_updates_every_50_new_frames(tmp_path):
    r = StreamRecorder(tmp_path, 's')
    for seq in range(1, 200, 2):  # 奇数 seq：旧逻辑 pos%50==0 永不命中
        r.write(seq, PCM)
    raw = (tmp_path / 's_000.wav').read_bytes()
    declared = struct.unpack_from('<I', raw, 40)[0]
    assert 50 * PAYLOAD_SIZE <= declared <= len(raw) - 44


def test_s3_wav_sequence_does_not_collide_with_legacy_upload(client):
    h, lib, sid = prepared(client)
    up = client.post(f'/api/sessions/{sid}/audio/mic/0', content=b'webm-bytes',
                     headers={**h, 'content-type': 'audio/webm'})
    assert up.status_code == 200
    with client.websocket_connect(f"/ws/audio/{sid}?token={h['X-Local-Token']}") as ws:
        ws.send_bytes(frame(0, generation=0))
    client.post(f'/api/sessions/{sid}/stop', json={}, headers=h)
    rec = client.get(f'/api/sessions/{sid}/recordings').json()
    assert sorted(r['sequence'] for r in rec) == [0, 1_000_000]
