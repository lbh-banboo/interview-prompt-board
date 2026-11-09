import test from 'node:test';
import assert from 'node:assert/strict';
import {
  filterEntries, filterCounts, scanMarks, batchBlockers, canBatch, batchCandidates, canApprove, progress,
  reviewBody, reviewUrl, entriesUrl, batchBody, batchUrl, describeRejected, keyAction, step,
} from '../web/secondary/logic.js';

const E = (id, o = {}) => ({ id, question: 'Q' + id, status: 'pending', roles: ['tech'], ...o });
const set = [
  E('QTECH-01'), E('QTECH-02', { status: 'approved' }), E('QOPS-01', { scan: [{ kind: '第一人称经历', frag: '我们做过' }] }),
  E('QOPS-02', { status: 'rejected' }), E('QGEN-01', { roles: ['general'], scan: [] }),
];

test('筛选与搜索', () => {
  const ids = (k, t) => filterEntries(set, k, t).map((e) => e.id);
  assert.deepEqual(ids('all'), ['QTECH-01', 'QTECH-02', 'QOPS-01', 'QOPS-02', 'QGEN-01']);
  assert.deepEqual(ids('pending'), ['QTECH-01', 'QOPS-01', 'QGEN-01']);
  assert.deepEqual(ids('approved'), ['QTECH-02']);
  assert.deepEqual(ids('rejected'), ['QOPS-02']);
  assert.deepEqual(ids('all', 'ops'), ['QOPS-01', 'QOPS-02']);
  assert.deepEqual(filterCounts(set), { all: 5, pending: 3, approved: 1, rejected: 1 });
});

test('合规标记与批量判定', () => {
  assert.deepEqual(scanMarks(set[0]), []);
  assert.deepEqual(scanMarks(E('x', { scan: null })), []);
  assert.deepEqual(batchBlockers(set[2]), ['合规扫描：第一人称经历']);
  assert.deepEqual(batchBlockers(set[0]), []);
  assert.equal(canBatch(set[2]), false);
  assert.equal(canBatch(set[1]), false);
  assert.deepEqual(batchCandidates(set).map((e) => e.id), ['QTECH-01', 'QGEN-01']);
});

test('通过校验：须勾选且无合规标记', () => {
  assert.equal(canApprove(set[0], false).ok, false);
  assert.match(canApprove(set[0], false).why, /勾选/);
  assert.equal(canApprove(set[0], true).ok, true);
  assert.match(canApprove(set[2], true).why, /合规扫描/);
});

test('进度文案', () => {
  const p = progress(set);
  assert.deepEqual([p.total, p.reviewed, p.approved, p.rejected, p.pending, p.pct], [5, 2, 1, 1, 3, 40]);
  assert.equal(p.label, '已审核 2/5');
  assert.equal(progress([]).label, '已审核 0/0');
});

test('请求体与 URL', () => {
  assert.deepEqual(reviewBody('approve', true), { status: 'approved', confirmed: true });
  assert.deepEqual(reviewBody('defer', true), { status: 'pending', confirmed: false });
  assert.deepEqual(reviewBody('reject', false), { status: 'rejected', confirmed: false });
  assert.equal(reviewUrl('QTECH-01'), '/api/secondary/QTECH-01/review');
  assert.equal(entriesUrl(''), '/api/secondary/entries');
  assert.equal(entriesUrl('ops'), '/api/secondary/entries?role=ops');
  assert.equal(batchUrl(), '/api/secondary/approve-batch');
  assert.deepEqual(batchBody(['a'], true), { ids: ['a'], confirmed: true });
  assert.deepEqual(describeRejected([{ id: 'b', reasons: ['非待审状态'] }]), ['b：非待审状态']);
  assert.deepEqual(describeRejected(), []);
});

test('键位与切换', () => {
  const k = (key, o = {}) => keyAction({ key, target: { tagName: 'DIV' }, ...o });
  assert.deepEqual(['j', 'k', 'a', 'h', 'r', 'x'].map((x) => k(x)), ['next', 'prev', 'approve', 'defer', 'reject', null]);
  assert.equal(keyAction({ key: 'a', target: { tagName: 'INPUT' } }), null);
  assert.equal(k('a', { metaKey: true }), null);
  assert.equal(step(set, 'QTECH-01', 1), 'QTECH-02');
  assert.equal(step(set, 'QTECH-01', -1), 'QTECH-01');
  assert.equal(step(set, 'QGEN-01', 1), 'QGEN-01');
  assert.equal(step([], 'a', 1), '');
});
