// 次优库审核台纯逻辑：筛选、进度、批量判定、请求体、键位。无 DOM 依赖，可 node 单测。
export interface ScanMark { kind: string; frag: string }
export interface Entry {
  id: string; question: string; status: string; roles?: string[]; source?: string;
  variants?: string[]; conclusion?: string; points?: string[]; example?: string; caution?: string;
  type?: string; label?: string; scan?: ScanMark[];
}
export type FilterKey = 'all' | 'pending' | 'approved' | 'rejected';
export type Action = 'approve' | 'defer' | 'reject';

export const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' }, { key: 'pending', label: '未审' },
  { key: 'approved', label: '通过' }, { key: 'rejected', label: '驳回' },
];

export const scanMarks = (e: Entry): ScanMark[] => (Array.isArray(e.scan) ? e.scan : []);

export function matchesFilter(e: Entry, key: FilterKey): boolean {
  switch (key) {
    case 'all': return true;
    case 'pending': return e.status === 'pending';
    case 'approved': return e.status === 'approved';
    case 'rejected': return e.status === 'rejected';
  }
}
export function filterEntries(entries: Entry[], key: FilterKey, text = ''): Entry[] {
  const q = text.trim().toLowerCase();
  return entries.filter((e) => matchesFilter(e, key) && (!q || (e.id + e.question).toLowerCase().includes(q)));
}
export function filterCounts(entries: Entry[]): Record<FilterKey, number> {
  const out = {} as Record<FilterKey, number>;
  for (const f of FILTERS) out[f.key] = entries.filter((e) => matchesFilter(e, f.key)).length;
  return out;
}

// 与后端 approve-batch 判据一致：合规扫描命中的条目不可批量
export function batchBlockers(e: Entry): string[] {
  return [...new Set(scanMarks(e).map((m) => '合规扫描：' + m.kind))];
}
export const canBatch = (e: Entry) => e.status === 'pending' && batchBlockers(e).length === 0;
export const batchCandidates = (entries: Entry[]) => entries.filter(canBatch);

export function canApprove(e: Entry, confirmed: boolean): { ok: boolean; why: string } {
  if (scanMarks(e).length) return { ok: false, why: '含合规扫描标记，需先修订种子内容' };
  if (!confirmed) return { ok: false, why: '请先勾选“已核对内容为通用建议”' };
  return { ok: true, why: '' };
}

export function progress(entries: Entry[]) {
  const count = (s: string) => entries.filter((e) => e.status === s).length;
  const approved = count('approved'), rejected = count('rejected'), total = entries.length;
  const reviewed = approved + rejected;
  return {
    approved, rejected, pending: count('pending'), total, reviewed,
    pct: total ? Math.round((reviewed / total) * 100) : 0,
    label: `已审核 ${reviewed}/${total}`,
  };
}

const STATUS: Record<Action, string> = { approve: 'approved', defer: 'pending', reject: 'rejected' };
export const reviewBody = (action: Action, confirmed: boolean) =>
  ({ status: STATUS[action], confirmed: action === 'approve' && confirmed });
export const reviewUrl = (id: string) => `/api/secondary/${encodeURIComponent(id)}/review`;
export const entriesUrl = (role: string) => '/api/secondary/entries' + (role ? `?role=${encodeURIComponent(role)}` : '');
export const progressUrl = () => '/api/secondary/progress';
export const batchUrl = () => '/api/secondary/approve-batch';
export const batchBody = (ids: string[], confirmed: boolean) => ({ ids, confirmed });

export function describeRejected(rejected: { id: string; reasons: string[] }[] = []): string[] {
  return rejected.map((r) => `${r.id}：${(r.reasons ?? []).join('、') || '被拒'}`);
}

export function keyAction(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; target?: unknown }):
  'next' | 'prev' | Action | null {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  const t = (e.target ?? {}) as { tagName?: string; isContentEditable?: boolean };
  const tag = (t.tagName ?? '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT' || t.isContentEditable) return null;
  switch (e.key) {
    case 'j': return 'next'; case 'k': return 'prev';
    case 'a': return 'approve'; case 'h': return 'defer'; case 'r': return 'reject';
    default: return null;
  }
}
export function step(entries: Entry[], current: string, dir: 1 | -1): string {
  if (!entries.length) return '';
  const i = entries.findIndex((e) => e.id === current);
  return entries[Math.min(entries.length - 1, Math.max(0, i < 0 ? 0 : i + dir))].id;
}
