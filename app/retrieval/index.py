"""M08-1/M08-2 检索索引：FTS5 trigram + 本地向量（sqlite-vec，缺失时降级为 BLOB+numpy）。

分数契约：bm25() 越负越相关，只用于排序；向量分数为余弦（已 L2 归一化）。
"""
import hashlib
import json
import re
import sqlite3
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "bge-small-zh-v1.5"
TOKENIZER_TAG = "fts5-trigram"

_CJK = "一-鿿"
_KEEP = re.compile(rf"[^a-z0-9{_CJK} ]")
_CJK_ASCII = re.compile(rf"(?<=[{_CJK}]) +(?=[a-z0-9])|(?<=[a-z0-9]) +(?=[{_CJK}])")


def normalize_fts(text):
    """小写；标点改空格；汉字与字母数字之间的空格去掉（“示例公司 AI 点单”==“示例公司AI点单”）。"""
    t = _KEEP.sub(" ", str(text).lower())
    t = re.sub(r" +", " ", t).strip()
    return _CJK_ASCII.sub("", t)


def trigrams(text):
    """按空格分段后取 3 字窗口，不跨段（避免跨标点拼接）。"""
    out = set()
    for seg in normalize_fts(text).split(" "):
        out.update(seg[i:i + 3] for i in range(len(seg) - 2))
    return out


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class HashEmbedder:
    """SIMULATED：字符 1-3 gram 哈希向量，仅用于测试与流程验证，不代表语义向量效果。"""
    name = "SIMULATED-hash-ngram"
    revision = "hash-v1"
    simulated = True

    def __init__(self, dim=256):
        self.dim = dim

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, t in enumerate(texts):
            s = normalize_fts(t).replace(" ", "")
            for n in (1, 2, 3):
                for i in range(len(s) - n + 1):
                    out[r, zlib.crc32(s[i:i + n].encode()) % self.dim] += float(n)
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norm == 0, 1, norm)


class FastEmbedder:
    """fastembed + bge-small-zh-v1.5 ONNX（本地目录加载，不联网）。问句对问句对称匹配，不加检索指令前缀。"""
    name = "BAAI/bge-small-zh-v1.5"
    simulated = False
    dim = 512

    def __init__(self, model_dir=MODEL_DIR):
        from fastembed import TextEmbedding
        self.model_dir = Path(model_dir)
        self.revision = self._revision()
        self._model = TextEmbedding("BAAI/bge-small-zh-v1.5", specific_model_path=str(self.model_dir),
                                    providers=["CPUExecutionProvider"])

    def _revision(self):
        manifest = ROOT / "models" / "MANIFEST.json"
        for item in json.loads(manifest.read_text("utf-8")):
            if item.get("file", "").endswith("model_optimized.onnx"):
                return f"{item['model_id']}@{item['revision']}+sha256:{item['sha256'][:16]}"
        return "unknown"

    def embed(self, texts):
        return np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)


