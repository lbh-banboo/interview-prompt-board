"""主库审核工作台后端：优先队列、安全批量启用、条目编辑、审核进度。路由由主线 register(app, ctx) 接线。"""
import json
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.library import conflicts, versioning
from app.library.enhance import outline_texts
from app.library.facts_extract import KINDS, check_model_fact, check_texts, norm
from app.retrieval.tags import g3_eligible, pending_marks

G3_TARGET = 50
FLAG_LABEL = {'pending_verify': '待核', 'conflict': '事实冲突', 'suspected_fabrication': '疑似虚构', 'cross_entry': '跨条目拼接'}


class BatchBody(BaseModel):
    ids: list[str] = Field(max_length=500)
    confirmed: bool = False


class FactBody(BaseModel):
    kind: str = Field(max_length=20)
    text: str = Field(max_length=500)
    source_span: str = Field(default='', max_length=2000)


class OutlineBody(BaseModel):
    conclusion: str = Field(default='', max_length=2000)
    points: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=20)
    cautions: list[str] = Field(default_factory=list, max_length=20)


class EditBody(BaseModel):
    question: str = Field(default='', max_length=500)
    variants: list[str] = Field(default_factory=list, max_length=8)
    outline: OutlineBody = Field(default_factory=OutlineBody)
    facts: list[FactBody] = Field(default_factory=list, max_length=50)


def unsafe_reasons(entry, conflict=False):
    """批量启用的唯一判据：返回不可批量的原因列表，空表示安全。conflict 来自 facts.conflict_group。"""
    reasons = []
    if entry.get('flagged'):
        reasons.append('含待核标记')
    if entry.get('attribution_uncertain'):
        reasons.append('说话人待核')
    if conflict:
        reasons.append('事实冲突（跨条目）')
    for f in entry.get('flags') or []:
        reasons.append('标记：' + FLAG_LABEL.get(f, str(f)))
    return reasons


def default_queue(entries):
    rows = [e for e in entries if e['status'] == 'pending']
    rows.sort(key=lambda e: bool(unsafe_reasons(e)))  # 稳定排序：无待核优先，其余保持库内顺序
    return [dict(n=i, id=e['id'], tier='-', why='未审核' + ('，含待核' if unsafe_reasons(e) else ''),
                 flagged=bool(e.get('flagged')), question=e['question']) for i, e in enumerate(rows, 1)]


