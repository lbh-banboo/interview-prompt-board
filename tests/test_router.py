"""M08-4/M08-6 路由器、意图分类、子句拆分（M08-T3/T4/T5）。HashEmbedder 为 SIMULATED，仅验证流程。"""
from types import SimpleNamespace as NS

import pytest

from app.retrieval.clauses import merge_coverage, split_clauses
from app.retrieval.index import HashEmbedder, Index
from app.retrieval.intent import BEHAVIOR, KNOWLEDGE, OPINION, PERSONAL, PROCESS, classify_intent
from app.retrieval.router import PERSONAL_NOTE, TAG_L1, TAG_L2, Router
from app.retrieval.search import Hit, SearchResult, Thresholds

ENTRIES = [
    {"id": "A", "question": "什么是缓存穿透，如何避免？"},
    {"id": "B", "question": "Kubernetes 的 Pod 是什么？"},
    {"id": "D", "question": "介绍一下你负责的支付系统架构"},
]


@pytest.fixture(scope="module")
def index():
    ix = Index(HashEmbedder())
    ix.build(ENTRIES)
    return ix


class FakeSecondary:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def search(self, query, roles):
        self.calls.append((query, roles))
        return self.hits


def res(cos1, cos2=None, top="A", kw=True, others=("B",)):
    hits = [Hit(top, 0.03, cos1, keyword_hit=kw)] + [Hit(o, 0.02, cos2 if cos2 is not None else 0.1) for o in others]
    return SearchResult("q", hits, cos1, cos2, top, "trigram", "v1")


def router(index, result, secondary=None, **kw):
    return Router(index, secondary, search_fn=lambda ix, q: result, **kw)


# ---------- 意图分类 ----------
@pytest.mark.parametrize("text,kind", [
    ("你们项目的并发量是多少", PERSONAL), ("你负责过哪些模块", PERSONAL), ("你在上一家公司做什么", PERSONAL),
    ("什么是缓存穿透", KNOWLEDGE), ("Redis 和 Memcached 的区别", KNOWLEDGE), ("这个怎么实现", KNOWLEDGE),
    ("你怎么看微服务拆分", OPINION), ("如何看待远程办公", OPINION),
    ("举个例子说明", BEHAVIOR), ("如果线上突然大量超时你会怎么排查", BEHAVIOR),
    ("能听到吗", PROCESS), ("我们开始吧", PROCESS), ("好的", PROCESS),
])
def test_intent_rules(text, kind):
    it = classify_intent(text)
    assert it.kind == kind and it.signals


def test_intent_default_is_knowledge_with_default_signal():
    assert classify_intent("缓存").kind == KNOWLEDGE and classify_intent("缓存").signals == ["default"]
    assert classify_intent(None).kind == PROCESS


@pytest.mark.parametrize("text", [
    "你最有成就感的一件事是什么", "你的优缺点是什么", "介绍一下你的职业经历", "你为什么离开上一家公司",
    "你遇到过的最大失败是什么", "你们团队现在有几个人", "你的职业规划是什么",
])
def test_personal_experience_questions_are_personal(text):
    """实测漏判回归：经历/成就/优缺/离职类叙事问句一律 personal（宁可请本人补充，也不外发）。"""
    it = classify_intent(text)
    assert it.kind == PERSONAL and it.signals


@pytest.mark.parametrize("text,kind", [
    ("什么是缓存穿透", KNOWLEDGE), ("Redis 和 Memcached 的区别", KNOWLEDGE),
    ("如何实现幂等", KNOWLEDGE), ("我们为什么要用消息队列", KNOWLEDGE),
    ("如何管理一个团队", KNOWLEDGE), ("你怎么看微服务拆分", OPINION),
    ("你怎么看待 RAG 的局限", OPINION), ("你觉得远程办公好吗", OPINION),
    ("你怎么看待团队的协作", OPINION),
])
def test_impersonal_knowledge_and_opinion_stay_external(text, kind):
    """保守规则不能把不指向本人经历的知识/观点题误判成个人题。"""
    assert classify_intent(text).kind == kind


MISS_RES = SearchResult("q", [], 0.0, None, None, "trigram", "v")


@pytest.mark.parametrize("text", ["你最有成就感的一件事是什么", "你的优缺点是什么",
                                  "介绍一下你的职业经历", "你为什么离开上一家公司"])
def test_missed_personal_questions_never_web(text, index):
    d = Router(index, search_fn=lambda ix, q: MISS_RES).route(text, ["tech"])
    assert d.subs[0].intent.kind == PERSONAL and d.subs[0].level == "personal_miss"
    assert d.subs[0].followup == "personal_miss" and not d.need_web and d.web_queries == []
    assert d.level == "personal_miss" and d.personal_note == PERSONAL_NOTE


