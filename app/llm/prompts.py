"""M09 提示模板。

smart_clip 改写自 JWM0203/MeetingCopilot@337973a0a2e34424fb541a158d89533b5c4514cd:electron/llm/prompts.ts (smartClip / 稳定前缀思路), Apache-2.0 © 2026 JWM0203.
Modified: 改为 Python；稳定前缀只含岗位 + 提纲格式（不放主库摘要）；提示词按“只整理不创作”重写（思路参考 Kaalia structurer）。
"""
import json
import re

PRIORITY = re.compile(r'\d|项目|职责|负责|成果|经历|指标|Project', re.I)
FRAGMENT_BUDGET = 1200

OUTLINE_FORMAT = (
    '输出 JSON，字段：{"conclusion": 一句话结论, "points": [3个核心点], "example": 例子或数字(没有则空串), '
    '"caution": 注意事项(没有则空串), "facts_used": [{"entry_id": 来源条目编号, "text": 摘自该条目的个人事实原句}]}。'
)

_RULES = [
    '你是面试提词器的“整理员”，只整理，不创作。',
    '只能使用用户消息里【检索片段】给出的内容；片段里没有的数字、项目、公司、职位、年限一律不得出现。',
    'conclusion / points / example / caution 写公共知识与通用做法，不得使用第一人称（我、我们、本人）。',
    '涉及个人经历、数字、项目的内容只能放进 facts_used，并且逐条标注片段的 entry_id，text 必须摘自该片段原句，不得改写数字。',
    '片段不足以回答时，对应字段留空，不要补充业绩数字。',
]


def build_stable_prefix(role=''):
    """字节稳定：同一输入恒得同一字符串（利于 DeepSeek 前缀缓存）。只放岗位与提纲格式。"""
    parts = list(_RULES)
    if role.strip():
        parts += ['', f'【岗位】{role.strip()}']
    parts += ['', OUTLINE_FORMAT]
    return '\n'.join(parts)


def warmup_messages(role=''):
    """预热：与正式请求共享 system 前缀，用户消息最小化。"""
    return [{'role': 'system', 'content': build_stable_prefix(role)}, {'role': 'user', 'content': '就绪'}]


def smart_clip(text, budget, priority=PRIORITY):
    """超预算时优先保留命中 priority 的段落，输出保持原文顺序。"""
    t = text.strip()
    if len(t) <= budget:
        return t
    paras = re.split(r'\n{2,}', t)
    picked, used = set(), 0

    def take(i):
        nonlocal used
        cost = len(paras[i]) + 2
        if i not in picked and used + cost <= budget:
            picked.add(i)
            used += cost

    for i, p in enumerate(paras):
        if priority.search(p):
            take(i)
    for i in range(len(paras)):
        take(i)
    if not picked:
        return t[:budget]
    return '\n\n'.join(p for i, p in enumerate(paras) if i in picked)


def build_outline_messages(question, fragments, role=''):
    """fragments: [{'entry_id','text'}]，输入只含检索到的片段。"""
    frag = '\n\n'.join(f'[{f["entry_id"]}]\n{smart_clip(f["text"], FRAGMENT_BUDGET)}' for f in fragments)
    user = f'【检索片段】\n{frag}\n【检索片段结束】\n\n面试官的问题：{question.strip()}\n请按格式输出 JSON。'
    return [{'role': 'system', 'content': build_stable_prefix(role)}, {'role': 'user', 'content': user}]


DECOMPOSE_SYSTEM = (
    '你是题库整理员，只整理，不创作。对给定的一条问答，输出 JSON：{"question": 规范问法, '
    '"variants": [3到5个同义问法], "answer_outline": {"conclusion","points"(3个),"example","caution"}, '
    '"facts": [{"text": 答案中出现的个人经历/项目/职责/数字原句}], "role_tags": [], "topic_tags": []}。'
    'facts 必须逐字摘自原答案；原答案没有的数字、项目、公司不得出现。'
)


def build_decompose_messages(question, answer):
    return [{'role': 'system', 'content': DECOMPOSE_SYSTEM},
            {'role': 'user', 'content': json.dumps({'question': question, 'answer': answer}, ensure_ascii=False)}]
