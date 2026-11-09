// 全站顶栏 E2E：11 个页面注入、当前页高亮、更多下拉键盘、会话状态、窄屏汉堡、
// 会话页采集中的离开确认与 beforeunload、无横向溢出与布局回归。
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
  try { return { browser: await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--disable-audio-output', '--use-fake-ui-for-media-stream'] }) } } catch (e) {
    return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] };
  }
}

function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'nav_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
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
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

const PAGES = [
  ['/home.html', 'workspace', null, 'header.top'],
  ['/records.html', 'records', null, 'header.top'],
  ['/settings.html', 'settings', null, 'header.top'],
  ['/import.html', 'workspace', 'import', 'header.top'],
  ['/review.html', 'workspace', 'review', 'header.top'],
  ['/secondary.html', 'workspace', 'secondary', 'header.top'],
  ['/annotate.html', 'workspace', 'annotate', 'header.top'],
  ['/setup.html', null, 'setup', 'header.top'],
  ['/setup.html?step=voice', null, 'voice', 'header.top'],
  ['/live.html', null, 'live', '.live-bar'],
  ['/board.html', null, 'live', '.board > .bar'],
  ['/capture.html', null, 'live', 'header'],
];

async function waitNoSession(page) {
  await page.waitForFunction(() => document.querySelector('[data-gnav-live-tag]')?.textContent === '无进行中');
}

async function navCurrent(page) {
  return page.evaluate(() => ({
    top: Array.from(document.querySelectorAll('.gnav-pill [data-gnav-top][aria-current="page"]')).map((a) => a.dataset.gnavTop),
    more: Array.from(document.querySelectorAll('.gnav-more-menu [data-gnav-more][aria-current="page"]')).map((a) => a.dataset.gnavMore),
  }));
}

/** 页内假媒体：440Hz 音频 + 带音频的“标签页”共享流（与 live.e2e 相同思路）。 */
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
      if (constraints && constraints.audio && !constraints.video) return tone();
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

async function startCapture(page) {
  await page.check('#consent');
  await page.click('#authorize');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="authorize"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
  await page.click('#meeting');
  await page.waitForFunction(() => document.querySelector('.g-step[data-step="meeting"]')?.dataset.phase === 'done',
    null, { timeout: 10000 });
  await page.click('#start');
  await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
  await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 10000 });
}

const frames = async (page) => {
  const text = await page.locator('#stats').textContent();
  const m = /mic (\d+)/.exec(text || '');
  return m ? Number(m[1]) : -1;
};

