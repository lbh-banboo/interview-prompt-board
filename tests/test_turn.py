"""M05-2/3 测试：语义规则 + 两阶段结束判定。全部脚本化事件，不读墙钟，不依赖 app.asr。"""
from dataclasses import dataclass

import pytest

from app.domain.semantic import classify, completeness, materially_changed
from app.domain.turn_detector import TurnDetector


@dataclass
class Tr:  # 鸭子类型的 TranscriptEvent
    text: str
    is_final: bool = False
    text_version: int = 1
    audio_covered_until_ms: int = 0
    t_audio_end_ms: int = 0
    t_audio_start_ms: int = 0


@dataclass
class V:  # 鸭子类型的 VadEvent
    kind: str
    t_ms: int


HIGH = '你能讲讲分布式事务的区别吗'
LOW = '怎么办'  # question 类型但过短，语义完整度低（statement 不成轮，不能再当 low 样本）
INCOMPLETE = '你先介绍一下项目然后'


def types(events):
    return [e.type for e in events]


def run_to(d, t, step=20):
    out = []
    while d.now < t:
        out += d.tick(min(d.now + step, t))
    return out


def speak(d, start, end, text, covered=None, version=1, final=False, key=None):
    """提问方 start..end 说话；partial 在 end 到达，覆盖到 covered（默认 end）。"""
    d.on_vad(V('speech_start', start))
    d.on_vad(V('speech_end', end))
    d.on_transcript(Tr(text, final, version, end if covered is None else covered,
                       t_audio_start_ms=start if key is None else key), now_ms=end)


def of(d, type_):
    return [e for e in d.history if e.type == type_]


# ---------------- 语义规则 ----------------
@pytest.mark.parametrize('text,kind', [
    ('嗯', 'backchannel'), ('好的好的', 'backchannel'), ('对对对', 'backchannel'), ('OK', 'backchannel'), ('', 'backchannel'),
    ('能听到吗', 'process'), ('我们开始吧', 'process'), ('那我们现在开始吧', 'process'), ('声音正常吗', 'process'),
    ('你好，请介绍一下你自己', 'request'), ('说说你的优缺点', 'request'), ('讲讲你的项目', 'request'),
    ('你能不能介绍一下缓存方案', 'request'), ('请你描述分布式事务的核心步骤', 'request'),
    ('你们为什么选择Kafka', 'question'), ('这个方案是否可行', 'question'), ('你这个缓存是用Redis对吧', 'question'),
    ('为什么？', 'question'), ('你是怎么做的', 'question'), ('这样对不对', 'question'),
    ('我们当时用的是Kafka', 'statement'), ('我先介绍一下我们团队', 'statement'), ('你的职业规划', 'statement'),
    ('我当时负责为什么要换方案的调研', 'statement'),
])
def test_classify(text, kind):
    assert classify(text) == kind


@pytest.mark.parametrize('text,level', [
    (HIGH, 'high'), ('请你介绍一下你的项目经历', 'high'), ('你们为什么选择Kafka', 'high'), ('为什么？', 'high'),
    ('你觉得这样好不好', 'high'), ('你这个缓存是用Redis对吧', 'high'),
    (LOW, 'low'), ('我们的项目主要做电商', 'low'), ('怎么办', 'low'), ('能听到吗', 'low'), ('嗯', 'low'),
    ('', 'incomplete'), ('然后', 'incomplete'), ('你先介绍一下项目然后', 'incomplete'), ('还有一个就是', 'incomplete'),
    ('比如说', 'incomplete'), ('就是说', 'incomplete'), ('你能不能', 'incomplete'), ('介绍一下', 'incomplete'),
    ('嗯，那你说说', 'incomplete'), ('你的项目经历，', 'incomplete'), ('你们用到了……', 'incomplete'),
    ('我们这个项目的', 'incomplete'), ('如果并发很高的话', 'incomplete'), ('你是如何', 'incomplete'),
])
def test_completeness_levels(text, level):
    assert completeness(text)[0] == level


def test_completeness_reasons_are_informative():
    assert 'tail:然后' in completeness('你先介绍一下项目然后')[1]
    assert 'question_mark' in completeness('这个方案可行吗？')[1]
    assert 'particle:吗' in completeness(HIGH)[1]
    assert 'tag_question' in completeness('缓存是Redis对吧')[1]
    assert completeness('怎么办')[1][-1] == 'too_short'
    assert 'request' in completeness('讲讲分布式事务')[1]


@pytest.mark.parametrize('old,new,changed,why', [
    ('你这个缓存是用Redis对吧', '你这个缓存不是用Redis对吧', True, 'negation_changed'),
    ('请介绍一下缓存方案', '请介绍一下缓存方案，另外说下性能', True, 'tail_extended'),
    ('缓存用瑞迪斯吗', '缓存用Redis吗？消息队列用什么？', True, 'new_subquestion'),
    ('请介绍一下缓存方案', '请介绍一下消息队列设计', True, 'tail_changed'),
    ('你好吗', '你好吗？', False, 'same'),
    ('介绍一下缓村方案', '介绍一下缓存方案', False, 'minor'),
])
def test_materially_changed(old, new, changed, why):
    assert materially_changed(old, new) == (changed, why)


# ---------------- 两阶段判定 ----------------
def test_high_completeness_candidate_and_commit_same_instant():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1400)
    assert types(d.history) == ['candidate', 'committed']
    c, m = d.history
    assert (c.t_ms, m.t_ms) == (1200, 1200)
    assert m.t_ms - m.t_speech_end_ms == 200
    assert m.reason == 'high_completeness' and m.completeness == 'high' and m.kind == 'question'
    assert m.turn_text == HIGH and m.text_version == 1
    assert d.phase == 'question_committed' and d.committed_count == 1


def test_low_completeness_commits_only_at_T_long():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1600)
    assert types(d.history) == ['candidate']
    assert d.history[0].t_ms == 1200 and d.phase == 'question_candidate'
    run_to(d, 2000)
    assert types(d.history) == ['candidate', 'committed']
    assert d.history[1].t_ms == 1700 and d.history[1].reason == 'long_silence'


def test_uncovered_partial_cannot_produce_high_commit():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH, covered=700)  # 覆盖 < 900
    run_to(d, 1600)
    assert types(d.history) == ['waiting_continue']
    assert d.history[0].reason == 'enter:coverage_insufficient' and d.waiting_continue
    run_to(d, 1700)
    assert types(d.history) == ['waiting_continue', 'waiting_continue', 'candidate', 'committed']
    m = of(d, 'committed')[0]
    assert m.t_ms == 1700 and m.completeness == 'low' and 'coverage_insufficient' in m.reason
    assert d.waiting_returns['long_silence_complete'] == 1


def test_coverage_arriving_late_unlocks_high_commit():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH, covered=700)
    run_to(d, 1300)
    assert of(d, 'committed') == []
    d.on_transcript(Tr(HIGH, False, 2, 1000), now_ms=1300)
    m = of(d, 'committed')[0]
    assert m.t_ms == 1300 and m.completeness == 'high' and m.text_version == 2
    assert d.waiting_returns['complete'] == 1 and not d.waiting_continue


