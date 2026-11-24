import ast
import sys

import pytest
from conftest import find_fn, make_project, run_traced
from pystepmap.analyze import analyze_project
from pystepmap.config import ProjectConfig
from pystepmap.index import build_index
from pystepmap.model import from_json, parse_key
from pystepmap.resolve import Resolver
from pystepmap.verify import verify

PROJECT = {"app/svc.py": """
    class Base:
        async def ping(self): ...
    class Svc(Base):
        async def match(self, x): ...
        async def both(self):
            await self.match(1); await self.ping()
    svc = Svc()
    async def helper(x): ...
""", "app/t.py": """
    import json
    from app.svc import Svc, helper, svc
    from app import svc as svc_mod
    async def only_here(): ...
    async def run(s: Svc, dep: Svc = Depends(get), other=None):
        def inner(): ...
        inner(); await helper(1); await s.match(1); await dep.match(1)
        local = Svc(); await local.match(1); await Svc().match(1)
        await svc.match(1); await svc_mod.helper(1)
        json.dumps(1); await other.only_here(); await other.match(1); await other.mystery()
"""}


def _calls(fn_node):
    """函数体里的调用（不进入嵌套函数定义），按源码顺序。"""
    out = []

    def visit(n):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return
        if isinstance(n, ast.Call):
            out.append(n)
        for c in ast.iter_child_nodes(n):
            visit(c)

    for st in fn_node.body:
        visit(st)
    return sorted(out, key=lambda c: (c.lineno, c.col_offset))


def _resolve_all(tmp_path, files, qualname):
    index = build_index(make_project(tmp_path, files), ProjectConfig())
    resolver = Resolver(index)
    fn = find_fn(index, qualname)
    res = []
    for c in _calls(fn.node):
        r = resolver.resolve(c, fn)
        res.append((r.display, r.how, index.functions[r.target].qualname if r.target else None))
    return res


def test_layers(tmp_path):
    got = _resolve_all(tmp_path, PROJECT, "run")
    assert got == [
        ("inner", "exact", "run.<locals>.inner"),
        ("helper", "exact", "helper"),
        ("s.match", "exact", "Svc.match"),
        ("dep.match", "exact", "Svc.match"),
        ("Svc", "external", None),
        ("local.match", "exact", "Svc.match"),
        ("Svc.match", "exact", "Svc.match"),
        ("Svc", "external", None),
        ("svc.match", "exact", "Svc.match"),
        ("svc_mod.helper", "exact", "helper"),
        ("json.dumps", "external", None),
        ("other.only_here", "unresolved", None),  # 唯一同名定义是模块级函数：不推测
        ("other.match", "unresolved", None),  # match 是 re.Pattern 的方法名：不推测
        ("other.mystery", "unresolved", None),
    ]


def test_self_and_inherited(tmp_path):
    got = _resolve_all(tmp_path, PROJECT, "Svc.both")
    assert got == [
        ("self.match", "exact", "Svc.match"),
        ("self.ping", "exact", "Base.ping"),
    ]


def test_conservative_cases(tmp_path):
    files = {"app/svc.py": """
        class Svc:
            async def match(self): ...
            async def run(self): ...
            def uniq(self): ...
        def get(): ...
    """, "app/t.py": """
        from typing import Optional
        from app.svc import Svc
        async def f(a: Optional[Svc], b: "Svc | None", svc=None):
            a.match(); b.match()
            svc.match()
            (await get()).uniq()
            x[0].run()
            "".join([])
    """}
    got = _resolve_all(tmp_path, files, "f")
    assert got == [
        ("a.match", "exact", "Svc.match"),
        ("b.match", "exact", "Svc.match"),
        ("svc.match", "unresolved", None),  # match 是 re.Pattern 的方法名：不推测
        ("get.uniq", "guessed", "Svc.uniq"),  # 接收者无法定型，方法名项目内唯一
        ("get", "unresolved", None),  # get 没导入，也不在本模块
        ("x.run", "unresolved", None),  # run 在常见名单里，不猜
        ("….join", "external", None)  # 字面量接收者,
    ]


