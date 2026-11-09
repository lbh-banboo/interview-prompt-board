// landing.logic：欢迎页纯逻辑。只做数据聚合与容错，不碰 DOM；
// 接口失败一律显示 —（不编造 0），空库/空列表是真实状态、如实显示 0。
export const DASH = '—';
const ASR_BACKEND_LABELS = {
    aliyun: '云转写',
    sherpa: '本地转写',
    none: '未接入',
};
const MODE_LABELS = {
    online: '线上',
    offline: '线下',
};
const STATUS_LABELS = {
    active: '进行中',
    stopped: '已结束',
    ended: '已结束',
};
export function sumApproved(libraries) {
    if (!Array.isArray(libraries))
        return null;
    let sum = 0;
    for (const lib of libraries) {
        if (lib && typeof lib.approved === 'number' && Number.isFinite(lib.approved))
            sum += lib.approved;
    }
    return sum;
}
export function secondaryApproved(res) {
    if (!res || typeof res !== 'object')
        return null;
    return typeof res.approved === 'number' && Number.isFinite(res.approved) ? res.approved : null;
}
export function sessionsCount(res) {
    if (!res || typeof res !== 'object')
        return null;
    if (typeof res.total === 'number' && Number.isFinite(res.total))
        return res.total;
    if (Array.isArray(res.sessions))
        return res.sessions.length;
    return null;
}
export function asrText(res) {
    if (!res || typeof res !== 'object')
        return null;
    if (typeof res.asr === 'string' && res.asr.trim())
        return res.asr.trim();
    if (typeof res.asr_backend === 'string')
        return ASR_BACKEND_LABELS[res.asr_backend] ?? null;
    return null;
}
function statText(value) {
    return value === null ? DASH : formatCount(value);
}
export function buildStats(input) {
    const primary = sumApproved(input.libraries ?? null);
    const secondary = secondaryApproved(input.secondary ?? null);
    const sessions = sessionsCount(input.sessions ?? null);
    const asr = asrText(input.status ?? null);
    return [
        { key: 'primary', label: '主库已启用', value: primary, text: statText(primary) },
        { key: 'secondary', label: '次优库已通过', value: secondary, text: statText(secondary) },
        { key: 'sessions', label: '历史会话', value: sessions, text: statText(sessions) },
        { key: 'asr', label: '转写方式', value: null, text: asr ?? DASH },
    ];
}
export function formatCount(n) {
    return String(Math.round(n));
}
/* ---------- count-up（与参考页一致的 easeOutCubic） ---------- */
export function easeOutCubic(t) {
    const c = Math.min(1, Math.max(0, t));
    return 1 - Math.pow(1 - c, 3);
}
export function countUpValue(target, t, decimals = 0) {
    return (target * easeOutCubic(t)).toFixed(decimals);
}
export function finalStatValue(target, decimals = 0) {
    return target.toFixed(decimals);
}
export function menuState(open) {
    return {
        open,
        expanded: open ? 'true' : 'false',
        label: open ? '关闭菜单' : '打开菜单',
        overlayHidden: !open,
        bodyClass: open,
    };
}
export function toggleMenu(open) {
    return !open;
}
export function shouldCloseMenuOnResize(open, width) {
    return open && width > 720;
}
export function formatSessionTime(ts) {
    if (typeof ts !== 'number' || !Number.isFinite(ts))
        return DASH;
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function recentSessions(res, limit = 3) {
    const rows = Array.isArray(res?.sessions) ? res.sessions : [];
    return rows
        .filter((r) => r && typeof r.id === 'string' && r.id && !r.deleted)
        .sort((a, b) => (Number(b.started_at) || 0) - (Number(a.started_at) || 0))
        .slice(0, limit)
        .map((r) => {
        const id = r.id;
        const mode = typeof r.mode === 'string' ? r.mode : '';
        const status = typeof r.status === 'string' ? r.status : '';
        return {
            id,
            modeLabel: MODE_LABELS[mode] ?? (mode || DASH),
            statusLabel: STATUS_LABELS[status] ?? (status || DASH),
            timeLabel: formatSessionTime(r.started_at ?? r.created_at ?? null),
            href: `/records.html?session=${encodeURIComponent(id)}`,
        };
    });
}
