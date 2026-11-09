// home.html E2E：两档视口无横向溢出、入口链接可达。浏览器/Python 不可用则 skip（NOT TESTED）。
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
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'board_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

for (const [w, h] of [[1440, 900], [1920, 1080]]) {
  test(`home E2E ${w}x${h}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    mkdirSync(SHOTS, { recursive: true });
    const backend = startBackend();
    const { port, session } = await backend.next();
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    try {
      // 这些路由尚未在 server.py 接线：用路由拦截模拟响应（SIMULATED），凭证响应里混入明文诱饵以验证不展示
      await page.route('**/api/libraries/*/progress', (r) => r.fulfill({ json: { total: 60, approved: 50, target: 50, g3_ready: true } }));
      await page.route('**/api/config', (r) => r.fulfill({ json: { config: { model: { name: 'deepseek-flash' }, budget: { daily_limit: 20, session_limit: 5 }, outbound: { asr_audio: true, llm_snippets: true, web_questions: true } }, errors: [] } }));
      await page.route('**/api/credentials/status', (r) => r.fulfill({ json: { DEEPSEEK_API_KEY: { status: 'configured', mask: 'sk-****abcd', source: 'keyring', value: 'sk-DECOYDECOY12345678' }, SEARCH_API_KEY: { status: 'missing', mask: '' } } }));  // pragma: allowlist secret（假 Key）
      await page.route('**/api/usage', (r) => r.fulfill({ json: { date: '2026-10-29', rows: [{ service: 'llm', calls: 2, units: 100, est_cost: 0.2 }], est_cost_total: 0.2, daily_limit: 20, note: '估算，非计费硬保证' } }));
      await page.goto(`http://127.0.0.1:${port}/home.html`);
      await page.waitForFunction(() => document.getElementById('session').dataset.active === 'true');
      await page.waitForFunction(() => document.getElementById('lamps').dataset.source === 'session');
      await page.waitForFunction(() => document.getElementById('prog-label').textContent !== '暂无主库审核数据');
      assert.equal(await page.locator('#g3').isVisible(), true);
      assert.equal(await page.locator('#prog-label').textContent(), '已启用 50 条，G3 达成');
      assert.equal(await page.locator('#creds .cred').count(), 2);
      assert.match(await page.locator('#creds').innerText(), /sk-\*\*\*\*abcd/);
      assert.match(await page.locator('#usage').innerText(), /今日估算 0\.20/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '无横向溢出');
      const hrefs = await page.$$eval('#cards a.card', (as) => as.map((a) => a.getAttribute('href')));
      assert.deepEqual(hrefs, ['/setup.html', '/import.html', '/setup.html?step=voice', '/review.html', '/annotate.html', '/secondary.html', '/records.html', '/settings.html', `/live.html?session=${session}`]);
      for (const href of hrefs) {
        const res = await page.request.get(`http://127.0.0.1:${port}${href}`);
        assert.equal(res.status(), 200, href);
      }
      // 凭证只显示状态，页面不含任何密钥形态文本
      assert.ok(!/DECOY|sk-[A-Za-z0-9]{8,}/.test(await page.locator('body').innerText()));
      await page.screenshot({ path: path.join(SHOTS, `home-${w}x${h}.png`), fullPage: true });
      // 点击会话页入口可进入
      await Promise.all([page.waitForURL(/live\.html\?session=/), page.click('#cards a[data-key="live"]')]);
      assert.deepEqual(problems, []);
    } finally {
      await page.close();
      await env.browser.close();
      await backend.stop();
    }
  });
}