def test_coverage_boundary_is_end_minus_100():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH, covered=900)
    run_to(d, 1200)
    assert types(d.history) == ['candidate', 'committed']
    d2 = TurnDetector()
    speak(d2, 0, 1000, HIGH, covered=899)
    run_to(d2, 1200)
    assert of(d2, 'committed') == []


def test_no_text_means_no_events():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 500))
    run_to(d, 5000)
    assert d.history == []


def test_T_silence_and_T_long_are_constructor_params():
    d = TurnDetector(T_silence=300, T_long=1000)
    speak(d, 0, 1000, LOW)
    run_to(d, 1299)
    assert d.history == []
    run_to(d, 1300)
    assert types(d.history) == ['candidate']
    run_to(d, 1999)
    assert len(d.history) == 1
    run_to(d, 2000)
    assert types(d.history) == ['candidate', 'committed']


# ---- M05-T3 ----
def test_T3_pause_600ms_candidate_withdrawn_not_premature():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1600)
    d.on_vad(V('speech_start', 1600))
    assert types(d.history) == ['candidate', 'withdrawn']
    assert d.history[1].t_ms == 1600 and d.history[1].reason == 'speech_resumed'
    assert d.candidate_withdrawn_count == 1 and d.premature_count == 0 and d.phase == 'listening_question'
    run_to(d, 2500)
    assert of(d, 'committed') == []  # 说话期间不判定
    d.on_vad(V('speech_end', 2400))
    d.on_transcript(Tr(LOW + '负责什么？', False, 2, 2400), now_ms=2400)
    run_to(d, 2700)
    assert types(d.history)[-2:] == ['candidate', 'committed']
    assert d.premature_count == 0


def test_T3_pause_600ms_after_commit_rolls_back_and_counts_premature():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1600)
    d.on_vad(V('speech_start', 1600))
    assert types(d.history) == ['candidate', 'committed', 'rolled_back']
    rb = d.history[2]
    assert rb.reason == 'speech_resumed_after_commit' and rb.text_version > d.history[1].text_version
    assert d.premature_count == 1 and d.candidate_withdrawn_count == 0
    assert d.history[1].type == 'committed'  # 原 commit 保留
    d.on_vad(V('speech_end', 2300))
    d.on_transcript(Tr(HIGH + '另外缓存呢？', False, 3, 2300), now_ms=2300)
    run_to(d, 2500)
    m = of(d, 'committed')
    assert len(m) == 2 and m[1].text_version > rb.text_version and m[1].turn_text.endswith('缓存呢？')
    assert d.premature_count == 1 and d.committed_count == 2


# ---- M05-T9 ----
@pytest.mark.parametrize('lead', [0, 100, 200])
@pytest.mark.parametrize('answerer_first', [True, False])
def test_T9_answerer_opens_before_commit_goes_straight_to_answering(lead, answerer_first):
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    t_open = 1200 - lead
    run_to(d, t_open - 1 if t_open > 1000 else 1000)
    if answerer_first:
        d.on_answerer_active(True, t_open)
        run_to(d, 1400)
    else:
        run_to(d, t_open)
        d.on_answerer_active(True, t_open)
        run_to(d, 1400)
    assert types(d.history) == ['candidate', 'committed']
    m = d.history[1]
    assert m.t_ms <= 1200 and d.phase == 'answering'
    assert m.reason in ('answerer_active', 'high_completeness')
    if lead:
        assert m.reason == 'answerer_active' and m.t_ms == t_open
    assert d.committed_count == 1  # 不需要第二次开口


def test_T9_answerer_opens_after_commit_before_render():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1220)
    assert d.phase == 'question_committed'
    assert d.on_answerer_active(True, 1220) == []
    assert d.phase == 'answering' and d.committed_count == 1


def test_T9_answerer_opens_on_low_candidate_commits_immediately():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    evs = d.on_answerer_active(True, 1300)
    assert types(evs) == ['committed'] and evs[0].reason == 'answerer_active' and d.phase == 'answering'


def test_answerer_opening_before_first_transcript_commits_when_text_arrives():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    assert d.on_answerer_active(True, 1050) == []
    evs = d.on_transcript(Tr(HIGH, False, 1, 1000), now_ms=1100)
    assert types(evs) == ['candidate', 'committed'] and d.phase == 'answering'


def test_answerer_overlap_while_questioner_speaking_does_not_commit():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_transcript(Tr(HIGH, False, 1, 500), now_ms=500)
    assert d.on_answerer_active(True, 600) == []
    assert d.phase == 'listening_question'
    assert d.on_answerer_active(True, 610) == []  # 重复
    assert d.on_answerer_active(False, 700) == []
    assert d.on_answerer_active(True, 800) == []  # 提问方仍在说话
    d.on_vad(V('speech_end', 900))  # 答方此刻仍 active，但按"变为 active"语义不补提交
    assert d.phase == 'listening_question'


def test_answerer_opens_on_process_turn_ignores_it():
    d = TurnDetector()
    speak(d, 0, 1000, '能听到吗', covered=500)
    d.on_answerer_active(True, 1050)
    assert d.history == [] and d.phase == 'idle' and d.ignored[0][0] == 'process'


# ---- M05-T10 waiting_continue ----
def test_waiting_path1_questioner_resumes():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1500)
    assert types(d.history) == ['waiting_continue']
    w = d.history[0]
    assert w.reason.startswith('enter:incomplete') and w.resume_state == 'listening_question'
    assert d.waiting_continue and of(d, 'candidate') == []
    d.on_vad(V('speech_start', 1500))
    assert not d.waiting_continue and d.waiting_returns['resumed'] == 1
    assert d.history[-1].reason == 'exit:resumed' and d.history[-1].resume_state == 'listening_question'
    assert d.candidate_withdrawn_count == 0 and d.premature_count == 0 and d.phase == 'listening_question'


def test_waiting_path2_long_silence_and_complete_commits():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)  # candidate 已发
    d.on_transcript(Tr(LOW + '，然后', False, 2, 1000), now_ms=1300)  # partial 修订后变成未完成
    w = of(d, 'waiting_continue')[0]
    assert w.reason.startswith('enter:incomplete') and w.resume_state == 'question_candidate'
    run_to(d, 1900)
    assert of(d, 'committed') == [] and d.waiting_continue  # 仍未完成，T_long 也不提交
    d.on_transcript(Tr(LOW + '，然后负责了缓存模块', True, 3, 1000), now_ms=1900)
    assert types(d.history)[-2:] == ['waiting_continue', 'committed']
    assert d.history[-2].reason == 'exit:long_silence_complete'
    assert d.history[-1].t_ms == 1900 and d.waiting_returns['long_silence_complete'] == 1


def test_waiting_path2_from_listening_with_early_resolution():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1300)
    d.on_transcript(Tr('你先介绍一下项目然后说说难点', True, 2, 1000), now_ms=1300)
    assert types(d.history) == ['waiting_continue', 'waiting_continue', 'candidate', 'committed']
    assert d.history[1].reason == 'exit:complete'


