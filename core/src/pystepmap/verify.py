"""执行路径核对：把 pytest 插件（trace.py）记下的真实执行，逐步放到静态图上走一遍，报告现实走了、图上却没有的每一步。

一条记录的核对分三步：
1. 行 → 节点行：执行到的每一行映射到它所属语句的起始行（多行语句只在后面的行上产生事件），只保留图上有节点的行，
   连续重复的合并。CPython 有几类行事件并不表示"执行了这一句"，映射前先去掉：
   - with 语句在离开时（调用 __exit__）回到头部的行：头部行事件紧跟在 with 体里的行之后，就是离开；
   - 没匹配上的 except / case 子句头部：头部之后的下一个事件不在它的子句体里，说明没进入这个子句。
     子句体和头部写在同一行时看不出来，这一行记为"可以不经过"。
   进入了的只写日志的 except（不画捕获节点）在序列里留一个标记（负的头部行号），作为异常被吞掉的证据。
   写在一行的 except 的头部行不论是否匹配都会出现，是否吞掉了异常要看后面：之后（跳过这一行、with 头部和异常
   往外传时经过的 finally 体）的第一行是 except 头部时没有吞掉（不匹配，或子句体又抛出），不留标记；否则留一个
   标记（画了捕获节点的也留，挂在这一行之后）。
   没有节点的行落在某个 try 语句的 try 体里时也留一个标记（负的 try 起始行号，两个节点行之间每个 try 至多一个），
   作为执行不经过节点就进入了这个 try 体的证据。
2. 走图：从 start 出发，每到一行依次经过该行的节点（按节点编号顺序）。一行有多个节点（如 if x: return y、
   for 写成一行）时，事件分不出走了其中哪几个，所以每行可以只走一个前缀，"可以不经过"的行还可以整行不走；
   在所有走法里取不符最少、其次近似最少的一种（先列长的前缀，同样好时取先列出的）。
   每一步前一个节点 A 到当前节点 B 要有连线。没有连线时只看这一步自己的起点和终点（没有跨步的状态）：
   - 异常与收尾入口（近似）：有一个 try 语句 T，A 在 T 的 try 体里且 B 是 T 的捕获节点、T 的 finally 体的入口，
     或（T 有只写日志、不画捕获节点的 except，或 T 只有 finally 时）T 之后的第一个节点；或者 A 在 T 的某个
     except 体或 else 体里且 B 是 T 的 finally 体的入口；并且 B 有 raise / finally 类型的入边。落到只写日志的
     except 之后（异常被吞掉）还要有证据：原始行号里 A 之后、B 之前出现了那个 except 的头部行。
     A 不在 T 的 try 体里时（try 体开头是不产生节点的语句，异常从那里抛出，A 还停在 try 之前），B 的条件同上，
     另外要有证据：原始行号里 A 之后、B 之前出现了 T 的 try 体里的行（T 自己的 try 行、except 头部和 except 体、
     else 体、finally 体里的行都不算）。
     （异常画法近似：raise 连线只从 try 体的入口画出。except / else 体里抛出的异常 Python 不会交给同一个 try 的
     其他 except，所以从它们出发只认 finally。内层 try 没接住、往外层传的异常，A 也在外层 try 体里，或者
     这一步之间出现了外层 try 体里的行。）
   - 其余都是不符。try / finally 里的 break / continue 建图时如实画成"跳转处 → finally 体 → 跳转目标"，
     return 画成"结局 → finally 体"，离开 finally 体的这些路径都有连线，不需要近似。范围都按语法树里的行号算。
   沿连线到达结局（return / raise 语句）算在结局里，不计步数；近似和不符都计步数，并逐处记下位置。
3. 结局：以 return 离开时，最后的节点要是结局、或者有连线到"执行到函数末尾"、或者经过 return 语句的结局 R 之后
   只走了 finally 里的节点。其间经过 R 正在经过的 finally 体 F 里的 raise 语句的结局时，要有那个异常在 F 里被
   吞掉的证据，R 才继续：之后经过的节点都还在 F 里，直到经过 F 里写成多行的 except 的捕获节点，或者原始行号里
   出现 F 里只写日志的 except、写在一行的 except 吞掉了异常的标记（见第 1 步；结局：最后的节点之后出现也算）。
   写在一行的 except 的头部行和捕获节点本身不算证据。没有这样的证据就离开了 F、或者到最后也没有时，异常逃出
   了 F，R 已被取消，不算。只经过 raise 语句的结局也不算：那个异常被吞掉了，函数是正常返回的。否则是结局不符。
   以 raise 离开时，停在 raise 语句的结局上（之后只走 finally 也算）是正常的，否则计为未捕获异常退出（图上有
   隐含的未捕获异常结局，不算不符）。
   吞掉的异常落到"执行到函数末尾"也算合法结局，记一次入口近似（终点行记为 0）：T 有只写日志的 except、吞掉的
   异常落到函数末尾，最后的节点在 T 的 try 体里、或者它之后的原始行号里出现了 T 的 try 体里的行，并且它之后
   出现了那个 except 的头部行。
   截断的记录不核对结局；
   一个节点行都没有的记录只看结局：起点能走到某个结局就算合法。
调用：记录里的每个"调用方在第 L 行 → 被调函数"依次判断：
1. 在调用方的 call_targets 里，或被调函数是调用方的内部函数 → 在图上；
2. 被调函数是属性访问器（@property、@cached_property、@x.setter / getter / deleter）或 __init__、__call__
   以外的魔术方法 → 隐式调用（Python 替代码调用的，源码里没有调用语法），单独列出，不算不符；
3. 第 L 行所属的语句把被调函数的名字作为参数（不是调用）传了出去，如 sorted(xs, key=rank)、map(helper, xs)
   → 动态调用（强匹配，回调）；
4. 第 L 行所属的语句里有无法判断目标的调用（图的 unresolved 里、行号映射到同一条语句的条目：回调参数、
   存在属性里的函数、没有类型的接收者）→ 动态调用，单独列出，不算不符；其中某个调用的名字最后一段正是
   被调函数的名字时为强匹配，否则为弱匹配。同一函数别的语句里有无法判断目标的调用不算数；
5. 否则是不符。
记录少记调用（经生成器表达式、lambda、库包装的调用不在 calls 里——它们是 icalls，核对不看），所以从不因为"没调用"
报告任何东西。
所有计数都乘以记录的 n。
记录文件的格式见 trace.py 的模块说明。核对只看执行记录（有 fn 的行）里的 fn、lines、exit、calls、n、truncated：
耗时 ns、测试结果行、以后加的字段和没有 fn 的行都不影响核对的结论。
walk_record 是第 1、2、3 步对一条执行记录的结果（依次经过的节点、停在哪个节点），verify 和运行路径（runpath）共用它。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .model import FunctionGraph, ProjectGraph

MismatchKind = Literal["edge", "end", "call"]
_KIND_ORDER = {"edge": 0, "end": 1, "call": 2}
_LANDING = frozenset({"raise", "finally"})  # 异常和收尾落脚的连线类型
_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef
Span = tuple[int, int]  # 行范围（含两端）


@dataclass
class Mismatch:
    kind: MismatchKind
    fn: str
    from_line: int  # edge：前一个节点的行；end：最后所在节点的行；call：调用发生时调用方所在的行
    to_line: int  # edge：当前节点的行；end：0；call：被调函数的 def 行
    detail: str
    count: int


@dataclass
class Approximation:
    fn: str
    from_line: int  # 前一个节点的行
    to_line: int  # 当前节点的行；0 表示"执行到函数末尾"
    kind: Literal["entry"]  # 异常与收尾入口
    count: int


@dataclass
class ImplicitCall:
    caller: str
    callee: str
    reason: Literal["property", "dunder"]  # 属性访问器 / 魔术方法
    count: int


@dataclass
class DynamicCall:
    caller: str
    callee: str
    line: int  # 调用发生时调用方所在的行
    strong: bool  # 被调函数的名字正是该语句里某个无法判断目标的调用的名字（最后一段），或被作为参数传出（回调）
    names: list[str]  # 该语句里无法判断目标的调用的显示名；回调时是作为参数传出的名字
    callback: bool  # 该语句把被调函数的名字作为参数传了出去
    count: int


@dataclass
class VerifyReport:
    functions_in_graph: int
    functions_seen: int
    distinct_paths: int  # 不同的 (函数, 节点行序列, 离开方式)
    steps_total: int
    steps_on_graph: int
    approx_entries: int  # 异常与收尾入口（近似画法，不算错）
    uncaught_exits: int  # 以未捕获异常退出（对应图上的隐含结局）
    calls_total: int
    implicit_calls: int  # 属性访问器、魔术方法（源码里没有调用语法）
    dynamic_calls: int  # 调用方有无法判断目标的调用
    truncated_records: int
    unknown_functions: int  # 记录里出现、图里没有的函数（个数）
    plugin_errors: int
    mismatches: list[Mismatch] = field(default_factory=list)  # 按次数从多到少
    approx_list: list[Approximation] = field(default_factory=list)  # 按次数从多到少
    implicit_call_list: list[ImplicitCall] = field(default_factory=list)  # 按次数从多到少
    dynamic_call_list: list[DynamicCall] = field(default_factory=list)  # 按次数从多到少
    plugin_error_list: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


class StaleGraphError(Exception):
    """图分析之后源码改过或读不到了：行号对不上，核对没有意义。"""

    def __init__(self, changed: list[str], unreadable: list[str]):
        super().__init__("图已过期：" + "、".join(changed + unreadable))
        self.changed = changed  # 内容变了
        self.unreadable = unreadable  # 读不到（删除、移动、没有权限）
        self.files = sorted(changed + unreadable)


class RecordError(ValueError):
    """记录文件里有一行读不了。"""


TRACE_FORMAT = "pystepmap-trace"
TRACE_VERSION = 1  # 能读的记录格式的最高版本
TEST_OUTCOMES = frozenset({"passed", "failed", "skipped", "error"})  # 测试结果行的 outcome


@dataclass(frozen=True)
class Walked:
    """一条执行记录在图上走出来的样子（见模块说明第 1–3 步，和 verify 用的是同一个走法）。"""

    nodes: tuple[str, ...]  # 依次经过的节点 id：n1（开始）起，循环里重复出现；以"执行到函数末尾"结束时最后是它
    # 每个节点从 lines 里的第几项开始（开始节点为 0；执行到函数末尾为 len(lines)）。同一行上的几个节点（return f()、
    # if c: return 0）从同一个行事件开始，位置相同；这一段的时间归谁由使用者定（runpath 归给最后一个不是结局的）
    line_index: tuple[int, ...]
    # 停在哪个节点：以 return 离开时是 return 语句的结局（之后只走了 finally 也是它）或"执行到函数末尾"；以 raise 离开时
    # 是 raise 语句的结局（同上），没有对应的结局时是"未捕获异常退出"（它没有入边，不放进 nodes）。结局不符时是最后
    # 所在的节点；截断的记录不知道停在哪里，为空串
    exit_node: str
    on_graph: bool  # 核对不报任何不符：每一步都有连线（异常与收尾入口的近似算在图上）、结局也在图上
    truncated: bool


# ---------- 对外接口 ----------


def stale_files(graph: ProjectGraph) -> tuple[list[str], list[str]]:
    """(内容与分析时不同的文件, 读不到的文件)，都是相对路径、排序。"""
    root = Path(graph.root)
    changed, unreadable = [], []
    for rel, digest in sorted(graph.file_hashes.items()):
        try:
            actual = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            unreadable.append(rel)
            continue
        if actual != digest:
            changed.append(rel)
    return changed, unreadable


def read_records(path: str | Path) -> Iterator[dict]:
    """逐行读记录文件（可能有几百 MB，不整个读入）。给出执行记录（有 fn）、测试结果行和插件错误行（有 error）；
    不认识的字段原样留着。格式只加不改，所以空行、版本头、不认识的测试结果（以后加的 outcome、字段不对的）和其余
    没有 fn / error 的 JSON 对象都跳过。某行不是 JSON、是 JSON 但不是对象、执行记录不合法、版本比能读的新时抛
    RecordError。"""
    with open(path, "rb") as f:
        for no, raw in enumerate(f, 1):
            try:
                text = raw.decode("utf-8")
                rec = json.loads(text) if text.strip() else None
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RecordError(f"第 {no} 行不是合法的 JSON：{e}") from None
            if rec is None:
                continue
            if isinstance(rec, dict) and rec.get("format") == TRACE_FORMAT:  # 版本头（可以没有，也可能有几行）
                if not (type(rec.get("v")) is int and 1 <= rec["v"] <= TRACE_VERSION):
                    raise RecordError(f"第 {no} 行：记录格式版本 {rec.get('v')!r} 不是这个 pystepmap 能读的"
                                      f"（最高 {TRACE_VERSION}），请升级 pystepmap")
                continue
            if not isinstance(rec, dict) or ("error" not in rec and "fn" in rec and not _well_formed(rec)):
                raise RecordError(f"第 {no} 行不是执行记录：{text.strip()[:80]}")
            if "error" not in rec and "fn" not in rec and not _test_result(rec):
                continue
            yield rec


def walk_record(graph: ProjectGraph, record: dict, cache: dict | None = None) -> Walked | None:
    """把一条执行记录放到图上走一遍（同 verify）。函数不在图里时为 None。cache：批量调用时传同一个字典（只用于这一张
    图），复用解析过的源码、每个函数的源码事实和走图结果。"""
    hit = _walk_cached(graph, record, {} if cache is None else cache)
    if hit is None:
        return None
    _, _, w, index = hit
    nodes = tuple(nid for nid, _ in w.path)
    at = tuple(0 if pos < 0 else index[pos] if pos < len(index) else len(record["lines"]) for _, pos in w.path)
    return Walked(nodes, at, w.exit_node, not w.problems, bool(record.get("truncated")))


def verify(graph: ProjectGraph, records: Iterable[dict], *, allow_stale: bool = False) -> VerifyReport:
    """把记录逐条放到图上核对。图过期时抛 StaleGraphError（allow_stale 为真时照样核对）。"""
    if not allow_stale:
        changed, unreadable = stale_files(graph)
        if changed or unreadable:
            raise StaleGraphError(changed, unreadable)
    cache: dict = {}
    sources = _cache_sources(graph, cache)
    verdicts: dict[tuple[str, str, int], tuple | None] = {}  # (调用方, 被调, 调用所在行) -> 判定
    totals: Counter[str] = Counter()
    problems: Counter[tuple] = Counter()
    approximations: Counter[tuple] = Counter()
    implicit: Counter[tuple] = Counter()
    dynamic: Counter[tuple] = Counter()
    seen: set[str] = set()
    unknown: set[str] = set()
    errors: list[str] = []
    for rec in records:
        if "error" in rec:
            errors.append(str(rec["error"]))
            continue
        if "fn" not in rec:  # 测试结果行等：不是执行记录
            continue
        name = rec["fn"]
        hit = _walk_cached(graph, rec, cache)
        if hit is None:
            unknown.add(name)
            continue
        fn, _, w, _ = hit
        seen.add(name)
        n = rec.get("n", 1)
        truncated = bool(rec.get("truncated"))
        totals["steps"] += w.steps * n
        totals["on_graph"] += w.on_graph * n
        totals["approx_in"] += w.approx_in * n
        totals["uncaught"] += w.uncaught * n
        totals["truncated"] += truncated * n
        for kind, a, b, detail in w.problems:
            problems[(kind, name, a, b, detail)] += n
        for a, b, kind in w.approximations:
            approximations[(name, a, b, kind)] += n
        for callee, call_line in rec.get("calls", ()):
            totals["calls"] += n
            vkey = (name, callee, call_line)
            if vkey not in verdicts:
                verdicts[vkey] = _call_verdict(graph, sources, fn, callee, call_line)
            v = verdicts[vkey]
            if v is None:
                continue
            if v[0] == "implicit":
                implicit[(name, callee, v[1])] += n
            elif v[0] == "dynamic":
                dynamic[(name, callee, call_line)] += n
            else:
                problems[("call", name, call_line, v[1], callee)] += n
    mismatches = [Mismatch(kind, fn_, a, b, detail, c) for (kind, fn_, a, b, detail), c in problems.items()]
    mismatches.sort(key=lambda m: (-m.count, _KIND_ORDER[m.kind], m.fn, m.from_line, m.to_line, m.detail))
    approx = [Approximation(fn_, a, b, kind, c) for (fn_, a, b, kind), c in approximations.items()]
    approx.sort(key=lambda x: (-x.count, x.fn, x.from_line, x.to_line, x.kind))
    imp = [ImplicitCall(caller, callee, reason, c) for (caller, callee, reason), c in implicit.items()]
    imp.sort(key=lambda d: (-d.count, d.caller, d.callee))
    dyn = [DynamicCall(caller, callee, ln, *verdicts[(caller, callee, ln)][1:], c)
           for (caller, callee, ln), c in dynamic.items()]
    dyn.sort(key=lambda d: (-d.count, d.caller, d.callee, d.line))
    return VerifyReport(
        functions_in_graph=len(graph.functions), functions_seen=len(seen),
        distinct_paths=len({k[:3] for k in cache.get("walks", ())}), steps_total=totals["steps"],
        steps_on_graph=totals["on_graph"], approx_entries=totals["approx_in"], uncaught_exits=totals["uncaught"],
        calls_total=totals["calls"], implicit_calls=sum(implicit.values()), dynamic_calls=sum(dynamic.values()),
        truncated_records=totals["truncated"], unknown_functions=len(unknown),
        plugin_errors=len(errors), mismatches=mismatches, approx_list=approx, implicit_call_list=imp,
        dynamic_call_list=dyn,
        plugin_error_list=errors)


def _cache_sources(graph: ProjectGraph, cache: dict) -> _Sources:
    sources = cache.get("sources")
    if sources is None:
        sources = cache["sources"] = _Sources(Path(graph.root))
    return sources


def _walk_cached(graph: ProjectGraph, rec: dict, cache: dict) -> tuple[_Fn, tuple, _Walk, tuple[int, ...]] | None:
    """(函数, 走图结果的键, 走图结果, 节点行序列每一项在 lines 里的位置)；函数不在图里时为 None。走图结果按
    (函数, 节点行序列, 离开方式, 是否截断) 缓存在 cache["walks"] 里（verify 用它数不同的路径）。"""
    name = rec["fn"]
    g = graph.functions.get(name)
    if g is None:
        return None
    fns = cache.setdefault("fns", {})
    fn = fns.get(name)
    if fn is None:
        fn = fns[name] = _Fn(g, _cache_sources(graph, cache).function(g))
    truncated = bool(rec.get("truncated"))
    seq, index = fn.node_lines_at(rec["lines"])
    wkey = (name, seq, rec["exit"], truncated)
    walks = cache.setdefault("walks", {})
    w = walks.get(wkey)
    if w is None:
        w = walks[wkey] = fn.walk(seq, rec["exit"], truncated)
    return fn, wkey, w, index


# ---------- 记录格式 ----------


def _well_formed(rec: dict) -> bool:
    n = rec.get("n", 1)
    return (isinstance(rec.get("fn"), str) and isinstance(rec.get("lines"), list)
            and all(type(x) is int for x in rec["lines"]) and rec.get("exit") in ("return", "raise")
            and isinstance(rec.get("calls", []), list) and all(_call_pair(c) for c in rec.get("calls", []))
            and type(n) is int and n > 0 and ("ns" not in rec or _times(rec["ns"], len(rec["lines"]))))


def _times(ns: object, length: int) -> bool:
    """执行记录的 ns：和 lines 等长的非负整数列表。"""
    return isinstance(ns, list) and len(ns) == length and all(type(x) is int and x >= 0 for x in ns)


def _test_result(rec: dict) -> bool:
    """测试结果行：{"test": node id, "outcome": ..., "seconds": 非负数, "n": 正整数}，可以有 "exc": 异常类型名。"""
    seconds, n, exc = rec.get("seconds"), rec.get("n", 1), rec.get("exc")
    return (isinstance(rec.get("test"), str) and rec.get("outcome") in TEST_OUTCOMES
            and type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0
            and type(n) is int and n > 0 and (exc is None or isinstance(exc, str)))


_NOT_IMPLICIT = frozenset({"__init__", "__call__"})  # 有调用语法（Cls(...)、obj(...)），图上应当有
_ACCESSOR_DECORATORS = frozenset({"property", "cached_property"})
_ACCESSOR_ATTRS = frozenset({"cached_property", "setter", "getter", "deleter"})


def _call_pair(c: object) -> bool:
    """[被调函数 key, 调用方所在行]。"""
    return isinstance(c, list) and len(c) == 2 and isinstance(c[0], str) and type(c[1]) is int


def _call_verdict(graph: ProjectGraph, sources: _Sources, caller: _Fn, callee: str, line: int) -> tuple | None:
    """None：图上有这个调用；("implicit", 原因)；("dynamic", 是否强匹配, 名字列表, 是否回调)；
    ("call", 被调函数 def 行)：不符。line 是调用发生时调用方所在的行。"""
    if callee in caller.call_targets:
        return None
    cg = graph.functions.get(callee)
    if cg is not None and cg.parent == caller.g.key:
        return None
    name = _simple_name(callee)
    if name.startswith("__") and name.endswith("__") and len(name) > 4 and name not in _NOT_IMPLICIT:
        return ("implicit", "dunder")
    node = sources.function(cg) if cg is not None else None
    if node is not None and _is_accessor(node):
        return ("implicit", "property")
    if name in caller.references_in_statement(line):
        return ("dynamic", True, [name], True)
    names = caller.unresolved_in_statement(line)
    if names:
        return ("dynamic", any(n.rsplit(".", 1)[-1] == name for n in names), names, False)
    return ("call", cg.line if cg is not None else _def_line(callee))


def _is_accessor(node: _FuncNode) -> bool:
    """@property、@cached_property / @functools.cached_property、@x.setter / getter / deleter。"""
    for d in node.decorator_list:
        if isinstance(d, ast.Name) and d.id in _ACCESSOR_DECORATORS:
            return True
        if isinstance(d, ast.Attribute) and d.attr in _ACCESSOR_ATTRS:
            return True
    return False


def _simple_name(key: str) -> str:
    """函数 key 里限定名的最后一段：app/a.py::Svc.handle@3 -> handle。"""
    return key.split("::", 1)[-1].rsplit("@", 1)[0].rsplit(".", 1)[-1]


def _def_line(key: str) -> int:
    try:
        return int(key.rsplit("@", 1)[1])
    except (IndexError, ValueError):
        return 0


# ---------- 源码 ----------


class _Sources:
    """每个源文件只解析一次。"""

    def __init__(self, root: Path):
        self.root = root
        self._defs: dict[str, dict[int, _FuncNode]] = {}

    def function(self, g: FunctionGraph) -> _FuncNode | None:
        """图里函数对应的语法树节点；文件读不了、解析不了或对不上（图过期）时为 None。"""
        defs = self._defs.get(g.file)
        if defs is None:
            defs = self._defs[g.file] = self._parse(g.file)
        node = defs.get(g.line)
        return node if node is not None and node.name == _simple_name(g.key) else None

    def _parse(self, rel: str) -> dict[int, _FuncNode]:
        try:
            tree = ast.parse((self.root / rel).read_bytes())  # 字节：编码声明、BOM 由解析器处理
        except (OSError, SyntaxError, ValueError, RecursionError):
            return {}
        return {n.lineno: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _header_end(st: ast.stmt) -> int:
    """语句头部的结束行：简单语句取整条；if / while 到条件结束；for 到迭代表达式结束；with 到最后一个上下文
    表达式（含 as 目标）结束；try 只有 try: 这一行；match 到主语表达式结束。"""
    if isinstance(st, ast.If | ast.While):
        return st.test.end_lineno
    if isinstance(st, ast.For | ast.AsyncFor):
        return st.iter.end_lineno
    if isinstance(st, ast.With | ast.AsyncWith):
        return max(max(i.context_expr.end_lineno, i.optional_vars.end_lineno if i.optional_vars else 0)
                   for i in st.items)
    if isinstance(st, ast.Try | ast.TryStar):
        return st.lineno
    if isinstance(st, ast.Match):
        return st.subject.end_lineno
    return st.end_lineno


@dataclass
class _TryRanges:
    """一个 try 语句各部分的行范围。"""

    whole: Span
    body: Span
    others: list[Span]  # 各 except 体（含 except 头）和 else 体
    catch_lines: list[int]  # 各 except 头所在的行（画出的捕获节点就在这一行）
    final: Span | None


@dataclass
class _TryFacts:
    """一个 try 语句在图上的近似落脚点。"""

    ranges: _TryRanges
    from_body: set[str]  # A 在 try 体里时可以近似到达的节点：捕获节点、finally 入口、（只有 finally 时）try 之后
    swallowed: set[str]  # A 在 try 体里、异常被只写日志的 except 吞掉后落到的 try 之后的节点（要有头部行作证）
    log_only_lines: frozenset[int]  # 只写日志、不画捕获节点的 except 的头部行
    from_others: set[str]  # A 在 except / else 体里时：finally 入口
    swallow_to_end: bool  # 有只写日志的 except，吞掉的异常落到函数末尾（要有头部行作证）


def _inside(line: int, span: Span | None) -> bool:
    return span is not None and span[0] <= line <= span[1]


@dataclass
class _Scope:
    """一个函数自己的帧里的源码事实（不含嵌套的函数、类、lambda 的体；嵌套定义的装饰器、默认值算外层的）。"""

    line_map: dict[int, int]  # 行 -> 所属语句（或 except 子句头、case 守卫、嵌套定义的头部）的起始行
    withs: dict[int, tuple[int, int, int, int]]  # with 头部的每一行 -> (头部起, 头部止, 体起, 体止)
    clauses: dict[int, tuple[int, int, int, int]]  # except / case 子句头部的每一行 -> 同上（体不在头部行上的子句）
    one_line_clauses: list[Span]  # 体写在头部行上的子句的头部范围
    tries: list[_TryRanges]
    references: list[tuple[int, str]]  # 作为调用的参数传出（不是调用）的名字：(所在行, 名字或属性名)
    one_line_excepts: set[int] = field(default_factory=set)  # 体写在头部行上的 except 子句的头部起始行
    raise_lines: set[int] = field(default_factory=set)  # raise 语句的起始行
    return_lines: set[int] = field(default_factory=set)  # return 语句的起始行

    @property
    def finals(self) -> list[Span]:
        return [t.final for t in self.tries if t.final is not None]


def _span(stmts: list[ast.stmt]) -> Span:
    return stmts[0].lineno, stmts[-1].end_lineno


def _def_header(n: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[ast.expr]:
    """嵌套定义里在外层帧执行的部分：装饰器、参数默认值；类的基类和关键字参数。"""
    if isinstance(n, ast.ClassDef):
        return [*n.decorator_list, *n.bases, *(k.value for k in n.keywords)]
    return [*n.decorator_list, *n.args.defaults, *(d for d in n.args.kw_defaults if d is not None)]


def _scope(fn: _FuncNode | None) -> _Scope:
    scope = _Scope({}, {}, {}, [], [], [])
    if fn is None:
        return scope
    spans: list[Span] = []

    def clause(hlo: int, hhi: int, body: list[ast.stmt]) -> None:
        spans.append((hlo, hhi))
        blo, bhi = body[0].lineno, body[-1].end_lineno
        if blo <= hhi:
            scope.one_line_clauses.append((hlo, hhi))
        else:
            for ln in range(hlo, hhi + 1):
                scope.clauses[ln] = (hlo, hhi, blo, bhi)

    stack: list[ast.AST] = list(reversed(fn.body))
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):  # 体是别的帧；头部在这一帧执行
            header = _def_header(n)
            spans.append((min([n.lineno, *(e.lineno for e in header)]), max([n.lineno, *(e.end_lineno for e in header)])))
            stack.extend(reversed(header))
            continue
        if isinstance(n, ast.stmt):
            end = _header_end(n)
            spans.append((n.lineno, end))
            if isinstance(n, ast.Raise):
                scope.raise_lines.add(n.lineno)
            elif isinstance(n, ast.Return):
                scope.return_lines.add(n.lineno)
            if isinstance(n, ast.With | ast.AsyncWith):
                for ln in range(n.lineno, end + 1):
                    scope.withs[ln] = (n.lineno, end, n.body[0].lineno, n.body[-1].end_lineno)
            elif isinstance(n, ast.Try | ast.TryStar):
                scope.tries.append(_TryRanges(
                    (n.lineno, n.end_lineno), _span(n.body),
                    [(h.lineno, h.end_lineno) for h in n.handlers] + ([_span(n.orelse)] if n.orelse else []),
                    [h.lineno for h in n.handlers], _span(n.finalbody) if n.finalbody else None))
        elif isinstance(n, ast.Call):
            for arg in [*n.args, *(k.value for k in n.keywords)]:
                arg = arg.value if isinstance(arg, ast.Starred) else arg
                if isinstance(arg, ast.Name):
                    scope.references.append((arg.lineno, arg.id))
                elif isinstance(arg, ast.Attribute):
                    scope.references.append((arg.lineno, arg.attr))
        elif isinstance(n, ast.excepthandler):
            hhi = n.type.end_lineno if n.type is not None else n.lineno
            clause(n.lineno, hhi, n.body)
            if n.body[0].lineno <= hhi:
                scope.one_line_excepts.add(n.lineno)
        elif isinstance(n, ast.match_case):
            if n.guard is not None:
                spans.append((n.guard.lineno, n.guard.end_lineno))
            clause(n.pattern.lineno, (n.guard or n.pattern).end_lineno, n.body)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    for lo, hi in sorted(spans, key=lambda s: s[1] - s[0], reverse=True):  # 小的范围（内层）覆盖大的
        for ln in range(lo, hi + 1):
            scope.line_map[ln] = lo
    return scope


# ---------- 走图 ----------


@dataclass
class _Walk:
    steps: int = 0
    on_graph: int = 0
    approx_in: int = 0
    uncaught: bool = False
    problems: tuple[tuple[str, int, int, str], ...] = ()
    approximations: tuple[tuple[int, int, str], ...] = ()  # (前一个节点的行, 当前节点的行, "entry")
    # 选中的走法依次经过的 (节点 id, 节点行序列里的位置)：开始节点的位置是 -1；最后补上的"执行到函数末尾"是 len(序列)
    path: tuple[tuple[str, int], ...] = ()
    exit_node: str = ""  # 见 Walked.exit_node


def _unlink(link: tuple | None) -> tuple:
    """(条目, 前面的链) 串成的链 → 按先后顺序的元组。"""
    out = []
    while link is not None:
        out.append(link[0])
        link = link[1]
    return tuple(reversed(out))


@dataclass
class _Trail:
    """一种走法走到某一行为止的状态。problems / approximations 是 (条目, 前面的链) 串成的链，避免每步复制。"""

    node: str
    tail: str | None  # 最后经过的结局节点，且之后只走了 finally 里的节点；否则 None（以 raise 离开时看它）
    # 上一个经过的节点之后的证据：只写日志的 except 头部行（异常被吞掉）、try 体里的行所属的 try 的起始行（进入了 try 体）
    markers: frozenset[int] = frozenset()
    cost: tuple[int, int] = (0, 0)  # (不符数, 近似数)
    steps: int = 0
    on_graph: int = 0
    approx_in: int = 0
    problems: tuple | None = None
    approximations: tuple | None = None
    ret: str | None = None  # 最后经过的 return 语句的结局，且之后只走了 finally 里的节点（以 return 离开时看它）
    wait: Span | None = None  # 经过了 ret 正在经过的 finally 体里的 raise 结局、异常还没被吞掉：那个 finally 体
    path: tuple | None = None  # 经过的 (节点 id, 节点行序列里的位置) 串成的链，不参与去重和比较代价


def _state(t: _Trail) -> tuple:
    """走法的去重键：同一节点、同样的结局状态和证据的走法只留代价最小的一种。"""
    return t.node, t.tail, t.ret, t.wait, t.markers


class _Fn:
    """一个函数的图和源码事实，核对它的每条记录。"""

    def __init__(self, g: FunctionGraph, node: _FuncNode | None):
        self.g = g
        self.title = {n.id: n.title for n in g.nodes}
        self.line = {n.id: n.line for n in g.nodes}
        self.start = next(n.id for n in g.nodes if n.type == "start")
        self.by_line: dict[int, list[str]] = {}  # 行 -> 该行的节点，按编号顺序（图里的顺序）
        for n in g.nodes:
            if n.type != "start" and n.line > 0:
                self.by_line.setdefault(n.line, []).append(n.id)
        self.edges: dict[tuple[str, str], frozenset[str]] = {}
        for e in g.edges:
            self.edges[(e.src, e.dst)] = self.edges.get((e.src, e.dst), frozenset()) | {e.kind}
        self.landing = {e.dst for e in g.edges if e.kind in _LANDING}  # 有 raise / finally 入边的节点
        self.ends = {n.id for n in g.nodes if n.type == "end"}
        self.failed = {n.id for n in g.nodes if n.type == "end" and n.end_kind == "failed"}
        self.fell_off = {n.id for n in g.nodes if n.type == "end" and n.line == 0 and n.end_kind == "ok"}
        self.fell_off_order = [n.id for n in g.nodes if n.id in self.fell_off]
        self.uncaught_end = next((n.id for n in g.nodes if n.type == "end" and n.end_kind == "exception"), "")
        self.into_fell_off = {src for src, dst in self.edges if dst in self.fell_off}
        self.call_targets = set(g.call_targets)
        self.scope = _scope(node)
        self._unresolved_by_statement: dict[int, list[str]] = {}  # 语句起始行 -> 其中无法判断目标的调用名
        for u in g.unresolved:
            names = self._unresolved_by_statement.setdefault(self.statement(u.line), [])
            if u.name not in names:
                names.append(u.name)
        self.finally_nodes = {n.id for n in g.nodes if n.line > 0
                              and any(lo <= n.line <= hi for lo, hi in self.scope.finals)}
        # raise 语句的结局（return failed(...) 之类的 return 结局 end_kind 可能相同，按语句区分）
        self.raise_ends = {n.id for n in g.nodes if n.type == "end" and n.line in self.scope.raise_lines
                           and n.line not in self.scope.return_lines}
        self.catch_nodes = {n.id for n in g.nodes if n.type == "catch"}
        self.except_headers = {ln for t in self.scope.tries for ln in t.catch_lines}
        # 写在一行的 except 的头部行 -> 异常从它那里往外传时会经过的 finally 体（它所属的 try 和外层 try 的）
        self._one_line_skip: dict[int, list[Span]] = {}
        for t in self.scope.tries:
            spans = [u.final for u in self.scope.tries if u.final is not None and (
                u is t or _inside(t.whole[0], u.body) or any(_inside(t.whole[0], span) for span in u.others))]
            for h in t.catch_lines:
                if h in self.scope.one_line_excepts:
                    self._one_line_skip[h] = spans
        self._tries = [self._try_facts(t, g) for t in self.scope.tries]
        self.log_only_headers = {ln for f in self._tries for ln in f.log_only_lines}
        self.try_bodies: dict[int, tuple[int, ...]] = {}  # 行 -> try 体包含这一行的 try 语句（起始行）
        for t in self.scope.tries:
            for ln in range(t.body[0], t.body[1] + 1):
                self.try_bodies[ln] = (*self.try_bodies.get(ln, ()), t.whole[0])
        # (A, B) -> 认可这一步的各种条件：(要求进入了 try 体的 try 起始行或 None, 要求的只写日志 except 头部行或 None)
        self._entries: dict[tuple[str, str], list[tuple[int | None, frozenset[int] | None]]] = {}
        self._references_by_statement: dict[int, set[str]] = {}  # 语句起始行 -> 其中作为参数传出的名字
        for ln, ref in self.scope.references:
            self._references_by_statement.setdefault(self.statement(ln), set()).add(ref)
        self.optional = {ln for ln in self.by_line
                         if any(lo <= ln <= hi for lo, hi in self.scope.one_line_clauses)}
        self.start_reaches_end = bool(self._reachable(self.start) & self.ends)

    def statement(self, line: int) -> int:
        """行所属语句（或 except 子句头、case 守卫）的起始行，同核对执行路径时的映射。多行语句的每一行都归到它，
        复合语句只有头部的行归到它（体里的行属于体里各自的语句）。"""
        return self.scope.line_map.get(line, line)

    def unresolved_in_statement(self, line: int) -> list[str]:
        """line 所属语句里无法判断目标的调用的显示名（行号未知为 0 时为空）。"""
        return self._unresolved_by_statement.get(self.statement(line), []) if line > 0 else []

    def references_in_statement(self, line: int) -> set[str]:
        """line 所属语句里作为调用参数传出的名字（行号未知为 0 时为空）。"""
        return self._references_by_statement.get(self.statement(line), set()) if line > 0 else set()

    # ---------- 异常与收尾的近似（见模块说明第 2 步）----------

    def _try_facts(self, t: _TryRanges, g: FunctionGraph) -> _TryFacts:
        """try 语句 t 在图上的近似落脚点（见模块说明第 2、3 步）；落脚点都要求有 raise / finally 入边。"""
        line = self.line
        catch_nodes = [n for n in g.nodes if n.type == "catch" and n.line in t.catch_lines]
        catches = {n.id for n in catch_nodes}
        finals = {b for a, b in self.edges if _inside(line[b], t.final) and not _inside(line[a], t.final)}
        drawn = {n.line for n in catch_nodes}
        log_only_lines = frozenset(ln for ln in t.catch_lines if ln not in drawn)
        after = {b for (a, b), kinds in self.edges.items()  # 从 try 里面出来、落到 try 外面的异常与收尾连线的终点
                 if kinds & _LANDING and _inside(line[a], t.whole) and line[b] > 0 and not _inside(line[b], t.whole)}
        swallow_to_end = bool(log_only_lines) and any(
            "raise" in kinds and _inside(line[a], t.whole) and b in self.fell_off for (a, b), kinds in self.edges.items())
        return _TryFacts(t, (catches | finals | (after if not t.catch_lines else set())) & self.landing,
                         (after if log_only_lines else set()) & self.landing, log_only_lines,
                         finals & self.landing, swallow_to_end)

    def _approx_entry(self, a: str, b: str, evidence: frozenset[int]) -> bool:
        """A → B 没有连线时，是否是某个 try 的异常与收尾入口近似。evidence：A 之后、B 之前原始行号里的证据——出现的
        只写日志的 except 头部行（异常被吞掉）和 try 体里的行所属的 try 的起始行（执行进入了 try 体）。"""
        conds = self._entries.get((a, b))
        if conds is None:
            conds = self._entries[(a, b)] = self._entry_conditions(a, b)
        return any((body is None or body in evidence) and (header is None or bool(header & evidence))
                   for body, header in conds)

    def _entry_conditions(self, a: str, b: str) -> list[tuple[int | None, frozenset[int] | None]]:
        """A → B 按入口近似认可的条件（见模块说明第 2 步）：A 在 T 的 try 体里，或 A 不在、但这一步之间出现了 T 的
        try 体里的行（要求 T 的起始行在证据里）；落到只写日志的 except 之后还要求那个 except 的头部行。"""
        ln, conds = self.line[a], []
        for f in self._tries:
            body = None if _inside(ln, f.ranges.body) else f.ranges.whole[0]
            if b in f.from_body:
                conds.append((body, None))
            if b in f.swallowed:
                conds.append((body, f.log_only_lines))
            if b in f.from_others and any(_inside(ln, span) for span in f.ranges.others):
                conds.append((None, None))
        return conds

    def _return_finally(self, r: str, e: str) -> Span | None:
        """return 结局 r 正在经过的、包含 raise 结局 e 的 finally 体：所属 try 的 try 体 / except / else 包含 r。"""
        lr, le = self.line[r], self.line[e]
        for t in self.scope.tries:
            if _inside(le, t.final) and (_inside(lr, t.body) or any(_inside(lr, span) for span in t.others)):
                return t.final
        return None

    def _swallowed_in(self, span: Span, nid: str | None, evidence: frozenset[int]) -> bool:
        """finally 体 span 里的 raise 之后，异常在 span 里被吞掉的证据：这一步走到的节点 nid 是 span 里（写成多行的）
        except 的捕获节点，或者这一步之间（结局：最后的节点之后）出现了 span 里只写日志的 except 的标记、或写在一行的
        except 吞掉了异常的标记（见 _one_line_swallowed）。写在一行的 except 的捕获节点本身不算：它的头部行事件不论
        是否匹配都会出现。"""
        line = self.line[nid] if nid is not None else 0
        if nid in self.catch_nodes and _inside(line, span) and line not in self.scope.one_line_excepts:
            return True
        return any(_inside(h, span) for h in evidence & (self.log_only_headers | self.scope.one_line_excepts))

    def _one_line_swallowed(self, raw: list[int], i: int, h: int) -> bool:
        """写在一行的 except（头部行 h，事件是 raw[i]）是否吞掉了异常：之后跳过这一行自己、with 头部（离开时的
        __exit__）和异常往外传时经过的 finally 体，第一行是 except 头部时没有（不匹配，或子句体又抛出了异常）。"""
        skip = self._one_line_skip[h]
        for ln in raw[i + 1:]:
            m = self.statement(ln)
            if m == h or ln in self.scope.withs or any(_inside(ln, span) for span in skip):
                continue
            return m not in self.except_headers
        return True

    def _approx_to_end(self, a: str, evidence: frozenset[int]) -> tuple[int, int, str] | None:
        """以 return 离开、停在 A 时，吞掉的异常落到"执行到函数末尾"的入口近似（见模块说明第 3 步）；不是时为 None。
        evidence：A 之后原始行号里的证据（同 _approx_entry）。"""
        la = self.line[a]
        for f in self._tries:
            if f.swallow_to_end and (_inside(la, f.ranges.body) or f.ranges.whole[0] in evidence) \
                    and f.log_only_lines & evidence:
                return (la, 0, "entry")
        return None

    def _reachable(self, src: str) -> set[str]:
        nexts: dict[str, list[str]] = {}
        for a, b in self.edges:
            nexts.setdefault(a, []).append(b)
        seen, stack = {src}, [src]
        while stack:
            for dst in nexts.get(stack.pop(), ()):
                if dst not in seen:
                    seen.add(dst)
                    stack.append(dst)
        return seen

    # ---------- 行 → 节点行 ----------

    def node_lines(self, raw: list[int]) -> tuple[int, ...]:
        """执行到的行 → 节点所在的语句起始行序列（见模块说明第 1 步），其中夹着证据标记（负数）：只写日志的 except
        的标记、写在一行的 except 吞掉了异常的标记（负的头部行号），以及进入了 try 体的标记（负的 try 起始行号，
        两个节点行之间每个 try 至多一个）。
        连续的头部行事件作为一串，只在第一个上判断。进入 try 体的标记先挂起，下一项是别的节点行、except 标记或
        记录结束时才写出；同一节点行再次出现时照旧合并，挂起的标记丢掉（它们属于一个不存在的步子）。"""
        return self.node_lines_at(raw)[0]

    def node_lines_at(self, raw: list[int]) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """(node_lines(raw), 序列里每一项在 raw 里的位置)：节点行是它（合并之前）第一次出现的位置，标记为 -1。"""
        sc, by_line = self.scope, self.by_line
        out: list[int] = []
        at: list[int] = []
        pending: list[int] = []  # 上一个节点行之后、还没写出的进入 try 体的标记
        prev: int | None = None
        leaving = entered = False
        for i, ln in enumerate(raw):
            p, prev = prev, ln
            w = sc.withs.get(ln)
            if w is not None:
                if p is None or not w[0] <= p <= w[1]:
                    leaving = p is not None and w[2] <= p <= w[3]  # 刚在 with 体里：这是离开时的 __exit__
                if leaving:
                    continue
            c = sc.clauses.get(ln)
            if c is not None:
                if p is None or not c[0] <= p <= c[1]:
                    j = i + 1
                    while j < len(raw) and c[0] <= raw[j] <= c[1]:
                        j += 1
                    entered = j < len(raw) and c[2] <= raw[j] <= c[3]
                if not entered:
                    continue
            m = sc.line_map.get(ln, ln)
            one_line = m in sc.one_line_excepts and self._one_line_swallowed(raw, i, m)  # 写在一行、吞掉了异常
            if m in by_line:
                if out and out[-1] == m:  # 同一节点行再次出现、中间没有节点：合并（写在一行的 except 的标记留着）
                    pending[:] = [x for x in pending if -x in sc.one_line_excepts]
                    continue
                out += pending
                at += [-1] * len(pending)
                pending.clear()
                out.append(m)
                at.append(i)
                if one_line:  # 写在一行、画了捕获节点的 except 吞掉了异常：标记挂在这一行之后
                    pending.append(-m)
                continue
            for t in self.try_bodies.get(ln, ()):  # 不经过节点就进入了 try 体：作为异常从 try 体开头抛出的证据
                if -t not in pending:
                    pending.append(-t)
            if m not in self.log_only_headers or (m in sc.one_line_excepts and not one_line):
                continue
            m = -m  # 进入了只写日志的 except：留一个标记，作为异常被吞掉的证据
            out += pending
            at += [-1] * len(pending)
            pending.clear()
            if not out or out[-1] != m:
                out.append(m)
                at.append(-1)
        return tuple(out + pending), tuple(at + [-1] * len(pending))

    # ---------- 走图 ----------

    def walk(self, seq: tuple[int, ...], exit_kind: str, truncated: bool) -> _Walk:
        """在所有走法（每行取一个前缀，可选行可以不走）里取不符最少、其次近似最少的一种（见模块说明第 2、3 步）。"""
        start = _Trail(self.start, None, path=((self.start, -1), None))
        states: dict[tuple, _Trail] = {_state(start): start}
        for pos, ln in enumerate(seq):
            if ln < 0:  # 证据标记（只写日志的 except 头部行、进入了 try 体的 try 起始行）
                marked = (replace(t, markers=t.markers | {-ln}) for t in states.values())
                states = {_state(t): t for t in marked}
                continue
            nodes = self.by_line[ln]
            options = [nodes[:j] for j in range(len(nodes), 0, -1)] + ([[]] if ln in self.optional else [])
            nxt: dict[tuple, _Trail] = {}
            for trail in states.values():
                for opt in options:
                    t = self._advance(trail, opt, pos)
                    k = _state(t)
                    if k not in nxt or t.cost < nxt[k].cost:
                        nxt[k] = t
            states = nxt
        best: tuple[tuple, _Trail, tuple | None, bool, tuple | None] | None = None
        no_lines = not any(ln > 0 for ln in seq)
        for trail in states.values():
            end_problem, uncaught, approx = (None, False, None) if truncated else self._outcome(trail, exit_kind, no_lines)
            cost = (trail.cost[0] + (end_problem is not None), trail.cost[1] + (approx is not None))
            if best is None or cost < best[0]:
                best = (cost, trail, end_problem, uncaught, approx)
        _, trail, end_problem, uncaught, approx = best
        found = _unlink(trail.problems) + ((end_problem,) if end_problem is not None else ())
        approxs = _unlink(trail.approximations) + ((approx,) if approx is not None else ())
        path = _unlink(trail.path)
        exit_node = "" if truncated else self._exit_node(trail, exit_kind, no_lines, end_problem, approx)
        if exit_node in self.fell_off and path[-1][0] != exit_node:
            path += ((exit_node, len(seq)),)
        return _Walk(trail.steps + (approx is not None), trail.on_graph, trail.approx_in + (approx is not None),
                     uncaught, found, approxs, path, exit_node)

    def _exit_node(self, t: _Trail, exit_kind: str, no_lines: bool, end_problem: tuple | None,
                   approx: tuple | None) -> str:
        """选中的走法停在哪个节点（见 Walked.exit_node），和 _outcome 的判断一致。"""
        if exit_kind == "raise":
            return t.tail if t.tail is not None and t.tail in self.failed else self.uncaught_end
        if end_problem is not None:
            return t.node
        if no_lines:  # 只看结局：一个节点行都没走，就是执行到了函数末尾
            return next((f for f in self.fell_off_order if (t.node, f) in self.edges),
                        self.fell_off_order[0] if self.fell_off_order else t.node)
        if t.ret is not None and (t.wait is None or self._swallowed_in(t.wait, None, t.markers)):
            return t.ret
        if approx is not None:  # 吞掉的异常落到函数末尾
            return self.fell_off_order[0]
        return next((f for f in self.fell_off_order if (t.node, f) in self.edges), t.node)  # 沿连线到函数末尾

    def _advance(self, t: _Trail, nodes: list[str], pos: int) -> _Trail:
        """沿 nodes 走一段。缺连线的一步只看它自己的起点和终点：入口近似，或不符。"""
        cur, tail, ret, wait, evidence = t.node, t.tail, t.ret, t.wait, t.markers  # 证据只属于进入这一行的第一步
        mism, approx = t.cost
        steps, on_graph, ain = t.steps, t.on_graph, t.approx_in
        problems, approxs, path = t.problems, t.approximations, t.path
        for nid in nodes:
            path = ((nid, pos), path)
            if self.edges.get((cur, nid)):
                if nid not in self.ends:  # 沿连线到达结局算在结局里，不计步数
                    steps += 1
                    on_graph += 1
            else:
                steps += 1
                if self._approx_entry(cur, nid, evidence):
                    ain += 1
                    approx += 1
                    approxs = ((self.line[cur], self.line[nid], "entry"), approxs)
                else:
                    mism += 1
                    problems = (("edge", self.line[cur], self.line[nid],
                                 f"{self.title[cur]} → {self.title[nid]}"), problems)
            if nid in self.ends:
                tail = nid
            elif nid not in self.finally_nodes:
                tail = None
            if wait is not None:  # 经过了 finally 体 wait 里的 raise 结局：异常要在 wait 里被吞掉，return 才继续
                if self._swallowed_in(wait, nid, evidence):
                    wait = None
                elif not _inside(self.line[nid], wait):  # 异常还没被吞掉就离开了 wait：return 已被取消
                    ret = wait = None
            if nid in self.ends and nid not in self.raise_ends:
                ret, wait = nid, None
            elif nid not in self.finally_nodes:
                ret = wait = None
            elif ret is not None and nid in self.raise_ends:
                wait = self._return_finally(ret, nid)
                if wait is None:
                    ret = None
            cur, evidence = nid, frozenset()
        markers = evidence if not nodes else frozenset()  # 整行不走时证据留给下一步
        return _Trail(cur, tail, markers, (mism, approx), steps, on_graph, ain, problems, approxs, ret, wait, path)

    def _outcome(self, t: _Trail, exit_kind: str, no_lines: bool) -> tuple[tuple | None, bool, tuple | None]:
        """(结局不符, 是否未捕获异常退出, 吞掉的异常落到函数末尾的近似)。"""
        if exit_kind == "raise":
            return None, not (t.tail is not None and t.tail in self.failed), None
        # 以 return 离开：只认 return 语句的结局之后只走 finally；raise 语句的结局说明那个异常被吞掉了，不说明函数在这里结束
        ret = t.ret is not None and (t.wait is None or self._swallowed_in(t.wait, None, t.markers))
        ok = self.start_reaches_end if no_lines else (ret or t.node in self.into_fell_off)
        if ok:
            return None, False, None
        approx = None if no_lines else self._approx_to_end(t.node, t.markers)
        if approx is not None:
            return None, False, approx
        return ("end", self.line[t.node], 0, f"以 return 离开，但停在「{self.title[t.node]}」"), False, None
