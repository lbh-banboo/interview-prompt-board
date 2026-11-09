import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import library_enhance
from app.core import Store
from app.library import versioning
from app.library.conflicts import batch_approvable, batch_approve, review_entries
from app.library.enhance import SplitInvalid, build_messages, enhance_library, validate_split
from app.library.facts_extract import extract_rule_facts, locate
from app.library.parser import parse_markdown

A = "**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我在示例科技公司负责“星河”项目，团队 8 人，2023年5月上线，性能提升 30%。"
B = "**H1-02｜00:02｜介绍“月光”项目？** 参考回答：我负责“月光”项目，团队 20 人，运行 3年。"
C = "**H1-03｜00:03｜怎么协作？** 参考回答：与相关团队同步。"
D = "**H1-04｜00:04｜待核题？** 参考回答：回答 [待核]"
TEXT = "## H1 合成\n" + "\n".join([A, B, C]) + "\n"


def split(question="Q", facts=(), **over):
    data = dict(question=question, question_variants=["a", "b", "c"],
                answer_outline=dict(conclusion="结论", points=["1点", "2点", "3点"], examples=[], cautions=[]),
                facts=list(facts), role_tags=["后端"], topic_tags=["项目"])
    data.update(over)
    return data


class Mock:
    """按条目编号返回预设输出；值为异常实例则抛出。"""
    def __init__(self, table):
        self.table, self.seen = table, []

    async def split(self, entry_text):
        self.seen.append(entry_text)
        out = self.table[entry_text.split("｜")[0].strip("*")]
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def lib(tmp_path):
    store = Store(tmp_path)
    return store, store.import_text("合成.md", TEXT)['id']


def run(store, library, enhancer, **kw):
    return asyncio.run(enhance_library(store, library, enhancer, **kw))


def by_id(store, library):
    return {e['id']: e for e in review_entries(store, library)}


def test_rule_facts_have_spans_inside_entry():
    item = parse_markdown(TEXT)[0]
    facts = extract_rule_facts(item)
    kinds = {(f['kind'], f['text']) for f in facts}
    assert {("project", "“星河”"), ("metric", "8 人"), ("metric", "30%"), ("time", "2023年5月"),
            ("org", "示例科技公司"), ("role", "负责“星河”项目")} <= kinds
    assert all(item['line_start'] <= f['line_start'] <= f['line_end'] <= item['line_end'] and f['source_span'] in item['text']
               for f in facts)
    assert [f['text'] for f in extract_rule_facts(parse_markdown(B)[0]) if f['kind'] == 'time'] == ["3年"]


def test_rule_facts_multiline_and_dedup():
    item = parse_markdown("**H1-01｜00:01｜问？** 参考回答：花了 3个月。\n续行 3个月 与 5 个团队。")[0]
    facts = extract_rule_facts(item)
    assert [(f['text'], f['line_start']) for f in facts if f['kind'] == 'time'] == [("3个月", 1), ("3个月", 2)]
    assert [f['text'] for f in facts if f['kind'] == 'metric'] == ["5 个"]


def test_validate_split_ok_and_errors():
    assert validate_split(split(facts=[dict(kind="metric", text=" 8人 ")]))['facts'] == [dict(kind="metric", text="8人", source_span="")]
    bad = [
        "text", split(question=" "), split(question_variants=["a", "b"]), split(question_variants=["a"] * 6),
        split(question_variants=["a", "b", ""]), split(question_variants="abc"),
        split(answer_outline="x"), split(answer_outline=dict(conclusion="")),
        split(answer_outline=dict(conclusion="c", points=["1", "2"], examples=[], cautions=[])),
        split(answer_outline=dict(conclusion="c", points=["1", "2", "3"], examples="x", cautions=[])),
        split(facts="x"), split(facts=["x"]), split(facts=[dict(kind="bogus", text="t")]),
        split(facts=[dict(kind="metric", text=" ")]), split(facts=[dict(kind="metric", text="t", source_span=3)]),
        split(role_tags=None),
    ]
    for data in bad:
        with pytest.raises(SplitInvalid):
            validate_split(data)


def test_build_messages_forbids_invention():
    msgs = build_messages("原文")
    assert "不创作" in msgs[0]['content'] and msgs[1]['content'].endswith("原文")


def test_locate_branches():
    assert locate(("number", "8"), "团队8人", []) is None
    assert locate(("number", "8"), "团队80人", ["8人"]) == "cross_entry"
    assert locate(("name", "星河"), "月光", []) == "suspected_fabrication"


