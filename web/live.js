import { BoardClient } from './board/client.js';
import { keyAction, nextScale } from './board/keys.js';
import { Renderer } from './board/render.js';
import { CaptureController } from './capture/controller.js';
import { CAPTURE_HINT, resolveSession } from './capture/session.js';
import { degradedText, elapsedSeconds, endConfirmText, exportName, formatElapsed, modeLabel, phaseLabel, phaseOf, setupHref, shortId, sourceSummary, } from './live/logic.js';
import { canStart, capturingSummary, createGuide, guideSteps, guideSummary, MIC_DENIED_HELP, MIC_SILENT_HELP, micGateStatus, micOnlyNotice, SOURCE_PLAN, SPEAKER_FAILED_HELP, SPEAKER_TEST_MS, startBlockedReason, START_READY_TEXT, stepIcon, stepStatus, VOICE_RMS, } from './live/guide.js';
import { profileText } from './setup/logic.js';
import { outboundLine } from './settings/logic.js';
const $ = (id) => document.getElementById(id);
const msg = (text) => { $('capture-msg').textContent = text; };
const nowSec = () => Date.now() / 1000;
const STORAGE_PREFIX = 'live-ended-at:';
let token = '';
let sid = '';
let info = { status: '', mode: '', started_at: null, stopped_at: null, capture_started_at: null, degraded_banner: null };
let phase = 'preparing';
let frozenEndSec;
let controller = null;
let renderer = null;
let client = null;
let guideMode = 'online';
let guide = createGuide();
let voiceNotice = '';
let speakerTestUntil = 0;
let voice = { known: false, enrolled: false };
let micGateOn = true; // 本场快照 turn.mic_as_question；关闭后仅麦克风恢复旧行为
async function api(url, body = {}) {
    const res = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': token }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok)
        throw new Error(data.detail || '本地请求失败');
    return data;
}
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
function normalize(raw) {
    const status = raw.status === 'ended' ? 'stopped' : typeof raw.status === 'string' ? raw.status : '';
    return {
        status,
        mode: typeof raw.mode === 'string' ? raw.mode : '',
        started_at: num(raw.started_at), stopped_at: num(raw.stopped_at),
        capture_started_at: num(raw.capture_started_at),
        degraded_banner: typeof raw.degraded_banner === 'string' ? raw.degraded_banner : null,
    };
}
/** setup 会话带 started_at / degraded_banner；旧会话回落到 /api/status 或快照，不让页面因 404 报错。 */
async function loadInfo(status) {
    try {
        const res = await fetch(`/api/setup/${encodeURIComponent(sid)}`);
        if (res.ok)
            return normalize(await res.json());
    }
    catch { /* 继续回落 */ }
    if (status?.active_session?.id === sid)
        return normalize(status.active_session);
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/snapshot`);
        if (res.ok) {
            const snap = await res.json();
            return normalize({ status: snap.session_status === 'active' ? 'active' : 'stopped', mode: snap.mode });
        }
    }
    catch { /* 无快照：仍显示准备中 */ }
    return normalize({});
}
/** 只读展示本场外发开关（会话启动时的快照）；旧会话没有该字段时不显示。 */
async function loadOutbound() {
    try {
        const res = await fetch(`/api/setup/${encodeURIComponent(sid)}`);
        if (!res.ok)
            return;
        const data = await res.json();
        if (data?.config?.turn?.mic_as_question === false)
            micGateOn = false;
        const flags = data?.config?.outbound;
        if (!flags)
            return;
        const node = $('live-outbound');
        node.textContent = `${outboundLine(flags)}（启动时快照，改动下一场生效）`;
        node.hidden = false;
    }
    catch { /* 展示失败不影响采集 */ }
}
/** 声纹登记状态：线上单麦模式需要已登记；线下的降级提示沿用原文案。 */
async function loadVoice() {
    let p = null;
    try {
        const res = await fetch('/api/voice/profile', { headers: { 'X-Local-Token': token } });
        if (res.ok)
            p = await res.json();
        voice = { known: res.ok, enrolled: !!p?.enrolled };
    }
    catch {
        voice = { known: false, enrolled: false };
    }
    if (guideMode !== 'offline')
        return;
    voiceNotice = p?.enrolled
        ? `声纹：${profileText(p)}。`
        : voice.known
            ? '声纹未登记：角色识别将降级为能量+上下文推断；可在会前配置向导“声音登记”完成登记。'
            : '声纹状态未知：角色识别可能降级。';
}
function storedEnd() {
    try {
        const v = parseFloat(localStorage.getItem(STORAGE_PREFIX + sid) || '');
        return Number.isFinite(v) ? v : undefined;
    }
    catch {
        return undefined;
    }
}
function disableCapture() {
    for (const id of ['start', 'meeting', 'meeting-skip', 'authorize', 'speaker-test', 'speaker-skip', 'end']) {
        $(id).disabled = true;
    }
    $('consent').disabled = true;
    $('device').disabled = true;
    $('meeting-mode').disabled = true;
    $('meeting-device').disabled = true;
}
function drawLive(state) {
    const cap = controller?.snapshot();
    $('live-sid').textContent = `会话 ${shortId(sid)}`;
    $('mode').textContent = modeLabel(info.mode || state?.mode || '');
    $('live-state').textContent = phaseLabel(phase);
    $('live-state').dataset.state = phase;
    const elapsed = elapsedSeconds(info, nowSec(), frozenEndSec);
    $('live-elapsed').textContent = elapsed === null ? '—' : formatElapsed(elapsed);
    const banner = degradedText(info.degraded_banner);
    $('live-banner').hidden = !banner;
    $('live-banner').textContent = banner;
    const sources = sourceSummary(cap ?? {
        frames: { mic: 0, meeting: 0, room: 0 }, gaps: 0,
        sources: { mic: { condition: 'idle' }, meeting: { condition: 'idle' }, room: { condition: 'idle' } },
    }, guideMode);
    const linkBad = phase === 'capturing' && (cap?.ws === 'reconnecting' || cap?.ws === 'error');
    $('live-sources').textContent = linkBad
        ? `${sources} · 音频连接${cap?.ws === 'error' ? '被拒绝' : '重连中'}`
        : sources;
    $('live-sources').dataset.link = linkBad ? 'bad' : '';
    const alertSource = guideMode === 'offline' ? cap?.sources.room.condition : cap?.sources.meeting.condition;
    $('live-sources').dataset.alert = alertSource === 'silent' ? (guideMode === 'offline' ? 'room' : 'meeting') : '';
    // 线上只采集麦克风：单麦模式用声纹区分双方；未登记声纹时单麦模式不可用（给出登记入口）。
    const gate = client?.state?.health?.mic_gate ?? null;
    const gateState = typeof gate?.state === 'string' ? gate.state : '';
    const micOnly = guideMode === 'online' && guide.meeting !== 'ok';
    const switched = phase === 'capturing' && gateState === 'switched';
    const noQuestioner = phase === 'capturing' && micOnly && !client?.state?.question && !switched;
    const unenrolled = micGateOn && voice.known && !voice.enrolled;
    $('lamp-meeting').dataset.need = noQuestioner ? '1' : '';
    const notice = noQuestioner || switched
        ? micOnlyNotice({ gateOn: micGateOn, known: voice.known, enrolled: voice.enrolled, switched })
        : '';
    setQNotice(notice, noQuestioner && unenrolled);
    let gateText = micGateStatus(gate);
    if (!gateText && phase === 'capturing' && micOnly && unenrolled)
        gateText = '单麦模式不可用：声纹未登记';
    $('mic-gate').hidden = !gateText;
    $('mic-gate').textContent = gateText;
    $('ended-card').hidden = phase !== 'ended';
    $('end').disabled = phase === 'ended';
    if (phase === 'ended') {
        $('next-session').setAttribute('href', setupHref());
        $('view-record').setAttribute('href', `/records.html?session=${encodeURIComponent(sid)}`);
        disableCapture();
    }
    drawGuide();
}
/** 当前问题下的说明行；未登记声纹时追加“去登记”入口。 */
function setQNotice(text, enrollLink) {
    const node = $('q-notice');
    node.hidden = !text;
    const key = `${text}|${enrollLink ? 1 : 0}`;
    if (node.dataset.key === key)
        return;
    node.dataset.key = key;
    if (!text) {
        node.textContent = '';
        return;
    }
    if (!enrollLink) {
        node.textContent = text;
        return;
    }
    const link = document.createElement('a');
    link.href = '/setup.html?step=voice';
    link.textContent = '去登记';
    link.className = 'q-enroll';
    node.replaceChildren(`${text} `, link);
}
function applyEnded(fromStop) {
    phase = 'ended';
    info = { ...info, status: 'stopped' }; // elapsedSeconds 按 info.status 判断是否冻结；只改 phase 计时会继续走
    if (fromStop) {
        frozenEndSec = nowSec();
        try {
            localStorage.setItem(STORAGE_PREFIX + sid, String(frozenEndSec));
        }
        catch { /* 隐私模式忽略 */ }
    }
    $('board').classList.add('live-ended');
    if (renderer && client?.state)
        renderer.render({ ...client.state, ended: true }, false);
    drawLive(client?.state);
}
function showNoSession() {
    $('no-session').hidden = false;
    $('no-session-text').textContent = CAPTURE_HINT;
    $('live-sid').textContent = '未选择会话';
    $('live-state').textContent = '未开始';
    $('live-state').dataset.state = 'ended';
    $('live-elapsed').textContent = '—';
    $('board').classList.add('no-session');
    disableCapture();
}
async function downloadRecord() {
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/export`, { headers: { 'X-Local-Token': token } });
        if (!res.ok) {
            $('ended-msg').textContent = `导出失败（${res.status}）`;
            return;
        }
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement('a');
        a.href = url;
        a.download = exportName(sid);
        a.click();
        URL.revokeObjectURL(url);
        $('ended-msg').textContent = '已导出本场记录';
    }
    catch (e) {
        $('ended-msg').textContent = `导出失败：${String(e)}`;
    }
}
async function endSession() {
    $('end').disabled = true;
    try {
        await controller?.stop();
        await api(`/api/sessions/${encodeURIComponent(sid)}/stop`, {});
        applyEnded(true);
    }
    catch (e) {
        $('end').disabled = false;
        msg(`结束失败：${e.message}`);
    }
}
// ---------- 采集引导 ----------
function thirdStepKey() {
    return guideMode === 'offline' ? 'speaker' : 'meeting';
}
function stepElement(key) {
    return document.querySelector(`.g-step[data-step="${key}"]`);
}
/** 只改状态与文案，不重建节点：输入框焦点和已展开的说明不会被打断。 */
function drawGuide() {
    const steps = guideSteps(guideMode, guide);
    const keys = ['choose', 'authorize', thirdStepKey()];
    stepElement('meeting')?.toggleAttribute('hidden', guideMode !== 'online');
    stepElement('speaker')?.toggleAttribute('hidden', guideMode !== 'offline');
    steps.forEach((step, i) => {
        const li = stepElement(keys[i]);
        if (!li)
            return;
        li.dataset.phase = step.phase;
        li.classList.toggle('cur', step.phase === 'active');
        const icon = li.querySelector('.g-icon');
        const status = li.querySelector('.g-status');
        if (icon)
            icon.textContent = stepIcon(step.phase);
        if (status)
            status.textContent = stepStatus(step.phase);
    });
    $('choose-hint').textContent = steps[0].hint;
    $('auth-hint').textContent = steps[1].hint;
    const sourceHint = steps[2].hint;
    if (guideMode === 'offline') {
        $('speaker-hint').textContent = `${sourceHint}${voiceNotice ? ` ${voiceNotice}` : ''}`;
    }
    else {
        stepElement('meeting').querySelector('.g-hint').textContent = sourceHint;
    }
    const micHelp = guide.mic === 'denied' ? MIC_DENIED_HELP : guide.mic === 'silent' ? MIC_SILENT_HELP : '';
    $('mic-help').hidden = !micHelp;
    $('mic-help').textContent = micHelp;
    $('speaker-help').hidden = guide.speaker !== 'failed';
    $('speaker-help').textContent = guide.speaker === 'failed' ? SPEAKER_FAILED_HELP : '';
    const idle = phase === 'preparing';
    const consent = guide.consent;
    const micReady = guide.mic === 'ok';
    const auth = $('authorize');
    auth.disabled = !idle || !consent || guide.mic === 'checking' || guide.mic === 'silent' || micReady;
    auth.textContent = micReady ? '麦克风已授权'
        : guide.mic === 'denied' ? '重新授权' : guide.mic === 'silent' ? '已授权，请说话' : '授权并测试麦克风';
    const meeting = $('meeting');
    meeting.disabled = !idle || !consent || !micReady || guide.meeting === 'ok';
    $('meeting-skip').disabled = !idle || !micReady || guide.meeting === 'ok';
    const test = $('speaker-test');
    const skip = $('speaker-skip');
    const testing = Date.now() < speakerTestUntil;
    test.disabled = !idle || !micReady || guide.speaker === 'ok' || testing;
    test.textContent = testing ? '监听中（5 秒）…' : '开始 5 秒外放测试';
    skip.disabled = !idle || !micReady || guide.speaker === 'ok';
    const start = $('start');
    start.disabled = !idle || !canStart(guideMode, guide);
    start.textContent = phase === 'capturing' ? '采集中' : '开始采集';
    const blocked = startBlockedReason(guideMode, guide);
    $('start-hint').hidden = !idle;
    $('start-hint').textContent = idle ? blocked ?? START_READY_TEXT : '';
    $('start-hint').dataset.state = blocked ? 'blocked' : 'ready';
    const summary = phase === 'capturing'
        ? `采集中 · ${capturingSummary(guideMode, guide, micGateOn, voice.enrolled)}`
        : guideSummary(guideMode, guide, micGateOn);
    $('guide-summary').textContent = summary;
    $('capture-title').textContent = phase === 'capturing' ? '采集' : '采集控制';
}
function onMicLevel(reading) {
    const roomBar = document.getElementById('level-room');
    const roomState = document.getElementById('state-room');
    if (roomBar)
        roomBar.value = Math.min(1, reading.rms * 4);
    if (reading.rms < VOICE_RMS)
        return;
    let changed = false;
    if (guide.mic !== 'ok') {
        guide.mic = 'ok';
        changed = true;
    }
    if (speakerTestUntil && Date.now() < speakerTestUntil) {
        speakerTestUntil = 0;
        guide.speaker = 'ok';
        if (roomState)
            roomState.textContent = '检测到声音';
        msg('外放测试通过（本页无法区分是谁在说，请确认刚才出声的是对方）');
        changed = true;
    }
    else if (roomState && speakerTestUntil) {
        roomState.textContent = '监听中…';
    }
    if (changed)
        drawGuide();
}
function onMicProblem(kind) {
    guide.mic = kind;
    drawGuide();
}
function startSpeakerTest() {
    guide.speaker = 'pending';
    speakerTestUntil = Date.now() + SPEAKER_TEST_MS;
    $('state-room').textContent = '监听中…';
    msg('外放测试中：请让对方说一句，或播放一段视频声音');
    drawGuide();
    window.setTimeout(() => {
        if (!speakerTestUntil || Date.now() < speakerTestUntil)
            return;
        speakerTestUntil = 0;
        guide.speaker = 'failed';
        $('state-room').textContent = '无声音';
        drawGuide();
    }, SPEAKER_TEST_MS + 50);
}
async function onAuthorize() {
    if (!guide.consent) {
        msg('请先确认录音知情许可');
        return;
    }
    guide.mic = 'checking';
    drawGuide();
    const ok = await controller.authorizeMic();
    if (!ok)
        guide.mic = 'denied';
    drawGuide();
}
async function main() {
    const status = await (await fetch('/api/status')).json();
    token = typeof status?.token === 'string' ? status.token : '';
    const choice = resolveSession(status, new URLSearchParams(location.search).get('session') ?? '');
    if (!choice) {
        showNoSession();
        return;
    }
    sid = choice.id;
    info = await loadInfo(status);
    await loadOutbound();
    guideMode = info.mode === 'offline' ? 'offline' : 'online';
    phase = phaseOf(info.status, false);
    if (phase === 'ended')
        frozenEndSec = storedEnd();
    await loadVoice();
    renderer = new Renderer();
    renderer.onPromptShown = (id) => client?.ack(id);
    client = new BoardClient(sid, token, {
        onState: (s, connected) => { renderer.render(s, connected); drawLive(s); },
        onBanner: (text) => { renderer.setBanner(text); if (client?.state)
            renderer.render(client.state, true); },
        sendAck: (ws, promptId) => ws.send(JSON.stringify({
            type: 'display_ack', prompt_id: promptId, t_visible: performance.timeOrigin + performance.now(),
        })),
    });
    controller = new CaptureController({
        session: sid, token, mode: guideMode,
        view: {
            device: $('device'), consent: $('consent'),
            start: $('start'), meeting: $('meeting'),
            meetingMode: $('meeting-mode'), meetingDevice: $('meeting-device'),
            ws: $('ws'), stats: $('stats'),
            level: (s) => $(s === 'meeting' ? 'level-meeting' : 'level-mic'),
            sourceText: (s) => $(s === 'meeting' ? 'state-meeting' : 'state-mic'),
            say: msg,
            onMicLevel: (reading) => onMicLevel(reading),
            onMicProblem: (kind) => onMicProblem(kind),
        },
    });
    controller.drawStats();
    $('choose-hint').textContent = SOURCE_PLAN[guideMode];
    $('consent').addEventListener('change', () => {
        guide.consent = $('consent').checked;
        if (!guide.consent) {
            guide.meeting = 'pending';
            guide.speaker = 'pending';
            speakerTestUntil = 0;
        }
        drawGuide();
    });
    $('authorize').addEventListener('click', () => { void onAuthorize(); });
    $('meeting').addEventListener('click', () => {
        guide.meeting = 'pending';
        drawGuide();
        void controller.addMeeting().then((ok) => {
            guide.meeting = ok ? 'ok' : 'failed';
            drawGuide();
        });
    });
    $('meeting-skip').addEventListener('click', () => {
        guide.meeting = 'skipped';
        drawGuide();
    });
    $('speaker-test').addEventListener('click', () => startSpeakerTest());
    $('speaker-skip').addEventListener('click', () => {
        speakerTestUntil = 0;
        guide.speaker = 'skipped';
        $('state-room').textContent = '已跳过';
        drawGuide();
    });
    $('start').addEventListener('click', () => {
        if (!canStart(guideMode, guide))
            return;
        void controller.start().then(async (ok) => {
            if (ok) {
                phase = 'capturing';
                $('capture-panel').open = false;
                // 采集起点由服务端在首个 live 帧时写入；取回来再计时，避免用“开始会话”时刻。
                // 首帧可能在 start() 返回后才落库，所以 1.5 秒后再取一次。
                const refresh = async () => {
                    let status = {};
                    try {
                        status = await (await fetch('/api/status')).json();
                    }
                    catch { /* 保留旧值 */ }
                    info = await loadInfo(status);
                    drawLive(client?.state);
                };
                await refresh();
                setTimeout(() => { void refresh(); }, 1500);
            }
        });
    });
    $('end').addEventListener('click', () => {
        if (phase === 'ended')
            return;
        $('end-title').textContent = endConfirmText;
        $('end-modal').hidden = false;
        $('end-confirm').focus();
    });
    $('end-cancel').addEventListener('click', () => { $('end-modal').hidden = true; });
    $('end-confirm').addEventListener('click', () => { $('end-modal').hidden = true; void endSession(); });
    $('export-record').addEventListener('click', () => { void downloadRecord(); });
    let scale = 0;
    document.addEventListener('keydown', (e) => {
        const cand = client?.state?.vadCandidate;
        const action = keyAction(e, !!cand);
        if (!action)
            return;
        if (action === 'accept_candidate') {
            e.preventDefault();
            if (cand)
                client?.acceptCandidate(cand.candidate_id);
        }
        else if (action === 'toggle_hide') {
            document.body.classList.toggle('is-hidden');
        }
        else {
            scale = nextScale(scale, action);
            document.documentElement.dataset.scale = String(scale);
        }
    });
    if (phase === 'ended')
        disableCapture();
    else if (info.status !== 'active' && info.status !== '')
        disableCapture();
    drawLive();
    await controller.initDevices();
    void client.start();
    setInterval(() => { controller?.drawStats(); drawLive(client?.state); }, 500);
    setInterval(() => drawLive(client?.state), 1000);
}
void main().catch((e) => {
    $('capture-msg').textContent = String(e);
});
