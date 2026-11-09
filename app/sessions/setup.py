"""会话 v2：岗位多选、资料关联、单场约束、draft -> ready -> active -> stopped。

状态兼容：core.py 的 Store.stop() 写 'ended'，本模块写 'stopped'；二者等价，读取时统一映射为 'stopped'
（session_view 的 status 字段）。唯一索引 one_active 仍只约束 status='active'，同一时刻最多一个。
"""
import json
import time
import uuid

from .profiles import ProfileError, get_profile
from ..library.versioning import freeze_session

ROLES = ('技术开发', '产品经理', '运营', '管理')
MODES = ('online', 'offline')
OUTBOUND_KEYS = ('asr_audio', 'llm_snippets', 'web_questions', 'asr_answerer')

# 对 sessions 表的幂等扩展（不改 Store.__init__）
NEW_COLUMNS = (
    ('profile_id', 'TEXT'),
    ('roles_json', 'TEXT'),
    ('selfcheck_json', 'TEXT'),
    ('config_snapshot_json', 'TEXT'),
    ('degraded_banner', 'TEXT'),
    ('created_at', 'REAL'),
    ('started_at', 'REAL'),
    ('stopped_at', 'REAL'),
    ('capture_started_at', 'REAL'),
    ('event_seq', 'INTEGER DEFAULT 0'),
)


class SetupError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def ensure_sessions_columns(store):
    with store.lock, store.connect() as db:
        have = {r['name'] for r in db.execute("PRAGMA table_info(sessions)")}
        for name, typ in NEW_COLUMNS:
            if name not in have:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {typ}")


def canonical_status(status):
    return 'stopped' if status == 'ended' else status


def role_options(extra=()):
    return list(dict.fromkeys([*ROLES, *extra]))


def library_options(store):
    """可选主库：启用(approved)条数>0 才可选，0 条置灰。"""
    return [dict(id=x['id'], name=x['name'], total=x['total'], approved=x['approved'] or 0,
                 selectable=(x['approved'] or 0) > 0) for x in store.libraries()]


def session_view(store, sid):
    ensure_sessions_columns(store)
    with store.connect() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise SetupError(404, '会话不存在')
    d = dict(row)
    d['status'] = canonical_status(d['status'])
    for src, dst in (('roles_json', 'roles'), ('selfcheck_json', 'selfcheck'), ('config_snapshot_json', 'config')):
        d[dst] = json.loads(d.pop(src)) if d.get(src) else None
        d.pop(src, None)
    return d


def _clean_roles(roles, allowed):
    roles = list(dict.fromkeys(roles or []))
    if not roles:
        raise SetupError(400, '请至少选择一个目标岗位')
    bad = [r for r in roles if r not in allowed]
    if bad:
        raise SetupError(400, f'未知岗位：{bad[0]}')
    return roles


def _approved(store, library_ids):
    by_id = {x['id']: x for x in library_options(store)}
    for lib in library_ids:
        if lib not in by_id:
            raise SetupError(404, f'主库不存在：{lib}')
        if not by_id[lib]['selectable']:
            raise SetupError(400, f'主库「{by_id[lib]["name"]}」启用 0 条，不可选择，请先审核')
    return by_id


def _active_id(db):
    row = db.execute("SELECT id FROM sessions WHERE status='active'").fetchone()
    return row['id'] if row else None


def _clean_outbound(outbound):
    """本场外发覆盖：只允许关闭已知开关（比全局更严格），返回 {key: False}。"""
    if not outbound:
        return {}
    if not isinstance(outbound, dict):
        raise SetupError(400, '外发覆盖必须是对象')
    out = {}
    for key, value in outbound.items():
        if key not in OUTBOUND_KEYS:
            raise SetupError(400, f'未知外发开关：{key}')
        if not isinstance(value, bool):
            raise SetupError(400, f'外发开关 {key} 必须是布尔值')
        if value:
            raise SetupError(400, '本场只能关闭外发，不能开启全局已关闭的外发')
        out[key] = False
    return out


