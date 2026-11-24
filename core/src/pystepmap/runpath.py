"""运行路径：把记录插件（trace.py）写的执行记录沿图走一遍（verify.walk_record，和核对同一套走法），按（测试，函数）
汇总成"走过哪些节点、各几次、各多久、走过哪些连线、停在哪里"，并找出没有任何测试走到的结局。

汇总的规则：
- 按稳定标识存：函数用函数 sid，节点用节点 sid（带 #n 的照用；没有 sid 的旧图用函数 key / 节点 id）。所以重新分析
  之后（行号、节点 id 变了）旧的运行路径仍能叠到新图上——但 #n 是位置编号：在前面增删一个同名定义（@overload 桩、
  property 的 setter / deleter、两个分支各定义一次）或一条同文本的语句，后面的 #n 就指向别的函数 / 节点
  （docs/graph-schema.md 不许按相等配 #n）。所以运行路径还记下运行时的样子（RunPaths.shapes、function_dups），
  叠到图上一律按下面"叠到图上"的规则（locate_function、match_function_run、locate_test_run），配不上的绝不配到
  别的函数 / 节点上。
- 一条执行记录（n 次相同的执行）：
  - nodes：依次经过的每个节点 visits 加 n（循环里重复经过的每次都算）；ns 加上它这一段的耗时——从它的第一个行事件
    起、到下一个节点的第一个行事件之前，这些行事件的 ns 之和（记录里的 ns 已经是 n 次之和）。所以节点之后那些不出
    节点的语句算在它上面，函数开头到第一个节点之前的算在开始节点上。同一行上的几个节点（return f()、if c: return
    f()、if c: return 0）从同一个行事件开始，这一段的时间算在其中最后一个不是结局的节点上（前两种是调用的步骤，第三
    种是判断）；结局只在独占一行时有时间。以"执行到函数末尾"结束时它也在经过的节点里（visits 加 n，耗时 0）。
  - edges："起点 sid|终点 sid" → 相邻两个经过的节点各加 n。同一对节点之间的几条连线（普通连线和 raise 连线并行）
    算在一起；图上没有连线的一步（异常与收尾入口的近似、核对报的不符）也照记，界面在图上找不到对应的连线就不画。
  - exits："停点 sid|return 或 raise|异常类型名或空" → n。停点见 verify.Walked.exit_node：return / raise 语句的结局、
    执行到函数末尾、未捕获异常退出（end:uncaught）；结局不符时是最后所在的节点；截断的记录不知道停在哪里，停点为空。
    没迭代完就被丢掉的生成器以 end:uncaught|raise|GeneratorExit 离开（close() 在 yield 处抛 GeneratorExit）。
  - first_path：这个测试里这个函数的第一条记录经过的节点 sid，最多 500 项，超出时前 499 项加"…"。
  - off_graph：核对会报不符的执行次数（这些执行能走的部分照样汇总）。
  - has_time：有一条记录带着和 lines 等长的 ns。没有 ns 的旧记录 ns 都是 0。
  - calls：记录插件实际看到的调用——被调函数 sid（运行时的）→ 这个测试里这个函数有几次执行调用过它：每条执行记录
    的 calls 里出现过的每个被调函数（同一条记录里在几行调用也只算一次）加这条记录的 n。被调函数不在图里的不记。
    这是"记录里有过这个调用"，不是调用次数（一次执行里调用十次也只算 1）。
  - icalls：同样的汇总，来自执行记录的 icalls——经项目以外的帧（库的包装函数、contextmanager、回调）的间接调用，
    记在栈上最近的那个正在执行的项目函数名下（见 trace.py）。写坏了的项跳过（核对不看这个字段）。
  所以汇总的大小不随循环圈数增长（只有次数和累加），first_path 封顶。
- 运行时的样子（每个函数只存一份，不随测试个数重复）：
  - function_dups：运行时的图里有不止一个定义的基础函数 sid（去掉 #n），排序；不管跑没跑过。
  - shapes：函数 sid → FunctionShape(shape, dups)。shape = function_shape（节点 sid 按图里的顺序用换行连起来的
    sha1，十六进制）；dups = 这个函数里有不止一个节点的基础节点 sid，排序。收录每个跑过的函数，以及 function_dups
    里每一组的每个定义（跑没跑过都收，用来看出这一组变没变）。
- 记录里的函数不在图里：跳过。插件错误行跳过（由核对报告）。
- 测试结果行按 test 汇总：cases 加 n，seconds 相加；outcome 取最坏的——failed > error > passed > skipped（参数化的
  几个用例有一个失败就是失败；有通过也有跳过算通过）。exc：第一个失败 / 出错、带着异常类型名的用例的类型名；excs：
  失败 / 出错的用例的所有不同的异常类型名，排序（记录插件只记类型名，见 trace.py）。只有执行记录、没有结果行的测试
  （旧记录、进程中途退出）outcome 为空串；测试之外的执行（收集阶段导入时）在 test 为 "" 的一项里，outcome 也为空串。
  tests 按 test 排序。
- 测试的入口函数（entry_functions）：这个测试里执行过、却没有任何一个执行过的函数记录过（calls 或 icalls）调用它的
  函数（自己调用自己不算），即由测试本身直接调用的，或者栈上没有项目函数在它之下的（事件循环调度的任务、别的线程）。
  按 sid 排序。界面用它判断失败的测试停在哪里：以 raise 离开的入口函数，异常就是从它传到测试里的。局限：
  calls / icalls 是"这条记录里有过"，不分哪一次调用——一个函数既被流程里的函数调用、又被测试直接调用时不算入口；
  记录插件记不到调用方的被调函数会被当成入口：asyncio.gather / create_task 调度的协程、别的线程里执行的函数、项目
  函数直接迭代的生成器、先存起来再 await 的协程，以及隔着超过 64 层项目以外的帧的调用（见 trace.py）；互相递归、又
  只由测试调用的两个函数都不算入口。早于 icalls 的数据只按 calls 算（经库的包装函数调用的步骤会被当成入口）。

叠到图上（服务端和界面都照这里做；base(sid) = 去掉结尾的 "#数字"，nth(sid) = 那个数字、没有时为 1）：
1. 函数——locate_function(graph, paths, run_sid)：设 B = base(run_sid)，S = 图里 base(函数 sid) == B 的函数，按 nth 排。
   a. B 不在 paths.function_dups 里、S 恰好一个：它的 sid == run_sid 时就是它，否则放不上。（运行时、现在都不重名）
   b. 否则（运行时或现在重名）：paths.shapes 里没有 run_sid → 放不上。B 在 function_dups 里、并且运行时这一组
      （paths.shapes 里基础 sid 为 B 的各项，按 nth 排的 [(sid, shape)]）和 S 的 [(sid, function_shape)] 完全相同
      → 这一组没变，取 S 里 sid == run_sid 的。否则，运行时的形状是"空"的（除了开始、执行到函数末尾、未捕获异常
      退出没有别的节点——只有赋值的 __init__、@overload 桩、简单的 setter 都是这个形状，见 TRIVIAL_SHAPES）→ 放不上；
      不空时取 S 里 function_shape == paths.shapes[run_sid].shape 的——恰好一个、并且运行时这一组里没有别的定义也是
      这个形状时才是它；否则放不上。
2. 节点——match_function_run(fg, run, shape)：运行里的节点 sid = run.nodes 的键 + run.exits 里非空的停点（edges、
   first_path 里的都在 nodes 里）。shape 不为 None 且 shape.shape == function_shape(fg) → 逐个按相等配；否则只有
   base 不在 shape.dups 里（shape 为 None 时不查这一条）、并且 fg 里基础 sid 为它的节点只有一个、sid 也相等的配上，
   其余配不上。得到 (运行 sid → 节点 id, 配不上的个数)。
3. 一个测试——locate_test_run(graph, paths, test)：对 test.functions 的每一项先 1 后 2，得到 {图里的函数 key:
   (函数图, 运行 sid → 节点 id)} 和"对不上的处数"= 放不上的函数个数 + 放上了的函数里配不上的节点 sid 个数。界面：节点按
   映射亮；连线 "a|b" 在 a、b 都配上、图上有这条连线时亮；处数 > 0 时说"这次运行之后代码改过，有 N 处对不上"。
没有 shapes / function_dups 的旧数据：只有运行时、现在都不重名的函数（1a）和节点（2 的后一半）放得上。
只取一部分运行路径给使用者（例如按测试切开、一次只给一个测试）时，shapes 里基础 sid 在 function_dups 里的每一项都
必须带上——不只是这个测试自己跑过的函数：重名组里没跑过的定义也要用来判断这一组变没变（1b）；少了它们，同一张图上
也会放不上或放错。function_dups 也要整个带上。

JSON（run_paths_to_json）：{"version": 1, "tests": [{"test", "outcome", "seconds", "cases", "functions": {函数 sid:
{"function", "executions", "nodes": {节点 sid: {"visits", "ns"}}, "edges": {...}, "exits": {...}, "first_path": [...],
"off_graph", "has_time", "calls": {被调函数 sid: 次数}, "icalls": {被调函数 sid: 次数}}}, "exc": 异常类型名,
"excs": [...]}], "shapes": {函数 sid: {"shape", "dups"}}, "function_dups": [...]}，字段名同下面的 dataclass，functions、
shapes 按函数 sid 排序。为省体积，空的 calls、icalls、空串的 exc、空的 excs 不写出来，读的时候缺了就是 {} / "" / []。
只有标识、次数、耗时、退出方式和异常类型名，没有任何取值。读的时候没有 shapes / function_dups 的旧数据读出来是 {} / []；第 1 轮每个函数自带的
shape / dups 忽略。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from itertools import pairwise

from .model import FlowLine, FunctionGraph, ProjectGraph
from .query import line_functions
from .verify import Walked, walk_record

VERSION = 1
FIRST_PATH_MAX = 500
# 没有语句节点的函数的形状（节点只有开始、执行到函数末尾、未捕获异常退出这几种隐含节点）：所有这样的函数都一样，
# 不能用来在重名组里认出是哪一个
TRIVIAL_SHAPES = frozenset(hashlib.sha1("\n".join(seq).encode("utf-8")).hexdigest() for seq in (
    ("start",), ("start", "end:implicit"), ("start", "end:uncaught"), ("start", "end:implicit", "end:uncaught")))
_MORE = "…"
_RANK = {"": 0, "skipped": 1, "passed": 2, "error": 3, "failed": 4}  # 测试结果汇总时取大的


@dataclass
class NodeStat:
    visits: int = 0
    ns: int = 0  # 这些次经过的耗时之和（含它之后到下一个节点之前那些不出节点的语句）


@dataclass
class FunctionRun:
    function: str  # 函数 sid
    executions: int  # 这个测试里这个函数执行了几次
    nodes: dict[str, NodeStat] = field(default_factory=dict)  # 节点 sid → 次数、耗时
    edges: dict[str, int] = field(default_factory=dict)  # "起点 sid|终点 sid" → 走过几次
    exits: dict[str, int] = field(default_factory=dict)  # "节点 sid|return 或 raise|异常类型名或空" → 次数
    first_path: list[str] = field(default_factory=list)  # 第一次执行依次经过的节点 sid，最多 500 个，超出时最后一项是 "…"
    off_graph: int = 0  # 有几次执行没能完整地走在图上（核对会报）
    has_time: bool = False  # 记录里有没有耗时
    calls: dict[str, int] = field(default_factory=dict)  # 被调函数 sid → 有几次执行调用过它（见模块说明）
    icalls: dict[str, int] = field(default_factory=dict)  # 同上，经项目以外的帧的间接调用（见模块说明）


@dataclass
class TestRun:
    __test__ = False  # 名字以 Test 开头：别让 pytest 当成测试类去收集

    test: str  # pytest 的 node id（去掉参数化后缀）；测试之外的执行为 ""
    outcome: str  # passed / failed / skipped / error；没有结果行时为 ""
    seconds: float
    cases: int  # 结果行的条数（参数化的每个用例一条）
    functions: dict[str, FunctionRun] = field(default_factory=dict)  # 函数 sid → 汇总
    exc: str = ""  # 让它失败的异常的类型名（第一个失败 / 出错的用例的）；不知道或没失败时为 ""
    excs: list[str] = field(default_factory=list)  # 失败 / 出错的用例的所有不同的异常类型名，排序


@dataclass
class FunctionShape:
    shape: str  # function_shape：节点 sid 按图里的顺序的 sha1
    dups: list[str] = field(default_factory=list)  # 有不止一个节点的基础节点 sid（去掉 #n），排序


@dataclass
class RunPaths:
    tests: list[TestRun]  # 按 test 排序
    version: int = VERSION
    shapes: dict[str, FunctionShape] = field(default_factory=dict)  # 运行时的函数 sid → 运行时的样子（见模块说明）
    function_dups: list[str] = field(default_factory=list)  # 运行时有不止一个定义的基础函数 sid，排序


@dataclass(frozen=True)
class Outcome:
    function: str  # 函数 sid
    node: str  # 结局节点的 sid
    title: str
    end_kind: str  # ok / skipped / failed
    file: str
    line: int  # 结局所在行；执行到函数末尾为 0
    # 说不准：这个结局属于同文本的一组（运行时或新图里有不止一个）、而运行之后这个函数的节点变了，或者这个函数属于
    # 同名的一组、而这一组有运行放不上，或者（给了运行时的图时）这个函数、这个结局是运行之后才有的。既不算走到也不算
    # 没走到；界面应当写"运行之后代码改过，这几处对不上"
    uncertain: bool = False
    # 说不准的原因是「那次运行时还没有它」（给了运行时的图才分得出）：整个函数、或者这个结局是运行之后才写的。
    # added 为 True 时 uncertain 也为 True；界面应当写"那次运行里还没有"，不说"对不上"
    added: bool = False


# ---------- 汇总 ----------


def build_run_paths(graph: ProjectGraph, records: Iterable[dict]) -> RunPaths:
    """执行记录和测试结果行 → 运行路径（规则见模块说明）。records 可以是 verify.read_records 读出来的。"""
    tests: dict[str, TestRun] = {}
    cache: dict = {}  # walk_record 的缓存：解析过的源码、走图结果
    infos: dict[str, _FnInfo] = {}  # 函数 key → 节点 sid 等

    def test_run(name: str) -> TestRun:
        t = tests.get(name)
        if t is None:
            t = tests[name] = TestRun(name, "", 0.0, 0)
        return t

    for rec in records:
        if "error" in rec:
            continue
        if "fn" in rec:
            walked = walk_record(graph, rec, cache)
            if walked is None:
                continue
            key = rec["fn"]
            info = infos.get(key)
            if info is None:
                info = infos[key] = _FnInfo(graph.functions[key])
            callees = {graph.functions[c[0]].sid or c[0] for c in rec.get("calls", ()) if c[0] in graph.functions}
            indirect = {graph.functions[c[0]].sid or c[0] for c in _pairs(rec.get("icalls")) if c[0] in graph.functions}
            _add_execution(test_run(rec.get("test", "")), info, rec, walked, callees, indirect)
        elif "outcome" in rec:
            t = test_run(rec["test"])
            t.cases += rec.get("n", 1)
            t.seconds += rec["seconds"]
            if _RANK.get(rec["outcome"], 0) > _RANK[t.outcome]:
                t.outcome = rec["outcome"]
            exc = rec.get("exc")
            if rec["outcome"] in ("failed", "error") and isinstance(exc, str) and exc:
                t.exc = t.exc or exc
                if exc not in t.excs:
                    t.excs = sorted([*t.excs, exc])
    by_sid = {g.sid or key: g for key, g in graph.functions.items()}
    function_dups = sorted(b for b, c in Counter(_base(s) for s in by_sid).items() if c > 1)
    dup_set = set(function_dups)
    keep = {fsid for t in tests.values() for fsid in t.functions} | {s for s in by_sid if _base(s) in dup_set}
    shapes = {s: FunctionShape(function_shape(by_sid[s]), _dups(by_sid[s])) for s in sorted(keep)}
    return RunPaths(sorted(tests.values(), key=lambda t: t.test), VERSION, shapes, function_dups)


class _FnInfo:
    """一个函数在汇总时用到的图上信息。"""

    def __init__(self, g: FunctionGraph):
        self.fsid = g.sid or g.key
        self.ids = {n.id: n.sid or n.id for n in g.nodes}  # 节点 id → 节点 sid
        self.ends = {n.id for n in g.nodes if n.type == "end"}


def _pairs(v: object) -> list[list]:
    """记录里的 icalls：[[被调函数 key, 行号], ...]；写坏了的项（核对不看这个字段，读的时候也不检查）跳过。"""
    if not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, list) and len(c) == 2 and isinstance(c[0], str)]


def _add_execution(t: TestRun, info: _FnInfo, rec: dict, walked: Walked, callees: set[str], indirect: set[str]) -> None:
    f = t.functions.get(info.fsid)
    if f is None:
        f = t.functions[info.fsid] = FunctionRun(info.fsid, 0)
    n = rec.get("n", 1)
    f.executions += n
    for callee in callees:
        f.calls[callee] = f.calls.get(callee, 0) + n
    for callee in indirect:
        f.icalls[callee] = f.icalls.get(callee, 0) + n
    if not walked.on_graph:
        f.off_graph += n
    lines, ns = rec["lines"], rec.get("ns")
    timed = isinstance(ns, list) and len(ns) == len(lines)
    f.has_time = f.has_time or timed
    path = [info.ids[nid] for nid in walked.nodes]
    times = _segment_times(walked, info.ends, ns, len(lines)) if timed else [0] * len(path)
    for s, t_ns in zip(path, times):
        stat = f.nodes.get(s)
        if stat is None:
            stat = f.nodes[s] = NodeStat()
        stat.visits += n
        stat.ns += t_ns
    for a, b in pairwise(path):
        k = f"{a}|{b}"
        f.edges[k] = f.edges.get(k, 0) + n
    stop = info.ids.get(walked.exit_node, "") if walked.exit_node else ""
    k = f"{stop}|{rec['exit']}|{rec.get('exc') or ''}"
    f.exits[k] = f.exits.get(k, 0) + n
    if not f.first_path:
        f.first_path = path if len(path) <= FIRST_PATH_MAX else [*path[:FIRST_PATH_MAX - 1], _MORE]


def _segment_times(walked: Walked, ends: set[str], ns: list[int], length: int) -> list[int]:
    """经过的每个节点这一次的耗时（见模块说明）：从同一个位置开始的几个节点（同一行上的）是一组，这一组的一段算在
    其中最后一个不是结局的节点上，全是结局时算在最后一个上。开始节点自己一段，不和别的节点成组。"""
    idx, nodes = walked.line_index, walked.nodes
    out = [0] * len(nodes)
    k = 0
    while k < len(nodes):
        j = k
        if k:
            while j + 1 < len(nodes) and idx[j + 1] == idx[k]:
                j += 1
        hi = idx[j + 1] if j + 1 < len(nodes) else length
        if hi > idx[k]:
            owner = next((m for m in range(j, k - 1, -1) if nodes[m] not in ends), j)
            out[owner] += sum(ns[idx[k]:hi])
        k = j + 1
    return out


def entry_functions(test: TestRun) -> list[str]:
    """这个测试的入口函数：执行过、却没有任何一个执行过的函数记录过（直接或间接）调用它的函数 sid（自己调用自己不算），
    按 sid 排序。局限见模块说明。"""
    called = {callee for fsid, f in test.functions.items() for callee in (*f.calls, *f.icalls) if callee != fsid}
    return sorted(fsid for fsid in test.functions if fsid not in called)


# ---------- 叠到图上 ----------


def function_shape(fg: FunctionGraph) -> str:
    """函数的形状：节点 sid（没有 sid 的旧图用节点 id）按图里的顺序用换行连起来的 sha1（十六进制）。形状相同 ⇔ 节点
    的集合和顺序都没变（只改了格式、注释、函数位置之类），这时 #n 指的还是同一个节点。"""
    return hashlib.sha1("\n".join(n.sid or n.id for n in fg.nodes).encode("utf-8")).hexdigest()


