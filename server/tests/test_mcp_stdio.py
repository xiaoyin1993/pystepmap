"""pystepmap mcp 端到端：起一个真的进程，按行往标准输入写 JSON-RPC，从标准输出读回复。

断言按传输层写（不依赖 MCP SDK 的客户端）：标准输出的每一行都是带 "jsonrpc": "2.0" 的 JSON；日志、分析子进程的
输出都不能混进来。另外：pystepmap serve 的数据目录锁被占着时照样能用；当前目录里的 json.py、mcp.py 不会被导入
（用户通常在被分析的项目里启动它）；数据目录打不开时中文说明、退出码 2。"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from pystepmap.analyze import analyze_with_notes
from pystepmap_server.cli import _lock_home
from pystepmap_server.flows import add_project
from pystepmap_server.store import Store

TOOLS = ["list_projects", "analyze_project", "project_status", "list_flows", "get_flow", "find_nodes",
         "paths_to_outcome", "get_node", "diff_flows", "check", "uncovered_outcomes",
         "list_requirements", "get_requirement", "save_requirement", "run_line_tests"]
MODULE_ARGS = [sys.executable, "-P", "-m", "pystepmap.cli", "mcp"]

ORDERS = '''\
from pystepkit import condition, outcome, step, workflow


@step(title="查询库存")
def load_stock(order: dict) -> dict:
    return {"left": 3, "wanted": order["count"]}


@condition(title="库存不够？")
def lacks_stock(stock: dict) -> bool:
    return stock["left"] < stock["wanted"]


@workflow(title="下单", group="订单")
def place_order(order: dict):
    stock = load_stock(order)
    if lacks_stock(stock):
        return outcome("failed", title="库存不够，没有下单")
    return outcome("ok", title="下单完成")
'''


def chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


class McpProcess:
    """一个 pystepmap mcp 进程：标准输出逐行收下来（原样留着，最后逐行检查），标准错误另外收。"""

    def __init__(self, args: list[str], home: Path, cwd: Path, env: dict[str, str] | None = None):
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith("PYSTEPMAP_") and k not in ("PYTHONPATH", "PYTHONSAFEPATH", "PYTHONHOME")}
        environ["PYSTEPMAP_HOME"] = str(home)
        environ.update(env or {})
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=environ, cwd=cwd)
        self.lines: list[str] = []
        self.errors: list[str] = []
        self.replies: dict[int, dict] = {}  # 等别的回复时先到的
        self._queue: queue.Queue[str] = queue.Queue()
        self._readers = [threading.Thread(target=self._pump, args=(self.proc.stdout, True), daemon=True),
                         threading.Thread(target=self._pump, args=(self.proc.stderr, False), daemon=True)]
        for reader in self._readers:
            reader.start()

    def _pump(self, stream, is_stdout: bool) -> None:
        for raw in stream:
            text = raw.decode("utf-8", "replace")
            if is_stdout:
                self.lines.append(text)
                self._queue.put(text)
            else:
                self.errors.append(text)

    @property
    def stderr(self) -> str:
        return "".join(self.errors)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, rid: int, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        return self.wait(rid, timeout)

    def wait(self, rid: int, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while rid not in self.replies:
            try:
                line = self._queue.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"{timeout} 秒内没有等到 {rid} 号请求的回复；标准错误：{self.stderr}") from None
            reply = json.loads(line)
            self.replies[reply.get("id")] = reply
        return self.replies.pop(rid)

    def initialize(self) -> dict:
        reply = self.request(1, "initialize", {"protocolVersion": LATEST_HANDSHAKE_VERSION, "capabilities": {},
                                               "clientInfo": {"name": "pystepmap-test", "version": "0"}})
        self.notify("notifications/initialized")
        return reply

    def call(self, rid: int, name: str, arguments: dict | None = None) -> dict:
        reply = self.request(rid, "tools/call", {"name": name, "arguments": arguments or {}})
        assert "result" in reply, reply
        return reply["result"]

    def close(self) -> int:
        """关掉标准输入：进程应该在 5 秒内自己退出。"""
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=5)
        finally:
            self.finish()

    def finish(self) -> None:
        """进程结束以后：等读线程收完最后的输出，再关掉三个管道（不留 ResourceWarning）。"""
        if self.proc.poll() is None:
            return
        for reader in self._readers:
            reader.join(5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except BrokenPipeError:  # 进程已经退出，写缓冲冲不出去
                    pass

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.finish()


@pytest.fixture
def started():
    procs: list[McpProcess] = []

    def start(args: list[str], home: Path, cwd: Path, env: dict[str, str] | None = None) -> McpProcess:
        p = McpProcess(args, home, cwd, env)
        procs.append(p)
        return p

    yield start
    for p in procs:
        p.kill()


@pytest.fixture
def home(tmp_path) -> Path:
    """预先加好项目 shop 的数据目录（在本进程里分析）。"""
    root = tmp_path / "work" / "shop"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "orders.py").write_text(ORDERS, encoding="utf-8")
    home = tmp_path / "home"
    result = add_project(Store(home / "pystepmap.db"), str(root), None, None, analyzer=analyze_with_notes)
    assert result["kind"] == "ok", result
    return home


def assert_protocol_only(p: McpProcess) -> None:
    assert p.lines
    for line in p.lines:
        message = json.loads(line)
        assert message.get("jsonrpc") == "2.0", line


def test_initialize_list_and_call_tools(started, home, tmp_path):
    p = started(MODULE_ARGS, home, tmp_path / "work" / "shop")
    init = p.initialize()["result"]
    assert init["protocolVersion"] == LATEST_HANDSHAKE_VERSION
    assert init["serverInfo"]["name"] == "pystepmap" and "tools" in init["capabilities"]
    assert chinese(init.get("instructions", ""))
    assert p.request(2, "ping") == {"jsonrpc": "2.0", "id": 2, "result": {}}
    listed = p.request(3, "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == TOOLS
    for tool in listed:
        assert chinese(tool["description"]) and tool["inputSchema"]["type"] == "object"
    projects = p.call(4, "list_projects")
    assert not projects.get("isError") and "shop" in projects["content"][0]["text"]
    missing = p.call(5, "get_flow", {"flow": "不存在的流程"})
    assert missing["isError"] is True
    assert "没有这条功能线" in missing["content"][0]["text"]
    bad = p.call(6, "get_flow", {"flow": "下单", "bogus": 1})
    assert bad["isError"] is True and "不认识的参数" in bad["content"][0]["text"]
    flow = p.call(7, "get_flow", {"flow": "下单"})
    assert flow["content"][0]["text"].startswith("# 下单  [line:app/orders.py::place_order]")
    # 重新分析：分析在子进程里跑，它的输出不能混进协议
    again = p.call(8, "analyze_project")
    assert not again.get("isError") and "已重新分析项目「shop」" in again["content"][0]["text"]
    assert p.close() == 0
    assert_protocol_only(p)
    assert "Warning" not in p.stderr, p.stderr


def test_runs_while_pystepmap_serve_holds_the_home_lock(started, home, tmp_path):
    fd = _lock_home(home)  # 和 pystepmap serve 拿的是同一把锁
    try:
        p = started(MODULE_ARGS, home, tmp_path)
        p.initialize()
        out = p.call(2, "list_projects")
        assert "shop" in out["content"][0]["text"]
        second = started(MODULE_ARGS, home, tmp_path)  # 同一个数据目录可以开多个
        second.initialize()
        assert "shop" in second.call(2, "list_projects")["content"][0]["text"]
        assert p.close() == 0 and second.close() == 0
    finally:
        os.close(fd)
    assert_protocol_only(p)


def test_console_script_does_not_import_the_working_directory(started, home, tmp_path):
    """用户在被分析的项目里启动 pystepmap mcp：项目里的 json.py、mcp.py 不能顶掉标准库和 SDK。"""
    script = Path(sys.executable).parent / "pystepmap"
    if not script.exists():
        pytest.skip(f"没有找到控制台脚本 {script}")
    cwd = tmp_path / "victim"
    cwd.mkdir()
    for name in ("json.py", "mcp.py", "anyio.py"):
        (cwd / name).write_text(textwrap.dedent("""\
            import sys
            sys.stderr.write("被导入了：当前目录进了模块搜索路径\\n")
            raise SystemExit(7)
            """), encoding="utf-8")
    p = started([str(script), "mcp"], home, cwd)
    p.initialize()
    assert "shop" in p.call(2, "list_projects")["content"][0]["text"]
    assert p.close() == 0
    assert "被导入了" not in p.stderr
    assert_protocol_only(p)


NOISY = """\
import os
import subprocess
import sys