class Index:
    """vector_backend: 'sqlite-vec' 或 'numpy-fallback'（sqlite-vec 不可用时的降级，如实暴露）。"""

    def __init__(self, embedder, path=":memory:", use_sqlite_vec=True):
        self.embedder = embedder
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.vector_backend = "numpy-fallback"
        if use_sqlite_vec:
            try:
                import sqlite_vec
                self.db.enable_load_extension(True)
                sqlite_vec.load(self.db)
                self.db.enable_load_extension(False)
                self.vector_backend = "sqlite-vec"
            except Exception:
                self.vector_backend = "numpy-fallback"
        self.index_version = ""
        self.size = 0
        self._tri = {}
        self._df = {}

    def build(self, entries):
        """entries: [{'id','question','variants':[...]}]。重建全部索引，返回 index_version。"""
        db = self.db
        for t in ("docs", "fts", "fts_vocab", "vec", "vecs"):
            db.execute(f"DROP TABLE IF EXISTS {t}")
        db.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, entry_id TEXT, kind TEXT, raw TEXT, norm TEXT)")
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(norm, tokenize='trigram')")
        db.execute("CREATE VIRTUAL TABLE fts_vocab USING fts5vocab(fts, 'row')")
        rows = []
        for e in entries:
            texts = [("q", e["question"])] + [("v", v) for v in e.get("variants") or []]
            rows += [(e["id"], k, t, normalize_fts(t)) for k, t in texts]
        with db:
            for i, (eid, kind, raw, norm) in enumerate(rows, 1):
                db.execute("INSERT INTO docs VALUES(?,?,?,?,?)", (i, eid, kind, raw, norm))
                db.execute("INSERT INTO fts(rowid,norm) VALUES(?,?)", (i, norm))
        vecs = self.embedder.embed([r[2] for r in rows]) if rows else np.zeros((0, self.embedder.dim), np.float32)
        dim = vecs.shape[1] if len(rows) else self.embedder.dim
        if self.vector_backend == "sqlite-vec":
            import sqlite_vec
            db.execute(f"CREATE VIRTUAL TABLE vec USING vec0(embedding float[{dim}] distance_metric=cosine)")
            with db:
                for i, v in enumerate(vecs, 1):
                    db.execute("INSERT INTO vec(rowid,embedding) VALUES(?,?)", (i, sqlite_vec.serialize_float32(v.tolist())))
        else:
            db.execute("CREATE TABLE vecs(id INTEGER PRIMARY KEY, v BLOB)")
            with db:
                for i, v in enumerate(vecs, 1):
                    db.execute("INSERT INTO vecs VALUES(?,?)", (i, v.astype(np.float32).tobytes()))
            self._mat = vecs.astype(np.float32)
        self.size = len(rows)
        self._tri = {i: trigrams(r[2]) for i, r in enumerate(rows, 1)}
        self._df = {t: d for t, d in ((r["term"], r["doc"]) for r in db.execute("SELECT term, doc FROM fts_vocab"))}
        self.index_version = self._version(entries)
        return self.index_version

    def _version(self, entries):
        canon = json.dumps([[e["id"], e["question"], list(e.get("variants") or [])]
                            for e in sorted(entries, key=lambda e: e["id"])], ensure_ascii=False)
        content = hashlib.sha256(canon.encode()).hexdigest()[:12]
        return f"{content}|{self.embedder.revision}|{TOKENIZER_TAG}"

    # ---- 查询原语 ----
    def doc(self, doc_id):
        return self.db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()

    def fts_query(self, query, top=20):
        """返回 (模式, [(doc_id, bm25|None)])。模式 'trigram' 或 'like'（查询不足 3 字，仅作关键词证据）。"""
        norm = normalize_fts(query)
        grams = sorted(trigrams(query))[:80]
        if grams:
            match = " OR ".join('"%s"' % g for g in grams)
            rows = self.db.execute("SELECT rowid, bm25(fts) s FROM fts WHERE fts MATCH ? ORDER BY s, rowid LIMIT ?",
                                   (match, top)).fetchall()
            return "trigram", [(r["rowid"], r["s"]) for r in rows]
        needle = norm.replace(" ", "")
        if not needle:
            return "like", []
        rows = self.db.execute(
            "SELECT id FROM docs WHERE replace(norm,' ','') LIKE ? ESCAPE '\\' ORDER BY length(norm), id LIMIT ?",
            ("%" + _like_escape(needle) + "%", top)).fetchall()
        return "like", [(r["id"], None) for r in rows]

    def vec_query(self, qvec, top=20):
        """返回 [(doc_id, cos)]，按 cos 降序。"""
        if self.size == 0:
            return []
        if self.vector_backend == "sqlite-vec":
            import sqlite_vec
            rows = self.db.execute("SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k=?",
                                   (sqlite_vec.serialize_float32(qvec.tolist()), min(top, self.size))).fetchall()
            return [(r["rowid"], 1.0 - r["distance"]) for r in sorted(rows, key=lambda r: r["distance"])]
        sims = self._mat @ qvec
        order = np.argsort(-sims, kind="stable")[:top]
        return [(int(i) + 1, float(sims[i])) for i in order]

    def cos_to_docs(self, qvec, doc_ids):
        if self.vector_backend == "sqlite-vec":
            out = {}
            for d in doc_ids:
                r = self.db.execute("SELECT embedding FROM vec WHERE rowid=?", (d,)).fetchone()
                out[d] = float(np.frombuffer(r[0], dtype=np.float32) @ qvec)
            return out
        return {d: float(self._mat[d - 1] @ qvec) for d in doc_ids}

    def keyword_overlap(self, query, doc_id, common_ratio=0.1):
        """查询 trigram 中（排除出现在 >common_ratio 文档里的高频 trigram）被该文档包含的个数。"""
        limit = max(1, int(self.size * common_ratio))
        return sum(1 for g in trigrams(query) if self._df.get(g, 0) <= limit and g in self._tri.get(doc_id, ()))
