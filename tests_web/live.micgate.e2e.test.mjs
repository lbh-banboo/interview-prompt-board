// 线上“仅麦克风”单麦模式 E2E：真实 uvicorn（临时数据目录、注入脚本化声纹 + MockASR、空凭证）
// + 真实 Chromium 假媒体流 → /ws/audio → 管线 → 声纹角色门控 → AnswerLoop → /ws/board → 同页提示板。
// 覆盖：questioner 产生提示；answerer/unknown 不产生提示但计数增加；未登记声纹给出“去登记”入口。
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = path.join(ROOT, 'eval', 'screens');
const PY = process.env.BOARD_PYTHON || path.join(ROOT, '.venv', 'bin', 'python');
const QUESTION = '你能讲讲分布式事务的区别吗';

function appendSilence(file, ms) {
  const buf = readFileSync(file);
  const at = buf.indexOf('data', 12);
  if (at < 0) throw new Error('WAV 缺少 data 块');
  const zero = Buffer.alloc(Math.round(16000 * ms / 1000) * 2);
  const out = Buffer.concat([buf, zero]);
  out.writeUInt32LE(out.length - 8, 4);
  out.writeUInt32LE(buf.readUInt32LE(at + 4) + zero.length, at + 4);
  writeFileSync(file, out);
}

/** 与链路 E2E 相同：say TTS，失败退回本地合成音；尾部静音保证 VAD 能结束语音段。 */
function synthSpeech() {
  const dir = mkdtempSync(path.join(tmpdir(), 'live-micgate-'));
  const wav = path.join(dir, 'question.wav');
  let tts = false;
  try {
    const r = spawnSync('say', ['-v', 'Tingting', '-o', wav, '--data-format=LEI16@16000', QUESTION], { timeout: 20000 });
    tts = r.status === 0 && existsSync(wav) && statSync(wav).size > 2000;
  } catch { tts = false; }
  if (!tts) {
    const rate = 16000;
    const n = rate * 2;
    const pcm = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i++) {
      const env = 0.35 * (0.5 + 0.5 * Math.sin(2 * Math.PI * 2 * i / rate));
      pcm.writeInt16LE(Math.round(8000 * env * Math.sin(2 * Math.PI * 220 * i / rate)), i * 2);
    }
    const header = Buffer.alloc(44);
    const total = 44 + pcm.length;
    header.write('RIFF', 0); header.writeUInt32LE(total - 8, 4); header.write('WAVE', 8);
    header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20);
    header.writeUInt16LE(1, 22); header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28);
    header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
    header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
    writeFileSync(wav, Buffer.concat([header, pcm]));
  }
  appendSilence(wav, 400);
  return wav;
}

function launch(wavFile) {
  return (async () => {
    let chromium;
    try { ({ chromium } = await import('playwright')); } catch (e) { return { why: 'playwright 未安装：' + e.message }; }
    try {
      return { browser: await chromium.launch({ args: [
        '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
        '--autoplay-policy=no-user-gesture-required', '--disable-audio-output', `--use-file-for-fake-audio-capture=${wavFile}`,
      ] }) };
    } catch (e) { return { why: 'Chromium 无法启动：' + String(e.message).split('\n')[0] }; }
  })();
}