from pystepmap_server import mcp_server, mcp_tools

quiet = mcp_tools.Tools.list_projects


def noisy(self):
    print("print 到标准输出")  # 进了 sys.stdout 的缓冲区
    os.write(1, "直接写描述符 1\\n".encode())
    subprocess.run([sys.executable, "-c", "print('子进程的标准输出')"], check=True)
    return quiet(self)


mcp_tools.Tools.list_projects = noisy
sys.exit(mcp_server.run())
"""


def test_stray_output_from_tools_never_reaches_the_protocol(started, home, tmp_path):
    """工具（引擎、流程）里万一有 print、直接写描述符 1、子进程继承了标准输出：都去标准错误，包括进程退出时才冲出来的
    sys.stdout 缓冲区。"""
    script = tmp_path / "noisy_server.py"
    script.write_text(NOISY, encoding="utf-8")
    p = started([sys.executable, "-P", str(script)], home, tmp_path)
    p.initialize()
    assert "shop" in p.call(2, "list_projects")["content"][0]["text"]
    assert "shop" in p.call(3, "list_projects")["content"][0]["text"]
    assert p.close() == 0
    assert_protocol_only(p)
    for text in ("print 到标准输出", "直接写描述符 1", "子进程的标准输出"):
        assert text in p.stderr


def test_unusable_home_exits_2_with_a_chinese_reason(started, tmp_path):
    bad = tmp_path / "not-a-dir"
    bad.write_text("x", encoding="utf-8")
    p = started(MODULE_ARGS, bad, tmp_path)
    p.proc.stdin.close()
    assert p.proc.wait(timeout=10) == 2
    p.finish()
    assert "无法打开平台数据目录" in p.stderr
    assert p.lines == []


MISSING_SDK = """\
import sys