def locate_function(graph: ProjectGraph, paths: RunPaths, run_sid: str) -> FunctionGraph | None:
    """运行时的函数 sid 是这张图（可以是运行之后重新分析的新图）里的哪个函数；放不上时为 None。规则见模块说明
    "叠到图上"第 1 条。"""
    return _Placer(graph, paths).locate(run_sid)


def match_function_run(fg: FunctionGraph, run: FunctionRun, shape: FunctionShape | None) -> tuple[dict[str, str], int]:
    """把一个函数的运行汇总叠到函数图 fg 上：(运行里的节点 sid → fg 里的节点 id, 配不上的 sid 个数)。shape 是运行时
    这个函数的样子（paths.shapes.get(运行时的函数 sid)，旧数据为 None）。规则见模块说明"叠到图上"第 2 条。"""
    return _match(fg, run, shape, function_shape(fg), _base_counts(fg))


def locate_test_run(graph: ProjectGraph, paths: RunPaths,
                    test: TestRun) -> tuple[dict[str, tuple[FunctionGraph, dict[str, str]]], int]:
    """一个测试的运行路径叠到这张图上：({图里的函数 key: (函数图, 运行 sid → 节点 id)}, 对不上的处数——放不上的函数
    个数加放上了的函数里配不上的节点 sid 个数)。规则见模块说明"叠到图上"第 3 条。"""
    placer = _Placer(graph, paths)
    out: dict[str, tuple[FunctionGraph, dict[str, str]]] = {}
    missing = 0
    for fsid, run in test.functions.items():
        fg = placer.locate(fsid)
        if fg is None:
            missing += 1
            continue
        mapping, unmatched = placer.match(fg, run, fsid)
        out[fg.key] = (fg, mapping)
        missing += unmatched
    return out, missing


