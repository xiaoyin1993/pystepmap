"""两次分析之间的流程差异：哪些函数的节点、连线变了，哪些功能线因此有变化。纯函数，只读两张图。

配对方法见 docs/graph-schema.md "两种标识：key 与 sid"：
- 函数、功能线按 sid 配对；只在新图里的是新增，只在旧图里的是删除。
- 配上对的函数：两边的节点各按（行，生成顺序）排成序列（开始节点最前，执行到函数末尾、未捕获异常退出最后），取去掉
  #n 的基础 sid 做序列对齐（difflib.SequenceMatcher）。对上的节点配对、不算变化（标题因为注释或被调函数的文档变了也不
  算）；对不上的一段里再按顺序配一次：类型相同、文字（标题 + 副标题，完整形式，前 200 字）相似度不低于 0.6 的，取
  保持顺序的最多对（最长公共子序列），配上的算改动（旧的改成了新的），其余旧的删除、新的新增。所以紧挨着改了的节点
  插进一个节点，不会让配对错开。一段太大时不逐对比（见 MAX_BLOCK_PAIRS、MAX_BLOCK_WORK）。
- 连线没有标识：只比两端都配上对（对上的或改动的）的连线，按（起点，终点，kind，label）比；一端是新增 / 删除节点的
  连线不单列，只有一个例外：新增的判断 / 循环 / 捕获节点的出边报成新增连线（看得到新分支通向哪里）。
  节点的增删已经说明了的连线变化也不单列（改动的节点算留下的）。少掉的 A→B（多出的同理，换成旧图和删除的节点）：
  新图里从 A 第一步进新增的节点（A 是判断节点时从同一个分支出发）、之后只经新增的节点走得到 B；或者新图里有一条从
  新增节点 P 到 B、kind 和 label 都相同的连线，而 P 只经新增的节点走得到 A（一段的入口换了，如 try 体第一句前插了
  一句，异常连线改从新的第一句出发）。在两个留下的节点之间插进 / 删掉节点因此不报连线变化。
- 功能线有变化：它涉及的函数（新图、旧图里各自的 line_functions，按 sid 合并）里有变化的，或者派发树上的任务集合变了。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Literal, get_args, get_origin, get_type_hints

from .model import (
    Edge,
    FlowLine,
    FunctionGraph,
    LineTask,
    Node,
    ProjectGraph,
    parse_key,
)
from .query import line_functions

DIFF_VERSION = 1
SIMILAR = 0.6  # 对不上的一段里，两个同类型节点的文字相似度（difflib ratio）不低于它才算"旧的改成了新的"
TEXT_LIMIT = 200  # 比相似时每个节点的文字只取前这么多字
# 对不上的一段太大时不再逐对比相似，全算删除和新增（整个函数大改时）：两边节点数之积超过 MAX_BLOCK_PAIRS，或两边文字
# 总长之积超过 MAX_BLOCK_WORK。后者按最坏情况（很长、很重复的文字）量过：到上限时逐对比相似约 1.5 秒
MAX_BLOCK_PAIRS = 30_000
MAX_BLOCK_WORK = 200_000_000
_BRANCHING = ("cond", "loop", "catch")  # 新增时把出边也报成新增连线的节点类型
_IMPLICIT_ENDS = ("end:implicit", "end:uncaught")


@dataclass
class NodeChange:
    """一个节点的变化；old_* / new_* 哪边没有就是空。标题、副标题是完整形式（x_full 不为空就用它，否则用 x）。"""

    status: Literal["added", "removed", "changed"]
    type: str
    new_id: str = ""
    new_sid: str = ""
    new_title: str = ""
    new_subtitle: str = ""
    new_line: int = 0
    old_sid: str = ""
    old_title: str = ""
    old_subtitle: str = ""
    old_line: int = 0
    # 以下只有 removed 有，界面和文本用来指位置：按旧图的控制流往回找到的、最近的仍然在的前驱节点（同样近的取源码里
    # 最靠后的）在新图里的 id、标题（完整形式），以及它通往被删节点的那条连线怎么走（是 / 否 / case 模式 / 每次 / 循环
    # 结束，普通顺序为空）。往回找不到时退回源码顺序里前面最近的仍然在的节点（after_label 为空）；都没有时为空
    after: str = ""
    after_title: str = ""
    after_label: str = ""


@dataclass
class EdgeChange:
    status: Literal["added", "removed"]
    kind: str
    label: str
    # 两端节点在新图里的 id。removed：两端都是留下的节点（对上的或改动的），这里是它们在新图里的 id（旧图里的 id 不同，
    # 按节点配对对应）。added：一般两端也都是留下的节点；新增的判断 / 循环 / 捕获节点的出边、新增节点的异常连线，起点是
    # 新增节点；留下的判断进新增节点的分支，终点是新增节点
    src: str
    dst: str
    # 两端节点在新图里的类型、标题（完整形式）、行号：给文本按标题写连线用
    src_type: str = ""
    src_title: str = ""
    src_line: int = 0
    dst_type: str = ""
    dst_title: str = ""
    dst_line: int = 0


@dataclass
class FunctionChange:
    """标题、文件：新增、改动的取新图，删除的取旧图。删除的函数不列节点；新增的函数节点全列为 added，连线只列判断 /
    循环 / 捕获节点的出边。"""

    status: Literal["added", "removed", "changed"]
    sid: str
    title: str
    file: str
    new_key: str = ""
    old_key: str = ""
    nodes: list[NodeChange] = field(default_factory=list)  # 按源码顺序（删除的在它原来的位置）
    edges: list[EdgeChange] = field(default_factory=list)  # 先多出的（新图的顺序），再少掉的（旧图的顺序）
    # 只有 changed 的函数会是 True：它所在的文件两次分析时内容一样（指纹相同）。流程却变了，说明不是这个函数的代码改了，
    # 而是引擎的画法变了，或者它调用的别的文件里的函数变了（标题、解析到的目标）
    file_same: bool = False


@dataclass
class LineChange:
    """标题、分组：新增、改动的取新图，删除的取旧图。"""

    status: Literal["added", "removed", "changed"]
    sid: str
    title: str
    group: str
    new_id: str = ""
    functions: list[str] = field(default_factory=list)  # 这条线涉及的、有变化的函数 sid（removed 的线为空）
    tasks_added: list[str] = field(default_factory=list)  # 派发树上多出的任务函数 sid（只有 changed 的线有）
    tasks_removed: list[str] = field(default_factory=list)  # 派发树上少掉的任务函数 sid（只有 changed 的线有）
    root: str = ""  # 根函数的 sid（删除的线取旧图，其余取新图）


@dataclass
class GraphDiff:
    functions: dict[str, FunctionChange] = field(default_factory=dict)  # 函数 sid → 变化，按 sid 排序；只列有变化的
    lines: list[LineChange] = field(default_factory=list)  # 只列有变化的，按（分组，标题）

    @property
    def empty(self) -> bool:
        return not self.functions and not self.lines


# ---------- 节点、连线 ----------


def _base(sid: str) -> str:
    """去掉区分重名的 #n。"""
    return sid.partition("#")[0]


