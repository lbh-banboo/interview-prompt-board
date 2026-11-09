"""S1b 提示闭环：committed -> 检索 -> prompt_primary / prompt_status / candidate -> display_ack。
全部临时库（tmp_path）+ HashEmbedder（SIMULATED）+ MockASR，不读 data/，不联网。
"""
import asyncio
import json
import math
import socket
import sqlite3
import sys
import struct
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.asr.mock import MockASR
from app.audio.frame import pack
from app.audio.vad import EnergyVad
from app.core import Store
from app.domain.turn_detector import TurnEvent
from app.realtime import answer
from app.realtime.answer import (AnswerLoop, IndexProvider, STATUS_PERSONAL, STATUS_TIE)
from app.realtime.audio_ws import _StoreMetrics
from app.realtime.events import EventBus
from app.retrieval.index import HashEmbedder
from app.server import create_app

Q_TX = '你能讲讲分布式事务的区别吗'
Q_REDIS = 'Redis持久化有哪几种方式'
A_TX = '先说结论：分布式事务常用两阶段提交和补偿方案。第一，两阶段提交强一致但有阻塞。第二，补偿方案最终一致。'
A_REDIS = '先说结论：Redis有RDB和AOF两种持久化。RDB是快照。AOF是追加日志。'
LIB = '\n'.join([
    f'**H1-01｜00:01｜{Q_TX}** 参考回答：{A_TX}',
    f'**H1-02｜00:02｜{Q_REDIS}** 参考回答：{A_REDIS}',
    '**H1-03｜00:03｜如何做数据库分库分表** 参考回答：未审核的草稿答案。',
    '**H1-04｜00:04｜请介绍一下消息队列的方案甲** 参考回答：甲方案。',
    '**H1-05｜00:05｜请介绍一下消息队列的方案乙** 参考回答：乙方案。',
])
APPROVED = {'H1-01': A_TX, 'H1-02': A_REDIS, 'H1-04': '甲方案。', 'H1-05': '乙方案。'}
SPEECH = struct.pack('<320h', *[int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(320)])
SILENCE = b'\x00' * 640


def wire(seq, speech):
    return pack('meeting', 1, 0, seq, SPEECH if speech else SILENCE, t_client_us=seq * 20000)


def make_session(store):
    lib = store.import_text('t', LIB)['id']
    for k, ans in APPROVED.items():
        store.review(lib, k, ans, 'approved', True)
    return lib, store.start('online', lib)['id']


def metric_rows(store, sid, name=None):
    with closing(sqlite3.connect(store.path)) as db:
        rows = db.execute('SELECT turn_id, name, t_ns, value FROM metrics WHERE session_id=? ORDER BY rowid', (sid,)).fetchall()
    return [(t, n, ns, json.loads(v) if v and v.startswith('{') else v) for t, n, ns, v in rows if name in (None, n)]


# ---------- 端到端：音频 WS -> board WS ----------
class Run:
    """脚本化 MockASR + 合成音频帧走 /ws/audio，board WS 收事件。"""

    def __init__(self, tmp_path, text, embedder=None):
        self.app = create_app(tmp_path, asr_factory=lambda s: MockASR([(900, text, False, 1000)], s),
                              embedder=embedder or HashEmbedder())
        self.app.state.audio_hub.vad_factory = lambda s: EnergyVad(s)
        self.client = TestClient(self.app)
        self.token = self.client.get('/api/status').json()['token']
        self.h = {'X-Local-Token': self.token}
        self.lib = self.client.post('/api/import', json={'name': 't', 'text': LIB}, headers=self.h).json()['id']
        for k, ans in APPROVED.items():
            self.client.post(f'/api/libraries/{self.lib}/entries/{k}/review',
                             json={'status': 'approved', 'answer': ans, 'confirmed': True}, headers=self.h)
        self.sid = self.client.post('/api/sessions', json={'mode': 'online', 'library': self.lib, 'consent': True},
                                    headers=self.h).json()['id']

    def play(self, until, on_event=None, accept=True):
        """喂 70 帧（前 50 帧有声），board WS 读到 until(event) 为真或读满 60 条为止。
        accept=True 时模拟用户按 Enter 采用 VAD 候选（上行 candidate_accept）。"""
        events = []
        with self.client.websocket_connect(f'/ws/board/{self.sid}?token={self.token}&since=0') as board, \
                self.client.websocket_connect(f'/ws/audio/{self.sid}?token={self.token}') as audio:
            for seq in range(70):
                audio.send_bytes(wire(seq, seq < 50))
            for _ in range(7):
                audio.receive_json()
            for _ in range(60):
                ev = board.receive_json()
                events.append(ev)
                if on_event:
                    on_event(board, ev)
                if accept and ev['type'] == 'prompt_candidate':
                    board.send_json({'type': 'candidate_accept', 'candidate_id': ev['payload']['candidate_id']})
                if until(ev):
                    break
        return events

    def stop(self):
        assert self.client.post(f'/api/sessions/{self.sid}/stop', json={}, headers=self.h).status_code == 200


