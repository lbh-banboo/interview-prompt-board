// 状态条的纯逻辑：能力灯与降级横幅，无 DOM。
import type { Health } from './reducer.js';

export const CAP_LABELS: Record<string, string> = {
  capture: '采集', asr: '转写', role: '角色', retrieval: '检索', llm: '模型', search: '搜索', voice: '声纹', disk: '磁盘',
};
const ALWAYS = ['asr', 'llm', 'search'];  // 没有上报也占位显示“未接入”
const TEXT: Record<string, string> = { ok: '正常', degraded: '降级', down: '不可用', silent: '静音', lost: '中断', reconnecting: '重连中' };
const CLS: Record<string, string> = { ok: 'ok', degraded: 'warn', silent: 'warn', reconnecting: 'warn', lost: 'fail', down: 'fail' };

export interface CapLamp { key: string; label: string; cls: string; text: string; reason: string }

/** 各能力灯：ok 绿 / degraded 黄 / down 红；兼容旧结构只含 asr。非 ok 时带状态文字，避免只靠颜色。 */
export function capLamps(health: Record<string, Health>): CapLamp[] {
  return Object.keys(CAP_LABELS).filter((k) => health[k] || ALWAYS.includes(k)).map((key) => {
    const h = health[key];
    if (!h) return { key, label: CAP_LABELS[key], cls: 'idle', text: '未接入', reason: '' };
    return { key, label: CAP_LABELS[key], cls: CLS[h.state] ?? 'idle', text: h.state === 'ok' ? '' : TEXT[h.state] ?? h.state, reason: h.reason ?? '' };
  });
}

/** GET /api/setup/{session} 的 degraded_banner；接口失败或无字段返回空串。 */
export function bannerFrom(body: unknown): string {
  const b = (body as { degraded_banner?: unknown } | null)?.degraded_banner;
  return typeof b === 'string' ? b : '';
}
