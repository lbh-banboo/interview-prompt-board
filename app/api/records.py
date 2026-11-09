"""M12 记录 API：历史列表 / 逐字稿 / 轮次 / 提示 / 打包 / 复盘编辑 / 删除（移至回收目录）。

单文件导出 /api/sessions/{sid}/export 由 app/server.py 提前注册（浏览器下载免令牌），本模块不再重复注册。
"""
import json
import tempfile
from pathlib import Path

from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.records.bundle import write_bundle
from app.records.review_edit import Deleter, ReviewError, edit_segment
from app.records.store import RecordStore
from app.sessions.setup import canonical_status, ensure_sessions_columns


class EditBody(BaseModel):
    text: str | None = None
    role: str | None = None


def _json(v, default=None):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _counts(store, table, ids, main=False):
    if not ids:
        return {}
    marks = ','.join('?' * len(ids))
    sql = f'SELECT session_id, COUNT(*) AS n FROM {table} WHERE session_id IN ({marks}) GROUP BY session_id'
    rows = (store.main_rows if main else store.rows)(sql, tuple(ids))
    return {r['session_id']: r['n'] for r in rows}


def session_list(store, main, limit=50, offset=0):
    """历史会话列表：只读、不含逐字稿正文与 Key。旧会话缺列/缺值按空值容错。"""
    ensure_sessions_columns(main)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    total_rows = store.main_rows('SELECT COUNT(*) AS n FROM sessions')
    total = total_rows[0]['n'] if total_rows else 0
    rows = store.main_rows('SELECT * FROM sessions ORDER BY COALESCE(started_at, created_at, 0) DESC, rowid DESC '
                           'LIMIT ? OFFSET ?', (limit, offset))
    ids = [r['id'] for r in rows]
    marks = ','.join('?' * len(ids)) if ids else "''"
    turns = _counts(store, 'turns', ids)
    prompts = _counts(store, 'prompts', ids)
    recs = _counts(store, 'recordings', ids, main=True)
    gaps = _counts(store, 'recording_gaps', ids, main=True)
    deleted = {r['session_id']: r['deleted_at']
               for r in store.rows(f'SELECT session_id, deleted_at FROM deleted_sessions WHERE session_id IN ({marks})',
                                   tuple(ids))} if ids else {}
    pids = [r.get('profile_id') for r in rows if r.get('profile_id')]
    profiles = {r['id']: r['alias'] for r in store.main_rows(
        f'SELECT id, alias FROM profiles WHERE id IN ({",".join("?" * len(pids))})', tuple(pids))} if pids else {}
    lids = [r.get('library_id') for r in rows if r.get('library_id')]
    libs = {r['id']: r['name'] for r in store.main_rows(
        f'SELECT id, name FROM libraries WHERE id IN ({",".join("?" * len(lids))})', tuple(lids))} if lids else {}

    out = []
    for r in rows:
        cfg = _json(r.get('config_snapshot_json'), {})
        cfg = cfg if isinstance(cfg, dict) else {}
        start, stop = _num(r.get('started_at')), _num(r.get('stopped_at'))
        roles = _json(r.get('roles_json')) or cfg.get('roles') or []
        alias = profiles.get(r.get('profile_id')) or (cfg.get('profile') or {}).get('alias')
        name = libs.get(r.get('library_id')) or ''
        if not name and isinstance(cfg.get('libraries'), dict):
            name = '、'.join(str(v) for v in cfg['libraries'].values())
        gone = r['id'] in deleted
        nrec = recs.get(r['id'], 0)
        out.append(dict(
            id=r['id'], mode=r.get('mode') or '', status=canonical_status(r.get('status') or ''),
            started_at=start, stopped_at=stop,
            duration_s=(stop - start) if start is not None and stop is not None and stop >= start else None,
            profile_alias=alias, roles=[str(x) for x in roles] if isinstance(roles, list) else [],
            library_name=name,
            turns=0 if gone else turns.get(r['id'], 0), prompts=0 if gone else prompts.get(r['id'], 0),
            recordings=0 if gone else nrec, gaps=0 if gone else gaps.get(r['id'], 0),
            has_recording=bool(nrec) and not gone,
            deleted=gone, deleted_at=deleted.get(r['id'])))
    return dict(sessions=out, total=total, limit=limit, offset=offset)


