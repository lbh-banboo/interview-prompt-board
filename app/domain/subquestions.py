"""子问题拆分（M05-6）：一次提问含多个问题时，按句末标点与连接词拆成子问题列表。纯函数，无状态。

只有拆出 >=2 个完整问句/请求才算多子问题；否则原样返回 [text]，保证单问句行为与不拆分时一致。
第一个为主问题，其余为同轮子问题。
"""
from __future__ import annotations

import re

from app.domain.semantic import classify, completeness, core

_SENTENCE_END = re.compile(r'(?<=[？?。！!；;])')
_CONNECTIVE = re.compile(r'[，,]?(?:以及|还有|另外|同时|并且|此外|而且)')


def _is_question(text: str) -> bool:
    return bool(core(text)) and classify(text) in ('question', 'request') and completeness(text)[0] != 'incomplete'


def _split_connective(part: str) -> list[str]:
    """连接词两侧各自都是完整问句才拆（"说说A以及B"这类单个请求带两个宾语不拆）。"""
    for m in _CONNECTIVE.finditer(part):
        left, right = part[:m.start()].strip(' ，,'), part[m.end():].strip(' ，,')
        if _is_question(left) and _is_question(right):
            return [left] + _split_connective(right)
    return [part]


def split_questions(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text or '') if core(p)]
    pieces = [q for p in parts for q in _split_connective(p)]
    questions = [p for p in pieces if _is_question(p)]
    return questions if len(questions) >= 2 else [text]
