"""答案环的外部依赖束与预算租约：次优库、外发开关、凭证、预算、health、搜索/模型传输。

传输由调用方（server/测试）注入；没有注入就不外发，本模块不创建任何网络对象。
"""
from dataclasses import dataclass


@dataclass
class Externals:
    secondary: object = None  # SecondaryLibrary
    outbound: object = None  # () -> {'llm_snippets': bool, 'web_questions': bool}
    timeouts: object = None  # () -> {'web_first_ms': int, 'web_total_ms': int}
    creds: object = None  # CredentialStore
    budget: object = None  # BudgetGuard
    health: object = None  # () -> SessionHealth
    search_transport: object = None
    llm_transport: object = None
    llm_live: bool = False  # True：允许用 httpx 默认真实传输（无注入 transport 时也不短路）
    llm_base_url: str = 'https://api.deepseek.com'
    llm_first_timeout_s: float = 3.0
    llm_timeout_s: float = 8.0
    model: str = 'deepseek-flash'
    warmup: bool = True
    search_live: bool = False  # True：无注入 transport 时使用 DeepSeek Search 真实传输
    search_endpoint: str = 'https://api.deepseek.com/v1/tools/search'
    search_count: int = 5
    search_need_content: bool = False

    def switches(self):
        return (self.outbound() if self.outbound else None) or {}

    def web_timeouts(self):
        t = (self.timeouts() if self.timeouts else None) or {}
        return t.get('web_first_ms', 1500), t.get('web_total_ms', 8000)

    def key(self, *names):
        return next((v for v in (self.creds.get(n) for n in names) if v), '') if self.creds else ''

    def report(self, name, state, reason=''):
        rt = self.health() if self.health else None
        if rt is not None:
            rt.set_capability(name, state, reason)


class Lease:
    """reserve 的结果：成功 settle，失败/取消 cancel；两者只生效一次。guard 为 None 时恒放行（无预算接线）。"""

    def __init__(self, guard, res):
        self.guard, self.res, self.closed = guard, res, False
        self.ok = res is None or res.ok
        self.message = '' if res is None else res.message

    def settle(self, units):
        if self.ok and not self.closed and self.guard is not None:
            self.closed = True
            self.guard.settle(self.res, units)

    def cancel(self):
        if self.ok and not self.closed and self.guard is not None:
            self.closed = True
            self.guard.cancel(self.res)


def take(guard, service, units, session):
    return Lease(guard, guard.reserve(service, units, session) if guard is not None else None)
