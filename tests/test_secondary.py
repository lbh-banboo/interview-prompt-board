import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import secondary as secondary_api
from app.core import Store
from app.library import secondary as sec
from app.library.secondary import SecondaryLibrary, parse_seed_text
from app.retrieval.index import HashEmbedder, Index
from tools import scan_seeds

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
OK, BAD = FIX / "seeds_ok", FIX / "seeds_bad"
MINS = {"tech": 2, "ops": 2, "mgmt": 1, "general": 2}


class Ctx:
    def __init__(self, store, config=None):
        self.store, self.root, self.token, self.bus, self.config = store, store.root, "t", None, config or {}


@pytest.fixture
def lib(tmp_path):
    return SecondaryLibrary(Store(tmp_path))


def approve_all(lib):
    for e in lib.candidates(["tech", "ops", "mgmt"], include_pending=True):
        assert lib.review(e["id"], "approved", True)


def test_parse_ok_fixture():
    entries, errors = parse_seed_text((OK / "tech.md").read_text("utf-8"))
    assert errors == []
    assert [e["id"] for e in entries] == ["QTECH-01", "QTECH-02"]
    assert entries[1]["roles"] == ["tech", "ops"] and entries[0]["type"] == "知识"
    assert len(entries[0]["variants"]) == 3 and len(entries[0]["points"]) == 3


def test_parse_errors_reported():
    entries, errors = parse_seed_text((BAD / "tech.md").read_text("utf-8"))
    msgs = " ".join(m for _, m in errors)
    for needle in ("变体须 3-5", "要点须 3 条", "缺少字段: 注意", "示例必须是", "类型须为"):
        assert needle in msgs
    assert len(entries) == 2


def test_parse_misc_errors():
    text = "### 坏标题\n### QA-1 x\n### QTECH-01 a\n变体: 内联\n乱写一行\n- 孤立列表\n结论: 甲\n结论: 乙\n岗位: Tech\n### QTECH-01 b\n"
    _, errors = parse_seed_text(text)
    msgs = " ".join(m for _, m in errors)
    for needle in ("题目标题格式错误", "须用 `- ` 列表", "无法识别的行", "字段重复", "岗位标签无效", "题号重复"):
        assert needle in msgs


def test_import_and_status(lib):
    r = lib.import_dir(OK)
    assert r["errors"] == [] and r["imported"] == 7 and r["files"] == 4
    assert lib.candidates(["tech"]) == []  # pending 不参与检索
    assert len(lib.candidates(["tech"], include_pending=True)) == 4
    assert lib.import_dir(OK)["unchanged"] == 7
    assert {x["role"] for x in lib.roles()} == {"tech", "ops", "mgmt", "general"}


def test_reimport_changed_resets_and_removes(lib, tmp_path):
    d = tmp_path / "seeds"
    shutil.copytree(OK, d)
    lib.import_dir(d)
    approve_all(lib)
    assert lib.review("QTECH-01", "pending", False)
    lib.review("QTECH-01", "approved", True)
    p = d / "tech.md"
    p.write_text(p.read_text("utf-8").replace("讲讲CAP理论", "讲讲CAP定理"), "utf-8")
    r = lib.import_dir(d)
    assert r["imported"] == 1
    st = {e["id"]: e["status"] for e in lib.candidates(["tech"], include_pending=True)}
    assert st["QTECH-01"] == "pending" and st["QTECH-02"] == "approved"
    p.write_text(p.read_text("utf-8").split("### QTECH-02")[0], "utf-8")
    lib.import_dir(d)
    assert [e["id"] for e in lib.candidates(["tech"], include_pending=True) if e["source"] == "tech.md"] == ["QTECH-01"]


