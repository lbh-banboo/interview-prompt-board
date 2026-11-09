"""答案环接入路由器 / 次优库 / 联网 / 模型补充 / 预算的测试。全部 tmp_path + HashEmbedder（SIMULATED）+ Mock 传输，不联网。"""
import asyncio
import itertools
import json
import socket
import sqlite3
from contextlib import closing

import httpx
import pytest
from test_answer_loop import A_REDIS, A_TX, FakePipe, Q_REDIS, Q_TX, metric_rows

from app.core import Store
from app.credentials import CredentialStore, MemoryBackend
from app.domain.turn_detector import TurnEvent
from app.health.capabilities import CapabilityBoard
from app.library.secondary import SecondaryLibrary
from app.realtime import answer
from app.realtime.answer import STATUS_DOWN, STATUS_PERSONAL, STATUS_TIE, AnswerLoop, IndexProvider
from app.realtime.answer_ext import Externals
from app.realtime.audio_ws import _StoreMetrics
from app.realtime.events import EventBus
from app.retrieval.index import HashEmbedder
from app.sessions.profiles import create_profile, ensure_profiles
from app.sessions.setup import create_session_v2, ensure_sessions_columns, mark_ready, start_session
from app.usage import BudgetGuard, UsageMeter
from app.wiring import SessionHealth

PRICES = dict(asr_per_hour=2.5, llm_per_1k_tokens=0.002, search_per_call=0.01)
FAKE_KEY = 'sk-' + 'x' * 20  # 假值，仅测试


def seed_md(*items):
    """items: (题号, 问题, 岗位标签, 结论)。"""
    out = []
    for qid, q, role, concl in items:
        out.append(f'### {qid} {q}\n变体:\n- 变体甲{q}\n- 变体乙{q}\n- 变体丙{q}\n结论: {concl}\n要点:\n- 要点一\n- 要点二\n- 要点三\n'
                   f'示例: [需本人补充：相关经历]\n注意: 不要空谈。\n岗位: {role}\n类型: 知识\n')
    return '\n'.join(out)


SEEDS = seed_md(('QTEC-01', '什么是幂等性', 'tech', '幂等是同一操作执行多次结果一致。'),
                ('QTEC-02', '什么是熔断降级', 'tech', '熔断是在下游异常时快速失败。'),
                ('QOPS-01', '如何做用户增长复盘', 'ops', '复盘先对齐目标再拆解漏斗。'),
                ('QGEN-01', '你怎么看待加班', 'general', '先说原则再说边界。'))
PRIMARY = {'H1-01': (Q_TX, A_TX), 'H1-02': (Q_REDIS, A_REDIS)}


class SearchT:
    def __init__(self, results=None, exc=None, gate=None):
        self.results, self.exc, self.gate, self.calls = results or [], exc, gate, []
        self.started = asyncio.Event()

    async def __call__(self, query, *, api_key, timeout_s):
        self.calls.append(query)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        return self.results


class LLMT:
    """httpx.MockTransport 的 SSE 应答；calls 记录请求体。"""

    def __init__(self, chunks=(), gate=None):
        self.chunks, self.gate, self.calls = list(chunks), gate, []
        self.started = asyncio.Event()
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request):
        self.calls.append(json.loads(request.content))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        body = ''.join('data: ' + json.dumps({'choices': [{'delta': {'content': c}}]}, ensure_ascii=False) + '\n\n'
                       for c in self.chunks) + 'data: [DONE]\n\n'
        return httpx.Response(200, content=body.encode(), headers={'content-type': 'text/event-stream'})


def llm_json(**kw):
    obj = dict(conclusion='', points=[], example='', caution='', facts_used=[])
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


class SpyGuard(BudgetGuard):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.log = []

    def reserve(self, service, units, session=''):
        r = super().reserve(service, units, session)
        self.log.append(('reserve', service, r.ok))
        return r

    def settle(self, res, units, calls=1):
        self.log.append(('settle', res.service))
        return super().settle(res, units, calls)

    def cancel(self, res):
        self.log.append(('cancel', res.service))
        super().cancel(res)

    def count(self, what, service=None):
        return len([x for x in self.log if x[0] == what and (service is None or x[1] == service) and (what != 'reserve' or x[2])])


def counter_ns(step=10_000_000):
    c = itertools.count()
    return lambda: next(c) * step


