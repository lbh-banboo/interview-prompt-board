"""M08 检索评测：命中率 / 库外误认率(Wilson 95% CI) / 延迟 / 阈值网格标定 / 离线对照（bigram BM25）。

类别: ALGO_REPLAY（文本侧检索评测，非语音链路）
"""
import argparse
import difflib
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.retrieval import ngram  # noqa: E402
from app.retrieval.index import FastEmbedder, HashEmbedder, Index  # noqa: E402
from app.retrieval.search import L1_HIT, Thresholds, classify_hit, search  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from build_retrieval_eval import OUT_OF_LIBRARY, load_entries  # noqa: E402

TAU = [round(0.50 + 0.02 * i, 2) for i in range(26)]
MARGIN_FLOOR = 0.03  # 评测集几乎没有并列样本，margin 无法由数据标定，选择时设先验下限
MARGIN = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10]
STRONG_GAP, MID_GAP = 0.06, 0.12


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(k, n):
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% (Wilson95 {100 * lo:.1f}-{100 * hi:.1f}%)" if n else "n/a"


def pctile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, math.ceil(q / 100 * len(xs)) - 1)]


def lat_line(xs):
    return f"P50 {pctile(xs, 50):.1f} / P95 {pctile(xs, 95):.1f} / P99 {pctile(xs, 99):.1f} ms (n={len(xs)})"


def split_of(item):
    key = item["expected"] if item["type"] == "paraphrase" else item["query"]
    return "cal" if int(hashlib.md5(str(key).encode()).hexdigest(), 16) % 2 == 0 else "hold"


def is_oos(it):
    return it["type"] in ("oos", "process") or (it["type"] == "short_term" and it["expected"] == OUT_OF_LIBRARY)


def accepted_ids(it):
    e = it["expected"]
    return {*(e if isinstance(e, list) else [e]), *it.get("equivalent_ids", [])}


def th_for(tau, margin):
    return Thresholds(tau, min(1.0, tau + STRONG_GAP), tau - MID_GAP, margin, False, "grid")


def metrics(rows, th):
    """rows: [(item, SearchResult)]。命中=L1_HIT 且 top1 正确；误认=库外题被判 L1_HIT；错命中=库内题 L1_HIT 但 id 不对。"""
    para = [(i, r) for i, r in rows if i["type"] == "paraphrase" and i["noise"] == "none"]
    oos = [(i, r) for i, r in rows if i["type"] in ("oos", "process")]
    hit = wrong = tie = 0
    for i, r in para:
        c = classify_hit(r, th)
        if c.label == L1_HIT:
            if r.top1_entry in accepted_ids(i):
                hit += 1
            else:
                wrong += 1
        elif c.label == "L1_TIE":
            tie += 1
    fa = sum(1 for _, r in oos if classify_hit(r, th).is_hit)
    return {"hit": hit, "wrong": wrong, "tie": tie, "n_para": len(para), "fa": fa, "n_oos": len(oos)}


def choose(rows):
    best = None
    for tau in TAU:
        for m in [x for x in MARGIN if x >= MARGIN_FLOOR]:
            s = metrics(rows, th_for(tau, m))
            if s["fa"] / max(1, s["n_oos"]) <= 0.02:
                key = (s["hit"] - s["wrong"], tau, m)
                if best is None or key > best[0]:
                    best = (key, tau, m, s)
    return best


def grid_table(rows):
    lines = ["| τ_cos \\ margin | " + " | ".join(f"{m:.2f}" for m in MARGIN) + " |", "|---|" + "---|" * len(MARGIN)]
    for tau in TAU[::2]:
        cells = []
        for m in MARGIN:
            s = metrics(rows, th_for(tau, m))
            cells.append(f"{100 * s['hit'] / s['n_para']:.0f}/{100 * s['fa'] / max(1, s['n_oos']):.1f}")
        lines.append(f"| {tau:.2f} | " + " | ".join(cells) + " |")
    return lines


