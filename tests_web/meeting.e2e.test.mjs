// 会议音频两种接入方式 E2E：假媒体流（Chromium fake device）+ 页面内 stub getDisplayMedia/getUserMedia。
// 后端为 live_e2e_server.py（真实 uvicorn，临时数据目录）；浏览器或 Python 不可用时整组 NOT TESTED（skip）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = path.join(ROOT, 'eval', 'screens');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try {
    return { browser: await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--disable-audio-output'] }) };
  } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'live_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watchProblems(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

/**
 * 页面内假媒体：
 * - enumerateDevices 追加 BlackHole 2ch；
 * - getUserMedia 精确选 blackhole-2ch 时返回带 440 Hz 的音频流，并记录约束；
 * - getDisplayMedia 返回带指定 displaySurface 的“视频轨 + 音频流”（silent 时音轨全零）。
 */
const FAKE_MEDIA = ({ surface, silent }) => {
  const md = navigator.mediaDevices;
  const origEnumerate = md.enumerateDevices.bind(md);
  Object.defineProperty(md, 'enumerateDevices', {
    configurable: true,
    value: async () => [...await origEnumerate(), {
      kind: 'audioinput', deviceId: 'blackhole-2ch', label: 'BlackHole 2ch', groupId: 'virtual', toJSON() { return this; },
    }],
  });
  const tone = async (gainValue) => {
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = gainValue;
    osc.frequency.value = 440;
    osc.connect(gain).connect(dest);
    osc.start();
    window.__meetingCtx = ctx;
    return dest.stream;
  };
  const origGum = md.getUserMedia.bind(md);
  Object.defineProperty(md, 'getUserMedia', {
    configurable: true,
    value: async (constraints) => {
      const exact = constraints?.audio?.deviceId?.exact;
      if (exact === 'blackhole-2ch') {
        window.__meetingConstraints = constraints;
        return tone(0.2);
      }
      return origGum(constraints);
    },
  });
  Object.defineProperty(md, 'getDisplayMedia', {
    configurable: true,
    value: async () => {
      const audio = await tone(silent ? 0 : 0.2);
      const canvas = document.createElement('canvas');
      canvas.width = 16;
      canvas.height = 16;
      const video = canvas.captureStream(1).getVideoTracks()[0];
      const settings = { displaySurface: surface, width: 16, height: 16 };
      Object.defineProperty(video, 'getSettings', { configurable: true, value: () => settings });
      return new MediaStream([...audio.getAudioTracks(), video]);
    },
  });
};

async function authorizeMic(page) {
  await page.check('#consent');
  await page.click('#authorize');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="authorize"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
}

/** 引导要求先加会议音频、再开始采集；开始后只验证帧流。 */
async function startCapture(page) {
  await authorizeMic(page);
  await page.click('#meeting');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="meeting"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
  await page.click('#start');
  await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
  await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 10000 });
}

async function openLive(page, port, session) {
  await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
  await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
}

