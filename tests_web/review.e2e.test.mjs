// M06-4 E2E：真实后端（review_e2e_server.py：create_app + 假增强器）+ Playwright 自带 Chromium。
// 覆盖：增强进度 → 冲突条目红且启用被拦 → 批量只提交安全条目 → 编辑后回待审 → 重新导入统计。
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

const REIMPORT = [
  '## H1 合成章节',
  '**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我负责“星河”项目，团队 8 人。',
  '**H1-03｜00:03｜如何做技术取舍？** 参考回答：按风险与收益排序，保留回退方案。',
  '**H1-04｜00:04｜讲一次失败复盘？** 参考回答：记录事实、归因、改进项 [待核]。',
  '**H1-05｜00:05｜如何做跨部门协作？** 参考回答：先对齐目标，再拆解约束、资源与优先级，最后约定复盘时间。',
  '**H1-06｜00:06｜如何做知识沉淀？** 参考回答：把复盘结论写进团队手册并定期更新。',
].join('\n') + '\n';

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

for (const [w, h, tag] of [[1440, 900, 'desktop'], [390, 844, 'narrow']]) {
  test(`review E2E ${tag}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    mkdirSync(SHOTS, { recursive: true });
    const child = spawn(PY, [path.join(ROOT, 'tests_web', 'review_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    const { port } = JSON.parse((await lines.next()).value);
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    const row = (id) => page.locator(`#list .row[data-id="${id}"]`);
    const open = async (id) => { await row(id).locator('.open').click(); await page.waitForSelector('#edit-question'); };
    try {
      await page.goto(`http://127.0.0.1:${port}/review.html`);
      await page.waitForSelector('#list .row');
      assert.equal(await page.locator('#list .row').count(), 5);
      assert.match(await page.locator('#prog-label').textContent(), /已启用 0 条 · 可计入 G3 0 条 · 达标 50/);
      assert.equal(await row('H1-01').getAttribute('class').then((c) => c.includes('conflict')), true);
      await page.screenshot({ path: path.join(SHOTS, `review2-${tag}-0-loaded.png`), fullPage: true });

      // 增强拆解进度
      await page.locator('#enhance-btn').click();
      await page.waitForFunction(() => /增强完成 5\/5/.test(document.getElementById('enhance-label').textContent));
      await page.waitForFunction(() => /已增强/.test(document.querySelector('#detail .dhead p').textContent));
      await page.screenshot({ path: path.join(SHOTS, `review2-${tag}-1-enhanced.png`), fullPage: true });

      // 冲突条目：标红、单条启用被拦
      await open('H1-01');
      assert.ok(await page.locator('.alert', { hasText: '禁止启用' }).isVisible());
      assert.match(await page.locator('.alert').first().textContent(), /事实冲突/);
      await page.locator('#review-confirm').check();
      await page.locator('[data-action=approve]').click();
      assert.match(await page.locator('#msg').textContent(), /禁止启用/);
      await page.screenshot({ path: path.join(SHOTS, `review2-${tag}-2-conflict.png`), fullPage: true });

      // 批量：冲突条目被前端拦截，只提交安全条目
      await page.locator('#list .row[data-id="H1-01"] input[type=checkbox]').check();
      await page.locator('#list .row[data-id="H1-03"] input[type=checkbox]').check();
      await page.locator('#batch-confirm').check();
      await page.locator('#batch-btn').click();
      await page.waitForFunction(() => /已批量启用 1 条/.test(document.getElementById('msg').textContent));
      assert.match(await page.locator('#batch-rejects').textContent(), /H1-01：.*事实冲突/);
      assert.equal(await row('H1-03').locator('.tag.st-approved').count(), 1);
      assert.equal(await row('H1-01').locator('.tag.st-approved').count(), 0);

      // 编辑已启用条目：保存后回到待审核
      await open('H1-03');
      await page.locator('#edit-question').fill('如何做技术取舍与回退？');
      await page.locator('#edit-variants .edit-row input').first().fill('技术取舍怎么讲？');
      await page.locator('#edit-facts .fact-row').count().then(async (n) => {
        if (n === 0) await page.locator('#edit-facts + .mini, .edit-group .mini', { hasText: '添加事实' }).click();
      });
      await page.locator('#edit-save').click();
      await page.waitForFunction(() => /已保存/.test(document.getElementById('msg').textContent));
      await page.waitForFunction(() => /待审/.test(document.querySelector('#list .row[data-id="H1-03"]')?.textContent || ''));
      assert.match(await row('H1-03').textContent(), /待审/);
      assert.equal(await row('H1-03').locator('.tag.st-approved').count(), 0);

      // 重新导入：新增 1 / 变更 1 / 删除 1
      await page.locator('#reimport-file').setInputFiles({ name: '新版.md', mimeType: 'text/markdown', buffer: Buffer.from(REIMPORT) });
      await page.locator('#reimport-btn').click();
      await page.waitForFunction(() => /新增 1/.test(document.getElementById('reimport-stats').textContent));
      const stats = await page.locator('#reimport-stats').textContent();
      assert.match(stats, /变更 1/);
      assert.match(stats, /删除 1/);
      await page.waitForSelector('#list .row[data-id="H1-06"]');
      await page.screenshot({ path: path.join(SHOTS, `review2-${tag}-3-reimport.png`), fullPage: true });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '无横向溢出');
      assert.deepEqual(problems, []);
    } finally {
      child.stdin.write('\n'); child.stdin.end();
      await env.browser.close();
      await new Promise((r) => child.once('exit', r));
    }
  });
}
