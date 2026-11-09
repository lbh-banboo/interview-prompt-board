"""跨条目冲突检测与批量启用资格。"""
import json
import re

from .facts_extract import RED_FLAGS

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _project(name):
    return re.sub(r"^[《“「\"]|[》”」\"]$", "", name)


def detect_conflicts(db, library_id, version):
    """同项目、同单位的数值在不同条目里不一致 → 同一 conflict_group；返回分组数。"""
    db.execute("UPDATE facts SET conflict_group=NULL WHERE library_id=? AND version=?", (library_id, version))
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM facts WHERE library_id=? AND version=? AND flag IS NULL ORDER BY id", (library_id, version))]
    projects = {}
    for r in rows:
        if r['kind'] == "project":
            projects.setdefault(r['entry_key'], []).append(_project(r['text']))
    groups, members = {}, {}
    for r in rows:
        if r['kind'] not in ("metric", "time"):
            continue
        own = projects.get(r['entry_key'], [])
        names = [p for p in own if p in r['source_span']] or (own if len(own) == 1 else [])
        unit = _NUMBER.sub("#", r['text']).replace(" ", "")
        for name in names:
            groups.setdefault((name, unit), {}).setdefault(r['entry_key'], set()).add(tuple(_NUMBER.findall(r['text'])))
            members.setdefault((name, unit), []).append(r['id'])
    count = 0
    for (name, unit), per_entry in groups.items():
        sets = list(per_entry.values())
        if len(sets) < 2 or all(s == sets[0] for s in sets):
            continue
        count += 1
        for fact_id in members[(name, unit)]:
            db.execute("UPDATE facts SET conflict_group=? WHERE id=?", (f"{name}|{unit}", fact_id))
    return count


def conflict_list(db, library_id, version):
    out = {}
    for r in db.execute("SELECT * FROM facts WHERE library_id=? AND version=? AND conflict_group IS NOT NULL ORDER BY id",
                        (library_id, version)):
        out.setdefault(r['conflict_group'], []).append(dict(
            entry=r['entry_key'], text=r['text'], source_span=r['source_span'],
            line_start=r['line_start'], line_end=r['line_end']))
    return [dict(group=g, project=g.split("|")[0], items=items) for g, items in out.items()]


def entry_flags(db, library_id, version, key, payload):
    """条目标记：(flags, details)。红色标记（疑似虚构/跨条目）禁止启用。"""
    flags, details = [], []
    if payload.get('flagged'):
        flags.append("pending_verify")
    if payload.get('attribution_uncertain'):
        flags.append("speaker_uncertain")
    row = db.execute("SELECT flags_json FROM entry_versions WHERE library_id=? AND version=? AND entry_key=?",
                     (library_id, version, key)).fetchone()
    details += json.loads(row['flags_json'])
    for r in db.execute("SELECT text,flag FROM facts WHERE library_id=? AND version=? AND entry_key=? AND flag IS NOT NULL",
                        (library_id, version, key)):
        details.append(dict(flag=r['flag'], token=r['text'], where="facts"))
    flags += [f for f in RED_FLAGS if any(d['flag'] == f for d in details)]
    if db.execute("SELECT 1 FROM facts WHERE library_id=? AND version=? AND entry_key=? AND conflict_group IS NOT NULL",
                  (library_id, version, key)).fetchone():
        flags.append("conflict")
    return flags, details


BATCH_BLOCKERS = {"pending_verify", "speaker_uncertain", "conflict", *RED_FLAGS}


def review_entries(store, library_id):
    """审核页数据：Store.entries 加上 facts/flags/enhanced/conflict_group/行号等扩展字段。"""
    from .versioning import connect, ensure_version
    version = ensure_version(store, library_id)
    out = []
    with connect(store) as db:
        meta = {r['entry_key']: r for r in db.execute(
            "SELECT * FROM entry_versions WHERE library_id=? AND version=?", (library_id, version))}
        for e in store.entries(library_id):
            m = meta[e['id']]
            flags, details = entry_flags(db, library_id, version, e['id'], e)
            facts = [dict(r) for r in db.execute(
                "SELECT kind,text,source_span,line_start,line_end,flag,conflict_group,origin FROM facts "
                "WHERE library_id=? AND version=? AND entry_key=? ORDER BY id", (library_id, version, e['id']))]
            groups = sorted({f['conflict_group'] for f in facts if f['conflict_group']})
            out.append(dict(
                e, version=version, content_hash=m['content_hash'], line_start=m['line_start'], line_end=m['line_end'],
                facts=facts, flags=flags, flag_details=details, enhanced=bool(m['enhanced']),
                enhance_error=m['enhance_error'], enhancement=json.loads(m['enhance_json']) if m['enhance_json'] else None,
                conflict_group=groups[0] if groups else None, conflict_groups=groups,
                can_approve=not any(f in RED_FLAGS or f == 'conflict' for f in flags),
                batch_approvable=e['status'] == 'pending' and not set(flags) & BATCH_BLOCKERS))
    return out


def conflict_entry_keys(store, library_id):
    """当前版本里有跨条目冲突事实的条目编号。"""
    from .versioning import connect, ensure_version
    version = ensure_version(store, library_id)
    with connect(store) as db:
        return {r['entry_key'] for r in db.execute(
            "SELECT DISTINCT entry_key FROM facts WHERE library_id=? AND version=? AND conflict_group IS NOT NULL",
            (library_id, version))}


def approve_block_reason(store, library_id, key):
    """单条启用前的硬拦：有跨条目冲突即返回原因，空串表示可进入审核。"""
    return '该条目含跨条目事实冲突，禁止启用；请先核对事实' if key in conflict_entry_keys(store, library_id) else ''


def batch_approvable(store, library_id):
    """可批量启用的条目 id：待审，且无待核、无说话人待核、无冲突、无疑似虚构/跨条目拼接。"""
    return [e['id'] for e in review_entries(store, library_id) if e['batch_approvable']]


def batch_approve(store, library_id, keys=None, confirmed=False):
    """批量启用；keys 缺省为全部可批量项；含不可批量项则整体拒绝。启用答案取条目原文答案。"""
    if not confirmed:
        raise ValueError("批量启用前必须确认")
    entries = {e['id']: e for e in review_entries(store, library_id)}
    allowed = [k for k, e in entries.items() if e['batch_approvable']]
    keys = allowed if keys is None else list(keys)
    bad = [k for k in keys if k not in allowed]
    if bad:
        raise ValueError("含待核/冲突/疑似虚构或不在待审状态的条目，不可批量启用：" + "、".join(bad))
    for k in keys:
        store.review(library_id, k, entries[k]['answer'], 'approved', True)
    return keys


def list_conflicts(store, library_id):
    from .versioning import connect, ensure_version
    version = ensure_version(store, library_id)
    with connect(store) as db:
        return conflict_list(db, library_id, version)
