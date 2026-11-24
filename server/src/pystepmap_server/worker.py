"""分析在子进程里跑：python -E -P -m pystepmap_server.worker。

为什么另起一个进程：配置里的正则写得不好（灾难性回溯）、项目大得离谱时，分析可能卡住很久；进程内的线程杀不掉，子进程
能。父进程（flows.run_analysis）等它到超时（环境变量 PYSTEPMAP_ANALYZE_TIMEOUT 秒，默认 120），超时就杀掉，项目和上一次
的结果保持原样。

协议：标准输入一个 JSON {"root", "config", "timeout"}；标准输出一个 JSON，
    {"ok": true, "graph_json", "summary", "engine_version", "file_lines"} 或 {"ok": false, "code", "reason"}；
日志、引擎里万一有的 print 都去标准错误。子进程异常退出（被杀、崩溃）或输出不是这个形状时，父进程给出 code "analysis"，
原因里带标准错误的最后几行。

约束检查（pystepmap mcp 的 check 工具）走同一个子进程，同样的超时、CPU 上限和"不导入项目代码"：请求多 "mode": "check"
和 "files"（只报告这些文件，null 表示全部）；结果 {"ok": true, "violations": [{"file", "line", "code", "message"}…],
"skipped": [[文件, 原因]…], "unmatched": [文件…], "ignored": [[文件, 原因]…]}（同 lint.CheckResult）或同样的失败形状。
父进程这边 check_in_subprocess；超时的原因以"检查超过 N 秒，已中止："开头。

本进程起的、还在跑的子进程记在一个集合里：kill_running() 把它们都杀掉（MCP 服务退出时用，不留下没人等的子进程）。

不导入、不执行被分析项目的代码：引擎只读源码；子进程的当前目录是平台数据目录（不是项目目录：-m 会把当前目录放进模块
搜索路径，项目里的 json.py 之类就会顶掉标准库），-P 再挡一层（不把当前目录放进搜索路径），-E 不看 PYTHONPATH、
PYTHONHOME 等环境变量（父进程的环境里可能指着别处）。不用 -I：它还会去掉用户目录的 site-packages。

子进程自己也有期限：父进程（平台服务、MCP）被杀掉时没人按超时杀它，所以它按请求里的 timeout 给自己定 CPU 时间上限
（向上取整再加 CPU_GRACE 秒，正常总是父进程先到点），到了系统发 SIGXCPU 结束它；同时关掉核心转储（RLIMIT_CORE 为 0），
被这样结束（或者崩溃）时不在当前目录——平台数据目录——里留下 core 文件。没有 resource 模块的系统跳过。

进程内的同一套（analyze_directory）给测试注入假的引擎用（flows 的 analyzer 参数）。这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from pystepmap.analyze import AnalysisNotes, analyze_with_notes
from pystepmap.config import ConfigError, ProjectConfig, parse_config
from pystepmap.engineid import ENGINE_VERSION
from pystepmap.entries import entry_notes, unlisted_notes
from pystepmap.lint import check_project
from pystepmap.model import ProjectGraph, to_json
from pystepmap.query import file_lines
from pystepmap.selfcheck import classify_unresolved

logger = logging.getLogger(__name__)

Analyzer = Callable[[Path, ProjectConfig], tuple[ProjectGraph, AnalysisNotes]]  # analyze_with_notes 的形状

WORKER_ARGS: tuple[str, ...] = ("-E", "-P", "-m", "pystepmap_server.worker")  # 跟在 sys.executable 后面
TIMEOUT_ENV = "PYSTEPMAP_ANALYZE_TIMEOUT"
DEFAULT_TIMEOUT = 120.0
CPU_GRACE = 5  # 子进程自己的 CPU 时间上限比父进程的超时多几秒
STDERR_LINES = 6  # 异常退出时原因里带标准错误的最后几行
STDERR_CHARS = 1500

_OK_KEYS = {"graph_json": str, "summary": dict, "engine_version": str, "file_lines": dict}
_CHECK_KEYS = {"violations": list, "skipped": list, "unmatched": list, "ignored": list}

_running: set[subprocess.Popen] = set()  # 本进程起的、还在跑的子进程
_running_lock = threading.Lock()


def summarize(graph: ProjectGraph, notes: AnalysisNotes, seconds: float,
              config: ProjectConfig | None = None) -> dict:
    """一次分析的摘要：和命令行 analyze 打印的那些数一致，存进库、列项目时返回给界面。"""
    sc = graph.selfcheck
    # 目标不明的调用里真正值得看的那部分（名字在项目里有定义，可能就是项目内的调用）：其余是第三方、标准库和日志
    blind = len(classify_unresolved(graph.functions, (config or ProjectConfig()).noise_receivers).blind)
    return {
        "functions": len(graph.functions),
        "lines": len(graph.lines),
        "common_tasks": len(graph.common_tasks),
        "files": len(graph.file_hashes),
        "unresolved_calls": sc.unresolved_calls,
        "unresolved_blind": blind,
        "guessed_calls": sc.guessed_calls,
        "unreachable": len(sc.unreachable),
        "dead_ends": len(sc.dead_ends),
        "skipped_files": [{"file": f, "reason": why} for f, why in sc.skipped_files],
        "source_roots": [r or "." for r in notes.source_roots],
        "rules": list(notes.rules),
        "excluded": dict(notes.excluded),
        "has_entries": bool(graph.lines),  # 有没有功能线（入口可以是路由、任务、工作流，也可以是命令、脚本、推断的）
        "entry_notes": entry_notes(graph),  # 功能线一条都没有、或者全靠推断时的说明（给人看的整句）
        # 不在任何功能线上、项目里也没有别处用到的函数（可能是没认出来的入口）
        "unlisted_notes": unlisted_notes(graph, getattr(notes, "mentioned", frozenset())),
        "mark_notes": list(notes.marks),  # 标记里读不出来的参数（group=算出来的值 等），每处一句
        "seconds": round(seconds, 2),
    }


def analyze_directory(root: str, config: object, analyzer: Analyzer | None = None) -> dict:
    """在本进程里调用引擎分析 root（只读）。引擎出的任何 Exception 都变成结果（见模块说明的形状）：配置不合法（包括
    运行中才发现的，比如 source_roots 里的目录不存在）code 是 "config"，其余 "analysis"。"""
    try:
        started = time.perf_counter()
        parsed = parse_config(config)
        graph, notes = (analyzer or analyze_with_notes)(Path(root), parsed)
        return {"ok": True, "graph_json": to_json(graph),
                "summary": summarize(graph, notes, time.perf_counter() - started, parsed),
                "engine_version": ENGINE_VERSION,
                "file_lines": file_lines(graph)}
    except ConfigError as e:
        return {"ok": False, "code": "config", "reason": f"分析配置不合法：{e}"}
    except Exception as e:  # 引擎出错：变成结果，项目和上一次的结果保持原样
        logger.exception("分析 %s 失败", root)
        return {"ok": False, "code": "analysis", "reason": f"分析失败：{type(e).__name__}: {e}"}


def check_directory(root: str, config: object, files: list[str] | None) -> dict:
    """在本进程里做约束检查（lint.check_project，只读）。形状见模块说明；配置不合法 code "config"，其余错 "analysis"。"""
    try:
        result = check_project(Path(root), parse_config(config), only_files=files)
    except ConfigError as e:
        return {"ok": False, "code": "config", "reason": f"分析配置不合法：{e}"}
    except Exception as e:  # 检查器出错：变成结果
        logger.exception("检查 %s 失败", root)
        return {"ok": False, "code": "analysis", "reason": f"检查失败：{type(e).__name__}: {e}"}
    return {"ok": True, "violations": [asdict(v) for v in result.violations],
            "skipped": [list(x) for x in result.skipped], "unmatched": list(result.unmatched),
            "ignored": [list(x) for x in result.ignored]}


def _limit_cpu(timeout: object) -> None:
    """关掉本进程的核心转储，并按父进程的超时定 CPU 时间上限（软限制，到了系统发 SIGXCPU，默认动作是结束进程）。
    timeout 不是正数时只关核心转储；没有 resource 模块（Windows）、设不了时跳过。只在子进程里调用：设了就收不回来。"""
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError) as e:
        logger.warning("没能关掉分析进程的核心转储：%s", e)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not (math.isfinite(timeout) and timeout > 0):
        return
    limit = math.ceil(timeout) + CPU_GRACE
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_CPU, (limit, hard))
    except (ValueError, OSError) as e:
        logger.warning("没能给分析进程定 CPU 时间上限：%s", e)


def main(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None, *, limit_cpu: bool = False) -> int:
    """子进程的入口：读请求、（limit_cpu 时按请求里的 timeout）给自己定 CPU 时间上限、分析、写一个 JSON 结果。结果总是
    写出来（退出码 0）；只有进程本身出了事（包括到了 CPU 时间上限）才异常退出。作为子进程运行时 limit_cpu 为真；测试在
    本进程里调用时不定（设了就收不回来）。"""
    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    try:
        request = json.loads(source.read().decode("utf-8"))
        root, config = request["root"], request["config"]
        mode, files = request.get("mode", "analyze"), request.get("files")
        if not isinstance(root, str):
            raise TypeError("root 必须是字符串")
        if mode not in ("analyze", "check"):
            raise ValueError(f"不认识的 mode：{mode!r}")
        if files is not None and not (isinstance(files, list) and all(isinstance(f, str) for f in files)):
            raise TypeError("files 必须是字符串列表或 null")
    except (ValueError, KeyError, TypeError) as e:
        result = {"ok": False, "code": "analysis", "reason": f"分析进程收到的请求不对：{type(e).__name__}: {e}"}
    else:
        if limit_cpu:
            _limit_cpu(request.get("timeout"))
        with contextlib.redirect_stdout(sys.stderr):  # 标准输出只留给结果
            result = analyze_directory(root, config) if mode == "analyze" else check_directory(root, config, files)
    sink.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sink.flush()
    return 0


# ---------- 父进程这边 ----------

def analyze_timeout() -> float:
    """分析的超时秒数：环境变量 PYSTEPMAP_ANALYZE_TIMEOUT（可以带小数），没有或不是正数时 120。每次调用时读。"""
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


def _stderr_tail(err: bytes) -> str:
    lines = [ln for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    return "\n".join(lines[-STDERR_LINES:])[-STDERR_CHARS:]


def _well_formed(result: object, keys: dict[str, type]) -> bool:
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return False
    if result["ok"]:
        return all(isinstance(result.get(k), tp) for k, tp in keys.items())
    return isinstance(result.get("code"), str) and isinstance(result.get("reason"), str)


def _read_result(returncode: int, out: bytes, err: bytes, keys: dict[str, type] = _OK_KEYS, what: str = "分析") -> dict:
    tail = _stderr_tail(err)
    if returncode != 0:
        how = f"被信号 {-returncode} 中止" if returncode < 0 else f"退出码 {returncode}"
        logger.warning("%s进程异常退出（%s）：%s", what, how, tail)
        return {"ok": False, "code": "analysis", "reason": f"{what}进程异常退出（{how}）：{tail or '没有错误输出'}"}
    try:
        result = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        result = None
    if not _well_formed(result, keys):
        logger.warning("%s进程的输出不是合法的结果（%d 字节）：%s", what, len(out), tail)
        return {"ok": False, "code": "analysis",
                "reason": f"{what}进程的输出不是合法的结果：{tail or out[:200].decode('utf-8', 'replace') or '没有输出'}"}
    if not result["ok"] and tail:
        logger.warning("%s失败，%s进程的错误输出：%s", what, what, tail)
    return result


def _communicate(payload: bytes, timeout: float, cwd: Path) -> tuple[int, bytes, bytes] | None:
    """起一个子进程、送请求、等结果：(退出码, 标准输出, 标准错误)；超时时杀掉、回收，返回 None。起不来时抛 OSError。
    子进程在跑的时候记在 _running 里（kill_running 用）。"""
    proc = subprocess.Popen([sys.executable, *WORKER_ARGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd)
    with _running_lock:
        _running.add(proc)
    try:
        try:
            out, err = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        except BaseException:  # Ctrl+C 等：不留下还在跑的子进程
            proc.kill()
            proc.communicate()
            raise
        return proc.returncode, out, err
    finally:
        with _running_lock:
            _running.discard(proc)


def kill_running() -> int:
    """杀掉本进程起的、还在跑的分析 / 检查子进程，返回杀了几个。等着它们的线程随即拿到"进程异常退出"的结果。"""
    with _running_lock:
        procs = [p for p in _running if p.poll() is None]
    for p in procs:
        with contextlib.suppress(OSError):
            p.kill()
    return len(procs)


def _timeout_reason(what: str, root: str, timeout: float) -> str:
    return (f"{what}超过 {timeout:g} 秒，已中止：{root}（项目太大，或者配置里的正则太慢；"
            f"可以用环境变量 {TIMEOUT_ENV} 调整秒数）")


def run_in_subprocess(root: str, config: dict, *, timeout: float, cwd: Path) -> dict:
    """在子进程里分析 root，结果的形状同 analyze_directory，另有超时：{"ok": False, "code": "timeout", "reason"}
    （子进程已被杀掉并回收）。cwd 是子进程的当前目录：给平台数据目录，不要给被分析的项目。"""
    payload = json.dumps({"root": root, "config": config, "timeout": timeout}, ensure_ascii=False).encode("utf-8")
    try:
        done = _communicate(payload, timeout, cwd)
    except OSError as e:
        return {"ok": False, "code": "analysis", "reason": f"无法启动分析进程：{e.strerror or e}"}
    if done is None:
        logger.warning("分析 %s 超过 %g 秒，已中止", root, timeout)
        return {"ok": False, "code": "timeout", "reason": _timeout_reason("分析", root, timeout)}
    return _read_result(*done)


def check_in_subprocess(root: str, config: dict, files: list[str] | None, *, timeout: float, cwd: Path) -> dict:
    """在子进程里做约束检查，结果的形状同 check_directory，另有超时（code "timeout"，原因以"检查超过 N 秒，已中止："
    开头）。cwd 同 run_in_subprocess：给平台数据目录。"""
    payload = json.dumps({"mode": "check", "root": root, "config": config, "files": files, "timeout": timeout},
                         ensure_ascii=False).encode("utf-8")
    try:
        done = _communicate(payload, timeout, cwd)
    except OSError as e:
        return {"ok": False, "code": "analysis", "reason": f"无法启动检查进程：{e.strerror or e}"}
    if done is None:
        logger.warning("检查 %s 超过 %g 秒，已中止", root, timeout)
        return {"ok": False, "code": "timeout", "reason": _timeout_reason("检查", root, timeout)}
    return _read_result(*done, keys=_CHECK_KEYS, what="检查")


if __name__ == "__main__":
    sys.exit(main(limit_cpu=True))