def register(app, ctx):
    store = ctx.store

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(403, '本地会话令牌无效，请刷新页面')

    def load(library):
        entries = store.entries(library)
        if not entries:
            raise HTTPException(404, '题库不存在或为空')
        return entries

    @app.get('/api/libraries/{library}/review-queue')
    def review_queue(library: str):
        entries = load(library)
        path = (ctx.config or {}).get('review_queue_path')
        if path and Path(path).is_file():
            known = {e['id']: e for e in entries}
            queue = [q for q in json.loads(Path(path).read_text(encoding='utf-8')) if q.get('id') in known]
            return [q for q in queue if known[q['id']]['status'] == 'pending']
        return default_queue(entries)

    @app.post('/api/libraries/{library}/approve-batch', dependencies=[Depends(guard)])
    def approve_batch(library: str, body: BatchBody):
        if not body.confirmed:
            raise HTTPException(400, '批量启用前必须确认已核对事实')
        if not body.ids:
            raise HTTPException(400, '没有选择条目')
        known = {e['id']: e for e in load(library)}
        conflicted = conflicts.conflict_entry_keys(store, library)
        rejected = []
        for i in body.ids:
            reasons = ['条目不存在'] if i not in known else unsafe_reasons(known[i], i in conflicted)
            if reasons:
                rejected.append(dict(id=i, reasons=reasons))
        if rejected:  # 整批拒绝，不做部分写入
            return JSONResponse(dict(ok=False, approved=0, rejected=rejected, detail='批量启用被拒：含待核/冲突/疑似虚构条目'), status_code=409)
        for i in body.ids:
            e = known[i]
            store.review(library, i, e['approved_answer'] or e['answer'], 'approved', True)
        return dict(ok=True, approved=len(body.ids), rejected=[])

    @app.put('/api/libraries/{library}/entries/{key}', dependencies=[Depends(guard)])
    def edit_entry(library: str, key: str, body: EditBody):
        """改问题/变体/提纲/事实；保存后该条目回到待审核（已启用条目必须重新审核）。"""
        if not any(x['id'] == library for x in store.libraries()):
            raise HTTPException(404, '题库不存在')
        with store.connect() as db:
            if db.execute("SELECT 1 FROM sessions WHERE status='active' AND library_id=?", (library,)).fetchone():
                raise HTTPException(409, '本场题库已冻结，结束会话后再修改')
        question = body.question.strip()
        if not question:
            raise HTTPException(400, '问题不能为空')
        variants = [v.strip() for v in body.variants if v.strip()]
        if any(len(v) > 300 for v in variants):
            raise HTTPException(400, '问题变体过长')
        outline = dict(
            conclusion=body.outline.conclusion.strip(),
            points=[x.strip() for x in body.outline.points if x.strip()],
            examples=[x.strip() for x in body.outline.examples if x.strip()],
            cautions=[x.strip() for x in body.outline.cautions if x.strip()])
        if any(len(x) > 1000 for x in [outline['conclusion'], *outline['points'], *outline['examples'], *outline['cautions']]):
            raise HTTPException(400, '提纲内容过长')
        facts = []
        for f in body.facts:
            if f.kind not in KINDS:
                raise HTTPException(400, '事实类型无效')
            if not f.text.strip():
                raise HTTPException(400, '事实内容不能为空')
            facts.append(dict(kind=f.kind, text=f.text.strip(), source_span=f.source_span.strip()))
        version = versioning.ensure_version(store, library)
        with store.lock, versioning.connect(store) as db:
            row = db.execute("SELECT payload FROM entries WHERE library_id=? AND id=?", (library, key)).fetchone()
            meta = db.execute("SELECT * FROM entry_versions WHERE library_id=? AND version=? AND entry_key=?",
                              (library, version, key)).fetchone()
            if not row or not meta:
                raise HTTPException(404, '条目不存在')
            payload = json.loads(row['payload'])
            own = norm(payload.get('text') or '')
            others = [norm((json.loads(x['payload']) or {}).get('text') or '') for x in
                      db.execute("SELECT payload FROM entries WHERE library_id=? AND id<>?", (library, key))]
            old = json.loads(meta['enhance_json']) if meta['enhance_json'] else {}
            split = dict(question=question, question_variants=variants, answer_outline=outline,
                         role_tags=old.get('role_tags', []), topic_tags=old.get('topic_tags', []))
            findings = check_texts(outline_texts(dict(split, facts=facts)), own, others, strict=False)
            db.execute("DELETE FROM facts WHERE library_id=? AND version=? AND entry_key=?", (library, version, key))
            for fact in facts:
                db.execute("INSERT INTO facts(library_id,version,entry_key,entry_content_hash,kind,text,source_span,"
                           "line_start,line_end,flag,origin) VALUES(?,?,?,?,?,?,?,?,?,?,'manual')",
                           (library, version, key, meta['content_hash'], fact['kind'], fact['text'],
                            fact['source_span'], meta['line_start'], meta['line_end'], check_model_fact(fact, own, others)))
            payload['question'] = question
            db.execute("UPDATE entries SET payload=?,status='pending',approved_answer='' WHERE library_id=? AND id=?",
                       (json.dumps(payload, ensure_ascii=False), library, key))
            db.execute("UPDATE entry_versions SET status='pending',approved_answer='',enhance_json=?,flags_json=? "
                       "WHERE library_id=? AND version=? AND entry_key=?",
                       (json.dumps(split, ensure_ascii=False), json.dumps(findings, ensure_ascii=False),
                        library, version, key))
            conflicts.detect_conflicts(db, library, version)
        return dict(ok=True, id=key, status='pending')

    @app.get('/api/libraries/{library}/progress')
    def progress(library: str):
        entries = load(library)
        count = lambda s: sum(e['status'] == s for e in entries)
        approved = count('approved')
        approved_entries = [e for e in entries if e['status'] == 'approved']
        eligible = sum(g3_eligible(e) for e in approved_entries)  # G3 只计已启用且非待核
        pending_approved = sum(pending_marks(e) > 0 for e in approved_entries)
        hint = f'已启用 {approved}/{G3_TARGET} · 其中可计入 G3 {eligible} · 达标 {G3_TARGET}'
        if pending_approved:
            hint += f'；含待核 {pending_approved} 条需逐条核实'
        if eligible >= G3_TARGET:
            hint += '；G3 可用'
        return dict(total=len(entries), approved=approved, rejected=count('rejected'), pending=count('pending'),
                    flagged_pending=sum(e['status'] == 'pending' and bool(unsafe_reasons(e)) for e in entries),
                    reviewed=approved + count('rejected'), g3_eligible=eligible,
                    g3_flagged=approved - eligible, pending_approved=pending_approved,
                    target=G3_TARGET, g3_ready=eligible >= G3_TARGET, hint=hint)
