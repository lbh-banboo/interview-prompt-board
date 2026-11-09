"""M09 LLM 层单测：DeepSeek 客户端（Mock 传输，SIMULATED，不联网）、提示模板、解析、答案组装。"""
import asyncio
import json

import httpx
import pytest

from app.credentials import CredentialStore, MemoryBackend
from app.domain.answer_compose import (TAG_GENERAL, TAG_MINE, TAG_NEED, TAG_PARTIAL, TAG_UNREVIEWED, collect, compose,
                                       outline_blocks)
from app.domain.facts import ApprovedEntry
from app.llm.client import DeepSeekClient, LLMError, normalize_error, redact
from app.llm.parse import StreamParser, extract_json, parse_decompose, parse_outline
from app.llm.prompts import (build_decompose_messages, build_outline_messages, build_stable_prefix,
                             smart_clip, warmup_messages)
from app.realtime.answer_ext import Externals
from app.realtime.answer_llm import make_client

KEY = 'sk-test-secret-1234567890'
MSG = [{'role': 'user', 'content': 'hi'}]


def sse(*deltas, done=True):
    lines = [f'data: {json.dumps({"choices": [{"delta": {"content": d}}]}, ensure_ascii=False)}\n\n' for d in deltas]
    if done:
        lines.append('data: [DONE]\n\n')
    return ''.join(lines).encode()


def client(handler, **kw):
    kw.setdefault('api_key', KEY)
    return DeepSeekClient(transport=httpx.MockTransport(handler), **kw)


def ok_handler(*deltas):
    return lambda req: httpx.Response(200, content=sse(*deltas))


class Slow(httpx.AsyncByteStream):
    def __init__(self, delay, chunks):
        self.delay, self.chunks = delay, chunks

    async def __aiter__(self):
        for c in self.chunks:
            await asyncio.sleep(self.delay)
            yield c


# ---------- client ----------

@pytest.mark.asyncio
async def test_stream_yields_deltas_and_sends_request_without_thinking():
    seen = {}

    def handler(req):
        seen['body'], seen['auth'], seen['url'] = json.loads(req.content), req.headers['authorization'], str(req.url)
        return httpx.Response(200, content=b': keepalive\n\ndata: not-json\n\ndata: {"choices": []}\n\n' + sse('你', '', '好'))

    c = client(handler)
    assert [d async for d in c.stream(MSG)] == ['你', '好']
    assert seen['body']['stream'] is True and seen['body']['thinking'] == {'type': 'disabled'}
    assert seen['body']['model'] == 'deepseek-flash'
    assert seen['auth'] == f'Bearer {KEY}' and seen['url'].endswith('/chat/completions')
    assert await c.complete(MSG) == '你好'


@pytest.mark.asyncio
async def test_stream_ends_without_done_marker():
    c = client(lambda r: httpx.Response(200, content=sse('a', done=False)))
    assert await c.complete(MSG) == 'a'


@pytest.mark.asyncio
async def test_no_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    calls = []
    c = client(lambda r: calls.append(r) or httpx.Response(200), api_key=None)
    assert not c.has_key
    with pytest.raises(LLMError) as ei:
        await c.complete(MSG)
    assert ei.value.code == 'NO_KEY' and not calls


def test_key_from_env_and_never_in_repr(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    c = DeepSeekClient()
    assert c.has_key and KEY not in repr(c) and 'has_key=True' in repr(c)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,retry', [
    (400, 'BAD_REQUEST', False), (401, 'INVALID_KEY', False), (402, 'INSUFFICIENT_BALANCE', False),
    (403, 'PERMISSION_DENIED', False), (404, 'MODEL_NOT_FOUND', False), (429, 'RATE_LIMITED', True),
    (500, 'PROVIDER_ERROR', True), (503, 'PROVIDER_ERROR', True), (418, 'UNKNOWN_ERROR', False)])
async def test_http_errors_normalized_and_key_redacted(status, code, retry):
    c = client(lambda r: httpx.Response(status, text=f'bad {KEY} Bearer abcdefgh'))
    with pytest.raises(LLMError) as ei:
        await c.complete(MSG)
    e = ei.value
    assert e.code == code and e.retryable is retry
    assert KEY not in e.detail and KEY not in str(e) and 'abcdefgh' not in e.detail


