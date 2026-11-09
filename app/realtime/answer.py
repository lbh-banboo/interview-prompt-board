"""S1b 提示闭环（规划 §4.2 步骤 4~7、M08、M09、M10、M11）：提问方 committed -> 三级路由 -> 经 EventBus 推送提示。

- 订阅 EventBus 的轮次事件（committed / text_updated / rolled_back / withdrawn），不读墙钟做逻辑；
  perf_counter_ns 只用于 prompt_sent 延迟埋点与联网耗时。
- 检索与路由在专用单线程执行器里跑（sqlite 连接不跨线程；也避免阻塞事件循环）。
- committed 的 sub_questions 逐子问路由：第一个命中 L1/L1p/L2 的子问对应主答案（首个子句未命中时不至于没有主答案），
  其余子问的命中走补充区；主答案本题内位置不变。联网（L3）与模型补充（M09）是后台任务，只产出补充 / 状态，不替换主答案。
- 防串题两道闸：结果回写前比对当前活动 (turn_id, epoch) 与本题最新任务序号，不一致只写 late_archived，不推送；
  发布层（EventBus.publish）还会再丢一次。联网 / 模型任务每次回写前同样比对，回答结束、改题、重提交即取消。
- 主答案本题内锁定：回答方已开始回答后，不同结果只作补充（prompt_supplement），不替换主答案。
- 只检索 status='approved' 的条目；个人经历 / 行为情景永不联网；模型生成的个人事实先校验后推送。
- 仅次优库 + 联网（secondary_only）：主库为空/未选时用空主库索引，知识/观点类走 L2 -> L3；个人/行为仍只给诚实文案。
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from dataclasses import dataclass, replace
from time import perf_counter_ns

import numpy as np

from ..domain.facts import ApprovedEntry
from ..domain.outline import outline as make_outline
from ..library.secondary import outline_text
from ..privacy.redact import redact
from ..retrieval.index import FastEmbedder, Index, trigrams
from ..retrieval.intent import BEHAVIOR, PERSONAL, PROCESS, classify_intent
from ..retrieval.router import PERSONAL_NOTE, TAG_PENDING, Router
from ..retrieval.search import (L1_TIE, MISS, Thresholds, classify_hit, load_thresholds, search,
                                with_thresholds)
from ..retrieval.tags import answer_tags
from ..search.client import MESSAGES as SEARCH_MESSAGES
from .answer_ext import Externals
from .answer_llm import make_client, model_blocks, warm_up
from .answer_secondary import build_secondary, role_tags
from .answer_web import build_vocab, web_items

STATUS_TIE = '暂无标准答案，正在补充'
STATUS_PERSONAL = PERSONAL_NOTE
STATUS_PARTIAL_NOTE = '部分命中，其余暂无标准答案'
STATUS_DOWN = '检索不可用，暂无标准答案'
DEGRADED_OVERLAP = 0.6  # FTS 单路：命中条目覆盖查询 trigram 的比例下限（未标定，保守，且只出“候选”）
HIT_LEVELS = ('L1', 'L1p', 'L2')
NO_TRANSPORT = {'search': '未配置搜索传输', 'llm': '未配置模型传输'}
log = logging.getLogger('app')

_real_embedder = []  # 进程内只加载一次真实模型；空=未尝试，[None]=加载失败


class _ZeroEmbedder:
    """FTS 单路降级用：零向量，search() 会跳过向量路。"""
    name = 'none'
    revision = 'fts-only'
    simulated = False
    dim = 8

    def embed(self, texts):
        return np.zeros((len(texts), self.dim), dtype=np.float32)


class Retriever:
    """一个库的已建索引（只含已审核条目）+ 三级路由器。route() / run() 必须在 IndexProvider.executor 线程里调用。

    降级（零向量）时主库只出“候选”：路由器用 FTS 单路适配器，命中伪装成 L1_TIE（证据里的 cos 为合成值，不可比较）。
    """

    def __init__(self, index, entries, thresholds, degraded, secondary=None, roles=()):
        self.index, self.entries, self.th, self.degraded = index, entries, thresholds, degraded
        self.index_version = index.index_version
        self.secondary, self.roles = secondary, tuple(roles)
        th = with_thresholds(thresholds, margin=max(thresholds.margin, 1e-6)) if degraded else thresholds
        self.router = Router(index, secondary, th, search_fn=self._fts_search if degraded else search)

    def bind(self, secondary, roles):
        """同一份主库索引，换成本场的次优库与岗位。"""
        return Retriever(self.index, self.entries, self.th, self.degraded, secondary, roles)

    def route(self, text):
        return self.router.route(text, self.roles)

    def card(self, sd):
        """子问命中的展示卡：dict(question, outline, text, tier)；条目缺失返回 None。"""
        if sd.level in ('L1', 'L1p'):
            e = self.entries.get(sd.entry_id)
            if e is None:
                return None
            o = make_outline(e['approved_answer'])
            return dict(question=e['question'], text=e['approved_answer'], tier='primary',
                        outline=dict(conclusion=o['conclusion'], points=o['points'], examples=o['example'], cautions=o['caution']))
        c = self.secondary.cards.get(sd.entry_id) if sd.level == 'L2' and self.secondary else None
        if c is None:
            return None
        return dict(question=c['question'], text=outline_text(c), tier='secondary',
                    outline=dict(conclusion=c['conclusion'], points=c['points'], examples=c['example'], cautions=c['caution']))

    def run(self, text):
        """单问检索的扁平结果（不经路由器）。"""
        res = search(self.index, text)
        if self.degraded:
            return self._run_fts_only(res, text)
        cls = classify_hit(res, self.th)
        by_cos = sorted(res.hits, key=lambda h: (-h.cos, h.entry_id))
        near = [h.entry_id for h in by_cos if h.cos >= self.th.tau_mid][:3]
        return dict(label=cls.label, hit_id=res.top1_entry, cos=round(res.cos_top1, 4),
                    candidates=near, index_version=res.index_version)

    @staticmethod
    def _fts_top(res, text):
        need = max(1, int(DEGRADED_OVERLAP * len(trigrams(text))))
        return next((h for h in res.hits if h.keyword_hit and h.kw_overlap >= need), None)

    def _run_fts_only(self, res, text):
        top = self._fts_top(res, text)
        if top is None:
            return dict(label=MISS, hit_id=None, cos=0.0, candidates=[], index_version=res.index_version)
        return dict(label=L1_TIE, hit_id=top.entry_id, cos=0.0, candidates=[top.entry_id], index_version=res.index_version)

    def _fts_search(self, index, text):
        res = search(index, text)
        top = self._fts_top(res, text)
        if top is None:
            return replace(res, hits=[], cos_top1=0.0, cos_top2=None, top1_entry=None)
        hit = replace(top, cos=self.th.tau_cos)
        return replace(res, hits=[hit], cos_top1=hit.cos, cos_top2=hit.cos, top1_entry=hit.entry_id)


class IndexProvider:
    """index_provider(library_id)：返回已预建的 Retriever 或 None。会前（POST /api/sessions 后）prepare 预建，
    缓存键 = 已审核条目内容 hash + 嵌入模型 revision + 阈值。state: none / ok / degraded / down。

    阈值优先取会话快照的 retrieval_thresholds（null 项回落 app/retrieval/thresholds.json 的现有默认）；
    threshold_source 标明本次构建用的是 'snapshot'（会话快照）还是 'default'（默认文件）。
    """

    def __init__(self, store, embedder=None):
        self.store = store
        self._embedder = embedder
        self.executor = ThreadPoolExecutor(1, thread_name_prefix='retrieval')
        self._cache = {}
        self._by_library = {}
        self._thresholds_provider = None
        self.threshold_source = 'default'
        self.state = 'none'

    def set_thresholds_provider(self, provider):
        """() -> 会话快照的 retrieval_thresholds 字典或 None。"""
        self._thresholds_provider = provider

    def __call__(self, library_id):
        return self._by_library.get(library_id)

    def prepare(self, library_id):
        """同步构建（在执行器线程里）；任何失败都只落到 state='down'，不抛出。"""
        try:
            self.state = self.executor.submit(self._build, library_id).result()
        except Exception:
            log.exception('检索索引构建失败')
            self.state = 'down'
        return self.state

    def empty(self):
        """主库为空 / 未选择时用的空索引；次优库由 AnswerLoop 绑上。须在执行器线程里调用。"""
        self._build(None)
        return self._by_library[None]

    def _pick_embedder(self):
        if self._embedder is not None:
            return self._embedder, False
        if not _real_embedder:
            try:
                _real_embedder.append(FastEmbedder())
            except Exception:
                log.exception('向量模型加载失败，降级为 FTS 单路')
                _real_embedder.append(None)
        if _real_embedder[0] is None:
            return _ZeroEmbedder(), True
        return _real_embedder[0], False

    def _session_thresholds(self, base):
        """快照阈值覆盖默认文件；全部为 null（未设置）时整份回落到默认文件。"""
        raw = None
        if self._thresholds_provider is not None:
            try:
                raw = self._thresholds_provider()
            except Exception:
                log.exception('读取会话检索阈值失败，使用默认文件')
        keys = ('tau_cos', 'tau_cos_strong', 'tau_mid', 'margin')
        if not isinstance(raw, dict) or all(raw.get(k) is None for k in keys):
            return base, 'default'
        vals = {k: raw[k] if raw.get(k) is not None else getattr(base, k) for k in keys}
        return Thresholds(vals['tau_cos'], vals['tau_cos_strong'], vals['tau_mid'], vals['margin'],
                          base.calibrated, 'snapshot'), 'snapshot'

    def _build(self, library_id):
        entries = {e['id']: e for e in self.store.entries(library_id) if e['status'] == 'approved'}
        embedder, degraded = self._pick_embedder()
        thresholds, source = self._session_thresholds(load_thresholds(embedder.revision))
        key = hashlib.sha256(json.dumps(
            [[e['id'], e['question'], e['approved_answer']] for e in entries.values()]
            + [embedder.revision, thresholds.tau_cos, thresholds.tau_cos_strong,
               thresholds.tau_mid, thresholds.margin, source], ensure_ascii=False).encode()).hexdigest()
        if key not in self._cache:
            index = Index(embedder)
            index.build([dict(id=e['id'], question=e['question'], variants=[]) for e in entries.values()])
            self._cache[key] = Retriever(index, entries, thresholds, degraded)
        self._by_library[library_id] = self._cache[key]
        self.threshold_source = source
        log.info('检索阈值来源：%s（tau_cos=%s tau_cos_strong=%s tau_mid=%s margin=%s）',
                 '会话快照' if source == 'snapshot' else '默认文件',
                 thresholds.tau_cos, thresholds.tau_cos_strong, thresholds.tau_mid, thresholds.margin)
        return 'degraded' if degraded else 'ok'


@dataclass(eq=False)
class Job:
    """一次联网 / 模型后台任务；no 是同题同类任务的序号，只有最新的可以回写。"""
    no: int
    kind: str
    turn_id: str
    epoch: int
    ev: object
    t_ns: int
    out: dict
    cancel: asyncio.Event
    archived: bool = False


class AnswerLoop:
    def __init__(self, session_id, store, bus, index_provider, metrics, pipe=None, search_fn=None, ns=perf_counter_ns,
                 ext=None, max_supplements=5):
        self.session_id, self.store, self.bus = session_id, store, bus
        self.provider, self.metrics, self.pipe = index_provider, metrics, pipe
        self.search_fn = search_fn  # (retriever, 子问文本) -> RouteDecision；测试注入慢路由用
        self.ns = ns
        self.ext = ext or Externals()
        self._token = 0  # 每个 committed/text_updated/rolled_back/withdrawn 递增；只有最新任务可以推送
        self._ended = set()  # answer_closed / interrupted 的 turn_id：其迟到结果只归档
        self._rolled = set()  # 发生过 rolled_back 的 turn_id：随后的 committed 是同题重提交
        self._pushed = {}  # turn_id -> {'hit_id', 'level'}，已推送的主答案
        self._last_note = {}  # (turn_id, epoch) -> 已推送的 status/candidate 键，避免同文重复
        self._sup_seen = {}  # turn_id -> 已推送补充的内容哈希（跨 epoch 去重）
        self.max_supplements = max_supplements  # 每题最多推送的补充条数，超出记 turn_quota
        self._jobs = {}  # job.no -> Job（运行中）
        self._latest = {}  # (turn_id, kind) -> 最新 job.no
        self._cand_t = {}  # (turn_id, epoch) -> 候选卡片发布的 t_ns（确认后补 primary 的 prompt_sent 延迟）
        self._launched = set()  # (turn_id, epoch, kind, 内容键)：同一批任务不重复启动
        self._job_no = 0
        self._bound = {}  # id(retr) -> (retr, 绑定了本场次优库的 Retriever)；只在执行器线程里读写
        self._warned = set()
        self._tasks = set()
        self._closed = False
        self._library = None
        self._roles = []
        self._secondary_only = False

    def attach(self):
        with self.store.connect() as db:
            row = db.execute('SELECT library_id FROM sessions WHERE id=?', (self.session_id,)).fetchone()
            try:
                raw = db.execute('SELECT roles_json, config_snapshot_json FROM sessions WHERE id=?', (self.session_id,)).fetchone()
            except sqlite3.OperationalError:  # 未做 ensure_sessions_columns 的旧库
                raw = None
        self._library = row['library_id'] if row else None
        self._roles = json.loads(raw[0]) if raw and raw[0] else []
        snap = json.loads(raw[1]) if raw and raw[1] else {}
        self._secondary_only = bool(snap.get('secondary_only'))  # 仅次优库 + 联网模式
        self.bus.set_listener(self.session_id, self.on_turn_event)
        if hasattr(self.bus, 'on_candidate'):
            self.bus.on_candidate = self._candidate_metric
        retr, ex = self.provider(self._library), getattr(self.provider, 'executor', None)
        if retr is not None and ex is not None and not self._secondary_only:
            ex.submit(self._bind, retr)  # 会话开始：预建次优索引
        self._warm_up()

    async def aclose(self):
        self._closed = True
        self.bus.set_listener(self.session_id, None)
        if getattr(self.bus, 'on_candidate', None) == self._candidate_metric:
            self.bus.on_candidate = None
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self):
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _track(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _bind(self, retr):
        got = self._bound.get(id(retr))
        if got is None or got[0] is not retr:
            sec = None
            if self.ext.secondary is not None and not retr.degraded:
                sec = build_secondary(self.ext.secondary, self._roles, retr.index.embedder)
            got = self._bound[id(retr)] = (retr, retr.bind(sec, role_tags(self._roles)))
        return got[1]

    def _warm_up(self):
        """会话开始预热模型的稳定前缀（只含岗位 + 提纲格式）；无 key / 开关关 / 无传输则不做。"""
        client, _ = make_client(self.ext)
        if client is None or not self.ext.warmup:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._track(warm_up(self.ext, self.session_id, client, '、'.join(self._roles)))

    # ---- 轮次事件入口（EventBus.pull 在事件循环线程里同步调用，这里只登记任务）----
    def on_turn_event(self, ev, turn_id, epoch, index):
        if self._closed:
            return
        self._token += 1
        if ev.type in ('answer_closed', 'interrupted'):
            self._ended.add(turn_id)
            self._cancel_jobs(turn_id)
            return
        if ev.type == 'rolled_back':
            self._rolled.add(turn_id)
            self._cancel_jobs(turn_id)
            return
        if ev.type not in ('committed', 'text_updated'):
            return
        recommit = ev.type == 'committed' and turn_id in self._rolled
        if ev.type == 'committed' and not recommit:
            self._cancel_jobs(None)  # 新题：此前的联网与模型任务全部作废；重提交只靠 _latest / epoch 取代
        ns = self.pipe.event_ns if self.pipe is not None and hasattr(self.pipe, 'event_ns') else []
        t_ns = ns[index] if index < len(ns) else self.ns()
        self._track(self._handle(ev, turn_id, epoch, self._token, t_ns, recommit))

    def _cancel_jobs(self, turn_id):
        for job in self._jobs.values():
            if turn_id is None or job.turn_id == turn_id:
                job.cancel.set()

    # ---- 检索与推送 ----
    async def _handle(self, ev, turn_id, epoch, token, t_ns, recommit):
        subs = [s for s in (getattr(ev, 'sub_questions', ()) or (ev.turn_text,)) if classify_intent(s).kind != PROCESS]
        if not subs:
            return  # 流程性发言：不检索、不提示
        retr = None if self._secondary_only else self.provider(self._library)
        if retr is None and self._secondary_only:
            empty = getattr(self.provider, 'empty', None)
            if empty is not None:  # 主库为空 / 不存在：空主库索引 + 次优库 + 联网
                try:
                    retr = await asyncio.get_running_loop().run_in_executor(
                        getattr(self.provider, 'executor', None), empty)
                except Exception:
                    log.exception('空主库索引构建失败')
        bound, decisions, note = None, [], None
        if retr is None:
            note = STATUS_DOWN
        else:
            route = self.search_fn or (lambda r, t: r.route(t))

            def work():
                b = self._bind(retr)
                return b, [route(b, s) for s in subs]
            try:
                bound, decisions = await asyncio.get_running_loop().run_in_executor(getattr(self.provider, 'executor', None), work)
            except Exception:
                log.exception('检索失败')
                note = STATUS_DOWN
                if self.ext.health:
                    self.ext.health().set_retrieval('down')
        units = [(si, sd) for si, d in enumerate(decisions) for sd in d.subs if not sd.skipped]
        out = self._summary(units, decisions, retr)
        snap = self.bus.snapshot(self.session_id)
        if turn_id in self._ended:
            return self._late(turn_id, out, ev, 'turn_closed', epoch)
        if (snap['turn_id'], snap['epoch']) != (turn_id, epoch):
            return self._late(turn_id, out, ev, 'stale_turn', epoch)
        if token != self._token:
            return self._late(turn_id, out, ev, 'superseded', epoch)
        self._deliver(units, note, out, ev, turn_id, epoch, t_ns, recommit, bound)

    @staticmethod
    def _summary(units, decisions, retr):
        # 首个子句未命中时，主答案取第一个真正命中的子问（与 _deliver 的选择一致）
        first = next((sd for _, sd in units if sd.level in HIT_LEVELS),
                     units[0][1] if units else None)
        hit = first and (first.entry_id or (first.candidates[0][0] if first.candidates else None))
        return dict(label=first.level if first else MISS, hit_id=hit or None,
                    index_version=decisions[0].index_version if decisions else (retr.index_version if retr else ''))

    def _answering(self):
        return self.bus.snapshot(self.session_id)['status'] == 'answering' or \
            (self.pipe is not None and self.pipe.detector.phase == 'answering')

    def _has_pending_candidate(self, turn_id, epoch):
        c = self.bus.snapshot(self.session_id).get('pending_candidate')
        return bool(c and c.get('turn_id') == turn_id and c.get('epoch') == epoch)

    def _tags(self, bound, sd, card):
        """主答案来源标签与待核标记（与审核台共用 app.retrieval.tags 的同一套口径）。"""
        payload = bound.entries.get(sd.entry_id) if sd.level in ('L1', 'L1p') else card
        return answer_tags(payload, sd.level)

    def _deliver(self, units, note, out, ev, turn_id, epoch, t_ns, recommit, bound):
        prev = self._pushed.get(turn_id)
        is_update = ev.type == 'text_updated'
        # 主答案取第一个真正命中的子问（首个子句未覆盖/未命中时不至于没有主答案）
        first = next((sd for _, sd in units if sd.level in HIT_LEVELS), units[0][1] if units else None)
        card = bound.card(first) if first is not None and first.level in HIT_LEVELS else None
        if card is not None:
            hit_id = first.entry_id
            tag, flagged, marks = self._tags(bound, first, card)
            hits = [dict(entry_id=first.entry_id, question=card['question'], level=first.level,
                         conclusion=card['outline']['conclusion'])]
            if (ev.type == 'committed' and ev.reason.startswith('vad_primary')) \
                    or (is_update and self._has_pending_candidate(turn_id, epoch)):
                # vad 提交可能还没问完：先给候选卡片，用户确认后才升级为主答案
                ok = self._sent('candidate_prompt', self.bus.publish_prompt_candidate(
                    self.session_id, turn_id, epoch, card['outline'], first.level, tag, hits, ev.turn_text,
                    flagged=flagged, pending_marks=marks, text_version=ev.text_version,
                    source_entry_id=first.entry_id),
                    turn_id, out, ev, t_ns, first.level)
                if ok:
                    self._cand_t[(turn_id, epoch)] = t_ns
                return
            same = is_update and prev and prev['hit_id'] == hit_id
            if not same:  # 同命中的文本修正不重复推主答案，但下面仍要处理新子问 / 状态 / 联网
                if prev and self._answering():  # 主答案锁定：只作补充
                    self._sent('supplement', self.bus.publish_supplement(
                        self.session_id, turn_id, epoch, first.tag + '·文本修正后命中',
                        f"{card['question']}：{card['outline']['conclusion']}", level=first.level),
                        turn_id, out, ev, t_ns, first.level)
                else:
                    corrected = recommit or (is_update and prev is not None)
                    if self._sent('primary', self.bus.publish_prompt_primary(
                            self.session_id, turn_id, epoch, card['outline'], first.level, tag, corrected=corrected,
                            flagged=flagged, pending_marks=marks, source_entry_id=first.entry_id),
                            turn_id, out, ev, t_ns, first.level):
                        self._pushed[turn_id] = dict(hit_id=hit_id, level=first.level)
        elif is_update and prev:
            pass  # 补全文本后检索变差：不降级已推送的主答案
        elif first is not None and first.level == 'L1c':
            ids = [i for i, _ in first.candidates if i in bound.entries]
            hits = [dict(title=bound.entries[i]['question'], entry_id=i) for i in ids]
            self._note(('candidate', tuple(ids)), turn_id, epoch, lambda: self._sent(
                'candidate', self.bus.publish_candidate(self.session_id, turn_id, epoch, hits, ev.turn_text, ev.text_version),
                turn_id, out, ev, t_ns, None))
        for si, sd in units[1:]:
            if sd is first:
                continue  # 已被选为主答案的子问不再重复进补充区
            self._extra(si, sd, bound, turn_id, epoch, out, ev, t_ns)
        text = note or self._status_text(units)
        if text:
            self._note(('status', text), turn_id, epoch, lambda: self._sent(
                'status', self.bus.publish_prompt_status(self.session_id, turn_id, epoch, text), turn_id, out, ev, t_ns, None))
        if bound is not None:
            self._launch(units, bound, turn_id, epoch, out, ev, t_ns)

    def _extra(self, si, sd, bound, turn_id, epoch, out, ev, t_ns):
        """非第一子问的命中：以补充追加，不动主答案。按内容哈希跨 epoch 去重，每题最多 max_supplements 条。"""
        if sd.level == 'L1c':
            ids = [i for i, _ in sd.candidates if i in bound.entries]
            content = '、'.join(bound.entries[i]['question'] for i in ids)
        else:
            card = bound.card(sd) if sd.level in HIT_LEVELS else None
            if card is None:
                return
            content = f"{card['question']}：{card['outline']['conclusion']}"
        if not content:
            return
        seen = self._sup_seen.setdefault(turn_id, set())
        digest = hashlib.sha256(content.encode()).hexdigest()
        if digest in seen:
            return
        if len(seen) >= self.max_supplements:
            self._late(turn_id, out, ev, 'turn_quota', epoch)
            return
        seen.add(digest)
        self._sent('supplement', self.bus.publish_supplement(self.session_id, turn_id, epoch, sd.tag, content,
                                                             level=sd.level),
                   turn_id, out, ev, t_ns, sd.level)

    @staticmethod
    def _status_text(units):
        if any(sd.personal_note for _, sd in units):
            return PERSONAL_NOTE
        pending = [sd for _, sd in units if not sd.covered]
        if not pending:
            return None
        if len(pending) < len(units) or any(sd.level == 'L1p' for sd in pending):
            return TAG_PENDING + '：' + '；'.join(sd.clause.text for sd in pending)
        return STATUS_TIE

    def _note(self, key, turn_id, epoch, send):
        if self._last_note.get((turn_id, epoch, key[0])) != key:
            self._last_note[(turn_id, epoch, key[0])] = key
            send()

    def _sent(self, kind, published, turn_id, out, ev, t_ns, level):
        if published is None:
            self._late(turn_id, out, ev, 'publish_dropped')
            return False
        self._mark(turn_id, 'prompt_sent', dict(
            kind=kind, level=level, hit_id=out['hit_id'], latency_ms=round((self.ns() - t_ns) / 1e6, 3),
            t_committed_ns=t_ns, prompt_id=published['payload'].get('prompt_id'), text_version=ev.text_version,
            epoch=published['epoch'], event_seq=published['event_seq'], index_version=out['index_version']))
        return True

    def _late(self, turn_id, out, ev, reason, epoch=0):
        self._mark(turn_id, 'late_archived', dict(reason=reason, hit_id=out['hit_id'], label=out['label'],
                                                  text_version=ev.text_version, epoch=epoch))

    def _mark(self, turn_id, name, value):
        self.metrics.mark(self.session_id, turn_id, name, self.ns(), json.dumps(value, ensure_ascii=False))

    def _candidate_metric(self, session, action, cand):
        """EventBus 的候选卡片埋点：candidate_confirmed / candidate_dropped；确认时补一条 primary 的 prompt_sent。"""
        if session != self.session_id or action not in ('confirmed', 'dropped'):
            return
        turn_id, epoch = cand.get('turn_id', ''), cand.get('epoch')
        hit_id = (cand.get('hits') or [{}])[0].get('entry_id')
        self._mark(turn_id, 'candidate_' + action,
                   dict(candidate_id=cand.get('candidate_id'), level=cand.get('level'), hit_id=hit_id, epoch=epoch,
                        reason=cand.get('reason'), text_version=cand.get('text_version')))
        t_ns = self._cand_t.pop((turn_id, epoch), None)
        if action == 'confirmed' and t_ns is not None:
            self._mark(turn_id, 'prompt_sent', dict(
                kind='primary', level=cand.get('level'), hit_id=hit_id, latency_ms=round((self.ns() - t_ns) / 1e6, 3),
                t_committed_ns=t_ns, prompt_id=cand.get('prompt_id'), text_version=cand.get('text_version'),
                epoch=epoch))

    # ---- 联网（L3）与模型补充（M09）后台任务 ----
    def _launch(self, units, bound, turn_id, epoch, out, ev, t_ns):
        # 个人经历 / 行为情景永不联网：路由器已保证，这里按意图再过滤一次
        queries = [sd.clause.text for _, sd in units if sd.need_web and sd.intent.kind not in (PERSONAL, BEHAVIOR)]
        if queries:
            self._start(turn_id, epoch, 'web', tuple(queries), ev, t_ns, out, lambda job: self._web_job(job, queries, bound))
        targets = [t for _, sd in units if (t := self._model_target(sd, bound)) is not None]
        if targets:
            self._start(turn_id, epoch, 'model', tuple(t[0] for t in targets), ev, t_ns, out,
                        lambda job: self._model_job(job, targets, bound))

    @staticmethod
    def _model_target(sd, bound):
        """L1p / L2 且有检索片段：(子问, 片段, 校验用条目)。L1 主库命中不调用模型。"""
        card = bound.card(sd) if sd.level in ('L1p', 'L2') else None
        if card is None:
            return None
        return (sd.clause.text, [dict(entry_id=sd.entry_id, text=card['text'])],
                [ApprovedEntry(sd.entry_id, card['text'], tier=card['tier'])], sd.level)

    def _start(self, turn_id, epoch, kind, key, ev, t_ns, out, make):
        if (turn_id, epoch, kind, key) in self._launched:
            return
        self._launched.add((turn_id, epoch, kind, key))
        for old in self._jobs.values():
            if old.turn_id == turn_id and old.kind == kind:
                old.cancel.set()  # 同题同类的新任务取代旧任务
        self._job_no += 1
        job = Job(self._job_no, kind, turn_id, epoch, ev, t_ns, out, asyncio.Event())
        self._jobs[job.no] = job
        self._latest[(turn_id, kind)] = job.no

        async def run():
            try:
                await make(job)
            finally:
                self._jobs.pop(job.no, None)
        self._track(run())

    def _ok(self, job):
        """回写前的闸：回答结束 / 改题 / 被取代 / 已取消即只归档，且不再刷新屏幕。"""
        snap = self.bus.snapshot(self.session_id)
        reason = ('turn_closed' if job.turn_id in self._ended
                  else 'stale_turn' if (snap['turn_id'], snap['epoch']) != (job.turn_id, job.epoch)
                  else 'superseded' if self._latest.get((job.turn_id, job.kind)) != job.no
                  else 'cancelled' if job.cancel.is_set() else None)
        if reason is None:
            return True
        job.cancel.set()
        if not job.archived:
            job.archived = True
            self._mark(job.turn_id, 'late_archived', dict(reason=reason, hit_id=job.out['hit_id'], label=job.kind,
                                                          text_version=job.ev.text_version, epoch=job.epoch))
        return False

    def _push_supplement(self, job, source_tag, content, level, url=None, elapsed_ms=None):
        published = self.bus.publish_supplement(self.session_id, job.turn_id, job.epoch, source_tag, content,
                                                url=url, elapsed_ms=elapsed_ms, level=level)
        self._sent('supplement', published, job.turn_id, job.out, job.ev, job.t_ns, level)

    def _push_status(self, job, text):
        self._note(('status', text), job.turn_id, job.epoch, lambda: self._sent(
            'status', self.bus.publish_prompt_status(self.session_id, job.turn_id, job.epoch, text),
            job.turn_id, job.out, job.ev, job.t_ns, None))

    def _vocab(self, bound):
        names = []
        try:
            with self.store.connect() as db:
                row = db.execute('SELECT p.alias FROM sessions s JOIN profiles p ON p.id=s.profile_id WHERE s.id=?',
                                 (self.session_id,)).fetchone()
            names = [row[0]] if row and row[0] else []
        except sqlite3.OperationalError:  # 没有 profiles 表 / profile_id 列：取不到就为空
            pass
        return build_vocab(bound.entries, names)

    async def _web_job(self, job, queries, bound):
        vocab, clock = self._vocab(bound), lambda: self.ns() / 1e9
        for q in queries:
            if not self._ok(job):
                return
            points = []
            async with aclosing(web_items(self.ext, self.session_id, q, vocab, job.cancel, clock)) as items:
                async for s in items:
                    if not self._ok(job):
                        return
                    if s.kind == 'point':
                        points.append(s)
                        self._push_supplement(job, s.source_tag, f'{s.title}：{s.snippet}' if s.title else s.snippet,
                                              'L3', url=s.url, elapsed_ms=s.elapsed_ms)
                        self.ext.report('search', 'ok')
                    else:
                        self._push_status(job, s.text)
                        self._report_search(s)
            if not self._ok(job) or (points and not await self._model_for_web(job, q, points, vocab)):
                return

    def _report_search(self, s):
        if s.kind == 'no_result':
            self.ext.report('search', 'ok')
        elif s.kind == 'unavailable':
            self.ext.report('search', 'degraded', NO_TRANSPORT['search'] if s.code == 'NO_TRANSPORT'
                            else SEARCH_MESSAGES.get(s.code, s.text))
        elif s.kind == 'timeout':
            self.ext.report('search', 'degraded', s.text)

    async def _model_for_web(self, job, question, points, vocab=None):
        """L3 的外部片段也只在开关 / key / 传输齐备时交给模型整理；返回 False 表示任务已被闸住。"""
        client = self._model_client()
        if client is None:
            return True
        frags = [dict(entry_id=f'web{i}', text=p.snippet) for i, p in enumerate(points, 1)]
        return await self._run_model(job, client, question, frags, [], 'L3', vocab)

    def _model_client(self):
        client, why = make_client(self.ext)
        if why == 'no_transport' and 'llm' not in self._warned:
            self._warned.add('llm')
            self.ext.report('llm', 'degraded', NO_TRANSPORT['llm'])
        return client

    async def _model_job(self, job, targets, bound=None):
        client = self._model_client()
        if client is None:
            return
        vocab = self._vocab(bound) if bound is not None else None
        for question, frags, entries, level in targets:
            if not await self._run_model(job, client, question, frags, entries, level, vocab):
                return

    async def _run_model(self, job, client, question, frags, entries, level, vocab=None):
        if vocab is not None:  # 外发前同一套脱敏；个人/私密问题不外发
            r = redact(question, vocab)
            if r.blocked:
                return True
            question = r.text
        async with aclosing(model_blocks(self.ext, self.session_id, client, question, frags, entries, '、'.join(self._roles))) as blocks:
            async for b in blocks:
                if not self._ok(job):
                    return False
                if isinstance(b, str):
                    self._push_status(job, b)
                    return True
                self._push_supplement(job, b.source_tag, b.text, level)
        return self._ok(job)
