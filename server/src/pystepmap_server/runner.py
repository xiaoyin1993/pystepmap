"""运行项目的测试：在被测项目的根目录里用 /bin/sh -c 执行用户填的测试命令，挂上记录插件（core 的 pystepmap.trace，
改名成 pystepmap_trace_plugin 放在数据目录里），
命令结束（或超时、被取消）后把它起的整个进程组都结束掉。

这是平台里唯一会执行被测项目代码的地方，和分析（worker.py）是两条分开的路：
- 分析：python -E -P -m pystepmap_server.worker，当前目录是平台数据目录，只读源码，不导入、不执行项目代码；
  分析、约束检查、MCP 的工具都只走那一条。
- 运行测试：用户自己填的命令，当前目录是项目根目录，按设计会执行项目代码（测试本身就是项目代码）。只有平台接口
  POST /api/projects/{id}/runs（本机页面上点"运行测试"，第一次运行一条命令前要确认）经 flows.run_project_tests
  走到这里；MCP 没有任何能启动、取消或配置测试运行的工具。

run_tests：命令自成一个会话和进程组（start_new_session），标准输入是空设备，标准输出和标准错误合在一起，只在内存里
留最后 TAIL_LINES 行（每行最多 LINE_CHARS 字）——pytest 的输出里有断言展开的取值，不写进库、不写文件，服务重启即丢。
环境变量在继承的基础上（先去掉平台服务自己的虚拟环境：指着它的 VIRTUAL_ENV、UV_PROJECT_ENVIRONMENT，PATH 里它的
bin，uv run 的运行期变量——测试命令要在用户自己的环境里找 python、pytest；这个虚拟环境就在被测项目里面时它就是项目
的解释器，留着；见 _without_own_venv）加：
PYSTEPMAP_GRAPH、PYSTEPMAP_TRACE_OUT（插件读的图、写的记录）、PYTHONPATH=插件目录（接在原有的
前面）、PYTEST_ADDOPTS=原有的 + " -p pystepmap_trace_plugin -p no:cacheprovider"（挂插件；不在项目里写 .pytest_cache）、
PYTHONDONTWRITEBYTECODE=1（不在项目里写 .pyc）；只跑一条功能线的测试时再加 PYSTEPMAP_ONLY_TESTS=名单文件（命令本身不改）。
结束进程组：超时（PYSTEPMAP_TEST_TIMEOUT 秒，默认 1800）或 cancel 置位（用户取消、服务退出）时先 SIGINT 整个进程组
（pytest 当作 Ctrl+C，照常跑 fixture 的收尾），最多等 INT_GRACE 秒；同时如果首进程还是 sh（几条命令的脚本），单给它
SIGTERM，免得 pytest 结束后它接着跑下一条命令；再 SIGTERM 整个进程组，KILL_GRACE 秒后还有剩下的 SIGKILL。
命令自己正常结束时，进程组里剩下的后台进程（测试起的服务等，可能还拿着输出管道）也照样结束（SIGTERM 起），不等它们。
管不到的：测试自己另开会话起的进程（setsid、start_new_session=True、守护进程化的服务）不在这个进程组里，结束进程组
碰不到它们，看门狗也碰不到——它们会一直跑，要测试自己收拾。它们拿着输出管道时，读输出的线程最多等 READER_GRACE 秒
就关掉我们这一端（不会一直挂着），之后它们往输出里写会收到 SIGPIPE / EPIPE。
看门狗：每次运行另起一个小进程（python -I，自成会话，不受终端的 Ctrl+C 影响），它一直读一个管道，写端在平台服务手里。
正常结束时服务往管道里写一个字节，看门狗随即退出；服务被强杀（SIGKILL、崩溃）时管道读到结尾，看门狗就结束测试的进程组
（同样先 SIGTERM，3 秒后 SIGKILL），再删掉这次运行的临时目录（只认 <数据目录>/runs/tmp-…）。服务下次启动时也会删掉
runs 下所有留下来的 tmp-… 目录（clean_leftovers；同一个数据目录只能开一个 pystepmap serve，所以这时不会有别的运行）。
服务正常退出（Ctrl+C、SIGTERM）时，app 的收尾调用 stop_all：取消这个数据目录的全部运行并等它们记下"已取消"。

插件目录（write_plugin_dir）：<数据目录>/plugin/pystepmap_trace_plugin.py——core 的 trace.py 原样复制、改了名字的一个
顶层模块，内容变了才重写。命令里的 python 按 PYTHONPATH 找到这一份，和服务端读记录的代码是同一个版本。不用 pystepmap 这个
包名：被测项目自己有 pystepmap 包时（pystepmap 仓库自己）会被挡住。插件按文件的真实路径认出自己（_SELF），副本在数据目录
里、不在任何项目的图里，所以不记录自己；被测项目自己的 trace.py（pystepmap 仓库里那份）是普通的项目代码，照常只记一次。

进程内的运行表（launch / announce / set_phase / running / cancel_run / stop_all）：每个（数据目录，项目）同时最多一次
运行；记着取消用的事件、当前阶段（preparing 拿到锁、检查图是否过期、准备临时文件 → analyzing 只在图过期、运行前重新
分析时 → testing 跑测试命令 → processing 读记录、核对、汇总、保存）和开始时间。最近一次运行的输出末尾（last_output）、最近一次没能存下运行记录的失败（last_failure：运行前的
分析失败、保存失败等，接口在 POST 返回 202 之后靠它告诉界面发生了什么）也只在这里，不入库。测试命令跑着时的进度
（live.LiveRun，set_live / live）也挂在运行表上：testflows 在测试命令开始前挂上、结束后取下，live 只在阶段 testing 时
给出；这里不导入 live（它只是一个对象）。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import math
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .store import private_dir, private_file

logger = logging.getLogger(__name__)

TIMEOUT_ENV = "PYSTEPMAP_TEST_TIMEOUT"
DEFAULT_TIMEOUT = 1800.0
TAIL_LINES = 200  # 输出只留最后几行
LINE_CHARS = 2000  # 每行最多几个字（更长的截断）
INT_GRACE = 5.0  # 超时、取消时先 SIGINT（pytest 照常收尾），等几秒再 SIGTERM
KILL_GRACE = 3.0  # SIGTERM 之后等几秒再 SIGKILL
KILL_WAIT = 2.0  # SIGKILL 之后最多再等几秒让进程组消失
READER_GRACE = 2.0  # 进程组结束之后最多等几秒把输出读完
WATCHDOG_WAIT = 5.0
START_WAIT = 30.0  # launch 最多等几秒让流程走到"标记正在忙"
PLUGIN_DIR = "plugin"
RUNS_DIR = "runs"
SCRATCH_PREFIX = "tmp-"
PLUGIN_MODULE = "pystepmap_trace_plugin"  # 插件目录里 core 的 trace.py 改名后的模块名
PYTEST_PLUGIN_OPTIONS = f"-p {PLUGIN_MODULE} -p no:cacheprovider"
PHASES = ("preparing", "analyzing", "testing", "processing")
CANCELLED_BEFORE_START = "运行在开始之前就被取消了"

_READ_CHUNK = 8192  # 每次最多读几个字节（一行更长时只留第一段）
_POLL = 0.05

# 看门狗：平台服务被强杀时结束测试的进程组、删掉这次运行的临时目录。只用标准库，用 python -I 跑。
_WATCHDOG = r"""
import os, shutil, signal, sys, time
pgid, scratch = int(sys.argv[1]), sys.argv[2]
try:
    told = sys.stdin.buffer.read(1)
