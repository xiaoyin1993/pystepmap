"""函数图：把一个函数的语法树变成节点和连线。

分两段：先把语句列表建成结构树，同时折叠掉没有内容的分支；再从结构树生成节点和连线。
生成时维护"悬空的出边"——起点已经确定、还没接到下一个节点上的连线。
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from itertools import count
from typing import get_args

from .config import ProjectConfig
from .external import ExternalCalls
from .index import FunctionInfo, ProjectIndex
from .model import (
    CallRef,
    Edge,
    EdgeKind,
    EndKind,
    FunctionGraph,
    Node,
    NodeType,
    OutcomeKind,
    UnresolvedCall,
    numbered,
)
from .resolve import Resolver, dotted_name_of, unwrap_thread_call
from .rules import ActiveRules, pystepkit_name
from .titles import FULL_LIMIT, clean, clean_full, comment_above, cut, first_sentence

TITLE_LIMIT = 60  # 标题、步骤副标题的最大长度（被截断时完整的另记，见 Node.title_full）
COND_LIMIT = 80  # 判断、循环、捕获节点副标题（源码）的最大长度
NO_MATCH_LABEL = "都不匹配"  # match 没有兜底分支时，越过 match 的那条 case 连线的标签
NO_MATCH_BRANCH = "else"  # 同一条连线的 branch
FELL_OFF_TITLE = "执行到函数末尾"
UNCAUGHT_TITLE = "未捕获异常退出"
UNCAUGHT_RETRY_TITLE = "未捕获异常退出（任务会重试）"
_OUTCOME_KINDS = get_args(OutcomeKind)  # pystepkit outcome(kind=...) 的取值
_BRANCH_LABELS = {"yes": "是", "no": "否"}  # 出边改成 break / loop_back / raise 时，原来的分支写进 label

# 嵌套定义：内部函数单独成图，类体不属于本函数的流程
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# ---------- 结构树 ----------


_Pos = tuple[int, int]  # 头部原文里的位置：行（从 1 起）、字符列，同 tokenize 的记法


def _full_form(text: str, whole: str) -> str:
    """节点标题 clean(text, TITLE_LIMIT) 的完整形式：whole 是 text 没有只取第一句时的原文（一样时就是 text）。
    显示的标题和完整的一样（或者只差句末的标点）时为 ""。"""
    full = cut(clean(whole), FULL_LIMIT)
    return "" if full.rstrip("。；;. ") == clean(text, TITLE_LIMIT).rstrip("。；;. ") else full



@dataclass(frozen=True)
class _Head:
    """节点代表的语句头部（见 Node.sid）：起始位置（行、UTF-8 字节列）和源码原文。skip：头部里不参与 sid 的 as 目标，
    每项是（它前面的上下文表达式的终点，目标的终点），见 _head_tokens。"""

    line: int
    col: int
    text: str
    skip: tuple[tuple[_Pos, _Pos], ...] = ()


@dataclass
class _Step:
    line: int
    end_line: int
    calls: list[CallRef]
    head: _Head
    quiet: dict[str, str] = field(default_factory=dict)  # 折叠掉的 if 变成的步骤：见 Node.quiet


@dataclass
class _If:
    stmt: ast.If
    calls: list[CallRef]  # 条件里有意义的调用
    body: list[_Item]
    orelse: list[_Item]
    quiet: dict[str, str] = field(default_factory=dict)  # 见 Node.quiet


@dataclass
class _Return:
    stmt: ast.Return


@dataclass
class _Case:
    label: str  # 进入分支的连线标签：模式原文，带守卫时为"模式 if 守卫"（超长截断）
    label_full: str  # label 被截断时的完整形式，否则为 ""（见 Edge.label_full）
    body: list[_Item]


@dataclass
class _Match:
    stmt: ast.Match
    calls: list[CallRef]  # 主语和各 case 守卫里有意义的调用
    cases: list[_Case]
    exhaustive: bool  # 有兜底分支（case _、不带守卫的捕获等必然匹配的模式），没有"都不匹配"的路径


@dataclass
class _Raise:
    stmt: ast.Raise


@dataclass
class _Break:
    pass


@dataclass
class _Continue:
    pass


@dataclass
class _Loop:
    stmt: ast.For | ast.AsyncFor | ast.While
    calls: list[CallRef]  # 循环头里有意义的调用
    body: list[_Item]
    orelse: list[_Item]


@dataclass
class _Handler:
    handler: ast.ExceptHandler
    body: list[_Item]  # 为空（只写日志等）时不画捕获节点


@dataclass
class _Try:
    body: list[_Item]
    handlers: list[_Handler]
    orelse: list[_Item]
    final: list[_Item]


_Item = _Step | _If | _Return | _Match | _Raise | _Break | _Continue | _Loop | _Try


@dataclass(frozen=True)
class _Out:
    """悬空的出边：src 节点的这条连线接到下一个生成的节点上。"""

    src: str
    kind: EdgeKind
    label: str = ""
    branch: str = ""  # 判断节点的分支出边：是哪个分支（见 Edge.branch）；改 kind 时随出边带着走，改成 raise 时清空
    jump: int = 0  # 非 0：一组正穿过 finally 体的 break / continue 的出边（编号认出是哪一组，见 _emit_finally）
    label_full: str = ""  # case 出边的 label 被截断时的完整形式（见 Edge.label_full）；改 kind 时随出边带着走


@dataclass
class _Jumps:
    """一个有 finally 体的 try：try 体、except、else 里越过它的 break / continue 的出边，等 finally 体生成时接进去。"""

    breaks: list[_Out]
    continues: list[_Out]


@dataclass
class _LoopCtx:
    head: str  # 循环节点 id：continue 和循环体的正常出口回到这里
    breaks: list[_Out]  # 已经过所有 finally 的 break 出边，接到循环之后的节点
    jumped: list[_Out]  # 每个 break / continue 语句的出边（原样记下；连线被去重时也在这里），供 _thrown 用
    finals: list[_Jumps]  # 包住当前位置、在这个循环之内的有 finally 体的 try（正在生成 try 体、except 或 else），内层在后


# ---------- 语法树辅助 ----------


def _walk(root: ast.AST, into_lambda: bool = False) -> Iterator[ast.AST]:
    """root 子树里的节点（先序），不进入嵌套的函数和类定义；是否进入 lambda 由 into_lambda 决定。"""
    stack: list[ast.AST] = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, _SCOPES) or (isinstance(n, ast.Lambda) and not into_lambda):
            continue
        yield n
        stack.extend(reversed(list(ast.iter_child_nodes(n))))


def _iter_calls(root: ast.AST, into_lambda: bool = False) -> Iterator[ast.Call]:
    """root 子树里的调用（先序），范围同 _walk。"""
    return (n for n in _walk(root, into_lambda) if isinstance(n, ast.Call))


def _nested_headers(root: ast.AST) -> Iterator[tuple[ast.expr, bool]]:
    """root 子树里（不进入嵌套定义的体）每个嵌套函数 / 类定义在外层帧执行的部分：(表达式, 是否装饰器)。
    函数是装饰器和参数默认值，类是装饰器、基类和关键字参数。"""
    stack: list[ast.AST] = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield from ((d, True) for d in n.decorator_list)
            if isinstance(n, ast.ClassDef):
                rest = [*n.bases, *(k.value for k in n.keywords)]
            else:
                rest = [*n.args.defaults, *(d for d in n.args.kw_defaults if d is not None)]
            yield from ((e, False) for e in rest)
        elif not isinstance(n, ast.Lambda):
            stack.extend(ast.iter_child_nodes(n))


def _short_circuited(n: ast.AST) -> Iterable[ast.expr]:
    """不一定求值的子表达式：三元表达式的两个分支、and / or 第一个操作数之后的各项、match 的 case 守卫
    （模式匹配上才求值；守卫里的调用记在 match 的判断节点上）。"""
    if isinstance(n, ast.IfExp):
        return (n.body, n.orelse)
    if isinstance(n, ast.BoolOp):
        return n.values[1:]
    if isinstance(n, ast.match_case) and n.guard is not None:
        return (n.guard,)
    return ()


def _irrefutable(p: ast.pattern) -> bool:
    """必然匹配的模式（同 CPython 的判断）：_ 和捕获、它们再加 as、含这样一项的 |。"""
    if isinstance(p, ast.MatchAs):
        return p.pattern is None or _irrefutable(p.pattern)
    return isinstance(p, ast.MatchOr) and any(_irrefutable(alt) for alt in p.patterns)


def _binds(n: ast.AST, name: str) -> bool:
    """n 是否在本作用域里绑定 name（赋值、for / with / except / import / match 的目标、global 等）。"""
    if isinstance(n, ast.Name):
        return n.id == name and not isinstance(n.ctx, ast.Load)
    if isinstance(n, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return n.name == name
    if isinstance(n, ast.MatchMapping):
        return n.rest == name
    if isinstance(n, ast.alias):
        return (n.asname or n.name.split(".")[0]) == name
    if isinstance(n, ast.Global | ast.Nonlocal):
        return name in n.names
    return False


def _by_position(calls: Iterable[ast.Call]) -> list[ast.Call]:
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def _outcome_args(call: ast.Call) -> dict[str, ast.expr | str]:
    """outcome(kind="ok", title=None, **字段) 的实参，以参数名为键；省略 kind 时为 "ok"。"""
    args: dict[str, ast.expr | str] = {"kind": "ok"}
    for name, value in zip(("kind", "title"), call.args):
        args[name] = value
    for kw in call.keywords:
        if kw.arg is not None:
            args[kw.arg] = kw.value
    return args


def _outcome_kind(kind: ast.expr | str) -> EndKind | None:
    """outcome 的 kind 实参就是结局分类，不看配置；不是这三个字符串常量之一时返回 None（交给配置判断）。"""
    value = kind.value if isinstance(kind, ast.Constant) else kind
    return value if value in _OUTCOME_KINDS else None


def _source(lines: list[str], node: ast.AST) -> str:
    """表达式的源码原文。跨行且含 # 时（可能夹着注释）改用 ast.unparse。"""
    try:
        rows = [r.encode() for r in lines[node.lineno - 1: node.end_lineno]]
        rows[-1] = rows[-1][: node.end_col_offset]  # 列号是 UTF-8 字节偏移
        rows[0] = rows[0][node.col_offset:]
        text = "\n".join(r.decode() for r in rows)
    except (AttributeError, IndexError, TypeError, UnicodeDecodeError):  # 没有位置信息或行号越界
        return ast.unparse(node)
    if node.lineno != node.end_lineno and "#" in text:
        return ast.unparse(node)
    return text