def test_local_rebinding_is_never_exact(tmp_path):
    files = {"app/svc.py": """
        class A:
            def m(self): ...
        class B:
            def m(self): ...
            def only_b(self): ...
    """, "app/t.py": """
        from app.svc import A, B
        def seq():
            x = A(); x.m(); x = B()
        def branch(c):
            if c:
                y = A()
            else:
                y = B()
            y.m()
            y.only_b()
        def same():
            z = A(); z = A(); z.m()
        def untyped_later(p: A):
            p.m(); p = None
        def ann():
            w: int = A()
            w.only_b()
    """}
    assert _resolve_all(tmp_path, files, "seq")[1:2] == [("x.m", "unresolved", None)]
    assert _resolve_all(tmp_path / "b", files, "branch")[-2:] == [
        ("y.m", "unresolved", None), ("y.only_b", "guessed", "B.only_b")]
    assert _resolve_all(tmp_path / "c", files, "same")[-1] == ("z.m", "exact", "A.m")
    assert _resolve_all(tmp_path / "d", files, "untyped_later") == [("p.m", "unresolved", None)]
    assert _resolve_all(tmp_path / "e", files, "ann") == [("A", "external", None), ("w.only_b", "guessed", "B.only_b")]


def test_constructors_and_callable_instances(tmp_path):
    """调用项目内的类 → 它（或最近的项目基类）的 __init__；调用有类型的实例 → __call__；都没有时同以前。"""
    files = {"app/base.py": """
        class Base:
            def __init__(self, x): ...
            def __call__(self, y): ...
    """, "app/svc.py": """
        from pydantic import BaseModel
        from app.base import Base
        from app import base as base_mod
        class Own(Base):
            def __init__(self, x):
                super().__init__(x)
        class Child(Base): ...
        class Plain: ...
        class Model(BaseModel): ...
        handler = Child(1)
        def f(dep: Child, p, plain: Plain):
            Own(1); Child(2); Plain(); Model(); base_mod.Base(3)
            dep(1); handler(2); local = Own(3); local(4); p(5); plain(6)
    """}
    index = build_index(make_project(tmp_path, files), ProjectConfig())
    resolver = Resolver(index)
    fn = find_fn(index, "f")
    got = []
    for c in _calls(fn.node):
        r = resolver.resolve(c, fn)
        got.append((r.display, r.how, index.functions[r.target].qualname if r.target else None, r.constructor))
    assert got == [
        ("Own", "exact", "Own.__init__", True),
        ("Child", "exact", "Base.__init__", True),  # 自己没有 __init__：最近的项目基类的
        ("Plain", "external", None, False),  # 项目里没有 __init__：构造函数在项目外
        ("Model", "external", None, False),  # 基类在项目外
        ("base_mod.Base", "exact", "Base.__init__", True),
        ("dep", "exact", "Base.__call__", False),  # 参数注解定型
        ("handler", "exact", "Base.__call__", False),  # 模块级实例
        ("Own", "exact", "Own.__init__", True),
        ("local", "exact", "Base.__call__", False),  # 局部变量定型，__call__ 在基类上
        ("p", "unresolved", None, False),  # 没有类型
        ("plain", "unresolved", None, False),  # 有类型但类上没有 __call__
    ]


def _targets(tmp_path, files, qualname):
    """(显示名, 解析方式, 目标 key)，目标 key 带文件，区分不同模块里的同名函数。"""
    index = build_index(make_project(tmp_path, files), ProjectConfig())
    resolver = Resolver(index)
    fn = find_fn(index, qualname)
    return [(r.display, r.how, r.target) for r in (resolver.resolve(c, fn) for c in _calls(fn.node))]


def test_import_inside_function_shadows_module_names(tmp_path):
    """函数体里的 import 绑定的是局部名：盖过同名的模块级函数和模块级 import（真实项目里见过的写法）。"""
    files = {"app/svc.py": """
        async def candidates(): ...
        def helper(): ...
    """, "app/other.py": """
        def helper(): ...
    """, "app/api.py": """
        from app.other import helper
        async def candidates(): ...
        async def route():
            from app.svc import candidates
            await candidates()
        async def plain():
            await candidates()
        def rel():
            from .svc import helper
            helper()
            def inner():
                helper()
            inner()
        def module_level():
            helper()
        def twice(c):
            if c:
                import json
            else:
                import json
            json.dumps(c)
    """}
    assert _targets(tmp_path, files, "route") == [("candidates", "exact", "app/svc.py::candidates@2")]
    assert _targets(tmp_path / "b", files, "plain") == [("candidates", "exact", "app/api.py::candidates@3")]
    assert _targets(tmp_path / "c", files, "rel") == [("helper", "exact", "app/svc.py::helper@3"),
                                                      ("inner", "exact", "app/api.py::rel.<locals>.inner@12")]
    assert _targets(tmp_path / "d", files, "rel.<locals>.inner") == [("helper", "exact", "app/svc.py::helper@3")]
    assert _targets(tmp_path / "e", files, "module_level") == [("helper", "exact", "app/other.py::helper@2")]
    assert _targets(tmp_path / "f", files, "twice") == [("json.dumps", "external", None)]  # 同一个名字导入两次


