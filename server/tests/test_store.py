"""存储：建表、从结构版本 1 升级、项目增删查、分析存取（压缩后取回逐字节相同）、对比基准和差异、只留最近几次（另留
基准那次）、手工项、同一目录不能加两次、并发写。"""

from __future__ import annotations

import dataclasses
import gc
import gzip
import hashlib
import json
import os
import sqlite3
import stat
import threading
import warnings

import pytest
from pystepmap_server import store as store_module
from pystepmap_server.store import (
    SCHEMA_VERSION,
    DuplicateRootError,
    NewAnalysis,
    NewRun,
    OverlapRootError,
    ProjectMissingError,
    Store,
    StoreError,
)

GRAPH = json.dumps({"schema_version": 1, "root": "/x", "lines": [], "名字": "中文"}, ensure_ascii=False, indent=2)


def analysis(n: int = 0, graph: str = GRAPH) -> NewAnalysis:
    return NewAnalysis(engine_version="0.1.0", summary={"functions": n, "lines": 1}, graph_json=graph)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home" / "pystepmap.db")


NEW_COLUMNS = ["config_json", "graph_sha", "base_id", "file_lines_json", "diff_json"]
RUN_COLUMNS = ["id", "project_id", "analysis_id", "started_at", "finished_at", "command", "status", "exit_code",
               "summary_json", "paths_size", "tests_json", "paths_index", "functions_gz", "paths_gz", "scope_json"]


def columns(path, table: str = "analyses") -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"pragma table_info({table})")]
    finally:
        con.close()


def test_opening_creates_tables_with_version(tmp_path):
    path = tmp_path / "a" / "b" / "pystepmap.db"
    Store(path)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    assert {"projects", "analyses", "line_overrides", "runs"} <= tables
    assert con.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION == 6
    con.close()
    cols = columns(path)
    assert set(NEW_COLUMNS) <= set(cols) and cols[-1] == "graph_gz"  # 大字段在最后一列
    assert columns(path, "projects")[-3:] == ["test_command", "test_confirmed", "mcp_tests"]
    assert columns(path, "runs") == RUN_COLUMNS  # 大字段在最后一列
    Store(path)  # 再打开一次：不重复建表、不报错


def test_newer_schema_is_refused_and_the_file_is_left_alone(tmp_path):
    path = tmp_path / "pystepmap.db"
    con = sqlite3.connect(path)  # 别的（更新的）版本建的库：普通的回滚日志模式，结构版本 3
    con.execute("create table t (x)")
    con.execute(f"pragma user_version = {SCHEMA_VERSION + 1}")
    con.commit()
    con.close()
    before = path.read_bytes()
    with pytest.raises(StoreError, match="更新版本"):
        Store(path)
    assert path.read_bytes() == before  # 没有改成 WAL、没有建表、没有改版本号


# 结构版本 1 的建表语句（阶段 4 的库），原样
V1_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    graph_size INTEGER NOT NULL,
    graph_gz BLOB NOT NULL
);
CREATE INDEX analyses_by_project ON analyses(project_id, id);
CREATE TABLE line_overrides (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    line_sid TEXT NOT NULL,
    title TEXT,
    group_name TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, line_sid)
);
"""


def make_v1_database(path, graphs: list[str]) -> None:
    """按版本 1 的建表语句建库：项目 p1 依次分析了 graphs，另有一次已删掉的分析（AUTOINCREMENT 记着它的 id）、
    一条手工项、一个没有分析的项目 p2。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    con.execute("insert into projects values ('p1', '项目一', '/srv/p1', '{\"exclude\": [\"gen\"]}', 't0', 't1')")
    con.execute("insert into projects values ('p2', '项目二', '/srv/p2', '{}', 't0', 't0')")
    for i, g in enumerate([*graphs, "deleted"]):
        raw = g.encode("utf-8")
        con.execute("insert into analyses (project_id, created_at, engine_version, summary_json, graph_size, graph_gz) "
                    "values ('p1', ?, '0.1.0', ?, ?, ?)",
                    (f"2026-11-0{i + 1}", json.dumps({"functions": i}), len(raw), gzip.compress(raw)))
    con.execute("delete from analyses where id = ?", (len(graphs) + 1,))
    con.execute("insert into line_overrides values ('p1', 'line:a.py::f', '手工标题', '手工分组', 't2')")
    con.execute("pragma user_version = 1")
    con.commit()
    con.close()


