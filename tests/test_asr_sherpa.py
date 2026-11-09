import asyncio
import os
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.asr import sherpa
from app.asr.base import ReconnectingASR
from app.asr.sherpa import DEFAULT_MODEL_DIR, SherpaStreamingASR

pytestmark = pytest.mark.asyncio

PCM = b'\0' * 640  # 20ms


class FakeResult:
    def __init__(self, text, timestamps=(), start_time=0.0):
        self.text, self.timestamps, self.start_time = text, list(timestamps), start_time


class FakeRecognizer:
    """脚本化：按“已喂入帧序号”给出 (text, endpoint)。每 ready_every 帧才有一次 decode（模拟 chunk）。"""

    def __init__(self, script, ready_every=1, timestamps=()):
        self.script, self.ready_every, self.ts = script, ready_every, timestamps
        self.frames = 0
        self.pending = False
        self.cur = ('', False)
        self.resets = 0
        self.finished = False

    def create_stream(self):
        return self

    def accept_waveform(self, sr, x):
        assert sr == 16000 and x.dtype.name == 'float32' and len(x) == 320
        self.frames += 1
        if self.frames % self.ready_every == 0:
            self.pending = True

    def is_ready(self, s):
        return self.pending

    def decode_stream(self, s):
        self.pending = False
        self.cur = self.script.get(self.frames, self.cur)

    def get_result_all(self, s):
        return FakeResult(self.cur[0], self.ts)

    def is_endpoint(self, s):
        return self.cur[1]

    def reset(self, s):
        self.resets += 1
        self.cur = ('', False)

    def input_finished(self):
        self.finished = True


async def run(rec, n, t0=0, **kw):
    asr = SherpaStreamingASR(recognizer=rec, clock_ns=lambda: 7, **kw)
    await asr.start('s')
    for i in range(n):
        await asr.push(PCM, t0 + i * 20)
    await asr.close()
    return [e async for e in asr.events()], asr


async def test_partial_final_mapping_and_versions():
    rec = FakeRecognizer({10: ('你好', False), 20: ('你好世界', False), 30: ('你好世界。', True),
                          40: ('下一句', False)})
    evs, _ = await run(rec, 40, lookahead_ms=10**6)  # 覆盖钳在句首，只剩文本变化事件
    got = [(e.text, e.is_final, e.text_version) for e in evs]
    assert got[:4] == [('你好', False, 1), ('你好世界', False, 2), ('你好世界。', True, 3), ('下一句', False, 1)]
    assert got[-1] == ('下一句', True, 2)  # close 补 final
    assert rec.resets == 1 and rec.finished
    assert all(e.source == 'meeting' and e.t_received_ns == 7 for e in evs)


async def test_sentence_start_constant_and_next_sentence_starts_at_endpoint():
    rec = FakeRecognizer({10: ('a', False), 20: ('ab', False), 30: ('ab', True), 40: ('c', False), 50: ('cd', False)})
    evs, _ = await run(rec, 50, t0=1000, lookahead_ms=10**6)
    s1 = [e for e in evs if e.t_audio_start_ms == 1000]
    assert [e.text for e in s1] == ['a', 'ab', 'ab']
    s2 = [e for e in evs if e.t_audio_start_ms != 1000]
    assert {e.t_audio_start_ms for e in s2} == {1000 + 30 * 20}  # endpoint 时已喂入的音频时刻
    assert [e.text_version for e in s2] == [1, 2, 3]


async def test_covered_is_conservative_monotonic_and_not_pushed_total():
    rec = FakeRecognizer({10: ('a', False)}, ready_every=10)
    evs, _ = await run(rec, 100, lookahead_ms=600)
    # decode 在 200,400,...,2000ms：减 600 后 <0(钳到句首0)/<0/0/200/.../1400；最后是 close 补的 final
    partial_cov = [e.audio_covered_until_ms for e in evs if not e.is_final]
    assert partial_cov == [0, 200, 400, 600, 800, 1000, 1200, 1400]
    for e in evs:
        assert e.audio_covered_until_ms <= 100 * 20 - 600  # 绝不等于已推音频总长
        assert e.t_audio_end_ms == e.audio_covered_until_ms
    assert evs[-1].is_final


async def test_covered_advances_without_text_change_but_only_when_cov_changes():
    rec = FakeRecognizer({5: ('x', False)})
    evs, _ = await run(rec, 60, lookahead_ms=100)
    # 文本不变，覆盖随 decode 推进：每帧一次 decode，覆盖每 20ms 变一次 -> 持续有事件
    partial = [e for e in evs if not e.is_final]
    cov = [e.audio_covered_until_ms for e in partial]
    assert len(set(cov)) == len(cov) and cov[-1] == 60 * 20 - 100  # 同文本重复事件只在覆盖变化时发
    assert [e.text_version for e in evs] == list(range(1, len(evs) + 1)) and evs[-1].is_final


