import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, fromSnapshot, reduce, statusLabel, turnClosed, levelLabel, queueLabel, clip } from '../web/board/reducer.js';
import { capLamps, bannerFrom } from '../web/board/health.js';
import { keyAction, nextScale, SCALES } from '../web/board/keys.js';

const OUTLINE = { conclusion: '先说结论', points: ['一', '二', '三'], examples: '例', cautions: '注意' };
let seq;
const ev = (state, type, payload = {}, o = {}) =>
  ({ type, session: 's', turn_id: state.turn_id, epoch: state.epoch, event_seq: state.event_seq + 1, text_version: 1, t_server: 0, payload, ...o });
const step = (state, type, payload, o) => reduce(state, ev(state, type, payload, o));
const commit = (state, turn, epoch, text = '问题') => {
  let s = reduce(state, ev(state, 'turn_state', { state: 'question_committed', reason: 'x', waiting_continue: false, answerer_active: false }, { turn_id: turn, epoch }));
  return reduce(s, ev(s, 'transcript', { source: 'meeting', role: 'questioner', text, is_final: true }, { turn_id: turn, epoch }));
};
const primary = (s, id = 'p1') => step(s, 'prompt_primary', { prompt_id: id, level: 'L1', outline: OUTLINE, source_tag: '主库' });

test('commit switches turn and shows question', () => {
  const s = commit(initialState('s'), 't1', 1, '你好吗');
  assert.equal(s.turn_id, 't1'); assert.equal(s.epoch, 1); assert.equal(s.question.text, '你好吗');
  assert.equal(statusLabel(s), '已判定');
});

test('late prompt with old epoch or other turn_id is dropped (M11-T4)', () => {
  let s = commit(commit(initialState('s'), 't1', 1), 't2', 2);
  const before = s;
  for (const [turn_id, epoch, reason] of [['t1', 1, 'stale_epoch'], ['t2', 1, 'stale_epoch'], ['t1', 2, 'turn_mismatch']]) {
    const r = step(s, 'prompt_primary', { prompt_id: 'late', level: 'L1', outline: OUTLINE, source_tag: 'x' }, { turn_id, epoch });
    assert.equal(r.primary, null); assert.equal(r.lastDrop, reason);
    s = r;
  }
  assert.equal(s.dropped, 3); assert.equal(s.primary, before.primary);
  assert.equal(primary(s).primary.prompt_id, 'p1');
});

test('event_seq not increasing is dropped; gap flags resync without applying', () => {
  let s = primary(commit(initialState('s'), 't1', 1));
  const dup = reduce(s, { ...ev(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }), event_seq: s.event_seq });
  assert.equal(dup.supplements.length, 0); assert.equal(dup.lastDrop, 'stale_seq'); assert.equal(dup.event_seq, s.event_seq);
  const old = reduce(s, { ...ev(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }), event_seq: 1 });
  assert.equal(old.supplements.length, 0);
  const gap = reduce(s, { ...ev(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }), event_seq: s.event_seq + 5 });
  assert.equal(gap.needResync, true); assert.equal(gap.supplements.length, 0); assert.equal(gap.lastDrop, 'seq_gap');
});

test('future epoch without commit requests resync', () => {
  const s = commit(initialState('s'), 't1', 1);
  const r = step(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }, { epoch: 2, turn_id: 't2' });
  assert.equal(r.needResync, true); assert.equal(r.lastDrop, 'future_epoch');
});

test('primary is locked within a turn; supplements append only and primary object is untouched', () => {
  let s = primary(commit(initialState('s'), 't1', 1));
  const p = s.primary;
  const again = step(s, 'prompt_primary', { prompt_id: 'p2', level: 'L2', outline: { conclusion: '改了', points: [] }, source_tag: 'x' });
  assert.equal(again.primary, p); assert.equal(again.lastDrop, 'primary_locked');
  s = again;
  for (let i = 0; i < 10; i++) s = step(s, 'prompt_supplement', { source_tag: '联网', content: `补${i}` });
  assert.equal(s.primary, p);
  assert.deepEqual(s.supplements.map((x) => x.content), Array.from({ length: 10 }, (_, i) => `补${i}`));
});

test('prompt_status and supplement keep prompt_id so the board can ACK them', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_status', { text: '外部补充不可用', prompt_id: 'p7' });
  assert.equal(s.promptStatus, '外部补充不可用');
  assert.equal(s.promptStatusId, 'p7');
  s = step(s, 'prompt_supplement', { source_tag: '联网', content: '补', prompt_id: 'p8' });
  assert.equal(s.supplements.at(-1).prompt_id, 'p8');
  s = primary(s, 'p9');
  assert.equal(s.promptStatus, '');  // 主答案到位后状态清空，ID 也清空
  assert.equal(s.promptStatusId, '');
});

