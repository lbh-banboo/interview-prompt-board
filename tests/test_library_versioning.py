import json

import pytest
import sqlite3

from app.core import Store
from app.library import versioning
from app.library.conflicts import batch_approve, batch_approvable, review_entries
from app.library.parser import parse_markdown

HEAD = "# 合成题库\n\n## H1 项目\n"


def md(rows):
    return HEAD + "\n".join(rows) + "\n"


def row(n, answer, question="问题"):
    return f"**H1-{n:02d}｜00:0{n}｜{question}{n}？** 参考回答：{answer}"


BASE = [row(1, "我负责“星河”项目，团队 8 人。"), row(2, "合成回答二。"), row(3, "合成回答三。"), row(4, "合成回答四。")]


@pytest.fixture
def lib(tmp_path):
    store = Store(tmp_path)
    return store, store.import_text("合成.md", md(BASE))['id']


def approve_all(store, library):
    for e in store.entries(library):
        store.review(library, e['id'], "确认答案" + e['id'], 'approved', True)


def test_t1_counts_and_line_ranges():
    rows = [row(i, "合成回答。[待核]" if i <= 6 else "合成回答。") for i in range(1, 11)]
    rows[6] = row(7, "合成回答。\n续行说明 [待核]")
    rows[7] = row(8, "合成回答。", question="说话人待核的问题")
    items = parse_markdown(md(rows))
    assert len(items) == 10
    assert sum(i['flagged'] for i in items) == 8
    assert sum(i['attribution_uncertain'] for i in items) == 1
    assert [(i['line_start'], i['line_end']) for i in items][5:8] == [(9, 9), (10, 11), (12, 12)]
    assert items[6]['text'].endswith("[待核]") and items[6]['line'] == 10


def test_span_stops_at_heading_and_blank_lines():
    items = parse_markdown(row(1, "答。") + "\n\n正文续写\n\n## H2 下一节\n" + row(2, "答。"))
    assert (items[0]['line_start'], items[0]['line_end']) == (1, 3)
    assert items[1]['line_start'] == 6
    items = parse_markdown(row(1, "答。") + "\n## H2\n尾部\n")
    assert items[0]['line_end'] == 1


def test_hash_normalizes_whitespace_and_width():
    assert versioning.content_hash("a  b\n c") == versioning.content_hash("ａ b c ")
    assert versioning.content_hash("a") != versioning.content_hash("b")


def test_ensure_version_baseline(lib):
    store, library = lib
    assert versioning.ensure_version(store, library) == 1
    assert versioning.ensure_version(store, library) == 1
    with versioning.connect(store) as db:
        assert db.execute("SELECT COUNT(*) FROM entry_versions").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM facts WHERE kind='project'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        versioning.ensure_version(store, "nope")


def test_library_sources_read_only(lib):
    store, library = lib
    versioning.ensure_version(store, library)
    with versioning.connect(store) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE library_sources SET raw_text='x'")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM library_sources")


def test_t3_reimport_changes_one_entry(lib):
    store, library = lib
    approve_all(store, library)
    changed = BASE[:2] + [row(3, "合成回答三已修改。")] + BASE[3:]
    result = versioning.reimport(store, library, md(changed), "合成.md", clock=lambda: 5.0)
    assert result == dict(version=2, changed=True, inherited=3, pending=["H1-03"])
    entries = {e['id']: e for e in store.entries(library)}
    assert [e['status'] for e in entries.values()] == ['approved', 'approved', 'pending', 'approved']
    assert entries['H1-01']['approved_answer'] == "确认答案H1-01" and entries['H1-03']['approved_answer'] == ""
    with versioning.connect(store) as db:
        assert db.execute("SELECT COUNT(*) FROM library_sources").fetchone()[0] == 2
        assert db.execute("SELECT inherited_from FROM entry_versions WHERE version=2 AND entry_key='H1-01'").fetchone()[0] == 1
        assert db.execute("SELECT inherited_from FROM entry_versions WHERE version=2 AND entry_key='H1-03'").fetchone()[0] is None
        # 上一版本存档了当时的审核状态
        assert db.execute("SELECT status FROM entry_versions WHERE version=1 AND entry_key='H1-03'").fetchone()[0] == 'approved'
        # 继承条目的事实一并带入
        assert db.execute("SELECT COUNT(*) FROM facts WHERE version=2 AND entry_key='H1-01'").fetchone()[0] == 3
    assert store.libraries()[0]['approved'] == 3


