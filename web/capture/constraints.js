export const ROOM_MIC_NOTE = '线下同室只用一路麦克风，对方声音外放。为避免回声消除把对方声音当成回声吃掉，' +
    '本模式关闭回声消除、降噪和自动增益；请让麦克风同时朝向双方，并在安静环境使用。';
/** 线下 room 通道：关闭三项处理，尽量保留远场人声。 */
export function roomAudioConstraints(deviceId = '') {
    const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
    if (deviceId)
        audio.deviceId = { exact: deviceId };
    return { audio };
}
/** 线上麦克风：保持浏览器默认处理，仅在选择具体设备时传 deviceId。 */
export function onlineMicConstraints(deviceId = '') {
    return { audio: deviceId ? { deviceId: { exact: deviceId } } : true };
}
export function micConstraints(mode, deviceId = '') {
    return mode === 'offline' ? roomAudioConstraints(deviceId) : onlineMicConstraints(deviceId);
}
/** 授权失败时给用户可执行的下一步，不把错误原样抛给用户。 */
export function micProblemText(error) {
    const name = error?.name ?? '';
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