def register(app, ctx):
    cfg = ctx.config or {}
    root = Path(ctx.root)
    main = ctx.store.connect
    store = cfg.get('record_store') or RecordStore(root / 'records.sqlite3', main=main)
    deleter = Deleter(store, cfg.get('trash_dir') or root / 'trash', root)

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(401, 'token 无效')

    def alive(sid):
        if store.rows('SELECT 1 FROM deleted_sessions WHERE session_id=?', (sid,)):
            raise HTTPException(404, '会话已移至回收目录')
        return sid

    def answerer_transcribed(sid):
        """本场是否转写回答者：与 server.py 的免令牌导出一致（读会话快照/当前配置）。
        独立挂载本模块（测试）时没有 wiring，返回 None，由导出按逐字稿推断。"""
        wiring = getattr(app.state, 'wiring', None)
        if wiring is None:
            return None
        return bool(wiring.snapshot(sid).get('outbound', {}).get('asr_answerer', False))

    def listing(table, order):
        def h(sid: str, x_local_token: str | None = Header(default=None)):
            guard(x_local_token)
            return store.rows(f'SELECT * FROM {table} WHERE session_id=? ORDER BY {order}', (alive(sid),))
        return h

    @app.get('/api/sessions')
    def sessions(limit: int = 50, offset: int = 0):
        return session_list(store, ctx.store, limit, offset)

    app.get('/api/sessions/{sid}/transcript')(listing('segments', 'COALESCE(t_start, 0), id'))
    app.get('/api/sessions/{sid}/turns')(listing('turns', 'rowid'))
    app.get('/api/sessions/{sid}/prompts')(listing('prompts', 'id'))

    # 单文件导出 /api/sessions/{sid}/export 由 app/server.py 提前注册（浏览器下载链接不带自定义头，
    # 有意免令牌）；本模块不再重复注册同路径路由，避免出现永远不可达的副本。

    @app.get('/api/sessions/{sid}/bundle')
    def bundle(sid: str, x_local_token: str | None = Header(default=None)):
        guard(x_local_token)
        tmp = tempfile.SpooledTemporaryFile(max_size=64 << 20)
        write_bundle(store, alive(sid), tmp, root, answerer_transcribed(sid))
        tmp.seek(0)

        def chunks():
            with tmp:
                while c := tmp.read(1 << 20):
                    yield c
        return StreamingResponse(chunks(), media_type='application/zip',
                                 headers={'Content-Disposition': f'attachment; filename="{sid}.zip"'})

    @app.post('/api/sessions/{sid}/segments/{seg}/edit')
    def edit(sid: str, seg: int, body: EditBody, x_local_token: str | None = Header(default=None)):
        guard(x_local_token)
        try:
            return edit_segment(store, alive(sid), seg, body.text, body.role)
        except ReviewError as e:
            raise HTTPException(e.code, str(e))

    @app.delete('/api/sessions/{sid}')
    def delete(sid: str, confirm: bool = False, confirm_token: str | None = None,
               x_local_token: str | None = Header(default=None)):
        guard(x_local_token)
        alive(sid)
        if not (confirm and confirm_token):
            raise HTTPException(409, detail=dict(need_confirm=True, confirm_token=deleter.request(sid)))
        try:
            dest = deleter.confirm(sid, confirm_token)
        except ReviewError as e:
            raise HTTPException(e.code, str(e))
        return dict(session=sid, trashed=True, path=str(dest))

    return store  # 主线用它做 attach / ingest_board_events