def test_version_1_database_is_upgraded(tmp_path):
    path = tmp_path / "home" / "pystepmap.db"
    g1, g2 = GRAPH, GRAPH.replace("中文", "改过")
    make_v1_database(path, [g1, g2, g2])  # 第 2、3 次的图相同
    store = Store(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == 6
    con.close()
    cols = columns(path)
    assert set(NEW_COLUMNS) <= set(cols) and cols[-1] == "graph_gz"
    analyses = store.list_analyses("p1")
    assert [a["id"] for a in analyses] == [3, 2, 1]
    assert [a["base_id"] for a in analyses] == [1, 1, None]  # 和它之前最近一次图不同的分析
    assert [a["has_diff"] for a in analyses] == [False] * 3  # 升级时不算差异
    assert [a["summary"] for a in analyses] == [{"functions": 2}, {"functions": 1}, {"functions": 0}]
    assert [a["created_at"] for a in analyses] == ["2026-11-03", "2026-11-02", "2026-11-01"]
    assert [a["graph_size"] for a in analyses] == [len(g.encode()) for g in (g2, g2, g1)]
    for aid, g in [(1, g1), (2, g2), (3, g2)]:
        assert store.graph_json(aid) == g
        extra = store.analysis_extra(aid)
        assert extra == {"config": None, "graph_sha": hashlib.sha256(g.encode()).hexdigest(),
                         "file_lines": None, "base_id": extra["base_id"], "diff": None, "acknowledged": False}
    assert store.get_project("p1") == {"id": "p1", "name": "项目一", "root": "/srv/p1", "config": {"exclude": ["gen"]},
                                       "created_at": "t0", "updated_at": "t1", "test_command": None,
                                       "test_confirmed": False, "mcp_tests": False}
    assert [p["id"] for p in store.list_projects()] == ["p1", "p2"]
    assert store.list_projects()[0]["analysis"]["id"] == 3 and store.list_projects()[1]["analysis"] is None
    assert store.get_overrides("p1") == {"line:a.py::f": {"title": "手工标题", "group": "手工分组"}}
    assert store.add_analysis("p1", analysis()) == 5  # 删掉过的 id 4 不再用
    Store(path)  # 再打开一次：已经是版本 2，不再升级
    assert [a["base_id"] for a in store.list_analyses("p1")][1:] == [1, 1, None]
    assert store.delete_project("p1") and store.list_analyses("p1") == []  # 外键照常：分析随项目删掉


def test_upgrade_is_all_or_nothing(tmp_path, monkeypatch):
    """升级中途出错（这里在算第二张图的指纹时）：整个事务回滚，库还是原样的版本 1。"""
    path = tmp_path / "pystepmap.db"
    make_v1_database(path, [GRAPH, GRAPH])
    seen: list[bytes] = []

    def boom(gz: bytes) -> str | None:
        seen.append(gz)
        if len(seen) == 2:
            raise RuntimeError("升级中途出错")
        return "x"

    monkeypatch.setattr(store_module, "_graph_sha", boom)
    with pytest.raises(RuntimeError, match="升级中途出错"):
        Store(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == 1
    assert [r[1] for r in con.execute("pragma table_info(analyses)")] == [
        "id", "project_id", "created_at", "engine_version", "summary_json", "graph_size", "graph_gz"]
    assert con.execute("select count(*) from analyses").fetchone()[0] == 2
    con.close()
    monkeypatch.undo()
    assert [a["base_id"] for a in Store(path).list_analyses("p1")] == [None, None]  # 再打开时照常升级


def test_upgrade_drops_analyses_whose_project_is_gone(tmp_path, caplog):
    """版本 1 的库里有没有对应项目的分析（外键检查关着时删了项目）：升级时丢掉它们并记一条日志，不让升级失败。"""
    path = tmp_path / "pystepmap.db"
    make_v1_database(path, [GRAPH, GRAPH])
    con = sqlite3.connect(path)  # 默认不检查外键
    raw = GRAPH.encode()
    con.execute("insert into analyses (project_id, created_at, engine_version, summary_json, graph_size, graph_gz) "
                "values ('ghost', 't', '0.1.0', '{}', ?, ?)", (len(raw), gzip.compress(raw)))
    con.commit()
    con.close()
    with caplog.at_level("WARNING", logger="pystepmap_server.store"):
        store = Store(path)
    assert [a["id"] for a in store.list_analyses("p1")] == [2, 1]
    con = sqlite3.connect(path)
    assert con.execute("select count(*) from analyses where project_id = 'ghost'").fetchone()[0] == 0
    assert con.execute("pragma foreign_key_check").fetchall() == []
    con.close()
    assert "1 次没有对应项目的分析" in caplog.text
    assert store.add_analysis("p1", analysis()) == 5  # 丢掉的那次的 id（4）也不再用


def test_upgrade_keeps_a_copy_of_the_old_database(tmp_path, loose_umask):
    """升级之前先把库复制成同目录的 pystepmap.db.bak-v<旧版本>（0600）：旧版本的代码还能打开它，内容和升级前一样。"""
    home = tmp_path / "home"
    path = home / "pystepmap.db"
    make_v1_database(path, [GRAPH, GRAPH.replace("中文", "改过")])
    Store(path)
    bak = home / "pystepmap.db.bak-v1"
    assert bak.is_file() and mode(bak) == 0o600
    con = sqlite3.connect(bak)
    try:
        assert con.execute("pragma user_version").fetchone()[0] == 1
        assert [r[1] for r in con.execute("pragma table_info(analyses)")] == [
            "id", "project_id", "created_at", "engine_version", "summary_json", "graph_size", "graph_gz"]
        assert [r[0] for r in con.execute("select id from analyses order by id")] == [1, 2]
        assert gzip.decompress(con.execute("select graph_gz from analyses where id = 1").fetchone()[0]).decode() == GRAPH
        assert con.execute("select title from line_overrides").fetchall() == [("手工标题",)]
        assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert sorted(f.name for f in home.iterdir() if ".bak" in f.name) == ["pystepmap.db.bak-v1"]  # 没有留下半截的临时文件


def test_no_copy_for_a_new_or_current_database_and_an_existing_copy_is_kept(tmp_path):
    home = tmp_path / "home"
    Store(home / "pystepmap.db")
    Store(home / "pystepmap.db")
    assert [f.name for f in home.iterdir() if ".bak" in f.name] == []  # 新库、已经是最新版本：不复制
    other = tmp_path / "other"
    make_v1_database(other / "pystepmap.db", [GRAPH])
    (other / "pystepmap.db.bak-v1").write_bytes(b"earlier copy")
    Store(other / "pystepmap.db")
    assert (other / "pystepmap.db.bak-v1").read_bytes() == b"earlier copy"  # 已经有了：不覆盖


def test_upgrade_is_not_done_when_the_copy_fails(tmp_path, monkeypatch):
    """复制失败（磁盘满、没有权限）：不升级，库保持版本 1，报中文的错误，不留下半截的副本。"""
    path = tmp_path / "pystepmap.db"
    make_v1_database(path, [GRAPH])

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store_module, "_copy_database", full)
    with pytest.raises(StoreError, match="升级之前没能备份.*pystepmap.db.bak-v1.*No space left"):
        Store(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == 1
    con.close()
    assert not (tmp_path / "pystepmap.db.bak-v1").exists()
    monkeypatch.undo()
    Store(path).close()
    assert (tmp_path / "pystepmap.db.bak-v1").is_file()


def test_upgrade_tolerates_a_graph_that_cannot_be_decompressed(tmp_path):
    """库里有一张解不开的图（坏了的数据）：那一行没有指纹，不挡住整个升级。"""
    path = tmp_path / "pystepmap.db"
    make_v1_database(path, [GRAPH, GRAPH])
    con = sqlite3.connect(path)
    con.execute("update analyses set graph_gz = x'00' where id = 1")
    con.commit()
    con.close()
    store = Store(path)
    assert store.analysis_extra(1)["graph_sha"] is None
    assert store.analysis_extra(2)["graph_sha"] == hashlib.sha256(GRAPH.encode()).hexdigest()
    assert store.analysis_extra(2)["base_id"] == 1  # 指纹不明的算作不同


# 结构版本 2 的建表语句（阶段 5 的库），原样
V2_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    graph_size INTEGER NOT NULL,
    config_json TEXT,
    graph_sha TEXT,
    base_id INTEGER,
    file_lines_json TEXT,
    diff_json TEXT,
    graph_gz BLOB NOT NULL
);
CREATE INDEX analyses_by_project ON analyses(project_id, id);
CREATE TABLE line_overrides (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    line_sid TEXT NOT NULL,
    title TEXT,
    group_name TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, line_sid)
);
"""


def make_v2_database(path) -> None:
    """按版本 2 的建表语句建库：项目 p1 三次分析——第 1 次、第 2 次（以第 1 次为基准、有差异）、第 3 次（标为已看的
    记号：base_id 指向自己）；一条手工项；一个没有分析的项目 p2。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(V2_SCHEMA)
    con.execute("insert into projects values ('p1', '项目一', '/srv/p1', '{\"exclude\": [\"gen\"]}', 't0', 't1')")
    con.execute("insert into projects values ('p2', '项目二', '/srv/p2', '{}', 't0', 't0')")
    raw = GRAPH.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    for i, (base, diff) in enumerate([(None, None), (1, '{"version": 1, "functions": {}, "lines": []}'), (3, None)], 1):
        con.execute("insert into analyses (project_id, created_at, engine_version, summary_json, graph_size, config_json, "
                    "graph_sha, base_id, file_lines_json, diff_json, graph_gz) "
                    "values ('p1', ?, '0.1.0', ?, ?, '{}', ?, ?, '{}', ?, ?)",
                    (f"2026-11-1{i}", json.dumps({"functions": i}), len(raw), sha, base, diff, gzip.compress(raw)))
    con.execute("insert into line_overrides values ('p1', 'line:a.py::f', '手工标题', '手工分组', 't2')")
    con.execute("pragma user_version = 2")
    con.commit()
    con.close()


def dump(path) -> dict:
    """库里三张旧表的全部内容（按主键排序），用来断言升级后原样。"""
    con = sqlite3.connect(path)
    try:
        return {t: con.execute(f"select * from {t} order by 1, 2").fetchall()
                for t in ("analyses", "line_overrides")} | {
            "projects": con.execute("select id, name, root, config_json, created_at, updated_at from projects "
                                    "order by id").fetchall()}
    finally:
        con.close()


