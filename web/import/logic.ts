// 导入页纯逻辑：5MB 校验、列表文案、结果文案。不含 DOM，不接触 Key。
export const MAX_BYTES = 5_000_000;

export const FORMAT_EXAMPLE = [
  '## H1 开场与经历',
  '**H1-01｜00:01｜请先做个自我介绍** 参考回答：先说明身份与目标，再给两条与岗位相关的线索。',
  '**H1-02｜00:02｜你最有代表性的项目是什么？** 参考回答：交代背景、你的职责与关键取舍，不虚构数字。',
].join('\n');

export const FORMAT_RULES = [
  '每行一个条目：**H编号-序号｜分:秒｜问题** 参考回答：……',
  '编号如 H1-01、H2-03，全库唯一；时间戳用 mm:ss。',
  '问题与参考回答之间用全角“｜”和“参考回答：”分隔。',
  '需要人工核对的原文保留 [待核]；说话人待核写在问题里。',
  '支持选择 .md 文件或直接粘贴文本，最大 5MB。',
  '导入后全部条目保持“待审核”，不会自动启用。',
];

export function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function validateImport(name: string, bytes: number, text: string): string | null {
  if (!name.trim()) return '请填写资料库名称';
  if (!text.trim()) return '请选择 .md 文件或粘贴文本';
  if (bytes > MAX_BYTES) return '资料超过 5MB，请拆分后导入';
  return null;
}

export function libraryRowText(l: { name: string; total: number; approved: number }): string {
  return `${l.name} · 共 ${l.total} 条 · 已启用 ${l.approved} 条`;
}

export function importedText(count: number, reused = false): string {
  return reused
    ? `该资料已导入过，共 ${count} 条；条目沿用原审核状态，不会自动启用`
    : `导入 ${count} 条，全部待审核，未自动启用`;
}

export function secondarySummary(p: any): string {
  const n = (v: unknown) => (typeof v === 'number' ? v : 0);
  return `共 ${n(p?.total)} 条 · 已审核 ${n(p?.reviewed)} 条 · 待审核 ${n(p?.pending)} 条`;
}

export function secondaryRolesText(roles: any): string {
  const rows = Array.isArray(roles) ? roles : [];
  if (!rows.length) return '暂无岗位标签';
  return rows.map((r: any) => `${r.role}（${r.total}）`).join(' · ');
}

export function secondaryImportText(result: any): string {
  const imported = typeof result?.imported === 'number' ? result.imported : 0;
  const unchanged = typeof result?.unchanged === 'number' ? result.unchanged : 0;
  const errors = Array.isArray(result?.errors) ? result.errors.length : 0;
  return `本次新增 ${imported} 条，未变 ${unchanged} 条${errors ? `，${errors} 处格式问题未导入` : ''}`;
}
