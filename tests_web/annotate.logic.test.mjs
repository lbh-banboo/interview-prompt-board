import test from 'node:test';
import assert from 'node:assert/strict';
import {
  clipWindow, fmtTime, guessLabel, isJudged, itemUrl, audioUrl, judgedLabel, keyAction, nextTodoId,
  noteOf, progress, stepId, verdictBody, visibleItems,
} from '../web/annotate/logic.js';

const H = (o = {}) => ({ is_question_end: null, role: null, is_topic_change: null, note: '', ...o });
const I = (id, o = {}) => ({ id, t_end_ms: id * 1000, text_final: '句' + id, guess_role: 'questioner', guess_kind: 'question', verified: false, human: H(), ...o });
const done = (id, human = {}) => I(id, { verified: true, human: H({ is_question_end: true, role: 'questioner', is_topic_change: false, ...human }) });
const set = [done(1), I(2), I(3), done(4, { is_question_end: false })];

test('进度：未判定不计入已判定数', () => {
  const p = progress(set);
  assert.deepEqual([p.judged, p.total, p.pct, p.label], [2, 4, 50, '已判定 2/4']);
  assert.deepEqual(progress([]), { total: 0, judged: 0, pct: 0, label: '已判定 0/0' });
  assert.equal(isJudged(I(9)), false);
  assert.equal(isJudged({ ...I(9), verified: true }), true);
});

test('筛选未判定 / 已判定 / 全部', () => {
  assert.deepEqual(visibleItems(set, 'todo').map((x) => x.id), [2, 3]);
  assert.deepEqual(visibleItems(set, 'done').map((x) => x.id), [1, 4]);
  assert.deepEqual(visibleItems(set, 'all').map((x) => x.id), [1, 2, 3, 4]);
});

test('上下条：边界夹住，当前条不在视图内时从首条开始', () => {
  const rows = visibleItems(set, 'todo');
  assert.equal(stepId(rows, 2, 1), 3);
  assert.equal(stepId(rows, 3, 1), 3);
  assert.equal(stepId(rows, 2, -1), 2);
  assert.equal(stepId(rows, 1, 1), 2);
  assert.equal(stepId(rows, 99, 1), 2);
  assert.equal(stepId([], 2, 1), 0);
});

test('判定后自动找下一条未判定（绕回），全部判完返回 0', () => {
  assert.equal(nextTodoId(set, 1), 2);
  assert.equal(nextTodoId(set, 3), 2);   // 绕回
  assert.equal(nextTodoId(set, 4), 2);
  assert.equal(nextTodoId([done(1), done(2)], 1), 0);
});

test('自动播放窗口：前 5 秒到后 2 秒，不早于 0', () => {
  assert.deepEqual(clipWindow(6960), { start: 1.96, end: 8.96 });
  assert.deepEqual(clipWindow(1000), { start: 0, end: 3 });
});

test('时间与猜测文案', () => {
  assert.equal(fmtTime(6960), '00:07.0');
  assert.equal(fmtTime(119123), '01:59.1');
  assert.equal(fmtTime(-5), '00:00.0');
  assert.equal(guessLabel(set[0]), '面试官?/问句');
  assert.equal(guessLabel({ ...set[0], guess_role: 'unknown', guess_kind: 'statement' }), '未知/陈述');
});

test('判定文案', () => {
  assert.equal(judgedLabel(I(2)), '未判定');
  assert.equal(judgedLabel(done(5)), '是');
  assert.equal(judgedLabel(done(5, { is_topic_change: true })), '改题');
  assert.equal(judgedLabel(done(5, { is_followup: true })), '追问');
  assert.equal(judgedLabel(done(5, { is_question_end: false, is_topic_change: null })), '不是');
  assert.equal(noteOf(I(2, { human: H({ note: '听不清' }) })), '听不清');
  assert.equal(noteOf(I(2)), '');
});

test('键位：忽略输入框与修饰键', () => {
  assert.equal(keyAction({ key: 'y' }), 'yes');
  assert.equal(keyAction({ key: 'n' }), 'no');
  assert.equal(keyAction({ key: 'f' }), 'followup');
  assert.equal(keyAction({ key: 't' }), 'topic_change');
  assert.equal(keyAction({ key: 's' }), 'skip');
  assert.equal(keyAction({ key: 'r' }), 'replay');
  assert.equal(keyAction({ key: 'j' }), 'next');
  assert.equal(keyAction({ key: 'k' }), 'prev');
  assert.equal(keyAction({ key: 'x' }), null);
  assert.equal(keyAction({ key: 'y', ctrlKey: true }), null);
  assert.equal(keyAction({ key: 'y', target: { tagName: 'TEXTAREA' } }), null);
  assert.equal(keyAction({ key: 'y', target: { tagName: 'INPUT' } }), null);
  assert.equal(keyAction({ key: 'y', target: { tagName: 'DIV', isContentEditable: true } }), null);
});

test('请求体与地址编码', () => {
  assert.deepEqual(verdictBody('yes', '备注'), { verdict: 'yes', note: '备注' });
  assert.deepEqual(verdictBody(null, ''), { verdict: null, note: '' });
  assert.equal(itemUrl('懂车 2', 7), '/api/annotations/%E6%87%82%E8%BD%A6%202/items/7');
  assert.equal(audioUrl('pdd'), '/api/annotations/pdd/audio');
});
