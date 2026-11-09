// landing.logic：欢迎页纯逻辑。只做数据聚合与容错，不碰 DOM；
// 接口失败一律显示 —（不编造 0），空库/空列表是真实状态、如实显示 0。

export const DASH = '—';

export interface LibraryRow {
  id?: string;
  name?: string;
  total?: number | null;
  approved?: number | null;
}

export interface SecondaryProgressRes {
  approved?: number | null;
  [key: string]: unknown;
}

export interface SessionRow {
  id?: string;
  mode?: string;
  status?: string;
  started_at?: number | null;
  created_at?: number | null;
  deleted?: boolean;
  [key: string]: unknown;
}

export interface SessionsRes {
  sessions?: SessionRow[];
  total?: number;
  [key: string]: unknown;
}

export interface StatusRes {
  asr?: string;
  asr_backend?: string;
  [key: string]: unknown;
}

export type StatKey = 'primary' | 'secondary' | 'sessions' | 'asr';

export interface Stat {
  key: StatKey;
  label: string;
  /** 数值统计为真实数字；取不到时为 null（显示 —）。转写方式恒为 null。 */
  value: number | null;
  text: string;
}

const ASR_BACKEND_LABELS: Record<string, string> = {
  aliyun: '云转写',
  sherpa: '本地转写',
  none: '未接入',
};

const MODE_LABELS: Record<string, string> = {
  online: '线上',
  offline: '线下',
};

const STATUS_LABELS: Record<string, string> = {
  active: '进行中',
  stopped: '已结束',
  ended: '已结束',
};

export function sumApproved(libraries: LibraryRow[] | null): number | null {
  if (!Array.isArray(libraries)) return null;
  let sum = 0;
  for (const lib of libraries) {
    if (lib && typeof lib.approved === 'number' && Number.isFinite(lib.approved)) sum += lib.approved;
  }
  return sum;
}

export function secondaryApproved(res: SecondaryProgressRes | null): number | null {
  if (!res || typeof res !== 'object') return null;
  return typeof res.approved === 'number' && Number.isFinite(res.approved) ? res.approved : null;
}

export function sessionsCount(res: SessionsRes | null): number | null {
  if (!res || typeof res !== 'object') return null;
  if (typeof res.total === 'number' && Number.isFinite(res.total)) return res.total;
  if (Array.isArray(res.sessions)) return res.sessions.length;
  return null;
}

export function asrText(res: StatusRes | null): string | null {
  if (!res || typeof res !== 'object') return null;
  if (typeof res.asr === 'string' && res.asr.trim()) return res.asr.trim();
  if (typeof res.asr_backend === 'string') return ASR_BACKEND_LABELS[res.asr_backend] ?? null;
  return null;
}

function statText(value: number | null): string {
  return value === null ? DASH : formatCount(value);
}

export interface StatsInput {
  libraries?: LibraryRow[] | null;
  secondary?: SecondaryProgressRes | null;
  sessions?: SessionsRes | null;
  status?: StatusRes | null;
}

export function buildStats(input: StatsInput): Stat[] {
  const primary = sumApproved(input.libraries ?? null);
  const secondary = secondaryApproved(input.secondary ?? null);
  const sessions = sessionsCount(input.sessions ?? null);
  const asr = asrText(input.status ?? null);
  return [
    { key: 'primary', label: '主库已启用', value: primary, text: statText(primary) },
    { key: 'secondary', label: '次优库已通过', value: secondary, text: statText(secondary) },
    { key: 'sessions', label: '历史会话', value: sessions, text: statText(sessions) },
    { key: 'asr', label: '转写方式', value: null, text: asr ?? DASH },
  ];
}

export function formatCount(n: number): string {
  return String(Math.round(n));
}

/* ---------- count-up（与参考页一致的 easeOutCubic） ---------- */

export function easeOutCubic(t: number): number {
  const c = Math.min(1, Math.max(0, t));
  return 1 - Math.pow(1 - c, 3);
}

export function countUpValue(target: number, t: number, decimals = 0): string {
  return (target * easeOutCubic(t)).toFixed(decimals);
}

export function finalStatValue(target: number, decimals = 0): string {
  return target.toFixed(decimals);
}

/* ---------- 汉堡菜单 ---------- */

export interface MenuState {
  open: boolean;
  expanded: 'true' | 'false';
  label: string;
  overlayHidden: boolean;
  bodyClass: boolean;
}

export function menuState(open: boolean): MenuState {
  return {
    open,
    expanded: open ? 'true' : 'false',
    label: open ? '关闭菜单' : '打开菜单',
    overlayHidden: !open,
    bodyClass: open,
  };
}

export function toggleMenu(open: boolean): boolean {
  return !open;
}

export function shouldCloseMenuOnResize(open: boolean, width: number): boolean {
  return open && width > 720;
}

/* ---------- 最近会话 ---------- */

export interface RecentSession {
  id: string;
  modeLabel: string;
  statusLabel: string;
  timeLabel: string;
  href: string;
}

export function formatSessionTime(ts: number | null): string {
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return DASH;
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function recentSessions(res: SessionsRes | null, limit = 3): RecentSession[] {
  const rows = Array.isArray(res?.sessions) ? res!.sessions! : [];
  return rows
    .filter((r) => r && typeof r.id === 'string' && r.id && !r.deleted)
    .sort((a, b) => (Number(b.started_at) || 0) - (Number(a.started_at) || 0))
    .slice(0, limit)
    .map((r) => {
      const id = r.id as string;
      const mode = typeof r.mode === 'string' ? r.mode : '';
      const status = typeof r.status === 'string' ? r.status : '';
      return {
        id,
        modeLabel: MODE_LABELS[mode] ?? (mode || DASH),
        statusLabel: STATUS_LABELS[status] ?? (status || DASH),
        timeLabel: formatSessionTime(r.started_at ?? r.created_at ?? null),
        href: `/records.html?session=${encodeURIComponent(id)}`,
      };
    });
}
