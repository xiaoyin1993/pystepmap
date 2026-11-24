"""项目外对象上的调用：没有 await 的调用里，哪些是"对外干活"（读写数据库、发请求、发邮件、调第三方服务），要画成步骤。

解析器只认得项目内的函数；被 await 的目标不明调用一向算步骤（await 多半是在等外部），同步写法的代码没有这个信号——
`db.execute(...)`、`requests.post(...)`、`session.add(...)` 全都不出节点，只包着它们的 if 也跟着消失，图上少掉的正是
业务规则。这里按"调用落在谁身上"补上这一类，不看方法名猜它做什么：

- 模块级的外部对象：从项目外、又不是纯计算的库导入的名字（`requests`、`smtplib`、`from flask_mail import mail`），
  用这类库的调用建出来的模块级变量（`db = sqlite3.connect(...)`、`client = OpenAI()`），以及从项目里别的模块导入的
  这种变量（`from app.db import db`）；
- 函数里的外部对象：注解是这类库里的类的参数（`db: Session`）、默认值是 `Depends(...)` 又没有注解的参数，以及用上面
  任何一种对象的调用结果赋值的局部变量（`cur = db.cursor()`、`with smtplib.SMTP(...) as s`）；
- 方法里 `self.某属性.方法(...)`：类型多半不知道的协作对象（`self.session.commit()`、`self.client.post(...)`）。

不算的：构造对象（被调用的名字以大写字母开头，同项目内的构造调用）；纯计算的库（PURE_TOPS：json、re、datetime……）；
读数据、摆弄容器和字符串的方法（QUIET_METHODS：get、append、fetchone、json……，只对"对象上的对象"和局部变量生效，
`requests.get` 这种直接落在库上的不受影响）；只是取请求、拼响应的框架函数（QUIET_IMPORTS）；接在另一个调用结果后面的
调用（`db.execute(sql).fetchone()` 由 `db.execute` 代表）。配置 `external_calls: false` 关掉整个识别。
"""

from __future__ import annotations

import ast
import builtins

from .index import FunctionInfo, ModuleInfo, ProjectIndex

# 纯计算、取时间、类型和数据结构的库：上面的调用不是"对外干活"
PURE_TOPS = frozenset({
    "abc", "array", "base64", "binascii", "bisect", "builtins", "calendar", "cmath", "collections", "contextlib",
    "copy", "dataclasses", "datetime", "decimal", "difflib", "enum", "fnmatch", "fractions", "functools", "glob",
    "hashlib", "heapq", "hmac", "html", "inspect", "io", "itertools", "json", "keyword", "locale", "logging", "math",
    "numbers", "operator", "os", "pathlib", "posixpath", "pprint", "random", "re", "secrets", "shlex", "statistics",
    "string", "struct", "sys", "textwrap", "time", "traceback", "types", "typing", "unicodedata", "uuid", "warnings",
    "weakref", "zoneinfo",
    # 解析、压缩、并发原语、命令行参数：不是业务上的"对外"
    "argparse", "ast", "asyncio", "codecs", "concurrent", "contextvars", "csv", "dis", "email", "gzip", "importlib", "ipaddress",
    "platform", "signal", "threading", "tokenize", "xml", "zlib",
    "annotated_types", "attr", "attrs", "bs4", "croniter", "dateutil", "jinja2", "jwt", "loguru", "lxml", "markdown",
    "numpy", "opentelemetry", "orjson", "prometheus_client", "pydantic", "pydantic_core", "pydantic_settings",
    "pystepkit", "pytz", "simplejson", "structlog", "tenacity", "typing_extensions", "ujson", "yaml",
})
PURE_PREFIXES = ("urllib.parse",)

# 拼查询语句的库：库里的函数（select、text、and_）只是在拼语句，不算步骤；但这些库的类当注解的参数（db: Session）、
# 用它们建出来的模块级对象（engine = create_engine(...)）是真的在读写数据库
BUILDER_TOPS = frozenset({"sqlalchemy", "sqlmodel"})

# 日志、打点、命令行参数声明的方法名：落在什么对象上都不算步骤
NEVER_STEPS = frozenset({
    "add_argument", "add_parser", "add_subparsers", "bind", "critical", "debug", "dec", "error", "exception", "inc",
    "info", "labels", "log", "observe", "parse_args", "set_attribute", "set_defaults", "warn", "warning",
})