class Env:
    def __init__(self, tmp_path, *, roles=None, seeds=SEEDS, approve=('QTEC-01', 'QOPS-01', 'QGEN-01'), web=True, llm=True,
                 key=True, search=None, llm_t=None, budget=True, library=None, embedder='hash', ns=None, search_fn=None,
                 warmup=False, profile_alias=None, empty_primary=False, secondary_only=False):
        self.store = Store(tmp_path)
        ensure_sessions_columns(self.store)
        entries = library or PRIMARY
        text = '\n'.join(f'**{k}｜00:0{i}｜{q}** 参考回答：{a}' for i, (k, (q, a)) in enumerate(entries.items(), 1))
        self.lib = self.store.import_text('t', text)['id']
        for k, (_, a) in entries.items():
            self.store.review(self.lib, k, a, 'approved', True)
        if empty_primary:  # 主库为空：走 setup 会话（library_id=''），由 secondary_only 决定次优库/联网
            ensure_profiles(self.store)
            pid = create_profile(self.store, profile_alias or '我')['id']
            self.lib = ''
            self.sid = create_session_v2(self.store, 'online', pid, roles or ['技术开发'], [], True,
                                         secondary_only=secondary_only)['id']
            mark_ready(self.store, self.sid, dict(items=[], overall='PASS', has_fail=False,
                                                  can_degrade_start=False, degraded_banner=None))
            start_session(self.store, self.sid)
        else:
            self.sid = self.store.start('online', self.lib)['id']
            if roles is not None:
                with self.store.connect() as db:
                    db.execute('UPDATE sessions SET roles_json=? WHERE id=?', (json.dumps(roles, ensure_ascii=False), self.sid))
            if profile_alias:
                ensure_profiles(self.store)
                pid = create_profile(self.store, profile_alias)['id']
                with self.store.connect() as db:
                    db.execute('UPDATE sessions SET profile_id=? WHERE id=?', (pid, self.sid))
        self.sec = None
        if seeds:
            sdir = tmp_path / 'seeds'
            sdir.mkdir()
            (sdir / 'tech.md').write_text(seeds, 'utf-8')
            self.sec = SecondaryLibrary(self.store)
            assert not self.sec.import_dir(sdir)['errors']
            for i in approve:
                self.sec.review(i, 'approved', True)
        self.bus, self.pipe = EventBus(), FakePipe()
        self.search, self.llm = search, llm_t
        self.guard = SpyGuard(UsageMeter(self.store.path, PRICES), 20.0, 5.0, 4) if budget is True else budget
        self.health = SessionHealth(self.sid, self.bus, lambda: 0.0)
        creds = CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': FAKE_KEY} if key else {})])
        self.ext = Externals(secondary=self.sec, outbound=lambda: dict(web_questions=web, llm_snippets=llm),
                             timeouts=lambda: dict(web_first_ms=1500, web_total_ms=8000), creds=creds, budget=self.guard,
                             health=lambda: self.health, search_transport=search,
                             llm_transport=llm_t.transport if llm_t else None, warmup=warmup)
        self.provider = IndexProvider(self.store, HashEmbedder() if embedder == 'hash' else embedder)
        if self.lib:  # setup 空主库会话不预建索引（与主线 /api/setup 一致）
            self.provider.prepare(self.lib)
        self.loop = AnswerLoop(self.sid, self.store, self.bus, self.provider, _StoreMetrics(self.store), pipe=self.pipe,
                               search_fn=search_fn, ns=ns or counter_ns(), ext=self.ext)
        self.loop.attach()

    def push(self, type_, text, subs=(), ver=1, reason='r'):
        self.pipe.turn_events.append(TurnEvent(type_, text, ver, 1000, reason, kind='question', completeness='high',
                                               sub_questions=tuple(subs)))
        self.pipe.event_ns.append(self.loop.ns())
        self.bus.pull(self.sid, self.pipe)

    def feed(self, text, subs=None):
        self.push('committed', text, (text,) if subs is None else subs)

    def events(self, *types):
        return [e for e in self.bus.replay(self.sid, 0) if not types or e['type'] in types]

    def statuses(self):
        return [e['payload']['text'] for e in self.events('prompt_status')]

    def late(self):
        return [(r[0], r[3]['reason'], r[3]['label']) for r in metric_rows(self.store, self.sid, 'late_archived')]

    async def run(self, text, subs=None):
        self.feed(text, subs)
        await self.loop.drain()


def no_network(monkeypatch):
    """socket / httpx 一律抛错；返回命中次数列表。

    只拦 AF_INET / AF_INET6 的真正外发；AF_UNIX 属本机 IPC，宿主环境
    （覆盖率 .pth、IDE 注入的 broker.sock）会在测试进程里连它，不算联网，放行。
    """
    hits = []
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **k):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            hits.append((self.family, address))
            raise AssertionError('不得联网')
        return real_connect(self, address, *a, **k)

    def boom(*a, **k):
        hits.append(a)
        raise AssertionError('不得联网')

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket, 'create_connection', boom)
    monkeypatch.setattr(socket, 'getaddrinfo', boom)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', boom)
    return hits


