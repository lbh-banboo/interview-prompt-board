"""M05-1 测试：合成语音(带噪声的调制音)与静音拼接。EnergyVad 为必测；Silero 在模型/包可用时测。"""
import shutil
import subprocess
import wave

import numpy as np
import pytest

from app.audio.vad import EnergyVad, SileroVad, VadEvent, VadSegmenter, make_vad

SR = 16000


def tone(ms, seed=0, amp=0.15, noise=0.005):
    rng = np.random.default_rng(seed)
    t = np.arange(ms * 16) / SR
    voice = amp * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return voice + noise * rng.standard_normal(len(t))


def quiet(ms, noise=0.002, seed=1):
    return noise * np.random.default_rng(seed).standard_normal(ms * 16)


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype('<i2')


def run(vad, signal, chunk_ms=20, t0=0):
    """逐块喂入，返回 [(事件, 喂入时刻 t_ms)]"""
    pcm = pcm16(signal)
    n = chunk_ms * 16
    out = []
    for i in range(0, len(pcm) - n + 1, n):
        t = t0 + i // 16
        out += [(e, t + chunk_ms) for e in vad.feed(pcm[i:i + n].tobytes(), t)]
    return out


def kinds(evs):
    return [(e.kind, e.t_ms) for e, _ in evs]


def test_event_is_frozen_dataclass():
    with pytest.raises(Exception):
        VadEvent('speech_start', 1).t_ms = 2


def test_silence_only_yields_no_events():
    assert run(EnergyVad('meeting'), quiet(3000)) == []


def test_speech_boundaries_use_last_speech_time_not_detection_time():
    sig = np.concatenate([quiet(500), tone(1000), quiet(1000)])
    evs = run(EnergyVad('meeting'), sig)
    assert kinds(evs) == [('speech_start', 500), ('speech_end', 1500)]
    end_ev, fed_at = evs[1]
    assert fed_at - end_ev.t_ms >= 100  # 检出晚于最后语音至少 hangover，但事件时刻不含静音等待
    assert evs[0][1] <= 500 + 60 + 20  # 起点确认延迟 = min_speech_ms


def test_click_shorter_than_min_speech_is_ignored():
    sig = np.concatenate([quiet(500), tone(40), quiet(1000)])
    assert run(EnergyVad('meeting'), sig) == []


def test_short_gap_bridged_by_hangover_but_long_gap_splits():
    v = EnergyVad('meeting')
    evs = run(v, np.concatenate([quiet(300), tone(500), quiet(60), tone(500), quiet(1000)]))
    assert [k for k, _ in kinds(evs)] == ['speech_start', 'speech_end']
    v = EnergyVad('meeting')
    evs = run(v, np.concatenate([quiet(300), tone(500), quiet(300), tone(500), quiet(1000)]))
    assert kinds(evs) == [('speech_start', 300), ('speech_end', 800), ('speech_start', 1100), ('speech_end', 1600)]


def test_adaptive_noise_floor_handles_noisy_background():
    # 背景噪声 0.01 rms 高于绝对下限 0.004，但低于 3 倍噪声底阈值，不应误报；语音出现后仍能检出
    v = EnergyVad('meeting')
    sig = np.concatenate([quiet(2000, noise=0.01), tone(800, amp=0.2), quiet(1000, noise=0.01)])
    evs = run(v, sig)
    assert [k for k, _ in kinds(evs)] == ['speech_start', 'speech_end']
    assert abs(evs[0][0].t_ms - 2000) <= 40 and abs(evs[1][0].t_ms - 2800) <= 20


def test_noise_floor_rises_at_most_2_percent_per_window_and_falls_instantly():
    v = EnergyVad('meeting', abs_floor=0.004)
    run(v, quiet(200, noise=0.003))
    high = v.noise_floor
    run(v, quiet(20, noise=0.0005, seed=3), t0=200)
    assert v.noise_floor < high
    assert v.noise_floor <= 0.002  # 下限 abs_floor/2


def test_pre_roll_holds_audio_before_onset():
    pre = quiet(1000, seed=7)
    sig = np.concatenate([pre, tone(800), quiet(500)])
    v = EnergyVad('meeting', pre_roll_ms=300)
    evs = run(v, sig)
    assert evs[0][0].t_ms == 1000
    assert len(v.pre_roll) == 300 * 16
    expect = pcm16(pre)[-300 * 16:].astype(np.float32) / 32768
    assert np.allclose(v.pre_roll, expect)


