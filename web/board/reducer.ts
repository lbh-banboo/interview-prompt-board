// 提示板状态 reducer：纯函数，无 DOM。规则见规划 §4.3 / §7.2 / M11。
export interface Outline { conclusion: string; points: string[]; examples?: string; cautions?: string }
export interface BoardEvent {
  type: string; session: string; turn_id: string; epoch: number; event_seq: number;
  text_version: number; t_server: number; payload: Record<string, any>;
}
export interface Primary { prompt_id: string; level: string; outline: Outline; source_tag: string; flagged?: boolean; pending_marks?: number }
export interface VadCandidate { candidate_id: string; text: string; level?: string; source_tag?: string; outline?: Outline }
export interface Supplement { prompt_id?: string; source_tag: string; content: string; url?: string | null; elapsed_ms?: number | null }
export interface Question { text: string; text_version: number; kind: string; corrected: boolean; sub_questions: string[]; revisions: { text: string; from_version: number | null; to_version: number; reason: string }[] }
export interface Archived { turn_id: string; epoch: number; question: string; primary: Primary | null; supplements: Supplement[]; outcome: 'interrupted' | 'closed' | 'revised' }
export interface Health { state: string; reason?: string; since?: number | null }
export interface BoardState {
  session: string; turn_id: string; epoch: number; event_seq: number; status: string;
  waiting_continue: boolean; answerer_active: boolean; interrupt: string | null;
  question: Question | null; candidate: { text: string; hits: any[] } | null; vadCandidate: VadCandidate | null;
  primary: Primary | null; primaryStale: boolean; promptStatus: string; promptStatusId: string;
  supplements: Supplement[]; queue: string[];
  sources: Record<string, { state: string; level?: number | null }>; health: Record<string, Health>; notices: string[];
  answerEnd: boolean; closeReason: string;
  mode: string; ended: boolean; needResync: boolean; dropped: number; lastDrop: string; history: Archived[];
}

const PROMPT_TYPES = ['prompt_primary', 'prompt_status', 'prompt_supplement', 'prompt_candidate'];
const HISTORY_MAX = 20;

export function initialState(session: string): BoardState {
  return {
    session, turn_id: '', epoch: 0, event_seq: 0, status: 'idle', waiting_continue: false, answerer_active: false,
    interrupt: null, question: null, candidate: null, vadCandidate: null, primary: null, primaryStale: false, promptStatus: '', promptStatusId: '',
    supplements: [], queue: [], sources: {}, health: {}, notices: [], answerEnd: false, closeReason: '', mode: '', ended: false, needResync: false, dropped: 0,
    lastDrop: '', history: [],
  };
}

/** health 载荷拆成各能力 {state,reason,since} 与 notices；兼容旧结构 {asr:{state,reason}}（无 notices 时保持原有提示）。 */
export function splitHealth(prev: Record<string, Health>, p: Record<string, any>, prevNotices: string[] = []): { health: Record<string, Health>; notices: string[] } {
  const health = { ...prev };
  for (const [k, v] of Object.entries(p)) {
    if (k !== 'notices' && v && typeof v === 'object' && typeof v.state === 'string') health[k] = v as Health;
  }
  return { health, notices: Array.isArray(p.notices) ? p.notices.map(String) : prevNotices };
}

/** 快照恢复：校验由调用方完成（session_id 必须一致）；快照之前的事件不再重放，event_seq 取快照值。 */
export function fromSnapshot(snap: Record<string, any>): BoardState {
  const s = initialState(String(snap.session_id));
  const q = snap.question;
  return {
    ...s, turn_id: snap.turn_id ?? '', epoch: snap.epoch ?? 0, event_seq: snap.event_seq ?? 0,
    status: snap.status ?? 'idle', waiting_continue: !!snap.waiting_continue, answerer_active: !!snap.answerer_active,
    question: q ? { text: q.text, text_version: q.text_version, kind: q.kind ?? '', corrected: !!q.corrected, sub_questions: q.sub_questions ?? [], revisions: [] } : null,
    candidate: snap.candidate ? { text: snap.candidate.text ?? '', hits: snap.candidate.hits ?? [] } : null,
    vadCandidate: snap.pending_candidate ?? snap.vad_candidate ?? null,
    primary: snap.primary ?? null, promptStatus: snap.prompt_status ?? '', promptStatusId: snap.prompt_status_id ?? '',
    supplements: snap.supplements ?? [],
    queue: snap.queue ?? [], sources: snap.sources ?? {}, ...splitHealth(s.health, snap.health ?? {}), mode: snap.mode ?? '',
    ended: snap.status === 'ended',
  };
}

