"""M15-3 用量统计与预算降级。费用均为按公开单价的估算，非计费硬保证。"""
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime

DISCLAIMER = '估算，非计费硬保证'
ASR_PRICE_NOTE = '云 ASR 单价待填写真实单价；当前值为估算占位'
UNITS_NOTE = '口径：ASR=秒；搜索=次；LLM=估算 token（约 2 字符/token 折算）；单价均为占位估算'
DEFAULT_ASR_DAILY = 5.0    # 估算，待真实单价
DEFAULT_ASR_SESSION = 1.5  # 估算，待真实单价


def estimate_cost(prices, service, units):
    """units：asr=秒，llm=token，search=次。"""
    if service == 'asr':
        return units / 3600 * prices['asr_per_hour']
    if service == 'llm':
        return units / 1000 * prices['llm_per_1k_tokens']
    if service == 'search':
        return units * prices['search_per_call']
    raise ValueError('未知服务')


class UsageMeter:
    def __init__(self, db_path, prices, clock=time.time):
        self.db_path, self._prices, self._prices_provider, self.clock = db_path, prices, None, clock
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage(date TEXT, service TEXT, calls INTEGER, units REAL, est_cost REAL, PRIMARY KEY(date, service))')

    def set_prices_provider(self, provider):
        """价格改为按当前配置读取（设置页保存后 /api/usage 与 guard 同口径）。"""
        self._prices_provider = provider

    @property
    def prices(self):
        return self._prices_provider() if self._prices_provider is not None else self._prices

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def today(self):
        return datetime.fromtimestamp(self.clock()).strftime('%Y-%m-%d')

    def record(self, service, units, calls=1):
        cost = estimate_cost(self.prices, service, units)
        with self._db() as db:
            db.execute('INSERT INTO usage VALUES(?,?,?,?,?) ON CONFLICT(date,service) DO UPDATE SET '
                       'calls=calls+excluded.calls, units=units+excluded.units, est_cost=est_cost+excluded.est_cost',
                       (self.today(), service, calls, units, cost))
        return cost

    def spent_today(self, service=None, exclude=None):
        """按服务过滤；ASR 独立限额时与搜索/模型互不挤占。"""
        sql, args = 'SELECT COALESCE(SUM(est_cost),0) FROM usage WHERE date=?', [self.today()]
        if service is not None:
            sql, args = sql + ' AND service=?', args + [service]
        elif exclude is not None:
            sql, args = sql + ' AND service<>?', args + [exclude]
        with self._db() as db:
            return db.execute(sql, args).fetchone()[0]

    def report(self):
        with self._db() as db:
            rows = [dict(date=d, service=s, calls=c, units=u, est_cost=e) for d, s, c, u, e in
                    db.execute('SELECT date,service,calls,units,est_cost FROM usage WHERE date=? ORDER BY service', (self.today(),))]
        prices = self.prices
        return dict(date=self.today(), rows=rows, est_cost_total=sum(r['est_cost'] for r in rows),
                    note=f'{DISCLAIMER}；{UNITS_NOTE}', units_note=UNITS_NOTE,
                    asr_price_note=prices.get('asr_note') or ASR_PRICE_NOTE)


@dataclass(eq=False)
class Reservation:
    ok: bool
    service: str = ''
    session: str = ''
    est_cost: float = 0.0
    reason: str = ''      # '' | inflight | daily | session
    message: str = ''