def test_protocol_methods_are_never_targets(tmp_path):
    """typing.Protocol 上的方法只是接口声明（体是 ...），运行时执行的是某个实现类的方法：不解析到它，也不按方法名猜到它
    （真实项目里见过：远程 shell 的协议类）。显式继承协议的实现类照常解析。"""
    files = {"app/sh.py": """
        import typing
        from typing import Protocol
        class Shell(Protocol):
            def run(self, cmd): ...
            def probe(self): ...
            def upload(self, f): ...
        class Shell2(typing.Protocol):
            def only_on_protocol(self): ...
        class Impl:
            def run(self, cmd): ...
            def upload(self, f): ...
        class Sub(Shell):
            def run(self, cmd): ...
        def use(sh: Shell, s2: Shell2, imp: Impl, sub: Sub, other):
            sh.run("x"); sh.probe(); s2.only_on_protocol(); imp.run("x"); sub.run("x"); other.only_on_protocol()
            other.upload(1)
    """}
    got = _resolve_all(tmp_path, files, "use")
    assert got == [
        ("sh.run", "unresolved", None),
        ("sh.probe", "unresolved", None),
        ("s2.only_on_protocol", "unresolved", None),
        ("imp.run", "exact", "Impl.run"),
        ("sub.run", "exact", "Sub.run"),
        ("other.only_on_protocol", "unresolved", None),  # 方法名只在协议上有定义：不猜
        ("other.upload", "unresolved", None),  # 协议和一个实现类都有：算两处定义，不猜
    ]


def test_protocol_methods_with_real_bodies_resolve_normally(tmp_path):
    """协议上写了真实方法体的方法是默认实现，显式继承的类会用到它：照常解析。只有空方法（...、pass、只有文档注释、
    raise NotImplementedError）不解析、不猜。"""
    files = {"app/proto.py": """
        from typing import Protocol
        class Greeter(Protocol):
            def greet(self) -> str:
                return "hi"
            def name(self) -> str: ...
            def doc_only(self):
                \"\"\"只有文档注释。\"\"\"
            def todo(self):
                \"\"\"还没实现。\"\"\"
                raise NotImplementedError
            def blank(self):
                pass
        class English(Greeter):
            def name(self) -> str:
                return "en"
            def both(self):
                return self.greet() + self.name()
        def use(e: English, g: Greeter):
            e.greet(); Greeter.greet(e); g.greet(); g.name(); g.doc_only(); g.todo(); g.blank()
    """}
    assert _resolve_all(tmp_path, files, "use") == [
        ("e.greet", "exact", "Greeter.greet"),
        ("Greeter.greet", "exact", "Greeter.greet"),
        ("g.greet", "exact", "Greeter.greet"),
        ("g.name", "unresolved", None),
        ("g.doc_only", "unresolved", None),
        ("g.todo", "unresolved", None),
        ("g.blank", "unresolved", None),
    ]
    assert _resolve_all(tmp_path / "b", files, "English.both") == [
        ("self.greet", "exact", "Greeter.greet"), ("self.name", "exact", "English.name")]


def test_annotated_dependency_parameters_are_typed(tmp_path):
    """FastAPI 的 Annotated[Svc, Depends(...)] 参数和 svc: Svc = Depends(...) 一样按 Svc 定型。"""
    files = {"app/svc.py": """
        class Svc:
            async def place(self): ...
    """, "app/api.py": """
        from typing import Annotated
        import typing
        from fastapi import Depends
        from app.svc import Svc
        async def a(s: Annotated[Svc, Depends(get)], t: typing.Annotated["Svc", Depends()], u: Annotated[int, 1]):
            await s.place(); await t.place(); await u.place()
    """}
    assert _resolve_all(tmp_path, files, "a") == [
        ("s.place", "exact", "Svc.place"), ("t.place", "exact", "Svc.place"), ("u.place", "guessed", "Svc.place")]