# ---- 离线对照：bigram BM25 与 ngram Jaccard（仅参考，不进线上）----
class Bigram:
    def __init__(self, entries, k1=1.5, b=0.75):
        self.ids = [e["id"] for e in entries]
        self.docs = [self.tok(e["question"]) for e in entries]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / len(self.docs)
        df = Counter(g for d in self.docs for g in set(d))
        n = len(self.docs)
        self.idf = {g: math.log(1 + (n - c + 0.5) / (c + 0.5)) for g, c in df.items()}

    @staticmethod
    def tok(t):
        s = ngram.normalize(t)
        return [s[i:i + 2] for i in range(len(s) - 1)]

    def top1(self, q):
        qs = self.tok(q)
        best = (None, 0.0)
        for i, d in enumerate(self.docs):
            tf = Counter(d)
            sc = sum(self.idf.get(g, 0) * tf[g] * (self.k1 + 1) / (tf[g] + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
                     for g in set(qs) if g in tf)
            if sc > best[1]:
                best = (self.ids[i], sc)
        norm = sum(self.idf.get(g, 0) for g in set(qs)) or 1.0
        return best[0], best[1] / norm


class Jaccard:
    def __init__(self, entries):
        self.e = [(e["id"], ngram.grams(e["question"])) for e in entries]

    def top1(self, q):
        g = ngram.grams(q)
        best = (None, 0.0)
        for i, gs in self.e:
            u = len(g | gs)
            s = len(g & gs) / u if u else 0.0
            if s > best[1]:
                best = (i, s)
        return best


def control_eval(model, items):
    out = [(it, *model.top1(it["query"])) for it in items if it["type"] in ("paraphrase", "oos", "process")]

    def run(rows):
        para = [r for r in rows if r[0]["type"] == "paraphrase" and r[0]["noise"] == "none"]
        oos = [r for r in rows if r[0]["type"] in ("oos", "process")]
        top1 = sum(1 for i, p, _ in para if p in accepted_ids(i))
        best = None
        for t in [x / 100 for x in range(0, 101, 2)]:
            fa = sum(1 for _, _, s in oos if s >= t)
            if fa / len(oos) <= 0.02:
                hit = sum(1 for i, p, s in para if s >= t and p in accepted_ids(i))
                if best is None or hit > best[1]:
                    best = (t, hit, fa)
        return len(para), len(oos), top1, best

    cal = [r for r in out if split_of(r[0]) == "cal"]
    t_cal = run(cal)[3]
    hold = [r for r in out if split_of(r[0]) == "hold"]
    hp, ho, top1, _ = run(hold)
    if t_cal is None:
        return {"top1_hold": (top1, hp), "threshold": None}
    t = t_cal[0]
    hit = sum(1 for i, p, s in hold if i["type"] == "paraphrase" and i["noise"] == "none" and s >= t and p in accepted_ids(i))
    fa = sum(1 for i, p, s in hold if i["type"] in ("oos", "process") and s >= t)
    return {"top1_hold": (top1, hp), "threshold": t, "hit_hold": (hit, hp), "fa_hold": (fa, ho)}


def cold_subprocess(embedder_kind):
    code = (
        "import time,sys;t0=time.perf_counter();sys.path.insert(0,%r)\n"
        "from app.retrieval.index import *;from app.retrieval.search import search\n"
        "t1=time.perf_counter();e=FastEmbedder();t2=time.perf_counter()\n"
        "e.embed(['示例公司 AI 点单底层接的是哪家模型？']);t3=time.perf_counter()\n"
        "e.embed(['你们点单用的什么模型']);t4=time.perf_counter()\n"
        "print(round((t1-t0)*1000,1),round((t2-t1)*1000,1),round((t3-t2)*1000,1),round((t4-t3)*1000,1))\n" % str(ROOT)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        return None
    a, b, c, d = map(float, r.stdout.split())
    return {"import_ms": a, "model_load_ms": b, "first_embed_ms": c, "second_embed_ms": d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", choices=["fastembed", "hash"], default="fastembed")
    ap.add_argument("--db", default=str(ROOT / "data" / "interview.sqlite3"))
    ap.add_argument("--eval", default=str(ROOT / "eval" / "retrieval" / "eval_set.json"))
    ap.add_argument("--report", default=str(ROOT / "eval" / "retrieval" / "report.md"))
    ap.add_argument("--no-write-thresholds", action="store_true")
    a = ap.parse_args()

    entries = load_entries(a.db)
    doc = json.loads(Path(a.eval).read_text("utf-8"))
    items = doc["items"]
    simulated = a.embedder == "hash"
    t0 = time.perf_counter()
    emb = HashEmbedder() if simulated else FastEmbedder()
    t_emb = time.perf_counter() - t0
    index = Index(emb)
    t0 = time.perf_counter()
    index.build(entries)
    t_build = time.perf_counter() - t0

    # 延迟：首条（冷，含 ONNX 会话首次推理）与热
    hot_embed = []
    for q in [it["query"] for it in items[:200]]:
        t0 = time.perf_counter(); emb.embed([q]); hot_embed.append((time.perf_counter() - t0) * 1000)
    for it in items[:5]:
        search(index, it["query"])
    rows, lat, lat_by = [], [], {}
    for it in items:
        t0 = time.perf_counter()
        r = search(index, it["query"])
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        lat_by.setdefault(r.keyword_mode, []).append(ms)
        rows.append((it, r))

    cal = [x for x in rows if split_of(x[0]) == "cal"]
    hold = [x for x in rows if split_of(x[0]) == "hold"]
    ch = choose(cal)
    placeholder = Thresholds()
    L = []
    w = L.append
    w("类别: ALGO_REPLAY（文本侧检索评测，非语音链路）")
    w("")
    w("# M08 检索评测报告")
    w("")
    if simulated:
        w("> **SIMULATED**：本次使用 HashEmbedder（字符 n-gram 哈希向量，非语义模型）。向量路指标 NOT TESTED，仅验证流程。")
    else:
        w(f"- 向量模型: {emb.name} / {emb.revision}（CPU，fastembed {_ver('fastembed')}，onnxruntime {_ver('onnxruntime')}）")
    w(f"- 向量后端: {index.vector_backend}；index_version: `{index.index_version}`")
    w(f"- 评测集: {doc['library_size']} 条主库（评测用全量建索引，不影响审核状态），{len(items)} 条查询，构成 {json.dumps(doc['counts'], ensure_ascii=False)}")
    w("- 数据来源与限制: 评测集由规则式改写生成（同义替换/口语化/语气词），库外题自编；**不等于真实面试问法或真实 ASR 转写噪声**。"
      "`synthetic_asr` 档为合成噪声（同音字替换/漏字），同样不代表真实 ASR 误差。库内主库条目为单一人物的面试复盘，主题集中，库外题中的 domain_adjacent 档刻意取相邻主题。")
    w("- 评测数据为本地个人数据，仅本机读取，未外发。")
    w(f"- 校准/留出划分: 按 id（改写）或查询文本（其他）哈希奇偶；阈值在校准半区选取，**留出半区**数字用于判断是否过拟合。")
    w("")
    w("## 阈值标定")
    w(f"网格: τ_cos {TAU[0]}..{TAU[-1]} × margin {MARGIN}；τ_cos_strong=τ_cos+{STRONG_GAP}，τ_mid=τ_cos−{MID_GAP}（固定偏移）；keyword 命中=trigram 重叠≥2（排除 df>10% 的高频 trigram）。")
    w(f"选择准则: 校准半区库外误认点估计≤2% 前提下，(正确命中−错命中)最大，平局取更保守（更高 τ_cos、更大 margin）；margin 仅在 ≥{MARGIN_FLOOR} 中选——评测集几乎没有真正并列的样本（仅 1 对完全相同的问题 H1-31/H5-13），margin 不能由数据标定，下限是先验，不是实测最优。")
    if ch is None:
        w("**没有网格点满足库外误认≤2%**，未写入默认值（保留占位）。")
        chosen = None
    else:
        _, tau, mg, s = ch
        chosen = th_for(tau, mg)
        w(f"- 选定: τ_cos={tau}, τ_cos_strong={chosen.tau_cos_strong:.2f}, τ_mid={chosen.tau_mid:.2f}, margin={mg}")
        w(f"- 校准半区: 命中 {pct(s['hit'], s['n_para'])}；错命中 {s['wrong']}；并列 {s['tie']}；库外误认 {pct(s['fa'], s['n_oos'])}")
        hs = metrics(hold, chosen)
        w(f"- **留出半区**: 命中 {pct(hs['hit'], hs['n_para'])}；错命中 {hs['wrong']}；并列 {hs['tie']}；库外误认 {pct(hs['fa'], hs['n_oos'])}")
        fs = metrics(rows, chosen)
        w(f"- 全集(含校准，乐观): 命中 {pct(fs['hit'], fs['n_para'])}；库外误认 {pct(fs['fa'], fs['n_oos'])}")
        w(f"- 注意: 库外题样本量有限（全集 {fs['n_oos']} 条），0 误认的 Wilson 95% 上界仍约 {100 * wilson(0, fs['n_oos'])[1]:.1f}%，"
          "不能据此宣称真实误认≤2%；需更大、更真实的库外样本复核。")
    w("")
    w("### 网格（全集；单元格=正确命中率%/库外误认率%，每隔一档 τ_cos 显示）")
    L.extend(grid_table(rows))
    w("")
    if chosen:
        w("## 分层（选定阈值，全集）")
        for name, pred in [("paraphrase clean", lambda i: i["type"] == "paraphrase" and i["noise"] == "none"),
                           ("paraphrase synthetic_asr（合成噪声）", lambda i: i["type"] == "paraphrase" and i["noise"] != "none")]:
            sub = [x for x in rows if pred(x[0])]
            h = sum(1 for i, r in sub if classify_hit(r, chosen).is_hit and r.top1_entry in accepted_ids(i))
            tie = sum(1 for i, r in sub if classify_hit(r, chosen).label == "L1_TIE")
            top3 = sum(1 for i, r in sub if accepted_ids(i) & {x.entry_id for x in r.hits[:3]})
            w(f"- {name}: 命中 {pct(h, len(sub))}；并列 {tie}；RRF top3 含正确条目 {pct(top3, len(sub))}")
        for key in ("general", "behavior", "domain_adjacent", "personal"):
            sub = [x for x in rows if x[0].get("subtype") == key]
            w(f"- 库外/{key}: 误认 {pct(sum(1 for _, r in sub if classify_hit(r, chosen).is_hit), len(sub))}")
        sub = [x for x in rows if x[0]["type"] == "process"]
        w(f"- 流程性发言(检索侧；线上由意图分类先拦截): 误认 {pct(sum(1 for _, r in sub if classify_hit(r, chosen).is_hit), len(sub))}")
        sub = [x for x in rows if x[0]["type"] == "short_term"]
        inlib = [x for x in sub if not is_oos(x[0])]
        kw_ok = sum(1 for i, r in inlib if any(h.entry_id in accepted_ids(i) and h.keyword_hit for h in r.hits[:5]))
        w(f"- 短词(<3 字走 LIKE，仅关键词证据；≥3 字走 trigram) 库内词关键词证据召回@5: {pct(kw_ok, len(inlib))}；"
          f"其中被判 L1 命中: {sum(1 for _, r in inlib if classify_hit(r, chosen).is_hit)} 条")
        oo = [x for x in sub if is_oos(x[0])]
        w(f"- 短词库外词误认: {pct(sum(1 for _, r in oo if classify_hit(r, chosen).is_hit), len(oo))}")
        w("")
    entry_q = {e["id"]: e["question"] for e in entries}
    cl = [x for x in rows if x[0]["type"] == "paraphrase" and x[0]["noise"] == "none"]
    sim = statistics.mean(difflib.SequenceMatcher(None, i["query"], entry_q[i["expected"]]).ratio() for i, _ in cl)
    w("## 难度与消融（读数时须知）")
    w(f"- 规则改写与原问题的平均字符相似度 {sim:.2f}（越高越容易）：评测集偏易，三种方法差异小，**不能据此证明向量路的增益**，只能说明在低难度改写上不退化。")
    vec_ok = sum(1 for i, r in cl if r.top1_entry in accepted_ids(i))
    fts_ok = sum(1 for i, r in cl if any(h.bm25_rank == 1 and h.entry_id in accepted_ids(i) for h in r.hits))
    rrf_ok = sum(1 for i, r in cl if r.hits and r.hits[0].entry_id in accepted_ids(i))
    w(f"- clean 改写 top1 准确率（不拒认，全集）: 仅向量 {pct(vec_ok, len(cl))}；仅 FTS trigram {pct(fts_ok, len(cl))}；RRF 融合 {pct(rrf_ok, len(cl))}")
    w("")
    w("## 延迟（本机 CPU，单线程，单条查询含 query 嵌入）")
    w(f"- 端到端 search 热: {lat_line(lat)}  → M08-T2 目标 P95≤50ms: {'满足(实测, 226 条规模)' if pctile(lat, 95) <= 50 else '未满足'}"
      + ("（SIMULATED 嵌入，不代表真实模型）" if simulated else ""))
    for k, v in lat_by.items():
        w(f"  - 其中 {k} 模式: {lat_line(v)}")
    w(f"- 向量单条嵌入 热: {lat_line(hot_embed)}（冷见下，建索引时已完成首次推理，故进程内不再单测冷值）")
    w(f"- 嵌入器构造 {t_emb * 1000:.0f} ms；建索引({index.size} 文档) {t_build * 1000:.0f} ms")
    if not simulated:
        cs = cold_subprocess("fastembed")
        if cs:
            w(f"- 冷启动(新子进程, 操作系统文件缓存已热): import {cs['import_ms']:.0f} ms, 模型加载 {cs['model_load_ms']:.0f} ms, 首条嵌入(冷) {cs['first_embed_ms']:.1f} ms, 第二条嵌入 {cs['second_embed_ms']:.1f} ms（单次采样）")
    w("- 规模限制: 仅 226 条/226 文档，未测 1 千条规模；CPU 型号/并发未控制。")
    w("")
    w("## 离线对照（M08-T6，仅参考，不进线上）")
    w("对照方法无 margin/多证据，只有单阈值（分数/查询 idf 总和 或 Jaccard）；阈值同样在校准半区选取（库外误认≤2%），数字取留出半区。")
    for name, model in [("bigram BM25", Bigram(entries)), ("现有 ngram Jaccard", Jaccard(entries))]:
        c = control_eval(model, items)
        n = c["top1_hold"]
        if c["threshold"] is None:
            w(f"- {name}: top1 准确率(不拒认, 留出) {pct(*n)}；无阈值满足误认≤2%")
        else:
            w(f"- {name}: top1 准确率(不拒认, 留出) {pct(*n)}；阈值 {c['threshold']:.2f}；留出命中 {pct(*c['hit_hold'])}；留出库外误认 {pct(*c['fa_hold'])}")
    if chosen:
        hs = metrics(hold, chosen)
        w(f"- trigram+向量 RRF（本方案，留出）: 命中 {pct(hs['hit'], hs['n_para'])}；库外误认 {pct(hs['fa'], hs['n_oos'])}"
          + ("（向量 SIMULATED）" if simulated else ""))
    w("")
    Path(a.report).write_text("\n".join(L) + "\n", "utf-8")
    if chosen and not simulated and not a.no_write_thresholds:
        out = ROOT / "app" / "retrieval" / "thresholds.json"
        out.write_text(json.dumps({
            "tau_cos": chosen.tau_cos, "tau_cos_strong": round(chosen.tau_cos_strong, 2), "tau_mid": round(chosen.tau_mid, 2),
            "margin": chosen.margin, "embedder_revision": emb.revision, "index_version": index.index_version,
            "source": "tools/eval_retrieval.py 校准半区选取（规则改写评测集，非真实语音；见 eval/retrieval/report.md）",
        }, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print("\n".join(L))


def _ver(pkg):
    from importlib.metadata import version
    return version(pkg)


if __name__ == "__main__":
    main()