def test_waiting_path2_candidate_waiting_clears_then_waits_for_T_long():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    d.on_transcript(Tr(LOW + '，然后', False, 2, 1000), now_ms=1300)
    d.on_transcript(Tr(LOW + '，然后在', False, 3, 1000), now_ms=1350)  # 仍在 waiting，幂等
    assert len(of(d, 'waiting_continue')) == 1
    d.on_transcript(Tr(LOW + '，具体负责缓存', False, 4, 1000), now_ms=1400)  # 变完整且 high（低完整度只剩 statement，不成轮）
    assert d.history[-2].reason == 'exit:complete'
    assert of(d, 'committed')[0].t_ms == 1400


def test_waiting_path3_answerer_opens():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1400)
    evs = d.on_answerer_active(True, 1450)
    assert types(evs) == ['waiting_continue', 'candidate', 'committed']
    assert evs[0].reason == 'exit:answerer_active'
    assert d.phase == 'answering' and d.waiting_returns['answerer_active'] == 1 and not d.waiting_continue


def test_waiting_in_candidate_then_answerer_opens():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    d.on_transcript(Tr(LOW + '，然后', False, 2, 1000), now_ms=1300)
    evs = d.on_answerer_active(True, 1350)
    assert types(evs) == ['waiting_continue', 'committed'] and d.waiting_returns['answerer_active'] == 1


def test_waiting_in_candidate_then_speech_resumes_withdraws_and_exits():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    d.on_transcript(Tr(LOW + '，然后', False, 2, 1000), now_ms=1300)
    d.on_vad(V('speech_start', 1400))
    assert types(d.history)[-2:] == ['withdrawn', 'waiting_continue']
    assert d.candidate_withdrawn_count == 1 and d.waiting_returns['resumed'] == 1


def test_waiting_statistics_consistent_across_paths():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1300)
    d.on_vad(V('speech_start', 1300))
    d.on_vad(V('speech_end', 2000))
    d.on_transcript(Tr(INCOMPLETE, False, 2, 2000, t_audio_start_ms=1300), now_ms=2000)
    run_to(d, 2300)
    d.on_answerer_active(True, 2300)
    assert d.waiting_returns == {'resumed': 1, 'complete': 0, 'long_silence_complete': 0, 'answerer_active': 1, 'dropped': 0}
    enters = [e for e in d.history if e.type == 'waiting_continue' and e.reason.startswith('enter')]
    exits = [e for e in d.history if e.type == 'waiting_continue' and e.reason.startswith('exit')]
    assert len(enters) == len(exits) == 2


def test_duplicate_and_out_of_order_events_are_idempotent():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    snapshot = list(d.history)
    # 重复 / 旧版本 / 乱序 VAD
    assert d.on_vad(V('speech_end', 1000)) == []
    assert d.on_vad(V('speech_start', 500)) == []
    assert d.on_transcript(Tr(HIGH, False, 1, 1000), now_ms=1200) == []
    assert d.on_transcript(Tr('完全不同的旧文本', False, 0, 1000), now_ms=1200) == []
    d.tick(1100)  # 时间倒退
    run_to(d, 3000)
    assert d.history == snapshot and d.committed_count == 1 and d.premature_count == 0


def test_stale_partial_after_final_is_rejected_and_final_version_upgrade_accepted():
    d = TurnDetector()
    speak(d, 0, 1000, LOW, final=True, version=3)
    assert d.on_transcript(Tr('旧的', False, 4, 1000), now_ms=1000) == []  # final 之后的 partial
    assert d.on_transcript(Tr('旧的', True, 3, 1000), now_ms=1000) == []  # 同版本 final 重复
    run_to(d, 1200)
    assert of(d, 'candidate')[0].turn_text == LOW
    evs = d.on_transcript(Tr(LOW + '，对吧', True, 4, 1000), now_ms=1200)  # 更高版本 final 被接受，文本变高完整度 -> 提交
    assert types(evs) == ['committed'] and evs[0].text_version == 4


def test_partial_replaced_by_same_version_final():
    d = TurnDetector()
    speak(d, 0, 1000, LOW, version=2)
    d.on_transcript(Tr(LOW + '。', True, 2, 1000), now_ms=1000)
    run_to(d, 1200)
    assert of(d, 'candidate')[0].turn_text == LOW + '。'


def test_multi_sentence_turn_text_is_concatenated_in_order():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 500))
    d.on_transcript(Tr('你好，', True, 1, 500, t_audio_start_ms=0), now_ms=500)
    d.on_vad(V('speech_start', 700))
    d.on_vad(V('speech_end', 1500))
    d.on_transcript(Tr('请你介绍一下缓存方案', False, 1, 1500, t_audio_start_ms=700), now_ms=1500)
    run_to(d, 1700)
    assert of(d, 'committed')[0].turn_text == '你好，请你介绍一下缓存方案'


# ---- M05-T11 ----
def test_T11_negation_final_after_commit_rolls_back():
    d = TurnDetector()
    speak(d, 0, 1000, '你这个缓存是用Redis对吧')
    run_to(d, 1300)
    c = of(d, 'committed')[0]
    evs = d.on_transcript(Tr('你这个缓存不是用Redis对吧', True, 2, 1000), now_ms=1400)
    assert types(evs) == ['rolled_back']
    rb = evs[0]
    assert rb.reason == 'final_changed:negation_changed' and rb.text_version > c.text_version
    assert rb.turn_text == '你这个缓存不是用Redis对吧' and d.premature_count == 1
    assert of(d, 'committed') == [c]  # 原 commit 历史保留
    assert d.on_transcript(Tr('你这个缓存不是用Redis对吧', True, 2, 1000), now_ms=1450) == []  # 重复 final
    assert d.premature_count == 1
    evs = d.on_transcript(Tr('你这个缓存不是用Redis对吧', True, 5, 1000), now_ms=1500)  # 同义新版本不再回滚
    assert evs == [] and d.premature_count == 1


def test_final_that_only_adds_punctuation_does_not_roll_back():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1300)
    assert d.on_transcript(Tr(HIGH + '？', True, 2, 1000), now_ms=1400) == []
    assert d.premature_count == 0


def test_final_change_during_answering_still_rolls_back():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    evs = d.on_transcript(Tr('你能讲讲分布式事务的区别吗，另外它和两阶段提交有什么不同', True, 2, 1000), now_ms=1500)
    assert types(evs) == ['rolled_back'] and d.phase == 'answering' and d.premature_count == 1


def test_final_before_commit_just_updates_text():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    d.on_transcript(Tr(LOW + '，不是缓存', True, 2, 1000), now_ms=1300)
    assert d.premature_count == 0
    run_to(d, 1700)
    assert of(d, 'committed')[0].turn_text.endswith('不是缓存') and d.premature_count == 0


# ---- 忽略的话轮 / 生命周期 ----
@pytest.mark.parametrize('text,kind', [('嗯', 'backchannel'), ('好的', 'backchannel'), ('能听到吗', 'process')])
def test_backchannel_and_process_are_not_turns(text, kind):
    d = TurnDetector()
    speak(d, 0, 400, text)
    run_to(d, 1500)
    assert d.history == [] and d.ignored == [(kind, text, 600)] and d.phase == 'idle'
    # 之后新问题正常成轮，不被前面的文本污染
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 3000))
    d.on_transcript(Tr(HIGH, False, 1, 3000, t_audio_start_ms=2000), now_ms=3000)
    run_to(d, 3200)
    assert of(d, 'committed')[0].turn_text == HIGH