class _Placer:
    """一张图上的放置：按基础 sid 分组的函数、各函数的形状（算一次）。"""

    def __init__(self, graph: ProjectGraph, paths: RunPaths):
        self.paths = paths
        self.dups = set(paths.function_dups)
        self.groups: dict[str, list[FunctionGraph]] = {}
        for key, g in graph.functions.items():
            self.groups.setdefault(_base(g.sid or key), []).append(g)
        for group in self.groups.values():
            group.sort(key=lambda g: _nth(g.sid or g.key))
        self._shapes: dict[str, str] = {}
        self._located: dict[str, FunctionGraph | None] = {}

    def shape(self, fg: FunctionGraph) -> str:
        s = self._shapes.get(fg.key)
        if s is None:
            s = self._shapes[fg.key] = function_shape(fg)
        return s

    def locate(self, run_sid: str) -> FunctionGraph | None:
        if run_sid not in self._located:
            self._located[run_sid] = self._locate(run_sid)
        return self._located[run_sid]

    def _locate(self, run_sid: str) -> FunctionGraph | None:
        base = _base(run_sid)
        group = self.groups.get(base, [])
        if base not in self.dups and len(group) <= 1:
            return group[0] if group and (group[0].sid or group[0].key) == run_sid else None
        stored = self.paths.shapes.get(run_sid)
        if stored is None:
            return None
        then = sorted(((s, v.shape) for s, v in self.paths.shapes.items() if _base(s) == base),
                      key=lambda item: _nth(item[0]))
        now = [(g.sid or g.key, self.shape(g)) for g in group]
        if base in self.dups and then == now:  # 这一组没变：#n 还指同一个定义
            return next(g for g in group if (g.sid or g.key) == run_sid)
        if stored.shape in TRIVIAL_SHAPES:  # 空形状认不出是哪一个
            return None
        hits = [g for g in group if self.shape(g) == stored.shape]
        twins = [s for s, shape in then if shape == stored.shape and s != run_sid]
        return hits[0] if len(hits) == 1 and not twins else None

    def match(self, fg: FunctionGraph, run: FunctionRun, run_sid: str) -> tuple[dict[str, str], int]:
        return _match(fg, run, self.paths.shapes.get(run_sid), self.shape(fg), _base_counts(fg))