def test_pre_roll_shorter_when_onset_is_near_stream_start():
    v = EnergyVad('meeting', pre_roll_ms=300)
    run(v, np.concatenate([quiet(100), tone(500), quiet(500)]))
    assert 0 < len(v.pre_roll) <= 100 * 16


@pytest.mark.parametrize('chunk_ms', [10, 20, 40, 60])
def test_results_do_not_depend_on_chunk_size(chunk_ms):
    sig = np.concatenate([quiet(500), tone(1000), quiet(1000)])
    evs = run(EnergyVad('meeting'), sig, chunk_ms=chunk_ms)
    assert kinds(evs) == [('speech_start', 500), ('speech_end', 1500)]


def test_duplicate_and_overlapping_chunks_are_idempotent():
    v = EnergyVad('meeting')
    sig = pcm16(np.concatenate([quiet(500), tone(1000), quiet(1000)]))
    out = []
    for i in range(0, len(sig) - 319, 320):
        t = i // 16
        chunk = sig[i:i + 320].tobytes()
        out += v.feed(chunk, t)
        out += v.feed(chunk, t)  # 重复
        if 640 <= i <= len(sig) - 960:
            out += v.feed(sig[i - 320:i + 640].tobytes(), t - 20)  # 与已处理部分重叠，尾部带新样本
    assert [(e.kind, e.t_ms) for e in out] == [('speech_start', 500), ('speech_end', 1500)]


def test_gap_in_stream_closes_open_speech_at_last_speech_time():
    v = EnergyVad('meeting')
    evs = run(v, np.concatenate([quiet(300), tone(400)]))
    assert kinds(evs) == [('speech_start', 300)]
    evs2 = run(v, quiet(200), t0=2000)  # 丢了 1.3s
    assert kinds(evs2) == [('speech_end', 700)]
    assert not v.speaking
    assert run(v, quiet(200), t0=2200) == []


def test_gap_while_idle_just_realigns():
    v = EnergyVad('meeting')
    run(v, quiet(200))
    assert kinds(run(v, np.concatenate([tone(400), quiet(500)]), t0=1000)) == [('speech_start', 1000), ('speech_end', 1400)]


def test_flush_ends_open_speech_and_is_noop_when_idle():
    v = EnergyVad('meeting')
    assert v.flush() == []
    run(v, np.concatenate([quiet(300), tone(400)]))
    assert v.speaking
    assert v.flush() == [VadEvent('speech_end', 700)]
    assert v.flush() == []


def test_reset_clears_state():
    v = EnergyVad('meeting')
    run(v, np.concatenate([quiet(300), tone(400)]))
    v.reset()
    assert not v.speaking and v.noise_floor == v.abs_floor
    assert kinds(run(v, np.concatenate([tone(300), quiet(500)]))) == [('speech_start', 0), ('speech_end', 300)]


def test_deterministic_same_input_same_events():
    sig = np.concatenate([quiet(500), tone(700, seed=3), quiet(300), tone(900, seed=4), quiet(800)])
    assert kinds(run(EnergyVad('meeting'), sig)) == kinds(run(EnergyVad('meeting'), sig))


def test_base_class_requires_subclass_decision():
    with pytest.raises(NotImplementedError):
        VadSegmenter('mic').feed(pcm16(quiet(20)).tobytes(), 0)


class ScriptedVad(VadSegmenter):
    """窗口 512(32ms) 的假分类器：按预设序列判决，用来验证 20ms 帧 -> 32ms 窗口的聚合与时间回填。"""
    window_samples = 512

    def __init__(self, source, script, **kw):
        self.script = list(script)
        self.calls = 0
        super().__init__(source, **kw)

    def _is_speech(self, window):
        assert len(window) == 512
        v = self.script[self.calls] if self.calls < len(self.script) else False
        self.calls += 1
        return v


def test_20ms_frames_are_aggregated_into_512_sample_windows():
    # 窗口 k 覆盖 [32k, 32k+32)ms；语音窗口 10..29 => 起点 320ms，最后语音结束 960ms
    script = [False] * 10 + [True] * 20 + [False] * 10
    v = ScriptedVad('meeting', script, hangover_ms=96, min_speech_ms=64)
    evs = run(v, quiet(1280))
    assert kinds(evs) == [('speech_start', 320), ('speech_end', 960)]
    assert v.calls == 40  # 1280ms / 32ms
    assert evs[1][1] >= 960 + 96  # 在 hangover 之后才检出，但 t_ms 仍是 960


