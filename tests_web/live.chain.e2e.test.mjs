// 会话页真实采集链路 E2E：真实 uvicorn（setup 会话、注入 MockASR/HashEmbedder/空凭证、主库索引预建）
// + 真实 Chromium 采集（假媒体流/合成语音）→ /ws/audio → 管线 → AnswerLoop → /ws/board → 同页提示板。
// 覆盖：A 开始按钮禁用原因；B-1 仅麦克风的说明与提问方音源醒目提示；B-2 线上会议音频/线下 room 的提示板；
// B-3 采集后 /ws/board 或 /ws/audio 掉线的可见提示。音频一律本地合成（say TTS，失败则合成音），不连外部服务。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = path.join(ROOT, 'eval', 'screens');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');
const QUESTION = '你能讲讲分布式事务的区别吗';

/** 追加静音：语音尾部留出 400ms，VAD 才能发 speech_end（Chrome 假音频文件循环播放）。 */
function appendSilence(file, ms) {
  const buf = readFileSync(file);
  const at = buf.indexOf('data', 12);
  if (at < 0) throw new Error('WAV 缺少 data 块');
  const zero = Buffer.alloc(Math.round(16000 * ms / 1000) * 2);
  const out = Buffer.concat([buf, zero]);
  out.writeUInt32LE(out.length - 8, 4);
  out.writeUInt32LE(buf.readUInt32LE(at + 4) + zero.length, at + 4);
  writeFileSync(file, out);
}

/** say TTS；不可用时退回幅度调制的合成音（仍是本地合成，报告里如实标注）。 */
function synthSpeech() {
  const dir = mkdtempSync(path.join(tmpdir(), 'live-chain-'));
  const wav = path.join(dir, 'question.wav');
  let tts = false;
  try {
    const r = spawnSync('say', ['-v', 'Tingting', '-o', wav, '--data-format=LEI16@16000', QUESTION], { timeout: 20000 });
    tts = r.status === 0 && existsSync(wav) && statSync(wav).size > 2000;
  } catch { tts = false; }
  if (!tts) {
    // 2s 合成音（振幅调制）+ 尾部静音：只驱动 VAD/帧链路，转写由脚本化 MockASR 提供。
    const rate = 16000;
    const n = rate * 2;
    const pcm = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i++) {
      const env = 0.35 * (0.5 + 0.5 * Math.sin(2 * Math.PI * 2 * i / rate));
      pcm.writeInt16LE(Math.round(8000 * env * Math.sin(2 * Math.PI * 220 * i / rate)), i * 2);
    }
    const header = Buffer.alloc(44);
    const total = 44 + pcm.length;
    header.write('RIFF', 0); header.writeUInt32LE(total - 8, 4); header.write('WAVE', 8);
    header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20);
    header.writeUInt16LE(1, 22); header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28);
    header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
    header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
    writeFileSync(wav, Buffer.concat([header, pcm]));
  }
  appendSilence(wav, 400);
  return { file: wav, tts };
}

function launch(wavFile) {
  return (async () => {
    let chromium;
    try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
    try {
      return { browser: await chromium.launch({ args: [
        '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
        '--autoplay-policy=no-user-gesture-required', '--disable-audio-output', `--use-file-for-fake-audio-capture=${wavFile}`,
      ] }) };
    } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
  })();
}

function startBackend(mode) {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'live_chain_e2e_server.py'), '--mode', mode, '--q', QUESTION],
    { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const next = async () => JSON.parse((await lines.next()).value);
  let done = false;
  return {
    next,
    async cmd(c) { child.stdin.write(JSON.stringify(c) + '\n'); return next(); },
    async stop() {
      if (done) return;
      done = true;
      child.stdin.write('{"op":"quit"}\n'); child.stdin.end();
      await new Promise((r) => child.once('exit', r));
    },
  };
}

