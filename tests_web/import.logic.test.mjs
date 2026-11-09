import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FORMAT_EXAMPLE, FORMAT_RULES, MAX_BYTES, byteLength, importedText, libraryRowText,
  secondaryImportText, secondaryRolesText, secondarySummary, validateImport,
} from '../web/import/logic.js';

test('5MB 上限按 UTF-8 字节计算', () => {
  assert.equal(MAX_BYTES, 5_000_000);
  assert.equal(byteLength('abc'), 3);
  assert.equal(byteLength('待'), 3);
  assert.equal(validateImport('库', MAX_BYTES, '内容'), null);
  assert.match(validateImport('库', MAX_BYTES + 1, '内容'), /5MB/);
  assert.match(validateImport('', 10, '内容'), /资料库名称/);
  assert.match(validateImport('库', 10, '  '), /选择 .md 文件或粘贴文本/);
});

test('导入结果固定说明“待审核、未自动启用”', () => {
  assert.equal(importedText(12), '导入 12 条，全部待审核，未自动启用');
  assert.match(importedText(12, true), /已导入过/);
  assert.match(importedText(12, true), /不会自动启用/);
});

test('主库与次优库列表文案', () => {
  assert.equal(libraryRowText({ name: '甲库', total: 226, approved: 3 }), '甲库 · 共 226 条 · 已启用 3 条');
  assert.equal(secondarySummary({ total: 7, reviewed: 2, pending: 5 }), '共 7 条 · 已审核 2 条 · 待审核 5 条');
  assert.equal(secondarySummary(null), '共 0 条 · 已审核 0 条 · 待审核 0 条');
  assert.equal(secondaryRolesText([{ role: 'tech', total: 60 }, { role: 'pm', total: 61 }]), 'tech（60） · pm（61）');
  assert.equal(secondaryRolesText([]), '暂无岗位标签');
  assert.equal(secondaryImportText({ imported: 1, unchanged: 6, errors: [] }), '本次新增 1 条，未变 6 条');
  assert.match(secondaryImportText({ imported: 0, unchanged: 7, errors: [{ id: 'x' }] }), /1 处格式问题/);
});

test('格式说明包含 H 编号｜时间｜问题 与参考回答约定', () => {
  assert.match(FORMAT_EXAMPLE, /## H1/);
  assert.match(FORMAT_EXAMPLE, /\*\*H1-01｜00:01｜.+?\*\* 参考回答：/);
  assert.ok(FORMAT_RULES.some((r) => /参考回答/.test(r)));
  assert.ok(FORMAT_RULES.some((r) => /5MB/.test(r)));
  assert.ok(FORMAT_RULES.some((r) => /待审核/.test(r)));
});