def test_version_2_database_is_upgraded_and_its_data_kept(tmp_path, loose_umask):
    """阶段 5 的库（含 base_id 指向自己的"标为已看"记号）升级到 3：旧数据逐行不变，项目多两列（没填命令、没确认），
    多一张空的 runs 表；升级前复制成 pystepmap.db.bak-v2，那份还是版本 2。"""
    home = tmp_path / "home"
    path = home / "pystepmap.db"
    make_v2_database(path)
    before = dump(path)
    store = Store(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == 6
    assert con.execute("select count(*) from runs").fetchone()[0] == 0
    con.close()
    assert dump(path) == before
    assert columns(path, "runs") == RUN_COLUMNS
    assert store.get_project("p1")["test_command"] is None and store.get_project("p1")["test_confirmed"] is False
    assert store.analysis_extra(3)["acknowledged"] is True and store.analysis_extra(2)["base_id"] == 1
    assert [a["has_diff"] for a in store.list_analyses("p1")] == [False, True, False]
    bak = home / "pystepmap.db.bak-v2"
    assert bak.is_file() and mode(bak) == 0o600
    con = sqlite3.connect(bak)
    try:
        assert con.execute("pragma user_version").fetchone()[0] == 2
        assert "runs" not in {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    finally:
        con.close()
    assert dump(bak) == before
    Store(path).close()  # 再打开一次：已经是最新版本，不再升级
    assert store.add_run("p1", new_run()) >= 1


def test_version_2_upgrade_is_all_or_nothing(tmp_path, monkeypatch):
    path = tmp_path / "pystepmap.db"
    make_v2_database(path)

    def boom(con):
        con.execute("ALTER TABLE projects ADD COLUMN test_command TEXT")
        raise RuntimeError("升级中途出错")

    monkeypatch.setitem(store_module._MIGRATIONS, 2, boom)
    with pytest.raises(RuntimeError, match="升级中途出错"):
        Store(path)
    con = sqlite3.connect(path)
    assert con.execute("pragma user_version").fetchone()[0] == 2
    assert "test_command" not in [r[1] for r in con.execute("pragma table_info(projects)")]
    con.close()


# ---------- 测试命令、运行 ----------

SUMMARY = {"tests": 9, "passed": 9, "failed": 0, "skipped": 0, "errors": 0, "seconds": 1.5,
           "verify": {"ok": True, "mismatches": {"edge": 0, "end": 0, "call": 0}}, "note": ""}
TESTS = [{"test": "tests/test_a.py::test_x", "outcome": "passed", "seconds": 0.1, "cases": 1, "functions": 3}]


EMPTY_PATHS = {"version": 1, "tests": []}


def new_run(status: str = "ok", paths: dict | None = EMPTY_PATHS, analysis_id: int | None = 1,
            command: str = "pytest -q") -> NewRun:
    return NewRun(analysis_id=analysis_id, started_at="2026-11-17T01:00:00+00:00",
                  finished_at="2026-11-17T01:00:02+00:00", command=command, status=status,
                  exit_code=0 if status == "ok" else None, summary=SUMMARY, tests=TESTS if paths else None,
                  paths=paths)


def a_test(name: str, filler: str) -> dict:
    return {"test": name, "outcome": "passed", "seconds": 0.5, "cases": 1,
            "functions": {"a.py::f": {"function": "a.py::f", "executions": 2, "nodes": {"start": {"visits": 2, "ns": 9}},
                                      "edges": {}, "exits": {"end:1|return|": 2}, "first_path": [filler] * 50,
                                      "off_graph": 0, "has_time": True}}}


def test_test_command_and_its_confirmation(store):
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    assert store.get_test_command(pid) == {"command": None, "confirmed": False}
    assert store.set_test_command(pid, "pytest -q") == {"command": "pytest -q", "confirmed": False}
    assert store.confirm_test_command(pid, "pytest -x") is False  # 确认的不是现在这条：不算
    assert store.confirm_test_command(pid, "pytest -q") is True
    assert store.get_test_command(pid) == {"command": "pytest -q", "confirmed": True}
    p = store.get_project(pid)
    assert (p["test_command"], p["test_confirmed"]) == ("pytest -q", True)
    assert store.set_test_command(pid, "pytest -q") == {"command": "pytest -q", "confirmed": True}  # 没改：仍确认过
    assert store.set_test_command(pid, "pytest -x") == {"command": "pytest -x", "confirmed": False}  # 改了：清零
    store.confirm_test_command(pid, "pytest -x")
    assert store.set_test_command(pid, None) == {"command": None, "confirmed": False}
    assert store.confirm_test_command(pid, None) is False
    assert store.get_test_command("nope") is None
    with pytest.raises(ProjectMissingError):
        store.set_test_command("nope", "pytest")
    assert store.confirm_test_command("nope", "pytest") is False


def test_runs_add_list_get_paths_and_cascade(store):
    pid, aid = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    other, _ = store.add_project(name="q", root="/q", config={}, analysis=analysis())
    tests = [a_test("", "收集"), a_test("tests/a.py::t", "中文"), a_test("tests/b.py::u", "x")]
    tests[2]["functions"]["b.py::g"] = {**tests[2]["functions"]["a.py::f"], "function": "b.py::g"}
    paths = {"version": 1, "shapes": {"a.py::f": {"shape": "s1", "dups": []}, "b.py::g": {"shape": "s2", "dups": ["x"]},
                                      "c.py::h": {"shape": "s3", "dups": []}},
             "function_dups": ["d.py::k"], "tests": tests}
    first = store.add_run(pid, new_run(paths=paths, analysis_id=aid))
    second = store.add_run(pid, new_run(status="timeout", paths=None, analysis_id=aid))
    assert second > first
    listed = store.list_runs(pid)
    assert [r["id"] for r in listed] == [second, first]  # 新的在前
    assert set(listed[0]) == {"id", "analysis_id", "started_at", "finished_at", "command", "status", "exit_code",
                              "summary", "paths_size", "scope"}
    gz = store.run_paths_gz(pid, first)
    assert gz[:2] == b"\x1f\x8b" and json.loads(gzip.decompress(gz)) == paths  # 一个完整的 gzip，解开就是原样的 JSON
    assert listed[1] == {"id": first, "analysis_id": aid, "started_at": "2026-11-17T01:00:00+00:00",
                         "finished_at": "2026-11-17T01:00:02+00:00", "command": "pytest -q", "status": "ok",
                         "exit_code": 0, "summary": SUMMARY, "paths_size": len(gzip.decompress(gz)), "scope": None}
    assert listed[0]["status"] == "timeout" and listed[0]["paths_size"] is None
    assert store.latest_run(pid)["id"] == second and store.latest_run(other) is None
    got = store.get_run(pid, first)
    assert got == {**listed[1], "tests": TESTS}
    assert store.get_run(pid, second)["tests"] == []
    assert store.get_run(other, first) is None and store.get_run(pid, 999) is None  # 别的项目的运行：没有
    assert store.run_paths_gz(pid, second) is None and store.run_paths_gz(other, first) is None
    # 一个测试的路径单独取：只读、只解压那一段，外加运行一级的小项（shapes 只留这个测试里的函数的）
    assert store.run_test_paths(pid, first, "tests/a.py::t") == {
        "version": 1, "shapes": {"a.py::f": {"shape": "s1", "dups": []}}, "function_dups": ["d.py::k"], "tests": [tests[1]]}
    assert store.run_test_paths(pid, first, "tests/b.py::u")["shapes"] == {k: paths["shapes"][k] for k in ("a.py::f", "b.py::g")}
    assert store.run_test_paths(pid, first, "")["tests"] == [tests[0]]
    assert store.run_test_paths(pid, first, "tests/c.py::nope") is None
    assert store.run_test_paths(pid, second, "") is None and store.run_test_paths(other, first, "") is None
    with pytest.raises(ProjectMissingError):
        store.add_run("nope", new_run())
    assert store.delete_project(pid)
    con = sqlite3.connect(store.path)
    assert con.execute("select count(*) from runs").fetchone()[0] == 0  # 随项目删掉
    con.close()


def test_runs_keep_their_row_when_the_analysis_is_pruned(store):
    pid, aid = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    rid = store.add_run(pid, new_run(analysis_id=aid))
    for i in range(3):
        store.add_analysis(pid, analysis(i))
    store.prune_analyses(pid, keep=1)
    assert store.get_run(pid, rid)["analysis_id"] == aid  # 那次分析已经清理掉了，运行照样在


def test_prune_runs_keeps_the_latest(store):
    a, _ = store.add_project(name="a", root="/a", config={}, analysis=analysis())
    b, _ = store.add_project(name="b", root="/b", config={}, analysis=analysis())
    ids = [store.add_run(a, new_run()) for _ in range(7)]
    store.add_run(b, new_run())
    assert store.prune_runs(a, keep=5) == 2
    assert [r["id"] for r in store.list_runs(a)] == list(reversed(ids[2:]))
    assert store.prune_runs(a, keep=5) == 0 and len(store.list_runs(b)) == 1


def test_listing_runs_does_not_read_the_paths(store, monkeypatch):
    """列运行、取一次运行的信息不读 paths_gz（可能几 MB）：查询里不出现这一列。"""
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    rid = store.add_run(pid, new_run())
    seen: list[str] = []
    real = store._connect

    def spy():
        cm = real()
        con = cm.__enter__()
        con.set_trace_callback(seen.append)
        return _Wrapped(cm, con)

    monkeypatch.setattr(store, "_connect", spy)
    store.list_runs(pid)
    store.latest_run(pid)
    store.get_run(pid, rid)
    assert seen and not any("paths_gz" in s for s in seen)
    assert any("tests_json" in s for s in seen)  # 取一次运行时读每个测试的那一列
    seen.clear()
    store.run_paths_gz(pid, rid)
    assert any("paths_gz" in s for s in seen)


def test_one_test_is_read_without_the_rest_of_the_blob(store, monkeypatch):
    """很多测试的运行（真实项目几百个测试、路径几十 MB）：一个测试的路径只解压它那一段，其余部分不碰；整个压缩包
    仍是标准的单个 gzip（接口原样发出去）。按测试切开让压缩率变差多少，在真实的记录上量（报告里）：这里的测试几乎
    一模一样，跨测试压缩的效果被放大了，不拿来比。"""
    tests = [a_test(f"tests/test_{i:03d}.py::test_{i}", f"step:{i:08x}") for i in range(300)]
    paths = {"version": 1, "shapes": {f"f{i}.py::f": {"shape": f"{i:040x}", "dups": []} for i in range(2000)},
             "function_dups": [], "tests": tests}
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    rid = store.add_run(pid, new_run(paths=paths))
    gz = store.run_paths_gz(pid, rid)
    assert json.loads(gzip.decompress(gz)) == paths
    inflated: list[int] = []
    real = store_module.zlib.decompressobj

    def spy(*args):
        d = real(*args)
        return _Counting(d, inflated)

    monkeypatch.setattr(store_module.zlib, "decompressobj", spy)
    assert store.run_test_paths(pid, rid, tests[150]["test"])["tests"] == [tests[150]]
    one = len(json.dumps(tests[150], ensure_ascii=False, separators=(",", ":")).encode())
    small = sum(len(json.dumps(paths[k], ensure_ascii=False, separators=(",", ":")).encode())
                for k in ("version", "shapes", "function_dups"))
    assert sum(inflated) == one + small  # 这个测试那一段，加上运行一级的几个小项


class _Counting:
    def __init__(self, d, seen: list[int]):
        self.d, self.seen = d, seen

    def decompress(self, data: bytes) -> bytes:
        out = self.d.decompress(data)
        self.seen.append(len(out))
        return out

    def __getattr__(self, name):
        return getattr(self.d, name)


class _Wrapped:
    def __init__(self, cm, con):
        self.cm, self.con = cm, con

    def __enter__(self):
        return self.con

    def __exit__(self, *exc):
        return self.cm.__exit__(*exc)


def test_new_columns_round_trip_and_analysis_extra(store):
    config, lines = {"exclude": ["gen"]}, {"a.py": ["line:a.py::f"]}
    diff = {"version": 1, "functions": {}, "lines": [{"sid": "line:a.py::f"}]}
    pid, first = store.add_project(name="p", root="/p", config=config,
                                   analysis=NewAnalysis("0.1.0", {}, GRAPH, config=config, file_lines=lines))
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, config=config, file_lines=lines,
                                                 base_id=first, diff=diff))
    assert store.analysis_extra(first) == {"config": config, "graph_sha": hashlib.sha256(GRAPH.encode()).hexdigest(),
                                           "file_lines": lines, "base_id": None, "diff": None, "acknowledged": False}
    assert store.analysis_extra(second)["base_id"] == first and store.analysis_extra(second)["diff"] == diff
    assert store.analysis_extra(999) is None
    latest = store.latest_analysis(pid)
    assert (latest["id"], latest["base_id"], latest["has_diff"]) == (second, first, True)
    assert [(a["base_id"], a["has_diff"]) for a in store.list_analyses(pid)] == [(first, True), (None, False)]
    item = store.list_projects()[0]["analysis"]
    assert set(item) == {"id", "created_at", "engine_version", "summary", "graph_size", "base_id", "has_diff"}
    assert (item["base_id"], item["has_diff"]) == (first, True)
    # 没有给配置等新字段（旧的调用方式）：新列为空，指纹照算
    third = store.add_analysis(pid, analysis())
    assert store.analysis_extra(third) == {"config": None, "graph_sha": hashlib.sha256(GRAPH.encode()).hexdigest(),
                                           "file_lines": None, "base_id": None, "diff": None, "acknowledged": False}


