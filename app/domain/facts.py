"""M09 事实校验器（项目红线：虚构或串题 1 次即 FAIL；规划 M09 §5、已确认 Q3 按条目约束）。

规则
- 个人事实块（kind=personal）：每句必须声明 entry_id，且该条目存在、已审核、属于主库（tier=primary）；
  句中的数字 / 带后缀的项目名·公司名·职位 / 英文技术名词，必须全部出现在**同一条**条目的原文或 facts 里。
  任何一项找不到（含“出现在别的条目里”）-> 整块丢弃并替换为“[需本人补充]”。禁止跨条目拼接。
- 公共知识块（kind=public）：不得第一人称；传入 public_corpus（引用片段）时，块中的数字/百分比必须能在片段里找到，
  否则整块丢弃（宁丢不拼）；数字不对照个人 facts，未传 public_corpus 时不校验数字（兼容只用个人事实的调用）。
- 本人现场口述（spoken）只作一致性线索：返回 Hint（verified=False），绝不进入可信语料，
  口述里才有的数字写进个人块照样被拦。

已知风险（登记，未解决）
- R10 同条目内关系错配：条目 A 同时含“项目甲 提升 30%”与“项目乙 提升 10%”，模型把 30% 配给项目乙，
  数字与名称都来自 A，本校验器拦不住（关系级建模放 P2 以后，R05）。
- 公司名/项目名靠后缀（项目/公司/系统/平台/职位等）与英文词识别，无后缀的中文专名（如“腾讯”）识别不到。
- 通用指代（“这个项目”“作为工程师”）按前一字跳过；识别宁可误拦（回到“[需本人补充]”）不放过。
"""
import re
from dataclasses import dataclass, field

PLACEHOLDER = '[需本人补充]'
FIRST_PERSON = re.compile(r'我(?!国)|咱|本人|俺')
_ARABIC = re.compile(r'\d+(?:\.\d+)?')
_CNUM = '零一二三四五六七八九十百千两'
_CN = re.compile(rf'(百分之)([{_CNUM}]+)|()([{_CNUM}]+)(?=%|年|个月|个|人|倍|成|万|亿|次|周|天|台|套|家|条|级)')
_SUFFIX = re.compile(r'项目|公司|集团|系统|平台|科技|银行|大学|工程师|经理|总监|架构师|负责人|主管|组长')
_GENERIC_PREV = set('个该本此的了过某每整各新做搞发与责导带是在和及为这那、，, ')
_LATIN = re.compile(r'[A-Za-z][A-Za-z0-9+#.]{1,}')
_SPLIT = re.compile(r'(?<=[。！？；!?;])')
_DIGIT = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


@dataclass(frozen=True)
class ApprovedEntry:
    entry_id: str
    text: str
    facts: tuple = ()
    tier: str = 'primary'  # secondary（次优库）不得支撑第一人称事实


@dataclass(frozen=True)
class Sentence:
    text: str
    entry_id: str | None = None


@dataclass(frozen=True)
class Block:
    kind: str  # personal | public | placeholder
    sentences: tuple

    @property
    def text(self):
        return ''.join(s.text for s in self.sentences)

    @classmethod
    def personal(cls, entry_id, text):
        parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
        return cls('personal', tuple(Sentence(p, entry_id) for p in parts))

    @classmethod
    def public(cls, text):
        return cls('public', (Sentence(text),))


@dataclass(frozen=True)
class Reason:
    block_index: int
    code: str
    detail: str = ''


@dataclass(frozen=True)
class Hint:
    text: str
    spoken: str
    entry_id: str
    verified: bool = False  # 口述永远是未审核


@dataclass
class ValidationResult:
    ok: bool
    dropped_blocks: list
    reasons: list
    blocks: list = field(default_factory=list)  # 可推送的块；被丢的个人块已换成占位
    hints: list = field(default_factory=list)


def _cn_value(s):
    if '百' not in s and '千' not in s and '十' not in s:
        return ''.join(str(_DIGIT.get(c, 0)) for c in s)
    total, cur = 0, 0
    for c in s:
        if c in _DIGIT:
            cur = _DIGIT[c]
        else:
            unit = {'十': 10, '百': 100, '千': 1000}[c]
            total += (cur or 1) * unit
            cur = 0
    return str(total + cur)


def numbers(text):
    """返回 {(数值字符串, 是否百分比)}；中文数字折算成阿拉伯数字。"""
    text = re.sub(r'(?<=\d),(?=\d{3})', '', text)
    out = set()
    for m in _ARABIC.finditer(text):
        out.add((m.group(), text[m.end():m.end() + 1] in ('%', '％')))
    for m in _CN.finditer(text):
        pct, num = m.group(1), m.group(2) or m.group(4)
        if num == '一' and text[m.end():m.end() + 1] == '个' and text[m.end():m.end() + 2] != '个月':
            continue
        out.add((_cn_value(num), bool(pct)))
    return out


