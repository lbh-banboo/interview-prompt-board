"""设置页后端：配置、凭证状态（永不回显明文）、用量。路由由主线 register(app, ctx) 接线。"""
import json
from pathlib import Path

from fastapi import Body, Depends, Header, HTTPException

from ..config import DEFAULT_PATH, ConfigManager
from ..credentials import KNOWN_NAMES, NAME_RE, CredentialStore
from ..usage import UsageMeter

THRESHOLD_KEYS = ('tau_cos', 'tau_cos_strong', 'tau_mid', 'margin')


def _thresholds_source(config):
    """当前配置对应的阈值来源：任一阈值已设置 -> 下一场会话快照；全部留空 -> 默认文件。"""
    th = (config or {}).get('retrieval_thresholds') or {}
    return 'snapshot' if any(th.get(k) is not None for k in THRESHOLD_KEYS) else 'default'


def register(app, ctx):
    cfg = ctx.config or {}
    root = Path(ctx.root)
    manager = cfg.get('config_manager') or ConfigManager(root / 'config.json', cfg.get('default_config_path', DEFAULT_PATH))
    creds = cfg.get('credential_store') or CredentialStore.default(root / '.env')
    app.state.config_manager, app.state.credential_store = manager, creds

    # 限额/价格改为按当前配置读取：保存设置后下一场会话即生效，无需重启。
    if getattr(app.state, 'budget', None) is not None:
        app.state.budget.bind_config(manager)
    if getattr(app.state, 'usage_meter', None) is not None:
        app.state.usage_meter.set_prices_provider(lambda: manager.config['prices'])

    def _snapshot_thresholds():
        """当前活动会话（若有）的阈值快照；没有活动会话返回 None（回落默认文件）。"""
        wiring = getattr(app.state, 'wiring', None)
        if wiring is None:
            return None
        with ctx.store.connect() as db:
            row = db.execute("SELECT id FROM sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        return (wiring.snapshot(row['id']) or {}).get('retrieval_thresholds')

    provider = getattr(app.state, 'index_provider', None)
    if provider is not None and hasattr(provider, 'set_thresholds_provider'):
        provider.set_thresholds_provider(_snapshot_thresholds)

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(403, '本地会话令牌无效，请刷新页面')

    def session_active():
        with ctx.store.connect() as db:
            return db.execute("SELECT 1 FROM sessions WHERE status='active'").fetchone() is not None

    def valid_name(name):
        if not NAME_RE.match(name):
            raise HTTPException(400, '凭证名无效')

    def defaults():
        """出厂默认值（无凭证），供设置页显示“恢复默认”；读不到时为空。"""
        try:
            return json.loads(Path(manager.default_path).read_text('utf-8'))
        except (OSError, ValueError):
            return {}

    @app.get('/api/config')
    def get_config():
        return dict(config=manager.config, errors=manager.errors, defaults=defaults(),
                    thresholds_source=_thresholds_source(manager.config))

    @app.put('/api/config', dependencies=[Depends(guard)])
    def put_config(patch: dict = Body(...)):
        active = session_active()
        try:
            config = manager.update(patch)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return dict(config=config, active_session=active, thresholds_source=_thresholds_source(config),
                    effective='下一场生效' if active else '已保存，下一场会话生效')

    @app.get('/api/credentials/status')
    def credentials_status():
        return creds.report(KNOWN_NAMES)

    @app.put('/api/credentials/{name}', dependencies=[Depends(guard)])
    def put_credential(name: str, body: dict = Body(...)):
        valid_name(name)
        try:
            creds.set(name, body.get('value'))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from None
        return dict(name=name, status=creds.status(name))

    @app.delete('/api/credentials/{name}', dependencies=[Depends(guard)])
    def delete_credential(name: str):
        valid_name(name)
        creds.delete(name)
        return dict(name=name, status=creds.status(name))

    @app.get('/api/usage')
    def usage():
        meter = UsageMeter(ctx.store.path, manager.config['prices'])
        meter.set_prices_provider(lambda: manager.config['prices'])
        b = manager.config['budget']
        report = meter.report()
        return dict(report, daily_limit=b['daily_limit'], session_limit=b['session_limit'],
                    asr_daily_limit=b.get('asr_daily_limit'), asr_session_limit=b.get('asr_session_limit'),
                    max_inflight=b['max_inflight'], note=report['note'])