# 头部记号流里不算的记号：注释、换行、缩进
_LAYOUT_TOKENS = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                            tokenize.ENDMARKER})


_OPENERS, _CLOSERS = frozenset("([{"), frozenset(")]}")


def _head_tokens(text: str, skip: tuple[tuple[_Pos, _Pos], ...] = ()) -> str:
    """语句头部的记号流（见 Node.sid）：分词，去掉注释、换行和缩进记号，其余记号之间用一个空格连接。换行重排、改缩进、
    改注释都不改变结果；改引号、加减末尾逗号会改变。
    skip 里的每一段（上下文表达式的终点，as 目标的终点）：从段里的 as 起到目标的终点的记号不算，包住目标的括号
    （as (x) 的右括号在目标之后）也不算；as 之前的记号（包住上下文表达式的右括号）照算。
    头部从语句开头起、到一个表达式的结尾止，可能少右括号（with (a(), b()) as x、if (a and b): 的头部）：这时分词到
    片段末尾才报错，之前的记号已经是全部。万一没分到末尾就出错（解析过的源码里不会发生）：退回到去掉已找到的注释、
    连续空白压成一个空格的源码（不再跳过 skip）。"""
    rows = text.split("\n")
    kept: list[str] = []
    comments: list[tuple[int, int]] = []
    end = (1, 0)  # 最后一个记号的终点
    segments = iter(sorted(skip))
    seg = next(segments, None)
    dropping = False  # 正在跳过 as 和它的目标
    unclosed = 0  # 跳过的记号里多出的左括号（包住目标的括号）：目标之后跳过同样多的右括号
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments.append(tok.start)
                continue
            if tok.type in _LAYOUT_TOKENS:
                continue
            end = tok.end
            while seg is not None and tok.start >= seg[1]:  # 走过了这一段
                seg, dropping = next(segments, None), False
            is_op = tok.type == tokenize.OP
            if seg is not None and tok.start >= seg[0] and (dropping or tok.string == "as"):
                dropping = True
                if is_op and tok.string in _OPENERS:
                    unclosed += 1
                elif is_op and tok.string in _CLOSERS:
                    unclosed -= 1
                continue
            if unclosed > 0 and is_op and tok.string in _CLOSERS:
                unclosed -= 1
                continue
            unclosed = 0
            kept.append(tok.string)
    except (tokenize.TokenError, SyntaxError):
        pass
    if end >= (len(rows), len(rows[-1].rstrip())):  # 分到了片段末尾
        return " ".join(kept)
    for row, col in comments:  # 注释到行尾为止：从注释起点截断那一行
        rows[row - 1] = rows[row - 1][:col]
    return " ".join("\n".join(rows).split())


def _text_pos(lines: list[str], start: ast.AST, lineno: int, col: int) -> _Pos:
    """源码位置（行、UTF-8 字节列）在从 start 起的头部原文里的位置（_Pos）。"""
    row = lines[lineno - 1].encode()[:col]
    if lineno == start.lineno:
        row = row[start.col_offset:]
    return (lineno - start.lineno + 1, len(row.decode()))


