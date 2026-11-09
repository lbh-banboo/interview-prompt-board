import json
import math
import sqlite3
import struct
import sys
import wave
from pathlib import Path

import pytest

from app.audio.frame import parse
from app.telemetry.metrics import Metrics, clock_offset
from app.telemetry.replay import WavFormatError, replay_wav, send_to_ws
from app.telemetry.report import build_report, percentiles, wilson_ci

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import annotate_draft  # noqa: E402


def write_wav(path, samples, rate=16000, ch=1, width=2):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(samples if isinstance(samples, bytes) else struct.pack(f'<{len(samples)}h', *samples))


def tone_then_silence(speech_ms, silence_ms, rate=16000, amp=8000, lead_ms=0):
    def tone(ms):
        return [int(amp * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate * ms // 1000)]
    return [0] * (rate * lead_ms // 1000) + tone(speech_ms) + [0] * (rate * silence_ms // 1000)


class FakeTime:
    def __init__(self):
        self.t = 0.0
    def clock(self):
        return self.t
    def sleep(self, d):
        self.t += d


def row(sid='s1', **kw):
    base = dict(sample_id=sid, audio_path=f'eval/{sid}.wav', log_path=f'eval/{sid}.log', verified=True,
                t_end=1000, t_committed=1200, premature=0, missed=False, role_ok=True,
                role_unknown=False, overlap_truth=False)
    base.update(kw)
    return base


# ---- M14-1 ----
def test_metrics_mark_roundtrip(tmp_path):
    m = Metrics(tmp_path / 'm.db')
    m.mark('s', 't1', 'committed', 200, 'x')
    m.mark('s', 't1', 'end', 100)
    assert m.rows('s') == [('s', 't1', 'end', 100, None), ('s', 't1', 'committed', 200, 'x')]
    m2 = Metrics(sqlite3.connect(':memory:'))
    m2.mark('a', None, 'n', 1)
    assert len(m2.rows('a')) == 1


def test_clock_offset_picks_min_rtt():
    # 真实偏移 5e9 ns：backend = browser_ms*1e6 + 5e9
    samples = [(100, 100e6 + 5e9 + 30e6, 160), (200, 200e6 + 5e9 + 2e6, 204), (300, 300e6 + 5e9 + 20e6, 350)]
    r = clock_offset(samples)
    assert r['rtt_ns'] == 4e6 and r['error_ns'] == 2e6 and r['n'] == 3
    assert abs(r['offset_ns'] - 5e9) <= r['error_ns']
    with pytest.raises(ValueError):
        clock_offset([])


# ---- M14-2 ----
def test_replay_frames_parse_and_seq(tmp_path):
    p = tmp_path / 'a.wav'
    samples = tone_then_silence(500, 0)
    write_wav(p, samples)
    ft = FakeTime()
    frames = list(replay_wav(p, 'meeting', clock=ft.clock, sleep=ft.sleep))
    parsed = [parse(f) for f in frames]
    assert [x['seq'] for x in parsed] == list(range(25))
    assert parsed[3]['t_client_us'] == 3 * 20000 and parsed[0]['source'] == 'meeting' and parsed[0]['replay']
    assert b''.join(x['pcm'] for x in parsed) == struct.pack(f'<{len(samples)}h', *samples)


@pytest.mark.parametrize('chunk', [20, 100, 200])
def test_replay_duration_equals_audio(tmp_path, chunk):
    p = tmp_path / 'a.wav'
    write_wav(p, tone_then_silence(1000, 500))
    ft = FakeTime()
    n = sum(1 for _ in replay_wav(p, 'mic', chunk_ms=chunk, clock=ft.clock, sleep=ft.sleep))
    assert n == 75
    assert abs(ft.t - 1.5) <= 0.02


def test_replay_nonrealtime_does_not_sleep(tmp_path):
    p = tmp_path / 'a.wav'
    write_wav(p, tone_then_silence(200, 0))
    ft = FakeTime()
    assert len(list(replay_wav(p, 'mic', realtime=False, clock=ft.clock, sleep=ft.sleep))) == 10
    assert ft.t == 0


def test_replay_pads_last_partial_frame(tmp_path):
    p = tmp_path / 'a.wav'
    write_wav(p, [1] * 400)
    frames = list(replay_wav(p, 'mic', realtime=False))
    assert len(frames) == 2 and parse(frames[1])['pcm'][-2:] == b'\x00\x00'


@pytest.mark.parametrize('kw', [dict(rate=44100), dict(ch=2), dict(width=1)])
def test_replay_rejects_bad_format(tmp_path, kw):
    p = tmp_path / 'bad.wav'
    write_wav(p, bytes(2000), **kw)
    with pytest.raises(WavFormatError):
        replay_wav(p, 'mic')


def test_replay_bad_chunk(tmp_path):
    p = tmp_path / 'a.wav'
    write_wav(p, [0] * 320)
    with pytest.raises(ValueError):
        replay_wav(p, 'mic', chunk_ms=50)


@pytest.mark.asyncio
async def test_send_to_ws():
    import websockets
    got = []

    async def handler(ws):
        async for msg in ws:
            got.append(msg)
    async with websockets.serve(handler, '127.0.0.1', 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        n = await send_to_ws(f'ws://127.0.0.1:{port}', [b'a', b'b'])
        import asyncio
        await asyncio.sleep(0.1)
    assert n == 2 and got == [b'a', b'b']


def test_known_latency_synthetic_replay(tmp_path):
    """M14-T1：管线=连续 200ms 静音后提交 + 100ms 处理；已知延迟 300ms，误差 <=20ms。"""
    p = tmp_path / 'q.wav'
    write_wav(p, tone_then_silence(1000, 600))
    ft = FakeTime()
    silent, t_committed = 0, None
    for f in replay_wav(p, 'meeting', clock=ft.clock, sleep=ft.sleep):
        pcm = parse(f)['pcm']
        loud = max(abs(v) for v in struct.unpack('<320h', pcm)) > 500
        silent = 0 if loud else silent + 1
        if silent == 10 and t_committed is None:
            t_committed = (ft.clock() + 0.100) * 1000
    assert abs((t_committed - 1000) - 300) <= 20


# ---- M14-3/5 ----
def test_percentiles_edges():
    assert percentiles([5]) == dict(p50=5, p95=5, p99=5, max=5)
    r = percentiles([10, 20])
    assert r['p50'] == 15 and r['p95'] == pytest.approx(19.5) and r['p99'] == pytest.approx(19.9) and r['max'] == 20
    assert percentiles([])['p95'] is None
    r = percentiles(range(1, 101))
    assert r['p50'] == pytest.approx(50.5) and r['p95'] == pytest.approx(95.05)


def test_wilson_matches_hand_values():
    lo, hi = wilson_ci(1, 30)  # 手算: center=0.0333+0.0641=>0.0959/1.1281... 区间 [0.0059,0.1667]
    assert (round(lo, 4), round(hi, 4)) == (0.0059, 0.1667)
    lo, hi = wilson_ci(0, 10)
    assert lo == 0 and round(hi, 4) == 0.2775
    assert wilson_ci(0, 0) is None


def test_report_contents_and_failure_list(tmp_path):
    rows = [row('a'), row('b', t_committed=1400),
            row('c', missed=True, t_committed=None, audio_path='eval/c.wav'),
            row('d', premature=1)]
    csv_path = tmp_path / 'r.csv'
    text = build_report(rows, 'ALGO_REPLAY', csv_path=csv_path)
    assert text.splitlines()[0] == '类别: ALGO_REPLAY'
    for key in ('n=4', 'P50', 'P95', 'P99', 'max', '95% CI'):
        assert key in text
    assert 'c | 漏判 | audio=eval/c.wav | log=eval/c.log' in text
    assert 'd | 过早x1' in text
    assert '结论: FAIL' in text and '点估计达标≠稳定达标' in text
    assert csv_path.read_text(encoding='utf-8').count('\n') == 5


def test_real_kind_pass_and_threshold():
    rows = [row(str(i), t_committed=1100 + i) for i in range(40)]
    assert '结论: PASS' in build_report(rows, 'REAL_VISIBLE')
    assert '点估计达标' not in build_report(rows, 'REAL_VISIBLE')
    slow = [row(str(i), t_committed=1500) for i in range(40)]
    assert '结论: FAIL' in build_report(slow, 'REAL_BROWSER')
    assert '结论: NOT TESTED' in build_report([], 'REAL_BROWSER')


def test_simulated_never_pass():
    rows = [row(str(i)) for i in range(50)]
    text = build_report(rows, 'SIMULATED')
    assert text.splitlines()[0] == '类别: SIMULATED' and '结论: SIMULATED' in text
    assert 'PASS' not in text
    with pytest.raises(ValueError):
        build_report(rows, 'MOCK')


def test_report_deterministic(tmp_path):
    rows = [row(str(i), t_committed=1100 + i * 7, role_ok=i % 3 != 0) for i in range(20)]
    a = build_report(rows, 'ALGO_REPLAY', csv_path=tmp_path / 'a.csv')
    b = build_report(list(rows), 'ALGO_REPLAY', csv_path=tmp_path / 'b.csv')
    assert a == b and (tmp_path / 'a.csv').read_bytes() == (tmp_path / 'b.csv').read_bytes()


def test_denominator_unknown_and_overlap():
    rows = [row('ok1'), row('ok2'), row('unk', role_ok=False, role_unknown=True),
            row('bad', role_ok=False),
            row('ov', role_ok=False, overlap_truth=True)]
    text = build_report(rows, 'ALGO_REPLAY')
    assert '真值重叠区间（不进分母，单列）: 1' in text
    assert '角色分母: 4' in text
    assert '角色正确率（unknown 计错）: 2/4' in text
    assert 'unknown 数: 1，覆盖率（非 unknown 占比）: 3/4' in text
    assert 'ov | audio=' in text
    assert '- unk | unknown' in text and '- bad | 角色错误' in text
    assert 'ov | 角色错误' not in text


def test_unverified_excluded_and_counted():
    rows = [row('v1'), row('v2'), row('u1', verified=False), row('u2', verified=None), row('u3', t_committed=9999)]
    rows[4].pop('verified')
    text = build_report(rows, 'ALGO_REPLAY')
    assert 'n=2（未校对排除 3' in text


def test_mode_separation_and_missed_derivation():
    rows = [row('on', mode='online'), row('off', mode='offline'), row('late', t_committed=4500)]
    text = build_report(rows, 'ALGO_REPLAY', mode='online')
    assert 'n=2（未校对排除 0，其他 mode 排除 1）' in text
    assert '漏判数: 1' in text and 'late | 漏判' in text


# ---- G4a ----
def test_annotate_draft(tmp_path):
    p = tmp_path / 'call1.wav'
    write_wav(p, tone_then_silence(1000, 1000, lead_ms=500) + tone_then_silence(600, 500))
    out = annotate_draft.annotate(p, tmp_path / 'ann')
    assert out.name == 'call1.draft.json'
    d = json.loads(out.read_text(encoding='utf-8'))
    assert d['resampled'] is False and len(d['segments']) == 2
    s0 = d['segments'][0]
    assert abs(s0['start_ms'] - 500) <= 40 and abs(s0['end_ms'] - 1500) <= 40
    assert s0['role'] is None and s0['t_end'] is None and s0['is_topic_change'] is None and s0['verified'] is False


def test_annotate_resamples_and_notes(tmp_path):
    p = tmp_path / 'hi.wav'
    write_wav(p, tone_then_silence(1000, 800, rate=48000, lead_ms=400), rate=48000)
    d = json.loads(annotate_draft.annotate(p, tmp_path / 'ann').read_text(encoding='utf-8'))
    assert d['resampled'] is True and d['original_sample_rate'] == 48000 and d['notes']
    assert len(d['segments']) == 1 and abs(d['segments'][0]['start_ms'] - 400) <= 60


def test_annotate_rejects_8bit(tmp_path):
    p = tmp_path / 'x.wav'
    write_wav(p, bytes(1000), width=1)
    with pytest.raises(ValueError):
        annotate_draft.annotate(p, tmp_path)


# ---- 评审修复：report 口径 ----
def test_all_verified_but_never_committed_is_fail_not_untested():
    rows = [row(str(i), t_committed=None) for i in range(10)]
    text = build_report(rows, 'REAL_BROWSER')
    assert '结论: FAIL' in text and 'NOT TESTED' not in text


def test_small_sample_real_is_provisional_not_pass():
    text = build_report([row('only')], 'REAL_VISIBLE')
    assert '结论: PROVISIONAL' in text and '结论: PASS' not in text
    text29 = build_report([row(str(i)) for i in range(29)], 'REAL_BROWSER')
    assert '结论: PROVISIONAL' in text29
    text30 = build_report([row(str(i)) for i in range(30)], 'REAL_BROWSER')
    assert '结论: PASS' in text30


def test_small_sample_failure_is_still_fail():
    assert '结论: FAIL' in build_report([row('x', t_committed=2000)], 'REAL_VISIBLE')


def test_wilson_upper_bound_shown_and_flagged():
    text = build_report([row(str(i)) for i in range(40)], 'REAL_VISIBLE')
    assert '结论: PASS' in text and '上界未达标' in text.split('\n')[4]
    assert '过早率' in text and '95% CI' in text
    big = build_report([row(str(i)) for i in range(2000)], 'REAL_VISIBLE')
    assert '结论: PASS' in big and '上界未达标' not in big


@pytest.mark.parametrize('kind', ['REAL_VISIBLE', 'REAL_BROWSER', 'ALGO_REPLAY'])
def test_simulated_row_forces_simulated_report(kind):
    rows = [row(str(i)) for i in range(50)]
    rows[7]['simulated'] = True
    text = build_report(rows, kind)
    assert text.splitlines()[0] == '类别: SIMULATED' and '结论: SIMULATED' in text and 'PASS' not in text


def test_premature_count_and_rate_consistent():
    text = build_report([row('a', premature=3)], 'REAL_VISIBLE')
    assert '回滚总数 3 / 题数 1' in text and '有回滚的题 1' in text
    rows = [row(str(i), premature=(2 if i < 2 else 0)) for i in range(40)]
    text = build_report(rows, 'REAL_VISIBLE')
    assert '回滚总数 4 / 题数 40' in text and '有回滚的题 2' in text
    assert '2/40' in text  # CI 用有回滚的题数


# ---- 评审修复：replay 批次 / live ----
class SendTimes:
    def __init__(self):
        self.ft = FakeTime()

    def run(self, p, chunk, **kw):
        out = []
        for f in replay_wav(p, 'mic', chunk_ms=chunk, clock=self.ft.clock, sleep=self.ft.sleep, **kw):
            out.append((self.ft.clock() * 1000, parse(f)))
        return out


@pytest.mark.parametrize('chunk', [100, 200])
def test_replay_batch_sent_at_batch_end(tmp_path, chunk):
    p = tmp_path / 'b.wav'
    write_wav(p, [100] * 16000 * 1)  # 1s = 50 帧
    got = SendTimes().run(p, chunk)
    k = chunk // 20
    assert len(got) == 50
    for i, (t, _) in enumerate(got):
        batch_end = (i // k + 1) * k
        assert t == pytest.approx(batch_end * 20)


def test_replay_live_flag(tmp_path):
    p = tmp_path / 'l.wav'
    write_wav(p, [100] * 640)
    assert all(f['replay'] for _, f in SendTimes().run(p, 20))
    assert not any(f['replay'] for _, f in SendTimes().run(p, 20, live=True))


def test_replay_eval_tool_uses_live_frames():
    src = (Path(__file__).resolve().parent.parent / 'tools' / 'run_replay_eval.py').read_text(encoding='utf-8')
    assert 'live=True' in src and "frame['replay'] = False" not in src
