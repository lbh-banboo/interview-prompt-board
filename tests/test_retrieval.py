import json
import sqlite3
import sys

import numpy as np
import pytest

from app.retrieval import precheck as pc
from app.retrieval.index import MODEL_DIR, HashEmbedder, Index, normalize_fts
from app.retrieval.search import (L1_HIT, L1_PARTIAL, L1_TIE, MISS, Hit, SearchResult, Thresholds,
                                  classify_hit, load_thresholds, rrf, search)
from app.retrieval.tags import answer_tags, g3_eligible, pending_marks

ENTRIES = [
    {"id": "A", "question": "什么是缓存穿透，如何避免？"},
    {"id": "B", "question": "Kubernetes 的 Pod 是什么？"},
    {"id": "C", "question": "你们的数据库为什么选择 MySQL 而不是 Oracle？", "variants": ["为啥用 MySQL 不用 Oracle"]},
    {"id": "D", "question": "介绍一下你负责的支付系统架构"},
    {"id": "E", "question": "团队里出现意见分歧时你怎么处理"},
]


@pytest.fixture
def index():
    ix = Index(HashEmbedder())
    ix.build(ENTRIES)
    return ix


# ---------- M08-1 FTS5 trigram / 短词回退 ----------
def test_trigram_hit_and_bm25_is_negative_ranking_only(index):
    r = search(index, "缓存穿透怎么处理")
    top = r.hits[0]
    assert r.keyword_mode == "trigram" and top.entry_id == "A"
    assert top.bm25_rank == 1 and top.bm25 < 0 and top.keyword_hit


def test_two_char_term_falls_back_to_like_as_keyword_evidence_only(index):
    r = search(index, "缓存")
    assert r.keyword_mode == "like"
    a = next(h for h in r.hits if h.entry_id == "A")
    assert a.keyword_hit and a.bm25 is None and a.bm25_rank == 1
    assert search(index, "缓存").keyword_mode == "like" and search(index, "我").keyword_mode == "like"


def test_like_fallback_escapes_wildcards(index):
    assert all(h.bm25_rank is None for h in search(index, "%_").hits)


def test_three_letter_term_uses_trigram_case_insensitive(index):
    ix = Index(HashEmbedder()); ix.build(ENTRIES + [{"id": "F", "question": "CAP 定理是什么"}])
    r = search(ix, "cap")
    assert r.keyword_mode == "trigram" and r.hits[0].entry_id == "F"


def test_english_term_matches_across_space_boundary(index):
    assert normalize_fts("示例公司 AI 点单") == normalize_fts("示例公司AI点单")
    r = search(index, "kubernetes")
    assert next(h for h in r.hits if h.entry_id == "B").keyword_hit


def test_variant_is_searched_and_best_rank_taken_per_entry(index):
    r = search(index, "为啥用 MySQL 不用 Oracle")
    c = [h for h in r.hits if h.entry_id == "C"]
    assert len(c) == 1 and c[0].bm25_rank == 1 and c[0].matched_text.startswith("为啥")


# ---------- M08-3 RRF ----------
def test_rrf_hand_computed():
    out = rrf([{"A": 1, "B": 2}, {"B": 1, "C": 3}])
    assert out["A"] == pytest.approx(1 / 61)
    assert out["B"] == pytest.approx(1 / 62 + 1 / 61)
    assert out["C"] == pytest.approx(1 / 63)
    assert max(out, key=out.get) == "B"
    assert rrf([{"A": 1}], k=0)["A"] == 1.0


def test_search_rrf_matches_ranks_and_is_not_normalized(index):
    r = search(index, "缓存穿透怎么处理")
    for h in r.hits:
        expect = sum(1 / (60 + x) for x in (h.vec_rank, h.bm25_rank) if x)
        assert h.rrf == pytest.approx(expect)
    assert r.hits[0].rrf < 1.0 and r.hits[0].rrf <= 2 / 61 + 1e-9
    assert [h.rrf for h in r.hits] == sorted((h.rrf for h in r.hits), reverse=True)


def test_every_hit_reports_cos_even_if_only_in_one_route(index):
    r = search(index, "Oracle")
    assert all(isinstance(h.cos, float) for h in r.hits)


# ---------- 命中证据三条各自否决 ----------
def mk(cos1, cos2, kw=True, top="X"):
    hits = [Hit("X", 0.03, cos1, 1, 1, -3.0, kw)]
    if cos2 is not None:
        hits.append(Hit("Y", 0.02, cos2, 2, 2, -2.0, False))
    return SearchResult("q", hits, cos1, cos2, top, "trigram", "v")


TH = Thresholds(tau_cos=0.8, tau_cos_strong=0.9, tau_mid=0.6, margin=0.05)


