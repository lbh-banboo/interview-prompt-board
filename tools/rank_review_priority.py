"""主库审核优先级：按“跨多场面试重复出现的问题主题”排序，本地计算，不外发。

用法: .venv/bin/python tools/rank_review_priority.py [--library-index 0] [--thr 0.78]
输出: eval/annotations/library_review_priority.md / .json（含问题原文，个人数据，仅本地）
聚类: bge-small-zh 余弦 >= thr 的单连接合并；频次 = 簇内覆盖的不同面试场次(H编号前缀)数。
每簇推荐一个代表条目（优先无[待核]、答案较完整），审核代表即可让检索覆盖该主题的各种问法。
"""
import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))
from app.retrieval.index import FastEmbedder  # noqa: E402


def load(db_path, idx):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    lib = db.execute('select id,name from libraries order by rowid').fetchall()[idx]
    rows = [json.loads(r['payload']) | {'status': r['status']} for r in
            db.execute('select payload,status from entries where library_id=? order by rowid', (lib['id'],))]
    return lib['id'], lib['name'], rows


def cluster(vecs, thr):
    n = len(vecs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    sims = vecs @ vecs.T
    for i in range(n):
        for j in range(i + 1, n):
            if sims[i, j] >= thr:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values()), sims


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library-index', type=int, default=0)
    ap.add_argument('--thr', type=float, default=0.78)
    a = ap.parse_args()
    lib_id, lib_name, rows = load(ROOT / 'data' / 'interview.sqlite3', a.library_index)
    qs = [r['question'] for r in rows]
    vecs = np.array(FastEmbedder().embed(qs), dtype=np.float64)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    groups, sims = cluster(vecs, a.thr)
    out = []
    for g in groups:
        interviews = sorted({rows[i]['id'].split('-')[0] for i in g}, key=lambda s: int(s[1:]))
        def quality(i):  # 越大越适合当代表：无待核、无说话人待核、答案较完整
            r = rows[i]
            return (not r['flagged'], not r['attribution_uncertain'], len(r['answer']))
        rep = max(g, key=quality)
        out.append(dict(interviews=len(interviews), size=len(g), reps=rows[rep]['id'], rep_question=rows[rep]['question'],
                        rep_flagged=rows[rep]['flagged'], interview_ids=interviews,
                        members=[dict(id=rows[i]['id'], question=rows[i]['question'], flagged=rows[i]['flagged']) for i in g]))
    out.sort(key=lambda c: (-c['interviews'], c['rep_flagged'], -c['size'], c['reps']))
    for rank, c in enumerate(out, 1):
        c['rank'] = rank
    # 审核队列：A 档=跨场重复主题（大簇取离簇中心最近的至多 4 条；小簇取全部），B 档=单场条目按“通用性启发式”排序
    by_id = {r['id']: r for r in rows}
    idx = {r['id']: i for i, r in enumerate(rows)}
    queue, seen = [], set()
    for c in [c for c in out if c['interviews'] >= 2]:
        ids = [m['id'] for m in c['members']]
        if len(ids) > 4:
            cen = vecs[[idx[i] for i in ids]].mean(axis=0)
            ids = sorted(ids, key=lambda i: -float(vecs[idx[i]] @ cen))[:4]
        for i in ids:
            queue.append(dict(id=i, tier='A', why=f"跨{c['interviews']}场重复主题(簇#{c['rank']})")); seen.add(i)
    generic = ['优势', '规划', '离职', '带人', '介绍', '为什么', '团队', '冲突', '失败', '最大', '挑战', '薪资', '期望', '压力', '管理',
               '沟通', '协作', '怎么', '如何', '看法', '理解', '复盘', '成长', '缺点', '不足', '加班', '到岗', '职业']
    def gscore(r):
        q, ans = r['question'], r['answer']
        return (3 * (not r['flagged']) + 2 * (not r['attribution_uncertain']) + sum(k in q for k in generic)
                + (1 if 60 <= len(ans) <= 700 else 0) - (1 if '示例公司' in q else 0))
    rest = sorted((r for r in rows if r['id'] not in seen), key=lambda r: (-gscore(r), r['id']))
    for r in rest[:60]:
        queue.append(dict(id=r['id'], tier='B', why=f"通用性启发式分={gscore(r)}"))
    for n, q in enumerate(queue, 1):
        q['n'] = n
        q['question'] = by_id[q['id']]['question']
        q['flagged'] = by_id[q['id']]['flagged']
    d = ROOT / 'eval' / 'annotations'
    (d / 'library_review_queue.json').write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    QL = ['# 主库审核队列（建议按序审核，先审 A 档再 B 档）', '',
          '审核标准：对照原文确认答案事实；含 `[待核]` 的需你逐条确认后才能启用；缺失处写“需本人补充”。',
          '第 1~50 条审完即满足 G3（≥50 条已审核）。A 档=跨多场面试被问到的主题；B 档=单场出现但问法通用的条目（启发式，不是频次）。', '',
          '| # | 档 | id | 待核 | 问题 | 入选原因 |', '|---|---|---|---|---|---|']
    QL += [f"| {q['n']} | {q['tier']} | {q['id']} | {'是' if q['flagged'] else ''} | {q['question']} | {q['why']} |" for q in queue]
    (d / 'library_review_queue.md').write_text('\n'.join(QL))
    (d / 'library_review_priority.json').write_text(json.dumps(dict(library=lib_name, thr=a.thr, clusters=out), ensure_ascii=False, indent=1))
    L = [f'# 主库审核优先清单（{lib_name}）', '',
         f'共 {len(rows)} 条 → {len(out)} 个问题主题簇（余弦阈值 {a.thr}，单连接）。**频次 = 该主题在几场不同面试里被问到**。',
         '审核每个主题的「推荐代表」一条即可；`[待核]` 标记表示原文有待核事实，需要你逐条确认后才能启用（批量启用不允许）。', '',
         '| 排名 | 场次数 | 簇大小 | 推荐审核(id) | 待核 | 代表问题 | 同主题其它条目 |', '|---|---|---|---|---|---|---|']
    for c in out:
        others = '、'.join(m['id'] for m in c['members'] if m['id'] != c['reps'])
        L.append(f"| {c['rank']} | {c['interviews']} | {c['size']} | {c['reps']} | {'是' if c['rep_flagged'] else ''} | {c['rep_question']} | {others} |")
    top = [c for c in out if c['interviews'] >= 2]
    L += ['', f'## 汇总', f'- 跨 ≥2 场面试重复出现的主题：{len(top)} 个；其代表条目无[待核]的：{sum(1 for c in top if not c["rep_flagged"])} 个。',
          f'- 先审核排名前 50 的代表条目，即可达到 G3（≥50 条已审核）。']
    (d / 'library_review_priority.md').write_text('\n'.join(L))
    print(f'{len(rows)} 条 -> {len(out)} 簇；跨>=2场: {len(top)}；前50簇代表无待核: {sum(1 for c in out[:50] if not c["rep_flagged"])}')
    from collections import Counter
    print('队列', len(queue), 'A档', sum(q['tier']=='A' for q in queue), '前50无待核', sum(not q['flagged'] for q in queue[:50]))
    print('场次数分布:', sorted(Counter(c['interviews'] for c in out).items(), reverse=True))


main()
