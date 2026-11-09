"""线上通道到角色映射与回声能量比（M03-1）。纯函数，无状态。"""

ONLINE_ROLES = {'mic': 'answerer', 'meeting': 'questioner', 'room': 'unknown'}

# 外放回声时 mic 能量须高于 meeting 回灌能量的倍数；默认值待 M14 评测集标定。
DEFAULT_ECHO_RATIO = 2.0


def echo_dominant(mic_rms, meeting_rms, ratio=DEFAULT_ECHO_RATIO):
    """同时段 mic 能量严格大于 meeting 能量 * ratio 才判 mic 为主导来源（回答方）。"""
    return mic_rms > meeting_rms * ratio


def role_for(source, mode):
    if mode == 'online':
        return ONLINE_ROLES.get(source, 'unknown')
    if mode == 'offline':
        # 线下单路混合音，通道无法区分角色，一律 unknown，由未来声纹模块（M03-2~4）接管。
        return 'unknown'
    raise ValueError(f'未知模式: {mode}')


# 线上单麦门控（turn.mic_as_question，默认开）：会话内尚未出现 meeting live 帧时，mic 走 SpeakerRoleTracker
# 声纹门控（等价 offline room 路径），questioner 段喂 TurnDetector；meeting 首帧到达立即切回双通道语义，
# 并清空门控状态避免重复提交。声纹未登记（tracker.degraded）时不得启用，保持原行为并给出页面提示。