def test_a_committed_to_prompt_primary_over_ws(tmp_path):
    run = Run(tmp_path, Q_TX)
    events = run.play(lambda e: e['type'] == 'prompt_primary')
    primary = events[-1]
    assert primary['payload']['level'] == 'L1' and primary['payload']['source_tag'] == '我的口径'
    assert primary['payload']['source_entry_id'] == 'H1-01'  # 记录库/导出用
    assert primary['payload']['outline']['conclusion'] == '先说结论：分布式事务常用两阶段提交和补偿方案。'
    seqs = [e['event_seq'] for e in events]
    assert seqs == sorted(set(seqs))
    assert (primary['turn_id'], primary['epoch']) == ('t1', 1)
    run.stop()
    sent = metric_rows(run.app.state.store, run.sid, 'prompt_sent')
    assert sent and sent[0][3]['level'] == 'L1' and sent[0][3]['hit_id'] == 'H1-01' and sent[0][3]['latency_ms'] >= 0


def test_b_out_of_library_only_status(tmp_path):
    run = Run(tmp_path, '今天上海的天气怎么样呢')
    events = run.play(lambda e: e['type'] == 'prompt_status')
    assert events[-1]['payload']['text'] == STATUS_TIE  # 库外知识题现在走 L3：先提示“暂无标准答案，正在补充”
    assert not [e for e in events if e['type'] == 'prompt_primary']
    run.stop()
    assert run.client.get(f'/api/sessions/{run.sid}/snapshot').json()['primary'] is None


def test_c_personal_experience_miss_status_only_no_network(tmp_path, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError('不得联网')

    import httpx
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *a, **k):
        # AF_UNIX 是本机 IPC（宿主注入的 broker.sock 等），不算联网，放行
        if self.family in (socket.AF_INET, socket.AF_INET6):
            calls.append((self.family, address))
            raise AssertionError('不得联网')
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket, 'create_connection', boom)
    monkeypatch.setattr(socket, 'getaddrinfo', boom)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', boom)
    run = Run(tmp_path, '你负责的项目里缓存怎么设计的')
    events = run.play(lambda e: e['type'] == 'prompt_status')
    assert events[-1]['payload']['text'] == STATUS_PERSONAL
    assert not [e for e in events if e['type'] in ('prompt_primary', 'prompt_supplement')]
    run.stop()
    assert calls == []


def test_f_display_ack_gives_push_latency(tmp_path):
    run = Run(tmp_path, Q_TX)

    def ack(board, ev):
        if ev['type'] == 'prompt_primary':
            board.send_json({'type': 'display_ack', 'prompt_id': ev['payload']['prompt_id'], 't_visible': 12345.5})
            time.sleep(0.3)  # 让服务端处理回执后再退出 TestClient

    run.play(lambda e: e['type'] == 'prompt_primary', on_event=ack)
    run.stop()
    store = run.app.state.store
    sent = next(r for r in metric_rows(store, run.sid, 'prompt_sent') if r[3]['kind'] == 'primary')
    acks = metric_rows(store, run.sid, 'display_ack')
    assert len(acks) == 1 and acks[0][3]['t_visible'] == 12345.5 and acks[0][3]['prompt_id'] == sent[3]['prompt_id']
    push_ms = (acks[0][2] - sent[3]['t_committed_ns']) / 1e6
    assert 0 <= push_ms < 5000  # commit -> 回执到达服务端的推送延迟（含 WS 往返）


