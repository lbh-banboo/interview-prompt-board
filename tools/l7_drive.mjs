// L7 真实可见端到端浏览器驱动：真实 Chromium 采集页（AudioWorklet → /ws/audio）+ 提示板（/ws/board → display_ack）。
// 假媒体：Chrome 的 --use-file-for-fake-audio-capture 播放面试音频；mic 轨在 getUserMedia 返回后静音（单音源限制如实计入报告）。
import { chromium } from 'playwright';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).map((a) => {
  const i = a.indexOf('=');
  return i < 0 ? [a.replace(/^--/, ''), true] : [a.slice(2, i), a.slice(i + 1)];
}));
const BASE = args.base;
const LIBRARY = args.library;
const AUDIO = args.audio;
const ANSWER_B64 = readFileSync(args.answer || 'eval/audio_l7/answer_burst.wav').toString('base64');
const OUT = args.out;
const SHOTS = args.shots;
const CATEGORY = args.category || 'tts';
const WAIT_MS = Number(args.wait_ms || 240000);
const ROLES = String(args.roles || '产品经理').split(',').map((s) => s.trim()).filter(Boolean);

mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 正常产品流程：setup 页面向导（资料、岗位、主库、自检）→ 开始会话 → 采集页复用 /api/status 的
// active_session（不再创建旧会话）→ 提示板。驱动不再拦截任何 /api/sessions 请求。
async function setupViaPage(context) {
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push('setup console: ' + m.text()); });
  page.on('pageerror', (e) => problems.push('setup pageerror: ' + e.message));
  await page.goto(`${BASE}/setup.html`);
  await page.waitForSelector('#steps .card');
  await page.locator('#steps .card', { hasText: '线上会议' }).click();
  await page.click('#next');
  await page.waitForSelector('input[placeholder="新资料的姓名代号"]');
  await page.fill('input[placeholder="新资料的姓名代号"]', `L7-${CATEGORY}`);
  await page.getByRole('button', { name: '新建资料' }).click();
  await page.waitForFunction(() => document.querySelector('select.field')?.value);
  await page.click('#next');
  for (const role of ROLES) await page.locator('#steps .chip', { hasText: role }).first().click();
  await page.click('#next');
  const options = await (await fetch(`${BASE}/api/setup/options`)).json();
  const lib = options.libraries.find((l) => l.id === LIBRARY);
  if (!lib) throw new Error('主库不存在：' + LIBRARY);
  await page.locator('#steps .opt', { hasText: lib.name }).first().click();
  await page.click('#next');
  const boxes = page.locator('#steps .check input');
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await page.click('#next');  // 创建 setup 会话并自检
  await page.waitForSelector('#check-list li');
  if (await page.locator('#degrade-row').isVisible()) await page.locator('#degrade-ok').check();
  await page.waitForFunction(() => !document.getElementById('start').disabled, null, { timeout: 15000 });
  await page.click('#start');
  await page.click('#share-ok');
  await page.waitForFunction(() => /已(降级)?开始/.test(document.getElementById('msg')?.textContent || ''), null, { timeout: 180000 });
  const status = await (await fetch(`${BASE}/api/status`)).json();
  const sid = status.active_session?.id;
  if (!sid) throw new Error('setup 页开始后没有 active_session');
  const view = await (await fetch(`${BASE}/api/setup/${sid}`)).json();
  const captureHref = await page.locator('#started-live').getAttribute('href');  // setup 现在只给“进入会话页”入口
  await page.close();
  return { sid, roles: view.roles, selfcheck: view.selfcheck?.overall, degraded: !!view.degraded_banner,
    captureHref, problems };  // 不落盘本地 token
}