test('follow-up does not wipe the primary answer', () => {
  let s = primary(commit(initialState('s'), 't1', 1));
  const p = s.primary;
  s = step(s, 'interrupt', { kind: 'followup' });
  assert.equal(s.primary, p); assert.equal(statusLabel(s), '追问：已追加到补充区'); assert.equal(turnClosed(s), false);
  s = step(s, 'prompt_supplement', { source_tag: '追问提示', content: '追问内容' });
  assert.equal(s.primary, p); assert.equal(s.supplements.length, 1);
  s = step(s, 'turn_state', { state: 'answering', reason: 'a', waiting_continue: false, answerer_active: true });
  assert.equal(s.interrupt, null); assert.equal(statusLabel(s), '回答中');
});

test('switch interrupts the old turn, archives it, then new turn replaces it', () => {
  let s = primary(commit(initialState('s'), 't1', 1, '旧问题'));
  s = step(s, 'interrupt', { kind: 'switch' });
  assert.equal(s.status, 'interrupted'); assert.equal(turnClosed(s), true);
  const late = step(s, 'prompt_supplement', { source_tag: 'a', content: '迟到' });
  assert.equal(late.supplements.length, 0); assert.equal(late.lastDrop, 'turn_closed');
  s = commit(late, 't2', 2, '新问题');
  assert.equal(s.turn_id, 't2'); assert.equal(s.primary, null); assert.equal(s.question.text, '新问题');
  assert.equal(s.history.length, 1);
  assert.equal(s.history[0].outcome, 'interrupted'); assert.equal(s.history[0].question, '旧问题'); assert.equal(s.history[0].primary.prompt_id, 'p1');
});

test('rolled_back marks corrected, keeps history and primary; recommit archives as revised', () => {
  let s = primary(commit(initialState('s'), 't1', 1, '原问题'));
  s = step(s, 'turn_rollback', { from_version: 1, to_version: 2, reason: 'final_changed', text: '改后' });
  assert.equal(s.question.corrected, true); assert.equal(s.question.text, '原问题');
  assert.equal(s.question.revisions.length, 1); assert.equal(s.primaryStale, true); assert.ok(s.primary);
  s = step(s, 'turn_state', { state: 'listening_question', reason: 'r', waiting_continue: false, answerer_active: false });
  s = commit(s, 't1', 2, '改后的问题');
  assert.equal(s.question.text, '改后的问题'); assert.equal(s.question.corrected, true);
  assert.equal(s.question.revisions[0].text, '原问题');
  assert.equal(s.primary, null); assert.equal(s.primaryStale, false);
  assert.equal(s.history.length, 1); assert.equal(s.history[0].outcome, 'revised'); assert.equal(s.history[0].primary.prompt_id, 'p1');
});

test('closed turn greys out and rejects prompts; ended disables', () => {
  let s = primary(commit(initialState('s'), 't1', 1));
  s = step(s, 'turn_state', { state: 'closed', reason: 'done', waiting_continue: false, answerer_active: false });
  assert.equal(turnClosed(s), true); assert.equal(statusLabel(s), '本题已结束');
  assert.equal(step(s, 'prompt_supplement', { source_tag: 'a', content: 'x' }).supplements.length, 0);
  s = step(s, 'turn_state', { state: 'ended', reason: 'session_stopped', waiting_continue: false, answerer_active: false });
  assert.equal(s.ended, true); assert.equal(statusLabel(s), '已结束');
});

test('candidate, withdrawn, queue, health, sources', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'candidate', { hits: [{ title: 'h' }], text: '听到' });
  assert.equal(s.candidate.hits.length, 1);
  s = step(s, 'candidate', { withdrawn: true, hits: [] });
  assert.equal(s.candidate, null);
  s = step(s, 'queue_update', { items: ['下一题'] });
  s = step(s, 'health', { asr: { state: 'reconnecting', reason: 'x' } });
  s = step(s, 'source_state', { source: 'mic', state: 'ok', level: null });
  assert.deepEqual(s.queue, ['下一题']); assert.equal(s.health.asr.state, 'reconnecting'); assert.equal(s.sources.mic.state, 'ok');
});

test('waiting_continue label and other-session events are dropped', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'turn_state', { state: 'question_candidate', reason: 'x', waiting_continue: true, answerer_active: false });
  assert.equal(statusLabel(s), '等待继续');
  const r = reduce(s, { ...ev(s, 'queue_update', { items: ['x'] }), session: 'other' });
  assert.equal(r.queue.length, 0); assert.equal(r.lastDrop, 'session_mismatch');
});