async def test_token_timestamps_used_when_available():
    rec = FakeRecognizer({5: ('hi', False)}, timestamps=[0.1, 0.3])
    evs, _ = await run(rec, 10, t0=500, lookahead_ms=600)
    assert evs[0].audio_covered_until_ms == 500 + 300


async def test_close_semantics():
    rec = FakeRecognizer({})
    evs, asr = await run(rec, 5)
    assert evs == [] and asr.health()['state'] == 'ok'
    await asr.close()  # 幂等
    with pytest.raises(ConnectionError):
        await asr.push(PCM, 0)
    assert [e async for e in asr.events()] == []


async def test_empty_endpoint_advances_segment_start():
    rec = FakeRecognizer({10: ('', True), 20: ('z', False)})
    evs, _ = await run(rec, 20, lookahead_ms=10**6)
    assert evs[0].t_audio_start_ms == 10 * 20 and evs[0].text_version == 1


async def test_inference_error_marks_down():
    class Boom(FakeRecognizer):
        def decode_stream(self, s):
            raise RuntimeError('boom')

    asr = SherpaStreamingASR(recognizer=Boom({}))
    await asr.start('s')
    with pytest.raises(RuntimeError):
        await asr.push(PCM, 0)
    assert asr.health()['state'] == 'down'
    with pytest.raises(RuntimeError):
        await asr.push(PCM, 20)
    await asr.close()


async def test_infer_time_recorded():
    _, asr = await run(FakeRecognizer({}), 3)
    assert len(asr.infer_ms) == 3 and all(x >= 0 for x in asr.infer_ms)


async def test_recognizer_cached_by_model_path_and_params(monkeypatch):
    sherpa._CACHE.clear()
    made = []

    def create(model_dir, num_threads, rule1, rule2, rule3):
        made.append((model_dir, num_threads, rule1, rule2, rule3))
        return FakeRecognizer({})

    monkeypatch.setattr(sherpa, '_create_recognizer', create)
    a = sherpa.build_recognizer(model_dir='m', num_threads=2)
    b = sherpa.build_recognizer(model_dir='m', num_threads=2)
    c = sherpa.build_recognizer(model_dir='m', num_threads=3)
    assert a is b and c is not a
    assert made == [('m', 2, 2.4, 1.2, 20.0), ('m', 3, 2.4, 1.2, 20.0)]
    sherpa._CACHE.clear()


async def test_recognizer_cache_is_thread_safe(monkeypatch):
    sherpa._CACHE.clear()
    made = []

    def create(*args):
        import time
        time.sleep(0.05)  # 放大并发窗口：无锁会重复加载
        made.append(args)
        return FakeRecognizer({})

    monkeypatch.setattr(sherpa, '_create_recognizer', create)
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda _: sherpa.build_recognizer(model_dir='same'), range(8)))
    assert len(made) == 1 and all(r is got[0] for r in got)
    sherpa._CACHE.clear()


@pytest.mark.asyncio
async def test_reconnect_reuses_cached_recognizer(monkeypatch):
    sherpa._CACHE.clear()
    made = []

    def create(*args):
        made.append(args)
        return FakeRecognizer({})

    monkeypatch.setattr(sherpa, '_create_recognizer', create)
    r = ReconnectingASR(lambda: SherpaStreamingASR(model_dir='cached'), backoffs=(0, 0, 0),
                       sleep=lambda _s: asyncio.sleep(0))
    await r.start('s')
    for _ in range(3):  # 连续三次“重连”
        await r._inner.close()
        for _ in range(5):
            await asyncio.sleep(0)
    await r.close()
    assert r.health()['state'] == 'ok' and len(made) == 1
    sherpa._CACHE.clear()


@pytest.mark.skipif(not os.path.exists(f'{DEFAULT_MODEL_DIR}/encoder.int8.onnx'), reason='模型未下载')
async def test_real_model_smoke():
    wav = 'eval/audio16k/pdd.wav'
    if not os.path.exists(wav):
        pytest.skip('无本地音频')
    w = wave.open(wav)
    raw = w.readframes(16000 * 20)
    asr = SherpaStreamingASR()
    await asr.start('s')
    for i in range(len(raw) // 640):
        await asr.push(raw[i * 640:(i + 1) * 640], i * 20)
    await asr.close()
    evs = [e async for e in asr.events()]
    assert evs and any(e.is_final for e in evs)
    assert all(e.audio_covered_until_ms < 20000 for e in evs)
