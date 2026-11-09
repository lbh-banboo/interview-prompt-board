import test from 'node:test';
import assert from 'node:assert/strict';
import {
  filterEntries, filterCounts, batchBlockers, canBatch, batchCandidates, canApprove, progress, reviewBody,
  describeRejected, orderByQueue, keyAction, step, flagsOf, isDeferred, reviewUrl, batchBody, batchUrl,
  batchSelection, approvedWarning, enhanceLabel, enhanceErrorLabel, editBody, statsText, outlineOf, variantList,
} from '../web/review/logic.js';

const E = (id, o = {}) => ({ id, question: 'Q' + id, answer: 'A', original: 'O', status: 'pending', ...o });
const set = [
  E('a'), E('b', { flagged: true }), E('c', { flags: ['conflict'] }), E('d', { status: 'approved', approved_answer: 'x' }),
  E('e', { status: 'rejected' }), E('f', { approved_answer: '暂存' }), E('g', { flags: ['suspected_fabrication'] }), E('h', { attribution_uncertain: true }),
];

test('flagsOf 兼容缺失字段并并入 flagged', () => {
  assert.deepEqual(flagsOf(E('x')), []);
  assert.deepEqual(flagsOf(E('x', { flagged: true, flags: ['pending_verify'] })), ['pending_verify']);
  assert.deepEqual(flagsOf(E('x', { flags: null })), []);
});
test('筛选', () => {
  const ids = (k, t) => filterEntries(set, k, t).map((e) => e.id);
  assert.equal(ids('all').length, 8);
  assert.deepEqual(ids('pending'), ['a', 'b', 'c', 'g', 'h']);
  assert.deepEqual(ids('verify'), ['b']);
  assert.deepEqual(ids('conflict'), ['c']);
  assert.deepEqual(ids('approved'), ['d']);
  assert.deepEqual(ids('rejected'), ['e']);
  assert.deepEqual(ids('deferred'), ['f']);
  assert.deepEqual(ids('all', 'qa'), ['a']);
  assert.equal(filterCounts(set).all, 8);
  assert.ok(isDeferred(set[5]) && !isDeferred(set[3]));
});
test('批量判定仅限无待核无标记的待审条目', () => {
  assert.deepEqual(batchCandidates(set).map((e) => e.id), ['a', 'f']);
  assert.deepEqual(batchBlockers(set[1]), ['含待核标记']);
  assert.deepEqual(batchBlockers(set[2]), ['标记：事实冲突']);
  assert.deepEqual(batchBlockers(set[6]), ['标记：疑似虚构']);
  assert.deepEqual(batchBlockers(set[7]), ['说话人待核']);
  assert.ok(!canBatch(set[3]));
});
test('启用校验', () => {
  assert.equal(canApprove(set[0], false, 'x').ok, false);
  assert.equal(canApprove(set[0], true, '  ').ok, false);
  assert.equal(canApprove(set[0], true, 'x').ok, true);
  assert.match(canApprove(set[6], true, 'x').why, /禁止启用/);
  assert.match(canApprove(set[2], true, 'x').why, /事实冲突/);  // 冲突禁止单条启用
  assert.equal(canApprove(set[1], true, 'x').ok, true);  // 待核可逐条确认后启用
});
test('已启用含冲突/待核只读警示', () => {
  assert.match(approvedWarning(E('x', { status: 'approved', flags: ['conflict'] })), /已启用/);
  assert.match(approvedWarning(E('x', { status: 'approved', flagged: true })), /已启用/);
  assert.equal(approvedWarning(E('x', { status: 'pending', flags: ['conflict'] })), '');
  assert.equal(approvedWarning(E('x', { status: 'approved' })), '');
});
test('进度', () => {
  const p = progress(set);
  assert.deepEqual([p.approved, p.rejected, p.verify, p.total, p.pct, p.g3Ready], [1, 1, 1, 8, 2, false]);
  assert.equal(p.label, '已启用 1 条 · 可计入 G3 1 条 · 达标 50');
  const many = Array.from({ length: 52 }, (_, i) => E('n' + i, { status: 'approved' }));
  const q = progress(many);
  assert.deepEqual([q.pct, q.g3Ready], [100, true]);
  assert.match(q.label, /G3 可用/);
  assert.equal(progress([]).pct, 0);
});
test('G3 只计已启用且非待核（flagged 或正文含 [待核]）', () => {
  const rows = [
    E('a', { status: 'approved', approved_answer: '干净答案' }),
    E('b', { status: 'approved', flagged: true, approved_answer: '待核答案' }),
    E('c', { status: 'approved', approved_answer: '原答案 [待核]' }),
    E('d', { status: 'pending', flagged: true }),
  ];
  const p = progress(rows);
  assert.deepEqual([p.approved, p.eligible, p.flaggedApproved, p.pendingApproved], [3, 1, 2, 1]);
  assert.match(p.label, /已启用 3 条 · 可计入 G3 1 条/);
  assert.match(p.label, /含待核 1 条需逐条核实/);
  assert.equal(p.pct, 2); assert.equal(p.g3Ready, false);
});
test('G3 达标按可计入条目而非全部已启用', () => {
  const clean = Array.from({ length: 49 }, (_, i) => E('n' + i, { status: 'approved' }));
  const dirty = E('dirty', { status: 'approved', flagged: true });
  const p = progress([...clean, dirty]);
  assert.equal(p.approved, 50); assert.equal(p.eligible, 49); assert.equal(p.g3Ready, false);
  assert.match(p.label, /可计入 G3 49/);
});
test('请求体与 URL', () => {
  assert.deepEqual(reviewBody('approve', 'x', true), { status: 'approved', answer: 'x', confirmed: true });
  assert.deepEqual(reviewBody('defer', 'x', true), { status: 'pending', answer: 'x', confirmed: false });
  assert.deepEqual(reviewBody('reject', 'x', false), { status: 'rejected', answer: 'x', confirmed: false });
  assert.deepEqual(batchBody(['a'], true), { keys: ['a'], confirmed: true });
  assert.equal(reviewUrl('L', 'H1-01'), '/api/libraries/L/entries/H1-01/review');
  assert.equal(batchUrl('L'), '/api/libraries/L/batch-approve');
});
test('批量选择只提交 /batch-approvable 允许的条目', () => {
  const { safe, blocked } = batchSelection(set, ['a', 'b', 'c', 'zz'], new Set(['a', 'f']));
  assert.deepEqual(safe, ['a']);
  assert.deepEqual(blocked.map((x) => x.id), ['b', 'c', 'zz']);
  assert.match(blocked.find((x) => x.id === 'b').reasons[0], /待核/);
  assert.match(blocked.find((x) => x.id === 'c').reasons[0], /事实冲突/);
});
test('增强进度与降级文案', () => {
  assert.equal(enhanceLabel(), '未开始增强');
  assert.equal(enhanceLabel({ status: 'running', total: 5, done: 2, enhanced: 1, failed: 0, error: '' }), '增强中 2/5');
  assert.equal(enhanceLabel({ status: 'done', total: 5, done: 5, enhanced: 4, failed: 1, error: 'no_key' }), '增强完成 4/5 · 失败 1');
  assert.match(enhanceErrorLabel('no_key'), /凭证/);
  assert.match(enhanceErrorLabel('outbound_disabled'), /外发/);
  assert.match(enhanceErrorLabel('budget_denied'), /预算/);
  assert.match(enhanceErrorLabel('call_failed:INVALID_KEY'), /INVALID_KEY/);
});
test('编辑/重导入请求体与统计文案', () => {
  const outline = { conclusion: '结论', points: ['1'], examples: [], cautions: [] };
  assert.deepEqual(editBody('Q', ['a'], outline, [{ kind: 'metric', text: '8 人', source_span: '团队 8 人' }]),
    { question: 'Q', variants: ['a'], outline, facts: [{ kind: 'metric', text: '8 人', source_span: '团队 8 人' }] });
  assert.equal(statsText({ version: 2, added: ['a'], changed: ['b'], deleted: [], inherited: 3 }),
    '版本 v2：新增 1、变更 1、删除 0、继承审核状态 3 条');
  const e = E('x', { enhancement: { question_variants: ['v1', 'v2'], answer_outline: { conclusion: 'c', points: ['p'] } } });
  assert.deepEqual(variantList(e), ['v1', 'v2']);
  assert.deepEqual(outlineOf(e), { conclusion: 'c', points: ['p'], examples: [], cautions: [] });
  assert.deepEqual(outlineOf(E('y')), { conclusion: '', points: [], examples: [], cautions: [] });
});
test('被拒原因文案', () => {
  assert.deepEqual(describeRejected([{ id: 'b', reasons: ['含待核标记', '说话人待核'] }, { id: 'z', reasons: [] }]), ['b：含待核标记、说话人待核', 'z：被拒']);
  assert.deepEqual(describeRejected(undefined), []);
});
test('优先队列排序', () => {
  assert.deepEqual(orderByQueue(set.slice(0, 4), [{ id: 'c' }, { id: 'zz' }, { id: 'a' }]).map((e) => e.id), ['c', 'a', 'b', 'd']);
});
test('键位', () => {
  const k = (key, o = {}) => keyAction({ key, target: { tagName: 'DIV' }, ...o });
  assert.deepEqual(['j', 'k', 'a', 'h', 'r', 'x'].map((x) => k(x)), ['next', 'prev', 'approve', 'defer', 'reject', null]);
  assert.equal(keyAction({ key: 'a', target: { tagName: 'TEXTAREA' } }), null);
  assert.equal(keyAction({ key: 'a', target: { isContentEditable: true } }), null);
  assert.equal(k('a', { metaKey: true }), null);
  assert.equal(keyAction({ key: 'j' }), 'next');
});
test('step 边界', () => {
  assert.equal(step(set, 'a', -1), 'a');
  assert.equal(step(set, 'h', 1), 'h');
  assert.equal(step(set, 'a', 1), 'b');
  assert.equal(step(set, 'nope', 1), 'a');
  assert.equal(step([], 'a', 1), '');
});
