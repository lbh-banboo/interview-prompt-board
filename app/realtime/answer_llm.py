"""M09 模型补充：只在 key + 外发开关 + 已注入 transport 都具备时调用；输入只含检索到的片段。

产出走补充区，不替换主答案；个人事实块由 answer_compose 先校验后产出。没有条件时返回 (None, 原因)，不外发。
"""
from ..domain.answer_compose import compose
from ..llm.client import MESSAGES, DeepSeekClient, LLMError
from ..llm.prompts import build_outline_messages, warmup_messages
from .answer_ext import take

MAX_TOKENS = 800
CHARS_PER_TOKEN = 2  # 粗略估算：中文约 1.5~2 字符/token；口径与 /api/usage 的 note 一致


def _chars(messages):
    return sum(len(m['content']) for m in messages)


def _tokens(chars):
    """字符数折算估算 token（向上取整，至少 1）。"""
    return max(1, (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def make_client(ext):
    """返回 (client, reason)；reason 非空表示不调用：switch_off / no_key / no_transport。"""
    if not ext.switches().get('llm_snippets'):
        return None, 'switch_off'
    key = ext.key('DEEPSEEK_API_KEY')
    if not key:
        return None, 'no_key'
    if ext.llm_transport is None and not ext.llm_live:
        return None, 'no_transport'
    return DeepSeekClient(key, base_url=ext.llm_base_url, model=ext.model, transport=ext.llm_transport,
                          first_token_timeout=ext.llm_first_timeout_s, total_timeout=ext.llm_timeout_s), ''


class _Tracked:
    """转发 stream，并记下 LLMError（compose 会吞掉它，预算与 health 需要知道）。"""
    has_key = True

    def __init__(self, client):
        self.client, self.error = client, None

    async def stream(self, messages, **kw):
        try:
            async for d in self.client.stream(messages, **kw):
                yield d
        except LLMError as e:
            self.error = e
            raise


def _chars(messages):
    return sum(len(m['content']) for m in messages)


async def model_blocks(ext, session, client, question, fragments, entries, role=''):
    """产出 AnswerBlock；预算被拒时产出一个 str（reserve 的 message）后结束。"""
    est = _tokens(_chars(build_outline_messages(question, fragments, role))) + MAX_TOKENS
    lease = take(ext.budget, 'llm', est, session)
    if not lease.ok:
        yield lease.message
        return
    tracked, out, done = _Tracked(client), 0, False
    try:
        async for b in compose(question, fragments=fragments, entries=entries, client=tracked, role=role):
            out += len(b.text)
            yield b
        done = True
    finally:
        if done and tracked.error is None:
            lease.settle(est - MAX_TOKENS + _tokens(out))
        else:
            lease.cancel()
    if tracked.error is None:
        ext.report('llm', 'ok')
    else:
        ext.report('llm', 'degraded', MESSAGES[tracked.error.code])


async def warm_up(ext, session, client, role=''):
    msgs = warmup_messages(role)
    est = _tokens(_chars(msgs)) + 1
    lease = take(ext.budget, 'llm', est, session)
    if not lease.ok:
        return
    try:
        await client.complete(msgs, max_tokens=1)
    except LLMError as e:
        lease.cancel()
        ext.report('llm', 'degraded', MESSAGES[e.code])
        return
    except BaseException:
        lease.cancel()
        raise
    lease.settle(est)
