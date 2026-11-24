"""HTTP 接口：逐个接口测状态码和返回体（含错误情形）；图接口带 gzip；line_sid 含 /、:、# 时手工项接口正常；
过期检查、流程差异、标为已看、按指定那次分析判断源码是否过期、分析超时 504；有 / 没有打包好的前端时 / 的表现。"""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pystepmap.analyze import analyze_project, analyze_with_notes
from pystepmap.engineid import ENGINE_VERSION
from pystepmap.model import from_json, to_json
from pystepmap_server import __version__ as SERVER_VERSION
from pystepmap_server import flows, runner, worker
from pystepmap_server.app import create_app

REPO = Path(__file__).resolve().parents[2]
TRANSFER = REPO / "examples" / "transfer"
TRANSFER_FILE = "transfer_workflow.py"
LOCAL = "http://127.0.0.1:5179"  # 服务只接受本机的 Host：测试客户端默认的 testserver 会被拒绝


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "pystepmap-home"


@pytest.fixture
def client(home, monkeypatch) -> TestClient:
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    with TestClient(create_app(home), base_url=LOCAL) as c:
        yield c


@pytest.fixture
def transfer(client) -> dict:
    r = client.post("/api/projects", json={"root": str(TRANSFER)})
    assert r.status_code == 201, r.text
    return r.json()


def engine_in_process(monkeypatch, analyzer) -> None:
    """把分析子进程换成本进程里的同一套（worker.analyze_directory），用给的假引擎；子进程本身见 test_worker。"""
    monkeypatch.setattr(flows, "run_in_subprocess",
                        lambda root, config, *, timeout, cwd: worker.analyze_directory(root, config, analyzer))


def error_of(r, status: int) -> str:
    assert r.status_code == status, r.text
    body = r.json()
    assert isinstance(body["error"], str) and body["error"]
    return body["error"]


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": SERVER_VERSION, "engine_version": ENGINE_VERSION}
    assert SERVER_VERSION == importlib.metadata.version("pystepmap-server")  # 手写的版本号和包的一致


def test_add_and_list_projects(client, transfer, home):
    assert transfer["name"] == "transfer" and transfer["root"] == str(TRANSFER.resolve())
    assert transfer["analyzing"] is False and transfer["created_at"] and transfer["updated_at"]
    a = transfer["analysis"]
    assert a["engine_version"] == ENGINE_VERSION and a["summary"]["lines"] == 1 and a["graph_size"] > 1000
    assert set(a) == {"id", "created_at", "engine_version", "summary", "graph_size", "base_id", "has_diff"}
    assert a["base_id"] is None and a["has_diff"] is False  # 第一次分析：没有可比的
    assert set(transfer) == {"id", "name", "root", "created_at", "updated_at", "analyzing", "analysis", "mcp_tests"}
    assert transfer["mcp_tests"] is False
    assert client.get("/api/projects").json() == [transfer]
    assert client.get(f"/api/projects/{transfer['id']}").json() == transfer
    assert (home / "pystepmap.db").is_file()


def test_add_with_name_and_config(client, tmp_path):
    proj = tmp_path / "p"
    (proj / "gen").mkdir(parents=True)
    (proj / "a.py").write_text("def f(): ...\n")
    (proj / "gen" / "b.py").write_text("def g(): ...\n")
    r = client.post("/api/projects", json={"root": str(proj) + "/", "name": "我的", "config": {"exclude": ["gen"]}})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "我的" and r.json()["analysis"]["summary"]["functions"] == 1
    assert client.get(f"/api/projects/{r.json()['id']}/config").json() == {"exclude": ["gen"]}


def test_add_errors(client, transfer, tmp_path):
    assert "目录不存在" in error_of(client.post("/api/projects", json={"root": str(tmp_path / "nope")}), 400)
    assert "绝对路径" in error_of(client.post("/api/projects", json={"root": "rel"}), 400)
    (tmp_path / "empty").mkdir()
    assert "没有 .py" in error_of(client.post("/api/projects", json={"root": str(tmp_path / "empty")}), 400)
    assert "未知字段" in error_of(client.post("/api/projects", json={"root": str(TRANSFER), "config": {"x": 1}}), 400)
    r = client.post("/api/projects", json={"root": str(TRANSFER) + "/"})
    assert "已经添加过" in error_of(r, 409)
    assert r.json()["project_id"] == transfer["id"] and r.json()["code"] == "duplicate"
    (tmp_path / "outer").mkdir()
    (tmp_path / "outer" / "x.py").write_text("x = 1\n")
    client.post("/api/projects", json={"root": str(tmp_path / "outer")})
    (tmp_path / "outer" / "inner").mkdir()
    (tmp_path / "outer" / "inner" / "y.py").write_text("y = 1\n")
    assert "里面" in error_of(client.post("/api/projects", json={"root": str(tmp_path / "outer" / "inner")}), 409)
    assert len(client.get("/api/projects").json()) == 2


def test_add_request_shape_errors(client):
    assert "root" in error_of(client.post("/api/projects", json={}), 400)
    assert "root" in error_of(client.post("/api/projects", json={"root": 3}), 400)
    assert "config" in error_of(client.post("/api/projects", json={"root": "/x", "config": [1]}), 400)
    assert "nope" in error_of(client.post("/api/projects", json={"root": "/x", "nope": 1}), 400)
    r = client.post("/api/projects", content=b"{not json", headers={"Content-Type": "application/json"})
    assert error_of(r, 400) == "请求参数不对：请求体不是合法的 JSON"
    error_of(client.post("/api/projects", json=["/x"]), 400)


def test_add_analysis_failure_is_500_and_leaves_nothing(client, monkeypatch):
    def broken(root, config):
        raise RuntimeError("引擎坏了")

    engine_in_process(monkeypatch, broken)
    r = client.post("/api/projects", json={"root": str(TRANSFER)})
    assert "引擎坏了" in error_of(r, 500) and r.json()["code"] == "analysis"
    assert client.get("/api/projects").json() == []


def test_get_and_delete_project(client, transfer):
    pid = transfer["id"]
    assert "项目不存在" in error_of(client.get("/api/projects/nope"), 404)
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 204 and r.content == b""
    assert client.get("/api/projects").json() == []
    error_of(client.delete(f"/api/projects/{pid}"), 404)
    error_of(client.get(f"/api/projects/{pid}"), 404)


