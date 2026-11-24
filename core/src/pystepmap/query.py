"""读图的查询：功能线涉及哪些函数、函数和文件在哪些功能线上；按文件:行或函数名反查节点，函数图里到某个节点的路径，
功能线里从根任务到某个函数的调用链，找结局。都是只读的函数，除了 find_by_location 为认出装饰器行、match 守卫行会读一次
源文件（见 _source_lines），都只读图。"""

from __future__ import annotations

import ast
import hashlib
import heapq
import os
import posixpath
import re
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .model import Edge, FlowLine, FunctionGraph, LineTask, Node, ProjectGraph


def line_functions(graph: ProjectGraph, line: FlowLine) -> list[str]:
    """这条线涉及的函数 key：派发树上的每个任务（先序），每个任务之后紧跟从它出发沿 call_targets 能到的所有函数
    （深度优先、按源码顺序）；按发现顺序，不重复。环（互相调用、任务派发自己）各只走一次；不在图里的 key 跳过。"""
    out: list[str] = []
    seen: set[str] = set()
    tasks = [line.root]
    while tasks:
        task = tasks.pop()
        stack = [task.function]
        while stack:
            key = stack.pop()
            if key in seen or key not in graph.functions:
                continue
            seen.add(key)
            out.append(key)
            stack.extend(reversed(graph.functions[key].call_targets))
        tasks.extend(reversed(task.children))
    return out


def lines_of_functions(graph: ProjectGraph) -> dict[str, list[str]]:
    """函数 key → 涉及它的功能线 id，按 graph.lines 的顺序；没有线涉及的函数不在里面。"""
    out: dict[str, list[str]] = {}
    for line in graph.lines:
        for key in line_functions(graph, line):
            out.setdefault(key, []).append(line.id)
    return out


def file_lines(graph: ProjectGraph) -> dict[str, list[str]]:
    """相对路径 → 涉及这个文件里任一函数的功能线 sid，按 graph.lines 的顺序；按路径排序，没有线涉及的文件不在里面。"""
    out: dict[str, list[str]] = {}
    for line in graph.lines:
        for file in dict.fromkeys(graph.functions[key].file for key in line_functions(graph, line)):
            out.setdefault(file, []).append(line.sid)
    return dict(sorted(out.items()))


# ---------- 反查节点 ----------


@dataclass(frozen=True)
class NodeHit:
    function: str  # 函数 key
    node: str  # 节点 id
    how: Literal["covers", "nearest", "definition", "call_site"]
    lines: tuple[str, ...]  # 涉及这个函数的功能线 id，按 graph.lines 的顺序


def _relative(graph: ProjectGraph, file: str) -> str | None:
    """相对 root 的路径（"/" 分隔、规范化）；绝对路径要在 root 下（经过指向 root 的符号链接给出的也认：比较所在目录的
    真实路径，文件本身不解析）。指到 root 之外的（含 ".."）为 None。"""
    path = file.replace("\\", "/")
    root = graph.root.replace("\\", "/").rstrip("/")
    if path.startswith("/"):
        path = posixpath.normpath(path)
        if path.startswith(root + "/"):
            path = path[len(root) + 1:]
        else:
            real_dir, real_root = os.path.realpath(posixpath.dirname(path)), os.path.realpath(root)
            if real_dir != real_root and not real_dir.startswith(real_root.rstrip(os.sep) + os.sep):
                return None
            path = posixpath.join(os.path.relpath(real_dir, real_root).replace(os.sep, "/"), posixpath.basename(path))
    path = posixpath.normpath(path)
    if path in (".", "", "..") or path.startswith(("../", "/")):
        return None
    return path


MAX_SOURCE_BYTES = 2 * 1024 * 1024  # find_by_location 读源文件的上限，超过的不读


def _read_analysed(graph: ProjectGraph, file: str) -> bytes | None:
    """读图里记了指纹的这个文件：解析后的真实路径要仍在 root 里；打开一次（不跟随符号链接、不因 FIFO 阻塞），按打开的
    描述符确认是普通文件、不超过 MAX_SOURCE_BYTES 再读；内容的 sha256 要和图里记的相同（改过的文件行号对不上图）。
    任何一条不满足、读不了都为 None。"""
    recorded = graph.file_hashes.get(file)
    if recorded is None:
        return None
    try:
        root = Path(graph.root).resolve(strict=True)
        path = (root / file).resolve(strict=True)
        path.relative_to(root)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError, RuntimeError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != recorded:
        return None
    return raw