def test_enhance_ok_stores_outline_and_variants(lib):
    store, library = lib
    mock = Mock({"H1-01": split("星河是什么", [dict(kind="metric", text="8 人", source_span="团队 8 人")]),
                 "H1-02": split(), "H1-03": split()})
    progress = run(store, library, mock, concurrency=2)
    assert progress.as_dict() == dict(status="done", total=3, done=3, enhanced=3, failed=0, error="")
    e = by_id(store, library)["H1-01"]
    assert e['enhanced'] and e['enhancement']['question'] == "星河是什么" and e['flags'] == []
    assert e['can_approve'] and [f['origin'] for f in e['facts']].count("model") == 1
    run(store, library, mock)  # 重跑替换旧的模型事实而不是追加
    assert [f['origin'] for f in by_id(store, library)["H1-01"]['facts']].count("model") == 1


def test_t2_fabricated_number_flagged_red(lib):
    store, library = lib
    mock = Mock({"H1-01": split(facts=[dict(kind="metric", text="性能提升 99%", source_span="性能提升 99%")]),
                 "H1-02": split(), "H1-03": split()})
    run(store, library, mock)
    e = by_id(store, library)["H1-01"]
    assert "suspected_fabrication" in e['flags'] and not e['can_approve'] and not e['batch_approvable']
    assert any(f['flag'] == "suspected_fabrication" for f in e['facts'])
    assert "H1-01" not in batch_approvable(store, library)
    with pytest.raises(ValueError):
        batch_approve(store, library, ["H1-01"], confirmed=True)


def test_t2_fabricated_number_in_outline_flagged(lib):
    store, library = lib
    outline = dict(conclusion="提升了 77%", points=["a", "b", "c"], examples=[], cautions=[])
    run(store, library, Mock({"H1-01": split(answer_outline=outline), "H1-02": split(), "H1-03": split()}))
    e = by_id(store, library)["H1-01"]
    assert e['flag_details'] == [dict(flag="suspected_fabrication", token="77", where="answer_outline")]


def test_t5_cross_entry_splice_flagged(lib):
    store, library = lib
    mock = Mock({"H1-01": split(), "H1-03": split(),
                 "H1-02": split(facts=[dict(kind="metric", text="团队 8 人"),
                                       dict(kind="project", text="星河项目", source_span="“星河”项目")])})
    run(store, library, mock)
    e = by_id(store, library)["H1-02"]
    assert "cross_entry" in e['flags'] and not e['can_approve']
    assert [f['flag'] for f in e['facts'] if f['origin'] == "model"] == ["cross_entry", "cross_entry"]
    assert by_id(store, library)["H1-01"]['flags'] == []


def test_model_fact_without_span_uses_text_anchor(lib):
    store, library = lib
    mock = Mock({"H1-01": split(facts=[dict(kind="role", text="负责“星河”项目"), dict(kind="org", text="某某银行")]),
                 "H1-02": split(), "H1-03": split()})
    run(store, library, mock)
    flags = [f['flag'] for f in by_id(store, library)["H1-01"]['facts'] if f['origin'] == "model"]
    assert flags == [None, "suspected_fabrication"]


def test_no_credentials_degrades(lib):
    store, library = lib
    progress = run(store, library, None)
    assert progress.failed == 3 and progress.enhanced == 0 and progress.status == "done"
    e = by_id(store, library)["H1-01"]
    assert not e['enhanced'] and e['enhance_error'] == "no_enhancer" and e['facts']
    store.review(library, "H1-01", "确认", 'approved', True)
    assert batch_approvable(store, library) == ["H1-02", "H1-03"]


def test_enhancer_failures_degrade_without_leaking(lib):
    store, library = lib
    mock = Mock({"H1-01": RuntimeError("sk-secret-token"), "H1-02": {"question": ""}, "H1-03": split()})
    progress = run(store, library, mock)
    assert (progress.enhanced, progress.failed) == (1, 2)
    entries = by_id(store, library)
    assert entries["H1-01"]['enhance_error'] == "call_failed:RuntimeError"
    assert entries["H1-02"]['enhance_error'].startswith("invalid_schema")
    assert "sk-secret" not in json.dumps(list(entries.values()), ensure_ascii=False)
    assert entries["H1-01"]['batch_approvable'] and entries["H1-03"]['enhanced']


def test_enhancement_inherited_on_reimport(lib):
    store, library = lib
    run(store, library, Mock({"H1-01": split(), "H1-02": split(), "H1-03": split()}))
    versioning.reimport(store, library, TEXT.replace("与相关团队同步", "与团队同步"))
    entries = by_id(store, library)
    assert entries["H1-01"]['enhanced'] and entries["H1-01"]['enhancement'] and not entries["H1-03"]['enhanced']


