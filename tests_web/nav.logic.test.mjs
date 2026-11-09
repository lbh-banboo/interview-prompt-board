// gnav.logic：导航项数据、路径高亮映射、会话链接、离开确认判定、汉堡菜单状态。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  TOP_ITEMS, MORE_ITEMS, buildMoreItems, matchRoute, normalizePath, liveHref,
  activeSessionId, needsLeaveConfirm, LEAVE_CONFIRM_TEXT, burgerState, shouldCloseMenuOnResize,
} from '../web/nav/logic.js';

test('导航固定 4 项，顺序与文案固定', () => {
  assert.deepEqual(TOP_ITEMS.map((i) => [i.key, i.label, i.href]), [
    ['home', '首页', '/'],
    ['workspace', '工作台', '/home.html'],
    ['records', '记录库', '/records.html'],
    ['settings', '设置', '/settings.html'],
  ]);
});

test('“更多”收纳 7 项，顺序与分组固定', () => {
  assert.deepEqual(MORE_ITEMS.map((i) => [i.key, i.label, i.href, i.group]), [
    ['import', '导入资料', '/import.html', 'workspace'],
    ['review', '主库审核', '/review.html', 'workspace'],
    ['secondary', '次优库审核', '/secondary.html', 'workspace'],
    ['annotate', '提问点校对', '/annotate.html', 'workspace'],
    ['voice', '声音登记', '/setup.html?step=voice', 'setup'],
    ['setup', '开始会话', '/setup.html', 'setup'],
    ['live', '会话页', '/live.html', 'session'],
  ]);
});

test('matchRoute：12 个页面与查询串的高亮映射', () => {
  const cases = [
    ['/', { top: 'home', more: null }],
    ['/?from=landing', { top: 'home', more: null }],
    ['/index.html', { top: 'home', more: null }],
    ['/home.html', { top: 'workspace', more: null }],
    ['/import.html', { top: 'workspace', more: 'import' }],
    ['/review.html', { top: 'workspace', more: 'review' }],
    ['/secondary.html', { top: 'workspace', more: 'secondary' }],
    ['/annotate.html', { top: 'workspace', more: 'annotate' }],
    ['/records.html', { top: 'records', more: null }],
    ['/records.html?session=a%20b', { top: 'records', more: null }],
    ['/settings.html', { top: 'settings', more: null }],
    ['/setup.html', { top: null, more: 'setup' }],
    ['/setup.html?step=voice', { top: null, more: 'voice' }],
    ['/setup.html?step=voice&x=1', { top: null, more: 'voice' }],
    ['/setup.html?x=1#top', { top: null, more: 'setup' }],
    ['/live.html?session=abc', { top: null, more: 'live' }],
    ['/board.html?session=abc', { top: null, more: 'live' }],
    ['/capture.html?session=abc', { top: null, more: 'live' }],
    ['/unknown.html', { top: null, more: null }],
    ['', { top: null, more: null }],
  ];
  for (const [url, want] of cases) assert.deepEqual(matchRoute(url), want, url);
});

test('normalizePath：去掉尾部斜杠，根路径保持 /', () => {
  assert.equal(normalizePath('/'), '/');
  assert.equal(normalizePath(''), '/');
  assert.equal(normalizePath('/home.html/'), '/home.html');
  assert.equal(normalizePath('///'), '/');
});

test('liveHref 编码 session；activeSessionId 兼容字符串与对象', () => {
  assert.equal(liveHref('a b&c'), '/live.html?session=a%20b%26c');
  assert.equal(liveHref(null), '/setup.html');
  assert.equal(liveHref(''), '/setup.html');
  assert.equal(activeSessionId({ active_session: { id: 's1', mode: 'online' } }), 's1');
  assert.equal(activeSessionId({ active_session: 's2' }), 's2');
  assert.equal(activeSessionId({ active_session: null }), null);
  assert.equal(activeSessionId({ active_session: { mode: 'online' } }), null);
  assert.equal(activeSessionId({ active_session: { id: '' } }), null);
  assert.equal(activeSessionId(null), null);
  assert.equal(activeSessionId('nope'), null);
});

test('buildMoreItems：会话页链接随 active_session 变化，且不改动源数据', () => {
  const off = buildMoreItems(null);
  assert.equal(off.find((i) => i.key === 'live').href, '/setup.html');
  assert.equal(off.length, 7);
  const on = buildMoreItems('a b');
  assert.equal(on.find((i) => i.key === 'live').href, '/live.html?session=a%20b');
  assert.equal(MORE_ITEMS.find((i) => i.key === 'live').href, '/live.html');
  assert.notEqual(off.find((i) => i.key === 'live'), on.find((i) => i.key === 'live'));
});

test('needsLeaveConfirm：仅会话页采集进行中需要确认', () => {
  assert.equal(needsLeaveConfirm({ pathname: '/live.html', phase: 'capturing' }), true);
  assert.equal(needsLeaveConfirm({ pathname: '/live.html/', phase: 'capturing' }), true);
  assert.equal(needsLeaveConfirm({ pathname: '/live.html', phase: 'preparing' }), false);
  assert.equal(needsLeaveConfirm({ pathname: '/live.html', phase: 'ended' }), false);
  assert.equal(needsLeaveConfirm({ pathname: '/board.html', phase: 'capturing' }), false);
  assert.equal(needsLeaveConfirm({ pathname: '/home.html', phase: 'capturing' }), false);
  assert.match(LEAVE_CONFIRM_TEXT, /中断本页的音频采集/);
  assert.match(LEAVE_CONFIRM_TEXT, /会话仍保持进行中/);
  assert.match(LEAVE_CONFIRM_TEXT, /已中断的采集不会自动恢复/);
  assert.ok(!/无缝/.test(LEAVE_CONFIRM_TEXT));
});

test('汉堡菜单状态与 resize 关闭阈值', () => {
  const open = burgerState(true);
  assert.equal(open.expanded, 'true');
  assert.equal(open.label, '关闭菜单');
  assert.equal(open.overlayHidden, false);
  const closed = burgerState(false);
  assert.equal(closed.expanded, 'false');
  assert.equal(closed.label, '打开菜单');
  assert.equal(closed.overlayHidden, true);
  assert.equal(shouldCloseMenuOnResize(true, 721), true);
  assert.equal(shouldCloseMenuOnResize(true, 720), false);
  assert.equal(shouldCloseMenuOnResize(false, 1000), false);
});
