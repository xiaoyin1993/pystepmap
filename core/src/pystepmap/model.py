"""图的数据结构与 JSON 读写。后续所有任务产出或消费这里定义的类型。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from typing import Literal

SCHEMA_VERSION = 1

NodeType = Literal["start", "step", "cond", "loop", "catch", "end"]
EdgeKind = Literal["next", "yes", "no", "case", "loop_each", "loop_back", "loop_exit", "break", "raise", "finally"]
OutcomeKind = Literal["ok", "skipped", "failed"]  # 业务结局（pystepkit outcome 的 kind、配置 outcome_kinds 的取值）
EndKind = Literal[OutcomeKind, "exception"]
FnKind = Literal["route", "task", "workflow", "step", "condition", "function"]
Resolution = Literal["exact", "guessed", "unresolved", "external"]


def fn_key(file: str, qualname: str, line: int) -> str:
    """函数身份：相对路径 + 完整限定名 + 定义行，如 "a/b.py::Q.name@12"。"""
    return f"{file}::{qualname}@{line}"


def parse_key(key: str) -> tuple[str, str, int]:
    file, rest = key.split("::", 1)
    qualname, line = rest.rsplit("@", 1)
    return file, qualname, int(line)


def numbered(names: Iterable[str]) -> list[str]:
    """按给定顺序给重名的编号：第一个原样，之后的依次加 #2、#3……；编号跳过已被别的名字占用的，结果互不相同。"""
    names = list(names)
    taken = set(names)
    last: dict[str, int] = {}  # 名字 -> 已用到的编号
    out: list[str] = []
    for name in names:
        if name not in last:
            last[name] = 1
            out.append(name)
            continue
        n = last[name] + 1
        while f"{name}#{n}" in taken:
            n += 1
        last[name] = n
        taken.add(f"{name}#{n}")
        out.append(f"{name}#{n}")
    return out


def function_sids(keys: Iterable[str]) -> dict[str, str]:
    """函数 key -> 稳定标识（见 FunctionGraph.sid）：同一文件里同一限定名的多处定义按定义行顺序编号。"""
    ordered = sorted(keys, key=parse_key)  # (文件, 限定名, 定义行)
    return dict(zip(ordered, numbered("::".join(parse_key(k)[:2]) for k in ordered)))


@dataclass
class CallRef:
    name: str  # 显示名，如 "svc.match_with_auth"
    target: str | None  # 被调用函数的 key
    resolution: Resolution
    is_dispatch: bool = False  # 这个调用本身是派发，target 是被派发的任务
    conditional: bool = False  # 位于三元表达式或 and/or 短路里，或是 match 的 case 守卫里的调用
    dispatches: list[str] = field(default_factory=list)  # target 内部（任意深度）会派发的任务 key


