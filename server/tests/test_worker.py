"""分析子进程：结果和进程内分析逐字节相同；卡住时按超时杀掉、不留下子进程；配置不合法、异常退出、输出不对各有说法；
不导入被分析项目的代码（项目里放一个会在导入时退出的 json.py）；引擎里的 print 不混进结果。"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pystepmap.analyze import analyze_with_notes
from pystepmap.config import parse_config
from pystepmap.engineid import ENGINE_VERSION
from pystepmap.model import to_json
from pystepmap.query import file_lines
from pystepmap_server import worker
from pystepmap_server.worker import (
    analyze_timeout,
    check_in_subprocess,
    run_in_subprocess,
    summarize,
)

REPO = Path(__file__).resolve().parents[2]
TRANSFER = (REPO / "examples" / "transfer").resolve()

# 灾难性回溯：(a+)+$ 去匹配一长串 a 后面跟着 "()" 的装饰器源码，要试 2^40 种拆法
SLOW_RULES = {"extra_rules": [{"name": "slow", "entry_decorators": [["(a+)+$", "route"]]}]}
SLOW_FILE = f"@{'a' * 40}()\ndef handler():\n    return 1\n"


def workers_of_this_process() -> list[str]:
    """本进程的子进程里还在的分析进程（ps 列出的命令行）。"""
    out = subprocess.run(["ps", "-A", "-o", "ppid=,pid=,command="], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines()
            if line.split(None, 1)[0] == str(os.getpid()) and "pystepmap_server.worker" in line]


def without_seconds(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k != "seconds"}


def test_subprocess_result_is_the_same_as_in_process(tmp_path):
    r = run_in_subprocess(str(TRANSFER), {}, timeout=60, cwd=tmp_path)
    assert r["ok"] is True, r
    graph, notes = analyze_with_notes(TRANSFER, parse_config({}))
    assert r["graph_json"] == to_json(graph)  # 逐字节相同
    assert without_seconds(r["summary"]) == without_seconds(summarize(graph, notes, 0.0))
    assert isinstance(r["summary"]["seconds"], float | int)
    assert r["engine_version"] == ENGINE_VERSION
    assert r["file_lines"] == file_lines(graph) and r["file_lines"]
    assert set(r) == {"ok", "graph_json", "summary", "engine_version", "file_lines"}
    assert worker.analyze_directory(str(TRANSFER), {})["graph_json"] == r["graph_json"]  # 进程内的同一个函数


def test_catastrophic_regex_is_stopped_by_the_timeout(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "slow.py").write_text(SLOW_FILE, encoding="utf-8")
    started = time.monotonic()
    r = run_in_subprocess(str(proj), SLOW_RULES, timeout=2, cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert r == {"ok": False, "code": "timeout", "reason": r["reason"]}
    assert r["reason"].startswith("分析超过 2 秒，已中止：") and "PYSTEPMAP_ANALYZE_TIMEOUT" in r["reason"]
    assert 2 <= elapsed < 10, elapsed
    assert workers_of_this_process() == []  # 子进程被杀掉并回收了


@pytest.mark.parametrize(("config", "pattern"), [
    ({"nope": 1}, "未知字段"),
    ({"exclude": "tests"}, "exclude"),
    ({"source_roots": ["no/such"]}, "no/such"),  # 运行中才发现的
    ([1, 2], "顶层必须是对象"),
])
def test_bad_config(tmp_path, config, pattern):
    r = run_in_subprocess(str(TRANSFER), config, timeout=60, cwd=tmp_path)
    assert r["ok"] is False and r["code"] == "config", r
    assert r["reason"].startswith("分析配置不合法：") and pattern in r["reason"]


@pytest.mark.parametrize("where", ["data_dir", "project"])
def test_the_projects_own_code_is_never_imported(tmp_path, where):
    """项目里有 json.py（还有和 pystepmap 的包同名的目录），导入它们就会退出：分析照常成功。子进程的当前目录是数据目录；
    就算是项目目录，-P 也不把它放进模块搜索路径。"""
    proj = tmp_path / "proj"
    for rel in ("json.py", "pystepmap/__init__.py", "pystepmap_server/__init__.py", "sitecustomize.py"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text('raise SystemExit("被导入了")\n', encoding="utf-8")
    (proj / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    r = run_in_subprocess(str(proj), {}, timeout=60, cwd=home if where == "data_dir" else proj)
    assert r["ok"] is True, r
    assert json.loads(r["graph_json"])["file_hashes"].keys() >= {"json.py", "app.py"}


def test_parents_pythonpath_is_ignored(tmp_path, monkeypatch):
    """父进程的环境里有 PYTHONPATH（指向一个有 json.py 的目录）：子进程用 -E 不看它，分析照常成功。"""
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "json.py").write_text('raise SystemExit("被导入了")\n', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(evil))
    r = run_in_subprocess(str(TRANSFER), {}, timeout=60, cwd=home)
    assert r["ok"] is True, r
    assert "-E" in worker.WORKER_ARGS and "-P" in worker.WORKER_ARGS


def test_request_carries_the_timeout(tmp_path, monkeypatch):
    echo = "import sys, json; req = json.load(sys.stdin); print(json.dumps({'ok': False, 'code': 'x', 'reason': json.dumps(req)}))"
    monkeypatch.setattr(worker, "WORKER_ARGS", ("-c", echo))
    r = run_in_subprocess("/srv/p", {"exclude": []}, timeout=7.5, cwd=tmp_path)
    assert json.loads(r["reason"]) == {"root": "/srv/p", "config": {"exclude": []}, "timeout": 7.5}


@pytest.mark.skipif(sys.platform == "win32", reason="没有 RLIMIT_CPU")
def test_worker_stops_itself_when_nobody_is_watching(tmp_path):
    """父进程被杀掉（MCP 的宿主随手就杀）以后没人按超时杀子进程：子进程按请求里的 timeout 给自己定了 CPU 时间上限
    （timeout 向上取整再加 CPU_GRACE 秒），到了系统发 SIGXCPU 把它结束。这里直接启动子进程，不经过父进程的超时。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "slow.py").write_text(SLOW_FILE, encoding="utf-8")
    request = json.dumps({"root": str(proj), "config": SLOW_RULES, "timeout": 1}).encode()
    started = time.monotonic()
    done = subprocess.run([sys.executable, *worker.WORKER_ARGS], input=request, capture_output=True, cwd=tmp_path,
                          timeout=60, check=False)
    elapsed = time.monotonic() - started
    assert done.returncode == -signal.SIGXCPU, (done.returncode, done.stderr[-500:])
    assert 1 + worker.CPU_GRACE - 1 <= elapsed < 30, elapsed
    assert done.stdout == b"" and list(tmp_path.glob("core*")) == []