# ---------- 子句拆分 ----------
def test_split_by_question_mark_and_connective_with_spans():
    t = "什么是缓存穿透？另外介绍一下你负责的项目，以及遇到的困难"
    cs = split_clauses(t)
    assert [c.text for c in cs] == ["什么是缓存穿透", "介绍一下你负责的项目", "遇到的困难"]
    assert all(t[c.start:c.end] == c.text for c in cs)


def test_split_keeps_coordination_and_inner_questions():
    assert len(split_clauses("Redis 和 Memcached 的区别是什么")) == 1
    assert len(split_clauses("缓存除了穿透还有什么问题")) == 1
    assert len(split_clauses("先说A；然后说B")) == 2
    assert split_clauses("") == [] and split_clauses(None) == []


def test_merge_coverage_partial():
    a, b, c = (NS(clause=x, covered=v, skipped=s) for x, v, s in [("a", True, False), ("b", False, False), ("c", False, True)])
    cov = merge_coverage([a, b, c])
    assert cov.covered == ["a"] and cov.pending == ["b"] and cov.partial
    assert not merge_coverage([a]).partial


# ---------- 路由：真实夹具索引 ----------
def test_l1_hit_marks_own_answer(index):
    d = Router(index).route("什么是缓存穿透，如何避免？", ["tech"])
    s = d.subs[0]
    assert d.level == "L1" and s.entry_id == "A" and s.tag == TAG_L1 and not d.need_web and d.personal_note is None
    assert d.index_version == index.index_version


def test_process_utterance_skips_without_search(index):
    def boom(*a):
        raise AssertionError("流程性发言不应检索")
    d = Router(index, search_fn=boom).route("能听到吗", ["tech"])
    assert d.level == "skip" and not d.need_web and d.subs[0].skipped and d.pending == []


@pytest.mark.parametrize("text", ["缓存", "CAP"])
def test_short_term_and_english_term_unrelated_never_standard_answer(index, text):
    d = Router(index).route(text, ["tech"])
    assert d.level == "L3" and d.need_web and d.web_queries == [text]


def test_english_term_hit(index):
    assert Router(index).route("Kubernetes 的 Pod 是什么", ["tech"]).level == "L1"


def test_empty_text_is_skip(index):
    d = Router(index).route("  ", ["tech"], {"turn_id": 3})
    assert d.level == "skip" and d.ctx == {"turn_id": 3}


def test_whole_text_used_when_no_clause_survives(index):
    assert Router(index).route("好", ["tech"]).level == "skip"


# ---------- M08-T3 个人经历未命中不联网 ----------
def test_personal_miss_gives_note_and_never_web(index):
    sec = FakeSecondary([NS(entry_id="S1", cos=0.99)])
    d = Router(index, sec).route("你做过哪些机器学习项目", ["tech"])
    assert d.level == "personal_miss" and not d.need_web and d.web_queries == []
    assert d.personal_note == PERSONAL_NOTE and sec.calls == []  # 个人题也不走次优库
    assert d.pending == [d.subs[0].clause]


def test_behavior_miss_also_no_web(index):
    d = Router(index).route("如果线上突然大量超时你会怎么排查", ["tech"])
    assert d.level == "personal_miss" and not d.need_web


# ---------- M08-T5 弱相关 / 并列 ----------
def test_tie_is_candidates_not_standard_then_l3(index):
    d = router(index, res(0.9, 0.89)).route("缓存相关问题", ["tech"])
    s = d.subs[0]
    assert d.level == "L1c" and s.entry_id is None and [c[0] for c in s.candidates] == ["A", "B"]
    assert d.need_web and s.followup == "L3" and not s.covered


def test_tie_with_personal_intent_no_web(index):
    d = router(index, res(0.9, 0.89)).route("你负责的项目是什么", ["tech"])
    assert d.level == "L1c" and not d.need_web and d.personal_note and d.subs[0].followup == "personal_miss"


def test_weak_top1_partial_not_standard(index):
    d = router(index, res(0.7, 0.2)).route("缓存相关问题", ["tech"])
    s = d.subs[0]
    assert d.level == "L1p" and s.entry_id == "A" and s.evidence["label"] == "L1_PARTIAL" and not s.covered
    assert d.need_web and s.followup == "L3"


