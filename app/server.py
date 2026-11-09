import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import logging_safe
from .api import annotations, library_enhance, library_review, records, secondary, settings, setup, voice
from .config import ConfigManager
from .context import AppContext
from .core import Store, redact_keys
from .credentials import CredentialStore
from .realtime import audio_ws, board_ws
from .library.secondary import SEED_DIR, SecondaryLibrary
from .library.conflicts import approve_block_reason
from .library.enhance_llm import DeepSeekEnhancer
from .realtime.answer import AnswerLoop, IndexProvider
from .realtime.answer_ext import Externals
from .realtime.events import Activity, EventBus
from .records.export import export_markdown
from .sessions import probes
from .sessions.setup import ensure_sessions_columns
from .usage import DISCLAIMER, BudgetGuard, UsageMeter
from .wiring import Wiring

ROOT = Path(__file__).resolve().parents[1]
SETUP_START = re.compile(r'/api/setup/([^/]+)/start')
CREDENTIAL_LABELS = {'asr': 'DASHSCOPE_API_KEY', 'llm': 'DEEPSEEK_API_KEY', 'search': 'SEARCH_API_KEY'}  # 状态里不出现凭证名
_log_handler = []  # 进程内只保留最近一个日志 handler（create_app 多次调用时替换）


def _secondary_seeds_outdated(store, seed_dir):
    """次优库为空、或种子文件内容与库内 source_hash 不一致时返回 True。"""
    seed_dir = Path(seed_dir)
    if not seed_dir.is_dir():
        return False
    with store.connect() as db:
        if not db.execute('SELECT 1 FROM secondary_entries LIMIT 1').fetchone():
            return True
        for path in sorted(seed_dir.glob('*.md')):
            if path.name.lower() == 'readme.md':
                continue
            digest = hashlib.sha256(path.read_text('utf-8').encode()).hexdigest()
            row = db.execute('SELECT source_hash FROM secondary_entries WHERE source=? LIMIT 1', (path.name,)).fetchone()
            if row is None or row['source_hash'] != digest:
                return True
    return False


class ImportBody(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=5_000_000)


class ReviewBody(BaseModel):
    answer: str = Field(max_length=20000)
    status: str
    confirmed: bool = False


class StartBody(BaseModel):
    mode: str
    library: str
    consent: bool


class EventBody(BaseModel):
    kind: str
    question: str = Field(default='', max_length=5000)
    turn_id: str | None = None
    content: str = Field(default='', max_length=20000)