# 读数据、摆弄容器 / 字符串 / 查询结果的方法名：落在局部变量或"对象上的对象"上时不算步骤
QUIET_METHODS = frozenset({
    "add_argument", "all", "append", "appendleft", "as_posix", "capitalize", "casefold", "clear", "close", "copy",
    "count", "decode", "dict", "difference", "discard", "encode", "endswith", "extend", "fetchall", "fetchmany",
    "fetchone", "find", "first", "format", "get", "get_json", "getlist", "group", "groupdict", "groups", "index",
    "insert", "intersection", "isdigit", "isoformat", "items", "join", "json", "keys", "lower", "lstrip", "mappings",
    "model_dump", "model_dump_json", "model_validate", "one", "one_or_none", "pop", "popleft", "raise_for_status",
    "remove", "replace", "reverse", "rsplit", "rstrip", "scalar", "scalar_one", "scalar_one_or_none", "scalars",
    "setdefault", "sort", "split", "splitlines", "startswith", "strftime", "strip", "timestamp", "title", "to_dict",
    "tolist", "total_seconds", "union", "unique", "update", "upper", "values",
})

# 取请求、拼响应、声明参数的框架名字：不是业务步骤
QUIET_IMPORTS = frozenset({
    "flask.request", "flask.g", "flask.session", "flask.current_app", "flask.jsonify", "flask.render_template",
    "flask.render_template_string", "flask.redirect", "flask.url_for", "flask.abort", "flask.make_response",
    "flask.flash", "flask.send_file", "flask.send_from_directory",
    "fastapi.Depends", "fastapi.Query", "fastapi.Path", "fastapi.Body", "fastapi.Header", "fastapi.Cookie",
    "fastapi.Form", "fastapi.File", "fastapi.Security",
    "django.shortcuts.render", "django.shortcuts.redirect", "django.urls.reverse",
})


def _root(e: ast.expr) -> tuple[str | None, int, bool]:
    """表达式最左边的名字、名字后面跟了几段属性、中间有没有穿过调用或下标（`a.b().c`、`a[0].c`）。"""
    depth, through = 0, False
    while True:
        if isinstance(e, ast.Name):
            return e.id, depth, through
        if isinstance(e, ast.Attribute):
            depth, e = depth + 1, e.value
        elif isinstance(e, ast.Call):
            through, e = True, e.func
        elif isinstance(e, ast.Subscript):
            through, e = True, e.value
        elif isinstance(e, ast.Await):
            e = e.value
        else:
            return None, depth, through


def _attr_names(e: ast.expr) -> list[str]:
    """属性链上的各段名字（最左边的名字在内）。"""
    out: list[str] = []
    while isinstance(e, ast.Attribute | ast.Call | ast.Subscript):
        if isinstance(e, ast.Attribute):
            out.append(e.attr)
            e = e.value
        else:
            e = e.func if isinstance(e, ast.Call) else e.value
    if isinstance(e, ast.Name):
        out.append(e.id)
    return out


def _called_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _is_constructor(call: ast.Call) -> bool:
    return _called_name(call)[:1].isupper()


class ExternalObjects:
    """整个项目共用：哪些点分名、哪些模块级名字是外部对象。"""

    def __init__(self, index: ProjectIndex, noise_receivers: list[str]):
        self.index = index
        self.noise = set(noise_receivers)
        self._tops = {d.split(".")[0] for d in [*index.by_dotted, *index.ambiguous]}
        self._module_roots: dict[str, set[str]] = {}
        self._plain: dict[tuple[str, str], set[str]] = {}

    def external(self, dotted: str | None, builders: bool = True) -> bool:
        """点分全名在项目外、又不是纯计算的库里。builders=False 时拼查询语句的库（BUILDER_TOPS）也不算。"""
        if not dotted or dotted.startswith(".") or dotted in QUIET_IMPORTS or dotted.startswith(PURE_PREFIXES):
            return False
        top = dotted.split(".")[0]
        return top not in self._tops and top not in PURE_TOPS and (builders or top not in BUILDER_TOPS)

    def plain_attrs(self, mod: ModuleInfo, cls_name: str) -> set[str]:
        """类的方法里用字面量、推导式、内置函数或纯计算库的调用赋过值的 self 属性（self.seen = set()、self.rows = []）：
        它们是普通的数据，上面的方法调用不是对外干活。"""
        key = (mod.rel, cls_name)
        found = self._plain.get(key)
        if found is None:
            found = self._plain[key] = set()
            cls = mod.classes.get(cls_name)
            for fkey in (cls.methods.values() if cls else ()):
                fn = self.index.functions.get(fkey)
                for n in (ast.walk(fn.node) if fn else ()):
                    if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None and self._plain_value(n.value, mod):
                        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                        found |= {t.attr for t in targets if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)}
        return found

    def _plain_value(self, value: ast.expr, mod: ModuleInfo) -> bool:
        if isinstance(value, ast.Constant | ast.JoinedStr | ast.List | ast.Dict | ast.Set | ast.Tuple | ast.ListComp
                      | ast.DictComp | ast.SetComp | ast.BinOp | ast.Compare | ast.BoolOp):
            return True
        if isinstance(value, ast.Call):
            name, _, _ = _root(value.func)
            if name is None:
                return False
            if name in mod.imports:
                return not self.external(mod.imports[name])
            return hasattr(builtins, name)
        return False

    def _own_roots(self, mod: ModuleInfo) -> set[str]:
        """本模块自己绑定的外部对象：外部导入的名字、用外部库的调用建出来的模块级变量。"""
        roots = {name for name, dotted in mod.imports.items() if self.external(dotted, builders=False)}
        roots |= {name for name, dotted in mod.instances.items() if self.external(dotted)}
        return roots

    def module_roots(self, mod: ModuleInfo) -> set[str]:
        found = self._module_roots.get(mod.rel)
        if found is None:
            found = self._own_roots(mod)
            for name, dotted in mod.imports.items():  # from app.db import db：别的项目模块里的外部对象
                owner, _, attr = dotted.rpartition(".")
                rel = self.index.by_dotted.get(owner)
                other = self.index.modules.get(rel) if rel else None
                if other is not None and attr in other.instances and self.external(other.instances[attr]):
                    found.add(name)
            self._module_roots[mod.rel] = found
        return found


