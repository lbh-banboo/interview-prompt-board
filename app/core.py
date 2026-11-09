"""Local-only preparation and deterministic turn protocol; no automatic ASR claims."""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .domain.outline import outline
from .library.parser import ENTRY, parse_markdown  # noqa: F401  (re-exported)
from .logging_safe import PATTERNS
from .retrieval.ngram import grams, normalize

log = logging.getLogger(__name__)


def redact_keys(text):
    """只脱 Key 样式字符串（sk-/Bearer/key=value），不动手机号邮箱等正文。"""
    for rx, repl in PATTERNS[:3]:
        text = rx.sub(repl, text)
    return text


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:  # 启动即强制数据目录仅本人可读写；失败只告警，不阻断启动
            os.chmod(self.root, 0o700)
        except OSError as exc:
            log.warning("无法将数据目录权限设为 0o700：%s（%s）", self.root, exc)
        self.path = self.root / "interview.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS libraries(id TEXT PRIMARY KEY, name TEXT, hash TEXT UNIQUE, source TEXT);
            CREATE TABLE IF NOT EXISTS entries(library_id TEXT, id TEXT, payload TEXT, status TEXT DEFAULT 'pending', approved_answer TEXT DEFAULT '', PRIMARY KEY(library_id,id));
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, mode TEXT, status TEXT, library_id TEXT, turn_id TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active ON sessions(status) WHERE status='active';
            CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, session_id TEXT, question TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_id TEXT, kind TEXT, payload TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS recordings(id TEXT PRIMARY KEY, session_id TEXT, source TEXT, mime TEXT, sequence INTEGER, path TEXT, UNIQUE(session_id,source,sequence));
            CREATE TABLE IF NOT EXISTS audio_streams(id TEXT PRIMARY KEY, session_id TEXT, source TEXT, stream_id INTEGER, capture_generation INTEGER, frames INTEGER);
            CREATE TABLE IF NOT EXISTS recording_gaps(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, source TEXT, capture_generation INTEGER, t_start_ms INTEGER, t_end_ms INTEGER, reason TEXT);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def import_text(self, name, text):
        entries = parse_markdown(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.lock, self.connect() as db:
            old = db.execute("SELECT id FROM libraries WHERE hash=?", (digest,)).fetchone()
            if old:
                return dict(id=old['id'], count=len(entries), reused=True)
            key = uuid.uuid4().hex
            db.execute("INSERT INTO libraries VALUES(?,?,?,?)", (key, name, digest, text))
            db.executemany("INSERT INTO entries(library_id,id,payload) VALUES(?,?,?)",
                           [(key, e['id'], json.dumps(e, ensure_ascii=False)) for e in entries])
        return dict(id=key, count=len(entries), reused=False)

    def libraries(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT l.id,l.name,COUNT(e.id) total,SUM(e.status='approved') approved FROM libraries l LEFT JOIN entries e ON l.id=e.library_id GROUP BY l.id")]

    def entries(self, library):
        with self.connect() as db:
            return [dict(**json.loads(x['payload']), status=x['status'], approved_answer=x['approved_answer'])
                    for x in db.execute("SELECT * FROM entries WHERE library_id=? ORDER BY rowid", (library,))]

    def review(self, library, key, answer, status, confirmed):
        if status not in ('pending', 'approved', 'rejected'):
            raise ValueError("审核状态无效")
        if status == 'approved' and (not confirmed or not answer.strip()):
            raise ValueError("启用前必须确认事实与保留的待核提醒，并提供审核答案")
        with self.lock, self.connect() as db:
            if db.execute("SELECT 1 FROM sessions WHERE status='active' AND library_id=?", (library,)).fetchone():
                raise ValueError("本场题库已冻结，结束会话后再修改")
            cur = db.execute("UPDATE entries SET status=?,approved_answer=? WHERE library_id=? AND id=?", (status, answer, library, key))
            if not cur.rowcount:
                raise ValueError("题目不存在")

    def search(self, library, query):
        start = time.perf_counter()
        q = grams(query)
        ranked = []
        for e in self.entries(library):
            if e['status'] != 'approved':
                continue
            t = grams(e['question'])
            score = 1.0 if normalize(query) == normalize(e['question']) else len(q & t)/max(1, len(q | t))
            if score >= .28:
                ranked.append(dict(id=e['id'], question=e['question'], score=round(score, 3),
                                   outline=outline(e['approved_answer']), flagged=e['flagged']))
        ranked.sort(key=lambda e: e['score'], reverse=True)
        return dict(results=ranked[:5], elapsed_ms=round((time.perf_counter()-start)*1000, 2),
                    method="本地字符二元组候选检索，非语义模型；匹配正确性未验收")

    def start(self, mode, library):
        if mode not in ('online', 'offline'):
            raise ValueError("收音模式无效")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM entries WHERE library_id=? AND status='approved'", (library,)).fetchone():
                raise ValueError("本场题库尚无已审核条目")
            key = uuid.uuid4().hex
            try:
                db.execute("INSERT INTO sessions(id,mode,status,library_id,turn_id) VALUES(?,?,'active',?,NULL)", (key, mode, library))
            except sqlite3.IntegrityError as exc:
                raise ValueError("已有运行中的会话，请先结束") from exc
        return self.session(key)

    def session(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("会话不存在")
            result = dict(row)
            result['turns'] = [dict(x) for x in db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (key,))]
            return result

    def event(self, session, kind, question='', turn_id=None, content=''):
        # Adapter/testing protocol, not a manual role-correction UI.
        with self.lock, self.connect() as db:
            s = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
            if not s:
                raise ValueError("会话不存在")
            if s['status'] != 'active' and kind not in ('supplement', 'follow_up'):
                raise ValueError("会话未运行")
            current = s['turn_id']
            target = current
            disposition = 'visible'
            if kind in ('question_complete', 'change_topic'):
                if not question.strip():
                    raise ValueError("问题不能为空")
                target = uuid.uuid4().hex
                if kind == 'change_topic' and current:
                    db.execute("UPDATE turns SET status='interrupted' WHERE id=?", (current,))
                queued = bool(current and kind == 'question_complete')
                db.execute("INSERT INTO turns VALUES(?,?,?,?)", (target, session, question, 'queued' if queued else 'active'))
                if not queued:
                    db.execute("UPDATE sessions SET turn_id=? WHERE id=?", (target, session))
                else:
                    disposition = 'queued'
            elif kind == 'answer_complete':
                if turn_id != current or not current:
                    raise ValueError("回答结束事件不属于当前题")
                db.execute("UPDATE turns SET status='completed' WHERE id=?", (current,))
                following = db.execute("SELECT id FROM turns WHERE session_id=? AND status='queued' ORDER BY rowid LIMIT 1", (session,)).fetchone()
                next_id = following['id'] if following else None
                if next_id:
                    db.execute("UPDATE turns SET status='active' WHERE id=?", (next_id,))
                db.execute("UPDATE sessions SET turn_id=? WHERE id=?", (next_id, session))
            elif kind in ('supplement', 'follow_up'):
                row = db.execute("SELECT * FROM turns WHERE id=? AND session_id=?", (turn_id, session)).fetchone()
                if not row:
                    raise ValueError("事件引用未知轮次")
                target = turn_id
                if turn_id != current or row['status'] != 'active':
                    disposition = 'archived'
            elif kind not in ('backchannel', 'uncertain', 'transcript', 'source_lost'):
                raise ValueError("不支持的事件")
            payload = dict(question=question, content=content, disposition=disposition, mode='adapter_protocol_not_asr')
            db.execute("INSERT INTO events(session_id,turn_id,kind,payload,created) VALUES(?,?,?,?,?)", (session, target, kind, json.dumps(payload, ensure_ascii=False), time.time()))
        return dict(turn_id=target, disposition=disposition, state=self.session(session))

    def stop(self, key):
        """结束会话：status='ended'（读取侧统一映射为 stopped）；stopped_at 只写首次结束时刻。

        create_app 会先执行 ensure_sessions_columns；裸 Store（测试/旧库）尚无该列时只结束状态，
        不影响原有语义。"""
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone():
                raise ValueError("会话不存在")
            columns = {r['name'] for r in db.execute("PRAGMA table_info(sessions)")}
            if 'stopped_at' in columns:
                db.execute("UPDATE sessions SET status='ended',turn_id=NULL,stopped_at=COALESCE(stopped_at,?) "
                           "WHERE id=?", (time.time(), key))
            else:
                db.execute("UPDATE sessions SET status='ended',turn_id=NULL WHERE id=?", (key,))
            db.execute("UPDATE turns SET status='closed' WHERE session_id=? AND status IN ('active','queued')", (key,))
        return self.session(key)

    def export(self, key):
        s = self.session(key)
        lines = ['# 面试记录', '', f"模式：{s['mode']}；状态：{s['status']}", '', '## 现场实际发言', '实时 ASR 未接入；没有自动逐字稿。', '', '## 系统建议与协议测试事件（不等于实际发言）']
        with self.connect() as db:
            for event in db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id", (key,)):
                p = json.loads(event['payload'])
                lines.append(f"- {event['kind']} / {event['turn_id']} / {p['disposition']}：{p['content'] or p['question']}")
            lines += ['', '## 录音分段']
            for r in db.execute("SELECT * FROM recordings WHERE session_id=? ORDER BY source,sequence", (key,)):
                lines.append(f"- {r['source']} 第{r['sequence']}段：/api/recordings/{r['id']}")
        lines += ['', '## 验证状态', '本版录音与本地题库可验证；声纹、自动轮次、模型、联网与实时性能尚未验收。']
        return redact_keys('\n'.join(lines))