def test_backchannel_with_uncovered_text_waits_for_coverage_not_T_long_to_be_ignored():
    d = TurnDetector()
    speak(d, 0, 1000, '嗯', covered=400)
    run_to(d, 1700)
    assert types(d.history) == ['waiting_continue'] and d.phase == 'listening_question' and d.ignored == []
    d.on_transcript(Tr('嗯', True, 2, 1000), now_ms=1800)  # 覆盖追上后才忽略
    assert d.ignored and d.phase == 'idle' and not d.waiting_continue
    assert d.history[-1].reason == 'exit:dropped' and d.waiting_returns['dropped'] == 1


def test_close_turn_resets_and_drops_late_transcripts_of_old_turn():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    d.close_turn(floor_ms=3000)
    assert d.phase == 'idle'
    assert d.on_transcript(Tr('迟到的旧句子', True, 9, 1000, t_audio_start_ms=0), now_ms=3000) == []
    d.on_vad(V('speech_start', 3100))
    d.on_vad(V('speech_end', 4000))
    d.on_transcript(Tr(HIGH, False, 1, 4000, t_audio_start_ms=3100), now_ms=4000)
    run_to(d, 4200)
    assert len(of(d, 'committed')) == 2 and of(d, 'committed')[1].turn_text == HIGH
    assert of(d, 'committed')[1].text_version == 1  # 新一轮版本从头计


def test_stale_sentence_key_does_not_drop_new_question_after_answer():
    """本地 sherpa 的句子键在上一题 final 后不会立刻推进；回答阶段的新题若 key 偏早但音频结束
    落在本次插话区间内，必须仍按新题处理（否则第二问会被当成旧文本丢弃）。"""
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    d.on_answerer_active(False, 3000)
    run_to(d, 3500)
    d.on_vad(V('speech_start', 3500))
    d.on_vad(V('speech_end', 4500))
    d.on_transcript(Tr('那你们的缓存策略是什么', True, 3, 4500, t_audio_start_ms=1000, t_audio_end_ms=4500), now_ms=4500)
    run_to(d, 4800)
    committed = of(d, 'committed')
    assert len(committed) == 2 and committed[1].turn_text == '那你们的缓存策略是什么'


def test_questioner_speech_during_answering_is_left_to_later_modules():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    n = len(d.history)
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 2500))
    run_to(d, 4000)
    assert len(d.history) == n and d.premature_count == 0 and d.phase == 'answering'


def test_transcript_before_speech_start_is_kept_for_the_turn():
    d = TurnDetector()
    d.on_transcript(Tr(HIGH, False, 1, 500), now_ms=450)  # 先于 VAD 事件到达
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 600))
    run_to(d, 800)
    assert of(d, 'committed')[0].turn_text == HIGH


# ---- 确定性回放 ----
def scripted_session():
    """覆盖全部事件类型的一段长脚本：(时刻, 操作, 参数)，按时间顺序。"""
    return [
        (0, 'vad', V('speech_start', 0)), (900, 'vad', V('speech_end', 900)),
        (900, 'tr', Tr(INCOMPLETE, False, 1, 900)),
        (1500, 'vad', V('speech_start', 1500)), (2300, 'vad', V('speech_end', 2300)),
        (2300, 'tr', Tr('你先介绍一下项目然后说说缓存方案', False, 2, 2300)),
        (2600, 'vad', V('speech_start', 2600)),
        (3400, 'vad', V('speech_end', 3400)), (3400, 'tr', Tr(LOW, False, 3, 3400)),
        (3700, 'vad', V('speech_start', 3700)),
        (4400, 'vad', V('speech_end', 4400)), (4400, 'tr', Tr('这个缓存是用Redis对吧', False, 4, 4400)),
        (4700, 'tr', Tr('这个缓存不是用Redis对吧', True, 5, 4400)),
        (4800, 'vad', V('speech_start', 4800)), (4800, 'tr', Tr('这个缓存不是用Redis对吧，那', False, 6, 4800)),
        (5400, 'vad', V('speech_end', 5400)), (5400, 'tr', Tr('这个缓存不是用Redis对吧，那消息队列呢', False, 7, 5400)),
        (5900, 'ans', True), (6000, 'tr', Tr('这个缓存不是用Redis对吧，那消息队列呢？', True, 8, 5400)),
    ]


def replay(script, **kw):
    d = TurnDetector(**kw)
    for t, op, arg in script:
        run_to(d, t)
        if op == 'vad':
            d.on_vad(arg)
        elif op == 'tr':
            d.on_transcript(arg, now_ms=t)
        else:
            d.on_answerer_active(arg, t)
    run_to(d, 8000)
    return d


def test_deterministic_replay_gives_identical_results():
    a, b = replay(scripted_session()), replay(scripted_session())
    assert a.history == b.history and len(a.history) > 8
    for attr in ('premature_count', 'candidate_withdrawn_count', 'committed_count', 'waiting_returns', 'ignored', 'phase'):
        assert getattr(a, attr) == getattr(b, attr)
    seen = {e.type for e in a.history}
    assert seen == {'candidate', 'withdrawn', 'committed', 'rolled_back', 'waiting_continue'}


def test_replay_with_other_parameters_is_also_deterministic_and_differs():
    base = replay(scripted_session())
    slow = replay(scripted_session(), T_silence=300)
    assert slow.history == replay(scripted_session(), T_silence=300).history
    assert slow.history != base.history


def test_close_turn_while_waiting_ends_waiting():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1300)
    assert d.waiting_continue
    d.close_turn(floor_ms=2000)
    assert not d.waiting_continue and d.history[-1].reason == 'exit:dropped'


def test_transcript_without_explicit_now_uses_latest_known_time():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000), now_ms=1000)
    run_to(d, 1100)
    evs = d.on_transcript(Tr(HIGH, False, 1, 1000))  # now 缺省 = 已知最新时刻 1100，静音 100 < T_silence
    assert evs == []
    assert types(d.tick(1200)) == ['candidate', 'committed']


# ---------------- 评审修复（先红后绿） ----------------
def _new_question(d, start, text=HIGH, end=None):
    end = start + 1000 if end is None else end
    d.on_vad(V('speech_start', start))
    d.on_vad(V('speech_end', end))
    d.on_transcript(Tr(text, False, 1, end, t_audio_start_ms=start), now_ms=end)
    run_to(d, end + 300)


def test_f1_late_final_of_ignored_backchannel_does_not_pollute_next_turn():
    d = TurnDetector()
    speak(d, 0, 300, '嗯', covered=300)
    run_to(d, 500)
    assert d.ignored
    d.on_transcript(Tr('嗯。', True, 2, 300), now_ms=600)
    _new_question(d, 3000, '请你介绍一下你做过的项目')
    assert of(d, 'candidate')[0].turn_text == '请你介绍一下你做过的项目'
    assert of(d, 'committed')[0].turn_text == '请你介绍一下你做过的项目'


