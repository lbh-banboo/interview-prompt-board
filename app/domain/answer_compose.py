"""M09 答案组装：检索结果 + 事实校验 -> 统一展示块（结论 -> 3 要点 -> 例子/数字 -> 注意）。

- 主库命中：直接用预生成提纲（0 模型延迟，tag=我的口径）；模型输出只进补充区（supplement=True），不替换主答案。
  L1p 部分命中来源的 personal facts 标“部分命中，非标准答案”，不得冒充“我的口径”。
- 公共知识块（conclusion/point/example/caution）随流式首个完整“点”即推送，tag=通用建议，不得第一人称。
- 个人事实块（facts_used）流结束后先过 facts.validate 再推送；不通过换成“[需本人补充]”。本模块不让模型直接推个人事实。
- 无凭证 / 超时 / 输出非结构化：回退规则提纲（app/domain/outline.py），不阻塞。
"""
from dataclasses import dataclass, field

from ..llm.client import LLMError
from ..llm.parse import StreamParser, parse_outline
from ..llm.prompts import build_outline_messages
from ..retrieval.tags import SOURCE_GENERAL, SOURCE_MINE, SOURCE_PARTIAL
from .facts import PLACEHOLDER, Block, validate

TAG_MINE, TAG_GENERAL, TAG_EXTERNAL, TAG_NEED, TAG_UNREVIEWED = SOURCE_MINE, SOURCE_GENERAL, '外部参考', '需本人补充', '未审核'
TAG_PARTIAL = SOURCE_PARTIAL
SECTIONS = ('conclusion', 'point', 'example', 'caution')


@dataclass(frozen=True)
class AnswerBlock:
    source_tag: str
    kind: str  # public | personal | placeholder | hint
    section: str  # conclusion | point | example | caution | fact | hint
    text: str
    entry_id: str | None = None
    supplement: bool = False


@dataclass
class ComposedAnswer:
    blocks: list = field(default_factory=list)

    @property
    def missing(self):
        """主答案里缺省的段落（例子/注意允许缺省，但必须显式列出）。"""
        have = {b.section for b in self.blocks if not b.supplement}
        return [s for s in SECTIONS if s not in have]


def outline_blocks(o, tag, supplement=False):
    seq = [('conclusion', o.get('conclusion'))] + [('point', p) for p in o.get('points') or []] \
        + [('example', o.get('example')), ('caution', o.get('caution'))]
    return [AnswerBlock(tag, 'public', sec, text, supplement=supplement) for sec, text in seq if text]


async def collect(agen):
    return ComposedAnswer([b async for b in agen])


async def compose(question, *, primary=None, fragments=(), entries=(), client=None,
                  spoken=(), fallback_answer='', role=''):
    """异步生成器，产出 AnswerBlock。
    primary: 主库命中条目的预生成提纲 dict；fragments: [{'entry_id','text'}] 仅检索片段；
    entries: 已审核 ApprovedEntry（个人事实校验的唯一依据）；spoken: 本人现场口述（仅一致性线索）。"""
    supplement = primary is not None
    if supplement:
        for b in outline_blocks(primary, TAG_MINE):
            yield b
    if client is None or not client.has_key or not fragments:
        if not supplement and fallback_answer:
            for b in outline_blocks(parse_outline('', fallback_answer)[0], TAG_GENERAL):
                yield b
        return

    public_corpus = '\n'.join(f.get('text', '') for f in fragments)  # 公共块数字必须能在这里找到
    parser, buf, sent = StreamParser(), '', False
    try:
        async for delta in client.stream(build_outline_messages(question, fragments, role)):
            buf += delta
            for sec, text in parser.feed(delta):
                if validate([Block.public(text)], entries, public_corpus=public_corpus).ok:
                    sent = True
                    yield AnswerBlock(TAG_GENERAL, 'public', sec, text, supplement=supplement)
    except LLMError:
        pass  # 超时/限流/鉴权：放弃补充，保留已有内容
    data, structured = parse_outline(buf, '' if supplement or sent else fallback_answer)
    if not structured:
        if not supplement and not sent:
            for b in outline_blocks(data, TAG_GENERAL):
                yield b
        return

    res = validate([Block.personal(f['entry_id'], f['text']) for f in data['facts_used']], entries, spoken)
    for b in res.blocks:
        if b.kind == 'placeholder':
            yield AnswerBlock(TAG_NEED, 'placeholder', 'fact', PLACEHOLDER, supplement=supplement)
        else:
            # 无主答案（补充模式）时模型产出的事实只可能来自 L1p 部分命中，不得标“我的口径”
            tag = TAG_MINE if primary is not None else TAG_PARTIAL
            yield AnswerBlock(tag, 'personal', 'fact', b.text, b.sentences[0].entry_id, supplement)
    for h in res.hints:
        yield AnswerBlock(TAG_UNREVIEWED, 'hint', 'hint', h.text, h.entry_id, supplement)
