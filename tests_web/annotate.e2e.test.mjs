// 提问点校对台 E2E：真实后端（annotate_e2e_server.py，临时目录假数据）+ Playwright 自带 Chromium。
// 浏览器或 Python 不可用时 skip，标 NOT TESTED。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
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

for (const [w, h, tag] of [[1440, 900, 'desktop'], [390, 844, 'narrow']]) {
  test(`annotate E2E ${tag}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    const child = spawn(PY, [path.join(ROOT, 'tests_web', 'annotate_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    const { port, ann } = JSON.parse((await lines.next()).value);
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    try {
      await page.goto(`http://127.0.0.1:${port}/annotate.html`);
      await page.waitForSelector('#list .row');
      assert.equal(await page.locator('#list .row').count(), 3);
      assert.match(await page.locator('#prog-label').textContent(), /已判定 0\/3/);
      assert.equal(await page.locator('#list .row.sel').getAttribute('data-id'), '1');

      // 跳过：只换条，不写判定
      await page.locator('body').click({ position: { x: 2, y: 2 } });
      await page.keyboard.press('s');
      assert.equal(await page.locator('#list .row.sel').getAttribute('data-id'), '2');
      assert.match(await page.locator('#prog-label').textContent(), /已判定 0\/3/);

      // 回到第一条，写备注后点「是问题结束点」
      await page.locator('#list .row[data-id="1"] .open').click();
      assert.match(await page.locator('#detail').textContent(), /合成句1/);
      await page.locator('#note').fill('完整问题');
      await page.locator('[data-verdict=yes]').click();
      await page.waitForFunction(() => /已判定 1\/3/.test(document.getElementById('prog-label').textContent));
      assert.equal(await page.locator('#list .row.sel').getAttribute('data-id'), '2');

      // 键盘：n 不是，t 改题
      await page.locator('body').click({ position: { x: 2, y: 2 } });
      await page.keyboard.press('n');
      await page.waitForFunction(() => /已判定 2\/3/.test(document.getElementById('prog-label').textContent));
      await page.locator('body').click({ position: { x: 2, y: 2 } });
      await page.keyboard.press('t');
      await page.waitForFunction(() => /已判定 3\/3/.test(document.getElementById('prog-label').textContent));
      assert.match(await page.locator('#msg').textContent(), /已判/);

      // 写回文件与 import_review_md 语义一致
      const doc = JSON.parse(readFileSync(path.join(ann, 'demo.review.json'), 'utf8'));
      assert.deepEqual(doc.items.map((i) => i.verified), [true, true, true]);
      assert.deepEqual(doc.items[0].human, { is_question_end: true, role: 'questioner', is_topic_change: false, is_followup: false, note: '完整问题' });
      assert.equal(doc.items[1].human.is_question_end, false);
      assert.equal(doc.items[1].human.is_topic_change, null);
      assert.equal(doc.items[2].human.is_topic_change, true);

      // 筛选与重播不报错
      await page.locator('#filters .chip', { hasText: '已判定' }).click();
      assert.equal(await page.locator('#list .row').count(), 3);
      await page.locator('#filters .chip', { hasText: '未判定' }).click();
      assert.equal(await page.locator('#list .row').count(), 0);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '无横向溢出');
      assert.deepEqual(problems, []);
    } finally {
      child.stdin.write('\n'); child.stdin.end();
      await env.browser.close();
      await new Promise((r) => child.once('exit', r));
    }
  });
}