@pytest.mark.asyncio
@pytest.mark.parametrize('exc,code', [
    (httpx.ConnectError('boom'), 'NETWORK_UNREACHABLE'), (httpx.ReadTimeout('t'), 'TIMEOUT'),
    (RuntimeError('x'), 'UNKNOWN_ERROR')])
async def test_transport_errors_normalized(exc, code):
    def handler(req):
        raise exc

    with pytest.raises(LLMError) as ei:
        await client(handler).complete(MSG)
    assert ei.value.code == code and ei.value.__cause__ is None


@pytest.mark.asyncio
async def test_first_token_timeout():
    c = client(lambda r: httpx.Response(200, stream=Slow(0.5, [sse('x')])), first_token_timeout=0.05)
    with pytest.raises(LLMError) as ei:
        await c.complete(MSG)
    assert ei.value.code == 'TIMEOUT'


@pytest.mark.asyncio
async def test_total_timeout():
    chunks = [sse('a', done=False)] * 20
    c = client(lambda r: httpx.Response(200, stream=Slow(0.03, chunks)), total_timeout=0.1, first_token_timeout=1)
    with pytest.raises(LLMError) as ei:
        await c.complete(MSG)
    assert ei.value.code == 'TIMEOUT'


@pytest.mark.asyncio
async def test_cancellation_propagates():
    c = client(lambda r: httpx.Response(200, stream=Slow(5, [sse('x')])), first_token_timeout=10, total_timeout=10)
    t = asyncio.create_task(c.complete(MSG))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


def test_normalize_error_passthrough_and_redact():
    e = LLMError('RATE_LIMITED')
    assert normalize_error(e) is e
    assert normalize_error(asyncio.TimeoutError()).code == 'TIMEOUT'
    assert redact('a sk-abcdef123456 b') == 'a [REDACTED] b'
    assert redact('xyz', key='y') == 'x[REDACTED]z'


# ---------- 真实传输接线（配置 + 凭证；仍用 MockTransport） ----------

def ext(**kw):
    data = dict(outbound=lambda: {'llm_snippets': True}, model='deepseek-chat', llm_live=True,
                llm_base_url='https://api.example.test/v1', llm_timeout_s=8.0,
                creds=CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': KEY})]))
    data.update(kw)
    return Externals(**data)


def test_make_client_uses_config_and_credentials_never_reprs_key():
    c, why = make_client(ext())
    assert why == '' and c.has_key and c.base_url == 'https://api.example.test/v1'
    assert c.model == 'deepseek-chat' and c.total_timeout == 8.0 and c.first_token_timeout == 3.0
    assert c.transport is None and KEY not in repr(c)


def test_make_client_without_live_or_transport_degrades():
    assert make_client(ext(llm_live=False)) == (None, 'no_transport')
    assert make_client(ext(llm_live=False, llm_transport=httpx.MockTransport(lambda r: None)))[1] == ''


def test_make_client_without_key_degrades():
    e = ext(creds=CredentialStore([MemoryBackend({})]))
    assert make_client(e) == (None, 'no_key')


def test_llm_defaults_match_plan():
    from app.config import load_config
    cfg, errors = load_config()
    assert errors == [] and cfg['model']['base_url'] == 'https://api.deepseek.com'
    assert cfg['timeouts']['llm_first_ms'] == 3000 and cfg['timeouts']['llm_total_ms'] == 8000
    assert Externals().llm_first_timeout_s == 3.0 and Externals().llm_timeout_s == 8.0
    assert Externals().llm_base_url == 'https://api.deepseek.com'


def test_llm_first_timeout_must_not_exceed_total(tmp_path):
    from app.config import load_config
    p = tmp_path / 'config.json'
    p.write_text(json.dumps({'timeouts': {'llm_first_ms': 9000, 'llm_total_ms': 8000}}))
    assert any('首 token' in e for e in load_config(user_path=p)[1])


@pytest.mark.asyncio
async def test_stream_deltas_are_yielded_before_stream_end():
    gate = asyncio.Event()

    class Gated(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse('一', done=False)
            await gate.wait()
            yield sse('二')

    got = []
    c = client(lambda r: httpx.Response(200, stream=Gated()))
    async for d in c.stream(MSG):
        got.append(d)
        if d == '一':  # 第一段到达时流尚未结束
            assert not gate.is_set()
            gate.set()
    assert got == ['一', '二']


@pytest.mark.asyncio
async def test_live_client_posts_openai_compatible_stream_with_configured_base_url():
    seen = {}

    def handler(req):
        seen['body'], seen['auth'], seen['url'] = json.loads(req.content), req.headers['authorization'], str(req.url)
        return httpx.Response(200, content=sse('甲', '乙'))

    c, why = make_client(ext(llm_transport=httpx.MockTransport(handler)))
    assert why == '' and await c.complete(MSG) == '甲乙'
    assert seen['url'] == 'https://api.example.test/v1/chat/completions'
    assert seen['auth'] == f'Bearer {KEY}' and seen['body']['stream'] is True
    assert seen['body']['model'] == 'deepseek-chat'


@pytest.mark.asyncio
async def test_configured_timeout_cancels_slow_stream():
    c, _ = make_client(ext(llm_transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow(0.5, [sse('x')]))),
                           llm_timeout_s=0.1))
    with pytest.raises(LLMError) as ei:
        await c.complete(MSG)
    assert ei.value.code == 'TIMEOUT'