# ---------- 按名字推测：只推测到方法，跳过内置方法名，只认调用方源码根下的定义 ----------


def test_guess_never_targets_module_level_functions(tmp_path):
    """x.name(...) 的唯一同名定义是模块级函数：不推测（真实项目里 db.refresh(obj) 曾连到路由函数 refresh）。
    同一源码根下同名的模块级函数和方法各一个：算两处定义，同样不推测。"""
    files = {"app/api.py": """
        async def refresh(token): ...
        def handler(db, other):
            db.refresh(1)
            other.ping_peer()
    """, "app/svc.py": """
        class Peer:
            def ping_peer(self): ...
        def ping_peer(): ...
    """}
    assert _resolve_all(tmp_path, files, "handler") == [
        ("db.refresh", "unresolved", None), ("other.ping_peer", "unresolved", None)]


def test_guess_skips_builtin_method_names(tmp_path):
    """方法名是常见内置 / 标准库类型的方法名：不推测，即使项目里恰有一个同名方法（真实项目里 path.open() 曾连到
    一个会话类的 open）。self.x()、有类型的接收者照常精确解析。"""
    from pystepmap.resolve import BUILTIN_METHOD_NAMES

    assert {"open", "read", "total_seconds", "match", "release", "info", "communicate", "fullmatch",
            "send", "throw", "aclose", "asend", "athrow"} <= BUILTIN_METHOD_NAMES  # 含生成器 / 协程的方法
    assert not any(n.startswith("_") for n in BUILTIN_METHOD_NAMES)
    assert not {"real", "days", "closed", "name", "begin"} & BUILTIN_METHOD_NAMES  # 不可调用的属性不算
    files = {"app/pty.py": """
        class Session:
            def open(self): ...
            def total_seconds(self): ...
            def begin(self):
                self.open()
        def use(path, delta, s, typed: Session):
            path.open(); delta.total_seconds(); s.begin(); typed.open()
    """}
    assert _resolve_all(tmp_path, files, "use") == [
        ("path.open", "unresolved", None),
        ("delta.total_seconds", "unresolved", None),
        ("s.begin", "guessed", "Session.begin"),
        ("typed.open", "exact", "Session.open"),
    ]
    assert _resolve_all(tmp_path / "b", files, "Session.begin") == [("self.open", "exact", "Session.open")]


def test_builtin_method_names_literal():
    """BUILTIN_METHOD_NAMES 是写死的清单（3.12 与 3.13 的并集）：等于当前解释器上重新算出的名字，加上只在另一个
    版本上才有的那几个。别的 Python 版本上不核对。"""
    from builtin_method_names import VERSION_ONLY, regenerate
    from pystepmap.resolve import BUILTIN_METHOD_NAMES

    here = sys.version_info[:2]
    if here not in ((3, 12), (3, 13)):
        pytest.skip("清单按 Python 3.12 与 3.13 生成")
    elsewhere = frozenset().union(*(names for v, names in VERSION_ONLY.items() if v != here))
    assert regenerate() == BUILTIN_METHOD_NAMES - elsewhere
    assert elsewhere <= BUILTIN_METHOD_NAMES


def test_never_guess_the_enclosing_function(tmp_path):
    """按名字推测不推到调用所在的函数（及其外层函数）自己：self._http.shutdown_all() 写在 shutdown_all 里，多半是
    转交给被包装的库对象（真实项目里的 self._http.aclose() 等）。"""
    files = {"app/m.py": """
        class Client:
            def shutdown_all(self):
                self._http.shutdown_all()
            def walk_tree(self):
                for c in self.children:
                    c.walk_tree()
            def flush_all(self):
                def inner(x):
                    x.flush_all()
                return inner
        def use(c):
            c.shutdown_all()
    """}
    assert _resolve_all(tmp_path, files, "Client.shutdown_all") == [("_http.shutdown_all", "unresolved", None)]
    assert _resolve_all(tmp_path / "b", files, "Client.walk_tree") == [("c.walk_tree", "unresolved", None)]
    assert _resolve_all(tmp_path / "c", files, "Client.flush_all.<locals>.inner") == [
        ("x.flush_all", "unresolved", None)]
    assert _resolve_all(tmp_path / "d", files, "use") == [("c.shutdown_all", "guessed", "Client.shutdown_all")]