def test_clear_base(store):
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    third = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    assert store.clear_base(pid) is True
    assert (store.latest_analysis(pid)["base_id"], store.latest_analysis(pid)["has_diff"]) == (None, False)
    assert store.analysis_extra(third)["diff"] is None
    assert store.analysis_extra(second)["base_id"] == first  # 只动最新一次
    assert store.clear_base(pid) is True  # 已经清过：照样成功
    assert store.clear_base("nope") is False


def test_clear_base_only_when_the_latest_is_the_one_that_was_seen(store):
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    assert store.clear_base(pid, analysis_id=first) is False  # 看过的是 first，最新的已经是 second
    assert store.latest_analysis(pid)["has_diff"] is True
    assert store.clear_base(pid, analysis_id=second) is True
    assert store.latest_analysis(pid)["has_diff"] is False


def test_acknowledged_analysis_is_marked_and_reported_without_a_base(store):
    """标为已看：那次分析记成"以自己为基准"（base_id = 自己的 id），对外 base_id 为 None、没有差异；analysis_extra 的
    acknowledged 为真。保存新分析时也可以直接带这个记号（没改流程的重新分析接着带）。"""
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    assert store.clear_base(pid, analysis_id=second) is True
    con = sqlite3.connect(store.path)
    assert con.execute("select base_id, diff_json from analyses where id = ?", (second,)).fetchone() == (second, None)
    con.close()
    head = store.latest_analysis(pid)
    assert (head["base_id"], head["has_diff"]) == (None, False)
    extra = store.analysis_extra(second)
    assert (extra["base_id"], extra["diff"], extra["acknowledged"]) == (None, None, True)
    assert store.latest_diff(pid) == {"head": head, "base": None, "diff": None, "baseline": None}
    third = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, acknowledged=True))
    assert store.analysis_extra(third)["acknowledged"] is True and store.latest_analysis(pid)["base_id"] is None
    assert store.analysis_extra(first)["acknowledged"] is False
    assert store.prune_analyses(pid, keep=1) == 2


