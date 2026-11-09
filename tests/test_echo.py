"""M02-5 回声相关性检测（合成信号）。"""
import numpy as np

from app.audio.echo import EchoDetector, echo_energy_ratio, max_norm_xcorr, rms, to_float
from app.domain.roles import echo_dominant

SR = 16000


def noise(seconds, seed, amp=0.3):
    return np.random.default_rng(seed).normal(0, amp, int(SR * seconds))


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype('<i2')


def delayed(x, ms):
    d = int(SR * ms / 1000)
    return np.concatenate((np.zeros(d), x))[:x.size]


def test_identical_signals_corr_one():
    x = noise(1, 1)
    r, lag = max_norm_xcorr(x, x, 3200)
    assert r > 0.99 and lag == 0


def test_uncorrelated_low():
    r, _ = max_norm_xcorr(noise(1, 1), noise(1, 2), 3200)
    assert r < 0.2


def test_delayed_echo_found_with_lag_sign():
    meeting = noise(1, 3)
    mic = delayed(meeting, 100)  # mic 晚于 meeting 100ms
    r, lag = max_norm_xcorr(mic, meeting, 3200)
    assert r > 0.85 and lag == 1600
    r2, lag2 = max_norm_xcorr(meeting, mic, 3200)
    assert r2 > 0.85 and lag2 == -1600


def test_delay_beyond_search_range_not_detected():
    meeting = noise(1, 4)
    r, _ = max_norm_xcorr(delayed(meeting, 400), meeting, 3200)
    assert r < 0.3


def test_silence_zero_and_pcm_inputs():
    z = np.zeros(SR)
    assert max_norm_xcorr(z, z, 100) == (0.0, 0)
    x = noise(0.5, 5)
    r, _ = max_norm_xcorr(pcm16(x).tobytes(), pcm16(x), 100)
    assert r > 0.99
    assert to_float(np.array([1.0])).dtype == np.float64


def test_detector_alerts_after_hold():
    det = EchoDetector(hold_s=3)
    meeting = noise(5, 6)
    mic = delayed(meeting, 50) * 0.5 + noise(5, 7, 0.02)
    events = []
    for i in range(5):
        s = slice(i * SR, (i + 1) * SR)
        events.append(det.feed(pcm16(mic[s]), pcm16(meeting[s])))
    assert [e and e['type'] for e in events] == [None, None, 'echo_suspected', None, None]
    assert events[2]['message'] == '可能未戴耳机，建议佩戴耳机' and events[2]['lag_ms'] == 50
    assert det.active


def test_detector_no_alert_for_uncorrelated_and_clears():
    det = EchoDetector(hold_s=2)
    for i in range(4):
        assert det.feed(pcm16(noise(1, 10 + i)), pcm16(noise(1, 20 + i))) is None
    assert not det.active
    meeting = noise(3, 8)
    ev = None
    for i in range(3):
        s = slice(i * SR, (i + 1) * SR)
        ev = det.feed(pcm16(meeting[s]), pcm16(meeting[s])) or ev
    assert ev['type'] == 'echo_suspected'
    assert det.feed(pcm16(noise(1, 30)), pcm16(noise(1, 31)))['type'] == 'echo_cleared'
    assert not det.active


def test_detector_silence_never_alerts_and_partial_chunks():
    det = EchoDetector(hold_s=1)
    assert det.feed(np.zeros(SR * 3, dtype='<i2'), np.zeros(SR * 3, dtype='<i2')) is None
    x = noise(1, 9)
    assert det.feed(pcm16(x[:8000]), pcm16(x[:8000])) is None  # 未攒满一个窗口
    assert det.feed(pcm16(x[8000:]), pcm16(x[8000:]))['type'] == 'echo_suspected'


def test_energy_ratio_feeds_echo_dominant():
    loud, quiet = pcm16(noise(1, 1, 0.4)), pcm16(noise(1, 2, 0.1))
    ratio = echo_energy_ratio(loud, quiet)
    assert ratio > 2.0
    assert echo_dominant(rms(loud), rms(quiet))
    assert echo_energy_ratio(loud, np.zeros(10)) == float('inf')
    assert echo_energy_ratio(np.zeros(10), np.zeros(10)) == 0.0
    assert rms(np.zeros(0)) == 0.0
