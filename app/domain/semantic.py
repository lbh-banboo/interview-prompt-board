"""中文语义完整度与话语分类（M05-2）。纯函数，无状态、无 IO。

Ported from buluoai6-eng/online-meeting-interview-assistant@78083e18e05eb29e089a612c496a2d425d882f1b:src/question-detector.js,
src/turn-guard.js (MIT © 2026 buluoai6-eng) and lesue1/interview-assistant@739b01f2a0f816dbeaf9c0d1b0afa80ed0c61f5e:main.py
is_non_question (MIT © 2026 lesue1).
Modified: JavaScript/Python -> Python re; 新增 completeness()（句末未完成信号、请求缺宾语、截断的 A-不-A）、
流程性发言(process)、backchannel 分类、materially_changed()（final 改义判定）；
不含 lesue1 的 FINALIZE_MS=1200 闸门（禁止使用），不含任何 LLM/云端调用。
"""
from __future__ import annotations

import re

_PUNCT_CHARS = '，。、？！；：,.?!;:~～…—－-"\'“”‘’()（）《》 \t\n\r'
_PUNCT_RE = re.compile(r'[\s' + re.escape(_PUNCT_CHARS) + r']+')

# ---- 分类用规则 ----
_FILLER_CHARS = frozenset('嗯哦噢啊呃额唉哎哟呀啦呐')
_BACKCHANNEL_WORDS = frozenset({
    '好的', '好', 'ok', 'okay', '对', '对的', '是', '是的', '行', '可以', '明白', '明白了', '知道', '了解', '收到',
    '我明白', '我知道', '好嘞', '没事', '没有', '这样啊', '没错', '有道理', '懂了', '清楚', '清楚了',
})
_PROCESS_ALTS = (
    r'(?:你|您|我)?(?:这边)?(?:能|可以)?(?:听|看)(?:得)?(?:到|见|清)(?:楚)?(?:我|我的|你|您|声音|画面|屏幕|了)?(?:说话|讲话)?(?:吗|么|不)?'
    r'|(?:我们|咱们|咱)?(?:现在|这就|就|正式|先)*(?:可以)?开始(?:了|吧|啦|吗|么)*'
    r'|准备好了?(?:吗|么)?|(?:请)?稍等(?:一下|下)?|等一下|等等'
    r'|(?:声音|画面|屏幕|网络|信号).{0,6}(?:正常|清楚|卡|有问题)(?:吗|么)?'
    r'|欢迎(?:你|您|大家)?(?:来)?|辛苦了?|谢谢|感谢|再见|你好|您好|大家好|早上好|下午好|晚上好'
    r'|共享(?:一下)?屏幕|(?:开|关)(?:一下)?(?:麦|摄像头|视频)|(?:我们)?先这样吧?')
_PROCESS_RE = re.compile(r'(?:好的?|那么?|嗯|哦|啊)*(?:' + _PROCESS_ALTS + r')(?:吧|啊|呀|了|啦)*')
_VERB = (r'(?:讲讲|说说|谈谈|聊聊|介绍|讲|谈|说|聊|解释|阐述|说明|分析|比较|对比|区分|评价|判断|列举|举例|概述|总结|拆解|'
         r'计算|回答|分享|描述)(?:一下|下)?')
_POLITE_STRONG = r'(?:请(?:你|您)?|麻烦(?:你|您)?|(?:你|您)?(?:能不能|能否|可以|可不可以|可否)(?:请)?(?:你|您)?)'
_REQ_START_RE = re.compile(r'(?:^|[，。,；;])(?:(?:好的?|那么?|接下来|下面|再|然后|关于)\s*)*(?:' + _POLITE_STRONG + r'|你|您)?(?:先|再|来)?' + _VERB)
_REQ_POLITE_RE = re.compile(_POLITE_STRONG + _VERB)
_REQ_YIXIA_RE = re.compile(r'(?:介绍|讲|谈|说|聊|解释|说明|分析|总结|描述|分享)(?:一下|下)|讲讲|说说|谈谈|聊聊|举(?:个|一个|几个)?例|列举')
_SELF_STATEMENT_RE = re.compile(r'(?<!自)(?:我|我们|咱们|本人)(?:这边)?(?:先|来|再|就|简单|给你|给您|跟你|跟您)*' + _VERB)
_IMPLIED_REQUEST_RE = re.compile(r'(?:(?:^|[，。,；;])(?:(?:好的?|那么?|接下来|然后)\s*)*(?:请|麻烦)?(?:你|您)?(?:先|再|来)?(?:做|作)?(?:个|一个)?自我介绍'
                                 r'|的(?:主要|核心|本质)?区别|有什么(?:区别|不同)|各?举.{0,8}(?:例子|案例)|意味着什么|原因(?:和|及|与)风险'
                                 r'|影响有哪些|处理方法|核心步骤|如何理解)')
