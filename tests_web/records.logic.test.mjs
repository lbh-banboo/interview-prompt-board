// 记录库纯逻辑：时长/时间格式、列表筛选、级别与角色标签、回答者未转写提示、轮次与提示归组。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ANSWERER_NOTE, answererNote, canEdit, deleteBlocked, filterCounts, filterSessions, formatDateTime, formatDuration,
  isEdited, levelLabel, noPrimaryNote, promptStateLabel, promptText, promptUrls, roleLabel, segmentOffset,
  segmentOriginal, segmentText, statusLabel, transcriptStart, turnGroups, turnLevel, visibleDelay,
} from '../web/records/logic.js';

const S = (id, o = {}) => ({ id, mode: 'online', status: 'stopped', roles: [], ...o });
const sessions = [
  S('a1', { profile_alias: '合成候选人A', roles: ['产品经理'], library_name: '合成主库', started_at: 1000 }),
  S('a2', { mode: 'offline', profile_alias: '合成候选人B', roles: ['技术开发'], started_at: 2000 }),
  S('live', { status: 'active', profile_alias: '进行中的人', started_at: 3000 }),
  S('draft1', { status: 'draft', started_at: null }),
  S('gone1', { deleted: true, deleted_at: 1700000000, profile_alias: '已删除的人' }),
];

test('时长格式：mm:ss / h:mm:ss，缺失与负数回落', () => {
  assert.equal(formatDuration(600), '10:00');
  assert.equal(formatDuration(3725), '1:02:05');
  assert.equal(formatDuration(0), '00:00');
  assert.equal(formatDuration(null), '—');
  assert.equal(formatDuration(undefined), '—');
  assert.equal(formatDuration(-5), '—');
  assert.equal(formatDuration(NaN), '—');
});