def test_nested_class_methods_compete(tmp_path):
    """嵌套在类里、定义在函数里的类的方法也算同名定义。"""
    files = {"app/a.py": """
        class Top:
            def ping_it(self): ...
        class Outer:
            class Inner:
                def ping_it(self): ...
                def only_inner(self): ...
        def factory():
            class Local:
                def ping_it(self): ...
            return Local
        def use(x):
            x.ping_it(); x.only_inner()
    """}
    assert _resolve_all(tmp_path, files, "use") == [
        ("x.ping_it", "unresolved", None), ("x.only_inner", "guessed", "Outer.Inner.only_inner")]


def test_no_guess_through_super(tmp_path):
    """super().m(...) 执行的是某个基类的 m：按名字推测几乎总是推到调用方自己（真实项目里见到的 5 处都是），不推测。"""
    files = {"app/m.py": """
        from html.parser import HTMLParser
        class P(HTMLParser):
            def __init__(self):
                super().__init__()
            def feed_more(self, x):
                super(P, self).feed_more(x)
    """}
    assert _resolve_all(tmp_path, files, "P.__init__") == [
        ("super.__init__", "unresolved", None), ("super", "external", None)]
    assert _resolve_all(tmp_path / "b", files, "P.feed_more")[0] == ("super.feed_more", "unresolved", None)


GUESS_ROOTS = {
    "backend/app/__init__.py": "",
    "backend/app/svc.py": """
        class Svc:
            def place(self): ...
            def only_backend(self): ...
            def twice(self): ...
        class Other:
            def twice(self): ...
    """,
    "backend/app/api.py": """
        def route(x):
            x.place(); x.only_backend(); x.only_scripts(); x.twice()
    """,
    "scripts/tool.py": """
        class Local:
            def place(self): ...
            def only_scripts(self): ...
        def main(x):
            x.place(); x.only_scripts(); x.only_backend()
    """,
}


def test_guess_only_within_callers_source_root(tmp_path):
    """同名方法有多处定义、其中恰有一处在调用方所在的源码根下：推测到那一处；只有一处定义但在别的源码根下：不推测。
    调用方所在的源码根是包含它的最内层源码根（backend/app/api.py 在 backend，scripts/tool.py 在 .）。"""
    root = make_project(tmp_path, GUESS_ROOTS)
    index = build_index(root, ProjectConfig())
    assert index.source_roots == ["", "backend"]
    resolver = Resolver(index)

    def got(qualname):
        fn = find_fn(index, qualname)
        return [(r.display, r.how, r.target) for r in (resolver.resolve(c, fn) for c in _calls(fn.node))]

    assert got("route") == [
        ("x.place", "guessed", "backend/app/svc.py::Svc.place@3"),
        ("x.only_backend", "guessed", "backend/app/svc.py::Svc.only_backend@4"),
        ("x.only_scripts", "unresolved", None),
        ("x.twice", "unresolved", None),  # 本源码根下有两处
    ]
    assert got("main") == [
        ("x.place", "guessed", "scripts/tool.py::Local.place@3"),
        ("x.only_scripts", "guessed", "scripts/tool.py::Local.only_scripts@4"),
        ("x.only_backend", "unresolved", None),
    ]


def test_guessed_call_title_is_display_name(tmp_path):
    """推测得到的调用：节点标题用调用的显示名，不取被推测函数的文档注释；exact 的照旧取文档注释。"""
    from conftest import fg_of, node_by

    fg = fg_of(tmp_path, '''
        class Svc:
            def place_order(self):
                """下单"""
        def run(x, s: Svc):
            x.place_order()
            s.place_order()
    ''', "run")
    guessed, exact = node_by(fg, type="step", line=6), node_by(fg, type="step", line=7)
    assert [c.resolution for c in guessed.calls] == ["guessed"] and guessed.title == "x.place_order"
    assert [c.resolution for c in exact.calls] == ["exact"] and exact.title == "下单"
    assert fg.call_targets == ["app/mod.py::Svc.place_order@3"]  # 推测的调用照旧进 call_targets


