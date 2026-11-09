// 采集页 E2E：进入即复用 active 的 setup 会话，不再 POST /api/sessions；没有 active 会话时提示去 setup。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try {
    return { browser: await chromium.launch({ args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--disable-audio-output'] }) };
  } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'capture_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

test('capture reuses the active setup session and never creates a legacy session', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage();
  const problems = [];
  let legacyPosts = 0;
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  page.on('request', (r) => { if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/sessions') legacyPosts++; });
  try {
    await page.goto(`http://127.0.0.1:${port}/capture.html`);
    await page.waitForFunction(() => document.getElementById('message').textContent.includes('已复用会话'));
    assert.match(await page.locator('#message').innerText(), new RegExp(session.slice(0, 8)));
    assert.equal(legacyPosts, 0, '进入采集页不应创建旧会话');

    await page.check('#consent');
    await page.click('#start');
    await page.waitForFunction(() => document.getElementById('message').textContent.includes('采集中'));
    const active = await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session?.id);
    assert.equal(active, session, '采集页使用的是 setup 会话');
    await page.waitForFunction(() => document.getElementById('ws').textContent.includes('已连接'), null, { timeout: 10000 });

    await page.click('#stop');
    await page.waitForFunction(() => document.getElementById('message').textContent.includes('已结束并保存'));
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session), null);
    assert.equal(legacyPosts, 0, '结束也不走旧会话接口');
    const hrefs = await page.locator('#message a').evaluateAll((as) => as.map((a) => a.getAttribute('href')));
    assert.deepEqual(hrefs, [`/board.html?session=${session}`, `/api/sessions/${session}/export`, '/setup.html'], '结束后给出提示板/导出/下一场出路');
    const [download] = await Promise.all([page.waitForEvent('download'), page.click('#message a[href$="/export"]')]);
    assert.match(download.suggestedFilename(), /^面试记录-.*\.md$/);

    await page.reload();
    await page.waitForFunction(() => document.getElementById('message').textContent.includes('请先在'));
    assert.equal(await page.locator('#start').isDisabled(), true);
    assert.equal(await page.locator('#message a[href="/setup.html"]').count(), 1);
    assert.equal(legacyPosts, 0, '没有会话时也不静默创建');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