def test_latest_diff_says_what_the_baseline_is(store):
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    created = store.get_project(pid)["created_at"]
    store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    assert store.latest_diff(pid)["baseline"] == {"kind": "first", "at": created}
    seen = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, acknowledged=True))
    store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=seen, diff={"version": 1}))
    state = store.latest_diff(pid)
    assert state["base"]["id"] == seen
    assert state["baseline"] == {"kind": "acknowledged", "at": state["base"]["created_at"]}


def test_latest_diff_in_one_read(store):
    diff = {"version": 1, "functions": {}, "lines": []}
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    assert store.latest_diff(pid) == {"head": store.latest_analysis(pid), "base": None, "diff": None, "baseline": None}
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff=diff))
    by_id = {a["id"]: a for a in store.list_analyses(pid)}
    assert store.latest_diff(pid) == {"head": by_id[second], "base": by_id[first], "diff": diff,
                                      "baseline": {"kind": "first", "at": store.get_project(pid)["created_at"]}}
    assert store.latest_diff("nope") is None


def test_missing_base_means_nothing_to_compare(store):
    """基准那次已经不在了（旧版本的服务按旧规则清理掉的）：到处都当作没有可比的——base_id 为 None、has_diff 为假、
    差异为 None，不会出现"有差异却没有基准"。"""
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    con = sqlite3.connect(store.path)
    con.execute("delete from analyses where id = ?", (first,))
    con.commit()
    con.close()
    head = store.latest_analysis(pid)
    assert (head["id"], head["base_id"], head["has_diff"]) == (second, None, False)
    assert store.list_analyses(pid) == [head] and store.list_projects()[0]["analysis"] == head
    extra = store.analysis_extra(second)
    assert (extra["base_id"], extra["diff"]) == (None, None)
    assert store.latest_diff(pid) == {"head": head, "base": None, "diff": None, "baseline": None}


def test_prune_also_keeps_the_base_of_the_latest(store):
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    ids = [first] + [store.add_analysis(pid, analysis(i)) for i in range(1, 4)]
    latest = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff={"version": 1}))
    assert store.prune_analyses(pid, keep=2) == 2
    assert [a["id"] for a in store.list_analyses(pid)] == [latest, ids[3], first]
    store.clear_base(pid)
    assert store.prune_analyses(pid, keep=2) == 1  # 基准清掉了：不再多留
    assert [a["id"] for a in store.list_analyses(pid)] == [latest, ids[3]]


def test_add_get_list_delete_project(store):
    pid, aid = store.add_project(name="演示", root="/srv/demo", config={"exclude": ["x"]}, analysis=analysis(3))
    assert len(pid) >= 6 and pid.isalnum()
    p = store.get_project(pid)
    assert p["name"] == "演示" and p["root"] == "/srv/demo" and p["config"] == {"exclude": ["x"]}
    assert p["created_at"] and p["updated_at"]
    [item] = store.list_projects()
    assert item["id"] == pid and item["analysis"]["id"] == aid
    assert item["analysis"]["summary"] == {"functions": 3, "lines": 1}
    assert item["analysis"]["engine_version"] == "0.1.0"
    assert store.find_project_by_root("/srv/demo")["id"] == pid
    assert store.get_project("nope") is None
    store.set_override(pid, "line:a.py::f", "标题", None)
    assert store.delete_project(pid) is True
    assert store.get_project(pid) is None and store.list_projects() == []
    assert store.delete_project(pid) is False
    con = sqlite3.connect(store.path)  # 分析和手工项随项目一起删掉
    assert con.execute("select count(*) from analyses").fetchone()[0] == 0
    assert con.execute("select count(*) from line_overrides").fetchone()[0] == 0
    con.close()


def test_project_ids_are_distinct(store):
    ids = {store.add_project(name=f"p{i}", root=f"/p{i}", config={}, analysis=analysis())[0] for i in range(20)}
    assert len(ids) == 20


def test_graph_roundtrip_is_byte_identical_and_gzipped(store):
    big = json.dumps({"x": ["节点" * 50] * 5000}, ensure_ascii=False, indent=2)
    pid, aid = store.add_project(name="p", root="/p", config={}, analysis=analysis(graph=big))
    gz = store.graph_gz(aid)
    assert gz[:2] == b"\x1f\x8b" and len(gz) < len(big.encode()) / 5
    assert gzip.decompress(gz) == big.encode("utf-8")
    assert store.graph_json(aid) == big
    latest = store.latest_analysis(pid)
    assert latest["id"] == aid and latest["graph_size"] == len(big.encode("utf-8"))
    assert store.graph_gz(999) is None and store.graph_json(999) is None


def test_list_shows_latest_analysis_and_prune_keeps_newest(store):
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis(0))
    ids = [first] + [store.add_analysis(pid, analysis(i)) for i in range(1, 7)]
    assert store.latest_analysis(pid)["id"] == ids[-1]
    assert store.list_projects()[0]["analysis"]["summary"]["functions"] == 6
    assert store.prune_analyses(pid, keep=5) == 2
    assert [a["id"] for a in store.list_analyses(pid)] == list(reversed(ids[2:]))
    assert store.prune_analyses(pid, keep=5) == 0


def test_prune_keeps_the_analysis_of_the_latest_full_run(store):
    """最近一次跑完的全量运行用的那次分析不清理：没走到的结局要用它的图分出哪些函数、结局是运行之后才有的。只跑一条线
    的运行、没跑完的运行用的不留；有了更新的全量运行之后，原来那次就可以清掉。"""
    pid, first = store.add_project(name="p", root="/p", config={}, analysis=analysis(0))
    second = store.add_analysis(pid, analysis(1))
    store.add_run(pid, new_run(analysis_id=first))
    store.add_run(pid, dataclasses.replace(new_run(analysis_id=second), scope={"line": "line:a.py::f", "line_title": "下单",
                                                                             "tests": 1, "base_run": 1}))
    store.add_run(pid, new_run(status="timeout", paths=None, analysis_id=second))
    ids = [store.add_analysis(pid, analysis(i)) for i in range(2, 5)]
    assert store.prune_analyses(pid, keep=2) == 2
    assert [a["id"] for a in store.list_analyses(pid)] == [ids[2], ids[1], first]
    assert store.graph_json(first) is not None
    store.add_run(pid, new_run(analysis_id=ids[2]))
    assert store.prune_analyses(pid, keep=2) == 1 and [a["id"] for a in store.list_analyses(pid)] == [ids[2], ids[1]]