# ---------- 没走到的结局 ----------


def uncovered_outcomes(graph: ProjectGraph, r: RunPaths, line: FlowLine | None = None,
                       then: ProjectGraph | None = None) -> list[Outcome]:
    """功能线涉及的函数里（line 为 None 时：所有功能线涉及的函数，按功能线的顺序去重），没有任何测试经过、也没有
    停在那里的结局节点，按函数、再按节点在图里的顺序。"未捕获异常退出"不算（图上没有进它的连线），"执行到函数末尾"
    算。运行按模块说明"叠到图上"的规则放到这张图上（运行之后重新分析过的新图也能用）。给出 uncertain=True、不算
    走到也不算没走到的：这个函数属于同名的一组（基础函数 sid 运行时或现在有不止一个定义）而这一组有运行放不上；或者
    放上了的运行形状和现在不同，而这个结局属于同文本的一组（基础节点 sid 在运行时的 dups 里、或现在重名）。没有
    运行过的函数照常都是没走到。截断的记录不知道停在哪里：一个函数的执行全被截断时，它以 return 离开的结局也会列在
    这里。
    then：运行时的那张图（graph 是运行之后重新分析的新图时给）。给了时，运行时的图里没有的函数（按函数 sid）、有这个
    函数却没有的结局（按节点 sid）也是 uncertain、并且 added=True——那次运行时它们还不存在，说"没有测试走到"是冤枉；不给时分不出来，
    这些都算没走到。"""
    placer = _Placer(graph, r)
    known = None if then is None else {g.sid or key: {n.sid or n.id for n in g.nodes}
                                       for key, g in then.functions.items()}
    placed: dict[str, list[tuple[FunctionRun, str]]] = {}  # 图里的函数 key → [(运行汇总, 运行时的函数 sid)]
    lost: set[str] = set()  # 有运行放不上的基础函数 sid
    for t in r.tests:
        for fsid, f in t.functions.items():
            fg = placer.locate(fsid)
            if fg is None:
                lost.add(_base(fsid))
            else:
                placed.setdefault(fg.key, []).append((f, fsid))
    out: list[Outcome] = []
    for key in _line_functions(graph, line):
        g = graph.functions[key]
        fsid = g.sid or key
        covered: set[str] = set()  # 走到的节点 id
        unsure: set[str] = set()  # 说不准的基础节点 sid
        for f, run_sid in placed.get(key, ()):
            mapping, _ = placer.match(g, f, run_sid)
            covered.update(mapping.values())
            stored = r.shapes.get(run_sid)
            if stored is None or stored.shape != placer.shape(g):
                unsure.update(stored.dups if stored is not None else ())
                unsure.update(b for b, c in _base_counts(g).items() if c > 1)
        new_fn = known is not None and fsid not in known
        whole = _base(fsid) in lost or new_fn
        there = known.get(fsid) if known is not None else None  # 运行时这个函数的节点 sid；None：不知道
        for n in g.nodes:
            if n.type == "end" and n.end_kind != "exception" and n.id not in covered:
                nsid = n.sid or n.id
                added = new_fn or (there is not None and nsid not in there)
                out.append(Outcome(fsid, nsid, n.title, n.end_kind or "", g.file, n.line,
                                   whole or _base(nsid) in unsure or added, added))
    return out


