"""M05-5/6 测试：打断分类、子问题拆分、待答队列、Q2 回答结束。全部脚本化事件，不读墙钟。"""
from dataclasses import dataclass

import pytest

from app.domain.interrupt import classify_interrupt, has_stop_phrase, similarity
from app.domain.subquestions import split_questions
from app.domain.turn_detector import TurnDetector

Q1 = 'Redis持久化有哪几种方式'


@dataclass
class Tr:
    text: str
    is_final: bool = False
    text_version: int = 1
    audio_covered_until_ms: int = 0
    t_audio_end_ms: int = 0
    t_audio_start_ms: int = 0


@dataclass
class V:
    kind: str
    t_ms: int


def run_to(d, t, step=20):
    out = []
    while d.now < t:
        out += d.tick(min(d.now + step, t))
    return out


def ask(d, start, end, text, covered=None, version=1, final=False):
    """提问方 start..end 说话，partial 在 end 到达。"""
    d.on_vad(V('speech_start', start))
    d.on_vad(V('speech_end', end))
    d.on_transcript(Tr(text, final, version, end if covered is None else covered, end, start), now_ms=end)


def types(d, since=0):
    return [e.type for e in d.history[since:]]


def answering(**kw):
    """Q1 提问 0~1000，回答方 1250 开口，进入 answering。"""
    d = TurnDetector(**kw)
    ask(d, 0, 1000, Q1)
    run_to(d, 1200)
    d.on_answerer_active(True, 1250)
    assert d.phase == 'answering' and types(d) == ['candidate', 'committed']
    return d


def of(d, type_):
    return [e for e in d.history if e.type == type_]


# ---------------- 子问题拆分 M05-T6 ----------------
@pytest.mark.parametrize('text,subs', [
    ('Redis是什么？MySQL索引怎么优化？', ['Redis是什么？', 'MySQL索引怎么优化？']),
    ('缓存雪崩是什么？怎么处理？什么时候会发生？', ['缓存雪崩是什么？', '怎么处理？', '什么时候会发生？']),
    ('Redis为什么这么快，以及它的持久化有哪几种方式', ['Redis为什么这么快', '它的持久化有哪几种方式']),
    ('介绍一下Kafka，另外消息丢了怎么办', ['介绍一下Kafka', '消息丢了怎么办']),
    ('Redis是什么。MySQL索引怎么优化', ['Redis是什么。', 'MySQL索引怎么优化']),
    ('我们公司主要做电商。Redis是什么？MySQL索引怎么优化？', ['Redis是什么？', 'MySQL索引怎么优化？']),
])
def test_split_multiple_questions(text, subs):
    assert split_questions(text) == subs


@pytest.mark.parametrize('text', [
    '你能讲讲分布式事务的区别吗', '说说Redis的持久化以及主从复制', '我们公司主要做电商。你们为什么选择Kafka？',
    '还有一个就是你们的并发量有多大', '', '嗯',
])
def test_split_keeps_single_question_unchanged(text):
    assert split_questions(text) == [text]


# ---------------- 打断分类 M05-5 ----------------
@pytest.mark.parametrize('text,dur,kind', [
    ('嗯', 300, 'backchannel'), ('好的', 800, 'backchannel'), ('OK', 500, 'backchannel'), ('对对对', 900, 'backchannel'),
    ('嗯', 1500, 'unknown'),
    ('那缓存呢？', 1200, 'followup'), ('具体说说RDB', 1500, 'followup'), ('能展开讲讲吗', 1200, 'followup'),
    ('不对，我是说持久化', 1500, 'followup'),
    ('Redis持久化的RDB和AOF有什么区别', 3000, 'followup'),  # 与当前问题相似度高
    ('MySQL的索引怎么优化', 2500, 'new_question'),  # 主题不同、无终止语：不是明确改题
    ('先停一下，换个问题，MySQL的索引怎么优化', 3000, 'switch'),
    ('先不说这个，你怎么看分布式事务', 3000, 'switch'),
    ('先停一下，换个问题', 1500, 'unknown'),
    ('我们公司主要做电商', 2000, 'unknown'),
    ('', 500, 'unknown'),
])
def test_classify_interrupt(text, dur, kind):
    assert classify_interrupt(text, dur, Q1).kind == kind