def test_all_three_evidences_pass_is_l1_hit():
    c = classify_hit(mk(0.85, 0.7), TH)
    assert c.label == L1_HIT and c.is_hit and c.cos_ok and c.keyword_ok and c.margin_ok


def test_veto_cos_below_tau():
    c = classify_hit(mk(0.79, 0.5), TH)
    assert c.label == L1_PARTIAL and not c.cos_ok and not c.is_hit


def test_veto_no_keyword_evidence_downgrades():
    c = classify_hit(mk(0.85, 0.5, kw=False), TH)
    assert c.label == L1_PARTIAL and not c.keyword_ok and "no_keyword_evidence" in c.reasons
    assert c.weak  # 无关键词证据的 L1_PARTIAL 是弱匹配，不得当主答案


def test_partial_band_with_keyword_evidence_is_not_weak():
    c = classify_hit(mk(0.7, 0.2, kw=True), TH)
    assert c.label == L1_PARTIAL and c.keyword_ok and not c.weak


def test_hit_and_tie_are_not_weak():
    assert not classify_hit(mk(0.91, 0.7, kw=False), TH).weak  # 高 cos 直接命中
    assert not classify_hit(mk(0.88, 0.86, kw=True), TH).weak  # 并列候选


def test_no_keyword_but_cos_strong_passes():
    assert classify_hit(mk(0.91, 0.5, kw=False), TH).label == L1_HIT


def test_veto_margin_tie_is_not_standard_answer():
    c = classify_hit(mk(0.88, 0.86), TH)
    assert c.label == L1_TIE and not c.margin_ok and not c.is_hit


def test_margin_exactly_at_threshold_passes():
    assert classify_hit(mk(0.85, 0.80), Thresholds(0.8, 0.9, 0.6, 0.05 - 1e-9)).label == L1_HIT


def test_partial_band_and_miss():
    assert classify_hit(mk(0.7, 0.2), TH).label == L1_PARTIAL
    assert classify_hit(mk(0.59, 0.2), TH).label == MISS
    assert classify_hit(SearchResult("q", [], 0.0, None, None, "trigram", "v"), TH).label == MISS


def test_single_entry_library_has_no_margin_veto():
    assert classify_hit(mk(0.85, None), TH).label == L1_HIT


def test_keyword_evidence_must_belong_to_cos_top1_entry():
    r = mk(0.85, 0.5, kw=False)
    r.hits.append(Hit("Z", 0.01, 0.4, None, 3, -1.0, True))  # 别的条目有关键词不算
    assert classify_hit(r, TH).label == L1_PARTIAL


def test_tied_duplicate_entries_end_to_end_not_hit():
    ix = Index(HashEmbedder())
    ix.build([{"id": "P", "question": "目前还在职吗"}, {"id": "Q", "question": "目前还在职吗"}])
    r = search(ix, "目前还在职吗")
    assert r.cos_top1 - r.cos_top2 == pytest.approx(0, abs=1e-6)
    assert classify_hit(r, Thresholds(0.5, 0.6, 0.3, 0.03)).label == L1_TIE


def test_out_of_library_query_is_refused(index):
    th = Thresholds(0.8, 0.9, 0.6, 0.05)
    for q in ["今天天气怎么样", "光合作用的过程", "我们开始吧"]:
        assert not classify_hit(search(index, q), th).is_hit


def test_in_library_exact_query_hits(index):
    th = Thresholds(0.8, 0.9, 0.6, 0.05)
    r = search(index, "什么是缓存穿透，如何避免？")
    c = classify_hit(r, th)
    assert c.is_hit and r.top1_entry == "A"


# ---------- 索引 / 版本 / 向量后端 ----------
def test_index_version_changes_with_content_model_and_is_order_insensitive():
    def ver(entries, emb=None):
        ix = Index(emb or HashEmbedder()); return ix.build(entries)
    v = ver(ENTRIES)
    assert v == ver(list(reversed(ENTRIES)))
    assert v != ver(ENTRIES[:-1])
    assert v != ver([{**ENTRIES[0], "question": ENTRIES[0]["question"] + "呢"}] + ENTRIES[1:])
    assert v != ver([{**ENTRIES[0], "variants": ["新变体"]}] + ENTRIES[1:])
    e2 = HashEmbedder(); e2.revision = "hash-v2"
    assert v != ver(ENTRIES, e2)
    assert "fts5-trigram" in v and "hash-v1" in v


def test_rebuild_replaces_content(index):
    index.build([{"id": "Z", "question": "完全不同的新问题内容"}])
    assert {h.entry_id for h in search(index, "缓存穿透").hits} <= {"Z"}


