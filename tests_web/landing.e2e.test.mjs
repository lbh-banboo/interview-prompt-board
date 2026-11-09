// 欢迎首页 E2E：/ 返回 200 且是新首页（不再 302）、统计数字等于真实计数、
// 接口失败显示 —、汉堡菜单、reduced-motion、单屏与溢出、无 console/pageerror。
// 浏览器/Python 不可用则 skip（NOT TESTED）。
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
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'landing_e2e_server.py')],
    { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('quit\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watchProblems(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

test('landing E2E 1440x900：新首页、真实统计、最近会话、单屏', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const info = await backend.next();
  const base = `http://127.0.0.1:${info.port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  try {
    const res = await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    assert.equal(res.status(), 200, '根路径直接 200');
    assert.equal(new URL(page.url()).pathname, '/', '不再 302 跳 /home.html');
    assert.match(await page.title(), /面试提示板/);
    assert.equal(await page.locator('h1').innerText(), 'Interview Copilot\n面试提词，现场可用');

    // 统计数字滚动后等于真实计数（主库/次优库/会话为数字，转写方式为状态文案）
    const c = info.counts;
    for (const [key, expected] of [['primary', String(c.primary)], ['secondary', String(c.secondary)],
      ['sessions', String(c.sessions)], ['asr', c.asr]]) {
      await page.waitForFunction(
        ([k, want]) => document.querySelector(`[data-stat="${k}"]`)?.textContent === want,
        [key, expected], { timeout: 8000 },
      );
    }

    // 最近会话：有会话时显示，按开始时间倒序，点击进入记录库
    assert.equal(await page.locator('#recent').isVisible(), true);
    assert.equal(await page.locator('#recentList .recent-item').count(), 2);
    await Promise.all([
      page.waitForURL(/\/records\.html\?session=/),
      page.click('#recentList .recent-item:first-child'),
    ]);
    assert.equal(new URL(page.url()).searchParams.get('session'), info.running);

    // CTA 与导航
    await page.goto(`${base}/`);
    assert.equal(await page.locator('a.cta').getAttribute('href'), '/setup.html');
    assert.equal(await page.locator('a.cta-ghost').getAttribute('href'), '/home.html');
    assert.equal(await page.locator('a.sign-in').getAttribute('href'), '/setup.html');
    assert.deepEqual(
      await page.$$eval('.nav-pill .nav-link', (as) => as.map((a) => a.getAttribute('href'))),
      ['/', '/home.html', '/records.html', '/settings.html']);

    // 桌面 1440x900：单屏（无纵向滚动）、无横向溢出
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), '无横向溢出');
    assert.ok(await page.evaluate(() => document.scrollingElement.scrollHeight <= window.innerHeight), '桌面单屏无纵向滚动');

    await page.waitForTimeout(2200);  // 等 count-up 与入场动画完成再截图
    await page.screenshot({ path: path.join(SHOTS, 'landing-desktop.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('landing E2E 接口失败：统计显示 —，最近会话隐藏', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const info = await backend.next();
  const base = `http://127.0.0.1:${info.port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    for (const api of ['/api/libraries', '/api/secondary/progress', '/api/sessions', '/api/status']) {
      await page.route(`**${api}`, (r) => r.fulfill({ status: 500, body: 'boom' }));
    }
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll('.stat-value')).every((el) => el.textContent === '—'),
      undefined, { timeout: 8000 },
    );
    assert.equal(await page.locator('#recent').isVisible(), false, '无会话数据时隐藏最近会话');
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('landing E2E 390 窄屏：无横向溢出，汉堡菜单开合与 Esc', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const info = await backend.next();
  const base = `http://127.0.0.1:${info.port}`;
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = watchProblems(page);
  try {
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), '窄屏无横向溢出');
    assert.equal(await page.locator('.burger').isVisible(), true);
    assert.equal(await page.locator('.nav-pill').isVisible(), false);

    await page.waitForTimeout(1800);
    await page.screenshot({ path: path.join(SHOTS, 'landing-narrow.png') });

    // 打开汉堡菜单
    await page.click('#burger');
    assert.equal(await page.locator('#burger').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#mobileOverlay').isVisible(), true);
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(SHOTS, 'landing-mobile-menu.png') });

    // Esc 关闭并把焦点还给按钮
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#burger').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#mobileOverlay').isVisible(), false);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'burger');

    // 再开一次，点遮罩关闭
    await page.click('#burger');
    await page.waitForTimeout(200);
    await page.mouse.click(390 / 2, 700);
    assert.equal(await page.locator('#mobileOverlay').isVisible(), false);

    // 菜单里的链接与桌面一致
    await page.click('#burger');
    assert.deepEqual(
      await page.$$eval('#mobileMenu .m-link', (as) => as.map((a) => a.getAttribute('href'))),
      ['/', '/home.html', '/records.html', '/settings.html']);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('landing E2E prefers-reduced-motion：视频暂停、数字直接显示终值', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const info = await backend.next();
  const base = `http://127.0.0.1:${info.port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const problems = watchProblems(page);
  try {
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
      (want) => document.querySelector('[data-stat="primary"]')?.textContent === want,
      String(info.counts.primary), { timeout: 8000 },
    );
    assert.equal(await page.evaluate(() => document.querySelector('video.bg-video')?.paused), true, '视频暂停保留画面');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
