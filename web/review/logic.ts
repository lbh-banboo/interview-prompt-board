// 审核工作台纯逻辑：筛选、批量判定、进度、编辑/重导入请求体、键位。无 DOM 依赖，可 node 单测。
export type Flag = 'pending_verify' | 'conflict' | 'suspected_fabrication' | 'cross_entry';
export interface Fact {
  kind?: string; text: string; span?: string; source_span?: string;
  conflict_group?: string | null; flag?: string | null; origin?: string;
}
export interface Outline { conclusion: string; points: string[]; examples: string[]; cautions: string[] }
export interface Enhancement {
  question?: string; question_variants?: string[]; answer_outline?: Partial<Outline>;
  role_tags?: string[]; topic_tags?: string[];
}
export interface Entry {
  id: string; question: string; answer: string; original: string; status: string; approved_answer?: string;
  section?: string; line?: number; flagged?: boolean; attribution_uncertain?: boolean;
  facts?: Fact[]; flags?: string[]; enhanced?: boolean; enhance_error?: string; enhancement?: Enhancement | null;
  line_start?: number; line_end?: number; variants?: string[]; version?: number;
  conflict_group?: string | null; conflict_groups?: string[]; batch_approvable?: boolean; content_hash?: string;
}
export interface EnhanceProgress { status: string; total: number; done: number; enhanced: number; failed: number; error: string }
export type FilterKey = 'all' | 'pending' | 'verify' | 'conflict' | 'approved' | 'rejected' | 'deferred';
export type Action = 'approve' | 'defer' | 'reject';

export const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' }, { key: 'pending', label: '待审' }, { key: 'verify', label: '待核' },
  { key: 'conflict', label: '冲突' }, { key: 'approved', label: '已启用' }, { key: 'rejected', label: '驳回' }, { key: 'deferred', label: '暂缓' },
];
export const G3_TARGET = 50;
export const FACT_KINDS = ['project', 'metric', 'role', 'time', 'org'];
const FLAG_LABEL: Record<string, string> = { pending_verify: '待核', conflict: '事实冲突', suspected_fabrication: '疑似虚构', cross_entry: '跨条目拼接' };

export function flagsOf(e: Entry): string[] {
  const set = new Set<string>(Array.isArray(e.flags) ? e.flags : []);
  if (e.flagged) set.add('pending_verify');
  return [...set];
}
export const flagLabel = (f: string) => FLAG_LABEL[f] ?? f;
export const isDeferred = (e: Entry) => e.status === 'pending' && !!(e.approved_answer ?? '').trim();
export const isFabricated = (e: Entry) => flagsOf(e).some((f) => f === 'suspected_fabrication' || f === 'cross_entry');
export const isConflict = (e: Entry) => flagsOf(e).includes('conflict') || conflictGroups(e).length > 0;
export const conflictGroups = (e: Entry): string[] =>
  Array.isArray(e.conflict_groups) && e.conflict_groups.length ? e.conflict_groups : e.conflict_group ? [e.conflict_group] : [];
export const approvedWarning = (e: Entry): string =>
  e.status === 'approved' && (isConflict(e) || flagsOf(e).includes('pending_verify')) ? '已启用但含冲突/待核（只读警示）' : '';
/** 正文里仍有 [待核] 的条目（审核后答案优先）。 */
export const hasPendingMark = (e: Entry): boolean => (e.approved_answer ?? e.answer ?? '').includes('[待核]');
/** 含待核/flagged 的条目不得计入 G3，也不得在实时链路冒充“我的口径”。 */
export const isFlaggedEntry = (e: Entry): boolean => flagsOf(e).includes('pending_verify') || hasPendingMark(e);

export const variantList = (e: Entry): string[] =>
  Array.isArray(e.enhancement?.question_variants) ? e.enhancement!.question_variants! : Array.isArray(e.variants) ? e.variants : [];
export function outlineOf(e: Entry): Outline {
  const o = e.enhancement?.answer_outline ?? {};
  return { conclusion: o.conclusion ?? '', points: [...(o.points ?? [])], examples: [...(o.examples ?? [])], cautions: [...(o.cautions ?? [])] };
}
export const factSpan = (f: Fact): string => f.span ?? f.source_span ?? '';
export const enhancedLabel = (e: Entry): string =>
  e.enhanced ? '已增强' : `未增强（${enhanceErrorLabel(e.enhance_error ?? '') || '规则拆解'}）`;