def test_sqlite_vec_backend_and_numpy_fallback_agree(index):
    fb = Index(HashEmbedder(), use_sqlite_vec=False); fb.build(ENTRIES)
    assert fb.vector_backend == "numpy-fallback"
    assert index.vector_backend in ("sqlite-vec", "numpy-fallback")
    a, b = search(index, "缓存穿透怎么避免"), search(fb, "缓存穿透怎么避免")
    assert [h.entry_id for h in a.hits] == [h.entry_id for h in b.hits]
    assert a.cos_top1 == pytest.approx(b.cos_top1, abs=1e-5)


def test_hash_embedder_is_deterministic_normalized_and_marked_simulated():
    e = HashEmbedder()
    v1, v2 = e.embed(["缓存穿透", "缓存穿透"])
    assert np.allclose(v1, v2) and np.linalg.norm(v1) == pytest.approx(1.0, abs=1e-5)
    assert e.simulated and e.name.startswith("SIMULATED")
    assert float(v1 @ e.embed(["缓存击穿"])[0]) > float(v1 @ e.embed(["今天吃饭"])[0])


def test_thresholds_default_is_placeholder_and_revision_bound(tmp_path):
    assert not Thresholds().calibrated
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"tau_cos": 0.7, "tau_cos_strong": 0.8, "tau_mid": 0.5, "margin": 0.03,
                             "embedder_revision": "rev1"}))
    assert load_thresholds("rev1", f).calibrated and load_thresholds("rev1", f).tau_cos == 0.7
    assert not load_thresholds("other", f).calibrated
    assert not load_thresholds("rev1", tmp_path / "none.json").calibrated


@pytest.mark.skipif(not (MODEL_DIR / "model_optimized.onnx").exists(), reason="bge-small-zh 模型未下载")
def test_real_bge_model_semantic_sanity():
    from app.retrieval.index import FastEmbedder
    e = FastEmbedder()
    a, b, c = e.embed(["示例公司 AI 点单底层接的是哪家模型？", "你们点单用的什么大模型", "今天天气怎么样"])
    assert e.dim == a.shape[0] == 512 and np.linalg.norm(a) == pytest.approx(1.0, abs=1e-4)
    assert float(a @ b) > float(a @ c) + 0.2
    assert "46fbe35f" in e.revision


# ---------- M08-5 预检索复用 ----------
def partial(text="介绍一下你做过的AI项目", **kw):
    return {"text": text, "text_version": 3, "turn_id": "t1", "index_version": "iv", **kw}


def reuse(p=None, committed="介绍一下你做过的AI项目", **kw):
    args = dict(committed_version=4, committed_turn_id="t1", committed_index_version="iv")
    args.update(kw)
    return pc.can_reuse_precheck(p or partial(), committed, **args)


def test_precheck_reuses_when_all_four_conditions_hold():
    d = reuse()
    assert d.reuse and d.reason == "ok" and bool(d) and d.similarity == 1.0


def test_precheck_veto_similarity_below_threshold():
    d = reuse(partial("介绍一下"), "介绍一下你做过的AI项目")
    assert not d and d.reason == "similarity_below_threshold"


def test_precheck_veto_tail_sentence_not_covered():
    # 整体相似度够高，但尾句 partial 里没有（commit 多说了一句话）
    p = partial("请介绍你在示例公司做过的完整度较高的项目是什么样的情况呢，另外介绍一下团队规模和分工")
    d = reuse(p, p["text"] + "吗。你们几个人？")
    assert d.reason in ("tail_not_covered", "similarity_below_threshold") and not d
    d2 = pc.can_reuse_precheck({"text": "你们的项目有几个人参加的啊", "text_version": 1}, "你们的项目有几个人参加的啊。是谁带队的", committed_version=2, min_similarity=0.7)
    assert d2.reason == "tail_not_covered"


def test_precheck_veto_negation_mismatch():
    d = pc.can_reuse_precheck({"text": "不是所有用户都有这个需求你觉得是否可行呢朋友们", "text_version": 1},
                              "是所有用户都有这个需求你觉得是否可行呢朋友们", committed_version=1, min_similarity=0.8)
    assert d.reason == "negation_mismatch"


def test_precheck_veto_subquestion_count_mismatch():
    d = pc.can_reuse_precheck({"text": "你们的方案是什么，以及怎么评估效果", "text_version": 1},
                              "你们的方案是什么。以及怎么评估效果", committed_version=1, min_similarity=0.8,
                              count_fn=lambda t: 2 if "。" in t else 1)
    assert d.reason == "subquestion_count_mismatch"
    assert pc.count_subquestions("先说方案。再说效果？") == 2 and pc.count_subquestions("说说方案") == 1