def test_prune_only_touches_one_project(store):
    a, _ = store.add_project(name="a", root="/a", config={}, analysis=analysis())
    b, _ = store.add_project(name="b", root="/b", config={}, analysis=analysis())
    for _ in range(3):
        store.add_analysis(a, analysis())
    assert store.prune_analyses(a, keep=1) == 3
    assert len(store.list_analyses(a)) == 1 and len(store.list_analyses(b)) == 1


def test_add_analysis_for_missing_project(store):
    with pytest.raises(ProjectMissingError):
        store.add_analysis("nope", analysis())


def test_same_root_cannot_be_added_twice(store):
    pid, _ = store.add_project(name="a", root="/same", config={}, analysis=analysis())
    with pytest.raises(DuplicateRootError) as e:
        store.add_project(name="b", root="/same", config={}, analysis=analysis())
    assert e.value.project_id == pid and "/same" in str(e.value)
    assert [p["name"] for p in store.list_projects()] == ["a"]
    con = sqlite3.connect(store.path)
    assert con.execute("select count(*) from analyses").fetchone()[0] == 1  # 失败的那次没有留下分析
    con.close()


def test_conflict_check_runs_inside_the_write_transaction(store):
    a, _ = store.add_project(name="a", root="/a", config={}, analysis=analysis())
    seen: list = []

    def refuse(projects):
        seen.append(projects)
        return OverlapRootError("和项目 a 重叠", projects[0]["id"])

    with pytest.raises(OverlapRootError) as e:
        store.add_project(name="b", root="/a/b", config={}, analysis=analysis(), conflict=refuse)
    assert e.value.project_id == a and seen == [[{"id": a, "name": "a", "root": "/a"}]]
    assert [p["name"] for p in store.list_projects()] == ["a"]
    store.add_project(name="c", root="/c", config={}, analysis=analysis(), conflict=lambda projects: None)
    assert [p["name"] for p in store.list_projects()] == ["a", "c"]


def test_add_project_is_atomic_when_saving_the_analysis_fails(store):
    con = sqlite3.connect(store.path)  # 模拟写分析时出错（磁盘满之类）：项目那一行也不能留下
    con.execute("create trigger boom before insert on analyses begin select raise(abort, 'disk full'); end")
    con.commit()
    con.close()
    with pytest.raises(StoreError, match="disk full"):
        store.add_project(name="a", root="/a", config={}, analysis=analysis())
    assert store.list_projects() == []


def test_overrides_replace_and_delete(store):
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    sid = "line:app/a b.py::Flow.run#2"
    assert store.get_overrides(pid) == {}
    store.set_override(pid, sid, "新标题", None)
    store.set_override(pid, "line:x.py::g", None, "新分组")
    assert store.get_overrides(pid) == {sid: {"title": "新标题"}, "line:x.py::g": {"group": "新分组"}}
    store.set_override(pid, sid, None, "分组")  # 整条改写：标题不再改
    assert store.get_overrides(pid)[sid] == {"group": "分组"}
    store.set_override(pid, sid, None, None)  # 两项都空 = 删除
    assert sid not in store.get_overrides(pid)
    store.set_override(pid, "never-set", None, None)  # 删除不存在的：不报错
    with pytest.raises(ProjectMissingError):
        store.set_override("nope", sid, "t", None)


def test_overrides_survive_new_analyses(store):
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    store.set_override(pid, "line:a", "标题", "分组")
    for _ in range(7):
        store.add_analysis(pid, analysis())
    store.prune_analyses(pid, keep=5)
    assert store.get_overrides(pid) == {"line:a": {"title": "标题", "group": "分组"}}


def test_set_config(store):
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    before = store.get_project(pid)["updated_at"]
    assert store.set_config(pid, {"exclude": ["gen"]}) is True
    assert store.get_project(pid)["config"] == {"exclude": ["gen"]}
    assert store.get_project(pid)["updated_at"] >= before
    assert store.set_config("nope", {}) is False


