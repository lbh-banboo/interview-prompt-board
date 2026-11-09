"""board 事件总线（M11 / 规划 §4.3、§7.2）。

- 按 session 发布；event_seq 会话内单调；保留最近 keep 条供重放。
- epoch 在新题 committed 时递增；turn_id 同步换新（rolled_back 之后的重新提交沿用 turn_id，只递增 epoch）。
- prompt_* 事件必须带与当前活动轮次一致的 (turn_id, epoch)，且本题未结束，否则发布层直接丢弃（不推送屏幕）；
  主答案本题内只接受一次（锁定）。前端 reducer 对同一规则再校验一次（双重保护）。
- 总线同时维护一份服务端状态，供 GET /api/sessions/{id}/snapshot 刷新恢复。
- vad 模式的提交先发 prompt_candidate（候选卡片，payload 含 candidate_id / hits / outline），不进主答案；
  前端上行 {type:'candidate_accept', candidate_id} 由 board_ws 转给 accept_candidate，确认后才发 prompt_primary；
  未确认的候选在新题提交 / 本轮关闭时清理，并经 on_candidate 钩子埋点 candidate_dropped。
- SessionPipeline 没有事件回调，pull() 按游标读取其 turn_events（只读），由 board_ws 周期调用。
"""
import asyncio
import threading
import time
from collections import deque

PROMPT_TYPES = ('prompt_primary', 'prompt_status', 'prompt_supplement', 'prompt_candidate')
SOURCE_LOST_S = 3.0  # 音源超过这么久没有帧即 lost
CLOSED = ('closed', 'ended')


class _Session:
    def __init__(self, keep):
        self.event_seq = 0
        self.epoch = 0
        self.turn_no = 0
        self.turn_id = ''
        self.status = 'idle'
        self.waiting_continue = False
        self.answerer_active = False
        self.question = None
        self.candidate = None
        self.primary = None
        self.prompt_status = ''
        self.prompt_status_id = ''
        self.supplements = []
        self.queue = []
        self.health = {}
        self.sources = {}
        self.history = deque(maxlen=keep)
        self.subs = []  # (loop, queue)
        self.prompts = {}  # prompt_id -> (turn_id, epoch)
        self.pending_candidate = None  # vad 提交的候选卡片：{candidate_id, turn_id, epoch, ...}，确认后升级为 prompt_primary
        self.cursor = 0
        self.answerer_cursor = 0
        self.pipe_id = None
        self.question_source = 'meeting'  # 提问方逐字稿来源（单麦 / 线下为实际来源）
        self.pending_recommit = False
        self.recommitted = False
        self.last_health = None
        self.dropped = 0
        self.locked_dropped = 0
        self.switched = False  # 刚发过 interrupted -> interrupt(switch)，随后 committed 不再重复发
        self.listener = None  # (turn_event, turn_id, epoch, index) -> None；供 AnswerLoop 订阅