test('nav: 11 个页面都有顶栏、高亮正确、无横向溢出与布局回归', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  try {
    // 首页不注入（已有自己的顶栏），11 个工具页都注入且当前项正确
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    assert.equal(await page.locator('#gnav').count(), 0, '首页不重复注入顶栏');

    for (const [url, top, more, contentSel] of PAGES) {
      await page.goto(base + url, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#gnav');
      await waitNoSession(page);
      const current = await navCurrent(page);
      assert.deepEqual(current.top, top ? [top] : [], `${url} 顶栏高亮`);
      assert.deepEqual(current.more, more ? [more] : [], `${url} 更多高亮`);
      assert.equal(await page.evaluate(() => document.body.firstElementChild?.id), 'gnav', `${url} 顶栏是 body 第一子元素`);

      const layout = await page.evaluate((sel) => {
        const nav = document.getElementById('gnav').getBoundingClientRect();
        const el = document.querySelector(sel);
        const rect = el?.getBoundingClientRect();
        return {
          navTop: nav.top,
          navH: nav.height,
          contentTop: rect ? rect.top : null,
          contentVisible: !!rect && rect.width > 0 && rect.height > 0,
          overflow: document.documentElement.scrollWidth - window.innerWidth,
        };
      }, contentSel);
      assert.equal(layout.navTop, 0, `${url} 顶栏吸顶`);
      assert.ok(layout.navH >= 40 && layout.navH <= 48, `${url} 紧凑高度 ${layout.navH}`);
      assert.equal(layout.contentVisible, true, `${url} 页面原有头部可见`);
      assert.ok(layout.contentTop >= layout.navH - 1, `${url} 页面内容未被顶栏盖住`);
      assert.ok(layout.overflow <= 0, `${url} 无横向溢出（${layout.overflow}px）`);
    }

    // 导航目标页都能 200
    for (const href of ['/', '/home.html', '/records.html', '/settings.html', '/import.html', '/review.html',
      '/secondary.html', '/annotate.html', '/setup.html?step=voice', '/setup.html', '/live.html']) {
      const res = await page.request.get(base + href);
      assert.equal(res.status(), 200, href);
    }

    // “更多”下拉：键盘开合、方向键、Esc 归还焦点
    await page.goto(`${base}/settings.html`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#gnav');
    await page.focus('#gnav-more-btn');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#gnav-more-btn').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#gnav-more-menu').isVisible(), true);
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.gnavMore), 'import');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#gnav-more-btn').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#gnav-more-menu').isVisible(), false);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'gnav-more-btn');

    await page.click('#gnav-more-btn');
    await page.waitForSelector('#gnav-more-menu:visible');
    await page.screenshot({ path: path.join(SHOTS, 'nav-settings-more.png') });
    await page.keyboard.press('Escape');

    // 无会话：会话页项灰显并指向开始会话，不出现推广的第 5 项
    assert.equal(await page.locator('#gnav-live-item').count(), 0);
    assert.equal(await page.locator('#gnav-more-menu [data-gnav-more="live"]').getAttribute('href'), '/setup.html');
    assert.equal(await page.locator('#gnav-more-menu [data-gnav-more="live"]').evaluate((a) => a.classList.contains('is-off')), true);

    // 有进行中会话：第 5 个可见项“会话”带状态点，直达该会话
    await backend.cmd({ op: 'start' });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#gnav');
    await page.waitForFunction(() => document.querySelector('#gnav-live-item')?.getAttribute('href')?.includes('live.html'));
    assert.equal(await page.locator('#gnav-live-item').isVisible(), true);
    assert.equal(await page.locator('#gnav-live-item .gnav-dot').count(), 1);
    assert.equal(await page.locator('#gnav-live-item').getAttribute('href'), `/live.html?session=${session}`);
    assert.equal(await page.locator('#gnav-more-menu [data-gnav-more="live"]').getAttribute('href'), `/live.html?session=${session}`);
    assert.equal(await page.locator('#gnav-more-menu [data-gnav-more="live"] .gnav-tag').textContent(), '进行中');
    assert.equal(await page.locator('#gnav-more-menu [data-gnav-more="live"]').evaluate((a) => a.classList.contains('is-off')), false);

    // 工作台页顶栏截图
    await page.goto(`${base}/home.html`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#gnav');
    await page.screenshot({ path: path.join(SHOTS, 'nav-workspace.png'), fullPage: true });

    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('nav: 会话页采集中的离开确认与 beforeunload', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  await page.addInitScript(FAKE_MEDIA);
  try {
    await backend.cmd({ op: 'start' });
    await page.goto(`${base}/live.html?session=${session}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
    await page.waitForSelector('#gnav');

    // 准备中：beforeunload 不拦截，点击导航直接跳转
    assert.equal(await page.evaluate(() => {
      const e = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    }), false);
    await Promise.all([page.waitForURL('**/records.html'), page.click('.gnav-pill [data-gnav-top="records"]')]);
    assert.equal(await page.locator('#gnav-confirm').isVisible(), false);

    // 回到会话页并开始采集
    await page.goto(`${base}/live.html?session=${session}`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
    await startCapture(page);
    const before = await frames(page);
    assert.ok(before >= 0);

    // 采集中：beforeunload 必须拦截
    assert.equal(await page.evaluate(() => {
      const e = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    }), true);

    // 点击导航：弹确认；Esc 取消并归还焦点，采集不中断
    await page.click('.gnav-pill [data-gnav-top="workspace"]');
    assert.equal(await page.locator('#gnav-confirm').isVisible(), true);
    assert.match(await page.locator('#gnav-confirm-text').textContent(), /中断本页的音频采集/);
    assert.match(await page.locator('#gnav-confirm-text').textContent(), /已中断的采集不会自动恢复/);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'gnav-confirm-cancel');
    assert.equal(new URL(page.url()).pathname, '/live.html');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#gnav-confirm').isVisible(), false);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.gnavTop), 'workspace');
    assert.equal(new URL(page.url()).pathname, '/live.html');

    // 再弹一次，截图后取消；帧计数继续增长
    await page.click('.gnav-pill [data-gnav-top="workspace"]');
    await page.waitForSelector('#gnav-confirm:visible');
    await page.screenshot({ path: path.join(SHOTS, 'nav-live-confirm.png') });
    await page.waitForTimeout(1400);
    await page.click('#gnav-confirm-cancel');
    const after = await frames(page);
    assert.ok(after > before, `取消后采集继续（${before} -> ${after}）`);
    assert.equal(await page.locator('#live-state').textContent(), '采集中');

    // 确认离开：跳转成功，后端会话仍 active
    await page.click('.gnav-pill [data-gnav-top="workspace"]');
    await Promise.all([page.waitForURL('**/home.html'), page.click('#gnav-confirm-ok')]);
    const active = await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session?.id);
    assert.equal(active, session, '离开后会话仍保持进行中');

    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('nav: 窄屏汉堡菜单开合、焦点与 Esc', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = watchProblems(page);
  try {
    await page.goto(`${base}/settings.html`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#gnav');
    await waitNoSession(page);
    assert.equal(await page.locator('.gnav-pill').isVisible(), false);
    assert.equal(await page.locator('#gnav-burger').isVisible(), true);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '窄屏无横向溢出');

    await page.click('#gnav-burger');
    assert.equal(await page.locator('#gnav-burger').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#gnav-overlay').isVisible(), true);
    assert.equal(await page.locator('.gnav-sheet a[data-gnav-link]').count(), 11, '菜单列出全部 4 + 7 项');
    await page.waitForFunction(() => document.activeElement?.dataset.gnavMobileTop === 'home');

    await page.screenshot({ path: path.join(SHOTS, 'nav-mobile-menu.png') });

    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#gnav-burger').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#gnav-overlay').isVisible(), false);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'gnav-burger');

    // 点遮罩关闭
    await page.click('#gnav-burger');
    await page.mouse.click(2, 800);
    assert.equal(await page.locator('#gnav-overlay').isVisible(), false);

    // 放大到桌面宽度自动关闭
    await page.click('#gnav-burger');
    await page.setViewportSize({ width: 1000, height: 800 });
    await page.waitForFunction(() => document.getElementById('gnav-overlay').hidden);
    assert.equal(await page.locator('#gnav-overlay').isVisible(), false);
    assert.equal(await page.locator('.gnav-pill').isVisible(), true);

    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
