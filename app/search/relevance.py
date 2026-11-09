"""M10-4 有效要点判定与排序：有来源 URL、相关度 ≥ 阈值、非广告/空内容；同域名合并，权威来源优先。

短专名/人名查询（2~4 字且无知识词）额外要求标题或摘要出现“完整实体串”：
实体串必须是独立词块，不能只是更长专名（如“莎莎国际”）的前缀；混合查询
（实体 + 知识词）要求同时保留实体串并命中知识词。被丢弃的条数按 reason
累计到模块级计数器（stats_snapshot()），供 health/metrics 读取。

下列阈值与规则均为经验值【待标定】，需用真实检索样本校准。
"""
import re
import threading
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

from app.retrieval.ngram import grams

SOURCE_TAG = "外部参考"
DEFAULT_THRESHOLD = 0.3  # 【待标定】非短专名查询的最低字符二元组覆盖率
MIN_SNIPPET_LEN = 10
ENTITY_MIN_LEN = 2  # 【待标定】短专名/人名长度下限
ENTITY_MAX_LEN = 4  # 【待标定】短专名/人名长度上限
DEFAULT_AUTHORITY = (
    "wikipedia.org", "baike.baidu.com", "developer.mozilla.org", "docs.python.org",
    "rfc-editor.org", "ietf.org", "w3.org", "iso.org", "readthedocs.io",
    "kubernetes.io", "redis.io", "postgresql.org", "mysql.com", "oracle.com",
)
AD_PATTERN = re.compile(r"广告|推广|赞助|优惠券|立即购买|点击领取|加微信|sponsored|\bads?\b", re.I)

# 【待标定】知识词/知识问法：命中即视为“知识查询”而非纯专名；用于混合查询判定。
KNOWLEDGE_PATTERN = re.compile(
    r"什么是|是什么|原理|区别|不同|对比|怎么实现|如何实现|怎样实现|为什么|如何|怎么|怎样|"
    r"有哪些|哪几种|几种|优缺点|定义|概念|机制|流程|架构|方案|实现|作用|意义|特点|优势|劣势|"
    r"方式|方法|步骤|商业模式|营收|收入|盈利|利润|财报|股价|市值"
)
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z]{2,}|\d+")
# 混合查询里要求“来源确实讲到”的内容词；疑问句式（是什么/如何/为什么）不算，
# 否则会把“张三丰 是谁”这类查询误判成缺少知识词。【待标定】
CONTENT_KNOWLEDGE_PATTERN = re.compile(
    r"原理|区别|不同|对比|优缺点|定义|概念|机制|流程|架构|方案|实现|作用|意义|特点|优势|劣势|"
    r"方式|方法|步骤|商业模式|营收|收入|盈利|利润|财报|股价|市值"
)
_CJK = re.compile(r"[一-鿿]")
_CJK_RUN = re.compile(r"[一-鿿]+")

# 实体词边界：这些字说明相邻的是语法成分而非专名的一部分。
# 【待标定】边界字表需按真实语料扩充。
_BOUNDARY_CHARS = set("的了是在和与及或有就都也很把被让给对从到向为以而且但所以如果那么这那哪个我你他她它它们等吗呢吧啊呀么之其"
                      "讲说介谈分认表称接告回答访赴出做用带管负主创推提")
# 公司/机构常见后缀：仅当查询实体本身 ≥3 字时允许“实体 + 后缀”视为同一实体，
# 避免把 2 字人名（如“莎莎”）误配到“莎莎国际”。【待标定】
_CORP_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "控股", "集团", "股份", "公司",
    "科技", "技术", "银行", "大学", "学院", "研究院", "事务所",
)
_SHORT_CORP_SUFFIXES = {"股份有限公司", "有限责任公司", "有限公司", "控股", "集团", "股份", "公司"}
_LEADING_ENTITY = re.compile(r"^\s*([一-鿿]{2,4})(?=[\s的:：，,、·]|$)")

_STATS = Counter()
_REASONS = Counter()
_STATS_LOCK = threading.Lock()


@dataclass
class Point:
    title: str
    url: str
    snippet: str
    score: float
    domain: str
    authoritative: bool
    source_tag: str = SOURCE_TAG


def overlap_score(question, text):
    """问题字符二元组被来源文本覆盖的比例。"""
    q = grams(question)
    return len(q & grams(text)) / len(q) if q else 0.0


def domain_of(url):
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    return u.hostname.lower().removeprefix("www.")


def is_authoritative(domain, authority=DEFAULT_AUTHORITY):
    return domain.startswith(("docs.", "developer.")) or any(
        domain == a or domain.endswith("." + a) for a in authority)


def _compact(text):
    return re.sub(r"\s+", "", text or "")


def _is_short_entity_query(question):
    """2~4 个汉字、不含知识词：按纯专名/人名处理。【待标定】"""
    q = _compact(question)
    return (ENTITY_MIN_LEN <= len(q) <= ENTITY_MAX_LEN and bool(_CJK_RUN.fullmatch(q))
            and not KNOWLEDGE_PATTERN.search(q))


def _leading_entity(question):
    """混合查询的前导实体：查询开头 2~4 个汉字，且本身不是知识词。【待标定】"""
    m = _LEADING_ENTITY.match(question or "")
    if not m or KNOWLEDGE_PATTERN.search(m.group(1)):
        return ""
    return m.group(1)