@dataclass
class Node:
    id: str  # 函数图内唯一："n1"、"n2"……（按生成顺序编号，前面多一个节点后面的都变）
    type: NodeType
    title: str
    # 稳定标识，函数图内唯一，不含行号："<节点类型>:<语句头部记号流的 sha1 前 8 位>"。语句头部：步骤是整条语句
    # （with 到最后一个上下文表达式为止，折叠掉的 if / match 到条件 / 主语为止），判断、循环是 if / elif / match /
    # for / while 到条件（主语、迭代对象）为止，捕获是 except 到异常类型为止，结局是整条 return / raise。记号流：头部
    # 源码分词，去掉注释、换行和缩进记号（with 还去掉每一项的 as 目标），其余记号之间用一个空格连接——换行重排、改缩进、
    # 改注释、给 with 的目标改名不改变 sid，改引号、加减末尾逗号会改变。标题、文档注释不参与。同一函数内相同的按源码
    # 位置从第二个起加 #2、#3。
    # 隐含节点用固定名字：start、end:implicit（执行到函数末尾）、end:uncaught（未捕获异常退出）。
    # 已知性质：编号随前面的同文本语句变化——删掉第一条，原来的 #2 变成不带后缀的，原来的 #3 变成 #2。
    sid: str = ""
    subtitle: str = ""  # 函数名或条件原文
    line: int = 0  # 语句起始行；隐含节点为 0
    end_line: int = 0  # 语句头部的结束行（if 取条件的结束行，match 取主语的结束行）
    calls: list[CallRef] = field(default_factory=list)
    end_kind: EndKind | None = None
    fields: dict[str, str] = field(default_factory=dict)  # 结局携带的字段
    passthrough: str | None = None  # 转交返回：被调用函数的 key
    # 显示用的 title / subtitle 被截断了（标题、步骤副标题超过 60 字，判断 / 循环 / 捕获的源码超过 80 字）时，同样清理
    # （单行、空白合并）但不截断的完整文字，最长 2000 字；没被截断时为空串。只在有值时写进 JSON
    title_full: str = ""
    subtitle_full: str = ""
    # if 的分支里没有节点（没有有意义的调用）、但改了变量时，图上看不到这条分支做了什么——这里记下来：分支（"yes" /
    # "no"）-> 分支里的赋值语句原文（几句用"；"连接，超过 80 字截断）。判断节点上是折叠掉的那一条分支；两条分支都折叠
    # 掉、判断变成步骤（条件里有调用）时记在那个步骤上。只在有值时写进 JSON
    quiet: dict[str, str] = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    label: str = ""
    # 判断节点的分支出边标明是哪个分支（kind 改成 break / loop_back / finally 时不变）：if / elif 为 "yes" / "no"；match
    # 为该 case 的 label（模式原文，带守卫时"模式 if 守卫"），没有兜底分支时越过 match 的那条为 "else"。其余连线（判断
    # 节点的 raise 出边、循环节点的 loop_each / loop_exit 等）为空。只在非空时写进 JSON
    branch: str = ""
    # case 连线的 label（模式原文）被截断了（超过 80 字）时的完整模式，最长 2000 字；否则为空串，只在有值时写进 JSON。
    # branch 照旧是截断后的 label
    label_full: str = ""


@dataclass
class UnresolvedCall:
    line: int  # 调用表达式的起始行
    name: str  # 显示名，如 "svc.match"


@dataclass
class FunctionGraph:
    key: str
    file: str
    qualname: str
    line: int
    end_line: int
    kind: FnKind
    title: str
    is_async: bool
    # 稳定标识，整个图里唯一，不含行号："相对路径::限定名"；同一文件里同一限定名有多处定义时（if 两个分支各定义
    # 一次、重复定义），按定义行顺序从第二处起加 #2、#3。key 用于记录与核对，sid 用于跨两次分析找回同一个函数
    sid: str = ""
    marked: bool = False  # 用了 pystepkit 标记
    group: str | None = None
    route: str | None = None  # "POST /pubsub"
    retries: bool = False  # 任务声明了失败重试
    parent: str | None = None  # 内部函数：外层函数的 key
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    call_targets: list[str] = field(default_factory=list)  # 函数体里每个解析到的项目内调用（含被折叠掉的、构造调用的 __init__）
    unresolved: list[UnresolvedCall] = field(default_factory=list)  # 函数体里每个无法判断目标的调用（不论是否进了节点），按 (行, 名字) 去重
    # 以下几项只在有值时写进 JSON
    rule: str | None = None  # 识别出入口 / 标记的规则集名（fastapi、dbos、pystepkit……）
    name: str | None = None  # pystepkit workflow 的 name=
    version: str | None = None  # pystepkit workflow 的 version=
    title_full: str = ""  # title 被截断了（超过 60 字）时的完整标题，最长 2000 字；否则为空串，只在有值时写进 JSON


@dataclass
class LineTask:
    function: str  # 任务函数的 key
    goto: bool = False  # True：本条功能线里已出现过，不再展开
    children: list[LineTask] = field(default_factory=list)


