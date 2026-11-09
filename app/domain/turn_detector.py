"""两阶段提问结束判定（M05-3）：candidate / committed，候选撤销、已提交回滚、waiting_continue 标志。

只处理提问方一路：输入 VAD 事件、ASR 转写事件、回答方开口事件与 tick(now_ms)；时间一律由调用方传入（音频毫秒），
不读墙钟，同一输入序列必得同一输出。
回答阶段（M05-5/6，Q2）见 _eval_answer：独立维护 answerer_active / 回答方静音起点；answer_closed 必须同时满足
(a) 回答方静音 >= T_answer_silence 且 (b) 提问方出现完整新问题（新题 commit）；明确改题（终止语+完整新问题）则当前题 interrupted。
只满足 (a) 仅发 answer_end_candidate；只满足 (b)（回答方仍在说）入待答队列。提问方在回答中开口先经 interrupt.classify_interrupt
分类（backchannel 保持 / followup 发 interrupt / unknown 发 interrupt）。关闭时内部调用 close_turn，无需上层再调。
回答阶段的提问方转写（key >= 本次插话起点-slack）分流到 _iv_sent，不污染原题 _sent；无转写的插话保持当前答案、不臆造事件。
被判 unclear 的插话保留该段句子键与区间，迟到 final 到达时只重开一次再按完整文本分类（附和/语气词仍不提交）。
提交发生在回答方已经开口时直接进入 answering，避免下一题被当成上一题的“续说”而合并 turn。

状态（规划 M05）：idle -> listening_question -> question_candidate -> question_committed -> answering。
waiting_continue 是附加标志（含 resume_state），不是独立状态。
TurnEvent.t_ms 是调用方传入的音频时钟（on_vad/on_transcript/tick 的毫秒），不是本地提交时的墙钟；
同一输入序列下 t_ms 恒定，t_ms - t_speech_end_ms 即以音频时间计的判定延迟。
statement（非问题陈述，如"我们公司主要做电商"）与 backchannel / process 一样不成轮：只记入 ignored，不提交；
statement 先按低完整度 candidate 走，静音达 T_long 仍无人接话则 withdrawn 并丢弃（backchannel / process 在 T_silence 即丢弃，无任何事件），
期间回答方开口则按"回答方在回应它"提交。被忽略的发言需覆盖到静音起点才会被丢弃，
未覆盖时保持 waiting_continue 等转写追上；同一句子 key 的迟到转写若仍不成轮则丢弃，若变成问题/请求则重新成轮。
incomplete 文本静音达 T_hard 仍未说完时按 low 提交（reason 含 hard_timeout，计入 hard_timeout_commits）：
这是对规划"不确定时保持"的受控偏离，用有上限的漏判换取不无限沉默。
trigger='vad'（G6）：触发以本地 VAD 静音为主。提问方静音达 T_silence 时取当前可得的最新转写（不要求覆盖到末尾）：
question/request 且完整度非 incomplete 则同刻发 candidate + committed（reason='vad_primary'，事件带 coverage_stale=覆盖是否 < t_end-100，
计入 vad_primary_commits / stale_commits）；尚无任何文本则只发 candidate（reason='no_text'），文本到达的音频时刻再 commit，T_long 仍无文本保持 waiting_continue
（空文本永不 hard_timeout 提交）；incomplete / statement / backchannel / process 沿用 coverage 模式的规则。
commit 后任何迟到 partial/final，先看它的音频结尾 t_audio_end_ms 是否 <= t_speech_end+100 且此后提问方未再开口（ASR 追赶）：
语义要素（类别/完整度/否定词/子问题数）不变则发 text_updated(reason='catchup')，计 catchup_updates，不计过早；任一改变则 rolled_back(reason='semantic_change')，
计 premature_count 与 semantic_rollbacks（用户看到的提示可能是错的，如实记账）。非追赶文本：materially_changed 或无法确认为 refine 一律 rolled_back 并 premature_count+1（计 late_rollbacks）。
提问方 commit 后真的重新开口的回滚计 premature_count 与 resume_rollbacks。恒有 premature_count == resume_rollbacks + semantic_rollbacks + late_rollbacks。
仅当 classify 与完整度不变且与已提交文本的编辑距离 <= 2（且未被 materially_changed 判改义）才发 text_updated（reason='refine'，同 turn，text_version 递增，不计过早）。
句内短停顿造成的 commit-再-回滚如实计入过早；min_words_for_vad_commit（按去标点字数，默认 0 不限制）仅用于未来标定，不足时退回 coverage 规则。
无回答时的轮次滚动（P0-A）：committed 后若回答方从未开口（on_answerer_active(True) 会标记 answered），提问方静音达 T_new_turn
再开口时不立即 close（避免句内停顿切题），而是挂起旧题；下一次提交前，若新文本不是旧题的精修（materially_changed 为 False
且子问题数不变）则并入旧题并 text_updated(reason='refine_after_no_answer')，否则先发 answer_closed(reason='no_answer_timeout')
+ close_turn，再提交新题。硬上限 max_turn_ms / max_turn_chars 达到后，下一次提问方开口强制换轮（不做精修合并）。
默认 trigger='coverage'，行为与未加此模式前完全一致。
时间轴重启（新 generation）须调 reset()；乱序旧 VAD 事件会被丢弃并计入 dropped_events，不静默。
转写事件按鸭子类型读取 text / is_final / text_version / audio_covered_until_ms，句子标识用 t_audio_start_ms（缺省 0）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.interrupt import STOP_RE, classify_interrupt
from app.domain.semantic import classify, completeness, core, materially_changed, semantic_signature
from app.domain.subquestions import split_questions

IDLE, LISTENING, CANDIDATE, COMMITTED, ANSWERING = (
    'idle', 'listening_question', 'question_candidate', 'question_committed', 'answering')
_NOT_TURN = ('backchannel', 'process', 'statement')
_CATCHUP_SLACK_MS = 100  # 迟到转写的音频结尾 <= t_speech_end+此值：描述的是 commit 时已存在的音频（ASR 追赶）
_REFINE_MAX_EDIT = 2  # vad 模式下 text_updated 允许的最大编辑距离（保守）


def _strip_stop(text: str) -> str:
    return STOP_RE.sub('', text).strip(' ，,。.、')


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass(frozen=True)
class TurnEvent:
    type: str  # candidate | withdrawn | committed | rolled_back | waiting_continue | text_updated（仅 vad 模式：commit 后文本补全/校正，语义不变）
    # 回答阶段：answer_end_candidate | answer_closed | interrupted | queued | dequeued | interrupt（kind=followup/unknown）
    turn_text: str
    text_version: int
    t_ms: int
    reason: str
    resume_state: str | None = None  # 仅 waiting_continue 事件
    kind: str = ''  # classify() 结果；type=interrupt 时为打断类别 followup / unknown
    completeness: str = ''  # high / low
    t_speech_end_ms: int | None = None  # 提问方最后语音时刻，t_ms - 它 = 判定延迟
    coverage_stale: bool | None = None  # 仅 vad 模式 candidate / committed：转写覆盖是否 < t_end-100
    sub_questions: tuple = ()  # committed / queued / dequeued：子问题列表，第一个为主问题
    queue_len: int | None = None  # queued / dequeued：事件后的待答队列长度


class TurnDetector:
    def __init__(self, T_silence: int = 200, T_long: int = 700, coverage_slack_ms: int = 100,
                 T_hard: int = 1500, trigger: str = 'coverage', min_words_for_vad_commit: int = 0,
                 T_answer_silence: int = 1500, sim_followup: float = 0.5, iv_merge_ms: int = 3000,
                 T_new_turn: int = 1200, max_turn_ms: int = 30000, max_turn_chars: int = 200):
        if trigger not in ('coverage', 'vad'):
            raise ValueError(f'unknown trigger: {trigger}')
        self.trigger = trigger
        self.T_answer_silence = T_answer_silence  # 回答方静音达此值才可能 answer_closed / 弹队列（Q2，初值 1.5s 待标定）
        self.T_new_turn = T_new_turn  # 未回答的上一题静音达此值后，提问方再开口视为新题（【待标定】）
        self.max_turn_ms = max_turn_ms  # 硬上限：单轮时长（【待标定】）
        self.max_turn_chars = max_turn_chars  # 硬上限：单轮累积字数（【待标定】）
        self.sim_followup = sim_followup  # 打断与当前问题相似度 >= 此值视为追问
        self.iv_merge_ms = iv_merge_ms  # 只有终止语的半句，与此窗口内的下一段插话合并判断
        self.min_words_for_vad_commit = min_words_for_vad_commit  # 0=不限制；按去标点字数
        self.vad_primary_commits = 0
        # 过早口径拆分：premature_count == resume_rollbacks + semantic_rollbacks + late_rollbacks；catchup_updates 不计过早
        self.resume_rollbacks = 0  # 提问方 commit 后真的重新开口
        self.semantic_rollbacks = 0  # vad 追赶文本改了语义要素（否定词 / 子问题数 / 类别 / 完整度）
        self.late_rollbacks = 0  # 其余迟到转写改义（含 coverage 模式的 final 改义、vad 非追赶文本）
        self.catchup_updates = 0  # vad：ASR 追赶且语义要素不变，发 text_updated(catchup)，不计过早
        self.stale_commits = 0  # vad_primary 中覆盖不足的次数
        self.T_silence = T_silence
        self.T_long = T_long
        self.coverage_slack_ms = coverage_slack_ms
        self.T_hard = T_hard  # 静音达此值而文本仍 incomplete：按 low 提交，避免漏判无上限
        self.history: list[TurnEvent] = []  # 只追加
        self.ignored: list[tuple[str, str, int]] = []  # (kind, text, t_ms)：backchannel / 流程性发言，不成轮
        self.candidate_withdrawn_count = 0
        self.premature_count = 0
        self.hard_timeout_commits = 0
        self.dropped_events = 0  # 乱序旧 VAD 事件（时间轴回退而未 reset()）
        self.committed_count = 0
        self.no_answer_closes = 0  # 上一题无人回答、由下一题顶掉而关闭的次数（reason='no_answer_timeout'）
        self.queue: list[dict] = []  # 待答队列：{text, subs, level, ckind, version}
        self.answer_end_candidates = self.closed_count = self.interrupted_count = 0
        self.queued_count = self.dequeued_count = 0
        self.followups = self.backchannels = self.unknowns = 0
        self.waiting_returns = {'resumed': 0, 'complete': 0, 'long_silence_complete': 0, 'answerer_active': 0,
                               'dropped': 0}  # dropped：话轮被忽略/关闭而结束 waiting（非规划三条路径）
        self.phase = IDLE
        self.now = 0
        self.waiting_continue = False
        self.resume_state: str | None = None
        self.answerer_active = False
        self._speaking = False
        self._t_start = -1
        self._t_end: int | None = None
        self._last_vad_t = -1
        self._floor_ms = 0
        self._sent: dict[int, dict] = {}
        self._covered = -1
        self._commit: dict | None = None
        self._last_ver = -1
        self._answerer_pending = False
        self._stale: dict | None = None  # 无人回答的上一题：提问方已开口，等新文本决定“精修”还是换轮
        self._force_new = False  # 硬上限触发的换轮：不再做精修合并
        self._ignored_keys: set[int] = set()  # 已被忽略的句子 key，其迟到转写不得污染下一轮
        self._span: tuple[int, int] | None = None  # 最近一次被忽略话轮的 speech 区间
        self._ans_silent_since: int | None = None  # 回答方静音起点（独立于状态机节点）
        self._ans_cand_sent = False
        self._iv_clear()

    # ---- 对外接口 ----
    def on_vad(self, ev, now_ms: int | None = None) -> list[TurnEvent]:
        start = len(self.history)
        self._advance(ev.t_ms if now_ms is None else now_ms)
        if ev.t_ms < self._last_vad_t:
            self.dropped_events += 1
            return []  # 乱序旧事件
        self._last_vad_t = ev.t_ms
        if ev.kind == 'speech_start' and not self._speaking:
            self._speech_start(ev.t_ms)
        elif ev.kind == 'speech_end' and self._speaking and ev.t_ms >= self._t_start:
            self._speaking = False
            if self._in_answer():
                self._iv_end = ev.t_ms
            else:
                self._t_end = ev.t_ms
            self._evaluate()
        return self.history[start:]

    def on_transcript(self, ev, now_ms: int | None = None) -> list[TurnEvent]:
        start = len(self.history)
        self._advance(now_ms)
        key = getattr(ev, 't_audio_start_ms', 0)
        if self._in_answer():
            self._covered = max(self._covered, ev.audio_covered_until_ms)
            if self._iv_transcript(ev, key):
                return self.history[start:]
        cur = self._sent.get(key)
        if key < self._floor_ms:
            return []  # 属于已关闭的上一轮
        if key in self._ignored_keys:
            if classify(ev.text) in _NOT_TURN:
                return []  # 被忽略句子的迟到版本，仍不成轮
            self._ignored_keys.discard(key)  # 同一句子改写成了问题/请求：重新成轮
        if cur is not None:
            if cur['final']:
                if not (ev.is_final and ev.text_version > cur['version']):
                    return []
            elif not (ev.text_version > cur['version'] or (ev.text_version == cur['version'] and ev.is_final)):
                return []  # 重复 / 乱序旧版本
        self._sent[key] = {'text': ev.text, 'version': ev.text_version, 'final': ev.is_final}
        self._covered = max(self._covered, ev.audio_covered_until_ms)
        if self.phase == IDLE and self._span and self._span[0] - self.coverage_slack_ms <= key <= self._span[1]:
            self.phase = LISTENING  # 属于刚被忽略的那段 speech 的新内容：重新评估
            self._t_start, self._t_end = self._span
        if ev.is_final or self.trigger == 'vad':
            self._check_final_change(ev)
        self._evaluate()
        return self.history[start:]

    def on_answerer_active(self, active: bool, t_ms: int) -> list[TurnEvent]:
        start = len(self.history)
        self._advance(t_ms)
        if active == self.answerer_active:
            return []
        self.answerer_active = active
        self._ans_silent_since = None if active else t_ms
        if not active:
            return []
        if self._stale is not None:  # 上一题终究有人回答：作废“新题”缓冲，回到该题
            self._drop_stale_keys()
        if self._commit is not None:
            self._commit['answered'] = True
        self._ans_cand_sent = False
        if self.phase == COMMITTED:
            self.phase = ANSWERING
        elif self.phase in (LISTENING, CANDIDATE) and not self._speaking and self._t_end is not None:
            self._answerer_pending = True
            self._commit_for_answerer()
        if self._in_answer():
            self._eval_answer()  # 待定的新问题：回答方又开口 -> 入队
        return self.history[start:]

    def tick(self, now_ms: int) -> list[TurnEvent]:
        start = len(self.history)
        self._advance(now_ms)
        self._evaluate()
        return self.history[start:]

    def close_turn(self, floor_ms: int) -> None:
        """回答结束 / 切题后由上层调用：回到 idle，丢弃起点早于 floor_ms 的转写。"""
        self._exit_waiting('dropped')
        self.phase = LISTENING if self._speaking else IDLE  # 提问方正在说下一个问题时不能丢
        self._floor_ms = max(self._floor_ms, floor_ms)
        self._sent = {k: v for k, v in self._sent.items() if k >= self._floor_ms}
        self._span = None
        self._iv_clear()
        self._reset_turn()

    def reset(self) -> None:
        """时间轴重启（新 generation）：回到初始状态，保留 history 与统计计数。"""
        self._exit_waiting('dropped')
        self.phase = IDLE
        self.now = 0
        self.answerer_active = False
        self._ans_silent_since = None
        self._ans_cand_sent = False
        self.queue = []
        self._iv_clear()
        self._speaking = False
        self._t_start = -1
        self._last_vad_t = -1
        self._floor_ms = 0
        self._sent = {}
        self._covered = -1
        self._ignored_keys = set()
        self._span = None
        self._reset_turn()

    # ---- 内部：状态 ----
    def _advance(self, now_ms: int | None) -> None:
        if now_ms is not None:
            self.now = max(self.now, now_ms)

    def _reset_turn(self) -> None:
        self._t_end = None
        self._commit = None
        self._last_ver = -1
        self._answerer_pending = False
        self._stale = None
        self._force_new = False
        self.waiting_continue = False
        self.resume_state = None

    def _answerer_answered(self) -> bool:
        return bool(self._commit and self._commit.get('answered'))

    def _over_cap(self) -> bool:
        """硬上限：单轮时长或累积字数超限（达到后下一次提问方开口强制换轮）。"""
        if self._commit is None:
            return False
        t_end = self._commit.get('t_end')
        return (len(self._turn_text()) >= self.max_turn_chars
                or (t_end is not None and self.now - t_end >= self.max_turn_ms))

    def _can_turn_over(self) -> bool:
        """本次提问方开口是否顶掉上一题：上一题无人回答、静音已超 T_new_turn（或已达硬上限）。"""
        if self._commit is None or self._commit.get('settled'):
            return False
        if self._over_cap():
            self._force_new = True
            return True
        t_end = self._commit.get('t_end')
        return (not self._answerer_answered() and t_end is not None
                and self.now - t_end >= self.T_new_turn)

    def _drop_stale_keys(self) -> None:
        """把“新题”缓冲退回去：只保留上一题自己的句子。"""
        stale, self._stale = self._stale, None
        self._force_new = False
        if stale is None:
            return
        self._ignored_keys.update(k for k in self._sent if k not in stale['keys'])
        self._sent = {k: v for k, v in self._sent.items() if k in stale['keys']}
        self._commit = stale
        self._t_end = stale.get('t_end')
        self.phase = COMMITTED

    def _flush_stale(self) -> bool:
        """提交前的换轮判定：True=已关闭无人回答的上一题（或无需换轮），可以提交；False=按精修并入上一题，不提交。"""
        stale = self._stale
        if stale is None:
            return True
        text = self._turn_text()
        if not text.strip():
            return False
        if not self._force_new and not materially_changed(stale['text'], text)[0] \
                and len(split_questions(stale['text'])) == len(split_questions(text)):
            self._stale = None
            ver = max(self._version(), self._last_ver + 1)
            self._emit('text_updated', text, ver, 'refine_after_no_answer', kind=classify(text),
                       t_speech_end_ms=self._t_end)
            self._commit = {**stale, 'text': text, 'shown': text, 'keys': stale['keys'] | set(self._sent),
                            'version': ver}
            self._last_ver = ver
            self.phase = COMMITTED
            return False
        new_keys = [k for k in self._sent if k not in stale['keys']]
        self._stale, self._force_new = None, False
        self._emit('answer_closed', stale['text'], stale['version'], 'no_answer_timeout')
        self.closed_count += 1
        self.no_answer_closes += 1
        t_end = self._t_end  # 新题自己的语音末尾：close_turn 会清掉，提交时要用
        self.close_turn(min(new_keys) if new_keys else self.now)
        self._t_end = t_end
        return True

    def _turn_text(self) -> str:
        stale = self._stale
        keys = [k for k in sorted(self._sent) if stale is None or k not in stale['keys']]
        return ''.join(self._sent[k]['text'] for k in keys)

    def _version(self) -> int:
        return self._sent[max(self._sent)]['version'] if self._sent else 0

    def _emit(self, type_: str, text: str, version: int, reason: str, **kw) -> None:
        self.history.append(TurnEvent(type_, text, version, self.now, reason, **kw))

    def _speech_start(self, t_ms: int) -> None:
        self._span = None
        if self._in_answer():
            self._iv_begin(t_ms)
            self._answerer_pending = False
            if not self._commit.get('settled'):
                self._commit['resumed'] = True
            self._speaking = True
            self._t_start = t_ms
            return
        if self.phase == IDLE:
            self._reset_turn()
            self.phase = LISTENING
        elif self.phase == CANDIDATE:
            self.candidate_withdrawn_count += 1
            self._emit('withdrawn', self._turn_text(), self._version(), 'speech_resumed', t_speech_end_ms=self._t_end)
            self._exit_waiting('resumed')
            self.phase = LISTENING
        elif self.phase == COMMITTED:
            if self._can_turn_over():
                # 上一题无人回答且已过 T_new_turn（或达硬上限）：先挂起旧题，等新文本判精修后再关闭；
                # 不能在 speech_start 直接 close（句内停顿会被切成新题）。
                self._stale = self._commit
                self._exit_waiting('resumed')
                self.phase = LISTENING
            else:
                self.premature_count += 1
                self.resume_rollbacks += 1
                ver = max(self._version(), self._last_ver + 1)
                self._emit('rolled_back', self._turn_text(), ver, 'speech_resumed_after_commit', t_speech_end_ms=self._t_end)
                self._last_ver = ver
                self.phase = LISTENING
        elif self.phase == LISTENING:
            self._exit_waiting('resumed')
        self._answerer_pending = False
        if self._commit is not None:
            self._commit['resumed'] = True
        self._speaking = True
        self._t_start = t_ms
        if self.phase != ANSWERING:
            self._t_end = None

    def _enter_waiting(self, reason: str) -> None:
        if self.waiting_continue:
            return
        self.waiting_continue = True
        self.resume_state = self.phase
        self._emit('waiting_continue', self._turn_text(), self._version(), 'enter:' + reason,
                   resume_state=self.resume_state, t_speech_end_ms=self._t_end)

    def _exit_waiting(self, path: str) -> None:
        if not self.waiting_continue:
            return
        self.waiting_continue = False
        self.waiting_returns[path] += 1
        self._emit('waiting_continue', self._turn_text(), self._version(), 'exit:' + path,
                   resume_state=self.resume_state, t_speech_end_ms=self._t_end)
        self.resume_state = None

    def _candidate(self, reason: str, level: str, kind: str, **kw) -> None:
        self._emit('candidate', self._turn_text(), self._version(), reason, kind=kind, completeness=level,
                   t_speech_end_ms=self._t_end, **kw)
        self.phase = CANDIDATE

    def _commit_now(self, reason: str, level: str, kind: str, text: str | None = None, sub_questions=None,
                    settled: bool = False, **kw) -> None:
        if not self._flush_stale():
            return
        text = self._turn_text() if text is None else text
        asr_ver = self._version()
        ver = asr_ver if asr_ver > self._last_ver else self._last_ver + 1
        subs = tuple(split_questions(text) if sub_questions is None else sub_questions)
        self._emit('committed', text, ver, reason, kind=kind, completeness=level, t_speech_end_ms=self._t_end,
                   sub_questions=subs, **kw)
        self._commit = {'text': text, 'version': ver, 'keys': set(self._sent), 't_end': self._t_end, 'shown': text,
                        'resumed': False, 'settled': settled, 'answered': settled or self.answerer_active}
        self._last_ver = ver
        self.committed_count += 1
        # 回答方已经在说（上一题的答案还没说完）：这次提交就是新题，直接进入回答阶段；
        # 否则下一次提问方开口会被当成“续说本题”，把新题并进旧 turn。
        self.phase = ANSWERING if self.answerer_active and not settled else COMMITTED

    def _ignore_turn(self, kind: str) -> None:
        if self._stale is not None:  # 新发言不成轮：上一题保持，不关闭
            new_text = ''.join(self._sent[k]['text'] for k in sorted(self._sent) if k not in self._stale['keys'])
            self.ignored.append((kind, new_text, self.now))
            self._ignored_keys.update(k for k in self._sent if k not in self._stale['keys'])
            self._drop_stale_keys()
            return
        self.ignored.append((kind, self._turn_text(), self.now))
        self._exit_waiting('dropped')
        self._ignored_keys.update(self._sent)
        self._span = (self._t_start, self._t_end)
        self._sent = {}
        self.phase = IDLE
        self._reset_turn()

    # ---- 内部：判定 ----
    def _commit_for_answerer(self) -> None:
        text = self._turn_text()
        if not text.strip():
            return  # 等首个转写到达后由 _evaluate 补提交
        kind = classify(text)
        if kind in ('backchannel', 'process'):
            self._ignore_turn(kind)
            return
        level, _ = completeness(text)
        self._exit_waiting('answerer_active')
        if self.phase == LISTENING:
            self._candidate('answerer_active', level, kind)
        self._commit_now('answerer_active', level, kind)
        self.phase = ANSWERING
        self._answerer_pending = False

    def _check_final_change(self, ev) -> None:
        if self.phase not in (COMMITTED, ANSWERING) or self._commit is None or self._commit.get('settled'):
            return
        c = self._commit
        # 只看该轮自己的句子：晚于该轮语音末尾的新句子（提问方无关的发言）不参与改义判断
        text = ''.join(self._sent[k]['text'] for k in sorted(self._sent) if k in c['keys'] or k <= c['t_end'])
        if self.trigger == 'vad' and not c['resumed'] and ev.t_audio_end_ms <= c['t_end'] + _CATCHUP_SLACK_MS:
            self._catchup(c, text)
            return
        changed, why = materially_changed(c['text'], text)
        if self.trigger == 'vad' and not changed:
            self._refine_or_roll_back(c, text, why)
            return
        if not changed:
            return
        self._rolled_back(c, text, 'final_changed:' + why, 'late_rollbacks')

    def _catchup(self, c: dict, text: str) -> None:
        if semantic_signature(c['text']) != semantic_signature(text):
            self._rolled_back(c, text, 'semantic_change', 'semantic_rollbacks')
            return
        if core(text) == core(c['shown']):
            return
        ver = max(self._version(), self._last_ver + 1)
        self._emit('text_updated', text, ver, 'catchup', kind=classify(text), t_speech_end_ms=self._t_end)
        self._commit = {**c, 'shown': text, 'version': ver}
        self._last_ver = ver
        self.catchup_updates += 1

    def _refine_or_roll_back(self, c: dict, text: str, why: str) -> None:
        if why == 'same' or core(text) == core(c['shown']):
            return
        if classify(text) == classify(c['text']) and completeness(text)[0] == completeness(c['text'])[0] \
                and _edit_distance(core(c['text']), core(text)) <= _REFINE_MAX_EDIT:
            ver = max(self._version(), self._last_ver + 1)
            self._emit('text_updated', text, ver, 'refine', kind=classify(text), t_speech_end_ms=self._t_end)
            self._commit = {**c, 'shown': text, 'version': ver}
            self._last_ver = ver
            return
        self._rolled_back(c, text, 'final_changed:not_refine', 'late_rollbacks')

    def _rolled_back(self, c: dict, text: str, reason: str, counter: str) -> None:
        ver = max(self._version(), self._last_ver + 1)
        self.premature_count += 1
        setattr(self, counter, getattr(self, counter) + 1)
        self._emit('rolled_back', text, ver, reason, kind=classify(text), t_speech_end_ms=self._t_end)
        self._commit = {**c, 'text': text, 'version': ver, 'shown': text}
        self._last_ver = ver

    def _evaluate(self) -> None:
        if self._in_answer():
            self._eval_answer()
            return
        if self._answerer_pending and self.phase in (LISTENING, CANDIDATE) and not self._speaking:
            self._commit_for_answerer()
            return
        if self.phase == LISTENING:
            self._eval_listening()
        if self.phase == CANDIDATE:
            self._eval_candidate()

    def _silence(self) -> int | None:
        if self._speaking or self._t_end is None:
            return None
        return self.now - self._t_end

    def _covered_ok(self) -> bool:
        return self._covered >= self._t_end - self.coverage_slack_ms

    def _eval_listening(self) -> None:
        sil = self._silence()
        text = self._turn_text()
        if sil is None or sil < self.T_silence:
            return
        if not text.strip():
            if self.trigger == 'vad':
                self._candidate('no_text', '', '', coverage_stale=True)  # 尚无转写：只发候选，文本到达再 commit
            return
        covered = self._covered_ok()
        long_ = sil >= self.T_long
        kind = classify(text)
        if kind in _NOT_TURN and (kind != 'statement' or long_):  # statement 静音不足 T_long 时先按候选走，留出回答方开口窗口
            if covered:
                self._ignore_turn(kind)
            else:
                self._enter_waiting('coverage_insufficient')  # 文本可能是过期 partial，不能据此丢弃
            return
        level, reasons = completeness(text)
        if level == 'incomplete':
            if sil >= self.T_hard:
                self._hard_timeout_commit(kind)
                return
            self._enter_waiting('incomplete:' + ','.join(reasons))
            return
        if self._vad_ready(text, kind):
            self._vad_commit(level, kind, covered, long_)
            return
        if not covered:
            if not long_:
                self._enter_waiting('coverage_insufficient')
                return
            level = 'low'  # 覆盖不足：降为低完整度，仅凭静音达 T_long 提交
        self._exit_waiting('long_silence_complete' if long_ else 'complete')
        self._candidate('silence>=T_silence' + ('' if covered else ';coverage_insufficient'), level, kind)
        if level == 'high':  # 能到这里的 high 必已覆盖（未覆盖已降为 low）
            self._commit_now('high_completeness', level, kind)
        elif long_:
            self._commit_now('long_silence' + ('' if covered else ';coverage_insufficient'), level, kind)

    def _vad_ready(self, text: str, kind: str) -> bool:
        return self.trigger == 'vad' and kind in ('question', 'request') and len(core(text)) >= self.min_words_for_vad_commit

    def _vad_commit(self, level: str, kind: str, covered: bool, long_: bool) -> None:
        self._exit_waiting('long_silence_complete' if long_ else 'complete')
        if self.phase == LISTENING:
            self._candidate('vad_primary', level, kind, coverage_stale=not covered)
        self._commit_now('vad_primary', level, kind, coverage_stale=not covered)
        self.vad_primary_commits += 1
        self.stale_commits += not covered

    def _hard_timeout_commit(self, kind: str) -> None:
        self._exit_waiting('long_silence_complete')
        if self.phase == LISTENING:
            self._candidate('hard_timeout', 'low', kind)
        self._commit_now('hard_timeout', 'low', kind)
        self.hard_timeout_commits += 1

    def _eval_candidate(self) -> None:
        sil = self.now - self._t_end  # CANDIDATE 下提问方必已静音
        text = self._turn_text()
        if self.trigger == 'vad' and not text.strip():  # no_text 候选：等文本；T_long 仍无则保持 waiting，空文本不提交
            if sil >= self.T_long:
                self._enter_waiting('no_text')
            return
        level, reasons = completeness(text)
        kind = classify(text)
        if kind in _NOT_TURN and (kind != 'statement' or sil >= self.T_long):  # candidate 之后 statement 无人接话 / 改写成不成轮的发言
            if self.trigger == 'vad' and not self._covered_ok():
                self._enter_waiting('coverage_insufficient')  # no_text 候选后到的文本可能是过期 partial，不能据此丢弃
                return
            self.candidate_withdrawn_count += 1
            self._emit('withdrawn', text, self._version(), 'reclassified:' + kind, t_speech_end_ms=self._t_end)
            self._ignore_turn(kind)
            return
        if level == 'incomplete':
            if sil >= self.T_hard:
                self._hard_timeout_commit(kind)
                return
            self._enter_waiting('incomplete:' + ','.join(reasons))
            return
        long_ = sil >= self.T_long
        if self._vad_ready(text, kind):
            self._vad_commit(level, kind, self._covered_ok(), long_)
            return
        # coverage 模式：candidate 必已覆盖末尾，覆盖只增不减，故此处 high 可信
        self._exit_waiting('long_silence_complete' if long_ else 'complete')
        if level == 'high' or long_:
            self._commit_now('high_completeness' if level == 'high' else 'long_silence', level, kind)

    # ---- 回答阶段（M05-5/6，Q2）----
    def _in_answer(self) -> bool:
        """回答中，或待答队列弹出后尚未开口的已定题（settled）：提问方的新发言按插话处理，不回滚该题。"""
        return self.phase == ANSWERING or (self.phase == COMMITTED and self._commit is not None
                                           and self._commit.get('settled', False))

    def _iv_clear(self) -> None:
        self._iv_sent: dict[int, dict] = {}
        self._iv_t0: int | None = None
        self._iv_end: int | None = None
        self._iv_seen: tuple | None = None
        self._iv_pending: dict | None = None  # 已判为新题、等回答方静音达阈值或又开口
        self._consumed: set[int] = set()  # 已处理完的插话句子 key，其迟到转写忽略
        # 被判 unclear 的插话：本地 ASR final 可能晚于打断窗口才到，留一次重开机会
        self._iv_reopen: dict | None = None

    def _iv_drop(self, consumed: bool = True, keep_open: bool = False) -> None:
        if consumed:
            self._consumed.update(self._iv_sent)
        if not keep_open:
            self._iv_reopen = None
        self._iv_sent, self._iv_t0, self._iv_end, self._iv_seen, self._iv_pending = {}, None, None, None, None

    def _iv_begin(self, t_ms: int) -> None:
        self._iv_reopen = None
        if self._iv_t0 is not None and self._iv_end is not None and t_ms - self._iv_end > self.iv_merge_ms:
            self._iv_drop()  # 留存的终止语半句已过期
        if self._iv_t0 is None:
            self._iv_t0 = t_ms
        self._iv_end = None
        self._iv_pending = None

    def _iv_transcript(self, ev, key: int) -> bool:
        """回答阶段的转写分流；返回 True 表示已处理（不再走原题路径）。

        句子键 t_audio_start_ms 是 ASR 的“片段起点”，本地 sherpa 在上一题 final 之后要等到下一次
        endpoint 才推进它，可能比本次插话的真实开口早数秒。只看 key 会把回答阶段的新题当旧句子丢掉；
        因此 key 偏早时再用该转写的音频结束时刻判断它是否落在本次插话区间内。
        """
        if key in self._consumed:
            return True
        rv = self._iv_reopen
        if ev.is_final and rv is not None and key in rv['keys']:
            # 这段插话此前只拿到半句被判 unclear；final 补全后重开一次，再按完整文本分类
            self._iv_reopen = None
            self._iv_sent, self._iv_t0, self._iv_end = dict(rv['sent']), rv['t0'], rv['end']
            self._iv_seen = rv.get('seen')
        end_ms = getattr(ev, 't_audio_end_ms', None) or 0
        if self._iv_t0 is not None and (key >= self._iv_t0 - self.coverage_slack_ms
                                        or end_ms >= self._iv_t0):
            cur = self._iv_sent.get(key)
            if cur is None or (ev.text_version, ev.is_final) > (cur['version'], cur['final']):
                self._iv_sent[key] = {'text': ev.text, 'version': ev.text_version, 'final': ev.is_final}
            self._evaluate()
            return True
        return bool(self._commit and self._commit.get('settled'))

    def _answer_text(self) -> str:
        return self._commit['text'] if self._commit else self._turn_text()

    def _answer_silent(self) -> bool:
        return self._ans_silent_since is not None and self.now - self._ans_silent_since >= self.T_answer_silence

    def _eval_answer(self) -> None:
        self._eval_iv()
        p = self._iv_pending
        if p is not None:
            if self.answerer_active:
                self._enqueue(p)
            elif self._answer_silent():
                self._switch('answer_closed', 'new_question', p)
            return
        if self.phase != ANSWERING or self._speaking or not self._answer_silent():
            return
        if self.queue:
            self._pop_queue()
        elif not self._ans_cand_sent:
            self._ans_cand_sent = True
            self.answer_end_candidates += 1
            self._emit('answer_end_candidate', self._answer_text(), self._commit['version'], 'answerer_silence>=T_answer_silence')

    def _eval_iv(self) -> None:
        if self._iv_t0 is None or self._speaking or self._iv_end is None or self._iv_pending is not None:
            return
        text = ''.join(self._iv_sent[k]['text'] for k in sorted(self._iv_sent))
        sil = self.now - self._iv_end
        if (text, self._iv_end) == self._iv_seen or sil < self.T_silence:
            return
        if not text.strip():  # 无转写（ASR 不可用 / 未到）：无法判断，保持当前答案
            if sil >= self.T_hard:
                self._iv_drop()
            return
        if self.trigger == 'coverage' and self._covered < self._iv_end - self.coverage_slack_ms and sil < self.T_hard:
            return
        r = classify_interrupt(text, self._iv_end - self._iv_t0, self._answer_text(), sim_followup=self.sim_followup)
        if r.kind != 'backchannel' and r.level != 'high' and sil < self.T_long:
            return
        if r.level == 'incomplete' and sil < self.T_hard:
            return
        self._iv_seen = (text, self._iv_end)
        ver = max(v['version'] for v in self._iv_sent.values())
        if r.kind == 'backchannel':
            self.backchannels += 1
            self._iv_drop()
        elif r.kind in ('followup', 'unknown'):
            if r.kind == 'followup':
                self.followups += 1
            else:
                self.unknowns += 1
            self._emit('interrupt', text, ver, r.reason, kind=r.kind)
            if not r.stop:
                if r.kind == 'unknown':
                    # 只有半句（可能还是“陈述”样子的截断）：保留句子键与区间，迟到的 final 还能重开一次；
                    # 重开后仍按完整文本分类，附和/语气词/陈述依旧不提交。
                    span = {'keys': set(self._iv_sent), 'sent': dict(self._iv_sent), 'seen': self._iv_seen,
                            't0': self._iv_t0, 'end': self._iv_end}
                    self._iv_drop(consumed=False, keep_open=True)
                    self._iv_reopen = span
                else:
                    self._iv_drop()
        else:
            level = r.level if r.level in ('high', 'low') else 'low'
            question = _strip_stop(text)
            p = {'kind': r.kind, 'text': text, 'subs': split_questions(question), 'level': level,
                 'ckind': classify(question), 'sent': dict(self._iv_sent), 't_start': self._iv_t0,
                 't_end': self._iv_end, 'version': ver}
            if r.kind == 'switch':
                self._switch('interrupted', 'explicit_switch', p)
            elif self.phase != ANSWERING or self.answerer_active:
                self._enqueue(p)
            elif self._answer_silent():
                self._switch('answer_closed', 'new_question', p)
            else:
                self._iv_pending = p

    def _enqueue(self, p: dict) -> None:
        self.queue.append({k: p[k] for k in ('text', 'subs', 'level', 'ckind', 'version')})
        self.queued_count += 1
        self._emit('queued', p['text'], p['version'], 'answerer_active' if self.answerer_active else 'answer_not_started',
                   sub_questions=tuple(p['subs']), queue_len=len(self.queue))
        self._iv_drop()

    def _switch(self, type_: str, reason: str, p: dict) -> None:
        """关闭 / 打断当前题，并把插话里的新题作为新一轮 committed。"""
        self._emit(type_, self._answer_text(), self._commit['version'], reason)
        if type_ == 'interrupted':
            self.interrupted_count += 1
        else:
            self.closed_count += 1
        self.close_turn(min(p['sent']) if p['sent'] else p['t_start'])
        self._sent, self._t_start, self._t_end = dict(p['sent']), p['t_start'], p['t_end']
        self._commit_now(reason, p['level'], p['ckind'], sub_questions=p['subs'])

    def _pop_queue(self) -> None:
        item = self.queue.pop(0)
        self._emit('answer_closed', self._answer_text(), self._commit['version'], 'queue_pop')
        self.closed_count += 1
        self.dequeued_count += 1
        self._floor_ms = max(self._floor_ms, max(self._sent) + 1 if self._sent else 0)
        self._sent = {}
        self.phase = IDLE
        self._reset_turn()
        self._emit('dequeued', item['text'], item['version'], 'answer_silence', sub_questions=tuple(item['subs']),
                   queue_len=len(self.queue))
        self._commit_now('dequeued', item['level'], item['ckind'], text=item['text'], sub_questions=item['subs'], settled=True)
        self._ans_cand_sent = False