def create_app(data_dir=None, asr_factory=None, clock=None, idle_timeout_s=None, trigger='vad',
               embedder=None, config=None, credentials=None):
    """config：并入 ctx.config 的额外项（如 voice_extractor / voice_model / selfcheck_deps，测试注入用）；
    credentials：CredentialStore，缺省按 环境变量 -> 钥匙串 -> data/.env。"""
    store = Store(data_dir or ROOT / 'data')
    ensure_sessions_columns(store)
    manager = ConfigManager(store.root / 'config.json')
    creds = credentials or CredentialStore.default(store.root / '.env')
    if _log_handler:
        logging.getLogger().removeHandler(_log_handler.pop())
    _log_handler.append(logging_safe.install(store.root / 'logs'))
    logging_safe.mask_uvicorn_tokens()  # WS 地址带 ?token=，uvicorn 自己的日志会原样写出
    token = secrets.token_urlsafe(32)
    app = FastAPI(title='面试提示板：本地验证版')
    app.state.store = store
    app.state.config_manager, app.state.credential_store = manager, creds
    cfg = manager.config
    meter = app.state.usage_meter = UsageMeter(store.path, cfg['prices'])
    app.state.budget = BudgetGuard(meter, cfg['budget']['daily_limit'], cfg['budget']['session_limit'],
                                   cfg['budget']['max_inflight'])
    bus = app.state.bus = EventBus()
    bus.pipe_health = False  # health 统一由 CapabilityBoard 发布
    wiring = app.state.wiring = Wiring(store, manager, creds, bus, clock or time.monotonic, voice_cfg=config,
                                       budget=app.state.budget)
    hub = app.state.audio_hub = audio_ws.AudioHub(store, asr_factory=asr_factory, trigger=trigger)
    hub.configure = lambda session, mode: wiring.configure(session, mode, trigger, asr_factory is not None)
    hub.on_late_metric = wiring.on_late_metric
    bus.on_late, bus.on_pull = wiring.on_late, wiring.on_pull
    ctx = AppContext(store=store, root=store.root, token=token, bus=bus,
                     config=dict(config or {}, config_manager=manager, credential_store=creds,
                                 outbound=dict(cfg['outbound'])))
    app.state.ctx = ctx
    if 'enhancer' not in ctx.config:  # 测试注入优先；缺省接 DeepSeek（无 Key/外发关闭/预算不足时降级）
        ctx.config['enhancer'] = DeepSeekEnhancer(creds, app.state.budget, lambda: manager.config,
                                                  transport=ctx.config.get('llm_transport'))
    idle = (lambda session: idle_timeout_s) if idle_timeout_s is not None else wiring.idle_timeout
    provider = app.state.index_provider = IndexProvider(store, embedder)
    answers = app.state.answer_loops = {}

    secondary_lib = SecondaryLibrary(store)
    try:  # 全新安装 L2 不为空：种子自动导入；已审核条目在内容未变时保持原状态。失败只记录，不阻断启动。
        seed_dir = Path((config or {}).get('seeds_dir') or SEED_DIR)
        if _secondary_seeds_outdated(store, seed_dir):
            secondary_lib.import_dir(seed_dir)
    except Exception:
        logging.getLogger('app').exception('次优库种子自动导入失败，启动继续')

    def attach_answer(session, pipe):
        snap = wiring.snapshot(session)
        sc = snap.get('search') or {}
        ext = Externals(secondary=secondary_lib, outbound=lambda: wiring.snapshot(session)['outbound'],
                        timeouts=lambda: wiring.snapshot(session)['timeouts'], creds=creds, budget=app.state.budget,
                        health=lambda: wiring.health(session), model=snap['model']['name'],
                        llm_live=bool(creds.get('DEEPSEEK_API_KEY')),
                        llm_base_url=snap.get('model', {}).get('base_url', 'https://api.deepseek.com'),
                        llm_first_timeout_s=snap.get('timeouts', {}).get('llm_first_ms', 3000) / 1000,
                        llm_timeout_s=snap.get('timeouts', {}).get('llm_total_ms', 8000) / 1000,
                        search_transport=ctx.config.get('search_transport'), llm_transport=ctx.config.get('llm_transport'),
                        search_live=bool(sc.get('live', True)),
                        search_endpoint=sc.get('endpoint', 'https://api.deepseek.com/v1/tools/search'),
                        search_count=sc.get('count', 5), search_need_content=bool(sc.get('need_content', False)))
        loop = answers[session] = AnswerLoop(session, store, bus, provider, pipe.metrics, pipe=pipe, ext=ext)
        loop.attach()

    async def close_answer(session):
        loop = answers.pop(session, None)
        if loop is not None:
            await loop.aclose()
        await wiring.stop_session(session)

    hub.on_pipeline, hub.on_closed = attach_answer, close_answer
    hub.on_progress = lambda session: bus.pull(session, hub.pipelines.get(session))
    activity = app.state.activity = Activity(clock) if clock else Activity()
    orig_ingest = hub.ingest

    def ingest(session, frame):  # 记录最近音频帧时刻（超时停止与音源灯）；AudioHub 本身不改
        activity.touch(session, frame['source'])
        return orig_ingest(session, frame)

    hub.ingest = ingest
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def secure(request: Request, call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('origin')
            if origin and (urlsplit(origin).netloc != request.headers.get('host') or urlsplit(origin).scheme not in ('http', 'https')):
                return JSONResponse({'detail': '禁止跨站请求'}, status_code=403)
            if request.headers.get('x-local-token') != token:
                return JSONResponse({'detail': '本地会话令牌无效，请刷新页面'}, status_code=403)
            length = request.headers.get('content-length')
            if length and (not length.isdigit() or int(length) > 8_000_000):
                return JSONResponse({'detail': '请求过大'}, status_code=413)
        response = await call_next(request)
        started = SETUP_START.fullmatch(request.url.path)
        if started and request.method == 'POST' and response.status_code == 200:
            sid = started.group(1)
            wiring.freeze(sid)  # /api/setup/{sid}/start 把会话置 active：此刻冻结配置
            with store.connect() as db:
                row = db.execute('SELECT library_id FROM sessions WHERE id=?', (sid,)).fetchone()
            if row and row['library_id']:  # 与 POST /api/sessions 相同：会前预建检索索引
                state = await run_in_threadpool(provider.prepare, row['library_id'])
                wiring.health(sid).set_retrieval(state)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Permissions-Policy'] = 'camera=(), geolocation=()'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logging.getLogger('app').exception('未处理异常')  # 细节只进服务端日志
        return JSONResponse({'detail': '服务内部错误'}, status_code=500)

    @app.get('/')
    @app.get('/index.html')
    def home():
        return FileResponse(ROOT / 'web' / 'index.html', media_type='text/html')  # 欢迎首页；不再 302 跳工作台

    @app.get('/api/status')
    async def status():
        await board_ws.expire_idle(store, hub, bus, activity, idle)
        with store.connect() as db:
            row = db.execute("SELECT id FROM sessions WHERE status='active'").fetchone()
        active = row['id'] if row else None
        backend = 'injected' if asr_factory is not None else wiring.asr_backend.get(active, 'none')
        label_cfg = dict(ctx.config, outbound=cfg.get('outbound', {}), search=cfg.get('search', {}))
        return dict(token=token, active_session=store.session(active) if active else None,
                    **probes.status_labels(creds, label_cfg, store, store.root), retrieval=provider.state,
                    asr_backend=backend,  # 真实能力文案；Key 只按 configured/missing 呈现，不外发请求
                    credentials={label: creds.status(name) for label, name in CREDENTIAL_LABELS.items()},  # 只给 configured/missing
                    budget=dict(spent_today=round(meter.spent_today(), 4), daily_limit=cfg['budget']['daily_limit'],
                                session_limit=cfg['budget']['session_limit'], note=DISCLAIMER),
                    stage='本地录音、审核与检索验证；不是实时面试系统验收版')

    @app.get('/api/libraries')
    def libraries():
        return store.libraries()

    @app.post('/api/import')
    def import_text(body: ImportBody):
        if len(body.text.encode('utf-8')) > 5_000_000:
            raise ValueError('资料超过 5MB，请拆分后导入')
        return store.import_text(body.name, body.text)

    @app.get('/api/libraries/{library}/entries')
    def entries(library: str):
        return store.entries(library)

    @app.post('/api/libraries/{library}/entries/{key}/review')
    def review(library: str, key: str, body: ReviewBody):
        if body.status == 'approved':  # 单条启用同样硬拦跨条目事实冲突
            reason = approve_block_reason(store, library, key)
            if reason:
                raise ValueError(reason)
        store.review(library, key, body.answer, body.status, body.confirmed)
        return {'ok': True}

    @app.get('/api/search')
    def search(library: str, q: str):
        if not q.strip() or len(q) > 5000:
            raise ValueError('请输入有效问题')
        return store.search(library, q)

    @app.post('/api/sessions')
    async def start(body: StartBody):
        if not body.consent:
            raise ValueError('请确认录音知情及辅助工具许可')
        result = await run_in_threadpool(store.start, body.mode, body.library)
        wiring.freeze(result['id'])
        activity.touch(result['id'])
        state = await run_in_threadpool(provider.prepare, body.library)  # 会前预建索引；失败不阻止会话开始，只在看板标 retrieval=down
        wiring.health(result['id']).set_retrieval(state)
        return result

    @app.post('/api/sessions/{key}/stop')
    async def stop(key: str):
        result = store.stop(key)
        pipe = hub.pipelines.get(key)
        await hub.aclose(key)
        bus.pull(key, pipe)  # pull / end 的 on_pull 钩子把事件写入记录库
        bus.end(key)
        return result

    @app.get('/api/sessions/{key}/snapshot')
    async def snapshot(key: str):
        with store.connect() as db:
            row = db.execute('SELECT status FROM sessions WHERE id=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, '会话不存在')
        await board_ws.expire_idle(store, hub, bus, activity, idle)
        bus.pull(key, hub.pipelines.get(key), activity)
        with store.connect() as db:
            row = db.execute('SELECT status, mode FROM sessions WHERE id=?', (key,)).fetchone()
        session_status = row['status']
        result = dict(bus.snapshot(key), session_status=session_status, mode=row['mode'])
        if session_status != 'active':
            result['status'] = 'ended'
        return result

    @app.post('/api/sessions/{key}/events')
    def event(key: str, body: EventBody):
        return store.event(key, **body.model_dump())

    @app.get('/api/sessions/{key}/export')
    def export(key: str):
        """记录版导出（逐字稿 / 轮次 / 系统建议分区）。

        有意注册在 records 路由之前并保持免令牌：浏览器下载链接无法附加 X-Local-Token。
        暴露面仅限回环地址（TrustedHostMiddleware 只允许 127.0.0.1/localhost），
        非 GET 的同源校验不覆盖下载，故不要再给本路由加令牌或改注册顺序。
        """
        store.session(key)
        if records_store.rows('SELECT 1 FROM deleted_sessions WHERE session_id=?', (key,)):
            raise HTTPException(404, '会话已移至回收目录')
        wiring.on_pull(key, hub.pipelines.get(key))
        transcribed = bool(wiring.snapshot(key).get('outbound', {}).get('asr_answerer', False))
        text = export_markdown(records_store, key, store.root, answerer_transcribed=transcribed)
        return PlainTextResponse(redact_keys(text), media_type='text/markdown; charset=utf-8',
                                 headers={'Content-Disposition': 'attachment; filename="interview.md"'})

    @app.get('/api/sessions/{key}/recordings')
    def recordings(key: str):
        store.session(key)
        with store.connect() as db:
            return [dict(id=r['id'], source=r['source'], sequence=r['sequence']) for r in db.execute('SELECT * FROM recordings WHERE session_id=? ORDER BY source,sequence', (key,))]

    @app.post('/api/sessions/{key}/audio/{source}/{sequence}')
    async def audio(key: str, source: str, sequence: int, request: Request):
        if source not in ('mic', 'meeting') or not 0 <= sequence <= 100000:
            raise ValueError('音源或序号无效')
        mime = request.headers.get('content-type', '').split(';')[0]
        if mime not in ('audio/webm', 'audio/mp4', 'audio/ogg'):
            raise ValueError('录音格式不支持')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8_000_000:
                raise HTTPException(413, '录音分段过大')
        if not data:
            raise ValueError('录音为空')
        # Each browser segment starts a new container: downloadable independently.
        with store.lock, store.connect() as db:
            store.session(key)
            existing = db.execute('SELECT id FROM recordings WHERE session_id=? AND source=? AND sequence=?', (key, source, sequence)).fetchone()
            if existing:
                return {'saved': True, 'id': existing['id']}
            rid = uuid.uuid4().hex
            path = store.root / (rid + {'audio/webm': '.webm', 'audio/mp4': '.m4a', 'audio/ogg': '.ogg'}[mime])
            with path.open('xb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            db.execute('INSERT INTO recordings VALUES(?,?,?,?,?,?)', (rid, key, source, mime, sequence, path.name))
        return {'saved': True, 'id': rid}

    @app.get('/api/recordings/{rid}')
    def recording(rid: str):
        with store.connect() as db:
            row = db.execute('SELECT * FROM recordings WHERE id=?', (rid,)).fetchone()
        if not row:
            raise HTTPException(404, '录音不存在')
        return FileResponse(store.root / row['path'], media_type=row['mime'], filename=Path(row['path']).name)

    for module in (annotations, library_review, library_enhance, secondary, voice):
        module.register(app, ctx)
    records_store = wiring.records = app.state.record_store = records.register(app, ctx)
    settings.register(app, ctx)
    setup.register(app, ctx)
    audio_ws.register(app, store, token, hub)
    board_ws.register(app, store, token, hub, bus, activity, idle, records=records_store)
    app.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='web')  # 必须在所有路由之后
    return app


def __getattr__(name):  # 供 uvicorn 'app.server:app'；惰性创建，导入本模块（含测试）不触碰 data/
    if name == 'app':
        globals()['app'] = create_app()
        return globals()['app']
    raise AttributeError(name)