@dataclass
class FlowLine:
    id: str  # 根任务函数的 key
    title: str
    group: str
    kind: FnKind
    root: LineTask
    sid: str = ""  # 稳定标识："line:" 加根任务函数的 sid；同一根函数有多条线时按列表顺序从第二条起加 #2
    title_full: str = ""  # 标题取自被截断的函数标题时是函数的 title_full，否则为空串；只在有值时写进 JSON


@dataclass
class SelfCheck:
    unreachable: list[tuple[str, str]]  # (函数 key, 节点 id)
    dead_ends: list[tuple[str, str]]
    unresolved_calls: int
    guessed_calls: int
    skipped_files: list[tuple[str, str]]  # (相对路径, 原因)


@dataclass
class ProjectGraph:
    schema_version: int
    root: str
    functions: dict[str, FunctionGraph]
    lines: list[FlowLine]
    common_tasks: list[str]
    selfcheck: SelfCheck
    file_hashes: dict[str, str]  # 相对路径 -> sha256
    sids: dict[str, str] = field(default_factory=dict)  # 函数 sid -> 函数 key，按 sid 排序


_OPTIONAL_FUNCTION_FIELDS = ("rule", "name", "version")


def _drop_empty(d: dict, *names: str) -> None:
    for name in names:
        if not d[name]:
            del d[name]


def to_json(graph: ProjectGraph) -> str:
    data = asdict(graph)
    for fn in data["functions"].values():
        for name in _OPTIONAL_FUNCTION_FIELDS:
            if fn[name] is None:
                del fn[name]
        _drop_empty(fn, "title_full")
        for node in fn["nodes"]:
            _drop_empty(node, "title_full", "subtitle_full", "quiet")
        for edge in fn["edges"]:
            _drop_empty(edge, "branch", "label_full")
    for line in data["lines"]:
        _drop_empty(line, "title_full")
    return json.dumps(data, ensure_ascii=False, indent=2)


def _known(cls, d: dict) -> dict:
    """只留 cls 认识的字段：新版本加的字段，旧的读取方忽略。"""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _line_task(d: dict) -> LineTask:
    return LineTask(
        function=d["function"],
        goto=d["goto"],
        children=[_line_task(c) for c in d["children"]],
    )


def _function(d: dict) -> FunctionGraph:
    d = _known(FunctionGraph, d)
    d["nodes"] = [
        Node(**{**_known(Node, n), "calls": [CallRef(**_known(CallRef, c)) for c in n["calls"]]}) for n in d["nodes"]
    ]
    d["edges"] = [Edge(**_known(Edge, e)) for e in d["edges"]]
    d["unresolved"] = [UnresolvedCall(**_known(UnresolvedCall, u)) for u in d.get("unresolved", [])]
    return FunctionGraph(**d)


def from_json(text: str) -> ProjectGraph:
    """读回 to_json 的结果。不认识的字段忽略，缺的可选字段（旧图没有的 sid、branch、title_full……）取默认值；
    顶层不是对象、版本不对时抛 ValueError。"""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("图文件的顶层必须是 JSON 对象")  # noqa: TRY004  调用方按 ValueError 接「图文件读不了」
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"不支持的图 schema_version：{version}（需要 {SCHEMA_VERSION}）")
    sc = data["selfcheck"]
    return ProjectGraph(
        schema_version=version,
        root=data["root"],
        functions={k: _function(v) for k, v in data["functions"].items()},
        lines=[FlowLine(**{**_known(FlowLine, ln), "root": _line_task(ln["root"])}) for ln in data["lines"]],
        common_tasks=list(data["common_tasks"]),
        selfcheck=SelfCheck(
            unreachable=[tuple(p) for p in sc["unreachable"]],
            dead_ends=[tuple(p) for p in sc["dead_ends"]],
            unresolved_calls=sc["unresolved_calls"],
            guessed_calls=sc["guessed_calls"],
            skipped_files=[tuple(p) for p in sc["skipped_files"]],
        ),
        file_hashes=dict(data["file_hashes"]),
        sids=dict(data.get("sids", {})),  # 没有 sid 的旧图：函数、节点、功能线的 sid 为空串
    )