const captureInit = (answerB64) => {
  // 1) 回答方（mic）用本地合成回答音频驱动：不是真实麦克风，只用于触发 answerer VAD / 回答结束。
  //    会议音频（meeting）仍走真实 getDisplayMedia。报告里如实标注，不冒充双真实音源。
  const bin = Uint8Array.from(atob(answerB64), (c) => c.charCodeAt(0));
  let actx = null, dest = null, abuf = null;
  const ready = (async () => {
    actx = new AudioContext();
    dest = actx.createMediaStreamDestination();
    abuf = await actx.decodeAudioData(bin.buffer.slice(0));
    await actx.resume();
  })();
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    md.getUserMedia = async () => {
      await ready;
      return dest.stream;
    };
  }
  window.__answer = { play: () => {
    if (!abuf) return false;
    const src = actx.createBufferSource();
    src.buffer = abuf;
    src.connect(dest);
    src.start();
    return true;
  } };
  // 2) 记录音频帧头（source / seq / t_client_us），用于把文件真值映射到浏览器时钟。
  window.__l7c = { meeting0: null, mic0: null, frames: { mic: 0, meeting: 0 }, gens: {} };
  const OW = window.WebSocket;
  class L7WS extends OW {
    send(data) {
      try {
        if (data && data.byteLength >= 24) {
          const dv = new DataView(data.buffer, data.byteOffset, 24);
          const source = dv.getUint8(3);
          const gen = dv.getUint16(10, false);
          const seq = dv.getUint32(14, false);
          const tUs = dv.getUint16(18, false) * 2 ** 32 + dv.getUint32(20, false);
          const sendMs = performance.timeOrigin + performance.now();
          const name = source === 1 ? 'mic' : source === 2 ? 'meeting' : 'other';
          window.__l7c.frames[name] = (window.__l7c.frames[name] || 0) + 1;
          window.__l7c.gens[name] = gen;
          if (name === 'meeting' && seq === 0 && !window.__l7c.meeting0) {
            window.__l7c.meeting0 = { t_client_us: tUs, frame_epoch_ms: performance.timeOrigin + tUs / 1000, send_epoch_ms: sendMs, generation: gen };
          }
          if (name === 'mic' && seq === 0 && !window.__l7c.mic0) {
            window.__l7c.mic0 = { frame_epoch_ms: performance.timeOrigin + tUs / 1000, send_epoch_ms: sendMs, generation: gen };
          }
        }
      } catch { /* 忽略非帧数据 */ }
      return super.send(data);
    }
  }
  window.WebSocket = L7WS;
};

