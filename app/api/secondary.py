"""M07 次优库 API：岗位列表、导入种子目录、审核台列表/进度、批量通过。"""
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.library.secondary import SEED_DIR, STATUSES, SecondaryLibrary


class ReviewBody(BaseModel):
    status: str
    confirmed: bool = False


class BatchBody(BaseModel):
    ids: list[str] = Field(max_length=500)
    confirmed: bool = False


def scan_marks(entry):
    """逐条合规扫描（与 tools/scan_seeds.py 同一规则），返回 [{kind, frag}]；空表示无标记。"""
    from tools.scan_seeds import scan_text
    text = "\n".join([entry["question"], *entry["variants"], entry["conclusion"],
                      *entry["points"], entry["example"], entry["caution"]])
    return [dict(kind=kind, frag=frag) for _, kind, frag in scan_text(text)]


def register(app, ctx):
    lib = SecondaryLibrary(ctx.store)
    seed_dir = Path((ctx.config or {}).get("seeds_dir") or SEED_DIR)

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(403, "本地会话令牌无效，请刷新页面")

    @app.get("/api/secondary/roles")
    def roles():
        return lib.roles()

    @app.post("/api/secondary/import")
    def import_seeds():
        return lib.import_dir(seed_dir)

    @app.get("/api/secondary/entries")
    def entries(role: str = "", status: str = ""):
        if status and status not in STATUSES:
            raise HTTPException(400, "状态无效")
        return [dict(e, scan=scan_marks(e)) for e in lib.entries(role or None, status or None)]

    @app.get("/api/secondary/progress")
    def progress():
        return lib.progress()

    @app.post("/api/secondary/{entry_id}/review")
    def review(entry_id: str, body: ReviewBody):
        try:
            ok = lib.review(entry_id, body.status, body.confirmed)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ok:
            raise HTTPException(404, "条目不存在")
        return dict(id=entry_id, status=body.status)

    @app.post("/api/secondary/approve-batch", dependencies=[Depends(guard)])
    def approve_batch(body: BatchBody):
        if not body.confirmed:
            raise HTTPException(400, "批量通过前必须确认已核对内容")
        if not body.ids:
            raise HTTPException(400, "没有选择条目")
        ids = list(dict.fromkeys(body.ids))
        known = {e["id"]: e for e in lib.entries()}
        rejected = []
        for i in ids:
            e = known.get(i)
            marks = scan_marks(e) if e else []
            if e is None:
                reasons = ["条目不存在"]
            elif e["status"] != "pending":
                reasons = ["非待审状态"]
            elif marks:
                reasons = ["合规扫描命中：" + "、".join(sorted({m["kind"] for m in marks}))]
            else:
                reasons = []
            if reasons:
                rejected.append(dict(id=i, reasons=reasons))
        if rejected:  # 整批拒绝，不做部分写入
            return JSONResponse(dict(ok=False, approved=0, rejected=rejected,
                                     detail="批量通过被拒：含非待审或合规扫描命中的条目"), status_code=409)
        for i in ids:
            lib.review(i, "approved", True)
        return dict(ok=True, approved=len(ids), rejected=[])