def _ordered(fg: FunctionGraph) -> list[Node]:
    """按（行，生成顺序）排的节点；开始节点最前，执行到函数末尾、未捕获异常退出最后（它们的行是 0）。"""
    def rank(item: tuple[int, Node]) -> tuple[int, int, int]:
        i, n = item
        if n.sid == "start":
            return 0, 0, i
        if n.sid in _IMPLICIT_ENDS:
            return 2, 0, i
        return 1, n.line, i
    return [n for _, n in sorted(enumerate(fg.nodes), key=rank)]


def _change(status: Literal["added", "removed", "changed"], old: Node | None, new: Node | None) -> NodeChange:
    c = NodeChange(status=status, type=(new or old).type)
    if new is not None:
        c.new_id, c.new_sid, c.new_line = new.id, new.sid, new.line
        c.new_title, c.new_subtitle = new.title_full or new.title, new.subtitle_full or new.subtitle
    if old is not None:
        c.old_sid, c.old_line = old.sid, old.line
        c.old_title, c.old_subtitle = old.title_full or old.title, old.subtitle_full or old.subtitle
    return c


def _text(n: Node) -> str:
    """比相似用的文字：标题 + 副标题（完整形式），只取前 TEXT_LIMIT 个字。"""
    return f"{n.title_full or n.title} {n.subtitle_full or n.subtitle}"[:TEXT_LIMIT]


def _similarity(old: list[str], new: list[str], old_types: list[str], new_types: list[str]) -> list[list[bool]]:
    """sim[i][j]：第 i 个旧节点和第 j 个新节点类型相同、文字（old / new）相似度不低于 SIMILAR。"""
    sim = [[False] * len(new) for _ in old]
    for j, b in enumerate(new):
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(b)  # seq2 的预处理只做一次
        for i, a in enumerate(old):
            if old_types[i] != new_types[j]:
                continue
            if a == b:
                sim[i][j] = True
                continue
            sm.set_seq1(a)
            sim[i][j] = sm.real_quick_ratio() >= SIMILAR and sm.quick_ratio() >= SIMILAR and sm.ratio() >= SIMILAR
    return sim