def web_hit(url, text, title='参考'):
    return dict(title=title, url=url, snippet=text)


BLOOM = [web_hit('https://zh.wikipedia.org/wiki/bloom', '布隆过滤器是一种空间效率很高的概率型数据结构，用于判断元素是否存在', '布隆过滤器'),
         web_hit('https://blog.example.com/bloom', '布隆过滤器通过多个哈希函数和位数组判断元素可能存在或一定不存在', '布隆过滤器详解')]
Q_BLOOM = '什么是布隆过滤器'
Q_PARTIAL = 'Redis持久化有哪些方式'  # HashEmbedder 下 cos≈0.79：介于 tau_mid 与 tau_cos 之间 -> L1p


# ---------- (a) 多子问 ----------
@pytest.mark.asyncio
async def test_a_multi_subquestions_primary_first_rest_supplement(tmp_path):
    env = Env(tmp_path, roles=['技术开发'])
    await env.run(f'{Q_TX}？什么是幂等性？{Q_REDIS}？', subs=(Q_TX, '什么是幂等性', Q_REDIS))
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['level'] == 'L1' and snap['primary']['outline']['conclusion'].startswith('先说结论：分布式事务')
    sups = snap['supplements']
    assert [s['source_tag'] for s in sups] == ['通用建议', '我的口径']
    assert sups[0]['content'].startswith('什么是幂等性：') and sups[1]['content'].startswith(Q_REDIS)
    seqs = {e['type']: e['event_seq'] for e in env.events('prompt_primary')}
    assert seqs['prompt_primary'] < min(e['event_seq'] for e in env.events('prompt_supplement'))
    # 同一文本再来一次（text_updated）：主答案相同直接去重，补充不重复
    env.push('text_updated', f'{Q_TX}？什么是幂等性？{Q_REDIS}？', ver=2)
    await env.loop.drain()
    assert len(env.events('prompt_primary')) == 1 and len(env.bus.snapshot(env.sid)['supplements']) == 2


@pytest.mark.asyncio
async def test_a2_primary_follows_first_subquestion(tmp_path):
    env = Env(tmp_path)
    await env.run('x', subs=(Q_REDIS, Q_TX))
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['outline']['conclusion'].startswith('先说结论：Redis')
    assert snap['supplements'][0]['content'].startswith(Q_TX)


@pytest.mark.asyncio
async def test_a3_first_subquestion_miss_later_hit_becomes_primary(tmp_path):
    """首个子问未命中时，主答案取第一个真正命中的子问（避免整题没有主答案）。"""
    env = Env(tmp_path, web=False)
    await env.run('x', subs=('今天上海的天气怎么样呢', Q_TX))
    snap = env.bus.snapshot(env.sid)
    assert snap['primary'] and snap['primary']['level'] == 'L1'
    assert snap['primary']['outline']['conclusion'].startswith('先说结论：分布式事务')
    assert all('分布式事务' not in s['content'] for s in snap['supplements'])


@pytest.mark.asyncio
async def test_a4_non_first_tie_goes_to_supplement(tmp_path):
    lib = {'H1-01': ('请介绍一下消息队列的方案甲', '甲方案。'), 'H1-02': ('请介绍一下消息队列的方案乙', '乙方案。'), 'H1-03': (Q_TX, A_TX)}
    env = Env(tmp_path, library=lib, web=False)
    await env.run('x', subs=(Q_TX, '请介绍一下消息队列的方案'))
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L1'
    sup = env.events('prompt_supplement')[0]['payload']
    assert sup['source_tag'] == '候选，未确认' and '方案甲' in sup['content'] and '方案乙' in sup['content']
    assert not env.events('candidate')


# ---------- (b) 次优库与岗位隔离 ----------
@pytest.mark.asyncio
async def test_b_l2_hit_labeled_general_advice_and_role_isolation(tmp_path):
    tech = Env(tmp_path / 'tech', roles=['技术开发'])
    await tech.run('什么是幂等性')
    p = tech.bus.snapshot(tech.sid)['primary']
    assert p['level'] == 'L2' and p['source_tag'] == '通用建议' and p['outline']['conclusion'] == '幂等是同一操作执行多次结果一致。'
    assert p['outline']['points'] == ['要点一', '要点二', '要点三']
    await tech.run('如何做用户增长复盘')  # 运营专属：技术会话不命中
    assert STATUS_TIE in tech.statuses()
    ops = Env(tmp_path / 'ops', roles=['运营'])
    await ops.run('什么是幂等性')  # 技术专属：运营会话不命中
    assert ops.bus.snapshot(ops.sid)['primary'] is None
    await ops.run('你怎么看待加班')  # 通用题任何岗位可见（这里是观点题，L2）
    assert ops.bus.snapshot(ops.sid)['primary']['source_tag'] == '通用建议'


