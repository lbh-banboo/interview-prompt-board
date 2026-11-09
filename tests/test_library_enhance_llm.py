"""DeepSeek 增强器：假传输（httpx.MockTransport）覆盖成功/越权事实/无 Key/外发关闭/预算拒绝/泄漏。"""
import asyncio
import json

import httpx
import pytest

from app.core import Store
from app.credentials import CredentialStore, MemoryBackend
from app.library.conflicts import review_entries
from app.library.enhance import EnhancerUnavailable, enhance_library
from app.library.enhance_llm import DeepSeekEnhancer
from app.server import create_app
from app.usage import BudgetGuard, UsageMeter

KEY = 'sk-fake-deepseek-key-000000'  # pragma: allowlist secret
PRICES = dict(asr_per_hour=2.5, llm_per_1k_tokens=0.002, search_per_call=0.01)
TEXT = '\n'.join([
    '## H1 合成',
    '**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我在示例科技公司负责“星河”项目，团队 8 人。',
    '**H1-02｜00:02｜怎么协作？** 参考回答：与相关团队同步。',
])


def config(switch=True):
    return lambda: dict(outbound={'llm_snippets': switch},
                        model={'name': 'deepseek-flash', 'base_url': 'https://api.deepseek.com'},
                        timeouts={'llm_first_ms': 1000, 'llm_total_ms': 5000})


def split_json(question='星河是什么？', facts=()):
    return json.dumps(dict(
        question=question,
        question_variants=['a', 'b', 'c'],
        answer_outline=dict(conclusion='结论', points=['1', '2', '3'], examples=[], cautions=[]),
        facts=list(facts),
        role_tags=['后端'], topic_tags=['项目'],
    ), ensure_ascii=False)


def sse(text):
    chunks = [text[i:i + 17] for i in range(0, len(text), 17)] or ['']
    lines = ['data: ' + json.dumps({'choices': [{'delta': {'content': c}}]}, ensure_ascii=False) for c in chunks]
    return '\n\n'.join(lines + ['data: [DONE]']) + '\n\n'


