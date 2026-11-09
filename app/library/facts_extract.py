"""个人事实抽取与同条目出处比对（Q3：按条目约束，禁止跨条目拼接）。"""
import re
import unicodedata

KINDS = ("project", "metric", "role", "time", "org")
RED_FLAGS = ("suspected_fabrication", "cross_entry")

_SENTENCE = re.compile(r"(?<=[。！？；!?;])")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_QUOTED = re.compile(r"《[^》]+》|“[^”]+”|「[^」]+」|\"[^\"]+\"")
_ORG = re.compile(r"[A-Za-z0-9一-鿿]{2,10}(?:公司|集团|银行|研究院|研究所|大学)")
_ORG_LEAD = re.compile(r"^(?:我|曾|现)?(?:就职于|任职于|加入|在|于)")
_ROLE = re.compile(r"(?:负责|主导|担任|带领|牵头|参与)[^，。；,.;！？\s]{2,20}")
_TIME = re.compile(r"\d{4}\s*年(?:\s*\d{1,2}\s*月)?|\d+(?:\.\d+)?\s*年|\d+\s*个月")
_METRIC = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|k|K|人|倍|个(?!月)|台|条|次|毫秒|ms|秒|QPS|TPS|GB|MB|元|家|款|套|名|天|小时|分钟)")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _sentences(item):
    """(行号, 句子)；首行只取“参考回答：”之后的部分。"""
    lines = item['text'].split("\n")
    lines[0] = item['answer']
    for offset, line in enumerate(lines):
        for sentence in _SENTENCE.split(line):
            if sentence.strip():
                yield item['line_start'] + offset, sentence.strip()


def extract_rule_facts(item):
    """规则抽取：每条事实的 source_span 都是条目原文里的整句，行号落在条目范围内。"""
    facts, seen = [], set()
    patterns = (("time", _TIME), ("metric", _METRIC), ("project", _QUOTED), ("org", _ORG), ("role", _ROLE))
    for line, sentence in _sentences(item):
        for kind, pattern in patterns:
            for match in pattern.finditer(sentence):
                text = match.group(0).strip()
                if kind == "org":
                    text = _ORG_LEAD.sub("", text)
                if (kind, text, line) in seen:
                    continue
                seen.add((kind, text, line))
                facts.append(dict(kind=kind, text=text, source_span=sentence, line_start=line,
                                  line_end=line, flag=None, origin="rule"))
    return facts


def _tokens(text, strict):
    """文本里需要出处的个人信息：数字、引号/书名号项目名、机构名；非 strict（提纲/变体）只认带单位的数字，免得误伤“第1点”。"""
    scope = norm(text) if strict else "".join(m.group(0) for p in (_METRIC, _TIME) for m in p.finditer(norm(text)))
    found = [("number", n) for n in _NUMBER.findall(scope)]
    found += [("name", m[1:-1]) for m in _QUOTED.findall(text)]
    found += [("name", _ORG_LEAD.sub("", m)) for m in _ORG.findall(text)]
    return found


def locate(token, own, others):
    """返回 None（同条目内找到）/'cross_entry'（只在别的条目）/'suspected_fabrication'。"""
    kind, value = token
    if kind == "number":
        in_text = lambda t: value in _NUMBER.findall(t)  # noqa: E731
    else:
        value = norm(value)
        in_text = lambda t: value in t  # noqa: E731
    if in_text(own):
        return None
    return "cross_entry" if any(in_text(t) for t in others) else "suspected_fabrication"


def check_texts(texts, own, others, strict=True):
    """逐段检查 [(where, text)]，own/others 为已 norm 的原文；返回红色发现列表。"""
    findings = []
    for where, text in texts:
        for token in _tokens(text, strict):
            flag = locate(token, own, others)
            if flag and not any(f['flag'] == flag and f['token'] == token[1] and f['where'] == where for f in findings):
                findings.append(dict(flag=flag, token=token[1], where=where))
    return findings


def check_model_fact(fact, own, others):
    """模型事实：数字/名称必须在本条目；project/org/role 的原文片段（span，缺省取 text）必须逐字在本条目。"""
    texts = [("facts", fact['text'])]
    span = fact.get('source_span') or ""
    anchor = norm(span or fact['text'])
    flag = None
    if span:
        texts.append(("facts", span))
    if fact['kind'] in ("project", "org", "role") and anchor not in own:
        flag = "cross_entry" if any(anchor in t for t in others) else "suspected_fabrication"
    for finding in check_texts(texts, own, others):
        flag = flag or finding['flag']
    return flag
