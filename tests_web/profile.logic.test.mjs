// 资料编辑纯逻辑：内容上限、表单→请求体、关联库默认选中、统计文案。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_PROFILE_BYTES, PRIVACY_NOTE, contentBytes, contentError, defaultLibraryIds, libraryChoice,
  linkLibraries, profileSaveBody, statsText, updateResultText,
} from '../web/profile/logic.js';

const p = { id: 'p1', alias: '甲', roles: ['管理'], note: 'n', library_ids: ['l1'], content_library_id: 'l1',
  library_stats: { id: 'l1', name: '资料：甲', total: 4, approved: 2, updated_at: 1762300000 } };

test('资料内容沿用 5MB 上限与导入页格式提示', () => {
  assert.equal(MAX_PROFILE_BYTES, 5_000_000);
  assert.equal(contentBytes('待'), 3);
  assert.equal(contentError('abc'), null);
  assert.match(contentError('  '), /粘贴文本/);
  assert.match(contentError('x'.repeat(MAX_PROFILE_BYTES + 1)), /5MB/);
  assert.match(PRIVACY_NOTE, /仅保存在本机/);
  assert.match(PRIVACY_NOTE, /DeepSeek/);
});

test('新建资料请求体：有内容时不关联已有库；无内容时单选关联', () => {
  const withContent = profileSaveBody({ alias: ' 甲 ', roles: ['管理'], note: ' n ', content: '正文', libraryId: 'l9' });
  assert.deepEqual(withContent, { alias: '甲', roles: ['管理'], note: 'n', library_ids: [], content: '正文' });
  const linkOnly = profileSaveBody({ alias: '乙', roles: [], note: '', content: '  ', libraryId: 'l9' });
  assert.deepEqual(linkOnly.library_ids, ['l9']);
  assert.equal(linkOnly.content, '');
  assert.deepEqual(profileSaveBody({ alias: '丙', roles: [], note: '', content: '', libraryId: '' }).library_ids, []);
});

test('选定资料后默认选中其关联库，且关联单选', () => {
  assert.deepEqual(defaultLibraryIds(p), ['l1']);
  assert.deepEqual(defaultLibraryIds({ ...p, library_ids: [] }), []);
  assert.deepEqual(defaultLibraryIds(null), []);
  assert.equal(libraryChoice(p), 'l1');
  assert.equal(libraryChoice({ ...p, library_ids: [] }), '__content__');
  assert.deepEqual(linkLibraries('l9', p), ['l9']);
  assert.deepEqual(linkLibraries('__content__', p), ['l1']);
  assert.deepEqual(linkLibraries('', p), []);
  assert.deepEqual(linkLibraries('__content__', { content_library_id: '' }), []);
});

test('绑定库状态与更新结果文案', () => {
  assert.match(statsText(p.library_stats), /共 4 条/);
  assert.match(statsText(p.library_stats), /已启用 2 条/);
  assert.match(statsText(p.library_stats), /最近更新 2025-11-0[45]/);
  assert.equal(statsText(null), '未关联答案库');
  assert.equal(updateResultText({ added: ['a'], changed: ['b', 'c'], deleted: [] }),
    '新增 1 / 变更 2 / 删除 0；变更条目已回到待审核');
  assert.equal(updateResultText({ added: 3, changed: 0, deleted: 1 }),
    '新增 3 / 变更 0 / 删除 1；变更条目已回到待审核');
  assert.equal(updateResultText(null), '新增 0 / 变更 0 / 删除 0；变更条目已回到待审核');
});

test('资料内容格式指引：模板可被解析格式要求覆盖，格式错误带指引', async () => {
  const { CONTENT_TEMPLATE, CONTENT_FORMAT_HELP, contentErrorHint } = await import('../web/profile/logic.js');
  assert.match(CONTENT_TEMPLATE, /\*\*H1-01｜00:01｜.+\*\* 参考回答：/);
  assert.match(CONTENT_FORMAT_HELP, /问题 \+ 参考回答/);
  assert.match(contentErrorHint('没有识别到附录问答格式'), /插入模板/);
  assert.equal(contentErrorHint('本场题库已冻结'), '本场题库已冻结');
});