class Hang(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b''


class Tracking(httpx.AsyncBaseTransport):
    def __init__(self):
        self.closed, self.started = False, asyncio.Event()

    async def handle_async_request(self, request):
        self.started.set()
        return httpx.Response(200, stream=Hang())

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_cancel_closes_connection():
    tr = Tracking()
    c, _ = make_client(ext(llm_transport=tr, llm_timeout_s=10))
    task = asyncio.create_task(c.complete(MSG))
    await tr.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tr.closed


# ---------- prompts ----------

def test_stable_prefix_is_deterministic_and_minimal():
    p = build_stable_prefix('后端开发')
    assert p == build_stable_prefix('后端开发') and '【岗位】后端开发' in p and 'facts_used' in p
    assert '【岗位】' not in build_stable_prefix()
    w = warmup_messages('后端开发')
    assert w[0]['content'] == p and len(w[1]['content']) < 5


def test_outline_messages_contain_only_fragments():
    m = build_outline_messages('讲讲缓存', [{'entry_id': 'e1', 'text': '片段甲'}, {'entry_id': 'e2', 'text': '片段乙'}])
    assert m[0]['content'] == build_stable_prefix()
    assert '[e1]' in m[1]['content'] and '片段乙' in m[1]['content'] and '讲讲缓存' in m[1]['content']


def test_decompose_messages():
    m = build_decompose_messages('Q', 'A')
    assert 'variants' in m[0]['content'] and json.loads(m[1]['content']) == {'question': 'Q', 'answer': 'A'}


def test_smart_clip_prefers_priority_paragraphs_in_order():
    paras = ['闲聊' * 10, '负责项目A提升30%' + '。' * 5, '无关' * 10, '成果 10 倍']
    text = '\n\n'.join(paras)
    clipped = smart_clip(text, 40)
    assert paras[1] in clipped and paras[3] in clipped and paras[0] not in clipped
    assert clipped.index(paras[1]) < clipped.index(paras[3])
    assert smart_clip('短文', 100) == '短文'
    assert smart_clip('x' * 100, 10) == 'x' * 10


# ---------- parse ----------

def test_extract_json_tolerant():
    assert extract_json('好的：```json\n{"a": 1,}\n``` 完毕') == {'a': 1}
    assert extract_json('{坏 {"b": 2}') == {'b': 2}
    assert extract_json('没有json') is None and extract_json('') is None


def test_parse_outline_structured():
    raw = json.dumps({'conclusion': ' 结论 ', 'points': ['一', '二', '三', '四'], 'example': 'e', 'caution': 5,
                      'facts_used': [{'entry_id': 'e1', 'text': '事实'}, {'entry_id': 'e2', 'text': ''}, 'bad']})
    d, ok = parse_outline(raw)
    assert ok and d['conclusion'] == '结论' and d['points'] == ['一', '二', '三'] and d['caution'] == ''
    assert d['facts_used'] == [{'entry_id': 'e1', 'text': '事实'}]


def test_parse_outline_fallback_to_rule_outline():
    d, ok = parse_outline('这不是JSON', '先说结论。第一点。第二点。')
    assert not ok and d['conclusion'] == '先说结论。' and d['points'] == ['第一点。', '第二点。'] and d['facts_used'] == []
    d, ok = parse_outline('{"points": []}')
    assert not ok and d['conclusion'] == ''


def test_parse_decompose():
    raw = json.dumps({'question': 'Q', 'variants': ['a', 'b', 'c', 'd', 'e', 'f'],
                      'answer_outline': {'conclusion': 'c', 'points': ['1'], 'example': '', 'caution': ''},
                      'facts': [{'text': 'f1'}, 'f2', ''], 'role_tags': ['后端'], 'topic_tags': []})
    d = parse_decompose(raw)
    assert d['variants'] == ['a', 'b', 'c', 'd', 'e'] and d['facts'] == ['f1', 'f2'] and d['role_tags'] == ['后端']
    assert parse_decompose('乱码') is None and parse_decompose('{"question": ""}') is None
    assert parse_decompose('{"question": "Q", "answer_outline": 3}')['answer_outline']['conclusion'] == ''


def test_stream_parser_emits_complete_items_only():
    raw = ('```json\n{"conclusion": "先\\"说\\"结论", "points": ["甲", "乙",\n "丙"], "example": "", '
           '"caution": "注意", "facts_used": []}')
    p = StreamParser()
    got = []
    for i in range(0, len(raw), 7):
        got += p.feed(raw[i:i + 7])
    assert got == [('conclusion', '先"说"结论'), ('point', '甲'), ('point', '乙'), ('point', '丙'), ('caution', '注意')]
    p2 = StreamParser()
    assert p2.feed('{"conclusion": "未闭合') == [] and p2.feed('", "points": ["a') == [('conclusion', '未闭合')]
    assert StreamParser().feed('{"conclusion": "\\u00"}') == [('conclusion', '\\u00')]


# ---------- compose ----------

ENTRIES = [ApprovedEntry('e1', '我在订单中心项目担任后端工程师，接口响应时间从800ms降到200ms，团队5人。'),
           ApprovedEntry('e2', '我在星河平台吞吐提升30%。')]
FRAGS = [{'entry_id': 'e1', 'text': '订单中心项目片段'}]
PRIMARY = {'conclusion': '主结论', 'points': ['一', '二', '三'], 'example': '例', 'caution': '注'}


def llm_json(**kw):
    d = {'conclusion': '通用结论', 'points': ['点一', '点二', '点三'], 'example': '', 'caution': '注意',
         'facts_used': []}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def streamed(text, n=10):
    return ok_handler(*[text[i:i + n] for i in range(0, len(text), n)])


@pytest.mark.asyncio
async def test_t2_no_credential_rule_outline_usable(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    c = DeepSeekClient(transport=httpx.MockTransport(lambda r: pytest.fail('不应发请求')))
    ans = await collect(compose('Q', fragments=FRAGS, client=c, fallback_answer='先说结论。第一点。第二点。第三点。'))
    assert [b.section for b in ans.blocks] == ['conclusion', 'point', 'point', 'point', 'caution']
    assert all(b.source_tag == TAG_GENERAL for b in ans.blocks)
    assert (await collect(compose('Q', fallback_answer='', client=None))).blocks == []


@pytest.mark.asyncio
async def test_t3_structure_complete_or_explicit_missing():
    full = await collect(compose('Q', primary=PRIMARY))
    assert full.missing == [] and all(b.source_tag == TAG_MINE and not b.supplement for b in full.blocks)
    part = await collect(compose('Q', primary={'conclusion': '只有结论'}))
    assert part.missing == ['point', 'example', 'caution']
    assert [b.section for b in outline_blocks(PRIMARY, TAG_MINE)] == ['conclusion', 'point', 'point', 'point', 'example', 'caution']


@pytest.mark.asyncio
async def test_primary_first_then_model_only_supplements():
    c = client(streamed(llm_json()))
    blocks = [b async for b in compose('Q', primary=PRIMARY, fragments=FRAGS, entries=ENTRIES, client=c)]
    main = [b for b in blocks if not b.supplement]
    assert [b.text for b in main] == ['主结论', '一', '二', '三', '例', '注']
    assert blocks[:len(main)] == main  # 主答案先出，位置不变
    sup = [b for b in blocks if b.supplement]
    assert sup and all(b.source_tag == TAG_GENERAL for b in sup)


@pytest.mark.asyncio
async def test_personal_facts_validated_before_push():
    good = {'entry_id': 'e1', 'text': '接口响应时间从800ms降到200ms。'}
    bad = {'entry_id': 'e1', 'text': '我把成本降低了40%。'}
    fake = {'entry_id': 'zzz', 'text': '团队5人。'}
    c = client(streamed(llm_json(facts_used=[good, bad, fake])))
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c)]
    facts = [b for b in blocks if b.section == 'fact']
    assert [(b.source_tag, b.kind) for b in facts] == [(TAG_PARTIAL, 'personal'), (TAG_NEED, 'placeholder'),
                                                      (TAG_NEED, 'placeholder')]
    assert facts[0].entry_id == 'e1' and '40%' not in ''.join(b.text for b in blocks)
    # 公共块先于个人块推送
    assert max(i for i, b in enumerate(blocks) if b.kind == 'public') < min(i for i, b in enumerate(blocks) if b.section == 'fact')


