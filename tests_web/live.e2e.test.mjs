// 会话页 E2E：同一页采集 + 提示板 + 状态条 + 结束会话（真实后端、脚本化提示事件、假媒体流）。
// 浏览器或 Python 不可用时整组 NOT TESTED（skip），不会假绿。
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
const OUTLINE = { conclusion: '先讲两阶段提交，再讲最终一致', points: ['2PC 强一致但阻塞', 'TCC 业务补偿'], examples: '订单支付：库存预留 + 超时回滚', cautions: '不要说“绝对不丢”' };

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

/**
 * 页面内假媒体：getUserMedia 返回 440Hz 音频（不依赖 fake device 的具体波形），
 * getDisplayMedia 返回带 browser 音频的“标签页”流。线上引导要求先授权麦克风、再加会议音频。
 */
const FAKE_MEDIA = () => {
  const md = navigator.mediaDevices;
  const origGum = md.getUserMedia.bind(md);
  const tone = async () => {
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0.2;
    osc.frequency.value = 440;
    osc.connect(gain).connect(dest);
    osc.start();
    window.__toneCtx = ctx;
    return dest.stream;
  };
  Object.defineProperty(md, 'getUserMedia', {
    configurable: true,
    value: async (constraints) => {
      if (constraints && constraints.audio && !constraints.video) {
        window.__micConstraints = constraints;
        return tone();
      }
      return origGum(constraints);
    },
  });
  Object.defineProperty(md, 'getDisplayMedia', {
    configurable: true,
    value: async () => {
      const audio = await tone();
      const canvas = document.createElement('canvas');
      canvas.width = 16; canvas.height = 16;
      const video = canvas.captureStream(1).getVideoTracks()[0];
      Object.defineProperty(video, 'getSettings', { configurable: true, value: () => ({ displaySurface: 'browser' }) });
      return new MediaStream([...audio.getAudioTracks(), video]);
    },
  });
};

async function prepareSources(page) {
  await page.check('#consent');
  await page.click('#authorize');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="authorize"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
  await page.click('#meeting');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="meeting"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
}

async function startCapture(page) {
  await prepareSources(page);
  await page.click('#start');
  await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
  await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 10000 });
  await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));
}

async function ackAtLeast(backend, n) {
  for (let i = 0; i < 60 && (await backend.cmd({ op: 'count_acks' })).acks < n; i++) await new Promise((r) => setTimeout(r, 50));
  return (await backend.cmd({ op: 'count_acks' })).acks;
}

test('live page: capture + board + status + end + export in one page', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA);
  try {
    await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
    await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
    assert.equal(await page.locator('#live-state').textContent(), '准备中');
    assert.match(await page.locator('#mode').textContent(), /线上/);
    await page.waitForFunction(() => !document.getElementById('live-outbound')?.hidden);
    assert.match(await page.locator('#live-outbound').textContent(), /本场外发：音频→云转写 开 · 片段→DeepSeek 开 · 联网 开/);
    assert.equal(await page.locator('#start').isDisabled(), true, '引导未完成前不能开始采集');
    assert.equal(await page.locator('#authorize').isVisible(), true);

    await startCapture(page);
    assert.equal(await page.locator('#live-state').textContent(), '采集中');
    assert.match(await page.locator('#live-elapsed').textContent(), /^\d+:\d{2}(:\d{2})?$/);

    // 同页提示板：脚本化事件 -> 渲染 -> display_ack
    await backend.cmd({ op: 'turn', type: 'candidate', text: '你能讲讲分布式事务' });
    await page.waitForFunction(() => document.getElementById('state').textContent === '候选');
    await backend.cmd({ op: 'turn', type: 'committed', text: '你能讲讲分布式事务的区别吗' });
    await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('分布式事务的区别'));
    await backend.cmd({ op: 'publish', method: 'publish_prompt_primary', args: ['t1', 1, OUTLINE, 'L1', '主库'] });
    await page.waitForSelector('#primary-body .conclusion');
    assert.equal(await ackAtLeast(backend, 1), 1, '同页提示板回报 display_ack');
    await page.waitForTimeout(1200);
    assert.match(await page.locator('#live-sources').textContent(), /麦克风 .* \d+ 帧/);
    await page.screenshot({ path: path.join(SHOTS, 'live-1440-capturing.png') });

    // 开始采集 1.5 秒后页面会再取一次会话信息；等它过去再结束，否则它会掩盖“结束后计时仍在走”
    await page.waitForTimeout(1600);
    // 结束会话：二次确认 -> 结束后进入只读已结束态
    await page.click('#end');
    assert.equal(await page.locator('#end-modal').isVisible(), true);
    await page.click('#end-confirm');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '已结束');
    const frozenElapsed = await page.locator('#live-elapsed').textContent();
    await page.waitForTimeout(2200);
    assert.equal(await page.locator('#live-elapsed').textContent(), frozenElapsed, '结束后计时立即停止，不再增长');
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session), null);
    // 结束路径必须落 stopped_at：记录库才能显示真实时长
    const ended = await page.evaluate(async (sid) => (await (await fetch('/api/sessions')).json()).sessions.find((s) => s.id === sid), session);
    assert.ok(ended && typeof ended.stopped_at === 'number', '结束会话写入 stopped_at');
    assert.ok(typeof ended.duration_s === 'number' && ended.duration_s >= 0, '记录库可算出时长');
    assert.equal(await page.locator('#start').isDisabled(), true);
    assert.equal(await page.locator('#meeting').isDisabled(), true);
    assert.equal(await page.locator('#end').isDisabled(), true);
    assert.equal(await page.locator('#ended-card').isVisible(), true);
    assert.equal(await page.locator('#next-session').getAttribute('href'), '/setup.html');
    assert.match(await page.locator('#primary-body .conclusion').textContent(), /两阶段提交/, '结束时保留提示板最终状态');

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.click('#export-record'),
    ]);
    assert.match(download.suggestedFilename(), /^面试记录-.*\.md$/);
    await page.screenshot({ path: path.join(SHOTS, 'live-1440-ended.png') });

    // 刷新已结束的会话：仍是已结束态，不报错
    await page.reload();
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '已结束');
    assert.equal(await page.locator('#ended-card').isVisible(), true);
    assert.equal(await page.locator('#start').isDisabled(), true);

    // 无会话：引导回 setup，不创建旧会话
    await page.goto(`http://127.0.0.1:${port}/live.html`);
    await page.waitForFunction(() => !document.getElementById('no-session').hidden);
    assert.match(await page.locator('#no-session').innerText(), /会前配置向导/);
    assert.equal(await page.locator('#no-session a[href="/setup.html"]').count(), 1);
    assert.equal(await page.locator('#start').isDisabled(), true);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('live page narrow viewport has no horizontal overflow', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA);
  try {
    await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
    await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
    await startCapture(page);
    await backend.cmd({ op: 'turn', type: 'candidate', text: '你能讲讲分布式事务' });
    await backend.cmd({ op: 'turn', type: 'committed', text: '你能讲讲分布式事务的区别吗' });
    await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('分布式事务的区别'));
    await backend.cmd({ op: 'publish', method: 'publish_prompt_primary', args: ['t1', 1, OUTLINE, 'L1', '主库'] });
    await page.waitForSelector('#primary-body .conclusion');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '窄屏无横向溢出');
    await page.screenshot({ path: path.join(SHOTS, 'live-390-capturing.png'), fullPage: true });
    await page.click('#end');
    await page.click('#end-confirm');
    await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '已结束');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '窄屏已结束态无横向溢出');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