def test_import_skips_bad_and_duplicates(lib, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    shutil.copy(BAD / "tech.md", d / "tech.md")
    shutil.copy(OK / "ops.md", d / "ops.md")
    (d / "README.md").write_text("### QX-01 不应被解析\n", "utf-8")
    (d / "dup.md").write_text((OK / "ops.md").read_text("utf-8"), "utf-8")
    r = lib.import_dir(d)
    assert any(e["error"] == "跨文件题号重复" for e in r["errors"])
    assert r["imported"] == 0 or all(e["id"] != "QX-01" for e in lib.candidates(["ops"], include_pending=True))
    ids = {e["id"] for e in lib.candidates(["tech", "ops"], include_pending=True)}
    assert "QTECH-01" not in ids and "QOPS-01" not in ids


def test_role_isolation_and_union(lib):
    lib.import_dir(OK)
    approve_all(lib)
    ids = lambda roles: {e["id"] for e in lib.candidates(roles)}
    ops = ids(["ops"])
    assert "QTECH-01" not in ops and {"QOPS-01", "QOPS-02", "QGEN-01", "QGEN-02", "QTECH-02"} <= ops  # QTECH-02 同标 ops
    assert "QTECH-01" not in ids([]) and ids([]) == {"QGEN-01", "QGEN-02"}
    assert ids(["tech", "ops"]) == ids(["tech"]) | ids(["ops"])
    assert "QMGT-01" not in ids(["tech", "ops"]) and "QMGT-01" in ids(["mgmt"])


def test_rejected_not_returned(lib):
    lib.import_dir(OK)
    approve_all(lib)
    lib.review("QOPS-01", "rejected", False)
    assert "QOPS-01" not in {e["id"] for e in lib.candidates(["ops"])}


def test_review_validation(lib):
    lib.import_dir(OK)
    with pytest.raises(ValueError):
        lib.review("QOPS-01", "approved", False)
    with pytest.raises(ValueError):
        lib.review("QOPS-01", "bogus", True)
    assert lib.review("NOPE-01", "approved", True) is False


def test_index_entries_compatible(lib):
    lib.import_dir(OK)
    approve_all(lib)
    entries = lib.index_entries(["tech"])
    e = next(x for x in entries if x["id"] == "QTECH-01")
    assert e["kind"] == "secondary" and e["label"] == "通用建议" and e["roles"] == ["tech"]
    assert "结论：" in e["outline"] and "[需本人补充：" in e["outline"]
    idx = Index(HashEmbedder())
    idx.build(entries)
    assert idx.doc("QTECH-01") is not None or idx.fts_query("CAP理论")


def test_provenance(lib):
    lib.import_dir(OK)
    e = next(x for x in lib.candidates(["tech"], include_pending=True) if x["id"] == "QTECH-01")
    assert e["source"] == "tech.md" and len(e["source_hash"]) == 64 and e["kind"] == "secondary"


def test_never_writes_facts(lib):
    with lib.store.connect() as db:
        db.execute("CREATE TABLE facts(id TEXT, text TEXT)")
    lib.import_dir(OK)
    approve_all(lib)
    lib.candidates(["tech"])
    with lib.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    with lib.store.connect() as db, pytest.raises(AssertionError):
        lib._sql(db, "INSERT INTO facts VALUES('a','b')")


def test_does_not_modify_store_tables(lib):
    with lib.store.connect() as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"secondary_entries", "secondary_roles", "libraries", "entries"} <= names


def test_api(tmp_path):
    store = Store(tmp_path)
    app = FastAPI()
    secondary_api.register(app, Ctx(store, {"seeds_dir": str(OK)}))
    c = TestClient(app)
    assert c.post("/api/secondary/import").json()["imported"] == 7
    roles = {r["role"]: r for r in c.get("/api/secondary/roles").json()}
    assert roles["tech"]["total"] == 2 and roles["tech"]["approved"] == 0
    assert c.post("/api/secondary/QTECH-01/review", json={"status": "approved"}).status_code == 400
    assert c.post("/api/secondary/QTECH-01/review", json={"status": "approved", "confirmed": True}).status_code == 200
    assert c.post("/api/secondary/NOPE-01/review", json={"status": "approved", "confirmed": True}).status_code == 404
    assert {r["role"]: r for r in c.get("/api/secondary/roles").json()}["tech"]["approved"] == 1


