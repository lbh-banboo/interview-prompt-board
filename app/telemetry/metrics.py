"""M14-1 埋点：metrics 表写入与浏览器/后端时钟偏移估计。"""
import sqlite3

SCHEMA = ('CREATE TABLE IF NOT EXISTS metrics ('
          'session_id TEXT, turn_id TEXT, name TEXT, t_ns INTEGER, value)')


class Metrics:
    def __init__(self, db):
        """db: sqlite3 连接或数据库路径。"""
        self.conn = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(str(db))
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def mark(self, session, turn_id, name, t_ns, value=None):
        self.conn.execute('INSERT INTO metrics VALUES (?,?,?,?,?)', (session, turn_id, name, int(t_ns), value))
        self.conn.commit()

    def rows(self, session):
        cur = self.conn.execute(
            'SELECT session_id, turn_id, name, t_ns, value FROM metrics WHERE session_id=? ORDER BY t_ns, rowid',
            (session,))
        return cur.fetchall()


def clock_offset(samples):
    """估计 backend_ns ≈ browser_ms*1e6 + offset_ns。

    samples: [(browser_send_ms, backend_ns, browser_recv_ms), ...]
    浏览器在 browser_send_ms 发探测，后端在 backend_ns 打戳，浏览器在 browser_recv_ms 收到回包。
    取往返(rtt)最小的样本，假设去程=回程，则偏移 = backend_ns - 中点*1e6，误差界 = rtt/2。
    返回 dict(offset_ns, error_ns, rtt_ns, n)。
    """
    if not samples:
        raise ValueError('至少需要 1 个时钟样本')
    best = min(samples, key=lambda s: s[2] - s[0])
    t0, srv_ns, t1 = best
    rtt_ns = (t1 - t0) * 1e6
    return dict(offset_ns=srv_ns - (t0 + t1) / 2 * 1e6, error_ns=rtt_ns / 2, rtt_ns=rtt_ns, n=len(samples))