# ---------- 答案环单元（FakePipe + 真 EventBus + 真检索）----------
class FakePipe:
    def __init__(self):
        self.turn_events, self.event_ns = [], []
        self.detector = SimpleNamespace(phase='question_committed', answerer_active=False)
        self.health = lambda: {'state': 'ok', 'reason': ''}


class Env:
    def __init__(self, tmp_path, search_fn=None):
        self.store = Store(tmp_path)
        self.lib, self.sid = make_session(self.store)
        self.bus, self.pipe = EventBus(), FakePipe()
        self.provider = IndexProvider(self.store, HashEmbedder())
        assert self.provider.prepare(self.lib) == 'ok'
        self.loop = AnswerLoop(self.sid, self.store, self.bus, self.provider, _StoreMetrics(self.store),
                               pipe=self.pipe, search_fn=search_fn)
        self.loop.attach()

    def feed(self, type_, text, ver=1, reason='r', **kw):
        self.pipe.turn_events.append(TurnEvent(type_, text, ver, 1000, reason, kind='question', completeness='high', **kw))
        self.pipe.event_ns.append(time.perf_counter_ns())
        self.bus.pull(self.sid, self.pipe)

    def events(self, *types):
        return [e for e in self.bus.replay(self.sid, 0) if not types or e['type'] in types]

    def primary(self):
        return self.bus.snapshot(self.sid)['primary']


