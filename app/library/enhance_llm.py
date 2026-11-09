"""DeepSeek 增强器：把单条问答整理成 enhance.validate_split 约定的 JSON。

边界：提示词与校验沿用 enhance.py（只整理、不创作；事实由 facts_extract 做同条目出处比对）。
Key 只从 CredentialStore 读取，不进日志/异常/返回；受 outbound.llm_snippets 与 BudgetGuard 约束。
"""
from ..llm.client import DeepSeekClient, LLMError
from ..llm.parse import extract_json
from ..privacy.redact import DEFAULT_RULES
from ..realtime.answer_ext import take
from .enhance import EnhancerUnavailable, build_messages

MAX_TOKENS = 1200
# 只遮盖证件/银行卡/手机号/邮箱。公司名、项目名不能遮：事实校验要拿模型给出的原文片段回原条目比对。
PII_RULES = tuple(pat for kind, pat in DEFAULT_RULES if kind != 'company')


def mask_pii(text):
    for pat in PII_RULES:
        text = pat.sub('[已遮盖]', text)
    return text


class DeepSeekEnhancer:
    """config 为可调用对象（返回当前配置），便于设置页修改后即时生效。"""

    def __init__(self, creds, budget=None, config=None, transport=None):
        self.creds, self.budget, self.config, self.transport = creds, budget, config, transport

    def __repr__(self):
        return f'DeepSeekEnhancer(transport={type(self.transport).__name__})'

    def _settings(self):
        cfg = (self.config() if self.config else None) or {}
        model = cfg.get('model') or {}
        timeouts = cfg.get('timeouts') or {}
        return dict(outbound=cfg.get('outbound') or {},
                    model=model.get('name') or 'deepseek-flash',
                    base_url=model.get('base_url') or 'https://api.deepseek.com',
                    first_timeout=timeouts.get('llm_first_ms', 3000) / 1000,
                    total_timeout=timeouts.get('llm_total_ms', 8000) / 1000)

    async def split(self, entry_text):
        cfg = self._settings()
        if not cfg['outbound'].get('llm_snippets'):
            raise EnhancerUnavailable('outbound_disabled')
        key = self.creds.get('DEEPSEEK_API_KEY') if self.creds else ''
        if not key:
            raise EnhancerUnavailable('no_key')
        messages = build_messages(mask_pii(entry_text))
        est = sum(len(m['content']) for m in messages) + MAX_TOKENS
        lease = take(self.budget, 'llm', est, 'library-enhance')
        if not lease.ok:
            raise EnhancerUnavailable('budget_denied')
        client = DeepSeekClient(key, base_url=cfg['base_url'], model=cfg['model'], transport=self.transport,
                                first_token_timeout=cfg['first_timeout'], total_timeout=cfg['total_timeout'])
        try:
            text = await client.complete(messages, temperature=0.1, max_tokens=MAX_TOKENS)
        except LLMError as exc:
            lease.cancel()
            raise EnhancerUnavailable('call_failed:' + exc.code) from None
        except BaseException:
            lease.cancel()
            raise
        data = extract_json(text)
        if data is None:
            lease.cancel()
            raise EnhancerUnavailable('invalid_json')
        lease.settle(max(0, est - MAX_TOKENS + len(text)))
        return data