def client(tmp_path, enhancer=None):
    store = Store(tmp_path)
    ctx = type("Ctx", (), dict(store=store, root=tmp_path, token="t", bus=None, config=dict(enhancer=enhancer)))()
    app = FastAPI()
    library_enhance.register(app, ctx)
    library = store.import_text("合成.md", TEXT + D + "\n")['id']
    return TestClient(app), library


def wait_done(http, base):
    for _ in range(200):
        state = http.get(base + "/enhance").json()
        if state['status'] == "done":
            return state
        time.sleep(0.02)
    raise AssertionError("增强未完成")


def test_routes_flow(tmp_path):
    mock = Mock({k: split() for k in ("H1-01", "H1-02", "H1-03", "H1-04")})
    http, library = client(tmp_path, mock)
    base = f"/api/libraries/{library}"
    with http:
        assert http.get(base + "/enhance").json()['status'] == "idle"
        assert http.post(base + "/enhance").status_code == 200
        assert wait_done(http, base)['enhanced'] == 4
        assert http.post(base + "/enhance").status_code == 200
        wait_done(http, base)
        entries = http.get(base + "/review-entries").json()
        assert entries[0]['enhanced'] and entries[3]['flags'] == ["pending_verify"]
        assert http.get(base + "/conflicts").json() == []
        assert http.get(base + "/batch-approvable").json() == dict(ids=["H1-01", "H1-02", "H1-03"])
        bad = http.post(base + "/batch-approve", json=dict(keys=["H1-04"], confirmed=True))
        assert bad.status_code == 400 and "H1-04" in bad.json()['detail']
        assert http.post(base + "/batch-approve", json=dict(confirmed=True)).json() == dict(approved=["H1-01", "H1-02", "H1-03"])
        done = http.post(base + "/reimport", json=dict(text=TEXT.replace("同步", "沟通") + D + "\n"))
        assert done.json()['pending'] == ["H1-03", "H1-04"]
        assert http.post(base + "/reimport", json=dict(text="没有问答")).status_code == 400


def test_running_job_not_restarted(tmp_path):
    class Slow:
        calls = 0

        async def split(self, entry_text):
            Slow.calls += 1
            await asyncio.sleep(0.3)
            return split()

    http, library = client(tmp_path, Slow())
    base = f"/api/libraries/{library}"
    with http:
        assert http.post(base + "/enhance").json()['status'] == "running"
        assert http.post(base + "/enhance").json()['status'] == "running"
        wait_done(http, base)
    assert Slow.calls == 4


def test_routes_unknown_library_and_no_enhancer(tmp_path):
    http, library = client(tmp_path)
    with http:
        for method, path in (("get", "/enhance"), ("post", "/enhance"), ("get", "/review-entries"), ("get", "/conflicts"),
                             ("get", "/batch-approvable")):
            assert getattr(http, method)(f"/api/libraries/nope{path}").status_code == 404
        assert http.post("/api/libraries/nope/batch-approve", json={}).status_code == 404
        assert http.post("/api/libraries/nope/reimport", json=dict(text="x")).status_code == 404
        http.post(f"/api/libraries/{library}/enhance")
        state = wait_done(http, f"/api/libraries/{library}")
        assert state['failed'] == 4 and state['enhanced'] == 0


def test_reimport_route_returns_add_change_delete_stats(tmp_path):
    http, library = client(tmp_path)
    base = f"/api/libraries/{library}"
    with http:
        http.get(base + "/review-entries")  # 确保版本基线
        changed = TEXT.replace("与相关团队同步", "与团队沟通") + D + "\n"
        new = changed.replace(D, "") + "**H1-05｜00:05｜新题？** 参考回答：新增回答。\n"
        r = http.post(base + "/reimport", json=dict(name="新版.md", text=new))
        assert r.status_code == 200
        body = r.json()
        assert body['stats'] == dict(added=["H1-05"], changed=["H1-03"], deleted=["H1-04"], inherited=2)
        assert {"H1-03", "H1-05"} <= set(body['pending'])
        body = http.post(base + "/reimport", json=dict(name="新版.md", text=TEXT + D + "\n")).json()
        assert body['stats'] == dict(added=["H1-04"], changed=["H1-03"], deleted=["H1-05"], inherited=2)
        assert {"H1-03", "H1-04"} <= set(body['pending'])
