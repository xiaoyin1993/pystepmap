"""约束检查：只检查带标记的函数。编排函数只调用步骤，步骤之间不互相调用。

每条规则是一个小函数，便于单独测试和日后放宽。宁可漏报也不误报。规则在一个真实的 DBOS 项目上校准过。
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path

from .config import ProjectConfig
from .index import (
    NOT_SOURCE_REASONS,
    FunctionInfo,
    ModuleInfo,
    ProjectIndex,
    build_index,
)
from .resolve import Resolved, Resolver, unwrap_thread_call
from .rules import ActiveRules, pystepkit_name

_BUILTIN_FUNCS = frozenset(n for n in dir(builtins) if not n.startswith("_"))
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_CALLEE_KINDS = ("step", "condition", "workflow")
_LITERALS = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set)
# 框架自带的编排原语（识别规则名 -> (编排对象的点分全名, 放行的方法)）：项目启用了该规则时，直接调用这些方法
# （以及在它们返回的句柄上继续调用）在编排函数里放行。DBOS 的这些方法在工作流里调用时由 DBOS 自己按步骤记录
# （dbos 2.30 的 _dbos.py：启动 / 入队子工作流、run_step、消息与事件、sleep、取别的工作流的结果与状态、写流、patch、
# cancel_workflow），重放安全。直接读写系统库、不记成步骤的接口（队列管理、read_stream、launch / destroy 等）、
# DBOS.logger 这类先取属性再调用的都不放行。
_DBOS_PRIMITIVES = frozenset({
    "start_workflow", "start_workflow_async", "enqueue_workflow", "enqueue_workflow_async",
    "enqueue_workflow_with_options", "enqueue_workflow_with_options_async", "run_step", "run_step_async",
    "send", "send_async", "send_bulk", "send_bulk_async", "recv", "recv_async",
    "set_event", "set_event_async", "get_event", "get_event_async", "get_all_events", "get_all_events_async",
    "sleep", "sleep_async", "asyncio_wait", "retrieve_workflow", "retrieve_workflow_async",
    "get_result", "get_result_async", "get_workflow_status", "get_workflow_status_async", "wait_first", "wait_first_async",
    "write_stream", "write_stream_async", "close_stream", "close_stream_async",
    "patch", "patch_async", "deprecate_patch", "deprecate_patch_async",
    "cancel_workflow", "cancel_workflow_async", "cancel_workflows", "cancel_workflows_async"})
_ORCHESTRATION: dict[str, tuple[str, frozenset[str]]] = {"dbos": ("dbos.DBOS", _DBOS_PRIMITIVES)}
# 容器 / 字符串取值：方法链上全是它们时算操作数据（根可以是参数）
_ACCESSORS = frozenset({"get", "pop", "items", "keys", "values", "copy", "strip", "split", "splitlines",
                        "lower", "upper", "startswith", "endswith", "replace"})


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    code: str
    message: str


@dataclass
class CheckResult:
    violations: list[Violation]
    skipped: list[tuple[str, str]]  # 检查范围内解析不了、没有检查的文件：(相对路径, 原因)
    unmatched: list[str]  # only_files 里没有对应到任何项目文件的项（规范化后的相对路径）
    # 检查范围内不是项目源文件、没有读的 .py 路径（指到项目之外或指到非 .py 的符号链接、FIFO 等）：只是警告
    ignored: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CallSite:
    call: ast.Call
    in_test: bool  # 位于 if / elif / while 的条件里
    awaited: bool


# ---------- 收集 ----------

def collect_body(fn: FunctionInfo) -> tuple[list[CallSite], list[ast.AST]]:
    """函数体里的所有调用，以及内部函数 / lambda 的定义（不进入它们内部）。装饰器与参数默认值不算函数体。"""
    calls: list[CallSite] = []
    nested: list[ast.AST] = []

    def visit(n: ast.AST, in_test: bool, awaited: bool) -> None:
        if isinstance(n, _NESTED):
            nested.append(n)
            return
        if isinstance(n, ast.Call):
            calls.append(CallSite(n, in_test, awaited))
        for field_name, value in ast.iter_fields(n):
            kids = value if isinstance(value, list) else [value]
            for c in kids:
                if not isinstance(c, ast.AST):
                    continue
                test = in_test or (isinstance(n, ast.If | ast.While) and field_name == "test")
                visit(c, test, isinstance(n, ast.Await))

    for st in fn.node.body:
        visit(st, False, False)
    return calls, nested


def local_names(fn: FunctionInfo) -> set[str]:
    """参数和函数体里绑定的局部名（不含内部函数里的）。"""
    a = fn.node.args
    names = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x is not None}

    def visit(n: ast.AST) -> None:
        if isinstance(n, _NESTED):
            if not isinstance(n, ast.Lambda):
                names.add(n.name)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        for c in ast.iter_child_nodes(n):
            visit(c)

    for st in fn.node.body:
        visit(st)
    return names


# ---------- 单条调用的判断 ----------

def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def is_outcome_call(call: ast.Call, mod: ModuleInfo) -> bool:
    """pystepkit 的 outcome(...)（经本模块的 import 来自 pystepkit）。"""
    return pystepkit_name(call.func, mod) == "outcome"


def is_construction(call: ast.Call, fn: FunctionInfo, index: ProjectIndex) -> bool:
    name = _callee_name(call)
    if name is None:
        return False
    if name[:1].isupper():
        return True
    return isinstance(call.func, ast.Name) and name in index.modules[fn.file].classes


def is_builtin_call(call: ast.Call, resolved: Resolved) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id in _BUILTIN_FUNCS and resolved.target is None


def _rooted_at(e: ast.expr, names: set[str]) -> bool:
    """表达式以 names 里的名字为根：穿过属性和下标（不穿过调用），以及 (x or {}) 这类带字面量兜底的写法。"""
    while isinstance(e, ast.Attribute | ast.Subscript):
        e = e.value
    if isinstance(e, ast.BoolOp):
        rooted = [v for v in e.values if not isinstance(v, _LITERALS)]
        return bool(rooted) and all(_rooted_at(v, names) for v in rooted)
    return isinstance(e, ast.Name) and e.id in names


def _method_chain(e: ast.expr) -> tuple[list[ast.Call], ast.expr]:
    """接收者表达式里的方法链：(链上的调用，从外到内；链的根)。穿过属性、下标、调用和 await。"""
    calls: list[ast.Call] = []
    while True:
        if isinstance(e, ast.Attribute | ast.Subscript | ast.Await):
            e = e.value
        elif isinstance(e, ast.Call):
            calls.append(e)
            e = e.func
        else:
            return calls, e


def is_orchestration_call(call: ast.Call, mod: ModuleInfo, rules: ActiveRules) -> bool:
    """直接调用框架的编排原语（DBOS.sleep(…)），或在它返回的句柄上继续调用（DBOS.start_workflow(…).get_result()）。"""
    allowed = {_ORCHESTRATION[name] for name in rules.names if name in _ORCHESTRATION}
    if not allowed:
        return False
    chain: list[ast.expr] = [call]
    e: ast.expr = call.func
    while True:
        chain.append(e)
        if isinstance(e, ast.Call):
            e = e.func
        elif isinstance(e, ast.Attribute | ast.Subscript | ast.Await):
            e = e.value
        else:
            break
    # chain 末尾应是 [..., 调用, 属性, 名字]：编排对象上的放行方法被直接调用
    if not (len(chain) >= 3 and isinstance(chain[-1], ast.Name) and isinstance(chain[-2], ast.Attribute)
            and isinstance(chain[-3], ast.Call)):
        return False
    target = mod.imports.get(chain[-1].id)
    return any(target == obj and chain[-2].attr in methods for obj, methods in allowed)


def is_result_call(call: ast.Call, fn: FunctionInfo, kind_of, index: ProjectIndex, rules: ActiveRules,
                   resolver: Resolver) -> bool:
    """调用的是步骤、判断函数、工作流、派发或编排原语：它的返回值是"步骤结果"。"""
    call = unwrap_thread_call(call, index.modules[fn.file])
    if rules.dispatch(call, fn, resolver) is not None or is_orchestration_call(call, index.modules[fn.file], rules):
        return True
    target = resolver.resolve(call, fn).target
    return target is not None and kind_of(target) in _CALLEE_KINDS


def result_names(fn: FunctionInfo, is_result) -> set[str]:
    """每次绑定都来自步骤结果（is_result 为真的调用，可以被 await）的局部名；参数和其他方式绑定过的名字不算。"""
    a = fn.node.args
    bad = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x is not None}
    good: set[str] = set()
    handled: set[int] = set()

    def from_result(v: ast.expr | None) -> bool:
        v = v.value if isinstance(v, ast.Await) else v
        return isinstance(v, ast.Call) and is_result(v)

    def visit(n: ast.AST) -> None:
        if isinstance(n, _NESTED):
            return
        if isinstance(n, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            ok = from_result(n.value)
            for t in targets:
                if isinstance(t, ast.Name):
                    handled.add(id(t))
                    (good if ok else bad).add(t.id)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and id(n) not in handled:
            bad.add(n.id)
        for c in ast.iter_child_nodes(n):
            visit(c)

    for st in fn.node.body:
        visit(st)
    return good - bad


def is_local_method_call(call: ast.Call, resolved: Resolved, locals_: set[str], awaited: bool,
                         results: set[str] = frozenset(), is_result=lambda c: False) -> bool:
    """操作步骤结果的方法调用，且不是项目内函数：
    - 不经方法链、直接在局部名或参数上调用方法（原规则；它把任何局部名都当作步骤结果，是遗留问题）；
    - 方法链从步骤结果开始：根是 results 里的局部名，或链最里面的调用本身就是步骤 / 派发 / 编排原语；
    - 方法链上全是容器 / 字符串取值（_ACCESSORS），根是局部名或参数。"""
    if awaited or not isinstance(call.func, ast.Attribute):
        return False
    if resolved.how == "exact" and resolved.target:
        return False
    chain, root = _method_chain(call.func.value)
    if not chain:
        return _rooted_at(call.func.value, locals_)
    if is_result(chain[-1]) or _rooted_at(root, results):
        return True
    accessors = call.func.attr in _ACCESSORS and all(
        isinstance(c.func, ast.Attribute) and c.func.attr in _ACCESSORS for c in chain)
    return accessors and _rooted_at(root, locals_)


def check_workflow_call(site: CallSite, fn: FunctionInfo, kind_of, index: ProjectIndex,
                        rules: ActiveRules, resolver: Resolver, locals_: set[str],
                        results: set[str] = frozenset()) -> tuple[str, str] | None:
    """SM001 / SM002 的判断；返回 (代码, 信息)，合规时 None。results：每次绑定都来自步骤结果的局部名。"""
    mod = index.modules[fn.file]
    call = unwrap_thread_call(site.call, mod)
    if is_orchestration_call(call, mod, rules):
        return None
    resolved = resolver.resolve(call, fn)
    target_kind = kind_of(resolved.target) if resolved.target else None
    show = resolved.display

    def is_result(c: ast.Call) -> bool:
        return is_result_call(c, fn, kind_of, index, rules, resolver)

    if site.in_test:
        ok = target_kind in ("step", "condition") or is_builtin_call(call, resolved) \
            or is_local_method_call(call, resolved, locals_, site.awaited, results, is_result)
        return None if ok else ("SM002", f"判断条件里不能做计算：{show}(…)，改为引用步骤结果或判断函数")
    if rules.dispatch(call, fn, resolver) is not None:
        return None
    if target_kind in _CALLEE_KINDS or is_outcome_call(call, mod) or is_construction(call, fn, index) \
            or is_builtin_call(call, resolved) or is_local_method_call(call, resolved, locals_, site.awaited, results,
                                                                       is_result):
        return None
    return ("SM001", f"编排函数只能调用步骤：{show}(…) 不是步骤")


def check_step_call(call: ast.Call, fn: FunctionInfo, kind_of, resolver: Resolver) -> str | None:
    """SM003：步骤里调用另一个步骤（只信精确解析；asyncio.to_thread(另一个步骤, …) 也算）。"""
    resolved = resolver.resolve(unwrap_thread_call(call, resolver.index.modules[fn.file]), fn)
    if resolved.how != "exact" or not resolved.target or resolved.target == fn.key:
        return None
    if kind_of(resolved.target) != "step":
        return None
    return f"步骤 {fn.node.name} 里不能调用另一个步骤 {resolver.index.functions[resolved.target].node.name}"


# ---------- 入口 ----------

def _norm_files(root: Path, files: list[str]) -> set[str]:
    out = set()
    for f in files:
        p = Path(f)
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(root)
            except ValueError:
                pass
        out.add(p.as_posix())
    return out


def check_project(root: Path, config: ProjectConfig | None = None, *,
                  treat_as_marked: frozenset[str] = frozenset(),
                  only_files: list[str] | None = None) -> CheckResult:
    """检查 root 下带标记的函数（only_files 给出时只检查这些文件里的）。解析不了的文件没法检查，在检查范围内的
    列在结果的 skipped 里；不是项目源文件、没有读的路径（见 index.NOT_SOURCE_REASONS）列在 ignored 里。"""
    config = config or ProjectConfig()
    root = Path(root).resolve()
    index = build_index(root, config)
    rules, resolver = ActiveRules(index, config), Resolver(index)
    wanted = _norm_files(root, only_files) if only_files is not None else None
    kinds = {k: rules.classify(f) for k, f in index.functions.items()}

    def kind_of(key: str) -> str:
        return kinds[key].kind

    out: list[Violation] = []
    for key in sorted(index.functions):
        fn, info = index.functions[key], kinds[key]
        if wanted is not None and fn.file not in wanted:
            continue
        if not (info.marked or info.rule in treat_as_marked) or info.kind not in ("workflow", "step"):
            continue
        calls, nested = collect_body(fn)
        if info.kind == "step":
            for site in calls:
                msg = check_step_call(site.call, fn, kind_of, resolver)
                if msg:
                    out.append(Violation(fn.file, site.call.lineno, "SM003", msg))
            continue
        locals_ = local_names(fn)
        results = result_names(fn, lambda c, fn=fn: is_result_call(c, fn, kind_of, index, rules, resolver))
        for site in calls:
            r = check_workflow_call(site, fn, kind_of, index, rules, resolver, locals_, results)
            if r:
                out.append(Violation(fn.file, site.call.lineno, r[0], r[1]))
        for n in nested:
            out.append(Violation(fn.file, n.lineno, "SM004", "编排函数里不能定义内部函数"))
    in_scope = [(f, why) for f, why in index.skipped if wanted is None or f in wanted]
    skipped = [(f, why) for f, why in in_scope if why not in NOT_SOURCE_REASONS]
    ignored = [(f, why) for f, why in in_scope if why in NOT_SOURCE_REASONS]
    known = set(index.modules) | {f for f, _ in index.skipped}
    unmatched = sorted(wanted - known) if wanted is not None else []
    return CheckResult(sorted(out, key=lambda v: (v.file, v.line, v.code)), skipped, unmatched, ignored)
