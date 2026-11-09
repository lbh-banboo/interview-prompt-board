"""M12-1 记录存储：segments / turns / turn_events / prompts（自建表，不改 Store.__init__）。

sessions / recordings / recording_gaps / metrics 位于主库（ctx.store），这里只读。
时间戳统一为浮点秒（board 事件取 t_server）。
"""
import hashlib
import json
import sqlite3
import threading
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, source TEXT, role TEXT, text TEXT,
  is_final INTEGER, t_start REAL, t_end REAL, confidence REAL, flags TEXT DEFAULT '',
  edited_text TEXT, orig_text TEXT, edited_role TEXT);
CREATE INDEX IF NOT EXISTS seg_session ON segments(session_id, t_start);
CREATE TABLE IF NOT EXISTS segment_edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, segment_id INTEGER, field TEXT,
  old TEXT, new TEXT, edited_at REAL);
CREATE TABLE IF NOT EXISTS turns(
  id TEXT, session_id TEXT, epoch INTEGER DEFAULT 0, parent_id TEXT, kind TEXT DEFAULT '',
  text TEXT DEFAULT '', text_version INTEGER DEFAULT 0, state TEXT DEFAULT '', level TEXT DEFAULT '',
  t_end REAL, t_candidate REAL, t_committed REAL, t_visible REAL, closed_reason TEXT,
  rolled_back_count INTEGER DEFAULT 0, PRIMARY KEY(session_id, id));
CREATE TABLE IF NOT EXISTS turn_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, event_seq INTEGER, turn_id TEXT, epoch INTEGER,
  type TEXT, payload_json TEXT, t_ns INTEGER);
CREATE TABLE IF NOT EXISTS prompts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_id TEXT, epoch INTEGER, text_version INTEGER,
  block TEXT, level TEXT, source_tag TEXT, source_entry_id TEXT, content_json TEXT, source_urls_json TEXT,
  created_at REAL, state TEXT, sent_at REAL, display_ack_at REAL, idempotency_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS ingested_events(session_id TEXT, event_seq INTEGER, PRIMARY KEY(session_id, event_seq));