def test_f2_stale_partial_not_covered_is_not_ignored_by_long_silence():
    d = TurnDetector()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1500))
    d.on_transcript(Tr('好的', False, 1, 400), now_ms=1500)
    run_to(d, 2200)
    assert d.ignored == [] and d.phase == 'listening_question'
    d.on_transcript(Tr('好的，请你介绍一下你做过的项目', True, 2, 1500), now_ms=2300)
    assert of(d, 'committed')[0].turn_text == '好的，请你介绍一下你做过的项目'


def test_f2_covered_backchannel_then_extended_final_restarts_turn_in_idle():
    d = TurnDetector()
    speak(d, 0, 1500, '好的', covered=1500)
    run_to(d, 1800)
    assert d.ignored and d.phase == 'idle'
    d.on_transcript(Tr('好的，请你介绍一下你做过的项目', True, 2, 1500), now_ms=1900)
    assert of(d, 'committed')[0].turn_text == '好的，请你介绍一下你做过的项目'


def test_f2_idle_transcript_outside_last_speech_span_is_not_restarted():
    d = TurnDetector()
    speak(d, 0, 300, '嗯', covered=300)
    run_to(d, 500)
    d.on_transcript(Tr(HIGH, True, 1, 5000, t_audio_start_ms=5000), now_ms=5000)
    assert d.phase == 'idle' and d.history == []


def test_f3_unrelated_later_final_after_commit_does_not_roll_back():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1300)
    assert d.committed_count == 1
    d.on_transcript(Tr('嗯，好的', True, 1, 5100, t_audio_start_ms=5000), now_ms=5100)
    assert of(d, 'rolled_back') == [] and d.premature_count == 0
    d.on_answerer_active(True, 5200)
    d.on_transcript(Tr('嗯，好的。', True, 2, 5100, t_audio_start_ms=5000), now_ms=5300)
    assert of(d, 'rolled_back') == [] and d.premature_count == 0


def test_f3_same_turn_sentence_final_still_rolls_back():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1300)
    d.on_transcript(Tr('你能讲讲分布式事务的区别吗，不是缓存', True, 2, 1000), now_ms=1400)
    assert of(d, 'rolled_back')[0].reason == 'final_changed:negation_changed' and d.premature_count == 1


def test_f4_question_ending_with_zhege_is_not_incomplete():
    assert completeness('你为什么选择用这个')[0] == 'high'
    assert completeness('你能讲讲这个')[0] == 'high'
    assert completeness('我们这个项目的主要')[0] == 'incomplete'


def test_f4_incomplete_text_hard_timeout_commits_low():
    d = TurnDetector()
    assert d.T_hard == 1500
    speak(d, 0, 1000, INCOMPLETE, final=True)
    run_to(d, 2400)
    assert of(d, 'committed') == [] and d.waiting_continue and d.hard_timeout_commits == 0
    run_to(d, 2600)
    assert types(d.history)[-2:] == ['candidate', 'committed']
    m = of(d, 'committed')[0]
    assert 'hard_timeout' in m.reason and m.completeness == 'low' and m.t_ms == 2500
    assert d.hard_timeout_commits == 1 and d.committed_count == 1 and d.phase == 'question_committed'
    assert not d.waiting_continue and d.waiting_returns['long_silence_complete'] == 1


def test_f4_hard_timeout_in_candidate_phase_and_param():
    d = TurnDetector(T_hard=900)
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    assert d.phase == 'question_candidate'
    d.on_transcript(Tr(LOW + '，然后', True, 2, 1000), now_ms=1300)
    run_to(d, 1800)
    assert of(d, 'committed') == [] and d.waiting_continue
    run_to(d, 1950)
    assert 'hard_timeout' in of(d, 'committed')[0].reason and d.hard_timeout_commits == 1


@pytest.mark.parametrize('text', ['自我介绍一下', '请自我介绍', '请做个自我介绍', '介绍一下你自己', '好的，请你先做一个自我介绍'])
def test_f5_self_introduction_request_is_request_high(text):
    assert classify(text) == 'request'
    assert completeness(text)[0] == 'high'


def test_f5_candidate_self_intro_statement_still_statement():
    assert classify('我介绍一下我的项目') == 'statement'
    assert classify('我先做个自我介绍') == 'statement'


def test_f5_self_intro_candidate_and_committed_same_instant():
    d = TurnDetector()
    speak(d, 0, 1000, '请做个自我介绍')
    run_to(d, 1300)
    assert types(d.history) == ['candidate', 'committed'] and d.history[0].t_ms == d.history[1].t_ms == 1200


def test_f6_close_turn_while_questioner_speaking_keeps_listening():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    d.on_vad(V('speech_start', 6000))
    d.close_turn(5900)
    assert d.phase == 'listening_question'
    d.on_vad(V('speech_end', 7000))
    d.on_transcript(Tr(HIGH, False, 1, 7000, t_audio_start_ms=6000), now_ms=7000)
    run_to(d, 7300)
    assert len(of(d, 'committed')) == 2


def test_f7_statement_is_not_a_turn():
    d = TurnDetector()
    speak(d, 0, 1000, '我们公司主要做电商。', final=True)
    run_to(d, 1600)
    assert types(d.history) == ['candidate'] and d.ignored == [] and d.phase == 'question_candidate'  # 留给回答方的开口窗口
    run_to(d, 1800)
    assert types(d.history) == ['candidate', 'withdrawn'] and of(d, 'committed') == []
    assert d.ignored == [('statement', '我们公司主要做电商。', 1700)] and d.phase == 'idle'


def test_f7_statement_committed_if_answerer_opens_within_window():
    d = TurnDetector()
    speak(d, 0, 1000, '你的职业规划', final=True)
    run_to(d, 1300)
    d.on_answerer_active(True, 1300)
    assert of(d, 'committed') and d.ignored == []


def test_f7_statement_uncovered_waits_for_coverage():
    d = TurnDetector()
    speak(d, 0, 1000, '我们公司主要做电商。', covered=400)
    run_to(d, 2000)
    assert d.ignored == [] and d.waiting_continue
    d.on_transcript(Tr('我们公司主要做电商。', True, 2, 1000), now_ms=2100)
    assert d.ignored and d.phase == 'idle'


def test_f7_candidate_revised_to_statement_is_ignored():
    d = TurnDetector()
    speak(d, 0, 1000, LOW)
    run_to(d, 1300)
    assert d.phase == 'question_candidate'
    d.on_transcript(Tr('我们公司主要做电商。', True, 2, 1000), now_ms=1300)
    assert d.ignored == [] and d.phase == 'question_candidate'
    run_to(d, 1800)
    assert d.ignored[0][0] == 'statement' and d.phase == 'idle'
    assert d.history[-1].type == 'withdrawn' and d.history[-1].reason == 'reclassified:statement'
    assert d.candidate_withdrawn_count == 1


def test_f8_reset_restarts_timeline_and_keeps_history():
    d = TurnDetector()
    speak(d, 5000, 6000, HIGH)
    run_to(d, 6300)
    d.on_vad(V('speech_start', 100))
    assert d.dropped_events == 1
    d.reset()
    assert d.phase == 'idle' and d.now == 0
    _new_question(d, 100)
    assert len(of(d, 'committed')) == 2 and d.dropped_events == 1


def test_f8_reset_ends_waiting():
    d = TurnDetector()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1500)
    assert d.waiting_continue
    d.reset()
    assert not d.waiting_continue and d.waiting_returns['dropped'] == 1