test('第二输入设备：选择器排除麦克风所选设备，meeting 通道产生帧且关闭人声处理', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA, { surface: 'browser', silent: false });
  try {
    await openLive(page, port, session);
    await page.waitForFunction(() => document.querySelectorAll('#device option').length > 1);
    const micId = await page.$eval('#device', (s) => [...s.options].find((o) => o.value)?.value);
    assert.ok(micId, 'fake Chromium 提供至少一个音频输入设备');

    await page.selectOption('#device', micId);
    await page.waitForFunction(() => [...document.querySelectorAll('#meeting-device option')].some((o) => o.value === 'blackhole-2ch'));
    const options = await page.$$eval('#meeting-device option', (os) => os.map((o) => ({ value: o.value, text: o.textContent })));
    assert.equal(options.some((o) => o.value === micId), false, '会议设备列表排除已被麦克风选中的设备');
    assert.match(options.find((o) => o.value === 'blackhole-2ch').text, /BlackHole 2ch（虚拟声卡）/);

    await authorizeMic(page);
    await page.selectOption('#meeting-mode', 'device');
    assert.equal(await page.locator('#meeting-device').isVisible(), true, '选虚拟声卡时显示设备选择器');
    await page.selectOption('#meeting-device', 'blackhole-2ch');
    await page.click('#meeting');
    await page.waitForFunction(() => document.querySelector('.g-step[data-step="meeting"]')?.dataset.phase === 'done', null, { timeout: 10000 });
    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    await page.waitForFunction(() => /meeting [1-9]\d*/.test(document.getElementById('stats')?.textContent || ''), null, { timeout: 10000 });

    const constraints = await page.evaluate(() => window.__meetingConstraints);
    assert.deepEqual(constraints.audio.deviceId, { exact: 'blackhole-2ch' });
    assert.equal(constraints.audio.echoCancellation, false);
    assert.equal(constraints.audio.noiseSuppression, false);
    assert.equal(constraints.audio.autoGainControl, false);
    assert.equal(constraints.video, undefined);

    await page.waitForFunction(() => document.getElementById('state-meeting')?.textContent === '正常', null, { timeout: 5000 });
    await page.click('#capture-panel > summary');
    await page.click('#meeting-help summary');
    await page.click('#meeting-trouble summary');
    assert.equal(await page.locator('#meeting-trouble ol li').count(), 3);
    await page.screenshot({ path: path.join(SHOTS, 'live-meeting-choice.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('displaySurface=monitor：立即提示整屏/窗口在 macOS 上通常无声音，仍允许继续', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA, { surface: 'monitor', silent: false });
  try {
    await openLive(page, port, session);
    await startCapture(page);
    await page.waitForFunction(() => /整个屏幕\/窗口/.test(document.getElementById('capture-msg')?.textContent || ''), null, { timeout: 10000 });
    assert.match(await page.locator('#capture-msg').textContent(), /Chrome 标签页/);
    assert.match(await page.locator('#capture-msg').textContent(), /虚拟声卡/);
    assert.match(await page.locator('#state-meeting').textContent(), /通常没有声音/);
    assert.equal(await page.locator('#meeting').isDisabled(), true, '提示后仍继续采集（按钮已置灰）');
    await page.screenshot({ path: path.join(SHOTS, 'live-meeting-monitor-warning.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('会议音轨全零：3 秒后状态与状态条显示“会议音频无声音”，窄屏无横向溢出', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA, { surface: 'browser', silent: true });
  const narrow = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const narrowProblems = watchProblems(narrow);
  await narrow.addInitScript(FAKE_MEDIA, { surface: 'browser', silent: true });
  try {
    await openLive(page, port, session);
    await startCapture(page);
    await page.waitForFunction(() => document.getElementById('state-meeting')?.textContent === '会议音频无声音', null, { timeout: 8000 });
    await page.waitForFunction(() => /会议音频无声音/.test(document.getElementById('live-sources')?.textContent || ''), null, { timeout: 3000 });
    await page.click('#capture-panel > summary');
    assert.match(await page.locator('#live-sources').textContent(), /会议音频无声音/);
    assert.equal(await page.locator('#live-sources').getAttribute('data-alert'), 'meeting');
    assert.match(await page.locator('#capture-msg').textContent(), /会议音频无声音/);
    const tips = await page.locator('#meeting-trouble ol li').allTextContents();
    assert.equal(tips.length, 3);
    assert.match(tips[0], /标签页/);
    assert.match(tips[1], /多输出设备/);
    await page.click('#meeting-trouble summary');
    await page.screenshot({ path: path.join(SHOTS, 'live-meeting-silent.png') });

    await openLive(narrow, port, session);
    await startCapture(narrow);
    await narrow.click('#capture-panel > summary');
    await narrow.click('#meeting-help summary');
    await narrow.click('#meeting-trouble summary');
    assert.ok(await narrow.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '窄屏无横向溢出');
    assert.deepEqual(problems, []);
    assert.deepEqual(narrowProblems, []);
  } finally {
    await page.close();
    await narrow.close();
    await env.browser.close();
    await backend.stop();
  }
});