function startBackend(args) {
  const child = spawn(PY, [path.join(ROOT, 'tests_web', 'live_chain_e2e_server.py'), ...args],
    { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const next = async () => JSON.parse((await lines.next()).value);
  let done = false;
  return {
    next,
    async cmd(c) { child.stdin.write(JSON.stringify(c) + '\n'); return next(); },
    async stop() {
      if (done) return;
      done = true;
      child.stdin.write('{"op":"quit"}\n'); child.stdin.end();
      await new Promise((r) => child.once('exit', r));
    },
  };
}

function watchProblems(page) {
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  return problems;
}

const waitStep = (page, id, phase) => page.waitForFunction(
  ({ id, phase }) => document.querySelector(`.g-step[data-step="${id}"]`)?.dataset.phase === phase,
  { id, phase }, { timeout: 15000 },
);

async function startMicOnly(page, port, session) {
  await page.goto(`http://127.0.0.1:${port}/live.html?session=${session}`);
  await page.waitForFunction((sid) => document.getElementById('live-sid')?.textContent.includes(sid), session.slice(0, 8));
  await page.check('#consent');
  await page.click('#authorize');
  await waitStep(page, 'authorize', 'done');
  await page.click('#meeting-skip');
  await page.click('#start');
  await page.waitForFunction(() => document.getElementById('live-state')?.textContent === '采集中');
  await page.waitForFunction(() => (document.getElementById('ws')?.textContent || '').includes('已连接'), null, { timeout: 15000 });
  await page.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'));
}

async function withBackend(t, args, fn) {
  const wav = synthSpeech();
  const env = await launch(wav);
  if (!env.browser || !existsSync(PY)) return t.skip('NOT TESTED: ' + (env.why || `缺少 Python 解释器 ${PY}`));
  mkdirSync(SHOTS, { recursive: true });
  const backend = startBackend(args);
  const { port, session } = await backend.next();
  const page = await env.browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = watchProblems(page);
  try {
    await fn({ page, backend, port, session, problems });
    assert.deepEqual(problems, []);
  } finally {
    await page.close();
    await env.browser.close();
    await backend.stop();
  }
}

test('单麦模式 questioner：只有麦克风，声纹判为对方 → 提示板同页出现提示与计数', async (t) => {
  await withBackend(t, ['--mode', 'online', '--role', 'questioner', '--q', QUESTION], async ({ page, backend, port, session }) => {
    await startMicOnly(page, port, session);
    await page.waitForFunction(() => /单麦模式/.test(document.getElementById('q-notice')?.textContent || ''), null, { timeout: 8000 });
    assert.match(await page.locator('#q-notice').textContent(), /声纹区分你和对方/);
    assert.match(await page.locator('#q-notice').textContent(), /外放或同一房间/);
    await page.waitForFunction((q) => document.getElementById('qtext')?.textContent.includes(q), QUESTION, { timeout: 30000 });
    await page.waitForSelector('#vad-cand:not([hidden])', { timeout: 30000 });
    await page.keyboard.press('Enter');  // 用户按 Enter 采用 VAD 候选
    await page.waitForSelector('#primary-body .conclusion', { timeout: 30000 });
    assert.match(await page.locator('#primary-body .conclusion').textContent(), /两阶段提交/);
    await page.waitForFunction(() => /已识别提问 [1-9]\d* 次/.test(document.getElementById('mic-gate')?.textContent || ''));
    assert.match(await page.locator('#mic-gate').textContent(), /未识别 \d+ 次/);
    assert.match(await page.locator('#mic-gate').textContent(), /声纹拿不准 \d+ 次/);
    assert.equal(await page.locator('#q-notice').isVisible(), false, '已有提问时收起模式说明');
    const sent = (await backend.cmd({ op: 'metrics' })).prompt_sent;
    assert.ok(sent.some((s) => s.level === 'L1'), '单麦门控提问经真实链路进入 prompt_sent');
    await page.screenshot({ path: path.join(SHOTS, 'live-miconly-questioner.png') });
  });
});

test('单麦模式 answerer：声纹判为本人 → 不出现提示，未识别计数增加', async (t) => {
  await withBackend(t, ['--mode', 'online', '--role', 'answerer', '--q', QUESTION], async ({ page, backend, port, session }) => {
    await startMicOnly(page, port, session);
    await page.waitForFunction(() => /未识别 [1-9]\d* 次/.test(document.getElementById('mic-gate')?.textContent || ''), null, { timeout: 30000 });
    assert.match(await page.locator('#mic-gate').textContent(), /已识别提问 0 次/);
    assert.match(await page.locator('#q-notice').textContent(), /单麦模式/);
    await page.waitForTimeout(3000);
    assert.equal(await page.locator('#qtext').textContent(), '等待提问…');
    assert.equal(await page.locator('#primary-body').isVisible(), false);
    const metrics = await backend.cmd({ op: 'metrics' });
    assert.deepEqual(metrics.prompt_sent, [], '回答方语音不产生提示');
    await page.screenshot({ path: path.join(SHOTS, 'live-miconly-answerer.png') });
  });
});

test('单麦模式 unknown：声纹拿不准 → 既不当提问也不当回答，只计数', async (t) => {
  await withBackend(t, ['--mode', 'online', '--role', 'unknown', '--q', QUESTION], async ({ page, backend, port, session }) => {
    await startMicOnly(page, port, session);
    await page.waitForFunction(() => /声纹拿不准 [1-9]\d* 次/.test(document.getElementById('mic-gate')?.textContent || ''), null, { timeout: 30000 });
    assert.match(await page.locator('#mic-gate').textContent(), /已识别提问 0 次/);
    assert.match(await page.locator('#mic-gate').textContent(), /未识别 0 次/);
    await page.waitForTimeout(3000);
    assert.equal(await page.locator('#qtext').textContent(), '等待提问…');
    const metrics = await backend.cmd({ op: 'metrics' });
    assert.deepEqual(metrics.prompt_sent, [], '拿不准的语音不产生提示');
    await page.screenshot({ path: path.join(SHOTS, 'live-miconly-unknown.png') });
  });
});

test('单麦模式未登记声纹：显示“去登记”入口，单麦模式不可用且不产生提示', async (t) => {
  await withBackend(t, ['--mode', 'online', '--role', 'questioner', '--no-voice', '--q', QUESTION],
    async ({ page, backend, port, session }) => {
      await startMicOnly(page, port, session);
      await page.waitForFunction(() => /未登记时单麦模式不可用/.test(document.getElementById('q-notice')?.textContent || ''), null,
        { timeout: 8000 });
      const enroll = page.locator('#q-notice a.q-enroll');
      assert.equal(await enroll.isVisible(), true);
      assert.equal(await enroll.getAttribute('href'), '/setup.html?step=voice');
      assert.match(await page.locator('#mic-gate').textContent(), /单麦模式不可用.*声纹未登记/);
      await page.waitForTimeout(4000);
      assert.equal(await page.locator('#qtext').textContent(), '等待提问…');
      const metrics = await backend.cmd({ op: 'metrics' });
      assert.deepEqual(metrics.prompt_sent, [], '未登记声纹时不得把所有麦克风语音当提问');
      await page.screenshot({ path: path.join(SHOTS, 'live-miconly-unenrolled.png') });
    });
});
