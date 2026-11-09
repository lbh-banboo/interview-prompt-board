"""M08-3 两路召回 + RRF(K=60) 融合 + 命中证据三条 + 库外拒认。

分数契约：bm25 为负数仅用于排序；RRF 是排名融合分，不是概率，不归一化；
cos 是唯一可与阈值比较的相似度，且只在同一 index_version 内可解释。
"""
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

RRF_K = 60
THRESHOLDS_FILE = Path(__file__).with_name("thresholds.json")

L1_HIT = "L1_HIT"
L1_TIE = "L1_TIE"          # 高分低 margin：并列候选，不得当标准答案
L1_PARTIAL = "L1_PARTIAL"  # 部分命中
MISS = "MISS"


@dataclass(frozen=True)
class Thresholds:
    tau_cos: float = 0.80          # 待标定（PLACEHOLDER，未经评测集标定）
    tau_cos_strong: float = 0.88   # 待标定（PLACEHOLDER）
    tau_mid: float = 0.65          # 待标定（PLACEHOLDER）
    margin: float = 0.03           # 待标定（PLACEHOLDER）
    calibrated: bool = False
    source: str = "placeholder"


def load_thresholds(embedder_revision=None, path=THRESHOLDS_FILE):
    """thresholds.json 由 tools/eval_retrieval.py 写入；模型 revision 不一致（如 HashEmbedder）则回退占位值。"""
    try:
        d = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return Thresholds()
    if embedder_revision is not None and d.get("embedder_revision") != embedder_revision:
        return Thresholds()
    return Thresholds(d["tau_cos"], d["tau_cos_strong"], d["tau_mid"], d["margin"], True, d.get("source", "calibrated"))


@dataclass
class Hit:
    entry_id: str
    rrf: float
    cos: float
    vec_rank: int = None
    bm25_rank: int = None
    bm25: float = None
    keyword_hit: bool = False
    kw_overlap: int = 0
    matched_text: str = ""


@dataclass
class SearchResult:
    query: str
    hits: list
    cos_top1: float
    cos_top2: float            # 不同条目的次高 cos；仅 1 个条目时为 None
    top1_entry: str            # 向量 cos 最高的条目（命中证据以它为准）
    keyword_mode: str          # 'trigram' | 'like'
    index_version: str


def rrf(rank_lists, k=RRF_K):
    """rank_lists: [{entry_id: rank(从1起)}]；返回 {entry_id: sum 1/(k+rank)}。"""
    out = {}
    for ranks in rank_lists:
        for eid, r in ranks.items():
            out[eid] = out.get(eid, 0.0) + 1.0 / (k + r)
    return out


def _best_rank(doc_scores, doc_entry):
    ranks, vals = {}, {}
    for pos, (doc_id, val) in enumerate(doc_scores, 1):
        eid = doc_entry[doc_id]
        if eid not in ranks:
            ranks[eid], vals[eid] = pos, val
    return ranks, vals


def search(index, query, top=20, kw_min_overlap=2):
    """问题与变体分别检索，按条目取最佳排名；两路各 top 条。"""
    mode, fts_docs = index.fts_query(query, top)
    qvec = index.embedder.embed([query])[0]
    vec_docs = index.vec_query(qvec, top) if qvec.any() else []  # 全标点/空查询嵌入为零向量，跳过向量路
    doc_entry = {r["id"]: (r["entry_id"], r["raw"]) for r in
                 index.db.execute("SELECT id, entry_id, raw FROM docs")} if index.size else {}
    de = {d: v[0] for d, v in doc_entry.items()}
    f_rank, f_val = _best_rank(fts_docs, de)
    v_rank, v_val = _best_rank(vec_docs, de)
    fused = rrf([f_rank, v_rank])
    f_doc = {}
    for d, _ in fts_docs:
        f_doc.setdefault(de[d], d)
    v_doc = {}
    for d, _ in vec_docs:
        v_doc.setdefault(de[d], d)

    entry_docs = {}
    for d, e in de.items():
        entry_docs.setdefault(e, []).append(d)
    cos = dict(v_val)
    missing = [e for e in fused if e not in cos]
    for e in missing:
        cos[e] = max(index.cos_to_docs(qvec, entry_docs[e]).values())
    ordered = sorted(fused, key=lambda e: (-fused[e], -cos[e], e))
    hits = []
    for e in ordered:
        ov = index.keyword_overlap(query, f_doc[e]) if e in f_doc and mode == "trigram" else 0
        kw = e in f_rank and (ov >= kw_min_overlap if mode == "trigram" else True)
        best_doc = v_doc.get(e) or f_doc.get(e)
        hits.append(Hit(e, fused[e], cos[e], v_rank.get(e), f_rank.get(e), f_val.get(e), kw, ov,
                        doc_entry[best_doc][1] if best_doc else ""))
    vec_sorted = sorted(v_val.items(), key=lambda kv: -kv[1])
    cos1 = vec_sorted[0][1] if vec_sorted else 0.0
    cos2 = vec_sorted[1][1] if len(vec_sorted) > 1 else None
    top1 = vec_sorted[0][0] if vec_sorted else None
    return SearchResult(query, hits, cos1, cos2, top1, mode, index.index_version)


@dataclass
class Classification:
    label: str
    cos_ok: bool
    keyword_ok: bool
    margin_ok: bool
    reasons: list = field(default_factory=list)

    @property
    def is_hit(self):
        return self.label == L1_HIT

    @property
    def weak(self):
        """弱匹配：L1_PARTIAL 但没有关键词证据（高 cos 空相似 / 低 cos 无词面重合）。

        cos 在 tau_mid~tau_cos 之间但有关键词证据的属于“真实部分覆盖”，不算弱匹配。
        """
        return self.label == L1_PARTIAL and not self.keyword_ok


def classify_hit(result, th=None):
    """L1 命中三条证据缺一不可：cos_top1≥τ_cos；FTS 对 cos 最高条目有关键词命中（或 cos≥τ_cos_strong）；
    cos_top1−cos_top2≥margin。"""
    th = th or Thresholds()
    cos1 = result.cos_top1
    if result.top1_entry is None or cos1 < th.tau_mid:
        return Classification(MISS, False, False, False, ["cos<tau_mid"])
    top1_hit = next((h for h in result.hits if h.entry_id == result.top1_entry), None)
    cos_ok = cos1 >= th.tau_cos
    keyword_ok = bool(top1_hit and top1_hit.keyword_hit) or cos1 >= th.tau_cos_strong
    margin_ok = result.cos_top2 is None or (cos1 - result.cos_top2) >= th.margin
    if not cos_ok:
        return Classification(L1_PARTIAL, False, keyword_ok, margin_ok, ["cos<tau_cos"])
    reasons = []
    if not keyword_ok:
        reasons.append("no_keyword_evidence")
    if not margin_ok:
        reasons.append("margin_too_small")
    if not reasons:
        return Classification(L1_HIT, True, True, True)
    if not margin_ok:
        return Classification(L1_TIE, True, keyword_ok, False, reasons)
    return Classification(L1_PARTIAL, True, False, True, reasons)


def with_thresholds(th, **kw):
    return replace(th, **kw)