test('时间格式：本地日期时间，缺失回落', () => {
  assert.match(formatDateTime(1700000000), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(formatDateTime(null), '—');
});

test('状态与模式标签', () => {
  assert.equal(statusLabel('stopped'), '已结束');
  assert.equal(statusLabel('ended'), '已结束');
  assert.equal(statusLabel('active'), '进行中');
  assert.equal(statusLabel('draft'), '草稿');
  assert.equal(statusLabel('ready'), '待开始');
  assert.equal(statusLabel(''), '未知');
});

test('列表筛选：状态、已删除、搜索资料/岗位/编号', () => {
  const ids = (k, q = '') => filterSessions(sessions, k, q).map((s) => s.id);
  assert.deepEqual(ids('all'), ['a1', 'a2', 'live', 'draft1', 'gone1']);
  assert.deepEqual(ids('stopped'), ['a1', 'a2']);
  assert.deepEqual(ids('active'), ['live']);
  assert.deepEqual(ids('pending'), ['draft1']);
  assert.deepEqual(ids('deleted'), ['gone1']);
  assert.deepEqual(ids('all', '候选人B'), ['a2']);
  assert.deepEqual(ids('all', '产品经理'), ['a1']);
  assert.deepEqual(ids('all', 'LIVE'), ['live']);
  assert.deepEqual(ids('stopped', '不存在'), []);
  assert.equal(filterCounts(sessions).all, 5);
  assert.equal(filterCounts(sessions).stopped, 2);
  assert.equal(filterCounts(sessions).deleted, 1);
});

test('级别标签：L1/L1p/L2/L3/个人，缺失为破折号', () => {
  assert.equal(levelLabel('L1'), 'L1');
  assert.equal(levelLabel('L1p'), 'L1p');
  assert.equal(levelLabel('L2'), 'L2');
  assert.equal(levelLabel('L3'), 'L3');
  assert.equal(levelLabel('personal'), '个人');
  assert.equal(levelLabel('个人'), '个人');
  assert.equal(levelLabel(''), '—');
});

test('角色与回答者未转写提示', () => {
  assert.equal(roleLabel('questioner'), '提问方');
  assert.equal(roleLabel('answerer'), '回答者');
  assert.equal(roleLabel('unknown'), '未识别');
  const onlyQ = [{ role: 'questioner', text: '问题' }];
  assert.equal(answererNote(onlyQ), ANSWERER_NOTE);
  assert.match(ANSWERER_NOTE, /默认不转写回答者/);
  assert.equal(answererNote([...onlyQ, { role: 'answerer', text: '回答' }]), '');
  assert.equal(answererNote([]), ANSWERER_NOTE);
});

test('逐字稿原文保留与修订标记', () => {
  const seg = { id: 1, role: 'questioner', text: '原句', orig_text: '原句', edited_text: null };
  assert.equal(segmentText(seg), '原句');
  assert.equal(isEdited(seg), false);
  assert.equal(segmentOriginal(seg), '原句');
  const edited = { ...seg, edited_text: '改后' };
  assert.equal(segmentText(edited), '改后');
  assert.equal(segmentOriginal(edited), '原句');
  assert.equal(isEdited(edited), true);
  const t0 = transcriptStart([{ t_start: 10 }, { t_start: null }, { t_start: 12 }]);
  assert.equal(t0, 10);
  assert.equal(segmentOffset({ t_start: 14 }, 10), 4);
  assert.equal(segmentOffset({ t_start: null }, 10), null);
  assert.equal(transcriptStart([]), null);
});

test('提示内容与来源：数组大纲、字符串状态、非法 JSON 容错', () => {
  assert.equal(promptText({ content_json: '["要点A","要点B"]' }), '要点A；要点B');
  assert.equal(promptText({ content_json: '"纯文本"' }), '纯文本');
  assert.equal(promptText({ content_json: 'not-json' }), '');
  assert.equal(promptText({}), '');
  assert.deepEqual(promptUrls({ source_urls_json: '["https://e.com/x"]' }), ['https://e.com/x']);
  assert.deepEqual(promptUrls({ source_urls_json: 'bad' }), []);
  assert.equal(promptStateLabel('sent'), '已发送');
  assert.equal(promptStateLabel('acked'), '已展示');
  assert.equal(promptStateLabel('late_archived'), '未展示（迟到归档）');
});

test('轮次与提示归组、级别回落、可见延迟', () => {
  const turns = [
    { id: 't1', text: '请介绍项目', level: 'L1', t_committed: 11, t_visible: 11.8, rolled_back_count: 2 },
    { id: 't2', text: '追问细节', level: '', t_committed: 20 },
  ];
  const prompts = [
    { id: 1, turn_id: 't1', level: 'L1', block: 'primary' },
    { id: 2, turn_id: 't1', level: '', block: 'supplement' },
    { id: 3, turn_id: 'tx', level: 'L2', block: 'supplement' },
  ];
  const { groups, orphans } = turnGroups(turns, prompts);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].prompts.map((p) => p.id), [1, 2]);
  assert.equal(turnLevel(groups[0].turn, groups[0].prompts), 'L1');
  assert.equal(turnLevel(groups[1].turn, groups[1].prompts), '');
  assert.equal(visibleDelay(turns[0]), 0.8);
  assert.equal(visibleDelay(turns[1]), null);
  assert.deepEqual(orphans.map((p) => p.id), [3]);
});

test('删除与复盘编辑可用性', () => {
  assert.match(deleteBlocked({ status: 'active' }), /进行中的会话不能删除/);
  assert.match(deleteBlocked({ status: 'stopped', deleted: true }), /回收目录/);
  assert.equal(deleteBlocked({ status: 'stopped' }), '');
  assert.equal(canEdit({ status: 'stopped' }), true);
  assert.equal(canEdit({ status: 'active' }), false);
  assert.equal(canEdit({ status: 'stopped', deleted: true }), false);
});

test('空主答案：明确说明本轮没有命中标准答案，而不是空白', () => {
  assert.equal(noPrimaryNote([{ block: 'supplement' }, { block: 'status' }]), '本轮没有命中标准答案');
  assert.equal(noPrimaryNote([]), '本轮没有命中标准答案');
  assert.equal(noPrimaryNote([{ block: 'supplement' }, { block: 'primary' }]), '');
});