export function matchesFilter(e: Entry, key: FilterKey): boolean {
  const fl = flagsOf(e);
  switch (key) {
    case 'all': return true;
    case 'pending': return e.status === 'pending' && !isDeferred(e);
    case 'verify': return fl.includes('pending_verify');
    case 'conflict': return fl.includes('conflict');
    case 'approved': return e.status === 'approved';
    case 'rejected': return e.status === 'rejected';
    case 'deferred': return isDeferred(e);
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

// 与后端 batch-approvable 判据一致：待核、说话人待核、冲突、疑似虚构/跨条目都不可批量
export function batchBlockers(e: Entry): string[] {
  const out = flagsOf(e).map((f) => (f === 'pending_verify' ? '含待核标记' : '标记：' + flagLabel(f)));
  if (e.attribution_uncertain) out.push('说话人待核');
  return out;
}
export const canBatch = (e: Entry) => e.status === 'pending' && batchBlockers(e).length === 0;
export const batchCandidates = (entries: Entry[]) => entries.filter(canBatch);

// 勾选集合按后端 /batch-approvable 拆分；冲突/待核/虚构条目只提示、不提交
export function batchSelection(entries: Entry[], checked: Iterable<string>, approvable: Set<string>) {
  const known = new Map(entries.map((e) => [e.id, e] as const));
  const safe: string[] = [], blocked: { id: string; reasons: string[] }[] = [];
  for (const id of checked) {
    const e = known.get(id);
    if (e && approvable.has(id)) safe.push(id);
    else if (e) blocked.push({ id, reasons: batchBlockers(e).length ? batchBlockers(e) : ['不在可批量启用列表'] });
    else blocked.push({ id, reasons: ['条目不存在'] });
  }
  return { safe, blocked };
}

export function canApprove(e: Entry, confirmed: boolean, answer: string): { ok: boolean; why: string } {
  if (isFabricated(e)) return { ok: false, why: '疑似虚构/跨条目拼接，禁止启用' };
  if (isConflict(e)) return { ok: false, why: '事实冲突（跨条目），禁止启用' };
  if (!confirmed) return { ok: false, why: '请先勾选“已核对事实并保留待核提醒”' };
  if (!answer.trim()) return { ok: false, why: '审核答案不能为空' };
  return { ok: true, why: '' };
}

export function progress(entries: Entry[]) {
  const approvedEntries = entries.filter((e) => e.status === 'approved');
  const approved = approvedEntries.length;
  const eligible = approvedEntries.filter((e) => !isFlaggedEntry(e)).length;
  const pendingApproved = approvedEntries.filter(hasPendingMark).length;
  const rejected = entries.filter((e) => e.status === 'rejected').length;
  const verify = entries.filter((e) => e.status === 'pending' && flagsOf(e).includes('pending_verify')).length;
  const g3Ready = eligible >= G3_TARGET;
  const label = `已启用 ${approved} 条 · 可计入 G3 ${eligible} 条 · 达标 ${G3_TARGET}`
    + (pendingApproved ? ` · 含待核 ${pendingApproved} 条需逐条核实` : '')
    + (g3Ready ? ' · G3 可用' : '');
  return {
    approved, eligible, flaggedApproved: approved - eligible, pendingApproved,
    rejected, verify, total: entries.length, reviewed: approved + rejected,
    pct: Math.min(100, Math.round((eligible / G3_TARGET) * 100)), g3Ready, label,
  };
}

const STATUS: Record<Action, string> = { approve: 'approved', defer: 'pending', reject: 'rejected' };
export function reviewBody(action: Action, answer: string, confirmed: boolean) {
  return { status: STATUS[action], answer, confirmed: action === 'approve' && confirmed };
}
export const reviewUrl = (lib: string, id: string) => `/api/libraries/${encodeURIComponent(lib)}/entries/${encodeURIComponent(id)}/review`;
export const editUrl = (lib: string, id: string) => `/api/libraries/${encodeURIComponent(lib)}/entries/${encodeURIComponent(id)}`;
export const batchUrl = (lib: string) => `/api/libraries/${encodeURIComponent(lib)}/batch-approve`;
export const batchBody = (keys: string[], confirmed: boolean) => ({ keys, confirmed });
export const reimportUrl = (lib: string) => `/api/libraries/${encodeURIComponent(lib)}/reimport`;

export function editBody(question: string, variants: string[], outline: Outline,
  facts: { kind: string; text: string; source_span: string }[]) {
  return { question, variants, outline, facts };
}
export const reimportBody = (name: string, text: string) => ({ name, text });

export const enhanceErrorLabel = (code = ''): string => {
  if (!code) return '';
  if (code.startsWith('call_failed:')) return '模型调用失败（' + code.slice(12) + '）';
  if (code.startsWith('invalid_schema:')) return '模型输出不合规';
  const why: Record<string, string> = {
    no_enhancer: '未接入增强器', no_key: '未配置 DeepSeek 凭证', outbound_disabled: '外发开关已关闭',
    budget_denied: '预算已用尽', invalid_json: '模型输出无法解析',
  };
  return why[code] ?? code;
};
export function enhanceLabel(p?: EnhanceProgress): string {
  if (!p || p.status === 'idle') return '未开始增强';
  if (p.status === 'running') return `增强中 ${p.done}/${p.total}`;
  return `增强完成 ${p.enhanced}/${p.total}` + (p.failed ? ` · 失败 ${p.failed}` : '');
}
export function statsText(s: { version?: number; added: string[]; changed: string[]; deleted: string[]; inherited: number }): string {
  return `版本 v${s.version ?? '?'}：新增 ${s.added.length}、变更 ${s.changed.length}、删除 ${s.deleted.length}、继承审核状态 ${s.inherited} 条`;
}

export function describeRejected(rejected: { id: string; reasons: string[] }[] = []): string[] {
  return rejected.map((r) => `${r.id}：${(r.reasons ?? []).join('、') || '被拒'}`);
}

// 优先队列排序：队列里的条目按队列顺序在前，其余保持原序
export function orderByQueue(entries: Entry[], queue: { id: string }[]): Entry[] {
  const rank = new Map(queue.map((q, i) => [q.id, i] as const));
  const inQ = entries.filter((e) => rank.has(e.id)).sort((a, b) => rank.get(a.id)! - rank.get(b.id)!);
  return [...inQ, ...entries.filter((e) => !rank.has(e.id))];
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