for name in ("mcp", "mcp.server", "mcp.server.stdio", "mcp.server.lowlevel", "mcp_types"):
    sys.modules[name] = None  # 像没装 MCP SDK 一样（pystepmap_server 装了）
from pystepmap.cli import main

sys.exit(main(["mcp"]))
"""


def test_backend_without_the_mcp_sdk_prints_an_install_hint(started, home, tmp_path):
    script = tmp_path / "no_sdk.py"
    script.write_text(MISSING_SDK, encoding="utf-8")
    p = started([sys.executable, "-P", str(script)], home, tmp_path)
    p.proc.stdin.close()
    assert p.proc.wait(timeout=20) == 2
    p.finish()
    assert "没有安装 MCP SDK" in p.stderr and "uv sync" in p.stderr and "Traceback" not in p.stderr, p.stderr
    assert p.lines == []


# ---------- 慢的工具调用：不挡住别的消息，退出时不等它 ----------

SLOW_RULES = {"extra_rules": [{"name": "slow", "entry_decorators": [["(a+)+$", "route"]]}]}


def make_check_slow(home: Path, tmp_path: Path) -> Path:
    """让 shop 的约束检查卡在灾难性回溯的正则上（只改项目配置，存着的分析不受影响）。返回项目目录。"""
    root = tmp_path / "work" / "shop"
    (root / "app" / "slow.py").write_text(f"@{'a' * 40}()\ndef handler():\n    return 1\n", encoding="utf-8")
    store = Store(home / "pystepmap.db")
    store.set_config(store.list_projects()[0]["id"], SLOW_RULES)
    return root


def workers_of(pid: int) -> list[int]:
    """pid 的子进程里的分析 / 检查子进程。"""
    out = subprocess.run(["ps", "-A", "-o", "ppid=,pid=,command="], capture_output=True, text=True, check=True).stdout
    return [int(line.split()[1]) for line in out.splitlines()
            if line.split(None, 1)[0] == str(pid) and "pystepmap_server.worker" in line]


def gone(pid: int, within: float = 5) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_slow_check_times_out_and_ping_is_answered_meanwhile(started, home, tmp_path):
    root = make_check_slow(home, tmp_path)
    p = started(MODULE_ARGS, home, root, {"PYSTEPMAP_ANALYZE_TIMEOUT": "3"})
    p.initialize()
    began = time.monotonic()
    p.send({"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "check", "arguments": {}}})
    for i in range(3):
        sent = time.monotonic()
        assert p.request(60 + i, "ping", timeout=2)["result"] == {}
        assert time.monotonic() - sent < 1
        time.sleep(0.3)
    assert "shop" in p.call(70, "list_projects")["content"][0]["text"]  # 别的工具也照常
    result = p.wait(50, timeout=30)["result"]
    assert result["isError"] is True and "检查超过 3 秒，已中止" in result["content"][0]["text"]
    assert time.monotonic() - began < 15
    assert p.close() == 0
    assert_protocol_only(p)


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM")
@pytest.mark.parametrize("how", ["stdin", "sigterm"])
def test_exits_promptly_while_a_tool_call_runs(started, home, tmp_path, how):
    """Claude Code 关掉标准输入或发 SIGTERM 时，正在跑的检查不挡住退出，它的子进程被杀掉。"""
    root = make_check_slow(home, tmp_path)
    p = started(MODULE_ARGS, home, root, {"PYSTEPMAP_ANALYZE_TIMEOUT": "120"})
    p.initialize()
    p.send({"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "check", "arguments": {}}})
    deadline = time.monotonic() + 15
    while not (children := workers_of(p.proc.pid)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert children, p.stderr
    stopped = time.monotonic()
    if how == "stdin":
        p.proc.stdin.close()
    else:
        p.proc.send_signal(signal.SIGTERM)
    assert p.proc.wait(timeout=15) == 0
    assert time.monotonic() - stopped < 5
    p.finish()
    assert all(gone(child) for child in children)
    assert_protocol_only(p)
