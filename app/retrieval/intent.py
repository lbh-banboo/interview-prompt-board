"""M08-4 意图分类：个人经历 / 知识概念 / 观点判断 / 行为情景 / 寒暄流程。规则+关键词，纯函数。

优先级：process > personal > opinion > behavior > knowledge（兜底）。
个人经历优先于行为情景：宁可不联网、请本人补充，也不让个人事实被通用答案顶替。
"""
import re
from dataclasses import dataclass, field

from ..domain.semantic import classify

PERSONAL, KNOWLEDGE, OPINION, BEHAVIOR, PROCESS = "personal", "knowledge", "opinion", "behavior", "process"

# 保守优先：第一/第二人称 + 经历类标记 -> PERSONAL（优先于 OPINION/KNOWLEDGE）。
# 判据是“是否指向本人经历”：不指向本人经历的知识/观点题（如“你怎么看待 RAG 的局限”）
# 仍走 KNOWLEDGE/OPINION，避免过度收紧。
_PRONOUN = r"(?:你|您|我|我们|咱们|本人)"
_SELF_EXPERIENCE = re.compile(
    rf"(?:你的|您的|我的|我们的|咱们的|你们的)(?:项目|团队|公司|经历|工作|职责|角色|岗位|职位|产品|系统|业务|职业|背景|优缺点)"
    rf"|(?:你们|我们|咱们)(?:的)?(?:项目|团队|公司|产品|系统|业务)"
    rf"|{_PRONOUN}.{{0,8}}(?:做过|负责|参与|主导|带过|管理过|带领|开发过|设计过|优化过|解决过|处理过|经历过|工作过|任职|入职|离职|离开|辞职|跳槽)"
    rf"|{_PRONOUN}.{{0,8}}(?:优缺|优点|缺点|成就|失败|业绩|贡献|成长|收获)"
    rf"|{_PRONOUN}.{{0,12}}(?:职业经历|职业规划|上一家|上一份|前一家)"
    rf"|{_PRONOUN}在.{{0,12}}(?:项目|公司|团队|岗位|阶段|上一家)"
    rf"|为什么.{{0,10}}{_PRONOUN}.{{0,10}}(?:离开|离职|辞职|跳槽|换工作)"
)
_SELF_EXTRA = re.compile(r"自我介绍|做过(的)?(最|哪)")

_RULES = [
    (PERSONAL, re.compile(r"你们?(的)?(项目|团队|公司|产品|系统|业务)|你(负责|做过|参与|主导|经历|带过|开发过|设计过|优化过)|"
                          r"你的(项目|经历|职责|工作|角色)|你在.{0,10}(项目|公司|团队|岗位|阶段)|自我介绍|做过(的)?(最|哪)")),
    (OPINION, re.compile(r"如何看待|怎么看|你觉得|你认为|你怎么评价|你的看法|你的观点|有什么看法|是否合理|值不值得")),
    (BEHAVIOR, re.compile(r"举(个|一个|几个)?(例子|案例)|遇到过|碰到过|如果.{0,30}你(会|将|要)|假如.{0,30}你|"
                          r"当时(是)?怎么(处理|解决|做)|怎么(处理|应对)(冲突|分歧|压力)|有没有(过)?.{0,8}(经历|经验)")),
    (KNOWLEDGE, re.compile(r"什么是|是什么|原理|区别|不同|对比|怎么实现|如何实现|怎样实现|为什么|如何|怎么|有哪些|优缺点|[A-Za-z]{2,}")),
]


@dataclass
class Intent:
    kind: str
    signals: list = field(default_factory=list)


def classify_intent(text):
    """单个（子）问题的意图。signals 为命中的关键词片段，兜底为 default。"""
    t = (text or "").strip()
    if classify(t) in ("process", "backchannel"):
        return Intent(PROCESS, ["process"])
    sig = [m.group(0) for m in _SELF_EXPERIENCE.finditer(t)] + [m.group(0) for m in _SELF_EXTRA.finditer(t)]
    if sig:
        return Intent(PERSONAL, sig)
    for kind, rx in _RULES:
        sig = [m.group(0) for m in rx.finditer(t)]
        if sig:
            return Intent(kind, sig)
    return Intent(KNOWLEDGE, ["default"])