LIMITS = """
import json, resource, sys
from pystepmap_server import worker
worker.analyze_directory = lambda root, config: {"ok": False, "code": "limits", "reason": json.dumps(
    [resource.getrlimit(resource.RLIMIT_CORE), resource.getrlimit(resource.RLIMIT_CPU)[0]])}
sys.exit(worker.main(limit_cpu=True))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="没有 resource 模块")
@pytest.mark.parametrize(("timeout", "cpu"), [(3, 3 + 5), (2.5, 3 + 5), (None, None), ("x", None)])
def test_worker_turns_off_core_dumps(tmp_path, timeout, cpu):
    """作为子进程运行时不留核心转储（到了 CPU 时间上限被系统结束时，不在数据目录里留下 core 文件）；CPU 上限按
    timeout 向上取整再加 CPU_GRACE，没有合法的 timeout 时不设。"""
    import resource

    request = json.dumps({"root": "/x", "config": {}, "timeout": timeout}).encode()
    done = subprocess.run([sys.executable, "-E", "-P", "-c", LIMITS], input=request, capture_output=True, cwd=tmp_path,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    core, soft_cpu = json.loads(json.loads(done.stdout)["reason"])
    assert core == [0, 0]
    unlimited = resource.getrlimit(resource.RLIMIT_CPU)[0]  # 父进程（测试进程）的，没有被改
    assert soft_cpu == (unlimited if cpu is None else cpu)


def test_worker_that_dies_is_reported_with_its_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKER_ARGS",
                        ("-c", "import sys; sys.stderr.write('前面的输出\\nRuntimeError: 坏了\\n'); sys.exit(3)"))
    r = run_in_subprocess(str(TRANSFER), {}, timeout=60, cwd=tmp_path)
    assert r["ok"] is False and r["code"] == "analysis"
    assert "退出码 3" in r["reason"] and "RuntimeError: 坏了" in r["reason"]


def test_worker_killed_by_a_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKER_ARGS", ("-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    r = run_in_subprocess(str(TRANSFER), {}, timeout=60, cwd=tmp_path)
    assert r["ok"] is False and r["code"] == "analysis" and "信号 9" in r["reason"]


@pytest.mark.parametrize("output", ["", "not json", "[1]", '{"ok": true}', '{"ok": false}'])
def test_worker_output_that_is_not_a_result(tmp_path, monkeypatch, output):
    monkeypatch.setattr(worker, "WORKER_ARGS", ("-c", f"import sys; sys.stdout.write({output!r})"))
    r = run_in_subprocess(str(TRANSFER), {}, timeout=60, cwd=tmp_path)
    assert r["ok"] is False and r["code"] == "analysis" and "输出" in r["reason"], r


def test_worker_main_keeps_engine_prints_out_of_the_result(monkeypatch, capsys):
    def noisy(root, config):
        print("引擎里的 print")
        return analyze_with_notes(root, config)

    monkeypatch.setattr(worker, "analyze_with_notes", noisy)
    out = io.BytesIO()
    assert worker.main(io.BytesIO(json.dumps({"root": str(TRANSFER), "config": {}}).encode()), out) == 0
    result = json.loads(out.getvalue().decode("utf-8"))
    assert result["ok"] is True and "引擎里的 print" in capsys.readouterr().err


def test_worker_main_with_a_bad_request():
    out = io.BytesIO()
    assert worker.main(io.BytesIO(b"{not json"), out) == 0
    result = json.loads(out.getvalue())
    assert result["ok"] is False and result["code"] == "analysis" and "请求" in result["reason"]


def test_engine_errors_in_process():
    def broken(root, config):
        raise RuntimeError("引擎坏了")

    r = worker.analyze_directory(str(TRANSFER), {}, broken)
    assert r == {"ok": False, "code": "analysis", "reason": "分析失败：RuntimeError: 引擎坏了"}


@pytest.mark.parametrize(("value", "seconds"), [(None, 120.0), ("30", 30.0), ("0.5", 0.5), ("abc", 120.0),
                                                ("0", 120.0), ("-3", 120.0), ("nan", 120.0), ("inf", 120.0)])
def test_timeout_comes_from_the_environment(monkeypatch, value, seconds):
    if value is None:
        monkeypatch.delenv("PYSTEPMAP_ANALYZE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("PYSTEPMAP_ANALYZE_TIMEOUT", value)
    assert analyze_timeout() == seconds


# ---------- 约束检查（pystepmap mcp 的 check 工具）：同一个子进程，mode 为 "check" ----------

BAD_FLOW = ('import time\nfrom pystepkit import outcome, workflow\n\n\n@workflow(title="坏流程")\ndef bad():\n'
            '    started = time.time()\n    return outcome("ok", title="完成", started=started)\n')


def test_check_in_subprocess_lists_violations(tmp_path):
    clean = {"ok": True, "violations": [], "skipped": [], "unmatched": [], "ignored": []}
    assert check_in_subprocess(str(TRANSFER), {}, None, timeout=60, cwd=tmp_path) == clean
    proj = tmp_path / "bad"
    (proj / "app").mkdir(parents=True)
    (proj / "app" / "flow.py").write_text(BAD_FLOW, encoding="utf-8")
    r = check_in_subprocess(str(proj), {}, None, timeout=60, cwd=tmp_path)
    assert r["ok"] is True and [(v["file"], v["line"], v["code"]) for v in r["violations"]] == [("app/flow.py", 7, "SM001")]
    assert r["violations"][0]["message"].startswith("编排函数只能调用步骤")
    assert worker.check_directory(str(proj), {}, None) == r  # 进程内的同一个函数
    only = check_in_subprocess(str(proj), {}, ["app/other.py"], timeout=60, cwd=tmp_path)
    assert only["violations"] == [] and only["unmatched"] == ["app/other.py"]


def test_check_with_a_bad_config(tmp_path):
    r = check_in_subprocess(str(TRANSFER), {"nope": 1}, None, timeout=60, cwd=tmp_path)
    assert r["ok"] is False and r["code"] == "config" and r["reason"].startswith("分析配置不合法：")


def test_check_with_a_catastrophic_regex_is_stopped_by_the_timeout(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "slow.py").write_text(SLOW_FILE, encoding="utf-8")
    started = time.monotonic()
    r = check_in_subprocess(str(proj), SLOW_RULES, None, timeout=2, cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert r["ok"] is False and r["code"] == "timeout", r
    assert r["reason"].startswith("检查超过 2 秒，已中止：") and "PYSTEPMAP_ANALYZE_TIMEOUT" in r["reason"]
    assert 2 <= elapsed < 10, elapsed
    assert workers_of_this_process() == []


def test_check_never_imports_the_project(tmp_path):
    proj = tmp_path / "proj"
    for rel in ("json.py", "pystepmap/__init__.py", "pystepmap_server/__init__.py", "sitecustomize.py"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text('raise SystemExit("被导入了")\n', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    r = check_in_subprocess(str(proj), {}, None, timeout=60, cwd=home)
    assert r["ok"] is True and r["violations"] == [], r


def test_kill_running_stops_the_workers(tmp_path):
    """MCP 服务退出（标准输入关了、收到 SIGTERM）时杀掉还在跑的分析 / 检查子进程，等着它的线程马上拿到结果。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "slow.py").write_text(SLOW_FILE, encoding="utf-8")
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(
        check_in_subprocess(str(proj), SLOW_RULES, None, timeout=60, cwd=tmp_path)))
    thread.start()
    deadline = time.monotonic() + 10
    while not workers_of_this_process() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert workers_of_this_process()
    assert worker.kill_running() == 1
    thread.join(5)
    assert not thread.is_alive()
    assert result["ok"] is False and result["code"] == "analysis" and "检查进程异常退出" in result["reason"], result
    assert workers_of_this_process() == [] and worker.kill_running() == 0


def test_summarize_separates_blind_spots_from_third_party_calls(tmp_path):
    """摘要里除了目标不明的调用总数，还要有"可能落在项目代码上"的那部分（其余是第三方、标准库和日志）。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("def f(session, c):\n    session.commit()\n    c.render()\n"
                               "class C:\n    def render(self): ...\nclass D:\n    def render(self): ...\n",
                               encoding="utf-8")
    graph, notes = analyze_with_notes(proj, parse_config({}))
    s = summarize(graph, notes, 0.0)
    assert s["unresolved_calls"] == 2  # session.commit 和 c.render
    assert s["unresolved_blind"] == 1  # 只有 render 在项目里有同名定义，commit 不可能是项目内的调用
