// 统一入口的纯逻辑：状态汇总、链接构造。不含 DOM；只处理脱敏后的状态，不接触密钥明文。
export interface Card { key: string; title: string; desc: string; href: string; disabled: boolean }
export interface SessionView { active: boolean; id: string; label: string }
export interface Progress { approved: number; target: number; pct: number; g3Ready: boolean; label: string }
export interface CredRow {
  name: string; label: string; desc: string; configured: boolean; text: string; mask: string;
  source: string; sourceLabel: string; envLocked: boolean; warning: string;
}
export interface UsageView { total: string; rows: { service: string; text: string }[]; note: string }

export const liveHref = (session: string): string => `/live.html?session=${encodeURIComponent(session)}`;
export const setupHref = (): string => '/setup.html';

export function sessionView(status: any): SessionView {
  const s = status?.active_session;
  if (!s?.id) return { active: false, id: '', label: '当前没有进行中的会话' };
  const mode = s.mode === 'offline' ? '线下' : s.mode === 'online' ? '线上' : '';
  return { active: true, id: String(s.id), label: `进行中的会话${mode ? '（' + mode + '）' : ''}` };
}

export function navCards(session: SessionView): Card[] {
  return [
    { key: 'setup', title: '会前配置向导', desc: '选收音模式、岗位、主库，做一次自检', href: setupHref(), disabled: false },
    { key: 'import', title: '导入资料', desc: '上传或粘贴面试资料（.md），导入后保持待审核', href: '/import.html', disabled: false },
    { key: 'voice', title: '声音登记', desc: '线下同室外放需要：朗读 3 段，仅存本机，可随时删除', href: '/setup.html?step=voice', disabled: false },
    { key: 'review', title: '主库审核', desc: '逐条核对并启用主库条目', href: '/review.html', disabled: false },
    { key: 'annotate', title: '提问点校对', desc: '听录音逐条判定提问结束点', href: '/annotate.html', disabled: false },
    { key: 'secondary', title: '次优库审核', desc: '审核通用建议条目，用于 G5 门禁', href: '/secondary.html', disabled: false },
    { key: 'records', title: '记录库', desc: '回看历史会话：逐字稿、轮次、提示，导出与复盘', href: '/records.html', disabled: false },
    { key: 'settings', title: '设置', desc: '配置参数、预算与外发开关', href: '/settings.html', disabled: false },
    { key: 'live', title: '会话页', desc: session.active ? '提示板与音源采集同页，可结束会话' : '需要先开始一场会话，进入会前配置向导',
      href: session.active ? liveHref(session.id) : setupHref(), disabled: false },
  ];
}

/** /api/status 的 retrieval 返回 ok/degraded/down/none；能力灯按状态映射颜色，不把 down 误报绿。
 *  中文状态（未接入/未登记/未配置/未检测/外发关闭）沿用旧文案；缺失/空值一律灰灯“未接入”。 */
const STATE_LAMP: Record<string, [string, string]> = {
  ok: ['ok', '正常'],
  degraded: ['warn', '降级'],
  down: ['fail', '不可用'],
  none: ['idle', '未接入'],
};

export function statusLamps(status: any): { key: string; label: string; cls: string; text: string }[] {
  const defs: [string, string][] = [['asr', '实时转写'], ['speaker', '说话人'], ['model', '模型'], ['search', '搜索'], ['retrieval', '检索']];
  return defs.map(([key, label]) => {
    const raw = typeof status?.[key] === 'string' ? status[key] : '';
    const state = STATE_LAMP[raw];
    if (state) return { key, label, cls: state[0], text: state[1] };
    const text = raw || '未接入';
    const cls = text === '未接入' || text === '未登记' || text === '未配置' || text === '未检测' ? 'idle'
      : text === '外发关闭' ? 'warn' : 'ok';
    return { key, label, cls, text };
  });
}

export function progressView(p: any): Progress | null {
  if (!p || typeof p.approved !== 'number') return null;
  const target = typeof p.target === 'number' && p.target > 0 ? p.target : 50;
  // G3 只计非「待核/flagged」的已启用条目；后端已给 g3_eligible 时以它为准。
  const eligible = typeof p.g3_eligible === 'number' ? p.g3_eligible : p.approved;
  const ready = typeof p.g3_ready === 'boolean' ? p.g3_ready : eligible >= target;
  const extra = eligible === p.approved ? '' : ` · 可计入 G3 ${eligible} 条`;
  return { approved: p.approved, target, pct: Math.min(100, Math.round((eligible / target) * 100)), g3Ready: ready,
    label: ready ? `已启用 ${p.approved} 条${extra}，G3 达成` : `已启用 ${p.approved}/${target}${extra}，达到 ${target} 条后 G3 可用` };
}

/** 只取 status、mask、source 与来源类别；即使响应里混入其他字段也不展示。 */
const CRED_META: Record<string, { label: string; desc: string }> = {
  DEEPSEEK_API_KEY: { label: 'DeepSeek', desc: 'AI 生成' },
  DASHSCOPE_API_KEY: { label: '阿里云', desc: '实时转写 ASR' },
  SEARCH_API_KEY: { label: '搜索', desc: '联网补充（DeepSeek Search）' },
};
const SOURCE_LABELS: Record<string, string> = { env: '环境变量', keyring: '钥匙串', dotenv: '.env', memory: '内存' };
export const ENV_LOCK_HINT = '当前由环境变量提供，优先于界面保存的值；如要改用界面配置，请取消该环境变量后重启。';

export function credentialRows(report: any): CredRow[] {
  return Object.entries(report ?? {}).map(([name, v]) => {
    const r = v as { status?: string; mask?: string; source?: string };
    const configured = r?.status === 'configured';
    const source = configured && typeof r.source === 'string' ? r.source : '';
    const meta = CRED_META[name];
    return {
      name, label: meta?.label ?? name, desc: meta?.desc ?? '', configured,
      text: configured ? '已配置' : '未配置',
      mask: configured && typeof r.mask === 'string' ? r.mask : '',
      source, sourceLabel: SOURCE_LABELS[source] ?? '',
      envLocked: configured && source === 'env',
      warning: configured && source === 'env' ? ENV_LOCK_HINT : '',
    };
  });
}

const SERVICES: Record<string, string> = { asr: '转写', llm: '模型', search: '搜索' };
export function usageView(u: any): UsageView {
  const money = (n: unknown) => `${(typeof n === 'number' ? n : 0).toFixed(2)}`;
  const rows = (Array.isArray(u?.rows) ? u.rows : []).map((r: any) => ({ service: String(r.service), text: `${SERVICES[r.service] ?? r.service}：${r.calls} 次，约 ${money(r.est_cost)}` }));
  const limit = typeof u?.daily_limit === 'number' ? `，日上限 ${money(u.daily_limit)}` : '';
  return { total: `今日估算 ${money(u?.est_cost_total)}${limit}`, rows, note: String(u?.note ?? '估算，非计费硬保证') };
}

export function configRows(res: any): string[] {
  const c = res?.config ?? {};
  const rows: string[] = [];
  if (c.model?.name) rows.push(`模型：${c.model.name}`);
  if (c.budget) rows.push(`预算：日 ${c.budget.daily_limit} / 单场 ${c.budget.session_limit}`);
  if (c.outbound) rows.push(`外发：转写音频${c.outbound.asr_audio ? '开' : '关'} · 模型片段${c.outbound.llm_snippets ? '开' : '关'} · 联网问题${c.outbound.web_questions ? '开' : '关'}`);
  if (Array.isArray(res?.errors) && res.errors.length) rows.push(`配置有 ${res.errors.length} 项问题`);
  return rows;
}