# ---------------- trigger='vad'（G6）----------------
def vd(**kw):
    return TurnDetector(trigger='vad', **kw)


def test_vad_default_trigger_is_coverage_and_rejects_unknown():
    assert TurnDetector().trigger == 'coverage'
    with pytest.raises(ValueError):
        TurnDetector(trigger='asr')


def test_vad_primary_commit_with_stale_coverage_at_T_silence():
    d = vd()
    speak(d, 0, 1000, HIGH, covered=700)  # coverage 模式此时只会 waiting
    run_to(d, 1200)
    assert types(d.history) == ['candidate', 'committed']
    c, m = d.history
    assert (c.t_ms, m.t_ms) == (1200, 1200) and c.reason == m.reason == 'vad_primary'
    assert c.coverage_stale is True and m.coverage_stale is True
    assert m.t_speech_end_ms == 1000 and m.turn_text == HIGH
    assert (d.vad_primary_commits, d.stale_commits, d.committed_count) == (1, 1, 1)


def test_vad_primary_not_stale_when_covered():
    d = vd()
    speak(d, 0, 1000, HIGH, covered=900)
    run_to(d, 1200)
    assert d.history[1].coverage_stale is False and d.history[1].reason == 'vad_primary'
    assert (d.vad_primary_commits, d.stale_commits) == (1, 0)


def test_vad_uses_latest_partial_text_without_covering_the_tail():
    d = vd()
    d.on_vad(V('speech_start', 0))
    d.on_transcript(Tr('你能讲讲分布式', False, 1, 600), now_ms=600)
    d.on_transcript(Tr('你能讲讲分布式事务', False, 2, 800), now_ms=800)
    d.on_vad(V('speech_end', 1000))
    run_to(d, 1200)
    assert of(d, 'committed')[0].turn_text == '你能讲讲分布式事务' and of(d, 'committed')[0].text_version == 2


def test_vad_low_completeness_question_also_commits():
    d = vd()
    speak(d, 0, 1000, LOW)
    run_to(d, 1200)
    m = of(d, 'committed')[0]
    assert m.reason == 'vad_primary' and m.completeness == 'low'


def test_vad_no_text_only_candidate_then_commit_on_arrival():
    d = vd()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    run_to(d, 1100)
    assert d.history == []
    run_to(d, 1300)
    assert types(d.history) == ['candidate']
    c = d.history[0]
    assert c.reason == 'no_text' and c.t_ms == 1200 and c.turn_text == '' and c.coverage_stale is True
    assert of(d, 'committed') == [] and d.phase == 'question_candidate'
    d.on_transcript(Tr(HIGH, False, 1, 1000), now_ms=1500)
    m = of(d, 'committed')[0]
    assert m.t_ms == 1500 and m.t_speech_end_ms == 1000 and m.reason == 'vad_primary' and m.coverage_stale is False
    assert types(d.history) == ['candidate', 'committed'] and d.vad_primary_commits == 1


def test_vad_no_text_stays_waiting_after_T_long_and_late_text_commits():
    d = vd()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    run_to(d, 3000)  # 越过 T_long 与 T_hard：空文本绝不提交
    assert types(d.history) == ['candidate', 'waiting_continue']
    assert d.history[1].reason == 'enter:no_text' and d.waiting_continue and of(d, 'committed') == []
    d.on_transcript(Tr('', False, 1, 1000), now_ms=3100)  # 空转写仍无文本
    assert of(d, 'committed') == []
    d.on_transcript(Tr(HIGH, False, 2, 1000), now_ms=3200)
    m = of(d, 'committed')[0]
    assert m.t_ms == 3200 and m.t_ms - m.t_speech_end_ms == 2200
    assert d.waiting_returns['long_silence_complete'] == 1 and not d.waiting_continue


def test_vad_no_text_candidate_late_statement_waits_for_coverage_then_dropped():
    d = vd()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    run_to(d, 1650)
    d.on_transcript(Tr('我们当时用的是Kafka', False, 1, 700), now_ms=1800)  # 未覆盖：不能据此丢弃
    assert types(d.history) == ['candidate', 'waiting_continue'] and d.history[1].reason == 'enter:coverage_insufficient'
    d.on_transcript(Tr('我们当时用的是Kafka', False, 2, 1000), now_ms=1900)
    assert of(d, 'withdrawn')[0].reason == 'reclassified:statement' and of(d, 'committed') == []
    assert d.ignored[-1][0] == 'statement' and d.phase == 'idle' and not d.waiting_continue


def test_vad_statement_candidate_rewritten_as_question_commits():
    d = vd()
    speak(d, 0, 1000, '我们当时用的是Kafka')
    run_to(d, 1300)
    assert types(d.history) == ['candidate'] and d.history[0].reason == 'silence>=T_silence'
    d.on_transcript(Tr(HIGH, False, 2, 1000), now_ms=1300)
    m = of(d, 'committed')[0]
    assert m.reason == 'vad_primary' and m.coverage_stale is False and types(d.history) == ['candidate', 'committed']


def test_vad_incomplete_keeps_waiting_and_hard_timeout():
    d = vd()
    speak(d, 0, 1000, INCOMPLETE)
    run_to(d, 1400)
    assert types(d.history) == ['waiting_continue'] and d.history[0].reason.startswith('enter:incomplete')
    run_to(d, 2500)
    m = of(d, 'committed')[0]
    assert m.reason == 'hard_timeout' and m.t_ms == 2500 and d.vad_primary_commits == 0 and d.hard_timeout_commits == 1


def test_vad_statement_and_backchannel_follow_coverage_rules():
    d = vd()
    speak(d, 0, 1000, '嗯好的', covered=1000)
    run_to(d, 1200)
    assert d.history == [] and d.ignored[0][0] == 'backchannel'
    d2 = vd()
    speak(d2, 0, 1000, '嗯好的', covered=500)  # 未覆盖：等待覆盖，不丢
    run_to(d2, 1200)
    assert types(d2.history) == ['waiting_continue'] and d2.ignored == []
    d3 = vd()
    speak(d3, 0, 1000, '我们当时用的是Kafka')
    run_to(d3, 1750)
    assert of(d3, 'committed') == [] and d3.ignored[0][0] == 'statement' and d3.vad_primary_commits == 0


def test_vad_speech_resumed_after_no_text_candidate_withdraws():
    d = vd()
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    run_to(d, 1300)
    d.on_vad(V('speech_start', 1300))
    assert types(d.history) == ['candidate', 'withdrawn'] and d.candidate_withdrawn_count == 1 and d.premature_count == 0


def test_vad_speech_resumed_after_commit_rolls_back_and_counts_premature():
    d = vd()
    speak(d, 0, 1000, HIGH, covered=700)
    run_to(d, 1300)
    d.on_vad(V('speech_start', 1300))
    assert types(d.history) == ['candidate', 'committed', 'rolled_back']
    assert d.history[2].reason == 'speech_resumed_after_commit' and d.premature_count == 1


