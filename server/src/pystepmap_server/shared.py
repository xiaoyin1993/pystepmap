"""流程（flows.py）、平台接口（api.py）、MCP（mcp_tools.py）共用的小东西：

- identity：目录的 (设备, inode)；
- 判断过期要用的、一次分析里不会变的东西（Facts：图里的 file_hashes、file_lines、那次分析用的配置），以及按分析 id
  缓存它们的 FactsCache。file_hashes 只在图里：从 10 MB 的图里解析一次要零点几秒，所以按分析 id 缓存；file_lines、
  配置从 analysis_extra 取，不用解析图。这些对同一个分析 id 永远不变（id 不重用），别的进程写库也不会让缓存过时。
- Uncovered：一次（分析的图，运行的路径）里没走到的结局，算一次、按函数分好（接口的 /uncovered 和 MCP 的
  uncovered_outcomes 用）。core 的 runpath.uncovered_outcomes 对每个函数的判断和功能线无关，一条线只是挑出它涉及的
  函数、按线上的顺序排；所以算一次全部的，按函数分组，再按每条线的函数顺序拼出来，和逐条线调用的结果一样（测试里
  对过），不用留着几十 MB 的运行路径。
- place_run_functions：一次运行里的函数 sid 放到一张图上（core 的 locate_function），一次放一批。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from pystepmap.model import ProjectGraph, from_json
from pystepmap.query import line_functions
from pystepmap.runpath import (
    Outcome,
    RunPaths,
    locate_function,
    run_paths_from_json,
    uncovered_outcomes,
)

from .store import Store


def identity(path: str | Path) -> tuple[int, int] | None:
    """目录的 (设备, inode)：大小写不敏感的文件系统上写法不同、或经 /System/Volumes/Data 这类固定链接到达的同一个
    目录，路径字符串不同，inode 相同。读不到时为 None。"""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


@dataclass(frozen=True)
class Facts:
    """一次分析里不会变的、判断过期要用的东西。"""

    hashes: dict[str, str]  # 图里的 file_hashes
    file_lines: dict | None  # 结构版本 1 留下的分析为 None
    config: dict | None  # 这次分析用的配置；结构版本 1 留下的分析为 None


NO_FACTS = Facts({}, None, None)


def load_facts(store: Store, analysis_id: int, graph: ProjectGraph | None = None) -> Facts | None:
    """从库里读一次分析的 Facts（不缓存）。graph 是已经解析好的那次分析的图（给了就不再解析图 JSON）。分析不存在
    （已被清理）时 None。"""
    extra = store.analysis_extra(analysis_id)
    if extra is None:
        return None
    if graph is not None:
        hashes = dict(graph.file_hashes)
    else:
        text = store.graph_json(analysis_id)
        if text is None:  # 刚被清理掉了
            return None
        hashes = dict(json.loads(text).get("file_hashes", {}))
    return Facts(hashes, extra["file_lines"], extra["config"])


class FactsCache:
    """最近用到的 size 次分析的 Facts。可以在多个线程里同时用。查的分析不存在时 None（不缓存）。"""

    def __init__(self, store: Store, size: int):
        self._store = store
        self._size = size
        self._items: OrderedDict[int, Facts] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, analysis_id: int, graph: ProjectGraph | None = None) -> Facts | None:
        with self._lock:
            if analysis_id in self._items:
                self._items.move_to_end(analysis_id)
                return self._items[analysis_id]
        facts = load_facts(self._store, analysis_id, graph)
        if facts is None:
            return None
        with self._lock:
            self._items[analysis_id] = facts
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return facts


class Uncovered:
    """一次（图，运行路径）的没走到的结局：outcomes(None) 是全部功能线的（同 uncovered_outcomes(graph, paths)），
    outcomes(功能线 sid) 是那一条线的；不是这张图里的功能线时抛 LookupError。只留结果，不留图和路径。
    then：运行时的那张图（graph 是运行之后重新分析的新图时给，见 graph_at_run）——运行之后才有的函数、结局算说不准，
    不算没走到。known_then：分得出哪些是运行之后才有的（graph 就是运行时的图，或者给了 then）。"""

    def __init__(self, graph: ProjectGraph, paths: RunPaths, then: ProjectGraph | None = None, *,
                 known_then: bool = True):
        self.known_then = known_then
        self._all = uncovered_outcomes(graph, paths, then=then)
        self._by_function: dict[str, list[Outcome]] = {}
        for o in self._all:
            self._by_function.setdefault(o.function, []).append(o)
        self._lines = {(ln.sid or ln.id): [graph.functions[k].sid or k for k in line_functions(graph, ln)]
                       for ln in graph.lines}

    def outcomes(self, line_sid: str | None = None) -> list[Outcome]:
        if line_sid is None:
            return list(self._all)
        if line_sid not in self._lines:
            raise LookupError(line_sid)
        return [o for f in self._lines[line_sid] for o in self._by_function.get(f, ())]


def uncovered_of(store: Store, project_id: str, run_id: int, analysis_id: int, graph: ProjectGraph | None = None
                 ) -> Uncovered | None:
    """一次运行叠到 analysis_id 那次分析的图上算出的没走到的结局（graph：已经解析好的那张图，没有时从库里取）。运行
    用的不是这次分析时，取运行时那次分析的图来分出运行之后才有的函数和结局（清理旧分析时留着最近一次全量运行用的
    那次，见 Store.prune_analyses）；那次分析已经清理掉时分不出来，known_then 为 False。None：分析或运行刚被清理掉了。
    读不出来的数据抛 OSError / EOFError / ValueError。"""
    run = store.run_info(project_id, run_id)
    gz = store.run_paths_gz(project_id, run_id)
    if graph is None:
        text = store.graph_json(analysis_id)
        graph = None if text is None else from_json(text)
    if run is None or gz is None or graph is None:
        return None
    then, known = None, True
    if run["analysis_id"] != analysis_id:
        text = store.graph_json(run["analysis_id"]) if run["analysis_id"] is not None else None
        then, known = (None, False) if text is None else (from_json(text), True)
    return Uncovered(graph, run_paths_from_json(json.loads(gzip.decompress(gz))), then, known_then=known)


def _base_sid(sid: str) -> str:
    """去掉结尾的重名编号 "#数字"（pystepmap.runpath 模块说明里的 base(sid)）。"""
    head, sep, tail = sid.rpartition("#")
    return head if sep and tail.isdigit() else sid


def place_run_functions(graph: ProjectGraph, meta: RunPaths, sids: list[str]) -> list[str | None]:
    """一次运行里的函数 sid 各放到图里的哪个函数（函数 key），放不上为 None——就是 core 的 locate_function(graph,
    paths, sid)。meta 只要运行一级的 shapes、function_dups（tests 可以是空的）。按 core 写下的规则（pystepmap.runpath
    "叠到图上"第 1 条），放一个函数只看图里和它同一个基础 sid 的函数、运行时同一个基础 sid 的 shapes，以及
    function_dups；所以按基础 sid 先分好组，每次只把那一组交给 locate_function（不然每次都要扫一遍整张图、全部 shapes，
    真实项目几千个函数要几秒）。和逐个对整张图调用的结果一样（测试里对过）。"""
    groups: dict[str, dict[str, object]] = {}
    for key, g in graph.functions.items():
        groups.setdefault(_base_sid(g.sid or key), {})[key] = g
    shapes: dict[str, dict] = {}
    for sid, shape in meta.shapes.items():
        shapes.setdefault(_base_sid(sid), {})[sid] = shape
    out: list[str | None] = []
    for sid in sids:
        base = _base_sid(sid)
        part = dataclasses.replace(graph, functions=groups.get(base, {}), lines=[], sids={})
        fg = locate_function(part, RunPaths([], meta.version, shapes.get(base, {}), meta.function_dups), sid)
        out.append(None if fg is None else fg.key)
    return out


def line_entries(graph: ProjectGraph, line: object) -> frozenset[str]:
    """一条功能线的入口函数：派发树上每个任务的函数（根任务和它派发出去的任务；goto 的不重复算）。一个测试"走过这条
    线"指它执行过其中至少一个——只执行了线上更深处的函数（共用的登录校验、数据库会话之类，几乎每条线都涉及）不算。"""
    out: set[str] = set()
    stack = [line.root]
    while stack:
        task = stack.pop()
        if task.function in graph.functions:
            out.add(task.function)
        stack += task.children
    return frozenset(out)


def tests_on_line(store: Store, project_id: str, run_id: int, graph: ProjectGraph, line_sid: str) -> list[str] | None:
    """这次运行里走过这条功能线的测试（测试 id，按字母排序；测试之外的执行不算）：执行过这条线的入口函数（line_entries）
    的。线按 graph，运行的函数按 core 的规则放到 graph 上（place_run_functions）——和接口 GET …/runs/{id}?line= 的
    on_line 不为 0 是同一个标准。graph 里没有这条线时抛 LookupError；这次运行没有函数索引、没有路径时 None。"""
    line = next((ln for ln in graph.lines if (ln.sid or ln.id) == line_sid), None)
    if line is None:
        raise LookupError(line_sid)
    index = store.run_function_index(project_id, run_id)
    meta = None if index is None else store.run_paths_meta(project_id, run_id)
    if index is None or meta is None:
        return None
    entries = line_entries(graph, line)
    placed = place_run_functions(graph, run_paths_from_json({**meta, "tests": []}), index["functions"])
    return sorted(test for test, used in index["tests"].items() if test and any(placed[i] in entries for i in used))
