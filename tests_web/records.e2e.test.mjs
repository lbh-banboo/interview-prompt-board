// 记录库 E2E：真实后端（records_e2e_server.py 合成 3 场会话）+ Playwright 自带 Chromium。
// 覆盖：列表/搜索/筛选 → 概览/逐字稿/轮次与提示/复盘编辑 → 导出/zip 下载 → 删除确认 → URL 直达 → 窄屏。
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
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'records_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const next = async () => JSON.parse((await lines.next()).value);
  return {
    next,
    async stop() { child.stdin.write('\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watchProblems(page) {
  const problems = [];
  // 删除是两步确认：第一次 DELETE 的 409 need_confirm 是协议的一部分，只过滤这一条资源日志（与 review E2E 同法）。
  page.on('console', (m) => {
    if (m.type() === 'error' && !/409 \(Conflict\)/.test(m.text())) problems.push('console: ' + m.text());
  });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

const noOverflow = (page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);

test('records page: list → details → edit → export/zip → delete → direct URL', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  try {
    await page.goto(`http://127.0.0.1:${port}/records.html`);
    await page.waitForSelector('#list .row');
    assert.equal(await page.locator('#list .row').count(), 3);
    assert.equal(await page.locator('#list .row').first().getAttribute('data-id'), 'running', '按开始时间倒序');

    // 搜索与状态筛选
    await page.fill('#search', '候选人A');
    assert.equal(await page.locator('#list .row').count(), 1);
    await page.fill('#search', '');
    await page.locator('#filters .chip', { hasText: '已结束' }).click();
    assert.equal(await page.locator('#list .row').count(), 2);
    await page.locator('#filters .chip', { hasText: '全部' }).click();

    // 进行中的会话：删除按钮禁用
    await page.locator('#list .row[data-id="running"] .open').click();
    await page.waitForFunction(() => /合成候选人A|未关联资料/.test(document.getElementById('detail').textContent));
    assert.equal(await page.locator('[data-act="delete"]').isDisabled(), true);
    assert.match(await page.locator('.panel-body').textContent(), /会话仍在进行中/);

    // URL 直达已结束会话
    await page.goto(`http://127.0.0.1:${port}/records.html?session=full`);
    await page.waitForFunction(() => document.getElementById('detail').textContent.includes('合成候选人A'));
    assert.match(await page.locator('.rec-dhead').textContent(), /合成候选人A/);
    assert.match(await page.locator('.panel-body').textContent(), /降级开始：自检有失败项/);
    assert.match(await page.locator('.kv').textContent(), /合成主库/);
    assert.match(await page.locator('.kv').textContent(), /10:00/);
    await page.screenshot({ path: path.join(SHOTS, 'records-1440-overview.png'), fullPage: true });

    // 逐字稿：时间线、角色、回答者片段存在
    await page.locator('[data-tab="transcript"]').click();
    assert.equal(await page.locator('.transcript .seg').count(), 3);
    assert.match(await page.locator('.transcript').textContent(), /00:10/);
    assert.equal(await page.locator('.seg[data-role="questioner"]').count(), 2);
    assert.equal(await page.locator('.seg[data-role="answerer"]').count(), 1);
    assert.equal(await page.locator('.alert').filter({ hasText: '默认不转写回答者' }).count(), 0);
    await page.screenshot({ path: path.join(SHOTS, 'records-1440-transcript.png'), fullPage: true });

    // 轮次与提示：级别、来源、可见延迟、回滚、迟到归档
    await page.locator('[data-tab="turns"]').click();
    const t1 = page.locator('.turn[data-turn="t1"]');
    assert.match(await t1.textContent(), /级别 L1/);
    assert.match(await t1.textContent(), /来源 主库/);
    assert.match(await t1.textContent(), /可见延迟 0\.80 秒/);
    assert.match(await t1.textContent(), /结论：先讲目标；要点：拆分约束/);
    const t2 = page.locator('.turn[data-turn="t2"]');
    assert.match(await t2.textContent(), /回滚 1 次/);
    assert.match(await t2.textContent(), /未展示（迟到归档）/);
    await page.screenshot({ path: path.join(SHOTS, 'records-1440-turns.png'), fullPage: true });

    // 复盘编辑：保存后原文保留可回看
    await page.locator('[data-tab="review"]').click();
    const area = page.locator('.edit-row[data-seg="1"] textarea');
    assert.equal(await area.inputValue(), '请介绍一个你负责的合成项目');
    await area.fill('改过的合成问题');
    await page.locator('.edit-row[data-seg="1"] [data-save]').click();
    await page.waitForFunction(() => /片段已保存/.test(document.getElementById('msg').textContent));
    assert.match(await page.locator('.edit-row[data-seg="1"]').textContent(), /原文：请介绍一个你负责的合成项目/);
    assert.match(await page.locator('.edit-row[data-seg="1"]').textContent(), /已修订/);
    await page.locator('[data-tab="transcript"]').click();
    assert.match(await page.locator('.transcript').textContent(), /改过的合成问题/);
    assert.match(await page.locator('.transcript').textContent(), /原文：请介绍一个你负责的合成项目/);

    // 导出 Markdown 与 zip
    const [md] = await Promise.all([page.waitForEvent('download'), page.locator('[data-act="export"]').click()]);
    assert.match(md.suggestedFilename(), /^面试记录-.*\.md$/);
    const [zip] = await Promise.all([page.waitForEvent('download'), page.locator('[data-act="zip"]').click()]);
    assert.match(zip.suggestedFilename(), /^\.?full\.zip$/);

    // 无回答者逐字稿：明确注明默认不转写
    await page.locator('#list .row[data-id="no_answerer"] .open').click();
    await page.waitForFunction(() => document.getElementById('detail').textContent.includes('合成候选人B'));
    await page.locator('[data-tab="transcript"]').click();
    assert.match(await page.locator('.alert').textContent(), /默认不转写回答者/);
    // 0 条主答案：轮次页明确“本轮没有命中标准答案”，不是空白
    await page.locator('[data-tab="turns"]').click();
    assert.match(await page.locator('.panel-body').textContent(), /本轮没有命中标准答案/);

    // 删除到回收：二次确认 → 列表标记 → 详情提示 → 编辑不可用
    await page.locator('[data-act="delete"]').click();
    await page.locator('[data-act="delete-confirm"]').waitFor();
    await page.locator('[data-act="delete-confirm"]').click();
    await page.waitForFunction(() => /已移至回收目录/.test(document.getElementById('msg').textContent));
    const row = page.locator('#list .row[data-id="no_answerer"]');
    assert.match(await row.textContent(), /已删除/);
    await page.locator('[data-tab="overview"]').click();
    assert.match(await page.locator('.panel-body').textContent(), /移至回收目录/);
    await page.locator('[data-tab="review"]').click();
    assert.match(await page.locator('.panel-body').textContent(), /已在回收目录，不能复盘编辑/);

    // 删除后的直链也不再读取逐字稿
    await page.goto(`http://127.0.0.1:${port}/records.html?session=no_answerer`);
    await page.waitForFunction(() => document.getElementById('detail').textContent.includes('回收目录'));
    assert.equal(await page.locator('.transcript .seg').count(), 0);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('records page narrow viewport has no horizontal overflow', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 390, height: 844 } });
  const problems = watchProblems(page);
  try {
    await page.goto(`http://127.0.0.1:${port}/records.html?session=full`);
    await page.waitForSelector('.rec-dhead');
    assert.ok(await noOverflow(page), '窄屏概览无横向溢出');
    await page.screenshot({ path: path.join(SHOTS, 'records-390-overview.png'), fullPage: true });
    await page.locator('[data-tab="transcript"]').click();
    assert.ok(await noOverflow(page), '窄屏逐字稿无横向溢出');
    await page.locator('[data-tab="turns"]').click();
    assert.ok(await noOverflow(page), '窄屏轮次无横向溢出');
    await page.locator('[data-tab="review"]').click();
    assert.ok(await noOverflow(page), '窄屏复盘无横向溢出');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