def _knowledge_terms(text):
    return set(CONTENT_KNOWLEDGE_PATTERN.findall(text or "")) | set(_LATIN_OR_DIGIT.findall(text or ""))


def _entity_present(text, name):
    """实体串是否作为独立词块出现，而不是更长专名的前缀/后缀。

    允许的邻接：文本边界、非汉字、语法边界字、以及（查询 ≥3 字时）公司/机构后缀。
    """
    hay, needle = _compact(text), _compact(name)
    if not needle:
        return False
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        left = hay[i - 1] if i else ""
        j = i + len(needle)
        right = hay[j] if j < len(hay) else ""
        left_ok = not left or not _CJK.fullmatch(left) or left in _BOUNDARY_CHARS
        right_ok = (not right or not _CJK.fullmatch(right) or right in _BOUNDARY_CHARS
                    or _corp_suffix_ok(hay[j:], needle))
        if left_ok and right_ok:
            return True
        start = i + 1


def _corp_suffix_ok(tail, name):
    if len(name) < 3:
        return any(tail.startswith(s) for s in _SHORT_CORP_SUFFIXES)
    return any(tail.startswith(s) for s in _CORP_SUFFIXES)


def _entity_gate(question, text):
    """短专名/混合查询的实体规则。返回 (True, '')、(False, reason) 或 (None, '') 表示不适用。"""
    if _is_short_entity_query(question):
        return _entity_present(text, _compact(question)), "entity_mismatch"
    entity = _leading_entity(question)
    if not entity:
        return None, ""
    terms = _knowledge_terms((question or "")[question.find(entity) + len(entity):])
    if not terms:  # 没有知识词：不是混合查询，沿用通用相关度
        return None, ""
    if not _entity_present(text, entity):
        return False, "entity_mismatch"
    low = text.lower()
    if not any(term.lower() in low for term in terms):
        return False, "knowledge_mismatch"
    return True, ""


def _record(reason=None):
    with _STATS_LOCK:
        _STATS["evaluated"] += 1
        if reason is None:
            _STATS["kept"] += 1
        else:
            _STATS["dropped"] += 1
            _REASONS[reason] += 1


def stats_snapshot():
    """模块级过滤计数（供 health/metrics 读取）；reason 为丢弃原因。"""
    with _STATS_LOCK:
        return dict(evaluated=_STATS["evaluated"], kept=_STATS["kept"],
                    dropped=_STATS["dropped"], reasons=dict(_REASONS))


def reset_stats():
    with _STATS_LOCK:
        _STATS.clear()
        _REASONS.clear()


def evaluate_detail(question, hit, *, threshold=DEFAULT_THRESHOLD, scorer=overlap_score,
                    authority=DEFAULT_AUTHORITY):
    """单条判定：返回 (Point, '') 或 (None, reason)。hit 为 SearchHit 或 dict。"""
    get = hit.get if isinstance(hit, dict) else lambda k, d="": getattr(hit, k, d)
    title, url, snippet = (str(get(k, "") or "").strip() for k in ("title", "url", "snippet"))
    domain = domain_of(url)
    if not domain:
        _record("no_url")
        return None, "no_url"
    if len(snippet) < MIN_SNIPPET_LEN:
        _record("snippet_too_short")
        return None, "snippet_too_short"
    if AD_PATTERN.search(title + " " + snippet):
        _record("ad")
        return None, "ad"
    text = title + " " + snippet
    ok, reason = _entity_gate(question, text)
    if ok is False:
        _record(reason)
        return None, reason
    score = scorer(question, text)
    if score < threshold:
        _record("low_relevance")
        return None, "low_relevance"
    _record()
    return Point(title, url, snippet, score, domain, is_authoritative(domain, authority)), ""


def evaluate(question, hit, *, threshold=DEFAULT_THRESHOLD, scorer=overlap_score, authority=DEFAULT_AUTHORITY):
    """单条判定：有效返回 Point，否则 None。"""
    return evaluate_detail(question, hit, threshold=threshold, scorer=scorer, authority=authority)[0]


def select_points(question, hits, *, threshold=DEFAULT_THRESHOLD, scorer=overlap_score,
                  authority=DEFAULT_AUTHORITY, stats=None):
    """过滤 + 同域名合并（留相关度最高者）+ 权威优先、相关度降序。

    stats 传入可变 dict 时写入本次调用的 total/kept/dropped/reasons/merged_domain，
    供调用方转成 health/metrics 事件。
    """
    stats = stats if stats is not None else {}
    stats.setdefault("reasons", {})
    stats["total"] = stats.get("total", 0) + len(hits)
    stats.setdefault("kept", 0)
    stats.setdefault("dropped", 0)
    stats.setdefault("merged_domain", 0)
    best = {}
    valid = 0
    for h in hits:
        p, reason = evaluate_detail(question, h, threshold=threshold, scorer=scorer, authority=authority)
        if p is None:
            stats["dropped"] += 1
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            continue
        valid += 1
        if p.domain not in best or p.score > best[p.domain].score:
            best[p.domain] = p
    stats["kept"] = len(best)
    stats["merged_domain"] = valid - len(best)
    return sorted(best.values(), key=lambda p: (not p.authoritative, -p.score))