def test_reimport_add_remove_and_noop(lib):
    store, library = lib
    approve_all(store, library)
    assert versioning.reimport(store, library, md(BASE))['changed'] is False
    new = BASE[:3] + [row(5, "新增条目。")]
    result = versioning.reimport(store, library, md(new))
    assert result['pending'] == ["H1-05"] and result['inherited'] == 3
    assert [e['id'] for e in store.entries(library)] == ["H1-01", "H1-02", "H1-03", "H1-05"]
    # 回到旧文本：H1-04 内容与历史版本相同，但上一版本已没有它 → 待审
    assert versioning.reimport(store, library, md(BASE))['pending'] == ["H1-04"]


def test_reimport_rejects(lib):
    store, library = lib
    other = store.import_text("别的", md([row(1, "别的内容。")]))['id']
    with pytest.raises(ValueError):
        versioning.reimport(store, library, md([row(1, "别的内容。")]))
    with pytest.raises(ValueError):
        versioning.reimport(store, library, "没有问答")
    approve_all(store, library)
    store.start('online', library)
    with pytest.raises(ValueError):
        versioning.reimport(store, library, md(BASE[:2]))
    assert other


def test_freeze_session_survives_later_edits(lib):
    store, library = lib
    approve_all(store, library)
    session = store.start('online', library)['id']
    frozen = versioning.freeze_session(store, session, library, index_version="idx1")
    assert frozen['library_version'] == 1 and len(frozen['approved_entry_hashes']) == 4
    store.stop(session)
    versioning.reimport(store, library, md(BASE[:3] + [row(4, "改动。")]))
    snap = versioning.session_snapshot(store, session, library)
    assert snap == dict(library_version=1, index_version="idx1", approved_entry_hashes=frozen['approved_entry_hashes'])
    with pytest.raises(ValueError):
        versioning.session_snapshot(store, "none", library)


def test_start_session_freezes_library_version(lib):
    """setup.start_session 开会时必须冻结版本；会后重新导入不影响本场快照。"""
    from app.sessions import setup as S
    from app.sessions.profiles import create_profile
    store, library = lib
    approve_all(store, library)
    sid = S.create_session_v2(store, 'online', create_profile(store, '甲')['id'], ['技术开发'], [library], True)['id']
    S.mark_ready(store, sid, dict(items=[], overall='PASS', has_fail=False, can_degrade_start=False,
                                  degraded_banner=None))
    S.start_session(store, sid)
    frozen = versioning.session_snapshot(store, sid, library)
    assert frozen['library_version'] == 1 and len(frozen['approved_entry_hashes']) == 4
    with pytest.raises(ValueError, match="冻结"):
        versioning.reimport(store, library, md(BASE[:2]))
    S.stop_session(store, sid)
    versioning.reimport(store, library, md(BASE[:3] + [row(4, "改动。")]))
    assert versioning.session_snapshot(store, sid, library) == frozen


def test_t4_batch_rejects_pending_verify(tmp_path):
    store = Store(tmp_path)
    library = store.import_text("x", md([row(1, "干净回答。"), row(2, "需核对 [待核]"), row(3, "另一条干净回答。")]))['id']
    assert batch_approvable(store, library) == ["H1-01", "H1-03"]
    with pytest.raises(ValueError, match="H1-02"):
        batch_approve(store, library, ["H1-01", "H1-02"], confirmed=True)
    assert all(e['status'] == 'pending' for e in store.entries(library))
    with pytest.raises(ValueError):
        batch_approve(store, library)
    assert batch_approve(store, library, confirmed=True) == ["H1-01", "H1-03"]
    assert batch_approvable(store, library) == []
    review = {e['id']: e for e in review_entries(store, library)}
    assert review["H1-02"]['flags'] == ["pending_verify"] and review["H1-02"]['can_approve']
    assert review["H1-01"]['line_start'] == 4 and review["H1-01"]['enhanced'] is False


def test_conflict_across_entries(tmp_path):
    store = Store(tmp_path)
    library = store.import_text("x", md([
        row(1, "我负责“星河”项目，团队 8 人。"), row(2, "在“星河”项目里带过 12 人团队。"),
        row(3, "“星河”项目的团队是 8 人。"), row(4, "“月光”项目团队 5 人。"), row(5, "无关回答。")]))['id']
    review = {e['id']: e for e in review_entries(store, library)}
    assert {k for k, e in review.items() if "conflict" in e['flags']} == {"H1-01", "H1-02", "H1-03"}
    assert review["H1-01"]['conflict_group'] == "星河|#人"
    assert batch_approvable(store, library) == ["H1-04", "H1-05"]
    from app.library.conflicts import list_conflicts
    groups = list_conflicts(store, library)
    assert len(groups) == 1 and groups[0]['project'] == "星河" and len(groups[0]['items']) == 3
