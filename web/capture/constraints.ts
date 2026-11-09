// 线下同室单麦的 getUserMedia 约束（纯逻辑，无 DOM）。
//
// 依据：
// - 外放时，浏览器的回声消除（AEC）会把扬声器放出的“对方声音”当成回声减掉，
//   正是“会议没有声音 / 对方声音进不来”的典型原因；因此 room 通道关闭 AEC。
// - 降噪与自动增益会改变远场人声的频谱与电平，声纹（app/audio/voiceprint.py 余弦相似度）
//   和 SpeakerRoleTracker 的 min_rms / 相似度阈值都按原始输入设计；登记与采集使用同一套
//   关闭处理的输入，避免模板与现场音频处理链不一致。
// - 线上麦克风保持浏览器默认处理（现有行为不变）；会议虚拟声卡仍沿用 meeting.ts 的三项关闭。
export interface AudioConstraintSet {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
  deviceId?: { exact: string };
}

export const ROOM_MIC_NOTE =
  '线下同室只用一路麦克风，对方声音外放。为避免回声消除把对方声音当成回声吃掉，' +
  '本模式关闭回声消除、降噪和自动增益；请让麦克风同时朝向双方，并在安静环境使用。';

/** 线下 room 通道：关闭三项处理，尽量保留远场人声。 */
export function roomAudioConstraints(deviceId = ''): MediaStreamConstraints {
  const audio: AudioConstraintSet = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
  if (deviceId) audio.deviceId = { exact: deviceId };
  return { audio };
}

/** 线上麦克风：保持浏览器默认处理，仅在选择具体设备时传 deviceId。 */
export function onlineMicConstraints(deviceId = ''): MediaStreamConstraints {
  return { audio: deviceId ? { deviceId: { exact: deviceId } } : true };
}

export function micConstraints(mode: 'online' | 'offline', deviceId = ''): MediaStreamConstraints {
  return mode === 'offline' ? roomAudioConstraints(deviceId) : onlineMicConstraints(deviceId);
}

/** 授权失败时给用户可执行的下一步，不把错误原样抛给用户。 */
export function micProblemText(error: unknown): string {
  const name = (error as { name?: string } | null)?.name ?? '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return '麦克风未授权：点地址栏左侧的锁/摄像头图标允许麦克风；macOS 还需在“系统设置 > 隐私与安全性 > 麦克风”中允许 Chrome，然后重试。';
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return '没有可用的麦克风：请连接麦克风或在系统声音设置中启用输入设备，然后重试。';
  }
  if (name === 'NotReadableError' || name === 'AbortError') {
    return '麦克风被其它程序占用：请关闭正在录音的应用（会议客户端等），然后重试。';
  }
  return `无法打开麦克风：${String(error)}。请检查系统麦克风权限后重试。`;
}
