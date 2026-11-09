// 记录库纯逻辑：不碰 DOM，便于 node 单测。列表/逐字稿/轮次的数据形状均来自 app/api/records.py 与主库 sessions。

export interface SessionRow {
  id: string; mode?: string; status?: string;
  started_at?: number | null; stopped_at?: number | null; duration_s?: number | null;
  profile_alias?: string | null; roles?: string[] | null; library_name?: string | null;
  turns?: number; prompts?: number; recordings?: number; gaps?: number;
  has_recording?: boolean; deleted?: boolean; deleted_at?: number | null;
}
export interface Segment {
  id: number; role?: string; source?: string; text?: string;
  edited_text?: string | null; orig_text?: string | null; edited_role?: string | null;
  t_start?: number | null; t_end?: number | null; flags?: string | null;
}
export interface Turn {
  id: string; text?: string; kind?: string; level?: string; state?: string;
  t_committed?: number | null; t_visible?: number | null; rolled_back_count?: number;
  parent_id?: string | null;
}
export interface Prompt {
  id?: number; turn_id?: string; block?: string; level?: string; source_tag?: string;
  content_json?: string; source_urls_json?: string; state?: string; display_ack_at?: number | null;
}

export type FilterKey = 'all' | 'stopped' | 'active' | 'pending' | 'deleted';
export const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'stopped', label: '已结束' },
  { key: 'active', label: '进行中' },
  { key: 'pending', label: '待开始' },
  { key: 'deleted', label: '已删除' },
];

export const ANSWERER_NOTE = '默认不转写回答者：本场没有回答者逐字稿，回答者发言仅保留在录音中。';
export const ANSWERER_HINT = '本版默认不转写回答者，回答者发言只保留录音；如出现回答者片段，说明本场已开启回答者转写。';
export const NO_PRIMARY_NOTE = '本轮没有命中标准答案';

export const shortId = (id: string): string => (id || '').slice(0, 8);
export const recordsHref = (sid: string): string => `/records.html?session=${encodeURIComponent(sid)}`;
export const exportName = (sid: string): string => `面试记录-${shortId(sid)}.md`;
export const bundleName = (sid: string): string => `${shortId(sid)}.zip`;

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
export const canonicalStatus = (s: string | undefined): string => (s === 'ended' ? 'stopped' : s || '');

export function modeLabel(mode: string | undefined): string {
  return mode === 'online' ? '线上' : mode === 'offline' ? '线下' : '未知模式';
}

const STATUS: Record<string, string> = { stopped: '已结束', active: '进行中', draft: '草稿', ready: '待开始' };
export const statusLabel = (s: string | undefined): string => STATUS[canonicalStatus(s)] || '未知';

