// 凭证配置 E2E：临时数据目录 + 假 Key。断言保存后清空、状态更新、来源提示、DOM/响应无明文。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');
const KEY = 'sk-fake-e2e-deepseek-1234567890';  // 假 Key，仅本次测试使用

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}
function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'credentials_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  return {
    next: async () => JSON.parse((await lines.next()).value),
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}
const card = (page, name) => page.locator(`.cred[data-name="${name}"]`);

test('凭证配置：保存/删除/环境变量只读提示', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  const backend = startBackend();
  const { port } = await backend.next();
  const page = await env.browser.newPage();
  const responses = [];
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  page.on('response', async (r) => {
    if (/\/api\/credentials/.test(r.url())) { try { responses.push(await r.text()); } catch { /* 忽略已关闭响应 */ } }
  });
  page.on('dialog', (d) => d.accept());
  try {
    await page.goto(`http://127.0.0.1:${port}/home.html`);
    await page.waitForFunction(() => document.querySelectorAll('#creds .cred').length === 3);  // KNOWN_NAMES：3 个核心凭证

    // 环境变量来源：显示类别、明确只读提示，保存/删除禁用
    const asr = card(page, 'DASHSCOPE_API_KEY');
    assert.match(await asr.innerText(), /来源：环境变量/);
    assert.match(await asr.innerText(), /当前由环境变量提供，优先于界面保存的值；如要改用界面配置，请取消该环境变量后重启。/);
    assert.equal(await asr.locator('button.save').isDisabled(), true);
    assert.equal(await asr.locator('button.del').isDisabled(), true);

    // 界面配置：保存后清空输入框、状态变已配置、只显示掩码
    const llm = card(page, 'DEEPSEEK_API_KEY');
    assert.match(await llm.innerText(), /未配置/);
    await llm.locator('input[type="password"]').fill(KEY);
    await llm.locator('button.save').click();
    await page.waitForFunction(() => {
      const el = document.querySelector('.cred[data-name="DEEPSEEK_API_KEY"] .st');
      return el && el.textContent === '已配置';
    });
    assert.equal(await card(page, 'DEEPSEEK_API_KEY').locator('input[type="password"]').inputValue(), '');
    assert.match(await card(page, 'DEEPSEEK_API_KEY').innerText(), /\*\*\*\*7890/);
    assert.equal(await page.evaluate(() => Object.keys(localStorage).length), 0);

    // DOM、响应、控制台均不得出现明文
    assert.ok(!(await page.locator('body').innerText()).includes(KEY));
    assert.ok(!(await page.content()).includes(KEY));
    assert.ok(responses.every((b) => !b.includes(KEY)), '网络响应含明文 Key');
    assert.deepEqual(problems, []);

    // 删除后回到未配置
    await card(page, 'DEEPSEEK_API_KEY').locator('button.del').click();
    await page.waitForFunction(() => {
      const el = document.querySelector('.cred[data-name="DEEPSEEK_API_KEY"] .st');
      return el && el.textContent === '未配置';
    });
    assert.equal(await card(page, 'DEEPSEEK_API_KEY').locator('input[type="password"]').inputValue(), '');
    assert.ok(!(await page.content()).includes(KEY));
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