def test_interrupt_result_fields_and_threshold_is_parameter():
    r = classify_interrupt('先停一下，换个问题', 1500, Q1)
    assert r.stop and r.level == '' and r.reason == 'stop_phrase_only'
    text = 'Redis持久化怎么配置'
    assert classify_interrupt(text, 2000, Q1).kind == 'followup'
    assert classify_interrupt(text, 2000, Q1, sim_followup=0.99).kind == 'new_question'
    assert classify_interrupt(text, 2000, '', sim_followup=0.1).kind == 'new_question'  # 无当前问题：相似度 0


def test_similarity_and_stop_phrase():
    assert similarity('abc', 'abc') == 1.0 and similarity('a', 'abc') == 0.0 and similarity('', 'x') == 0.0
    assert similarity(Q1, 'MySQL的索引怎么优化') < 0.5 <= similarity(Q1, 'Redis持久化有什么区别')
    assert has_stop_phrase('先停一下') and not has_stop_phrase('Redis是什么')


# ---------------- 回答阶段状态机 ----------------
def test_T4_backchannel_keeps_answer_and_is_silent():
    d = answering()
    n = len(d.history)
    ask(d, 2000, 2400, '嗯')
    run_to(d, 4000)
    assert len(d.history) == n and d.backchannels == 1 and d.phase == 'answering' and d.premature_count == 0
    assert d.queue == [] and not of(d, 'answer_closed')


def test_T1_followup_keeps_main_answer_and_signals_interrupt_not_close():
    d = answering()
    ask(d, 2000, 3000, '那缓存呢？')
    evs = run_to(d, 3300)
    assert types(d, 2) == ['interrupt'] and evs[0].kind == 'followup' and evs[0].turn_text == '那缓存呢？'
    assert d.followups == 1 and d.phase == 'answering' and d.committed_count == 1 and d.premature_count == 0
    assert not of(d, 'answer_closed') and not of(d, 'interrupted')
    # 追问后回答方继续原回答：不关闭
    d.on_answerer_active(False, 3400)
    d.on_answerer_active(True, 3800)
    run_to(d, 9000)
    assert not of(d, 'answer_closed') and d.phase == 'answering'


def test_T7_thinking_pause_4s_not_closed_only_candidate_once():
    d = answering()
    d.on_answerer_active(False, 2000)
    run_to(d, 3400)
    assert types(d, 2) == []  # 1.4s < T_answer_silence
    run_to(d, 6000)
    assert types(d, 2) == ['answer_end_candidate'] and d.answer_end_candidates == 1
    assert d.phase == 'answering' and not of(d, 'answer_closed')
    # 回答方重新开口后再静音：重新给一次候选
    d.on_answerer_active(True, 6100)
    d.on_answerer_active(False, 6500)
    run_to(d, 8200)
    assert d.answer_end_candidates == 2


def test_T7_new_topic_while_answerer_speaking_is_queued_not_closed():
    d = answering()
    ask(d, 2000, 3200, 'MySQL的索引怎么优化')
    run_to(d, 3500)
    q = of(d, 'queued')
    assert len(q) == 1 and q[0].queue_len == 1 and q[0].turn_text == 'MySQL的索引怎么优化' and q[0].reason == 'answerer_active'
    assert d.phase == 'answering' and not of(d, 'answer_closed') and d.committed_count == 1
    assert [x['text'] for x in d.queue] == ['MySQL的索引怎么优化']
    # 回答方仍在说：长时间也不弹出
    run_to(d, 8000)
    assert not of(d, 'dequeued')


def test_queue_pops_after_answerer_silence_with_epoch_semantics_and_closes_current():
    d = answering()
    ask(d, 2000, 3200, 'MySQL的索引怎么优化')
    run_to(d, 3500)
    d.on_answerer_active(False, 4000)
    run_to(d, 5400)
    assert not of(d, 'dequeued')  # 静音 1.4s 未达阈值
    run_to(d, 5600)
    names = types(d, 2)
    assert names == ['queued', 'answer_closed', 'dequeued', 'committed'], names
    closed, deq, com = of(d, 'answer_closed')[0], of(d, 'dequeued')[0], d.history[-1]
    assert closed.reason == 'queue_pop' and closed.turn_text == Q1
    assert deq.queue_len == 0 and com.turn_text == 'MySQL的索引怎么优化' and com.reason == 'dequeued'
    assert com.t_speech_end_ms is None and d.queue == [] and d.committed_count == 2
    assert d.phase == 'question_committed'  # 弹出的题等回答方开口再 answering
    d.on_answerer_active(True, 6000)
    assert d.phase == 'answering'
    assert d.premature_count == 0


