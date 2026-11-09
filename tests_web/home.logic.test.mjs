import test from 'node:test';
import assert from 'node:assert/strict';
import { liveHref, sessionView, navCards, statusLamps, progressView, credentialRows, usageView, configRows, ENV_LOCK_HINT } from '../web/home/logic.js';

test('liveHref 编码 session', () => {
  assert.equal(liveHref('abc'), '/live.html?session=abc');
  assert.equal(liveHref('a b&c'), '/live.html?session=a%20b%26c');
});

test('sessionView / navCards: 会话页合并采集与提示板；无会话时引导 setup', () => {
  const none = sessionView({ active_session: null });
  assert.equal(none.active, false);
  const off = navCards(none).find((c) => c.key === 'live');
  assert.equal(off.disabled, false); assert.equal(off.href, '/setup.html'); assert.match(off.desc, /会前配置向导/);
  const on = sessionView({ active_session: { id: 's1', mode: 'online' } });
  assert.equal(on.active, true); assert.match(on.label, /线上/);
  const cards = navCards(on);
  assert.deepEqual(cards.map((c) => c.key), ['setup', 'import', 'voice', 'review', 'annotate', 'secondary', 'records', 'settings', 'live']);
  assert.equal(cards.find((c) => c.key === 'live').href, '/live.html?session=s1');
  assert.deepEqual(cards.filter((c) => c.key !== 'live').map((c) => c.href), ['/setup.html', '/import.html', '/setup.html?step=voice', '/review.html', '/annotate.html', '/secondary.html', '/records.html', '/settings.html']);
  assert.equal(cards.filter((c) => c.key === 'live').length, 1, '采集页/提示板入口已合并');
  assert.equal(sessionView(null).active, false);
});

test('statusLamps: retrieval ok/degraded/down/none 与中文状态分别映射颜色', () => {
  const byKey = (status, key = 'retrieval') => statusLamps(status).find((x) => x.key === key);
  // /api/status 的 retrieval 只返回 ok/degraded/down/none；ready 从不是服务端取值。
  assert.deepEqual([byKey({ retrieval: 'ok' }).cls, byKey({ retrieval: 'ok' }).text], ['ok', '正常']);
  assert.deepEqual([byKey({ retrieval: 'degraded' }).cls, byKey({ retrieval: 'degraded' }).text], ['warn', '降级']);
  assert.deepEqual([byKey({ retrieval: 'down' }).cls, byKey({ retrieval: 'down' }).text], ['fail', '不可用']);
  assert.deepEqual([byKey({ retrieval: 'none' }).cls, byKey({ retrieval: 'none' }).text], ['idle', '未接入']);
  assert.deepEqual([byKey({ retrieval: '未检测' }).cls, byKey({ retrieval: '未检测' }).text], ['idle', '未检测']);
  // 空值/缺失仍是灰色“未接入”，不能误报绿。
  assert.deepEqual([byKey({}).cls, byKey({}).text], ['idle', '未接入']);

  const l = Object.fromEntries(statusLamps({ asr: '未接入', retrieval: 'ok', model: '', search: 'none', speaker: '未登记' }).map((x) => [x.key, x]));
  assert.equal(l.asr.cls, 'idle'); assert.equal(l.model.cls, 'idle'); assert.equal(l.search.text, '未接入');
  assert.equal(l.speaker.cls, 'idle'); assert.equal(l.speaker.text, '未登记');
  const real = Object.fromEntries(statusLamps({ asr: '云转写', speaker: '已登记', model: '已配置', search: '未接入', retrieval: 'ok' }).map((x) => [x.key, x]));
  assert.equal(real.asr.cls, 'ok'); assert.equal(real.speaker.cls, 'ok'); assert.equal(real.model.cls, 'ok');
  assert.equal(real.search.cls, 'idle');
  const off = Object.fromEntries(statusLamps({ asr: '云转写', speaker: '已登记', model: '已配置', search: '外发关闭', retrieval: 'ok' }).map((x) => [x.key, x]));
  assert.equal(off.search.cls, 'warn'); assert.equal(off.search.text, '外发关闭');
  const noKey = Object.fromEntries(statusLamps({ asr: '云转写', speaker: '已登记', model: '已配置', search: '未配置', retrieval: 'ok' }).map((x) => [x.key, x]));
  assert.equal(noKey.search.cls, 'idle'); assert.equal(noKey.search.text, '未配置');
});

test('progressView: 50 条达成 G3', () => {
  assert.equal(progressView(null), null);
  const a = progressView({ approved: 49, target: 50 });
  assert.equal(a.g3Ready, false); assert.equal(a.pct, 98); assert.match(a.label, /49\/50/);
  const b = progressView({ approved: 50, target: 50 });
  assert.equal(b.g3Ready, true); assert.match(b.label, /G3 达成/);
  assert.equal(progressView({ approved: 80, target: 50 }).pct, 100);
});

test('credentialRows 只含状态、掩码与来源类别，不透出其它字段', () => {
  const rows = credentialRows({ DEEPSEEK_API_KEY: { status: 'configured', mask: 'sk-****abcd', source: 'env', value: 'sk-SECRET' }, SEARCH_KEY: { status: 'missing', mask: 'x' } });
  assert.deepEqual(rows[0], {
    name: 'DEEPSEEK_API_KEY', label: 'DeepSeek', desc: 'AI 生成', configured: true, text: '已配置',
    mask: 'sk-****abcd', source: 'env', sourceLabel: '环境变量', envLocked: true, warning: ENV_LOCK_HINT,
  });
  assert.deepEqual(rows[1], {
    name: 'SEARCH_KEY', label: 'SEARCH_KEY', desc: '', configured: false, text: '未配置',
    mask: '', source: '', sourceLabel: '', envLocked: false, warning: '',
  });
  assert.ok(!JSON.stringify(rows).includes('SECRET'));
  const ui = credentialRows({ DASHSCOPE_API_KEY: { status: 'configured', mask: '****1234', source: 'keyring' } })[0];
  assert.equal(ui.sourceLabel, '钥匙串'); assert.equal(ui.envLocked, false); assert.equal(ui.warning, '');
  assert.deepEqual(credentialRows(null), []);
});

test('usageView / configRows', () => {
  const u = usageView({ est_cost_total: 1.234, daily_limit: 20, rows: [{ service: 'llm', calls: 3, est_cost: 1.2 }], note: '估算，非计费硬保证' });
  assert.match(u.total, /1\.23/); assert.match(u.total, /20\.00/); assert.equal(u.rows[0].text, '模型：3 次，约 1.20');
  assert.match(usageView({}).total, /0\.00/);
  const c = configRows({ config: { model: { name: 'm' }, budget: { daily_limit: 20, session_limit: 5 }, outbound: { asr_audio: true, llm_snippets: false, web_questions: true } }, errors: ['x'] });
  assert.equal(c.length, 4); assert.match(c[2], /模型片段关/);
  assert.deepEqual(configRows(null), []);
});
