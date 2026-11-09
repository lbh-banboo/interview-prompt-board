export const FILTERS = [
    { key: 'all', label: '全部' }, { key: 'pending', label: '待审' }, { key: 'verify', label: '待核' },
    { key: 'conflict', label: '冲突' }, { key: 'approved', label: '已启用' }, { key: 'rejected', label: '驳回' }, { key: 'deferred', label: '暂缓' },
];
export const G3_TARGET = 50;
export const FACT_KINDS = ['project', 'metric', 'role', 'time', 'org'];
const FLAG_LABEL = { pending_verify: '待核', conflict: '事实冲突', suspected_fabrication: '疑似虚构', cross_entry: '跨条目拼接' };
export function flagsOf(e) {
    const set = new Set(Array.isArray(e.flags) ? e.flags : []);
    if (e.flagged)
        set.add('pending_verify');
    return [...set];
}
export const flagLabel = (f) => FLAG_LABEL[f] ?? f;
export const isDeferred = (e) => e.status === 'pending' && !!(e.approved_answer ?? '').trim();
export const isFabricated = (e) => flagsOf(e).some((f) => f === 'suspected_fabrication' || f === 'cross_entry');
export const isConflict = (e) => flagsOf(e).includes('conflict') || conflictGroups(e).length > 0;
export const conflictGroups = (e) => Array.isArray(e.conflict_groups) && e.conflict_groups.length ? e.conflict_groups : e.conflict_group ? [e.conflict_group] : [];
export const approvedWarning = (e) => e.status === 'approved' && (isConflict(e) || flagsOf(e).includes('pending_verify')) ? '已启用但含冲突/待核（只读警示）' : '';
/** 正文里仍有 [待核] 的条目（审核后答案优先）。 */
export const hasPendingMark = (e) => (e.approved_answer ?? e.answer ?? '').includes('[待核]');
/** 含待核/flagged 的条目不得计入 G3，也不得在实时链路冒充“我的口径”。 */
export const isFlaggedEntry = (e) => flagsOf(e).includes('pending_verify') || hasPendingMark(e);
export const variantList = (e) => Array.isArray(e.enhancement?.question_variants) ? e.enhancement.question_variants : Array.isArray(e.variants) ? e.variants : [];
export function outlineOf(e) {
    const o = e.enhancement?.answer_outline ?? {};
    return { conclusion: o.conclusion ?? '', points: [...(o.points ?? [])], examples: [...(o.examples ?? [])], cautions: [...(o.cautions ?? [])] };
}
export const factSpan = (f) => f.span ?? f.source_span ?? '';
export const enhancedLabel = (e) => e.enhanced ? '已增强' : `未增强（${enhanceErrorLabel(e.enhance_error ?? '') || '规则拆解'}）`;
export function matchesFilter(e, key) {
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
// 与后端 batch-approvable 判据一致：待核、说话人待核、冲突、疑似虚构/跨条目都不可批量
export function batchBlockers(e) {
    const out = flagsOf(e).map((f) => (f === 'pending_verify' ? '含待核标记' : '标记：' + flagLabel(f)));
    if (e.attribution_uncertain)
        out.push('说话人待核');
    return out;
}
export const canBatch = (e) => e.status === 'pending' && batchBlockers(e).length === 0;
export const batchCandidates = (entries) => entries.filter(canBatch);
// 勾选集合按后端 /batch-approvable 拆分；冲突/待核/虚构条目只提示、不提交
export function batchSelection(entries, checked, approvable) {
    const known = new Map(entries.map((e) => [e.id, e]));
    const safe = [], blocked = [];
    for (const id of checked) {
        const e = known.get(id);
        if (e && approvable.has(id))
            safe.push(id);
        else if (e)
            blocked.push({ id, reasons: batchBlockers(e).length ? batchBlockers(e) : ['不在可批量启用列表'] });
        else
            blocked.push({ id, reasons: ['条目不存在'] });
    }
    return { safe, blocked };
}
export function canApprove(e, confirmed, answer) {
    if (isFabricated(e))
        return { ok: false, why: '疑似虚构/跨条目拼接，禁止启用' };
    if (isConflict(e))
        return { ok: false, why: '事实冲突（跨条目），禁止启用' };
    if (!confirmed)
        return { ok: false, why: '请先勾选“已核对事实并保留待核提醒”' };
    if (!answer.trim())
        return { ok: false, why: '审核答案不能为空' };
    return { ok: true, why: '' };
}
export function progress(entries) {
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
const STATUS = { approve: 'approved', defer: 'pending', reject: 'rejected' };
export function reviewBody(action, answer, confirmed) {
    return { status: STATUS[action], answer, confirmed: action === 'approve' && confirmed };
}
export const reviewUrl = (lib, id) => `/api/libraries/${encodeURIComponent(lib)}/entries/${encodeURIComponent(id)}/review`;
export const editUrl = (lib, id) => `/api/libraries/${encodeURIComponent(lib)}/entries/${encodeURIComponent(id)}`;
export const batchUrl = (lib) => `/api/libraries/${encodeURIComponent(lib)}/batch-approve`;
export const batchBody = (keys, confirmed) => ({ keys, confirmed });
export const reimportUrl = (lib) => `/api/libraries/${encodeURIComponent(lib)}/reimport`;
export function editBody(question, variants, outline, facts) {
    return { question, variants, outline, facts };
}
export const reimportBody = (name, text) => ({ name, text });
export const enhanceErrorLabel = (code = '') => {
    if (!code)
        return '';
    if (code.startsWith('call_failed:'))
        return '模型调用失败（' + code.slice(12) + '）';
    if (code.startsWith('invalid_schema:'))
        return '模型输出不合规';
    const why = {
        no_enhancer: '未接入增强器', no_key: '未配置 DeepSeek 凭证', outbound_disabled: '外发开关已关闭',
        budget_denied: '预算已用尽', invalid_json: '模型输出无法解析',
    };
    return why[code] ?? code;
};
export function enhanceLabel(p) {
    if (!p || p.status === 'idle')
        return '未开始增强';
    if (p.status === 'running')
        return `增强中 ${p.done}/${p.total}`;
    return `增强完成 ${p.enhanced}/${p.total}` + (p.failed ? ` · 失败 ${p.failed}` : '');
}
export function statsText(s) {
    return `版本 v${s.version ?? '?'}：新增 ${s.added.length}、变更 ${s.changed.length}、删除 ${s.deleted.length}、继承审核状态 ${s.inherited} 条`;
}
export function describeRejected(rejected = []) {
    return rejected.map((r) => `${r.id}：${(r.reasons ?? []).join('、') || '被拒'}`);
}
// 优先队列排序：队列里的条目按队列顺序在前，其余保持原序
export function orderByQueue(entries, queue) {
    const rank = new Map(queue.map((q, i) => [q.id, i]));
    const inQ = entries.filter((e) => rank.has(e.id)).sort((a, b) => rank.get(a.id) - rank.get(b.id));
    return [...inQ, ...entries.filter((e) => !rank.has(e.id))];
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
