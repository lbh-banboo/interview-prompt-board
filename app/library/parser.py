import re

ENTRY = re.compile(r"^\*\*(H\d+-\d+)｜([^｜]+)｜(.+?)\*\*\s*参考回答：\s*(.*)$")


def parse_markdown(text: str):
    items, section = [], ""
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if line.startswith("## H"):
            section = line[3:]
        match = ENTRY.match(line)
        if match:
            key, timestamp, question, answer = match.groups()
            items.append(dict(id=key, timestamp=timestamp, question=question,
                              answer=answer, section=section, line=number, original=line,
                              flagged="待核" in line, attribution_uncertain="说话人待核" in question))
    if not items:
        raise ValueError("没有识别到附录问答格式；本版仅支持 H编号｜时间｜问题 + 参考回答 格式")
    if len({x['id'] for x in items}) != len(items):
        raise ValueError("题目编号重复，请先核对原文")
    _attach_spans(items, lines)
    return items


def _attach_spans(items, lines):
    """行号范围：条目起始行到下一条目/标题前的最后一个非空行；续行里的 [待核] 同样计入。"""
    for i, item in enumerate(items):
        stop = items[i + 1]['line'] - 1 if i + 1 < len(items) else len(lines)
        end = item['line']
        for number in range(item['line'] + 1, stop + 1):
            line = lines[number - 1]
            if line.startswith("#"):
                break
            if line.strip():
                end = number
        item['line_start'], item['line_end'] = item['line'], end
        item['text'] = "\n".join(lines[item['line'] - 1:end])
        if "待核" in item['text']:
            item['flagged'] = True
