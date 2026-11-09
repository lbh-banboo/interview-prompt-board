"""M03-2/3/5：声纹提取、登记质量检查、模板存储、HTTP 接口、标定与基准脚本（Fake 提取器，SIMULATED）。"""
import base64
import io
import os
import re
import sqlite3
import stat
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import voice
from app.audio import enroll as en
from app.audio import voiceprint as vp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import bench_voiceprint  # noqa: E402
import calibrate_voiceprint as cal  # noqa: E402

SR = 16000
TEMPLATE_DIR = {200: np.array([1, 0, 0, 0.]), 210: np.array([0.95, 0.3, 0, 0.]), 400: np.array([0, 1, 0, 0.]),
                600: np.array([0, 0, 1, 0.])}


def tone(freq, sec, amp=0.2, noise=0.002, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * SR)) / SR
    x = amp * np.sin(2 * np.pi * freq * t) + noise * rng.standard_normal(len(t))
    return (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()


def tone_fn(x):
    """按主频返回固定方向向量；主频未登记则返回与所有类别都接近正交的向量。"""
    f = int(np.argmax(np.abs(np.fft.rfft(x))) * SR / len(x) / 10) * 10
    return TEMPLATE_DIR.get(f, np.array([0, 0, 0, 1.]))


def fake():
    return vp.FakeExtractor(tone_fn)


# ---------- voiceprint ----------
def test_to_float_variants():
    assert vp.to_float(b'\x00\x40')[0] == pytest.approx(0.5)
    assert vp.to_float(np.array([16384], np.int16))[0] == pytest.approx(0.5)
    assert vp.to_float(np.array([0.25], np.float64)).dtype == np.float32


def test_cosine_and_zero_vector():
    assert vp.cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert vp.cosine([1, 0], [0, 1]) == pytest.approx(0)
    assert vp.cosine([0, 0], [1, 0]) == 0.0


def test_mean_template_normalized_and_scale_invariant():
    t = vp.mean_template([[10, 0], [0, 1]])
    assert np.linalg.norm(t) == pytest.approx(1)
    assert t[0] == pytest.approx(t[1])


def test_fake_extractor_default_separates_tones_and_records_calls():
    ex = vp.FakeExtractor()
    a, b, c = ex.extract(tone(150, 1)), ex.extract(tone(150, 1, seed=3)), ex.extract(tone(900, 1))
    assert vp.cosine(a, b) > 0.95 > vp.cosine(a, c)
    assert ex.calls == [SR] * 3


def test_load_extractor_missing_returns_none(tmp_path):
    assert vp.load_extractor(tmp_path / 'nope.onnx') is None


def test_sherpa_extractor_missing_model_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vp.SherpaExtractor(tmp_path / 'nope.onnx')


def test_sherpa_invalid_config(tmp_path, monkeypatch):
    f = tmp_path / 'm.onnx'
    f.write_bytes(b'x')
    import sherpa_onnx
    monkeypatch.setattr(sherpa_onnx.SpeakerEmbeddingExtractorConfig, 'validate', lambda self: False)
    with pytest.raises(RuntimeError):
        vp.SherpaExtractor(f)
    assert vp.load_extractor(f) is None


def test_bench_extract_with_injected_clock():
    ticks = iter(range(0, 1000))
    r = vp.bench_extract(lambda: fake(), tone(200, 1), runs=4, clock=lambda: next(ticks) / 1000)
    assert r['runs'] == 4 and r['hot_p50_ms'] == pytest.approx(1.0) and r['cold_ms'] == pytest.approx(1.0)


@pytest.mark.skipif(not vp.DEFAULT_MODEL.is_file(), reason='声纹模型未下载')
def test_real_model_smoke():
    ex = vp.SherpaExtractor()
    a, b = ex.extract(tone(150, 1.5)), ex.extract(tone(150, 1.5, seed=1))
    assert a.shape == (ex.dim,) and vp.cosine(a, b) > 0.9


# ---------- enroll 质量检查 ----------
def segs(sec=(4, 4, 4), freqs=(200, 200, 200), **kw):
    """每段 = 语音 + 0.6s 仅底噪的停顿（噪声底估计需要停顿，与自然朗读一致）。"""
    return [tone(f, s, seed=i, **kw) + tone(f, 0.6, amp=0.0, seed=i + 9) for i, (s, f) in enumerate(zip(sec, freqs))]


def test_enroll_ok_returns_template_and_public_has_no_embedding():
    r = en.check_enrollment(segs(), fake())
    assert r.ok and r.template is not None and r.reason is None
    assert r.quality['effective_speech_s'] >= 8 and r.quality['min_pair_similarity'] > 0.99
    assert 'template' not in r.public() and 'embedding' not in str(r.public())


def continuous(sec=11.0, freq=200, speech=1.2, pause=0.3):
    """模拟连续朗读：有声块之间留短暂停顿（停顿占比 >10%，噪声底才能估到环境噪声）。"""
    parts, i, t = [], 0, 0.0
    while t < sec:
        d = min(speech, sec - t)
        parts.append(tone(freq, d, seed=i))
        t += d
        if t < sec:
            p = min(pause, sec - t)
            parts.append(tone(freq, p, amp=0.0, seed=100 + i))
            t += p
        i += 1
    return b''.join(parts)


def test_enroll_segment_count_bounds():
    """0 段或超过 3 段都失败；1~3 段留给后续用例。"""
    r = en.check_enrollment([], fake())
    assert not r.ok and r.reason is en.Reason.NEED_SEGMENT and '至少' in r.message
    r = en.check_enrollment(segs(sec=(4, 4, 4, 4), freqs=(200, 200, 200, 200)), fake())
    assert not r.ok and r.reason is en.Reason.NEED_SEGMENT and '至少' in r.message


def test_enroll_continuous_no_pause_still_counts_speech():
    """连续朗读几乎没有停顿时，10 分位噪声底会取到语音本身；应退回绝对能量下限，不把整段判成 0 秒。"""
    r = en.check_enrollment([tone(200, 11)], fake())
    assert r.ok and r.quality['effective_speech_s'] >= 8


def test_enroll_single_segment_ok():
    """新的默认：一段连续录音，整段有效语音 ≥8 秒即可登记。"""
    r = en.check_enrollment([continuous()], fake())
    assert r.ok and r.template is not None and r.reason is None
    assert r.quality['effective_speech_s'] >= 8
    assert r.quality['min_pair_similarity'] > 0.99
    assert 'similarity_skipped' not in r.quality


def test_enroll_two_segments_still_ok():
    r = en.check_enrollment(segs(sec=(5, 5)), fake())
    assert r.ok and r.quality['effective_speech_s'] >= 8


def test_enroll_m03_t1_only_5s_speech():
    r = en.check_enrollment(segs(sec=(2, 2, 1)), fake())
    assert r.reason is en.Reason.TOO_SHORT_SPEECH and '录满约 15 秒' in r.message


def test_enroll_single_segment_short_message_has_effective_seconds():
    r = en.check_enrollment([continuous(6)], fake())
    assert r.reason is en.Reason.TOO_SHORT_SPEECH
    m = re.search(r'有效语音 ([\d.]+) 秒', r.message)
    assert m and float(m.group(1)) < 8
    assert '不足 8 秒' in r.message and '15 秒' in r.message


def mixed_single(freqs=(200, 400, 600), sec=3.5, pause=0.5):
    """一段连续录音，但三个等长子段是不同基频（模拟他人声音/中途换设备）。"""
    return b''.join(tone(f, sec, seed=i) + tone(f, pause, amp=0.0, seed=50 + i)
                    for i, f in enumerate(freqs))


def test_enroll_single_segment_inconsistent_when_sound_changes():
    r = en.check_enrollment([mixed_single()], fake())
    assert r.reason is en.Reason.INCONSISTENT
    assert r.quality['min_pair_similarity'] < 0.6


def test_enroll_similarity_skipped_when_too_short_to_split():
    """有效语音过短、无法切成 3 个子段时跳过相似度检查，并明确标注（不假造相似度）。"""
    r = en.check_enrollment(segs(sec=(0.7,)), fake(), min_speech_s=0.1)
    assert r.ok and r.quality['similarity_skipped'] is True
    assert 'min_pair_similarity' not in r.quality


def test_enroll_silence_counts_as_no_speech():
    r = en.check_enrollment([b'\x00\x00' * SR * 4] * 3, fake())
    assert r.reason is en.Reason.TOO_SHORT_SPEECH and r.quality['effective_speech_s'] == 0


def test_enroll_too_short_for_a_frame():
    assert en.analyze_segment(np.zeros(10, np.float32))[1] == 0.0


def test_enroll_low_snr():
    r = en.check_enrollment(segs(), fake(), min_snr_db=40)
    assert r.reason is en.Reason.LOW_SNR and r.quality['snr_db'] < 40


def test_enroll_inconsistent_segments():
    r = en.check_enrollment(segs(freqs=(200, 400, 600)), fake())
    assert r.reason is en.Reason.INCONSISTENT and r.quality['min_pair_similarity'] < 0.6


def test_enroll_threshold_override():
    r = en.check_enrollment(segs(sec=(2, 2, 2)), fake(), min_speech_s=5.0)
    assert r.ok


def test_reason_messages_cover_all():
    assert set(en.MESSAGES) == set(en.Reason)


# ---------- 模板存储 ----------
class FakeStore:
    def __init__(self, root):
        self.path = Path(root) / 'db.sqlite3'
        self.lock = threading.RLock()

    def connect(self):
        return sqlite3.connect(self.path)


@pytest.fixture
def pstore(tmp_path):
    return en.VoiceProfileStore(FakeStore(tmp_path).connect, threading.RLock(), tmp_path, clock=lambda: 123.0)


def test_store_roundtrip_replace_delete_and_perms(tmp_path, pstore):
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    assert pstore.load('p') is None
    pstore.save('p', np.array([1, 2, 3], np.float32), {'a': 1})
    pstore.save('p', np.array([4, 5, 6], np.float32), {'a': 2})
    t, q, ts = pstore.load('p')
    assert t.tolist() == [4, 5, 6] and q == {'a': 2} and ts == 123.0
    with sqlite3.connect(tmp_path / 'db.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM voice_profiles').fetchone()[0] == 1
    assert pstore.delete('p') is True and pstore.delete('p') is False and pstore.load('p') is None


def test_enroll_modules_have_no_network_imports():
    for m in (vp, en):
        src = Path(m.__file__).read_text()
        for bad in ('import socket', 'requests', 'urllib', 'httpx', 'http.client'):
            assert bad not in src


# ---------- HTTP ----------
def wav_b64(pcm):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


def pcm_b64(pcm):
    return base64.b64encode(pcm).decode()


@pytest.fixture
def client(tmp_path):
    ctx = SimpleNamespace(store=FakeStore(tmp_path), root=tmp_path, token='tok', bus=None,
                          config={'voice_extractor': fake()})
    app = FastAPI()
    voice.register(app, ctx)
    c = TestClient(app)
    c.ctx = ctx
    return c


H = {'x-local-token': 'tok'}


def test_api_requires_token(client):
    assert client.get('/api/voice/profile').status_code == 403
    assert client.post('/api/voice/enroll', json={'segments': []}, headers={'x-local-token': 'bad'}).status_code == 403
    assert client.delete('/api/voice/profile').status_code == 403


def test_api_enroll_status_delete_flow(client):
    st = client.get('/api/voice/profile', headers=H).json()
    assert st['enrolled'] is False and st['model_available'] is True and st['enroll_text']
    body = {'segments': [wav_b64(continuous())]}
    r = client.post('/api/voice/enroll', json=body, headers=H)
    assert r.status_code == 200 and r.json()['ok'] is True
    st = client.get('/api/voice/profile', headers=H).json()
    assert st['enrolled'] and 'embedding' not in st and st['quality']['effective_speech_s'] >= 8
    # 重录覆盖
    body2 = {'segments': [pcm_b64(continuous())]}
    assert client.post('/api/voice/enroll', json=body2, headers=H).json()['ok']
    # 旧客户端仍可提交 3 段
    legacy = {'segments': [pcm_b64(s) for s in segs()]}
    assert client.post('/api/voice/enroll', json=legacy, headers=H).json()['ok']
    assert client.delete('/api/voice/profile', headers=H).json() == {'deleted': True}
    assert client.get('/api/voice/profile', headers=H).json()['enrolled'] is False


def test_api_enroll_failure_not_saved(client):
    r = client.post('/api/voice/enroll', json={'segments': [pcm_b64(segs(sec=(1,))[0])]}, headers=H)
    assert r.json()['ok'] is False and r.json()['reason'] == 'too_short_speech'
    assert '有效语音' in r.json()['message'] and '15 秒' in r.json()['message']
    assert client.get('/api/voice/profile', headers=H).json()['enrolled'] is False


def test_api_bad_inputs(client):
    with pytest.raises(ValueError):
        voice.decode_segment('!!!')
    with pytest.raises(ValueError):
        voice.decode_segment(base64.b64encode(b'RIFFxxxx').decode())
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes(b'\x00' * 8)
    with pytest.raises(ValueError):
        voice.decode_segment(base64.b64encode(buf.getvalue()).decode())
    assert voice.decode_segment(base64.b64encode(b'\x01\x00\x02').decode()) == b'\x01\x00'


def test_api_model_unavailable_503(tmp_path):
    ctx = SimpleNamespace(store=FakeStore(tmp_path), root=tmp_path, token='tok', bus=None,
                          config={'voice_model': str(tmp_path / 'none.onnx')})
    app = FastAPI()
    voice.register(app, ctx)
    c = TestClient(app)
    assert c.get('/api/voice/profile', headers=H).json()['model_available'] is False
    r = c.post('/api/voice/enroll', json={'segments': []}, headers=H)
    assert r.status_code == 503 and '降级' in r.json()['detail']


def test_api_default_config_none(tmp_path):
    ctx = SimpleNamespace(store=FakeStore(tmp_path), root=tmp_path, token='tok', bus=None, config=None)
    app = FastAPI()
    voice.register(app, ctx)
    c = TestClient(app)
    assert 'model_available' in c.get('/api/voice/profile', headers=H).json()


# ---------- 标定 / 基准脚本 ----------
def test_compute_eer_perfect_and_overlapping():
    eer, thr = cal.compute_eer([0.9, 0.8, 0.85], [0.1, 0.2, 0.3])
    assert eer == 0 and 0.3 < thr <= 0.8
    eer, _ = cal.compute_eer([0.5, 0.6, 0.7, 0.8], [0.4, 0.5, 0.6, 0.7])
    assert 0 < eer < 0.5


def test_suggest_thresholds_order():
    high, low = cal.suggest_thresholds(np.linspace(0.6, 0.9, 100), np.linspace(0.0, 0.4, 100))
    assert high > 0.39 and low < 0.61


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes(pcm)


def make_dataset(root):
    for spk, f in (('a', 150), ('b', 900), ('c', 2500)):
        (root / spk).mkdir()
        for i in range(3):
            write_wav(root / spk / f'{i}.wav', tone(f, 1, seed=i))


def test_calibrate_report_simulated_without_model(tmp_path, capsys):
    make_dataset(tmp_path)
    out = tmp_path / 'r.txt'
    cal.main([str(tmp_path), '--model', str(tmp_path / 'no.onnx'), '--out', str(out)])
    text = out.read_text()
    assert text.splitlines()[0] == 'SIMULATED' and 'EER=' in text and '待真实数据标定' in text
    assert capsys.readouterr().out.startswith('SIMULATED')


def test_calibrate_marker_forces_simulated_even_with_model(tmp_path, monkeypatch):
    make_dataset(tmp_path)
    (tmp_path / 'SIMULATED').write_text('')
    model = tmp_path / 'm.onnx'
    model.write_bytes(b'x')
    monkeypatch.setattr(cal, 'SherpaExtractor', lambda p: vp.FakeExtractor())
    assert cal.report(str(tmp_path), str(model)).splitlines()[0] == 'SIMULATED'
    (tmp_path / 'SIMULATED').unlink()
    assert cal.report(str(tmp_path), str(model)).splitlines()[0] == 'REAL'


def test_calibrate_rejects_bad_dataset_and_wav(tmp_path):
    (tmp_path / 'a').mkdir()
    write_wav(tmp_path / 'a' / '0.wav', tone(150, 1))
    with pytest.raises(ValueError):
        cal.report(str(tmp_path), str(tmp_path / 'no.onnx'))
    (tmp_path / 'b').mkdir()
    with wave.open(str(tmp_path / 'b' / '0.wav'), 'wb') as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
        w.writeframes(b'\x00\x00')
    with pytest.raises(ValueError):
        cal.load_dataset(str(tmp_path))


def test_bench_script_not_tested_without_model(tmp_path, capsys):
    assert bench_voiceprint.main(['--model', str(tmp_path / 'no.onnx')]) == 1
    assert 'NOT TESTED' in capsys.readouterr().out


@pytest.mark.skipif(not vp.DEFAULT_MODEL.is_file(), reason='声纹模型未下载')
def test_bench_script_real(capsys):
    assert bench_voiceprint.main(['--seconds', '1', '--runs', '3']) == 0
    assert 'hot_p50_ms' in capsys.readouterr().out