/** 会议音频：本地合成问句循环播放；在点“开始采集”时才起播，保证首段语音从采集时刻开始。 */
const MEETING_MEDIA = ({ b64 }) => {
  const md = navigator.mediaDevices;
  const bin = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let ctx = null, dest = null, buffer = null, source = null;
  const ensure = async () => {
    if (ctx) return;
    ctx = new AudioContext();
    dest = ctx.createMediaStreamDestination();
    buffer = await ctx.decodeAudioData(bin.buffer.slice(0));
    await ctx.resume();
    document.addEventListener('click', (e) => {
      if (source || !e.target || !e.target.closest || !e.target.closest('#start')) return;
      source = ctx.createBufferSource();
      source.buffer = buffer; source.loop = true; source.connect(dest); source.start();
    }, true);
  };
  Object.defineProperty(md, 'getDisplayMedia', {
    configurable: true,
    value: async () => {
      await ensure();
      const canvas = document.createElement('canvas');
      canvas.width = 16; canvas.height = 16;
      const video = canvas.captureStream(1).getVideoTracks()[0];
      Object.defineProperty(video, 'getSettings', { configurable: true, value: () => ({ displaySurface: 'browser' }) });
      return new MediaStream([...dest.stream.getAudioTracks(), video]);
    },
  });
};

async function ackAtLeast(backend, n) {
  let acks = 0;
  for (let i = 0; i < 40 && acks < n; i++) {
    acks = (await backend.cmd({ op: 'metrics' })).display_ack;
    if (acks < n) await new Promise((r) => setTimeout(r, 50));
  }
  return acks;
}

function watchProblems(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

const waitStep = (page, id, phase) => page.waitForFunction(
  ({ id, phase }) => document.querySelector(`.g-step[data-step="${id}"]`)?.dataset.phase === phase,
  { id, phase }, { timeout: 15000 },
);

async function openLive(page, port, session) {
  await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
  await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
}

async function authorize(page) {
  await page.check('#consent');
  await page.click('#authorize');
  await waitStep(page, 'authorize', 'done');
}

test('A + B-1：开始按钮禁用时给出具体原因；仅麦克风且未登记声纹时给出单麦模式说明与登记入口', async (t) => {
  const speech = synthSpeech();
  const env = await launch(speech.file);
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('online');
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  let stopped = false;
  try {
    await openLive(page, port, session);
    assert.equal(await page.locator('#start').isDisabled(), true);
    await page.waitForFunction(() => !document.getElementById('start-hint')?.hidden);
    assert.match(await page.locator('#start-hint').textContent(), /第①步/, '未同意时说明卡在第①步');

    await page.check('#consent');
    assert.match(await page.locator('#start-hint').textContent(), /第②步.*授权/);
    await page.click('#authorize');
    await waitStep(page, 'authorize', 'done');
    assert.match(await page.locator('#start-hint').textContent(), /第③步.*会议音频/);
    assert.match(await page.locator('#start-hint').textContent(), /跳过，仅用麦克风/);
    assert.equal(await page.locator('.g-step.cur').count(), 1, '只有一个“进行中”步骤');
    assert.match(await page.locator('.g-step.cur').innerText(), /添加会议音频/);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#start-hint').isVisible(), true, '窄屏也能看到禁用原因');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '窄屏无横向溢出');
    await page.screenshot({ path: path.join(SHOTS, 'live-fix-390-blocked.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.click('#meeting-skip');
    assert.equal(await page.locator('#start').isDisabled(), false);
    assert.equal(await page.locator('#start-hint').textContent(), '可以开始采集');

    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 15000 });
    await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));

    // 设计行为：只有 mic（回答方），声纹未登记 => 单麦模式不可用；页面必须说明并给出登记入口。
    await page.waitForFunction(() => !document.getElementById('q-notice')?.hidden, null, { timeout: 5000 });
    const notice = await page.locator('#q-notice').textContent();
    assert.match(notice, /要识别对方提问/);
    assert.match(notice, /声音登记/);
    assert.match(notice, /未登记时单麦模式不可用/);
    const enroll = page.locator('#q-notice a.q-enroll');
    assert.equal(await enroll.isVisible(), true, '给出“去登记”入口');
    assert.equal(await enroll.getAttribute('href'), '/setup.html?step=voice');
    assert.equal(await page.locator('#qtext').textContent(), '等待提问…');
    assert.equal(await page.locator('#lamp-meeting').getAttribute('data-need'), '1', '提问方音源缺失要醒目');
    assert.match(await page.locator('#lamp-meeting').textContent(), /提问方音源/);
    assert.match(await page.locator('#mic-gate').textContent(), /单麦模式不可用.*声纹未登记/);

    await page.waitForTimeout(4000);
    assert.equal(await page.locator('#qtext').textContent(), '等待提问…');
    assert.equal(await page.locator('#primary-body').isVisible(), false);
    const metrics = await backend.cmd({ op: 'metrics' });
    assert.deepEqual(metrics.prompt_sent, [], '只有麦克风时不产生提示（设计行为）');
    const snap = (await backend.cmd({ op: 'snapshot' })).snapshot;
    assert.equal(snap.question, null);
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/status')).json()).asr_backend), 'injected',
      '测试使用注入 ASR，不选云端 ASR');
    await page.screenshot({ path: path.join(SHOTS, 'live-fix-mic-only.png') });
    assert.deepEqual(problems, []);

    // B-3：后端退出 = /ws/audio 与 /ws/board 同时掉线，页面必须可见地提示并在重连。
    await backend.stop();
    stopped = true;
    await page.waitForFunction(() => document.getElementById('lamp-conn')?.textContent.includes('不可用'), null, { timeout: 8000 });
    await page.waitForFunction(() => /音频连接重连中/.test(document.getElementById('live-sources')?.textContent || ''), null, { timeout: 8000 });
    await page.screenshot({ path: path.join(SHOTS, 'live-fix-disconnected.png') });
  } finally {
    await page.close();
    await env.browser.close();
    if (!stopped) await backend.stop();
  }
});