def _pair_block(old: list[Node], new: list[Node]) -> list[tuple[str, Node | None, Node | None]]:
    """对不上的一段：保持顺序、配上最多对相似的节点（最长公共子序列），配上的是改动，其余旧的删除、新的新增。
    一段太大时不配，全算删除和新增：两边节点数之积超过 MAX_BLOCK_PAIRS，或两边文字总长之积超过 MAX_BLOCK_WORK
    （逐对比相似的开销大致和它成正比）。"""
    n, m = len(old), len(new)
    old_texts, new_texts = [_text(o) for o in old], [_text(x) for x in new]
    if n * m > MAX_BLOCK_PAIRS or sum(map(len, old_texts)) * sum(map(len, new_texts)) > MAX_BLOCK_WORK:
        return [("removed", o, None) for o in old] + [("added", None, x) for x in new]
    sim = _similarity(old_texts, new_texts, [o.type for o in old], [x.type for x in new])
    best = [[0] * (m + 1) for _ in range(n + 1)]  # best[i][j]：old[i:] 和 new[j:] 最多配上几对
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1], best[i + 1][j + 1] + 1 if sim[i][j] else 0)
    out: list[tuple[str, Node | None, Node | None]] = []
    i = j = 0
    while i < n or j < m:
        if i < n and j < m and sim[i][j] and best[i][j] == best[i + 1][j + 1] + 1:
            out.append(("changed", old[i], new[j]))
            i, j = i + 1, j + 1
        elif j == m or (i < n and best[i + 1][j] >= best[i][j + 1]):
            out.append(("removed", old[i], None))
            i += 1
        else:
            out.append(("added", None, new[j]))
            j += 1
    return out


