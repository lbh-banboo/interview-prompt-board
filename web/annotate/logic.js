export const FILTERS = [
    { key: 'todo', label: '未判定' }, { key: 'done', label: '已判定' }, { key: 'all', label: '全部' },
];
export const isJudged = (it) => it.verified === true; // 未判定不计入已判定数
export const noteOf = (it) => it.human?.note ?? '';
export function progress(items) {
    const judged = items.filter(isJudged).length;
    return {
        total: items.length, judged,
        pct: items.length ? Math.round((judged / items.length) * 100) : 0,
        label: `已判定 ${judged}/${items.length}`,
    };
}
export function visibleItems(items, key) {
    return key === 'all' ? items.slice() : items.filter((it) => isJudged(it) === (key === 'done'));
}
export function stepId(items, current, dir) {
    if (!items.length)
        return 0;
    const i = items.findIndex((x) => x.id === current);
    if (i < 0)
        return items[0].id; // 当前条不在视图内（如已判定被筛掉）：从首条开始
    return items[Math.min(items.length - 1, Math.max(0, i + dir))].id;
}
/** 从当前条之后找下一条未判定，绕回开头；全部判完返回 0。 */
export function nextTodoId(items, current) {
    const n = items.length;
    if (!n)
        return 0;
    const i = Math.max(0, items.findIndex((x) => x.id === current));
    for (let k = 1; k <= n; k++) {
        const it = items[(i + k) % n];
        if (!isJudged(it))
            return it.id;
    }
    return 0;
}
/** 自动播放窗口：时间点前 5 秒到后 2 秒，起点不早于 0。 */
export function clipWindow(tEndMs) {
    return { start: Math.max(0, tEndMs / 1000 - 5), end: tEndMs / 1000 + 2 };
}
export function fmtTime(ms) {
    const s = Math.max(0, ms) / 1000;
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2, '0')}:${(s - m * 60).toFixed(1).padStart(4, '0')}`;
}
const ROLE = { questioner: '面试官?', unknown: '未知' };
const KIND = { question: '问句', statement: '陈述', request: '请求' };
export const guessLabel = (it) => `${ROLE[it.guess_role] ?? it.guess_role}/${KIND[it.guess_kind] ?? it.guess_kind}`;
export const guessConfidence = (it) => (typeof it.confidence === 'number' ? it.confidence.toFixed(2) : '—');
export function judgedLabel(it) {
    if (!isJudged(it))
        return '未判定';
    const h = it.human;
    if (h.is_question_end === false)
        return '不是';
    return h.is_topic_change ? '改题' : h.is_followup ? '追问' : '是';
}
export const verdictBody = (verdict, note) => ({ verdict, note });
export const itemUrl = (audioId, n) => `/api/annotations/${encodeURIComponent(audioId)}/items/${n}`;
export const audioUrl = (audioId) => `/api/annotations/${encodeURIComponent(audioId)}/audio`;
const KEYS = {
    y: 'yes', n: 'no', f: 'followup', t: 'topic_change', s: 'skip', r: 'replay', j: 'next', k: 'prev',
};
export function keyAction(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey)
        return null;
    const t = (ev.target ?? {});
    const tag = (t.tagName ?? '').toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT' || t.isContentEditable)
        return null;
    return KEYS[ev.key] ?? null;
}
