import { OUTBOUND_ITEMS } from '../settings/logic.js';
export { OUTBOUND_ITEMS };
export const STEPS = ['mode', 'profile', 'roles', 'library', 'voice', 'source'];
export const STEP_TITLES = {
    mode: '收音模式', profile: '回答者资料', roles: '目标岗位', library: '本场主库', voice: '声音登记', source: '音源与同意',
};
/** 所有模式共用同一套六步；声音登记在线上模式为可选，线下同室仍必须。 */
export const stepsFor = (_mode = '') => STEPS;
export const VOICE_OPTIONAL_NOTE = '可选：声音登记只对线下同室模式是必须的；线上模式可跳过，已登记的声纹仍可用于线下。';
export const SHARE_REMINDER = '请只共享需要演示的应用窗口，不要共享整个屏幕';
export const UNION_HINT = '次优库将按所选岗位并集检索';
export const CHECK_LABELS = {
    audio_sources: '音源有声', asr_connect: 'ASR 连通', llm_connect: 'DeepSeek 连通', search_connect: '搜索连通',
    voice_enrolled: '声音登记', disk_space: '磁盘空间', library_approved_count: '主库启用条数',
};
export const emptyState = () => ({
    mode: '', profileId: '', roles: [], libraryIds: [], secondaryOnly: false, sourceConfirmed: false, consent: false,
    voiceEnrolled: false, voiceUnavailable: false,
    outbound: { asr_audio: true, llm_snippets: true, web_questions: true },
    outboundBaseline: { asr_audio: true, llm_snippets: true, web_questions: true },
});
/** 用全局配置初始化本场外发开关（默认与全局一致；本场只能关，不能开）。 */
export function initOutbound(s, config) {
    const base = {
        asr_audio: config?.outbound?.asr_audio !== false,
        llm_snippets: config?.outbound?.llm_snippets !== false,
        web_questions: config?.outbound?.web_questions !== false,
    };
    return { ...s, outbound: { ...base }, outboundBaseline: { ...base } };
}
/** 只提交“全局开、本场关”的项；空对象表示沿用全局。 */
export function outboundPatch(s) {
    const patch = {};
    for (const item of OUTBOUND_ITEMS) {
        if (s.outboundBaseline[item.key] && !s.outbound[item.key])
            patch[item.key] = false;
    }
    return patch;
}
export function validateStep(s, step, libs) {
    switch (step) {
        case 'mode': return s.mode ? null : '请选择收音模式';
        case 'profile': return s.profileId ? null : '请选择或新建回答者资料';
        case 'roles': return s.roles.length ? null : '请至少选择一个目标岗位';
        case 'library': {
            const bad = s.libraryIds.find((id) => !libs.find((l) => l.id === id)?.selectable);
            if (bad)
                return '所选主库启用 0 条，不可选择，请先去导入资料或题库审核启用条目';
            if (s.libraryIds.length || s.secondaryOnly)
                return null;
            return libs.some((l) => l.selectable)
                ? '请选择一个主库，或选择“仅用次优库和联网继续”'
                : '没有可选的主库。请先去导入资料或到题库审核启用条目，或选择“仅用次优库和联网继续”';
        }
        case 'voice':
            if (s.mode !== 'offline' || s.voiceEnrolled || s.voiceUnavailable)
                return null;
            return '请先完成声音登记（点“开始录音”，把全文读完再停止）；声纹模型不可用时，可勾选“声纹不可用，继续（角色识别降级）”';
        case 'source':
            if (!s.sourceConfirmed)
                return '请确认开始会话后会在采集页授权音源';
            return s.consent ? null : '请先勾选知情同意';
    }
}
export const firstInvalid = (s, libs) => stepsFor(s.mode).find((k) => validateStep(s, k, libs) !== null) ?? null;
export const toggle = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
/** 主库单选：点选已选项取消，点其它项替换为该项。 */
export const selectLibrary = (list, id) => (list[0] === id ? [] : [id]);
export const unionHint = (s) => (s.roles.length > 1 ? UNION_HINT : '');
export function libraryRows(libs) {
    return libs.map((l) => ({
        ...l,
        label: `${l.name}（启用 ${l.approved} / 共 ${l.total} 条）`,
        hint: l.selectable ? '' : '启用 0 条 · 先导入资料或去题库审核',
    }));
}
export function createBody(s) {
    return { mode: s.mode, profile_id: s.profileId, roles: s.roles, library_ids: s.libraryIds, consent: s.consent,
        secondary_only: s.secondaryOnly, outbound: outboundPatch(s) };
}
export const startBody = (degraded) => ({ degraded });
export const liveHref = (sid) => `/live.html?session=${encodeURIComponent(sid)}`;
export function activeSession(status) {
    const s = status?.active_session;
    if (!s?.id)
        return { active: false, id: '', label: '' };
    const id = String(s.id);
    const mode = s.mode === 'offline' ? '线下' : s.mode === 'online' ? '线上' : '';
    return { active: true, id, label: `进行中的会话${mode ? '（' + mode + '）' : ''} ${id.slice(0, 8)}` };
}
/** 创建会话遇到 409 时，把“继续/结束”的出路补进文案；其它错误原样返回。 */
export function conflictMessage(detail) {
    return /已有进行中的会话/.test(detail) ? `${detail}。可在“进行中的会话”卡片继续，或先结束该会话。` : detail;
}
export function summarize(report) {
    const rows = report.items.map((i) => ({ ...i, label: CHECK_LABELS[i.name] ?? i.name }));
    const count = (st) => rows.filter((r) => r.status === st).length;
    return {
        rows,
        counts: { PASS: count('PASS'), WARN: count('WARN'), FAIL: count('FAIL') },
        headline: report.has_fail ? `${count('FAIL')} 项失败，不能静默开始` : report.overall === 'WARN' ? '有提醒，可以开始' : '全部通过',
        needsDegrade: report.has_fail,
        banner: report.degraded_banner,
    };
}
// 开始按钮：无 FAIL 直接可开；有 FAIL 必须显式确认降级
export function startMode(report, degradeConfirmed) {
    if (!report)
        return 'wait';
    if (!report.has_fail)
        return 'start';
    return degradeConfirmed ? 'degrade' : 'blocked';
}
// ---------- 声音登记（线下）：连续录音，点开始/点停止 ----------
export const ENROLL_SEGMENTS = 1; // 连续录音只产生一段；保留常量便于判断提交条件
export const ENROLL_MIN_SECONDS = 1;
export const ENROLL_RECOMMENDED_SECONDS = 12;
export const ENROLL_NOTICE = '声纹只保存在本机（SQLite），不外发；可随时删除或重新登记。';
export const ENROLL_HINT = '点“开始录音”，完整朗读上面的固定文本，读完再点“停止录音”。'
    + `建议至少录 ${ENROLL_RECOMMENDED_SECONDS} 秒（正常语速读完全文约 15 秒）。`;
