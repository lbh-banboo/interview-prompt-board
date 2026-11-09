// 导入页与向导主库步骤 E2E：真实后端（临时数据目录）+ Playwright Chromium。
// 浏览器或 Python 不可用时 skip，标 NOT TESTED。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { Buffer } from 'node:buffer';
import { existsSync, mkdirSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = path.join(ROOT, 'eval', 'screens');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');

const ONE = '## H1 上传\n**H1-01｜00:01｜上传的问题** 参考回答：上传的回答。';
const PASTE = [
  '## H2 粘贴',
  '**H2-01｜00:01｜粘贴问题一** 参考回答：回答一。',
  '**H2-02｜00:02｜粘贴问题二** 参考回答：回答二。',
].join('\n');

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend(mode = 'import') {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'import_e2e_server.py'), '--mode', mode],
    { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watch(page, problems) {
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
}

async function stepToLibrary(page, port) {
  await page.goto(`http://127.0.0.1:${port}/setup.html`);
  await page.waitForFunction(() => document.getElementById('next').disabled === false);
  await page.click('#steps .card');
  await page.click('#next');
  await page.selectOption('#steps select.field', { index: 1 });
  await page.click('#next');
  await page.click('#steps .chip');
  await page.click('#next');
  await page.waitForFunction(() => !document.getElementById('next').disabled);
}

test('导入页：上传与粘贴都进入主库列表，去审核可达；根路径跳到工作台', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('import');
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  watch(page, problems);
  try {
    await page.goto(`http://127.0.0.1:${port}/import.html`);
    await page.waitForFunction(() => !document.getElementById('libs-empty').hidden);
    assert.equal(await page.locator('#libs li').count(), 0);
    assert.match(await page.locator('#format-rules').innerText(), /参考回答/);

    // 上传 .md：自动读取文本并预填库名
    await page.locator('#file').setInputFiles({ name: '上传资料.md', mimeType: 'text/markdown', buffer: Buffer.from(ONE) });
    await page.waitForFunction(() => document.getElementById('text').value.includes('H1-01'));
    assert.equal(await page.locator('#name').inputValue(), '上传资料');
    await page.click('#submit');
    await page.waitForFunction(() => !document.getElementById('result').hidden);
    assert.match(await page.locator('#result').innerText(), /导入 1 条，全部待审核，未自动启用/);
    assert.equal(await page.locator('#result a.go').getAttribute('href'), '/review.html');
    await page.waitForFunction(() => document.querySelectorAll('#libs li').length === 1);
    assert.match(await page.locator('#libs').innerText(), /上传资料 · 共 1 条 · 已启用 0 条/);

    // 粘贴文本：多条目
    await page.fill('#name', '粘贴库');
    await page.fill('#text', PASTE);
    await page.click('#submit');
    await page.waitForFunction(() => document.querySelectorAll('#libs li').length === 2);
    assert.match(await page.locator('#result').innerText(), /导入 2 条/);

    // 次优库：启动时已自动导入种子，按钮可再次幂等导入
    await page.waitForFunction(() => /共 7 条/.test(document.getElementById('secondary-summary').textContent));
    await page.click('#secondary-import');
    await page.waitForFunction(() => /本次新增/.test(document.getElementById('secondary-msg').textContent));
    assert.match(await page.locator('#secondary-msg').innerText(), /本次新增 0 条，未变 7 条/);
    assert.match(await page.locator('#secondary-roles').innerText(), /tech/);
    assert.equal(await (await page.request.get(`http://127.0.0.1:${port}/review.html`)).status(), 200);
    await page.screenshot({ path: path.join(SHOTS, 'import-desktop.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '无横向溢出');

    // 首页上线：/ 与 /index.html 直接返回欢迎页（不再 302 跳工作台）
    for (const p of ['/', '/index.html']) {
      const res = await page.request.get(`http://127.0.0.1:${port}${p}`, { maxRedirects: 0 });
      assert.equal(res.status(), 200, p);
      assert.match(await res.text(), /Interview Copilot/);
    }
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('导入页窄屏无横向溢出', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('import');
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = [];
  watch(page, problems);
  try {
    await page.goto(`http://127.0.0.1:${port}/import.html`);
    await page.waitForFunction(() => !document.getElementById('libs-empty').hidden);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '无横向溢出');
    await page.screenshot({ path: path.join(SHOTS, 'import-narrow.png'), fullPage: true });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('向导主库单选：点选替换、再点取消', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend('setup');
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  watch(page, problems);
  try {
    await stepToLibrary(page, port);
    assert.equal(await page.locator('#steps .opt').count(), 2);
    const labels = await page.$$eval('#steps .opt', (os) => os.map((o) => o.textContent));
    await page.locator('#steps .opt').nth(0).click();
    assert.equal(await page.locator('#steps .opt.on').innerText(), labels[0]);
    await page.locator('#steps .opt').nth(1).click();
    assert.equal(await page.locator('#steps .opt.on').innerText(), labels[1]);
    assert.notEqual(await page.locator('#steps .opt.on').innerText(), labels[0]);
    await page.locator('#steps .opt').nth(1).click();
    assert.equal(await page.locator('#steps .opt.on').count(), 0);
    assert.equal(await page.locator('#go-import').getAttribute('href'), '/import.html');
    assert.equal(await page.locator('#go-review').getAttribute('href'), '/review.html');
    await page.locator('#steps .opt').nth(0).click();
    await page.click('#next');
    await page.waitForFunction(() => document.getElementById('steps').textContent.includes('音源与同意'));
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('向导无可用主库：给出导入资料与题库审核两个入口，不再死循环', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend('empty');
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  watch(page, problems);
  try {
    await stepToLibrary(page, port);
    assert.equal(await page.locator('#steps .opt').count(), 0);
    assert.equal(await page.locator('#go-import').getAttribute('href'), '/import.html');
    assert.equal(await page.locator('#go-review').getAttribute('href'), '/review.html');
    assert.match(await page.locator('#steps').innerText(), /还没有可用的主库/);
    await page.click('#next');
    await page.waitForFunction(() => /导入资料/.test(document.getElementById('msg').textContent));
    assert.match(await page.locator('#msg').innerText(), /没有可选的主库/);
    await page.screenshot({ path: path.join(SHOTS, 'import-setup-empty.png'), fullPage: true });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
