// 会话页纯逻辑：阶段推导、时长格式、音源摘要与链接。无 DOM，便于单测。
export type SessionPhase = 'preparing' | 'capturing' | 'ended';
export type SourceCondition = 'idle' | 'checking' | 'ok' | 'silent' | 'lost';

export interface SessionInfoLike {
  status?: string; mode?: string; started_at?: number | null; stopped_at?: number | null;
  capture_started_at?: number | null;
  degraded_banner?: string | null;
}
export interface SourceSnapshot { condition: SourceCondition }
export interface CaptureSnapshotLike {
  frames: { mic: number; meeting: number; room?: number };
  gaps: number;
  sources: { mic?: SourceSnapshot; meeting?: SourceSnapshot; room?: SourceSnapshot };
}

export const liveHref = (sid: string): string => `/live.html?session=${encodeURIComponent(sid)}`;
export const setupHref = (): string => '/setup.html';
export const shortId = (id: string): string => (id || '').slice(0, 8);
export const exportName = (sid: string): string => `面试记录-${shortId(sid)}.md`;
export const endConfirmText = '结束会话会停止采集并保存本场记录，确定结束？';

export function modeLabel(mode: string | undefined): string {
  return mode === 'offline' ? '线下' : mode === 'online' ? '线上' : '未知模式';
}

const PHASE_LABELS: Record<SessionPhase, string> = { preparing: '准备中', capturing: '采集中', ended: '已结束' };
export const phaseLabel = (p: SessionPhase): string => PHASE_LABELS[p];

/** stopped/ended 一律视为已结束；active/ready/draft 未开始采集时是准备中。 */
export function phaseOf(status: string | undefined, capturing: boolean): SessionPhase {
  if (status === 'stopped' || status === 'ended') return 'ended';
  return capturing ? 'capturing' : 'preparing';
}

export const canCapture = (phase: SessionPhase): boolean => phase !== 'ended';

/** 秒 -> mm:ss，超过 1 小时 -> h:mm:ss。 */
export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** 已用秒数：从“开始采集”计（无采集起点时回落会话开始），结束冻结在 stopped_at / 页面记录时刻。 */
export function elapsedSeconds(info: SessionInfoLike, nowSec: number, frozenEndSec?: number): number | null {
  const start = typeof info.capture_started_at === 'number' ? info.capture_started_at
    : typeof info.started_at === 'number' ? info.started_at : null;
  if (start === null) return null;
  const ended = phaseOf(info.status, false) === 'ended';
  const end = ended
    ? (typeof info.stopped_at === 'number' ? info.stopped_at : frozenEndSec ?? start)
    : nowSec;
  return Math.max(0, Math.floor(end - start));
}

const CONDITION_LABELS: Record<SourceCondition, string> = {
  idle: '未开始', checking: '检测中', ok: '正常', silent: '无声音', lost: '已断开',
};
export const conditionLabel = (c: SourceCondition): string => CONDITION_LABELS[c] ?? '未开始';

/** 音源摘要：线上两路；线下只有房间麦克风一路（单麦同时收录双方）。 */
export function sourceSummary(snap: CaptureSnapshotLike, mode: string | undefined = 'online'): string {
  if (mode === 'offline') {
    const src = snap.sources.room ?? { condition: 'idle' as SourceCondition };
    return `房间麦克风 ${conditionLabel(src.condition)} ${snap.frames.room ?? 0} 帧 · 缺口 ${snap.gaps} 帧`;
  }
  const part = (name: string, src: SourceSnapshot | undefined, frames: number) => {
    const condition = src?.condition ?? 'idle';
    // 会议通道无能量时用整词提示，状态条一眼能看出“会议音频无声音”。
    const label = name === '会议音频' && condition === 'silent' ? '会议音频无声音' : `${name} ${conditionLabel(condition)}`;
    return `${label} ${frames} 帧`;
  };
  return `${part('麦克风', snap.sources.mic, snap.frames.mic)} · ${part('会议音频', snap.sources.meeting, snap.frames.meeting)} · 缺口 ${snap.gaps} 帧`;
}

export const degradedText = (banner: string | null | undefined): string => (banner ? `降级：${banner}` : '');