def covered_summary(graph: ProjectGraph, r: RunPaths) -> dict:
    """{"functions_run": 执行过、放得到这张图上的函数个数（不管在不在功能线上）, "functions_on_lines": 功能线涉及的函数个数,
    "outcomes": 这些函数的结局个数（不含未捕获异常退出）, "outcomes_covered": 其中确定有测试走到的, "tests": 测试个数
    （不含测试之外的那一项）, "failed": 失败或出错的测试个数, "outcomes_uncertain": 配不上、说不准的结局个数（见
    uncovered_outcomes）}。outcomes = outcomes_covered + 没走到的 + outcomes_uncertain。"""
    on_lines = _line_functions(graph, None)
    placer = _Placer(graph, r)
    run = {fsid for t in r.tests for fsid in t.functions}
    outcomes = sum(1 for key in on_lines for n in graph.functions[key].nodes
                   if n.type == "end" and n.end_kind != "exception")
    left = uncovered_outcomes(graph, r)
    return {"functions_run": sum(1 for fsid in run if placer.locate(fsid) is not None),
            "functions_on_lines": len(on_lines), "outcomes": outcomes,
            "outcomes_covered": outcomes - len(left),
            "tests": sum(1 for t in r.tests if t.test), "failed": sum(1 for t in r.tests if t.outcome in ("failed", "error")),
            "outcomes_uncertain": sum(1 for o in left if o.uncertain)}