# ---------- 模块级别名、类体里的别名、classmethod 里的 cls(...) ----------


ALIASES = {"app/a.py": """
    from app import b
    def func(): ...
    alias = func
    alias2 = b.target
    chained = alias
    class K:
        def m1(self): ...
        m2 = m1
        def use(self):
            self.m2()
    def run():
        alias(); alias2(); chained()
""", "app/b.py": """
    def target(): ...
""", "app/c.py": """
    from app.a import alias, alias2
    from app import a
    def other():
        alias(); alias2(); a.alias(); a.K().m2()
"""}


def test_module_and_class_body_aliases_resolve(tmp_path):
    """模块级 alias = func、alias = mod.func，类体里 m2 = m1：调用别名就是调用它指向的函数（exact）；别的模块
    from a import alias 进来的同样解析。"""
    func, target, m1 = "app/a.py::func@3", "app/b.py::target@2", "app/a.py::K.m1@8"
    assert _targets(tmp_path, ALIASES, "run") == [
        ("alias", "exact", func), ("alias2", "exact", target), ("chained", "exact", func)]
    assert _targets(tmp_path / "b", ALIASES, "K.use") == [("self.m2", "exact", m1)]
    assert _targets(tmp_path / "c", ALIASES, "other") == [
        ("alias", "exact", func), ("alias2", "exact", target), ("a.alias", "exact", func),
        ("K.m2", "exact", m1), ("a.K", "external", None)]  # K 没有 __init__：构造函数在项目外


def test_no_alias_for_rebound_or_computed_names(tmp_path):
    """同一个名字在模块级被赋值两次（目标不同）、赋值右边是调用结果 / partial(...) / 条件表达式 / 项目外的名字、
    同时被 import 绑定、或在函数里声明 global 后重新赋值：不建立别名，结果同以前。函数里的同名局部变量遮蔽模块级
    别名。类体里赋值两次也不算。"""
    files = {"app/a.py": """
        import functools
        import json as dumps
        def f(): ...
        def g(): ...
        def make(): ...
        twice = f
        twice = g
        same = f
        same = f
        made = make()
        part = functools.partial(f, 1)
        cond = f if make else g
        ext = functools.reduce
        dumps = f
        shadowed = f
        swapped = f
        def swap():
            global swapped
            swapped = g
        class K:
            def m1(self): ...
            def m3(self): ...
            m2 = m1
            m2 = m3
            def use(self):
                self.m2()
        def run():
            twice(); same(); made(); part(); cond(); ext(); dumps.loads(1); swapped()
        def local_shadow(c):
            shadowed = c
            shadowed()
        def local_shadow_later():
            shadowed()
            shadowed = None
    """}
    assert _targets(tmp_path, files, "run") == [
        ("twice", "unresolved", None), ("same", "exact", "app/a.py::f@4"), ("made", "unresolved", None),
        ("part", "unresolved", None), ("cond", "unresolved", None), ("ext", "unresolved", None),
        ("dumps.loads", "external", None), ("swapped", "unresolved", None)]  # 函数里 global 后重新赋值
    assert _targets(tmp_path / "b", files, "K.use") == [("self.m2", "unresolved", None)]
    assert _targets(tmp_path / "c", files, "local_shadow") == [("shadowed", "unresolved", None)]
    assert _targets(tmp_path / "d", files, "local_shadow_later") == [("shadowed", "unresolved", None)]


