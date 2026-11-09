// 快捷键：Esc / H 隐藏与恢复（不停止录音与后台），F 放大字号，Shift+F 缩小。
// Enter 仅在 VAD 候选存在时采用（hasCandidate），由调用方传入是否有候选。
export const SCALES = [1, 1.15, 1.3, 1.5];
export type KeyAction = 'toggle_hide' | 'font_up' | 'font_down';

export function keyAction(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; shiftKey?: boolean; repeat?: boolean },
  hasCandidate?: boolean): KeyAction | 'accept_candidate' | null;
export function keyAction(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; shiftKey?: boolean; repeat?: boolean },
  hasCandidate = false): KeyAction | 'accept_candidate' | null {
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return null;
  const k = e.key.toLowerCase();
  if (e.key === 'Enter' && hasCandidate) return 'accept_candidate';
  if (e.key === 'Escape' || k === 'h') return 'toggle_hide';
  if (k === 'f') return e.shiftKey ? 'font_down' : 'font_up';
  return null;
}

export function nextScale(index: number, action: 'font_up' | 'font_down'): number {
  return action === 'font_up' ? (index + 1) % SCALES.length : Math.max(0, index - 1);
}