@pytest.mark.asyncio
async def test_d_late_result_archived_not_pushed(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow(retr, text):
        if text == Q_TX:
            started.set()
            gate.wait(5)
        return retr.route(text)

    env = Env(tmp_path, search_fn=slow)
    env.feed('committed', Q_TX)
    while not started.is_set():
        await asyncio.sleep(0.01)
    env.feed('committed', Q_REDIS)  # 下一题 commit：旧题检索还没回来
    gate.set()
    await env.loop.drain()
    prim = env.events('prompt_primary')
    assert len(prim) == 1 and prim[0]['turn_id'] == 't2'
    assert prim[0]['payload']['outline']['conclusion'].startswith('先说结论：Redis')
    late = metric_rows(env.store, env.sid, 'late_archived')
    assert len(late) == 1 and late[0][0] == 't1' and late[0][3]['hit_id'] == 'H1-01' and late[0][3]['reason'] == 'stale_turn'


@pytest.mark.asyncio
async def test_e_rollback_refreshes_before_answering_and_supplements_after(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX)
    await env.loop.drain()
    assert env.primary()['outline']['conclusion'].startswith('先说结论：分布式事务')
    # 回答前：rolled_back -> 新文本重提交，主答案刷新并标已修正
    env.feed('rolled_back', Q_REDIS, ver=2, reason='semantic_change')
    env.feed('committed', Q_REDIS, ver=2)
    await env.loop.drain()
    snap = env.bus.snapshot(env.sid)
    assert snap['turn_id'] == 't1' and snap['epoch'] == 2
    assert snap['primary']['outline']['conclusion'].startswith('先说结论：Redis') and snap['primary']['corrected'] is True
    assert snap['question']['corrected'] is True
    # 回答开始后：text_updated 带来不同命中 -> 只补充，不替换主答案
    env.pipe.detector.phase = 'answering'
    env.bus.pull(env.sid, env.pipe)
    assert env.bus.snapshot(env.sid)['status'] == 'answering'
    env.feed('text_updated', Q_TX, ver=3, reason='catchup')
    await env.loop.drain()
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['outline']['conclusion'].startswith('先说结论：Redis')
    assert len(snap['supplements']) == 1 and '分布式事务' in snap['supplements'][0]['content']
    assert env.bus.stats(env.sid)['locked_dropped'] == 0  # 答案环自己就没有去替换


@pytest.mark.asyncio
async def test_e2_text_updated_before_answering_refreshes_or_dedups(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX)
    await env.loop.drain()
    n = len(env.events('prompt_primary'))
    env.feed('text_updated', Q_TX, ver=2, reason='catchup')  # 同结果：不推送
    await env.loop.drain()
    assert len(env.events('prompt_primary')) == n
    env.feed('text_updated', Q_REDIS, ver=3, reason='catchup')  # 不同结果、未回答：刷新并标已修正
    await env.loop.drain()
    prim = env.events('prompt_primary')
    assert len(prim) == n + 1 and prim[-1]['payload']['corrected'] is True
    assert env.primary()['outline']['conclusion'].startswith('先说结论：Redis')


@pytest.mark.asyncio
async def test_g_process_speech_not_searched_and_tie_is_not_standard_answer(tmp_path):
    calls = []

    def counting(retr, text):
        calls.append(text)
        return retr.route(text)

    env = Env(tmp_path, search_fn=counting)
    env.feed('committed', '我们开始吧')
    await env.loop.drain()
    assert calls == [] and not env.events('prompt_primary', 'prompt_status', 'candidate')
    env.feed('committed', '请介绍一下消息队列的方案')  # 甲/乙并列
    await env.loop.drain()
    assert not env.events('prompt_primary')
    cand = env.events('candidate')[-1]
    assert cand['payload']['label'] == '候选，未确认' and len(cand['payload']['hits']) == 2
    assert STATUS_TIE in [e['payload']['text'] for e in env.events('prompt_status')]  # 其后联网路径还会补一条状态


@pytest.mark.asyncio
async def test_unapproved_entry_never_retrieved(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', '如何做数据库分库分表')
    await env.loop.drain()
    assert not env.events('prompt_primary') and env.events('prompt_status')[0]['payload']['text'] == STATUS_TIE


@pytest.mark.asyncio
async def test_h_same_input_same_output(tmp_path):
    outs = []
    for name in ('a', 'b'):
        env = Env(tmp_path / name)
        for text in (Q_TX, '今天上海的天气怎么样呢', '请介绍一下消息队列的方案', '你能讲讲分布式事务有哪些区别吗'):
            env.feed('committed', text)
            await env.loop.drain()
        outs.append([(e['type'], e['turn_id'], e['epoch'], e['payload']) for e in env.events()])
    assert outs[0] == outs[1] and any(t == 'prompt_primary' for t, *_ in outs[0])


# ---------- 索引与状态 ----------
def test_status_and_health_when_index_down(tmp_path, monkeypatch):
    monkeypatch.setattr(IndexProvider, '_build', lambda self, lib: (_ for _ in ()).throw(RuntimeError('x')))
    run = Run(tmp_path, Q_TX)
    snap = run.client.get(f'/api/sessions/{run.sid}/snapshot').json()
    assert snap['session_status'] == 'active' and snap['health']['retrieval']['state'] == 'down'
    status = run.client.get('/api/status').json()
    assert status['retrieval'] == 'down' and str(tmp_path) not in json.dumps(status)


def test_degraded_to_fts_when_model_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(answer, '_real_embedder', [None])
    app = create_app(tmp_path, embedder=None)
    c = TestClient(app)
    h = {'X-Local-Token': c.get('/api/status').json()['token']}
    lib = c.post('/api/import', json={'name': 't', 'text': LIB}, headers=h).json()['id']
    for k, ans in APPROVED.items():
        c.post(f'/api/libraries/{lib}/entries/{k}/review', json={'status': 'approved', 'answer': ans, 'confirmed': True}, headers=h)
    sid = c.post('/api/sessions', json={'mode': 'online', 'library': lib, 'consent': True}, headers=h).json()['id']
    assert c.get(f'/api/sessions/{sid}/snapshot').json()['health']['retrieval']['state'] == 'degraded'
    assert c.get('/api/status').json()['retrieval'] == 'degraded'
    prov = app.state.index_provider
    run = lambda text: prov.executor.submit(prov(lib).run, text).result()  # noqa: E731  索引只能在其执行器线程里用
    assert run(Q_TX)['label'] == 'L1_TIE'  # FTS 单路最多出“候选”，永远不出标准答案
    assert run('今天上海的天气怎么样呢')['label'] == 'MISS'


# ---------- M05-5/6 回答阶段事件接线 ----------
def push(env, type_, text, ver=1, reason='r', **kw):
    env.pipe.turn_events.append(TurnEvent(type_, text, ver, 1000, reason, **kw))
    env.pipe.event_ns.append(time.perf_counter_ns())
    env.bus.pull(env.sid, env.pipe)


@pytest.mark.asyncio
async def test_closed_turn_late_result_only_archived(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow(retr, text):
        started.set()
        gate.wait(5)
        return retr.route(text)

    env = Env(tmp_path, search_fn=slow)
    env.feed('committed', Q_TX)
    while not started.is_set():
        await asyncio.sleep(0.01)
    push(env, 'answer_closed', Q_TX, reason='new_question')
    gate.set()
    await env.loop.drain()
    assert not env.events('prompt_primary', 'prompt_status', 'candidate')
    late = metric_rows(env.store, env.sid, 'late_archived')
    assert len(late) == 1 and late[0][0] == 't1' and late[0][3]['reason'] == 'turn_closed'
    assert env.bus.snapshot(env.sid)['status'] == 'closed'


@pytest.mark.asyncio
async def test_interrupted_switches_once_and_late_result_only_archived(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow(retr, text):
        if text == Q_TX:
            started.set()
            gate.wait(5)
        return retr.route(text)

    env = Env(tmp_path, search_fn=slow)
    env.feed('committed', Q_TX)
    while not started.is_set():
        await asyncio.sleep(0.01)
    push(env, 'interrupted', Q_TX, reason='explicit_switch')
    push(env, 'committed', '先停一下，换个问题，' + Q_REDIS, reason='explicit_switch', kind='question', completeness='high',
         sub_questions=(Q_REDIS,))
    gate.set()
    await env.loop.drain()
    switches = [e for e in env.events('interrupt') if e['payload']['kind'] == 'switch']
    assert len(switches) == 1 and switches[0]['turn_id'] == 't1'
    prim = env.events('prompt_primary')
    assert len(prim) == 1 and prim[0]['turn_id'] == 't2'  # 新题按 sub_questions[0] 检索，不带终止语
    assert prim[0]['payload']['outline']['conclusion'].startswith('先说结论：Redis')
    late = metric_rows(env.store, env.sid, 'late_archived')
    assert [(r[0], r[3]['reason']) for r in late] == [('t1', 'turn_closed')]


@pytest.mark.asyncio
async def test_multi_subquestion_primary_uses_first_and_board_gets_sub_list(tmp_path):
    env = Env(tmp_path)
    push(env, 'committed', f'{Q_TX}？{Q_REDIS}？', kind='question', completeness='high',
         sub_questions=(Q_TX + '？', Q_REDIS + '？'))
    await env.loop.drain()
    snap = env.bus.snapshot(env.sid)
    assert snap['primary']['outline']['conclusion'].startswith('先说结论：分布式事务')
    assert snap['question']['sub_questions'] == [Q_TX + '？', Q_REDIS + '？']


@pytest.mark.asyncio
async def test_bus_maps_answer_phase_events_to_board_events(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX)
    push(env, 'interrupt', '那缓存呢？', reason='marker', kind='followup')
    snap = env.bus.snapshot(env.sid)
    assert [e['payload']['kind'] for e in env.events('interrupt')] == ['followup']
    assert snap['supplements'][-1]['source_tag'] == '追问' and snap['supplements'][-1]['content'] == '那缓存呢？'
    push(env, 'interrupt', '我们公司主要做电商', reason='unclear', kind='unknown')
    assert env.events('interrupt')[-1]['payload']['kind'] == 'unknown' and len(env.bus.snapshot(env.sid)['supplements']) == 1
    push(env, 'queued', '下一个问题甲', sub_questions=('下一个问题甲',), queue_len=1)
    push(env, 'queued', '下一个问题乙', sub_questions=('下一个问题乙',), queue_len=2)
    assert env.bus.snapshot(env.sid)['queue'] == ['下一个问题甲', '下一个问题乙']
    push(env, 'answer_end_candidate', Q_TX, reason='answerer_silence>=T_answer_silence')
    assert env.bus.snapshot(env.sid)['status'] == 'answering'
    push(env, 'answer_closed', Q_TX, reason='queue_pop')
    assert env.bus.snapshot(env.sid)['status'] == 'closed'
    push(env, 'dequeued', '下一个问题甲', sub_questions=('下一个问题甲',), queue_len=1)
    push(env, 'committed', '下一个问题甲', reason='dequeued', kind='question', completeness='high')
    snap = env.bus.snapshot(env.sid)
    assert snap['queue'] == ['下一个问题乙'] and snap['epoch'] == 2 and snap['turn_id'] == 't2' and snap['status'] == 'question_committed'
    assert [e['payload']['kind'] for e in env.events('interrupt')] == ['followup', 'unknown']  # 已关闭的题再提交不发 switch
    await env.loop.drain()


def test_personal_question_partial_hit_still_shows_personal_note(tmp_path, monkeypatch):
    """个人经历题即使主库部分命中（L1p），也必须同时给出“请按真实经历回答”提示（L7 Q16）。"""
    q = '请讲讲你经历过的一次最大失败，从中你学到了什么？'
    lib = LIB + '\n**H1-06｜00:06｜举一个没那么成功的项目，你从中学到了什么、以后做了哪些调整？** 参考回答：当场举某付费充值包项目，具体复盘调整需本人补充。'
    monkeypatch.setattr(sys.modules[__name__], 'LIB', lib)
    monkeypatch.setitem(APPROVED, 'H1-06', '当场举某付费充值包项目，具体复盘调整需本人补充。')
    run = Run(tmp_path, q)
    events = run.play(lambda e: e['type'] == 'prompt_status', )
    run.stop()
    texts = [e['payload'].get('text') for e in events if e['type'] == 'prompt_status']
    assert STATUS_PERSONAL in texts, [(e['type'], e['payload'].get('text')) for e in events]



# ---------- P0-C：vad 提交只进候选区，确认后才升级主答案 ----------
@pytest.mark.asyncio
async def test_vad_commit_publishes_candidate_until_confirmed(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX, reason='vad_primary')
    await env.loop.drain()
    assert not env.events('prompt_primary')  # 未确认前不发布主答案
    cand = env.bus.snapshot(env.sid)['pending_candidate']
    pc = env.events('prompt_candidate')
    assert len(pc) == 1 and cand is not None
    assert pc[0]['payload']['candidate_id'] == cand['candidate_id']
    assert '可能未问完' in pc[0]['payload']['label']
    assert pc[0]['payload']['hits'][0]['entry_id'] == 'H1-01'
    assert pc[0]['payload']['level'] == 'L1'
    ev = env.bus.accept_candidate(env.sid, cand['candidate_id'])
    assert ev['type'] == 'prompt_primary' and env.primary()['outline']['conclusion'].startswith('先说结论：分布式事务')
    assert env.bus.snapshot(env.sid)['pending_candidate'] is None
    assert metric_rows(env.store, env.sid, 'candidate_confirmed')
    assert env.bus.accept_candidate(env.sid, cand['candidate_id']) is None  # 重复确认不再升级


@pytest.mark.asyncio
async def test_unconfirmed_candidate_dropped_on_turn_close(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX, reason='vad_primary')
    await env.loop.drain()
    cand = env.bus.snapshot(env.sid)['pending_candidate']
    env.feed('committed', Q_REDIS)  # 下一题 committed：上一轮候选清理
    await env.loop.drain()
    assert env.bus.snapshot(env.sid)['pending_candidate'] is None
    dropped = metric_rows(env.store, env.sid, 'candidate_dropped')
    assert dropped and dropped[0][3]['candidate_id'] == cand['candidate_id']
    assert [e['turn_id'] for e in env.events('prompt_primary')] == ['t2']  # 旧候选没有被升级成主答案


@pytest.mark.asyncio
async def test_flagged_entry_primary_is_red_flagged_and_not_my_wording(tmp_path):
    env = Env(tmp_path)
    lib = env.store.import_text('flag', '**H9-01｜00:01｜你能讲讲分布式事务的区别吗** 参考回答：先说结论：两阶段提交 [待核]')['id']
    env.store.review(lib, 'H9-01', '先说结论：两阶段提交 [待核]', 'approved', True)
    env.loop._library = lib
    env.provider.prepare(lib)
    env.feed('committed', Q_TX)
    await env.loop.drain()
    prim = env.primary()
    assert prim['flagged'] is True and prim['pending_marks'] == 1
    assert prim['source_tag'] == '含待核内容'


@pytest.mark.asyncio
async def test_no_flag_entry_gets_explicit_false_metrics(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX)
    await env.loop.drain()
    prim = env.primary()
    assert prim['flagged'] is False and prim['pending_marks'] == 0 and prim['source_tag'] == '我的口径'


# ---------- P1-A/B/C/D：补充去重与配额、重提交、早退分支 ----------
@pytest.mark.asyncio
async def test_supplement_dedup_across_epoch_and_turn_quota(tmp_path):
    env = Env(tmp_path)
    subs = (Q_TX, Q_REDIS)
    env.feed('committed', Q_TX, sub_questions=subs)
    await env.loop.drain()
    assert len(env.events('prompt_supplement')) == 1
    assert env.events('prompt_supplement')[0]['payload']['level'] == 'L1'  # 复盘可区分等级
    # 回滚后重提交（epoch 变、turn 不变）：同样内容不重复推
    env.feed('rolled_back', Q_TX, ver=2, reason='semantic_change')
    env.feed('committed', Q_TX, ver=2, sub_questions=subs)
    await env.loop.drain()
    assert len(env.events('prompt_supplement')) == 1
    # 配额：每题最多 max_supplements 条，超出记 turn_quota
    env.loop.max_supplements = 1
    env.feed('text_updated', Q_TX, ver=3, reason='catchup', sub_questions=(Q_TX, '请介绍一下消息队列的方案甲'))
    await env.loop.drain()
    assert len(env.events('prompt_supplement')) == 1
    assert any(r[3]['reason'] == 'turn_quota' for r in metric_rows(env.store, env.sid, 'late_archived'))


@pytest.mark.asyncio
async def test_withdrawn_clears_pending_recommit_so_next_commit_is_new_turn(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX)                 # t1
    env.feed('rolled_back', Q_TX, ver=2)        # 回滚后又撤回 -> 不该并入 t1
    env.feed('withdrawn', Q_TX, ver=3, reason='speech_resumed')
    env.feed('committed', Q_REDIS)
    await env.loop.drain()
    snap = env.bus.snapshot(env.sid)
    assert snap['turn_id'] == 't2'
    assert [e['turn_id'] for e in env.events('prompt_primary')] == ['t2']


@pytest.mark.asyncio
async def test_recommit_does_not_cancel_all_jobs(tmp_path):
    env = Env(tmp_path)
    calls = []
    orig = env.loop._cancel_jobs
    env.loop._cancel_jobs = lambda tid: calls.append(tid) or orig(tid)
    env.feed('committed', Q_TX)
    env.feed('rolled_back', Q_TX, ver=2)
    env.feed('committed', Q_TX, ver=2)  # 同题重提交：不 _cancel_jobs(None)
    await env.loop.drain()
    assert calls.count(None) == 1 and calls[-1] == 't1'
    assert not any(r[3]['reason'] == 'cancelled' for r in metric_rows(env.store, env.sid, 'late_archived'))


@pytest.mark.asyncio
async def test_same_hit_with_new_subquestion_still_supplements_and_launches(tmp_path):
    env = Env(tmp_path)
    launches = []
    env.loop._launch = lambda *a, **k: launches.append(a[:3])
    env.feed('committed', Q_TX, sub_questions=(Q_TX,))
    await env.loop.drain()
    n_primary = len(env.events('prompt_primary'))
    launches.clear()
    env.feed('text_updated', Q_TX, ver=2, reason='catchup',
             sub_questions=(Q_TX, '请介绍一下消息队列的方案甲'))
    await env.loop.drain()
    assert len(env.events('prompt_primary')) == n_primary  # 同命中不重复推主答案
    sups = env.events('prompt_supplement')
    assert sups and '消息队列' in sups[-1]['payload']['content']  # 新子问照常补提示
    assert launches  # 仍会走 _launch（联网/模型补充）


@pytest.mark.asyncio
async def test_model_query_redacted_before_outbound(tmp_path):
    from test_answer_routing import LLMT, llm_json

    from app.credentials import CredentialStore, MemoryBackend
    from app.realtime.answer import Job
    from app.realtime.answer_ext import Externals

    lt = LLMT([llm_json(conclusion='公共结论')])
    env = Env(tmp_path)
    env.loop.ext = Externals(
        outbound=lambda: dict(web_questions=False, llm_snippets=True),
        timeouts=lambda: dict(web_first_ms=1000, web_total_ms=4000),
        creds=CredentialStore([MemoryBackend({'DEEPSEEK_API_KEY': 'sk-' + 'x' * 20})]),
        llm_transport=lt.transport)
    env.loop._ok = lambda job: True
    job = Job(1, 'model', 't1', 1, TurnEvent('committed', 'q', 1, 0, 'r'), env.loop.ns(),
              dict(hit_id=None, label='L1p', index_version=''), asyncio.Event())
    client = env.loop._model_client()
    assert client is not None
    frags = [dict(entry_id='H1-01', text='Redis有RDB和AOF两种持久化方式。')]
    await env.loop._run_model(job, client, '我手机13800138000，Redis怎么持久化', frags, [], 'L1p',
                              dict(names=[], terms=[]))
    user = lt.calls[0]['messages'][1]['content']
    assert '13800138000' not in user and 'Redis' in user
    n = len(lt.calls)
    await env.loop._run_model(job, client, '甲项目的Redis怎么持久化', frags, [], 'L1p',
                              dict(names=[], terms=['甲项目']))
    assert len(lt.calls) == n  # 个人/私密问题被 blocked：不外发


def test_g_vad_candidate_over_ws_needs_accept_before_primary(tmp_path):
    run = Run(tmp_path, Q_TX)
    events = run.play(lambda e: e['type'] == 'prompt_candidate', accept=False)
    types_ = [e['type'] for e in events]
    assert types_[-1] == 'prompt_candidate' and 'prompt_primary' not in types_
    cand = events[-1]['payload']
    assert cand['candidate_id'].startswith('c') and '可能未问完' in cand['label']
    assert cand['hits'][0]['entry_id'] == 'H1-01'
    assert run.client.get(f'/api/sessions/{run.sid}/snapshot').json()['pending_candidate']['candidate_id'] \
        == cand['candidate_id']
    run.stop()


@pytest.mark.asyncio
async def test_vad_candidate_update_does_not_leak_primary_before_confirm(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', Q_TX, reason='vad_primary')
    await env.loop.drain()
    env.feed('text_updated', Q_TX, ver=2, reason='catchup')
    await env.loop.drain()
    assert not env.events('prompt_primary')  # 未确认前，后续文本修正也只更新候选
    assert len(env.events('prompt_candidate')) == 2
    cand = env.bus.snapshot(env.sid)['pending_candidate']
    assert cand and cand['text_version'] == 2


@pytest.mark.asyncio
async def test_primary_uses_first_hit_subquestion_when_first_clause_misses(tmp_path):
    env = Env(tmp_path)
    env.feed('committed', '今天上海的天气怎么样呢', sub_questions=('今天上海的天气怎么样呢', Q_TX))
    await env.loop.drain()
    prim = env.primary()
    assert prim and prim['level'] == 'L1'
    assert prim['outline']['conclusion'].startswith('先说结论：分布式事务')
    assert prim['source_entry_id'] == 'H1-01'
