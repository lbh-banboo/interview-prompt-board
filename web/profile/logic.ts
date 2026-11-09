// 资料编辑纯逻辑：内容上限、表单→请求体、关联库默认选中与统计文案。无 DOM、不接触 Key。
export const MAX_PROFILE_BYTES = 5_000_000;
export const CONTENT_HINT = '支持 .md 文件或直接粘贴文本，最大 5MB；条目格式与导入页一致。';
export const PRIVACY_NOTE =
  '资料内容仅保存在本机；只有被你审核启用的条目会用于提示，片段可能发送给 DeepSeek（取决于外发开关）。';

export interface LibraryStats {
  id: string; name: string; total: number; approved: number; updated_at?: number | null;
}
export interface ProfileLike {
  id: string; alias: string; roles?: string[]; note?: string; library_ids?: string[];
  content_library_id?: string; has_content?: boolean; library_stats?: LibraryStats | null; content?: string;
}
export interface LibraryOption { id: string; name: string; total: number; approved: number; selectable: boolean }

export interface ProfileDraft {
  alias: string; roles: string[]; note: string; content: string; libraryId: string;
}

export function contentBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function contentError(text: string): string | null {
  if (!text.trim()) return '请选择 .md 文件或粘贴文本';
  if (contentBytes(text) > MAX_PROFILE_BYTES) return '资料超过 5MB，请拆分后导入';
  return null;
}

/** 选定资料后默认选中其关联的主库（至多 1 个）。 */
export function defaultLibraryIds(p: ProfileLike | null | undefined): string[] {
  return (p?.library_ids ?? []).slice(0, 1);
}

/** 关联库下拉的当前值；只有仍关联着内容库时才显示“用资料内容生成/更新”。 */
export function libraryChoice(p: ProfileLike | null | undefined): string {
  const linked = p?.library_ids?.[0];
  if (linked) return linked;
  return p?.content_library_id ? '__content__' : '';
}

export function linkLibraries(value: string, p: ProfileLike | null | undefined): string[] {
  if (!value) return [];
  if (value === '__content__') return p?.content_library_id ? [p.content_library_id] : [];
  return [value];
}

/** 新建资料请求体：填写内容时由后端生成并绑定答案库，否则只关联所选主库（单选）。 */
export function profileSaveBody(v: ProfileDraft) {
  const content = v.content.trim() ? v.content : '';
  return {
    alias: v.alias.trim(),
    roles: v.roles,
    note: v.note.trim(),
    library_ids: content ? [] : (v.libraryId ? [v.libraryId] : []),
    content,
  };
}

export function statsText(s: LibraryStats | null | undefined): string {
  if (!s) return '未关联答案库';
  const date = typeof s.updated_at === 'number'
    ? new Date(s.updated_at * 1000).toISOString().slice(0, 10) : '—';
  return `共 ${s.total} 条 · 已启用 ${s.approved} 条 · 最近更新 ${date}`;
}

export function updateResultText(stats: any): string {
  const n = (v: unknown) => (Array.isArray(v) ? v.length : typeof v === 'number' ? v : 0);
  return `新增 ${n(stats?.added)} / 变更 ${n(stats?.changed)} / 删除 ${n(stats?.deleted)}；变更条目已回到待审核`;
}

export function profileOptionText(p: ProfileLike): string {
  return p.has_content ? `${p.alias}（有资料内容）` : p.alias;
}

/** 资料内容必须是“问题 + 参考回答”条目格式；自由文本简历需先整理。模板可一键插入。 */
export const CONTENT_TEMPLATE = [
  '## H1 开场与经历',
  '**H1-01｜00:01｜请先做个自我介绍** 参考回答：（写你自己确认过的事实：身份、目标、与岗位相关的两条线索。）',
  '**H1-02｜00:02｜你最有代表性的项目是什么？** 参考回答：（背景、你的职责、关键取舍；没把握的数字保留 [待核]，不要虚构。）',
].join('\n');

export const CONTENT_FORMAT_HELP =
  '只接受“问题 + 参考回答”格式；直接粘贴的简历原文会被拒绝。请先把简历/项目经历整理成可能被问到的问题和你自己的回答，再粘贴；可点“插入模板”。';

/** 后端对不符合格式的内容返回“没有识别到…格式”，换成带指引的提示。 */
export function contentErrorHint(message: string): string {
  return /没有识别到|格式/.test(message || '') ? `${message}。${CONTENT_FORMAT_HELP}` : message;
}
