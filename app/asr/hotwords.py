"""热词提取与下发（M04-4）：从主库条目文本提取技术术语/项目名，格式化为阿里热词参数。

只做规则提取（无分词依赖）：
  1. 英文技术词：含大写字母、数字或 +/# 的英文词（Kafka、Redis、MySQL、C++、HTTP2），及其后紧跟的常见中文后缀（B+树）
  2. 书名号/引号内的专名
  3. 高频中文片段：按标点与虚词切分后出现 >= min_freq 次的 2~8 字片段（名词短语的近似）
权重：score = 频次 * (1 + 长度/10) + 来源加成，映射为 1~5 的通用权重。
隐私：blocklist（姓名/电话等，可注入）命中的词一律丢弃；纯数字、含 @ 的词也丢弃。
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field

MAX_WORDS = 500
MIN_LEN, MAX_LEN = 2, 20

_ZH_SUFFIX = '树|表|锁|池|图|库|链|队列|索引|引擎|协议'
_ASCII = re.compile(rf'[A-Za-z0-9][A-Za-z0-9+#._\-]*[A-Za-z0-9+#]?(?:{_ZH_SUFFIX})?')
_QUOTED = re.compile(r'《([^《》]+)》|“([^“”]+)”|「([^「」]+)」|"([^"\n]+)"')
_ZH_SPLIT = re.compile(r'[^一-鿿]+|[的了是在和与及或把被我你他她它们有就也都而并要会能可很不这那么个]')
_STOP_EN = {'The', 'This', 'That', 'And', 'For', 'With', 'From', 'What', 'How', 'Why', 'When', 'Not'}


@dataclass(frozen=True)
class HotwordSet:
    words: list = field(default_factory=list)
    weights: list = field(default_factory=list)  # 与 words 等长，整数 1~5

    def pairs(self):
        return list(zip(self.words, self.weights))


def _is_tech_en(tok):
    if tok in _STOP_EN:
        return False
    return any(c.isupper() or c.isdigit() or c in '+#' for c in tok) and any(c.isalpha() for c in tok)


def _candidates(text):
    """产出 (词, 来源加成)。"""
    for m in _QUOTED.finditer(text):
        yield next(g for g in m.groups() if g).strip(), 2.0
    for m in _ASCII.finditer(text):
        tok = m.group().strip('._-')
        if _is_tech_en(re.sub(r'(?:%s)$' % _ZH_SUFFIX, '', tok)):
            yield tok, 2.0
    for seg in _ZH_SPLIT.split(text):
        if len(seg) <= 8:
            yield seg, 0.0


def extract_hotwords(texts, blocklist=(), max_words=MAX_WORDS, min_freq=3):
    counts, bonus, forms = Counter(), {}, {}
    for text in texts:
        seen = set()
        for word, b in _candidates(text):
            word = word.strip()
            if not (MIN_LEN <= len(word) <= MAX_LEN):
                continue
            key = word.lower()
            if not re.search(r'[A-Za-z一-鿿]', word) or '@' in word:
                continue
            bonus[key] = max(bonus.get(key, 0.0), b)
            forms.setdefault(key, Counter())[word] += 1
            if key not in seen:  # 同一条目内重复只计一次，避免单条长文刷频次
                seen.add(key)
                counts[key] += 1
    block = [b.lower() for b in blocklist if b]
    scored = []
    for key, n in counts.items():
        if bonus[key] == 0.0 and n < min_freq:
            continue
        if any(b in key for b in block):
            continue
        scored.append((n * (1 + len(key) / 10) + bonus[key], key))
    scored.sort(key=lambda x: (-x[0], x[1]))
    scored = scored[:max_words]
    words = [forms[k].most_common(1)[0][0] for _, k in scored]
    weights = [min(5, 1 + int(math.log2(s))) for s, _ in scored]
    return HotwordSet(words, weights)


def format_aliyun_vocabulary(hs):
    """阿里 fun-asr 热词表（创建 vocabulary_id 时的 vocabulary 列表）：[{"text","weight"}]，weight 1~5。"""
    return [{'text': w, 'weight': wt} for w, wt in hs.pairs()]