test('B-2 线上：会议音频通道（合成问句）驱动真实管线，同页出现当前问题与主答案', async (t) => {
  const speech = synthSpeech();
  const env = await launch(speech.file);
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('online');
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(MEETING_MEDIA, { b64: readFileSync(speech.file).toString('base64') });
  try {
    await openLive(page, port, session);
    await authorize(page);
    await page.click('#meeting');
    await waitStep(page, 'meeting', 'done');
    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 15000 });
    await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));

    await page.waitForFunction((q) => document.getElementById('qtext')?.textContent.includes(q), QUESTION, { timeout: 30000 });
    await page.waitForSelector('#vad-cand:not([hidden])', { timeout: 30000 });
    await page.keyboard.press('Enter');  // 用户按 Enter 采用 VAD 候选
    await page.waitForSelector('#primary-body .conclusion', { timeout: 30000 });
    assert.match(await page.locator('#primary-body .conclusion').textContent(), /两阶段提交/);
    assert.equal(await page.locator('#q-notice').isVisible(), false, '会议音频已接入，不显示仅麦克风说明');
    assert.notEqual(await page.locator('#lamp-meeting').getAttribute('data-need'), '1');

    await page.waitForFunction(() => /会议音频 .* \d+ 帧/.test(document.getElementById('live-sources')?.textContent || ''));
    const sent = (await backend.cmd({ op: 'metrics' })).prompt_sent;
    assert.ok(sent.some((s) => s.level === 'L1'), '主库 L1 命中进入 prompt_sent 埋点');
    const acks = await ackAtLeast(backend, 1);
    assert.ok(acks >= 1, '同页提示板回报 display_ack');
    await page.screenshot({ path: path.join(SHOTS, 'live-fix-online-meeting.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('B-2 线下：房间麦克风（合成问句）驱动真实管线，同页出现当前问题与主答案', async (t) => {
  const speech = synthSpeech();
  const env = await launch(speech.file);
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('offline');
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  try {
    await openLive(page, port, session);
    assert.match(await page.locator('#mode').textContent(), /线下/);
    assert.equal(await page.locator('#start').isDisabled(), true);
    assert.match(await page.locator('#start-hint').textContent(), /第①步/);
    await authorize(page);
    assert.match(await page.locator('#start-hint').textContent(), /第③步.*外放测试/);
    await page.click('#speaker-test');
    await waitStep(page, 'speaker', 'done');
    assert.equal(await page.locator('#start-hint').textContent(), '可以开始采集');

    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 15000 });
    await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));

    await page.waitForFunction((q) => document.getElementById('qtext')?.textContent.includes(q), QUESTION, { timeout: 30000 });
    await page.waitForSelector('#vad-cand:not([hidden])', { timeout: 30000 });
    await page.keyboard.press('Enter');  // 用户按 Enter 采用 VAD 候选
    await page.waitForSelector('#primary-body .conclusion', { timeout: 30000 });
    assert.match(await page.locator('#primary-body .conclusion').textContent(), /两阶段提交/);
    const sent = (await backend.cmd({ op: 'metrics' })).prompt_sent;
    assert.ok(sent.some((s) => s.level === 'L1'), '线下 room 命中经真实管线进入 prompt_sent');
    await page.screenshot({ path: path.join(SHOTS, 'live-fix-offline-room.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