def test_weak_match_without_keyword_evidence_is_candidate_not_primary(index):
    """高 cos 但无关键词证据：弱匹配，降为候选，不得当主答案。"""
    d = router(index, res(0.85, 0.2, kw=False)).route("缓存相关问题", ["tech"])
    s = d.subs[0]
    assert s.level == "L1c" and s.entry_id is None and [c[0] for c in s.candidates] == ["A"]
    assert s.tag == "候选，未确认" and not s.covered and s.followup == "L3" and d.need_web


def test_partial_band_with_keyword_evidence_stays_l1p(index):
    """cos 介于 tau_mid~tau_cos 但有关键词证据：真实部分覆盖，仍为 L1p。"""
    d = router(index, res(0.7, 0.2, kw=True)).route("缓存相关问题", ["tech"])
    s = d.subs[0]
    assert s.level == "L1p" and s.entry_id == "A" and s.evidence["cos_ok"] is False and s.evidence["keyword_ok"]


def test_partial_with_secondary_l2_followup(index):
    sec = FakeSecondary([NS(entry_id="S1", cos=0.9)])
    d = router(index, res(0.7, 0.2), sec).route("缓存相关问题", ["tech", "ops"])
    s = d.subs[0]
    assert d.level == "L1p" and s.followup == "L2" and s.secondary["entry_id"] == "S1" and not d.need_web
    assert sec.calls == [("缓存相关问题", ["tech", "ops"])]


def test_below_tau_mid_is_miss_to_web(index):
    d = router(index, res(0.3, 0.1)).route("今天天气怎么样", ["tech"])
    assert d.level == "L3" and d.need_web and d.subs[0].tag == "暂无标准答案，正在补充"


# ---------- L2 ----------
def test_l2_hit_labelled_generic_advice(index):
    sec = FakeSecondary([NS(entry_id="S1", cos=0.85)])
    d = router(index, res(0.3, 0.1), sec).route("你觉得远程办公好吗", ["tech"])
    s = d.subs[0]
    assert d.level == "L2" and s.tag == TAG_L2 and s.entry_id == "S1" and not d.need_web and s.covered


def test_l2_below_tau2_goes_web_and_custom_tau2(index):
    sec = FakeSecondary([NS(entry_id="S1", cos=0.5)])
    d = router(index, res(0.3, 0.1), sec).route("你觉得远程办公好吗", ["tech"])
    assert d.level == "L3" and d.need_web and d.subs[0].intent.kind == OPINION
    assert router(index, res(0.3, 0.1), sec, tau2=0.4).route("你觉得远程办公好吗", ["tech"]).level == "L2"


def test_empty_secondary_hits_goes_web(index):
    d = router(index, res(0.3, 0.1), FakeSecondary([]), thresholds=Thresholds()).route("什么是熔断", ["tech"])
    assert d.level == "L3"


# ---------- M08-T4 多子问 / 部分命中 / 个人+知识混合 ----------
def test_partial_coverage_shows_hit_first_pending_marked(index):
    d = Router(index).route("什么是缓存穿透，如何避免？另外讲讲你做过的区块链项目", ["tech"])
    assert d.level == "L1p" and d.partial
    assert [c.text for c in d.covered] == ["什么是缓存穿透，如何避免"]
    assert [c.text for c in d.pending] == ["讲讲你做过的区块链项目"]
    assert d.subs[0].level == "L1" and d.subs[1].level == "personal_miss"
    assert not d.need_web and d.personal_note == PERSONAL_NOTE


def test_mixed_personal_and_knowledge_split(index):
    d = Router(index).route("你做过哪些机器学习项目？还有什么是量子退火", ["tech"])
    p, k = d.subs
    assert (p.intent.kind, p.level, p.need_web) == (PERSONAL, "personal_miss", False)
    assert (k.intent.kind, k.level, k.need_web) == (KNOWLEDGE, "L3", True)
    assert d.need_web and d.web_queries == [k.clause.text] and d.personal_note == PERSONAL_NOTE
    assert d.level == "L3"


def test_mixed_with_l2_for_knowledge_part(index):
    sec = FakeSecondary([NS(entry_id="S1", cos=0.9)])
    d = Router(index, sec).route("你做过哪些机器学习项目？还有什么是量子退火", ["tech"])
    assert [s.level for s in d.subs] == ["personal_miss", "L2"] and d.level == "L2" and not d.need_web


def test_multi_all_l1_and_process_clause_ignored(index):
    d = Router(index).route("好的，我们开始吧。什么是缓存穿透，如何避免？Kubernetes 的 Pod 是什么", ["tech"])
    assert [s.level for s in d.subs] == ["skip", "L1", "L1"] and d.level == "L1" and not d.pending
