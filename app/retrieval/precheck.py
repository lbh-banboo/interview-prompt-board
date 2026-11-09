"""M08-5 预检索复用判定（纯函数）。四条件全满足才复用 partial 的检索结果，否则重新检索（R04）。"""
import difflib
import re
from dataclasses import dataclass

from .index import normalize_fts

NEGATIONS = "不没无未别非否"
_SENT_SPLIT = re.compile(r"[。？！?!；;\n]+")
_SUBQ_SPLIT = re.compile(r"[。？！?!；;\n]+|以及|还有|另外|同时|并且|然后")


def similarity(a, b):
    return difflib.SequenceMatcher(None, normalize_fts(a).replace(" ", ""), normalize_fts(b).replace(" ", "")).ratio()


def tail_sentence(text):
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if normalize_fts(p)]
    return parts[-1] if parts else ""


def negation_profile(text):
    return tuple(text.count(c) for c in NEGATIONS)


def count_subquestions(text):
    """临时规则（M08-6 子句拆分上线后经 count_fn 注入替换）。"""
    return max(1, sum(1 for p in _SUBQ_SPLIT.split(text) if normalize_fts(p)))


@dataclass
class ReuseDecision:
    reuse: bool
    reason: str          # 'ok' 或第一个被否决的条件
    similarity: float = 0.0

    def __bool__(self):
        return self.reuse


def can_reuse_precheck(partial_hit, committed_text, committed_version=None, committed_turn_id=None,
                       committed_index_version=None, min_similarity=0.9, count_fn=count_subquestions):
    """partial_hit: {'text','text_version','turn_id'?,'index_version'?}（预检索时的文本与版本）。

    1 相似度≥min_similarity；2 commit 文本的尾句已被 partial 文本覆盖；3 否定词一致；
    4 子问题数一致；5 text_version 可追溯（有版本号、不晚于 commit 版本、同 turn、同 index_version）。
    """
    ptext = (partial_hit or {}).get("text") or ""
    sim = similarity(ptext, committed_text) if ptext and committed_text else 0.0
    if sim < min_similarity:
        return ReuseDecision(False, "similarity_below_threshold", sim)
    tail = normalize_fts(tail_sentence(committed_text)).replace(" ", "")
    if not tail or tail not in normalize_fts(ptext).replace(" ", ""):
        return ReuseDecision(False, "tail_not_covered", sim)
    if negation_profile(ptext) != negation_profile(committed_text):
        return ReuseDecision(False, "negation_mismatch", sim)
    if count_fn(ptext) != count_fn(committed_text):
        return ReuseDecision(False, "subquestion_count_mismatch", sim)
    v = partial_hit.get("text_version")
    if not isinstance(v, int) or isinstance(v, bool) or committed_version is None or v > committed_version:
        return ReuseDecision(False, "text_version_untraceable", sim)
    for key, expect in (("turn_id", committed_turn_id), ("index_version", committed_index_version)):
        if expect is not None and partial_hit.get(key) != expect:
            return ReuseDecision(False, "text_version_untraceable", sim)
    return ReuseDecision(True, "ok", sim)