except (OSError, KeyboardInterrupt):
    told = b""
if told == b"d":
    sys.exit(0)

def alive():
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True

def signal_group(sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass

if alive():
    signal_group(signal.SIGTERM)
    deadline = time.monotonic() + 3
    while alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if alive():
        signal_group(signal.SIGKILL)
if scratch and os.path.basename(scratch).startswith("tmp-") and os.path.basename(os.path.dirname(scratch)) == "runs":
    shutil.rmtree(scratch, ignore_errors=True)
"""


@dataclass
class RunResult:
    status: Literal["finished", "timeout", "cancelled", "failed_to_start"]
    exit_code: int | None  # 命令（/bin/sh）的退出码；被信号结束时为负数；没跑完时 None
    seconds: float
    tail: list[str]  # 输出的最后 TAIL_LINES 行，只留在内存里
    reason: str = ""  # 中文说明（超时、取消、没能启动时）


def run_timeout() -> float:
    """测试命令的超时秒数：环境变量 PYSTEPMAP_TEST_TIMEOUT（可以带小数），没有或不是正的有限数时 1800。每次调用时读。"""
    text = os.environ.get(TIMEOUT_ENV)
    if text is None:
        return DEFAULT_TIMEOUT
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        logger.warning("环境变量 %s 的值 %r 不是正数秒数，按 %g 秒", TIMEOUT_ENV, text, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value


# ---------- 插件目录、临时目录 ----------

def _trace_source() -> bytes:
    spec = importlib.util.find_spec("pystepmap.trace")
    if spec is None or spec.origin is None:
        raise OSError("找不到 pystepmap.trace（core 没有装好）")
    return Path(spec.origin).read_bytes()


def _write_if_changed(path: Path, data: bytes) -> None:
    """内容和权限都对时不动；否则写到同目录的临时文件再改名（正在跑的测试读到的总是完整的文件）。"""
    try:
        if path.read_bytes() == data and (os.name != "posix" or (path.stat().st_mode & 0o777) == 0o600):
            return
    except OSError:
        pass
    tmp = path.with_name(f".tmp-{secrets.token_hex(4)}-{path.name}")
    try:
        private_file(tmp)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_plugin_dir(home: Path) -> Path:
    """<数据目录>/plugin/pystepmap_trace_plugin.py：core 的 trace.py 原样复制、改个名字（它只用标准库、不导入 pystepmap 的
    其他模块，改名照样能用）。目录 0700、文件 0600；内容变了才重写。以前的代码放的 plugin/pystepmap/ 包删掉。返回
    <数据目录>/plugin（放进 PYTHONPATH 的那一级）。写不了时抛 OSError。"""
    base = Path(home) / PLUGIN_DIR
    private_dir(Path(home))
    private_dir(base)
    old = base / "pystepmap"  # 以前是一个叫 pystepmap 的包：会挡住被测项目自己的 pystepmap 包
    if old.is_symlink() or old.is_file():
        old.unlink()
    elif old.is_dir():
        shutil.rmtree(old)
    _write_if_changed(base / f"{PLUGIN_MODULE}.py", _trace_source())
    for extra in base.iterdir():  # 以前中途失败留下的临时文件
        if extra.name.startswith(".tmp-"):
            extra.unlink(missing_ok=True)
    return base


def make_scratch(home: Path) -> Path:
    """这次运行的临时目录 <数据目录>/runs/tmp-<随机>（0700）：图的副本、记录文件放在这里。写不了时抛 OSError。"""
    runs = Path(home) / RUNS_DIR
    private_dir(Path(home))
    private_dir(runs)
    path = runs / f"{SCRATCH_PREFIX}{secrets.token_hex(6)}"
    path.mkdir(mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def remove_scratch(path: Path | None) -> None:
    """删掉这次运行的临时目录（可以重复调用，不存在时什么都不做）。"""
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def clean_leftovers(home: Path) -> None:
    """删掉 <数据目录>/runs 下所有 tmp-… 目录：上次服务被强杀、看门狗也没来得及删的。只在服务启动时调用。"""
    runs = Path(home) / RUNS_DIR
    try:
        found = [p for p in runs.iterdir() if p.name.startswith(SCRATCH_PREFIX)]
    except OSError:
        return
    for p in found:
        shutil.rmtree(p, ignore_errors=True)


# ---------- 运行一条命令 ----------

def _same_path(a: str | None, b: str) -> bool:
    return bool(a) and os.path.isabs(a) and os.path.realpath(a) == b


def _inside_dir(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _without_own_venv(env: dict[str, str], root: str) -> dict[str, str]:
    """去掉平台服务自己的虚拟环境（uv run pystepmap serve、或激活了 pystepmap 的 .venv 时带进来的）：指着它的
    VIRTUAL_ENV（连同 VIRTUAL_ENV_PROMPT）和 UV_PROJECT_ENVIRONMENT、PATH 里它的 bin 那一项（只去这一项），以及 uv run
    的运行期变量（UV_RUN_RECURSION_DEPTH 和随它设的 UV、UV_INTERNAL__ 开头的）。别的照留。服务不在虚拟环境里跑时
    （sys.prefix 就是基础解释器）不碰 VIRTUAL_ENV 和 PATH；这个虚拟环境就在被测项目里面时（pystepmap 装在项目自己的
    venv 里，比如在 pystepmap 仓库自己里跑它的测试）也不碰——那就是项目的解释器。uv run 的运行期变量总是去掉（它们只属于
    启动服务的那一次 uv run）。"""
    env = dict(env)
    own = os.path.realpath(sys.prefix)
    if sys.prefix != sys.base_prefix and not _inside_dir(own, os.path.realpath(root)):
        if _same_path(env.get("VIRTUAL_ENV"), own):
            env.pop("VIRTUAL_ENV")
            env.pop("VIRTUAL_ENV_PROMPT", None)
        if _same_path(env.get("UV_PROJECT_ENVIRONMENT"), own):
            env.pop("UV_PROJECT_ENVIRONMENT")
        own_bin = os.path.join(own, "Scripts" if os.name == "nt" else "bin")
        if "PATH" in env:
            env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not _same_path(p, own_bin))
    if env.pop("UV_RUN_RECURSION_DEPTH", None) is not None:  # 在 uv run 里：UV 是它设的
        env.pop("UV", None)
    for name in [k for k in env if k.startswith("UV_INTERNAL__")]:
        env.pop(name)
    return env


def _environment(root: str, graph_path: Path, trace_path: Path, plugin_dir: Path,
                 only_path: Path | None = None) -> dict[str, str]:
    env = _without_own_venv(dict(os.environ), root)
    env["PYSTEPMAP_GRAPH"] = str(graph_path)
    env["PYSTEPMAP_TRACE_OUT"] = str(trace_path)
    env.pop("PYSTEPMAP_ONLY_TESTS", None)  # 继承来的不算：只跑哪些测试由这次运行自己定
    if only_path is not None:
        env["PYSTEPMAP_ONLY_TESTS"] = str(only_path)
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(plugin_dir) + (os.pathsep + old_path if old_path else "")
    old_opts = env.get("PYTEST_ADDOPTS", "")
    env["PYTEST_ADDOPTS"] = (old_opts + " " if old_opts.strip() else "") + PYTEST_PLUGIN_OPTIONS
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _pump(fd: int, tail: deque, stop: threading.Event) -> None:
    """把输出逐行放进 tail（只留最后 TAIL_LINES 行）。每读到一块只从后往前找这一块里最后 TAIL_LINES 行（前面的反正会被
    挤掉：输出刷屏时花的时间和一块里有多少行无关，也不和接口抢太多 GIL），手里最多留一段不到 _READ_CHUNK 字节的半行。很长的行只留
    第一段（截到 LINE_CHARS 字），后面的部分丢掉。用 select 等数据、每 0.1 秒看一次 stop：管道的写端被一个逃出进程组
    的进程拿着时（读不到结尾），run_tests 置位 stop 就能让这个线程结束，再关掉我们这一端。"""
    rest = b""  # 还没遇到换行的半行（最多 _READ_CHUNK 字节）
    skipping = False  # 正在丢一行太长的后半截

    def emit(raw: bytes) -> None:
        tail.append(raw[:LINE_CHARS * 4].decode("utf-8", "replace").rstrip("\r")[:LINE_CHARS])

    try:
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], _POLL * 2)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data = rest + chunk
            end = data.rfind(b"\n")
            if end >= 0:
                rest = data[end + 1:]  # 最后一段没有换行
                lines: list[bytes] = []
                while len(lines) < TAIL_LINES:  # 从后往前只找最后 TAIL_LINES 行：不切开整块
                    start = data.rfind(b"\n", 0, end) + 1
                    lines.append(data[start:end])
                    if start == 0:
                        if skipping:  # 一直找到了开头：第一行是那行太长的结尾
                            lines.pop()
                        break
                    end = start - 1
                skipping = False  # 见到了换行：太长的那一行结束了
                for line in reversed(lines):
                    emit(line)
            else:
                rest = data
            if len(rest) > _READ_CHUNK:
                if not skipping:
                    emit(rest)
                    skipping = True
                rest = b""
        if rest and not skipping:
            emit(rest)
    except (OSError, ValueError):  # 管道出了问题：输出就到这里
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):  # 没了（编号被别人的进程组用了也说明我们的早没了）
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _wait_group(proc: subprocess.Popen, pgid: int, seconds: float) -> bool:
    """最多等 seconds 秒让进程组里一个进程都不剩（同时回收首进程）。"""
    deadline = time.monotonic() + seconds
    while True:
        proc.poll()
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _leader_is_shell(pid: int) -> bool:
    """进程组的首进程现在还是我们起的 /bin/sh（命令是几条命令的脚本）；只有一条命令时 sh 直接换成了那个程序（exec），
    就不是了。看不出来时当作不是（宁可让脚本多跑下一条，也不提前打断 pytest 的收尾）。"""
    try:
        name = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2,
                              check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.basename(name) in ("sh", "bash", "dash", "zsh", "ksh")


def _end_group(proc: subprocess.Popen, pgid: int, *, interrupt: bool) -> None:
    """结束整个进程组；首进程总是被回收。interrupt（超时、取消、服务退出）时先 SIGINT——pytest 当作 Ctrl+C，照常跑
    fixture 的收尾——最多等 INT_GRACE 秒；同时首进程如果还是 sh（命令是几条命令的脚本），单给它 SIGTERM：pytest 被打断
    以退出码 2 结束后，sh 不会接着跑下一条命令。然后（命令自己正常结束、只剩后台进程时直接从这里开始）SIGTERM 整个
    进程组，KILL_GRACE 秒后还有剩下的 SIGKILL。注意：sh 的后台进程（&）不理 SIGINT，要等到 SIGTERM。"""
    proc.poll()
    if not _group_alive(pgid):
        proc.wait()
        return
    if interrupt:
        if proc.poll() is None and _leader_is_shell(proc.pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, signal.SIGTERM)  # 先停住脚本：之后的命令不再开始
        _signal_group(pgid, signal.SIGINT)
        if _wait_group(proc, pgid, INT_GRACE):
            proc.wait()
            return
    _signal_group(pgid, signal.SIGTERM)
    if not _wait_group(proc, pgid, KILL_GRACE):
        _signal_group(pgid, signal.SIGKILL)
        _wait_group(proc, pgid, KILL_WAIT)
    proc.wait()


def _start_watchdog(pgid: int, scratch: Path | None) -> subprocess.Popen | None:
    try:
        return subprocess.Popen([sys.executable, "-I", "-c", _WATCHDOG, str(pgid), str(scratch or "")],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as e:  # 起不来就不要看门狗：服务被强杀时测试进程会留下，记一条警告
        logger.warning("没能启动测试运行的看门狗进程（%s），服务被强杀时测试进程不会被结束", e.strerror or e)
        return None


def _release_watchdog(watchdog: subprocess.Popen | None) -> None:
    """告诉看门狗"正常结束了"（写一个字节），等它退出。"""
    if watchdog is None:
        return
    with contextlib.suppress(OSError, ValueError):
        watchdog.stdin.write(b"d")
    with contextlib.suppress(OSError, ValueError):
        watchdog.stdin.close()
    try:
        watchdog.wait(WATCHDOG_WAIT)
    except subprocess.TimeoutExpired:
        watchdog.kill()
        watchdog.wait()


def _wait_for(proc: subprocess.Popen, cancel: threading.Event, deadline: float) -> str:
    while True:
        if proc.poll() is not None:
            return "finished"
        if cancel.is_set():
            return "cancelled"
        if time.monotonic() >= deadline:
            return "timeout"
        cancel.wait(_POLL)


def run_tests(root: str, command: str, *, graph_path: Path, trace_path: Path, plugin_dir: Path,
              timeout: float, cancel: threading.Event, output: deque | None = None,
              scratch: Path | None = None, only_path: Path | None = None) -> RunResult:
    """在 root 里用 /bin/sh -c 执行 command（环境变量、进程组、超时、取消见模块说明），返回结果。从不抛 Exception：
    起不来时 status 为 failed_to_start。output 给出时（一个 deque）输出边跑边放进去（运行中看输出用）；scratch 是这次
    运行的临时目录，服务被强杀时看门狗删掉它。cancel 在开始之前就置位时什么都不执行，直接 cancelled。only_path：
    只跑一条功能线的测试时，名单文件（JSON，测试 id 的列表）的路径——记录插件收集完测试后只留名单里的
    （PYSTEPMAP_ONLY_TESTS，见 core 的 trace.pytest_collection_modifyitems；名单读不了时插件一个测试都不跑）。"""
    tail: deque = output if output is not None else deque(maxlen=TAIL_LINES)
    if cancel.is_set():
        return RunResult("cancelled", None, 0.0, [], CANCELLED_BEFORE_START)
    env = _environment(root, graph_path, trace_path, plugin_dir, only_path)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except (OSError, ValueError) as e:
        return RunResult("failed_to_start", None, 0.0, [], f"命令没能启动：{getattr(e, 'strerror', None) or e}")
    pgid = proc.pid  # 新会话的首进程：进程组号就是它的进程号
    watchdog: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    stop = threading.Event()
    status, exit_code, seconds, problem = "", None, 0.0, None
    try:  # 看门狗、读输出的线程也在这里面起：起不来时进程组照样结束
        watchdog = _start_watchdog(pgid, scratch)
        reader = threading.Thread(target=_pump, args=(proc.stdout.fileno(), tail, stop), daemon=True,
                                  name="pystepmap-test-output")
        reader.start()
        status = _wait_for(proc, cancel, started + timeout)
        seconds = time.monotonic() - started
        exit_code = proc.returncode if status == "finished" else None
    except (RuntimeError, OSError, ValueError) as e:  # 读输出的线程起不来（系统线程用完）等：当作没能启动
        problem = e
    finally:  # 怎么结束的都一样：不留下进程。没跑完（超时、取消、出错、被打断）时先 SIGINT
        _end_group(proc, pgid, interrupt=status != "finished")
        _release_watchdog(watchdog)
        if reader is not None and reader.is_alive():
            reader.join(READER_GRACE)  # 进程组没了：通常马上读到结尾
            stop.set()  # 还没完：写端在一个逃出进程组的进程手里，不等它
            reader.join(1.0)
        proc.stdout.close()
    if problem is not None:
        return RunResult("failed_to_start", None, round(time.monotonic() - started, 3), list(tail),
                         f"命令没能启动：没能开始读取命令的输出（{type(problem).__name__}）")
    reason = {"timeout": f"测试运行超过 {timeout:g} 秒，已中止（可以用环境变量 {TIMEOUT_ENV} 调整秒数）",
              "cancelled": "运行被取消了"}.get(status, "")
    return RunResult(status, exit_code, round(seconds, 3), list(tail), reason)


# ---------- 进程内的运行表 ----------

@dataclass
class _Run:
    cancel: threading.Event
    started_at: str
    started: float
    phase: str = "preparing"
    announced: bool = False  # 流程已经走到"标记正在忙"
    given_up: bool = False  # launch 已经不等了（之后的失败要记下来）
    result: dict | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    cancelled_by: str | None = None  # "user"（cancel_run）或 "shutdown"（stop_all）
    forgotten: bool = False  # 跑着的时候项目被删了（forget）：结束时什么都不记
    live: object | None = None  # 测试命令跑着时的进度（live.LiveRun），testflows 挂上、取下
    scope: dict | None = None  # 只跑一条功能线的测试时 {"line", "line_title", "tests"}；全部测试时 None


_lock = threading.Lock()
_runs: dict[tuple[str, str], _Run] = {}
_outputs: dict[tuple[str, str], deque | list] = {}
_failures: dict[tuple[str, str], dict] = {}


def _key(home: Path, project_id: str) -> tuple[str, str]:
    return str(Path(home)), project_id


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _failure_of(result: dict | None) -> dict | None:
    """流程的结局没存下运行记录（没有 run_id）时，给界面看的失败：{"title", "reason", "code", "at"}。"""
    if result is None or result.get("kind") == "ok" or result.get("run_id") is not None:
        return None
    return {"title": result.get("title", ""), "reason": result.get("reason", ""), "code": result.get("code", ""),
            "at": _now()}


def launch(home: Path, project_id: str, target: Callable[[threading.Event], dict], *,
           wait: float = START_WAIT, scope: dict | None = None) -> dict | None:
    """在后台线程里跑 target(cancel)（运行测试的流程）。等到流程宣布开始（announce：已经拿到项目的锁）就返回 None；
    流程在那之前就结束了（项目不存在、没填命令、正在分析……）时返回它的结局，由接口直接回给界面。本进程里这个项目
    已经有一次运行时返回 busy 的结局。流程之后的失败（没存下运行记录的）记在 last_failure 里。"""
    key = _key(home, project_id)
    run = _Run(threading.Event(), _now(), time.monotonic(), scope=scope)
    with _lock:
        if key in _runs:
            return {"kind": "failed", "title": "正在运行测试", "reason": "这个项目正在运行测试，等它跑完或先取消",
                    "code": "busy"}
        _runs[key] = run

    def body() -> None:
        result: dict | None = None
        try:
            result = target(run.cancel)
        except BaseException as e:  # 后台线程：不能让异常悄悄消失
            logger.exception("运行项目 %s 的测试时出错", project_id)
            result = {"kind": "failed", "title": "运行测试时内部出错", "reason": f"内部错误：{type(e).__name__}",
                      "code": "internal"}
        finally:
            with _lock:
                run.result = result
                _runs.pop(key, None)
                if run.forgotten:  # 项目删了：它的输出、失败都不留
                    _outputs.pop(key, None)
                    _failures.pop(key, None)
                elif run.announced or run.given_up:
                    failure = _failure_of(result)
                    if failure is None:
                        _failures.pop(key, None)
                    else:
                        _failures[key] = failure
            run.ready.set()

    run.thread = threading.Thread(target=body, name=f"pystepmap-run-{project_id}", daemon=True)
    run.thread.start()
    run.ready.wait(wait)
    with _lock:
        if run.result is not None and not run.announced:
            return run.result
        run.given_up = True
    return None


def announce(home: Path, project_id: str) -> None:
    """流程已经拿到项目的锁：launch 可以返回了；之前记着的失败作废。不在运行表里（直接调用流程）时什么都不做。"""
    key = _key(home, project_id)
    with _lock:
        run = _runs.get(key)
        if run is None:
            return
        run.announced = True
        run.phase = "preparing"
        _failures.pop(key, None)
    run.ready.set()


def set_phase(home: Path, project_id: str, phase: str) -> None:
    with _lock:
        run = _runs.get(_key(home, project_id))
        if run is not None:
            run.phase = phase


def set_live(home: Path, project_id: str, live: object | None) -> None:
    """挂上（测试命令开始前）或取下（结束后，None）这次运行的进度。不在运行表里时什么都不做。"""
    with _lock:
        run = _runs.get(_key(home, project_id))
        if run is not None:
            run.live = live


def live(home: Path, project_id: str) -> object | None:
    """正在跑测试命令（阶段 testing）的那次运行挂着的进度；别的时候 None。"""
    with _lock:
        run = _runs.get(_key(home, project_id))
        if run is None or not run.announced or run.phase != "testing":
            return None
        return run.live


def running(home: Path, project_id: str) -> dict | None:
    """本进程里这个项目正在进行的运行：{"started_at", "seconds", "phase", "scope"}；没有时 None。scope：只跑一条
    功能线的测试时 {"line", "line_title", "tests"}，全部测试时 None（launch 时给的）。"""
    with _lock:
        run = _runs.get(_key(home, project_id))
        if run is None or not run.announced:
            return None
        return {"started_at": run.started_at, "seconds": round(time.monotonic() - run.started, 1), "phase": run.phase,
                "scope": run.scope}


def cancel_run(home: Path, project_id: str) -> str | None:
    """请求取消正在进行的运行，返回它此刻的阶段（没有在跑的：None）。testing 时结束测试命令；preparing、analyzing 时
    测试命令不再开始（analyzing：等运行前的分析做完就停）；processing 时测试命令已经跑完，取消没有用——不置位，照常存完。"""
    with _lock:
        run = _runs.get(_key(home, project_id))
        if run is None or not run.announced:
            return None
        if run.phase != "processing":
            run.cancelled_by = run.cancelled_by or "user"
            run.cancel.set()
        return run.phase


def cancel_reason(home: Path, project_id: str) -> str | None:
    """这次运行是谁取消的："user"、"shutdown"（服务退出），没有取消时 None。"""
    with _lock:
        run = _runs.get(_key(home, project_id))
        return None if run is None else run.cancelled_by


def stop_all(home: Path, wait: float = 20.0) -> None:
    """服务退出时：取消这个数据目录的全部运行，最多等 wait 秒让它们结束进程组、记下"已取消"、放开锁。"""
    prefix = str(Path(home))
    with _lock:
        runs = [r for (h, _), r in _runs.items() if h == prefix]
        for r in runs:
            r.cancelled_by = "shutdown"
            r.cancel.set()
    deadline = time.monotonic() + wait
    for r in runs:
        if r.thread is not None:
            r.thread.join(max(0.0, deadline - time.monotonic()))


def keep_output(home: Path, project_id: str, lines: deque | list) -> None:
    """记下这个项目最近一次运行的输出（可以是还在增长的 deque）。项目已经删了（forget）的运行不记。"""
    key = _key(home, project_id)
    with _lock:
        run = _runs.get(key)
        if run is None or not run.forgotten:
            _outputs[key] = lines


def last_output(home: Path, project_id: str) -> list[str]:
    """本进程里这个项目最近一次运行的输出末尾（运行中时是到现在为止的）；没有时空表。"""
    with _lock:
        lines = _outputs.get(_key(home, project_id))
        return [] if lines is None else list(lines)


def last_failure(home: Path, project_id: str) -> dict | None:
    """本进程里这个项目最近一次没能存下运行记录的失败 {"title", "reason", "code", "at"}；之后又开始了运行或存下了运行
    时清掉。"""
    with _lock:
        failure = _failures.get(_key(home, project_id))
        return None if failure is None else dict(failure)


def forget(home: Path, project_id: str) -> None:
    """项目删除了：丢掉它的输出和失败；还在跑的那次运行结束时也什么都不记。"""
    key = _key(home, project_id)
    with _lock:
        _outputs.pop(key, None)
        _failures.pop(key, None)
        run = _runs.get(key)
        if run is not None:
            run.forgotten = True