@pytest.mark.asyncio
async def test_b2_pending_secondary_never_hit_and_no_entries_no_error(tmp_path):
    env = Env(tmp_path, roles=['技术开发'])
    await env.run('什么是熔断降级')  # QTEC-02 未审核
    assert env.bus.snapshot(env.sid)['primary'] is None
    empty = Env(tmp_path / 'e', approve=())
    await empty.run('什么是幂等性')
    assert empty.bus.snapshot(empty.sid)['primary'] is None and STATUS_TIE in empty.statuses()
    none = Env(tmp_path / 'n', seeds=None)
    await none.run('什么是幂等性')
    assert none.bus.snapshot(none.sid)['primary'] is None


@pytest.mark.asyncio
async def test_b3_secondary_build_failure_only_loses_l2(tmp_path, monkeypatch):
    monkeypatch.setattr(SecondaryLibrary, 'index_entries', lambda self, roles: (_ for _ in ()).throw(RuntimeError('x')))
    env = Env(tmp_path, roles=['技术开发'])
    await env.run(Q_TX)
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L1'
    await env.run('什么是幂等性')
    assert env.bus.snapshot(env.sid)['primary'] is None


def test_role_tags_maps_labels_and_passes_unknown():
    from app.realtime.answer_secondary import role_tags
    assert role_tags(['技术开发', '运营', 'backend']) == ['backend', 'ops', 'tech'] and role_tags(None) == []


# ---------- (b2) 仅次优库 + 联网（主库为空） ----------
@pytest.mark.asyncio
async def test_b4_secondary_only_uses_l2_then_l3_without_transport(tmp_path):
    env = Env(tmp_path, roles=['技术开发'], empty_primary=True, secondary_only=True, search=None)
    await env.run('什么是幂等性')  # 次优库命中：标“通用建议”，不联网
    p = env.bus.snapshot(env.sid)['primary']
    assert p['level'] == 'L2' and p['source_tag'] == '通用建议'
    await env.run('什么是熔断降级')  # 次优库未审核：走 L3；无传输 -> 外部补充不可用
    assert env.statuses()[-1] == '外部补充不可用'
    assert env.bus.snapshot(env.sid)['primary'] is None