/** 秒 -> mm:ss / h:mm:ss；缺失或负数返回破折号。 */
export function formatDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${String(m).padStart(2, '0')}:${sec}`;
}

export function formatDateTime(ts: number | null | undefined): string {
  if (!isNum(ts)) return '—';
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function filterSessions(list: SessionRow[], key: FilterKey, query = ''): SessionRow[] {
  const q = query.trim().toLowerCase();
  return list.filter((s) => {
    const st = canonicalStatus(s.status);
    if (key === 'deleted' && !s.deleted) return false;
    if (key === 'stopped' && (s.deleted || st !== 'stopped')) return false;
    if (key === 'active' && (s.deleted || st !== 'active')) return false;
    if (key === 'pending' && (s.deleted || !['draft', 'ready'].includes(st))) return false;
    if (!q) return true;
    const hay = [s.id, s.profile_alias || '', (s.roles || []).join(' '), s.library_name || '',
      formatDateTime(s.started_at)].join(' ').toLowerCase();
    return hay.includes(q);
  });
}

export function filterCounts(list: SessionRow[]): Record<FilterKey, number> {
  return {
    all: list.length,
    stopped: filterSessions(list, 'stopped').length,
    active: filterSessions(list, 'active').length,
    pending: filterSessions(list, 'pending').length,
    deleted: filterSessions(list, 'deleted').length,
  };
}

export function levelLabel(level: string | null | undefined): string {
  if (!level) return '—';
  return level === 'personal' || level === '个人' ? '个人' : String(level);
}

export function roleLabel(role: string | null | undefined): string {
  if (role === 'questioner') return '提问方';
  if (role === 'answerer') return '回答者';
  return '未识别';
}

const KIND: Record<string, string> = { question: '主问题', followup: '追问', sub: '子问题' };
export const kindLabel = (kind: string | undefined): string => KIND[kind || ''] || kind || '未分类';

const BLOCK: Record<string, string> = { primary: '主答案', supplement: '补充', status: '状态' };
export const blockLabel = (block: string | undefined): string => BLOCK[block || ''] || block || '—';

const PROMPT_STATE: Record<string, string> = {
  sent: '已发送', acked: '已展示', late_archived: '未展示（迟到归档）',
};
export const promptStateLabel = (state: string | undefined): string =>
  PROMPT_STATE[state || ''] || state || '—';

/** 逐字稿时间线：相对第一条片段（秒）。 */
export function transcriptStart(segs: Segment[]): number | null {
  const starts = segs.map((s) => s.t_start).filter(isNum);
  return starts.length ? Math.min(...starts) : null;
}

export function segmentOffset(seg: Segment, t0: number | null): number | null {
  if (t0 === null || !isNum(seg.t_start)) return null;
  return Math.max(0, seg.t_start - t0);
}

export const segmentText = (seg: Segment): string => seg.edited_text ?? seg.text ?? '';
export const segmentOriginal = (seg: Segment): string => seg.orig_text ?? seg.text ?? '';
export const isEdited = (seg: Segment): boolean => seg.edited_text != null || !!seg.edited_role;
export const segmentFlags = (seg: Segment): string[] => (seg.flags || '').split(',').filter(Boolean);

export function answererNote(segs: Segment[]): string {
  return segs.some((s) => (s.edited_role || s.role) === 'answerer') ? '' : ANSWERER_NOTE;
}

/** 0 条主答案时明确提示“本轮没有命中标准答案”，避免空列表被误认为丢数据。 */
export function noPrimaryNote(prompts: Prompt[]): string {
  return prompts.some((p) => p.block === 'primary') ? '' : NO_PRIMARY_NOTE;
}

export function promptText(p: Prompt): string {
  const raw = p.content_json;
  if (Array.isArray(raw)) return raw.map(String).join('；');
  if (typeof raw !== 'string' || !raw) return '';
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v.map(String).join('；') : typeof v === 'string' ? v : '';
  } catch { return ''; }
}

export function promptUrls(p: Prompt): string[] {
  if (!p.source_urls_json) return [];
  try {
    const v = JSON.parse(p.source_urls_json);
    return Array.isArray(v) ? v.map(String) : [];
  } catch { return []; }
}

export interface TurnGroup { turn: Turn; prompts: Prompt[] }

/** 提示按 turn_id 归到轮次；找不到轮次的提示单独返回，不丢。 */
export function turnGroups(turns: Turn[], prompts: Prompt[]): { groups: TurnGroup[]; orphans: Prompt[] } {
  const by = new Map<string, Prompt[]>();
  for (const p of prompts) {
    const k = p.turn_id || '';
    by.set(k, [...(by.get(k) || []), p]);
  }
  const known = new Set(turns.map((t) => t.id));
  return {
    groups: turns.map((t) => ({ turn: t, prompts: by.get(t.id) || [] })),
    orphans: prompts.filter((p) => !known.has(p.turn_id || '')),
  };
}

export function turnLevel(turn: Turn, prompts: Prompt[]): string {
  return turn.level || prompts.find((p) => p.level)?.level || '';
}

/** 可见延迟 = t_visible - t_committed（秒，保留两位）；缺任一项返回 null。 */
export function visibleDelay(turn: Turn): number | null {
  if (!isNum(turn.t_visible) || !isNum(turn.t_committed)) return null;
  return Math.round(Math.max(0, turn.t_visible - turn.t_committed) * 100) / 100;
}

export const turnRollbacks = (turn: Turn): number =>
  isNum(turn.rolled_back_count) ? turn.rolled_back_count : 0;

/** 进行中的会话不可删除；已在回收目录的也不再提供删除。 */
export function deleteBlocked(s: Pick<SessionRow, 'status' | 'deleted'>): string {
  if (s.deleted) return '该会话已在回收目录';
  if (canonicalStatus(s.status) === 'active') return '进行中的会话不能删除，请先在会话页结束';
  return '';
}

/** 只有已结束且未删除的会话可复盘编辑。 */
export const canEdit = (s: Pick<SessionRow, 'status' | 'deleted'>): boolean =>
  !s.deleted && canonicalStatus(s.status) === 'stopped';