export const ENROLL_FALLBACK_TEXT = '今天我来参加这次面试，很高兴有机会和各位老师交流。接下来我会用平常的语速，清晰地朗读这段文字，'
    + '请保持安静的环境，并和麦克风保持适中的距离。';
export const VOICE_UNAVAILABLE_LABEL = '声纹不可用或暂不登记，继续（角色识别降级为能量+上下文推断）';
/** 录音状态机的按钮文案：录制中可停止；已有一段时可重录。 */
export function recordButtonText(recording, hasRecording) {
    return recording ? '停止录音' : hasRecording ? '重新录制' : '开始录音';
}
/** 提交按钮可用：已停止、已有录音、已录时长 ≥1 秒且不在提交中（最终有效性由后端判定）。 */
export function submitEnabled(recording, hasRecording, seconds, busy) {
    return !recording && !busy && hasRecording && seconds >= ENROLL_MIN_SECONDS;
}
/** 已录时长 mm:ss（向下取整；非法值按 00:00）。 */
export function durationText(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0)
        return '00:00';
    const total = Math.floor(seconds);
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    return `${mm}:${ss}`;
}
/** 质量数字若存在则展示；缺失字段不编造。 */
export function qualityText(q) {
    if (!q)
        return '';
    const parts = [];
    if (typeof q.effective_speech_s === 'number')
        parts.push(`有效语音 ${q.effective_speech_s} 秒`);
    if (typeof q.snr_db === 'number')
        parts.push(`SNR ${q.snr_db} dB`);
    if (typeof q.min_pair_similarity === 'number')
        parts.push(`最低相似度 ${q.min_pair_similarity}`);
    return parts.join(' / ');
}
/** “已登记（质量 x，日期）” / “未登记”；不把未登记渲染成死胡同。 */
export function profileText(profile) {
    if (!profile?.enrolled)
        return '未登记';
    const q = qualityText(profile.quality);
    const date = typeof profile.created_at === 'number'
        ? new Date(profile.created_at * 1000).toISOString().slice(0, 10) : '';
    const meta = [q, date].filter(Boolean).join('，');
    return meta ? `已登记（${meta}）` : '已登记';
}
/** 登记结果文案：成功给质量；失败用后端 Reason/MESSAGES 文案，不覆盖成笼统错误。 */
export function enrollResultText(result) {
    if (!result)
        return { ok: false, text: '登记失败：没有收到结果，请重试。', detail: '' };
    const detail = qualityText(result.quality);
    if (result.ok)
        return { ok: true, text: `登记成功${detail ? `：${detail}` : ''}`, detail };
    let text = result.message || '登记失败，请重试。';
    const effective = result.quality?.effective_speech_s;
    if (result.reason === 'too_short_speech' && typeof effective === 'number' && !text.includes(String(effective))) {
        text += `（有效语音 ${effective} 秒，请把全文读完，建议录满 15 秒。）`;
    }
    return { ok: false, text, detail };
}
/** 是否已有可提交的录音（连续录音下为 ≥1 段）。 */
export const enrollReady = (segments) => segments >= ENROLL_SEGMENTS;