def _line_functions(graph: ProjectGraph, line: FlowLine | None) -> list[str]:
    if line is not None:
        return line_functions(graph, line)
    return list(dict.fromkeys(key for ln in graph.lines for key in line_functions(graph, ln)))


def _base(sid: str) -> str:
    """去掉重名编号 #n 的基础 sid。"""
    head, sep, tail = sid.rpartition("#")
    return head if sep and tail.isdigit() else sid


def _nth(sid: str) -> int:
    """重名编号：sid 结尾的 #n 的 n，没有时为 1。"""
    _, sep, tail = sid.rpartition("#")
    return int(tail) if sep and tail.isdigit() else 1


def _base_counts(fg: FunctionGraph) -> Counter[str]:
    return Counter(_base(n.sid or n.id) for n in fg.nodes)


def _dups(fg: FunctionGraph) -> list[str]:
    return sorted(b for b, c in _base_counts(fg).items() if c > 1)


def _run_sids(run: FunctionRun) -> set[str]:
    """运行里提到的节点 sid：经过的节点和停点（截断的记录停点为空，不算）。"""
    out = set(run.nodes)
    out.update(s for s in (k.split("|", 1)[0] for k in run.exits) if s)
    return out


def _match(fg: FunctionGraph, run: FunctionRun, stored: FunctionShape | None, shape: str,
           counts: Counter[str]) -> tuple[dict[str, str], int]:
    by_sid = {n.sid or n.id: n.id for n in fg.nodes}
    sids = _run_sids(run)
    if stored is not None and stored.shape == shape:
        mapping = {s: by_sid[s] for s in sids if s in by_sid}
    else:
        dups = set(stored.dups) if stored is not None else set()
        mapping = {s: by_sid[s] for s in sids if s in by_sid and _base(s) not in dups and counts[_base(s)] == 1}
    return mapping, len(sids) - len(mapping)