def test_popped_question_is_not_rolled_back_by_next_questioner_speech_and_next_new_question_queues():
    d = answering()
    ask(d, 2000, 3200, 'MySQL的索引怎么优化')
    run_to(d, 3500)
    d.on_answerer_active(False, 4000)
    run_to(d, 5600)
    assert d.phase == 'question_committed' and d.committed_count == 2
    ask(d, 6000, 7000, '说说消息队列的可靠性怎么保证')
    run_to(d, 7400)
    assert d.premature_count == 0 and not of(d, 'rolled_back')
    assert types(d, 6)[-1] == 'queued' and len(d.queue) == 1 and d.queue[0]['text'].startswith('说说消息队列')
    assert of(d, 'queued')[-1].reason == 'answer_not_started'
    assert d.on_transcript(Tr('迟到的旧句子', True, 9, 7000, 7000, 2000), now_ms=7500) == []  # 已处理句子的迟到转写忽略
    run_to(d, 9000)
    assert d.phase == 'question_committed' and d.committed_count == 2  # 未开口的已定题不被队列顶掉


def test_T8_main_path_answer_silence_then_new_question_closes_and_switches():
    d = answering()
    d.on_answerer_active(False, 2000)
    run_to(d, 3600)
    assert types(d, 2) == ['answer_end_candidate']
    ask(d, 4000, 5000, 'MySQL的索引怎么优化')
    run_to(d, 5300)
    names = types(d, 2)
    assert names == ['answer_end_candidate', 'answer_closed', 'committed'], names
    closed, com = of(d, 'answer_closed')[0], d.history[-1]
    assert closed.reason == 'new_question' and com.turn_text == 'MySQL的索引怎么优化' and com.reason == 'new_question'
    assert com.t_speech_end_ms == 5000 and com.t_ms == 5200 and com.text_version == 1
    assert d.premature_count == 0 and not of(d, 'rolled_back') and d.phase == 'question_committed'
    # 旧题迟到转写被丢弃，不污染新题
    assert d.on_transcript(Tr(Q1 + '呢', True, 5, 1000, 1000, 0), now_ms=5400) == []


def test_new_question_before_answerer_silence_threshold_waits_then_closes():
    d = answering()
    d.on_answerer_active(False, 2000)
    ask(d, 2200, 3000, 'MySQL的索引怎么优化')
    run_to(d, 3400)
    assert types(d, 2) == [] and d._iv_pending is not None  # 回答方静音仅 1.4s：只满足 (b)，先不关闭
    run_to(d, 3600)
    assert types(d, 2) == ['answer_closed', 'committed'] and d.history[-1].t_ms == 3500


def test_new_question_pending_goes_to_queue_if_answerer_resumes():
    d = answering()
    d.on_answerer_active(False, 2000)
    ask(d, 2200, 3000, 'MySQL的索引怎么优化')
    run_to(d, 3400)
    d.on_answerer_active(True, 3450)
    assert types(d, 2) == ['queued'] and d.phase == 'answering' and d._iv_pending is None
    run_to(d, 9000)
    assert not of(d, 'answer_closed')


def test_pending_new_question_cancelled_when_questioner_keeps_talking_and_merges():
    d = answering()
    d.on_answerer_active(False, 2000)
    ask(d, 2200, 3000, 'MySQL的索引怎么优化')
    run_to(d, 3300)
    assert d._iv_pending is not None
    ask(d, 3400, 4200, '还有为什么要用B+树', version=1)  # 同一轮继续说（key 不同）
    run_to(d, 5000)
    com = of(d, 'committed')[-1]
    assert com.turn_text == 'MySQL的索引怎么优化还有为什么要用B+树'
    assert com.sub_questions == ('MySQL的索引怎么优化', '为什么要用B+树')
    assert types(d)[-2:] == ['answer_closed', 'committed']


def test_T7_explicit_stop_and_new_question_interrupts_even_if_answerer_speaking():
    d = answering()
    ask(d, 2000, 3500, '先停一下，换个问题，MySQL的索引怎么优化')
    run_to(d, 3800)
    names = types(d, 2)
    assert names == ['interrupted', 'committed'], names
    it, com = d.history[-2], d.history[-1]
    assert it.reason == 'explicit_switch' and it.turn_text == Q1
    assert com.reason == 'explicit_switch' and com.turn_text.startswith('先停一下') and com.sub_questions == ('MySQL的索引怎么优化',)
    assert d.interrupted_count == 1 and d.closed_count == 0 and d.queue == [] and d.premature_count == 0