def test_scripted_single_window_blip_is_dropped_by_min_speech():
    v = ScriptedVad('meeting', [False, True, False, False, False, False], hangover_ms=96, min_speech_ms=64)
    assert run(v, quiet(192)) == []


# ---------------- Silero ----------------
silero_available = True
try:
    SileroVad('meeting')
except (ImportError, FileNotFoundError):
    silero_available = False
needs_silero = pytest.mark.skipif(not silero_available, reason='sherpa-onnx 或 models/silero_vad_16k_op15.onnx 不可用')


@needs_silero
def test_silero_silence_yields_no_events_and_window_is_512():
    v = SileroVad('meeting')
    assert v.window_samples == 512
    assert run(v, quiet(2000)) == []


@needs_silero
def test_silero_is_the_default_of_make_vad_and_is_deterministic():
    assert isinstance(make_vad('meeting'), SileroVad)
    sig = quiet(1500)
    assert kinds(run(make_vad('meeting'), sig)) == kinds(run(make_vad('meeting'), sig))


@needs_silero
@pytest.mark.skipif(shutil.which('say') is None or shutil.which('afconvert') is None, reason='需要 macOS say/afconvert 生成真实语音')
def test_silero_detects_tts_speech_with_correct_end_time(tmp_path):
    aiff, wav = tmp_path / 't.aiff', tmp_path / 't.wav'
    subprocess.run(['say', '-o', str(aiff), '你好，请你介绍一下你做过的项目。'], check=True)
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', str(aiff), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        speech = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float32) / 32768
    sig = np.concatenate([np.zeros(8000, np.float32), speech, np.zeros(16000, np.float32)])
    evs = run(SileroVad('meeting'), sig)
    ks = [k for k, _ in kinds(evs)]
    assert ks[0] == 'speech_start' and ks[-1] == 'speech_end'
    total_ms = len(sig) // 16
    last_end = evs[-1][0].t_ms
    assert 500 < last_end <= total_ms - 1000 + 50  # 末尾 1s 静音(含 hangover)不计入 speech_end 时刻


def test_make_vad_falls_back_to_energy_when_model_missing(tmp_path):
    v = make_vad('meeting', model_path=tmp_path / 'nope.onnx', hangover_ms=150)
    assert isinstance(v, EnergyVad) and v.hangover_ms == 150


def test_make_vad_falls_back_to_energy_when_sherpa_missing(monkeypatch):
    monkeypatch.setitem(__import__('sys').modules, 'sherpa_onnx', None)
    assert isinstance(make_vad('meeting'), EnergyVad)


def test_make_vad_energy_preferred():
    assert isinstance(make_vad('meeting', prefer='energy', pre_roll_ms=100), EnergyVad)


# ---------------- 评审修复（先红后绿） ----------------
def test_f8_timeline_restart_without_reset_is_counted_not_silent():
    v = EnergyVad('meeting')
    sig = np.concatenate([quiet(200), tone(600), quiet(400)])
    run(v, sig)
    assert v.dropped_chunks == 0
    assert run(v, sig) == [] and v.dropped_chunks == len(sig) // 320


def test_f8_reset_accepts_restarted_timeline_and_keeps_counter():
    v = EnergyVad('meeting')
    sig = np.concatenate([quiet(200), tone(600), quiet(400)])
    run(v, sig)
    run(v, sig)
    dropped = v.dropped_chunks
    v.reset()
    evs = run(v, sig)
    assert [e.kind for e, _ in evs] == ['speech_start', 'speech_end'] and v.dropped_chunks == dropped


def test_f9_digital_silence_prefix_does_not_pin_noise_floor_at_zero():
    noise = quiet(1500, noise=0.005, seed=4)
    ref = EnergyVad('meeting')
    assert run(ref, noise) == []
    v = EnergyVad('meeting')
    run(v, np.zeros(16 * 200))  # 数字静音前缀（含开头 5 个全零帧）
    assert v.noise_floor > 0
    assert run(v, noise, t0=200) == []
    evs = run(v, np.concatenate([tone(800), quiet(500)]), t0=1700)
    assert [e.kind for e, _ in evs] == ['speech_start', 'speech_end']
