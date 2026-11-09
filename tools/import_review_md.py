"""把 <audio_id>.review.md 中的勾选导入 <audio_id>.review.json 的 human 与 verified。

规则：勾「是问题结束点」或「追问」「改题」=> is_question_end:true, role:'questioner'；
      勾「不是」=> is_question_end:false；追问/改题单勾视为隐含「是」；同时勾「是」与「不是」报错跳过；
      都没勾 => 保持 verified:false 不动。「改题」=> is_topic_change:true，否则（是问题结束点时）false。
用法: .venv/bin/python tools/import_review_md.py <audio_id> [--ann-dir eval/annotations]
"""
import argparse
import json
import re
from pathlib import Path

ROW_RE = re.compile(r'^\s*(\d+)\s*\|(.*)\|(.*)$')
BOX_RE = re.compile(r'\[\s*([xX✓√])?\s*\]\s*(是问题结束点|不是|追问|改题)')


def parse_md(text):
    """返回 {序号: {'是问题结束点': bool, '不是': bool, '追问': bool, '改题': bool}}，只含有勾选框的行。"""
    out = {}
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        boxes = {name: bool(mark) for mark, name in BOX_RE.findall(m.group(3))}
        if len(boxes) == 4:
            out[int(m.group(1))] = boxes
    return out


def mark_item(it, b):
    """把一条的勾选写进 item 的 human/verified；「是」与「不是」同勾返回 False 且不改动。"""
    yes = b['是问题结束点'] or b['追问'] or b['改题']
    if yes and b['不是']:
        return False
    h = it['human']
    if yes:
        h.update(is_question_end=True, role='questioner', is_topic_change=b['改题'], is_followup=b['追问'])
    else:
        h.update(is_question_end=False, is_topic_change=None, is_followup=False)
    it['verified'] = True
    return True


def apply(doc, marks):
    """更新 doc 的 items，返回 (已导入, 冲突序号列表, 未勾选数)。"""
    done, conflicts, blank = 0, [], 0
    for it in doc['items']:
        b = marks.get(it['id'])
        if b is None or not any(b.values()):
            blank += 1
            continue
        if not mark_item(it, b):
            conflicts.append(it['id'])
            continue
        done += 1
    return done, conflicts, blank


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('audio_id')
    ap.add_argument('--ann-dir', default='eval/annotations')
    a = ap.parse_args()
    d = Path(a.ann_dir)
    jp = d / f'{a.audio_id}.review.json'
    doc = json.loads(jp.read_text(encoding='utf-8'))
    done, conflicts, blank = apply(doc, parse_md((d / f'{a.audio_id}.review.md').read_text(encoding='utf-8')))
    jp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'已导入 {done} 条，未勾选 {blank} 条，冲突(是+不是)跳过 {len(conflicts)} 条 {conflicts}')
