"""主答案来源标签 / 待核标记 / G3 口径（纯函数，实时链路与审核台共用）。

决策：含 [待核] 或 payload.flagged 的条目仍保留在实时链路，但主答案必须显著标注，
来源不得写“我的口径”，且不计入 G3。
"""
PENDING_MARK = '[待核]'
SOURCE_MINE = '我的口径'
SOURCE_PARTIAL = '部分命中，非标准答案'
SOURCE_GENERAL = '通用建议'
SOURCE_FLAGGED = '含待核内容'
LEVEL_SOURCE = {'L1': SOURCE_MINE, 'L1p': SOURCE_PARTIAL, 'L2': SOURCE_GENERAL}


def entry_text(payload):
    """条目正文：审核后的答案优先，其次规则答案/原文。"""
    p = payload or {}
    return p.get('approved_answer') or p.get('answer') or p.get('text') or ''


def pending_marks(payload):
    """正文中 [待核] 出现的次数。"""
    return entry_text(payload).count(PENDING_MARK)


def is_flagged(payload):
    """含待核标记：payload.flagged 或正文里仍有 [待核]。"""
    return bool((payload or {}).get('flagged')) or pending_marks(payload) > 0


def answer_tags(payload, level):
    """返回 (source_tag, flagged, pending_marks)。

    flagged 条目：source_tag='含待核内容'，不得冒充“我的口径”；
    干净条目：按级别 L1 -> 我的口径、L1p -> 部分命中，非标准答案、L2 -> 通用建议。
    """
    marks = pending_marks(payload)
    if is_flagged(payload):
        return SOURCE_FLAGGED, True, marks
    return LEVEL_SOURCE.get(level, SOURCE_GENERAL), False, 0


def g3_eligible(entry):
    """该条目能否计入 G3：调用方需自行确认 status == 'approved'，此处只排除待核。"""
    return not is_flagged(entry)
