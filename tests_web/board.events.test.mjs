// 新事件消费：interrupt 分类、turn_state 原因、队列、子问题、health 新旧结构、text_updated、降级横幅。
import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, fromSnapshot, reduce, statusLabel, turnClosed, levelLabel, queueLabel, clip } from '../web/board/reducer.js';
import { capLamps, bannerFrom } from '../web/board/health.js';

const OUTLINE = { conclusion: '结论', points: ['一'] };
const ev = (state, type, payload = {}, o = {}) =>
  ({ type, session: 's', turn_id: state.turn_id, epoch: state.epoch, event_seq: state.event_seq + 1, text_version: 1, t_server: 0, payload, ...o });
const step = (state, type, payload, o) => reduce(state, ev(state, type, payload, o));
const commit = (state, turn, epoch, text = '问题', extra = {}, tv = 1) => {
  const s = reduce(state, ev(state, 'turn_state', { state: 'question_committed', reason: 'x', waiting_continue: false, answerer_active: false }, { turn_id: turn, epoch }));
  return reduce(s, ev(s, 'transcript', { source: 'meeting', role: 'questioner', text, is_final: true, ...extra }, { turn_id: turn, epoch, text_version: tv }));
};
const tstate = (s, state, reason) => step(s, 'turn_state', { state, reason, waiting_continue: false, answerer_active: state === 'answering' });

test('interrupt: switch / followup / unknown 文案区分，backchannel 不显示', () => {
  const base = commit(initialState('s'), 't1', 1);
  assert.equal(statusLabel(step(base, 'interrupt', { kind: 'switch' })), '已切换到新问题');
  assert.equal(statusLabel(step(base, 'interrupt', { kind: 'followup' })), '追问：已追加到补充区');
  assert.equal(statusLabel(step(base, 'interrupt', { kind: 'unknown' })), '检测到插话，继续判断');
  assert.equal(statusLabel(step(base, 'interrupt', { kind: 'weird' })), '检测到插话，继续判断');
  const bc = step(base, 'interrupt', { kind: 'backchannel' });
  assert.equal(bc.interrupt, null); assert.equal(statusLabel(bc), '已判定'); assert.equal(bc.event_seq, base.event_seq + 1);
});

test('turn_state: answer_end_candidate 只提示不关题；新的 turn_state 清除提示', () => {
  let s = tstate(commit(initialState('s'), 't1', 1), 'answering', 'answerer_started');
  s = tstate(s, 'answering', 'answerer_silence>=T_answer_silence');
  assert.equal(statusLabel(s), '回答可能已结束（等待面试官提问）'); assert.equal(turnClosed(s), false);
  assert.equal(statusLabel(tstate(s, 'answering', 'answer_end_candidate')), '回答可能已结束（等待面试官提问）');
  s = tstate(s, 'answering', 'answerer_active');
  assert.equal(statusLabel(s), '回答中');
});

test('turn_state closed(new_question / queue_pop) 当前题置灰并拒收提示', () => {
  for (const reason of ['new_question', 'queue_pop']) {
    let s = tstate(commit(initialState('s'), 't1', 1), 'answering', 'a');
    s = tstate(s, 'closed', reason);
    assert.equal(turnClosed(s), true); assert.equal(s.closeReason, reason); assert.equal(statusLabel(s), '本题已结束');
    assert.equal(step(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }).lastDrop, 'turn_closed');
  }
});

test('queue_update: 增减替换；队首弹出；label 截断', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'queue_update', { items: ['A', 'B'] });
  assert.deepEqual(s.queue, ['A', 'B']);
  s = step(s, 'queue_update', { items: ['B'] });
  assert.deepEqual(s.queue, ['B']);
  s = step(s, 'queue_update', { items: [] });
  assert.deepEqual(s.queue, []);
  assert.equal(queueLabel(1, 'x'.repeat(100)).length, 3 + 40);
  assert.equal(queueLabel(0, '短'), '1. 短');
  assert.equal(clip('abc', 40), 'abc');
});

test('transcript.sub_questions 记入问题，快照恢复保持', () => {
  const s = commit(initialState('s'), 't1', 1, '主问题', { sub_questions: ['主问题', '第二问'] });
  assert.deepEqual(s.question.sub_questions, ['主问题', '第二问']);
  assert.deepEqual(commit(initialState('s'), 't1', 1, 'q').question.sub_questions, []);
  const r = fromSnapshot({ session_id: 's', question: { text: 'Q', text_version: 1, sub_questions: ['a', 'b'] } });
  assert.deepEqual(r.question.sub_questions, ['a', 'b']);
});

test('health: 新结构（含 since / notices）与旧结构 {asr:{state,reason}} 兼容', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'health', { asr: { state: 'reconnecting', reason: 'x' } });
  assert.equal(s.health.asr.state, 'reconnecting'); assert.deepEqual(s.notices, []);
  s = step(s, 'health', { capture: { state: 'ok', reason: '', since: 1 }, asr: { state: 'down', reason: 'ws', since: 2 }, role: { state: 'degraded', reason: 'r', since: 3 }, notices: ['实时转写不可用', '可能未戴耳机，建议佩戴耳机'] });
  assert.equal(s.health.asr.state, 'down'); assert.equal(s.health.role.state, 'degraded'); assert.equal(s.health.notices, undefined);
  assert.deepEqual(s.notices, ['实时转写不可用', '可能未戴耳机，建议佩戴耳机']);
  s = step(s, 'health', { asr: { state: 'ok', reason: '' } });  // 旧结构不清空 notices 与其他能力
  assert.equal(s.health.role.state, 'degraded'); assert.equal(s.notices.length, 2);
  s = step(s, 'health', { notices: [] });
  assert.deepEqual(s.notices, []);
  const snap = fromSnapshot({ session_id: 's', health: { llm: { state: 'down', reason: 'k', since: 1 }, notices: ['外部补充不可用'] } });
  assert.equal(snap.health.llm.state, 'down'); assert.deepEqual(snap.notices, ['外部补充不可用']);
});