def test_vad_late_partial_tail_extension_rolls_back():
    d = vd()
    speak(d, 0, 1000, '你能讲讲分布式', covered=700)
    run_to(d, 1200)
    d.on_transcript(Tr('你能讲讲分布式事务的原理', False, 2, 1000, 1500), now_ms=1500)
    rb = of(d, 'rolled_back')[0]
    assert rb.reason == 'final_changed:tail_extended' and rb.text_version == 2 and rb.t_ms == 1500
    assert d.premature_count == 1 and of(d, 'text_updated') == []


def test_vad_late_final_negation_change_rolls_back():
    d = vd()
    speak(d, 0, 1000, '你这个缓存是用Redis对吧')
    run_to(d, 1200)
    d.on_transcript(Tr('你这个缓存不是用Redis对吧', True, 2, 1000, 1500), now_ms=1400)
    assert of(d, 'rolled_back')[0].reason == 'final_changed:negation_changed' and d.premature_count == 1


def test_vad_late_homophone_fix_is_refine_text_updated_not_rollback():
    d = vd()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_transcript(Tr('你能讲讲分布式事物的区别吗', True, 2, 1000, 1500), now_ms=1500)
    u = of(d, 'text_updated')[0]
    assert u.reason == 'refine' and u.text_version == 2 and u.turn_text == '你能讲讲分布式事物的区别吗'
    assert u.t_ms == 1500 and u.kind == 'question'
    assert of(d, 'rolled_back') == [] and d.premature_count == 0 and d.committed_count == 1


def test_vad_refine_is_idempotent_and_ignores_stale_versions():
    d = vd()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    fixed = '你能讲讲分布式事物的区别吗'
    d.on_transcript(Tr(fixed, False, 2, 1000, 1500), now_ms=1300)
    n = len(d.history)
    d.on_transcript(Tr(fixed, False, 2, 1000, 1500), now_ms=1310)  # 重复
    d.on_transcript(Tr(HIGH, False, 1, 1000, 1500), now_ms=1320)  # 乱序旧版本
    d.on_transcript(Tr(fixed, False, 3, 1000, 1500), now_ms=1330)  # 新版本同文本：不再重复发
    d.on_transcript(Tr(fixed + '。', True, 4, 1000, 1500), now_ms=1340)  # 仅标点
    assert len(d.history) == n and len(of(d, 'text_updated')) == 1 and d.premature_count == 0


def test_vad_text_updated_versions_increase_across_refines():
    d = vd()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_transcript(Tr('你能讲讲分布式事物的区别吗', False, 2, 1000, 1500), now_ms=1300)
    d.on_transcript(Tr('你能讲讲分布式实务的区别吗', False, 3, 1000, 1500), now_ms=1400)
    vers = [e.text_version for e in of(d, 'text_updated')]
    assert vers == [2, 3] and d.premature_count == 0


def test_vad_minor_but_not_conservative_refine_rolls_back():
    d = vd()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    assert materially_changed(HIGH, '你能讲讲分布式微服务事务的区别吗')[1] == 'minor'  # 编辑距离 3 > 2
    d.on_transcript(Tr('你能讲讲分布式微服务事务的区别吗', False, 2, 1000, 1500), now_ms=1300)
    assert of(d, 'rolled_back')[0].reason == 'final_changed:not_refine' and of(d, 'text_updated') == []
    assert d.premature_count == 1


def test_coverage_mode_unchanged_partials_after_commit_are_ignored():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_transcript(Tr('你能讲讲分布式事物的区别吗', False, 2, 1000), now_ms=1300)
    d.on_transcript(Tr('你能讲讲分布式事务的区别吗，另外缓存呢', False, 3, 1000), now_ms=1400)  # partial：coverage 模式不看
    assert types(d.history) == ['candidate', 'committed'] and d.vad_primary_commits == 0
    d.on_transcript(Tr('你能讲讲分布式事物的区别吗', True, 4, 1000), now_ms=1500)
    assert of(d, 'text_updated') == [] and d.premature_count == 0  # final 的 minor 在 coverage 模式不发事件


def test_vad_min_words_for_vad_commit_falls_back_to_coverage_rules():
    d = vd(min_words_for_vad_commit=30)
    speak(d, 0, 1000, HIGH, covered=700)
    run_to(d, 1300)
    assert d.vad_primary_commits == 0 and types(d.history) == ['waiting_continue']
    d.on_transcript(Tr(HIGH, False, 2, 1000), now_ms=1300)
    assert of(d, 'committed')[0].reason == 'high_completeness'
    d2 = vd(min_words_for_vad_commit=5)
    speak(d2, 0, 1000, HIGH, covered=700)
    run_to(d2, 1200)
    assert of(d2, 'committed')[0].reason == 'vad_primary'  # 超过字数门槛则正常


def test_vad_min_words_applies_to_late_text_in_candidate():
    d = vd(min_words_for_vad_commit=30)
    d.on_vad(V('speech_start', 0))
    d.on_vad(V('speech_end', 1000))
    run_to(d, 1300)
    d.on_transcript(Tr(HIGH, False, 1, 1000), now_ms=1300)
    assert of(d, 'committed')[0].reason == 'high_completeness'


def test_vad_replay_is_deterministic():
    def play():
        d = vd()
        speak(d, 0, 1000, '你能讲讲分布式', covered=700)
        run_to(d, 1200)
        d.on_transcript(Tr('你能讲讲分布式事务的区别吗', False, 2, 1000), now_ms=1500)
        d.on_vad(V('speech_start', 1600))
        d.on_vad(V('speech_end', 2400))
        d.on_transcript(Tr('你能讲讲分布式事务的区别吗，另外缓存呢？', True, 3, 2400), now_ms=2450)
        run_to(d, 3000)
        return d.history, (d.vad_primary_commits, d.stale_commits, d.premature_count)
    assert play() == play()


# ---------------- vad 模式：过早口径拆分 + ASR 追赶 ----------------
def commit_stale(d, stale_text='你能讲讲分布式', covered=700):
    speak(d, 0, 1000, stale_text, covered=covered)
    run_to(d, 1200)
    assert of(d, 'committed')


def counters(d):
    return (d.premature_count, d.resume_rollbacks, d.semantic_rollbacks, d.late_rollbacks, d.catchup_updates)


def test_vad_catchup_same_semantics_emits_text_updated_not_premature():
    d = vd()
    commit_stale(d)
    d.on_transcript(Tr('你能讲讲分布式事务', False, 2, 1000, 1000), now_ms=1500)
    u = of(d, 'text_updated')[0]
    assert u.reason == 'catchup' and u.turn_text == '你能讲讲分布式事务' and u.text_version == 2 and u.t_ms == 1500
    assert of(d, 'rolled_back') == [] and counters(d) == (0, 0, 0, 0, 1)
    d.on_transcript(Tr('你能讲讲分布式事务', True, 3, 1000, 1000), now_ms=1600)  # 同文本再到：不重复发
    d.on_transcript(Tr('你能讲讲分布式事务。', True, 4, 1000, 1000), now_ms=1650)  # 仅标点
    assert len(of(d, 'text_updated')) == 1 and d.catchup_updates == 1


