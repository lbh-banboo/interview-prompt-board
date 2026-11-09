"""导入版本、条目内容哈希、重新导入时的审核继承、开会时的版本快照。

新表由本模块 CREATE IF NOT EXISTS，与 Store 共用同一个 sqlite 文件；Store 的 libraries/entries 保存“当前版本”的实时状态。
"""
import hashlib
import json
import re
import sqlite3
import time
import unicodedata

from .conflicts import detect_conflicts
from .facts_extract import extract_rule_facts
from .parser import parse_markdown

DDL = """
CREATE TABLE IF NOT EXISTS library_sources(library_id TEXT, version INTEGER, file_name TEXT, raw_text TEXT, created REAL,
    PRIMARY KEY(library_id,version));
CREATE TRIGGER IF NOT EXISTS library_sources_no_update BEFORE UPDATE ON library_sources
    BEGIN SELECT RAISE(ABORT,'library_sources 只读'); END;
CREATE TRIGGER IF NOT EXISTS library_sources_no_delete BEFORE DELETE ON library_sources
    BEGIN SELECT RAISE(ABORT,'library_sources 只读'); END;
CREATE TABLE IF NOT EXISTS library_versions(library_id TEXT, version INTEGER, source_hash TEXT, entry_count INTEGER, created REAL,
    PRIMARY KEY(library_id,version));
CREATE TABLE IF NOT EXISTS entry_versions(library_id TEXT, version INTEGER, entry_key TEXT, content_hash TEXT,
    line_start INTEGER, line_end INTEGER, status TEXT, approved_answer TEXT DEFAULT '',
    enhanced INTEGER DEFAULT 0, enhance_json TEXT DEFAULT '', enhance_error TEXT DEFAULT '',
    flags_json TEXT DEFAULT '[]', inherited_from INTEGER,
    PRIMARY KEY(library_id,version,entry_key));
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY AUTOINCREMENT, library_id TEXT, version INTEGER, entry_key TEXT,
    entry_content_hash TEXT, kind TEXT, text TEXT, source_span TEXT, line_start INTEGER, line_end INTEGER,
    flag TEXT, origin TEXT, conflict_group TEXT);
CREATE INDEX IF NOT EXISTS facts_entry ON facts(library_id,version,entry_key);
CREATE TABLE IF NOT EXISTS session_library_versions(session_id TEXT, library_id TEXT, library_version INTEGER,
    approved_entry_hashes_json TEXT, index_version TEXT DEFAULT '', PRIMARY KEY(session_id,library_id));
"""


class _Conn(sqlite3.Connection):
    def __exit__(self, *exc):  # with 块结束时提交并关闭
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect(store):
    db = sqlite3.connect(store.path, timeout=10, factory=_Conn)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    return db


def content_hash(text):
    return hashlib.sha256(re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().encode()).hexdigest()


def current_version(db, library_id):
    row = db.execute("SELECT MAX(version) v FROM library_versions WHERE library_id=?", (library_id,)).fetchone()
    return row['v']


def _live(db, library_id):
    return {r['id']: dict(status=r['status'], approved_answer=r['approved_answer']) for r in
            db.execute("SELECT id,status,approved_answer FROM entries WHERE library_id=?", (library_id,))}


def _add_version(db, library_id, version, raw, file_name, items, clock, states, previous=None):
    """写入新版本；previous 为 {content_hash: 上一版本行}，命中的条目继承状态与增强结果。"""
    previous = previous or {}
    db.execute("INSERT INTO library_sources VALUES(?,?,?,?,?)", (library_id, version, file_name, raw, clock()))
    db.execute("INSERT INTO library_versions VALUES(?,?,?,?,?)",
               (library_id, version, hashlib.sha256(raw.encode()).hexdigest(), len(items), clock()))
    inherited = 0
    for item in items:
        digest = content_hash(item['text'])
        old = previous.get(digest)
        inherited += bool(old)
        state = dict(status=old['status'], approved_answer=old['approved_answer']) if old \
            else states.get(item['id'], dict(status='pending', approved_answer=''))
        states[item['id']] = state
        db.execute("INSERT INTO entry_versions(library_id,version,entry_key,content_hash,line_start,line_end,status,"
                   "approved_answer,enhanced,enhance_json,enhance_error,flags_json,inherited_from) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (library_id, version, item['id'], digest, item['line_start'], item['line_end'],
                    state['status'], state['approved_answer'],
                    old['enhanced'] if old else 0, old['enhance_json'] if old else '',
                    old['enhance_error'] if old else '', old['flags_json'] if old else '[]',
                    old['version'] if old else None))
        if old:
            db.execute("INSERT INTO facts(library_id,version,entry_key,entry_content_hash,kind,text,source_span,line_start,"
                       "line_end,flag,origin) SELECT library_id,?,?,entry_content_hash,kind,text,source_span,line_start,"
                       "line_end,flag,origin FROM facts WHERE library_id=? AND version=? AND entry_key=?",
                       (version, item['id'], library_id, old['version'], old['entry_key']))
        else:
            for f in extract_rule_facts(item):
                db.execute("INSERT INTO facts(library_id,version,entry_key,entry_content_hash,kind,text,source_span,"
                           "line_start,line_end,flag,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (library_id, version, item['id'], digest, f['kind'], f['text'], f['source_span'],
                            f['line_start'], f['line_end'], f['flag'], f['origin']))
    detect_conflicts(db, library_id, version)
    return inherited


