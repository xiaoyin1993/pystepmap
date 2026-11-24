"""调用解析：判断一个调用指向哪个项目内函数——精确、带标记的猜测、项目外，或无法判断。

宁可答 unresolved 也不连错线：任何一层拿不准都往下一层走，最后仍不确定就放弃。
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator
from dataclasses import dataclass

from .index import ClassInfo, FunctionInfo, ModuleInfo, ProjectIndex, import_bindings
from .model import Resolution

# 方法名太常见时，即使项目里只有一个定义也不猜
COMMON_NAMES = frozenset({"get", "set", "delete", "run", "execute", "commit", "close", "add", "send",
                          "start", "stop", "list", "create", "update", "save", "load"})

# 常见内置 / 标准库类型（str、bytes、list、dict、set、tuple、int、float、io.IOBase、io.TextIOWrapper、io.BytesIO、
# pathlib.Path、re.Pattern、re.Match、datetime 的 datetime / date / timedelta、asyncio 的 Future / Task / Queue / Lock /
# Event、logging.Logger、threading.Thread、subprocess.Popen、生成器、协程、异步生成器）上可调用、不以 _ 开头的方法名：
# 接收者类型未知时，x.open()、x.read() 多半调用的是它们的方法，这些方法名不按名字推测。写死成清单（Python 3.12 与
# 3.13 算出的并集），分析结果不随运行的 Python 版本变；重新生成见 core/tests/builtin_method_names.py。
BUILTIN_METHOD_NAMES = frozenset({
    "absolute", "aclose", "acquire", "add", "addFilter", "addHandler", "add_done_callback", "append",
    "as_integer_ratio", "as_posix", "as_uri", "asend", "astimezone", "athrow", "bit_count", "bit_length",
    "callHandlers", "cancel", "cancelled", "cancelling", "capitalize", "casefold", "center", "chmod", "clear",
    "close", "combine", "communicate", "conjugate", "copy", "count", "critical", "ctime", "cwd", "date", "debug",
    "decode", "detach", "difference", "difference_update", "discard", "done", "dst", "empty", "encode", "end",
    "endswith", "error", "exception", "exists", "expand", "expandtabs", "expanduser", "extend", "fatal", "fileno",
    "filter", "find", "findCaller", "findall", "finditer", "flush", "format", "format_map", "from_bytes",
    "from_uri", "fromhex", "fromisocalendar", "fromisoformat", "fromkeys", "fromordinal", "fromtimestamp", "full",
    "full_match", "fullmatch", "get", "getChild", "getChildren", "getEffectiveLevel", "getName", "get_context",
    "get_coro", "get_loop", "get_name", "get_nowait", "get_stack", "getbuffer", "getvalue", "glob", "group",
    "groupdict", "groups", "handle", "hardlink_to", "hasHandlers", "hex", "home", "index", "info", "insert",
    "intersection", "intersection_update", "isDaemon", "isEnabledFor", "is_absolute", "is_alive", "is_block_device",
    "is_char_device", "is_dir", "is_fifo", "is_file", "is_integer", "is_junction", "is_mount", "is_relative_to",
    "is_reserved", "is_set", "is_socket", "is_symlink", "isalnum", "isalpha", "isascii", "isatty", "isdecimal",
    "isdigit", "isdisjoint", "isidentifier", "islower", "isnumeric", "isocalendar", "isoformat", "isoweekday",
    "isprintable", "isspace", "issubset", "issuperset", "istitle", "isupper", "items", "iterdir", "join",
    "joinpath", "keys", "kill", "lchmod", "ljust", "locked", "log", "lower", "lstat", "lstrip", "makeRecord",
    "maketrans", "match", "mkdir", "now", "open", "owner", "partition", "poll", "pop", "popitem", "print_stack",
    "put", "put_nowait", "qsize", "read", "read1", "read_bytes", "read_text", "readable", "readinto", "readinto1",
    "readline", "readlines", "readlink", "reconfigure", "relative_to", "release", "remove", "removeFilter",
    "removeHandler", "remove_done_callback", "removeprefix", "removesuffix", "rename", "replace", "resolve",
    "result", "reverse", "rfind", "rglob", "rindex", "rjust", "rmdir", "rpartition", "rsplit", "rstrip", "run",
    "samefile", "scanner", "search", "seek", "seekable", "send", "send_signal", "set", "setDaemon", "setLevel",
    "setName", "set_exception", "set_name", "set_result", "setdefault", "shutdown", "sort", "span", "split",
    "splitlines", "start", "startswith", "stat", "strftime", "strip", "strptime", "sub", "subn", "swapcase",
    "symlink_to", "symmetric_difference", "symmetric_difference_update", "task_done", "tell", "terminate", "throw",
    "time", "timestamp", "timetuple", "timetz", "title", "to_bytes", "today", "toordinal", "total_seconds", "touch",
    "translate", "truncate", "tzname", "uncancel", "union", "unlink", "update", "upper", "utcfromtimestamp",
    "utcnow", "utcoffset", "utctimetuple", "values", "wait", "walk", "warn", "warning", "weekday", "with_name",
    "with_segments", "with_stem", "with_suffix", "writable", "write", "write_bytes", "write_text", "writelines",
    "zfill"
})

_BUILTIN_NAMES = frozenset(dir(builtins))
_LITERALS = (ast.Constant, ast.JoinedStr, ast.List, ast.Dict, ast.Set, ast.Tuple,
             ast.ListComp, ast.DictComp, ast.SetComp)
_PROTOCOLS = frozenset({"typing.Protocol", "typing_extensions.Protocol"})
_THREAD_WRAPPERS = frozenset({"asyncio.to_thread"})  # 把函数交给别的线程执行：第一个参数是被执行的函数

# 符号：("func", key) / ("class", ClassInfo) / ("inst", ClassInfo) / ("module", 点分名) / ("external",)
# / ("cls", ClassInfo)：classmethod 的首参，是这个类或它的某个子类本身（取属性同类和实例，调用见 _cls_call）
Sym = tuple | None


@dataclass
class Resolved:
    target: str | None  # 函数 key
    how: Resolution
    display: str  # "svc.match_with_auth"、"load_raw"
    constructor: bool = False  # 调用的是项目内的类本身，target 是它（或最近的项目基类）的 __init__


def _is_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """方法体只是占位：...、pass、只有文档注释、raise NotImplementedError（可以带文档注释）。"""
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    for st in body:
        if isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                                        and st.value.value is Ellipsis):
            continue
        if isinstance(st, ast.Raise):
            exc = st.exc.func if isinstance(st.exc, ast.Call) else st.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                continue
        return False
    return True


def _short(e: ast.expr) -> str:
    """接收者的显示名：只留最后一个部分。"""
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        return e.attr
    if isinstance(e, ast.Call):
        return _short(e.func)
    if isinstance(e, ast.Subscript | ast.Await):
        return _short(e.value)
    return "…"


def _display(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_short(func.value)}.{func.attr}"
    # 被调用的是另一个调用的结果（f()()）、下标取出来的东西（handlers[k]()）：写出来，不和里面那个调用同名——
    # 否则自检里"目标不明：f"看上去像是 f 本身没解析到，其实不明的是它返回的东西
    if isinstance(func, ast.Call):
        return f"{_display(func.func)}()"
    if isinstance(func, ast.Subscript):
        return f"{_short(func.value)}[…]"
    return _short(func)


def dotted_name_of(e: ast.expr, mod: ModuleInfo) -> str | None:
    """名字或属性链在本模块里的点分全名（根名按 import 展开）；其他表达式为 None。"""
    parts: list[str] = []
    while isinstance(e, ast.Attribute):
        parts.append(e.attr)
        e = e.value
    if not isinstance(e, ast.Name):
        return None
    return ".".join([mod.imports.get(e.id, e.id), *reversed(parts)])


def unwrap_thread_call(call: ast.Call, mod: ModuleInfo) -> ast.Call:
    """把函数交给别的线程执行，等于在那个线程里调用它：asyncio.to_thread(f, *a, **kw) → f(*a, **kw)，
    <事件循环>.run_in_executor(执行器, f, *a) → f(*a)。f 要直接写出名字或属性（lambda、partial(...) 等不展开：
    lambda 体里的调用本来就算外层函数的调用）。返回代表被执行的那次调用的节点（位置同原调用，每次新建）；
    其他调用原样返回。构建函数图和约束检查共用。"""
    args = call.args
    if args and isinstance(args[0], ast.Name | ast.Attribute) and dotted_name_of(call.func, mod) in _THREAD_WRAPPERS:
        func, rest, keywords = args[0], args[1:], call.keywords
    elif isinstance(call.func, ast.Attribute) and call.func.attr == "run_in_executor" and len(args) >= 2 \
            and isinstance(args[1], ast.Name | ast.Attribute) and not isinstance(args[0], ast.Starred):
        func, rest, keywords = args[1], args[2:], []
    else:
        return call
    return ast.copy_location(ast.Call(func=func, args=rest, keywords=keywords), call)


def _value_exprs(e: ast.expr | None) -> Iterator[ast.expr]:
    """作为值出现的名字 / 属性：e 本身，或列表、元组、集合字面量里的元素（可以嵌套）。"""
    if isinstance(e, ast.Starred):
        e = e.value
    if isinstance(e, ast.Name | ast.Attribute):
        yield e
    elif isinstance(e, ast.List | ast.Tuple | ast.Set):
        for x in e.elts:
            yield from _value_exprs(x)


class Resolver:
    def __init__(self, index: ProjectIndex):
        self.index = index
        self._tops = {d.split(".")[0] for d in [*index.by_dotted, *index.ambiguous]}  # 项目内模块的顶层名
        self._locals_cache: dict[str, dict[str, Sym]] = {}
        self._inner: dict[tuple[str, str], str] | None = None
        self._by_name: dict[str, set[str]] | None = None
        self._protocol_methods: set[str] = set()  # 协议类上的空方法 key，随 _names() 一起建
        self._module_functions: set[str] = set()  # 模块级函数的 key，随 _names() 一起建
        self._aliases: dict[tuple[str, str], Sym] = {}  # (模块相对路径, 别名) -> 指向的函数
        self._subclasses: dict[int, list[ClassInfo]] | None = None  # id(类) -> 直接的项目内子类

    # ---------- 对外入口 ----------

    def resolve(self, call: ast.Call, ctx: FunctionInfo) -> Resolved:
        func = call.func
        display = _display(func)
        mod = self.index.modules[ctx.file]
        if isinstance(func, ast.Name):
            return self._resolve_name(func.id, ctx, mod, display)
        if not isinstance(func, ast.Attribute):
            return Resolved(None, "unresolved", display)
        base = self._sym(func.value, mod, self._locals(ctx))
        kind = base[0] if base else None
        if kind in ("inst", "class", "cls"):
            key = self._find_method(base[1], func.attr, set())
            return Resolved(key, "exact", display) if key else Resolved(None, "unresolved", display)
        if kind == "module":
            return self._from_symbol(self._lookup(f"{base[1]}.{func.attr}"), display)
        if kind == "external" or isinstance(func.value, _LITERALS):
            return Resolved(None, "external", display)
        if isinstance(func.value, ast.Call) and isinstance(func.value.func, ast.Name) and func.value.func.id == "super":
            return Resolved(None, "unresolved", display)  # 执行的是某个基类的方法：按名字推测多半推到调用方自己
        return self._guess(func.attr, display, ctx)

    def symbol(self, e: ast.expr, mod: ModuleInfo, ctx: FunctionInfo | None) -> Sym:
        """表达式（名字或属性链）本身指向的符号，不是调用它：内部函数、局部名、模块级符号、导入；类或有类型的实例上的
        方法为 ("func", 方法 key)。ctx 为 None 时按 mod 的模块级代码看。"""
        local = self._locals(ctx) if ctx is not None else {}
        if isinstance(e, ast.Name):
            key = self._inner_function(e.id, ctx)
            return ("func", key) if key else self._sym(e, mod, local)
        if isinstance(e, ast.Attribute):
            base = self._sym(e.value, mod, local)
            if base and base[0] in ("inst", "class", "cls"):
                key = self._find_method(base[1], e.attr, set())
                return ("func", key) if key else None
        return self._sym(e, mod, local)

    def value_references(self) -> set[str]:
        """项目里被当作值交出去的函数（调用的参数、字典的值、赋值的右边，含其中的列表 / 元组 / 集合字面量的元素）：
        别处会调用它们（回调、注册表、依赖注入）。函数只在自己里面引用自己不算。"""
        by_node = {id(f.node): f for f in self.index.functions.values()}
        found: set[str] = set()
        for mod in self.index.modules.values():
            stack: list[tuple[ast.AST, FunctionInfo | None]] = [(mod.tree, None)]
            while stack:
                n, ctx = stack.pop()
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
                    ctx = by_node.get(id(n), ctx)
                if isinstance(n, ast.Call):
                    values = [*n.args, *(k.value for k in n.keywords)]
                elif isinstance(n, ast.Dict):
                    values = [v for v in n.values if v is not None]
                elif isinstance(n, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                    values = [n.value]
                else:
                    values = []
                for v in values:
                    for e in _value_exprs(v):
                        sym = self.symbol(e, mod, ctx)
                        if sym and sym[0] == "func" and (ctx is None or sym[1] != ctx.key):
                            found.add(sym[1])
                stack.extend((c, ctx) for c in ast.iter_child_nodes(n))
        return found

    def mentions(self, names: set[str]) -> set[str]:
        """项目里有地方提到过的函数（只查名字在 names 里的）：调用、当作值交出去、当装饰器用、写在模块顶层的调用、
        `(a if x else b)()` 这种算出来再调的都算——只要名字出现在一个表达式里并且解析到它。函数只在自己里面提到自己
        不算；import 语句不算（只是把名字带进来）。"""
        by_node = {id(f.node): f for f in self.index.functions.values()}
        found: set[str] = set()
        for mod in self.index.modules.values():
            stack: list[tuple[ast.AST, FunctionInfo | None]] = [(mod.tree, None)]
            while stack:
                n, ctx = stack.pop()
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
                    ctx = by_node.get(id(n), ctx)
                if (isinstance(n, ast.Name) and n.id in names or isinstance(n, ast.Attribute) and n.attr in names) \
                        and isinstance(n.ctx, ast.Load):
                    sym = self.symbol(n, mod, ctx)
                    if sym and sym[0] == "func" and (ctx is None or sym[1] != ctx.key):
                        found.add(sym[1])
                stack.extend((c, ctx) for c in ast.iter_child_nodes(n))
        return found

    # ---------- 各层 ----------

    def _inner_function(self, name: str, ctx: FunctionInfo | None) -> str | None:
        """ctx 自己和各级外层函数里叫 name 的内部函数。"""
        inner = self._inner_map()
        cur = ctx
        while cur is not None:
            key = inner.get((cur.key, name))
            if key:
                return key
            cur = self.index.functions.get(cur.parent) if cur.parent else None
        return None

    def _resolve_name(self, name: str, ctx: FunctionInfo, mod: ModuleInfo, display: str) -> Resolved:
        key = self._inner_function(name, ctx)  # 1. 外层函数里的内部函数（自己和各级外层）
        if key:
            return Resolved(key, "exact", display)
        return self._from_symbol(self._sym(ast.Name(id=name), mod, self._locals(ctx)), display)

    def _from_symbol(self, sym: Sym, display: str) -> Resolved:
        """调用一个符号：函数 → 它自己；类 → 构造函数 __init__（类上或最近的项目基类上定义的；都没有时算项目外）；
        有类型的实例 → 它的 __call__（同样查到项目基类；没有时无法判断）；classmethod 的 cls → 见 _cls_call。"""
        kind = sym[0] if sym else None
        if kind == "func":
            return Resolved(sym[1], "exact", display)
        if kind == "class":
            init = self._find_method(sym[1], "__init__", set())
            return Resolved(init, "exact", display, constructor=True) if init else Resolved(None, "external", display)
        if kind == "cls":
            return self._cls_call(sym[1], display)
        if kind == "inst":
            call = self._find_method(sym[1], "__call__", set())
            return Resolved(call, "exact", display) if call else Resolved(None, "unresolved", display)
        if kind == "external":
            return Resolved(None, "external", display)
        return Resolved(None, "unresolved", display)

    def _cls_call(self, cls: ClassInfo, display: str) -> Resolved:
        """classmethod 里调用 cls(...)：运行时 cls 可能是这个类或它的任何项目内子类（直接、间接，含定义在函数里的）。
        每个都按方法查找（同 _find_method，沿项目内基类）得到 __init__：都是同一个函数时为 exact；都得不到项目内的
        __init__ 时同调用项目内的类（项目外）；得到的不一样、或有哪个类体里的 __init__ 不是顶层 def（__init__ = 函数、
        if 下的 def）时目标不确定，记为无法判断——不推测。"""
        family = [cls, *self._descendants(cls)]
        if any("__init__" in c.other_bindings for c in family):
            return Resolved(None, "unresolved", display)
        inits = {self._find_method(c, "__init__", set()) for c in family}
        if inits == {None}:
            return Resolved(None, "external", display)
        if len(inits) == 1:
            return Resolved(inits.pop(), "exact", display, constructor=True)
        return Resolved(None, "unresolved", display)

    def _guess(self, name: str, display: str, ctx: FunctionInfo) -> Resolved:
        """接收者类型未知的 x.name(...)：按方法名推测。方法名太常见、或是常见内置 / 标准库类型的方法名时不推测；
        只看调用方所在源码根（包含它的最内层源码根）下的定义：恰好一处、而且是类上的方法才推测到它——模块级函数、
        协议上的空方法、调用所在的函数及其外层函数自己不是推测目标，但都算作同名定义。"""
        if name in COMMON_NAMES or name in BUILTIN_METHOD_NAMES:
            return Resolved(None, "unresolved", display)
        root = self.index.modules[ctx.file].source_root
        here = [k for k in self._names().get(name, ()) if self._source_root_of(k) == root]
        if len(here) == 1 and here[0] not in self._module_functions and here[0] not in self._protocol_methods \
                and here[0] not in self._enclosing(ctx):
            return Resolved(here[0], "guessed", display)
        return Resolved(None, "unresolved", display)

    def _enclosing(self, ctx: FunctionInfo) -> set[str]:
        """ctx 自己和各级外层函数的 key。"""
        keys: set[str] = set()
        cur: FunctionInfo | None = ctx
        while cur is not None:
            keys.add(cur.key)
            cur = self.index.functions.get(cur.parent) if cur.parent else None
        return keys

    # ---------- 索引辅助 ----------

    def _inner_map(self) -> dict[tuple[str, str], str]:
        if self._inner is None:
            self._inner = {}
            for fi in self.index.functions.values():
                if fi.parent:
                    parent = self.index.functions[fi.parent]
                    name = fi.node.name
                    if fi.qualname == f"{parent.qualname}.<locals>.{name}":
                        self._inner[(fi.parent, name)] = fi.key
        return self._inner

    def _names(self) -> dict[str, set[str]]:
        """方法名（含类体里的别名；嵌套在类里、定义在函数里的类的方法也算）/ 模块级函数名 -> 项目里的定义（含协议类上
        的空方法：协议声明了这个方法，说明可能有多个实现）。"""
        if self._by_name is None:
            self._by_name = {}
            for m in self.index.modules.values():
                for n, k in m.functions.items():
                    self._by_name.setdefault(n, set()).add(k)
                    self._module_functions.add(k)
            for c in self.index.classes:
                for n, k in [*c.methods.items(), *c.aliases.items()]:
                    self._by_name.setdefault(n, set()).add(k)
                if self._is_protocol(c):
                    self._protocol_methods.update(k for k in c.methods.values() if self._stub(k))
        return self._by_name

    def _source_root_of(self, key: str) -> str:
        return self.index.modules[self.index.functions[key].file].source_root

    def _descendants(self, cls: ClassInfo) -> list[ClassInfo]:
        """cls 的项目内子类（直接和间接，含嵌套定义的类）。"""
        if self._subclasses is None:
            self._subclasses = {}
            for c in self.index.classes:
                for base in c.bases:
                    b = self._base_class(base, self.index.modules[c.file])
                    if b is not None:
                        self._subclasses.setdefault(id(b), []).append(c)
        seen = {id(cls)}
        out: list[ClassInfo] = []
        stack = list(self._subclasses.get(id(cls), []))
        while stack:
            c = stack.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            out.append(c)
            stack.extend(self._subclasses.get(id(c), []))
        return out

    def _base_class(self, base: str, mod: ModuleInfo) -> ClassInfo | None:
        """基类表达式（源码文本）在模块 mod 里指向的项目内类。带下标的（BaseRepo[int]）看下标前的部分：运行时
        继承的就是它。"""
        try:
            expr = ast.parse(base, mode="eval").body
        except SyntaxError:
            return None
        if isinstance(expr, ast.Subscript):
            expr = expr.value
        sym = self._sym(expr, mod, {})
        return sym[1] if sym and sym[0] == "class" else None

    def _is_protocol(self, cls: ClassInfo) -> bool:
        """直接基类里有 typing.Protocol：它的空方法只是接口声明，运行时执行的是某个实现类的方法。"""
        mod = self.index.modules[cls.file]
        for base in cls.bases:
            head, _, rest = base.split("[", 1)[0].partition(".")
            if ".".join(filter(None, [mod.imports.get(head, head), rest])) in _PROTOCOLS:
                return True
        return False

    def _stub(self, key: str) -> bool:
        return _is_stub(self.index.functions[key].node)

    def _find_method(self, cls: ClassInfo, name: str, seen: set[int]) -> str | None:
        """类上（或沿项目内基类找到）的方法；落在协议类的空方法上时算找不到（协议上写了方法体的是默认实现，照常解析）。"""
        if id(cls) in seen:
            return None
        seen.add(id(cls))
        key = cls.methods.get(name) or cls.aliases.get(name)  # 类体里的 m2 = m1：m2 就是 m1
        if key:
            return None if self._is_protocol(cls) and self._stub(key) else key
        mod = self.index.modules[cls.file]
        for base in cls.bases:
            b = self._base_class(base, mod)
            if b is not None:
                key = self._find_method(b, name, seen)
                if key:
                    return key
        return None

    # ---------- 符号求值 ----------

    def _lookup(self, dotted: str, depth: int = 0) -> Sym:
        """点分全名 -> 符号；项目外为 external，项目内查不到为 None。"""
        if dotted.split(".")[0] not in self._tops:
            return ("external",)
        if dotted in self.index.by_dotted:
            return ("module", dotted)
        mod_part, _, last = dotted.rpartition(".")
        rel = self.index.by_dotted.get(mod_part)
        if rel is None:
            return None
        m = self.index.modules[rel]
        if last in m.functions:
            return ("func", m.functions[last])
        if last in m.classes:
            return ("class", m.classes[last])
        if last in m.instances:
            sym = self._lookup(m.instances[last], depth + 1) if depth < 5 else None
            return ("inst", sym[1]) if sym and sym[0] == "class" else None
        if last in m.aliases:
            sym = self._alias(m, last)
            if sym:
                return sym
        if last in m.imports and depth < 5:  # 包的 __init__ 里转手导出
            return self._lookup(m.imports[last], depth + 1)
        return None

    def _alias(self, mod: ModuleInfo, name: str) -> Sym:
        """模块级别名 name = 函数 / 模块.函数 / 类.方法 指向的项目内函数 ("func", key)；右边不是项目内函数时 None
        （不建立别名，查找照旧往下走）。别名互相指向成环时为 None。"""
        k = (mod.rel, name)
        if k not in self._aliases:
            self._aliases[k] = None  # 先占位：成环时查到这里为止
            sym = self.symbol(mod.aliases[name], mod, None)
            self._aliases[k] = sym if sym and sym[0] == "func" else None
        return self._aliases[k]

    def _sym(self, e: ast.expr, mod: ModuleInfo, local: dict[str, Sym]) -> Sym:
        if isinstance(e, ast.Name):
            return self._name_sym(e.id, mod, local)
        if isinstance(e, ast.Attribute):
            base = self._sym(e.value, mod, local)
            if base and base[0] == "module":
                return self._lookup(f"{base[1]}.{e.attr}")
            if base and base[0] == "external":
                return base
            return None
        if isinstance(e, ast.Call):
            f = self._sym(e.func, mod, local)
            return ("inst", f[1]) if f and f[0] == "class" else None
        return None

    def _name_sym(self, name: str, mod: ModuleInfo, local: dict[str, Sym]) -> Sym:
        if name in local:
            return local[name]
        if name in mod.functions:
            return ("func", mod.functions[name])
        if name in mod.classes:
            return ("class", mod.classes[name])
        if name in mod.instances:
            sym = self._lookup(mod.instances[name])
            return ("inst", sym[1]) if sym and sym[0] == "class" else None
        if name in mod.aliases:
            sym = self._alias(mod, name)
            if sym:
                return sym
        if name in mod.imports:
            return self._lookup(mod.imports[name])
        if name in _BUILTIN_NAMES:
            return ("external",)
        return None

    # ---------- 局部变量类型 ----------

    def _locals(self, ctx: FunctionInfo) -> dict[str, Sym]:
        """函数自己和各级外层函数里的局部名；有类型的给 ("inst", 类)，函数体里 import 绑定的给它导入的符号，
        其余为 None（挡住同名的模块级符号）。"""
        cached = self._locals_cache.get(ctx.key)
        if cached is not None:
            return cached
        chain: list[FunctionInfo] = []
        cur: FunctionInfo | None = ctx
        while cur is not None:
            chain.append(cur)
            cur = self.index.functions.get(cur.parent) if cur.parent else None
        out: dict[str, Sym] = {}
        for fn in reversed(chain):  # 外层先，内层覆盖
            out.update(self._scan(fn))
        self._locals_cache[ctx.key] = out
        return out

    def _scan(self, fn: FunctionInfo) -> dict[str, Sym]:
        mod = self.index.modules[fn.file]
        node = fn.node
        out: dict[str, Sym] = {}
        bound: set[str] = set()

        def bind(name: str, sym: Sym) -> None:
            """名字的所有绑定必须一致才有类型；任何分歧或无类型的重绑定都让它永久无类型。"""
            if name not in bound:
                bound.add(name)
                out[name] = sym
            elif sym is None or out[name] is None or out[name] != sym:
                out[name] = None

        a = node.args
        positional = [*a.posonlyargs, *a.args]
        for arg in [*positional, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None:
                bind(arg.arg, self._inst_of_annotation(arg.annotation, mod) if arg.annotation else None)
        decorators = {d.id for d in fn.decorators if isinstance(d, ast.Name)}
        if fn.cls and positional and "staticmethod" not in decorators and fn.cls in mod.classes:
            cls = mod.classes[fn.cls]
            if "classmethod" in decorators and cls.methods.get(fn.node.name) == fn.key:
                out[positional[0].arg] = ("cls", cls)  # classmethod 的首参是类本身（运行时可能是子类）
            else:
                out[positional[0].arg] = ("inst", cls)  # 首参视为本类实例
        handled: set[int] = set()

        def visit(n: ast.AST) -> None:
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
                return
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                handled.add(id(n.target))
                sym = self._inst_of_annotation(n.annotation, mod)  # 有注解就只信注解
                bind(n.target.id, sym)
            elif isinstance(n, ast.Assign):
                sym = self._inst_of_value(n.value, mod, out)
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        handled.add(id(t))
                        bind(t.id, sym)
            elif isinstance(n, ast.Import | ast.ImportFrom):  # 函数体里的 import 绑定局部名，盖过同名的模块级符号
                for name, target in import_bindings(n, mod):
                    bind(name, self._lookup(target))
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and id(n) not in handled:
                bind(n.id, None)
            for c in ast.iter_child_nodes(n):
                visit(c)

        for st in node.body:
            visit(st)
        return out

    def _inst_of_value(self, value: ast.expr, mod: ModuleInfo, local: dict[str, Sym]) -> Sym:
        """x = ClassName(...) 才有类型。"""
        if isinstance(value, ast.Call):
            f = self._sym(value.func, mod, local)
            if f and f[0] == "class":
                return ("inst", f[1])
        return None

    def _inst_of_annotation(self, ann: ast.expr, mod: ModuleInfo) -> Sym:
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(ann, ast.Subscript):  # Optional[Svc]；Annotated[Svc, Depends(...)] 同 svc: Svc = Depends(...)，看第一项
            wrapper = _short(ann.value) if isinstance(ann.value, ast.Name | ast.Attribute) else None
            if wrapper == "Optional":
                return self._inst_of_annotation(ann.slice, mod)
            if wrapper == "Annotated" and isinstance(ann.slice, ast.Tuple) and ann.slice.elts:
                return self._inst_of_annotation(ann.slice.elts[0], mod)
            return None
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):  # Svc | None
            sides = [s for s in (ann.left, ann.right) if not (isinstance(s, ast.Constant) and s.value is None)]
            return self._inst_of_annotation(sides[0], mod) if len(sides) == 1 else None
        if isinstance(ann, ast.Name | ast.Attribute):
            sym = self._sym(ann, mod, {})
            if sym and sym[0] == "class":
                return ("inst", sym[1])
        return None
