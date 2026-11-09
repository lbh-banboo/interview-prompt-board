// gnav.logic：全站顶栏的纯逻辑。不碰 DOM，便于 node 测试。
// 导航固定 4 项 + “更多”；路径映射决定顶栏高亮与“更多”内的 aria-current。
export const TOP_ITEMS = [
    { key: 'home', label: '首页', href: '/' },
    { key: 'workspace', label: '工作台', href: '/home.html' },
    { key: 'records', label: '记录库', href: '/records.html' },
    { key: 'settings', label: '设置', href: '/settings.html' },
];
export const MORE_ITEMS = [
    { key: 'import', label: '导入资料', href: '/import.html', group: 'workspace' },
    { key: 'review', label: '主库审核', href: '/review.html', group: 'workspace' },
    { key: 'secondary', label: '次优库审核', href: '/secondary.html', group: 'workspace' },
    { key: 'annotate', label: '提问点校对', href: '/annotate.html', group: 'workspace' },
    { key: 'voice', label: '声音登记', href: '/setup.html?step=voice', group: 'setup' },
    { key: 'setup', label: '开始会话', href: '/setup.html', group: 'setup' },
    { key: 'live', label: '会话页', href: '/live.html', group: 'session' },
];
const TOP_BY_PATH = {
    '/': 'home',
    '/index.html': 'home',
    '/home.html': 'workspace',
    '/import.html': 'workspace',
    '/review.html': 'workspace',
    '/secondary.html': 'workspace',
    '/annotate.html': 'workspace',
    '/records.html': 'records',
    '/settings.html': 'settings',
};
const MORE_BY_PATH = {
    '/import.html': 'import',
    '/review.html': 'review',
    '/secondary.html': 'secondary',
    '/annotate.html': 'annotate',
    '/live.html': 'live',
    '/board.html': 'live',
    '/capture.html': 'live',
};
export function normalizePath(pathname) {
    const path = pathname.trim();
    if (!path || path === '/')
        return '/';
    const trimmed = path.replace(/\/+$/, '');
    return trimmed || '/';
}
/** 接受 pathname、带查询串的 URL 或完整 URL。 */
export function matchRoute(url) {
    const clean = String(url || '').split('#')[0];
    if (!clean.trim())
        return { top: null, more: null };
    const q = clean.indexOf('?');
    const rawPath = q === -1 ? clean : clean.slice(0, q);
    const query = q === -1 ? '' : clean.slice(q + 1);
    const path = normalizePath(rawPath);
    const top = TOP_BY_PATH[path] ?? null;
    let more = MORE_BY_PATH[path] ?? null;
    if (path === '/setup.html') {
        more = new URLSearchParams(query).get('step') === 'voice' ? 'voice' : 'setup';
    }
    return { top, more };
}
export function liveHref(sessionId) {
    return sessionId ? `/live.html?session=${encodeURIComponent(sessionId)}` : '/setup.html';
}
/** /api/status 的 active_session 兼容字符串与 {id} 两种形态。 */
export function activeSessionId(status) {
    if (!status || typeof status !== 'object')
        return null;
    const active = status.active_session;
    if (typeof active === 'string' && active)
        return active;
    if (active && typeof active === 'object') {
        const id = active.id;
        if (typeof id === 'string' && id)
            return id;
    }
    return null;
}
export function buildMoreItems(sessionId) {
    return MORE_ITEMS.map((item) => (item.key === 'live' ? { ...item, href: liveHref(sessionId) } : { ...item }));
}
export const LEAVE_CONFIRM_TEXT = '离开会话页会中断本页的音频采集；会话仍保持进行中，可从工作台或首页再进入会话页继续（但已中断的采集不会自动恢复）。';
/** 仅在会话页且采集进行中才需要离开确认。 */
export function needsLeaveConfirm(state) {
    return normalizePath(state.pathname) === '/live.html' && state.phase === 'capturing';
}
export function burgerState(open) {
    return {
        open,
        expanded: open ? 'true' : 'false',
        label: open ? '关闭菜单' : '打开菜单',
        overlayHidden: !open,
    };
}
export function shouldCloseMenuOnResize(open, width) {
    return open && width > 720;
}
