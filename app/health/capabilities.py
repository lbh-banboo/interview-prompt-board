"""能力状态模型与降级矩阵（M13-2）。

按能力降级，不设统一开关：每个能力有独立的 ok/degraded/down 状态，某能力降级只影响依赖它的功能。
时钟由调用方注入（秒，单调）；本模块不读墙钟。
"""
OK, DEGRADED, DOWN = 'ok', 'degraded', 'down'
_SEVERITY = {OK: 0, DEGRADED: 1, DOWN: 2}

CAPABILITIES = ('capture', 'asr', 'role', 'retrieval', 'llm', 'search', 'voice', 'disk')

# 故障 -> (受影响能力及状态, 系统行为, 提示文案, 提示时限秒)。文案取自规划 M13 降级矩阵。
# llm_down / search_down 对应矩阵“模型/搜索不可用”一行；disk_low 的状态由调用方按 WARN/告警 级别指定。
DEGRADE_MATRIX = {
    'llm_down': dict(caps={'llm': DOWN}, behavior='只用本地题库', message='外部补充不可用', deadline_s=12.0),
    'search_down': dict(caps={'search': DOWN}, behavior='只用本地题库', message='外部补充不可用', deadline_s=12.0),
    'asr_disconnected': dict(caps={'asr': DOWN}, behavior='继续录音，保留当前答案，不识别新题',
                             message='实时转写不可用', deadline_s=3.0),
    'asr_stalled': dict(caps={'asr': DEGRADED}, behavior='继续录音，保留当前答案，不识别新题',
                        message='实时转写停滞', deadline_s=3.0),
    'source_lost': dict(caps={'capture': DEGRADED, 'role': DEGRADED}, behavior='对该音源相关角色停止可靠判断',
                        message='会议音频已断开', deadline_s=3.0),
    'uncertain': dict(caps={'role': DEGRADED}, behavior='保留当前答案', message='不确定，继续判断', deadline_s=3.0),
    'voice_unavailable': dict(caps={'voice': DOWN, 'role': DEGRADED}, behavior='线下角色降级推断',
                              message='角色识别不确定', deadline_s=3.0),
    'disk_low': dict(caps={'disk': DEGRADED}, behavior='告警，必要时停录', message='磁盘空间不足', deadline_s=12.0),
}

SOURCE_LOST_MESSAGES = {'meeting': '会议音频已断开', 'mic': '麦克风音频已断开', 'room': '现场音频已断开'}


def offline_capabilities(local_asr_verified=False):
    """外网断开时仍可用的功能。云 ASR 断网后无法识别新题，只有真实验证过本地 ASR 才可宣称可识别。"""
    return dict(recording=True, local_library_hint=True, new_question_recognition=bool(local_asr_verified))


class CapabilityBoard:
    def __init__(self, clock, on_change=None):
        self.clock = clock
        self.on_change = on_change  # 能力状态变化时以 payload() 回调，由主线转成 health 事件
        self._active = {}  # (fault, source) -> dict(since, caps, message, reason)
        self._states = {c: dict(state=OK, reason='', since=clock()) for c in CAPABILITIES}
        self.history = []  # 已结束的故障时段
        self._notices = []

    def raise_fault(self, fault, source=None, reason='', state=None):
        """登记/更新故障。state 仅用于覆盖主能力的严重度（如磁盘 down）。"""
        spec = DEGRADE_MATRIX[fault]
        caps = dict(spec['caps'])
        if state is not None:
            caps = {c: state if i == 0 else s for i, (c, s) in enumerate(caps.items())}
        message = SOURCE_LOST_MESSAGES.get(source, spec['message']) if fault == 'source_lost' else spec['message']
        key = (fault, source)
        cur = self._active.get(key)
        since = cur['since'] if cur else self.clock()
        self._active[key] = dict(since=since, caps=caps, message=message, reason=reason)
        self._refresh()

    def clear_fault(self, fault, source=None):
        rec = self._active.pop((fault, source), None)
        if rec is None:
            return
        self.history.append(dict(fault=fault, source=source, start=rec['since'], end=self.clock(),
                                 reason=rec['reason']))
        self._refresh()

    def is_active(self, fault, source=None):
        return (fault, source) in self._active

    def state(self, cap):
        return dict(self._states[cap])

    def notices(self):
        """当前生效的提示文案（去重，保持登记顺序）。"""
        seen = []
        for rec in self._active.values():
            if rec['message'] not in seen:
                seen.append(rec['message'])
        return seen

    def payload(self):
        """health 事件 payload：各能力 {state, reason, since}，另附当前提示文案。"""
        out = {c: dict(s) for c, s in self._states.items()}
        out['notices'] = self.notices()
        return out

    def _refresh(self):
        new = {}
        for c in CAPABILITIES:
            worst, reason, since = OK, '', None
            for rec in self._active.values():
                st = rec['caps'].get(c)
                if st and _SEVERITY[st] > _SEVERITY[worst]:
                    worst, reason, since = st, rec['reason'] or rec['message'], rec['since']
            old = self._states[c]
            if worst == old['state'] and (worst == OK or reason == old['reason']):
                new[c] = old
            else:
                new[c] = dict(state=worst, reason=reason, since=since if since is not None else self.clock())
        notices = self.notices()
        changed = new != self._states or notices != self._notices
        self._states, self._notices = new, notices
        if changed and self.on_change:
            self.on_change(self.payload())