def test_alias_shapes_that_never_give_exact(tmp_path):
    """这些写法都不建立别名（不给 exact）：别名成环；if / try 的分支里赋给不同目标；del；元组赋值、增量赋值；
    右边的名字在模块里绑定了不止一次（try 里导入、except 里再定义；def 两次；import 后又赋值）；模块里有
    from x import *；类体里的别名写在它指向的方法定义之前。"""
    cases = {
        "cycle": ("a1 = a2\na2 = a1\n", "a1"),
        "if_else": ("import sys\nif sys.platform == 'x':\n    alias = f\nelse:\n    alias = g\n", "alias"),
        "try_except": ("try:\n    alias = f\nexcept ImportError:\n    alias = g\n", "alias"),
        "del": ("alias = f\ndel alias\n", "alias"),
        "tuple": ("alias = f\nalias, other = g, g\n", "alias"),
        "augmented": ("alias = f\nalias += f\n", "alias"),
        "try_import_fallback": (("try:\n    from app.fast import impl\nexcept ImportError:\n    def impl(): ...\n"
                                 "alias = impl\n"), "alias"),
        "def_twice": ("def h(): ...\nalias = h\ndef h(): ...\n", "alias"),
        "import_then_rebound": ("from app.fast import impl\nimpl = g\nalias = impl\n", "alias"),
        "star_import": ("alias = f\nfrom app.fast import *\n", "alias"),
    }
    for i, (name, (body, called)) in enumerate(cases.items()):
        files = {"app/fast.py": "def impl(): ...\nalias = impl\n",
                 "app/a.py": f"def f(): ...\ndef g(): ...\n{body}def use():\n    {called}()\n"}
        got = _targets(tmp_path / str(i), files, "use")
        assert got[0][1] != "exact", (name, got)
    files = {"app/a.py": """
        def helper(self): ...
        class K:
            m2 = helper
            def helper(self): ...
            def use(self):
                self.m2()
    """}
    assert _targets(tmp_path / "k", files, "K.use") == [("self.m2", "unresolved", None)]  # 运行时是模块级的 helper


CLS_CALLS = {"app/base.py": """
    class Base:
        def __init__(self, x): ...
        @classmethod
        def make(cls, x):
            cls.helper()
            return cls(x)
        @classmethod
        def helper(cls): ...
    class Leaf(Base):
        @classmethod
        def build(cls):
            return cls(1)
    class Parent:
        def __init__(self): ...
        @classmethod
        def make(cls):
            return cls()
    class Root:
        def __init__(self): ...
        @classmethod
        def make(cls):
            return cls()
    class Mid(Root): ...
    class NoInit:
        @classmethod
        def make(cls):
            return cls()
    class NoInitWithSub:
        @classmethod
        def make(cls):
            return cls()
    class Solo:
        def __init__(self): ...
        @classmethod
        def make(cls):
            return cls()
""", "app/subs.py": """
    from app.base import Parent, Mid, NoInitWithSub, Solo
    class Child(Parent):
        def __init__(self): ...
    class Deep(Mid):
        def __init__(self): ...
    class WithInit(NoInitWithSub):
        def __init__(self): ...
    def factory():
        class Inner(Solo):
            def __init__(self): ...
        return Inner
"""}


def _cls_got(index, qualname):
    resolver = Resolver(index)
    fn = find_fn(index, qualname)
    return [(r.display, r.how, r.target, r.constructor) for r in (resolver.resolve(c, fn) for c in _calls(fn.node))]


def test_cls_call_in_classmethod(tmp_path):
    """@classmethod 里的 cls(...)：运行时 cls 可能是任何项目内子类。这个类和它的每个项目内子类（直接、间接，含定义在
    函数里的）按方法查找得到的 __init__ 都是同一个函数时为 exact；有不同的时无法判断（不推测）。这个类和子类都得不到
    项目内的 __init__ 时同调用项目内的类（项目外）。cls.其他方法(...) 同以前。"""
    index = build_index(make_project(tmp_path, CLS_CALLS), ProjectConfig())
    base_init = "app/base.py::Base.__init__@3"
    assert _cls_got(index, "Base.make") == [("cls.helper", "exact", "app/base.py::Base.helper@9", False),
                                            ("cls", "exact", base_init, True)]  # 子类 Leaf 没有自己的 __init__
    assert _cls_got(index, "Leaf.build") == [("cls", "exact", base_init, True)]
    assert _cls_got(index, "Parent.make") == [("cls", "unresolved", None, False)]  # Child 有自己的 __init__
    assert _cls_got(index, "Root.make") == [("cls", "unresolved", None, False)]  # 间接子类 Deep
    assert _cls_got(index, "NoInit.make") == [("cls", "external", None, False)]
    assert _cls_got(index, "NoInitWithSub.make") == [("cls", "unresolved", None, False)]
    assert _cls_got(index, "Solo.make") == [("cls", "unresolved", None, False)]  # 子类定义在函数里


