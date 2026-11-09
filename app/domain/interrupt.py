"""打断分类（M05-5）：提问方在回答中开口时，先分类再决定是否关闭回答。纯函数 + 规则，无状态、无模型。

kind:
- backchannel：<backchannel_max_ms 且为"嗯/对/好的/OK"等；
- followup：追问/纠正，与当前问题字符 n-gram 相似度 >= sim_followup，或含"那…呢/具体说说/展开讲讲/我是说"等；
- switch：含终止语（先停一下/换个问题/先不说这个…）且去掉终止语后是完整新问题；
- new_question：完整成问、相似度低、无终止语（"主题相似度低"不等于明确改题）；
- unknown：其余（含只有终止语还没说出新问题，stop=True）。
"完整成问" = classify 为 question/request 且完整度非 incomplete。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.semantic import classify, completeness, core

STOP_RE = re.compile(
    r'先停一下|先停下|先停停|先不说这个|先不说了|先不聊这个|先不讲这个|先别说这个|换个问题|换一个问题|换个话题|换一个话题|'
    r'这个先跳过|这个问题先跳过|不说这个了|我们换一下|问下一个问题|问下一题')
_FOLLOWUP_RE = re.compile(
    r'那.{1,24}呢|具体(?:说说|讲讲|聊聊|展开)|展开(?:讲讲|说说|聊聊|讲一下|说一下)|详细(?:说说|讲讲|讲一下|说一下)|'
    r'再(?:具体|详细)|继续(?:说|讲)|不对|我是说|我的意思是|更正一下')
_NOISE_CHARS = frozenset('的了是吗呢么吧啊嘛你我他请说讲谈聊一下个')


@dataclass(frozen=True)
class InterruptResult:
    kind: str  # backchannel | followup | switch | new_question | unknown
    similarity: float
    reason: str
    stop: bool = False  # 含终止语
    level: str = ''  # 去掉终止语后的完整度：high / low / incomplete；无可评估文本为 ''


def _bigrams(text: str) -> set[str]:
    chars = ''.join(ch for ch in core(text) if ch not in _NOISE_CHARS)
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def similarity(a: str, b: str) -> float:
    """字符 bigram 重合度 = |A∩B| / min(|A|,|B|)（去掉疑问/语气字，避免它们抬高相似度）。"""
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / min(len(ga), len(gb)) if ga and gb else 0.0


def has_stop_phrase(text: str) -> bool:
    return bool(STOP_RE.search(text or ''))


def classify_interrupt(text: str, duration_ms: int, current_question: str, *,
                       sim_followup: float = 0.5, backchannel_max_ms: int = 1000) -> InterruptResult:
    if not core(text):
        return InterruptResult('unknown', 0.0, 'empty')
    kind = classify(text)
    if kind == 'backchannel':
        if duration_ms < backchannel_max_ms:
            return InterruptResult('backchannel', 0.0, 'short_backchannel')
        return InterruptResult('unknown', 0.0, 'long_backchannel')
    stop = has_stop_phrase(text)
    rest = STOP_RE.sub('', text).strip(' ，,。.、')
    has_rest = bool(core(rest))
    level = completeness(rest)[0] if has_rest else ''
    asks = has_rest and classify(rest) in ('question', 'request') and level != 'incomplete'
    sim = similarity(rest if has_rest else text, current_question)
    if stop:
        if asks:
            return InterruptResult('switch', sim, 'stop_phrase+new_question', True, level)
        return InterruptResult('unknown', sim, 'stop_phrase_only', True, level)
    if _FOLLOWUP_RE.search(text) or sim >= sim_followup:
        return InterruptResult('followup', sim, 'marker' if _FOLLOWUP_RE.search(text) else 'similar', False, level)
    if asks:
        return InterruptResult('new_question', sim, 'complete_question', False, level)
    return InterruptResult('unknown', sim, 'unclear', False, level)