def test_stop_phrase_alone_is_unknown_then_merges_with_following_question():
    d = answering()
    ask(d, 2000, 2800, '先停一下，换个问题', version=1)
    run_to(d, 3600)
    assert types(d, 2) == ['interrupt'] and d.history[-1].kind == 'unknown' and d.phase == 'answering'
    run_to(d, 4400)
    assert types(d, 2) == ['interrupt']  # 同一文本不重复发
    ask(d, 4600, 5600, 'MySQL的索引怎么优化', version=1)
    run_to(d, 6000)
    assert types(d, 2) == ['interrupt', 'interrupted', 'committed']
    assert d.history[-1].sub_questions == ('MySQL的索引怎么优化',)


def test_stop_phrase_hold_expires_so_later_question_is_just_new_question():
    d = answering(iv_merge_ms=1000)
    ask(d, 2000, 2800, '先停一下，换个问题')
    run_to(d, 3600)
    ask(d, 6000, 7000, 'MySQL的索引怎么优化')
    run_to(d, 7400)
    assert types(d, 2) == ['interrupt', 'queued']  # 半句过期，不再算明确改题；回答方仍在说 -> 入队


def test_unclear_interjection_emits_unknown_and_keeps_answer():
    d = answering()
    ask(d, 2000, 3000, '我们公司主要做电商')
    run_to(d, 3800)
    assert types(d, 2) == ['interrupt'] and d.history[-1].kind == 'unknown' and d.unknowns == 1
    assert d.phase == 'answering' and not of(d, 'answer_closed') and d.queue == []


def test_interjection_without_transcript_changes_nothing():
    d = answering()
    n = len(d.history)
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 2500))
    run_to(d, 4500)
    assert len(d.history) == n and d.phase == 'answering' and d._iv_t0 is None


def test_interjection_waits_for_transcript_coverage_then_classifies():
    d = answering()
    ask(d, 2000, 3000, 'MySQL的索引', covered=2000)  # 旧 partial，覆盖不足
    run_to(d, 3600)
    assert types(d, 2) == []
    d.on_transcript(Tr('MySQL的索引怎么优化', False, 2, 3000, 3000, 2000), now_ms=3700)
    assert types(d, 2) == ['queued']


def test_incomplete_interjection_waits_for_hard_timeout_then_unknown():
    d = answering()
    ask(d, 2000, 3000, '你能不能')
    run_to(d, 4000)
    assert types(d, 2) == []
    run_to(d, 4600)
    assert types(d, 2) == ['interrupt'] and d.history[-1].kind == 'unknown'


def test_truncated_partial_that_looks_like_statement_is_recovered_by_late_final():
    """实际 L7 样本：截断 partial 先被判 unknown(unclear)，完整 final 到达后仍应成题。"""
    d = answering()
    d.on_answerer_active(False, 1600)
    partial = '示例公司的门店店长可以自己设'
    full = '示例公司的门店店长可以自己设置营销活动吗？'
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 2470))
    d.on_transcript(Tr(partial, False, 1, 2470, 2470, 2000), now_ms=2470)
    run_to(d, 4100)  # T_hard：半句被判 unclear
    assert d.history[-1].type == 'interrupt' and d.history[-1].kind == 'unknown'
    d.on_transcript(Tr(full, True, 2, 2470, 2470, 2000), now_ms=4300)
    run_to(d, 5000)
    assert of(d, 'committed')[-1].turn_text == full and of(d, 'answer_closed')


def test_short_question_final_after_unclear_window_is_recovered():
    """本地 ASR final 晚于打断判定窗口：短问句不能因先被判 unclear 就永久丢弃。"""
    d = answering()
    d.on_answerer_active(False, 1600)
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 2600))
    d.on_transcript(Tr('请解释', False, 1, 2600, 2600, 2000), now_ms=2600)
    run_to(d, 4200)  # sil 达 T_hard：先判 unclear 并发出 interrupt(unknown)
    assert d.history[-1].type == 'interrupt' and d.history[-1].kind == 'unknown'
    full = '请解释 CAP 定理中一致性和可用性如何取舍？'
    d.on_transcript(Tr(full, True, 2, 2600, 2600, 2000), now_ms=4300)
    run_to(d, 5000)
    com = of(d, 'committed')
    assert com and com[-1].turn_text == full and com[-1].reason == 'new_question'
    assert of(d, 'answer_closed') and d.phase == 'question_committed'


