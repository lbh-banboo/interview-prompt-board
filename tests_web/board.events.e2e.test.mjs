// 新事件 E2E：interrupt 三类、队列、health 新结构、answer_end_candidate、子问题、降级横幅、text_updated。
// 浏览器或 Python 不可用时 skip（NOT TESTED）。
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
const OUTLINE = { conclusion: '先讲两阶段提交，再讲最终一致', points: ['2PC 强一致但阻塞', 'TCC 业务补偿'], examples: '订单支付', cautions: '不要说绝对不丢' };
const HEALTH = {
  capture: { state: 'ok', reason: '', since: 1 }, asr: { state: 'down', reason: 'asr 连接断开', since: 2 }, role: { state: 'degraded', reason: '角色不确定', since: 3 },
  retrieval: { state: 'ok', reason: '', since: 1 }, llm: { state: 'ok', reason: '', since: 1 }, search: { state: 'degraded', reason: '搜索超时', since: 4 },
  voice: { state: 'ok', reason: '', since: 1 }, disk: { state: 'ok', reason: '', since: 1 },
  notices: ['实时转写不可用', '可能未戴耳机，建议佩戴耳机'],
};

async function launch() {
  let chromium;
  try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
  try { return { browser: await chromium.launch() }; } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
}
function startBackend() {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'board_e2e_server.py')], { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const next = async () => JSON.parse((await lines.next()).value);
  return {
    next,
    async cmd(c) { child.stdin.write(JSON.stringify(c) + '\n'); return next(); },
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}
const box = (page, sel) => page.locator(sel).boundingBox();
const overlap = (a, b) => Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));