@pytest.mark.asyncio
async def test_b5_secondary_only_personal_never_web(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    st = SearchT(BLOOM)
    env = Env(tmp_path, roles=['技术开发'], empty_primary=True, secondary_only=True, search=st, llm_t=LLMT([]))
    await env.run('你负责的项目里缓存怎么设计的')
    assert env.statuses() == [STATUS_PERSONAL]
    assert not env.events('prompt_primary', 'prompt_supplement') and st.calls == [] and net == []


@pytest.mark.asyncio
async def test_b6_empty_primary_without_secondary_only_still_reports_down(tmp_path):
    env = Env(tmp_path, roles=['技术开发'], empty_primary=True, secondary_only=False)
    await env.run('什么是幂等性')
    assert env.statuses() == [STATUS_DOWN]


@pytest.mark.asyncio
async def test_b7_secondary_only_ignores_selected_primary(tmp_path):
    env = Env(tmp_path, roles=['技术开发'], search=None)
    env.loop._secondary_only = True  # 勾选了“仅用次优库和联网”，即使本场也选了主库也不用它
    await env.run(Q_TX)
    assert env.bus.snapshot(env.sid)['primary'] is None and env.statuses()[-1] == '外部补充不可用'


# ---------- (c) 个人经历 / 行为情景 ----------
@pytest.mark.asyncio
async def test_c_personal_miss_status_only_zero_external_calls(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    st, lt = SearchT(BLOOM), LLMT([llm_json(conclusion='x')])
    env = Env(tmp_path, search=st, llm_t=lt, roles=['技术开发'])
    for q in ('你负责的项目里缓存怎么设计的', '举个例子说说你遇到过的线上故障'):
        await env.run(q)
    assert env.statuses() == [STATUS_PERSONAL] * 2 and STATUS_PERSONAL == answer.PERSONAL_NOTE
    assert not env.events('prompt_primary', 'prompt_supplement')
    assert st.calls == [] and lt.calls == [] and net == [] and env.guard.log == []


@pytest.mark.asyncio
async def test_c2_loop_refuses_web_for_personal_even_if_router_flagged(tmp_path):
    st = SearchT(BLOOM)

    def forged(retr, text):
        d = retr.route(text)
        for sd in d.subs:
            sd.need_web = True
        return d

    env = Env(tmp_path, search=st, search_fn=forged)
    await env.run('你负责的项目里缓存怎么设计的')
    assert st.calls == [] and env.guard.log == []


# ---------- 降级 / 检索不可用 ----------
@pytest.mark.asyncio
async def test_degraded_fts_only_candidates_never_l1(tmp_path, monkeypatch):
    monkeypatch.setattr(answer, '_real_embedder', [None])
    env = Env(tmp_path, embedder=None, roles=['技术开发'], web=False)
    assert env.provider.state == 'degraded'
    await env.run(Q_TX)
    cand = env.events('candidate')[-1]['payload']
    assert not env.events('prompt_primary') and [h['entry_id'] for h in cand['hits']] == ['H1-01']
    assert STATUS_TIE in env.statuses()
    await env.run('什么是幂等性')  # 降级不建次优索引：L2 不命中
    assert not env.events('prompt_primary')
    await env.run('今天上海的天气怎么样呢')
    assert env.statuses()[-1] == '联网检索已关闭'


@pytest.mark.asyncio
async def test_index_missing_and_route_failure_report_down(tmp_path):
    env = Env(tmp_path)
    env.loop._library = 'nope'
    await env.run(Q_TX)
    assert env.statuses() == [STATUS_DOWN]

    def boom(retr, text):
        raise RuntimeError('x')

    env2 = Env(tmp_path / 'b', search_fn=boom)
    await env2.run(Q_TX)
    assert env2.statuses() == [STATUS_DOWN] and env2.health.payload()['retrieval']['state'] == 'down'
    env3 = Env(tmp_path / 'c', search_fn=boom)
    env3.ext.health = None
    await env3.run(Q_TX)
    assert env3.statuses() == [STATUS_DOWN]


# ---------- (j) 已有规则：回答前刷新、回答后补充 ----------
@pytest.mark.asyncio
async def test_j_text_updated_after_answering_supplements_with_l2_label(tmp_path):
    env = Env(tmp_path, roles=['技术开发'])
    await env.run(Q_TX)
    env.pipe.detector.phase = 'answering'
    env.bus.pull(env.sid, env.pipe)
    env.push('text_updated', '什么是幂等性', ver=2)
    await env.loop.drain()
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['level'] == 'L1'
    assert snap['supplements'][-1]['source_tag'] == '通用建议·文本修正后命中'
    env.push('text_updated', '今天上海的天气怎么样呢', ver=3)  # 变差：不降级主答案
    await env.loop.drain()
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L1'


# ---------- (k) 确定性 ----------
@pytest.mark.asyncio
async def test_k_same_input_same_output_with_external(tmp_path):
    runs = []
    for name in 'ab':
        env = Env(tmp_path / name, roles=['技术开发'], search=SearchT(BLOOM), llm_t=LLMT([llm_json(conclusion='公共结论')]))
        for q in (Q_TX, '什么是幂等性', Q_BLOOM, '你负责的项目里缓存怎么设计的'):
            await env.run(q)
        runs.append([(e['type'], e['turn_id'], e['epoch'], e['payload']) for e in env.events() if e['type'].startswith(('prompt', 'cand'))])
    assert runs[0] == runs[1] and any(t == 'prompt_supplement' and p['url'] for t, _, _, p in runs[0])


def test_set_capability_publishes_only_on_change():
    bus = EventBus()
    rt = SessionHealth('s', bus, lambda: 1.0)
    rt.set_capability('search', 'degraded', '原因')
    rt.set_capability('search', 'degraded', '原因')
    assert len(bus.replay('s', 0)) == 1 and rt.payload()['search']['state'] == 'degraded'
    rt.set_capability('search', 'degraded', '另一个原因')
    rt.set_capability('search', 'ok')
    rt.set_capability('search', 'ok')
    assert len(bus.replay('s', 0)) == 3 and rt.payload()['search']['state'] == CapabilityBoard(lambda: 0).state('search')['state']


@pytest.mark.asyncio
async def test_l1p_primary_is_not_labelled_as_my_voice(tmp_path):
    """L1p 是“部分命中，非标准答案”：主答案来源标签不得冒充“我的口径”（L7 Q16）。"""
    env = Env(tmp_path)
    await env.run(Q_PARTIAL)
    p = env.bus.snapshot(env.sid)['primary']
    assert p['level'] == 'L1p', p
    assert p['source_tag'] == '部分命中，非标准答案', p['source_tag']
