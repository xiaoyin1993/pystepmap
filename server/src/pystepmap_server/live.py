"""运行中的进度：测试命令跑着的时候（运行的阶段 testing），GET /api/projects/{id}/runs 的 running 里多一个 progress，
让界面边跑边看到跑完了几个测试、正在跑哪个、最近跑完的几个，以及哪些功能线、节点已经走到过。

每次运行一个 LiveRun（testflows.run_test_command 在测试命令开始前建、结束后丢，挂在 runner 的运行表上；不入库、不写
文件）。它读记录插件正在写的记录文件（core 的 pystepmap.progress.TraceProgress：每次只读新追加的部分），按这次运行用的
那次分析（准备运行时放在临时目录里的图的副本）算：
- 功能线：每条线涉及的函数（core 的 query.line_functions，同 GET /runs/{id}?line= 和界面的功能线）里有几个执行过；
- 节点（?live_line=<功能线 sid>）：这条线涉及的每个执行过的函数里，哪些节点"走到过"——节点语句头部的行（line 到
  end_line）执行过。开始节点在函数执行过时算。光看执行过的行分不清的不给：捕获节点（except 那一行在异常只是经过、
  没被它接住时也执行）、和别的节点写在同一行的结局（return f(x)：f 抛出时这一行也执行过）、行号为 0 的结局（执行到
  函数末尾、未捕获异常退出）。
图第一次要用时才读（读一次，只留每条线的函数 key 和每个函数可以判断的节点，图本身丢掉）；读不了时只给计数。
读记录文件在一把锁里，最多每秒一次（POLL_EVERY），不管同时来多少个请求：一秒之内的请求用上次读到的。
插件每个测试结束时才写出执行记录，所以"走到过"说的是已经跑完的测试（加上跨测试结束才结束的执行）。

返回给界面的只有标识（测试 id、功能线 sid、函数 key、节点 sid）、计数、秒数、结果种类。
这里不是流程代码，不加 pystepkit 标记。只有 testflows（建）和 api（经 runner 读）用它；pystepmap mcp、分析子进程都不导入。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pystepmap.model import ProjectGraph, from_json
from pystepmap.progress import TraceProgress
from pystepmap.query import line_functions

logger = logging.getLogger(__name__)

POLL_EVERY = 1.0  # 最多每隔几秒读一次记录文件
RUNNING_SHOWN = 3  # running 里最多列几个正在跑的测试（其余的只给个数）
_START = "start"


@dataclass(frozen=True)
class _Graph:
    """这次运行用的图里要用的那一点：每条功能线涉及的函数 key，每个函数可以判断走没走到的节点。"""

    lines: dict[str, tuple[str, ...]]
    # 函数 key -> [(节点 sid, 头部的行；开始节点为 None)]，按图里节点的顺序
    nodes: dict[str, tuple[tuple[str, range | None], ...]]


def _graph_of(graph: ProjectGraph) -> _Graph:
    lines = {(ln.sid or ln.id): tuple(line_functions(graph, ln)) for ln in graph.lines}
    nodes: dict[str, tuple[tuple[str, range | None], ...]] = {}
    for key, fg in graph.functions.items():
        heads = [range(n.line, max(n.line, n.end_line) + 1) if n.line > 0 else range(0) for n in fg.nodes]
        taken = {ln for n, h in zip(fg.nodes, heads) if n.type != "end" for ln in h}
        out: list[tuple[str, range | None]] = []
        for n, head in zip(fg.nodes, heads):
            if n.type == "start":
                out.append((n.sid or n.id, None))
            elif n.type == "catch" or not head or (n.type == "end" and any(ln in taken for ln in head)):
                continue  # 光看执行过的行分不清（见模块说明）
            else:
                out.append((n.sid or n.id, head))
        nodes[key] = tuple(out)
    return _Graph(lines, nodes)


class LiveRun:
    """一次运行的进度。view() 可以从多个线程同时调用。"""

    def __init__(self, trace_path: Path, graph_path: Path, *, clock: Callable[[], float] = time.monotonic,
                 every: float = POLL_EVERY) -> None:
        self._trace_path = Path(trace_path)
        self._graph_path = Path(graph_path)
        self._clock = clock
        self._every = every
        self._lock = threading.Lock()
        self._graph: _Graph | None = None
        self._progress: TraceProgress | None = None
        self._snap: dict | None = None
        self._at = -math.inf
        self._lines: dict[str, dict] | None = None  # 按上次读到的算好的功能线
        self._nodes: dict[str, dict[str, list[str]]] = {}  # 同上，按功能线算好的节点

    def view(self, live_line: str | None = None) -> dict | None:
        """{"collected", "done", "passed", "failed", "skipped", "errors", "running": [最多 3 个测试 id], "running_more",
        "recent": [{test, outcome, seconds}], "lines": {功能线 sid: {"functions": 执行过的, "total": 涉及的}}（只列有
        函数执行过的线）}；给了 live_line 并且图里有这条线时多 "nodes": {函数 key: [节点 sid]}。读的时候出了意外（引擎
        的缺陷等）时记下来、返回 None（界面就不显示进度）。"""
        try:
            with self._lock:
                return self._view(live_line)
        except Exception:
            logger.exception("读运行中的进度时出错（%s）", self._trace_path)
            return None

    def _view(self, live_line: str | None) -> dict:
        if self._progress is None:
            self._graph = self._load_graph()
            self._progress = TraceProgress(self._trace_path,
                                           functions=None if self._graph is None else frozenset(self._graph.nodes))
        now = self._clock()
        if self._snap is None or now - self._at >= self._every:
            self._snap = self._progress.poll()
            self._at = now
            self._lines = None
            self._nodes = {}
        snap = self._snap
        out = {k: snap[k] for k in ("collected", "done", "passed", "failed", "skipped", "errors")}
        out["running"] = snap["running"][:RUNNING_SHOWN]
        out["running_more"] = max(0, len(snap["running"]) - RUNNING_SHOWN)
        out["recent"] = snap["recent"]
        out["lines"] = self._lines_reached(snap["functions"])
        if live_line is not None:
            nodes = self._nodes_reached(live_line, snap["functions"])
            if nodes is not None:
                out["nodes"] = nodes
        return out

    def _load_graph(self) -> _Graph | None:
        try:
            return _graph_of(from_json(self._graph_path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001 —— 图的副本读不了、不是图：只给计数
            logger.warning("读不了这次运行的图（%s），运行中的进度只给计数：%s", self._graph_path, type(e).__name__)
            return None

    def _lines_reached(self, functions: dict) -> dict[str, dict]:
        if self._lines is None:
            self._lines = {}
            for sid, keys in (self._graph.lines.items() if self._graph is not None else ()):
                reached = sum(1 for k in keys if k in functions)
                if reached:
                    self._lines[sid] = {"functions": reached, "total": len(keys)}
        return self._lines

    def _nodes_reached(self, line: str, functions: dict) -> dict[str, list[str]] | None:
        g = self._graph
        if g is None or line not in g.lines:
            return None
        found = self._nodes.get(line)
        if found is None:
            found = {}
            for key in g.lines[line]:
                run = functions.get(key)
                if run is None:
                    continue
                executed = run["lines"]
                found[key] = [sid for sid, head in g.nodes.get(key, ())
                              if head is None or any(ln in executed for ln in head)]
            self._nodes[line] = found
        return found
