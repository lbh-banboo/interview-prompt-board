"""M12-4 复盘编辑与手动删除：现场数据（text/role）不动，修订另存并留痕；删除只移入回收目录。"""
import json
import secrets
import shutil
from pathlib import Path

from .export import recording_files


class ReviewError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def edit_segment(store, session, segment_id, text=None, role=None):
    """修正文本 / 角色。text -> edited_text（orig_text 始终保留），role -> edited_role；每次修改写 segment_edits。"""
    if text is None and role is None:
        raise ReviewError('需要 text 或 role')
    rows = store.rows('SELECT * FROM segments WHERE id=? AND session_id=?', (segment_id, session))
    if not rows:
        raise ReviewError('片段不存在', 404)
    seg = rows[0]
    now = store.clock()
    with store.lock:
        for field, new, col, old in (('text', text, 'edited_text', seg['edited_text'] if seg['edited_text'] is not None else seg['text']),
                                     ('role', role, 'edited_role', seg['edited_role'] or seg['role'])):
            if new is None:
                continue
            store.conn.execute(f'UPDATE segments SET {col}=? WHERE id=?', (new, segment_id))
            store.conn.execute('INSERT INTO segment_edits(session_id,segment_id,field,old,new,edited_at) VALUES(?,?,?,?,?,?)',
                               (session, segment_id, field, old, new, now))
        store.conn.commit()
    return store.rows('SELECT * FROM segments WHERE id=?', (segment_id,))[0]


class Deleter:
    """两步删除：request() 发 token，confirm() 凭 token 把会话数据与录音移入 trash（不直接删除）。"""

    def __init__(self, store, trash_dir, root=None):
        self.store, self.trash, self.root = store, Path(trash_dir), root
        self.pending = {}

    def request(self, session):
        token = secrets.token_urlsafe(16)
        self.pending[token] = session
        return token

    def confirm(self, session, token):
        if self.pending.get(token) != session:
            raise ReviewError('确认 token 无效', 409)
        if any(r['status'] == 'active' for r in self.store.main_rows('SELECT status FROM sessions WHERE id=?', (session,))):
            raise ReviewError('进行中的会话不能删除', 409)
        del self.pending[token]
        dest = self.trash / f'{session}_{int(self.store.clock())}'
        n = 0
        while dest.exists():
            n += 1
            dest = self.trash / f'{session}_{int(self.store.clock())}_{n}'
        (dest / 'recordings').mkdir(parents=True)
        s = self.store
        dump = {t: s.rows(f'SELECT * FROM {t} WHERE session_id=?', (session,))
                for t in ('segments', 'segment_edits', 'turns', 'turn_events', 'prompts')}
        dump['metrics'] = s.main_rows('SELECT * FROM metrics WHERE session_id=?', (session,))
        dump['recordings'] = [dict(r, path=str(r['path'])) for r in recording_files(s, session, self.root)]
        for r in dump['recordings']:
            if r['status'] != 'missing':
                shutil.move(r['path'], dest / 'recordings' / r['name'])
        (dest / 'session.json').write_text(json.dumps(dump, ensure_ascii=False, indent=1, default=str), 'utf-8')
        with s.lock:
            for t in ('segments', 'segment_edits', 'turns', 'turn_events', 'prompts', 'ingested_events'):
                s.conn.execute(f'DELETE FROM {t} WHERE session_id=?', (session,))
            s.conn.execute('INSERT OR REPLACE INTO deleted_sessions VALUES(?,?,?)', (session, str(dest), s.clock()))
            s.conn.commit()
        return dest
