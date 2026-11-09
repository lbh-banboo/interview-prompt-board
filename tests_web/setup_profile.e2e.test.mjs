// 会前向导：统一六步（线上声音登记可选）、资料内容生成/更新答案库、首页 ?step=voice 直达。
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

const CONTENT = [
  '# 合成资料', '', '## H1 项目',
  '**H1-01｜00:01｜你最有代表性的项目是什么？** 参考回答：我负责“星河”项目，团队 8 人。',
  '**H1-02｜00:02｜你在项目里的角色是什么？** 参考回答：我主导交付与复盘。',
].join('\n');
const UPDATED = [
  '# 合成资料', '', '## H1 项目',
  '**H1-01｜00:01｜你最有代表性的项目是什么？** 参考回答：我负责“星河”项目，团队 8 人。',
  '**H1-02｜00:02｜你在项目里的角色是什么？** 参考回答：我主导交付、复盘并带 3 人小组。',
  '**H1-03｜00:03｜你如何衡量结果？** 参考回答：用交付周期与缺陷率复盘。',
].join('\n');

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}

function startBackend(args = []) {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'setup_e2e_server.py'), ...args], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

function watch(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

async function pickProfileAndRoles(page) {
  await page.selectOption('#profile-select', { index: 1 });
  await page.click('#next');
  await page.click('#steps .chip');
  await page.click('#next');
}

test('线上六步、声音登记可选可跳过、选定资料默认选中其主库', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watch(page);
  try {
    await page.goto(`${base}/setup.html`);
    await page.waitForSelector('#steps .card');
    await page.click('#steps .card');
    await page.click('#next');
    await pickProfileAndRoles(page);
    assert.equal(await page.locator('#steps .opt.on').count(), 1, '选资料后默认选中关联主库');
    await page.click('#next');
    assert.equal(await page.locator('#steps .step').count(), 6);
    assert.match(await page.locator('#steps .step.cur').innerText(), /声音登记（可选）/);
    assert.match(await page.locator('.voice-optional').innerText(), /线上模式可跳过/);
    await page.screenshot({ path: path.join(SHOTS, 'setup2-steps.png'), fullPage: true });
    await page.click('#next');
    assert.match(await page.locator('#steps .step.cur').innerText(), /音源与同意/);

    // 首页“声音登记”入口：直达但不强制线下；登记步骤可直接继续
    await page.goto(`${base}/setup.html?step=voice`);
    await page.waitForSelector('.voice-optional');
    assert.match(await page.locator('#steps .step.cur').innerText(), /声音登记/);
    assert.equal(await page.locator('#steps .card.on').count(), 0, '?step=voice 不强制选择线下模式');
    await page.click('#next');
    assert.match(await page.locator('#steps .step.cur').innerText(), /音源与同意/);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('新建资料并粘贴内容生成待审核库；更新内容显示统计；窄屏无溢出', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watch(page);
  try {
    await page.goto(`${base}/setup.html`);
    await page.waitForSelector('#steps .card');
    await page.click('#steps .card');
    await page.click('#next');
    await page.click('#profile-new');
    await page.waitForSelector('#pe-alias');
    await page.fill('#pe-alias', '合成乙');
    await page.fill('#pe-content', CONTENT);
    await page.screenshot({ path: path.join(SHOTS, 'setup2-profile-editor.png'), fullPage: true });
    await page.click('#pe-save');
    await page.waitForFunction(() => document.getElementById('msg').textContent.includes('已生成答案库'));
    assert.match(await page.locator('#profile-notice').innerText(), /待审核/);
    assert.equal(await page.locator('#profile-review').getAttribute('href'), '/review.html');
    assert.equal((await page.request.get(`${base}/review.html`)).status(), 200);

    const created = await page.evaluate(async () => {
      const list = await (await fetch('/api/profiles')).json();
      return list.find((p) => p.alias === '合成乙');
    });
    assert.equal(created.library_ids.length, 1, '资料只关联一个主库');
    assert.equal(created.library_stats.approved, 0, '新条目未自动启用');

    await page.click('#profile-edit');
    await page.waitForSelector('#pe-content');
    await page.fill('#pe-content', UPDATED);
    await page.click('#pe-sync');
    await page.waitForFunction(() => document.getElementById('msg').textContent.includes('新增 1 / 变更 1'));
    assert.match(await page.locator('#msg').innerText(), /变更条目已回到待审核/);
    assert.match(await page.locator('#profile-notice').innerText(), /新增 1 \/ 变更 1 \/ 删除 0/);
    await page.screenshot({ path: path.join(SHOTS, 'setup2-update-result.png'), fullPage: true });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '窄屏无横向溢出');
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});

test('线下模式仍必须声音登记（或显式降级）', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend(['--voice-missing']);
  const { port } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watch(page);
  try {
    await page.goto(`${base}/setup.html`);
    await page.waitForSelector('#steps .card');
    await page.locator('#steps .card').nth(1).click();
    await page.click('#next');
    await pickProfileAndRoles(page);
    if (!(await page.locator('#steps .opt.on').count())) await page.click('#steps .opt:not(.off)');
    await page.click('#next');
    assert.equal(await page.locator('.voice-optional').count(), 0, '线下不标注可选');
    await page.click('#next');
    await page.waitForFunction(() => document.getElementById('msg').textContent.includes('声音登记'));
    assert.match(await page.locator('#steps .step.cur').innerText(), /声音登记/);
    await page.locator('#steps input[type=checkbox]').first().check();
    await page.click('#next');
    assert.match(await page.locator('#steps .step.cur').innerText(), /音源与同意/);
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