def _align(old: list[Node], new: list[Node]) -> list[tuple[str, Node | None, Node | None]]:
    """按基础 sid 对齐两个节点序列：(same | changed | removed | added, 旧节点, 新节点)，按源码顺序。"""
    a, b = [_base(n.sid) for n in old], [_base(n.sid) for n in new]
    ops = [("equal", 0, len(a), 0, len(b))] if a == b else \
        difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: list[tuple[str, Node | None, Node | None]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(("same", o, n) for o, n in zip(old[i1:i2], new[j1:j2], strict=True))
        else:  # replace；delete / insert 只有一边
            out += _pair_block(old[i1:i2], new[j1:j2])
    return out


EdgeKey = tuple[str, str, str, str]  # （起点，终点，kind，label），端点是新图里的 id


class _Links:
    """一张函数图的连线，按起点、终点各建一份索引。"""

    def __init__(self, fg: FunctionGraph) -> None:
        self.out: dict[str, list[Edge]] = {}
        self.into: dict[str, list[Edge]] = {}
        for e in fg.edges:
            self.out.setdefault(e.src, []).append(e)
            self.into.setdefault(e.dst, []).append(e)

    def reaches(self, start: str, goal: str, through: set[str]) -> bool:
        """从 start 出发、中间只经过 through 里的节点（可以一个都不经过），走得到 goal。"""
        stack, seen = [start], {start}
        while stack:
            for e in self.out.get(stack.pop(), ()):
                if e.dst == goal:
                    return True
                if e.dst in through and e.dst not in seen:
                    seen.add(e.dst)
                    stack.append(e.dst)
        return False

    def explains(self, edge: Edge, src: str, dst: str, src_is_cond: bool, through: set[str]) -> bool:
        """另一张图里的连线 edge（两端在这张图里是 src、dst）在这张图里没有了，能不能由这张图里新增（或删除）的节点
        through 说明：
        1. 从 src 出发、第一步就进 through（src 是判断节点时，这一步的分支要和 edge 相同；edge 是异常连线时，这一步
           也要是异常连线），之后只经 through 里的节点，走得到 dst——在两个留下的节点之间插进 / 删掉了节点；
        2. 这张图里有一条从 through 里的节点 P 到 dst 的连线，kind、label 和 edge 相同，P 只经 through 里的节点走得到
           src——一段的入口换了，例如 try 体第一句前面插了一句，异常连线改从新的第一句出发。"""
        for e in self.out.get(src, ()):
            if e.dst in through and (not src_is_cond or e.branch == edge.branch) \
                    and (edge.kind != "raise" or e.kind == "raise") and self.reaches(e.dst, dst, through):
                return True
        return any(e.src in through and (e.kind, e.label) == (edge.kind, edge.label) and self.reaches(e.src, src, through)
                   for e in self.into.get(dst, ()))


def _edge_change(status: Literal["added", "removed"], key: EdgeKey, nodes: dict[str, Node]) -> EdgeChange:
    src, dst, kind, label = key
    s, t = nodes[src], nodes[dst]
    return EdgeChange(status, kind, label, src, dst, s.type, s.title_full or s.title, s.line,
                      t.type, t.title_full or t.title, t.line)


def _paired_edges(fg: FunctionGraph, to_new: dict[str, str]) -> dict[EdgeKey, Edge]:
    """两端都配上对的连线，键的端点换成新图里的 id；按连线顺序。"""
    out: dict[EdgeKey, Edge] = {}
    for e in fg.edges:
        src, dst = to_new.get(e.src), to_new.get(e.dst)
        if src is not None and dst is not None:
            out.setdefault((src, dst, e.kind, e.label), e)
    return out


def _edges_with_added_end(fg: FunctionGraph, added: set[str], nodes: dict[str, Node]) -> dict[EdgeKey, EdgeChange]:
    """一端是新增节点、照样报成新增连线的（让人看到新的分支、新的异常落点通向哪里）：
    - 新增的判断 / 循环 / 捕获节点的出边；
    - 新增节点的异常连线（新的 try 体入口）；
    - 留下的判断节点进新增节点的分支（新的 case、新的 else，或分支开头插进的节点）。"""
    out: dict[EdgeKey, EdgeChange] = {}
    for e in fg.edges:
        src_added, dst_added = e.src in added, e.dst in added
        if (src_added and (nodes[e.src].type in _BRANCHING or e.kind == "raise")) \
                or (not src_added and dst_added and nodes[e.src].type == "cond" and e.branch):
            key = (e.src, e.dst, e.kind, e.label)
            out[key] = _edge_change("added", key, nodes)
    return out


def _after_word(e: Edge) -> str:
    """连线怎么走，给删除的节点指位置用：判断的分支是 是 / 否 / case 模式（都不匹配），循环进体是"每次"、正常结束是
    "循环结束"，其余（顺序等）为空。"""
    if e.branch == "yes":
        return "是"
    if e.branch == "no":
        return "否"
    if e.branch:
        return e.label
    return {"loop_each": "每次", "loop_exit": "循环结束"}.get(e.kind, "")


def _after(olds: list[Node], to_new: dict[str, str], links: _Links) -> dict[str, tuple[str, str]]:
    """每个删除的旧节点 -> (挂在哪个仍然在的节点之后：它的新 id, 通往它的那条连线怎么走)。
    按旧图的控制流往回找：沿入边广度优先（不看 loop_back、raise），只穿过删除的节点，到最近一层仍然在的前驱；同一层
    有几个时取源码里最靠后的。一个都找不到（比如只有异常连线进来的捕获节点）时退回源码顺序里前面最近的仍然在的节点。"""
    position = {n.id: i for i, n in enumerate(olds)}
    out: dict[str, tuple[str, str]] = {}
    last = ""  # 源码顺序里前面最近的仍然在的节点（新 id）
    for node in olds:
        if node.id in to_new:
            last = to_new[node.id]
            continue
        frontier, seen = [node.id], {node.id}
        found: dict[str, Edge] = {}  # 仍然在的前驱（旧 id）-> 从它出发、通往被删节点的连线
        while frontier and not found:
            step: list[str] = []
            for x in frontier:
                for e in links.into.get(x, ()):
                    if e.kind in ("loop_back", "raise"):
                        continue
                    if e.src in to_new:
                        found.setdefault(e.src, e)
                    elif e.src not in seen:
                        seen.add(e.src)
                        step.append(e.src)
            frontier = step
        if found:
            best = max(found, key=position.__getitem__)
            out[node.id] = (to_new[best], _after_word(found[best]))
        else:
            out[node.id] = (last, "")
    return out


def _diff_function(old: FunctionGraph, new: FunctionGraph) -> tuple[list[NodeChange], list[EdgeChange]]:
    olds = _ordered(old)
    pairs = _align(olds, _ordered(new))
    to_new = {o.id: n.id for status, o, n in pairs if status in ("same", "changed")}
    to_old = {n_id: o_id for o_id, n_id in to_new.items()}
    added_ids = {n.id for status, _, n in pairs if status == "added"}
    removed_ids = {o.id for status, o, _ in pairs if status == "removed"}
    by_id = {n.id: n for n in new.nodes}
    old_links, new_links = _Links(old), _Links(new)
    after = _after(olds, to_new, old_links)
    nodes = []
    for status, o, n in pairs:
        if status == "same":
            continue
        c = _change(status, o, n)
        if status == "removed" and after[o.id][0]:
            c.after, c.after_label = after[o.id]
            c.after_title = by_id[c.after].title_full or by_id[c.after].title
        nodes.append(c)
    old_edges, new_edges = _paired_edges(old, to_new), _paired_edges(new, {i: i for i in to_new.values()})
    changes = _edges_with_added_end(new, added_ids, by_id)
    for key, e in new_edges.items():
        if key not in old_edges and not old_links.explains(
                e, to_old[key[0]], to_old[key[1]], by_id[key[0]].type == "cond", removed_ids):
            changes[key] = _edge_change("added", key, by_id)
    order = {(e.src, e.dst, e.kind, e.label): i for i, e in enumerate(new.edges)}
    edges = sorted(changes.values(), key=lambda c: order[(c.src, c.dst, c.kind, c.label)])
    edges += [_edge_change("removed", key, by_id) for key, e in old_edges.items()
              if key not in new_edges and not new_links.explains(e, key[0], key[1], by_id[key[0]].type == "cond",
                                                                 added_ids)]
    return nodes, edges


# ---------- 函数、功能线 ----------


def _sids(graph: ProjectGraph, keys: list[str]) -> list[str]:
    return list(dict.fromkeys(graph.functions[k].sid for k in keys if k in graph.functions))


def _tasks(root: LineTask) -> list[str]:
    """派发树上的任务 key，先序，不重复。"""
    out: dict[str, None] = {}
    stack = [root]
    while stack:
        t = stack.pop()
        out[t.function] = None
        stack.extend(reversed(t.children))
    return list(out)


def _diff_functions(old: ProjectGraph, new: ProjectGraph) -> dict[str, FunctionChange]:
    out: dict[str, FunctionChange] = {}
    for sid in sorted(old.sids.keys() | new.sids.keys()):
        o, n = old.functions.get(old.sids.get(sid, "")), new.functions.get(new.sids.get(sid, ""))
        if n is not None and o is None:
            by_id = {x.id: x for x in n.nodes}
            out[sid] = FunctionChange("added", sid, n.title, n.file, new_key=n.key,
                                      nodes=[_change("added", None, x) for x in _ordered(n)],
                                      edges=list(_edges_with_added_end(n, set(by_id), by_id).values()))
        elif o is not None and n is None:
            out[sid] = FunctionChange("removed", sid, o.title, o.file, old_key=o.key)
        elif o is not None and n is not None:
            nodes, edges = _diff_function(o, n)
            if nodes or edges:
                digest = new.file_hashes.get(n.file)
                same = o.file == n.file and digest is not None and old.file_hashes.get(o.file) == digest
                out[sid] = FunctionChange("changed", sid, n.title, n.file, n.key, o.key, nodes, edges, file_same=same)
    return out


def _root_sid(graph: ProjectGraph, line: FlowLine) -> str:
    fg = graph.functions.get(line.root.function)
    return fg.sid if fg is not None else ""


def _diff_lines(old: ProjectGraph, new: ProjectGraph, changed: dict[str, FunctionChange]) -> list[LineChange]:
    olds, news = {ln.sid: ln for ln in old.lines}, {ln.sid: ln for ln in new.lines}
    out: list[LineChange] = []
    for sid in olds.keys() | news.keys():
        o, n = olds.get(sid), news.get(sid)
        if n is None:
            out.append(LineChange("removed", sid, o.title, o.group, root=_root_sid(old, o)))
            continue
        involved = dict.fromkeys(_sids(new, line_functions(new, n)))
        if o is not None:
            involved.update(dict.fromkeys(_sids(old, line_functions(old, o))))
        functions = [s for s in involved if s in changed]
        if o is None:
            out.append(LineChange("added", sid, n.title, n.group, n.id, functions, root=_root_sid(new, n)))
            continue
        new_tasks, old_tasks = _sids(new, _tasks(n.root)), _sids(old, _tasks(o.root))
        new_set, old_set = set(new_tasks), set(old_tasks)
        added = [s for s in new_tasks if s not in old_set]
        removed = [s for s in old_tasks if s not in new_set]
        if functions or added or removed:
            out.append(LineChange("changed", sid, n.title, n.group, n.id, functions, added, removed,
                                  _root_sid(new, n)))
    out.sort(key=lambda c: (c.group, c.title, c.sid))
    return out


def diff_graphs(old: ProjectGraph, new: ProjectGraph) -> GraphDiff:
    """old 到 new 流程变了什么。两张图要分析的是同一个目录（函数 sid 里的路径相对分析的目录）。"""
    functions = _diff_functions(old, new)
    return GraphDiff(functions=functions, lines=_diff_lines(old, new, functions))


# ---------- JSON ----------


def diff_to_json(d: GraphDiff) -> dict:
    """dataclass 原样转字典，空字段照留；顶层另带 "version"。"""
    return {"version": DIFF_VERSION, **asdict(d)}


class DiffFormatError(ValueError):
    """存下来的流程差异格式不对（缺字段、类型不对）。信息是中文。"""


def _fits(value: object, tp: object) -> bool:
    """value 是不是 tp（字段的类型注解）要的值：Literal 的取值、str、int（不含 bool）、list（list[str] 逐项看）。"""
    origin = get_origin(tp)
    if origin is Literal:
        return value in get_args(tp)
    if origin is list:
        return isinstance(value, list) and (get_args(tp)[0] is not str or all(isinstance(x, str) for x in value))
    if tp is bool:
        return type(value) is bool
    if tp is int:
        return type(value) is int
    if tp is str:
        return isinstance(value, str)
    return True


def _load(cls, data: object, where: str):
    """按 dataclass cls 的字段读一个 JSON 对象：不认识的字段忽略；缺了没有默认值的字段、类型不对时抛 DiffFormatError。"""
    if not isinstance(data, dict):
        raise DiffFormatError(f"流程差异的格式不对：{where} 应该是对象")
    hints = get_type_hints(cls)
    values = {}
    for f in fields(cls):
        if f.name not in data:
            if f.default is MISSING and f.default_factory is MISSING:
                raise DiffFormatError(f"流程差异的格式不对：{where} 缺少 {f.name}")
            continue
        value = data[f.name]
        if not _fits(value, hints[f.name]):
            shown = json.dumps(value, ensure_ascii=False)[:80]
            raise DiffFormatError(f"流程差异的格式不对：{where}.{f.name} 的值不对：{shown}")
        values[f.name] = value
    return cls(**values)


def diff_from_json(data: dict) -> GraphDiff:
    """读回 diff_to_json 的结果。不认识的字段忽略，缺的可选字段取默认值；不是对象、版本不对、缺字段、类型不对时抛
    ValueError（中文说明；格式不对时是它的子类 DiffFormatError）。"""
    version = data.get("version") if isinstance(data, dict) else None
    if version != DIFF_VERSION:
        raise ValueError(f"不是版本 {DIFF_VERSION} 的流程差异（顶层不是对象，或 version 是 {version!r}）")
    functions, lines = data.get("functions"), data.get("lines")
    if not isinstance(functions, dict) or not isinstance(lines, list):
        raise DiffFormatError("流程差异的格式不对：functions 应该是对象，lines 应该是列表")

    def function(sid: str, f: object) -> FunctionChange:
        fc = _load(FunctionChange, f, f"functions[{sid}]")
        fc.nodes = [_load(NodeChange, c, f"functions[{sid}].nodes[{i}]") for i, c in enumerate(fc.nodes)]
        fc.edges = [_load(EdgeChange, e, f"functions[{sid}].edges[{i}]") for i, e in enumerate(fc.edges)]
        return fc

    return GraphDiff(
        functions={sid: function(sid, f) for sid, f in functions.items()},
        lines=[_load(LineChange, ln, f"lines[{i}]") for i, ln in enumerate(lines)],
    )


# ---------- 给 AI 看的文本 ----------

_STATUS = {"added": "新增", "removed": "删除", "changed": "改动"}
_TYPE = {"start": "开始", "step": "步骤", "cond": "判断", "loop": "循环", "catch": "捕获", "end": "结局"}
_KIND = {"next": "顺序", "yes": "是", "no": "否", "case": "分支", "loop_each": "进入循环体", "loop_back": "回到循环",
         "loop_exit": "循环结束", "break": "跳出循环", "raise": "异常", "finally": "进入 finally"}
_IMPLICIT = ("start", "end:uncaught")  # 每个函数都有的隐含节点，文本里不列
MAX_TEXT_LINES = 30  # 文本里完整列出的有变化的功能线
MAX_TEXT_ITEMS = 40  # 每个函数完整列出的节点 + 连线
MAX_TEXT_CHARS = 20_000  # 整段文本的硬上限（含最后那句提示）


def _more(count: int, unit: str) -> str:
    """超出上限时的一行提示；unit 和数的东西一致：条功能线、处（节点 + 连线）、行（整段截断）。"""
    return f"…还有 {count} {unit}未列出（用 flow 参数只看一条功能线）"


def _label(title: str, subtitle: str) -> str:
    return f"「{title}」" + (f"（{subtitle}）" if subtitle and subtitle != title else "")


def _loc(file: str, line: int) -> str:
    return f"{file}:{line}" if line else file


def _node_text(c: NodeChange, file: str) -> str:
    kind = _TYPE.get(c.type, c.type)
    if c.status == "added":
        return f"{kind}{_label(c.new_title, c.new_subtitle)}  {_loc(file, c.new_line)}"
    if c.status == "removed":
        where = f"（原来在「{c.after_title}」之后{'，' + c.after_label if c.after_label else ''}）" if c.after_title else ""
        return f"{kind}{_label(c.old_title, c.old_subtitle)}  {_loc(file, c.old_line)}{where}"
    if (c.old_title, c.old_subtitle) == (c.new_title, c.new_subtitle):
        return f"{kind}{_label(c.new_title, c.new_subtitle)}（标题没变，语句变了）  " \
               f"{_loc(file, c.old_line)} → {_loc(file, c.new_line)}"
    return f"{kind}{_label(c.old_title, c.old_subtitle)} → {_label(c.new_title, c.new_subtitle)}  " \
           f"{_loc(file, c.new_line)}"


def _end(type_: str, title: str, line: int) -> str:
    """连线一端：类型「标题」L行（隐含节点没有行号）。"""
    return f"{_TYPE.get(type_, type_)}「{title}」" + (f"L{line}" if line else "")


def _word(e: EdgeChange) -> str:
    """连线怎么走：是 / 否、case 的模式、顺序、回到循环 是……"""
    if e.kind == "case":
        return e.label
    word = _KIND.get(e.kind, e.kind)
    return f"{word} {e.label}" if e.label else word


def _edge_text(e: EdgeChange) -> str:
    return f"连线{'新增' if e.status == 'added' else '删除'}：{_end(e.src_type, e.src_title, e.src_line)} " \
           f"—{_word(e)}→ {_end(e.dst_type, e.dst_title, e.dst_line)}"


def engine_note(d: GraphDiff, engines: tuple[str, str] | None) -> str:
    """两次分析的引擎标识（基准那次, 最新一次；见 engineid）不一样时的一句提醒：有变化的函数里几个所在的文件没改——它们
    的变化不是代码改动引起的。引擎没变、没有函数级的变化时空串。"""
    if engines is None or engines[0] == engines[1]:
        return ""
    changed = [fc for fc in d.functions.values() if fc.status == "changed"]
    same = sum(1 for fc in changed if fc.file_same)
    head = f"⚠ 两次分析之间引擎也变了（{engines[0]} → {engines[1]}）"
    if not changed:
        return head + "：新增、删除的函数是代码改动，其余变化可能有一部分是引擎的画法变了引起的。"
    if same == 0:
        return head + f"：流程有改动的 {len(changed)} 个函数所在的文件也都改过，分不出哪些变化是引擎引起的，对照代码的改动看。"
    which = "所在的文件内容都没变" if same == len(changed) else f"里 {same} 个所在的文件内容没变（下面标着「文件没改」）"
    return (head + f"：流程有改动的 {len(changed)} 个函数{which}——这些不是代码改动，是引擎的画法变了引起的"
                   "（少数情况是它调用的别的文件里的函数改了）。不要把它们当成这次改代码造成的流程变化。")


MAX_BRIEF_OUTCOMES = 6  # 只写个数的那一行里最多写出几个结局的标题
MAX_BRIEF_NAMES = 12  # "另有新增的函数"那一行里最多列几个标题


def _counts(fc: FunctionChange) -> str:
    """一个函数的变化缩成一句。新增的函数：各类节点的个数和结局的标题；改动的函数：节点新增 / 删除 / 改动各几个、
    连线变了几条。"""
    nodes = [c for c in fc.nodes if (c.new_sid or c.old_sid) not in _IMPLICIT]
    if fc.status == "added":
        parts = [f"{_TYPE[t]} {n}" for t in ("step", "cond", "loop", "catch", "end")
                 if (n := sum(1 for c in nodes if c.type == t))]
        ends = [c.new_title for c in nodes if c.type == "end"]
        more = f" 等 {len(ends)} 个" if len(ends) > MAX_BRIEF_OUTCOMES else ""
        return "、".join(parts) + ("（" + "".join(f"「{t}」" for t in ends[:MAX_BRIEF_OUTCOMES]) + more + "）" if ends else "")
    parts = [f"节点{_STATUS[st]} {n}" for st in ("added", "removed", "changed") if (n := sum(1 for c in nodes if c.status == st))]
    return "、".join(parts + ([f"连线变化 {len(fc.edges)}"] if fc.edges else []))


def _function_text(fc: FunctionChange, tag: bool = False, brief: bool = False) -> list[str]:
    """一个函数的变化：节点按 新增 / 删除 / 改动 分组（新增的判断 / 循环 / 捕获节点下面列它的出边），其余连线在
    "连线"下面；节点 + 连线最多列 MAX_TEXT_ITEMS 处。brief：只写一行，节点、连线不逐个列（见 _counts）。"""
    key = fc.new_key or fc.old_key
    where = _loc(fc.file, parse_key(key)[2]) if key else fc.file
    if fc.status == "removed":
        return [f"### 函数 [删除] {fc.title}  {fc.sid}（原在 {where}）"]
    out = [f"### 函数 [{_STATUS[fc.status]}] {fc.title}  {fc.sid}  {where}{'  〔文件没改：引擎变化引起〕' if tag and fc.file_same else ''}"]
    if brief:
        return [out[0] + (f"  ——{counts}" if (counts := _counts(fc)) else "")]
    added = {c.new_id for c in fc.nodes if c.status == "added"}
    under: dict[str, list[EdgeChange]] = {}  # 新增节点的 id -> 它的出边
    others: list[EdgeChange] = []
    for e in fc.edges:
        (under.setdefault(e.src, []) if e.status == "added" and e.src in added else others).append(e)
    items: list[tuple[str, str]] = []  # (小标题, 行)
    for status in ("added", "removed", "changed"):
        for c in fc.nodes:
            if c.status == status and (c.new_sid or c.old_sid) not in _IMPLICIT:
                items.append((_STATUS[status], f"  - {_node_text(c, fc.file)}"))
                items += [(_STATUS[status], f"      {_word(e)} → {_end(e.dst_type, e.dst_title, e.dst_line)}")
                          for e in (under.get(c.new_id, ()) if status == "added" else ())]
    items += [("连线", f"  - {_edge_text(e)}") for e in others]
    heading = ""
    for group, row in items[:MAX_TEXT_ITEMS]:
        if group != heading:
            out.append(group)
            heading = group
        out.append(row)
    if len(items) > MAX_TEXT_ITEMS:
        out.append(_more(len(items) - MAX_TEXT_ITEMS, "处"))
    return out


def _capped(rows: list[str]) -> str:
    """整段不超过 MAX_TEXT_CHARS：超出时在行边界截断，最后一行说还有多少行（不算空行）没列出。"""
    text = "\n".join(rows)
    if len(text) <= MAX_TEXT_CHARS:
        return text
    room = MAX_TEXT_CHARS - len(_more(len(rows), "行")) - 1
    kept: list[str] = []
    size = -1  # 第一行前面没有换行符
    for row in rows:
        if size + 1 + len(row) > room:
            break
        size += 1 + len(row)
        kept.append(row)
    return "\n".join([*kept, _more(sum(1 for row in rows[len(kept):] if row.strip()), "行")])


def _line_functions(ln: LineChange, functions: dict[str, FunctionChange]) -> list[str]:
    """文本里列在这条线下面的函数 sid：有变化的线是它涉及的有变化的函数；删除的线是它的根函数（有变化时）。"""
    if ln.status != "removed":
        return ln.functions
    return [ln.root] if ln.root in functions else []


def diff_text(d: GraphDiff, *, line_sid: str | None = None, engines: tuple[str, str] | None = None,
              brief: Literal["", "added", "all"] = "") -> str:
    """给 AI 看的中文文本：按有变化的功能线、再按函数分组，每个节点带文件:行号（新增、改动的是本次分析的行号，删除的
    是上一次的），连线写两端节点的标题和行号。删除的线下面列它的根函数。line_sid 给出时只说这条线。没有变化时是一句话。
    上限：完整列出前 MAX_TEXT_LINES 条线、每个函数前 MAX_TEXT_ITEMS 处（节点 + 连线），整段不超过 MAX_TEXT_CHARS 字；
    超出的部分用一行"…还有 N 条功能线 / 处 / 行未列出"带过。同一个函数在几条线里出现时只详细列一次，之后写"同上：函数
    sid"。engines：（基准那次, 最新一次）的引擎标识，不一样时开头加一句提醒（engine_note），所在文件没改的函数标出来。
    brief：哪些函数只写一行、不逐个列节点和连线（见 _counts）——"added" 是新增的函数（刚写出来的，写的人知道里面有什么），
    "all" 是全部；默认都逐个列。brief 时新增的函数里只有功能线的根函数单独一行，别的并成一行标题。"""
    note = engine_note(d, engines)
    tag = bool(note)

    folded: set[str] = set()  # 已经并进"另有新增的函数"那一行的函数 sid

    def rows(fcs: list[FunctionChange], root: str = "") -> list[str]:
        """几个函数的变化。brief 时，新增的函数里只有功能线的根函数（root）单独写一行，其余的（它用到的步骤、判断函数）
        并成一行只列标题；同一个函数在几条线里出现时只详细列一次，之后写"同上"（并进一行的不再重复）。"""
        got: list[str] = []
        names: list[str] = []
        for fc in fcs:
            if brief and fc.status == "added" and fc.sid != root:
                if fc.sid not in folded:
                    folded.add(fc.sid)
                    names.append(fc.title)
            elif fc.sid in shown:
                got.append(f"### 同上：{fc.sid}")
            else:
                shown.add(fc.sid)
                got += _function_text(fc, tag, brief == "all" or (brief == "added" and fc.status == "added"))
        if names:
            more = f" 等 {len(names)} 个" if len(names) > MAX_BRIEF_NAMES else ""
            got.append(f"另有新增的函数 {len(names)} 个（步骤、判断函数等）：{'、'.join(names[:MAX_BRIEF_NAMES])}{more}")
        return got
    if line_sid is not None:
        lines = [ln for ln in d.lines if ln.sid == line_sid]
        if not lines:
            return f"功能线 {line_sid} 的流程没有变化"
        out = []
    else:
        if d.empty:
            return "流程没有变化"
        lines = d.lines
        out = [f"流程有变化：{len(d.lines)} 条功能线、{len(d.functions)} 个函数。"]
    if not brief or any(fc.status != "added" for fc in d.functions.values()) and brief != "all":
        out.append("新增、改动的节点给本次分析的行号，删除的给拿来比的那次（较早的）分析的行号；连线两端是本次分析的节点。")
    if note:
        out.append(note)
    shown: set[str] = set()
    for ln in lines[:MAX_TEXT_LINES]:
        out += ["", f"## 功能线 [{_STATUS[ln.status]}] {ln.title}（{ln.group}）  {ln.sid}"]
        if ln.tasks_added:
            out.append("派发的任务多了：" + "、".join(ln.tasks_added))
        if ln.tasks_removed:
            out.append("派发的任务少了：" + "、".join(ln.tasks_removed))
        out += rows([d.functions[sid] for sid in _line_functions(ln, d.functions) if sid in d.functions], ln.root)
    if len(lines) > MAX_TEXT_LINES:
        out += ["", _more(len(lines) - MAX_TEXT_LINES, "条功能线")]
    on_lines = {sid for ln in d.lines for sid in _line_functions(ln, d.functions)}
    rest = [fc for sid, fc in d.functions.items() if sid not in on_lines] if line_sid is None else []
    if rest:
        out += ["", "## 其他有变化的函数（不在任何有变化的功能线上）"]
        out += rows(rest)
    return _capped(out)
