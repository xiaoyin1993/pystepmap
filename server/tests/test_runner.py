"""运行项目的测试（runner.run_tests）：命令的退出码和输出末尾、超时和取消时整个进程组（含孙进程）都被结束、命令正常
结束后进程组里剩下的后台进程也被结束、传给命令的环境变量和工作目录、平台服务被强杀后看门狗结束进程组并删掉这次运行的
临时目录；插件目录（write_plugin_dir）和 core 里的记录插件一样、内容没变时不重写；进程内的运行表（launch、进度、取消、
输出末尾）。"""

from __future__ import annotations

import importlib.util
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pystepmap_server import runner
from pystepmap_server.runner import RunResult, last_output, run_tests, write_plugin_dir

TRACE_SOURCE = Path(importlib.util.find_spec("pystepmap.trace").origin)


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "pystepmap-home"


@pytest.fixture
def root(tmp_path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


def run(root: Path, home: Path, command: str, *, timeout: float = 30, cancel: threading.Event | None = None,
        **kwargs) -> RunResult:
    scratch = home / "runs" / "tmp-test"
    scratch.mkdir(parents=True, exist_ok=True)
    return run_tests(str(root), command, graph_path=scratch / "graph.json", trace_path=scratch / "trace.jsonl",
                     plugin_dir=write_plugin_dir(home), timeout=timeout, cancel=cancel or threading.Event(), **kwargs)


def group_gone(pgid: int, within: float = 2.0) -> bool:
    """进程组 pgid 里一个进程都不剩了（被结束的孙进程由系统回收，可能要一小会儿）。"""
    deadline = time.monotonic() + within
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # 编号已经被别人的进程组用了：我们的早就没了
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)


def read_pid(path: Path, within: float = 5.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        text = path.read_text() if path.exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"{path} 里没有进程号")


# ---------- 退出码和输出 ----------

def test_echo_finishes_with_exit_code_0_and_its_output(root, home):
    r = run(root, home, "echo hi")
    assert (r.status, r.exit_code, r.reason) == ("finished", 0, "")
    assert r.tail == ["hi"] and 0 <= r.seconds < 5


def test_exit_code_is_kept(root, home):
    r = run(root, home, "echo before; exit 3")
    assert (r.status, r.exit_code, r.tail) == ("finished", 3, ["before"])


def test_unknown_command_is_reported_by_the_shell(root, home):
    r = run(root, home, "no-such-command-pystepmap-xyz --flag")
    assert (r.status, r.exit_code) == ("finished", 127)
    assert any("no-such-command-pystepmap-xyz" in line and "not found" in line for line in r.tail), r.tail


def test_stderr_is_in_the_tail_too_and_only_the_last_200_lines_are_kept(root, home):
    r = run(root, home, "echo to-stderr 1>&2; i=1; while [ $i -le 500 ]; do echo line$i; i=$((i+1)); done")
    assert len(r.tail) == runner.TAIL_LINES == 200
    assert r.tail[0] == "line301" and r.tail[-1] == "line500"
    r = run(root, home, "echo to-stderr 1>&2")
    assert r.tail == ["to-stderr"]


def test_very_long_lines_are_cut(root, home):
    r = run(root, home, "head -c 200000 /dev/zero | tr '\\0' x; echo; echo after")
    assert len(r.tail) == 2 and r.tail[0] == "x" * runner.LINE_CHARS and r.tail[1] == "after"


def test_output_is_visible_while_the_command_runs(root, home, tmp_path):
    """给了 output（一个 deque）时，命令还在跑就能看到已经输出的行（界面运行中看输出用）。"""
    from collections import deque

    live: deque[str] = deque(maxlen=runner.TAIL_LINES)
    flag = tmp_path / "go"
    seen: list[list[str]] = []

    def watch():
        deadline = time.monotonic() + 5
        while not live and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.append(list(live))
        flag.write_text("1")

    t = threading.Thread(target=watch)
    t.start()
    r = run(root, home, f"echo first; while [ ! -f {flag} ]; do sleep 0.05; done; echo second", output=live)
    t.join()
    assert seen == [["first"]] and r.tail == ["first", "second"] and list(live) == r.tail


# ---------- 超时、取消、留下的进程 ----------

@pytest.fixture
def quick_interrupt(monkeypatch):
    """sh 的后台进程（&）不理 SIGINT：不等满 5 秒就接着 SIGTERM，测试快一点。"""
    monkeypatch.setattr(runner, "INT_GRACE", 0.3)