_INTERROGATIVE_RE = re.compile(r'为什么|为何|为啥|怎么|怎样|如何|什么|哪些|哪个|哪种|哪里|哪儿|几个|几种|多少|多久|多大|是否|有没有|能不能|能否|可否|'
                               r'可不可以|是不是|会不会|对不对|行不行|好不好|有无|你觉得|你认为|谁|what|why|how|could you|can you|would you', re.I)
_INTERROGATIVE_END_RE = re.compile(r'(?:为什么|为何|为啥|什么|哪些|哪个|哪种|哪里|多少|几个|几种|怎么样|怎样|谁|吗|么|呢)$')
_TAG_QUESTION_RE = re.compile(r'(?:对|是|行|好|可以|没错|不对)吧$')
_END_Q_PARTICLE_RE = re.compile(r"(?:吗|(?<![什怎甚])么|呢)$")
_CANDIDATE_RESPONSE_RE = re.compile(r'(?:^|[，。,.；;\s])(?:我们当时|我当时|我负责|我参与|我认为|我的理解|在我看来|首先|其次|最后|具体来说|举个例子|例如|所以|因此|结果是|这说明)')

# ---- 完整度用规则 ----
# 句末未完成信号（匹配去掉末尾标点后的尾部）
_INCOMPLETE_TAIL_RE = re.compile(
    r'(?:然后|就是说|就是|比如说|比如|例如|还有一个就是|还有就是|还有一个|另外|以及|并且|而且|或者|还是|因为|所以|但是|不过|'
    r'如果|假如|那么|的话|和|与|跟|在|对于|关于|把|被|让|从|向|为了|通过|根据|按照|其实|主要|首先|其次|第一|第二|第三|'
    r'你知道|我想问一下|我想问|想问一下|请问一下|请问|这个|那个|就是那个|怎么说呢|怎么讲|你能|你可以|'
    r'是否|能否|可否|如何|怎么|怎样|呃|额|嗯)$')
_WEAK_TAILS = frozenset({'这个', '那个', '主要'})
_TRUNCATED_MODAL_RE = re.compile(r'^.{0,2}(?:能不能|可不可以|会不会)$')  # "你能不能"截断；"这样对不对"则完整
_OPEN_PUNCT_TAIL_RE = re.compile(r'[，、,；;：:]\s*$|(?:…|\.\.\.|——|--)\s*$')
_MIN_OBJECT = 2  # 请求动词后至少 2 个有效字才算带宾语

# ---- final 改义用规则 ----
_NEGATION_RE = re.compile(r'不是|没有|不要|不能|不会|不用|不需要|不对|没|不|别|未|非|无')


def core(text: str) -> str:
    """去标点/空白/大小写后的文本。"""
    return _PUNCT_RE.sub('', text or '').lower()