def mock(body, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        if status != 200:
            return httpx.Response(status, text='{"error":"denied"}')
        return httpx.Response(200, text=sse(body))

    return httpx.MockTransport(handler), seen


def make_enhancer(tmp_path, body=None, status=200, switch=True, budget=None, key=KEY):
    transport, seen = mock(body or split_json(), status)
    creds = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': key} if key else {})])
    meter = UsageMeter(tmp_path / 'usage.sqlite3', PRICES)
    enhancer = DeepSeekEnhancer(creds, budget if budget is not None else BudgetGuard(meter, 20.0, 5.0, 4),
                                config(switch), transport=transport)
    return enhancer, seen, meter


def library(tmp_path):
    store = Store(tmp_path)
    return store, store.import_text('合成.md', TEXT)['id']


def test_split_success_uses_transport_and_settles_budget(tmp_path):
    store, lib = library(tmp_path)
    enhancer, seen, meter = make_enhancer(tmp_path)
    data = asyncio.run(enhancer.split(TEXT.splitlines()[1]))
    assert data['question'] == '星河是什么？' and len(seen) == 1
    assert seen[0].headers['authorization'].startswith('Bearer ')
    assert meter.spent_today() > 0
    assert KEY not in json.dumps(data, ensure_ascii=False) and KEY not in repr(enhancer)
    assert asyncio.run(enhance_library(store, lib, enhancer)).enhanced == 2


def test_pii_is_masked_before_request_but_company_and_project_names_survive(tmp_path):
    """手机号/邮箱/证件/银行卡不得出现在外发请求里；公司名项目名保留（事实比对要用原文）。"""
    enhancer, seen, _ = make_enhancer(tmp_path)
    raw = ('**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我在示例科技有限公司负责“星河”项目，'
           '电话 13812345678，邮箱 zhang.san@example.com，身份证 110101199003071234，卡号 6222021234567890123。')
    asyncio.run(enhancer.split(raw))
    sent = seen[0].content.decode('utf-8')
    for leak in ('13812345678', 'zhang.san@example.com', '110101199003071234', '6222021234567890123'):
        assert leak not in sent
    assert '示例科技有限公司' in sent and '星河' in sent and '[已遮盖]' in sent


def test_overreach_fact_is_rejected_by_existing_checks(tmp_path):
    store, lib = library(tmp_path)
    enhancer, _, _ = make_enhancer(tmp_path, split_json(facts=[
        dict(kind='metric', text='性能提升 99%', source_span='性能提升 99%')]))
    asyncio.run(enhance_library(store, lib, enhancer))
    entries = {e['id']: e for e in review_entries(store, lib)}
    assert 'suspected_fabrication' in entries['H1-01']['flags']
    assert not entries['H1-01']['can_approve'] and entries['H1-01']['enhanced']


def test_no_key_degrades_without_request(tmp_path):
    enhancer, seen, _ = make_enhancer(tmp_path, key=None)
    with pytest.raises(EnhancerUnavailable) as exc:
        asyncio.run(enhancer.split('原文'))
    assert exc.value.reason == 'no_key' and seen == []
    store, lib = library(tmp_path)
    progress = asyncio.run(enhance_library(store, lib, enhancer))
    assert progress.failed == 2 and progress.error == 'no_key'
    assert {e['enhance_error'] for e in review_entries(store, lib)} == {'no_key'}


def test_outbound_switch_off_degrades_without_request(tmp_path):
    enhancer, seen, _ = make_enhancer(tmp_path, switch=False)
    with pytest.raises(EnhancerUnavailable) as exc:
        asyncio.run(enhancer.split('原文'))
    assert exc.value.reason == 'outbound_disabled' and seen == []


def test_budget_denied_degrades_without_request(tmp_path):
    store, _ = library(tmp_path)
    guard = BudgetGuard(UsageMeter(tmp_path / 'usage.sqlite3', PRICES), 0.0, 5.0, 4)
    enhancer, seen, _ = make_enhancer(tmp_path, budget=guard)
    with pytest.raises(EnhancerUnavailable) as exc:
        asyncio.run(enhancer.split('原文'))
    assert exc.value.reason == 'budget_denied' and seen == []
    # 预算拒绝后仍保留规则拆解
    _, lib = store, store.libraries()[0]['id']
    asyncio.run(enhance_library(store, lib, enhancer))
    entries = {e['id']: e for e in review_entries(store, lib)}
    assert all(not e['enhanced'] for e in entries.values()) and entries['H1-01']['facts']


def test_bad_json_and_provider_error_never_leak_key(tmp_path):
    store, lib = library(tmp_path)
    enhancer, _, _ = make_enhancer(tmp_path, body='不是 JSON')
    asyncio.run(enhance_library(store, lib, enhancer))
    assert {e['enhance_error'] for e in review_entries(store, lib)} == {'invalid_json'}
    enhancer2, _, _ = make_enhancer(tmp_path, status=401)
    progress = asyncio.run(enhance_library(store, lib, enhancer2))
    assert progress.failed == 2
    dumped = json.dumps(review_entries(store, lib), ensure_ascii=False)
    assert KEY not in dumped and 'sk-fake' not in dumped
    assert {e['enhance_error'] for e in review_entries(store, lib)} == {'call_failed:INVALID_KEY'}


def test_create_app_injects_default_enhancer_but_keeps_injected(tmp_path):
    transport, _ = mock(split_json())
    app = create_app(tmp_path / 'a', config=dict(llm_transport=transport))
    assert isinstance(app.state.ctx.config['enhancer'], DeepSeekEnhancer)
    assert app.state.ctx.config['enhancer'].transport is transport
    fake = object()
    app2 = create_app(tmp_path / 'b', config=dict(enhancer=fake))
    assert app2.state.ctx.config['enhancer'] is fake