def test_timeout_kills_the_whole_group_including_grandchildren(root, home, tmp_path, quick_interrupt):
    pidfile = tmp_path / "pgid"
    started = time.monotonic()
    r = run(root, home, f"echo $$ > {pidfile}; sh -c 'sleep 30 & sleep 30'", timeout=1)
    took = time.monotonic() - started
    assert (r.status, r.exit_code) == ("timeout", None)
    assert took < 3.0 and 1.0 <= r.seconds < 3.0
    assert group_gone(read_pid(pidfile))


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(root, home, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "KILL_GRACE", 0.5)
    monkeypatch.setattr(runner, "INT_GRACE", 0.3)
    pidfile = tmp_path / "pgid"
    r = run(root, home, f"echo $$ > {pidfile}; trap '' INT TERM; sleep 30 & wait; sleep 30", timeout=0.5)
    assert r.status == "timeout"
    assert group_gone(read_pid(pidfile))


def test_cancel_stops_the_command(root, home, tmp_path, quick_interrupt):
    pidfile = tmp_path / "pgid"
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    r = run(root, home, f"echo $$ > {pidfile}; echo started; sleep 30 & sleep 30", cancel=cancel)
    timer.join()
    assert (r.status, r.exit_code) == ("cancelled", None) and r.tail == ["started"]
    assert time.monotonic() - started < 2.0
    assert group_gone(read_pid(pidfile))


def test_cancelled_before_starting_runs_nothing(root, home, tmp_path):
    cancel = threading.Event()
    cancel.set()
    marker = tmp_path / "ran"
    r = run(root, home, f"touch {marker}", cancel=cancel)
    assert (r.status, r.exit_code, r.tail) == ("cancelled", None, []) and not marker.exists()


def test_background_processes_left_after_a_normal_finish_are_ended(root, home, tmp_path):
    """命令自己结束了、后台还留着进程（还拿着输出管道）：不等它，结束整个进程组。"""
    pidfile = tmp_path / "pgid"
    started = time.monotonic()
    r = run(root, home, f"echo $$ > {pidfile}; sleep 30 & echo done")
    assert (r.status, r.exit_code, r.tail) == ("finished", 0, ["done"])
    assert time.monotonic() - started < 2.0
    assert group_gone(read_pid(pidfile))


def test_a_directory_that_is_gone_means_failed_to_start(home, tmp_path):
    r = run(tmp_path / "nope", home, "echo hi")
    assert (r.status, r.exit_code, r.tail) == ("failed_to_start", None, [])
    assert r.reason.startswith("命令没能启动")


def test_a_nul_in_the_command_means_failed_to_start(root, home):
    r = run(root, home, "echo a\0b")
    assert r.status == "failed_to_start" and r.reason.startswith("命令没能启动")


WATCHED = """
import sys, threading
from pathlib import Path
from pystepmap_server.runner import run_tests, write_plugin_dir
root, home, pidfile = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
scratch = home / "runs" / "tmp-watched"
scratch.mkdir(parents=True)
(scratch / "trace.jsonl").write_text("")
print("go", flush=True)
run_tests(root, f"echo $$ > {pidfile}; sh -c 'sleep 60 & sleep 60'", graph_path=scratch / "graph.json",
          trace_path=scratch / "trace.jsonl", plugin_dir=write_plugin_dir(home), timeout=600,
          cancel=threading.Event(), scratch=scratch)
"""