class Activity:
    """音频帧活动记录：会话级最近帧时刻（无帧超时）与音源级最近帧时刻（音源灯）。时钟可注入。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = {}
        self.sources = {}

    def touch(self, session, source=None):
        now = self.clock()
        self.last[session] = now
        if source:
            self.sources.setdefault(session, {})[source] = now

    def last_activity(self, session):
        """首次被问到时以当前时刻起算（服务重启后给满一个超时窗口）。"""
        return self.last.setdefault(session, self.clock())

    def idle_s(self, session):
        return self.clock() - self.last_activity(session)


class EventBus:
    def __init__(self, keep=200, clock=time.time):
        self.keep = keep
        self.clock = clock
        self._s = {}
        self._lock = threading.RLock()
        self.pipe_health = True  # False：pull() 不再按 pipeline.health() 发 health（由 CapabilityBoard 统一发布）
        self.on_pull = None  # (session, pipe|None) -> pull()/end() 之后的钩子（记录落库）
        self.on_late = None  # (session, type, payload, turn_id, epoch) -> 发布层丢弃 prompt_* 时的归档钩子
        self.on_candidate = None  # (session, 'confirmed'|'dropped', candidate) -> 埋点钩子

    def _get(self, session):
        s = self._s.get(session)
        if s is None:
            s = self._s[session] = _Session(self.keep)
        return s

    # ---- 订阅 / 重放 ----
    def subscribe(self, session):
        q = asyncio.Queue()
        with self._lock:
            self._get(session).subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, session, q):
        with self._lock:
            s = self._get(session)
            s.subs = [x for x in s.subs if x[1] is not q]

    def replay(self, session, since):
        with self._lock:
            return [e for e in self._get(session).history if e['event_seq'] > since]

    # ---- 发布 ----
    def publish(self, session, type_, payload=None, turn_id=None, epoch=None, text_version=0):
        """返回事件；被发布层丢弃时返回 None。省略 turn_id/epoch 即取当前活动轮次。"""
        with self._lock:
            s = self._get(session)
            explicit = turn_id is not None or epoch is not None
            if explicit and (turn_id != s.turn_id or epoch != s.epoch):
                s.dropped += 1
                self._late(session, type_, payload, turn_id, epoch)
                return None
            if type_ in PROMPT_TYPES:
                if s.status in CLOSED or not s.turn_id:
                    s.dropped += 1
                    self._late(session, type_, payload, s.turn_id, s.epoch)
                    return None
                if type_ == 'prompt_primary' and s.primary is not None \
                        and not (payload and payload.get('corrected') and s.status != 'answering'):
                    s.locked_dropped += 1  # 主答案本题内锁定；仅“回答前的修正刷新”可替换，回答开始后一律不可
                    self._late(session, type_, payload, s.turn_id, s.epoch)
                    return None
            return self._emit(s, session, type_, payload or {}, s.turn_id, s.epoch, text_version)

    def _late(self, session, type_, payload, turn_id, epoch):
        if self.on_late is not None and type_ in ('prompt_primary', 'prompt_supplement'):
            self.on_late(session, type_, payload or {}, turn_id or '', epoch or 0)

    def _emit(self, s, session, type_, payload, turn_id, epoch, text_version):
        s.event_seq += 1
        if type_ in PROMPT_TYPES:  # 各类提示都可 ACK：prompt_id 由发布层分配，前端按渲染结果回报
            payload = dict(payload, prompt_id=f'p{s.event_seq}')
            s.prompts[payload['prompt_id']] = (turn_id, epoch)
        if type_ == 'prompt_candidate':
            payload = dict(payload, candidate_id=f'c{s.event_seq}')
        ev = dict(type=type_, session=session, turn_id=turn_id, epoch=epoch, event_seq=s.event_seq,
                  text_version=text_version, t_server=self.clock(), payload=payload)
        self._apply(s, session, ev)
        s.history.append(ev)
        for loop, q in list(s.subs):
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:  # 订阅方事件循环已关闭
                s.subs = [x for x in s.subs if x[1] is not q]
        return ev

    def _apply(self, s, session, ev):
        t, p = ev['type'], ev['payload']
        if t == 'turn_state':
            if p['state'] in ('question_committed', 'closed', 'ended'):
                self._drop_candidate(s, session, 'turn_closed')  # 未确认的候选随本轮关闭而清理
            if p['state'] == 'question_committed':
                s.primary, s.supplements, s.candidate, s.prompt_status = None, [], None, ''
                s.prompt_status_id = ''
            s.status = p['state']
            s.waiting_continue = bool(p.get('waiting_continue'))
            s.answerer_active = bool(p.get('answerer_active'))
        elif t == 'transcript' and p.get('role') == 'questioner' and p.get('is_final'):
            s.question = dict(text=p['text'], text_version=ev['text_version'], kind=p.get('kind', ''),
                              corrected=s.recommitted, sub_questions=list(p.get('sub_questions', [])),
                              source=p.get('source', s.question_source))
        elif t == 'candidate':
            s.candidate = None if p.get('withdrawn') else p
        elif t == 'prompt_candidate':
            s.pending_candidate = dict(p, turn_id=ev['turn_id'], epoch=ev['epoch'],
                                       text_version=ev['text_version'])
        elif t == 'prompt_primary':
            self._drop_candidate(s, session, 'accepted')
            s.primary = p
        elif t == 'prompt_status':
            s.prompt_status = p.get('text', '')
            s.prompt_status_id = p.get('prompt_id', '')
        elif t == 'prompt_supplement':
            s.supplements.append(p)
        elif t == 'queue_update':
            s.queue = list(p.get('items', []))
        elif t == 'health':
            s.health.update(p)
        elif t == 'source_state':
            s.sources[p['source']] = p

    def _drop_candidate(self, s, session, reason):
        cand = s.pending_candidate
        if cand is None:
            return
        s.pending_candidate = None
        if self.on_candidate is not None:
            self.on_candidate(session, 'dropped', dict(cand, reason=reason))

    # ---- 业务发布封装 ----
    def publish_prompt_primary(self, session, turn_id, epoch, outline, level, source_tag, corrected=False,
                               flagged=False, pending_marks=0, source_entry_id=None):
        payload = dict(level=level, outline=outline, source_tag=source_tag, source_entry_id=source_entry_id,
                       flagged=bool(flagged), pending_marks=int(pending_marks))
        if corrected:
            payload['corrected'] = True
        return self.publish(session, 'prompt_primary', payload, turn_id=turn_id, epoch=epoch)

    def publish_prompt_candidate(self, session, turn_id, epoch, outline, level, source_tag, hits, text,
                                 flagged=False, pending_marks=0, text_version=0, source_entry_id=None):
        """vad 提交的候选卡片：可能未问完，等用户确认（candidate_accept）后才升级为 prompt_primary。"""
        payload = dict(level=level, outline=outline, source_tag=source_tag, hits=list(hits), text=text,
                       flagged=bool(flagged), pending_marks=int(pending_marks), source_entry_id=source_entry_id,
                       label='可能未问完，按 Enter 采用', tentative=True)
        return self.publish(session, 'prompt_candidate', payload, turn_id=turn_id, epoch=epoch,
                            text_version=text_version)

    def accept_candidate(self, session, candidate_id):
        """前端上行 candidate_accept：把当前待确认候选升级为主答案。返回发布的事件或 None（已过期 / 不匹配）。"""
        with self._lock:
            s = self._get(session)
            cand = s.pending_candidate
            if cand is None or not candidate_id or cand.get('candidate_id') != candidate_id:
                return None
            if s.status in CLOSED or (cand['turn_id'], cand['epoch']) != (s.turn_id, s.epoch):
                self._drop_candidate(s, session, 'stale')
                return None
            payload = dict(level=cand.get('level', ''), outline=cand.get('outline'), source_tag=cand.get('source_tag', ''),
                           flagged=bool(cand.get('flagged')), pending_marks=int(cand.get('pending_marks', 0)),
                           source_entry_id=cand.get('source_entry_id'), corrected=False, confirmed=True)
            s.pending_candidate = None
            ev = self._emit(s, session, 'prompt_primary', payload, s.turn_id, s.epoch,
                            cand.get('text_version', 0))
            if self.on_candidate is not None:
                self.on_candidate(session, 'confirmed', dict(cand, prompt_id=ev['payload'].get('prompt_id'),
                                                             event_seq=ev['event_seq']))
            return ev

    def publish_candidate(self, session, turn_id, epoch, hits, text, text_version):
        return self.publish(session, 'candidate', dict(hits=list(hits), text=text, text_version=text_version,
                                                       tentative=True, label='候选，未确认'),
                            turn_id=turn_id, epoch=epoch, text_version=text_version)

    def set_listener(self, session, fn):
        with self._lock:
            self._get(session).listener = fn

    def publish_prompt_status(self, session, turn_id, epoch, text):
        return self.publish(session, 'prompt_status', dict(text=text), turn_id=turn_id, epoch=epoch)

    def publish_supplement(self, session, turn_id, epoch, source_tag, content, url=None, elapsed_ms=None, level=''):
        return self.publish(session, 'prompt_supplement',
                            dict(source_tag=source_tag, content=content, url=url, elapsed_ms=elapsed_ms,
                                 level=level),
                            turn_id=turn_id, epoch=epoch)

    def publish_queue(self, session, items):
        return self.publish(session, 'queue_update', dict(items=list(items)))

    def publish_interrupt(self, session, kind):
        return self.publish(session, 'interrupt', dict(kind=kind))

    def close_turn(self, session, reason='answer_complete'):
        with self._lock:
            s = self._get(session)
            if s.turn_id and s.status not in CLOSED:
                return self._emit(s, session, 'turn_state', dict(state='closed', reason=reason, waiting_continue=False,
                                                                 answerer_active=False), s.turn_id, s.epoch, 0)

    def end(self, session):
        with self._lock:
            s = self._get(session)
            ev = None
            if s.status != 'ended':
                ev = self._emit(s, session, 'turn_state', dict(state='ended', reason='session_stopped',
                                                               waiting_continue=False, answerer_active=False),
                                s.turn_id, s.epoch, 0)
        if self.on_pull is not None:
            self.on_pull(session, None)
        return ev

    # ---- 快照 / 回执 ----
    def snapshot(self, session):
        with self._lock:
            s = self._get(session)
            return dict(session_id=session, turn_id=s.turn_id, epoch=s.epoch, event_seq=s.event_seq,
                        status=s.status, waiting_continue=s.waiting_continue, answerer_active=s.answerer_active,
                        question=s.question, candidate=s.candidate, primary=s.primary,
                        pending_candidate=s.pending_candidate,
                        prompt_status=s.prompt_status, prompt_status_id=s.prompt_status_id,
                        supplements=list(s.supplements), queue=list(s.queue),
                        health=dict(s.health), sources=dict(s.sources))

    def prompt_turn(self, session, prompt_id):
        with self._lock:
            return self._get(session).prompts.get(prompt_id)

    def stats(self, session):
        s = self._get(session)
        return dict(dropped=s.dropped, locked_dropped=s.locked_dropped)

    # ---- 从 SessionPipeline 拉取 ----
    def pull(self, session, pipe, activity=None):
        """把 pipeline.turn_events 的新增部分、阶段 / 健康 / 音源变化转成 board 事件。只读 pipeline。"""
        with self._lock:
            s = self._get(session)
            if pipe is not None:
                if s.pipe_id != id(pipe):
                    s.pipe_id, s.cursor, s.answerer_cursor = id(pipe), 0, 0
                s.question_source = getattr(pipe, 'question_source', 'meeting')
                events = pipe.turn_events
                while s.cursor < len(events):
                    self._turn_event(s, session, events[s.cursor])
                    if s.listener is not None:
                        s.listener(events[s.cursor], s.turn_id, s.epoch, s.cursor)
                    s.cursor += 1
                for ev in getattr(pipe, 'answerer_events', [])[s.answerer_cursor:]:  # 回答者逐字稿：只入记录/导出
                    self._emit(s, session, 'transcript',
                               dict(source='mic', role='answerer', text=ev.text, is_final=True),
                               s.turn_id, s.epoch, ev.text_version)
                s.answerer_cursor = len(getattr(pipe, 'answerer_events', []))
                det = pipe.detector
                if det.phase == 'answering' and s.status not in ('answering',) + CLOSED:
                    self._state(s, session, 'answering', 'answerer_started')
                elif det.answerer_active != s.answerer_active and s.status not in CLOSED:
                    self._state(s, session, s.status, 'answerer_active', s.waiting_continue, det.answerer_active)
                health = pipe.health()
                if self.pipe_health and health != s.last_health:
                    s.last_health = dict(health)
                    self._emit(s, session, 'health', dict(asr=dict(health)), s.turn_id, s.epoch, 0)
            if activity is not None:
                now = activity.clock()
                for source, ts in activity.sources.get(session, {}).items():
                    state = 'ok' if now - ts <= SOURCE_LOST_S else 'lost'
                    if s.sources.get(source, {}).get('state') != state:
                        self._emit(s, session, 'source_state', dict(source=source, state=state, level=None),
                                   s.turn_id, s.epoch, 0)
        if self.on_pull is not None:
            self.on_pull(session, pipe)

    def _state(self, s, session, state, reason, waiting=False, answerer=None, tv=0):
        answerer = s.answerer_active if answerer is None else answerer
        return self._emit(s, session, 'turn_state', dict(state=state, reason=reason, waiting_continue=waiting,
                                                         answerer_active=answerer), s.turn_id, s.epoch, tv)

    def _turn_event(self, s, session, ev):
        tv = ev.text_version
        if s.status == 'ended':
            return
        if ev.type == 'candidate':
            self._emit(s, session, 'candidate', dict(hits=[], text=ev.turn_text, text_version=tv, kind=ev.kind,
                                                     completeness=ev.completeness, reason=ev.reason),
                       s.turn_id, s.epoch, tv)
            self._state(s, session, 'question_candidate', ev.reason, tv=tv)
        elif ev.type == 'withdrawn':
            s.pending_recommit = False  # 候选被撤回：回滚留下的重提交上下文失效（下一个 committed 是新题）
            self._emit(s, session, 'candidate', dict(hits=[], text=ev.turn_text, text_version=tv, withdrawn=True,
                                                     reason=ev.reason), s.turn_id, s.epoch, tv)
            self._state(s, session, 'listening_question', ev.reason, tv=tv)
        elif ev.type == 'committed':
            recommit = s.pending_recommit
            s.pending_recommit = False
            if s.turn_id and not recommit and s.status not in CLOSED and not s.switched:
                self._emit(s, session, 'interrupt', dict(kind='switch'), s.turn_id, s.epoch, tv)
            s.switched = False
            s.epoch += 1
            if not recommit:
                s.turn_no += 1
                s.turn_id = f't{s.turn_no}'
            self._state(s, session, 'question_committed', ev.reason, tv=tv)
            s.recommitted = recommit
            self._emit(s, session, 'transcript', dict(source=s.question_source, role='questioner', text=ev.turn_text,
                                                      is_final=True, kind=ev.kind, completeness=ev.completeness,
                                                      sub_questions=list(ev.sub_questions)),
                       s.turn_id, s.epoch, tv)
        elif ev.type == 'rolled_back':
            old = s.question['text_version'] if s.question else None
            s.pending_recommit = True
            self._emit(s, session, 'turn_rollback', dict(from_version=old, to_version=tv, reason=ev.reason,
                                                         text=ev.turn_text), s.turn_id, s.epoch, tv)
            self._state(s, session, 'listening_question', ev.reason, tv=tv)
        elif ev.type == 'text_updated':  # vad 模式：commit 后文本补全，不换 epoch / turn，仅刷新展示文本
            self._emit(s, session, 'transcript', dict(source=s.question_source, role='questioner', text=ev.turn_text,
                                                      is_final=False, kind=ev.kind, completeness=ev.completeness),
                       s.turn_id, s.epoch, tv)
            if s.status == 'question_candidate' and ev.reason == 'refine_after_no_answer':
                self._state(s, session, 'question_committed', ev.reason, tv=tv)
        elif ev.type == 'waiting_continue':
            self._state(s, session, ev.resume_state or s.status, ev.reason, ev.reason.startswith('enter'), tv=tv)
        elif ev.type == 'answer_end_candidate':  # 仅回答方静音达阈值：回答保持，只提示"可能答完"
            self._state(s, session, 'answering', ev.reason, tv=tv)
        elif ev.type == 'answer_closed':
            s.pending_recommit = False  # 回答已结束：此前回滚留下的重提交上下文失效
            self._state(s, session, 'closed', ev.reason, False, False, tv=tv)
        elif ev.type == 'interrupted':  # 明确改题：前端 interrupt(switch) 置 interrupted，随后 committed 归档为 interrupted
            s.pending_recommit = False
            self._emit(s, session, 'interrupt', dict(kind='switch', reason=ev.reason), s.turn_id, s.epoch, tv)
            s.switched = True
        elif ev.type == 'interrupt':
            self._emit(s, session, 'interrupt', dict(kind=ev.kind, reason=ev.reason, text=ev.turn_text),
                       s.turn_id, s.epoch, tv)
            if ev.kind == 'followup':  # 追问：主答案保持，补充区追加提示
                self._emit(s, session, 'prompt_supplement', dict(source_tag='追问', content=ev.turn_text, url=None,
                                                                elapsed_ms=None, level=''),
                           s.turn_id, s.epoch, tv)
        elif ev.type == 'queued':
            self._emit(s, session, 'queue_update', dict(items=s.queue + [ev.turn_text]), s.turn_id, s.epoch, tv)
        elif ev.type == 'dequeued':
            self._emit(s, session, 'queue_update', dict(items=s.queue[1:]), s.turn_id, s.epoch, tv)