def test_api_default_seed_dir(tmp_path):
    app = FastAPI()
    secondary_api.register(app, Ctx(Store(tmp_path)))
    r = TestClient(app).post("/api/secondary/import").json()
    assert r["files"] >= 2 and r["errors"] == []


def test_api_entries_and_progress(tmp_path):
    app = FastAPI()
    secondary_api.register(app, Ctx(Store(tmp_path), {"seeds_dir": str(OK)}))
    c = TestClient(app)
    c.post("/api/secondary/import")

    rows = c.get("/api/secondary/entries").json()
    assert len(rows) == 7
    e = next(x for x in rows if x["id"] == "QTECH-01")
    assert e["roles"] == ["tech"] and e["status"] == "pending" and e["label"] == "通用建议"
    assert e["source"] == "tech.md" and len(e["points"]) == 3 and e["scan"] == []
    assert {x["id"] for x in c.get("/api/secondary/entries?role=ops").json()} == {"QTECH-02", "QOPS-01", "QOPS-02"}
    assert c.get("/api/secondary/entries?status=bogus").status_code == 400
    assert c.post("/api/secondary/QTECH-01/review", json={"status": "approved", "confirmed": True}).status_code == 200
    assert {x["id"] for x in c.get("/api/secondary/entries?status=approved").json()} == {"QTECH-01"}

    p = c.get("/api/secondary/progress").json()
    assert (p["total"], p["approved"], p["pending"], p["reviewed"]) == (7, 1, 6, 1)
    roles = {r["role"]: r for r in p["by_role"]}
    assert roles["ops"]["total"] == 3 and roles["tech"]["approved"] == 1
    assert c.get("/api/secondary/progress").status_code == 200


def test_api_approve_batch(tmp_path):
    app = FastAPI()
    secondary_api.register(app, Ctx(Store(tmp_path), {"seeds_dir": str(OK)}))
    c = TestClient(app)
    c.post("/api/secondary/import")
    H = {"x-local-token": "t"}
    url = "/api/secondary/approve-batch"
    assert c.post(url, json={"ids": ["QTECH-01"], "confirmed": True}).status_code == 403
    assert c.post(url, headers=H, json={"ids": ["QTECH-01"], "confirmed": False}).status_code == 400
    assert c.post(url, headers=H, json={"ids": [], "confirmed": True}).status_code == 400
    r = c.post(url, headers=H, json={"ids": ["QTECH-01", "QOPS-01"], "confirmed": True})
    assert r.status_code == 200 and r.json()["approved"] == 2
    assert c.get("/api/secondary/progress").json()["approved"] == 2
    r = c.post(url, headers=H, json={"ids": ["QTECH-01"], "confirmed": True})
    assert r.status_code == 409 and r.json()["rejected"][0]["reasons"] == ["非待审状态"]
    r = c.post(url, headers=H, json={"ids": ["QGEN-01", "NOPE-01"], "confirmed": True})
    assert r.status_code == 409 and {x["id"] for x in r.json()["rejected"]} == {"NOPE-01"}
    assert "QGEN-01" in {x["id"] for x in c.get("/api/secondary/entries?status=pending").json()}  # 整批不部分写入


def test_api_batch_blocks_scan_marks(tmp_path):
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "tech.md").write_text(
        "### QSCAN-01 如何做增长\n变体:\n- 变体一\n- 变体二\n- 变体三\n结论: 我们做过推荐系统。\n"
        "要点:\n- 要点一。\n- 要点二。\n- 要点三。\n示例: [需本人补充：相关经历]\n注意: 一句话提醒。\n"
        "岗位: tech\n类型: 知识\n", "utf-8")
    app = FastAPI()
    secondary_api.register(app, Ctx(Store(tmp_path / "data"), {"seeds_dir": str(seeds)}))
    c = TestClient(app)
    assert c.post("/api/secondary/import").json()["imported"] == 1
    e = c.get("/api/secondary/entries").json()[0]
    assert {m["kind"] for m in e["scan"]} == {"第一人称经历"}
    r = c.post("/api/secondary/approve-batch", headers={"x-local-token": "t"},
               json={"ids": ["QSCAN-01"], "confirmed": True})
    assert r.status_code == 409 and "合规扫描命中" in r.json()["rejected"][0]["reasons"][0]
    assert c.get("/api/secondary/progress").json()["approved"] == 0


