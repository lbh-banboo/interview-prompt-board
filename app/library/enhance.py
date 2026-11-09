"""模型增强拆解：Enhancer 由调用方注入；输出手写 schema 校验后再做同条目出处比对。

Ported from Kaalia0912/ai-interview-assistant@f6dc0f7b11887c0bc81d8dc5e8a37c38e9da6bef:rag-assistant/app/core/structurer.py, MIT © 2026 Kaalia0912.
Modified: 仅借用“只整理、不创作；缺失不编造”的提示词思路，输出改为本项目 JSON 结构；不含任何 LLM 调用代码。
"""
import asyncio
import json
from typing import Protocol

from .conflicts import detect_conflicts
from .facts_extract import KINDS, check_model_fact, check_texts, norm
from .versioning import connect, ensure_version

SYSTEM_PROMPT = """你是面试题库整理助手，把给定的一条问答整理成 JSON。
要求：
1. 只整理、不创作：不得新增原文没有的数字、项目名、公司名、职责与经历；
2. 信息缺失时写“需本人补充”，严禁编造；原文中的 [待核] 提醒必须保留在 cautions；
3. 只输出一个 JSON 对象：{"question": 问题, "question_variants": [3-5 种问法],
   "answer_outline": {"conclusion": 结论, "points": [恰好 3 条要点], "examples": [例子], "cautions": [注意事项]},
   "facts": [{"kind": "project|metric|role|time|org", "text": 事实, "source_span": 原文中逐字出现的片段}],
   "role_tags": [角色标签], "topic_tags": [主题标签]}"""


class Enhancer(Protocol):
    async def split(self, entry_text: str) -> dict: ...


class EnhancerUnavailable(RuntimeError):
    """增强不可用（无 Key / 外发关闭 / 预算拒绝）：保留规则拆解，不重试、不报错。"""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def build_messages(entry_text):
    return [dict(role="system", content=SYSTEM_PROMPT), dict(role="user", content=f"原文：\n{entry_text}")]


class SplitInvalid(ValueError):
    pass


def _strings(value, path, low=0, high=None):
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value) \
            or len(value) < low or (high is not None and len(value) > high):
        raise SplitInvalid(f"{path} 格式无效")
    return value


def validate_split(data):
    """手写 schema 校验；返回规整后的字典，不合规抛 SplitInvalid。"""
    if not isinstance(data, dict):
        raise SplitInvalid("输出不是对象")
    question = data.get('question')
    if not isinstance(question, str) or not question.strip():
        raise SplitInvalid("question 格式无效")
    outline = data.get('answer_outline')
    if not isinstance(outline, dict) or not isinstance(outline.get('conclusion'), str) or not outline['conclusion'].strip():
        raise SplitInvalid("answer_outline 格式无效")
    facts = data.get('facts')
    if not isinstance(facts, list):
        raise SplitInvalid("facts 格式无效")
    clean_facts = []
    for fact in facts:
        if not isinstance(fact, dict) or fact.get('kind') not in KINDS or not isinstance(fact.get('text'), str) \
                or not fact['text'].strip() or not isinstance(fact.get('source_span', ""), str):
            raise SplitInvalid("facts 项格式无效")
        clean_facts.append(dict(kind=fact['kind'], text=fact['text'].strip(), source_span=fact.get('source_span', "").strip()))
    return dict(
        question=question.strip(),
        question_variants=_strings(data.get('question_variants'), "question_variants", 3, 5),
        answer_outline=dict(conclusion=outline['conclusion'].strip(),
                            points=_strings(outline.get('points'), "points", 3, 3),
                            examples=_strings(outline.get('examples'), "examples"),
                            cautions=_strings(outline.get('cautions'), "cautions")),
        facts=clean_facts,
        role_tags=_strings(data.get('role_tags'), "role_tags"),
        topic_tags=_strings(data.get('topic_tags'), "topic_tags"))


def outline_texts(split):
    o = split['answer_outline']
    texts = [("question", split['question'])] + [("question_variants", v) for v in split['question_variants']]
    texts.append(("answer_outline", o['conclusion']))
    texts += [("answer_outline", t) for t in o['points'] + o['examples'] + o['cautions']]
    return texts


