"""会话编排管线（M05-4）：live 帧 -> VAD -> ASR -> TurnDetector -> 埋点。

规则：
- replay 帧只写盘（由 AudioHub 负责），这里直接返回，绝不进 VAD/ASR/轮次判定。
- 每个 source 一条路由（VAD + 可选 ASR），按 (stream_id, generation) 标识；新 generation 重置该路状态，
  旧 generation 的迟到 live 帧丢弃。音频时间轴 t_ms = seq*20 在新 generation 回到 0，
  TurnDetector 需要单调时间，所以新 generation 路由带 base_ms（= 切换时刻的会话音频时钟）平移后再喂给它。
- 线上模式：meeting=提问方（VAD+ASR 喂 TurnDetector），mic=回答方（VAD 驱动 on_answerer_active）；
  answerer_asr=True 时 mic 也做 ASR，但只进 answerer_events 供逐字稿/导出，不喂 TurnDetector，且在后台任务里推送。
- 线下模式：room 通道只有一路混合音，SpeakerRoleTracker 在 VAD 语音段内逐段判角色；角色确定后才把该段
  的 VAD / 转写交给 TurnDetector（questioner -> on_vad / on_transcript，answerer -> on_answerer_active），
  角色段事件另存 role_events（不进 turn_events，避免干扰 AnswerLoop 的任务序号）。
- 线上模式且会议通道缺席（会话内尚未出现过 meeting 的 live 帧）时，若声纹已登记，mic 这一路按“线下同室”处理：
  单麦门控（mic_gate）下 mic 的 VAD 段经 SpeakerRoleTracker 判角色，questioner 段喂 TurnDetector，
  answerer 段只驱动 on_answerer_active，unknown 段只计数、既不提问也不当回答。
  一旦 meeting 首次出现，立即切回双通道语义（mic=回答方、meeting=提问方）并清空门控状态，避免重复提交。
  开关关闭或声纹未登记时不启用门控，保持原行为。
- 健康：可选 monitor（HealthMonitor）接收帧 / VAD 语音 / ASR 文本 / ASR 健康 / 不确定态；可选 echo 在线上模式检测回声。
- 埋点 turn id 与 board 的 t{n} 一致（见 _metric_turn）。
- 不读墙钟：时间推进只来自帧的音频时刻；唯一的时钟读数是埋点用的 perf_counter_ns。
"""
import asyncio
import dataclasses
import json
from time import perf_counter_ns

from ..asr.base import BACKOFFS_S, ReconnectingASR
from ..audio.vad import VadEvent, make_vad
from ..domain.roles import role_for
from ..domain.turn_detector import TurnDetector

FRAME_MS = 20
METRIC_TYPES = ('candidate', 'committed', 'withdrawn', 'rolled_back', 'text_updated', 'answer_end_candidate',
                'answer_closed', 'interrupted', 'queued', 'dequeued', 'interrupt')
SETTLE_ROUNDS = 4  # ASR 内部 queue -> 重连包装 queue -> 消费任务，需要让出事件循环几轮
_SEVERITY = {'ok': 0, 'reconnecting': 1, 'down': 2}


async def _yield_once():
    fut = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(lambda: fut.done() or fut.set_result(None))
    await fut


@dataclasses.dataclass
class _Route:
    key: tuple  # (stream_id, generation)
    base_ms: int
    vad: object
    asr: object = None
    task: object = None
    source: str = ''
    room: dict = None  # 线下 room 路 / 线上单麦门控路：{'active','start','role'}
    role: str = ''
    queue: object = None  # 回答者 ASR 的推送队列（后台任务消费，不阻塞面试官通道）
    push_task: object = None
    silenced: bool = False  # 切回双通道时被丢弃的门控路：不再向 TurnDetector 投递任何结果


