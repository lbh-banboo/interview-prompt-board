"""主线接线：每会话的配置快照、健康运行时（CapabilityBoard + HealthMonitor）、ASR 选择、声纹 tracker、记录落库。

server.py 只负责建对象与挂钩子；各钩子的逻辑集中在 Wiring，便于单测。
"""
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from .asr.aliyun import AliyunRealtimeASR
from .asr.hotwords import extract_hotwords
from .asr.metered import CHUNK_S as ASR_CHUNK_S, FALLBACK_NOTICE, MeteredASR
from .asr.mock import MockASR
from .asr.sherpa import SherpaStreamingASR
from .audio.echo import HINT as ECHO_HINT, EchoDetector
from .audio.enroll import VoiceProfileStore
from .audio.voiceprint import DEFAULT_MODEL, load_extractor
from .domain.speaker_roles import SpeakerRoleTracker
from .domain.turn_detector import TurnDetector
from .health.capabilities import CapabilityBoard
from .health.monitor import HealthMonitor

ROOT = Path(__file__).resolve().parents[1]
SHERPA_DIR = ROOT / 'models' / 'sherpa-streaming-paraformer-bilingual-zh-en'
PROFILE = 'default'  # 与 app.api.voice 一致
LLM_HOST = ('api.deepseek.com', 443)
RETRIEVAL_REASON = {'degraded': '仅关键词检索', 'down': '检索不可用'}
CONFIG_MISSING = '配置缺失'


def _tcp_probe(host, port):
    async def probe():
        _, writer = await asyncio.open_connection(host, port)
        writer.close()
        return True
    return probe