CREATE TABLE IF NOT EXISTS deleted_sessions(session_id TEXT PRIMARY KEY, trash_path TEXT, deleted_at REAL);
'''

# board turn_state.state -> turn_events.type
STATE_TYPES = {'question_committed': 'committed', 'closed': 'closed', 'queued': 'queued'}


class RecordStore:
    def __init__(self, db, main=None, clock=time.time):
        """db: 记录库路径或连接；main: 主库路径 / 返回连接的可调用（只读 sessions/recordings/gaps/metrics）。"""
        self.conn = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(str(db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.main = main
        self.clock = clock
        self.lock = threading.RLock()
        self._pending_acks = {}  # (session, prompt_id) -> display_ack_at：ACK 可能早于事件入档

    # ---- 查询 ----
    def main_rows(self, sql, args=()):
        if self.main is None:
            return []
        conn = self.main() if callable(self.main) else sqlite3.connect(str(self.main))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError:  # 主库没有该表
            return []
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    # ---- 逐字稿 ----
    def append_segment(self, session, source, role, text, is_final, t_start=None, t_end=None,
                       confidence=None, flags=()):
        """只有 final 定稿入库；partial 不入库，返回 None。flags: overlap/unknown/backchannel。"""
        if not is_final:
            return None
        flags = flags.split(',') if isinstance(flags, str) else flags
        with self.lock:
            cur = self.conn.execute(
                'INSERT INTO segments(session_id,source,role,text,is_final,t_start,t_end,confidence,flags,orig_text)'
                ' VALUES(?,?,?,?,1,?,?,?,?,?)',
                (session, source, role, text, t_start, t_end, confidence, ','.join(f for f in flags if f), text))
            self.conn.commit()
            return cur.lastrowid

    # ---- 轮次 ----
    def record_turn_event(self, session, type_, turn_id, epoch=0, payload=None, t_ns=None, event_seq=None,
                          t=None, text=None, text_version=None, kind=None, reason=None):
        """追加 turn_events，并按类型更新 turns。t 为秒。"""
        t = self.clock() if t is None else t
        sets, args = ['epoch=?'], [epoch]
        for col, val in (('text', text), ('text_version', text_version), ('kind', kind or None)):
            if val is not None:
                sets.append(f'{col}=?')
                args.append(val)
        if type_ == 'candidate':
            sets += ['t_candidate=?', "state='candidate'"]
            args.append(t)
        elif type_ == 'withdrawn':
            sets.append("state='withdrawn'")
        elif type_ == 'committed':
            sets += ['t_committed=?', "state='committed'"]
            args.append(t)
        elif type_ == 'rolled_back':
            sets += ["state='rolled_back'", 'rolled_back_count=rolled_back_count+1']
        elif type_ == 'interrupted':
            sets.append("state='interrupted'")
        elif type_ == 'closed':
            sets += ["state='closed'", 'closed_reason=?']
            args.append(reason)
        elif type_ == 'queued':
            sets.append("state='queued'")
        with self.lock:
            self.conn.execute('INSERT INTO turn_events(session_id,event_seq,turn_id,epoch,type,payload_json,t_ns)'
                              ' VALUES(?,?,?,?,?,?,?)',
                              (session, event_seq, turn_id, epoch, type_,
                               json.dumps(payload or {}, ensure_ascii=False), t_ns))
            self.conn.execute('INSERT OR IGNORE INTO turns(id,session_id) VALUES(?,?)', (turn_id, session))
            self.conn.execute(f'UPDATE turns SET {",".join(sets)} WHERE session_id=? AND id=?',
                              (*args, session, turn_id))
            self.conn.commit()

    # ---- 系统提示 ----
    def record_prompt(self, session, turn_id, epoch, block, content, key=None, source_tag='', source_urls=(),
                      level='', text_version=0, state='sent', source_entry_id=None, reason=''):
        """idempotency_key 唯一：重复写入返回 False（补发与重试不重复入档）。
        reason 纳入幂等键：同轮同 epoch 的迟到归档按“原因”分别留档，不互相压掉。"""
        key = key or hashlib.sha256(json.dumps([session, turn_id, epoch, block, reason, content], sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()
        now = self.clock()
        with self.lock:
            cur = self.conn.execute(
                'INSERT OR IGNORE INTO prompts(session_id,turn_id,epoch,text_version,block,level,source_tag,'
                'source_entry_id,content_json,source_urls_json,created_at,state,sent_at,idempotency_key)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (session, turn_id, epoch, text_version, block, level, source_tag, source_entry_id,
                 json.dumps(content, ensure_ascii=False), json.dumps(list(source_urls), ensure_ascii=False),
                 now, state, now if state in ('sent', 'acked') else None, key))
            if cur.rowcount and level:
                self.conn.execute('UPDATE turns SET level=? WHERE session_id=? AND id=?', (level, session, turn_id))
            self.conn.commit()
            return cur.rowcount == 1

    def mark_ack(self, session, prompt_id, t_visible=None):
        """浏览器 ACK：prompt_id = p{event_seq}，与 ingest 时的 idempotency_key（session:event_seq）对应。
        未入档 / 迟到归档的提示不改状态。t_visible 是浏览器 epoch 毫秒，落库统一为秒。"""
        if not isinstance(prompt_id, str) or len(prompt_id) < 2 or prompt_id[0] != 'p' or not prompt_id[1:].isdigit():
            return False
        at = t_visible / 1000 if isinstance(t_visible, (int, float)) else self.clock()
        with self.lock:
            cur = self.conn.execute("UPDATE prompts SET state='acked', display_ack_at=? "
                                    "WHERE session_id=? AND idempotency_key=? AND state IN ('sent', 'acked')",
                                    (at, session, f'{session}:{prompt_id[1:]}'))
            if cur.rowcount:
                self._pending_acks.pop((session, prompt_id), None)
            else:
                self._pending_acks[(session, prompt_id)] = at  # 等 ingest 入档后补写
            self.conn.commit()
            return cur.rowcount == 1

    def mark_late_archived(self, key):
        with self.lock:
            cur = self.conn.execute("UPDATE prompts SET state='late_archived' WHERE idempotency_key=?", (key,))
            self.conn.commit()
            return cur.rowcount == 1

    # ---- board 事件适配 ----
    def ingest_board_events(self, events):
        """events: EventBus 事件字典列表（type/session/turn_id/epoch/event_seq/text_version/t_server/payload）。
        按 (session, event_seq) 去重，可重复喂入同一段 replay。prompt_* 的 payload.late=True 入档为迟到。"""
        for ev in events:
            session, seq = ev['session'], ev.get('event_seq')
            if seq is not None:
                with self.lock:
                    cur = self.conn.execute('INSERT OR IGNORE INTO ingested_events VALUES(?,?)', (session, seq))
                    self.conn.commit()
                if cur.rowcount == 0:
                    continue
            self._ingest(ev, session, seq)

    def _ingest(self, ev, session, seq):
        t, p = ev['type'], ev.get('payload') or {}
        turn, tv, ts = ev.get('turn_id'), ev.get('text_version') or 0, ev.get('t_server')
        common = dict(epoch=ev.get('epoch') or 0, event_seq=seq, t=ts, payload=p, text_version=tv)
        if t == 'candidate':
            self.record_turn_event(session, 'withdrawn' if p.get('withdrawn') else 'candidate', turn,
                                   text=p.get('text'), kind=p.get('kind'), **common)
        elif t == 'turn_state' and p.get('state') in STATE_TYPES:
            self.record_turn_event(session, STATE_TYPES[p['state']], turn, reason=p.get('reason'), **common)
        elif t == 'turn_rollback':
            self.record_turn_event(session, 'rolled_back', turn, **common)
        elif t == 'interrupt':
            self.record_turn_event(session, 'interrupted', turn, **common)
        elif t == 'transcript' and p.get('is_final'):
            self.append_segment(session, p.get('source', ''), p.get('role', 'unknown'), p.get('text', ''), True,
                                ts, ts, p.get('confidence'), p.get('flags') or ())
            if p.get('role') == 'questioner':
                self.record_turn_event(session, 'text_updated', turn, text=p.get('text'), kind=p.get('kind'),
                                       **common)
        elif t in ('prompt_primary', 'prompt_supplement', 'prompt_status'):
            primary = t == 'prompt_primary'
            block = 'primary' if primary else 'supplement' if t == 'prompt_supplement' else 'status'
            self.record_prompt(session, turn, common['epoch'], block,
                               p.get('outline') if primary else p.get('content') if block == 'supplement' else p.get('text'),
                               f'{session}:{seq}' if seq is not None else None, p.get('source_tag', ''),
                               [p['url']] if p.get('url') else p.get('urls') or [],
                               p.get('level') or '', tv, 'late_archived' if p.get('late') else 'sent',
                               source_entry_id=p.get('source_entry_id'))
            at = self._pending_acks.pop((session, f'p{seq}'), None) if seq is not None else None
            if at is not None:  # ACK 早于事件入档：补写状态与可见时刻
                with self.lock:
                    self.conn.execute("UPDATE prompts SET state='acked', display_ack_at=? "
                                      "WHERE session_id=? AND idempotency_key=?",
                                      (at, session, f'{session}:{seq}'))
                    self.conn.commit()

    def attach(self, bus, session):
        """只读订阅：返回 pull()；主线周期调用，把 bus.replay 的新事件写入记录（bus 仅保留最近 keep 条，需及时拉）。"""
        cursor = [0]

        def pull():
            events = bus.replay(session, cursor[0])
            if events:
                cursor[0] = max(e['event_seq'] for e in events)
                self.ingest_board_events(events)
            return len(events)
        return pull
