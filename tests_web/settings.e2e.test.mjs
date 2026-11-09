// 设置页 E2E：改参数→刷新仍在、非法值显示错误、外发关闭进新会话快照、进行中会话提示下一场生效、窄屏无溢出。
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
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'settings_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

const field = (key) => `input[data-key="${key}"]`;

test('settings: edit/save/validate, outbound snapshot, active notice, narrow screen', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port } = await backend.next();
  const base = `http://127.0.0.1:${port}`;
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/40[49]/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  page.on('dialog', (d) => { void d.accept(); });
  try {
    await page.goto(`${base}/settings.html`);
    await page.waitForSelector(field('turn.T_silence'));
    await page.waitForSelector(field('search.endpoint'));
    await page.waitForSelector(field('budget.asr_daily_limit'));
    assert.equal(await page.locator(field('search.live')).getAttribute('type'), 'checkbox');
    assert.match(await page.locator('#params').innerText(), /DeepSeek Search/);
    assert.match(await page.locator('#params').innerText(), /按次计费/);
    assert.match(await page.locator('#params').innerText(), /阈值来源：默认文件/);
    assert.match(await page.locator('#params').innerText(), /待标定/);
    await page.waitForFunction(() => document.querySelectorAll('#outbound .switch-row').length === 4);
    assert.equal(await page.locator('#active-note').isVisible(), false);
    await page.screenshot({ path: path.join(SHOTS, 'settings-desktop.png'), fullPage: true });

    // 1) 修改 turn.T_silence → 保存 → 刷新仍在
    await page.fill(field('turn.T_silence'), '320');
    await page.click('#save');
    await page.waitForFunction(() => document.getElementById('save-msg').textContent.includes('已保存'));
    assert.match(await page.locator('#save-msg').innerText(), /下一场会话生效/);
    await page.reload();
    await page.waitForSelector(field('turn.T_silence'));
    assert.equal(await page.inputValue(field('turn.T_silence')), '320');

    // 2) 非法值 → 出现范围错误，且不写盘
    await page.fill(field('turn.T_silence'), '1');
    await page.click('#save');
    await page.waitForFunction(() => document.querySelectorAll('#errors li').length > 0);
    assert.match(await page.locator('#errors').innerText(), /不能小于 50/);
    const still = await page.evaluate(async () => (await (await fetch('/api/config')).json()).config.turn.T_silence);
    assert.equal(still, 320);

    // 3) 关闭一类外发（片段 → DeepSeek）→ 保存到全局配置
    await page.uncheck('input[data-switch-key="llm_snippets"]');
    await page.waitForFunction(() => document.getElementById('outbound-msg').textContent.includes('已保存'));
    const flags = await page.evaluate(async () => (await (await fetch('/api/config')).json()).config.outbound);
    assert.equal(flags.llm_snippets, false);

    // 4) 新建并开始会话 → 启动快照里为 false
    const info = await page.evaluate(async () => {
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
      const started = await (await fetch(`/api/setup/${s.id}/start`, {
        method: 'POST', headers: head, body: JSON.stringify({ degraded: false }),
      })).json();
      const snap = await (await fetch(`/api/setup/${s.id}`)).json();
      return { sid: s.id, status: started.status, outbound: snap.config.outbound };
    });
    assert.equal(info.status, 'active');
    assert.equal(info.outbound.llm_snippets, false);
    assert.equal(info.outbound.asr_audio, true);

    // 5) 有进行中会话 → 醒目“下一场生效”提示
    await page.reload();
    await page.waitForSelector(field('turn.T_silence'));
    await page.waitForFunction(() => !document.getElementById('active-note').hidden);
    assert.match(await page.locator('#active-note').innerText(), /快照.*下一场生效/);
    await page.screenshot({ path: path.join(SHOTS, 'settings-active.png'), fullPage: true });

    // 6) 资料重命名；删除被进行中的会话挡住时给出后端原因
    await page.fill('#profiles input[data-pid]', '设置测试改名');
    await page.click('#profiles button[data-action="save-profile"]');
    await page.waitForFunction(() => document.getElementById('profile-msg').textContent.includes('已重命名'));
    await page.click('#profiles button[data-action="delete-profile"]');
    await page.waitForFunction(() => document.getElementById('profile-msg').textContent.includes('删除失败'));
    assert.match(await page.locator('#profile-msg').innerText(), /进行中的会话|不能删除/);

    // 6b) “编辑资料”展开共用面板：内容/关联库/更新入口与绑定库状态
    await page.click('#profiles button[data-action="edit-profile"]');
    await page.waitForSelector('#pe-content');
    assert.match(await page.locator('#pe-status').innerText(), /共 1 条 · 已启用 1 条/);
    assert.ok(await page.locator('#pe-sync').isVisible(), '更新内容并同步答案库入口可见');
    assert.match(await page.locator('.pe-privacy').innerText(), /仅保存在本机/);

    // 7) 窄屏无横向溢出
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '窄屏无横向溢出');
    await page.screenshot({ path: path.join(SHOTS, 'settings-390.png'), fullPage: true });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
