export const CAPTURE_HINT = '没有进行中的会话：请先在会前配置向导完成配置并点击“开始会话”，再回到本页采集。';
export function resolveSession(status, query) {
    const linked = (query || '').trim();
    if (linked)
        return { id: linked, source: 'query' };
    const active = typeof status?.active_session?.id === 'string' ? status.active_session.id : '';
    return active ? { id: active, source: 'active' } : null;
}
// 结束并保存后的出路：提示板（本场最终状态）、导出本场记录、回会前配置开始下一场。
export function afterStopLinks(session) {
    const q = encodeURIComponent(session);
    return [
        { href: `/board.html?session=${q}`, text: '打开提示板' },
        { href: `/api/sessions/${q}/export`, text: '导出本场记录' },
        { href: '/setup.html', text: '开始下一场' },
    ];
}
