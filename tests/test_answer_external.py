"""答案环：联网 L3、模型补充 M09、预算与迟到归档。Mock 传输，无任何真实网络。"""
import asyncio

import pytest
from test_answer_routing import (BLOOM, FAKE_KEY, Q_BLOOM, Q_PARTIAL, SEEDS, Env, LLMT, SearchT, SpyGuard, llm_json,
                                 no_network, seed_md, web_hit)
from test_answer_loop import Q_REDIS, Q_TX

from app.realtime.answer import STATUS_TIE
from app.usage import UsageMeter

REDIS_LIB = {'H1-01': ('Redis持久化有哪几种方式', '先说结论：Redis有RDB和AOF两种持久化。我在甲项目用RDB每5分钟做一次快照，恢复耗时30秒。'),
             'H1-02': ('Kafka如何保证高吞吐', '先说结论：Kafka靠顺序写和批量发送。我在乙项目把吞吐提升到了99%。')}
UNAVAILABLE = '外部补充不可用'


# ---------- (d) 联网 L3 ----------
@pytest.mark.asyncio
async def test_d_web_points_with_url_elapsed_and_order(tmp_path):
    st = SearchT(BLOOM)
    env = Env(tmp_path, search=st, llm=False)
    await env.run(Q_BLOOM)
    sups = [e['payload'] for e in env.events('prompt_supplement')]
    assert st.calls == [Q_BLOOM] and len(sups) == 2
    assert [s['source_tag'] for s in sups] == ['外部参考'] * 2
    assert sups[0]['url'] == BLOOM[0]['url'] and sups[1]['url'] == BLOOM[1]['url']  # 权威来源在先
    assert all(isinstance(s['elapsed_ms'], int) for s in sups) and sups[0]['elapsed_ms'] <= sups[1]['elapsed_ms']
    seq = [e['event_seq'] for e in env.events('prompt_status', 'prompt_supplement')]
    assert seq == sorted(seq) and env.statuses()[0] == STATUS_TIE
    assert env.bus.snapshot(env.sid)['primary'] is None
    assert env.guard.count('reserve', 'search') == 1 and env.guard.count('settle', 'search') == 1
    assert env.health.payload()['search']['state'] == 'ok'
    row = [r for r in env.guard.meter.report()['rows'] if r['service'] == 'search']
    assert row and row[0]['calls'] == 1


@pytest.mark.asyncio
async def test_d2_web_non_point_outcomes_are_status_only(tmp_path):
    for i, results in enumerate(([], [web_hit('https://a.com/x', '无关内容完全不相关的一段文字说明')])):
        text = '暂无可靠的外部参考'
        env = Env(tmp_path / str(i), search=SearchT(results), llm=False)
        await env.run(Q_BLOOM)
        assert env.statuses()[-1] == text and not env.events('prompt_supplement')
        assert env.guard.count('settle') == 1
    env = Env(tmp_path / 'oserr', search=SearchT(exc=OSError('x')), llm=False)
    await env.run(Q_BLOOM)
    assert env.statuses()[-1] == UNAVAILABLE and not env.events('prompt_supplement')
    assert env.health.payload()['search'] == dict(state='degraded', reason='无法连接服务商，请检查网络', since=0.0)
    assert env.guard.count('cancel', 'search') == 1 and env.guard.count('settle') == 0 and env.guard.inflight == []


@pytest.mark.asyncio
async def test_d3_web_timeout_status(tmp_path):
    st = SearchT(BLOOM, gate=asyncio.Event())
    env = Env(tmp_path, search=st, llm=False)
    env.ext.timeouts = lambda: dict(web_first_ms=100, web_total_ms=100)
    await env.run(Q_BLOOM)
    assert env.statuses()[-1] == '外部补充超时' and env.health.payload()['search']['reason'] == '外部补充超时'
    assert env.guard.count('cancel', 'search') == 1