def _head(lines: list[str], start: ast.AST, end: ast.AST | None = None,
          targets: Iterable[tuple[ast.expr, ast.expr]] = ()) -> _Head:
    """从 start 的起点到 end（省略时同 start）的终点的语句头部。targets：头部里不参与 sid 的 as 目标，每项是
    （上下文表达式，它的 as 目标）。"""
    end = end or start
    try:
        rows = [r.encode() for r in lines[start.lineno - 1: end.end_lineno]]
        rows[-1] = rows[-1][: end.end_col_offset]  # 列号是 UTF-8 字节偏移
        rows[0] = rows[0][start.col_offset:]
        text = "\n".join(r.decode() for r in rows)
        skip = tuple((_text_pos(lines, start, e.end_lineno, e.end_col_offset),
                      _text_pos(lines, start, t.end_lineno, t.end_col_offset)) for e, t in targets)
    except (AttributeError, IndexError, TypeError, UnicodeDecodeError):  # 同 _source：没有位置信息或行号越界
        text, skip = ast.unparse(end), ()
    return _Head(start.lineno, start.col_offset, text, skip)


def _head_sid(node_type: NodeType, head: _Head) -> str:
    """节点的稳定标识（未编号）：类型加头部记号流（_head_tokens）的 sha1 前 8 位。"""
    tokens = _head_tokens(head.text, head.skip)
    return f"{node_type}:{hashlib.sha1(tokens.encode('utf-8')).hexdigest()[:8]}"


def _join_label(a: str, b: str) -> str:
    return " · ".join(x for x in (a, b) if x)


def _code_label(text: str) -> tuple[str, str]:
    """连线标签里的源码原文：合并空白，超长截断。返回（标签，被截断时的完整形式（最长 FULL_LIMIT），否则 ""）。
    不用 clean：它去掉的 ` 和 ** 在模式里有意义（{"k": 1, **rest}）。"""
    s = " ".join(text.split())
    return cut(s, COND_LIMIT), (cut(s, FULL_LIMIT) if len(s) > COND_LIMIT else "")


def _redirect(outs: list[_Out], kind: EdgeKind) -> list[_Out]:
    """把出边改成 kind（break / loop_back / finally / raise），原来的"是 / 否"写进 label（case 连线的 label 本来就是
    模式，照旧，label_full 也照旧）。branch 不变；改成 raise 时清空：异常连线不是分支。异常连线（raise）保持原样；
    改成其他类型时内层循环的 loop_exit 也保持原样：每个循环节点都要有自己的 loop_exit。"""
    keep = ("raise",) if kind == "raise" else ("raise", "loop_exit")
    return [o if o.kind in keep else replace(o, kind=kind, label=_join_label(o.label, _BRANCH_LABELS.get(o.kind, "")),
                                             branch="" if kind == "raise" else o.branch)
            for o in outs]


def _loop_head(lines: list[str], st: ast.For | ast.AsyncFor | ast.While) -> str:
    """循环头源码，如 "for i in range(n)"、"while x"。"""
    if isinstance(st, ast.While):
        return f"while {_source(lines, st.test)}"
    prefix = "async for" if isinstance(st, ast.AsyncFor) else "for"
    return f"{prefix} {_source(lines, st.target)} in {_source(lines, st.iter)}"


# FastAPI / Starlette 的 HTTP 异常（fastapi.HTTPException 就是 starlette 那个的子类）
_HTTP_EXCEPTIONS = frozenset({"fastapi.HTTPException", "fastapi.exceptions.HTTPException",
                              "starlette.exceptions.HTTPException"})


def _call_arg(call: ast.Call, pos: int, name: str) -> ast.expr | None:
    """调用的第 pos 个位置参数或关键字参数 name；前面有 *args 展开时位置对不上，不认。"""
    if len(call.args) > pos and not any(isinstance(a, ast.Starred) for a in call.args[: pos + 1]):
        return call.args[pos]
    return next((k.value for k in call.keywords if k.arg == name), None)


def _status_code(e: ast.expr | None) -> int | None:
    """HTTP 状态码：整数字面量，或名字形如 HTTP_404_NOT_FOUND 的常量（fastapi / starlette 的 status 模块）。"""
    if isinstance(e, ast.Constant) and type(e.value) is int:
        return e.value
    name = e.attr if isinstance(e, ast.Attribute) else e.id if isinstance(e, ast.Name) else ""
    m = re.fullmatch(r"HTTP_(\d{3})_\w+", name)
    return int(m.group(1)) if m else None


def _is_dict_key(root: ast.expr, node: ast.Constant) -> bool:
    """node 是 root 里某个字典字面量的键（{"error": "…"} 的 "error"）。"""
    return any(isinstance(d, ast.Dict) and any(k is node for k in d.keys) for d in ast.walk(root))


# ---------- 建图 ----------