def test_reanalyze(client, transfer, monkeypatch):
    pid = transfer["id"]
    r = client.post(f"/api/projects/{pid}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == pid and body["analysis"]["id"] > transfer["analysis"]["id"]
    assert body["analysis"]["summary"] == transfer["analysis"]["summary"] | {"seconds": body["analysis"]["summary"]["seconds"]}
    error_of(client.post("/api/projects/nope/analyze"), 404)
    mark = flows.mark_analyzing(client.app.state.store, pid)  # 假装有一次正在进行
    try:
        assert client.get("/api/projects").json()[0]["analyzing"] is True
        r = client.post(f"/api/projects/{pid}/analyze")
        assert error_of(r, 409) == "正在分析" and r.json()["code"] == "busy"
    finally:
        flows.unmark_analyzing(mark)

    def broken(root, config):
        raise RuntimeError("坏了")

    engine_in_process(monkeypatch, broken)
    assert "坏了" in error_of(client.post(f"/api/projects/{pid}/analyze"), 500)
    assert client.get(f"/api/projects/{pid}").json()["analysis"]["id"] == body["analysis"]["id"]  # 上一次的还在


def test_analysis_runs_off_the_event_loop_and_a_second_click_is_refused(client, transfer, monkeypatch):
    """分析在线程池里跑：进行中别的请求照常响应、列表里 analyzing 为 true；同时再点一次"重新分析"得到 409。"""
    pid = transfer["id"]
    started, gate = threading.Event(), threading.Event()

    def slow(root, config):
        started.set()
        assert gate.wait(10)
        return analyze_with_notes(root, config)

    engine_in_process(monkeypatch, slow)
    results: list = []
    t = threading.Thread(target=lambda: results.append(client.post(f"/api/projects/{pid}/analyze")))
    t.start()
    try:
        assert started.wait(10)
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/projects").json()[0]["analyzing"] is True
        assert client.get(f"/api/projects/{pid}/graph").status_code == 200  # 上一次的图照常能取
        r = client.post(f"/api/projects/{pid}/analyze")
        assert error_of(r, 409) == "正在分析"
    finally:
        gate.set()
        t.join(30)
    assert results[0].status_code == 200 and results[0].json()["analyzing"] is False
    assert len(client.app.state.store.list_analyses(pid)) == 2


def test_graph_is_sent_gzipped_as_stored(client, transfer):
    pid, aid = transfer["id"], transfer["analysis"]["id"]
    store = client.app.state.store
    with client.stream("GET", f"/api/projects/{pid}/graph", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
        headers = r.headers
    assert r.status_code == 200
    assert headers["content-encoding"] == "gzip" and headers["content-type"].startswith("application/json")
    assert headers["x-pystepmap-analysis"] == str(aid)
    assert raw == store.graph_gz(aid) and int(headers["content-length"]) == len(raw)
    text = gzip.decompress(raw).decode("utf-8")
    assert int(headers["x-pystepmap-graph-size"]) == len(text.encode("utf-8"))
    assert text == to_json(analyze_project(TRANSFER))
    r = client.get(f"/api/projects/{pid}/graph")  # 客户端自动解压
    assert r.json()["schema_version"] == 1 and r.json()["root"] == str(TRANSFER.resolve())


def test_graph_without_gzip_support(client, transfer):
    r = client.get(f"/api/projects/{transfer['id']}/graph", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    assert json.loads(r.content)["schema_version"] == 1


def test_graph_errors(client):
    error_of(client.get("/api/projects/nope/graph"), 404)


SIDS = ["line:app/a.py::Flow.run", "line:app/x y.py::f#2", "line:中文/路径.py::类.方法#3", "line:a?b=c&d.py::g"]


def test_overrides_with_awkward_sids(client, transfer):
    pid = transfer["id"]
    url = f"/api/projects/{pid}/overrides"
    assert client.get(url).json() == {}
    for i, sid in enumerate(SIDS):
        r = client.put(url, params={"sid": sid}, json={"title": f"标题{i}"})
        assert r.status_code == 200, r.text
    assert client.get(url).json() == {sid: {"title": f"标题{i}"} for i, sid in enumerate(SIDS)}
    r = client.put(url, params={"sid": SIDS[1]}, json={"group": "  分组  "})  # 整条改写；首尾空白去掉
    assert r.json()[SIDS[1]] == {"group": "分组"}
    r = client.put(url, params={"sid": SIDS[0]}, json={"title": "新", "group": "组"})
    assert r.json()[SIDS[0]] == {"title": "新", "group": "组"}
    for empty in ({}, {"title": None, "group": None}, {"title": "", "group": "   "}):
        client.put(url, params={"sid": SIDS[2]}, json={"title": "x"})
        r = client.put(url, params={"sid": SIDS[2]}, json=empty)
        assert r.status_code == 200 and SIDS[2] not in r.json()
    assert client.get(url).json() == r.json()
    # 重新分析以后手工项还在
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200
    assert client.get(url).json() == r.json()


def test_override_errors(client, transfer):
    url = f"/api/projects/{transfer['id']}/overrides"
    assert "sid" in error_of(client.put(url, json={"title": "x"}), 400)
    error_of(client.put(url, params={"sid": ""}, json={"title": "x"}), 400)
    error_of(client.put(url, params={"sid": "line:a"}, json={"title": 3}), 400)
    error_of(client.put(url, params={"sid": "line:a"}, json={"title": "x" * 201}), 400)
    error_of(client.put(url, params={"sid": "line:a"}, json={"color": "red"}), 400)
    error_of(client.put("/api/projects/nope/overrides", params={"sid": "line:a"}, json={"title": "x"}), 404)
    error_of(client.get("/api/projects/nope/overrides"), 404)


def test_source(client, transfer):
    url = f"/api/projects/{transfer['id']}/source"
    r = client.get(url, params={"file": TRANSFER_FILE, "start": 1, "end": 3})
    assert r.status_code == 200, r.text
    lines = (TRANSFER / TRANSFER_FILE).read_text(encoding="utf-8").splitlines()
    assert r.json() == {"file": TRANSFER_FILE, "start": 1, "end": 3, "total": len(lines), "lines": lines[:3],
                        "stale": False}
    r = client.get(url, params={"file": TRANSFER_FILE, "start": 200, "end": 100_000})  # 超出末尾的不返回
    assert (r.json()["end"], len(r.json()["lines"])) == (len(lines), len(lines) - 199)
    for params in [{"file": "../core/src/pystepmap/cli.py", "start": 1, "end": 2},
                   {"file": str(TRANSFER / TRANSFER_FILE), "start": 1, "end": 2},
                   {"file": "README.md", "start": 1, "end": 2},
                   {"file": TRANSFER_FILE, "start": 0, "end": 2},
                   {"file": TRANSFER_FILE, "start": 3, "end": 2},
                   {"start": 1, "end": 2}]:
        error_of(client.get(url, params=params), 400)
    error_of(client.get(url, params={"file": "nope.py", "start": 1, "end": 2}), 404)
    error_of(client.get("/api/projects/nope/source", params={"file": TRANSFER_FILE, "start": 1, "end": 2}), 404)


def test_source_stale_after_the_file_changes(client, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("def f():\n    return 1\n")
    pid = client.post("/api/projects", json={"root": str(proj)}).json()["id"]
    url = f"/api/projects/{pid}/source"
    assert client.get(url, params={"file": "a.py", "start": 1, "end": 2}).json()["stale"] is False
    (proj / "a.py").write_text("def f():\n    return 2\n")
    body = client.get(url, params={"file": "a.py", "start": 1, "end": 2}).json()
    assert body["stale"] is True and body["lines"] == ["def f():", "    return 2"]
    (proj / "new.py").write_text("x = 1\n")  # 分析之后才出现的文件：图里没有指纹
    assert client.get(url, params={"file": "new.py", "start": 1, "end": 1}).json()["stale"] is None
    client.post(f"/api/projects/{pid}/analyze")
    assert client.get(url, params={"file": "a.py", "start": 1, "end": 2}).json()["stale"] is False


def test_config(client, transfer):
    pid, aid = transfer["id"], transfer["analysis"]["id"]
    url = f"/api/projects/{pid}/config"
    assert client.get(url).json() == {}
    cfg = {"exclude": [".venv", "tests"], "common_task_threshold": 3}
    r = client.put(url, json=cfg)
    assert r.status_code == 200 and r.json() == cfg
    assert client.get(url).json() == cfg
    assert client.get(f"/api/projects/{pid}").json()["analysis"]["id"] == aid  # 改配置不自动重新分析
    assert "未知字段" in error_of(client.put(url, json={"nope": 1}), 400)
    assert "common_task_threshold" in error_of(client.put(url, json={"common_task_threshold": 0}), 400)
    assert "对象" in error_of(client.put(url, json=[1, 2]), 400)
    # 源码根现在就按项目目录检查，不等下次分析才失败
    assert "source_roots 里的目录不存在" in error_of(client.put(url, json={"source_roots": ["no/such"]}), 400)
    assert "之内" in error_of(client.put(url, json={"source_roots": ["../outside"]}), 400)
    assert client.get(url).json() == cfg
    assert client.put(url, json={"source_roots": ["."]}).json() == {"source_roots": ["."]}
    client.put(url, json=cfg)
    error_of(client.get("/api/projects/nope/config"), 404)
    error_of(client.put("/api/projects/nope/config", json={}), 404)


def test_unknown_api_paths_and_methods(client):
    assert "没有这个接口" in error_of(client.get("/api/nope"), 404)
    assert "没有这个接口" in error_of(client.get("/api/projects/x/nope"), 404)
    error_of(client.patch("/api/projects"), 405)
    error_of(client.put("/api/health"), 405)


def test_root_without_built_frontend(client):
    r = client.get("/")
    assert r.status_code == 200 and "npm run build" in r.text and "text/html" in r.headers["content-type"]


def test_root_serves_built_frontend(home, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>pystepmap</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(dist))
    with TestClient(create_app(home), base_url=LOCAL) as c:
        assert c.get("/").text == "<!doctype html><title>pystepmap</title>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/health").json()["ok"] is True
        assert "没有这个接口" in error_of(c.get("/api/nope"), 404)  # 接口路径不落到静态文件上
        for path in ("/api", "/api/"):  # 挂着前端时 /api 本身也回中文的错误，不是 {"detail": "Not Found"}
            assert "没有这个接口" in error_of(c.get(path), 404)
        assert error_of(c.post("/api"), 405)
        # 接口以外的路径找不到时也回中文的 {"error": …}，不是 {"detail": "Not Found"}
        assert error_of(c.get("/no-such-page"), 404) == "没有这个页面：/no-such-page"
        assert error_of(c.get("/assets/nope.js"), 404) == "没有这个页面：/assets/nope.js"
    with TestClient(create_app(home, web_dist=dist), base_url=LOCAL) as c:
        assert c.get("/").status_code == 200


def test_unknown_page_without_built_frontend_is_chinese(client):
    assert error_of(client.get("/no-such-page"), 404) == "没有这个页面：/no-such-page"
    assert "不支持 POST" in error_of(client.post("/"), 405)


def test_data_survives_a_restart(home, monkeypatch):
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    with TestClient(create_app(home), base_url=LOCAL) as c:
        pid = c.post("/api/projects", json={"root": str(TRANSFER)}).json()["id"]
        c.put(f"/api/projects/{pid}/overrides", params={"sid": "line:x"}, json={"title": "t"})
    with TestClient(create_app(home), base_url=LOCAL) as c:
        assert [p["id"] for p in c.get("/api/projects").json()] == [pid]
        assert c.get(f"/api/projects/{pid}/overrides").json() == {"line:x": {"title": "t"}}
        assert hashlib.sha256(c.get(f"/api/projects/{pid}/graph").content).hexdigest()


# ---------- 只接受本机（Host / Origin） ----------

EVIL = "http://evil.example"


def test_foreign_host_is_refused(client, transfer):
    """DNS 重绑定：别的网站的域名解析到 127.0.0.1，浏览器发来的 Host 是那个域名。"""
    for path in ("/api/projects", "/api/health", f"/api/projects/{transfer['id']}/graph", "/"):
        r = client.get(path, headers={"Host": "attacker.example:5179"})
        assert "只接受用本机地址访问" in error_of(r, 403), path
    for host in ("127.0.0.1:5179", "127.0.0.1", "localhost:5180", "LOCALHOST", "[::1]:5179"):
        assert client.get("/api/projects", headers={"Host": host}).status_code == 200, host


def test_cross_site_writes_are_refused(client, transfer):
    pid = transfer["id"]
    store = client.app.state.store
    client.put(f"/api/projects/{pid}/overrides", params={"sid": "line:a"}, json={"title": "原来的"})
    before = (client.get("/api/projects").json(), client.get(f"/api/projects/{pid}/overrides").json(),
              client.get(f"/api/projects/{pid}/config").json())
    for origin in (EVIL, "null", "http://127.0.0.1.evil.example:5179", "http://localhost.evil.example"):
        h = {"Origin": origin}
        for r in (client.post("/api/projects", json={"root": str(REPO / "kit")}, headers=h),
                  client.post(f"/api/projects/{pid}/analyze", headers=h),  # 没有请求体的 POST
                  client.put(f"/api/projects/{pid}/overrides", params={"sid": "line:a"}, json={"title": "x"}, headers=h),
                  client.put(f"/api/projects/{pid}/config", json={"exclude": []}, headers=h),
                  client.delete(f"/api/projects/{pid}", headers=h)):
            assert "只接受本机页面发来的修改请求" in error_of(r, 403), (origin, r.request.method, r.request.url)
    after = (client.get("/api/projects").json(), client.get(f"/api/projects/{pid}/overrides").json(),
             client.get(f"/api/projects/{pid}/config").json())
    assert after == before and len(store.list_analyses(pid)) == 1
    # 读接口不看 Origin（跨站页面读不到没有 CORS 头的响应）
    assert client.get("/api/projects", headers={"Origin": EVIL}).status_code == 200


def test_local_pages_can_write(client, transfer):
    """开发服务器（http://localhost:5180，经 Vite 代理）和打包后的页面（http://127.0.0.1:5179）都能改；不带 Origin 的
    请求（curl、脚本）也能改。"""
    pid = transfer["id"]
    url = f"/api/projects/{pid}/overrides"
    for origin in ("http://localhost:5180", "http://127.0.0.1:5179", "http://[::1]:5180", None):
        h = {} if origin is None else {"Origin": origin}
        assert client.put(url, params={"sid": "line:a"}, json={"title": str(origin)}, headers=h).status_code == 200
        assert client.post(f"/api/projects/{pid}/analyze", headers=h).status_code == 200
    # Vite 代理不改 Host（changeOrigin 关）时 Host 是 localhost:5180，改（changeOrigin 开）时是 127.0.0.1:5179
    for host in ("localhost:5180", "127.0.0.1:5179"):
        r = client.put(url, params={"sid": "line:b"}, json={"title": "t"},
                       headers={"Host": host, "Origin": "http://localhost:5180"})
        assert r.status_code == 200


def test_root_replaced_by_a_symlink(client, tmp_path):
    proj, outside = tmp_path / "proj2", tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    (proj / "a.py").write_text("def a():\n    return 1\n")
    (outside / "secret.py").write_text("SECRET = 'SECRET-TOKEN-123'\n")
    p = client.post("/api/projects", json={"root": str(proj)}).json()
    os.rename(proj, tmp_path / "proj2-moved")
    os.symlink(outside, proj)
    r = client.get(f"/api/projects/{p['id']}/source", params={"file": "secret.py", "start": 1, "end": 5})
    assert "请删除后重新添加" in error_of(r, 400) and "SECRET" not in r.text
    r = client.post(f"/api/projects/{p['id']}/analyze")
    assert "不是添加时的那个目录" in error_of(r, 400) and r.json()["code"] == "invalid"
    assert client.get(f"/api/projects/{p['id']}").json() == p  # 上一次的结果原样保留
    assert client.get(f"/api/projects/{p['id']}/graph").json()["root"] == p["root"]  # 不是 outside


# ---------- 过期检查、流程差异 ----------

ORDERS = """from pystepkit import condition, outcome, step, workflow


@step(title="查询库存")
def load_stock(order):
    return {"left": 3}


@condition(title="库存不够？")
def lacks_stock(stock):
    return stock["left"] < 1


@workflow(title="下单", group="订单")
def place_order(order):
    stock = load_stock(order)
    if lacks_stock(stock):
        return outcome("failed", title="库存不够，没有下单")
    return outcome("ok", title="下单完成")
"""


@pytest.fixture
def orders(client, tmp_path) -> dict:
    """一个只有下单流程的小项目（测试会改它的代码）。返回 {"root", "project"}。"""
    root = tmp_path / "orders"
    (root / "gen").mkdir(parents=True)
    (root / "orders.py").write_text(ORDERS, encoding="utf-8")
    (root / "gen" / "made.py").write_text("x = 1\n", encoding="utf-8")
    r = client.post("/api/projects", json={"root": str(root), "config": {"extra_exclude": ["gen"]}})
    assert r.status_code == 201, r.text
    return {"root": root.resolve(), "project": r.json()}


def add_branch(root: Path) -> None:
    text = (root / "orders.py").read_text(encoding="utf-8")
    text = text.replace('@workflow(title="下单"', '@condition(title="数量太多？")\ndef too_many(order):\n'
                        '    return order["count"] > 100\n\n\n@workflow(title="下单"')
    text = text.replace("    stock = load_stock(order)\n",
                        '    stock = load_stock(order)\n    if too_many(order):\n'
                        '        return outcome("failed", title="数量太多，没有下单")\n')
    (root / "orders.py").write_text(text, encoding="utf-8")


def test_status(client, orders):
    pid, root = orders["project"]["id"], orders["root"]
    aid = orders["project"]["analysis"]["id"]
    url = f"/api/projects/{pid}/status"
    clean = {"checked": True, "stale": False, "changed": [], "added": [], "removed": [], "unreadable": [], "lines": [],
             "total": {"changed": 0, "added": 0, "removed": 0, "unreadable": 0}}
    assert client.get(url).json() == {"analysis_id": aid, "analyzing": False, "stale": clean, "base_id": None,
                                      "has_diff": False, "run": None, "running": False}
    (root / "orders.py").write_text(ORDERS + "\n# 改了\n", encoding="utf-8")
    (root / "new.py").write_text("y = 2\n", encoding="utf-8")
    stale = client.get(url).json()["stale"]
    assert stale["stale"] is True and stale["changed"] == ["orders.py"] and stale["added"] == ["new.py"]
    sid = client.get(f"/api/projects/{pid}/graph").json()["lines"][0]["sid"]
    assert stale["lines"] == [sid]
    # 按那次分析时的配置（排除 gen）：项目配置改了、还没重新分析，gen 里的文件照样不算新增
    assert client.put(f"/api/projects/{pid}/config", json={}).status_code == 200
    assert client.get(url).json()["stale"]["added"] == ["new.py"]
    mark = flows.mark_analyzing(client.app.state.store, pid)
    try:
        assert client.get(url).json()["analyzing"] is True
    finally:
        flows.unmark_analyzing(mark)
    error_of(client.get("/api/projects/nope/status"), 404)


def test_status_of_an_analysis_saved_by_the_old_version(client, orders, home):
    """结构版本 1 留下的分析没有配置和 file_lines：用项目现在的配置比，lines 为 null。"""
    pid, root = orders["project"]["id"], orders["root"]
    con = sqlite3.connect(home / "pystepmap.db")
    con.execute("update analyses set config_json = null, file_lines_json = null")
    con.commit()
    con.close()
    client.put(f"/api/projects/{pid}/config", json={})  # 现在不排除 gen 了
    (root / "orders.py").write_text(ORDERS + "\n", encoding="utf-8")
    stale = client.get(f"/api/projects/{pid}/status").json()["stale"]
    assert stale["changed"] == ["orders.py"] and stale["added"] == ["gen/made.py"] and stale["lines"] is None


def test_status_when_the_directory_is_gone_or_replaced(client, orders, tmp_path):
    pid, root = orders["project"]["id"], orders["root"]
    url = f"/api/projects/{pid}/status"
    os.rename(root, tmp_path / "moved")
    os.symlink(tmp_path / "moved", root)
    r = client.get(url)
    assert r.status_code == 200 and r.json()["stale"]["checked"] is False
    assert "不是添加时的那个目录" in r.json()["stale"]["reason"]
    os.unlink(root)
    r = client.get(url)
    assert r.status_code == 200 and r.json()["stale"] == {"checked": False, "reason": f"项目目录不存在了：{root}"}
    assert r.json()["analysis_id"] == orders["project"]["analysis"]["id"]


def test_diff_and_acknowledge(client, orders):
    pid, root = orders["project"]["id"], orders["root"]
    first = orders["project"]["analysis"]
    url = f"/api/projects/{pid}/diff"
    assert client.get(url).json() == {"head": first, "base": None, "diff": None, "baseline": None}
    add_branch(root)
    r = client.post(f"/api/projects/{pid}/analyze")
    assert r.status_code == 200, r.text
    head = r.json()["analysis"]
    assert head["base_id"] == first["id"] and head["has_diff"] is True
    body = client.get(url).json()
    assert body["head"] == head and body["base"] == first
    assert body["baseline"] == {"kind": "first", "at": orders["project"]["created_at"]}  # 没标为已看过：相对第一次分析
    assert body["diff"]["version"] == 1 and [ln["title"] for ln in body["diff"]["lines"]] == ["下单"]
    [fc] = [f for f in body["diff"]["functions"].values() if f["title"] == "下单"]  # 另有新增的判断函数本身
    assert any(n["status"] == "added" and n["new_title"] == "数量太多？" for n in fc["nodes"])
    status = client.get(f"/api/projects/{pid}/status").json()
    assert (status["base_id"], status["has_diff"]) == (first["id"], True)
    # 外来页面发来的"标为已看"被挡住，什么都没变
    r = client.post(f"{url}/ack", params={"analysis": head["id"]}, headers={"Origin": "http://evil.example"})
    assert "只接受本机页面发来的修改请求" in error_of(r, 403)
    assert client.get(url).json() == body
    assert "analysis" in error_of(client.post(f"{url}/ack"), 400)  # 必须说明看的是哪一次
    # 看的是旧的一次（最新一次已经换了）：409 moved，什么都没变
    r = client.post(f"{url}/ack", params={"analysis": first["id"]})
    assert error_of(r, 409) == "分析结果已经更新，请先看新的变化" and r.json()["code"] == "moved"
    assert client.get(url).json() == body
    r = client.post(f"{url}/ack", params={"analysis": head["id"]})
    assert r.status_code == 200, r.text
    acked = {**head, "base_id": None, "has_diff": False}
    assert r.json() == {"head": acked, "base": None, "diff": None, "baseline": None}
    assert client.get(url).json() == r.json()
    assert client.get(f"/api/projects/{pid}").json()["analysis"] == acked
    error_of(client.get("/api/projects/nope/diff"), 404)
    error_of(client.post("/api/projects/nope/diff/ack", params={"analysis": 1}), 404)
    error_of(client.get(f"{url}/ack"), 405)


def test_acknowledge_while_analyzing_is_409(client, orders):
    pid = orders["project"]["id"]
    mark = flows.mark_analyzing(client.app.state.store, pid)
    try:
        r = client.post(f"/api/projects/{pid}/diff/ack", params={"analysis": orders["project"]["analysis"]["id"]})
        assert error_of(r, 409) == "正在分析，等分析完再标为看过" and r.json()["code"] == "busy"
    finally:
        flows.unmark_analyzing(mark)


def test_diff_with_a_base_that_is_gone(client, orders, home):
    """基准那次被旧版本的服务清理掉了：/diff 当作没有可比的（base、diff 都是 null），项目信息也一致。"""
    pid, root = orders["project"]["id"], orders["root"]
    first = orders["project"]["analysis"]["id"]
    add_branch(root)
    client.post(f"/api/projects/{pid}/analyze")
    con = sqlite3.connect(home / "pystepmap.db")
    con.execute("delete from analyses where id = ?", (first,))
    con.commit()
    con.close()
    body = client.get(f"/api/projects/{pid}/diff").json()
    assert (body["base"], body["diff"], body["head"]["base_id"], body["head"]["has_diff"]) == (None, None, None, False)
    assert client.get(f"/api/projects/{pid}").json()["analysis"] == body["head"]
    status = client.get(f"/api/projects/{pid}/status").json()
    assert (status["base_id"], status["has_diff"]) == (None, False)


def test_diff_is_read_in_one_go(client, orders, monkeypatch):
    """head 和它的差异一次读出来（Store.latest_diff，一条语句），中间插不进别的进程的写入。"""
    store = client.app.state.store
    pid = orders["project"]["id"]
    calls: list[str] = []
    real = store.latest_diff
    monkeypatch.setattr(store, "latest_diff", lambda project_id: calls.append(project_id) or real(project_id))
    for name in ("list_analyses", "latest_analysis", "analysis_extra"):
        monkeypatch.setattr(store, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该分开读")))
    assert client.get(f"/api/projects/{pid}/diff").status_code == 200 and calls == [pid]


def test_source_judged_against_a_given_analysis(client, orders, transfer):
    pid, root = orders["project"]["id"], orders["root"]
    first = orders["project"]["analysis"]["id"]
    (root / "orders.py").write_text(ORDERS + "\n# 第二版\n", encoding="utf-8")
    second = client.post(f"/api/projects/{pid}/analyze").json()["analysis"]["id"]
    url = f"/api/projects/{pid}/source"
    q = {"file": "orders.py", "start": 1, "end": 2}
    assert client.get(url, params=q).json()["stale"] is False  # 默认按最新一次
    assert client.get(url, params={**q, "analysis": second}).json()["stale"] is False
    assert client.get(url, params={**q, "analysis": first}).json()["stale"] is True  # 第一次分析时的内容不一样
    # 那次分析不存在（已被清理）或不是这个项目的：按最新一次
    assert client.get(url, params={**q, "analysis": 999_999}).json()["stale"] is False
    other = transfer["analysis"]["id"]
    assert client.get(url, params={**q, "analysis": other}).json()["stale"] is False
    assert "analysis" in error_of(client.get(url, params={**q, "analysis": "abc"}), 400)


SLOW_RULES = {"extra_rules": [{"name": "slow", "entry_decorators": [["(a+)+$", "route"]]}]}


def test_analysis_timeout_is_504(client, orders, monkeypatch):
    pid, root = orders["project"]["id"], orders["root"]
    (root / "slow.py").write_text(f"@{'a' * 40}()\ndef handler():\n    return 1\n", encoding="utf-8")
    client.put(f"/api/projects/{pid}/config", json=SLOW_RULES)
    before = client.get(f"/api/projects/{pid}").json()
    monkeypatch.setenv("PYSTEPMAP_ANALYZE_TIMEOUT", "1")
    r = client.post(f"/api/projects/{pid}/analyze")
    assert error_of(r, 504).startswith("分析超过 1 秒，已中止") and r.json()["code"] == "timeout"
    assert client.get(f"/api/projects/{pid}").json() == before  # 上一次的结果原样保留，不在分析了


HOLDER = """
import sys
from pathlib import Path
from pystepmap_server.locks import try_lock_project
fd = try_lock_project(Path(sys.argv[1]), sys.argv[2])
print("locked" if fd is not None else "busy", flush=True)
sys.stdin.read()
"""


def test_analysis_in_another_process_shows_and_blocks(client, orders, home):
    """MCP 等另一个进程正在分析这个项目：列表、项目、状态里 analyzing 为 true；这边点重新分析得到 409。"""
    pid = orders["project"]["id"]
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home), pid], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert client.get("/api/projects").json()[-1]["analyzing"] is True
        assert client.get(f"/api/projects/{pid}").json()["analyzing"] is True
        assert client.get(f"/api/projects/{pid}/status").json()["analyzing"] is True
        r = client.post(f"/api/projects/{pid}/analyze")
        assert error_of(r, 409) == "正在分析" and r.json()["code"] == "busy"
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    assert client.get(f"/api/projects/{pid}").json()["analyzing"] is False
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200


def test_new_analysis_by_another_process_is_seen(client, orders, home):
    """另一个进程（MCP）用同一个数据目录重新分析了：这边列表、图、差异马上是新的（不靠缓存）。"""
    from pystepmap_server.flows import reanalyze_project
    from pystepmap_server.store import Store

    pid, root = orders["project"]["id"], orders["root"]
    add_branch(root)
    other = Store(home / "pystepmap.db")  # 另一个进程打开的是同一个库文件
    r = reanalyze_project(other, pid)
    assert r["kind"] == "ok"
    view = client.get(f"/api/projects/{pid}").json()
    assert view["analysis"]["id"] == r["analysis_id"] and view["analysis"]["has_diff"] is True
    assert client.get(f"/api/projects/{pid}/graph").headers["x-pystepmap-analysis"] == str(r["analysis_id"])
    assert client.get(f"/api/projects/{pid}/diff").json()["diff"] is not None
    assert client.get(f"/api/projects/{pid}/status").json()["stale"]["stale"] is False
    shutil.rmtree(root)


def test_listing_works_where_flock_is_not_supported(client, orders, monkeypatch):
    """数据目录在不支持 flock 的文件系统上：列项目、看状态、重新分析都照常（只在本进程内互斥），不是 500。"""
    import errno
    import fcntl

    from pystepmap_server import locks

    class NoFlock:
        LOCK_EX, LOCK_SH, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_NB, fcntl.LOCK_UN

        @staticmethod
        def flock(fd, op):
            raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(locks, "fcntl", NoFlock)
    pid = orders["project"]["id"]
    assert client.get("/api/projects").status_code == 200
    assert client.get(f"/api/projects/{pid}/status").json()["analyzing"] is False
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200



# ---------- 测试命令和运行 ----------
# 测试命令跑的是 tmp_path 里转账示例的副本（含它的测试），用当前的解释器

PYTEST = f"{shlex.quote(sys.executable)} -m pytest tests -q"


@pytest.fixture
def example(client, tmp_path) -> dict:
    """转账示例的副本，已添加。返回 {"root", "id"}。"""
    root = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    r = client.post("/api/projects", json={"root": str(root)})
    assert r.status_code == 201, r.text
    return {"root": root, "id": r.json()["id"]}


def set_command(client, pid: str, command: str, confirm: bool = True) -> None:
    assert client.put(f"/api/projects/{pid}/test-command", json={"command": command}).status_code == 200
    if confirm:
        client.app.state.store.confirm_test_command(pid, command)


def wait_for_run(client, pid: str, within: float = 60) -> dict:
    """轮询 GET …/runs，直到没有在跑的。返回最后一次的响应体。"""
    deadline = time.monotonic() + within
    while True:
        body = client.get(f"/api/projects/{pid}/runs").json()
        if body["running"] is None:
            return body
        assert time.monotonic() < deadline, body
        time.sleep(0.05)


def wait_until(check, within: float = 10) -> None:
    deadline = time.monotonic() + within
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_interpreters_found_in_root_and_direct_subdirs(client, example, tmp_path):
    root = example["root"]
    url = f"/api/projects/{example['id']}/interpreters"
    assert client.get(url).json() == {"interpreters": []}
    outside = tmp_path / "outside"
    for d in (root / ".venv", root / "backend" / "venv", root / "a" / "b" / ".venv", root / ".hidden" / ".venv", outside / ".venv"):
        (d / "bin").mkdir(parents=True)
        (d / "bin" / "python").write_text("", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)  # 链到项目外面的目录不看
    (root / "tools" / "env" / "bin").mkdir(parents=True)  # 有目录、没有解释器
    assert client.get(url).json() == {
        "interpreters": [{"cwd": "", "python": ".venv/bin/python"}, {"cwd": "backend", "python": "venv/bin/python"}]
    }
    error_of(client.get("/api/projects/nope/interpreters"), 404)


def test_test_command_get_and_put(client, example):
    url = f"/api/projects/{example['id']}/test-command"
    assert client.get(url).json() == {"command": None, "confirmed": False}
    assert client.put(url, json={"command": "  pytest -q \n"}).json() == {"command": "pytest -q", "confirmed": False}
    client.app.state.store.confirm_test_command(example["id"], "pytest -q")
    assert client.get(url).json() == {"command": "pytest -q", "confirmed": True}
    assert client.put(url, json={"command": "pytest -q"}).json() == {"command": "pytest -q", "confirmed": True}
    assert client.put(url, json={"command": "pytest -x"}).json() == {"command": "pytest -x", "confirmed": False}
    assert client.put(url, json={"command": "   "}).json() == {"command": None, "confirmed": False}  # 空 = 清掉
    assert client.put(url, json={"command": None}).json() == {"command": None, "confirmed": False}
    assert "2000" in error_of(client.put(url, json={"command": "x" * 2001}), 400)
    assert client.put(url, json={"command": " " + "x" * 2000 + " "}).status_code == 200  # 去掉首尾空白后再量
    assert "空字符" in error_of(client.put(url, json={"command": "a\0b"}), 400)
    error_of(client.put(url, json={"command": 3}), 400)
    error_of(client.put(url, json={"command": "x", "other": 1}), 400)
    error_of(client.put(url, json={}), 400)
    error_of(client.get("/api/projects/nope/test-command"), 404)
    error_of(client.put("/api/projects/nope/test-command", json={"command": "x"}), 404)


def test_run_needs_a_command_and_a_confirmation_then_runs_in_the_background(client, example):
    pid = example["id"]
    runs = f"/api/projects/{pid}/runs"
    r = client.post(runs, json={})
    assert "测试命令" in error_of(r, 400) and r.json()["code"] == "no_command"
    error_of(client.post("/api/projects/nope/runs", json={}), 404)
    set_command(client, pid, PYTEST, confirm=False)
    for body in ({}, {"confirm": False}, None):
        r = client.post(runs, json=body) if body is not None else client.post(runs)
        msg = error_of(r, 409)
        assert r.json()["code"] == "confirm" and r.json()["command"] == PYTEST and r.json()["root"] == str(example["root"])
        assert f"会在你的项目目录（{example['root']}）里执行" in msg and "真的访问" in msg
    assert client.get(runs).json() == {"running": None, "runs": [], "last_failure": None}
    r = client.post(runs, json={"confirm": True, "command": PYTEST})
    assert r.status_code == 202 and r.json() == {"run": "started"}
    assert client.get(f"/api/projects/{pid}/test-command").json()["confirmed"] is True
    body = wait_for_run(client, pid)
    [run] = body["runs"]
    assert body["last_failure"] is None and run["status"] == "ok" and run["command"] == PYTEST
    assert set(run) == {"id", "analysis_id", "started_at", "finished_at", "command", "status", "exit_code", "summary",
                        "paths_size", "scope"}
    assert run["scope"] is None
    assert run["summary"]["tests"] == 9 and run["summary"]["verify"]["ok"] is True
    detail = client.get(f"{runs}/{run['id']}").json()
    assert {k: v for k, v in detail.items() if k != "tests"} == run
    named = [t for t in detail["tests"] if t["test"]]
    assert len(named) == 6 and set(named[0]) == {"test", "outcome", "seconds", "cases", "functions"}
    lines = client.get(f"{runs}/output").json()["lines"]
    assert any("9 passed" in line for line in lines)
    status = client.get(f"/api/projects/{pid}/status").json()
    assert status["running"] is False
    assert status["run"] == {"id": run["id"], "status": "ok", "finished_at": run["finished_at"], "failed": 0,
                             "verify_ok": True}
    # 确认过了：再跑不用确认；命令改了就要重新确认
    assert client.post(runs, json={}).status_code == 202
    wait_for_run(client, pid)
    set_command(client, pid, PYTEST + " -x", confirm=False)
    assert client.post(runs, json={}).json()["code"] == "confirm"


def test_run_paths_whole_and_one_test(client, example):
    pid = example["id"]
    set_command(client, pid, PYTEST)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    [run] = wait_for_run(client, pid)["runs"]
    url = f"/api/projects/{pid}/runs/{run['id']}/paths"
    store = client.app.state.store
    r = client.get(url)  # 测试客户端说接受 gzip：原样发出存着的压缩数据
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.headers["x-pystepmap-run"] == str(run["id"]) and r.headers["x-pystepmap-analysis"] == str(run["analysis_id"])
    assert r.headers["x-pystepmap-paths-size"] == str(run["paths_size"])
    whole = r.json()
    assert whole["version"] == 1 and len([t for t in whole["tests"] if t["test"]]) == 6
    assert r.headers["content-length"] == str(len(store.run_paths_gz(pid, run["id"])))
    plain = client.get(url, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == whole
    name = "tests/test_transfer.py::test_transfer_succeeds"
    one = client.get(url, params={"test": name})
    assert one.status_code == 200 and one.headers["x-pystepmap-run"] == str(run["id"])
    sliced = one.json()  # 和整份同一个形状：运行一级的小项 + 只有这一个测试
    test = next(t for t in whole["tests"] if t["test"] == name)
    assert sliced["tests"] == [test] and set(sliced) == set(whole)
    for key in set(whole) - {"tests"}:
        expected = {f: whole[key][f] for f in test["functions"] if f in whole[key]} if key == "shapes" else whole[key]
        assert sliced[key] == expected, key
    error_of(client.get(url, params={"test": "tests/test_transfer.py::nope"}), 404)
    error_of(client.get(f"/api/projects/{pid}/runs/999/paths"), 404)
    error_of(client.get(f"/api/projects/{pid}/runs/999"), 404)
    error_of(client.get(f"/api/projects/{pid}/runs/abc"), 400)


def test_runs_of_another_project_are_not_found(client, example, orders):
    set_command(client, example["id"], "true")
    client.post(f"/api/projects/{example['id']}/runs", json={})
    [run] = wait_for_run(client, example["id"])["runs"]
    other = orders["project"]["id"]
    for path in ("", "/paths", "/uncovered"):
        error_of(client.get(f"/api/projects/{other}/runs/{run['id']}{path}"), 404)


def test_busy_while_analysing_and_while_running(client, example, tmp_path):
    pid = example["id"]
    flag = tmp_path / "go"
    set_command(client, pid, f"while [ ! -f {flag} ]; do sleep 0.05; done; echo done")
    store = client.app.state.store
    mark = flows.mark_analyzing(store, pid)
    try:
        r = client.post(f"/api/projects/{pid}/runs", json={})
        assert r.json()["code"] == "busy" and "正在分析或运行测试" in error_of(r, 409)
    finally:
        flows.unmark_analyzing(mark)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    try:
        wait_until(lambda: (client.get(f"/api/projects/{pid}/runs").json()["running"] or {}).get("phase") == "testing")
        running = client.get(f"/api/projects/{pid}/runs").json()["running"]
        assert running["started_at"] and running["seconds"] >= 0
        r = client.post(f"/api/projects/{pid}/runs", json={})
        assert r.json()["code"] == "busy" and error_of(r, 409)
        r = client.post(f"/api/projects/{pid}/analyze")
        assert r.json()["code"] == "busy" and "正在运行测试" in error_of(r, 409)
        # 测试在跑：状态、列表照常应答；analyzing 说的是分析，running 说的是测试
        status = client.get(f"/api/projects/{pid}/status").json()
        assert status["running"] is True and status["analyzing"] is False
        assert client.get(f"/api/projects/{pid}").json()["analyzing"] is False
        assert client.get("/api/projects").status_code == 200
    finally:
        flag.write_text("1")
    assert wait_for_run(client, pid)["runs"][0]["status"] == "error"  # echo done 没有跑 pytest：没有记录


def test_cancel_a_run(client, example, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "INT_GRACE", 0.3)  # sh 的后台进程不理 SIGINT：不等满 5 秒
    pid = example["id"]
    pidfile = tmp_path / "pgid"
    set_command(client, pid, f"echo $$ > {pidfile}; sleep 30 & sleep 30")
    r = client.post(f"/api/projects/{pid}/runs/cancel")
    assert r.json()["code"] == "not_running" and error_of(r, 409)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    wait_until(lambda: pidfile.exists() and pidfile.read_text().strip())
    r = client.post(f"/api/projects/{pid}/runs/cancel")
    assert r.status_code == 200 and r.json() == {"cancelling": True}  # 测试命令在跑：结束它
    [run] = wait_for_run(client, pid)["runs"]
    assert run["status"] == "cancelled" and run["paths_size"] is None
    pgid = int(pidfile.read_text())
    wait_until(lambda: not alive(pgid), within=3)
    error_of(client.get(f"/api/projects/{pid}/runs/{run['id']}/paths"), 404)
    error_of(client.get(f"/api/projects/{pid}/runs/{run['id']}/uncovered"), 404)
    assert client.post(f"/api/projects/{pid}/runs/cancel").status_code == 409


def alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


GATE_TEST = """
import os
import time


def test_zz_waits_for_the_gate():
    gate = os.environ["PYSTEPMAP_TEST_GATE"]
    open(gate + ".reached", "w").close()
    deadline = time.monotonic() + 60
    while not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(0.02)
"""


def test_progress_while_the_tests_run(client, example, tmp_path, monkeypatch):
    """测试跑着的时候（阶段 testing）GET …/runs 的 running 里有 progress：跑完了几个、正在跑哪个、最近跑完的、走到过的
    功能线；?live_line= 时还有这条线上走到过的节点。跑完了（running 为 null）就没有了。旧的字段照旧。"""
    gate = tmp_path / "gate"
    monkeypatch.setenv("PYSTEPMAP_TEST_GATE", str(gate))
    (example["root"] / "tests" / "test_zz_gate.py").write_text(GATE_TEST, encoding="utf-8")
    pid = example["id"]
    runs = f"/api/projects/{pid}/runs"
    set_command(client, pid, PYTEST)
    graph = client.get(f"/api/projects/{pid}/graph").json()
    line = graph["lines"][0]["sid"]
    wf = next(k for k, f in graph["functions"].items() if f["qualname"] == "TransferWorkflow.run")
    step = next(n["sid"] for n in graph["functions"][wf]["nodes"] if n["title"] == "执行转账")
    assert client.post(runs, json={}).status_code == 202
    try:
        wait_until(lambda: (tmp_path / "gate.reached").exists(), within=60)
        wait_until(lambda: (client.get(runs).json()["running"] or {}).get("progress", {}).get("done") == 9, within=10)
        running = client.get(runs, params={"live_line": line}).json()["running"]
        assert running["phase"] == "testing" and {"started_at", "seconds", "phase", "progress", "scope"} == set(running)
        assert running["scope"] is None  # 全部测试
        p = running["progress"]
        assert (p["collected"], p["done"], p["passed"], p["failed"], p["skipped"], p["errors"]) == (10, 9, 9, 0, 0, 0)
        assert p["running"] == ["tests/test_zz_gate.py::test_zz_waits_for_the_gate"] and p["running_more"] == 0
        assert len(p["recent"]) == 8 and all(set(r) == {"test", "outcome", "seconds"} for r in p["recent"])
        assert p["lines"][line]["functions"] > 1 and p["lines"][line]["total"] >= p["lines"][line]["functions"]
        assert "start" in p["nodes"][wf] and step in p["nodes"][wf]
        assert "nodes" not in client.get(runs).json()["running"]["progress"]
        assert "nodes" not in client.get(runs, params={"live_line": "line:nope.py::x"}).json()["running"]["progress"]
    finally:
        gate.write_text("1")
    body = wait_for_run(client, pid)
    assert body["running"] is None and body["runs"][0]["summary"]["tests"] == 10


def test_no_progress_outside_the_testing_phase(client, example, tmp_path, monkeypatch):
    """测试命令跑完、开始整理结果（processing）时进度已经丢了；准备、整理结果的时候都没有 progress。"""
    pid = example["id"]
    flag = tmp_path / "go"
    set_command(client, pid, f"while [ ! -f {flag} ]; do sleep 0.05; done; echo done")
    seen = []
    real = runner.set_phase

    def watch(home, project_id, phase):
        seen.append((phase, client.get(f"/api/projects/{pid}/runs").json()["running"]))
        real(home, project_id, phase)

    monkeypatch.setattr(runner, "set_phase", watch)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    wait_until(lambda: (client.get(f"/api/projects/{pid}/runs").json()["running"] or {}).get("phase") == "testing")
    assert "progress" in client.get(f"/api/projects/{pid}/runs").json()["running"]
    flag.write_text("1")
    wait_for_run(client, pid)
    assert [phase for phase, _ in seen] == ["testing", "processing"]
    assert [now["phase"] for _, now in seen] == ["preparing", "testing"]
    assert all("progress" not in now for _, now in seen)  # 进入 testing 之前、测试命令结束之后都没有


def test_failures_after_the_start_are_reported_in_the_listing(client, example, monkeypatch):
    """202 之后才失败（运行前的分析失败）：GET …/runs 的 last_failure 说原因，没有存运行。"""
    pid = example["id"]
    set_command(client, pid, PYTEST)
    (example["root"] / "transfer_workflow.py").write_text("x = 1\n", encoding="utf-8")  # 图过期：先重新分析
    engine_in_process(monkeypatch, lambda root, config: 1 / 0)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    body = wait_for_run(client, pid)
    assert body["runs"] == [] and body["last_failure"]["code"] == "analysis"
    assert "ZeroDivisionError" in body["last_failure"]["reason"] and body["last_failure"]["at"]
    monkeypatch.undo()
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    body = wait_for_run(client, pid)
    assert body["last_failure"] is None and len(body["runs"]) == 1


def test_uncovered_outcomes_by_sid_also_after_reanalysis(client, example):
    pid = example["id"]
    set_command(client, pid, PYTEST)
    client.post(f"/api/projects/{pid}/runs", json={})
    [run] = wait_for_run(client, pid)["runs"]
    graph = client.get(f"/api/projects/{pid}/graph").json()
    line = graph["lines"][0]["sid"]
    url = f"/api/projects/{pid}/runs/{run['id']}/uncovered"
    body = client.get(url).json()
    assert body["stale"] is False
    [only] = body["outcomes"]
    assert only["title"] == "超过单笔限额，已拒绝" and only["end_kind"] == "failed"
    assert only["file"] == TRANSFER_FILE and only["line"] > 0 and only["function"].endswith("TransferWorkflow.run")
    assert only["node"].startswith("end:") and only["uncertain"] is False
    assert set(only) == {"function", "node", "title", "end_kind", "file", "line", "uncertain", "added"}
    assert client.get(url, params={"line": line}).json() == body
    assert "没有这条功能线" in error_of(client.get(url, params={"line": "line:nope.py::x"}), 404)
    # 代码改了、重新分析了：按 sid 叠到新图上，标明是旧的运行
    p = example["root"] / TRANSFER_FILE
    p.write_text("# 加一行，行号都变了\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200
    again = client.get(url).json()
    assert again["stale"] is True
    assert [(o["node"], o["title"], o["uncertain"]) for o in again["outcomes"]] == [(only["node"], only["title"], False)]
    assert again["outcomes"][0]["line"] == only["line"] + 1
    # 再把这个结局连同它的判断复制一份（同文本的两个结局）：这个函数的节点变了，两个都说不准，不算走到也不算没走到
    text = p.read_text(encoding="utf-8")
    block = text[text.index("        if self.exceeds_single_limit(req):"):text.index("        balance = await")]
    p.write_text(text.replace(block, block + block), encoding="utf-8")
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200
    unsure = client.get(url).json()["outcomes"]
    assert [(o["title"], o["uncertain"]) for o in unsure] == [("超过单笔限额，已拒绝", True)] * 2


def test_cross_site_run_requests_are_refused(client, example):
    pid = example["id"]
    set_command(client, pid, "true")
    h = {"Origin": EVIL}
    for r in (client.post(f"/api/projects/{pid}/runs", json={"confirm": True, "command": "true"}, headers=h),
              client.post(f"/api/projects/{pid}/runs/cancel", headers=h),
              client.put(f"/api/projects/{pid}/test-command", json={"command": "rm -rf ~"}, headers=h)):
        assert "只接受本机页面发来的修改请求" in error_of(r, 403)
    assert client.get(f"/api/projects/{pid}/test-command").json() == {"command": "true", "confirmed": True}
    assert client.get(f"/api/projects/{pid}/runs").json()["runs"] == []
    r = client.post(f"/api/projects/{pid}/runs", json={}, headers={"Origin": "http://localhost:5180"})
    assert r.status_code == 202
    wait_for_run(client, pid)


def test_deleting_a_project_stops_its_run(client, example, tmp_path, home):
    pid = example["id"]
    pidfile = tmp_path / "pgid"
    set_command(client, pid, f"echo $$ > {pidfile}; sleep 30")
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    wait_until(lambda: pidfile.exists() and pidfile.read_text().strip())
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    wait_until(lambda: client.get(f"/api/projects/{pid}/runs").status_code == 404 and not alive(int(pidfile.read_text())))
    wait_until(lambda: not list((home / "runs").glob("tmp-*")) and not flows.is_analyzing(client.app.state.store, pid))


def test_closing_the_app_stops_running_tests(home, tmp_path, monkeypatch):
    """服务正常退出（这里是测试客户端关掉，和 pystepmap serve 收到 Ctrl+C 走同一个收尾）：在跑的测试进程组被结束，
    这次运行记成已取消，锁放掉。"""
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    root = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    pidfile = tmp_path / "pgid"
    with TestClient(create_app(home), base_url=LOCAL) as c:
        pid = c.post("/api/projects", json={"root": str(root)}).json()["id"]
        set_command(c, pid, f"echo $$ > {pidfile}; sleep 30")
        assert c.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
        wait_until(lambda: pidfile.exists() and pidfile.read_text().strip())
        store = c.app.state.store
    assert not alive(int(pidfile.read_text()))
    [run] = store.list_runs(pid)
    assert run["status"] == "cancelled" and run["summary"]["note"] == "服务停止时测试还在跑，被中止了"  # 和用户取消分开说
    assert not flows.is_analyzing(store, pid) and list((home / "runs").glob("tmp-*")) == []


def test_the_app_removes_scratch_left_by_a_killed_server(home, monkeypatch):
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    (home / "runs" / "tmp-left").mkdir(parents=True)
    (home / "runs" / "tmp-left" / "trace.jsonl").write_text("{}")
    with TestClient(create_app(home), base_url=LOCAL):
        assert not (home / "runs" / "tmp-left").exists()


def expected_on_line(store, pid: str, run_id: int, line_sid: str) -> dict[str, int]:
    """每个测试执行了这条功能线上的几个函数：直接用 core（整份路径、最新的图、locate_function）算。"""
    from pystepmap.query import line_functions
    from pystepmap.runpath import locate_function, run_paths_from_json

    graph = from_json(store.graph_json(store.latest_analysis(pid)["id"]))
    paths = run_paths_from_json(json.loads(gzip.decompress(store.run_paths_gz(pid, run_id))))
    keys = set(line_functions(graph, next(ln for ln in graph.lines if ln.sid == line_sid)))
    return {t.test: len({fg.key for f in t.functions if (fg := locate_function(graph, paths, f)) is not None
                         and fg.key in keys}) for t in paths.tests}


def test_tests_of_a_run_counted_on_a_feature_line(client, example, home):
    pid = example["id"]
    set_command(client, pid, PYTEST)
    client.post(f"/api/projects/{pid}/runs", json={})
    [run] = wait_for_run(client, pid)["runs"]
    store = client.app.state.store
    url = f"/api/projects/{pid}/runs/{run['id']}"
    plain = client.get(url).json()
    assert "line_known" not in plain and all("on_line" not in t for t in plain["tests"])  # 不给 line：和原来一样
    line = client.get(f"/api/projects/{pid}/graph").json()["lines"][0]["sid"]
    body = client.get(url, params={"line": line}).json()
    assert body["line_known"] is True
    assert {k: v for k, v in body.items() if k not in ("tests", "line_known")} == \
        {k: v for k, v in plain.items() if k != "tests"}
    assert [{k: v for k, v in t.items() if k != "on_line"} for t in body["tests"]] == plain["tests"]
    expected = expected_on_line(store, pid, run["id"], line)
    assert {t["test"]: t["on_line"] for t in body["tests"]} == expected
    assert expected["tests/test_transfer.py::test_transfer_succeeds"] >= 5
    assert "没有这条功能线" in error_of(client.get(url, params={"line": "line:nope.py::x"}), 404)
    # 代码改了、重新分析了：按 core 的规则放到新图上再数
    p = example["root"] / TRANSFER_FILE
    p.write_text("# 加一行\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert client.post(f"/api/projects/{pid}/analyze").status_code == 200
    again = client.get(url, params={"line": line}).json()
    assert {t["test"]: t["on_line"] for t in again["tests"]} == expected_on_line(store, pid, run["id"], line)
    # 没有函数索引的运行（本阶段较早的代码存的）：line_known 为假，不给 on_line
    con = sqlite3.connect(home / "pystepmap.db")
    con.execute("update runs set functions_gz = null")
    con.commit()
    con.close()
    with TestClient(create_app(home), base_url=LOCAL) as fresh:  # 运行存下以后不会变，缓存按（分析，运行）：换一个服务读
        old = fresh.get(url, params={"line": line}).json()
        assert old["line_known"] is False and all("on_line" not in t for t in old["tests"])
        assert "没有这条功能线" in error_of(fresh.get(url, params={"line": "line:nope.py::x"}), 404)


def test_confirming_confirms_the_command_that_was_shown(client, example):
    """确认的是界面弹窗里给用户看的那一条：确认时带上它（command），和现在存着的不一样（中间被改了）时不确认、不运行，
    409 confirm 带着现在的命令重新问。"""
    pid = example["id"]
    runs = f"/api/projects/{pid}/runs"
    set_command(client, pid, "echo SHOWN", confirm=False)
    shown = client.post(runs, json={})
    assert shown.json()["code"] == "confirm" and shown.json()["command"] == "echo SHOWN"
    set_command(client, pid, "echo NEVER_SHOWN", confirm=False)  # 另一个标签页里改了
    r = client.post(runs, json={"confirm": True, "command": "echo SHOWN"})
    assert r.json()["code"] == "confirm" and r.json()["command"] == "echo NEVER_SHOWN" and error_of(r, 409)
    r = client.post(runs, json={"confirm": True})  # 确认时没说确认的是哪一条
    assert r.json()["code"] == "confirm" and r.json()["command"] == "echo NEVER_SHOWN" and error_of(r, 409)
    assert client.get(f"/api/projects/{pid}/test-command").json() == {"command": "echo NEVER_SHOWN", "confirmed": False}
    assert client.get(runs).json()["runs"] == [] and runner.last_output(client.app.state.store.path.parent, pid) == []
    r = client.post(runs, json={"confirm": True, "command": "echo NEVER_SHOWN"})
    assert r.status_code == 202
    assert wait_for_run(client, pid)["runs"][0]["command"] == "echo NEVER_SHOWN"
    error_of(client.post(runs, json={"confirm": True, "command": 3}), 400)


def test_status_says_nothing_was_verified_when_no_records_arrived(client, example):
    pid = example["id"]
    set_command(client, pid, "true")
    client.post(f"/api/projects/{pid}/runs", json={})
    [run] = wait_for_run(client, pid)["runs"]
    assert run["summary"]["verify"]["ok"] is None
    assert client.get(f"/api/projects/{pid}/status").json()["run"]["verify_ok"] is None


def test_cancel_while_processing_is_too_late(client, example, monkeypatch):
    """测试已经跑完、正在读记录汇总：取消不了，409 too_late；这次运行照常存完。"""
    from pystepmap_server import testflows

    pid = example["id"]
    set_command(client, pid, PYTEST)
    entered, release = threading.Event(), threading.Event()
    real = testflows._verify_records

    def slow(prepared):
        entered.set()
        release.wait(30)
        return real(prepared)

    monkeypatch.setattr(testflows, "_verify_records", slow)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    try:
        assert entered.wait(30)
        assert client.get(f"/api/projects/{pid}/runs").json()["running"]["phase"] == "processing"
        r = client.post(f"/api/projects/{pid}/runs/cancel")
        assert r.json() == {"error": "测试已经跑完，正在整理结果，取消不了", "code": "too_late"} and r.status_code == 409
    finally:
        release.set()
    assert wait_for_run(client, pid)["runs"][0]["status"] == "ok"


def test_cancel_during_the_analysis_before_the_tests(client, example, monkeypatch):
    """运行前的重新分析期间取消：200，说"正在重新分析，分析完就停"；分析完以后不跑测试，记成已取消。"""
    pid = example["id"]
    set_command(client, pid, PYTEST)
    p = example["root"] / TRANSFER_FILE
    p.write_text(p.read_text(encoding="utf-8") + "\n# 图过期了\n", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    def slow(root, config):
        entered.set()
        release.wait(30)
        return analyze_with_notes(root, config)

    engine_in_process(monkeypatch, slow)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    try:
        assert entered.wait(30)
        assert client.get(f"/api/projects/{pid}/runs").json()["running"]["phase"] == "analyzing"
        r = client.post(f"/api/projects/{pid}/runs/cancel")
        assert r.status_code == 200 and r.json() == {"cancelling": True, "note": "正在重新分析，分析完就停"}
    finally:
        release.set()
    [run] = wait_for_run(client, pid)["runs"]
    assert run["status"] == "cancelled" and run["summary"]["tests"] == 0
    assert runner.last_output(client.app.state.store.path.parent, pid)[-1] == "[pystepmap] 运行在开始之前就被取消了"


def test_a_run_that_needs_no_analysis_is_preparing_not_analysing(client, example, monkeypatch):
    """图没过期：开始时的阶段是 preparing，从不报成 analyzing（项目列表不说正在分析）；这时取消不说"正在重新分析"，
    测试不开始，记成已取消。"""
    pid = example["id"]
    set_command(client, pid, PYTEST)
    entered, release = threading.Event(), threading.Event()
    make_scratch = runner.make_scratch

    def slow_scratch(home):
        entered.set()
        release.wait(30)
        return make_scratch(home)

    monkeypatch.setattr(runner, "make_scratch", slow_scratch)
    assert client.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
    try:
        assert entered.wait(30)
        assert client.get(f"/api/projects/{pid}/runs").json()["running"]["phase"] == "preparing"
        assert client.get(f"/api/projects/{pid}").json()["analyzing"] is False
        r = client.post(f"/api/projects/{pid}/runs/cancel")
        assert r.status_code == 200 and r.json() == {"cancelling": True}
    finally:
        release.set()
    [run] = wait_for_run(client, pid)["runs"]
    assert run["status"] == "cancelled" and run["summary"]["tests"] == 0


def test_run_ids_too_large_for_the_database_are_not_found(client, example):
    pid = example["id"]
    for rid in (2 ** 63, 2 ** 70, -1, 0):
        for path in ("", "/paths", "/paths?test=x", "/uncovered"):
            error_of(client.get(f"/api/projects/{pid}/runs/{rid}{path}"), 404)


def test_a_run_stopped_by_shutdown_during_the_analysis_says_so(home, tmp_path, monkeypatch):
    """服务退出时正在运行前的重新分析：测试还没开始，说明写"服务停止时正在重新分析，测试还没开始"。"""
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    root = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    entered, release = threading.Event(), threading.Event()

    def slow(root_, config):
        entered.set()
        release.wait(30)
        return analyze_with_notes(root_, config)

    with TestClient(create_app(home), base_url=LOCAL) as c:
        pid = c.post("/api/projects", json={"root": str(root)}).json()["id"]
        set_command(c, pid, PYTEST)
        (root / TRANSFER_FILE).write_text((root / TRANSFER_FILE).read_text(encoding="utf-8") + "\n# 改了\n",
                                         encoding="utf-8")
        engine_in_process(monkeypatch, slow)
        assert c.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
        assert entered.wait(30)

        def release_on_shutdown():
            deadline = time.monotonic() + 30
            while runner.cancel_reason(home, pid) != "shutdown" and time.monotonic() < deadline:
                time.sleep(0.02)
            release.set()

        threading.Thread(target=release_on_shutdown, daemon=True).start()
        store = c.app.state.store
    [run] = store.list_runs(pid)
    assert run["status"] == "cancelled" and run["summary"]["note"] == "服务停止时正在重新分析，测试还没开始"


def test_a_run_stopped_by_shutdown_while_preparing_says_the_tests_had_not_started(home, tmp_path, monkeypatch):
    """服务退出时还在准备（图没过期，没有重新分析）：说明写"服务停止时测试还没开始"，不说正在重新分析。"""
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(home / "no-web"))
    root = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    entered, release = threading.Event(), threading.Event()
    make_scratch = runner.make_scratch

    def slow_scratch(home_):
        entered.set()
        release.wait(30)
        return make_scratch(home_)

    with TestClient(create_app(home), base_url=LOCAL) as c:
        pid = c.post("/api/projects", json={"root": str(root)}).json()["id"]
        set_command(c, pid, PYTEST)
        monkeypatch.setattr(runner, "make_scratch", slow_scratch)
        assert c.post(f"/api/projects/{pid}/runs", json={}).status_code == 202
        assert entered.wait(30)

        def release_on_shutdown():
            deadline = time.monotonic() + 30
            while runner.cancel_reason(home, pid) != "shutdown" and time.monotonic() < deadline:
                time.sleep(0.02)
            release.set()

        threading.Thread(target=release_on_shutdown, daemon=True).start()
        store = c.app.state.store
    [run] = store.list_runs(pid)
    assert run["status"] == "cancelled" and run["summary"]["note"] == "服务停止时测试还没开始"


def test_requirements_endpoint_lists_them_with_comparison_and_a_design_graph(client, example):
    url = f"/api/projects/{example['id']}/requirements"
    d = client.get(url).json()
    [r] = d["requirements"]
    assert (r["id"], r["title"], r["status"], r["line"], r["errors"]) == ("R001", "账户转账", "已确认", "账户转账", [])
    assert r["comparison"]["matches"] is True and r["comparison"]["differences"] == [] and r["reachable_status"] == "已实现"
    assert r["text"] == (example["root"] / r["file"]).read_text(encoding="utf-8") and r["text"].startswith("# 账户转账")
    assert r["comparison"]["line_sid"] == "line:transfer_workflow.py::TransferWorkflow.run" and d["run"] is None
    assert {o["covered"] for o in r["comparison"]["outcomes"]} == {"unknown"} and len(r["comparison"]["outcomes"]) == 7
    fn = d["graph"]["functions"][r["design_line"]]
    assert [ln["id"] for ln in d["graph"]["lines"]] == [r["design_line"]] and fn["nodes"][0]["type"] == "start"
    # 改设计：多一步、读不懂的另一条
    folder = example["root"] / ".pystepmap" / "requirements"
    f = folder / "R001-账户转账.md"
    f.write_text(f.read_text(encoding="utf-8").replace("步骤：冻结资金\n", "步骤：冻结资金\n步骤：风控打分\n"), encoding="utf-8")
    (folder / "R002-坏的.md").write_text("# 坏的\n- 编号：R002\n- 状态：草稿\n## 设计的流程\n```flow\n乱写\n```\n", encoding="utf-8")
    d = client.get(url).json()  # 不用重新分析就看得到
    one, bad = d["requirements"]
    [diff] = one["comparison"]["differences"]
    assert (diff["kind"], diff["title"], one["reachable_status"]) == ("missing", "风控打分", "已确认")
    assert len(diff["design_nodes"]) == 1 and diff["design_nodes"][0].startswith("step:L")
    assert bad["comparison"] is None and bad["design_line"] is None and bad["errors"]
    assert list(d["graph"]["functions"]) == [one["design_line"]]
    error_of(client.get("/api/projects/nope/requirements"), 404)


# ---------- 只跑一条功能线的测试、MCP 的开关 ----------

LINE = "line:transfer_workflow.py::TransferWorkflow.run"


def test_line_tests_and_a_line_run_through_the_api(client, example):
    pid = example["id"]
    base = f"/api/projects/{pid}"
    assert client.put(f"{base}/test-command", json={"command": PYTEST}).status_code == 200
    client.app.state.store.confirm_test_command(pid, PYTEST)
    asked = client.get(f"{base}/line-tests", params={"line": LINE}).json()
    assert (asked["ok"], asked["code"], asked["tests"], asked["base_run"]) == (False, "no_full_run", [], None)
    r = client.post(f"{base}/runs", json={"line": LINE})
    assert r.status_code == 409 and r.json()["code"] == "no_full_run" and "先运行一次全部测试" in r.json()["error"]
    assert client.get(f"{base}/line-tests", params={"line": "line:x.py::nope"}).json()["code"] == "no_full_run"  # 先说这个
    assert client.post(f"{base}/runs", json={}).status_code == 202
    [full] = wait_for_run(client, pid)["runs"]
    asked = client.get(f"{base}/line-tests", params={"line": LINE}).json()
    assert asked["ok"] is True and asked["base_run"] == full["id"] and len(asked["tests"]) == 6
    error_of(client.get(f"{base}/line-tests", params={"line": "line:x.py::nope"}), 404)
    error_of(client.post(f"{base}/runs", json={"line": "line:x.py::nope"}), 404)
    assert client.post(f"{base}/runs", json={"line": LINE}).status_code == 202
    now = client.get(f"{base}/runs").json()["running"]
    assert now is None or now["scope"] == {"line": LINE, "line_title": "账户转账", "tests": 6}  # 跑得快时已经结束了
    scoped, kept = wait_for_run(client, pid)["runs"]
    assert kept["id"] == full["id"] and kept["scope"] is None
    assert scoped["scope"] == {"line": LINE, "line_title": "账户转账", "tests": 6, "base_run": full["id"]}
    assert scoped["status"] == "ok" and scoped["summary"]["tests"] == 9
    # 没走到的结局：单条线的运行只回答它那条线
    assert len(client.get(f"{base}/runs/{scoped['id']}/uncovered", params={"line": LINE}).json()["outcomes"]) == 1
    r = client.get(f"{base}/runs/{scoped['id']}/uncovered")
    assert r.status_code == 409 and r.json()["code"] == "scoped" and r.json()["line"] == LINE
    assert client.get(f"{base}/runs/{full['id']}/uncovered").status_code == 200


def test_runs_asked_for_by_mcp_need_the_switch_a_line_and_a_confirmed_command(client, example):
    pid = example["id"]
    base = f"/api/projects/{pid}"
    client.put(f"{base}/test-command", json={"command": PYTEST})
    r = client.post(f"{base}/runs", json={"line": LINE, "via": "mcp"})
    assert r.status_code == 403 and r.json()["code"] == "mcp_off" and "由用户在平台界面" in r.json()["error"]
    assert client.put(f"{base}/mcp-tests", json={"allow": True}).json() == {"allow": True}
    assert client.get(base).json()["mcp_tests"] is True
    r = client.post(f"{base}/runs", json={"line": LINE, "via": "mcp"})  # 命令还没确认：MCP 不能替用户确认
    assert r.status_code == 409 and r.json()["code"] == "confirm" and "经 MCP 不能确认" in r.json()["error"]
    for body in ({"via": "mcp"}, {"line": LINE, "via": "mcp", "confirm": True, "command": PYTEST}):
        r = client.post(f"{base}/runs", json=body)
        assert r.status_code == 403 and r.json()["code"] == "mcp_scope", body
    assert client.get(f"{base}/test-command").json()["confirmed"] is False and client.get(f"{base}/runs").json()["runs"] == []
    error_of(client.post(f"{base}/runs", json={"line": LINE, "via": "someone"}), 400)
    client.app.state.store.confirm_test_command(pid, PYTEST)
    r = client.post(f"{base}/runs", json={"line": LINE, "via": "mcp"})
    assert r.status_code == 409 and r.json()["code"] == "no_full_run"  # 开关、确认都有了：剩下的和界面一样
    assert client.put(f"{base}/mcp-tests", json={"allow": False}).json() == {"allow": False}
    assert client.post(f"{base}/runs", json={"line": LINE, "via": "mcp"}).status_code == 403
    error_of(client.put("/api/projects/nope/mcp-tests", json={"allow": True}), 404)
    error_of(client.put(f"{base}/mcp-tests", json={"allow": "yes please"}), 400)


def test_marking_changes_as_seen_leaves_a_review_record(client, orders):
    """改了流程、重新分析、标为已看：变更记录里多一条，明细里有这次变化的文字；之后差异栏空了，记录还在。"""
    pid = orders["project"]["id"]
    base = f"/api/projects/{pid}"
    assert client.get(f"{base}/reviews").json() == {"reviews": []}
    src = orders["root"] / "orders.py"
    text = src.read_text(encoding="utf-8")
    branch = '    if lacks_stock(stock):\n        return outcome("failed", title="库存不够，没有下单")\n'
    assert branch in text
    src.write_text(text.replace(branch, ""), encoding="utf-8")
    head = client.post(f"{base}/analyze").json()["analysis"]
    assert head["has_diff"] is True
    assert client.post(f"{base}/diff/ack", params={"analysis": head["id"]}).status_code == 200
    [rec] = client.get(f"{base}/reviews").json()["reviews"]
    assert rec["head_id"] == head["id"] and rec["summary"]["removed"] >= 1 and rec["summary"]["lines"]
    one = client.get(f"{base}/reviews/{rec['id']}").json()
    assert "库存不够，没有下单" in one["text"] and one["diff"]["version"] == 1 and one["summary"] == rec["summary"]
    assert [ln["title"] for ln in rec["summary"]["lines"]] == ["下单"]
    assert client.get(f"{base}/diff").json()["diff"] is None  # 差异栏空了，记录还在
    # 当时的图也在：和标为已看的那次分析的图一样，diff 里的 new_id 是它的节点
    then = client.get(f"{base}/reviews/{rec['id']}/graph")
    assert then.status_code == 200 and then.headers["X-Pystepmap-Analysis"] == str(head["id"])
    assert then.json() == client.get(f"{base}/graph").json() and rec["graph_size"] == len(then.content)
    error_of(client.get(f"{base}/reviews/999/graph"), 404)
    error_of(client.get(f"{base}/reviews/999"), 404)
    error_of(client.get("/api/projects/nope/reviews"), 404)
