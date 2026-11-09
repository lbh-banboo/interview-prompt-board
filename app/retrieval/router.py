"""M08-4 三级路由器：按子问题分别分类、检索、分流，汇总为 RouteDecision。

级别（sub.level / decision.level）：
  L1 主库命中；L1c 高分低 margin 并列候选 / 无关键词证据的弱匹配（均不当标准答案）；
  L1p 真实部分覆盖（cos 在 tau_mid~tau_cos 之间但有关键词证据，仅展示命中部分，非标准答案）；
  L2 次优库命中（通用建议）；L3 联网（仅知识/观点类）；personal_miss 个人/行为类未命中（诚实框架+需本人补充，绝不联网）；
  miss 无可用结果且不联网；skip 流程性发言（不检索不提示）。
个人经历与行为情景子问只走 L1，永不走 L2/L3；检索范围 = 本场主库 + roles 并集的次优库。
本模块不联网、不生成答案，只给出路由结论与证据。
"""
from dataclasses import dataclass, field
from typing import Protocol

from .clauses import merge_coverage, split_clauses, Clause
from .intent import BEHAVIOR, PERSONAL, PROCESS, classify_intent
from .search import L1_HIT, L1_PARTIAL, L1_TIE, Thresholds, classify_hit, search
from .tags import SOURCE_GENERAL, SOURCE_MINE, SOURCE_PARTIAL

PERSONAL_NOTE = "个人经历类问题，库内无已审核口径，请按真实经历回答；缺失处需本人补充"
TAG_L1, TAG_L1C, TAG_L1P = SOURCE_MINE, "候选，未确认", SOURCE_PARTIAL
TAG_L2, TAG_L3, TAG_PENDING = SOURCE_GENERAL, "暂无标准答案，正在补充", "待补充"
_PRECEDENCE = ["L1c", "L2", "L3", "personal_miss", "miss"]


class SecondaryIndex(Protocol):
    def search(self, query, roles):
        """返回按 cos 降序的命中列表，元素需有 entry_id 与 cos 属性。"""


@dataclass
class SubDecision:
    clause: Clause
    intent: object
    level: str = "miss"
    tag: str = TAG_PENDING
    entry_id: str = None            # L1/L1p/L2 展示的条目
    candidates: list = field(default_factory=list)   # L1c：[(entry_id, cos)]
    evidence: dict = field(default_factory=dict)
    followup: str = None            # L1c/L1p 未覆盖部分的后续：'L2' | 'L3' | 'personal_miss' | None
    secondary: dict = None          # 次优库命中证据
    need_web: bool = False
    personal_note: str = None

    @property
    def skipped(self):
        return self.level == "skip"

    @property
    def covered(self):
        return self.level in ("L1", "L2")


@dataclass
class RouteDecision:
    level: str
    subs: list
    need_web: bool
    personal_note: str
    pending: list                   # 未覆盖子句（待补充）
    covered: list
    web_queries: list               # 需联网的子句文本
    index_version: str = ""
    ctx: dict = field(default_factory=dict)

    @property
    def partial(self):
        return bool(self.covered) and bool(self.pending)


class Router:
    def __init__(self, primary_index, secondary=None, thresholds=None, tau2=None, search_fn=search):
        self.primary, self.secondary = primary_index, secondary
        self.th = thresholds or Thresholds()
        self.tau2 = self.th.tau_cos if tau2 is None else tau2
        self.search_fn = search_fn

    def route(self, text, roles, now_ctx=None):
        clauses = split_clauses(text) or ([Clause(text.strip(), 0, len(text))] if (text or "").strip() else [])
        subs = [self._sub(c, roles) for c in clauses]
        cov = merge_coverage(subs)
        live = [s for s in subs if not s.skipped]
        web = [s.clause.text for s in live if s.need_web]
        note = PERSONAL_NOTE if any(s.personal_note for s in live) else None
        return RouteDecision(self._level(live, cov), subs, bool(web), note, cov.pending, cov.covered, web,
                             self.primary.index_version, dict(now_ctx or {}))

    @staticmethod
    def _level(live, cov):
        if not live:
            return "skip"
        if not cov.pending:
            return "L1" if all(s.level == "L1" for s in live) else "L2"
        levels = {s.level for s in live}
        if "L1p" in levels or "L1" in levels:
            return "L1p"
        return next(lv for lv in _PRECEDENCE if lv in levels)

    def _sub(self, clause, roles):
        intent = classify_intent(clause.text)
        sub = SubDecision(clause, intent)
        if intent.kind == PROCESS:
            sub.level, sub.tag = "skip", ""
            return sub
        res = self.search_fn(self.primary, clause.text)
        cls = classify_hit(res, self.th)
        by_cos = sorted(res.hits, key=lambda h: (-h.cos, h.entry_id))
        sub.evidence = dict(label=cls.label, cos_top1=res.cos_top1, cos_top2=res.cos_top2, top1_entry=res.top1_entry,
                            cos_ok=cls.cos_ok, keyword_ok=cls.keyword_ok, margin_ok=cls.margin_ok,
                            reasons=list(cls.reasons), keyword_mode=res.keyword_mode)
        if cls.label == L1_HIT:
            sub.level, sub.tag, sub.entry_id = "L1", TAG_L1, res.top1_entry
            return sub
        if cls.label == L1_TIE:
            sub.level, sub.tag = "L1c", TAG_L1C
            sub.candidates = [(h.entry_id, h.cos) for h in by_cos if h.cos >= self.th.tau_mid][:2]
        elif cls.label == L1_PARTIAL and cls.weak:
            # 弱匹配（无关键词证据）不作主答案，降为候选/补充；真实部分覆盖仍为 L1p
            sub.level, sub.tag = "L1c", TAG_L1C
            sub.candidates = [(h.entry_id, h.cos) for h in by_cos if h.cos >= self.th.tau_mid][:2]
        elif cls.label == L1_PARTIAL:
            sub.level, sub.tag, sub.entry_id = "L1p", TAG_L1P, res.top1_entry
        return self._fallback(sub, clause.text, roles)

    def _fallback(self, sub, query, roles):
        """主库未能给出标准答案后的去向：个人/行为只请本人补充；知识/观点先 L2 再 L3。"""
        kind = sub.intent.kind
        if kind in (PERSONAL, BEHAVIOR):
            sub.followup, sub.personal_note = "personal_miss", PERSONAL_NOTE
            if sub.level == "miss":
                sub.level, sub.tag = "personal_miss", TAG_PENDING
            return sub
        hits = self.secondary.search(query, roles) if self.secondary is not None else []
        top = hits[0] if hits else None
        if top is not None and top.cos >= self.tau2:
            sub.secondary = dict(entry_id=top.entry_id, cos=top.cos)
            if sub.level == "miss":
                sub.level, sub.tag, sub.entry_id = "L2", TAG_L2, top.entry_id
            else:
                sub.followup = "L2"
            return sub
        sub.need_web = True  # 走到这里只剩知识/观点类
        sub.followup = "L3"
        if sub.level == "miss":
            sub.level, sub.tag = "L3", TAG_L3
        return sub