def _is_backchannel(text: str) -> bool:
    c = core(text)
    stripped = ''.join(ch for ch in c if ch not in _FILLER_CHARS)
    if not stripped or c in _BACKCHANNEL_WORDS or stripped in _BACKCHANNEL_WORDS:
        return True
    # 叠字："好的好的好的"、"对对对"
    return len(stripped) <= 8 and any(len(stripped) % len(w) == 0 and stripped == w * (len(stripped) // len(w))
                                      for w in _BACKCHANNEL_WORDS)


def _request_match(text: str):
    """返回请求式表达的 match（取最靠后的结束位置，便于判断是否带宾语），否则 None。"""
    t = text.strip()
    polite = _REQ_POLITE_RE.search(t)
    if _SELF_STATEMENT_RE.search(t) and not polite:
        return None
    ms = [m for m in (_REQ_START_RE.search(t), polite, _REQ_YIXIA_RE.search(t), _IMPLIED_REQUEST_RE.search(t)) if m]
    return max(ms, key=lambda m: m.end()) if ms else None


def classify(text: str) -> str:
    """'question' | 'request' | 'statement' | 'process' | 'backchannel'。"""
    t = (text or '').strip()
    c = core(t)
    if not c or _is_backchannel(t):
        return 'backchannel'
    if len(c) <= 16 and _PROCESS_RE.fullmatch(c):
        return 'process'
    if re.search(r'[?？]\s*$', t) or _END_Q_PARTICLE_RE.search(c) or _TAG_QUESTION_RE.search(c):
        return 'question'
    if _request_match(t):
        return 'request'
    if _INTERROGATIVE_RE.search(c):
        return 'statement' if _CANDIDATE_RESPONSE_RE.search(t) else 'question'
    return 'statement'


def completeness(text: str) -> tuple[str, list[str]]:
    """返回 (level, reasons)。level: 'high' 可立即提交 / 'low' 语义上已完整但把握不足 / 'incomplete' 句子未说完。

    不含覆盖判定：调用方必须保证 text 来自覆盖到音频末尾的 partial，否则不得采信 'high'。
    """
    t = (text or '').strip()
    c = core(t)
    if not c:
        return 'incomplete', ['empty']
    kind = classify(t)
    if kind in ('backchannel', 'process'):
        return 'low', [kind]

    explicit_q = bool(re.search(r'[?？]\s*$', t))
    if not explicit_q:
        if _OPEN_PUNCT_TAIL_RE.search(t):
            return 'incomplete', ['trailing_open_punct']
        tail_core = core(re.sub(r'[。.！!]+$', '', t))
        m = _INCOMPLETE_TAIL_RE.search(tail_core)
        if m and m.group(0) in _WEAK_TAILS and kind in ('question', 'request'):
            m = None  # "你为什么选择用这个"：问句/请求里的指示词收尾是正常的宾语，不是没说完
        if m:
            return 'incomplete', ['tail:' + m.group(0)]
        if _TRUNCATED_MODAL_RE.match(tail_core):
            return 'incomplete', ['truncated_modal']
        if tail_core.endswith('的') and kind == 'statement' and '是' not in tail_core:
            return 'incomplete', ['tail:的']
        if kind == 'request':
            rm = _request_match(t)
            if len(tail_core[len(core(t[:rm.end()])):]) < _MIN_OBJECT and not _IMPLIED_REQUEST_RE.search(t):
                return 'incomplete', ['request_without_object']

    reasons: list[str] = []
    if explicit_q:
        reasons.append('question_mark')
    if _END_Q_PARTICLE_RE.search(c):
        reasons.append('particle:' + c[-1])
    if _TAG_QUESTION_RE.search(c):
        reasons.append('tag_question')
    m = _INTERROGATIVE_RE.search(c)
    if m:
        reasons.append('interrogative:' + m.group(0))
    if kind == 'request':
        reasons.append('request')

    if kind == 'statement':
        return 'low', reasons + ['statement']
    if (explicit_q and len(c) >= 2) or len(c) >= 4 or _INTERROGATIVE_END_RE.search(c):
        return 'high', reasons
    return 'low', reasons + ['too_short']


def materially_changed(old: str, new: str) -> tuple[bool, str]:
    """final 相对已提交文本是否"实质改义"。返回 (changed, reason)；标点/语气词/小幅同音字不算。"""
    a, b = core(old), core(new)
    if a == b:
        return False, 'same'
    if _neg_signature(a) != _neg_signature(b):
        return True, 'negation_changed'
    if b.startswith(a) and len(b) - len(a) >= 2:
        return True, 'tail_extended'
    if _question_parts(new) > _question_parts(old):
        return True, 'new_subquestion'
    ta, tb = a[-8:], b[-8:]
    if _bigram_overlap(ta, tb) < 0.5:
        return True, 'tail_changed'
    return False, 'minor'


def _neg_signature(c: str) -> list[str]:
    return _NEGATION_RE.findall(c)


def _question_parts(text: str) -> int:
    parts = [p for p in re.split(r'[？?。！!；;]', text or '') if core(p)]
    return sum(1 for p in parts if classify(p) in ('question', 'request'))


def _bigram_overlap(a: str, b: str) -> float:
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / min(len(ga), len(gb))


def semantic_signature(text: str) -> tuple:
    """(类别, 完整度, 否定词序列, 问句/请求子句数)：任一变化即语义要素改变（ASR 追赶时据此区分补全与改义）。"""
    return classify(text), completeness(text)[0], _neg_signature(core(text)), _question_parts(text)
