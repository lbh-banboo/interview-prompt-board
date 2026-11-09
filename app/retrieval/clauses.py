"""M08-6 中文子句拆分与覆盖汇总。

拆分点：问号/句号/感叹号/分号/换行，以及连接词“以及/还有/另外/然后/同时”。
“A 和 B 的区别”中的“和/与/跟”不拆；“还有什么/还有哪些”等疑问内部的“还有”不拆。
"""
import re
from dataclasses import dataclass, field

_SEP = re.compile(r"[？?。！!；;\n]+|[，,、]?\s*(?:以及|另外|然后|同时|还有(?!什么|哪些|没有|吗|其他|别的|其它))")
_PUNCT = "，。、？！；：,.?!;:~～… \t\n"


@dataclass
class Clause:
    text: str
    start: int   # 原文区间 [start, end)
    end: int


@dataclass
class Coverage:
    covered: list = field(default_factory=list)
    pending: list = field(default_factory=list)

    @property
    def partial(self):
        return bool(self.covered) and bool(self.pending)


def split_clauses(text):
    text = text or ""
    out, pos = [], 0
    for m in list(_SEP.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        seg = text[pos:end]
        lead = len(seg) - len(seg.lstrip(_PUNCT))
        body = seg.strip(_PUNCT)
        if len(body) >= 2:
            out.append(Clause(body, pos + lead, pos + lead + len(body)))
        if m:
            pos = m.end()
    return out


def merge_coverage(sub_results):
    """sub_results: 带 .clause 与 .covered 的对象序列；已跳过（.skipped）的不计入。"""
    cov = Coverage()
    for s in sub_results:
        if getattr(s, "skipped", False):
            continue
        (cov.covered if s.covered else cov.pending).append(s.clause)
    return cov