# ---------- JSON ----------


def run_paths_to_json(r: RunPaths) -> dict:
    """functions 按函数 sid 排序（与汇总时遇到的先后无关）。"""
    tests = []
    for t in r.tests:
        d = asdict(t)
        d["functions"] = {fsid: d["functions"][fsid] for fsid in sorted(d["functions"])}
        for f in d["functions"].values():  # 空的不写：省体积
            for name in ("calls", "icalls"):
                if not f[name]:
                    del f[name]
        if not d["exc"]:
            del d["exc"]
        if not d["excs"]:
            del d["excs"]
        tests.append(d)
    return {"version": r.version, "tests": tests, "shapes": {s: asdict(r.shapes[s]) for s in sorted(r.shapes)},
            "function_dups": list(r.function_dups)}


def run_paths_from_json(d: dict) -> RunPaths:
    """run_paths_to_json 的反过程；格式不对时抛 ValueError（中文，说明哪里不对）。"""
    if not isinstance(d, dict):
        raise _bad("不是 JSON 对象")
    if d.get("version") != VERSION or type(d.get("version")) is not int:
        raise _bad(f"版本 {d.get('version')!r} 不是 {VERSION}")
    tests = d.get("tests")
    if not isinstance(tests, list):
        raise _bad("没有 tests 列表")
    shapes, function_dups = d.get("shapes", {}), d.get("function_dups", [])  # 没有这两项的旧数据
    if not (isinstance(shapes, dict) and all(isinstance(v, dict) and isinstance(v.get("shape"), str)
                                             and _strings(v.get("dups")) for v in shapes.values())):
        raise _bad("shapes 不对")
    if not _strings(function_dups):
        raise _bad("function_dups 不对")
    return RunPaths([_test_from(t, i) for i, t in enumerate(tests)], VERSION,
                    {s: FunctionShape(v["shape"], list(v["dups"])) for s, v in shapes.items()}, list(function_dups))