# ---- 扫描脚本 ----
def test_scan_ok_fixture():
    problems, stats = scan_seeds.scan_dir(OK, minimums=MINS)
    assert problems == [] and stats["tech.md"] == 2


def test_scan_bad_fixture():
    problems, _ = scan_seeds.scan_dir(BAD, minimums={"tech": 1})
    text = "\n".join(problems)
    for needle in ("百分比", "金额", "具体年份", "第一人称经历", "公司名", "具体人名", "示例非占位", "结构", "业绩数字"):
        assert needle in text, needle


def test_scan_count_and_tags(tmp_path):
    shutil.copy(OK / "tech.md", tmp_path / "tech.md")
    problems, _ = scan_seeds.scan_dir(tmp_path)
    assert any("题数 2 < 60" in p for p in problems)
    (tmp_path / "pm.md").write_text((OK / "tech.md").read_text("utf-8").replace("QTECH", "QPM"), "utf-8")
    problems, _ = scan_seeds.scan_dir(tmp_path, minimums={"tech": 2, "pm": 1})
    assert any("岗位标签须包含 pm" in p for p in problems)
    dup = tmp_path / "pm.md"
    dup.write_text((OK / "tech.md").read_text("utf-8").replace("岗位: tech", "岗位: pm, tech"), "utf-8")
    problems, _ = scan_seeds.scan_dir(tmp_path, minimums={"tech": 2, "pm": 1})
    assert any("重复" in p for p in problems)
    assert scan_seeds.scan_dir(tmp_path / "none")[0]


@pytest.mark.parametrize("line,kind", [
    ("效果提升30%", "百分比"), ("预算 500万元", "金额"), ("2022 年完成", "具体年份"),
    ("我们做过推荐系统", "第一人称经历"), ("我在某某公司工作", "第一人称经历"),
    ("成本降低了3倍", "业绩数字"), ("DAU 为 100", "业绩数字"), ("张经理说", "具体人名"),
    ("字节的做法", "公司名"), ("北京某某有限公司", "公司名"),
])
def test_scan_text_violations(line, kind):
    assert kind in {k for _, k, _ in scan_seeds.scan_text(line)}


@pytest.mark.parametrize("line", [
    "TCP 三次握手", "HTTP 状态码 200 表示成功", "Redis 默认端口 6379", "示例: [需本人补充：2023 年 30% 的项目经历]",
    "周总结要写清楚", "建议先说明背景，可以说明负责的范围", "我们可以这样组织回答",
])
def test_scan_text_clean(line):
    assert scan_seeds.scan_text(line) == []


def test_scan_custom_whitelist(tmp_path):
    assert scan_seeds.scan_text("增长30%", whitelist=scan_seeds.WHITELIST + ["30%"]) == []


def test_cli_exit_codes(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "scan_seeds.py"), *a], capture_output=True, text=True)
    wl = tmp_path / "wl.txt"
    wl.write_text("\n", "utf-8")
    ok = run(str(OK), "--min", "tech=2", "--min", "ops=2", "--min", "mgmt=1", "--min", "general=2", "--whitelist", str(wl))
    assert ok.returncode == 0 and "0 项违规" in ok.stdout
    bad = run(str(BAD), "--min", "tech=1")
    assert bad.returncode == 1 and "违规:" in bad.stdout


def test_real_seeds_pass_scan():
    problems, stats = scan_seeds.scan_dir(ROOT / "seeds" / "secondary")
    mine = [p for p in problems if p.startswith(("tech.md", "general.md"))]
    assert mine == []
    assert stats["tech.md"] >= 60 and stats["general.md"] >= 40


def test_real_seeds_import(lib):
    r = lib.import_dir(ROOT / "seeds" / "secondary")
    assert r["imported"] >= 100
    assert not [e for e in r["errors"] if e["source"] in ("tech.md", "general.md")]