class _Builder:
    def __init__(self, fn: FunctionInfo, index: ProjectIndex, rules: ActiveRules,
                 resolver: Resolver, config: ProjectConfig):
        self.fn = fn
        self.index = index
        self.rules = rules
        self.resolver = resolver
        self.config = config
        self.lines = index.modules[fn.file].lines
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self._heads: dict[str, _Head] = {}  # 节点 id -> 它代表的语句头部（隐含节点没有）
        self._refs: dict[int, CallRef] = {}  # id(调用节点) -> CallRef，每个调用只解析一次
        self._run: dict[int, ast.Call] = {}  # id(调用节点) -> 实际执行的调用（交给线程的包装换成被执行的函数）
        self._no_title: set[int] = set()  # id(CallRef)：不给步骤当标题的调用（见 _calls）
        self._constructors: set[int] = set()  # 构造项目内类的调用（target 是 __init__）的 id
        self._decorator_calls: dict[int, ast.Call] = {}  # id(装饰器表达式) -> 代表"应用装饰器"的调用节点
        self._awaited = {id(n.value) for st in fn.node.body for n in ast.walk(st)
                         if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}
        self._conditional = {id(c) for st in fn.node.body for n in _walk(st)
                             for part in _short_circuited(n) for c in _iter_calls(part)}
        # 没有 await 的调用里落在项目外对象上的（db.execute、requests.post）也算步骤，见 external.py
        self._external = ExternalCalls(fn, rules.external) if config.external_calls else None
        self._entry = rules.classify(fn)
        self._loops: list[_LoopCtx] = []  # 正在生成的循环，内层在后
        self._jump_ids = count(1)  # _Out.jump 的编号
        # 只有结局、没被捕获的异常进去的 finally 体的出口：不往后接，外层还有 finally 时进外层的（见 _emit_try）
        self._leaving: list[_Out] = []

    def build(self) -> FunctionGraph:
        fn = self.fn
        entry = self._entry
        whole = entry.title or fn.doc_title or fn.node.name  # 文档注释的整个第一行；标题只取它的第一句
        text = entry.title or first_sentence(fn.doc_title) or fn.node.name
        title, title_full = clean(text, TITLE_LIMIT), _full_form(text, whole)
        start = self._place([], "start", text, sid="start", line=fn.line, end_line=fn.line, full=whole)
        pending = self._emit(self._structure(fn.node.body), [_Out(start, "next")])
        live = self._reachable(start)  # 所有路径都已结束之后的死代码不补"执行到函数末尾"
        pending = [o for o in pending if o.src in live]
        if pending:
            self._place(pending, "end", FELL_OFF_TITLE, sid="end:implicit", end_kind="ok")
        self._place([], "end", UNCAUGHT_RETRY_TITLE if entry.retries else UNCAUGHT_TITLE, sid="end:uncaught",
                    end_kind="exception")
        self._number_sids()
        return FunctionGraph(
            key=fn.key, file=fn.file, qualname=fn.qualname, line=fn.line, end_line=fn.end_line,
            kind=entry.kind, title=title, is_async=fn.is_async, marked=entry.marked, group=entry.group,
            route=entry.route, retries=entry.retries, parent=fn.parent, nodes=self.nodes, edges=self.edges,
            call_targets=self._call_targets(), unresolved=self._unresolved(), rule=entry.rule, name=entry.name,
            version=entry.version, title_full=title_full)

    # ---------- 调用 ----------

    def _is_outcome(self, e: ast.expr | None) -> bool:
        """pystepkit 的结局辅助函数 outcome(...)：经本模块的 import 来自 pystepkit（from pystepkit import outcome、fk.outcome）。"""
        return isinstance(e, ast.Call) and pystepkit_name(e.func, self.index.modules[self.fn.file]) == "outcome"

    def _executed(self, call: ast.Call) -> ast.Call:
        """call 实际执行的调用：asyncio.to_thread(f, …)、run_in_executor(执行器, f, …) 是对 f 的调用，其余是它自己。"""
        run = self._run.get(id(call))
        if run is None:
            run = self._run[id(call)] = unwrap_thread_call(call, self.index.modules[self.fn.file])
        return run

    def _ref(self, call: ast.Call) -> CallRef:
        """调用的 CallRef（按实际执行的调用解析）。"""
        ref = self._refs.get(id(call))
        if ref is None:
            run = self._executed(call)
            d = self.rules.dispatch(run, self.fn, self.resolver)
            conditional = id(call) in self._conditional
            if d is None:
                r = self.resolver.resolve(run, self.fn)
                ref = CallRef(r.display, r.target, r.how, conditional=conditional)
                if r.constructor:
                    self._constructors.add(id(call))
            else:
                ref = CallRef(f"派发 {d.name or '未知任务'}", d.target, d.how, is_dispatch=True, conditional=conditional)
            self._refs[id(call)] = ref
        return ref

    def _calls(self, roots: Iterable[ast.AST]) -> list[CallRef]:
        """roots 里有意义的调用，按源码位置排序。派发调用接收者链上的调用（如 kicker(t).kiq() 里的
        kicker(t)）由派发代表，不单独列出。构造对象不是业务步骤：构造调用只进 call_targets，不进节点。"""
        found = _by_position(c for root in roots for c in _iter_calls(root))
        chain = {id(c) for d in found if self._ref(d).is_dispatch for c in _iter_calls(d.func)}
        refs = []
        for call in found:
            ref = self._ref(call)
            if id(call) in self._constructors:
                continue
            run = self._executed(call)
            # is_noise 把与内置函数同名的裸名都算噪音；已解析到项目内函数的（如项目自己的 format）不算
            shadows_builtin = isinstance(run.func, ast.Name) and self._callee(ref) is not None
            noise = self.rules.is_noise(run) and not shadows_builtin
            if id(call) in chain or noise or self._listed_noise(run, ref):
                continue
            if ref.is_dispatch or ref.resolution in ("exact", "guessed") or id(call) in self._awaited \
                    or (self._external is not None and self._external.is_step(run)):
                refs.append(ref)
        # await get_redis().delete(key_of(x))：这一步做的事是目标不明的 .delete，get_redis() 只是取对象、key_of(x) 只是
        # 算参数——它们的文档注释说的不是这一步，不拿来当标题（标题退到上方注释或调用的显示名）
        listed = {id(r) for r in refs}
        for outer in found:
            ref = self._ref(outer)
            if id(ref) in listed and not ref.is_dispatch and self._callee(ref) is None \
                    and any(self._callee(self._ref(c)) is not None for c in _iter_calls(outer.func)):
                self._no_title |= {id(self._ref(c)) for c in _iter_calls(outer) if c is not outer}
        return refs

    def _listed_noise(self, call: ast.Call, ref: CallRef) -> bool:
        """config.noise_functions：按被调用的名字或目标函数的限定名匹配。"""
        names = self.config.noise_functions
        if not names:
            return False
        f = call.func
        called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if called in names:
            return True
        callee = self._callee(ref)
        return callee is not None and callee.qualname in names

    def _callee(self, ref: CallRef) -> FunctionInfo | None:
        """被调用的项目内函数；派发的目标是被派发的任务，不算被调用。"""
        if ref.is_dispatch or ref.target is None:
            return None
        return self.index.functions.get(ref.target)

    def _frame_calls(self, into_lambda: bool) -> list[ast.Call]:
        """在本函数的帧里执行的全部调用，按源码位置排序：函数体里的（不含嵌套函数、类的体；是否含 lambda 体由
        into_lambda 决定），加上嵌套定义的装饰器、参数默认值、类的基类里的调用，以及直接写名字的装饰器
        （@deco）的应用——它们都在外层帧执行。"""
        found = [c for st in self.fn.node.body for c in _iter_calls(st, into_lambda)]
        for st in self.fn.node.body:
            for expr, is_decorator in _nested_headers(st):
                found += _iter_calls(expr, into_lambda)
                if is_decorator and isinstance(expr, ast.Name | ast.Attribute):
                    found.append(self._decorator_call(expr))
        return _by_position(found)

    def _decorator_call(self, expr: ast.Name | ast.Attribute) -> ast.Call:
        """@deco 应用时等于在外层帧调用 deco(被装饰的函数)：造一个位置相同的调用节点交给解析器（每个装饰器只造一次）。"""
        call = self._decorator_calls.get(id(expr))
        if call is None:
            call = self._decorator_calls[id(expr)] = ast.copy_location(ast.Call(func=expr, args=[], keywords=[]), expr)
            call.end_lineno, call.end_col_offset = expr.end_lineno, expr.end_col_offset
        return call

    def _call_targets(self) -> list[str]:
        """在本函数帧里执行的调用（含 lambda 体里的）解析到的项目内函数（含构造调用的 __init__），按源码位置首次
        出现的顺序去重。"""
        seen: dict[str, None] = {}
        for call in self._frame_calls(into_lambda=True):
            ref = self._ref(call)
            if not ref.is_dispatch and ref.resolution in ("exact", "guessed") and ref.target:
                seen.setdefault(ref.target, None)
        return list(seen)

    def _unresolved(self) -> list[UnresolvedCall]:
        """在本函数帧里执行、无法判断目标的调用（不论是否被 await、是否进了节点）：所在行和显示名，按源码顺序、
        (行, 名字) 去重。lambda 体里的调用在 lambda 自己的帧里执行，不算。"""
        seen: dict[tuple[int, str], None] = {}
        for call in self._frame_calls(into_lambda=False):
            ref = self._ref(call)
            if ref.resolution == "unresolved":
                seen.setdefault((call.lineno, ref.name), None)
        return [UnresolvedCall(line, name) for line, name in seen]

    # ---------- 第一段：语句 → 结构树 ----------

    def _structure(self, stmts: list[ast.stmt]) -> list[_Item]:
        items: list[_Item] = []
        for st in stmts:
            items += self._statement(st)
        return items

    def _statement(self, st: ast.stmt) -> list[_Item]:
        if isinstance(st, _SCOPES):
            return []
        if isinstance(st, ast.If):
            return self._if(st)
        if isinstance(st, ast.Return):
            return self._return(st)
        if isinstance(st, ast.With | ast.AsyncWith):  # 不出节点，里面的语句接在主线上
            # 头部到最后一个上下文表达式为止；前面各项的 as 目标也不参与 sid
            targets = [(i.context_expr, i.optional_vars) for i in st.items[:-1] if i.optional_vars is not None]
            return self._step(st, [i.context_expr for i in st.items], targets) + self._structure(st.body)
        if isinstance(st, ast.Match):
            return self._match(st)
        if isinstance(st, ast.For | ast.AsyncFor | ast.While):
            return self._loop(st)
        if isinstance(st, ast.Try | ast.TryStar):
            return self._try(st)
        if isinstance(st, ast.Raise):
            return self._raise(st)
        if isinstance(st, ast.Break):
            return [_Break()]
        if isinstance(st, ast.Continue):
            return [_Continue()]
        return self._step(st, [st])

    def _step(self, at: ast.stmt, roots: list[ast.AST],
              targets: Iterable[tuple[ast.expr, ast.expr]] = ()) -> list[_Item]:
        """roots（语句本身或复合语句的头部）里有意义的调用合成一个步骤；没有则不出节点。targets 见 _head。"""
        calls = self._calls(roots)
        if not calls:
            return []
        last = max(roots, key=lambda r: (r.end_lineno, r.end_col_offset))
        return [_Step(at.lineno, last.end_lineno, calls, _head(self.lines, at, last, targets))]

    def _if(self, st: ast.If) -> list[_Item]:
        body, orelse = self._structure(st.body), self._structure(st.orelse)
        quiet = {branch: text for branch, stmts, kept in (("yes", st.body, body), ("no", st.orelse, orelse))
                 if not kept and (text := self._assignments(stmts))}
        if body or orelse:
            return [_If(st, self._calls([st.test]), body, orelse, quiet)]
        steps = self._step(st, [st.test])  # 两个分支都折叠掉了：只剩条件里的调用
        for step in steps:
            step.quiet = quiet
        return steps

    def _assignments(self, stmts: list[ast.stmt]) -> str:
        """折叠掉的分支里的赋值语句原文（按源码顺序，"；"连接）；没有赋值时 ""。分支只写日志、只有 pass 时不用说。"""
        found = [n for st in stmts for n in _walk(st) if isinstance(n, ast.Assign | ast.AugAssign | ast.AnnAssign)]
        found.sort(key=lambda n: (n.lineno, n.col_offset))
        return clean("；".join(_source(self.lines, n) for n in found), COND_LIMIT)

    def _return(self, st: ast.Return) -> list[_Item]:
        v = st.value
        if v is None:
            roots = []
        elif self._is_outcome(v):  # outcome(...) 本身只是构造结局，只看它的实参
            roots = [*v.args, *(kw.value for kw in v.keywords)]
        else:
            roots = [v]
        calls = self._calls(roots)
        step = [_Step(st.lineno, st.end_lineno, calls, _head(self.lines, st))] if calls else []
        return [*step, _Return(st)]

    def _raise(self, st: ast.Raise) -> list[_Item]:
        calls = self._calls([r for r in (st.exc, st.cause) if r is not None])
        step = [_Step(st.lineno, st.end_lineno, calls, _head(self.lines, st))] if calls else []
        return [*step, _Raise(st)]

    def _match(self, st: ast.Match) -> list[_Item]:
        """match 是一个判断节点，守卫不单独出节点：主语和守卫里的调用都记在判断节点上。"""
        cases = [_Case(*self._case_label(case), self._structure(case.body)) for case in st.cases]
        calls = self._calls([st.subject, *(case.guard for case in st.cases if case.guard is not None)])
        if not any(c.body for c in cases):  # 所有分支都折叠掉了：只剩主语和守卫里的调用（同 if）
            return [_Step(st.lineno, st.subject.end_lineno, calls, _head(self.lines, st, st.subject))] if calls else []
        exhaustive = any(case.guard is None and _irrefutable(case.pattern) for case in st.cases)
        return [_Match(st, calls, cases, exhaustive)]

    def _case_label(self, case: ast.match_case) -> tuple[str, str]:
        """（label，被截断时的完整形式），见 _code_label。"""
        text = _source(self.lines, case.pattern)
        if case.guard is not None:
            text += f" if {_source(self.lines, case.guard)}"
        return _code_label(text)

    def _loop(self, st: ast.For | ast.AsyncFor | ast.While) -> list[_Item]:
        calls = self._calls([st.test] if isinstance(st, ast.While) else [st.target, st.iter])
        body, orelse = self._structure(st.body), self._structure(st.orelse)
        if not calls and not orelse and all(isinstance(it, _Break | _Continue) for it in body):
            return []  # 循环体里只有 break / continue（或什么都没有），循环头里也没有有意义的调用
        return [_Loop(st, calls, body, orelse)]

    def _try(self, st: ast.Try | ast.TryStar) -> list[_Item]:
        t = _Try(self._structure(st.body), [_Handler(h, self._structure(h.body)) for h in st.handlers],
                 self._structure(st.orelse), self._structure(st.finalbody))
        return [t] if t.body or t.orelse or t.final or any(h.body for h in t.handlers) else []

    # ---------- 第二段：结构树 → 节点和连线 ----------

    def _place(self, pending: list[_Out], node_type: NodeType, title: str, head: _Head | None = None,
               **attrs) -> str:
        """新建节点，把悬空的出边接到它上面，返回节点 id。title 和 attrs 里的 subtitle 是原文，在这里清理、截断（标题和
        步骤的副标题到 TITLE_LIMIT，判断、循环、捕获的副标题到 COND_LIMIT），被截断时另记完整形式（title_full、
        subtitle_full）。head：节点代表的语句头部（隐含节点没有，sid 由调用方给）。"""
        nid = f"n{len(self.nodes) + 1}"
        if head is not None:
            attrs["sid"] = _head_sid(node_type, head)
            self._heads[nid] = head
        sub = attrs.pop("subtitle", "")
        full = attrs.pop("full", None)  # 标题只取了文档注释第一句时：整个第一行（进 title_full）
        sub_limit = TITLE_LIMIT if node_type == "step" else COND_LIMIT
        self.nodes.append(Node(nid, node_type, clean(title, TITLE_LIMIT), subtitle=clean(sub, sub_limit),
                               title_full=_full_form(title, full or title), subtitle_full=clean_full(sub, sub_limit),
                               **attrs))
        self._connect(pending, nid)
        return nid

    def _number_sids(self) -> None:
        """有语句头部的节点按源码位置排好，sid 相同的从第二个起加 #2、#3（见 Node.sid）。"""
        by_id = {n.id: n for n in self.nodes}
        placed = sorted(self._heads, key=lambda nid: (self._heads[nid].line, self._heads[nid].col))
        for nid, sid in zip(placed, numbered(by_id[nid].sid for nid in placed)):
            by_id[nid].sid = sid

    def _connect(self, outs: list[_Out], dst: str) -> None:
        """把出边接到 dst（可以是已有节点），去重（label_full 不参与：label 相同的就是同一条，留先接上的）。同一起点到
        dst 至多一条异常连线（raise，不论标签）；已有别的类型的连线时照样画：它标出异常的落脚点（如 try 体以 if 开头，
        "条件不成立"的连线和异常连线落在同一处）。"""
        for o in dict.fromkeys(outs):
            if o.kind == "raise" and any(e.src == o.src and e.dst == dst and e.kind == "raise" for e in self.edges):
                continue
            key = (o.src, dst, o.kind, o.label, o.branch)
            if not any((e.src, e.dst, e.kind, e.label, e.branch) == key for e in self.edges):
                self.edges.append(Edge(*key, label_full=o.label_full))

    def _reachable(self, start: str) -> set[str]:
        """从 start 沿连线能走到的节点。"""
        nexts: dict[str, list[str]] = {}
        for e in self.edges:
            nexts.setdefault(e.src, []).append(e.dst)
        seen, stack = {start}, [start]
        while stack:
            for dst in nexts.get(stack.pop(), []):
                if dst not in seen:
                    seen.add(dst)
                    stack.append(dst)
        return seen

    def _emit(self, items: list[_Item], pending: list[_Out]) -> list[_Out]:
        """依次生成 items 的节点和连线，pending 接到第一个节点上；返回之后仍悬空的出边。"""
        for it in items:
            if isinstance(it, _Step):
                nid = self._place(pending, "step", self._step_title(it), it.head, full=self._step_title(it, whole=True),
                                  subtitle=" + ".join(c.name for c in it.calls),
                                  line=it.line, end_line=it.end_line, calls=it.calls, quiet=it.quiet)
                pending = [_Out(nid, "next")]
            elif isinstance(it, _If):
                st = it.stmt
                test = _source(self.lines, st.test)
                nid = self._place(pending, "cond", self._cond_title(st.lineno, st.test, test),
                                  _head(self.lines, st, st.test), subtitle=test,
                                  line=st.lineno, end_line=st.test.end_lineno, calls=it.calls, quiet=it.quiet)
                pending = (self._emit(it.body, [_Out(nid, "yes", branch="yes")])
                           + self._emit(it.orelse, [_Out(nid, "no", branch="no")]))
            elif isinstance(it, _Match):
                pending = self._emit_match(it, pending)
            elif isinstance(it, _Loop):
                pending = self._emit_loop(it, pending)
            elif isinstance(it, _Try):
                pending = self._emit_try(it, pending)
            elif isinstance(it, _Break | _Continue):
                pending = self._jump(it, pending)
            elif isinstance(it, _Raise):
                self._place(pending, "end", **self._raise_end(it.stmt))
                pending = []
            else:
                self._place(pending, "end", **self._end(it.stmt))
                pending = []
        return pending

    def _emit_loop(self, lp: _Loop, pending: list[_Out]) -> list[_Out]:
        """循环节点 → 循环体（loop_each）；循环体的正常出口回到循环节点（loop_back）；循环节点的 loop_exit
        接 else 体，break 的出边越过 else 接到循环之后。"""
        st = lp.stmt
        head = _loop_head(self.lines, st)
        last = st.test if isinstance(st, ast.While) else st.iter
        nid = self._place(pending, "loop", comment_above(self.lines, st.lineno) or self._marked_title(last) or head,
                          _head(self.lines, st, last), subtitle=head, line=st.lineno,
                          end_line=last.end_lineno, calls=lp.calls)
        self._loops.append(_LoopCtx(nid, [], [], []))
        body_out = self._emit(lp.body, [_Out(nid, "loop_each")])
        ctx = self._loops.pop()
        self._connect(_redirect(body_out, "loop_back"), nid)
        return self._emit(lp.orelse, [_Out(nid, "loop_exit")]) + ctx.breaks

    def _jump(self, it: _Break | _Continue, pending: list[_Out]) -> list[_Out]:
        """break / continue 的出边：先经过它和所属循环之间每个有 finally 体的 try 的 finally 体（见 _leave）。"""
        if not self._loops:  # 循环外的 break / continue 编译不过，这里只是不崩溃
            return pending
        ctx = self._loops[-1]
        ctx.jumped += pending
        self._leave(ctx, pending, isinstance(it, _Break))
        return []

    def _leave(self, ctx: _LoopCtx, outs: list[_Out], is_break: bool) -> None:
        """跳转的出边往外走一层：被有 finally 体的 try 包住时记到最内层的那个 try，等它的 finally 体生成时接进去
        （_emit_finally）；没有时到目标——break 留到循环之后再接，continue 连回循环节点。"""
        if ctx.finals:
            jumps = ctx.finals[-1]
            (jumps.breaks if is_break else jumps.continues).extend(outs)
        elif is_break:
            ctx.breaks += _redirect(outs, "break")
        else:
            self._connect(_redirect(outs, "loop_back"), ctx.head)

    def _emit_try(self, t: _Try, pending: list[_Out]) -> list[_Out]:
        """try 体照常接在主线上。"这一段里抛出了异常"的出边（thrown，见 _thrown）连到每个捕获节点；只写日志的
        except 吞掉异常后照常往下走，thrown 也接到后面。有 finally 时，正常出口、thrown（没被捕获的异常同样
        经过 finally）、try / except / else 里每个结局和越过它的 break / continue 都进入 finally 体（_emit_finally）。
        进 finally 体的只有结局和没被捕获的异常时（try / except / else 没有正常出口，也没有 break / continue 穿过），出了 finally 体函数就结束了
        （return 生效，或者异常接着往外抛）：finally 体的出口不往后接，外层还有 finally 时进外层的 finally 体。"""
        mark = len(self.nodes)
        ctx = self._loops[-1] if self._loops else _LoopCtx("", [], [], [])  # 不在循环里：没有 break / continue 可记
        jumps = _Jumps([], [])
        if t.final:
            ctx.finals.append(jumps)
        jumped = len(ctx.jumped)  # try 体生成前已记下的数量
        leaving = len(self._leaving)
        body_out = self._emit(t.body, pending)
        thrown = self._thrown(mark, body_out + ctx.jumped[jumped:])
        outs = self._emit(t.orelse, body_out)
        for h in t.handlers:
            if h.body:
                catch = self._place(thrown, "catch", **self._catch(h.handler))
                outs += self._emit(h.body, [_Out(catch, "next")])
            else:
                outs += thrown
        if not t.final:
            return outs
        ctx.finals.pop()
        ends = [_Out(n.id, "finally") for n in self.nodes[mark:] if n.type == "end"]
        inner = _redirect(self._leaving[leaving:], "finally")  # 里层 finally 体走完、接着往外离开的
        del self._leaving[leaving:]
        fin_out = self._emit_finally(t.final, outs + thrown + ends + inner, jumps, ctx)
        # 有 break / continue 穿过时照旧（finally 体的出口已经接到跳转目标，见 _emit_finally）
        if outs or jumps.breaks or jumps.continues:
            return fin_out
        self._leaving += fin_out
        return []

    def _emit_finally(self, final: list[_Item], pending: list[_Out], jumps: _Jumps, ctx: _LoopCtx) -> list[_Out]:
        """finally 体只画一份。pending（正常出口、异常、结局）和 jumps 里越过这个 finally 的 break / continue 的出边
        一起进入 finally 体，后者的连线类型是 finally（"是 / 否"写进 label）。有跳转时，finally 体的每个出口再往外走
        一层（_leave：外面还有包住跳转的 finally 时进那个 finally，否则到跳转目标）。finally 体里没经过节点的路径上，
        跳转的出边原样穿过：按 jump 编号认出，同样往外走。返回正常往后走的出边（原有的出口不变）。"""
        mark = len(self.nodes)
        groups: list[tuple[int, list[_Out], bool]] = []  # (编号, 跳转的出边, 是否 break)
        for outs, is_break in ((jumps.breaks, True), (jumps.continues, False)):
            if outs:
                groups.append((next(self._jump_ids), outs, is_break))
        entries = [replace(o, jump=gid) for gid, outs, _ in groups for o in _redirect(outs, "finally")]
        fin_out = self._emit(final, pending + entries)
        inside = {n.id for n in self.nodes[mark:]}
        exits = [o for o in fin_out if o.src in inside]
        for gid, _, is_break in groups:
            self._leave(ctx, exits + [o for o in fin_out if o.jump == gid], is_break)
        ids = {gid for gid, _, _ in groups}
        return [o for o in fin_out if o.jump not in ids]

    def _thrown(self, mark: int, leaving: list[_Out]) -> list[_Out]:
        """try 体（节点 mark 起）里抛出异常的 raise 出边。起点合起来要拦住每条经过 try 体的路径：
        - try 体里每个有连线从外面进来的节点（入口；通常只有第一个节点，就是它）。第一个创建的节点不一定是入口：
          例如 try 体以"try 体没有节点"的内层 try 开头时，它是内层的捕获节点。入口都是 try 体里新建的节点，
          出边被 _connect 去重时同一起点总有另一条连线接到它，所以按连线找入口不会漏。
        - leaving（try 体生成完时悬空的出边，以及期间新记下的 break / continue 出边）里起点在 try 体外的：
          这些路径没经过任何节点就离开了 try 体；try 体没有节点时就是 try 之前的前驱。连线可能被去重，
          所以这一类只看记下的出边，不看连线。"""
        inside = {n.id for n in self.nodes[mark:]}
        entered = {e.dst for e in self.edges if e.dst in inside and e.src not in inside}
        crossed = [o for o in leaving if o.src not in inside]
        return [_Out(n.id, "raise") for n in self.nodes[mark:] if n.id in entered] + _redirect(crossed, "raise")

    def _emit_match(self, m: _Match, pending: list[_Out]) -> list[_Out]:
        """判断节点 → 每个 case 一条 case 连线（label 是模式，带守卫时加"if 守卫"；branch 同 label），分支体为空时这条
        连线直接越过 match；没有兜底分支时再加一条"都不匹配"的 case 连线越过 match（branch 为 else）。守卫不成立时
        落到后面的分支：判断节点到后面各分支都有连线，不另画节点。"""
        st = m.stmt
        head = f"match {_source(self.lines, st.subject)}"
        nid = self._place(pending, "cond", self._cond_title(st.lineno, st.subject, head),
                          _head(self.lines, st, st.subject), subtitle=head, line=st.lineno,
                          end_line=st.subject.end_lineno, calls=m.calls)
        outs: list[_Out] = []  # 某个分支执行完、越过 match 的出边
        for case in m.cases:
            outs += self._emit(case.body, [_Out(nid, "case", case.label, branch=case.label,
                                                label_full=case.label_full)])
        return outs if m.exhaustive else [*outs, _Out(nid, "case", NO_MATCH_LABEL, branch=NO_MATCH_BRANCH)]

    # ---------- 标题与结局 ----------

    def _callee_title(self, ref: CallRef, whole: bool = False) -> str:
        """被调用函数的规则标题 → 它的文档注释第一行的第一句（whole：整个第一行）。按名字推测（guessed）的调用不取：
        目标不一定是它，标题同无法判断目标的调用（上方注释或调用的显示名）。"""
        callee = self._callee(ref)
        if callee is None or ref.resolution == "guessed":
            return ""
        return self.rules.classify(callee).title or (callee.doc_title if whole else first_sentence(callee.doc_title))

    def _step_title(self, step: _Step, whole: bool = False) -> str:
        """步骤标题的原文（_place 清理、截断）。whole：取自文档注释时用整个第一行（完整形式用），否则只用第一句。"""
        if len(step.calls) == 1 and step.calls[0].is_dispatch:  # 只有一个派发
            ref = step.calls[0]
            task = self.index.functions.get(ref.target) if ref.target else None
            if task is None:
                return "派发 未知任务"
            if ref.resolution == "guessed":  # 只按任务名对应上的：目标不一定是它，不取它的标题（同副标题"派发 任务名"）
                return ref.name
            doc = task.doc_title if whole else first_sentence(task.doc_title)
            return "派发：" + (self.rules.classify(task).title or doc or task.node.name)
        for ref in step.calls:
            title = "" if id(ref) in self._no_title else self._callee_title(ref, whole)
            if title:
                return title
        return comment_above(self.lines, step.line) or " + ".join(c.name for c in step.calls)

    def _cond_title(self, line: int, test: ast.expr, text: str) -> str:
        """判断节点（第 line 行，条件 test：if 的条件、match 的主语）标题的原文：条件是对 condition 类型函数的单个调用
        （可带 await）时取它的标题 → 上方注释 → 条件原文 text（match 是"match 主语"）。"""
        call = test.value if isinstance(test, ast.Await) else test
        title = ""
        if isinstance(call, ast.Call):
            ref = self._ref(call)
            callee = self._callee(ref)
            if callee is not None and self.rules.classify(callee).kind == "condition":
                title = self._callee_title(ref)
        return title or comment_above(self.lines, line) or self._marked_title(test) or text

    def _marked_title(self, e: ast.expr | None) -> str:
        """e 是对一个带规则标题的项目内函数（@step、@condition 这样标了标题的）的单个调用（可带 await）时，那个标题；
        不是时 ""。循环头、判断条件、return 的值整个就是这样一个调用时，节点标题用它而不是源码。文档注释不算：
        那是按名字借来的说明，只在步骤节点上用。按名字推测的目标也不算。"""
        call = e.value if isinstance(e, ast.Await) else e
        if not isinstance(call, ast.Call) or self._is_outcome(call):
            return ""
        ref = self._ref(call)
        callee = self._callee(ref)
        if callee is None or ref.resolution == "guessed" or ref.is_dispatch:
            return ""
        return self.rules.classify(callee).title

    def _end(self, st: ast.Return) -> dict:
        v = st.value
        title = ""
        end_kind: EndKind | None = None
        pairs: list[tuple[str, ast.expr | str]] = []
        if isinstance(v, ast.Dict):
            pairs = [(k.value, val) for k, val in zip(v.keys, v.values)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        elif self._is_outcome(v):
            args = _outcome_args(v)
            given = args.get("title")
            if isinstance(given, ast.Constant) and isinstance(given.value, str):
                title = given.value
            end_kind = _outcome_kind(args["kind"])
            pairs = list(args.items())
        fields = {name: self._field_value(val) for name, val in pairs if name in self.config.outcome_fields}
        status = self._status_return(v)
        if status is not None and end_kind is None and not fields:
            title, end_kind = status, "failed"
        if not title:
            marked = self._marked_title(v)
            # 带结局字段（status、reason……）的字典也写出返回的内容：一律写"结束"时，同一个函数里的几个结局分不清
            title = "结束" if fields and not isinstance(v, ast.Dict) else f"返回「{marked}」的结果" if marked else \
                "返回 " + ("None" if v is None else _source(self.lines, v))
        return {"title": title, "head": _head(self.lines, st), "line": st.lineno,
                "end_line": st.end_lineno, "end_kind": end_kind or self._end_kind(fields), "fields": fields,
                "passthrough": self._passthrough(v)}

    def _status_return(self, v: ast.expr | None) -> str | None:
        """接口函数里 `return 响应, 状态码`（Flask 的写法）、状态码是 400 及以上时的结局标题"状态码 说明"，如
        "400 标题不能为空"；不是这种写法时 None。说明取响应里第一个字符串（jsonify(error="…")、{"error": "…"}），
        没有时写状态码的标准说法。"""
        if self._entry.kind != "route" and not self._entry.route:
            return None
        if not isinstance(v, ast.Tuple) or len(v.elts) < 2:
            return None
        code = _status_code(v.elts[1])
        if code is None or code < 400:
            return None
        said = next((n.value for n in ast.walk(v.elts[0])
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.strip()
                     and not _is_dict_key(v.elts[0], n)), None)
        if said is None:
            try:
                said = HTTPStatus(code).phrase
            except ValueError:
                said = ""
        return f"{code} {said}".strip()

    def _raise_end(self, st: ast.Raise) -> dict:
        text = _source(self.lines, st.exc) if st.exc is not None else "原异常"
        title = self._http_error_title(st.exc) or f"抛出 {text}"
        return {"title": title, "head": _head(self.lines, st), "line": st.lineno,
                "end_line": st.end_lineno, "end_kind": "failed"}

    def _http_error_title(self, exc: ast.expr | None) -> str | None:
        """raise HTTPException(...)（FastAPI / Starlette 的，按本模块的 import 认）的结局标题"状态码 说明"，如
        "404 用户不存在"：接口的调用方看到的就是这两样。状态码认整数字面量和 status.HTTP_404_NOT_FOUND 这样的常量，
        看不出来时 None（照旧写"抛出 源码"）；说明认字符串、f-string（{…} 照写）、只赋值过一次的变量（看赋的值），别的表达式写源码，没给时写状态码的
        标准说法（Starlette 的默认说明）。"""
        if not isinstance(exc, ast.Call) or \
                dotted_name_of(exc.func, self.index.modules[self.fn.file]) not in _HTTP_EXCEPTIONS:
            return None
        code = _status_code(_call_arg(exc, 0, "status_code"))
        if code is None:
            return None
        detail = _call_arg(exc, 1, "detail")
        if isinstance(detail, ast.Name):  # detail=msg，msg 只赋值过一次：看那次赋的值
            detail = self._sole_value(detail.id) or detail
        if detail is None or (isinstance(detail, ast.Constant) and detail.value is None):
            try:
                said = HTTPStatus(code).phrase
            except ValueError:
                said = ""
        elif isinstance(detail, ast.Constant) and isinstance(detail.value, str):
            said = detail.value
        elif isinstance(detail, ast.JoinedStr):
            said = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                           else "{" + _source(self.lines, v.value) + "}" if isinstance(v, ast.FormattedValue)
                           else _source(self.lines, v) for v in detail.values)
        else:
            said = _source(self.lines, detail)
        return f"{code} {said}".strip()

    def _catch(self, h: ast.ExceptHandler) -> dict:
        kind = _source(self.lines, h.type) if h.type is not None else "任意异常"
        head = _head(self.lines, h, h.type) if h.type is not None else _Head(h.lineno, h.col_offset, "except")
        return {"title": f"捕获 {kind}", "head": head, "subtitle": kind,
                "line": h.lineno, "end_line": h.type.end_lineno if h.type is not None else h.lineno}

    def _passthrough(self, v: ast.expr | None) -> str | None:
        """转交返回：return f(...) / return await f(...)，或 return x 且 x 是这样一个调用的结果；
        f 须解析（exact / guessed）到项目内函数，派发不算。返回 f 的 key。"""
        if isinstance(v, ast.Name):
            v = self._sole_value(v.id)
        if isinstance(v, ast.Await):
            v = v.value
        if not isinstance(v, ast.Call) or self._is_outcome(v):
            return None
        ref = self._ref(v)
        if ref.is_dispatch or ref.resolution not in ("exact", "guessed") or id(v) in self._constructors:
            return None  # 返回新构造的对象不是转交返回
        return ref.target

    def _sole_value(self, name: str) -> ast.expr | None:
        """name 在本函数里（不含嵌套定义）只绑定一次、且那次是 name = 值 时返回那个值。参数也算一次绑定。"""
        a = self.fn.node.args
        if name in {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if p}:
            return None
        values: dict[int, ast.expr] = {}  # id(赋值目标) -> 右边
        binds: list[ast.AST] = []
        for st in self.fn.node.body:
            for n in _walk(st):
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    values[id(n.targets[0])] = n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    values[id(n.target)] = n.value
                if _binds(n, name):
                    binds.append(n)
        return values.get(id(binds[0])) if len(binds) == 1 else None

    def _field_value(self, v: ast.expr | str) -> str:
        """字段值：常量取其值，否则取源码。"""
        if isinstance(v, str):
            return v
        if isinstance(v, ast.Constant):
            return v.value if isinstance(v.value, str) else str(v.value)
        return _source(self.lines, v)

    def _end_kind(self, fields: dict[str, str]) -> EndKind:
        """按 config.outcome_kinds 的顺序匹配字段值，先匹配到的为准；都不匹配为 "ok"。"""
        for name, mapping in self.config.outcome_kinds.items():
            value = fields.get(name)
            if value is not None and value in mapping:
                return mapping[value]
        return "ok"


def build_function_graph(fn: FunctionInfo, index: ProjectIndex, rules: ActiveRules,
                         resolver: Resolver, config: ProjectConfig) -> FunctionGraph:
    """把一个函数画成图：start、step、cond、loop、catch、end 节点和它们之间的连线。节点的 sid 在这里算好；函数的 sid
    要看同一文件里的全部定义，这里留空，由 analyze 填。"""
    return _Builder(fn, index, rules, resolver, config).build()
