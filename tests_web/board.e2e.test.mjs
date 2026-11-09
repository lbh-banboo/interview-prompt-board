// M11-T1/T2/T3/T4 E2E：真实后端（test 模式灌入脚本化事件）+ Playwright 自带 Chromium（不使用系统 Chrome）。
// 浏览器或 Python 环境不可用时整组标记 NOT TESTED（skip），不会假绿。
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
const OUTLINE = { conclusion: '先讲两阶段提交，再讲最终一致', points: ['2PC 强一致但阻塞', 'TCC 业务补偿', '本地消息表 + 重试'], examples: '订单支付：库存预留 + 超时回滚', cautions: '不要说“绝对不丢”' };

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
    child, next,
    async cmd(c) { child.stdin.write(JSON.stringify(c) + '\n'); return next(); },
    async stop() { child.stdin.write('{"op":"quit"}\n'); child.stdin.end(); await new Promise((r) => child.once('exit', r)); },
  };
}

const box = (page, sel) => page.locator(sel).boundingBox();
const overlap = (a, b) => Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));

for (const [w, h] of [[1440, 900], [1920, 1080]]) {
  test(`board E2E ${w}x${h}`, async (t) => {
    const env = await launch();
    if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
    mkdirSync(SHOTS, { recursive: true });
    const backend = startBackend();
    const { port, session } = await backend.next();
    const page = await env.browser.newPage({ viewport: { width: w, height: h } });
    const problems = [];
    page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
    page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
    try {
      await page.goto(`http://127.0.0.1:${port}/board.html?session=${session}`);
      await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-0-empty.png`) });

      // 听题 -> 候选 -> 判定
      await backend.cmd({ op: 'turn', type: 'candidate', text: '你能讲讲分布式事务' });
      await page.waitForFunction(() => document.getElementById('state').textContent === '候选');
      assert.match(await page.locator('#cand summary').textContent(), /候选，未确认/);
      await backend.cmd({ op: 'turn', type: 'committed', text: '你能讲讲分布式事务的区别吗' });
      await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('分布式事务的区别'));
      assert.equal(await page.locator('#state').textContent(), '已判定');

      // 主答案出现 + display_ack
      await backend.cmd({ op: 'publish', method: 'publish_prompt_primary', args: ['t1', 1, OUTLINE, 'L1', '主库'] });
      await page.waitForSelector('#primary-body .conclusion');
      await page.waitForFunction(async () => true);
      for (let i = 0; i < 50 && (await backend.cmd({ op: 'count_acks' })).acks < 1; i++) await new Promise((r) => setTimeout(r, 50));
      assert.equal((await backend.cmd({ op: 'count_acks' })).acks, 1, 'display_ack 已写入 metrics');
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-1-primary.png`) });

      // 字号：默认 >=18px，结论 >=24px
      const size = (sel) => page.locator(sel).first().evaluate((e) => parseFloat(getComputedStyle(e).fontSize));
      assert.ok(await size('#primary .points li') >= 18);
      assert.ok(await size('#primary .conclusion') >= 24);
      assert.ok(await size('#qtext') >= 18);

      // M11-T2：追加 10 条补充，主答案 boundingBox 不变
      await page.waitForFunction(() => document.getAnimations().every((a) => { const t = a.effect?.getComputedTiming(); return t.iterations === Infinity || a.playState !== 'running'; }));  // 有限动画结束后再量位置（无限脉冲动画不阻塞）
      const before = { primary: await box(page, '#primary'), body: await box(page, '#primary-body'), question: await box(page, '.question') };
      for (let i = 0; i < 10; i++) {
        await backend.cmd({ op: 'publish', method: 'publish_supplement', args: ['t1', 1, '联网要点', `补充要点 ${i + 1}：一条较长的补充说明用来占位`, null, 300 + i] });
      }
      await page.waitForFunction(() => document.querySelectorAll('#supp-list li').length === 10);
      for (let i = 0; i < 50 && (await backend.cmd({ op: 'count_acks' })).acks < 11; i++) await new Promise((r) => setTimeout(r, 50));
      assert.equal((await backend.cmd({ op: 'count_acks' })).acks, 11, '主答案 + 10 条补充都回报 display_ack');
      assert.deepEqual({ primary: await box(page, '#primary'), body: await box(page, '#primary-body'), question: await box(page, '.question') }, before);
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-2-supplements.png`) });

      // 无遮挡：各区域在视口内、互不重叠，主答案中心点命中自身，页面无横向溢出
      const regions = ['.bar', '.question', '#primary', '.supp', '.cand', '.queue'];
      const boxes = {};
      for (const r of regions) {
        boxes[r] = await box(page, r);
        assert.ok(boxes[r].x >= 0 && boxes[r].y >= 0 && boxes[r].x + boxes[r].width <= w && boxes[r].y + boxes[r].height <= h, `${r} 在视口内`);
      }
      for (let i = 0; i < regions.length; i++) for (let j = i + 1; j < regions.length; j++) assert.equal(overlap(boxes[regions[i]], boxes[regions[j]]), 0, `${regions[i]} 与 ${regions[j]} 不重叠`);
      assert.ok(await page.evaluate(() => { const r = document.getElementById('primary').getBoundingClientRect(); return !!document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.closest('#primary'); }));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));

      // M11-T3：Esc 隐藏 / H 恢复 < 100ms，后台事件不中断
      const toggle = (key) => page.evaluate(async (k) => {
        const t0 = performance.now();
        document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
        await new Promise((r) => requestAnimationFrame(() => r()));
        return performance.now() - t0;
      }, key);
      assert.ok(await toggle('Escape') < 100);
      assert.equal(await page.locator('#board').evaluate((e) => getComputedStyle(e).visibility), 'hidden');
      await backend.cmd({ op: 'publish', method: 'publish_supplement', args: ['t1', 1, '模型补充', '隐藏期间到达的补充', null, null] });
      await page.waitForFunction(() => document.querySelectorAll('#supp-list li').length === 11);
      assert.ok(await toggle('h') < 100);
      assert.equal(await page.locator('#board').evaluate((e) => getComputedStyle(e).visibility), 'visible');
      assert.deepEqual(await box(page, '#primary'), before.primary);
      await page.keyboard.press('f');
      assert.ok(await size('#primary .conclusion') > 24 * 1.1);
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-3-font.png`) });
      await page.keyboard.press('Shift+F');

      // 刷新恢复：先拉快照，主答案、补充、位置保持
      await page.reload();
      await page.waitForFunction(() => document.querySelectorAll('#supp-list li').length === 11);
      assert.match(await page.locator('#primary .conclusion').textContent(), /两阶段提交/);
      assert.deepEqual(await box(page, '#primary'), before.primary);

      // 本题结束置灰；新题进入后旧提示被替换，旧题迟到事件在发布层被丢弃
      await backend.cmd({ op: 'publish', method: 'close_turn', args: [] });
      await page.waitForSelector('#board.closed');
      assert.equal((await backend.cmd({ op: 'publish', method: 'publish_supplement', args: ['t1', 1, '联网', '迟到', null, null] })).published, false);
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-4-closed.png`) });
      await backend.cmd({ op: 'turn', type: 'committed', text: '那你们的缓存策略是什么' });
      await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('缓存策略'));
      assert.equal(await page.locator('#primary-body .conclusion').count(), 0);
      assert.equal(await page.locator('#supp-list li').count(), 0);
      assert.equal(await page.locator('#board.closed').count(), 0);
      // 状态提示也要渲染后回报 ACK（无主答案时状态区可见）
      const beforeStatus = (await backend.cmd({ op: 'count_acks' })).acks;
      await backend.cmd({ op: 'publish', method: 'publish_prompt_status', args: ['t2', 2, '外部补充不可用'] });
      await page.waitForFunction(() => document.getElementById('primary-empty').textContent.includes('外部补充不可用'));
      for (let i = 0; i < 50 && (await backend.cmd({ op: 'count_acks' })).acks <= beforeStatus; i++) await new Promise((r) => setTimeout(r, 50));
      assert.equal((await backend.cmd({ op: 'count_acks' })).acks, beforeStatus + 1, '状态提示回报 display_ack');
      assert.equal((await backend.cmd({ op: 'publish', method: 'publish_prompt_primary', args: ['t1', 1, OUTLINE, 'L1', '迟到'] })).published, false);
      await page.screenshot({ path: path.join(SHOTS, `board-${w}x${h}-5-next-turn.png`) });

      // 会话结束整体置灰
      await backend.cmd({ op: 'publish', method: 'end', args: [] });
      await page.waitForFunction(() => document.getElementById('state').textContent === '已结束');
      assert.deepEqual(problems, []);
    } finally {
      await page.close();
      await env.browser.close();
      await backend.stop();
    }
  });
}

test('board 红标与常驻状态：flagged 主答案、主答案下方的待补充条、vad 候选卡', async (t) => {
  const env = await launch();
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend();
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  try {
    await page.goto(`http://127.0.0.1:${port}/board.html?session=${session}`);
    await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));
    await backend.cmd({ op: 'turn', type: 'committed', text: '讲讲你们项目的取舍' });
    await page.waitForFunction(() => document.getElementById('qtext').textContent.includes('取舍'));

    await backend.cmd({ op: 'publish', method: 'publish_prompt_primary',
      args: ['t1', 1, OUTLINE, 'L1', '含待核内容', false, true, 2] });
    await page.waitForSelector('#primary-body .conclusion');
    await page.waitForFunction(() => !document.getElementById('primary-alert')?.hidden);
    assert.match(await page.locator('#primary-alert').textContent(), /含未核实内容，非已确认口径/);
    assert.match(await page.locator('#primary-alert').textContent(), /待核 2 处/);
    assert.equal(await page.locator('#source-tag').textContent(), '含待核内容');

    // 主答案存在时，状态提示不能被吞掉：常驻在主答案下方
    await backend.cmd({ op: 'publish', method: 'publish_prompt_status', args: ['t1', 1, '待补充：个人经历需本人补充'] });
    await page.waitForFunction(() => document.getElementById('primary-status')?.textContent.includes('需本人补充'));
    assert.ok(await page.locator('#primary-status').isVisible());
    assert.ok(await page.locator('#primary-body .conclusion').isVisible());

    // VAD 候选：可能未问完，按 Enter 采用
    await backend.cmd({ op: 'publish', method: 'publish_prompt_candidate',
      args: ['t1', 1, OUTLINE, 'L1', '我的口径', [{ entry_id: 'H1-01', question: 'Q', conclusion: '候选结论' }], '你能讲讲分布式事务', false, 0, 1] });
    await page.waitForFunction(() => !document.getElementById('vad-cand')?.hidden);
    assert.match(await page.locator('#vad-cand').textContent(), /可能未问完 · 按 Enter 采用/);
    assert.match(await page.locator('#vad-cand').textContent(), /先讲两阶段提交/);
    await page.screenshot({ path: path.join(SHOTS, 'board-flagged-vad.png') });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
});