class SessionHealth:
    """一场会话的能力看板。board 是唯一来源；retrieval 与外发/回声等提示不在降级矩阵里，叠加在 payload 上。"""

    def __init__(self, session, bus, clock, probes=None, disk_path='.'):
        self.session, self.bus, self.clock = session, bus, clock
        self.overrides = {}  # 能力 -> {state, reason, since}
        self.notes = {}  # 名称 -> 提示文案
        self.board = CapabilityBoard(clock, on_change=lambda _payload: self.publish())
        self.monitor = HealthMonitor(self.board, clock, probes=probes, disk_path=disk_path)

    def payload(self):
        p = self.board.payload()
        p.update({k: dict(v) for k, v in self.overrides.items()})
        p['notices'] = p['notices'] + list(self.notes.values())
        return p

    def publish(self):
        self.bus.publish(self.session, 'health', self.payload())

    def note(self, name, text):
        if text:
            self.notes[name] = text
        else:
            self.notes.pop(name, None)
        self.publish()

    def set_retrieval(self, state):
        if state in RETRIEVAL_REASON:
            self.overrides['retrieval'] = dict(state=state, reason=RETRIEVAL_REASON[state], since=self.clock())
        else:
            self.overrides.pop('retrieval', None)
        self.note('retrieval', '检索不可用，暂无标准答案' if state == 'down' else None)

    def set_capability(self, name, state, reason=''):
        """答案环上报检索之外的能力状态（如搜索传输未配置）；ok 即撤销覆盖；无变化不重复发布。"""
        cur = self.overrides.get(name)
        if state == 'ok':
            if self.overrides.pop(name, None) is None:
                return
        elif cur and (cur['state'], cur['reason']) == (state, reason):
            return
        else:
            self.overrides[name] = dict(state=state, reason=reason, since=self.clock())
        self.publish()

    def set_echo(self, active):
        self.note('echo', ECHO_HINT if active else None)

    def set_mic_gate(self, payload):
        """单麦门控状态 / 计数（health 载荷）；同一状态无变化不重复发布。"""
        cur = self.overrides.get('mic_gate')
        if payload is None:
            if cur is None:
                return
            self.overrides.pop('mic_gate', None)
            self.notes.pop('monologue', None)
        else:
            p = dict(payload)
            p.setdefault('state', 'off')
            p['since'] = cur['since'] if cur and cur.get('state') == p['state'] else self.clock()
            if cur == p:
                return
            self.overrides['mic_gate'] = p
            if p.get('monologue'):
                self.notes['monologue'] = '检测到连续独白，已暂停提词'
            else:
                self.notes.pop('monologue', None)
        self.publish()

    def start(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.monitor.start()

    async def stop(self):
        try:
            await self.monitor.stop()
        except RuntimeError:  # 任务属于已关闭的事件循环（TestClient 每请求一个循环）
            pass


class Wiring:
    def __init__(self, store, manager, creds, bus, clock=time.monotonic, voice_cfg=None, budget=None):
        self.store, self.manager, self.creds, self.bus, self.clock = store, manager, creds, bus, clock
        self.voice_cfg = voice_cfg or {}
        self.budget = budget  # BudgetGuard；None 表示不计量（测试/未接线）
        self.records = None  # 由 server 在 records.register 之后设置
        self.sessions = {}  # session -> SessionHealth
        self.asr_backend = {}  # session -> 'aliyun' | 'sherpa' | 'none'
        self._pulls, self._role_cursor, self._extractor, self._turn_state = {}, {}, [], {}
        if budget is not None and hasattr(budget, 'on_denied'):
            budget.on_denied = self._budget_notice

    def _budget_notice(self, res, session):
        """预算耗尽经 health notices 顶层提示；并发占满属短暂状态，不刷提示。"""
        if res.reason == 'inflight':
            return
        rt = self.sessions.get(session) or self.health(session)
        name = f'budget_{res.service}'
        if rt.notes.get(name) == res.message:
            return
        rt.note(name, res.message)

    # ---- 配置快照 ----
    def freeze(self, sid):
        """会话开始：把 ConfigManager 当前配置写入 sessions.config_snapshot_json；已有 setup 快照则并入。幂等。"""
        with self.store.lock, self.store.connect() as db:
            row = db.execute('SELECT config_snapshot_json FROM sessions WHERE id=?', (sid,)).fetchone()
            if row is None:
                return
            cur = json.loads(row[0]) if row[0] else {}
            if 'turn' in cur:
                return
            if cur:
                merged = {**cur, **self.manager.config}
                # 会前向导的本场外发覆盖（只允许关闭）优先于全局配置
                if cur.get('outbound'):
                    merged['outbound'] = {**merged.get('outbound', {}), **cur['outbound']}
                snap = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            else:
                snap = self.manager.snapshot_for_session()
            db.execute('UPDATE sessions SET config_snapshot_json=? WHERE id=?', (snap, sid))

    def snapshot(self, sid):
        with self.store.connect() as db:
            row = db.execute('SELECT config_snapshot_json FROM sessions WHERE id=?', (sid,)).fetchone()
        snap = json.loads(row[0]) if row and row[0] else {}
        return snap if 'turn' in snap else self.manager.config  # 未冻结（旧数据）退回当前配置

    def idle_timeout(self, sid):
        return self.snapshot(sid)['session']['idle_timeout_s']

    # ---- 健康 ----
    def health(self, sid):
        rt = self.sessions.get(sid)
        if rt is None:
            rt = self.sessions[sid] = SessionHealth(sid, self.bus, self.clock, self._probes(), self.store.root)
            self._missing_key_faults(rt)
            rt.publish()
        return rt

    def _probes(self):
        probes = {}
        if self.creds.get('DEEPSEEK_API_KEY'):
            probes['llm'] = _tcp_probe(*LLM_HOST)
        if self.creds.get('SEARCH_API_KEY') or self.creds.get('DEEPSEEK_API_KEY'):
            probes['search'] = _tcp_probe(*LLM_HOST)  # 联网检索与模型同 Key、同服务商
        return probes

    def _missing_key_faults(self, rt):
        """无 Key 时不发请求，直接标 down（reason 配置缺失）。"""
        if not self.creds.get('DEEPSEEK_API_KEY'):
            rt.board.raise_fault('llm_down', reason=CONFIG_MISSING)
        if not (self.creds.get('SEARCH_API_KEY') or self.creds.get('DEEPSEEK_API_KEY')):
            rt.board.raise_fault('search_down', reason=CONFIG_MISSING)

    async def stop_session(self, sid):
        rt = self.sessions.pop(sid, None)
        if rt is not None:
            await rt.stop()

    # ---- ASR 选择 ----
    def hotwords(self, sid):
        with self.store.connect() as db:
            row = db.execute('SELECT library_id FROM sessions WHERE id=?', (sid,)).fetchone()
        if not row or not row[0]:
            return []
        texts = [f"{e['question']} {e['approved_answer']}" for e in self.store.entries(row[0]) if e['status'] == 'approved']
        return extract_hotwords(texts, blocklist=self._alias_blocklist(sid)).words

    def _alias_blocklist(self, sid):
        """热词会发给云 ASR：回答者代号/姓名不下发（取不到就为空，与 answer._vocab 同口径）。"""
        try:
            with self.store.connect() as db:
                row = db.execute('SELECT p.alias FROM sessions s JOIN profiles p ON p.id=s.profile_id WHERE s.id=?',
                                 (sid,)).fetchone()
        except sqlite3.OperationalError:
            return []
        return [row[0]] if row and row[0] else []

    def asr_factory(self, sid):
        """asr.backend 选择：auto（有云 Key 且外发开 -> 阿里云，否则本地 sherpa）/ aliyun / sherpa。
        aliyun 受 outbound.asr_audio 约束；room / 单麦混音同样走云（已确认允许，质量优先），
        asr_answerer 只控制回答者逐字稿是否入记录/导出，不控制是否上传。
        显式选择不可用时降级到本地，并留下提示。
        云 ASR 按时长计入预算：预算不足时不建连，降级为不识别新题。"""
        rt = self.health(sid)
        snap = self.snapshot(sid)
        outbound = snap.get('outbound') or {}
        cloud_allowed = bool(outbound.get('asr_audio', True))
        backend = (snap.get('asr') or {}).get('backend', 'auto')
        key = self.creds.get('DASHSCOPE_API_KEY')
        if backend == 'tencent':  # 旧快照：腾讯 ASR 已移除，不阻止会话
            rt.note('asr_backend', '腾讯 ASR 已移除，使用本地转写')
            backend = 'auto'
        if not cloud_allowed:
            if backend == 'aliyun':
                rt.note('asr_backend', '所选云转写已被外发开关关闭，使用本地转写')
            else:
                rt.note('asr_outbound', '云端转写已按外发开关关闭，使用本地转写')
        elif backend in ('auto', 'aliyun'):
            if key:
                words = self.hotwords(sid) or None  # 库为空则不下发
                self.asr_backend[sid] = 'aliyun'

                def factory(source):
                    if source in ('room', 'mic'):
                        # 混音只有一路，含回答方声音；已确认“允许混音上云”（质量优先）。
                        # asr_answerer 只控制逐字稿是否入记录/导出，不控制是否上传。
                        # 双通道下 mic 只在 answerer_asr 开启时才会建路由，到这里同样是回答声音在上云。
                        rt.note('asr_mixed_cloud', '麦克风/房间音频已上云转写（含你的回答声音）')
                    self.asr_backend[sid] = 'aliyun'
                    return self._cloud_asr(rt, sid, source, key, words)
                return factory
            if backend == 'aliyun':
                rt.note('asr_backend', '已选阿里云转写但未配置 DASHSCOPE_API_KEY，使用本地转写')
        return self._local_asr(rt, sid)

    def _local_asr(self, rt, sid):
        if self._sherpa_available():
            self.asr_backend[sid] = 'sherpa'
            return lambda source: SherpaStreamingASR(source, model_dir=str(SHERPA_DIR))
        self.asr_backend[sid] = 'none'
        rt.board.raise_fault('asr_disconnected', 'unavailable', reason='无可用转写后端')
        return lambda source: MockASR([], source)

    @staticmethod
    def _sherpa_available():
        return (SHERPA_DIR / 'encoder.int8.onnx').is_file()

    def _sherpa_adapter(self, source):
        """本地回退用：本机模型不存在或加载失败时返回 None（不回退，才置 down）。"""
        if not self._sherpa_available():
            return None
        try:
            return SherpaStreamingASR(source, model_dir=str(SHERPA_DIR))
        except Exception:
            logging.getLogger('app').exception('本地转写回退初始化失败')
            return None

    def _metered(self, rt, sid, source, make):
        if self.budget is None:
            return make()
        lease = self.budget.reserve('asr', ASR_CHUNK_S, sid)
        if not lease.ok and lease.reason != 'inflight':
            local = self._sherpa_adapter(source)
            if local is not None:  # 预算用尽：自动回退本地，不计量金额
                self.asr_backend[sid] = 'sherpa'
                rt.note('asr_budget', FALLBACK_NOTICE)
                return local
            self._asr_exhausted(rt, sid, lease.message, lease.reason)
            return MockASR([], source)
        fallback = (lambda: self._sherpa_adapter(source)) if self._sherpa_available() else None
        return MeteredASR(make(), self.budget, sid, lease if lease.ok else None, fallback=fallback,
                          on_exhausted=lambda message, reason: self._asr_exhausted(rt, sid, message, reason),
                          on_fallback=lambda message: self._asr_fallback(rt, sid, message))

    def _cloud_asr(self, rt, sid, source, key, hotwords):
        return self._metered(rt, sid, source,
                             lambda: AliyunRealtimeASR(api_key=key, source=source, hotwords=hotwords))

    def _asr_fallback(self, rt, sid, message):
        self.asr_backend[sid] = 'sherpa'
        rt.note('asr_budget', message or FALLBACK_NOTICE)

    def _asr_exhausted(self, rt, sid, message, reason='daily'):
        if reason == 'inflight':  # 并发占满不是致命错误，不置 down
            rt.note('asr_budget', message)
            return
        self.asr_backend[sid] = 'none'
        rt.set_capability('asr', 'down', '云 ASR 预算用尽')
        rt.note('asr_budget', message)

    # ---- 声纹 ----
    def tracker(self, source='room'):
        if not self._extractor:
            self._extractor.append(self.voice_cfg.get('voice_extractor')
                                   or load_extractor(self.voice_cfg.get('voice_model', DEFAULT_MODEL)))
        row = VoiceProfileStore(self.store.connect, self.store.lock, self.store.root).load(PROFILE)
        return SpeakerRoleTracker(template=row[0] if row else None, extractor=self._extractor[0], source=source)

    # ---- 管线参数（AudioHub.configure）----
    def configure(self, sid, mode, trigger, inject_asr):
        snap = self.snapshot(sid)
        t = snap['turn']
        rt = self.health(sid)
        kw = dict(detector=TurnDetector(trigger=trigger, T_silence=t['T_silence'], T_long=t['T_long'],
                                        T_hard=t['T_hard'], T_answer_silence=t['T_answer_silence'],
                                        T_new_turn=t.get('T_new_turn', 1200), max_turn_ms=t.get('max_turn_ms', 30000),
                                        max_turn_chars=t.get('max_turn_chars', 200)),
                  monitor=rt.monitor, answerer_asr=bool(snap['outbound'].get('asr_answerer', False)),
                  monologue_segments=t.get('monologue_segments', 3), T_monologue=t.get('T_monologue', 10000))
        if not inject_asr:
            kw['asr_factory'] = self.asr_factory(sid)
        if mode == 'online':
            kw.update(echo=EchoDetector(), on_echo=rt.set_echo)
            # 线上跳过会议音频时，mic 可用声纹区分提问方/回答方；需已登记声纹，且默认开启、可在设置关闭。
            if not bool((snap.get('turn') or {}).get('mic_as_question', True)):
                rt.set_mic_gate(dict(state='off', reason='disabled_by_config'))
            else:
                tracker = self.tracker('mic')
                reason = ''
                if tracker.degraded:
                    reason = 'not_enrolled' if tracker.degraded_reason == 'no_template' else tracker.degraded_reason
                    rt.set_mic_gate(dict(state='unavailable', reason=reason))
                else:
                    rt.monitor.on_voice_health(True)
                    kw.update(room_tracker=tracker, on_voice=rt.monitor.on_voice_health)
                kw.update(mic_gate=True, mic_gate_reason=reason, on_gate=rt.set_mic_gate)
        else:
            tracker = self.tracker()
            rt.monitor.on_voice_health(not tracker.degraded, tracker.degraded_reason or '')
            kw.update(room_tracker=tracker, on_voice=rt.monitor.on_voice_health)
        rt.start()
        return kw

    # ---- 记录库旁路 ----
    def on_late(self, session, type_, payload, turn_id, epoch):
        primary = type_ == 'prompt_primary'
        self.records.record_prompt(session, turn_id, epoch, 'primary' if primary else 'supplement',
                                   payload.get('outline') if primary else payload.get('content'),
                                   source_tag=payload.get('source_tag', ''), level=payload.get('level') or '',
                                   state='late_archived')

    def on_late_metric(self, session, turn_id, name, value):
        if name != 'late_archived':
            return
        v = json.loads(value)
        if v.get('reason') == 'publish_dropped':
            return  # 发布层已按完整内容归档
        reason, epoch = v.get('reason') or '', int(v.get('epoch') or 0)
        self.records.record_prompt(session, turn_id, epoch, 'late_archived',
                                   f"迟到检索结果未展示（{reason}；命中 {v.get('hit_id')}）",
                                   state='late_archived', reason=reason)

    def _record_view(self, session, ev):
        """board 的 candidate 事件挂在“上一题”的 turn_id 上（前端语义）；入记录库时改挂到即将成立的那一题，
        否则会把上一题的状态改成 candidate，首题之前还会产生 id 为空的轮次。规则与 pipeline._metric_turn 相同。"""
        st = self._turn_state.setdefault(session, [0, False])  # [已提交题数, rolled_back 后待重提交]
        t, p = ev['type'], ev.get('payload') or {}
        if t == 'turn_state' and p.get('state') == 'question_committed':
            st[0] += 0 if st[1] else 1
            st[1] = False
        elif t == 'turn_rollback':
            st[1] = True
        elif t == 'candidate':
            if p.get('withdrawn'):
                st[1] = False  # 候选被撤回：回滚留下的重提交上下文失效，下一个 committed 是新题
            elif not st[1]:
                return dict(ev, turn_id=f't{st[0] + 1}')
        return ev

    def on_pull(self, session, pipe):
        pull = self._pulls.get(session)
        if pull is None:
            view = SimpleNamespace(replay=lambda sid, since: [self._record_view(sid, e) for e in self.bus.replay(sid, since)])
            pull = self._pulls[session] = self.records.attach(view, session)
        pull()
        if pipe is not None:
            self._ingest_roles(session, pipe)

    def _ingest_roles(self, session, pipe):
        events = getattr(pipe, 'role_events', [])
        done = self._role_cursor.get((session, id(pipe)), 0)
        if done >= len(events):
            return
        snap = self.bus.snapshot(session)
        with self.records.lock:
            for seg in events[done:]:
                self.records.conn.execute(
                    'INSERT INTO turn_events(session_id,event_seq,turn_id,epoch,type,payload_json,t_ns) VALUES(?,?,?,?,?,?,?)',
                    (session, None, snap['turn_id'], snap['epoch'], 'speaker_segment',
                     json.dumps(seg, ensure_ascii=False), None))
            self.records.conn.commit()
        self._role_cursor[(session, id(pipe))] = len(events)