@pytest.mark.asyncio
async def test_spoken_hint_is_unreviewed():
    c = client(streamed(llm_json(facts_used=[{'entry_id': 'e1', 'text': '接口响应时间从800ms降到200ms。'}])))
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c, spoken=['接口响应时间降到了100ms'])]
    hints = [b for b in blocks if b.kind == 'hint']
    assert len(hints) == 1 and hints[0].source_tag == TAG_UNREVIEWED and '不一致' in hints[0].text


@pytest.mark.asyncio
async def test_public_first_person_not_pushed():
    c = client(streamed(llm_json(points=['我做过这个，提升了30%', '点二', '点三'])))
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c)]
    assert '我做过' not in ''.join(b.text for b in blocks) and len([b for b in blocks if b.section == 'point']) == 2


@pytest.mark.asyncio
async def test_unstructured_output_falls_back_to_rule_outline():
    c = client(streamed('抱歉，我无法提供JSON'))
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c, fallback_answer='先说结论。第一点。')]
    assert [b.text for b in blocks][:2] == ['先说结论。', '第一点。'] and blocks[-1].section == 'caution'
    # 补充区非结构化：什么都不加
    c2 = client(streamed('乱码'))
    blocks = [b async for b in compose('Q', primary=PRIMARY, fragments=FRAGS, entries=ENTRIES, client=c2, fallback_answer='x。')]
    assert all(not b.supplement for b in blocks)


@pytest.mark.asyncio
async def test_timeout_without_first_token_keeps_existing_content():
    c = client(lambda r: httpx.Response(200, stream=Slow(0.5, [sse('x')])), first_token_timeout=0.05)
    blocks = [b async for b in compose('Q', primary=PRIMARY, fragments=FRAGS, entries=ENTRIES, client=c)]
    assert len(blocks) == 6 and all(not b.supplement for b in blocks)
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c, fallback_answer='先说结论。')]
    assert [b.text for b in blocks][0] == '先说结论。'


@pytest.mark.asyncio
async def test_error_after_partial_stream_keeps_pushed_public_blocks():
    partial = '{"conclusion": "已完整", "points": ["未闭'
    c = client(lambda r: httpx.Response(200, content=sse(partial, done=False)))
    blocks = [b async for b in compose('Q', fragments=FRAGS, entries=ENTRIES, client=c, fallback_answer='回退。')]
    assert [b.text for b in blocks] == ['已完整']
