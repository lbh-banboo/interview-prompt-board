"""宽容的模型输出解析：整段 JSON、流式增量；非结构化时回退规则提纲（app/domain/outline.py）。"""
import json
import re

from ..domain.outline import outline as rule_outline

_FENCE = re.compile(r'```(?:json)?\s*|\s*```', re.I)
_TRAIL_COMMA = re.compile(r',\s*([}\]])')
_STR = r'"((?:[^"\\]|\\.)*)"'
_ITEM = re.compile(rf'\s*,?\s*{_STR}')


def extract_json(text):
    """从可能带围栏/前后废话/尾逗号的文本里取第一个 JSON 对象；失败返回 None。"""
    text = _TRAIL_COMMA.sub(r'\1', _FENCE.sub('', text or ''))
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{', text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        return obj
    return None


def _s(v):
    return v.strip() if isinstance(v, str) else ''


def _list(v):
    return [_s(x) for x in v if _s(x)] if isinstance(v, list) else []


def parse_outline(text, fallback_answer=''):
    """返回 (dict, structured)。dict 恒含 conclusion/points/example/caution/facts_used。
    非结构化或缺结论 -> 用 fallback_answer 走规则提纲（facts_used 为空）。"""
    obj = extract_json(text)
    if obj and _s(obj.get('conclusion')):
        facts = [{'entry_id': _s(f.get('entry_id')), 'text': _s(f.get('text'))}
                 for f in obj.get('facts_used') or [] if isinstance(f, dict) and _s(f.get('text'))]
        return dict(conclusion=_s(obj['conclusion']), points=_list(obj.get('points'))[:3],
                    example=_s(obj.get('example')), caution=_s(obj.get('caution')), facts_used=facts), True
    if fallback_answer:
        o = rule_outline(fallback_answer)
        return dict(conclusion=o['conclusion'], points=o['points'], example=o['example'],
                    caution=o['caution'], facts_used=[]), False
    return dict(conclusion='', points=[], example='', caution='', facts_used=[]), False


def parse_decompose(text):
    """会前拆解（M06）。无法解析返回 None（调用方保留规则拆解，标“未增强”）。"""
    obj = extract_json(text)
    if not obj or not _s(obj.get('question')):
        return None
    ao = obj.get('answer_outline') if isinstance(obj.get('answer_outline'), dict) else {}
    facts = [_s(f.get('text')) if isinstance(f, dict) else _s(f) for f in obj.get('facts') or []]
    return dict(question=_s(obj['question']), variants=_list(obj.get('variants'))[:5],
                answer_outline=dict(conclusion=_s(ao.get('conclusion')), points=_list(ao.get('points'))[:3],
                                    example=_s(ao.get('example')), caution=_s(ao.get('caution'))),
                facts=[f for f in facts if f], role_tags=_list(obj.get('role_tags')),
                topic_tags=_list(obj.get('topic_tags')))


def _unescape(s):
    try:
        return json.loads(f'"{s}"').strip()
    except ValueError:
        return s.strip()


class StreamParser:
    """增量解析：feed(delta) 返回本次新完成的 (section, text)。section: conclusion|point|example|caution。
    只认已闭合的字符串；facts_used 不在此流式产出（个人事实先校验后推送）。"""

    def __init__(self):
        self.buf, self._done, self._points = '', set(), 0

    def feed(self, delta):
        self.buf += delta
        out = []
        for key in ('conclusion', 'example', 'caution'):
            m = None if key in self._done else re.search(rf'"{key}"\s*:\s*{_STR}', self.buf)
            if m:
                self._done.add(key)
                if _unescape(m.group(1)):
                    out.append((key, _unescape(m.group(1))))
        m = re.search(r'"points"\s*:\s*\[', self.buf)
        pos, n = (m.end() if m else 0), 0
        while m:
            im = _ITEM.match(self.buf, pos)
            if not im:
                break
            pos, n = im.end(), n + 1
            if n > self._points:
                self._points = n
                if _unescape(im.group(1)):
                    out.append(('point', _unescape(im.group(1))))
        return out