def create_session_v2(store, mode, profile_id, roles, library_ids=(), consent=False, extra_roles=(), secondary_only=False,
                      now=time.time, outbound=None):
    ensure_sessions_columns(store)
    if mode not in MODES:
        raise SetupError(400, '收音模式无效')
    if not consent:
        raise SetupError(400, '请先确认知情同意')
    try:
        profile = get_profile(store, profile_id)
    except ProfileError as exc:
        raise SetupError(exc.status, exc.message) from exc
    roles = _clean_roles(roles, role_options(extra_roles))
    library_ids = list(dict.fromkeys(library_ids or []))
    names = {k: v['name'] for k, v in _approved(store, library_ids).items() if k in library_ids}
    sid = uuid.uuid4().hex
    snapshot = dict(mode=mode, profile=dict(id=profile['id'], alias=profile['alias']), roles=roles,
                    library_ids=library_ids, libraries=names, consent=True, secondary_only=bool(secondary_only))
    override = _clean_outbound(outbound)
    if override:
        snapshot['outbound'] = override  # 启动冻结时并入全局 outbound（只能关闭）
    with store.lock, store.connect() as db:
        if _active_id(db):
            raise SetupError(409, '已有进行中的会话，请先结束后再创建')
        db.execute(
            "INSERT INTO sessions(id,mode,status,library_id,turn_id,profile_id,roles_json,config_snapshot_json,created_at,event_seq) "
            "VALUES(?,?,?,?,NULL,?,?,?,?,0)",
            (sid, mode, 'draft', library_ids[0] if library_ids else '', profile_id,
             json.dumps(roles, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), now()))
    return session_view(store, sid)


def update_session_config(store, sid, profile_id=None, roles=None, extra_roles=()):
    """仅 draft/ready 可改资料与岗位；面试中（active）不允许。"""
    s = session_view(store, sid)
    if s['status'] == 'active':
        raise SetupError(409, '面试进行中，不能切换回答者资料和岗位')
    if s['status'] == 'stopped':
        raise SetupError(409, '会话已结束')
    cfg = s['config']
    if profile_id is not None:
        try:
            p = get_profile(store, profile_id)
        except ProfileError as exc:
            raise SetupError(exc.status, exc.message) from exc
        cfg['profile'] = dict(id=p['id'], alias=p['alias'])
    else:
        profile_id = s['profile_id']
    if roles is not None:
        cfg['roles'] = _clean_roles(roles, role_options(extra_roles))
    with store.lock, store.connect() as db:
        db.execute("UPDATE sessions SET profile_id=?,roles_json=?,config_snapshot_json=?,status='draft',selfcheck_json=NULL WHERE id=?",
                   (profile_id, json.dumps(cfg['roles'], ensure_ascii=False), json.dumps(cfg, ensure_ascii=False), sid))
    return session_view(store, sid)


def approved_total(store, sid):
    cfg = session_view(store, sid)['config']
    by_id = {x['id']: x for x in library_options(store)}
    return sum(by_id[i]['approved'] for i in cfg['library_ids'] if i in by_id)


def mark_ready(store, sid, report):
    """自检完成：draft/ready -> ready，并保存自检报告。"""
    s = session_view(store, sid)
    if s['status'] not in ('draft', 'ready'):
        raise SetupError(409, f'当前状态 {s["status"]} 不能自检')
    with store.lock, store.connect() as db:
        db.execute("UPDATE sessions SET status='ready',selfcheck_json=? WHERE id=?",
                   (json.dumps(report, ensure_ascii=False), sid))
    return session_view(store, sid)


def start_session(store, sid, degraded=False, now=time.time):
    """ready -> active。自检有 FAIL 时必须显式 degraded=True（降级开始），否则拒绝；FAIL 不能静默开始。"""
    s = session_view(store, sid)
    if s['status'] != 'ready':
        raise SetupError(409, '请先完成会前自检' if s['status'] == 'draft' else f'当前状态 {s["status"]} 不能开始')
    report = s['selfcheck']
    banner = None
    if report['has_fail']:
        if not degraded:
            raise SetupError(409, '自检有失败项，需确认“降级开始”')
        banner = report['degraded_banner']
    with store.lock, store.connect() as db:
        if _active_id(db):
            raise SetupError(409, '已有进行中的会话，请先结束后再开始')
        db.execute("UPDATE sessions SET status='active',started_at=?,degraded_banner=? WHERE id=?", (now(), banner, sid))
    library_ids = (s.get('config') or {}).get('library_ids') or ([s['library_id']] if s.get('library_id') else [])
    for library_id in library_ids:  # 开会即固定本场题库版本；会后编辑/重导入不影响本场
        freeze_session(store, sid, library_id)
    return session_view(store, sid)


def stop_session(store, sid, now=time.time):
    s = session_view(store, sid)
    if s['status'] == 'stopped':
        raise SetupError(409, '会话已结束')
    with store.lock, store.connect() as db:
        db.execute("UPDATE sessions SET status='stopped',turn_id=NULL,stopped_at=? WHERE id=?", (now(), sid))
    return session_view(store, sid)