@pytest.mark.parametrize("p,kw", [
    ({"text_version": None}, {}),                       # 无版本号
    ({"text_version": 9}, {}),                          # 晚于 commit 版本
    ({"text_version": True}, {}),                       # 非整数版本
    ({"turn_id": "t2"}, {}),                            # 跨 turn
    ({"index_version": "other"}, {}),                   # 索引版本不同，分数不可比
    ({}, {"committed_version": None}),                  # commit 版本未知
])
def test_precheck_veto_text_version_untraceable(p, kw):
    d = reuse(partial(**p), **kw)
    assert not d and d.reason == "text_version_untraceable"


def test_precheck_empty_inputs_never_reuse():
    assert not pc.can_reuse_precheck(None, "x", committed_version=1)
    assert not pc.can_reuse_precheck({"text": "", "text_version": 1}, "", committed_version=1)


# ---------- M08-7 评测集构建 ----------
DB = __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "interview.sqlite3"


@pytest.mark.skipif(not DB.exists(), reason="本地主库不存在")
def test_eval_set_builder_is_deterministic_and_meets_size():
    sys.path.insert(0, str(DB.parents[1] / "tools"))
    import build_retrieval_eval as b
    entries = b.load_entries(DB)
    items = b.build(entries)
    assert items == b.build(entries)
    para = [i for i in items if i["type"] == "paraphrase" and i["noise"] == "none"]
    oos = [i for i in items if i["type"] == "oos"]
    ids = {e["id"] for e in entries}
    assert len(para) >= 200 and len(oos) >= 100
    assert {i["type"] for i in items} == {"paraphrase", "oos", "process", "short_term"}
    assert all(i["expected"] in ids for i in para)
    assert all(i["query"] != next(e["question"] for e in entries if e["id"] == i["expected"]) for i in para)
    assert all(i["expected"] == b.OUT_OF_LIBRARY for i in oos)
    lib_q = {normalize_fts(e["question"]) for e in entries}
    assert not any(normalize_fts(i["query"]) in lib_q for i in oos)


# ---------- 主答案来源标签 / 待核标记 / G3 口径（纯函数） ----------
def test_answer_tags_flagged_entry_never_my_voice():
    e = {'flagged': True, 'approved_answer': '项目复盘 [待核]，另有 [待核] 一处', 'answer': ''}
    tag, flagged, marks = answer_tags(e, 'L1')
    assert (tag, flagged, marks) == ('含待核内容', True, 2)


def test_answer_tags_mark_in_body_counts_as_flagged_even_without_payload_flag():
    e = {'flagged': False, 'approved_answer': '数字 6379 [待核]'}
    assert answer_tags(e, 'L1p') == ('含待核内容', True, 1)


def test_answer_tags_clean_entry_by_level():
    e = {'flagged': False, 'approved_answer': '干净答案'}
    assert answer_tags(e, 'L1') == ('我的口径', False, 0)
    assert answer_tags(e, 'L1p') == ('部分命中，非标准答案', False, 0)
    assert answer_tags(e, 'L2') == ('通用建议', False, 0)


def test_pending_marks_prefers_reviewed_answer():
    assert pending_marks({'approved_answer': '已核实', 'answer': '原答案 [待核]'}) == 0
    assert pending_marks({'approved_answer': '', 'answer': '原答案 [待核]'}) == 1


def test_g3_eligibility_excludes_flagged_and_pending_marks():
    assert g3_eligible({'status': 'approved', 'flagged': False, 'approved_answer': 'x'})
    assert not g3_eligible({'status': 'approved', 'flagged': True, 'approved_answer': 'x'})
    assert not g3_eligible({'status': 'approved', 'flagged': False, 'approved_answer': 'x [待核]'})


def test_review_progress_g3_excludes_flagged_entries(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import library_review
    from app.core import Store

    store = Store(tmp_path)
    lib = store.import_text('样例', '**H1-01｜00:01｜问题1？** 参考回答：答案1\n'
                                    '**H1-02｜00:02｜问题2？** 参考回答：答案2 [待核]\n'
                                    '**H1-03｜00:03｜问题3？** 参考回答：答案3')['id']
    for key, answer in (('H1-01', '答案1'), ('H1-02', '答案2 [待核]'), ('H1-03', '答案3')):
        store.review(lib, key, answer, 'approved', True)
    app = FastAPI()
    library_review.register(app, SimpleNamespace(store=store, root=tmp_path, token='tok', bus=None, config={}))
    p = TestClient(app).get(f'/api/libraries/{lib}/progress').json()
    assert (p['approved'], p['g3_eligible'], p['pending_approved'], p['g3_ready']) == (3, 2, 1, False)
    assert '已启用 3/50' in p['hint'] and '其中可计入 G3 2' in p['hint'] and '含待核 1 条' in p['hint']