def test_watchdog_ends_the_group_and_removes_the_scratch_when_the_server_is_killed(root, home, tmp_path):
    """平台服务被强杀（SIGKILL，来不及收尾）：看门狗进程发现父进程没了，结束测试的进程组、删掉这次运行的临时目录。"""
    pidfile = tmp_path / "pgid"
    server = subprocess.Popen([sys.executable, "-c", WATCHED, str(root), str(home), str(pidfile)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout.readline().strip() == "go"
        pgid = read_pid(pidfile)
        assert not group_gone(pgid, within=0.3)  # 在跑
    finally:
        server.send_signal(signal.SIGKILL)
        server.wait(10)
        server.stdout.close()
    assert group_gone(pgid, within=6.0)
    deadline = time.monotonic() + 3
    while (home / "runs" / "tmp-watched").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not (home / "runs" / "tmp-watched").exists()


def test_watchdog_does_nothing_after_a_normal_finish(root, home, tmp_path):
    scratch = home / "runs" / "tmp-kept"
    scratch.mkdir(parents=True)
    r = run_tests(str(root), "echo hi", graph_path=scratch / "g.json", trace_path=scratch / "t.jsonl",
                  plugin_dir=write_plugin_dir(home), timeout=30, cancel=threading.Event(), scratch=scratch)
    assert r.status == "finished"
    time.sleep(0.3)
    assert scratch.is_dir()  # 临时目录由流程删，看门狗只在服务被强杀时删


# ---------- 环境变量、工作目录 ----------

def env_of(tail: list[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in tail if "=" in line)


def test_environment_and_working_directory(root, home, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x --tb=short")
    monkeypatch.setenv("PYTHONPATH", "/opt/a:/opt/b")
    monkeypatch.setenv("PYSTEPMAP_SOMETHING_INHERITED", "yes")
    scratch = home / "runs" / "tmp-test"  # run() 用的
    r = run(root, home, "env; echo CWD=$(pwd -P)")
    env = env_of(r.tail)
    plugin = home / "plugin"
    assert env["PYSTEPMAP_GRAPH"] == str(scratch / "graph.json")
    assert env["PYSTEPMAP_TRACE_OUT"] == str(scratch / "trace.jsonl")
    assert env["PYTHONPATH"] == f"{plugin}{os.pathsep}/opt/a:/opt/b"
    assert env["PYTEST_ADDOPTS"] == "-x --tb=short -p pystepmap_trace_plugin -p no:cacheprovider"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYSTEPMAP_SOMETHING_INHERITED"] == "yes"
    assert env["CWD"] == os.path.realpath(root)


def test_environment_without_earlier_values(root, home, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = env_of(run(root, home, "env").tail)
    assert env["PYTHONPATH"] == str(home / "plugin")
    assert env["PYTEST_ADDOPTS"] == "-p pystepmap_trace_plugin -p no:cacheprovider"


def test_the_command_gets_no_stdin(root, home):
    r = run(root, home, "cat; echo end")
    assert (r.status, r.exit_code, r.tail) == ("finished", 0, ["end"])


# ---------- 插件目录 ----------

def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_plugin_dir_holds_a_renamed_copy_of_the_trace_plugin(home):
    """插件是一个顶层模块 pystepmap_trace_plugin（core 的 trace.py 原样复制、改个名字），不是叫 pystepmap 的包：被测项目
    自己有 pystepmap 包时（比如 pystepmap 仓库自己）不会被挡住。"""
    plugin = write_plugin_dir(home)
    assert plugin == home / "plugin" and runner.PLUGIN_MODULE == "pystepmap_trace_plugin"
    module = plugin / "pystepmap_trace_plugin.py"
    assert module.read_bytes() == TRACE_SOURCE.read_bytes()
    assert b'rec["icalls"]' in module.read_bytes()  # 现在的插件（S14：记间接调用）
    assert sorted(p.name for p in plugin.iterdir()) == ["pystepmap_trace_plugin.py"]
    assert mode(plugin) == 0o700 and mode(module) == 0o600
    before = module.stat()
    time.sleep(0.01)
    assert write_plugin_dir(home) == plugin
    after = module.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)  # 没变：不重写


def test_plugin_dir_is_rewritten_when_it_differs_and_the_old_package_is_removed(home):
    plugin = write_plugin_dir(home)
    (plugin / "pystepmap_trace_plugin.py").write_text("# 旧版本\n")
    (plugin / "pystepmap").mkdir()  # 以前的代码放的包
    (plugin / "pystepmap" / "__init__.py").write_text("")
    (plugin / "pystepmap" / "trace.py").write_text("# 旧的\n")
    write_plugin_dir(home)
    assert (plugin / "pystepmap_trace_plugin.py").read_bytes() == TRACE_SOURCE.read_bytes()
    assert sorted(p.name for p in plugin.iterdir()) == ["pystepmap_trace_plugin.py"]  # 旧包和临时文件都没留下


def test_the_plugin_is_importable_and_does_not_take_the_name_pystepmap(home, root):
    """命令里的 python 按 PYTHONPATH 找到 pystepmap_trace_plugin；pystepmap 这个名字不指向插件目录。"""
    code = ("import importlib.util, pystepmap_trace_plugin as t; print(t.__file__); "
            "s = importlib.util.find_spec('pystepmap'); print(s.origin if s else None)")
    r = run(root, home, f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
    assert r.tail[0] == str(home / "plugin" / "pystepmap_trace_plugin.py"), r.tail
    assert not r.tail[1].startswith(str(home)), r.tail


# ---------- 超时设置、临时目录 ----------

@pytest.mark.parametrize(("value", "expected"), [(None, 1800.0), ("60", 60.0), ("0.5", 0.5), ("0", 1800.0),
                                                 ("-3", 1800.0), ("abc", 1800.0), ("nan", 1800.0), ("inf", 1800.0)])
def test_run_timeout_reads_the_environment(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(runner.TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(runner.TIMEOUT_ENV, value)
    assert runner.TIMEOUT_ENV == "PYSTEPMAP_TEST_TIMEOUT"
    assert runner.run_timeout() == expected


def test_scratch_directories_are_private_and_removed(home):
    one = runner.make_scratch(home)
    two = runner.make_scratch(home)
    assert one != two and one.parent == two.parent == home / "runs"
    assert one.name.startswith("tmp-") and mode(one) == 0o700 and mode(home / "runs") == 0o700
    (one / "trace.jsonl").write_text("x")
    runner.remove_scratch(one)
    runner.remove_scratch(one)  # 可以重复调用
    assert not one.exists() and two.exists()
    runner.clean_leftovers(home)  # 服务启动时：上次被强杀留下的都删掉
    assert list((home / "runs").iterdir()) == []
    runner.clean_leftovers(home / "nope")  # 还没有 runs 目录：什么都不做


# ---------- 进程内的运行表 ----------

def test_launch_waits_until_the_run_is_announced(home):
    """launch 在后台线程里跑 target：target 宣布开始（announce）后 launch 就返回 None；target 在宣布之前就结束时
    launch 返回它的结果。同一个项目同时只能有一个。"""
    gate = threading.Event()
    phases: list = []

    def target(cancel):
        runner.announce(home, "p1")
        gate.wait(5)
        runner.set_phase(home, "p1", "testing")
        phases.append(runner.running(home, "p1")["phase"])
        cancel.wait(5)
        return {"kind": "failed", "code": "x", "reason": "取消了", "title": "t", "run_id": 1}

    assert runner.launch(home, "p1", target) is None
    now = runner.running(home, "p1")
    assert now["phase"] == "preparing" and now["seconds"] >= 0 and now["started_at"]
    busy = runner.launch(home, "p1", target)
    assert busy["kind"] == "failed" and busy["code"] == "busy"
    gate.set()
    deadline = time.monotonic() + 5
    while not phases and time.monotonic() < deadline:
        time.sleep(0.01)
    assert phases == ["testing"]
    assert runner.cancel_reason(home, "p1") is None
    assert runner.cancel_run(home, "p1") == "testing" and runner.cancel_reason(home, "p1") == "user"
    deadline = time.monotonic() + 5
    while runner.running(home, "p1") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.running(home, "p1") is None and runner.cancel_run(home, "p1") is None
    assert runner.last_failure(home, "p1") is None  # 存了一次运行（run_id）：不算失败


def test_launch_returns_an_early_ending_and_remembers_late_failures(home):
    early = {"kind": "failed", "code": "no_command", "reason": "没填", "title": "t"}
    assert runner.launch(home, "p2", lambda cancel: early) == early
    assert runner.running(home, "p2") is None
    assert runner.last_failure(home, "p2") is None  # 宣布之前的失败由接口直接返回，不记

    def late(cancel):
        runner.announce(home, "p2")
        return {"kind": "failed", "code": "analysis", "reason": "运行前的分析失败：xx", "title": "分析失败"}

    assert runner.launch(home, "p2", late) is None
    deadline = time.monotonic() + 5
    while runner.running(home, "p2") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    failure = runner.last_failure(home, "p2")
    assert failure["code"] == "analysis" and failure["reason"] == "运行前的分析失败：xx" and failure["at"]


def test_a_crashing_target_is_reported_not_raised(home, caplog):
    def boom(cancel):
        runner.announce(home, "p3")
        raise RuntimeError("内部错误")

    assert runner.launch(home, "p3", boom) is None
    deadline = time.monotonic() + 5
    while runner.running(home, "p3") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    failure = runner.last_failure(home, "p3")
    assert failure["code"] == "internal" and "RuntimeError" in failure["reason"]


def test_output_is_kept_per_project_in_memory(home, tmp_path):
    assert last_output(home, "p4") == []
    runner.keep_output(home, "p4", ["a", "b"])
    assert last_output(home, "p4") == ["a", "b"] and last_output(tmp_path / "other", "p4") == []
    runner.forget(home, "p4")
    assert last_output(home, "p4") == []


def test_stop_all_cancels_the_runs_of_one_home_and_waits(home, tmp_path):
    other = tmp_path / "other-home"
    stopped: list[str] = []

    def target_for(where, pid):
        def target(cancel):
            runner.announce(where, pid)
            cancel.wait(10)
            stopped.append(pid)
            return {"kind": "failed", "code": "cancelled", "reason": "", "title": "t", "run_id": 1}
        return target

    assert runner.launch(home, "a", target_for(home, "a")) is None
    assert runner.launch(other, "b", target_for(other, "b")) is None
    runner.stop_all(home, wait=5)
    assert stopped == ["a"] and runner.running(home, "a") is None and runner.running(other, "b") is not None
    runner.stop_all(other, wait=5)
    assert stopped == ["a", "b"]


# ---------- 不把 pystepmap 自己的虚拟环境传给测试命令 ----------

OWN_VENV = sys.prefix != sys.base_prefix
needs_venv = pytest.mark.skipif(not OWN_VENV, reason="平台服务不在虚拟环境里跑：没有要去掉的")


@needs_venv
def test_pystepmaps_own_virtualenv_is_not_passed_on(root, home, monkeypatch, tmp_path):
    """uv run pystepmap serve（或激活了 pystepmap 的 .venv）时，环境里有 pystepmap 自己的虚拟环境：PATH 最前面是它的 bin、
    VIRTUAL_ENV 指着它、uv run 的运行期变量。测试命令要在用户自己的环境里跑：这些去掉（PATH 里只去掉那一项），别的照留。"""
    own_bin = os.path.join(sys.prefix, "bin")
    other = str(tmp_path / "other" / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([own_bin, "/usr/bin", "/bin", own_bin + "/", other]))
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "pystepmap")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", sys.prefix)
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")
    monkeypatch.setenv("UV", "/somewhere/uv")
    monkeypatch.setenv("UV_INTERNAL__PARENT_INTERPRETER", sys.executable)
    monkeypatch.setenv("UV_CACHE_DIR", "/keep/this/cache")
    monkeypatch.setenv("PYSTEPMAP_UNRELATED", "kept")
    r = run(root, home, "echo PY=$(command -v python || true); echo PY3=$(command -v python3 || true); env")
    env = env_of(r.tail)
    for name in ("PY", "PY3"):
        assert not env[name].startswith(sys.prefix), env[name]  # 找到的不是 pystepmap 的解释器
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin", other])
    for gone in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "UV_PROJECT_ENVIRONMENT", "UV_RUN_RECURSION_DEPTH", "UV",
                 "UV_INTERNAL__PARENT_INTERPRETER"):
        assert gone not in env, gone
    assert env["UV_CACHE_DIR"] == "/keep/this/cache" and env["PYSTEPMAP_UNRELATED"] == "kept"
    assert env["PYTEST_ADDOPTS"].endswith(runner.PYTEST_PLUGIN_OPTIONS)  # 加上的照加


def test_someone_elses_virtualenv_is_kept(root, home, monkeypatch, tmp_path):
    """用户自己激活的别的虚拟环境（不是 pystepmap 的）、不在 uv run 里时的 UV：原样传下去。"""
    theirs = tmp_path / "their-venv"
    monkeypatch.setenv("PATH", os.pathsep.join([str(theirs / "bin"), "/usr/bin", "/bin"]))
    monkeypatch.setenv("VIRTUAL_ENV", str(theirs))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(theirs))
    monkeypatch.delenv("UV_RUN_RECURSION_DEPTH", raising=False)
    monkeypatch.setenv("UV", "/their/uv")
    env = env_of(run(root, home, "env").tail)
    assert env["VIRTUAL_ENV"] == str(theirs) and env["UV_PROJECT_ENVIRONMENT"] == str(theirs)
    assert env["PATH"] == os.pathsep.join([str(theirs / "bin"), "/usr/bin", "/bin"]) and env["UV"] == "/their/uv"



# ---------- 先 SIGINT（让 pytest 收尾），再 SIGTERM、SIGKILL ----------

TEARDOWN_TEST = """
import os
import time

import pytest


@pytest.fixture
def resource():
    yield
    with open(os.environ["TEARDOWN_FILE"], "w") as f:
        f.write("teardown ran")


def test_slow(resource):
    with open(os.environ["STARTED_FILE"], "w") as f:
        f.write("started")
    time.sleep(30)
"""


# 超时的那两个用例给 pytest 启动到 fixture 的时间：机器忙（同时起很多进程）时 3 秒不够
STOP_TIMEOUT = 6.0


def _stop_time(how: str, began: float, stopped_at: list[float]) -> float:
    """平台发 SIGINT 的时刻：超时是开始后 STOP_TIMEOUT 秒，取消是测试里置位 cancel 的那一刻。"""
    return began + STOP_TIMEOUT if how == "timeout" else stopped_at[0]


@pytest.mark.parametrize("how", ["timeout", "cancel"])
def test_stopping_interrupts_first_so_pytest_runs_fixture_teardown(root, home, tmp_path, how):
    """超时、取消（服务退出时也是取消）：先给整个进程组 SIGINT，pytest 当作 Ctrl+C，照常跑 fixture 的收尾；收尾完了
    进程组就没了，不用等满 5 秒。"""
    (root / "tests").mkdir()
    (root / "tests" / "test_slow.py").write_text(TEARDOWN_TEST, encoding="utf-8")
    teardown, started = tmp_path / "teardown.txt", tmp_path / "started.txt"
    command = (f"TEARDOWN_FILE={teardown} STARTED_FILE={started} {shlex.quote(sys.executable)} -m pytest -q "
               f"-p no:cacheprovider tests")
    cancel = threading.Event()
    stopped_at: list[float] = []  # 取消的时刻（超时的时刻是 began + STOP_TIMEOUT）
    if how == "cancel":
        def cancel_when_started():
            deadline = time.monotonic() + 20
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            stopped_at.append(time.monotonic())
            cancel.set()
        threading.Thread(target=cancel_when_started, daemon=True).start()
    began = time.monotonic()
    r = run(root, home, command, timeout=STOP_TIMEOUT if how == "timeout" else 60, cancel=cancel)
    assert r.status == ("timeout" if how == "timeout" else "cancelled")
    assert teardown.read_text() == "teardown ran"
    assert time.monotonic() - _stop_time(how, began, stopped_at) < 4.5  # 从发 SIGINT 起算：没有等满之后的 5 秒（INT_GRACE）


IGNORES_INT = """
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
def on_term(*_):
    open(sys.argv[1], "w").write("term")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
open(sys.argv[2], "w").write(str(os.getpgrp()))
while True:
    time.sleep(0.05)
"""


def test_a_process_ignoring_sigint_gets_sigterm_after_the_interrupt_grace(root, home, tmp_path, monkeypatch):
    """不理 SIGINT 的程序（一条命令：sh 直接换成了它，不是 sh）：等满 INT_GRACE 秒才收到 SIGTERM。"""
    monkeypatch.setattr(runner, "INT_GRACE", 0.5)
    pidfile, log = tmp_path / "pgid", tmp_path / "log"
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(IGNORES_INT)} {log} {pidfile}"
    started = time.monotonic()
    r = run(root, home, command, timeout=0.5)
    assert r.status == "timeout" and log.read_text().strip() == "term"
    assert 1.0 <= time.monotonic() - started < 3
    assert group_gone(read_pid(pidfile))


ESCAPER = ("import subprocess, sys; p = subprocess.Popen(['sleep', '315'], start_new_session=True); "
           "print('escaped', p.pid, flush=True)")


def test_a_process_that_escapes_the_group_cannot_hold_the_output_open(root, home):
    """测试自己起的、另开会话的进程（start_new_session / setsid）不在进程组里，结束进程组碰不到它；它还拿着输出管道
    的写端。读输出的线程不等它：最多等 READER_GRACE 秒，然后关掉我们这一端，run_tests 照常返回。"""
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(ESCAPER)}; echo done"
    before = {t for t in threading.enumerate()}
    started = time.monotonic()
    r = run(root, home, command)
    pid = next(int(line.split()[1]) for line in r.tail if line.startswith("escaped"))
    try:
        assert (r.status, r.exit_code) == ("finished", 0) and r.tail[-1] == "done"
        assert time.monotonic() - started < runner.READER_GRACE + 3
        time.sleep(0.3)
        assert not [t for t in threading.enumerate() if t not in before and t.name == "pystepmap-test-output"]
    finally:
        os.kill(pid, signal.SIGKILL)  # 那个 sleep 不在任何进程组里：测试自己收拾


def test_a_reader_that_cannot_start_does_not_leave_the_command_running(root, home, monkeypatch):
    started: list = []
    real_popen = runner.subprocess.Popen

    def popen(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        started.append(p)
        return p

    class NoThread(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.threading, "Thread", NoThread)
    r = run(root, home, "sleep 30")
    assert r.status == "failed_to_start" and "RuntimeError" in r.reason
    command = started[0]
    assert command.poll() is not None and group_gone(command.pid)



def test_cancel_is_not_passed_on_while_processing_and_shutdown_is_told_apart(home):
    """在 processing 阶段取消没有用（测试命令已经跑完）：cancel_run 只回阶段，不置位；服务退出（stop_all）记成 shutdown。"""
    seen: list = []
    gate = threading.Event()

    def target(cancel):
        runner.announce(home, "p9")
        runner.set_phase(home, "p9", "processing")
        gate.wait(5)
        seen.append(cancel.is_set())
        runner.set_phase(home, "p9", "testing")
        cancel.wait(5)
        seen.append(runner.cancel_reason(home, "p9"))
        return {"kind": "failed", "code": "cancelled", "reason": "", "title": "t", "run_id": 1}

    assert runner.launch(home, "p9", target) is None
    assert runner.cancel_run(home, "p9") == "processing"
    gate.set()
    deadline = time.monotonic() + 5
    while runner.running(home, "p9")["phase"] != "testing" and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.stop_all(home, wait=5)
    assert seen == [False, "shutdown"]



def test_a_run_of_a_deleted_project_leaves_nothing_behind(home):
    """项目删了（forget）以后运行才结束：它的失败和输出不再记进表里。"""
    gate = threading.Event()

    def target(cancel):
        runner.announce(home, "gone")
        gate.wait(5)
        runner.keep_output(home, "gone", ["late output"])
        return {"kind": "failed", "code": "not_found", "reason": "项目在运行测试期间被删除了", "title": "t"}

    assert runner.launch(home, "gone", target) is None
    runner.forget(home, "gone")
    gate.set()
    deadline = time.monotonic() + 5
    while runner.running(home, "gone") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.last_failure(home, "gone") is None and runner.last_output(home, "gone") == []
    key = (str(home), "gone")
    assert key not in runner._failures and key not in runner._outputs


# ---------- 两份插件同时挂上：只有一份记录 ----------

REPO = Path(__file__).resolve().parents[2]


def traced_records(root: Path, tmp_path: Path, home: Path, plugins: list[str], name: str) -> tuple[list[dict], str]:
    """在 root 里跑 pytest，挂上 plugins（按顺序）；返回 (记录文件里的记录（去掉版本头和 ns）, pytest 的输出)。"""
    import json

    from pystepmap.analyze import analyze_project
    from pystepmap.model import to_json

    graph, out = tmp_path / "g.json", tmp_path / f"{name}.jsonl"
    if not graph.exists():
        graph.write_text(to_json(analyze_project(root)), encoding="utf-8")
    env = {**os.environ, "PYSTEPMAP_GRAPH": str(graph), "PYSTEPMAP_TRACE_OUT": str(out), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(write_plugin_dir(home)), str(REPO / "core" / "src")])}
    env.pop("PYTEST_ADDOPTS", None)
    args = [a for p in plugins for a in ("-p", p)]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", *args, "-p", "no:cacheprovider", "tests"], cwd=root,
                       env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    return sorted(json.dumps({k: v for k, v in rec.items() if k not in ("ns", "seconds")}, sort_keys=True)
                  for rec in records if "format" not in rec), r.stdout


@pytest.mark.parametrize("order", [["pystepmap_trace_plugin", "pystepmap.trace"], ["pystepmap.trace", "pystepmap_trace_plugin"]])
def test_two_copies_of_the_plugin_record_only_once(home, tmp_path, order):
    """项目自己就挂着 -p pystepmap.trace（阶段 1 的做法，或者写在它的 addopts 里），平台又挂上自己的那份
    （pystepmap_trace_plugin）：两份都在，但只有先启用的那一份记录，另一份在汇总里说一句；记录和只挂一份时一样，没有 error 行。"""
    import shutil

    root = tmp_path / "transfer"
    shutil.copytree(REPO / "examples" / "transfer", root, ignore=shutil.ignore_patterns("__pycache__"))
    alone, _ = traced_records(root, tmp_path, home, ["pystepmap_trace_plugin"], "alone")
    both, output = traced_records(root, tmp_path, home, order, "both")
    assert alone and both == alone
    assert not any('"error"' in rec for rec in both)
    assert "不再重复记录" in output and output.count("pystepmap 记录：") == 1


@needs_venv
def test_a_virtualenv_inside_the_tested_project_is_kept(home, monkeypatch):
    """pystepmap 装在被测项目自己的虚拟环境里（服务的 sys.prefix 在项目目录里面，比如在 pystepmap 仓库自己里跑它的测试）：
    那就是项目的解释器，不去掉——否则 python -m pytest、pytest 会找到别的解释器（"No module named pytest" / 127）。"""
    own = os.path.realpath(sys.prefix)
    root = Path(own).parent  # 虚拟环境所在的目录当作项目
    own_bin = os.path.join(sys.prefix, "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([own_bin, "/usr/bin", "/bin"]))
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    r = run(root, home, "echo PY=$(command -v python); python -c 'import sys; print(\"PREFIX=\" + sys.prefix)'; env")
    env = env_of(r.tail)
    assert os.path.realpath(env["PY"]).startswith(own) or env["PY"].startswith(own_bin)
    assert os.path.realpath(env["PREFIX"]) == own
    assert env["VIRTUAL_ENV"] == sys.prefix and env["PATH"].split(os.pathsep)[0] == own_bin


@pytest.mark.parametrize("how", ["timeout", "cancel"])
def test_a_script_does_not_carry_on_with_its_next_command(root, home, tmp_path, how):
    """测试命令是几条命令的脚本时：pytest 被 SIGINT 打断后以退出码 2 结束，sh 会接着跑下一条。所以同时给还在的 sh
    SIGTERM：下一条不再开始；pytest 照样跑完 fixture 的收尾。"""
    (root / "tests").mkdir()
    (root / "tests" / "test_slow.py").write_text(TEARDOWN_TEST, encoding="utf-8")
    teardown, started = tmp_path / "teardown.txt", tmp_path / "started.txt"
    command = (f"TEARDOWN_FILE={teardown} STARTED_FILE={started} {shlex.quote(sys.executable)} -m pytest -q "
               f"-p no:cacheprovider tests; echo SECOND; sleep 30")
    cancel = threading.Event()
    stopped_at: list[float] = []  # 取消的时刻（超时的时刻是 began + STOP_TIMEOUT）
    if how == "cancel":
        def cancel_when_started():
            deadline = time.monotonic() + 20
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            stopped_at.append(time.monotonic())
            cancel.set()
        threading.Thread(target=cancel_when_started, daemon=True).start()
    began = time.monotonic()
    r = run(root, home, command, timeout=STOP_TIMEOUT if how == "timeout" else 60, cancel=cancel)
    assert r.status == ("timeout" if how == "timeout" else "cancelled")
    assert "SECOND" not in r.tail, r.tail
    assert teardown.read_text() == "teardown ran"
    assert time.monotonic() - _stop_time(how, began, stopped_at) < 4.5  # 从发 SIGINT 起算，同上



def test_a_flood_of_output_keeps_only_the_last_lines(root, home):
    """大量输出：只留最后 TAIL_LINES 行（每块输出只处理它最后那些行，内存有上限），跨块的行、跨块的长行都对。"""
    r = run(root, home, "seq 1 300000; head -c 30000 /dev/zero | tr '\\0' y; echo; echo end")
    assert len(r.tail) == runner.TAIL_LINES
    assert r.tail[-1] == "end" and r.tail[-2] == "y" * runner.LINE_CHARS
    assert r.tail[:-2] == [str(i) for i in range(300000 - runner.TAIL_LINES + 3, 300001)]


PRE_GUARD = "642bebe~1:core/src/flowmap/trace.py"  # 加单份启用的记号之前的插件（"旧版"）


@pytest.fixture
def old_plugin(tmp_path) -> Path:
    """旧版插件放成一个顶层模块 pystepmap_old_trace（被测项目的环境里装着旧版 pystepmap、自己挂着插件的情形）。"""
    r = subprocess.run(["git", "show", PRE_GUARD], cwd=REPO, capture_output=True, check=False)
    if r.returncode != 0:
        pytest.skip("取不到旧版插件（不在 pystepmap 的 git 仓库里）")
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "pystepmap_old_trace.py").write_bytes(r.stdout.replace(b"flowmap", b"pystepmap").replace(b"FLOWMAP", b"PYSTEPMAP"))
    return folder


def traced_with(root: Path, tmp_path: Path, home: Path, plugins: list[str], name: str, extra: Path) -> tuple[list, str]:
    import json

    from pystepmap.analyze import analyze_project
    from pystepmap.model import to_json

    graph, out = tmp_path / "g.json", tmp_path / f"{name}.jsonl"
    if not graph.exists():
        graph.write_text(to_json(analyze_project(root)), encoding="utf-8")
    env = {**os.environ, "PYSTEPMAP_GRAPH": str(graph), "PYSTEPMAP_TRACE_OUT": str(out), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(write_plugin_dir(home)), str(extra)])}
    env.pop("PYTEST_ADDOPTS", None)
    args = [a for p in plugins for a in ("-p", p)]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", *args, "-p", "no:cacheprovider", "tests"], cwd=root,
                       env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()], r.stdout


