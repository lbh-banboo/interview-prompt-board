"""M06 题库增强接口：register(app, ctx)。增强器从 ctx.config['enhancer'] 取，缺省视为无凭证（降级为规则拆解）。"""
import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.library import conflicts, versioning
from app.library.enhance import Progress, enhance_library


class ReimportBody(BaseModel):
    name: str = ""
    text: str


class BatchBody(BaseModel):
    keys: list[str] | None = None
    confirmed: bool = False


def register(app: FastAPI, ctx):
    store = ctx.store
    jobs, tasks = {}, set()

    def guard(library):
        if not any(x['id'] == library for x in store.libraries()):
            raise HTTPException(404, "题库不存在")

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/libraries/{library}/enhance')
    async def start_enhance(library: str):
        guard(library)
        progress = jobs.setdefault(library, Progress())
        if progress.status == "running":
            return progress.as_dict()
        progress.status = "running"
        enhancer = (ctx.config or {}).get('enhancer')
        task = asyncio.create_task(enhance_library(store, library, enhancer, progress=progress))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return progress.as_dict()

    @app.get('/api/libraries/{library}/enhance')
    def enhance_progress(library: str):
        guard(library)
        return jobs.get(library, Progress()).as_dict()

    @app.get('/api/libraries/{library}/review-entries')
    def review_entries(library: str):
        guard(library)
        return conflicts.review_entries(store, library)

    @app.get('/api/libraries/{library}/conflicts')
    def conflict_list(library: str):
        guard(library)
        return conflicts.list_conflicts(store, library)

    @app.get('/api/libraries/{library}/batch-approvable')
    def batch_approvable(library: str):
        guard(library)
        return dict(ids=conflicts.batch_approvable(store, library))

    @app.post('/api/libraries/{library}/batch-approve')
    def batch_approve(library: str, body: BatchBody):
        guard(library)
        return dict(approved=call(conflicts.batch_approve, store, library, body.keys, body.confirmed))

    @app.post('/api/libraries/{library}/reimport')
    def reimport(library: str, body: ReimportBody):
        guard(library)
        before = {e['id']: e['content_hash'] for e in conflicts.review_entries(store, library)}
        result = call(versioning.reimport, store, library, body.text, body.name or None)
        after = {e['id']: e['content_hash'] for e in conflicts.review_entries(store, library)}
        stats = dict(added=[k for k in after if k not in before],
                     changed=[k for k in after if k in before and before[k] != after[k]],
                     deleted=[k for k in before if k not in after],
                     inherited=result['inherited'])
        return dict(result, stats=stats)
