// 线下同室（单麦外放）E2E：一路麦克风、room 帧、引导条推进、拒绝授权指引、窄屏；
// 以及 setup 的声音登记成功/质量不足/删除、自检 voice_enrolled FAIL 的“去登记”入口。
// 后端为真实 uvicorn（临时数据目录、FakeExtractor），浏览器或 Python 不可用时整组 NOT TESTED（skip）。
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

function startBackend(script, args = []) {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', script), ...args], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const next = async () => JSON.parse((await lines.next()).value);
  return {
    next,
    async cmd(c) { child.stdin.write(JSON.stringify(c) + '\n'); return next(); },
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watchProblems(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

/** 假麦克风：返回 440Hz 音频，并记录 getUserMedia 约束；线下页面不应调用 getDisplayMedia。 */
const FAKE_MEDIA = () => {
  const md = navigator.mediaDevices;
  const orig = md.getUserMedia.bind(md);
  const tone = async () => {
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0.1;
    osc.frequency.value = 440;
    // 幅度调制：有声/接近静音交替。登记质量检查用 10 分位噪声底，恒定音会因“处处是语音”被判 0 秒。
    const lfo = ctx.createOscillator();
    const depth = ctx.createGain();
    lfo.frequency.value = 2;
    depth.gain.value = 0.09;
    lfo.connect(depth).connect(gain.gain);
    lfo.start();
    osc.connect(gain).connect(dest);
    osc.start();
    window.__toneCtx = ctx;
    return dest.stream;
  };
  Object.defineProperty(md, 'getUserMedia', {
    configurable: true,
    value: async (constraints) => {
      window.__micConstraints = constraints;
      window.__gumCalls = (window.__gumCalls || 0) + 1;
      if (constraints && constraints.video) return orig(constraints);
      return tone();
    },
  });
  Object.defineProperty(md, 'getDisplayMedia', {
    configurable: true,
    value: async () => { window.__displayCalls = (window.__displayCalls || 0) + 1; throw new Error('线下模式不应共享会议'); },
  });
};

/** 登记用假麦克风：220Hz 有声块 + 150ms 停顿，模拟正常朗读（约 87% 有声，能通过有效语音与 SNR）。 */
const FAKE_ENROLL_MEDIA = () => {
  const md = navigator.mediaDevices;
  const orig = md.getUserMedia.bind(md);
  Object.defineProperty(md, 'getUserMedia', {
    configurable: true,
    value: async (constraints) => {
      window.__micConstraints = constraints;
      if (constraints && constraints.video) return orig(constraints);
      const ctx = new AudioContext();
      await ctx.resume();
      const dest = ctx.createMediaStreamDestination();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = 220;
      gain.gain.value = 0.15;
      osc.connect(gain).connect(dest);
      osc.start();
      window.__enrollTimer = setInterval(() => {
        gain.gain.value = 0.002;
        setTimeout(() => { gain.gain.value = 0.15; }, 150);
      }, 1200);
      window.__toneCtx = ctx;
      return dest.stream;
    },
  });
};

const DENY_MEDIA = () => {
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
    configurable: true,
    value: async () => { throw Object.assign(new Error('permission denied'), { name: 'NotAllowedError' }); },
  });
};

const waitStep = (page, id, phase) => page.waitForFunction(
  ({ id, phase }) => document.querySelector(`.g-step[data-step="${id}"]`)?.dataset.phase === phase,
  { id, phase }, { timeout: 10000 },
);

async function openLive(page, port, session) {
  await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
  await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
}

test('线下会话：只有一路麦克风，room 帧入库；引导条按同意/授权/外放测试推进', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('live_e2e_server.py', ['--offline']);
  const { port, session, mode } = await backend.next();
  assert.equal(mode, 'offline');
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA);
  try {
    await openLive(page, port, session);
    assert.match(await page.locator('#mode').textContent(), /线下/);
    await waitStep(page, 'choose', 'active');
    assert.equal(await page.locator('.g-step[data-step="speaker"]').isVisible(), true);
    assert.equal(await page.locator('.g-step[data-step="meeting"]').isVisible(), false, '线下不显示会议音频步骤');
    assert.equal(await page.locator('#meeting-mode').isVisible(), false);
    assert.match(await page.locator('#choose-hint').textContent(), /一路麦克风/);
    assert.equal(await page.locator('#start').isDisabled(), true, '未完成引导不能开始');

    await page.check('#consent');
    await waitStep(page, 'authorize', 'active');
    await page.click('#authorize');
    await waitStep(page, 'authorize', 'done');
    const constraints = await page.evaluate(() => window.__micConstraints);
    assert.equal(constraints.audio.echoCancellation, false, '线下关闭回声消除，避免吃掉外放的对方声音');
    assert.equal(constraints.audio.noiseSuppression, false);
    assert.equal(constraints.audio.autoGainControl, false);
    assert.match(await page.locator('#auth-hint').textContent(), /已检测到人声|麦克风正常/);
    assert.match(await page.locator('#speaker-hint').textContent(), /声纹/);
    await page.screenshot({ path: path.join(SHOTS, 'live-offline-guide.png') });

    await page.click('#speaker-test');
    await waitStep(page, 'speaker', 'done');
    assert.equal(await page.locator('#start').isDisabled(), false, '全部就绪后开始采集可用');
    await page.screenshot({ path: path.join(SHOTS, 'live-offline-ready.png') });

    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    await page.waitForFunction(() => /房间麦克风 正常 \d+ 帧/.test(document.getElementById('live-sources')?.textContent || ''),
      null, { timeout: 12000 });
    const summary = await page.locator('#live-sources').textContent();
    assert.match(summary, /房间麦克风/);
    assert.doesNotMatch(summary, /会议音频/);
    assert.match(summary, /^房间麦克风/);
    assert.equal(await page.locator('#capture-panel').evaluate((d) => d.open), false, '采集开始后引导收起为一行摘要');
    await page.screenshot({ path: path.join(SHOTS, 'live-offline-capturing.png') });

    await page.click('#end');
    await page.click('#end-confirm');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '已结束');
    const streams = (await backend.cmd({ op: 'streams' })).streams;
    assert.deepEqual(streams.map((s) => s.source), ['room'], '只发 room 一路');
    assert.ok(streams[0].frames > 0, 'room 帧已入库');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('拒绝麦克风授权：就地给出 macOS 系统设置路径，开始按钮保持禁用', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend('live_e2e_server.py', ['--offline']);
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(DENY_MEDIA);
  try {
    await openLive(page, port, session);
    await page.check('#consent');
    await page.click('#authorize');
    await waitStep(page, 'authorize', 'attention');
    assert.equal(await page.locator('#mic-help').isVisible(), true);
    const help = await page.locator('#mic-help').textContent();
    assert.match(help, /系统设置/);
    assert.match(help, /隐私与安全性/);
    assert.match(help, /Chrome/);
    assert.match(await page.locator('#authorize').textContent(), /重新授权/);
    assert.equal(await page.locator('#start').isDisabled(), true);
    await page.screenshot({ path: path.join(SHOTS, 'live-offline-denied.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('线下引导与采集窄屏无横向溢出', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend('live_e2e_server.py', ['--offline']);
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA);
  try {
    await openLive(page, port, session);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '引导条窄屏无横向溢出');
    await page.check('#consent');
    await page.click('#authorize');
    await waitStep(page, 'authorize', 'done');
    await page.click('#speaker-test');
    await waitStep(page, 'speaker', 'done');
    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '采集态窄屏无横向溢出');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

// ---------- setup 声音登记 ----------
async function gotoVoiceStep(page) {
  await page.click('#steps .card:nth-child(2)'); // 线下同室
  await page.click('#next');
  await page.selectOption('#steps select.field', { index: 1 });
  await page.click('#next');
  await page.click('#steps .chip');
  await page.click('#next');
  if (!(await page.locator('#steps .opt.on').count())) await page.click('#steps .opt:not(.off)');
  await page.click('#next');
  await page.waitForSelector('#voice-record');
}

test('声音登记：连续录音 开始/停止 -> 真实后端质量不足文案 -> 重录成功 -> 删除', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('setup_e2e_server.py');
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  await page.addInitScript(FAKE_ENROLL_MEDIA);
  try {
    await page.goto(`http://127.0.0.1:${port}/setup.html`);
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    await gotoVoiceStep(page);
    const notice = await page.locator('.voice-notice').textContent();
    assert.match(notice, /本机/);
    assert.match(notice, /不外发/);
    assert.match(notice, /删除/);
    assert.match(await page.locator('.enroll-text').textContent(), /今天我来参加这次面试/);
    const hint = await page.locator('#steps .step.cur').innerText();
    assert.match(hint, /开始录音/);
    assert.match(hint, /停止录音/);
    assert.match(hint, /15 秒/);
    assert.doesNotMatch(hint, /3 段|第 \d+ 段/, '不再出现分段录音文案');
    assert.equal(await page.locator('#voice-record').textContent(), '开始录音');
    assert.equal(await page.locator('#voice-submit').isDisabled(), true, '没录音不能提交');

    // 短录音：由真实后端 check_enrollment 判定有效语音不足，前端展示秒数与 15 秒指引。
    await page.click('#voice-record');
    await page.waitForFunction(() => document.getElementById('voice-record')?.textContent === '停止录音');
    assert.equal(await page.locator('#voice-record').evaluate((b) => b.classList.contains('recording')), true,
      '录音中按钮醒目');
    await page.waitForTimeout(1700);
    await page.click('#voice-record');
    await page.waitForFunction(() => /^已录 [\d.]+ 秒$/.test(document.getElementById('voice-progress')?.textContent || ''));
    assert.equal(await page.locator('#voice-record').textContent(), '重新录制');
    assert.equal(await page.locator('#voice-submit').isDisabled(), false, '已录 ≥1 秒即可提交');
    await page.click('#voice-submit');
    await page.waitForFunction(() => /有效语音/.test(document.getElementById('voice-error')?.textContent || ''));
    const error = await page.locator('#voice-error').textContent();
    assert.match(error, /有效语音 [\d.]+ 秒/);
    assert.match(error, /15 秒/);
    assert.match(await page.locator('#voice-status').textContent(), /未登记/);
    await page.screenshot({ path: path.join(SHOTS, 'enroll-continuous-too-short.png') });

    // 连续录到 ≥12 秒再停止，走同一路径提交；成功与否完全由真实后端判定。
    await page.click('#voice-record');
    await page.waitForFunction(() => document.getElementById('voice-record')?.textContent === '停止录音');
    await page.waitForFunction(() => {
      const m = document.getElementById('voice-progress')?.textContent.match(/录制中 (\d+):(\d+)/);
      return !!m && (+m[1] * 60 + +m[2]) >= 12;
    }, null, { timeout: 25000 });
    await page.screenshot({ path: path.join(SHOTS, 'enroll-continuous-recording.png') });
    await page.click('#voice-record');
    await page.waitForFunction(() => {
      const m = document.getElementById('voice-progress')?.textContent.match(/^已录 ([\d.]+) 秒$/);
      return !!m && parseFloat(m[1]) >= 12;
    }, null, { timeout: 10000 });
    await page.click('#voice-submit');
    await page.waitForFunction(() => /已登记/.test(document.getElementById('voice-status')?.textContent || ''), null, { timeout: 10000 });
    const status = await page.locator('#voice-status').textContent();
    assert.match(status, /有效语音/);
    assert.match(await page.locator('#msg').textContent(), /登记成功/);

    await page.click('#voice-delete');
    await page.waitForFunction(() => document.getElementById('voice-status')?.textContent === '未登记');
    const profile = await page.evaluate(async () => {
      const st = await (await fetch('/api/status')).json();
      return (await fetch('/api/voice/profile', { headers: { 'X-Local-Token': st.token } })).json();
    });
    assert.equal(profile.enrolled, false, '删除后本机不再保留声纹');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('自检 voice_enrolled 未登记：给出“去登记”入口，不是死胡同', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend('setup_e2e_server.py', ['--voice-missing']);
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  try {
    await page.goto(`http://127.0.0.1:${port}/setup.html`);
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    await gotoVoiceStep(page);
    // 未登记不是死胡同：可显式降级继续。
    await page.check('#steps input[type=checkbox]:last-of-type');
    await page.click('#next');
    await page.locator('#steps input[type=checkbox]').nth(0).check();
    await page.locator('#steps input[type=checkbox]').nth(1).check();
    await page.click('#next');
    await page.waitForFunction(() => [...document.querySelectorAll('#check-list .ck')]
      .some((li) => li.textContent.includes('声音登记') && li.classList.contains('FAIL')));
    assert.equal(await page.locator('#go-voice').isVisible(), true);
    await page.click('#go-voice');
    await page.waitForSelector('#voice-record');
    assert.match(await page.locator('#steps .step.cur .step-title').textContent(), /声音登记/);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