def _fact_text(f):
    return f.get('text', '') if isinstance(f, dict) else str(f)


def _corpus(entry):
    return re.sub(r'\s+', '', entry.text + '\n' + '\n'.join(_fact_text(f) for f in entry.facts))


def _missing(sentence, corpus):
    """句中不在 corpus 里的受保护信息；空列表=通过。"""
    s = re.sub(r'\s+', '', sentence)
    bad = []
    cn = {(v, p) for v, p in numbers(corpus)}
    cvals = {v for v, _ in cn}
    for v, pct in numbers(s):
        if v not in cvals or (pct and (v, True) not in cn):
            bad.append(('number', v + ('%' if pct else '')))
    for m in _SUFFIX.finditer(s):
        pre = s[max(0, m.start() - 8):m.start()]
        if len(pre) < 2 or pre[-1] in _GENERIC_PREV:
            continue
        if not any(pre[-k:] + m.group() in corpus for k in range(2, len(pre) + 1)):
            bad.append(('name', pre[-2:] + m.group()))
    low = corpus.lower()
    for w in _LATIN.findall(s):
        if w.lower() not in low:
            bad.append(('term', w))
    return bad


def _topic(s):
    t = re.sub(r'[\d\W]', '', s)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _public_missing_numbers(text, corpus):
    """公共块中的数字/百分比必须能在引用片段里找到；返回找不到的列表。"""
    cn = set(numbers(corpus))
    cvals = {v for v, _ in cn}
    return [v + ('%' if pct else '') for v, pct in numbers(text)
            if v not in cvals or (pct and (v, True) not in cn)]


def validate(blocks, entries, spoken=(), public_corpus=None):
    """校验模型生成的内容块。entries: ApprovedEntry 可迭代（仅已审核）；spoken: 本人现场口述文本（仅作线索）。
    public_corpus: 公共块可引用的检索片段文本（传入时启用“公共块数字必须在片段中”的校验）。"""
    by_id = {e.entry_id: e for e in entries}
    corpora = {}
    dropped, reasons, out, hints = [], [], [], []
    seen_hint = set()
    spoken_nums = set()
    for u in spoken:
        spoken_nums |= numbers(u)
    public_text = '' if public_corpus is None else str(public_corpus)

    for i, b in enumerate(blocks):
        if b.kind == 'public':
            if FIRST_PERSON.search(b.text):
                dropped.append(i)
                reasons.append(Reason(i, 'public_first_person'))
                continue
            bad = [] if public_corpus is None else _public_missing_numbers(b.text, public_text)
            if bad:
                dropped.append(i)
                reasons.append(Reason(i, 'public_number_not_in_fragment', bad[0]))
                continue
            out.append(b)
            continue
        if b.kind != 'personal':
            dropped.append(i)
            reasons.append(Reason(i, 'bad_kind', b.kind))
            continue
        why = _check_personal(i, b, by_id, corpora, spoken_nums)
        if why:
            dropped.append(i)
            reasons.append(why)
            out.append(Block('placeholder', (Sentence(PLACEHOLDER),)))
            continue
        out.append(b)
        for s in b.sentences:
            ns = numbers(s.text)
            for u in spoken:
                nu = numbers(u)
                key = (u, s.entry_id)
                if ns and nu and ns.isdisjoint(nu) and len(_topic(s.text) & _topic(u)) >= 3 and key not in seen_hint:
                    seen_hint.add(key)
                    hints.append(Hint(f'与你刚才说的「{u}」不一致', u, s.entry_id))
    return ValidationResult(not dropped, dropped, reasons, out, hints)


def _check_personal(i, b, by_id, corpora, spoken_nums):
    if not b.sentences:
        return Reason(i, 'empty_block')
    for s in b.sentences:
        if not s.entry_id:
            return Reason(i, 'missing_entry_id', s.text[:20])
        e = by_id.get(s.entry_id)
        if e is None:
            return Reason(i, 'unknown_entry_id', s.entry_id)
        if e.tier != 'primary':
            return Reason(i, 'secondary_entry', s.entry_id)
        corpus = corpora.setdefault(e.entry_id, _corpus(e))
        bad = _missing(s.text, corpus)
        if bad:
            kind, val = bad[0]
            only_spoken = kind == 'number' and any(v == val.rstrip('%') for v, _ in spoken_nums)
            return Reason(i, 'spoken_only' if only_spoken else f'{kind}_not_in_entry', val)
    return None
