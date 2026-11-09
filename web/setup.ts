import {
  ActiveSession, ENROLL_FALLBACK_TEXT, ENROLL_HINT, ENROLL_NOTICE, ENROLL_RECOMMENDED_SECONDS,
  EnrollQuality, LibraryOption, Report, STEP_TITLES, SHARE_REMINDER, StepKey, VOICE_OPTIONAL_NOTE, VOICE_UNAVAILABLE_LABEL,
  WizardState, activeSession, conflictMessage, createBody, durationText, emptyState, enrollReady, enrollResultText,
  initOutbound, libraryRows, liveHref, OUTBOUND_ITEMS, profileText, recordButtonText, selectLibrary, startBody,
  startMode, stepsFor, submitEnabled, summarize, toggle, unionHint, validateStep,
} from './setup/logic.js';
import { defaultLibraryIds, profileOptionText } from './profile/logic.js';
import { ProfileEditor } from './profile/editor.js';
import { startCapture, type CaptureHandle } from './capture/capture.js';
import { micConstraints } from './capture/constraints.js';

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
function el(tag: string, text = '', cls = ''): HTMLElement {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
const S = {
  token: '', step: 0, st: emptyState() as WizardState, roles: [] as string[], libs: [] as LibraryOption[],
  profiles: [] as any[], sid: '', report: null as Report | null, started: false,
  active: activeSession(null) as ActiveSession,
  editor: null as ProfileEditor | null, editorOpen: false, profileNotice: '',
  voice: {
    enrolled: false, quality: null as EnrollQuality | null, createdAt: null as number | null,
    text: ENROLL_FALLBACK_TEXT, modelAvailable: true, segments: [] as Int16Array[], recording: false,
    elapsedMs: 0,
    busy: false, error: '', unavailable: false, reenroll: false,
  },
};
const say = (t: string, bad = false) => { const m = $('msg'); m.textContent = t; m.classList.toggle('bad', bad); };
const voiceSay = (t: string, bad = false) => {
  const m = document.getElementById('voice-error');
  if (m) { m.textContent = t; m.classList.toggle('bad', bad); }
  say(t, bad);
};

async function api(url: string, method = 'GET', body?: unknown) {
  const headers = { 'Content-Type': 'application/json', 'X-Local-Token': S.token };
  const r = await fetch(url, body === undefined && method === 'GET' ? { headers } : {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || '本地请求失败');
  return data;
}

function chip(text: string, on: boolean, click: () => void): HTMLElement {
  const b = el('button', text, 'chip' + (on ? ' on' : ''));
  b.setAttribute('type', 'button'); b.setAttribute('aria-pressed', String(on));
  b.addEventListener('click', click);
  return b;
}
function checkbox(label: string, checked: boolean, change: (v: boolean) => void): HTMLElement {
  const wrap = el('label', '', 'check');
  const i = document.createElement('input');
  i.type = 'checkbox'; i.checked = checked;
  i.addEventListener('change', () => change(i.checked));
  wrap.append(i, el('span', label));
  return wrap;
}

function body(key: StepKey): HTMLElement {
  const box = el('div', '', 'step-body');
  if (key === 'mode') {
    const cards = el('div', '', 'cards');
    for (const [m, t, d] of [['online', '线上会议', '两路音源：你的麦克风 + 会议音频。建议戴耳机。'], ['offline', '线下同室', '一路麦克风同时收录双方，需要先做声音登记。']] as const) {
      const c = el('button', '', 'card' + (S.st.mode === m ? ' on' : ''));
      c.setAttribute('type', 'button');
      c.append(el('strong', t), el('div', d, 'muted'));
      c.addEventListener('click', () => { S.st.mode = m; draw(); });
      cards.append(c);
    }
    box.append(cards);
  } else if (key === 'profile') {
    const sel = document.createElement('select');
    sel.className = 'field'; sel.setAttribute('aria-label', '回答者资料');
    sel.id = 'profile-select';
    sel.append(new Option('请选择资料', ''));
    for (const p of S.profiles) sel.append(new Option(profileOptionText(p), p.id));
    sel.value = S.st.profileId;
    sel.addEventListener('change', () => {
      S.st.profileId = sel.value;
      const p = S.profiles.find((x) => x.id === sel.value);
      S.st.libraryIds = defaultLibraryIds(p);  // 选定资料后默认选中其关联主库（仍可更改）
      S.profileNotice = '';
      draw();
    });
    const row = el('div', '', 'row');
    const add = el('button', '新建资料', 'btn'); add.id = 'profile-new'; add.setAttribute('type', 'button');
    add.addEventListener('click', () => { openProfileEditor(null); });
    const edit = el('button', '编辑资料', 'btn'); edit.id = 'profile-edit'; edit.setAttribute('type', 'button');
    (edit as HTMLButtonElement).disabled = !S.st.profileId;
    edit.addEventListener('click', () => { openProfileEditor(S.profiles.find((x) => x.id === S.st.profileId) ?? null); });
    row.append(add, edit);
    box.append(sel, row);
    if (S.profileNotice) {
      const notice = el('p', S.profileNotice, 'profile-notice');
      notice.id = 'profile-notice';
      box.append(notice);
    }
    const chosen = S.profiles.find((x) => x.id === S.st.profileId);
    if (chosen?.library_stats && chosen.library_stats.approved < chosen.library_stats.total) {
      const rev = el('a', '去审核', 'btn small') as HTMLAnchorElement; rev.id = 'profile-review'; rev.href = '/review.html';
      box.append(rev);
    }
    if (S.editorOpen && S.editor) box.append(S.editor.el);
  } else if (key === 'roles') {
    const chips = el('div', '', 'chips');
    for (const r of S.roles) chips.append(chip(r, S.st.roles.includes(r), () => { S.st.roles = toggle(S.st.roles, r); draw(); }));
    box.append(chips);
    if (unionHint(S.st)) box.append(el('div', unionHint(S.st), 'muted'));
  } else if (key === 'library') {
    for (const l of libraryRows(S.libs)) {
      const on = S.st.libraryIds[0] === l.id;
      const o = el('button', l.label, 'opt' + (on ? ' on' : '') + (l.selectable ? '' : ' off'));
      o.setAttribute('type', 'button');
      if (!l.selectable) { o.setAttribute('aria-disabled', 'true'); o.append(el('span', l.hint, 'hint')); }
      else o.addEventListener('click', () => { S.st.libraryIds = selectLibrary(S.st.libraryIds, l.id); draw(); });
      box.append(o);
    }
    box.append(checkbox('仅用次优库和联网继续（现场只给通用建议，个人经历题需本人补充）', S.st.secondaryOnly, (v) => { S.st.secondaryOnly = v; draw(); }));
    const links = el('div', '', 'row');
    const imp = el('a', '去导入资料', 'btn'); imp.id = 'go-import'; imp.setAttribute('href', '/import.html');
    const rev = el('a', '去题库审核', 'btn'); rev.id = 'go-review'; rev.setAttribute('href', '/review.html');
    links.append(imp, rev);
    box.append(links);
    if (!S.libs.some((l) => l.selectable)) box.append(el('p', '还没有可用的主库：先导入资料，再到题库审核启用条目后回来。', 'muted'));
  } else if (key === 'voice') {
    box.append(voiceBody());
  } else {
    box.append(
      el('div', '开始会话后，在会话页授权麦克风/会议音频。音频默认会上传云端转写、识别出的片段会发送给 DeepSeek（可在下方“本场外发”关闭）。', 'muted'),
      checkbox('我确认开始会话后会在会话页完成音源授权', S.st.sourceConfirmed, (v) => { S.st.sourceConfirmed = v; draw(); }),
      checkbox('我已知情并同意本场录音与转写', S.st.consent, (v) => { S.st.consent = v; draw(); }));
    box.append(el('h3', '本场外发', 'source-title'));
    box.append(el('p', '默认与设置页一致；本场只能关闭，不能开启全局已关闭的项。', 'muted'));
    for (const item of OUTBOUND_ITEMS) {
      const baseOn = S.st.outboundBaseline[item.key];
      const on = S.st.outbound[item.key];
      const wrap = document.createElement('label');
      wrap.className = 'check';
      const i = document.createElement('input');
      i.type = 'checkbox';
      i.checked = on;
      i.disabled = !baseOn;
      i.dataset.outbound = item.key;
      i.setAttribute('aria-label', `${item.title}（本场）`);
      i.addEventListener('change', () => { S.st.outbound[item.key] = i.checked; draw(); });
      const text = document.createElement('span');
      text.textContent = `${item.title}：${baseOn ? (on ? '本场开' : '本场关') : '全局已关闭'} — ${item.text}`;
      wrap.append(i, text);
      box.append(wrap);
    }
  }
  return box;
}

function concatPcm(chunks: Int16Array[]): Int16Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Int16Array(total);
  let offset = 0;
  for (const c of chunks) { out.set(c, offset); offset += c.length; }
  return out;
}

/** Int16 -> 小端字节 -> base64，与 app.api.voice.decode_segment 的 16k 单声道 PCM16 约定一致。 */
function pcmBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function voiceBody(): HTMLElement {
  const box = el('div', '', 'step-body');
  if (S.st.mode !== 'offline') box.append(el('p', VOICE_OPTIONAL_NOTE, 'muted voice-optional'));
  box.append(el('p', ENROLL_NOTICE, 'muted voice-notice'));
  const status = el('p', profileText(S.voice.enrolled
    ? { enrolled: true, quality: S.voice.quality, created_at: S.voice.createdAt } : null), 'voice-status');
  status.id = 'voice-status';
  box.append(status);

  if (S.voice.enrolled && !S.voice.reenroll) {
    const actions = el('div', '', 'row');
    const re = el('button', '重新登记', 'btn'); re.id = 'voice-reenroll'; re.setAttribute('type', 'button');
    re.addEventListener('click', () => { S.voice.reenroll = true; S.voice.segments = []; S.voice.elapsedMs = 0; S.voice.error = ''; draw(); });
    const del = el('button', '删除已登记声纹', 'btn danger'); del.id = 'voice-delete'; del.setAttribute('type', 'button');
    del.addEventListener('click', () => { void deleteVoice(); });
    actions.append(re, del);
    box.append(actions);
    box.append(el('p', '删除后线下角色识别会降级；可随时重新登记。', 'muted'));
    return box;
  }

  if (S.voice.reenroll) box.append(el('p', '重新登记成功后会覆盖原有声纹。', 'muted'));
  box.append(el('blockquote', S.voice.text, 'enroll-text'));
  box.append(el('p', ENROLL_HINT, 'muted'));
  const level = document.createElement('progress');
  level.id = 'voice-level'; level.max = 1; level.value = 0;
  const state = el('span', S.voice.recording ? '录音中…' : '未开始录音');
  state.id = 'voice-state';
  const row = el('p', '', 'voice-level-row');
  row.append('电平 ', level, ' ', state);
  box.append(row);
  const progress = el('p', voiceProgressText(), 'voice-progress');
  progress.id = 'voice-progress';
  box.append(progress);
  const actions = el('div', '', 'row');
  const rec = el('button', recordButtonText(S.voice.recording, S.voice.segments.length > 0),
    'btn' + (S.voice.recording ? ' recording' : ''));
  rec.id = 'voice-record'; rec.setAttribute('type', 'button');
  (rec as HTMLButtonElement).disabled = S.voice.busy;
  rec.addEventListener('click', () => {
    if (S.voice.busy) return;
    if (S.voice.recording) { requestStopVoice(); return; }
    if (S.voice.segments.length) { S.voice.segments = []; S.voice.elapsedMs = 0; }
    void startVoiceRecording();
  });
  const submit = el('button', S.voice.busy ? '提交中…' : '提交登记', 'btn pill');
  submit.id = 'voice-submit'; submit.setAttribute('type', 'button');
  (submit as HTMLButtonElement).disabled = !submitEnabled(
    S.voice.recording, S.voice.segments.length > 0, voiceSeconds(), S.voice.busy);
  submit.addEventListener('click', () => { void submitEnrollment(); });
  actions.append(rec, submit);
  box.append(actions);
  box.append(checkbox(VOICE_UNAVAILABLE_LABEL, S.voice.unavailable, (v) => {
    S.voice.unavailable = v; S.st.voiceUnavailable = v; draw();
  }));
  const err = el('p', S.voice.error, 'voice-error' + (S.voice.error ? ' bad' : ''));
  err.id = 'voice-error';
  box.append(err);
  return box;
}

function voiceSeconds(): number {
  const pcm = S.voice.segments[0];
  return pcm ? pcm.length / 16000 : S.voice.elapsedMs / 1000;
}

function voiceProgressText(): string {
  const sec = voiceSeconds();
  if (S.voice.recording) {
    return `录制中 ${durationText(sec)} · 建议至少录 ${ENROLL_RECOMMENDED_SECONDS} 秒（读完全文约 15 秒）`;
  }
  return sec > 0 ? `已录 ${sec.toFixed(1)} 秒` : '尚未录音';
}

function updateVoiceProgress(): void {
  const p = document.getElementById('voice-progress');
  if (p) p.textContent = voiceProgressText();
}

let voiceStop: (() => void) | null = null;
let voiceStopRequested = false;

function requestStopVoice(): void {
  voiceStopRequested = true;
  voiceStop?.();
}

/** 连续录音：点“开始录音”后一直录，直到用户点“停止录音”；只产生一段 PCM。 */
async function startVoiceRecording(): Promise<void> {
  if (S.voice.recording || S.voice.busy) return;
  S.voice.recording = true;
  S.voice.error = '';
  S.voice.elapsedMs = 0;
  voiceStopRequested = false;
  voiceStop = null;
  draw();
  let handle: CaptureHandle | null = null;
  let stream: MediaStream | null = null;
  const chunks: Int16Array[] = [];
  let samples = 0;
  try {
    stream = await navigator.mediaDevices.getUserMedia(micConstraints('offline'));
    handle = await startCapture(stream, {
      onFrame: (pcm) => {
        chunks.push(pcm.slice());
        samples += pcm.length;
        S.voice.elapsedMs = Math.round((samples / 16000) * 1000);
        updateVoiceProgress();
      },
      onLevel: (reading) => {
        const bar = document.getElementById('voice-level') as HTMLProgressElement | null;
        const text = document.getElementById('voice-state');
        if (bar) bar.value = Math.min(1, reading.rms * 4);
        if (text) text.textContent = reading.rms >= 0.005 ? '检测到声音' : '请朗读…';
      },
      onLost: () => { /* 录制结束时忽略 */ },
    });
    await new Promise<void>((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      voiceStop = () => { void handle!.stop().then(finish, finish); };
      if (voiceStopRequested) voiceStop();  // 麦克风授权期间就点了停止
    });
    const pcm = concatPcm(chunks);
    if (pcm.length === 0) throw new Error('没有收到音频，请检查麦克风');
    S.voice.segments = [pcm];
    S.voice.elapsedMs = Math.round((pcm.length / 16000) * 1000);
    voiceSay(`已录 ${(pcm.length / 16000).toFixed(1)} 秒，可提交登记或重新录制`);
  } catch (e) {
    S.voice.segments = [];
    S.voice.elapsedMs = 0;
    S.voice.error = `录音失败：${(e as Error).message}`;
  } finally {
    stream?.getTracks().forEach((t) => t.stop());
    voiceStop = null;
    S.voice.recording = false;
    draw();
  }
}

async function submitEnrollment(): Promise<void> {
  if (S.voice.busy || !enrollReady(S.voice.segments.length)) return;
  S.voice.busy = true;
  S.voice.error = '';
  draw();
  try {
    const result = await api('/api/voice/enroll', 'POST', { segments: S.voice.segments.map(pcmBase64) });
    const view = enrollResultText(result);
    if (view.ok) {
      S.voice.enrolled = true;
      S.voice.reenroll = false;
      S.st.voiceEnrolled = true;
      S.voice.quality = (result.quality ?? null) as EnrollQuality | null;
      S.voice.createdAt = Date.now() / 1000;
      S.st.voiceUnavailable = false;
      S.voice.unavailable = false;
      voiceSay(view.text);
    } else {
      S.voice.error = view.text + (view.detail ? `（${view.detail}）` : '');
      voiceSay(S.voice.error, true);
    }
  } catch (e) {
    S.voice.error = (e as Error).message;
    if (/声纹模型不可用|503/.test(S.voice.error)) S.voice.modelAvailable = false;
    voiceSay(S.voice.error, true);
  } finally {
    S.voice.busy = false;
    draw();
  }
}

async function deleteVoice(): Promise<void> {
  try {
    await api('/api/voice/profile', 'DELETE');
    S.voice.enrolled = false;
    S.voice.reenroll = false;
    S.st.voiceEnrolled = false;
    S.voice.quality = null;
    S.voice.createdAt = null;
    S.voice.segments = [];
    S.voice.elapsedMs = 0;
    voiceSay('已删除本机声纹');
    draw();
  } catch (e) {
    voiceSay((e as Error).message, true);
  }
}

async function loadVoiceProfile(): Promise<void> {
  try {
    const p = await api('/api/voice/profile');
    S.voice.enrolled = !!p.enrolled;
    S.st.voiceEnrolled = S.voice.enrolled;
    S.voice.quality = p.quality ?? null;
    S.voice.createdAt = typeof p.created_at === 'number' ? p.created_at : null;
    S.voice.modelAvailable = p.model_available !== false;
    if (typeof p.enroll_text === 'string' && p.enroll_text) S.voice.text = p.enroll_text;
  } catch (e) {
    S.voice.error = (e as Error).message;
  }
}

function openProfileEditor(profile: any | null): void {
  if (!S.editor) {
    S.editor = new ProfileEditor({
      token: S.token,
      onSaved: (p, message) => { void afterProfileSaved(p, message); },
      onCancel: () => { S.editorOpen = false; draw(); },
    });
  }
  S.editorOpen = true;
  void S.editor.open(profile);
  draw();
}

async function afterProfileSaved(p: any, message: string): Promise<void> {
  const [profiles, options] = await Promise.all([api('/api/profiles'), api('/api/setup/options')]);
  S.profiles = profiles;
  S.libs = options.libraries;
  S.st.profileId = p.id;
  S.st.libraryIds = defaultLibraryIds(p);
  if (!S.st.roles.length && Array.isArray(p.roles) && p.roles.length) S.st.roles = p.roles;
  S.profileNotice = message;
  S.editorOpen = false;
  say(message);
  draw();
}

function drawSteps() {
  const list = $('steps');
  list.replaceChildren();
  const steps = stepsFor(S.st.mode);
  S.step = Math.min(S.step, steps.length - 1);
  steps.forEach((key, i) => {
    const li = el('li', '', 'step' + (i < S.step ? ' done' : '') + (i === S.step ? ' cur' : ''));
    const title = key === 'voice' && S.st.mode !== 'offline' ? `${STEP_TITLES[key]}（可选）` : STEP_TITLES[key];
    const t = el('button', `${i + 1}. ${title}`, 'step-title');
    t.setAttribute('type', 'button');
    if (i < S.step && !S.sid) t.addEventListener('click', () => { S.step = i; draw(); });
    li.append(t);
    if (i === S.step) li.append(body(key));
    list.append(li);
  });
  const last = S.step === steps.length - 1;
  $<HTMLButtonElement>('prev').disabled = S.step === 0 || !!S.sid;
  const next = $<HTMLButtonElement>('next');
  next.textContent = last ? '创建会话并自检' : '下一步';
  next.disabled = !!S.sid || S.active.active;
}

function drawCheck() {
  const list = $('check-list');
  list.replaceChildren();
  const r = S.report;
  const sum = r && summarize(r);
  $('check-head').textContent = sum ? `${sum.headline}（通过 ${sum.counts.PASS} · 提醒 ${sum.counts.WARN} · 失败 ${sum.counts.FAIL}）` : '完成六步配置后运行自检。';
  for (const row of sum?.rows ?? []) {
    const li = el('li', '', 'ck ' + row.status);
    const text = el('span', row.label);
    text.append(el('span', row.detail, 'detail'));
    li.append(el('span', row.status, 'tag'), text);
    if (row.name === 'voice_enrolled' && row.status !== 'PASS') {
      const go = el('button', '去登记', 'btn small'); go.id = 'go-voice'; go.setAttribute('type', 'button');
      go.addEventListener('click', () => {
        const steps = stepsFor(S.st.mode);
        S.step = Math.max(0, steps.indexOf('voice'));
        draw();
        $('steps').scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
      li.append(go);
    }
    list.append(li);
  }
  const banner = $('banner');
  banner.hidden = !sum?.banner;
  banner.textContent = sum?.banner ? `降级：${sum.banner}` : '';
  $('degrade-row').hidden = !sum?.needsDegrade;
  $('rerun').hidden = !r || S.started;
  const mode = startMode(r, $<HTMLInputElement>('degrade-ok').checked);
  $<HTMLButtonElement>('start').disabled = S.started || mode === 'wait' || mode === 'blocked';
  $('start').textContent = mode === 'degrade' ? '降级开始' : '开始会话';
}
function drawActive() {
  const card = $('active-card');
  card.hidden = !S.active.active;
  if (!S.active.active) return;
  $('active-label').textContent = S.active.label;
  $<HTMLAnchorElement>('active-live').href = liveHref(S.active.id);
  $<HTMLButtonElement>('active-stop').onclick = () => stopActive();
}

async function stopActive() {
  const id = S.active.id;
  if (!id) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/stop`, 'POST', {});
    S.active = activeSession(null);
    say('已结束进行中的会话，可以创建新会话');
    draw();
  } catch (e) { say((e as Error).message, true); }
}

async function refreshActive() {
  const status = await api('/api/status');
  S.token = status.token;
  S.active = activeSession(status);
  draw();
}

function draw() { drawSteps(); drawCheck(); drawActive(); $('started-card').hidden = !S.started; }

async function runCheck() {
  S.report = await api(`/api/setup/${S.sid}/selfcheck`, 'POST');
  $<HTMLInputElement>('degrade-ok').checked = false;
  say(S.report!.has_fail ? '自检有失败项，请查看右侧面板' : '自检完成');
  draw();
}

async function onNext() {
  const steps = stepsFor(S.st.mode);
  const key = steps[S.step]!;
  const err = validateStep(S.st, key, S.libs);
  if (err) return say(err, true);
  say('');
  if (S.step < steps.length - 1) { S.step++; return draw(); }
  try {
    S.sid = (await api('/api/setup/sessions', 'POST', createBody(S.st))).id;
    await runCheck();
  } catch (e) {
    const msg = (e as Error).message;
    if (/已有进行中的会话/.test(msg)) await refreshActive().catch(() => undefined);
    say(conflictMessage(msg), true); draw();
  }
}

async function doStart() {
  const degraded = $<HTMLInputElement>('degrade-ok').checked;
  try {
    const s = await api(`/api/setup/${S.sid}/start`, 'POST', startBody(degraded));
    S.started = true;
    $<HTMLAnchorElement>('started-live').href = liveHref(S.sid);
    say(s.degraded_banner ? `已降级开始：${s.degraded_banner}` : '会话已开始，请进入会话页授权音源');
    draw();
    $('started-card').scrollIntoView({ block: 'center', behavior: 'smooth' });
  } catch (e) { say((e as Error).message, true); }
}

async function main() {
  const status = await api('/api/status');
  S.token = status.token;
  S.active = activeSession(status);
  await loadVoiceProfile();
  const [o, profiles, cfg] = await Promise.all([api('/api/setup/options'), api('/api/profiles'), api('/api/config')]);
  S.roles = o.roles; S.libs = o.libraries; S.profiles = profiles;
  S.st = initOutbound(S.st, cfg?.config);
  $('share-text').textContent = SHARE_REMINDER;
  if (new URLSearchParams(location.search).get('step') === 'voice') {  // 首页“声音登记”入口：直达登记步骤，但不强制切换模式
    S.step = Math.max(0, stepsFor(S.st.mode).indexOf('voice'));
  }
  $('prev').addEventListener('click', () => { if (S.step > 0) { S.step--; draw(); } });
  $('next').addEventListener('click', onNext);
  $('rerun').addEventListener('click', () => runCheck().catch((e) => say(e.message, true)));
  $('degrade-ok').addEventListener('change', drawCheck);
  $('start').addEventListener('click', () => { $('share-modal').hidden = false; $('share-ok').focus(); });
  $('share-cancel').addEventListener('click', () => { $('share-modal').hidden = true; });
  $('share-ok').addEventListener('click', () => { $('share-modal').hidden = true; doStart(); });
  draw();
}
main().catch((e) => say(e.message, true));
