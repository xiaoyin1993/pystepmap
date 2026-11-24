"""pystepmap mcp：标准输入输出方式的 MCP 服务，给 Claude Code 查项目的流程。工具在 mcp_tools.py。

协议层用官方 mcp Python SDK 的低层 Server（mcp.server.lowlevel）和 stdio_server：标准输入一行一条 JSON-RPC 消息，
标准输出只写协议消息。SDK 的 stdio_server 在服务期间把文件描述符 1 指到标准错误、0 指到空设备，协议走它自己复制出来
的描述符，所以直接写描述符 1 的和子进程的输出到不了协议上；sys.stdout 在服务期间另外换成标准错误（print 留在缓冲区
里的字不会在退出时冲到协议上）；日志在导入别的模块之前就配到标准错误。
关掉标准输入时服务结束，退出码 0；数据目录打不开时标准错误写中文原因，退出码 2。

和 pystepmap serve 用同一个数据目录（PYSTEPMAP_HOME，默认 ~/.pystepmap），但不拿 pystepmap.lock：可以和服务同时开，也可以
开多个。同一项目的分析靠项目级的锁互斥（locks.py），库的写入靠 SQLite 的写锁。

工具调用在工作线程里跑（分析要几秒，不挡住 ping 等别的消息）；Tools 可以在多个线程里同时调用。分析和约束检查都在
子进程里跑（worker.py，带超时）。退出不等还在跑的工具调用：标准输入关了，服务结束后杀掉还在跑的分析 / 检查子进程
（worker.kill_running），再最多等 EXIT_GRACE 秒让工作线程收尾，还没完就直接退出；收到 SIGTERM 时杀掉子进程、立刻
退出（退出码都是 0）。

不导入、不执行被分析项目的代码：Claude Code 通常在被分析的项目里启动它（当前目录就是项目），控制台脚本 pystepmap
不把当前目录放进模块搜索路径（用 python -m 启动时要加 -P）；分析在子进程里跑，子进程的当前目录是数据目录。
也不导入平台的接口和运行测试的代码：这里只导入 store（库、DB_NAME、default_home）和 mcp_tools（以及它们用到的 flows、
worker、shared 等），不导入 app、api、testflows、runner——pystepmap mcp 的进程里没有能执行项目测试的代码（测试真的
走一遍 pystepmap mcp 的入口，再查 sys.modules）。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

EXIT_GRACE = 3.0  # 标准输入关了以后，最多等还在跑的工具调用收尾几秒
_SDK_HINT = ("没有安装 MCP SDK（缺少 {name}）：在 pystepmap 仓库根目录运行 uv sync，再用 uv run pystepmap mcp 启动"
             "（不用 uv 时：pip install ./server）")

_busy = 0  # 正在工作线程里跑的工具调用
_busy_lock = threading.Lock()

SERVER_NAME = "pystepmap"
_BASE = (
    "pystepmap 把 Python 后端静态分析成业务流程图：每条功能线是一个入口（接口、后台任务、工作流）的流程，节点是一次步骤调用、"
    "一个判断、一个循环或一个结局。改带流程的代码之前，先用 find_nodes（文件 + 行号）和 get_flow 看清要改的位置在哪条"
    "功能线的哪个节点；改完调用 analyze_project 重新分析，再用 diff_flows 把流程的变化告诉用户。结果开头出现"
    "\"⚠ 代码已变更\"时图可能过期，先 analyze_project。不给 project 时用当前目录所在的项目。"
    "代码不需要任何标记就能分析：接口（FastAPI、Flask）、后台任务（taskiq、Celery、DBOS）、命令行子命令、"
    "`if __name__ == \"__main__\":` 里调用的函数都会各成一条功能线。新写了一个业务入口之后，analyze_project 再用 "
    "list_flows 确认它在列表里；不在时（analyze_project 的结果会列出\"不在任何功能线上的函数\"），在它的 def 上面"
    "（有装饰器时是第一个装饰器上面）加一行注释 `# pystepmap: 入口 <中文标题>` 再重新分析，这样写一定成线，不用装任何包。"
    "不在功能线上的函数也能看流程：get_flow 只给 function。"
)
_OWN_REQUIREMENTS = (
    "接到业务需求时：先 list_requirements 看有没有对应的需求；没有就用 save_requirement 把改完之后的流程写成一条需求"
    "（状态草稿），把返回里的流程图地址给用户看图确认，确认后再实现；实现并 analyze_project 之后用 get_requirement "
    "把设计和实现的差异告诉用户。"
)


_OPENSPEC_REQUIREMENTS = (
    "这个项目用 OpenSpec 管需求：需求、变更和任务照 OpenSpec 的流程走，不要用 save_requirement。涉及流程变化时，把改完之后"
    "的流程写成 OpenSpec 变更的 design.md 里的一个 ```flow 代码块（块里第一行 `功能线：<功能线的标题>`，写法见 "
    "save_requirement 的说明）；写好后用 get_requirement（编号就是变更名）取这份设计的流程图地址，给用户看图确认；"
    "实现并 analyze_project 之后再用 get_requirement 把设计和实现的差异告诉用户。"
)
INSTRUCTIONS = _BASE + _OWN_REQUIREMENTS  # 不知道当前目录是什么项目时的说明


def _project_root(cwd: Path | None) -> Path | None:
    """当前目录所在的项目根：从当前目录往上，第一个有 openspec/、.pystepmap/ 或 .git 的目录；找不到时 None。"""
    if cwd is None:
        return None
    for folder in (cwd, *cwd.parents):
        if any((folder / name).exists() for name in ("openspec", ".pystepmap", ".git")):
            return folder
    return None


def instructions_for(cwd: Path | None) -> str:
    """给 AI 的服务说明。需求那一段看当前目录所在的项目：用 OpenSpec（有 openspec/ 目录）的让需求走 OpenSpec、
    设计的流程写在它的文件里；其余的用 pystepmap 自己的需求文件。两段不会同时出现——同一件事只有一个入口。"""
    root = _project_root(cwd)
    if root is not None and (root / "openspec").is_dir():
        return _BASE + _OPENSPEC_REQUIREMENTS
    return INSTRUCTIONS


def _cwd() -> Path | None:
    try:
        return Path(os.getcwd())
    except OSError:  # 当前目录被删掉了
        return None


def _call(tools, name: str, arguments: dict | None) -> tuple[str, bool]:
    """调用一个工具：(文本, 是否出错)。ToolError 的信息原样给 AI；别的异常记日志（标准错误），给一句中文。"""
    from .mcp_tools import ToolError

    global _busy
    with _busy_lock:
        _busy += 1
    try:
        return tools.call(name, arguments), False
    except ToolError as e:
        return str(e), True
    except Exception as e:  # 不让一个工具的错误带走整个服务
        logger.exception("工具 %s 出错", name)
        return f"pystepmap 内部错误（{name}）：{type(e).__name__}: {e}", True
    finally:
        with _busy_lock:
            _busy -= 1


def _busy_calls() -> int:
    with _busy_lock:
        return _busy


def _terminate(signum, frame) -> None:
    """SIGTERM：杀掉还在跑的分析 / 检查子进程，立刻退出（不等工作线程，也不等标准输入）。"""
    from .worker import kill_running

    kill_running()
    sys.stderr.flush()
    os._exit(0)


def _finish_calls() -> None:
    """服务结束以后：杀掉还在跑的子进程，最多等 EXIT_GRACE 秒让工具调用收尾；还没完就直接退出。"""
    from .worker import kill_running

    kill_running()
    deadline = time.monotonic() + EXIT_GRACE
    while _busy_calls() and time.monotonic() < deadline:
        time.sleep(0.05)
    if _busy_calls():
        logger.warning("还有 %d 个工具调用没做完，不等了", _busy_calls())
        sys.stderr.flush()
        os._exit(0)


def build_server(tools, instructions: str = INSTRUCTIONS):
    """把 Tools 包成 SDK 的低层 Server：tools/list 来自 TOOL_SPECS，tools/call 在工作线程里调 Tools.call。"""
    import anyio
    import mcp_types as types
    from mcp.server.lowlevel import Server

    from . import __version__
    from .mcp_tools import TOOL_SPECS

    listed = [types.Tool(name=s["name"], description=s["description"], input_schema=s["inputSchema"]) for s in TOOL_SPECS]

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=listed)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        # abandon_on_cancel：客户端断开（标准输入关了）时不等这个线程，退出时由 _finish_calls 收尾
        text, failed = await anyio.to_thread.run_sync(_call, tools, params.name, params.arguments,
                                                      abandon_on_cancel=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=failed)

    return Server(SERVER_NAME, version=__version__, instructions=instructions,
                  on_list_tools=list_tools, on_call_tool=call_tool)


def run(home: Path | None = None) -> int:
    """pystepmap mcp 的入口：打开数据目录，在标准输入输出上提供工具，直到标准输入关闭。返回退出码。"""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="pystepmap mcp %(levelname)s %(name)s: %(message)s")
    try:  # 先看 SDK 在不在：缺了给一句中文，不是一大段 traceback
        import anyio
        import mcp_types  # noqa: F401
        from mcp.server.lowlevel import Server  # noqa: F401
        from mcp.server.stdio import stdio_server
    except ModuleNotFoundError as e:
        print(_SDK_HINT.format(name=e.name or "mcp"), file=sys.stderr)
        return 2
    from .store import DB_NAME, Store, StoreError, default_home

    home = Path(home) if home is not None else default_home()
    try:
        store = Store(home / DB_NAME)
    except StoreError as e:
        print(f"无法打开平台数据目录：{e}", file=sys.stderr)
        return 2

    from .mcp_tools import Tools

    server = build_server(Tools(store, _cwd()), instructions_for(_cwd()))

    async def serve() -> None:
        # stdio_server 先认领描述符 1（要求这时 sys.stdout 还是它），之后 Python 层面的 print 也改去标准错误：
        # 只靠描述符改道不够，留在 sys.stdout 缓冲区里的字会在退出时（描述符已经还原）冲到协议上
        async with stdio_server() as (read_stream, write_stream):
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    await server.run(read_stream, write_stream, server.create_initialization_options())
            finally:
                if sys.__stdout__ is not None:  # 万一有人写了原来的 sys.stdout：趁描述符 1 还指着标准错误冲掉
                    sys.__stdout__.flush()

    in_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _terminate) if in_main else None
    try:
        anyio.run(serve)
    except KeyboardInterrupt:  # 终端里 Ctrl+C：用户要停，不是出错
        pass
    finally:
        if in_main:
            signal.signal(signal.SIGTERM, previous)
    _finish_calls()
    return 0