def test_vad_catchup_boundary_is_t_end_plus_100():
    d = vd()
    commit_stale(d)
    d.on_transcript(Tr('你能讲讲分布式事务', False, 2, 1000, 1100), now_ms=1500)
    assert of(d, 'text_updated')[0].reason == 'catchup'
    d2 = vd()
    commit_stale(d2)
    d2.on_transcript(Tr('你能讲讲分布式事务的原理', False, 2, 1000, 1101), now_ms=1500)  # 描述了更晚的音频：不算追赶
    assert of(d2, 'rolled_back')[0].reason == 'final_changed:tail_extended' and counters(d2) == (1, 0, 0, 1, 0)


@pytest.mark.parametrize('old,new', [
    ('你这个缓存是用Redis对吧', '你这个缓存不是用Redis对吧'),  # 否定词翻转
    ('缓存用Redis吗', '缓存用Redis吗？消息队列用什么？'),  # 子问题数变化
    ('请介绍一下缓存方案', '我先介绍一下缓存方案'),  # request -> statement
])
def test_vad_catchup_semantic_change_rolls_back_and_counts_premature(old, new):
    d = vd()
    commit_stale(d, old, covered=700)
    d.on_transcript(Tr(new, False, 2, 1000, 1000), now_ms=1500)
    rb = of(d, 'rolled_back')[0]
    assert rb.reason == 'semantic_change' and rb.text_version == 2 and rb.turn_text == new
    assert counters(d) == (1, 0, 1, 0, 0) and of(d, 'text_updated') == []


def test_vad_catchup_completeness_change_is_semantic():
    d = vd()
    commit_stale(d, LOW, covered=700)  # '怎么办' low
    d.on_transcript(Tr(HIGH, False, 2, 1000, 1000), now_ms=1500)  # 完整度 low -> high
    assert of(d, 'rolled_back')[0].reason == 'semantic_change' and d.semantic_rollbacks == 1


def test_vad_resume_rollback_counted_separately_and_no_catchup_after_resume():
    d = vd()
    commit_stale(d)
    d.on_vad(V('speech_start', 1300))
    assert counters(d) == (1, 1, 0, 0, 0) and d.history[-1].reason == 'speech_resumed_after_commit'
    d.on_transcript(Tr('你能讲讲分布式事务', False, 2, 1000, 1000), now_ms=1400)  # 开口后 phase 已离开 committed
    assert of(d, 'text_updated') == [] and d.catchup_updates == 0


def test_vad_no_catchup_when_questioner_spoke_again_during_answering():
    d = vd()
    commit_stale(d)
    d.on_answerer_active(True, 1250)
    assert d.phase == 'answering'
    d.on_vad(V('speech_start', 1300))
    d.on_transcript(Tr('你能讲讲分布式事务的原理', False, 2, 1000, 1000), now_ms=1400)
    assert of(d, 'text_updated') == [] and counters(d) == (1, 0, 0, 1, 0)


def test_premature_is_sum_of_resume_semantic_and_late_rollbacks():
    d = vd()
    commit_stale(d)  # 第 1 题
    d.on_transcript(Tr('你能讲讲分布式事务', False, 2, 1000, 1000), now_ms=1300)  # catchup，不计
    d.on_transcript(Tr('你能讲讲分布式事务不对吗', False, 3, 1000, 1000), now_ms=1350)  # 否定词变化 -> semantic
    d.on_transcript(Tr('你能讲讲分布式事务不对吗的原理', False, 4, 1000, 1500), now_ms=1400)  # 更晚音频 -> late
    d.on_vad(V('speech_start', 1450))  # resume
    p, r, s_, l, c = counters(d)
    assert (r, s_, l, c) == (1, 1, 1, 1) and p == r + s_ + l == 3
    assert len(of(d, 'rolled_back')) == p


def test_coverage_mode_late_final_change_counts_as_late_rollback():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_transcript(Tr(HIGH + '另外缓存呢', True, 2, 1000, 1000), now_ms=1400)
    assert counters(d) == (1, 0, 0, 1, 0) and d.history[-1].reason == 'final_changed:tail_extended'
    d.on_vad(V('speech_start', 1500))
    assert counters(d) == (2, 1, 0, 1, 0)


# ---------------- P0-A：无人回答时的轮次滚动 ----------------
def test_no_answer_questions_roll_to_new_turns_without_accumulating_text():
    d = TurnDetector()
    q2, q3 = '请介绍一下缓存方案', '你们为什么选择Kafka'
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    speak(d, 2500, 3500, q2)      # 上一题静音 1500ms >= T_new_turn：新题
    run_to(d, 3700)
    speak(d, 5000, 6000, q3)
    run_to(d, 6200)
    commits = of(d, 'committed')
    assert [c.turn_text for c in commits] == [HIGH, q2, q3]  # 每题只含当题内容，不累积
    assert [c.reason for c in commits] == ['high_completeness'] * 3
    assert of(d, 'rolled_back') == [] and d.premature_count == 0
    assert d.no_answer_closes == 2 and d.closed_count == 2
    closed = of(d, 'answer_closed')
    assert [e.reason for e in closed] == ['no_answer_timeout', 'no_answer_timeout']
    assert [e.turn_text for e in closed] == [HIGH, q2]


def test_no_answer_turnover_respects_T_new_turn():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_vad(V('speech_start', 2000))  # 静音 1000ms < 1200：仍是同一题的续说
    assert types(d.history) == ['candidate', 'committed', 'rolled_back']
    assert d.no_answer_closes == 0 and d.premature_count == 1


def test_refine_after_no_answer_gap_keeps_same_turn():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    speak(d, 2500, 3500, HIGH + '？')  # 只差标点：精修，不换轮
    run_to(d, 3700)
    assert len(of(d, 'committed')) == 1 and of(d, 'answer_closed') == []
    upd = of(d, 'text_updated')
    assert len(upd) == 1 and upd[0].reason == 'refine_after_no_answer'
    assert d.no_answer_closes == 0 and d.phase == 'question_committed'


def test_hard_cap_forces_new_turn_before_T_new_turn():
    d = TurnDetector(max_turn_chars=5)
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_vad(V('speech_start', 1600))  # 静音仅 600ms，但单轮字数已达上限
    assert d._stale is d._commit
    d.on_vad(V('speech_end', 2600))
    d.on_transcript(Tr('请介绍一下缓存方案', False, 1, 2600, t_audio_start_ms=1600), now_ms=2600)
    run_to(d, 2800)
    assert [c.turn_text for c in of(d, 'committed')] == [HIGH, '请介绍一下缓存方案']
    assert [e.reason for e in of(d, 'answer_closed')] == ['no_answer_timeout']


def test_answerer_marked_turn_does_not_turn_over():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    d.on_answerer_active(True, 1300)
    d.on_answerer_active(False, 2400)
    run_to(d, 4000)
    assert d._commit['answered'] is True and d.no_answer_closes == 0


def test_backchannel_after_no_answer_gap_keeps_previous_turn():
    d = TurnDetector()
    speak(d, 0, 1000, HIGH)
    run_to(d, 1200)
    speak(d, 2500, 2900, '嗯')  # 附和：不成轮，也不关掉上一题
    run_to(d, 3100)
    assert len(of(d, 'committed')) == 1 and of(d, 'answer_closed') == []
    assert d.phase == 'question_committed' and d._commit['text'] == HIGH