def test_concurrent_writes_from_threads(store):
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    graphs = [json.dumps({"n": i, "pad": "x" * 200_000}) for i in range(2)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def save(text: str) -> None:
        try:
            barrier.wait()
            for _ in range(5):
                store.add_analysis(pid, analysis(graph=text))
        except BaseException as e:  # noqa: BLE001 — 线程里的异常要带回主线程
            errors.append(e)

    threads = [threading.Thread(target=save, args=(g,)) for g in graphs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    saved = store.list_analyses(pid)
    assert len(saved) == 11
    texts = [store.graph_json(a["id"]) for a in saved[:10]]
    assert sorted(texts) == sorted(graphs * 5)


# ---------- 只对本人可读写 ----------

def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def loose_umask():
    """测试期间用最常见的宽松 umask（022：新文件别人可读），结束后还原。"""
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_new_data_directory_and_database_are_private(tmp_path, loose_umask):
    """新建的数据目录是 0700；库文件和写入时 SQLite 建的 -wal、-shm 都是 0600（别的用户读不到分析结果和源码路径）。"""
    home = tmp_path / "home"
    store = Store(home / "pystepmap.db")
    store.add_project(name="x", root="/x", config={}, analysis=analysis(), conflict=lambda projects: None)
    con = sqlite3.connect(home / "pystepmap.db")  # 开着一个连接：-wal、-shm 这时一定在
    try:
        con.execute("SELECT count(*) FROM projects").fetchone()
        store.set_config(store.list_projects()[0]["id"], {"exclude": []})
        assert mode(home) == 0o700
        for name in ("pystepmap.db", "pystepmap.db-wal", "pystepmap.db-shm"):
            assert (home / name).exists(), name
            assert mode(home / name) == 0o600, name
    finally:
        con.close()


def test_existing_wider_directory_and_files_are_tightened(tmp_path, loose_umask):
    """以前的版本建的数据目录（0755）和库文件（0644）：启动时收紧一次。"""
    home = tmp_path / "home"
    Store(home / "pystepmap.db")
    os.chmod(home, 0o755)
    os.chmod(home / "pystepmap.db", 0o644)
    Store(home / "pystepmap.db")
    assert mode(home) == 0o700
    assert mode(home / "pystepmap.db") == 0o600


# ---------- 两个进程用同一个库：开着一个闲着的连接 ----------

def test_store_keeps_one_idle_connection_so_the_wal_stays(tmp_path):
    """每次调用各开各关连接：进程里（也是所有进程里）最后一个连接关上时，SQLite 做检查点并删掉 -wal、-shm；另一个
    进程（服务和 MCP 用同一个数据目录）正好在开库、读库时，偶尔读出 disk I/O error（SQLITE_IOERR_SHORT_READ）等。
    Store 开着一个闲着的连接直到 close()：-wal、-shm 一直在，不会被删了再建。"""
    home = tmp_path / "home"
    store = Store(home / "pystepmap.db")
    store.add_project(name="p", root="/p", config={}, analysis=analysis())
    store.list_projects()  # 这次调用的连接已经关了
    assert (home / "pystepmap.db-wal").exists() and (home / "pystepmap.db-shm").exists()
    store.close()
    assert not (home / "pystepmap.db-wal").exists()  # 闲着的连接是最后一个：关上时照常检查点、删掉
    store.close()  # 再关一次不报错
    assert [p["name"] for p in store.list_projects()] == ["p"]  # 关的只是闲着的那个，读写照常


def test_the_idle_connection_holds_no_read_snapshot(tmp_path):
    """闲着的连接不留着读事务：别的连接写入以后检查点能做完，-wal 不会一直长下去。"""
    store = Store(tmp_path / "pystepmap.db")
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    for i in range(20):
        store.add_analysis(pid, analysis(i, graph=json.dumps({"pad": os.urandom(20_000).hex()})))
    con = sqlite3.connect(store.path)
    busy, log, done = con.execute("pragma wal_checkpoint(truncate)").fetchone()
    con.close()
    assert busy == 0 and log == done == 0  # 没有被读事务挡住：整个 -wal 都写回去了
    store.close()


def test_dropping_a_store_closes_its_idle_connection(tmp_path):
    """Store 被回收时关掉闲着的连接：测试里用完的临时库不漏描述符，也没有 ResourceWarning（3.13 起没关的 sqlite3 连接
    被回收时会警告）。打开失败的 Store 被回收也不出错。只看这个库自己的连接（全量测试里别的测试留下的垃圾也会在
    这时被回收）。"""
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = Store(tmp_path / "pystepmap.db")
        store.list_projects()
        keeper = store._keeper
        assert keeper is not None and (tmp_path / "pystepmap.db-wal").exists()
        del store
        gc.collect()
        with pytest.raises(sqlite3.ProgrammingError):
            keeper.execute("select 1")  # 已经关了
        assert not (tmp_path / "pystepmap.db-wal").exists()  # 它是这个库最后一个连接：关上时删掉了 -wal
        con = sqlite3.connect(tmp_path / "pystepmap.db")
        con.execute(f"pragma user_version = {SCHEMA_VERSION + 1}")
        con.close()
        with pytest.raises(StoreError):
            Store(tmp_path / "pystepmap.db")
        gc.collect()
    assert [str(w.message) for w in caught if "sqlite3" in str(w.message) or "Store" in str(w.message)] == []


TWIN = """
import sys

from pystepkit import workflow


def g(v):
    return v


if sys.version_info >= (3, 0):
    def f(x):
        return g(x)
else:
    def f(x):
        return g(x)


@workflow(name="W", title="W")
class W:
    def run(self, x):
        return f(x)
"""


def traced_paths(tmp_path, files: dict[str, str], test_src: str):
    """在 tmp_path 里建项目、分析、挂（平台的）记录插件真跑一次 pytest，返回 (图, 运行路径)。"""
    import os
    import subprocess
    import sys

    from pystepmap.analyze import analyze_project
    from pystepmap.model import to_json
    from pystepmap.runpath import build_run_paths
    from pystepmap.verify import read_records
    from pystepmap_server.runner import PYTEST_PLUGIN_OPTIONS, write_plugin_dir

    root = tmp_path / "proj"
    for rel, text in {**files, "tests/test_it.py": test_src}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    graph = analyze_project(root)
    (tmp_path / "g.json").write_text(to_json(graph), encoding="utf-8")
    env = {**os.environ, "PYSTEPMAP_GRAPH": str(tmp_path / "g.json"), "PYSTEPMAP_TRACE_OUT": str(tmp_path / "t.jsonl"),
           "PYTHONPATH": f"{write_plugin_dir(tmp_path / 'home')}{os.pathsep}{root}", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "pytest", "-q", *PYTEST_PLUGIN_OPTIONS.split(), "tests"], cwd=root, env=env,
                   check=True, capture_output=True)
    return graph, build_run_paths(graph, read_records(tmp_path / "t.jsonl"))


def test_a_slice_carries_the_whole_duplicate_group(store, tmp_path):
    """一个函数有两个同样的定义（f、f#2），只跑了第一个：切片里 shapes 要带上这一组的每个定义（不只是这个测试跑过的），
    否则 core 判断不了"这一组变没变"，同一张图上也放不上。切片和整份在 locate_test_run 下结果一样。"""
    from pystepmap.runpath import (
        locate_test_run,
        run_paths_from_json,
        run_paths_to_json,
    )

    graph, paths = traced_paths(tmp_path, {"twin.py": TWIN},
                                "from twin import W\n\ndef test_w():\n    assert W().run(3) == 3\n")
    data = run_paths_to_json(paths)
    assert data["function_dups"] == ["twin.py::f"] and {"twin.py::f", "twin.py::f#2"} <= set(data["shapes"])
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    rid = store.add_run(pid, new_run(paths=data))
    name = "tests/test_it.py::test_w"
    sliced = run_paths_from_json(store.run_test_paths(pid, rid, name))
    assert "twin.py::f#2" in sliced.shapes and "twin.py::f#2" not in sliced.tests[0].functions
    whole_test = next(t for t in paths.tests if t.test == name)

    def placed(p, t):
        found, missing = locate_test_run(graph, p, t)
        return {k: sorted(m.items()) for k, (_fg, m) in found.items()}, missing

    assert placed(sliced, sliced.tests[0]) == placed(paths, whole_test)
    found, missing = locate_test_run(graph, paths, whole_test)
    assert missing == 0 and {fg.sid for fg, _m in found.values()} == {"twin.py::W.run", "twin.py::f", "twin.py::g"}


INDEX = {"functions": ["a.py::f", "b.py::g", "c.py::h"], "tests": {"": [0], "tests/a.py::t": [0, 1], "tests/b.py::u": [2]}}


def test_the_function_index_of_a_run(store):
    """每次运行另存一份小的函数索引（运行里的函数 sid 一份 + 每个测试的下标），压缩存在自己的一列：按功能线数一个测试
    执行了几个函数时用它，不用解开整份路径。没有给（没跑完）时 None。"""
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    rid = store.add_run(pid, dataclasses.replace(new_run(), function_index=INDEX))
    assert store.run_function_index(pid, rid) == INDEX
    other = store.add_run(pid, new_run(status="timeout", paths=None))
    assert store.run_function_index(pid, other) is None and store.run_function_index(pid, 999) is None
    con = sqlite3.connect(store.path)
    gz = con.execute("select functions_gz from runs where id = ?", (rid,)).fetchone()[0]
    con.close()
    assert gz[:2] == b"\x1f\x8b" and json.loads(gzip.decompress(gz)) == INDEX


def test_runs_meta_without_the_tests(store):
    """运行一级的各项（version、shapes、function_dups）单独取：只读那几段。"""
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    paths = {"version": 1, "tests": [a_test("t", "x")], "shapes": {"a.py::f": {"shape": "s", "dups": []}},
             "function_dups": []}
    rid = store.add_run(pid, new_run(paths=paths))
    assert store.run_paths_meta(pid, rid) == {"version": 1, "shapes": {"a.py::f": {"shape": "s", "dups": []}},
                                              "function_dups": []}
    assert store.run_paths_meta(pid, store.add_run(pid, new_run(paths=None))) is None


def test_a_version_3_database_from_before_the_function_index_gets_the_column(tmp_path):
    """本阶段较早的代码建的结构版本 3 的库没有 functions_gz 这一列：打开时补上（已有的运行这一列为空），照常读写。"""
    path = tmp_path / "home" / "pystepmap.db"
    first = Store(path)
    pid, _ = first.add_project(name="p", root="/p", config={}, analysis=analysis())
    old_run = first.add_run(pid, new_run())
    first.close()
    con = sqlite3.connect(path)
    con.execute("alter table runs drop column functions_gz")
    con.commit()
    con.close()
    assert "functions_gz" not in columns(path, "runs")
    store = Store(path)
    assert "functions_gz" in columns(path, "runs")
    assert store.run_function_index(pid, old_run) is None and store.get_run(pid, old_run)["status"] == "ok"
    rid = store.add_run(pid, dataclasses.replace(new_run(), function_index=INDEX))
    assert store.run_function_index(pid, rid) == INDEX


def test_a_slice_read_racing_a_prune_is_consistent(store, monkeypatch):
    """取一个测试的路径时先读索引、再读压缩包：两步之间别的连接把这次运行清理掉了，也不出 500——同一个读事务里
    看到的是同一个快照。"""
    pid, _ = store.add_project(name="p", root="/p", config={}, analysis=analysis())
    paths = {"version": 1, "tests": [a_test("t", "x")]}
    rid = store.add_run(pid, new_run(paths=paths))
    real = store._connect

    class Racing:
        def __init__(self, con):
            self.con = con

        def blobopen(self, *args, **kwargs):  # 读完索引、要读压缩包的那一刻：另一个连接删掉这一行
            other = sqlite3.connect(store.path)
            other.execute("delete from runs where id = ?", (rid,))
            other.commit()
            other.close()
            return self.con.blobopen(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.con, name)

    def racing():
        cm = real()
        return _Wrapped(cm, Racing(cm.__enter__()))

    monkeypatch.setattr(store, "_connect", racing)
    assert store.run_test_paths(pid, rid, "t")["tests"] == paths["tests"]
    monkeypatch.undo()
    assert store.run_test_paths(pid, rid, "t") is None and store.run_paths_meta(pid, rid) is None


def test_scoped_runs_are_stored_and_pruned_apart_from_full_runs(store):
    """只跑一条功能线的运行带着范围存；清理时全量的、单条线的各留最近 keep 次；latest_full_run 只认跑完的全量运行。
    mcp_tests 开关默认关，只有 set_mcp_tests 能改。"""
    pid, _ = store.add_project(name="p", root="/p-scoped", config={}, analysis=analysis())
    def run(scope=None, paths=True):
        return store.add_run(pid, NewRun(analysis_id=None, started_at="t0", finished_at="t1", command="pytest", status="ok",
                                         exit_code=0, summary={}, tests=[] if paths else None,
                                         paths={"tests": {}} if paths else None, scope=scope))
    scope = {"line": "line:a.py::f", "line_title": "下单", "tests": 3, "base_run": 1}
    full1, _s1, s2, unfinished, s3 = run(), run(scope), run(scope), run(paths=False), run(scope)
    assert store.latest_full_run(pid)["id"] == full1  # 没跑完的全量不算，单条线的不算
    assert [r["scope"] for r in store.list_runs(pid)] == [scope, None, scope, scope, None]
    assert store.prune_runs(pid, keep=2) == 1
    assert [r["id"] for r in store.list_runs(pid)] == [s3, unfinished, s2, full1]
    assert store.get_project(pid)["mcp_tests"] is False and store.set_mcp_tests(pid, True) is True
    assert store.get_project(pid)["mcp_tests"] is True
    with pytest.raises(ProjectMissingError):
        store.set_mcp_tests("nope", True)


def test_marking_as_seen_keeps_a_review_record_of_that_diff(store, monkeypatch):
    """标为已看时，那次的流程差异存成一条变更记录（同一个事务）：记着是哪次分析相对哪次、概要和差异原样；没有差异、
    已经标过的不多存；看的不是最新一次时不存；每个项目只留最近 KEEP_REVIEWS 条；删项目时一起删。"""
    import pystepmap_server.store as store_module

    diff = {"version": 1, "lines": [{"sid": "line:a.py::f", "title": "下单", "group": "订单", "status": "changed"}],
            "functions": {"a.py::f": {"status": "changed", "nodes": [{"status": "added"}, {"status": "removed"},
                                                                    {"status": "removed"}, {"status": "changed"}]}}}
    pid, first = store.add_project(name="p", root="/p-review", config={}, analysis=analysis())
    assert store.clear_base(pid) is True and store.list_reviews(pid) == []  # 第一次分析：没有差异，不存
    second = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=first, diff=diff))
    assert store.clear_base(pid, analysis_id=first) is False and store.list_reviews(pid) == []  # 看的不是最新一次
    assert store.clear_base(pid, analysis_id=second) is True
    [rec] = store.list_reviews(pid)
    assert (rec["head_id"], rec["base_id"]) == (second, first) and rec["acked_at"] and rec["head_at"] and rec["base_at"]
    assert rec["summary"] == {"lines": [{"sid": "line:a.py::f", "title": "下单", "group": "订单", "status": "changed"}],
                              "functions": 1, "added": 1, "removed": 2, "changed": 1}
    assert store.get_review(pid, rec["id"])["diff"] == diff and "diff" not in rec
    assert rec["graph_size"] == len(GRAPH.encode("utf-8"))  # 当时的图也存下了（head 那次的）
    assert gzip.decompress(store.review_graph_gz(pid, rec["id"])).decode("utf-8") == GRAPH
    assert store.review_graph_gz(pid, 999) is None and store.review_graph_gz("nope", rec["id"]) is None
    assert store.clear_base(pid) is True and len(store.list_reviews(pid)) == 1  # 已经标过：不多存
    assert store.get_review(pid, 999) is None and store.get_review("nope", rec["id"]) is None
    monkeypatch.setattr(store_module, "KEEP_REVIEWS", 3)
    last = second
    for _ in range(4):
        last = store.add_analysis(pid, NewAnalysis("0.1.0", {}, GRAPH, base_id=last, diff=diff))
        assert store.clear_base(pid, analysis_id=last) is True
    kept = store.list_reviews(pid)
    assert len(kept) == 3 and kept[0]["head_id"] == last and kept[0]["id"] > kept[1]["id"] > kept[2]["id"]
    store.delete_project(pid)
    assert store.list_reviews(pid) == []


def test_version_5_reviews_are_kept_without_a_graph(tmp_path):
    """结构版本 5 的库（变更记录还没有存图）升到 6：记录还在，graph_size 为空，取不到图。"""
    path = tmp_path / "home" / "pystepmap.db"
    store = Store(path)
    pid, _first = store.add_project(name="p", root="/p-v5", config={}, analysis=analysis())
    store.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE reviews DROP COLUMN graph_gz")
    con.execute("ALTER TABLE reviews DROP COLUMN graph_size")
    con.execute("INSERT INTO reviews (project_id, acked_at, head_id, head_at, base_id, base_at, summary_json, diff_json) "
                "VALUES (?, 't', 2, 't', 1, 't', ?, ?)", (pid, json.dumps({"lines": []}), json.dumps({"version": 1})))
    con.execute("pragma user_version = 5")
    con.commit()
    con.close()
    store = Store(path)
    [rec] = store.list_reviews(pid)
    assert rec["graph_size"] is None and store.review_graph_gz(pid, rec["id"]) is None
    assert (path.parent / "pystepmap.db.bak-v5").exists()
    store.close()