def apply_split(db, library_id, version, item_row, split, others):
    """写入模型增强结果：outline/变体/标签入 enhance_json，模型 facts 经同条目出处比对后入 facts 表。"""
    key, digest = item_row['entry_key'], item_row['content_hash']
    own = norm(item_row['text'])
    findings = check_texts(outline_texts(split), own, others, strict=False)
    db.execute("DELETE FROM facts WHERE library_id=? AND version=? AND entry_key=? AND origin='model'", (library_id, version, key))
    for fact in split['facts']:
        flag = check_model_fact(fact, own, others)
        db.execute("INSERT INTO facts(library_id,version,entry_key,entry_content_hash,kind,text,source_span,line_start,"
                   "line_end,flag,origin) VALUES(?,?,?,?,?,?,?,?,?,?,'model')",
                   (library_id, version, key, digest, fact['kind'], fact['text'], fact['source_span'],
                    item_row['line_start'], item_row['line_end'], flag))
    stored = {k: v for k, v in split.items() if k != 'facts'}
    db.execute("UPDATE entry_versions SET enhanced=1,enhance_json=?,enhance_error='',flags_json=? "
               "WHERE library_id=? AND version=? AND entry_key=?",
               (json.dumps(stored, ensure_ascii=False), json.dumps(findings, ensure_ascii=False), library_id, version, key))


class Progress:
    def __init__(self):
        self.status, self.total, self.done, self.enhanced, self.failed, self.error = "idle", 0, 0, 0, 0, ""

    def as_dict(self):
        return dict(status=self.status, total=self.total, done=self.done, enhanced=self.enhanced,
                    failed=self.failed, error=self.error)


async def enhance_library(store, library_id, enhancer, concurrency=4, progress=None):
    """对当前版本每条问答调用 enhancer.split；无 enhancer 或单条失败 → 保留规则拆解，标 enhanced=false。"""
    progress = progress or Progress()
    version = ensure_version(store, library_id)
    with connect(store) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM entry_versions WHERE library_id=? AND version=? ORDER BY rowid",
                                            (library_id, version))]
        texts = {r['id']: json.loads(r['payload']) for r in db.execute("SELECT id,payload FROM entries WHERE library_id=?", (library_id,))}
    for r in rows:
        r['text'] = texts[r['entry_key']]['text']
    spans = {r['entry_key']: norm(r['text']) for r in rows}
    progress.status, progress.total, progress.done, progress.enhanced, progress.failed = "running", len(rows), 0, 0, 0
    gate = asyncio.Semaphore(max(1, concurrency))

    def mark(row, error):
        with store.lock, connect(store) as db:
            db.execute("UPDATE entry_versions SET enhanced=0,enhance_error=? WHERE library_id=? AND version=? AND entry_key=?",
                       (error, library_id, version, row['entry_key']))

    async def one(row):
        try:
            if enhancer is None:
                mark(row, "no_enhancer")
                progress.failed += 1
                return
            async with gate:
                try:
                    split = validate_split(await enhancer.split(row['text']))
                except EnhancerUnavailable as exc:
                    mark(row, exc.reason)
                    if not progress.error:
                        progress.error = exc.reason
                    progress.failed += 1
                    return
                except SplitInvalid as exc:
                    mark(row, f"invalid_schema:{exc}")
                    progress.failed += 1
                    return
                except Exception as exc:  # 凭证/网络等失败：只记异常类型，不记内容
                    mark(row, f"call_failed:{type(exc).__name__}")
                    progress.failed += 1
                    return
            others = [t for k, t in spans.items() if k != row['entry_key']]
            with store.lock, connect(store) as db:
                apply_split(db, library_id, version, row, split, others)
            progress.enhanced += 1
        finally:
            progress.done += 1

    await asyncio.gather(*(one(r) for r in rows))
    with store.lock, connect(store) as db:
        detect_conflicts(db, library_id, version)
    progress.status = "done"
    return progress
