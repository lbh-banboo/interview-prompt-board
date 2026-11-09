"""M10-2 / M16 脱敏器（纯函数）：外发联网前去除 PII，并判定该问题是否根本不该外发。

vocab（本场资料词表，调用方传入）：
  - 可迭代 str，或 dict 的 names/companies/projects：命中即从文本删除；
  - dict 的 terms（主库专有词）：命中不删除，直接 blocked；
  - 个人经历意图（“你负责/你们项目”等）也 blocked。
blocked 时 reason='personal_or_private'，调用方不得发起任何外发。
正则不能保证内部知识已脱敏，这是已登记风险。
"""
import logging
import re
from dataclasses import dataclass, field

REASON_BLOCKED = "personal_or_private"
_REMOVE_KEYS = ("names", "companies", "projects")
MIN_ALIAS_LEN = 2  # 单字别名不参与全文替换，避免误删常用字
log = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s]+", re.I)
_URL_USERINFO = re.compile(r"(?<=//)[^/\s@]+@")
_URL_USER_PARAM = re.compile(
    r"([?&])(?:user|uid|userid|user_id|username|account|email|token|access_token|apikey|sid|session)=[^&\s#]*&?", re.I)
_URL_USER_PATH = re.compile(r"(?<=/)((?:u|user|users|people|profile)/)[^/\s?#]+", re.I)

# (kind, pattern)，按顺序执行；长数字先于短数字，避免被拆
DEFAULT_RULES = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("id_card", re.compile(r"(?<![0-9A-Za-z])[1-9]\d{16}[\dXx](?![0-9A-Za-z])")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")),
    ("company", re.compile(
        r"[一-鿿A-Za-z]{2,12}(?:股份有限公司|有限责任公司|有限公司|集团|公司)"
        r"|[A-Z][\w&.-]*(?: [A-Z][\w&.-]*)* (?:Inc|Ltd|LLC|Corp|Co)\b\.?")),
)

DEFAULT_PERSONAL_PATTERNS = (
    r"你们?的?(?:项目|公司|团队|经历|经验|工作|实习|简历|职责|角色|离职|薪资|业务|架构|产品)",
    r"你(?:们)?(?:之前|以前|曾经|当时|有没有|是否)?(?:负责|做过|参与|主导|设计过|实现过|开发过|用过|遇到过|处理过|优化过|带过|写过)",
    r"自我介绍|介绍一下你|讲讲你|说说你|为什么(?:离职|选择我们|想来)",
    r"\b(?:your (?:project|experience|role|resume|team)|have you (?:ever|worked|used|done|built)|tell me about yourself)\b",
)


@dataclass
class RedactResult:
    text: str
    hits: list = field(default_factory=list)  # 每次命中的类别名，不含原文
    blocked: bool = False
    reason: str = ""


def _split_vocab(vocab):
    """拆成 (to_remove, to_block)；仅做去重/去空/长词优先排序。"""
    remove, block = [], []
    if isinstance(vocab, dict):
        for k in _REMOVE_KEYS:
            remove += list(vocab.get(k) or [])
        block = list(vocab.get("terms") or [])
    elif vocab:
        remove = list(vocab)
    clean = lambda ws: sorted({w.strip() for w in ws if w and w.strip()}, key=len, reverse=True)
    return clean(remove), clean(block)


def _clean_url(m, hits):
    url = m.group(0)
    new = _URL_USERINFO.sub("", url)
    new = _URL_USER_PARAM.sub(lambda p: p.group(1) if p.group(0).endswith("&") else "", new)
    new = _URL_USER_PATH.sub(lambda p: p.group(1).rstrip("/"), new).rstrip("?&")
    if new != url:
        hits.append("url_user")
    return new


def redact(text, vocab=None, rules=DEFAULT_RULES, personal_patterns=DEFAULT_PERSONAL_PATTERNS):
    """返回 RedactResult；rules/personal_patterns 可配置。"""
    src = text or ""
    remove, block = _split_vocab(vocab)
    short = [w for w in remove if len(w) < MIN_ALIAS_LEN]
    if short:  # 不记录别名原文，避免把 PII 写进日志
        log.warning("忽略 %d 个单字别名，不参与全文替换", len(short))
        remove = [w for w in remove if len(w) >= MIN_ALIAS_LEN]
    hits = []
    out = _URL.sub(lambda m: _clean_url(m, hits), src)
    for kind, pat in rules:
        out, n = pat.subn(" ", out)
        hits += [kind] * n
    for w in remove:
        out, n = re.subn(re.escape(w), " ", out, flags=re.I)
        hits += ["vocab"] * n
    out = re.sub(r"\s+", " ", out).strip()
    low = src.lower()
    private = (any(w.lower() in low for w in block)
               or any(re.search(p, src, re.I) for p in personal_patterns)
               or (bool(hits) and not out))
    return RedactResult(out, hits, private, REASON_BLOCKED if private else "")