function drop(state: BoardState, reason: string, advance: boolean, ev: BoardEvent, resync = false): BoardState {
  return { ...state, event_seq: advance ? ev.event_seq : state.event_seq, dropped: state.dropped + 1, lastDrop: reason,
    needResync: state.needResync || resync };
}

function archive(state: BoardState, outcome: Archived['outcome']): Archived[] {
  if (!state.turn_id || (!state.primary && !state.supplements.length && !state.question)) return state.history;
  const item: Archived = { turn_id: state.turn_id, epoch: state.epoch, question: state.question?.text ?? '',
    primary: state.primary, supplements: state.supplements, outcome };
  return [...state.history, item].slice(-HISTORY_MAX);
}

export function reduce(state: BoardState, ev: BoardEvent): BoardState {
  if (ev.session !== state.session) return drop(state, 'session_mismatch', false, ev);
  if (ev.event_seq <= state.event_seq) return drop(state, 'stale_seq', false, ev);
  if (ev.event_seq > state.event_seq + 1) return drop(state, 'seq_gap', false, ev, true);
  const p = ev.payload || {};
  const commit = ev.type === 'turn_state' && p.state === 'question_committed';
  if (commit) {
    if (ev.epoch <= state.epoch) return drop(state, 'stale_epoch', true, ev);
  } else if (ev.epoch < state.epoch) {
    return drop(state, 'stale_epoch', true, ev);
  } else if (ev.epoch > state.epoch) {
    return drop(state, 'future_epoch', true, ev, true);
  } else if (ev.turn_id !== state.turn_id) {
    return drop(state, 'turn_mismatch', true, ev);
  }
  if (PROMPT_TYPES.includes(ev.type) && (state.status === 'closed' || state.status === 'ended' || state.status === 'interrupted')) {
    return drop(state, 'turn_closed', true, ev);
  }
  const next: BoardState = { ...state, event_seq: ev.event_seq };
  switch (ev.type) {
    case 'turn_state':
      if (commit) {
        const sameTurn = ev.turn_id === state.turn_id;
        const outcome = sameTurn ? 'revised' : state.status === 'closed' ? 'closed' : 'interrupted';
        next.history = archive(state, outcome);
        next.turn_id = ev.turn_id; next.epoch = ev.epoch;
        next.primary = null; next.primaryStale = false; next.supplements = []; next.candidate = null;
        next.vadCandidate = null;
        next.promptStatus = ''; next.promptStatusId = ''; next.interrupt = null; next.answerEnd = false; next.closeReason = '';
        if (!sameTurn) next.question = null;
      } else {
        next.interrupt = null;
      }
      next.answerEnd = p.state === 'answering' && isAnswerEndReason(p.reason);
      next.closeReason = p.state === 'closed' ? String(p.reason ?? '') : '';
      next.status = p.state; next.waiting_continue = !!p.waiting_continue; next.answerer_active = !!p.answerer_active;
      next.ended = p.state === 'ended' || state.ended;
      return next;
    case 'transcript':
      if (p.role === 'questioner' && p.is_final) {
        next.question = { text: p.text, text_version: ev.text_version, kind: p.kind ?? '',
          corrected: !!state.question?.corrected, sub_questions: Array.isArray(p.sub_questions) ? p.sub_questions.map(String) : [],
          revisions: state.question?.revisions ?? [] };
      } else if (p.role === 'questioner' && state.question && state.status !== 'answering' && !turnClosed(state)
          && ev.text_version > state.question.text_version) {
        // text_updated：回答前文本补全，只换问题文本与版本；主答案与“已修正”徽标不动
        next.question = { ...state.question, text: p.text, text_version: ev.text_version };
      }
      return next;
    case 'turn_rollback':
      next.primaryStale = !!state.primary;
      if (state.question) {
        next.question = { ...state.question, corrected: true, revisions: [...state.question.revisions,
          { text: state.question.text, from_version: p.from_version ?? null, to_version: p.to_version, reason: p.reason ?? '' }] };
      }
      return next;
    case 'candidate':
      next.candidate = p.withdrawn ? null : { text: p.text ?? '', hits: p.hits ?? [] };
      return next;
    case 'prompt_candidate':
      // VAD 提前候选：可能没问完；Enter 采用（candidate_accept），新题判定后清空
      next.vadCandidate = p.withdrawn || !p.candidate_id ? null : {
        candidate_id: String(p.candidate_id), text: String(p.text ?? ''),
        ...(p.level ? { level: String(p.level) } : {}),
        ...(p.source_tag ? { source_tag: String(p.source_tag) } : {}),
        ...(p.outline ? { outline: p.outline as Outline } : {}),
      };
      return next;
    case 'prompt_primary':
      // 回答前的修正刷新（corrected）允许替换主答案；回答开始后一律锁定
      if (state.primary && !(p.corrected && state.status !== 'answering')) return drop(state, 'primary_locked', true, ev);
      next.primary = { prompt_id: p.prompt_id, level: p.level, outline: p.outline, source_tag: p.source_tag,
        flagged: !!p.flagged, pending_marks: Number(p.pending_marks ?? 0) };
      next.vadCandidate = null;  // 候选已确认升级为主答案
      next.promptStatus = '';
      next.promptStatusId = '';
      return next;
    case 'prompt_status':
      next.promptStatus = p.text ?? '';
      next.promptStatusId = p.prompt_id ?? '';
      return next;
    case 'prompt_supplement':
      next.supplements = [...state.supplements, { prompt_id: p.prompt_id, source_tag: p.source_tag, content: p.content, url: p.url, elapsed_ms: p.elapsed_ms }];
      return next;
    case 'queue_update':
      next.queue = [...(p.items ?? [])];
      return next;
    case 'interrupt':
      if (p.kind === 'backchannel') return next;  // 附和不打扰
      next.interrupt = p.kind === 'switch' || p.kind === 'followup' ? p.kind : 'unknown';
      if (p.kind === 'switch') next.status = 'interrupted';
      return next;
    case 'source_state':
      next.sources = { ...state.sources, [p.source]: { state: p.state, level: p.level } };
      return next;
    case 'health':
      Object.assign(next, splitHealth(state.health, p, state.notices));
      return next;
    default:
      return next;
  }
}