CLS_SHAPES = {"app/a.py": """
    import sys
    def other_init(self): ...
    class FromDict:
        @classmethod
        def from_dict(cls, d):
            return cls(**d)
    class Base:
        def __init__(self, **kw): ...
    class Svc(FromDict, Base): ...
    class InitMixin:
        def __init__(self, *a): ...
    class A:
        def __init__(self, *a): ...
        @classmethod
        def make(cls):
            return cls()
    class B(InitMixin, A): ...
    class C:
        def __init__(self): ...
        @classmethod
        def make(cls):
            return cls()
    class D(C):
        __init__ = other_init
    class E:
        def __init__(self): ...
        @classmethod
        def make(cls):
            return cls()
    class F(E):
        if sys.version_info >= (3,):
            def __init__(self): ...
"""}


def test_cls_call_when_subclasses_get_init_elsewhere(tmp_path):
    """子类的 __init__ 来自别的项目基类（混入类在前）、类体里 __init__ = 函数、类体里 if 下的 def __init__：都无法
    判断，不进 call_targets。"""
    index = build_index(make_project(tmp_path, CLS_SHAPES), ProjectConfig())
    for qualname in ("FromDict.from_dict", "A.make", "C.make", "E.make"):
        assert _cls_got(index, qualname) == [("cls", "unresolved", None, False)], qualname
    graph = analyze_project(tmp_path)
    for qualname in ("FromDict.from_dict", "A.make", "C.make", "E.make"):
        fg = find_fn(graph, qualname)
        assert fg.call_targets == [] and [u.name for u in fg.unresolved] == ["cls"], qualname


def test_cls_call_verified_on_real_run(tmp_path):
    """真实执行：Svc.from_dict 实际调用 Base.__init__，C2.make 实际调用 C2.__init__；核对没有"调用不在图上"，
    这两处记为动态调用。"""
    files = {"app/__init__.py": "", "app/a.py": """
        class FromDict:
            @classmethod
            def from_dict(cls, d):
                return cls(**d)

        class Base:
            def __init__(self, **kw):
                self.kw = kw

        class Svc(FromDict, Base):
            pass

        class P:
            def __init__(self):
                self.x = 1

            @classmethod
            def make(cls):
                return cls()

        class C2(P):
            def __init__(self):
                self.x = 2
    """}
    test_src = """
        from app.a import C2, Svc

        def test_it():
            Svc.from_dict({"a": 1})
            C2.make()
    """
    recs = run_traced(tmp_path, files, test_src)
    graph = from_json((tmp_path / "graph.json").read_text(encoding="utf-8"))
    report = verify(graph, recs)
    assert [m for m in report.mismatches if m.kind == "call"] == []
    assert {parse_key(d.callee)[1] for d in report.dynamic_call_list} >= {"Base.__init__", "C2.__init__"}


SUBSCRIPTED_BASE = {"app/repo.py": """
    from typing import Generic, TypeVar
    T = TypeVar("T")
    class BaseRepo(Generic[T]):
        def __init__(self, session): ...
        def helper(self): ...
        @classmethod
        def create(cls, session):
            return cls(session)
    class UserRepo(BaseRepo[int]):
        def __init__(self, session): ...
    def use():
        UserRepo(1).helper()
"""}


def test_subscripted_base_is_a_base(tmp_path):
    """带下标的基类（BaseRepo[int]）也是基类：cls(...) 能看到这样写的子类（它有自己的 __init__，无法判断），
    方法查找也沿它找到基类上的方法。"""
    index = build_index(make_project(tmp_path, SUBSCRIPTED_BASE), ProjectConfig())
    assert _cls_got(index, "BaseRepo.create") == [("cls", "unresolved", None, False)]
    assert _resolve_all(tmp_path / "b", SUBSCRIPTED_BASE, "use") == [
        ("UserRepo.helper", "exact", "BaseRepo.helper"), ("UserRepo", "exact", "UserRepo.__init__")]


def test_calling_the_result_of_a_call_is_not_named_like_the_inner_call():
    """f()() 外层那次调用的显示名是 "f()"，handlers[k]() 是 "handlers[…]"：自检里列目标不明时不会和 f 本身混在一起。"""
    import ast

    from pystepmap.resolve import _display

    def name(src: str) -> str:
        return _display(ast.parse(src, mode="eval").body.func)

    assert name("get_factory()()") == "get_factory()"
    assert name("a.get_factory()()") == "a.get_factory()"
    assert name("handlers[k]()") == "handlers[…]"
    assert name("f()") == "f" and name("a.b.c()") == "b.c"
