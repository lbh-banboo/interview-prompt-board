"""M03-4：滚动 1:1 验证、三态归属、短语音继承、overlap、降级（Fake 提取器，SIMULATED）。"""
import numpy as np
import pytest

from app.audio.voiceprint import FakeExtractor
from app.domain.speaker_roles import SpeakerRoleTracker
from test_enroll import SR, tone, tone_fn

TEMPLATE = np.array([1, 0, 0, 0.])


def make(**kw):
    ex = FakeExtractor(tone_fn)
    return SpeakerRoleTracker(TEMPLATE, ex, source='room', **kw), ex


def run(tr, pcm, t0=0, chunk=1600, **fkw):
    """按 100ms 块喂入一个说话段，返回 (滚动更新列表, 最终段)。"""
    tr.on_speech_start(t0, noise_rms=fkw.pop('noise_rms', None))
    ups = []
    for i in range(0, len(pcm), chunk * 2):
        ups += tr.feed(pcm[i:i + chunk * 2], **fkw)
    return ups, tr.on_speech_end(t0 + len(pcm) // 2 // (SR // 1000))


def test_answerer_decided_mid_speech_with_rolling_cadence():
    tr, ex = make()
    ups, seg = run(tr, tone(200, 3))
    assert [u['role'] for u in ups] == ['answerer'] and ups[0]['final'] is False
    assert ups[0]['t_end'] <= 900 and seg['latency_ms'] == 800   # 窗口满 0.8s 即定，不等说完
    assert seg['role'] == 'answerer' and seg['final'] and seg['confidence'] > 0.9 and not seg['inherited']
    # 800, 1300, 1800, 2300, 2800 ms 共 5 次滚动提取，窗口 <= 3s
    assert len(ex.calls) == 5 and max(ex.calls) <= 3 * SR and min(ex.calls) >= int(0.8 * SR)


def test_non_self_with_normal_energy_is_questioner():
    tr, _ = make()
    ups, seg = run(tr, tone(400, 2))
    assert seg['role'] == 'questioner' and seg['flags'] == [] and ups[0]['role'] == 'questioner'


def test_non_self_noise_background_is_unknown():
    tr, _ = make()
    _, seg = run(tr, tone(400, 2, amp=0.004, noise=0.0))
    assert seg['role'] == 'unknown' and seg['flags'] == ['noise_background']


def test_non_self_buried_in_measured_noise_is_unknown():
    tr, _ = make()
    _, seg = run(tr, tone(400, 2, amp=0.1), noise_rms=0.2)
    assert seg['role'] == 'unknown' and 'noise_background' in seg['flags']
    tr2, _ = make()
    assert run(tr2, tone(400, 2, amp=0.1), noise_rms=0.01)[1]['role'] == 'questioner'


def test_middle_zone_is_unknown():
    tr, _ = make(high=0.9, low=0.1)
    ups, seg = run(tr, tone(210, 2))   # cos ~ 0.95/0.997 -> 约 0.953 > 0.9；改用 400+210 混合
    assert seg['role'] == 'answerer'
    tr, _ = make(high=0.99, low=0.1)
    _, seg = run(tr, tone(210, 2))
    assert seg['role'] == 'unknown' and 0 < seg['confidence'] <= 0.5


def test_cough_in_middle_does_not_flip_role():
    tr, _ = make()
    pcm = tone(200, 1.5) + tone(600, 0.5, seed=5) + tone(200, 1.5, seed=6)
    ups, seg = run(tr, pcm)
    assert seg['role'] == 'unknown' or seg['role'] == 'answerer'
    # 均值 = 以 1.0/0 为主；咳嗽窗口使均值下降但不低于 low，不得翻成提问方
    assert seg['role'] != 'questioner'


def test_overlap_from_caller_not_attributed():
    tr, _ = make()
    ups, seg = run(tr, tone(200, 2), overlap=True)
    assert seg['role'] == 'unknown' and 'overlap' in seg['flags'] and seg['flags'].count('overlap') == 1


def test_overlap_detected_when_both_high_and_low_in_one_segment():
    tr, _ = make()
    ups, seg = run(tr, tone(200, 1.2) + tone(400, 3))   # 先像本人，后窗口(3s)整体像他人
    assert 'overlap' in seg['flags'] and seg['role'] == 'unknown'


def test_overlap_on_short_segment():
    tr, _ = make()
    _, seg = run(tr, tone(200, 0.4), overlap=True)
    assert seg['role'] == 'unknown' and 'overlap' in seg['flags'] and 'short' in seg['flags']


def test_short_speech_no_context_is_unknown_and_never_extracts():
    tr, ex = make()
    _, seg = run(tr, tone(400, 0.5, amp=0.1))
    assert seg['role'] == 'unknown' and not seg['inherited'] and 'short' in seg['flags'] and ex.calls == []


def test_short_question_after_answer_not_forced_questioner():
    tr, _ = make()
    run(tr, tone(200, 2), t0=0)                      # 回答方说完
    _, seg = run(tr, tone(400, 0.5, amp=0.1), t0=2500)   # 之后一句“为什么？”
    assert seg['role'] == 'unknown'   # M03-T6：继承或 unknown，不强判提问方


def test_short_inherits_answerer_while_answering():
    tr, _ = make()
    run(tr, tone(200, 2), t0=0)
    tr.in_answer = True
    _, seg = run(tr, tone(200, 0.4, amp=0.1), t0=2300)
    assert seg['role'] == 'answerer' and seg['inherited'] and seg['confidence'] == 0.4


def test_short_inherits_questioner_when_not_in_answer():
    tr, _ = make()
    run(tr, tone(400, 2), t0=0)
    _, seg = run(tr, tone(400, 0.4, amp=0.1), t0=2300)
    assert seg['role'] == 'questioner' and seg['inherited']


def test_short_context_expires_and_needs_energy():
    tr, _ = make()
    run(tr, tone(400, 2), t0=0)
    assert run(tr, tone(400, 0.4, amp=0.1), t0=2000 + 4000)[1]['role'] == 'unknown'
    assert run(tr, tone(400, 0.4, amp=0.001, noise=0), t0=2000)[1]['role'] == 'unknown'


def test_backchannel_candidate_short_and_quiet():
    tr, _ = make()
    _, seg = run(tr, tone(200, 0.4, amp=0.01, noise=0))
    assert 'backchannel_candidate' in seg['flags']
    _, loud = run(tr, tone(200, 0.4, amp=0.2), t0=10000)
    assert 'backchannel_candidate' not in loud['flags']
    _, long_ = run(tr, tone(200, 2, amp=0.01, noise=0), t0=20000)
    assert 'backchannel_candidate' not in long_['flags']


def test_unknown_role_does_not_overwrite_last_role():
    tr, _ = make()
    run(tr, tone(400, 2), t0=0)
    run(tr, tone(600, 0.4), t0=100000)   # 短且无上下文 -> unknown
    assert tr.last_role == 'questioner'


def test_feed_ignored_when_not_active():
    tr, _ = make()
    assert tr.feed(tone(200, 1)) == [] and tr.on_speech_end(0) is None


def test_role_change_emits_new_update():
    tr, _ = make(max_window_ms=1000)
    ups, seg = run(tr, tone(200, 1.2) + tone(400, 3), chunk=1600)
    roles = [u['role'] for u in ups]
    assert roles[0] == 'answerer' and len(roles) >= 2 and roles[0] != roles[-1]


# ---------- 降级 ----------
def test_degraded_model_missing_energy_and_context():
    tr = SpeakerRoleTracker(None, None)
    assert tr.degraded and tr.degraded_reason == 'model_unavailable'
    ups, seg = run(tr, tone(200, 2))
    assert ups == [] and seg['role'] == 'questioner' and seg['inherited'] and seg['confidence'] == 0.3
    assert seg['flags'] == ['degraded']
    tr.in_answer = True
    assert run(tr, tone(200, 2), t0=5000)[1]['role'] == 'answerer'
    assert run(tr, tone(200, 2, amp=0.001, noise=0), t0=9000)[1]['role'] == 'unknown'
    assert run(tr, tone(200, 2), t0=12000, overlap=True)[1]['role'] == 'unknown'


def test_degraded_when_no_template():
    tr = SpeakerRoleTracker(None, FakeExtractor(tone_fn))
    assert tr.degraded_reason == 'no_template'
    assert run(tr, tone(200, 2))[1]['flags'][0] == 'degraded'


def test_runtime_extract_failure_degrades():
    def boom(x):
        raise RuntimeError('x')
    tr = SpeakerRoleTracker(TEMPLATE, FakeExtractor(boom))
    assert not tr.degraded
    ups, seg = run(tr, tone(200, 2))
    assert tr.degraded and tr.degraded_reason == 'extract_failed' and 'degraded' in seg['flags']


def test_segment_schema():
    tr, _ = make()
    _, seg = run(tr, tone(200, 2), t0=1000)
    assert set(seg) == {'source', 'role', 'confidence', 't_start', 't_end', 'inherited', 'flags', 'final', 'latency_ms'}
    assert seg['t_start'] == 1000 and seg['t_end'] == 3000 and seg['source'] == 'room'


def test_offline_role_for_unchanged():
    from app.domain.roles import role_for
    assert role_for('room', 'offline') == 'unknown'
