export const FILTERS = [
    { key: 'all', label: '全部' }, { key: 'pending', label: '未审' },
    { key: 'approved', label: '通过' }, { key: 'rejected', label: '驳回' },
];
export const scanMarks = (e) => (Array.isArray(e.scan) ? e.scan : []);
export function matchesFilter(e, key) {
    switch (key) {
        case 'all': return true;
        case 'pending': return e.status === 'pending';
        case 'approved': return e.status === 'approved';
        case 'rejected': return e.status === 'rejected';
    }
}
export function filterEntries(entries, key, text = '') {
    const q = text.trim().toLowerCase();
    return entries.filter((e) => matchesFilter(e, key) && (!q || (e.id + e.question).toLowerCase().includes(q)));
}
export function filterCounts(entries) {
    const out = {};
    for (const f of FILTERS)
        out[f.key] = entries.filter((e) => matchesFilter(e, f.key)).length;
    return out;
}
// 与后端 approve-batch 判据一致：合规扫描命中的条目不可批量
export function batchBlockers(e) {
    return [...new Set(scanMarks(e).map((m) => '合规扫描：' + m.kind))];
}
export const canBatch = (e) => e.status === 'pending' && batchBlockers(e).length === 0;
export const batchCandidates = (entries) => entries.filter(canBatch);
export function canApprove(e, confirmed) {
    if (scanMarks(e).length)
        return { ok: false, why: '含合规扫描标记，需先修订种子内容' };
    if (!confirmed)
        return { ok: false, why: '请先勾选“已核对内容为通用建议”' };
    return { ok: true, why: '' };
}
export function progress(entries) {
    const count = (s) => entries.filter((e) => e.status === s).length;
    const approved = count('approved'), rejected = count('rejected'), total = entries.length;
    const reviewed = approved + rejected;
    return {
        approved, rejected, pending: count('pending'), total, reviewed,
        pct: total ? Math.round((reviewed / total) * 100) : 0,
        label: `已审核 ${reviewed}/${total}`,
    };
}
const STATUS = { approve: 'approved', defer: 'pending', reject: 'rejected' };
export const reviewBody = (action, confirmed) => ({ status: STATUS[action], confirmed: action === 'approve' && confirmed });
export const reviewUrl = (id) => `/api/secondary/${encodeURIComponent(id)}/review`;
export const entriesUrl = (role) => '/api/secondary/entries' + (role ? `?role=${encodeURIComponent(role)}` : '');
export const progressUrl = () => '/api/secondary/progress';
export const batchUrl = () => '/api/secondary/approve-batch';
export const batchBody = (ids, confirmed) => ({ ids, confirmed });
export function describeRejected(rejected = []) {
    return rejected.map((r) => `${r.id}：${(r.reasons ?? []).join('、') || '被拒'}`);
}
export function keyAction(e) {
    if (e.ctrlKey || e.metaKey || e.altKey)
        return null;
    const t = (e.target ?? {});
    const tag = (t.tagName ?? '').toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT' || t.isContentEditable)
        return null;
    switch (e.key) {
        case 'j': return 'next';
        case 'k': return 'prev';
        case 'a': return 'approve';
        case 'h': return 'defer';
        case 'r': return 'reject';
        default: return null;
    }
}
export function step(entries, current, dir) {
    if (!entries.length)
        return '';
    const i = entries.findIndex((e) => e.id === current);
    return entries[Math.min(entries.length - 1, Math.max(0, i < 0 ? 0 : i + dir))].id;
}