def _strings(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _bad(where: str) -> ValueError:
    return ValueError(f"运行路径格式不对：{where}")


def _count(v: object) -> bool:
    return type(v) is int and v >= 0


def _test_from(t: object, i: int) -> TestRun:
    where = f"第 {i + 1} 个测试"
    if not isinstance(t, dict):
        raise _bad(f"{where}不是对象")
    if not isinstance(t.get("test"), str) or t.get("outcome") not in _RANK:
        raise _bad(f"{where}的 test / outcome 不对")
    seconds = t.get("seconds")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0 or not _count(t.get("cases")):
        raise _bad(f"{where}的 seconds / cases 不对")
    functions = t.get("functions")
    if not isinstance(functions, dict):
        raise _bad(f"{where}没有 functions")
    exc, excs = t.get("exc", ""), t.get("excs", [])  # 空的不写出来；更早的数据没有
    if not (isinstance(exc, str) and _strings(excs)):
        raise _bad(f"{where}的 exc / excs 不对")
    return TestRun(t["test"], t["outcome"], float(seconds), t["cases"],
                   {fsid: _function_from(f, fsid, where) for fsid, f in functions.items()}, exc, list(excs))


def _function_from(f: object, fsid: str, where: str) -> FunctionRun:
    where = f"{where}的函数 {fsid}"
    if not isinstance(f, dict) or f.get("function") != fsid:
        raise _bad(f"{where}不是对象或 function 对不上")
    if not (_count(f.get("executions")) and _count(f.get("off_graph")) and type(f.get("has_time")) is bool):
        raise _bad(f"{where}的 executions / off_graph / has_time 不对")
    nodes, edges, exits, first = f.get("nodes"), f.get("edges"), f.get("exits"), f.get("first_path")
    if not (isinstance(nodes, dict) and all(isinstance(s, dict) and set(s) == {"visits", "ns"}
                                            and _count(s["visits"]) and _count(s["ns"]) for s in nodes.values())):
        raise _bad(f"{where}的 nodes 不对")
    for name, counts in (("edges", edges), ("exits", exits)):
        if not (isinstance(counts, dict) and all(_count(c) for c in counts.values())):
            raise _bad(f"{where}的 {name} 不对")
    if not _strings(first):
        raise _bad(f"{where}的 first_path 不对")
    calls, icalls = f.get("calls", {}), f.get("icalls", {})  # 空的不写出来；更早的数据没有
    for name, counts in (("calls", calls), ("icalls", icalls)):
        if not (isinstance(counts, dict) and all(_count(c) for c in counts.values())):
            raise _bad(f"{where}的 {name} 不对")
    # 第 1 轮的数据每个函数自带 shape / dups：忽略（运行时的样子现在在运行一级）
    return FunctionRun(fsid, f["executions"], {s: NodeStat(v["visits"], v["ns"]) for s, v in nodes.items()},
                       dict(edges), dict(exits), list(first), f["off_graph"], f["has_time"], dict(calls), dict(icalls))