def ensure_version(store, library_id, clock=time.time):
    """为尚无版本记录的库补建版本 1（取 libraries.source 与实时审核状态）；返回当前版本号。"""
    with store.lock, connect(store) as db:
        version = current_version(db, library_id)
        if version:
            return version
        row = db.execute("SELECT name,source FROM libraries WHERE id=?", (library_id,)).fetchone()
        if not row:
            raise ValueError("题库不存在")
        _add_version(db, library_id, 1, row['source'], row['name'], parse_markdown(row['source']), clock, _live(db, library_id))
        return 1


def reimport(store, library_id, text, file_name=None, clock=time.time):
    """重新导入：按条目内容哈希继承审核状态与已确认答案，变化/新增条目回到 pending。"""
    items = parse_markdown(text)
    ensure_version(store, library_id, clock)
    digest = hashlib.sha256(text.encode()).hexdigest()
    with store.lock, connect(store) as db:
        if db.execute("SELECT 1 FROM sessions WHERE status='active' AND library_id=?", (library_id,)).fetchone():
            raise ValueError("本场题库已冻结，结束会话后再修改")
        lib = db.execute("SELECT name,hash FROM libraries WHERE id=?", (library_id,)).fetchone()
        prev = current_version(db, library_id)
        if lib['hash'] == digest:
            return dict(version=prev, changed=False, inherited=0, pending=[])
        if db.execute("SELECT 1 FROM libraries WHERE hash=? AND id<>?", (digest, library_id)).fetchone():
            raise ValueError("该内容已作为另一个题库导入")
        live = _live(db, library_id)
        for key, state in live.items():  # 把上一版本的收尾状态存档
            db.execute("UPDATE entry_versions SET status=?,approved_answer=? WHERE library_id=? AND version=? AND entry_key=?",
                       (state['status'], state['approved_answer'], library_id, prev, key))
        previous = {r['content_hash']: dict(r) for r in db.execute(
            "SELECT * FROM entry_versions WHERE library_id=? AND version=?", (library_id, prev))}
        states = {}
        version = prev + 1
        inherited = _add_version(db, library_id, version, text, file_name or lib['name'], items, clock, states, previous)
        keys = [i['id'] for i in items]
        db.execute(f"DELETE FROM entries WHERE library_id=? AND id NOT IN ({','.join('?' * len(keys))})", (library_id, *keys))
        for item in items:
            state = states[item['id']]
            db.execute("INSERT INTO entries(library_id,id,payload,status,approved_answer) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(library_id,id) DO UPDATE SET payload=excluded.payload,status=excluded.status,"
                       "approved_answer=excluded.approved_answer",
                       (library_id, item['id'], json.dumps(item, ensure_ascii=False), state['status'], state['approved_answer']))
        db.execute("UPDATE libraries SET hash=?,source=? WHERE id=?", (digest, text, library_id))
        pending = [i['id'] for i in items if states[i['id']]['status'] == 'pending']
        return dict(version=version, changed=True, inherited=inherited, pending=pending)


def freeze_session(store, session_id, library_id, index_version=""):
    """开会时固定该库版本与已启用条目的内容哈希；会后编辑主库不影响旧会话复现。"""
    version = ensure_version(store, library_id)
    with store.lock, connect(store) as db:
        approved = {r['entry_key']: r['content_hash'] for r in db.execute(
            "SELECT v.entry_key,v.content_hash FROM entry_versions v JOIN entries e ON e.library_id=v.library_id AND e.id=v.entry_key "
            "WHERE v.library_id=? AND v.version=? AND e.status='approved'", (library_id, version))}
        db.execute("INSERT OR REPLACE INTO session_library_versions VALUES(?,?,?,?,?)",
                   (session_id, library_id, version, json.dumps(approved, ensure_ascii=False), index_version))
    return dict(library_version=version, approved_entry_hashes=approved)


def session_snapshot(store, session_id, library_id):
    with connect(store) as db:
        row = db.execute("SELECT * FROM session_library_versions WHERE session_id=? AND library_id=?",
                         (session_id, library_id)).fetchone()
        if not row:
            raise ValueError("会话未固定题库版本")
        return dict(library_version=row['library_version'], index_version=row['index_version'],
                    approved_entry_hashes=json.loads(row['approved_entry_hashes_json']))
