"""识别规则：哪些函数是入口、哪些调用是派发、哪些调用是噪音。纯静态匹配，不导入被分析代码。"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .config import ProjectConfig
from .external import ExternalObjects
from .index import FunctionInfo, ModuleInfo, ProjectIndex, _attr_chain
from .index import _scope_bindings as _all_bindings
from .model import FnKind, Resolution
from .resolve import dotted_name_of

if TYPE_CHECKING:
    from .resolve import Resolver


@dataclass
class EntryInfo:
    kind: FnKind
    title: str | None = None
    group: str | None = None
    route: str | None = None
    retries: bool = False
    marked: bool = False
    rule: str | None = None  # 识别出该入口的规则集名
    name: str | None = None  # pystepkit workflow 的 name=
    version: str | None = None  # pystepkit workflow 的 version=


@dataclass
class DispatchRule:
    methods: list[str]  # 方法名，如 ["kiq"]
    task_from: Literal["receiver", "arg0"]  # 任务在接收者表达式里，还是参数里
    dynamic_ok: bool = True  # 找不到任务名时是否仍算派发（目标未知）


@dataclass
class RuleSet:
    name: str
    entry_decorators: list[tuple[str, FnKind]]  # (匹配装饰器源码的正则, 类型)
    dispatch: list[DispatchRule] = field(default_factory=list)
    retry_keywords: list[str] = field(default_factory=list)


_ROUTE_METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "websocket", "api_route")
_HTTP = r"(" + "|".join(_ROUTE_METHODS) + r")\b"
# 模块级实例是这些类时，它上面的 _ROUTE_METHODS 装饰器都是路由（不论变量叫什么）
_FASTAPI_CLASSES = frozenset({"fastapi.FastAPI", "fastapi.APIRouter", "fastapi.applications.FastAPI",
                              "fastapi.routing.APIRouter"})
BUILTIN: dict[str, RuleSet] = {
    "fastapi": RuleSet("fastapi", [(r"^\w*router\." + _HTTP, "route"), (r"^app\." + _HTTP, "route")], []),
    # Flask（以及写法相同的 Quart、Sanic、Bottle）：@app.route(...) / @bp.route(...)；@app.get(...) 这类由 fastapi 那条认
    "flask": RuleSet("flask", [(r"\.route\b", "route")], []),
    "taskiq": RuleSet("taskiq", [(r"\.task\b", "task")],
                      [DispatchRule(["kiq"], "receiver")], ["retry_on_error"]),
    # Celery：@shared_task、@app.task；派发是 任务.delay(...) / 任务.apply_async(...)。只在项目导入了 celery 时启用
    # （_uses_celery），这时 @x.task 归它、不归 taskiq
    "celery": RuleSet("celery", [(r"^(celery\.)?shared_task\b", "task"), (r"\.task\b", "task")],
                      [DispatchRule(["delay", "apply_async"], "receiver", dynamic_ok=False)]),
    "dbos": RuleSet("dbos", [(r"^DBOS\.workflow\b", "workflow"), (r"^DBOS\.step\b", "step")], [
        DispatchRule(["start_workflow", "start_workflow_async"], "arg0"),
        DispatchRule(["enqueue", "enqueue_async", "enqueue_workflow", "enqueue_workflow_async"], "arg0",
                     dynamic_ok=False)]),
    "pystepkit": RuleSet("pystepkit", []),  # 不按源码文字匹配：装饰器要经 import 来自 pystepkit（见 _pystepkit_mark）
}
_PYSTEPKIT_MARKS: dict[str, FnKind] = {"workflow": "workflow", "step": "step", "node": "step", "condition": "condition"}

_COUNTER_METHODS = {"inc", "dec", "observe", "set"}


def known_rule_names(config: ProjectConfig) -> set[str]:
    """内置识别规则和配置里 extra_rules 的规则名（不论项目里有没有用到）。"""
    return set(BUILTIN) | {d.get("name", "extra") for d in config.extra_rules}


def rule_entry_kinds(config: ProjectConfig) -> dict[str, set[FnKind]]:
    """每条识别规则（内置的和配置里 extra_rules 的）能识别出的入口类型。pystepkit 的标记不按正则认，这里是空集。"""
    sets = [*BUILTIN.values(), *(_ruleset_from_dict(d) for d in config.extra_rules)]
    return {rs.name: {k for _, k in rs.entry_decorators} for rs in sets}


def _ruleset_from_dict(d: dict) -> RuleSet:
    return RuleSet(
        name=d.get("name", "extra"),
        entry_decorators=[(p, k) for p, k in d.get("entry_decorators", [])],
        dispatch=[DispatchRule(list(x.get("methods", [])), x.get("task_from", "arg0"),
                               x.get("dynamic_ok", True)) for x in d.get("dispatch", [])],
        retry_keywords=list(d.get("retry_keywords", [])))


def _str_kw(dec: ast.expr, name: str) -> str | None:
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
    return None


def _module_strings(mod: ModuleInfo) -> dict[str, str]:
    """模块级的字符串常量：名字 -> 值。只算写在模块顶层（不在 if / try 里）的 NAME = "……"（可带注解），而且这个名字
    在模块里只绑定了这一次、没有函数用 global 改写它。"""
    bindings = _all_bindings(mod.tree.body)
    rebound = {name for n in ast.walk(mod.tree) if isinstance(n, ast.Global) for name in n.names}
    out: dict[str, str] = {}
    for st in mod.tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            target, value = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign):
            target, value = st.target, st.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str) \
                and len(bindings.get(target.id, ())) == 1 and target.id not in rebound:
            out[target.id] = value.value
    return out


# 标记的参数读不出来时，当作没写之后的结果（mark_notes 里说）
_UNREAD_EFFECT = {"group": "这条功能线按文件名（或入口的种类）分组", "title": "标题改用文档注释或函数名"}
MAX_CONST_HOPS = 4  # 常量经导入转了几手还跟


def _str_arg(dec: ast.Call, pos: int, name: str) -> str | None:
    """第 pos 个位置参数或关键字参数 name 的字符串常量。"""
    v = dec.args[pos] if len(dec.args) > pos else next((k.value for k in dec.keywords if k.arg == name), None)
    return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None


def _constructor_call(mod: ModuleInfo, name: str) -> ast.Call | None:
    """模块级 name = 某某(...) 的那个调用（多次赋值取最后一次，同 ModuleInfo.instances）。"""
    found = None
    for st in mod.tree.body:
        target = st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else \
            st.target if isinstance(st, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == name and isinstance(st.value, ast.Call):
            found = st.value
    return found


_OTHER = object()  # 名字在这一层绑定成了别的东西（参数、循环变量、解包赋值、嵌套的 def / class、import……）


def _target_names(t: ast.expr) -> list[str]:
    """赋值目标里绑定的名字（解包、星号展开都算；属性、下标不算）。"""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Tuple | ast.List):
        return [n for e in t.elts for n in _target_names(e)]
    if isinstance(t, ast.Starred):
        return _target_names(t.value)
    return []


def _scope_bindings(scope: ast.AST) -> dict[str, object]:
    """函数 scope 这一层绑定的名字 -> 按源码顺序最后一次绑定的值：`x = 值`、`x: T = 值`、`x = y = 值`、`(x := 值)`
    是那个值的表达式；参数、解包赋值、循环变量、with … as、except … as、增量赋值、import、嵌套的 def / class 是 _OTHER。
    不进嵌套的函数、类、lambda 的里面（那是别的作用域）。"""
    out: dict[str, object] = {}
    args = getattr(scope, "args", None)
    if isinstance(args, ast.arguments):
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None:
                out[a.arg] = _OTHER
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        st = stack.pop(0)
        if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out[st.name] = _OTHER
            continue
        if isinstance(st, ast.Lambda | ast.arguments):
            continue
        if isinstance(st, ast.Assign):
            for t in st.targets:
                for n in _target_names(t):
                    out[n] = st.value if isinstance(t, ast.Name) else _OTHER
        elif isinstance(st, ast.AnnAssign):
            if isinstance(st.target, ast.Name) and st.value is not None:
                out[st.target.id] = st.value
        elif isinstance(st, ast.NamedExpr):
            out[st.target.id] = st.value
        elif isinstance(st, ast.AugAssign | ast.For | ast.AsyncFor | ast.comprehension):
            for n in _target_names(st.target):
                out[n] = _OTHER
        elif isinstance(st, ast.withitem):
            if st.optional_vars is not None:
                for n in _target_names(st.optional_vars):
                    out[n] = _OTHER
        elif isinstance(st, ast.ExceptHandler):
            if st.name:
                out[st.name] = _OTHER
        elif isinstance(st, ast.Import | ast.ImportFrom):
            for al in st.names:
                out[(al.asname or al.name).split(".")[0]] = _OTHER
        stack[0:0] = list(ast.iter_child_nodes(st))
    return out


def _route_receiver(dec: ast.expr) -> str | None:
    """@x.get(...) 等路由装饰器里的 x；不是这个样子时 None。"""
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in _ROUTE_METHODS \
            and isinstance(dec.func.value, ast.Name):
        return dec.func.value.id
    return None


def _route_title(dec: ast.Call, mod: ModuleInfo, ctor: ast.Call | None = None) -> str:
    """路由标题 "方法 前缀路径"：websocket 为 WS；api_route 取字面量的 methods=（省略时同 FastAPI 为 GET，不是
    字面量时为 ROUTE）；前缀取 APIRouter(prefix=...) 的字面量（ctor：外层函数里的构造调用；没有时找同一模块的模块级
    实例）。"""
    attr = dec.func.attr
    if attr == "websocket":
        method = "WS"
    elif attr in ("api_route", "route"):  # route 是 Flask 的写法：methods= 省略时也是 GET
        given = next((k.value for k in dec.keywords if k.arg == "methods"), None)
        if given is None:
            method = "GET"
        elif isinstance(given, ast.List | ast.Tuple) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in given.elts):
            method = ",".join(e.value.upper() for e in given.elts)
        else:
            method = "ROUTE"
    else:
        method = attr.upper()
    prefix = ""
    if isinstance(dec.func.value, ast.Name):
        ctor = ctor or _constructor_call(mod, dec.func.value.id)
        if ctor is not None and name_of(ctor.func) == "APIRouter":
            prefix = _str_kw(ctor, "prefix") or ""
        elif ctor is not None and name_of(ctor.func) == "Blueprint":
            prefix = _str_kw(ctor, "url_prefix") or ""
    return f"{method} {prefix}{_str_arg(dec, 0, 'path') or ''}".strip()


def pystepkit_name(e: ast.expr, mod: ModuleInfo) -> str | None:
    """名字或属性链经本模块的 import 展开后在 pystepkit 或 pystepkit.* 里时，返回它的最后一段（workflow、outcome……）；
    否则 None。from pystepkit import workflow [as wf]、import pystepkit [as fk] 后的 fk.workflow 都算。"""
    dotted = dotted_name_of(e, mod)
    return dotted.rpartition(".")[2] if dotted and dotted.startswith("pystepkit.") else None


def _pystepkit_mark(dec: ast.expr, mod: ModuleInfo) -> FnKind | None:
    """装饰器是 pystepkit 的 workflow / step / node / condition（可以带参数）时，返回对应的类型。"""
    return _PYSTEPKIT_MARKS.get(pystepkit_name(dec.func if isinstance(dec, ast.Call) else dec, mod) or "")


def _fastapi_instance_route(dec: ast.expr, mod: ModuleInfo) -> bool:
    """@x.get(...) 等，x 是本模块里 FastAPI() / APIRouter() 的模块级实例。"""
    name = _route_receiver(dec)
    return name is not None and mod.instances.get(name) in _FASTAPI_CLASSES


def _leftmost_name(node: ast.expr) -> str | None:
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return None


@dataclass
class Dispatch:
    """一个派发调用：被派发的任务。"""

    name: str | None  # 任务表达式里的名字；找不到任务时 None（目标未知）
    target: str | None  # 被派发函数的 key
    how: Resolution


def name_of(e: ast.expr) -> str | None:
    """名字的 id、属性的属性名；其他表达式为 None。"""
    return e.id if isinstance(e, ast.Name) else e.attr if isinstance(e, ast.Attribute) else None


class ActiveRules:
    def __init__(self, index: ProjectIndex, config: ProjectConfig):
        self.index = index
        self.config = config
        self.external = ExternalObjects(index, config.noise_receivers)  # 建图时判断"落在项目外对象上的调用"用
        self._compiled: dict[str, list[tuple[re.Pattern, FnKind]]] = {}
        self._sets: dict[str, RuleSet] = {}
        texts = [ast.unparse(d) for f in index.functions.values() for d in f.decorators]
        texts += [ast.unparse(d) for m in index.modules.values()
                  for c in m.classes.values() for d in c.decorators]
        imported = {d.split(".")[0] for m in index.modules.values() for d in m.imports.values()}
        skip = "taskiq" if "celery" in imported and "taskiq" not in imported else "celery"
        for rs in BUILTIN.values():
            if rs.name != skip and any(re.search(p, t) for p, _ in rs.entry_decorators for t in texts):
                self._add(rs)
        self._bindings: dict[int, dict[str, object]] = {}  # id(函数的 AST 节点) -> 这一层绑定的名字（每个函数只扫一次）
        self._strings: dict[str, dict[str, str]] = {}  # 模块相对路径 -> 模块级字符串常量（用到时才扫）
        self._unread: dict[tuple[str, int, str], str] = {}  # (文件, 行, 参数名) -> 标记里读不出来的参数的原文
        if "fastapi" not in self._sets and any(
                self._fastapi_route(d, index.modules[f.file], self._scopes(f))
                for f in index.functions.values() for d in f.decorators):
            self._add(BUILTIN["fastapi"])
        decorated = [(d, index.modules[f.file]) for f in index.functions.values() for d in f.decorators]
        decorated += [(d, m) for m in index.modules.values() for c in m.classes.values() for d in c.decorators]
        if any(_pystepkit_mark(d, m) for d, m in decorated):
            self._add(BUILTIN["pystepkit"])
        for d in config.extra_rules:
            self._add(_ruleset_from_dict(d))
        self.names: list[str] = list(self._sets)
        self.task_names: set[str] = set()  # 任务 / 工作流的名字（pystepkit 类形式工作流用类名）：派发识别按它找任务表达式
        self.tasks: dict[str, str] = {}  # 只有一个任务叫这个名字时：名字 -> key（派发解析不了时按它猜）
        self._class_tasks: dict[int, str] = {}  # id(pystepkit 类形式工作流的 ClassInfo) -> 入口方法的 key
        self._task_keys: set[str] = set()
        self._build_tasks()

    def _add(self, rs: RuleSet) -> None:
        self._sets[rs.name] = rs
        self._compiled[rs.name] = [(re.compile(p), k) for p, k in rs.entry_decorators]

    def _scopes(self, fn: FunctionInfo) -> list[ast.AST]:
        """fn 外层的函数（从最近的一层往外）。"""
        out: list[ast.AST] = []
        parent = fn.parent
        while parent is not None and parent in self.index.functions:
            out.append(self.index.functions[parent].node)
            parent = self.index.functions[parent].parent
        return out

    def _local_ctor(self, dec: ast.expr, mod: ModuleInfo, scopes: list[ast.AST]) -> ast.Call | None | bool:
        """@x.get(...) 等，x 在外层函数（scopes：从最近的一层往外）里绑定过时，看最近绑定它的那一层：最后一次绑定是
        x = FastAPI(...) / APIRouter(...) → 返回那个构造调用；绑定成了别的（参数、别的值）→ False（这个 x 不是模块级
        的那个实例，也不再往外找）。哪一层都没绑定过、或者装饰器不是这个样子 → None。"""
        name = _route_receiver(dec)
        if name is None:
            return None
        for scope in scopes:
            found = self._bindings.get(id(scope))
            if found is None:
                found = self._bindings[id(scope)] = _scope_bindings(scope)
            if name not in found:
                continue
            value = found[name]
            chain = _attr_chain(value.func) if isinstance(value, ast.Call) else None
            if chain and chain[0] in mod.imports and ".".join([mod.imports[chain[0]], *chain[1:]]) in _FASTAPI_CLASSES:
                return value
            return False
        return None

    def _fastapi_route(self, dec: ast.expr, mod: ModuleInfo, scopes: list[ast.AST]) -> bool:
        """装饰器是 FastAPI() / APIRouter() 实例上的路由：外层函数里的局部实例；外层没有绑定这个名字时看模块级的。"""
        local = self._local_ctor(dec, mod, scopes)
        return _fastapi_instance_route(dec, mod) if local is None else local is not False

    def _match_one(self, dec: ast.expr, mod: ModuleInfo, scopes: list[ast.AST]):
        """一个装饰器对上的 (规则集, 类型, 装饰器)；对不上时 None。"""
        mark = _pystepkit_mark(dec, mod) if "pystepkit" in self._sets else None
        if mark is not None:
            return self._sets["pystepkit"], mark, dec
        text = ast.unparse(dec)
        for name, pats in self._compiled.items():
            for pat, kind in pats:
                if pat.search(text):
                    return self._sets[name], kind, dec
        if "fastapi" in self._sets and self._fastapi_route(dec, mod, scopes):
            return self._sets["fastapi"], "route", dec
        return None

    def _matches(self, decorators: list[ast.expr], mod: ModuleInfo, scopes: list[ast.AST] | None = None) -> list:
        """每个对得上的装饰器的 (规则集, 类型, 装饰器)，排在前面的说了算：pystepkit 标记最先，其次是别的类型，接口
        （route）最后——同一个函数既是工作流又挂在路由上时算工作流，两个装饰器谁写在上面都一样。同一档里按源码顺序。"""
        found = [m for m in (self._match_one(d, mod, scopes or []) for d in decorators) if m is not None]
        return sorted(found, key=lambda m: 0 if m[0].name == "pystepkit" else 2 if m[1] == "route" else 1)

    def _match(self, decorators: list[ast.expr], mod: ModuleInfo, scopes: list[ast.AST] | None = None):
        """返回 (规则集, 类型, 装饰器)；无匹配时 None。pystepkit 标记按 import 来源认；其他规则按装饰器源码的正则匹配；
        另外 FastAPI() / APIRouter() 的实例（模块级的，或者 scopes——外层函数——里的局部变量，比如 create_app 里的
        api = FastAPI()）上的路由装饰器不论变量名都算路由。几个装饰器都对得上时见 _matches。"""
        found = self._matches(decorators, mod, scopes)
        return found[0] if found else None

    def _const_str(self, e: ast.expr, mod: ModuleInfo, hops: int = 0) -> str | None:
        """表达式的字符串值：字面量；模块级的字符串常量（_module_strings）——本模块的名字、from 导入的名字、
        导入的模块.名字，项目内转手导入的接着跟。读不出来时 None。"""
        if isinstance(e, ast.Constant):
            return e.value if isinstance(e.value, str) else None
        chain = _attr_chain(e)
        if not chain or hops > MAX_CONST_HOPS:
            return None
        if len(chain) == 1 and chain[0] in self._module_strs(mod):
            return self._module_strs(mod)[chain[0]]
        if chain[0] not in mod.imports:
            return None
        module, _, name = ".".join([mod.imports[chain[0]], *chain[1:]]).rpartition(".")
        rel = self.index.by_dotted.get(module)
        return None if rel is None else self._const_str(ast.Name(id=name, ctx=ast.Load()), self.index.modules[rel], hops + 1)

    def _module_strs(self, mod: ModuleInfo) -> dict[str, str]:
        if mod.rel not in self._strings:
            self._strings[mod.rel] = _module_strings(mod)
        return self._strings[mod.rel]

    def _mark_str(self, dec: ast.expr, name: str, mod: ModuleInfo) -> str | None:
        """pystepkit 标记的关键字参数 name 的字符串值（见 _const_str）。写了却读不出来的（算出来的值、绑定了几次的
        名字、项目外的常量）当作没写，并记下是哪一处：mark_notes 里说出来，不能悄悄丢掉。"""
        kw = next((k for k in dec.keywords if k.arg == name), None) if isinstance(dec, ast.Call) else None
        if kw is None:
            return None
        value = self._const_str(kw.value, mod)
        if value is None and not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
            self._unread[(mod.rel, kw.value.lineno, name)] = ast.unparse(kw.value)
        return value

    def mark_notes(self) -> list[str]:
        """标记里读不出来的参数，每处一句（按文件、行排）。classify 过的函数才有：分析时每个函数都会 classify 一次。"""
        return [f"{file}:{line} 标记里的 {name}={text} 读不出来，当作没写（{_UNREAD_EFFECT.get(name, '按没有这个参数处理')}）："
                "要写成字符串字面量，或者模块级只赋值一次的字符串常量"
                for (file, line, name), text in sorted(self._unread.items())]

    def _class_info(self, fn: FunctionInfo):
        mod = self.index.modules.get(fn.file)
        return mod.classes.get(fn.cls) if mod and fn.cls else None

    def _pystepkit_class_entry(self, fn: FunctionInfo) -> ast.expr | None:
        """fn 是 pystepkit 类形式工作流的入口方法时，返回类上的 workflow 装饰器；否则 None。"""
        cls = self._class_info(fn)
        if cls is None:
            return None
        m = self._match(cls.decorators, self.index.modules[fn.file])
        if m and m[0].name == "pystepkit" and m[1] == "workflow":
            entry = self._mark_str(m[2], "entry", self.index.modules[fn.file]) or "run"
            if fn.node.name == entry and fn.qualname == f"{cls.name}.{entry}":
                return m[2]
        return None

    def classify(self, fn: FunctionInfo) -> EntryInfo:
        dec = self._pystepkit_class_entry(fn)
        mod = self.index.modules[fn.file]
        read = lambda name: self._mark_str(dec, name, mod)
        if dec is not None:
            return EntryInfo("workflow", read("title"), read("group"), marked=True, rule="pystepkit",
                             name=read("name"), version=read("version"))
        scopes = self._scopes(fn)
        found = self._matches(fn.decorators, mod, scopes)
        if not found:
            return EntryInfo("function")
        rs, kind, dec = found[0]
        info = EntryInfo(kind, rule=rs.name)
        if rs.name == "pystepkit":
            info.marked = True
            info.title, info.group = read("title"), read("group")
            if kind == "workflow":
                info.name, info.version = read("name"), read("version")
        # 路由装饰器：自己就是接口，或者工作流、任务同时挂在路由上——"方法 路径" 照样记下
        rdec = next((d for _, k, d in found if k == "route"), None)
        if isinstance(rdec, ast.Call) and isinstance(rdec.func, ast.Attribute):
            info.route = _route_title(rdec, mod, self._local_ctor(rdec, mod, scopes) or None)
        if isinstance(dec, ast.Call):
            info.retries = any(kw.arg in rs.retry_keywords and isinstance(kw.value, ast.Constant)
                               and kw.value.value is True for kw in dec.keywords)
        return info

    def _build_tasks(self) -> None:
        """任务名登记：任务和工作流按函数名；pystepkit 类形式工作流按类名（入口方法的名字不登记）。"""
        by_name: dict[str, set[str]] = {}
        for fn in self.index.functions.values():
            if self.classify(fn).kind not in ("task", "workflow"):
                continue
            if self._pystepkit_class_entry(fn) is not None:
                cls = self._class_info(fn)
                self._class_tasks[id(cls)] = fn.key
                name = cls.name
            else:
                name = fn.node.name
            by_name.setdefault(name, set()).add(fn.key)
        self.task_names = set(by_name)
        self.tasks = {name: next(iter(keys)) for name, keys in by_name.items() if len(keys) == 1}
        self._task_keys = {k for keys in by_name.values() for k in keys}

    def dispatch(self, call: ast.Call, fn: FunctionInfo, resolver: Resolver) -> Dispatch | None:
        """函数 fn 里的调用是不是派发；是时给出被派发的任务。不是派发时 None。
        任务在接收者链里（kiq）或参数里（DBOS）。先按解析器找：其中第一个解析到任务函数的名字 / 属性（按导入、模块级
        符号，exact；pystepkit 类形式工作流的类对应到入口方法）；没有时取第一个名字是某个任务名的，再解析它（_named_task）；
        都没有时，规则允许的话算目标未知的派发。"""
        if not isinstance(call.func, ast.Attribute):
            return None
        mod = self.index.modules[fn.file]
        for rs in self._sets.values():
            for rule in rs.dispatch:
                if call.func.attr not in rule.methods:
                    continue
                roots = [call.func.value] if rule.task_from == "receiver" else [*call.args, *(k.value for k in call.keywords)]
                candidates = [n for root in roots for n in ast.walk(root) if isinstance(n, ast.Name | ast.Attribute)]
                for n in candidates:
                    key = self._task_of(resolver.symbol(n, mod, fn))
                    if key:
                        return Dispatch(name_of(n), key, "exact")
                named = next((n for n in candidates if name_of(n) in self.task_names), None)
                if named is not None:
                    return Dispatch(name_of(named), *self._named_task(named, fn, resolver))
                if rule.dynamic_ok:
                    return Dispatch(None, None, "unresolved")
        return None

    def _named_task(self, e: ast.expr, fn: FunctionInfo, resolver: Resolver) -> tuple[str | None, Resolution]:
        """名字是某个任务名、但没有解析到任务的表达式（在函数 fn 里）→ (被派发函数的 key, 解析方式)：解析到项目内的
        函数时就是它（exact）；解析到项目外为 external；解析不了时，恰好一个任务叫这个名字才按名字对应（guessed），
        同名的有几个就无法判断（unresolved）。"""
        sym = resolver.symbol(e, self.index.modules[fn.file], fn)
        kind = sym[0] if sym else None
        if kind == "func":
            return sym[1], "exact"
        if kind == "external":
            return None, "external"
        key = self.tasks.get(name_of(e))
        return (key, "guessed") if key else (None, "unresolved")

    def _task_of(self, sym) -> str | None:
        """符号是任务函数（或 pystepkit 类形式工作流的类）时，任务函数的 key。"""
        if sym and sym[0] == "func" and sym[1] in self._task_keys:
            return sym[1]
        if sym and sym[0] == "class":
            return self._class_tasks.get(id(sym[1]))
        return None

    def is_noise(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return hasattr(builtins, func.id) and not func.id.startswith("_")
        if isinstance(func, ast.Attribute):
            if func.attr in _COUNTER_METHODS and isinstance(func.value, ast.Call) \
                    and isinstance(func.value.func, ast.Attribute) and func.value.func.attr == "labels":
                return True
            return _leftmost_name(func.value) in self.config.noise_receivers
        return False
