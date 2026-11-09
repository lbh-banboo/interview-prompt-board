// setup 页 E2E：开始成功后给醒目出口；刷新遇到进行中的会话时提供继续/结束，不让向导走进 409。
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
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'setup_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

async function runWizard(page) {
  await page.click('#steps .card');
  await page.click('#next');
  await page.selectOption('#steps select.field', { index: 1 });
  await page.click('#next');
  await page.click('#steps .chip');
  await page.click('#next');
  if (!(await page.locator('#steps .opt.on').count())) await page.click('#steps .opt:not(.off)');
  await page.click('#next');
  await page.click('#next'); // 线上模式：声音登记可选，直接跳过
  await page.locator('#steps input[type=checkbox]').nth(0).check();
  await page.locator('#steps input[type=checkbox]').nth(1).check();
  await page.click('#next');
}

test('setup shows obvious way forward after start and on reload with an active session', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  try {
    await page.goto(`http://127.0.0.1:${port}/setup.html`);
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    assert.equal(await page.locator('#next').isDisabled(), false);

    await runWizard(page);
    await page.waitForFunction(() => !document.getElementById('start').disabled);
    await page.click('#start');
    await page.click('#share-ok');
    await page.waitForFunction(() => !document.getElementById('started-card').hidden);
    const live = await page.locator('#started-live').getAttribute('href');
    assert.match(live, /^\/live\.html\?session=/);
    assert.equal(await page.locator('#started-card a').count(), 1, '开始后只有“进入会话页”一个入口');
    assert.equal(await page.locator('#started-card a').textContent(), '进入会话页');
    const sid = new URL(live, `http://127.0.0.1:${port}`).searchParams.get('session');
    assert.match(await page.locator('#msg').innerText(), /会话已开始|已降级开始/);
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session?.id), sid);

    // 刷新：出现“进行中的会话”卡片，向导被挡住（不再创建导致 409）
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('active-card').hidden);
    assert.match(await page.locator('#active-label').innerText(), /进行中的会话/);
    assert.equal(await page.locator('#active-live').getAttribute('href'), `/live.html?session=${sid}`);
    assert.equal(await page.locator('#active-card a').count(), 1, '进行中会话也只给会话页入口');
    assert.equal(await page.locator('#next').isDisabled(), true);

    // 结束该会话后，向导恢复可用
    await page.click('#active-stop');
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    assert.match(await page.locator('#msg').innerText(), /已结束进行中的会话/);
    assert.equal(await page.locator('#next').isDisabled(), false);
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/status')).json()).active_session), null);

    // 创建时遇 409：文案给出“继续/结束”出路，并即时显示进行中卡片
    await page.reload();
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    const sid2 = await page.evaluate(async () => {
      const st = await (await fetch('/api/status')).json();
      const head = { 'Content-Type': 'application/json', 'X-Local-Token': st.token };
      const [opts, profiles] = await Promise.all([
        (await fetch('/api/setup/options')).json(), (await fetch('/api/profiles')).json(),
      ]);
      const lib = opts.libraries.find((l) => l.selectable).id;
      const s = await (await fetch('/api/setup/sessions', {
        method: 'POST', headers: head,
        body: JSON.stringify({ mode: 'online', profile_id: profiles[0].id, roles: ['产品经理'], library_ids: [lib], consent: true }),
      })).json();
      await fetch(`/api/setup/${s.id}/selfcheck`, { method: 'POST', headers: head });
      await fetch(`/api/setup/${s.id}/start`, { method: 'POST', headers: head, body: JSON.stringify({ degraded: false }) });
      return s.id;
    });
    await runWizard(page);
    await page.waitForFunction(() => document.getElementById('msg').textContent.includes('已有进行中的会话'));
    assert.match(await page.locator('#msg').innerText(), /卡片继续/);
    assert.equal(await page.locator('#active-live').getAttribute('href'), `/live.html?session=${sid2}`);
    assert.equal(await page.locator('#next').isDisabled(), true);
    await page.click('#active-stop');
    await page.waitForFunction(() => document.getElementById('active-card').hidden);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
