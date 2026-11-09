// landing.logic：统计聚合与容错、数字格式化与 count-up 终值、汉堡菜单状态、最近会话筛选。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DASH, sumApproved, secondaryApproved, sessionsCount, asrText, buildStats,
  formatCount, easeOutCubic, countUpValue, finalStatValue,
  menuState, toggleMenu, shouldCloseMenuOnResize,
  recentSessions, formatSessionTime,
} from '../web/landing/logic.js';

test('sumApproved: 汇总主库 approved；接口失败为 null；空数组/缺失为 0', () => {
  assert.equal(sumApproved([{ approved: 2 }, { approved: 1 }, { approved: null }]), 3);
  assert.equal(sumApproved([]), 0);
  assert.equal(sumApproved([{ total: 5 }]), 0);
  assert.equal(sumApproved(null), null);
  assert.equal(sumApproved('x'), null);
});

test('secondaryApproved / sessionsCount: 缺 total 时回退列表长度', () => {
  assert.equal(secondaryApproved({ approved: 7 }), 7);
  assert.equal(secondaryApproved({}), null);
  assert.equal(secondaryApproved(null), null);
  assert.equal(sessionsCount({ total: 12, sessions: [] }), 12);
  assert.equal(sessionsCount({ sessions: [{}, {}] }), 2);
  assert.equal(sessionsCount({}), null);
  assert.equal(sessionsCount(null), null);
});

test('asrText: 优先 /api/status 的 asr 文案，其次 asr_backend 映射', () => {
  assert.equal(asrText({ asr: '本地转写' }), '本地转写');
  assert.equal(asrText({ asr_backend: 'aliyun' }), '云转写');
  assert.equal(asrText({ asr_backend: 'sherpa' }), '本地转写');
  assert.equal(asrText({ asr_backend: 'none' }), '未接入');
  assert.equal(asrText({}), null);
  assert.equal(asrText(null), null);
});

test('buildStats: 四项真实计数；任一接口失败显示 —，不编造 0', () => {
  const ok = buildStats({
    libraries: [{ approved: 2 }, { approved: 1 }],
    secondary: { approved: 5 },
    sessions: { total: 2 },
    status: { asr: '本地转写' },
  });
  assert.deepEqual(ok.map((s) => [s.label, s.text]), [
    ['主库已启用', '3'], ['次优库已通过', '5'], ['历史会话', '2'], ['转写方式', '本地转写'],
  ]);
  assert.deepEqual(ok.map((s) => s.value), [3, 5, 2, null]);

  const failed = buildStats({ libraries: null, secondary: null, sessions: null, status: null });
  assert.deepEqual(failed.map((s) => s.text), [DASH, DASH, DASH, DASH]);
  assert.deepEqual(failed.map((s) => s.value), [null, null, null, null]);

  // 空库是真实状态：显示 0，而不是 —
  const empty = buildStats({ libraries: [], secondary: { approved: 0 }, sessions: { sessions: [] }, status: {} });
  assert.deepEqual(empty.map((s) => s.text), ['0', '0', '0', DASH]);
});

test('formatCount / count-up：终值等于目标，过程值单调', () => {
  assert.equal(formatCount(12), '12');
  assert.equal(finalStatValue(3), '3');
  assert.equal(countUpValue(3, 0), '0');
  assert.equal(countUpValue(3, 1), '3');
  assert.equal(countUpValue(99.99, 1, 2), '99.99');
  assert.equal(easeOutCubic(0), 0);
  assert.equal(easeOutCubic(1), 1);
  assert.ok(easeOutCubic(0.5) > 0.5, 'easeOutCubic 前快后慢');
  assert.ok(Number(countUpValue(10, 0.5)) <= 10);
});

test('menuState / toggleMenu / shouldCloseMenuOnResize', () => {
  const open = menuState(true);
  assert.equal(open.expanded, 'true');
  assert.equal(open.label, '关闭菜单');
  assert.equal(open.overlayHidden, false);
  const closed = menuState(false);
  assert.equal(closed.expanded, 'false');
  assert.equal(closed.label, '打开菜单');
  assert.equal(closed.overlayHidden, true);
  assert.equal(toggleMenu(false), true);
  assert.equal(toggleMenu(true), false);
  assert.equal(shouldCloseMenuOnResize(true, 721), true);
  assert.equal(shouldCloseMenuOnResize(true, 720), false);
  assert.equal(shouldCloseMenuOnResize(false, 1000), false);
});

test('recentSessions: 过滤已删除、按开始时间倒序、最多 3 条、文案与深链', () => {
  const res = {
    total: 4,
    sessions: [
      { id: 's1', mode: 'online', status: 'stopped', started_at: 1_700_000_000, deleted: true },
      { id: 's2', mode: 'offline', status: 'active', started_at: 1_700_000_500 },
      { id: 's3', mode: 'online', status: 'stopped', started_at: 1_700_000_300 },
      { id: 's4', mode: 'online', status: 'stopped', started_at: 1_700_000_100 },
      { id: 's5', mode: 'online', status: 'stopped', started_at: 1_699_999_000 },
    ],
  };
  const items = recentSessions(res);
  assert.deepEqual(items.map((i) => i.id), ['s2', 's3', 's4']);
  assert.equal(items[0].modeLabel, '线下');
  assert.equal(items[0].statusLabel, '进行中');
  assert.equal(items[1].statusLabel, '已结束');
  assert.match(items[1].timeLabel, /^\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(items[0].href, '/records.html?session=s2');
  assert.equal(recentSessions({ sessions: [{ id: 'a b' }] })[0].href, '/records.html?session=a%20b');
  assert.deepEqual(recentSessions(null), []);
  assert.deepEqual(recentSessions({ sessions: [] }), []);
  // 无 started_at 的旧会话不崩溃
  const legacy = recentSessions({ sessions: [{ id: 'old', mode: '', status: '' }] });
  assert.equal(legacy[0].timeLabel, '—');
  assert.equal(legacy[0].modeLabel, '—');
});

test('formatSessionTime: 非法输入返回 —', () => {
  assert.equal(formatSessionTime(null), '—');
  assert.equal(formatSessionTime(Number.NaN), '—');
});