def test_an_old_plugin_that_started_first_keeps_the_new_one_off(home, tmp_path, old_plugin):
    """旧版插件（没有单份启用的记号）先启用、占着名叫 pystepmap 的 sys.monitoring 工具编号：新的这份不再启用，汇总里说一句；
    记录只有一份。"""
    import shutil

    root = tmp_path / "transfer"
    shutil.copytree(REPO / "examples" / "transfer", root, ignore=shutil.ignore_patterns("__pycache__"))
    alone, _ = traced_with(root, tmp_path, home, ["pystepmap_trace_plugin"], "alone", old_plugin)
    # pytest 按 -p 的倒序调用 pytest_configure：后写的先启用
    both, output = traced_with(root, tmp_path, home, ["pystepmap_trace_plugin", "pystepmap_old_trace"], "both", old_plugin)
    assert sum("outcome" in r for r in both) == sum("outcome" in r for r in alone) == 9
    assert not any("error" in r for r in both)
    assert "sys.monitoring 工具编号" in output and "不再重复记录" in output


def test_a_new_plugin_that_started_first_marks_the_duplicate_records(home, tmp_path, old_plugin):
    """新的这份先启用、旧版后启用（旧版不看记号）：两份都在记，数字会重复。新的这份发现了就在记录文件里写一行
    {"error": "SecondPlugin: …"}，汇总里也说一句——平台据此在这次运行的说明里提醒。"""
    import shutil

    root = tmp_path / "transfer"
    shutil.copytree(REPO / "examples" / "transfer", root, ignore=shutil.ignore_patterns("__pycache__"))
    both, output = traced_with(root, tmp_path, home, ["pystepmap_old_trace", "pystepmap_trace_plugin"], "both", old_plugin)
    errors = [r["error"] for r in both if "error" in r]
    assert len(errors) == 1 and errors[0].startswith("SecondPlugin: ")
    assert sum("outcome" in r for r in both) == 18  # 确实重复了
    assert "记录可能重复" in output
