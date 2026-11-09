"""M01 路由：/api/profiles、/api/setup/*。由主线 register(app, ctx) 接线。

自检依赖通过 ctx.config['selfcheck_deps']（dict，可注入假依赖）；缺省项报“未配置”。
"""
import shutil

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from app.sessions import profiles as P
from app.sessions import probes
from app.sessions import setup as S
from app.sessions.selfcheck import default_registry, run_selfcheck


class ProfileBody(BaseModel):
    alias: str
    roles: list[str] = []
    note: str = ''
    library_ids: list[str] = []
    content: str = ''


class ContentBody(BaseModel):
    text: str


class CreateBody(BaseModel):
    mode: str
    profile_id: str
    roles: list[str]
    library_ids: list[str] = []
    consent: bool = False
    secondary_only: bool = False
    outbound: dict[str, bool] = {}


class UpdateBody(BaseModel):
    profile_id: str | None = None
    roles: list[str] | None = None


class StartBody(BaseModel):
    degraded: bool = False


def register(app, ctx):
    store = ctx.store
    cfg = ctx.config or {}
    extra_roles = tuple(cfg.get('extra_roles', ()))

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(403, '本地会话令牌无效，请刷新页面')

    def run(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (S.SetupError, P.ProfileError) as exc:
            raise HTTPException(exc.status, exc.message) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/profiles')
    def profiles():
        return P.list_profiles(store)

    @app.get('/api/profiles/{pid}')
    def profile(pid: str):
        return run(P.get_profile, store, pid)

    @app.post('/api/profiles', dependencies=[Depends(guard)])
    def profile_create(body: ProfileBody):
        return run(P.create_profile, store, body.alias, body.roles, body.note, body.library_ids, body.content)

    @app.put('/api/profiles/{pid}', dependencies=[Depends(guard)])
    def profile_update(pid: str, body: ProfileBody):
        return run(P.update_profile, store, pid, body.alias, body.roles, body.note, body.library_ids)

    @app.post('/api/profiles/{pid}/content', dependencies=[Depends(guard)])
    def profile_content(pid: str, body: ContentBody):
        return run(P.set_profile_content, store, pid, body.text)

    @app.delete('/api/profiles/{pid}', dependencies=[Depends(guard)])
    def profile_delete(pid: str):
        run(P.delete_profile, store, pid)
        return dict(ok=True)

    @app.get('/api/setup/options')
    def options():
        return dict(roles=S.role_options(extra_roles), libraries=S.library_options(store), modes=list(S.MODES))

    @app.post('/api/setup/sessions', dependencies=[Depends(guard)])
    def create(body: CreateBody):
        return run(S.create_session_v2, store, body.mode, body.profile_id, body.roles, body.library_ids,
                   body.consent, extra_roles, body.secondary_only, outbound=body.outbound)

    @app.get('/api/setup/{sid}')
    def get(sid: str):
        return run(S.session_view, store, sid)

    @app.put('/api/setup/{sid}', dependencies=[Depends(guard)])
    def update(sid: str, body: UpdateBody):
        return run(S.update_session_config, store, sid, body.profile_id, body.roles, extra_roles)

    @app.post('/api/setup/{sid}/selfcheck', dependencies=[Depends(guard)])
    async def selfcheck(sid: str):
        s = run(S.session_view, store, sid)
        item_timeout = cfg.get('selfcheck_item_timeout', 3.0)
        # 生产默认探测兜底；测试/用户注入的 selfcheck_deps 优先（含显式 None）。
        # 会话快照里的本场外发覆盖并入探测（与运行时一致：以快照为准）。
        deps = probes.default_deps(ctx, timeout=min(2.0, item_timeout),
                                   config=probes.session_outbound(ctx, s['config']))
        deps.update(cfg.get('selfcheck_deps') or {})
        deps.setdefault('disk_free', lambda: shutil.disk_usage(ctx.root).free)
        reg = default_registry(deps, lambda: S.approved_total(store, sid))
        report = await run_selfcheck(reg.for_mode(s['mode']), item_timeout,
                                     cfg.get('selfcheck_total_timeout', 10.0))
        run(S.mark_ready, store, sid, report)
        return report

    @app.post('/api/setup/{sid}/start', dependencies=[Depends(guard)])
    def start(sid: str, body: StartBody):
        return run(S.start_session, store, sid, body.degraded)

    @app.post('/api/setup/{sid}/stop', dependencies=[Depends(guard)])
    def stop(sid: str):
        return run(S.stop_session, store, sid)