for (const [w, h] of [[1440, 900], [1920, 1080]]) {
  test(`board 新事件 E2E ${w}x${h}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    mkdirSync(SHOTS, { recursive: true });
    const backend = startBackend();
    const { port, session } = await backend.next();
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    const text = (sel) => page.locator(sel).textContent();
    const waitState = (s) => page.waitForFunction((x) => document.getElementById('state').textContent === x, s);
    const shot = (n) => page.screenshot({ path: path.join(SHOTS, `board-events-${w}x${h}-${n}.png`) });
    const pub = (method, ...args) => backend.cmd({ op: 'publish', method, args });
    try {
      // /api/setup 路由尚未在 server.py 接线：用路由拦截模拟其响应（SIMULATED）
      await page.route(`**/api/setup/${session}`, (r) => r.fulfill({ json: { id: session, degraded_banner: '搜索连通不可用，已降级' } }));
      await page.goto(`http://127.0.0.1:${port}/board.html?session=${session}`);
      await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));
      await page.waitForFunction(() => document.querySelector('.banner-degraded'));
      assert.equal(await text('.banner-degraded'), '搜索连通不可用，已降级');

      // 子问题 + L1p 标签
      await backend.cmd({ op: 'turn', type: 'committed', text: '先讲分布式事务，再讲缓存', sub: ['先讲分布式事务', '再讲缓存'] });
      await page.waitForFunction(() => document.querySelectorAll('#subq li').length === 2);
      assert.match(await text('#subq li:first-child'), /^主 /);
      await pub('publish_prompt_primary', 't1', 1, OUTLINE, 'L1p', '部分命中');
      await page.waitForSelector('#primary-body .conclusion');
      assert.equal(await text('#level'), 'L1p 部分命中');
      await page.waitForFunction(() => document.getAnimations().every((a) => { const t = a.effect?.getComputedTiming(); return t.iterations === Infinity || a.playState !== 'running'; }));  // 有限动画结束后再量位置（无限脉冲动画不阻塞）
      const base = { primary: await box(page, '#primary'), question: await box(page, '.question'), bar: await box(page, '.bar'), notices: await box(page, '#notices') };
      await shot('1-subq');

      // health 新结构：灯 + 常驻文案；版面不动
      await pub('publish', 'health', HEALTH);
      await page.waitForFunction(() => document.querySelectorAll('#caps .lamp').length === 8);
      assert.match(await page.locator('#cap-asr').getAttribute('class'), /fail/);
      assert.match(await page.locator('#cap-role').getAttribute('class'), /warn/);
      assert.match(await page.locator('#cap-capture').getAttribute('class'), /ok/);
      assert.match(await page.locator('#cap-asr').getAttribute('title'), /asr 连接断开/);
      assert.match(await text('#notices'), /实时转写不可用/);
      assert.match(await text('#notices'), /建议佩戴耳机/);
      await shot('2-health');

      // text_updated：回答前文本随新 text_version 更新，主答案不动
      await backend.cmd({ op: 'turn', type: 'text_updated', text: '先讲分布式事务的区别，再讲缓存策略', ver: 2 });
      await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('的区别'));
      assert.equal(await page.locator('#qbadge').isHidden(), true);
      assert.match(await text('#primary .conclusion'), /两阶段提交/);

      // interrupt：追问 / 不明
      await backend.cmd({ op: 'turn', type: 'interrupt', kind: 'followup', text: '那超时怎么办' });
      await waitState('追问：已追加到补充区');
      await page.waitForFunction(() => document.querySelectorAll('#supp-list li').length === 1);
      await backend.cmd({ op: 'turn', type: 'interrupt', kind: 'unknown', text: '嗯这个' });
      await waitState('检测到插话，继续判断');
      await shot('3-interrupt');

      // 队列增减
      await backend.cmd({ op: 'turn', type: 'queued', text: '第二个问题：你们怎么做限流' });
      await backend.cmd({ op: 'turn', type: 'queued', text: '第三个问题：' + '很长'.repeat(40) });
      await page.waitForFunction(() => document.querySelectorAll('#queue li').length === 2);
      assert.match(await text('#queue li:first-child'), /^1\. 第二个问题/);
      assert.ok((await text('#queue li:nth-child(2)')).length <= 3 + 40);
      await backend.cmd({ op: 'turn', type: 'dequeued', text: '第二个问题：你们怎么做限流' });
      await page.waitForFunction(() => document.querySelectorAll('#queue li').length === 1);
      assert.match(await text('#queue li'), /^1\. 第三个问题/);

      // answer_end_candidate：只提示，不关题
      await backend.cmd({ op: 'turn', type: 'answer_end_candidate', reason: 'answerer_silence>=T_answer_silence' });
      await waitState('回答可能已结束（等待面试官提问）');
      assert.equal(await page.locator('#board.closed').count(), 0);
      await shot('4-answer-end');

      // 追加补充 / 队列变化 / 健康后：主答案、问题框、状态条、文案条位置不变
      assert.deepEqual({ primary: await box(page, '#primary'), question: await box(page, '.question'), bar: await box(page, '.bar'), notices: await box(page, '#notices') }, base);

      // 无遮挡
      const regions = ['.bar', '#notices', '.question', '#primary', '.supp', '.cand', '.queue'];
      const boxes = {};
      for (const r of regions) {
        boxes[r] = await box(page, r);
        assert.ok(boxes[r].x >= 0 && boxes[r].y >= 0 && boxes[r].x + boxes[r].width <= w && boxes[r].y + boxes[r].height <= h, `${r} 在视口内`);
      }
      for (let i = 0; i < regions.length; i++) for (let j = i + 1; j < regions.length; j++) assert.equal(overlap(boxes[regions[i]], boxes[regions[j]]), 0, `${regions[i]} 与 ${regions[j]} 不重叠`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));

      // Esc / H 隐藏恢复 < 100ms
      const toggle = (key) => page.evaluate(async (k) => {
        const t0 = performance.now();
        document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
        await new Promise((r) => requestAnimationFrame(() => r()));
        return performance.now() - t0;
      }, key);
      assert.ok(await toggle('Escape') < 100);
      assert.equal(await page.locator('#board').evaluate((e) => getComputedStyle(e).visibility), 'hidden');
      assert.ok(await toggle('h') < 100);
      assert.equal(await page.locator('#board').evaluate((e) => getComputedStyle(e).visibility), 'visible');
      assert.deepEqual(await box(page, '#primary'), base.primary);

      // closed(queue_pop) 置灰；新题；explicit switch
      await backend.cmd({ op: 'turn', type: 'answer_closed', reason: 'queue_pop' });
      await page.waitForSelector('#board.closed');
      await waitState('本题已结束');
      await backend.cmd({ op: 'turn', type: 'committed', text: '第三个问题：限流' });
      await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('限流'));
      assert.equal(await page.locator('#board.closed').count(), 0);
      assert.equal(await page.locator('#subq').isHidden(), true);
      await backend.cmd({ op: 'turn', type: 'interrupted', reason: 'explicit_switch' });
      await waitState('已切换到新问题');
      await shot('5-switch');
      assert.deepEqual(problems, []);
    } finally {
      await page.close();
      await env.browser.close();
      await backend.stop();
    }
  });
}
