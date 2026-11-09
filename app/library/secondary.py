"""M07 次优库：种子解析、SQLite 导入、岗位隔离检索。

次优库只存“通用原则与回答框架”，绝不写入 facts；展示标签统一“通用建议”。
格式规范见 seeds/secondary/README.md。
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = ROOT / "seeds" / "secondary"
GENERAL = "general"
LABEL_TEXT = "通用建议"
TYPES = ("知识", "行为", "情景", "观点")
STATUSES = ("pending", "approved", "rejected")

HEAD = re.compile(r"^### (Q[A-Z]{2,6}-\d{2,3}) (.+?)\s*$")
LABEL = re.compile(r"^(变体|结论|要点|示例|注意|岗位|类型)\s*[:：]\s*(.*)$")
ITEM = re.compile(r"^- (.+?)\s*$")
TAG = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
PLACEHOLDER = re.compile(r"^(\[需本人补充[:：][^\[\]]+\])+$")
LIST_LABELS = {"变体": "variants", "要点": "points"}
TEXT_LABELS = {"结论": "conclusion", "示例": "example", "注意": "caution"}


def parse_seed_text(text):
    """返回 (entries, errors)；errors 为 [(题号或'-', 说明)]。非 `### Q` 块的标题/说明文字被忽略。"""
    entries, errors = [], []
    cur = None
    label = None

    def close():
        if cur is not None:
            errors.extend((cur["id"], m) for m in _validate(cur))
            entries.append(cur)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "---":
            continue
        if line.startswith("#"):
            m = HEAD.match(line)
            if line.startswith("### ") and not m:
                errors.append(("-", f"题目标题格式错误: {line[:40]}"))
            close()
            cur, label = None, None
            if m:
                cur = dict(id=m.group(1), question=m.group(2), variants=[], conclusion="", points=[],
                           example="", caution="", roles=[], type="", seen=[])
            continue
        if cur is None:
            continue
        m = LABEL.match(line)
        if m:
            label, rest = m.group(1), m.group(2).strip()
            if label in cur["seen"]:
                errors.append((cur["id"], f"字段重复: {label}"))
            cur["seen"].append(label)
            if label in TEXT_LABELS:
                cur[TEXT_LABELS[label]] = rest
            elif label == "岗位":
                cur["roles"] = [t.strip() for t in re.split(r"[,，]", rest) if t.strip()]
            elif label == "类型":
                cur["type"] = rest
            elif rest:
                errors.append((cur["id"], f"{label} 须用 `- ` 列表书写"))
            continue
        m = ITEM.match(line)
        if m and label in LIST_LABELS:
            cur[LIST_LABELS[label]].append(m.group(1))
        else:
            errors.append((cur["id"], f"无法识别的行: {line[:40]}"))
    close()
    ids = [e["id"] for e in entries]
    errors.extend((i, "题号重复") for i in sorted({i for i in ids if ids.count(i) > 1}))
    for e in entries:
        del e["seen"]
    return entries, errors


def _validate(e):
    errs = []
    for need in LIST_LABELS.keys() | TEXT_LABELS.keys() | {"岗位", "类型"}:
        if need not in e["seen"]:
            errs.append(f"缺少字段: {need}")
    if not 3 <= len(e["variants"]) <= 5 and "变体" in e["seen"]:
        errs.append(f"变体须 3-5 条，实际 {len(e['variants'])}")
    if len(e["points"]) != 3 and "要点" in e["seen"]:
        errs.append(f"要点须 3 条，实际 {len(e['points'])}")
    for f, name in (("conclusion", "结论"), ("caution", "注意")):
        if name in e["seen"] and not e[f]:
            errs.append(f"{name} 不能为空")
    if "示例" in e["seen"] and not PLACEHOLDER.match(e["example"]):
        errs.append("示例必须是 [需本人补充：…] 占位")
    if "岗位" in e["seen"]:
        bad = [t for t in e["roles"] if not TAG.match(t)]
        if not e["roles"] or bad:
            errs.append(f"岗位标签无效: {bad or '空'}")
    if "类型" in e["seen"] and e["type"] not in TYPES:
        errs.append(f"类型须为 {'/'.join(TYPES)}")
    return errs


def outline_text(e):
    """回答框架文本（供检索展示/索引）。"""
    pts = "\n".join(f"- {p}" for p in e["points"])
    return f"结论：{e['conclusion']}\n要点：\n{pts}\n示例：{e['example']}\n注意：{e['caution']}"


class SecondaryLibrary:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS secondary_entries(id TEXT PRIMARY KEY, question TEXT, payload TEXT,
                content_hash TEXT, source TEXT, source_hash TEXT, status TEXT DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS secondary_roles(entry_id TEXT, role TEXT, PRIMARY KEY(entry_id, role));
            CREATE INDEX IF NOT EXISTS secondary_roles_role ON secondary_roles(role);
            """)

    @staticmethod
    def _sql(db, sql, args=()):
        # 次优库不得写 facts：所有 SQL 经此守卫
        assert "facts" not in sql.lower(), "次优库不得触碰 facts"
        return db.execute(sql, args)

    def import_dir(self, directory=SEED_DIR):
        """导入目录下全部 *.md（README.md 除外）；返回 dict(files, imported, unchanged, errors)。"""
        parsed, errors = [], []
        for path in sorted(Path(directory).glob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            text = path.read_text("utf-8")
            entries, errs = parse_seed_text(text)
            errors.extend(dict(source=path.name, id=i, error=m) for i, m in errs)
            parsed.append((path.name, hashlib.sha256(text.encode()).hexdigest(), entries))
        ids = [e["id"] for _, _, es in parsed for e in es]
        errors.extend(dict(source="-", id=i, error="跨文件题号重复") for i in sorted({i for i in ids if ids.count(i) > 1}))
        bad = {x["id"] for x in errors}
        imported = unchanged = 0
        with self.store.lock, self.store.connect() as db:
            for name, shash, entries in parsed:
                keep = []
                for e in entries:
                    if e["id"] in bad:
                        continue
                    keep.append(e["id"])
                    payload = dict(e, kind="secondary", label=LABEL_TEXT)
                    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    chash = hashlib.sha256(blob.encode()).hexdigest()
                    old = self._sql(db, "SELECT content_hash FROM secondary_entries WHERE id=?", (e["id"],)).fetchone()
                    if old and old["content_hash"] == chash:
                        self._sql(db, "UPDATE secondary_entries SET source=?,source_hash=? WHERE id=?", (name, shash, e["id"]))
                        unchanged += 1
                        continue
                    self._sql(db, "INSERT INTO secondary_entries(id,question,payload,content_hash,source,source_hash,status) "
                                  "VALUES(?,?,?,?,?,?,'pending') ON CONFLICT(id) DO UPDATE SET question=excluded.question,"
                                  "payload=excluded.payload,content_hash=excluded.content_hash,source=excluded.source,"
                                  "source_hash=excluded.source_hash,status='pending'",
                              (e["id"], e["question"], blob, chash, name, shash))
                    self._sql(db, "DELETE FROM secondary_roles WHERE entry_id=?", (e["id"],))
                    for r in e["roles"]:
                        self._sql(db, "INSERT INTO secondary_roles VALUES(?,?)", (e["id"], r))
                    imported += 1
                if not any(x["source"] == name for x in errors):
                    marks = ",".join("?" * len(keep))
                    gone = [r["id"] for r in self._sql(
                        db, f"SELECT id FROM secondary_entries WHERE source=? AND id NOT IN ({marks})", (name, *keep))]
                    for i in gone:
                        self._sql(db, "DELETE FROM secondary_entries WHERE id=?", (i,))
                        self._sql(db, "DELETE FROM secondary_roles WHERE entry_id=?", (i,))
        return dict(files=len(parsed), imported=imported, unchanged=unchanged, errors=errors)

    def review(self, entry_id, status, confirmed):
        if status not in STATUSES:
            raise ValueError("状态无效")
        if status == "approved" and not confirmed:
            raise ValueError("批准须人工确认")
        with self.store.lock, self.store.connect() as db:
            cur = self._sql(db, "UPDATE secondary_entries SET status=? WHERE id=?", (status, entry_id))
            return cur.rowcount > 0

    def roles(self):
        with self.store.connect() as db:
            rows = self._sql(db, "SELECT r.role role, COUNT(*) total, SUM(e.status='approved') approved "
                                 "FROM secondary_roles r JOIN secondary_entries e ON e.id=r.entry_id "
                                 "GROUP BY r.role ORDER BY r.role").fetchall()
        return [dict(role=r["role"], total=r["total"], approved=r["approved"] or 0) for r in rows]

    def entries(self, role=None, status=None):
        """审核台列表：按岗位/状态筛选，payload 字段展开并附 roles/source/status。"""
        sql = ("SELECT e.id id, e.question question, e.payload payload, e.source source, e.status status, "
               "(SELECT GROUP_CONCAT(r.role) FROM secondary_roles r WHERE r.entry_id=e.id) roles "
               "FROM secondary_entries e WHERE 1=1")
        args = []
        if role:
            sql += " AND EXISTS(SELECT 1 FROM secondary_roles r WHERE r.entry_id=e.id AND r.role=?)"
            args.append(role)
        if status:
            sql += " AND e.status=?"
            args.append(status)
        with self.store.connect() as db:
            rows = self._sql(db, sql + " ORDER BY e.id", args).fetchall()
        return [dict(json.loads(r["payload"]), id=r["id"], question=r["question"], status=r["status"],
                     source=r["source"], roles=sorted(filter(None, (r["roles"] or "").split(","))))
                for r in rows]

    def progress(self):
        """审核进度：总体与分岗位计数（多岗位题在各自岗位下各计一次）。"""
        with self.store.connect() as db:
            top = self._sql(db, "SELECT COUNT(*) total, SUM(status='approved') approved, "
                                "SUM(status='rejected') rejected, SUM(status='pending') pending "
                                "FROM secondary_entries").fetchone()
            rows = self._sql(db, "SELECT r.role role, COUNT(*) total, SUM(e.status='approved') approved, "
                                 "SUM(e.status='rejected') rejected, SUM(e.status='pending') pending "
                                 "FROM secondary_roles r JOIN secondary_entries e ON e.id=r.entry_id "
                                 "GROUP BY r.role ORDER BY r.role").fetchall()

        def counts(row):
            return dict(total=row["total"], approved=row["approved"] or 0, rejected=row["rejected"] or 0,
                        pending=row["pending"] or 0)

        out = counts(top)
        out["reviewed"] = out["approved"] + out["rejected"]
        out["by_role"] = [dict(role=r["role"], **counts(r)) for r in rows]
        return out

    def candidates(self, roles, include_pending=False):
        """所选岗位并集 + 跨岗位通用；默认仅已审核。每条带 source/source_hash/status。"""
        wanted = sorted(set(roles or []) | {GENERAL})
        marks = ",".join("?" * len(wanted))
        status = "" if include_pending else "AND e.status='approved'"
        with self.store.connect() as db:
            rows = self._sql(db, f"SELECT DISTINCT e.id,e.payload,e.source,e.source_hash,e.status FROM secondary_entries e "
                                 f"JOIN secondary_roles r ON r.entry_id=e.id WHERE r.role IN ({marks}) {status} ORDER BY e.id",
                             wanted).fetchall()
        return [dict(json.loads(r["payload"]), source=r["source"], source_hash=r["source_hash"], status=r["status"])
                for r in rows]

    def index_entries(self, roles, include_pending=False):
        """与 app.retrieval.index.Index.build 兼容：id/question/variants，另附 outline/roles/kind。"""
        return [dict(id=e["id"], question=e["question"], variants=e["variants"], outline=outline_text(e),
                     roles=e["roles"], kind="secondary", label=LABEL_TEXT)
                for e in self.candidates(roles, include_pending)]
