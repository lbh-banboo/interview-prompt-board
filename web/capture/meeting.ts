// Original code. 会议音源两种接入方式（网页会议标签页 / 桌面客户端虚拟声卡）的纯逻辑：
// 约束构造、displaySurface 判定、输入设备过滤与排序。无 DOM，便于单测。

/** 会议通道 3 秒无能量时的状态文本；会话页状态条也用它。 */
export const MEETING_NO_SOUND_TEXT = '会议音频无声音';

/** macOS 上整屏/窗口共享通常没有声音：拿到流时立即提示，不静默继续。 */
export const DISPLAY_SURFACE_WARNING =
  '整个屏幕/窗口在 macOS 上通常没有声音，请改选“Chrome 标签页”并勾选共享标签页音频，或改用“虚拟声卡”方式';

export const NO_AUDIO_HINT =
  '未获得会议声音：请重新共享并勾选“共享标签页音频”，或改用“虚拟声卡/第二输入设备”方式';

export const NO_MEETING_DEVICE_HINT =
  '未选择会议音频输入设备：请先选择虚拟声卡（如 BlackHole 2ch），或改选“共享浏览器标签页”';

/** Chrome 的 getDisplayMedia 提示参数；TS DOM 类型未收录，这里显式声明后转成标准类型。 */
export interface DisplayMediaHints extends DisplayMediaStreamOptions {
  selfBrowserSurface?: 'include' | 'exclude';
  systemAudio?: 'include' | 'exclude' | 'mute';
  surfaceSwitching?: 'include' | 'exclude';
  preferCurrentTab?: boolean;
}

/**
 * 只使用 Chrome 已支持的提示参数：
 * - selfBrowserSurface=exclude：不把本页列为可共享标签页；
 * - systemAudio=include：允许共享标签页/系统声音；
 * - surfaceSwitching=include：共享中可切换标签页；
 * - preferCurrentTab=false：会议不在本页所在标签页，不预选当前标签页。
 * monitorTypeSurfaces 等参数拿不准则不加，避免浏览器拒绝未知约束。
 */
export const DISPLAY_CONSTRAINTS: DisplayMediaHints = {
  video: true,
  audio: true,
  selfBrowserSurface: 'exclude',
  systemAudio: 'include',
  surfaceSwitching: 'include',
  preferCurrentTab: false,
};

/** 读取共享流的 displaySurface；拿到流后、停用视频轨之前调用。 */
export function displaySurfaceOf(track: Pick<MediaStreamTrack, 'getSettings'> | null | undefined): string {
  try {
    return track?.getSettings?.().displaySurface ?? '';
  } catch {
    return '';
  }
}

/** monitor/window 返回明确提示，其余返回 null。 */
export function displaySurfaceWarning(track: Pick<MediaStreamTrack, 'getSettings'> | null | undefined): string | null {
  const surface = displaySurfaceOf(track);
  return surface === 'monitor' || surface === 'window' ? DISPLAY_SURFACE_WARNING : null;
}

const VIRTUAL_RE = /blackhole|loopback|soundflower|vb[- ]?cable|vb[- ]?audio|virtual|虚拟声卡/i;

export function isVirtualDevice(label: string): boolean {
  return VIRTUAL_RE.test(label);
}

export function meetingDeviceLabel(label: string): string {
  return isVirtualDevice(label) ? `${label}（虚拟声卡）` : label;
}

export interface AudioInputLike { kind?: string; deviceId?: string; label?: string }
export interface MeetingInput { deviceId: string; label: string }

/** 会议输入设备：过滤非 audioinput、无 deviceId 与已被麦克风选中的设备；虚拟声卡优先且保持原顺序。 */
export function meetingInputs(devices: AudioInputLike[], micId: string): MeetingInput[] {
  const picked = devices
    .filter((d) => d.kind === 'audioinput' && !!d.deviceId && d.deviceId !== micId)
    .map((d, index) => ({
      index,
      deviceId: d.deviceId!,
      label: meetingDeviceLabel(d.label || `输入设备 ${index + 1}`),
    }));
  picked.sort((a, b) => Number(isVirtualDevice(b.label)) - Number(isVirtualDevice(a.label)) || a.index - b.index);
  return picked.map(({ deviceId, label }) => ({ deviceId, label }));
}

/**
 * 虚拟声卡必须关闭浏览器的人声处理：回声消除/降噪/自动增益会把会议声音处理坏。
 * 麦克风那一路保持浏览器默认处理，不改。
 */
export function meetingAudioConstraints(deviceId: string): MediaStreamConstraints {
  return {
    audio: {
      deviceId: { exact: deviceId },
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  };
}