class BudgetGuard:
    """请求前 reserve（占在途名额与估算额度），结束 settle 核销或 cancel 释放。

    ASR 有独立日/场限额（budget.asr_daily_limit / asr_session_limit），只受自身额度约束，
    搜索/模型的超额不会挤停转写；限额与价格均可绑定当前配置，保存设置后下一场会话自动生效。
    """

    def __init__(self, meter, daily_limit, session_limit, max_inflight=4, *,
                 asr_daily_limit=DEFAULT_ASR_DAILY, asr_session_limit=DEFAULT_ASR_SESSION, limits=None):
        self.meter = meter
        self._base = dict(daily_limit=daily_limit, session_limit=session_limit, max_inflight=max_inflight,
                          asr_daily_limit=asr_daily_limit, asr_session_limit=asr_session_limit)
        self._limits = limits  # () -> budget 配置字典；None 用构造时的值（测试/未接线）
        self.lock = threading.Lock()
        self.inflight = []
        self.session_spent = {}  # (session, service) -> cost
        self.on_denied = None  # (Reservation, session) 回调：由 wiring 接到 health notice

    def bind_config(self, manager):
        """指向 ConfigManager：每次 reserve 读当前 config，无需重建 guard。"""
        self._limits = lambda: (getattr(manager, 'config', {}) or {}).get('budget', {})
        if hasattr(self.meter, 'set_prices_provider'):
            self.meter.set_prices_provider(lambda: manager.config['prices'])

    @property
    def max_inflight(self):
        return self._base['max_inflight']

    @max_inflight.setter
    def max_inflight(self, value):
        self._base['max_inflight'] = value

    def limits(self):
        cfg = (self._limits() if self._limits is not None else None) or {}
        return {k: cfg.get(k, v) for k, v in self._base.items()}

    def _deny(self, service, reason):
        if reason == 'inflight':
            msg = f'外部请求并发已达上限，本次改用本地处理（{DISCLAIMER}）'
        else:
            scope = '今日' if reason == 'daily' else '本场'
            msg = f'{scope}预算已用尽（{DISCLAIMER}），外部调用已停止，改用本地处理'
            if service == 'asr':
                msg = (f'{scope}云 ASR 预算已用尽（{DISCLAIMER}）：云 ASR 已停止，不再识别新题；'
                       '若本机有本地模型将回退本地转写')
        return Reservation(False, service, reason=reason, message=msg)

    def _spent_today(self, service):
        return (self.meter.spent_today(service='asr') if service == 'asr'
                else self.meter.spent_today(exclude='asr'))

    def _session_spent(self, session, service):
        if service == 'asr':
            return self.session_spent.get((session, 'asr'), 0)
        return sum(v for (s, svc), v in self.session_spent.items() if s == session and svc != 'asr')

    def _notify_denied(self, res, session):
        if self.on_denied is None:
            return
        try:
            self.on_denied(res, session)
        except Exception:  # 通知失败不影响预算判断
            pass

    def reserve(self, service, units, session=''):
        lim = self.limits()
        est = estimate_cost(self.meter.prices, service, units)
        is_asr = service == 'asr'
        with self.lock:
            res = self._decide(service, is_asr, est, session, lim)
        if not res.ok:  # 回调放在锁外：health 发布可能同步触发其他逻辑
            self._notify_denied(res, session)
        return res

    def _decide(self, service, is_asr, est, session, lim):
        if len(self.inflight) >= lim['max_inflight']:
            return self._deny(service, 'inflight')
        pending = sum(r.est_cost for r in self.inflight if (r.service == 'asr') == is_asr)
        daily = lim['asr_daily_limit'] if is_asr else lim['daily_limit']
        sess = lim['asr_session_limit'] if is_asr else lim['session_limit']
        if daily <= 0 or self._spent_today(service) + pending + est > daily:
            return self._deny(service, 'daily')
        pending_s = sum(r.est_cost for r in self.inflight
                        if r.session == session and (r.service == 'asr') == is_asr)
        if sess <= 0 or self._session_spent(session, service) + pending_s + est > sess:
            return self._deny(service, 'session')
        res = Reservation(True, service, session, est)
        self.inflight.append(res)
        return res

    def _release(self, res):
        with self.lock:
            if res in self.inflight:
                self.inflight.remove(res)
                return True
        return False

    def settle(self, res, units, calls=1):
        """按实际用量核销并入账。"""
        if not self._release(res):
            return 0.0
        cost = self.meter.record(res.service, units, calls)
        with self.lock:
            key = (res.session, res.service)
            self.session_spent[key] = self.session_spent.get(key, 0) + cost
        return cost

    def cancel(self, res):
        """请求未发出/失败且无用量：只释放预留。"""
        self._release(res)