class ExternalCalls:
    """一个函数里：哪些没有 await 的调用落在外部对象上。"""

    def __init__(self, fn: FunctionInfo, objects: ExternalObjects):
        self.objects = objects
        self.mod = objects.index.modules[fn.file]
        node = fn.node
        a = node.args
        params = [*a.posonlyargs, *a.args, *a.kwonlyargs]
        decorators = {d.id for d in fn.decorators if isinstance(d, ast.Name)}
        self.me = params[0].arg if fn.cls and params and "staticmethod" not in decorators else None
        self.plain = objects.plain_attrs(self.mod, fn.cls) if self.me else set()
        bound = {p.arg for p in [*params, a.vararg, a.kwarg] if p is not None}
        stores = [n for st in node.body for n in self._walk(st) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
        bound |= {n.id for n in stores}
        self.globals = objects.module_roots(self.mod) - bound  # 被局部名盖住的不算
        self.locals: set[str] = set()
        defaults = dict(zip([p.arg for p in [*a.posonlyargs, *a.args][::-1]], a.defaults[::-1]))
        defaults |= {p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
        for p in params:
            if p.annotation is not None:
                if self._external_annotation(p.annotation):
                    self.locals.add(p.arg)
            elif isinstance(defaults.get(p.arg), ast.Call) and _called_name(defaults[p.arg]) == "Depends":
                self.locals.add(p.arg)
        for _ in range(2):  # 第二遍接住"先用后赋值"的写法（循环里）
            for st in node.body:
                for n in self._walk(st):
                    self._bind(n)

    @staticmethod
    def _walk(root: ast.AST):
        stack = [root]
        while stack:
            n = stack.pop()
            yield n
            for c in ast.iter_child_nodes(n):
                if not isinstance(c, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
                    stack.append(c)

    def _external_annotation(self, ann: ast.expr) -> bool:
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return False
        if isinstance(ann, ast.Subscript):  # Optional[Session]、Annotated[Session, Depends(...)]
            inner = ann.slice.elts[0] if isinstance(ann.slice, ast.Tuple) and ann.slice.elts else ann.slice
            return self._external_annotation(inner)
        if isinstance(ann, ast.BinOp):  # Session | None
            return self._external_annotation(ann.left) or self._external_annotation(ann.right)
        if isinstance(ann, ast.Name | ast.Attribute):
            name, _, through = _root(ann)
            return not through and name in self.mod.imports and self.objects.external(self.mod.imports[name])
        return False

    def _from_external(self, value: ast.expr | None) -> bool:
        """value 是落在外部对象上的调用（或对它的 await）：它的结果也当外部对象。"""
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            return False
        name, _, _ = _root(value.func)
        return name is not None and (name in self.globals or name in self.locals)

    def _bind(self, n: ast.AST) -> None:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, ast.AnnAssign | ast.NamedExpr):
            targets, value = [n.target], n.value
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            targets, value = [n.optional_vars], n.context_expr
        if self._from_external(value):
            self.locals |= {t.id for t in targets if isinstance(t, ast.Name)}

    def is_step(self, call: ast.Call) -> bool:
        """call（没有 await、没有解析到项目内函数）是不是落在外部对象上的干活的调用。"""
        if _is_constructor(call) or _called_name(call) in NEVER_STEPS:
            return False
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.globals
        if not isinstance(func, ast.Attribute):
            return False
        name, depth, through = _root(func.value)
        if name is None or through or set(_attr_names(func.value)) & self.objects.noise:
            return False
        quiet = func.attr in QUIET_METHODS or func.attr.startswith("__")
        if name in self.globals:
            return depth == 0 or not quiet
        if name in self.locals:
            return not quiet
        if name != self.me or depth < 1 or quiet:
            return False
        return _attr_names(func.value)[-2] not in self.plain  # self.<属性>.….方法()：属性是普通数据时不算