class SessionPipeline:
    def __init__(self, session_id, store, asr_factory, metrics, mode='online', vad_factory=make_vad,
                 detector=None, reconnect_backoffs=BACKOFFS_S, monitor=None, echo=None, on_echo=None,
                 room_tracker=None, on_voice=None, answerer_asr=False, mic_gate=False, mic_gate_reason='',
                 on_gate=None, monologue_segments=3, T_monologue=10000):
        self.session_id = session_id
        self.store = store
        self.asr_factory = asr_factory  # source -> ASRAdapter
        self.metrics = metrics
        self.mode = mode
        self.vad_factory = vad_factory
        self.detector = detector or TurnDetector()
        self.reconnect_backoffs = reconnect_backoffs
        self.turn_events = []
        self.event_ns = []  # 与 turn_events 一一对应的 perf_counter_ns（入列时刻），供提示延迟统计
        self._routes = {}  # source -> _Route
        self._bases = {}  # source -> base_ms，同一 generation 重连时沿用
        self._generations = {}  # source -> 最近的 generation
        self._now_ms = 0  # 会话音频时钟（TurnDetector 时间轴）
        self._turn_n = 0  # 已提交的题数（与 EventBus.turn_no 同规则）
        self._recommit = False  # rolled_back 之后的 committed 沿用同一题
        self._lock = None
        self._health = {}  # source -> 最近一次 ASR health
        self.monitor, self.echo, self.on_echo = monitor, echo, on_echo
        self.room_tracker, self.on_voice = room_tracker, on_voice
        self.answerer_asr = answerer_asr  # 回答者（mic）逐字稿开关；只入记录，不参与轮次判定
        self.answerer_events = []  # 回答者 final 转写（供 EventBus -> 记录库 -> 导出）
        self.role_events = []  # 线下角色段 / 单麦门控角色段（tracker 输出的 speaker_segment dict）
        self._q_spans = []  # 线下：提问方语音段 [start, end|None]（TurnDetector 时间轴）
        self._voice_ok = None
        self._uncertain = False
        self._echo_pending = {}  # source -> {seq: pcm}，等另一路同 seq 帧配对
        self.mic_gate = mic_gate  # 线上且会议通道缺席时，是否把 mic 按声纹角色门控处理
        self.mic_gate_reason = mic_gate_reason  # 未启用原因（not_enrolled / model_unavailable / disabled_by_config）
        self.on_gate = on_gate  # (stats dict) -> None，供 health/诊断
        self._seen_live = set()  # 会话内出现过的 live 帧来源（判断会议通道是否缺席）
        self._gate_last = None  # 上次上报的门控状态（去重）
        self.question_source = 'meeting'  # 喂 TurnDetector 的提问方来源（board 逐字稿标注用）
        # 单麦门控独白：连续 questioner 段且 T_monologue 内无 answerer -> 暂停提词（【待标定】）
        self.monologue_segments = monologue_segments
        self.T_monologue = T_monologue
        self._last_answerer_t = 0  # 最近一次 answerer 段结束时刻（音频时钟；0=尚未出现）
        self._q_streak = 0  # 连续 questioner 段数（answerer 段清零）
        self._monologue = False
        self.mic_gate_stats = dict(
            state='active' if mic_gate and room_tracker is not None else 'unavailable' if mic_gate else 'off',
            reason=mic_gate_reason, counts=dict(questioner=0, answerer=0, unknown=0),
            last_latency_ms=None, latency_n=0, latency_sum_ms=0.0, avg_latency_ms=None, monologue=False)

    # ---- 对外 ----
    async def on_frame(self, frame):
        if frame['replay']:
            return
        source = frame['source']
        if self.monitor is not None:
            self.monitor.on_source_frame(source, self.monitor.clock())
        self._echo_feed(source, frame)
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            first_meeting = source == 'meeting' and source not in self._seen_live
            self._seen_live.add(source)
            if first_meeting and self.mode == 'online' and self.mic_gate:
                await self._on_meeting_joined()
            role = self._role_for(source)
            if role not in ('questioner', 'answerer', 'room'):
                return
            route = await self._route_for(frame, role)
            if route is None:
                return
            t_ms = frame['seq'] * FRAME_MS
            end = route.base_ms + t_ms + FRAME_MS
            self._now_ms = max(self._now_ms, end)
            self._dispatch_vad(role, route, route.vad.feed(frame['pcm'], t_ms), self._now_ms)
            if route.room is not None and route.room['active']:
                for seg in self.room_tracker.feed(frame['pcm']):
                    self._room_segment(route, seg, self._now_ms)
            if route.asr is not None:
                if route.role == 'answerer':
                    route.queue.put_nowait((frame['pcm'], t_ms))  # 后台推送：不等它，避免拖慢面试官通道
                else:
                    await route.asr.push(frame['pcm'], t_ms)
                    for _ in range(SETTLE_ROUNDS):
                        await _yield_once()
                    self._health[source] = route.asr.health()
                    if self.monitor is not None:
                        self.monitor.on_asr_health(self.health())
            self._record(self.detector.tick(self._now_ms))
            if self.monitor is not None and self.detector.waiting_continue != self._uncertain:
                self._uncertain = self.detector.waiting_continue
                self.monitor.on_uncertain(self._uncertain)

    def health(self):
        worst = {'state': 'ok', 'reason': ''}
        for h in self._health.values():
            if _SEVERITY[h['state']] > _SEVERITY[worst['state']]:
                worst = dict(h)
        return worst

    async def close(self, final=False):
        """收尾：VAD flush、关闭 ASR 并等消费任务排空。可重复调用；之后再来 live 帧会重新建路由。
        final=True 表示会话正常结束（而非 WS 断线），此时通知 monitor 不再按“无帧”判音源丢失。"""
        for source in list(self._routes):
            await self._drop_route(source, final)
        self._lock = None

    # ---- 路由 ----
    def _role_for(self, source):
        """通道 -> 角色。线上且会议通道尚未出现时，mic 走单麦门控（等价 offline room）。"""
        if self.mode == 'offline' and self.room_tracker is not None and source == 'room':
            self._voice_health()
            return 'room'
        if self.mode == 'online' and self.mic_gate and source == 'mic' and 'meeting' not in self._seen_live:
            if self.room_tracker is not None:
                self._voice_health()
                self._notify_gate('active')
                return 'room'
            self._notify_gate('unavailable')
        return role_for(source, self.mode)

    def _gate_route(self):
        route = self._routes.get('mic')
        return route if route is not None and route.room is not None else None

    async def _on_meeting_joined(self):
        """会议通道首次出现：单麦门控必须立刻让位给双通道语义。"""
        if self._gate_route() is not None:
            await self._leave_mic_gate()
        else:
            self._notify_gate('dual')

    async def _leave_mic_gate(self):
        """丢弃 mic 门控路：关闭当前角色段（只发一次 speech_end，不重放已保留的转写），
        清空 tracker/q_spans 后再按 answerer 建新路由。"""
        route = self._routes.pop('mic', None)
        if route is None or route.room is None:
            return
        if route.room.get('active'):
            self._room_vad(route, VadEvent('speech_end', self._now_ms), self._now_ms)
        route.silenced = True
        route.room = None
        await self._drop_route_obj(route, final=False, flush=False)
        self._reset_gate_state()
        self._notify_gate('switched')

    def _reset_gate_state(self):
        reset = getattr(self.room_tracker, 'reset', None)
        if reset is not None:
            reset()
        self._q_spans = []
        self._last_answerer_t, self._q_streak, self._monologue = 0, 0, False
        self.mic_gate_stats['monologue'] = False

    def _notify_gate(self, state=None):
        if not self.mic_gate:
            return
        s = self.mic_gate_stats
        if state is not None:
            s['state'] = state
        payload = dict(state=s['state'], reason=self.mic_gate_reason if s['state'] in ('off', 'unavailable') else '',
                       counts=dict(s['counts']), last_latency_ms=s['last_latency_ms'],
                       avg_latency_ms=s['avg_latency_ms'], monologue=s['monologue'])
        if payload == self._gate_last:
            return
        self._gate_last = payload
        if self.on_gate is not None:
            self.on_gate(payload)

    def _count_gate_segment(self, seg):
        """单麦门控：每段最终角色计数 + 角色确定耗时埋点/health。unknown 只计数，绝不当作提问或回答。
        同时维护独白判定：连续 questioner 段且 T_monologue 内无 answerer 即暂停提词，answerer 段即恢复。"""
        s = self.mic_gate_stats
        role = seg.get('role')
        if role in s['counts']:
            s['counts'][role] += 1
        if self.mode == 'online':
            if role == 'answerer':
                self._last_answerer_t = seg.get('t_end') or self._now_ms
                self._q_streak = 0
                self._set_monologue(False)
            elif role == 'questioner':
                self._q_streak += 1
                if self._q_streak >= self.monologue_segments \
                        and self._now_ms - self._last_answerer_t >= self.T_monologue:
                    self._set_monologue(True)
        lat = seg.get('latency_ms')
        if isinstance(lat, (int, float)):
            s['last_latency_ms'] = int(round(lat))
            s['latency_sum_ms'] += float(lat)
            s['latency_n'] += 1
            s['avg_latency_ms'] = int(round(s['latency_sum_ms'] / s['latency_n']))
        self.metrics.mark(self.session_id, f't{max(1, self._turn_n)}', 'role_latency', perf_counter_ns(),
                          json.dumps({'role': role, 'latency_ms': lat, 't_start': seg.get('t_start'),
                                      't_end': seg.get('t_end'), 'flags': seg.get('flags')}, ensure_ascii=False))
        self._notify_gate()

    def _set_monologue(self, on):
        if on == self._monologue:
            return
        self._monologue = on
        self.mic_gate_stats['monologue'] = on
        self._notify_gate()

    async def _route_for(self, frame, role):
        source = frame['source']
        key = (frame['stream_id'], frame['generation'])
        route = self._routes.get(source)
        if route is not None and route.key == key:
            return route
        last_gen = self._generations.get(source)
        if last_gen is not None and frame['generation'] < last_gen:
            return None  # 旧 generation 的迟到 live 帧
        if route is not None:
            await self._drop_route(source)
        if last_gen is not None and frame['generation'] != last_gen:
            self._bases[source] = self._now_ms
        base = self._bases.setdefault(source, 0)
        self._generations[source] = frame['generation']
        route = _Route(key, base, self.vad_factory(source), source=source, role=role)
        if role in ('questioner', 'room'):
            self.question_source = source
        if role == 'room':
            route.room = dict(active=False, start=0, role=None, held=[])
        if role in ('questioner', 'room') or (role == 'answerer' and self.answerer_asr):
            factory = lambda: self.asr_factory(source)  # noqa: E731
            route.asr = ReconnectingASR(factory, backoffs=self.reconnect_backoffs)
            await route.asr.start(f'{self.session_id}:{source}:{key[0]}:{key[1]}')
            route.task = asyncio.create_task(self._consume(route))
            if role == 'answerer':
                route.queue = asyncio.Queue()
                route.push_task = asyncio.create_task(self._answerer_push(route))
        self._routes[source] = route
        return route

    async def _drop_route(self, source, final=False, flush=True):
        route = self._routes.pop(source)
        await self._drop_route_obj(route, final, flush)

    async def _drop_route_obj(self, route, final=False, flush=True):
        if flush:
            role = 'room' if route.room is not None else role_for(route.source, self.mode)
            self._dispatch_vad(role, route, route.vad.flush(), self._now_ms)
        if route.asr is not None:
            if route.role == 'answerer':
                await self._stop_answerer(route)
            else:
                self._health[route.source] = route.asr.health()
                await route.asr.close()
                await route.task
        if final and self.monitor is not None:
            self.monitor.on_source_closed(route.source)

    async def _answerer_push(self, route):
        """回答者 ASR 推送队列：独立后台任务，面试官通道的 on_frame 只入队不等待。"""
        while True:
            item = await route.queue.get()
            if item is None:
                return
            try:
                await route.asr.push(*item)
            except Exception:
                return
            await _yield_once()

    async def _stop_answerer(self, route):
        route.queue.put_nowait(None)
        await route.push_task
        for _ in range(SETTLE_ROUNDS):  # 让已产出的转写经 pump 进入 answerer_events
            await _yield_once()
        await route.asr.close()
        await route.task

    # ---- 事件分发 ----
    def _dispatch_vad(self, role, route, events, now_ms):
        for ev in events:
            shifted = VadEvent(ev.kind, ev.t_ms + route.base_ms)
            if ev.kind == 'speech_start' and route.asr is not None and route.role != 'answerer' and self.monitor is not None:
                self.monitor.on_vad_speech(route.source, self.monitor.clock())
            if role == 'room':
                self._room_vad(route, shifted, now_ms)
            elif role == 'questioner':
                self._record(self.detector.on_vad(shifted, now_ms))
            else:
                self._record(self.detector.on_answerer_active(ev.kind == 'speech_start', shifted.t_ms))

    # ---- 线下 room 路：声纹角色门控 ----
    def _room_vad(self, route, ev, now_ms):
        rs, tr = route.room, self.room_tracker
        if ev.kind == 'speech_start':
            tr.in_answer = self.detector.phase == 'answering'
            tr.on_speech_start(ev.t_ms)
            rs.update(active=True, start=ev.t_ms, role=None, held=[])
            return
        if not rs['active']:
            return
        seg = tr.on_speech_end(ev.t_ms)
        if seg is not None:
            self._room_segment(route, seg, now_ms)
        rs['active'] = False
        if rs['role'] == 'questioner':
            self._q_spans[-1][1] = ev.t_ms
            if not self._monologue:
                self._record(self.detector.on_vad(ev, now_ms))
        elif rs['role'] == 'answerer':
            self._record(self.detector.on_answerer_active(False, ev.t_ms))
        rs['role'], rs['held'] = None, []

    def _room_segment(self, route, seg, now_ms):
        """tracker 输出一段（滚动或最终）：记录角色段；本段首次得到明确角色时，补发该段 speech_start。"""
        rs = route.room
        self.role_events.append(seg)
        self._voice_health()
        if self.mode == 'online' and seg.get('final'):
            self._count_gate_segment(seg)
        if self._monologue and seg['role'] == 'questioner':
            return  # 独白已确认：questioner 段只计数 / 入 role_events，不再喂 TurnDetector
        if rs['role'] is not None or seg['role'] not in ('questioner', 'answerer'):
            return
        rs['role'] = seg['role']
        if seg['role'] == 'questioner':
            self._q_spans.append([rs['start'], None])
            self._record(self.detector.on_vad(VadEvent('speech_start', rs['start']), now_ms))
            for ev in rs['held']:  # 角色确定前到达的转写补喂
                self._record(self.detector.on_transcript(ev, now_ms))
            rs['held'] = []
        else:
            self._record(self.detector.on_answerer_active(True, rs['start']))

    def _voice_health(self):
        ok = not self.room_tracker.degraded
        if ok != self._voice_ok:
            self._voice_ok = ok
            if self.on_voice is not None:
                self.on_voice(ok, self.room_tracker.degraded_reason or '')

    def _questioner_time(self, ev):
        return any(ev.t_audio_start_ms <= (b if b is not None else float('inf')) and ev.t_audio_end_ms >= a
                   for a, b in self._q_spans)

    # ---- 线上回声检测 ----
    def _echo_feed(self, source, frame):
        if self.echo is None or source not in ('mic', 'meeting'):
            return
        other = 'meeting' if source == 'mic' else 'mic'
        seq = frame['seq']
        peer = self._echo_pending.get(other, {}).pop(seq, None)
        if peer is None:
            mine = self._echo_pending.setdefault(source, {})
            mine[seq] = frame['pcm']
            for k in [k for k in mine if k < seq - 100]:  # 另一路迟迟不来：丢弃 2s 前的
                del mine[k]
            return
        mic, meeting = (frame['pcm'], peer) if source == 'mic' else (peer, frame['pcm'])
        ev = self.echo.feed(mic, meeting)
        if ev is not None and self.on_echo is not None:
            self.on_echo(ev['type'] == 'echo_suspected')

    async def _consume(self, route):
        async for ev in route.asr.events():
            if route.silenced:
                continue  # 单麦门控已切回双通道：丢弃旧门控路的迟到结果，避免重复提交
            if route.base_ms:
                ev = dataclasses.replace(
                    ev, t_audio_start_ms=ev.t_audio_start_ms + route.base_ms,
                    t_audio_end_ms=ev.t_audio_end_ms + route.base_ms,
                    audio_covered_until_ms=ev.audio_covered_until_ms + route.base_ms)
            if route.role == 'answerer':  # 只入逐字稿，绝不喂 TurnDetector
                if ev.is_final:
                    self.answerer_events.append(ev)
                continue
            if self.monitor is not None:
                self.monitor.on_asr_text(route.source, self.monitor.clock())
            if route.room is not None:
                if self._monologue:
                    continue  # 独白暂停提词：转写只留在 ASR 侧，不喂 TurnDetector
                rs = route.room
                if rs['active'] and rs['role'] is None:
                    rs['held'].append(ev)  # 本段角色未定：先留着
                    continue
                if not self._questioner_time(ev):
                    continue  # 线下：非提问方时段的转写不喂 TurnDetector
            self._record(self.detector.on_transcript(ev, self._now_ms))

    # ---- 轮次事件与埋点 ----
    def _metric_turn(self, ev):
        """与 EventBus 的 t{n} 同规则：committed 换题（rolled_back 之后的重提交沿用）；
        提交前的 candidate / withdrawn 记在即将成立的那一题上。"""
        if ev.type == 'committed':
            if not self._recommit:
                self._turn_n += 1
            self._recommit = False
        elif ev.type == 'rolled_back':
            self._recommit = True
        elif ev.type == 'withdrawn':
            pending = self._recommit
            self._recommit = False  # 候选被撤回：回滚留下的重提交上下文失效
            return f't{self._turn_n}' if pending else f't{self._turn_n + 1}'
        elif ev.type in ('answer_closed', 'interrupted'):
            self._recommit = False  # 回答已结束 / 明确改题：回滚留下的重提交上下文失效
        elif ev.type == 'candidate' and not self._recommit:
            return f't{self._turn_n + 1}'
        return f't{self._turn_n}'

    def _record(self, events):
        for ev in events:
            self.turn_events.append(ev)
            self.event_ns.append(perf_counter_ns())
            turn = self._metric_turn(ev)
            if ev.type in METRIC_TYPES:
                latency = None if ev.t_speech_end_ms is None else ev.t_ms - ev.t_speech_end_ms
                self.metrics.mark(self.session_id, turn, ev.type, perf_counter_ns(), json.dumps(
                    {'t_ms': ev.t_ms, 't_speech_end_ms': ev.t_speech_end_ms, 'latency_ms': latency,
                     'reason': ev.reason, 'text_version': ev.text_version, 'coverage_stale': ev.coverage_stale}, ensure_ascii=False))
