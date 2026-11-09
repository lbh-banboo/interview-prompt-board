"""回答者资料 profiles：姓名代号、目标岗位、备注、自带资料内容；一份资料最多关联一个主库。

资料内容只在本地 SQLite 保存；带内容保存时解析成一份“该资料的主库”，新条目保持待审核。
更新内容走 versioning.reimport：未变条目继承审核状态，变更/新增条目回到待审核。
"""
import json
import time
import uuid

from ..library import conflicts, versioning
from ..library.parser import parse_markdown

MAX_CONTENT = 5_000_000


class ProfileError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def ensure_profiles(store):
    with store.lock, store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, alias TEXT, roles_json TEXT, note TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS profile_libraries(profile_id TEXT, library_id TEXT, PRIMARY KEY(profile_id, library_id));
        """)
        cols = {r['name'] for r in db.execute("PRAGMA table_info(profiles)")}
        for name, typ in (('content', "TEXT DEFAULT ''"), ('content_library_id', 'TEXT')):
            if name not in cols:
                db.execute(f"ALTER TABLE profiles ADD COLUMN {name} {typ}")


def _clean(alias, roles, note):
    alias = (alias or '').strip()
    if not alias:
        raise ProfileError(400, '姓名代号不能为空')
    roles = list(dict.fromkeys(r.strip() for r in roles or [] if r and r.strip()))
    return alias, roles, (note or '').strip()


def _content_error(text):
    text = text or ''
    if not text.strip():
        raise ProfileError(400, '请选择 .md 文件或粘贴文本')
    if len(text.encode('utf-8')) > MAX_CONTENT:
        raise ProfileError(400, '资料超过 5MB，请拆分后导入')
    return text


def _library_stats(db, library_id):
    if not library_id:
        return None
    lib = db.execute("SELECT name FROM libraries WHERE id=?", (library_id,)).fetchone()
    if not lib:
        return None
    counts = db.execute("SELECT COUNT(id) total, COALESCE(SUM(status='approved'),0) approved "
                        "FROM entries WHERE library_id=?", (library_id,)).fetchone()
    updated = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_versions'").fetchone():
        row = db.execute("SELECT MAX(created) c FROM library_versions WHERE library_id=?", (library_id,)).fetchone()
        updated = row['c'] if row else None
    return dict(id=library_id, name=lib['name'], total=counts['total'] or 0,
                approved=counts['approved'] or 0, updated_at=updated)


def _row(db, row, detail=False):
    libs = [x['library_id'] for x in db.execute(
        "SELECT library_id FROM profile_libraries WHERE profile_id=? ORDER BY library_id", (row['id'],))]
    data = dict(id=row['id'], alias=row['alias'], roles=json.loads(row['roles_json']), note=row['note'],
                created_at=row['created_at'], library_ids=libs,
                content_library_id=row['content_library_id'] or '', has_content=bool((row['content'] or '').strip()),
                library_stats=_library_stats(db, libs[0] if libs else None))
    if detail:
        data['content'] = row['content'] or ''
    return data


def _link(db, pid, library_ids):
    unique = list(dict.fromkeys(library_ids or []))
    if len(unique) > 1:
        raise ProfileError(400, '一份资料最多关联一个主库')
    db.execute("DELETE FROM profile_libraries WHERE profile_id=?", (pid,))
    for lib in unique:
        if not db.execute("SELECT 1 FROM libraries WHERE id=?", (lib,)).fetchone():
            raise ProfileError(404, f'主库不存在：{lib}')
        db.execute("INSERT INTO profile_libraries VALUES(?,?)", (pid, lib))


def _review_map(store, library_id):
    return {e['id']: e['content_hash'] for e in conflicts.review_entries(store, library_id)}


def set_profile_content(store, pid, text):
    """保存/更新资料内容：首个版本导入成主库，后续版本走 reimport 并回传统计。"""
    profile = get_profile(store, pid)
    text = _content_error(text)
    try:
        parse_markdown(text)
    except ValueError as exc:
        raise ProfileError(400, str(exc)) from exc
    library_id = profile['content_library_id']
    with store.connect() as db:
        existing = bool(library_id and db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone())
    try:
        if existing:
            before = _review_map(store, library_id)
            result = versioning.reimport(store, library_id, text, f'资料：{profile["alias"]}')
            after = _review_map(store, library_id)
            stats = dict(added=[k for k in after if k not in before],
                         changed=[k for k in after if k in before and before[k] != after[k]],
                         deleted=[k for k in before if k not in after],
                         inherited=result['inherited'])
            reused = False
        else:
            imported = store.import_text(f'资料：{profile["alias"]}', text)
            library_id = imported['id']
            versioning.ensure_version(store, library_id)
            stats = dict(added=[e['id'] for e in store.entries(library_id)], changed=[], deleted=[], inherited=0)
            reused = imported['reused']
    except ValueError as exc:
        raise ProfileError(400, str(exc)) from exc
    with store.lock, store.connect() as db:
        db.execute("UPDATE profiles SET content=?,content_library_id=? WHERE id=?", (text, library_id, pid))
        linked = db.execute("SELECT 1 FROM profile_libraries WHERE profile_id=?", (pid,)).fetchone()
        if not linked:
            db.execute("INSERT OR IGNORE INTO profile_libraries VALUES(?,?)", (pid, library_id))
    return dict(profile=get_profile(store, pid), library_id=library_id, stats=stats, reused=reused)


def create_profile(store, alias, roles=(), note='', library_ids=(), content='', now=time.time):
    ensure_profiles(store)
    alias, roles, note = _clean(alias, roles, note)
    if content.strip():
        _content_error(content)
        try:
            parse_markdown(content)
        except ValueError as exc:
            raise ProfileError(400, str(exc)) from exc
    pid = uuid.uuid4().hex
    with store.lock, store.connect() as db:
        db.execute("INSERT INTO profiles(id,alias,roles_json,note,created_at,content,content_library_id) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (pid, alias, json.dumps(roles, ensure_ascii=False), note, now(), '', ''))
        _link(db, pid, library_ids)
    if content.strip():
        return set_profile_content(store, pid, content)['profile']
    return get_profile(store, pid)


def get_profile(store, pid, detail=True):
    ensure_profiles(store)
    with store.connect() as db:
        row = db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ProfileError(404, '资料不存在')
        return _row(db, row, detail=detail)


def list_profiles(store):
    ensure_profiles(store)
    with store.connect() as db:
        return [_row(db, r) for r in db.execute("SELECT * FROM profiles ORDER BY created_at, id")]


def update_profile(store, pid, alias, roles=(), note='', library_ids=()):
    get_profile(store, pid)
    alias, roles, note = _clean(alias, roles, note)
    with store.lock, store.connect() as db:
        db.execute("UPDATE profiles SET alias=?,roles_json=?,note=? WHERE id=?",
                   (alias, json.dumps(roles, ensure_ascii=False), note, pid))
        _link(db, pid, library_ids)
    return get_profile(store, pid)


def delete_profile(store, pid):
    get_profile(store, pid)
    with store.lock, store.connect() as db:
        cols = {r['name'] for r in db.execute("PRAGMA table_info(sessions)")}
        if 'profile_id' in cols and db.execute(
                "SELECT 1 FROM sessions WHERE profile_id=? AND status='active'", (pid,)).fetchone():
            raise ProfileError(409, '该资料正在进行中的会话使用，不能删除')
        db.execute("DELETE FROM profile_libraries WHERE profile_id=?", (pid,))
        db.execute("DELETE FROM profiles WHERE id=?", (pid,))