test('capLamps: 颜色映射、旧结构占位、非 ok 带文字', () => {
  const old = capLamps({ asr: { state: 'reconnecting' } });
  assert.deepEqual(old.map((l) => l.key), ['asr', 'llm', 'search']);
  assert.equal(old[0].cls, 'warn'); assert.equal(old[1].cls, 'idle'); assert.equal(old[1].text, '未接入');
  const all = capLamps({ capture: { state: 'ok' }, asr: { state: 'down', reason: '断开' }, role: { state: 'degraded', reason: 'r' } });
  const by = Object.fromEntries(all.map((l) => [l.key, l]));
  assert.equal(by.capture.cls, 'ok'); assert.equal(by.capture.text, '');
  assert.equal(by.asr.cls, 'fail'); assert.equal(by.asr.text, '不可用'); assert.equal(by.asr.reason, '断开');
  assert.equal(by.role.cls, 'warn'); assert.equal(by.role.text, '降级');
});

test('降级横幅：仅取字符串 degraded_banner，其余忽略', () => {
  assert.equal(bannerFrom({ degraded_banner: '搜索连通不可用' }), '搜索连通不可用');
  for (const b of [null, undefined, {}, { degraded_banner: null }, { detail: '会话不存在' }, 'x']) assert.equal(bannerFrom(b), '');
});

test('level 标签', () => {
  assert.equal(levelLabel('L1'), 'L1 我的口径'); assert.equal(levelLabel('L1p'), 'L1p 部分命中');
  assert.equal(levelLabel('L2'), 'L2 通用建议'); assert.equal(levelLabel('Lx'), 'Lx');
});

test('text_updated(非 final transcript): 回答前随新 text_version 更新，回答后不变，主答案不动', () => {
  let s = commit(initialState('s'), 't1', 1, '你能讲讲事务', {}, 1);
  s = step(s, 'prompt_primary', { prompt_id: 'p1', level: 'L1', outline: OUTLINE, source_tag: '主库' });
  const primary = s.primary;
  const upd = (st, text, tv) => step(st, 'transcript', { source: 'meeting', role: 'questioner', text, is_final: false }, { text_version: tv });
  s = upd(s, '你能讲讲事务的区别吗', 2);
  assert.equal(s.question.text, '你能讲讲事务的区别吗'); assert.equal(s.question.text_version, 2);
  assert.equal(s.question.corrected, false); assert.equal(s.primary, primary);
  const same = upd(s, '旧版本回流', 2);
  assert.equal(same.question.text, '你能讲讲事务的区别吗');
  const older = upd(s, '更旧', 1);
  assert.equal(older.question.text, '你能讲讲事务的区别吗');
  let a = tstate(s, 'answering', 'answerer_started');
  a = upd(a, '回答中不应改', 3);
  assert.equal(a.question.text, '你能讲讲事务的区别吗'); assert.equal(a.primary, primary);
  const closed = upd(tstate(s, 'closed', 'done'), '关闭后不应改', 3);
  assert.equal(closed.question.text, '你能讲讲事务的区别吗');
  const was = step(s, 'turn_rollback', { from_version: 2, to_version: 3, reason: 'final_changed', text: 'x' });
  assert.equal(upd(was, '再补全', 4).question.corrected, true);
});

test('乱序 / 旧 epoch / 重复 seq 对新事件同样丢弃', () => {
  let s = commit(commit(initialState('s'), 't1', 1), 't2', 2);
  const r = step(s, 'interrupt', { kind: 'switch' }, { turn_id: 't1', epoch: 1 });
  assert.equal(r.interrupt, null); assert.equal(r.lastDrop, 'stale_epoch');
  const dup = reduce(s, { ...ev(s, 'queue_update', { items: ['x'] }), event_seq: s.event_seq });
  assert.deepEqual(dup.queue, []); assert.equal(dup.lastDrop, 'stale_seq');
  const gap = reduce(s, { ...ev(s, 'health', { asr: { state: 'down' } }), event_seq: s.event_seq + 2 });
  assert.equal(gap.needResync, true); assert.deepEqual(gap.health, {});
});

test('answering 之后 corrected 的 prompt_primary 不替换（回答前可替换）', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_primary', { prompt_id: 'p1', level: 'L1p', outline: OUTLINE, source_tag: '部分命中' });
  const before = step(s, 'prompt_primary', { prompt_id: 'p2', level: 'L1', outline: { conclusion: '改', points: [] }, source_tag: '我的口径', corrected: true });
  assert.equal(before.primary.prompt_id, 'p2');
  const a = tstate(s, 'answering', 'answerer_started');
  const after = step(a, 'prompt_primary', { prompt_id: 'p3', level: 'L1', outline: OUTLINE, source_tag: 'x', corrected: true });
  assert.equal(after.primary, a.primary); assert.equal(after.lastDrop, 'primary_locked');
});