@pytest.mark.parametrize('late', ['对', '嗯', '好的'])
def test_late_non_question_final_after_unclear_is_not_committed(late):
    """反例：迟到 final 只是附和/语气词时，不得因为上面的补救逻辑提交成题。"""
    d = answering()
    d.on_answerer_active(False, 1600)
    before = len(of(d, 'committed'))
    d.on_vad(V('speech_start', 2000))
    d.on_vad(V('speech_end', 2600))
    d.on_transcript(Tr('请解释', False, 1, 2600, 2600, 2000), now_ms=2600)
    run_to(d, 4200)
    d.on_transcript(Tr(late, True, 2, 2600, 2600, 2000), now_ms=4300)
    run_to(d, 6000)
    assert len(of(d, 'committed')) == before and not of(d, 'answer_closed') and not of(d, 'interrupted')
    assert d.backchannels == 1 and d.phase == 'answering'


def test_recommit_while_answerer_still_speaking_keeps_answering_phase():
    """回答方仍在说时重新提交新题，必须进入 answering；否则下一题会被当成“续说”并入本题。"""
    d = TurnDetector(trigger='vad')
    ask(d, 0, 1000, Q1)  # 首题提交后回答方尚未开口
    run_to(d, 1200)
    assert d.phase == 'question_committed'
    d.on_vad(V('speech_start', 1300))  # 提问方几乎立刻续说（VAD 尾部抖动）
    d.on_answerer_active(True, 1350)  # 回答方在续说期间开口
    d.on_vad(V('speech_end', 2000))
    d.on_transcript(Tr(Q1, False, 2, 2000, 2000, 1300), now_ms=2000)
    run_to(d, 2400)  # 续说后重提交
    assert d.phase == 'answering' and d.answerer_active
    d.on_answerer_active(False, 2500)
    q2 = 'MySQL的索引怎么优化？'
    ask(d, 4000, 4500, q2)
    run_to(d, 7000)
    com = of(d, 'committed')
    assert com[-1].turn_text == q2 and com[-1].reason == 'new_question'
    assert of(d, 'answer_closed') and d.closed_count == 1 and d.queue == []


def test_late_original_question_final_still_rolls_back_during_answering():
    d = answering()
    ask(d, 2000, 3000, '那缓存呢？')
    run_to(d, 3300)
    evs = d.on_transcript(Tr(Q1 + '，另外它和AOF有什么不同', True, 2, 1000, 1000, 0), now_ms=3400)
    assert [e.type for e in evs] == ['rolled_back'] and d.premature_count == 1


def test_vad_trigger_classifies_without_coverage():
    d = answering(trigger='vad')
    ask(d, 2000, 3200, 'MySQL的索引怎么优化', covered=1000)
    run_to(d, 3500)
    assert types(d, 2) == ['queued']


def test_close_turn_and_reset_clear_answer_state():
    d = answering()
    ask(d, 2000, 3200, 'MySQL的索引怎么优化')
    run_to(d, 3500)
    d.on_vad(V('speech_start', 3600))
    assert d._iv_t0 is not None and len(d.queue) == 1
    d.close_turn(3000)
    assert d._iv_t0 is None and len(d.queue) == 1  # close_turn 不丢待答队列
    d.reset()
    assert d.queue == [] and d._ans_silent_since is None and d.phase == 'idle'


def test_replay_is_deterministic():
    def script():
        d = answering()
        ask(d, 2000, 3200, 'MySQL的索引怎么优化')
        run_to(d, 3500)
        d.on_answerer_active(False, 4000)
        run_to(d, 5600)
        ask(d, 6000, 6400, '嗯')
        ask(d, 7000, 8000, '那MySQL的B+树呢？')
        run_to(d, 9000)
        return d.history
    assert script() == script()


def test_answerer_active_is_independent_of_phase():
    d = TurnDetector()
    d.on_answerer_active(True, 100)
    d.on_answerer_active(False, 500)
    assert d._ans_silent_since == 500 and d.phase == 'idle' and d.answerer_active is False
    assert d.on_answerer_active(False, 600) == [] and d._ans_silent_since == 500


def test_M07_calibrate_tool_is_simulated_and_deterministic():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
    import calibrate_turn
    a = calibrate_turn.main(['--n', '3', '--step', '40'])
    b = calibrate_turn.main(['--n', '3', '--step', '40'])
    lines = a.splitlines()
    assert a == b and lines[0].startswith('# SIMULATED') and 'PASS' not in a
    rows = [ln for ln in lines if ln.startswith('| ') and ln.split('|')[1].strip().isdigit()]
    assert len(rows) == 16
