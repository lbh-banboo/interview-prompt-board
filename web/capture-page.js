import { CaptureController } from './capture/controller.js';
import { CAPTURE_HINT, afterStopLinks, resolveSession } from './capture/session.js';
const $ = (id) => document.getElementById(id);
const message = (text) => { $('message').textContent = text; };
let token = '';
let session = '';
let controller = null;
async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': token }, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.detail ?? '请求失败');
    }
    return data;
}
async function stop() {
    await controller?.stop();
    await api(`/api/sessions/${session}/stop`, {});
    message('已结束并保存');
    for (const l of afterStopLinks(session)) {
        const a = document.createElement('a');
        a.href = l.href;
        a.textContent = l.text;
        if (l.href.endsWith('/export'))
            a.onclick = (ev) => { ev.preventDefault(); exportRecord(a); };
        $('message').append(' ', a);
    }
}
// 导出接口要本地令牌头，普通链接会 403：带令牌取回后以文件下载。
async function exportRecord(a) {
    const r = await fetch(a.href, { headers: { 'X-Local-Token': token } });
    if (!r.ok)
        return message(`导出失败（${r.status}）`);
    const url = URL.createObjectURL(await r.blob());
    const dl = document.createElement('a');
    dl.href = url;
    dl.download = `面试记录-${session.slice(0, 8)}.md`;
    dl.click();
    URL.revokeObjectURL(url);
}
function view() {
    return {
        device: $('device'), consent: $('consent'),
        start: $('start'), meeting: $('meeting'), stop: $('stop'),
        meetingMode: $('meeting-mode'), meetingDevice: $('meeting-device'),
        ws: $('ws'), stats: $('stats'),
        level: (s) => $(`level-${s}`),
        sourceText: (s) => $(`state-${s}`),
        say: message,
    };
}
for (const [id, fn] of [
    ['start', () => controller.start()],
    ['meeting', () => controller.addMeeting()],
    ['stop', stop],
]) {
    $(id).onclick = () => { fn().catch((e) => message(String(e))); };
}
setInterval(() => controller?.drawStats(), 500);
async function init() {
    const status = await api('/api/status');
    token = status.token;
    const choice = resolveSession(status, new URLSearchParams(location.search).get('session') ?? '');
    if (!choice) {
        message(CAPTURE_HINT);
        const link = document.createElement('a');
        link.href = '/setup.html';
        link.textContent = '打开会前配置向导';
        $('message').append(' ', link);
        $('start').disabled = true;
        return;
    }
    session = choice.id;
    controller = new CaptureController({ session, token, view: view() });
    await controller.initDevices();
    message(`本地服务就绪；已复用会话 ${session.slice(0, 8)}…`);
}
init().catch((e) => message(String(e)));
