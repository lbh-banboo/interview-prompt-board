// 会话页采集引导的纯状态机（无 DOM）：线上三步 / 线下三步，决定步骤状态、可开始条件与摘要。
// 线上：① 选择声音方式（知情同意）→ ② 授权麦克风 → ③ 添加会议音频
// 线下：① 选择声音方式（知情同意）→ ② 授权麦克风 → ③ 确认外放测试（可跳过）
/** 电平高于此值视为检测到人声（约 -46 dBFS；level.ts 的静音线更低）。 */
export const VOICE_RMS = 0.005;
/** 外放测试监听时长。 */
export const SPEAKER_TEST_MS = 5000;
export const SOURCE_PLAN = {
    online: '线上：你的麦克风 + 会议音频两路采集。建议戴耳机，避免回声。',
    offline: '线下：只用一路麦克风同时收录双方，对方声音外放；由声纹区分谁在说话。',
};
export const CONSENT_TEXT = '我已知情并同意本场录音与转写';
export const MIC_DENIED_HELP = '麦克风被拒绝或没有设备：点地址栏左侧的锁/摄像头图标允许麦克风；macOS 还需在“系统设置 > 隐私与安全性 > 麦克风”中允许 Chrome；然后点“重新授权”。';
export const MIC_SILENT_HELP = '没有检测到声音：请确认麦克风没有静音、系统输入设备正确，然后对着麦克风说一句话。';
export const SPEAKER_HINT = '请让对方说一句，或播放一段视频声音。本页只检测外放能量，无法区分是谁在说；测试时请确保你自己不出声。';
export const SPEAKER_FAILED_HELP = '5 秒内没有检测到声音：请调大扬声器音量、让对方再说一句，或跳过此项。';
export const MIC_ONLY_NOTICE = '当前只采集麦克风，对方声音未接入，提示板不会出现提示。需要提示请添加会议音频，或使用线下同室外放模式。';
export const MIC_GATE_NOTICE = '单麦模式：用声纹区分你和对方；对方声音需要真的进到麦克风（外放或同一房间），声纹拿不准的语音不会当成提问。';
export const MIC_GATE_UNENROLLED = '要识别对方提问，请先完成声音登记，或添加会议音频。未登记时单麦模式不可用。';
export const MIC_GATE_SWITCHED = '已切换为双通道模式：麦克风是回答方，会议音频是提问方。';
/** 状态条上的单麦门控实时计数；未启用/已切回双通道时按状态给一行说明。 */
export function micGateStatus(g) {
    if (!g)
        return '';
    if (g.state === 'switched')
        return '已切换为双通道模式';
    if (g.state !== 'active')
        return '';
    const c = g.counts ?? {};
    const lat = g.avg_latency_ms ? ` · 角色判定约 ${g.avg_latency_ms}ms` : '';
    return `单麦模式：已识别提问 ${c.questioner ?? 0} 次 · 未识别 ${c.answerer ?? 0} 次 · 声纹拿不准 ${c.unknown ?? 0} 次${lat}`;
}
/** 线上“仅麦克风”时的说明文案（不是死胡同：未登记声纹给出登记入口）。 */
export function micOnlyNotice(opts) {
    if (opts.switched)
        return MIC_GATE_SWITCHED;
    if (!opts.gateOn)
        return MIC_ONLY_NOTICE;
    if (opts.known && !opts.enrolled)
        return MIC_GATE_UNENROLLED;
    return MIC_GATE_NOTICE;
}
export const createGuide = () => ({ consent: false, mic: 'pending', meeting: 'pending', speaker: 'pending' });
const STATUS = {
    pending: '未开始', active: '进行中', done: '完成', attention: '需要处理', skipped: '已跳过',
};
export const stepIcon = (phase) => phase === 'done' ? '✓' : phase === 'attention' ? '!' : phase === 'skipped' ? '–' : phase === 'active' ? '▶' : '○';
export const stepStatus = (phase) => STATUS[phase];
const sourceTitle = (mode) => (mode === 'offline' ? '确认外放测试' : '添加会议音频');
function authorizeStep(state) {
    if (!state.consent)
        return { phase: 'pending', hint: '先在上一步确认知情同意，然后授权麦克风。' };
    switch (state.mic) {
        case 'ok': return { phase: 'done', hint: '已检测到人声。' };
        case 'denied': return { phase: 'attention', hint: MIC_DENIED_HELP };
        case 'silent': return { phase: 'attention', hint: MIC_SILENT_HELP };
        case 'checking': return { phase: 'active', hint: '正在打开麦克风，请允许浏览器使用麦克风。' };
        default: return { phase: 'active', hint: '点击“授权并测试麦克风”，授权后请说一句话。' };
    }
}
function sourceStep(mode, state) {
    if (state.mic !== 'ok')
        return { phase: 'pending', hint: '先完成第 2 步：授权麦克风并检测到人声。' };
    if (mode === 'offline') {
        switch (state.speaker) {
            case 'ok': return { phase: 'done', hint: '已检测到外放声音。' };
            case 'skipped': return { phase: 'skipped', hint: '已跳过外放测试；仍可开始，但请自行确认能听到对方。' };
            case 'failed': return { phase: 'attention', hint: SPEAKER_FAILED_HELP };
            default: return { phase: 'active', hint: SPEAKER_HINT };
        }
    }
    switch (state.meeting) {
        case 'ok': return { phase: 'done', hint: '会议音频已加入。' };
        case 'skipped': return { phase: 'skipped', hint: '已跳过会议音频：单麦模式用声纹区分你和对方，对方声音需要真的进到麦克风；未登记声纹时单麦模式不可用。' };
        case 'failed': return { phase: 'attention', hint: '未获得会议音频：请重新选择“Chrome 标签页”并勾选共享标签页音频，或改用虚拟声卡；也可以跳过，仅用麦克风开始。' };
        default: return { phase: 'active', hint: '共享会议标签页并勾选“共享标签页音频”，或选择虚拟声卡/第二输入设备。' };
    }
}
/** 当前模式的三个步骤及状态（第三步步名随线上/线下变化）。 */
export function guideSteps(mode, state) {
    const choose = state.consent ? 'done' : 'active';
    const auth = authorizeStep(state);
    const src = sourceStep(mode, state);
    const rows = [
        {
            id: 'choose', title: '选择声音方式', phase: choose,
            hint: state.consent ? SOURCE_PLAN[mode] : `${SOURCE_PLAN[mode]} 先确认下面的知情同意。`,
        },
        { id: 'authorize', title: '授权麦克风', ...auth },
        { id: 'source', title: sourceTitle(mode), ...src },
    ];
    return rows.map((r, i) => ({ ...r, index: i + 1, status: STATUS[r.phase] }));
}
/** 麦克风就绪才允许开始采集。线下外放测试、线上会议音频均可“跳过”（失败也可只用麦克风开始）。 */
export function canStart(mode, state) {
    if (!state.consent || state.mic !== 'ok')
        return false;
    return mode === 'offline' ? state.speaker === 'ok' || state.speaker === 'skipped' : state.meeting !== 'pending';
}
/** 开始按钮旁的一行就绪文案：null 表示可以开始。 */
export const START_READY_TEXT = '可以开始采集';
/** 开始按钮被禁用时的具体原因（null = 可以开始）；让用户知道卡在哪一步，而不是只看到灰色按钮。 */
export function startBlockedReason(mode, state) {
    if (!state.consent)
        return '还差：第①步勾选“我已知情并同意本场录音与转写”';
    switch (state.mic) {
        case 'ok': break;
        case 'denied': return '还差：第②步麦克风未授权（点“重新授权”）';
        case 'silent': return '还差：第②步没有检测到麦克风声音（对着麦克风说一句话）';
        case 'checking': return '还差：第②步正在打开麦克风…';
        default: return '还差：第②步点“授权并测试麦克风”';
    }
    if (mode === 'offline') {
        if (state.speaker === 'ok' || state.speaker === 'skipped')
            return null;
        return state.speaker === 'failed'
            ? '还差：第③步外放测试没听到声音（可重试，或点“跳过”）'
            : '还差：第③步点“开始 5 秒外放测试”，或点“跳过”';
    }
    if (state.meeting !== 'pending')
        return null;
    return '还差：第③步请添加会议音频，或点“跳过，仅用麦克风”';
}
/** 引导条收起后的一行摘要。gateOn=false 表示用户在设置里关闭了单麦模式。 */
export function guideSummary(mode, state, gateOn = true) {
    if (mode === 'offline') {
        const test = state.speaker === 'ok' ? '外放测试通过' : state.speaker === 'skipped' ? '外放测试已跳过' : '外放待测试';
        return `房间麦克风（单麦外放）· ${test}`;
    }
    if (state.meeting === 'ok')
        return '麦克风 + 会议音频';
    return gateOn ? '仅麦克风 · 单麦模式' : '仅麦克风（未加入会议音频）';
}
/** 采集开始后状态条上的一行说明。 */
export function capturingSummary(mode, state, gateOn = true, enrolled = true) {
    if (mode === 'offline')
        return '房间麦克风（单麦外放），声纹区分双方';
    if (!state || state.meeting === 'ok')
        return '麦克风 + 会议音频';
    if (!gateOn)
        return '仅麦克风，对方声音没有被采集';
    return enrolled ? '仅麦克风 · 单麦模式（声纹区分双方）' : '仅麦克风 · 单麦模式不可用（声纹未登记）';
}
