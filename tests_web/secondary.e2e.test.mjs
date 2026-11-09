// M07 E2E：次优库审核台，真实后端（secondary_e2e_server.py，临时库 + 合成种子）+ Playwright Chromium。
// 浏览器或 Python 不可用时 skip，标 NOT TESTED。
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

for (const [w, h, tag] of [[1440, 900, 'desktop'], [390, 844, 'narrow']]) {
  test(`secondary E2E ${tag}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    mkdirSync(SHOTS, { recursive: true });
    const child = spawn(PY, [path.join(ROOT, 'tests_web', 'secondary_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    const { port } = JSON.parse((await lines.next()).value);
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    try {
      await page.goto(`http://127.0.0.1:${port}/secondary.html`);
      await page.waitForSelector('#list .row');
      assert.equal(await page.locator('#list .row').count(), 7);
      assert.match(await page.locator('#prog-label').textContent(), /已审核 0\/7/);
      await page.screenshot({ path: path.join(SHOTS, `secondary-${tag}-0-loaded.png`), fullPage: true });

      // 详情含通用建议提示；未勾选确认时通过被拦
      await page.locator('#list .row[data-id="QTECH-01"] .open').click();
      assert.ok(await page.locator('.alert', { hasText: '不冒充个人经历' }).isVisible());
      await page.locator('[data-action=approve]').click();
      assert.match(await page.locator('#msg').textContent(), /已核对内容为通用建议/);
      await page.locator('#review-confirm').check();
      await page.locator('[data-action=approve]').click();
      await page.waitForFunction(() => /QTECH-01 已通过/.test(document.getElementById('msg').textContent));
      assert.match(await page.locator('#prog-label').textContent(), /已审核 1\/7/);
      await page.screenshot({ path: path.join(SHOTS, `secondary-${tag}-1-approved.png`), fullPage: true });

      // 键盘 j 切条
      await page.locator('body').click({ position: { x: 2, y: 2 } });
      const before = await page.locator('#list .row.sel').getAttribute('data-id');
      await page.keyboard.press('j');
      assert.notEqual(await page.locator('#list .row.sel').getAttribute('data-id'), before);

      // 批量通过：勾选两条 + 确认
      await page.locator('#list .row[data-id="QOPS-01"] input[type=checkbox]').check();
      await page.locator('#list .row[data-id="QGEN-01"] input[type=checkbox]').check();
      assert.ok(await page.locator('#batch-btn').isDisabled());
      await page.locator('#batch-confirm').check();
      await page.locator('#batch-btn').click();
      await page.waitForFunction(() => /已批量通过 2 条/.test(document.getElementById('msg').textContent));
      assert.match(await page.locator('#prog-label').textContent(), /已审核 3\/7/);
      await page.screenshot({ path: path.join(SHOTS, `secondary-${tag}-2-batch.png`), fullPage: true });

      // 岗位筛选：tech 仅两条，进度按岗位显示
      await page.selectOption('#role', 'tech');
      await page.waitForFunction(() => document.querySelectorAll('#list .row').length === 2);
      assert.match(await page.locator('#prog-label').textContent(), /已审核 1\/2 · tech/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '无横向溢出');
      await page.screenshot({ path: path.join(SHOTS, `secondary-${tag}-3-role.png`), fullPage: true });
      assert.deepEqual(problems, []);
    } finally {
      child.stdin.write('\n'); child.stdin.end();
      await env.browser.close();
      await new Promise((r) => child.once('exit', r));
    }
  });
}