def _source_lines(graph: ProjectGraph, file: str) -> tuple[dict[int, int], list[tuple[int, int, int]]]:
    """从源文件补图里没有的两样行号：(def 行 → 第一个装饰器所在行, [(match 行, 守卫起始行, 守卫结束行)])。
    文件按 _read_analysed 的条件读；读不了、解析不了都当作没有（({}, [])）。不执行任何代码。"""
    raw = _read_analysed(graph, file)
    if raw is None:
        return {}, []
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError, RecursionError):
        return {}, []
    decorated: dict[int, int] = {}
    guards: list[tuple[int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.decorator_list:
            decorated[node.lineno] = min(d.lineno for d in node.decorator_list)
        elif isinstance(node, ast.Match):
            guards += [(node.lineno, c.guard.lineno, c.guard.end_lineno or c.guard.lineno)
                       for c in node.cases if c.guard is not None]
    return decorated, guards


def find_by_location(graph: ProjectGraph, file: str, line: int) -> list[NodeHit]:
    """文件 file（相对 root，或以 root 开头的绝对路径）第 line 行对应的节点。找包住这一行的最内层函数（从 def 行——
    含装饰器行——到 end_line）：
    - 行落在某些节点的 [line, end_line] 里 → 这些节点，how="covers"；match 守卫所在的行算 match 判断节点的（守卫里的
      调用记在判断节点上）；装饰器行算开始节点的；
    - 没有节点包住这一行 → 行号不大于它的最近的节点（同一行有几个时都给），how="nearest"（赋值、日志等不出节点的语句）；
    - 行不在任何函数里、文件不在图里、路径指到 root 之外 → 空表。
    装饰器行、守卫行要读源文件才知道（见 _source_lines）；文件在分析之后改过时不读，只按图里的行号。已经有节点包住这一行、
    这一行又不可能是守卫行（函数里没有在它之前开始的 match）时不读：节点行不会是装饰器行。"""
    rel = _relative(graph, file)
    if rel is None:
        return []
    fns = [fg for fg in graph.functions.values() if fg.file == rel]
    if not fns:
        return []
    plain = [fg for fg in fns if fg.line <= line <= fg.end_line]
    if plain:
        inner = max(plain, key=lambda f: (f.line, f.key))
        covers = [n for n in inner.nodes if n.line and n.line <= line <= n.end_line]
        guard_possible = any(n.type == "cond" and n.subtitle.startswith("match ") and n.line < line
                             for n in inner.nodes)
        if covers and not guard_possible:
            lines = tuple(lines_of_functions(graph).get(inner.key, []))
            return [NodeHit(inner.key, n.id, "covers", lines) for n in covers]
    decorated, guards = _source_lines(graph, rel)
    around = [fg for fg in fns if decorated.get(fg.line, fg.line) <= line <= fg.end_line]
    if not around:
        return []
    fg = max(around, key=lambda f: (f.line, f.key))  # 最内层：定义得最晚的那个（范围是嵌套的）
    lines = tuple(lines_of_functions(graph).get(fg.key, []))
    covers = [n for n in fg.nodes if n.line and n.line <= line <= n.end_line]
    if line < fg.line:  # 装饰器行
        covers = [n for n in fg.nodes if n.type == "start"]
    matches = {m for m, start, end in guards if start <= line <= end}
    covers += [n for n in fg.nodes if n.type == "cond" and n.line in matches and n.subtitle.startswith("match ")
               and n not in covers]
    if covers:
        return [NodeHit(fg.key, n.id, "covers", lines) for n in covers]
    before = [n for n in fg.nodes if n.line and n.line <= line]
    if not before:
        return []
    top = max(n.line for n in before)
    return [NodeHit(fg.key, n.id, "nearest", lines) for n in before if n.line == top]


def _named(fg: FunctionGraph, name: str) -> bool:
    return name in (fg.key, fg.sid, fg.qualname, fg.qualname.rsplit(".", 1)[-1])


def find_by_function(graph: ProjectGraph, name: str) -> list[NodeHit]:
    """name 是函数 sid、key、限定名（Svc.run）或最后一段名字（run）。每个匹配的函数（按 key 的顺序）：它的开始节点
    how="definition"，接着是别的函数里 calls[].target 是它的节点 how="call_site"（含派发；按调用方 key、节点顺序）。"""
    by_fn = lines_of_functions(graph)
    out: list[NodeHit] = []
    for key, fg in graph.functions.items():
        if not _named(fg, name):
            continue
        start = next((n for n in fg.nodes if n.type == "start"), None)
        if start is not None:
            out.append(NodeHit(key, start.id, "definition", tuple(by_fn.get(key, []))))
        for caller_key, caller in graph.functions.items():
            if caller_key == key:
                continue
            for n in caller.nodes:
                if any(ref.target == key for ref in n.calls):
                    out.append(NodeHit(caller_key, n.id, "call_site", tuple(by_fn.get(caller_key, []))))
    return out


# ---------- 路径 ----------

MAX_PATH_WORK = 200_000  # paths_to 最多展开这么多条部分路径，超出时按已找到的截断


@dataclass(frozen=True)
class PathStep:
    node: str
    kind: str = ""  # 从这个节点出去走的那条连线；终点那一步三项为空
    label: str = ""
    branch: str = ""


def paths_to(graph: ProjectGraph, function: str, node: str, *, limit: int = 20) -> tuple[list[list[PathStep]], bool]:
    """函数图里从开始节点到 node 的路径（到 node 即止，不再往后走）：每条连线在一条路径里至多走一次（所以循环至多走
    一圈）；短的在前（一样长的先后不作保证，通常按连线在图里的顺序）。超过 limit 条时只给前 limit 条，第二项为 True；展开的部分路径超过
    MAX_PATH_WORK 条时同样截断。函数、节点不存在或走不到时为 ([], False)；limit 不是正数时抛 ValueError。
    一样长的部分路径先展开走得深的（往终点钻），平衡的分支再多也很快找到前几条。"""
    if limit <= 0:
        raise ValueError(f"limit 必须是正整数：{limit}")
    fg = graph.functions.get(function)
    if fg is None or all(n.id != node for n in fg.nodes):
        return [], False
    start = next((n.id for n in fg.nodes if n.type == "start"), None)
    if start is None:
        return [], False
    outs: dict[str, list[int]] = {}
    ins: dict[str, list[str]] = {}
    for i, e in enumerate(fg.edges):
        outs.setdefault(e.src, []).append(i)
        ins.setdefault(e.dst, []).append(e.src)
    dist = {node: 0}  # 到 node 的最少连线数：既用来剪掉走不到 node 的，也是 A* 的下界
    queue = deque([node])
    while queue:
        cur = queue.popleft()
        for src in ins.get(cur, []):
            if src not in dist:
                dist[src] = dist[cur] + 1
                queue.append(src)
    if start not in dist:
        return [], False
    found: list[list[PathStep]] = []
    seq = 0
    heap: list[tuple[int, int, int, str, tuple[int, ...]]] = [(dist[start], 0, seq, start, ())]
    work = 0
    while heap:
        _, _, _, cur, used = heapq.heappop(heap)
        if cur == node:
            if len(found) == limit:
                return found, True
            found.append(_steps(fg.edges, used, node))
            continue
        work += 1
        if work > MAX_PATH_WORK:
            return found, True
        for i in outs.get(cur, []):
            dst = fg.edges[i].dst
            if i in used or dst not in dist:
                continue
            seq += 1
            heapq.heappush(heap, (len(used) + 1 + dist[dst], -(len(used) + 1), seq, dst, (*used, i)))
    return found, False


def _steps(edges: list[Edge], used: tuple[int, ...], last: str) -> list[PathStep]:
    return [PathStep(edges[i].src, edges[i].kind, edges[i].label, edges[i].branch) for i in used] + [PathStep(last)]


# ---------- 功能线里的调用链 ----------


@dataclass(frozen=True)
class ChainLink:
    function: str  # 函数 key
    node: str  # 在这个节点上派发 / 调用了下一环
    how: Literal["dispatch", "call"]


def _tree_tasks(task: LineTask) -> set[str]:
    out, stack = set(), [task]
    while stack:
        t = stack.pop()
        out.add(t.function)
        stack.extend(t.children)
    return out


def entry_chain(graph: ProjectGraph, line: FlowLine, function: str) -> list[ChainLink] | None:
    """从功能线的根任务到 function 最短的一条链（按环数；一样短的按函数发现顺序、节点顺序）：每一环是"在这个函数的这个
    节点上派发 / 调用了下一环"。派发只走到这条线派发树上的任务，调用只走到这条线涉及的函数（line_functions）。
    调用不在任何节点上的（折叠掉的分支、构造对象调用的 __init__ 等，只在 call_targets 里）记在开始节点上。
    function 就是根任务时为 []；这条线不涉及它时为 None。"""
    involved = set(line_functions(graph, line))
    if function not in involved:
        return None
    root = line.root.function
    if function == root:
        return []
    tasks = _tree_tasks(line.root)
    prev: dict[str, tuple[str, str, Literal["dispatch", "call"]] | None] = {root: None}
    queue = deque([root])
    while queue:
        key = queue.popleft()
        fg = graph.functions[key]
        hops: list[tuple[str, str, Literal["dispatch", "call"]]] = []
        for n in fg.nodes:
            for ref in n.calls:
                if ref.target is not None and (not ref.is_dispatch or ref.target in tasks):
                    hops.append((ref.target, n.id, "dispatch" if ref.is_dispatch else "call"))
        start = next((n.id for n in fg.nodes if n.type == "start"), "")
        hops += [(t, start, "call") for t in fg.call_targets]
        for target, node, how in hops:
            if target in prev or target not in involved:
                continue
            prev[target] = (key, node, how)
            if target == function:
                chain: list[ChainLink] = []
                cur = target
                while (step := prev[cur]) is not None:
                    chain.append(ChainLink(*step))
                    cur = step[0]
                return chain[::-1]
            queue.append(target)
    return None  # line_functions 能到的这里一定能到；防御


_RAISED = re.compile(r"抛出 ([A-Za-z_][\w.]*)")
_CATCH_ALL = {"Exception", "BaseException", "任意异常"}


def caught_on_chain(graph: ProjectGraph, chain: list[ChainLink], end: Node) -> tuple[str, Node] | None:
    """end 是被调函数里的一个 `raise X(...)` 结局、chain 是从功能线入口到那个函数的调用链时：这个异常在链上哪里被接住
    ——（函数 key, 捕获节点）；没被接住（或者看不出来）时 None。从离它最近的调用处往上找：调用处的节点有"出错"的连线
    通到捕获节点，捕获的类型里写着这个异常的类名，或者是 Exception / BaseException / 不写类型。被接住的异常不会让
    功能线以它结束（接住它的函数接着往下走），所以不是这条线的结局。派发出去的任务里抛的异常传不回派发它的函数，
    找到派发那一环为止。只按类名的字面比：父类捕获子类（except LookupError 接 KeyError）看不出来，算没接住。"""
    m = _RAISED.match(end.title_full or end.title) if end.type == "end" else None
    if m is None:
        return None
    name = m.group(1).rsplit(".", 1)[-1]
    for link in reversed(chain):
        if link.how != "call":
            return None
        fg = graph.functions[link.function]
        by_id = {n.id: n for n in fg.nodes}
        seen, todo = set(), [link.node]
        while todo:  # 一个 try 的几个 except 可能串着连
            src = todo.pop()
            for e in fg.edges:
                if e.src != src or e.kind != "raise" or e.dst in seen or e.dst not in by_id:
                    continue
                seen.add(e.dst)
                catch = by_id[e.dst]
                if catch.type != "catch":
                    continue
                kinds = {k.rsplit(".", 1)[-1] for k in re.findall(r"[A-Za-z_][\w.]*", catch.subtitle_full or catch.subtitle)}
                if name in kinds or kinds & _CATCH_ALL or "任意异常" in (catch.subtitle or ""):
                    return link.function, catch
                todo.append(catch.id)
    return None


def find_outcomes(graph: ProjectGraph, line: FlowLine, text: str) -> list[tuple[str, str]]:
    """这条线涉及的函数里（line_functions 的顺序），标题含 text（不分大小写，完整标题）或 end_kind 等于 text 的结局
    节点：[(函数 key, 节点 id)]，函数内按节点顺序。text 为空时为空表。"""
    if not text:
        return []
    needle = text.casefold()
    out: list[tuple[str, str]] = []
    for key in line_functions(graph, line):
        for n in graph.functions[key].nodes:
            if n.type == "end" and (needle in (n.title_full or n.title).casefold() or n.end_kind == text):
                out.append((key, n.id))
    return out