const boardInit = () => {
  window.__l7 = { events: [], acks: [], errors: [] };
  const OW = window.WebSocket;
  class L7WS extends OW {
    constructor(...a) {
      super(...a);
      this.addEventListener('message', (m) => {
        try {
          const d = JSON.parse(m.data);
          if (d && d.type) window.__l7.events.push({ recv_ms: performance.timeOrigin + performance.now(), ...d });
        } catch { /* 非 JSON */ }
      });
    }
    send(data) {
      try {
        const d = JSON.parse(data);
        if (d && d.type === 'display_ack') window.__l7.acks.push({ send_ms: performance.timeOrigin + performance.now(), ...d });
      } catch { /* 非 JSON */ }
      return super.send(data);
    }
  }
  window.WebSocket = L7WS;
  window.addEventListener('error', (e) => window.__l7.errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', (e) => window.__l7.errors.push(String(e.reason)));
};

async function shot(page, idx, label) {
  const file = path.join(SHOTS, `l7-${CATEGORY}-${String(idx).padStart(2, '0')}-${label}.png`);
  await page.screenshot({ path: file });
  return file;
}

async function main() {
  const browser = await chromium.launch({
    headless: true,
    args: [
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${AUDIO}`,
      '--autoplay-policy=no-user-gesture-required',
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows',
    ],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  const result = { category: CATEGORY, base: BASE, library: LIBRARY, audio: AUDIO, screenshots: [], problems };
  try {
    const setup = await setupViaPage(context);
    result.setup = setup;
    problems.push(...setup.problems);
    const cap = await context.newPage();
    cap.on('console', (m) => { if (m.type() === 'error') problems.push('capture console: ' + m.text()); });
    cap.on('pageerror', (e) => problems.push('capture pageerror: ' + e.message));
    await cap.addInitScript(captureInit, ANSWER_B64);
    await cap.goto(`${BASE}/capture.html`);
    await cap.waitForFunction(() => (document.getElementById('message')?.textContent || '').includes('本地服务就绪'));
    await cap.check('#consent');
    await cap.click('#start');
    await cap.waitForFunction(() => (document.getElementById('message')?.textContent || '').includes('采集中'));

    // 等 /api/status 出现 active_session
    let sid = '';
    for (let i = 0; i < 200 && !sid; i++) {
      sid = await cap.evaluate(async () => (await (await fetch('/api/status')).json()).active_session?.id || '');
      if (!sid) await sleep(100);
    }
    if (!sid) throw new Error('会话未创建');
    if (sid !== setup.sid) throw new Error(`采集页没有复用 setup 会话：${sid} != ${setup.sid}`);
    result.session = sid;
    result.t_start_click = await cap.evaluate(() => performance.timeOrigin + performance.now());

    const board = await context.newPage();
    board.on('console', (m) => { if (m.type() === 'error') problems.push('board console: ' + m.text()); });
    board.on('pageerror', (e) => problems.push('board pageerror: ' + e.message));
    await board.addInitScript(boardInit);
    await board.goto(`${BASE}/board.html?session=${encodeURIComponent(sid)}`);
    await board.waitForFunction(() => document.getElementById('lamp-conn')?.classList.contains('ok'), null, { timeout: 15000 });

    // 添加会议音频：真实 getDisplayMedia（Chrome 假媒体把它映射到同一个假音频设备）
    result.t_meeting_click = await cap.evaluate(() => performance.timeOrigin + performance.now());
    await cap.click('#meeting');
    await cap.waitForFunction(() => (document.getElementById('state-meeting')?.textContent || '').includes('采集中'), null, { timeout: 20000 });

    const shotFlags = {};
    let answerSeq = -1;
    const start = Date.now();
    while (Date.now() - start < WAIT_MS) {
      const st = await board.evaluate(() => {
        const ev = window.__l7.events;
        const commits = ev.filter((e) => e.type === 'turn_state' && e.payload?.state === 'question_committed');
        return {
          n: ev.length,
          commits: commits.length,
          primary: ev.filter((e) => e.type === 'prompt_primary').length,
          supp: ev.filter((e) => e.type === 'prompt_supplement').length,
          status: ev.filter((e) => e.type === 'prompt_status').map((e) => e.payload?.text || ''),
          lastCommitRecv: commits.length ? commits[commits.length - 1].recv_ms : 0,
        };
      });
      const commitSeqs = await board.evaluate(() => window.__l7.events
        .filter((e) => e.type === 'turn_state' && e.payload?.state === 'question_committed')
        .map((e) => e.event_seq));
      for (const seq of commitSeqs) {
        if (seq > answerSeq) {
          answerSeq = seq;
          await cap.evaluate(() => window.__answer?.play());
        }
      }
      if (!shotFlags.primary && st.primary > 0) {
        shotFlags.primary = true;
        await sleep(450);
        result.screenshots.push(await shot(board, 1, 'first-primary'));
      }
      if (!shotFlags.supp && st.supp > 0) {
        shotFlags.supp = true;
        await sleep(350);
        result.screenshots.push(await shot(board, 2, 'first-supplement'));
      }
      if (!shotFlags.unavailable && st.status.some((t) => t.includes('外部补充不可用') || t.includes('不可用'))) {
        shotFlags.unavailable = true;
        result.screenshots.push(await shot(board, 3, 'status-unavailable'));
      }
      if (!shotFlags.personal && st.status.some((t) => t.includes('个人经历'))) {
        shotFlags.personal = true;
        result.screenshots.push(await shot(board, 4, 'status-personal'));
      }
      await sleep(300);
    }
    await sleep(1200);
    result.screenshots.push(await shot(board, 5, 'final'));

    result.capture = await cap.evaluate(() => window.__l7c);
    result.board = await board.evaluate(() => window.__l7);
    result.boardDom = await board.evaluate(() => ({
      state: document.getElementById('state')?.textContent,
      qtext: document.getElementById('qtext')?.textContent,
      primary: document.getElementById('primary-body')?.textContent?.slice(0, 400),
      sourceTag: document.getElementById('source-tag')?.textContent,
      supp: [...document.querySelectorAll('#supp-list li')].map((x) => x.textContent?.slice(0, 200)),
      notices: document.getElementById('notices')?.textContent,
    }));

    // 结束会话：让服务端 finalize 录音并落 metrics
    const token = await cap.evaluate(async () => (await (await fetch('/api/status')).json()).token);
    const stop = await fetch(`${BASE}/api/sessions/${sid}/stop`, { method: 'POST', headers: { 'X-Local-Token': token } });
    result.stop_status = stop.status;
    await sleep(500);
  } catch (e) {
    problems.push('driver: ' + String(e && e.stack || e));
    result.fatal = String(e);
  } finally {
    writeFileSync(OUT, JSON.stringify(result, null, 2), 'utf-8');
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
  if (result.fatal) process.exitCode = 1;
}

main();