const INTERRUPT_LABELS: Record<string, string> = { switch: '已切换到新问题', followup: '追问：已追加到补充区', unknown: '检测到插话，继续判断' };
const LEVEL_LABELS: Record<string, string> = { L1: 'L1 我的口径', L1p: 'L1p 部分命中', L1m: 'L1 多条命中', L2: 'L2 通用建议', L3: 'L3 外部参考', personal: '需本人补充' };
export const levelLabel = (level: string): string => LEVEL_LABELS[level] ?? level;
/** 后端实际 reason 为 answerer_silence>=T_answer_silence；同时兼容 answer_end_candidate。 */
export const isAnswerEndReason = (reason: unknown): boolean => typeof reason === 'string' && (reason === 'answer_end_candidate' || reason.startsWith('answerer_silence'));
export const clip = (text: string, max = 40): string => (text.length > max ? text.slice(0, max - 1) + '…' : text);
export const queueLabel = (i: number, text: string): string => `${i + 1}. ${clip(text)}`;

export function statusLabel(s: BoardState): string {
  if (s.ended) return '已结束';
  if (s.interrupt || s.status === 'interrupted') return INTERRUPT_LABELS[s.interrupt ?? 'switch'];
  if (s.waiting_continue) return '等待继续';
  if (s.answerEnd) return '回答可能已结束（等待面试官提问）';
  switch (s.status) {
    case 'answering': return '回答中';
    case 'question_committed': return '已判定';
    case 'question_candidate': return '候选';
    case 'closed': return '本题已结束';
    default: return '听题中';
  }
}

export function turnClosed(s: BoardState): boolean {
  return s.ended || s.status === 'closed' || s.status === 'interrupted';
}