test('snapshot restore: events at or before snapshot event_seq are not replayed', () => {
  const snap = { session_id: 's', turn_id: 't1', epoch: 1, event_seq: 7, status: 'answering', primary: { prompt_id: 'p5', level: 'L1', outline: OUTLINE, source_tag: '主库' },
    supplements: [{ source_tag: 'a', content: '已有' }], queue: ['q'], health: {}, question: { text: 'Q', text_version: 1, kind: '', corrected: false } };
  let s = fromSnapshot(snap);
  assert.equal(s.event_seq, 7); assert.equal(s.primary.prompt_id, 'p5'); assert.equal(statusLabel(s), '回答中');
  const replayed = reduce(s, ev(s, 'prompt_supplement', { source_tag: 'a', content: '重复' }, { event_seq: 7 }));
  assert.equal(replayed.supplements.length, 1);
  const next = reduce(s, ev(s, 'prompt_supplement', { source_tag: 'a', content: '新' }));
  assert.deepEqual(next.supplements.map((x) => x.content), ['已有', '新']);
});

test('keys: Esc/H hide, F font, modifiers ignored', () => {
  assert.equal(keyAction({ key: 'Escape' }), 'toggle_hide');
  assert.equal(keyAction({ key: 'h' }), 'toggle_hide');
  assert.equal(keyAction({ key: 'H', shiftKey: true }), 'toggle_hide');
  assert.equal(keyAction({ key: 'f' }), 'font_up');
  assert.equal(keyAction({ key: 'F', shiftKey: true }), 'font_down');
  assert.equal(keyAction({ key: 'h', metaKey: true }), null);
  assert.equal(keyAction({ key: 'h', repeat: true }), null);
  assert.equal(keyAction({ key: 'x' }), null);
  assert.equal(nextScale(SCALES.length - 1, 'font_up'), 0);
  assert.equal(nextScale(0, 'font_down'), 0);
  assert.ok(SCALES.every((x) => x >= 1));  // 基础字号 18px，缩放不低于 1
});

test('corrected primary replaces the primary before answering, but never once answering', () => {
  let s = primary(commit(initialState('s'), 't1', 1));
  const fixed = step(s, 'prompt_primary', { prompt_id: 'p2', level: 'L1', outline: { conclusion: '修正后', points: [] }, source_tag: '我的口径', corrected: true });
  assert.equal(fixed.primary.prompt_id, 'p2'); assert.equal(fixed.primary.outline.conclusion, '修正后');
  const answering = { ...s, status: 'answering' };
  const locked = step(answering, 'prompt_primary', { prompt_id: 'p3', level: 'L1', outline: { conclusion: '不应替换', points: [] }, source_tag: 'x', corrected: true });
  assert.equal(locked.primary, answering.primary); assert.equal(locked.lastDrop, 'primary_locked');
});

test('primary 透传 flagged / pending_marks 供红标渲染', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_primary', { prompt_id: 'p1', level: 'L1', outline: OUTLINE, source_tag: '含待核内容', flagged: true, pending_marks: 2 });
  assert.equal(s.primary.flagged, true); assert.equal(s.primary.pending_marks, 2);
  const clean = step(commit(initialState('s'), 't2', 1), 'prompt_primary', { prompt_id: 'p2', level: 'L1', outline: OUTLINE, source_tag: '我的口径' });
  assert.equal(clean.primary.flagged, false);
});

test('vad 候选：prompt_candidate 展示，Enter 采用，新题提交后清空', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_candidate', { candidate_id: 'c1', text: '你能讲讲分布式事务' });
  assert.deepEqual(s.vadCandidate, { candidate_id: 'c1', text: '你能讲讲分布式事务' });
  assert.equal(keyAction({ key: 'Enter' }, true), 'accept_candidate');
  assert.equal(keyAction({ key: 'Enter' }, false), null);
  assert.equal(keyAction({ key: 'Enter', ctrlKey: true }, true), null);
  s = commit(s, 't2', 2, '你能讲讲分布式事务的区别吗');
  assert.equal(s.vadCandidate, null);
});

test('vad 候选可被 withdrawn 清空', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_candidate', { candidate_id: 'c1', text: 'x' });
  s = step(s, 'prompt_candidate', { candidate_id: 'c1', text: 'x', withdrawn: true });
  assert.equal(s.vadCandidate, null);
});

test('候选确认（prompt_primary）后清空，候选答案一起渲染', () => {
  let s = commit(initialState('s'), 't1', 1);
  s = step(s, 'prompt_candidate', { candidate_id: 'c1', text: '你能讲讲分布式事务', outline: OUTLINE, level: 'L1' });
  assert.deepEqual(s.vadCandidate.outline, OUTLINE);
  s = step(s, 'prompt_primary', { prompt_id: 'p1', level: 'L1', outline: OUTLINE, source_tag: '我的口径' });
  assert.equal(s.vadCandidate, null);
});
test('快照恢复 pending_candidate 为 vad 候选', () => {
  const s = fromSnapshot({ session_id: 's', pending_candidate: { candidate_id: 'c1', text: 'x' } });
  assert.deepEqual(s.vadCandidate, { candidate_id: 'c1', text: 'x' });
});