@pytest.mark.asyncio
async def test_d4_redaction_vocab_blocks_and_strips(tmp_path):
    lib = {'H1-01': ('怎么设计缓存', '我在电商项目里负责缓存设计。')}
    st = SearchT(BLOOM)
    env = Env(tmp_path / 'a', library=lib, search=st, llm=False)
    await env.run('电商项目里的布隆过滤器是什么')  # 命中主库专有词：不外发
    assert st.calls == [] and env.statuses()[-1] == '该问题不适合联网'
    assert env.guard.count('cancel', 'search') == 1
    st2 = SearchT(BLOOM)
    env2 = Env(tmp_path / 'b', library=lib, search=st2, llm=False, profile_alias='小林')
    await env2.run('小林说的布隆过滤器是什么')  # 回答者代号被删除后再外发
    assert st2.calls == ['说的布隆过滤器是什么']


# ---------- (e) 没有搜索传输 ----------
@pytest.mark.asyncio
async def test_e_no_search_transport_unavailable_zero_network(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    env = Env(tmp_path, search=None, llm=False)
    await env.run(Q_BLOOM)
    assert env.statuses() == [STATUS_TIE, UNAVAILABLE] and not env.events('prompt_supplement')
    assert env.health.payload()['search']['state'] == 'degraded'
    assert env.health.payload()['search']['reason'] == '未配置搜索传输'
    assert net == [] and env.guard.log == []
    assert env.events('health')


# ---------- (f) 迟到结果只归档 ----------
@pytest.mark.asyncio
async def test_f_web_late_after_answer_closed_only_archived(tmp_path):
    gate = asyncio.Event()
    st = SearchT(BLOOM, gate=gate)
    env = Env(tmp_path, search=st, llm=False)
    env.feed(Q_BLOOM)
    await st.started.wait()
    env.push('answer_closed', Q_BLOOM, reason='new_question')
    gate.set()
    await env.loop.drain()
    assert not env.events('prompt_supplement') and env.late() == [('t1', 'turn_closed', 'web')]
    assert env.guard.count('cancel', 'search') == 1 and env.guard.inflight == []


@pytest.mark.asyncio
async def test_f2_web_stale_after_next_question_and_rollback(tmp_path):
    gate = asyncio.Event()
    env = Env(tmp_path, search=SearchT(BLOOM, gate=gate), llm=False)
    env.feed(Q_BLOOM)
    await env.search.started.wait()
    env.feed(Q_TX)  # 下一题 commit
    gate.set()
    await env.loop.drain()
    assert [e['turn_id'] for e in env.events('prompt_supplement')] == [] and env.late() == [('t1', 'stale_turn', 'web')]
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L1'
    gate2 = asyncio.Event()
    env2 = Env(tmp_path / 'r', search=SearchT(BLOOM, gate=gate2), llm=False)
    env2.feed(Q_BLOOM)
    await env2.search.started.wait()
    env2.push('rolled_back', Q_BLOOM, ver=2)
    gate2.set()
    await env2.loop.drain()
    assert not env2.events('prompt_supplement') and env2.late() == [('t1', 'cancelled', 'web')]


@pytest.mark.asyncio
async def test_f3_model_late_after_answer_closed_only_archived(tmp_path):
    gate = asyncio.Event()
    lt = LLMT([llm_json(conclusion='公共结论', points=['一', '二', '三'])], gate=gate)
    env = Env(tmp_path, roles=['技术开发'], llm_t=lt, web=False)
    env.feed('什么是幂等性')
    await lt.started.wait()
    env.push('answer_closed', '什么是幂等性', reason='new_question')
    gate.set()
    await env.loop.drain()
    assert not env.events('prompt_supplement') and env.late() == [('t1', 'turn_closed', 'model')]
    assert env.guard.count('cancel', 'llm') == 1 and env.guard.count('settle') == 0


@pytest.mark.asyncio
async def test_f4_newer_web_job_supersedes_older(tmp_path):
    gate = asyncio.Event()
    st = SearchT(BLOOM, gate=gate)
    env = Env(tmp_path, search=st, llm=False)
    env.feed(Q_BLOOM)
    await st.started.wait()
    env.push('text_updated', Q_BLOOM + '呢', ver=2)  # 同题文本补全 -> 新查询取代旧任务
    await asyncio.sleep(0.05)  # 新路由完成、新任务启动、旧任务被取代
    gate.set()
    await env.loop.drain()
    assert env.late() == [('t1', 'superseded', 'web')]
    assert len(env.events('prompt_supplement')) == 2  # 只有新任务的结果


# ---------- (g) 预算 ----------
@pytest.mark.asyncio
async def test_g_budget_zero_denies_search_and_model_with_message(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    st, lt = SearchT(BLOOM), LLMT([llm_json(conclusion='x')])
    guard = lambda tp: SpyGuard(UsageMeter(tp / 'u.db', {'asr_per_hour': 1, 'llm_per_1k_tokens': 1, 'search_per_call': 1}), 0, 0, 4)
    env = Env(tmp_path, search=st, llm_t=lt, roles=['技术开发'], budget=guard(tmp_path))
    await env.run(Q_BLOOM)
    assert '预算已用尽' in env.statuses()[-1] and st.calls == []
    await env.run('什么是幂等性')  # L2 -> 模型补充也被拒
    assert '预算已用尽' in env.statuses()[-1] and lt.calls == [] and net == []
    assert env.guard.count('reserve') == 0 and env.guard.count('settle') == 0 and env.guard.inflight == []
    assert [x for x in env.guard.log if x[0] == 'reserve' and x[2]] == []


@pytest.mark.asyncio
async def test_g2_inflight_limit_message_and_pairing(tmp_path):
    gate = asyncio.Event()
    st, lt = SearchT(BLOOM, gate=gate), LLMT([llm_json(conclusion='公共结论')])
    env = Env(tmp_path, library=REDIS_LIB, search=st, llm_t=lt)
    env.guard.max_inflight = 1
    env.feed('x', subs=(Q_PARTIAL, Q_BLOOM))  # 两个子句都要联网（L1p 的未覆盖部分也走 L3），顺序各占一个名额
    await st.started.wait()
    await asyncio.sleep(0.1)  # 联网占着唯一的在途名额，模型补充被拒
    assert any('并发已达上限' in t for t in env.statuses()) and lt.calls == []
    gate.set()
    await env.loop.drain()
    assert env.guard.inflight == [] and env.guard.count('reserve', 'search') == 2 and env.guard.count('settle', 'search') == 2


# ---------- (h) 模型补充 ----------
@pytest.mark.asyncio
async def test_h_l1_hit_never_calls_model(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    lt = LLMT([llm_json(conclusion='x')])
    env = Env(tmp_path, library=REDIS_LIB, llm_t=lt, search=SearchT(BLOOM))
    await env.run('Redis持久化有哪几种方式')
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L1' and lt.calls == [] and net == []
    assert env.statuses() == [] and not env.events('prompt_supplement')


@pytest.mark.asyncio
async def test_h2_partial_hit_model_supplement_validates_personal_facts(tmp_path):
    chunks = [llm_json(
        conclusion='Redis可以用RDB快照或AOF日志做持久化', points=['RDB按时间点快照', 'AOF记录写命令', 'Redis默认端口6379'],
        example='', caution='',
        facts_used=[dict(entry_id='H1-01', text='我在甲项目用RDB每5分钟做一次快照。'),   # 条目原文：通过
                    dict(entry_id='H1-01', text='我在甲项目把吞吐提升到了99%。')])]  # 99% 只在 H1-02：跨条目拼接
    lt = LLMT(chunks)
    env = Env(tmp_path, library=REDIS_LIB, llm_t=lt, web=False)
    await env.run(Q_PARTIAL)
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['level'] == 'L1p' and snap['primary']['source_tag'] == '部分命中，非标准答案'
    assert env.statuses()[0].startswith('待补充：')
    contents = [s['content'] for s in snap['supplements']]
    tags = {s['content']: s['source_tag'] for s in snap['supplements']}
    assert not any('6379' in c for c in contents)  # 公共块数字不在引用片段中：宁丢不拼
    assert '我在甲项目用RDB每5分钟做一次快照。' in contents and tags['我在甲项目用RDB每5分钟做一次快照。'] == '部分命中，非标准答案'
    assert not any('99%' in c for c in contents) and '[需本人补充]' in contents
    assert all(s['source_tag'] in ('通用建议', '部分命中，非标准答案', '需本人补充') for s in snap['supplements'])
    body = lt.calls[0]
    user = body['messages'][1]['content']
    assert 'Kafka' not in user and '99%' not in user  # 只发检索到的片段，不发整库
    assert FAKE_KEY not in str(body)
    assert env.guard.count('settle', 'llm') == 1 and env.health.payload()['llm']['state'] == 'ok'


@pytest.mark.asyncio
async def test_h2b_public_numbers_must_appear_in_cited_fragments(tmp_path):
    """公共块里的数字必须能在引用片段中找到；找不到整块丢弃（宁丢不拼）。"""
    chunks = [llm_json(conclusion='公共结论', points=['Redis默认端口6379', 'RDB每5分钟快照'],
                       example='', caution='', facts_used=[])]
    env = Env(tmp_path, library=REDIS_LIB, llm_t=LLMT(chunks), web=False)
    await env.run(Q_PARTIAL)
    contents = [s['content'] for s in env.bus.snapshot(env.sid)['supplements']]
    assert not any('6379' in c for c in contents)
    assert any('RDB每5分钟快照' in c for c in contents)


@pytest.mark.asyncio
async def test_h3_l2_model_supplement_personal_facts_always_placeholder(tmp_path):
    lt = LLMT([llm_json(conclusion='幂等的关键是去重', facts_used=[dict(entry_id='QTEC-01', text='我在甲项目做过幂等')])])
    env = Env(tmp_path, roles=['技术开发'], llm_t=lt, web=False)
    await env.run('什么是幂等性')
    sups = env.bus.snapshot(env.sid)['supplements']
    assert [s['content'] for s in sups] == ['幂等的关键是去重', '[需本人补充]'] and sups[1]['source_tag'] == '需本人补充'
    assert env.bus.snapshot(env.sid)['primary']['level'] == 'L2'  # 不替换主答案


@pytest.mark.asyncio
async def test_h4_web_points_go_through_model_with_only_snippets(tmp_path):
    lt = LLMT([llm_json(conclusion='布隆过滤器用位数组判断存在性')])
    env = Env(tmp_path, search=SearchT(BLOOM), llm_t=lt)
    await env.run(Q_BLOOM)
    tags = [e['payload']['source_tag'] for e in env.events('prompt_supplement')]
    assert tags == ['外部参考', '外部参考', '通用建议']
    user = lt.calls[0]['messages'][1]['content']
    assert '[web1]' in user and '[web2]' in user and 'H1-01' not in user


@pytest.mark.asyncio
async def test_h5_model_failure_cancels_lease_and_marks_llm_degraded(tmp_path):
    import httpx
    lt = LLMT()
    lt.transport = httpx.MockTransport(lambda r: httpx.Response(401, text='bad'))
    env = Env(tmp_path, roles=['技术开发'], llm_t=lt, web=False)
    await env.run('什么是幂等性')
    assert not env.events('prompt_supplement') and env.guard.count('cancel', 'llm') == 1 and env.guard.count('settle') == 0
    assert env.health.payload()['llm']['reason'] == 'API Key 无效或已删除'


@pytest.mark.asyncio
async def test_h6_warmup_once_with_minimal_context(tmp_path):
    lt = LLMT(['就绪'])
    env = Env(tmp_path, roles=['技术开发'], llm_t=lt, warmup=True)
    await env.loop.drain()
    assert len(lt.calls) == 1 and lt.calls[0]['messages'][1]['content'] == '就绪' and '技术开发' not in lt.calls[0]['messages'][1]['content']
    assert '【岗位】技术开发' in lt.calls[0]['messages'][0]['content'] and env.guard.count('settle', 'llm') == 1
    assert 'Redis' not in str(lt.calls[0])
    lt2 = LLMT(['就绪'])
    off = Env(tmp_path / 'b', llm_t=lt2, warmup=True, key=False)
    await off.loop.drain()
    assert lt2.calls == []


@pytest.mark.asyncio
async def test_h7_warmup_budget_and_failure_paths(tmp_path):
    import httpx
    lt = LLMT()
    lt.transport = httpx.MockTransport(lambda r: httpx.Response(429, text='slow'))
    env = Env(tmp_path, llm_t=lt, warmup=True)
    await env.loop.drain()
    assert env.guard.count('cancel', 'llm') == 1 and env.health.payload()['llm']['reason'] == '请求过于频繁'
    denied = Env(tmp_path / 'd', llm_t=LLMT(['x']), warmup=True,
                 budget=SpyGuard(UsageMeter(tmp_path / 'd.db', {'asr_per_hour': 1, 'llm_per_1k_tokens': 1, 'search_per_call': 1}), 0, 0, 4))
    await denied.loop.drain()
    assert denied.llm.calls == []


# ---------- (i) 外发开关 ----------
@pytest.mark.asyncio
async def test_i_switches_off_zero_calls(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    st, lt = SearchT(BLOOM), LLMT([llm_json(conclusion='x')])
    env = Env(tmp_path, roles=['技术开发'], search=st, llm_t=lt, web=False, llm=False)
    await env.run(Q_BLOOM)
    await env.run('什么是幂等性')
    await env.run(Q_PARTIAL)
    assert st.calls == [] and lt.calls == [] and net == [] and env.guard.log == []
    assert '联网检索已关闭' in env.statuses()


@pytest.mark.asyncio
async def test_i2_no_key_or_no_transport_no_model_call(tmp_path, monkeypatch):
    net = no_network(monkeypatch)
    lt = LLMT([llm_json(conclusion='x')])
    nokey = Env(tmp_path / 'a', roles=['技术开发'], llm_t=lt, key=False, web=False)
    await nokey.run('什么是幂等性')
    assert lt.calls == [] and nokey.bus.snapshot(nokey.sid)['primary']['level'] == 'L2'  # 规则提纲即最终结果
    notrans = Env(tmp_path / 'b', roles=['技术开发'], llm_t=None, web=False)
    await notrans.run('什么是幂等性')
    await notrans.run(Q_BLOOM)
    assert notrans.health.payload()['llm'] == dict(state='degraded', reason='未配置模型传输', since=0.0)
    assert net == [] and notrans.guard.log == []
    assert len([e for e in notrans.events('health') if 'llm' in e['payload']]) == 1  # 只上报一次


# ---------- 关闭 / 接线 ----------
@pytest.mark.asyncio
async def test_aclose_cancels_inflight_web_and_releases_lease(tmp_path):
    st = SearchT(BLOOM, gate=asyncio.Event())
    env = Env(tmp_path, search=st, llm=False)
    env.feed(Q_BLOOM)
    await st.started.wait()
    assert len(env.guard.inflight) == 1
    await env.loop.aclose()
    assert env.guard.inflight == [] and env.guard.count('cancel', 'search') == 1 and not env.events('prompt_supplement')
    env.feed(Q_TX)  # 已关闭：不再处理
    assert not env.events('prompt_primary')


def test_server_wires_externals_end_to_end(tmp_path, monkeypatch):
    import test_answer_loop as tl
    st = SearchT(BLOOM)
    real = tl.create_app
    monkeypatch.setattr(tl, 'create_app', lambda *a, **k: real(*a, config={'search_transport': st}, **k))
    run = tl.Run(tmp_path, Q_BLOOM)
    events = run.play(lambda e: e['type'] == 'prompt_supplement')
    sup = events[-1]['payload']
    assert st.calls == [Q_BLOOM] and sup['source_tag'] == '外部参考' and sup['url'] == BLOOM[0]['url'] and sup['elapsed_ms'] >= 0
    health = run.app.state.wiring.sessions[run.sid].payload()
    assert health['llm']['state'] == 'down' and health['llm']['reason'] == '配置缺失'  # 无 key：沿用 wiring 的看板状态
    run.stop()
    assert run.app.state.usage_meter.report()['rows'][0]['service'] == 'search'
