"""pytest 执行记录插件：在临时项目里用子进程跑 pytest -p pystepmap.trace，读回 JSONL。"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import os
import subprocess
import sys
from itertools import groupby

from conftest import CORE_SRC, is_progress, one, recs_of, run_traced, write_files
from pystepmap import trace
from pystepmap.analyze import analyze_project
from pystepmap.model import parse_key, to_json


def errors(recs: list[dict]) -> list[dict]:
    return [r for r in recs if "error" in r]


def test_sync_lines_exit_and_calls(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": """
        def load(x):
            return x
        def handler(x):
            v = load(x)
            if v is None:
                return "skip"
            return "ok"
    """}, "from app.t import handler\ndef test_a():\n    assert handler(1) == 'ok'\n")
    h = one(recs, "handler")
    assert h["lines"] == [4, 5, 7] and h["exit"] == "return" and h["exc"] is None
    assert h["calls"] == [["app/t.py::load@1", 4]]  # [被调函数, 调用方当时所在的行]
    assert h["test"].endswith("::test_a")
    assert h["fn"] == "app/t.py::handler@3" and h["n"] == 1
    assert one(recs, "load")["lines"] == [2] and errors(recs) == []


def test_coroutine_suspension_is_one_record(tmp_path):
    recs = run_traced(tmp_path, {"app/c.py": """
        import asyncio
        async def step():
            return 2
        async def handler():
            await asyncio.sleep(0)
            await step()
            return 1
    """}, "import asyncio\nfrom app.c import handler\ndef test_a():\n    assert asyncio.run(handler()) == 1\n")
    h = one(recs, "handler")
    assert h["lines"] == [5, 6, 7] and h["exit"] == "return" and h["exc"] is None
    assert h["calls"] == [["app/c.py::step@2", 6]]
    assert errors(recs) == []


def test_exit_by_exception_through_with_block(tmp_path):
    recs = run_traced(tmp_path, {"app/w.py": """
        from contextlib import contextmanager
        @contextmanager
        def ctx():
            yield
        def boom():
            raise ValueError("x")
        def through_with():
            with ctx():
                boom()
            return "never"
        def through_finally():
            try:
                boom()
            finally:
                done = True
            return done
        def caught():
            try:
                boom()
            except ValueError:
                pass
            return "ok"
    """}, """
        import pytest
        from app.w import caught, through_finally, through_with
        def test_a():
            with pytest.raises(ValueError):
                through_with()
            with pytest.raises(ValueError):
                through_finally()
            assert caught() == "ok"
    """)
    for name in ("through_with", "through_finally"):
        r = one(recs, name)
        assert (r["exit"], r["exc"]) == ("raise", "ValueError"), r
    w = one(recs, "through_with")
    # 异常穿出时 with 行再执行一次（调用 __exit__）；ctx 由 contextlib 的 __enter__（项目外的帧）启动，不算直接调用
    assert w["lines"] == [8, 9, 8] and w["calls"] == [["app/w.py::boom@5", 9]]
    assert one(recs, "through_finally")["lines"] == [12, 13, 15]
    c = one(recs, "caught")
    assert (c["exit"], c["exc"]) == ("return", None) and c["lines"] == [18, 19, 20, 21, 22]
    assert one(recs, "boom")["n"] == 3
    assert errors(recs) == []


def test_cancelled_coroutine_does_not_crash(tmp_path):
    recs = run_traced(tmp_path, {"app/c.py": """
        import asyncio
        async def handler():
            await asyncio.sleep(10)
            return 1
    """}, """
        import asyncio
        import pytest
        from app.c import handler
        async def main():
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(handler(), 0.01)
        def test_a():
            asyncio.run(main())
    """)
    h = one(recs, "handler")
    # 3.13 起被取消时在 await 那一行再产生一次行事件（[3, 3]）：只看去掉连续重复之后的行
    assert (h["exit"], h["exc"]) == ("raise", "CancelledError") and [k for k, _ in groupby(h["lines"])] == [3]
    assert errors(recs) == []


def test_same_named_inner_functions_are_distinguished(tmp_path):
    recs = run_traced(tmp_path, {"app/i.py": """
        def a(x):
            def reject():
                return "a"
            return reject()
        def b(x):
            def reject():
                return "b"
            return reject()
    """}, "from app.i import a, b\ndef test_a():\n    assert a(1) == 'a' and b(1) == 'b'\n")
    ra, rb = one(recs, "a.<locals>.reject"), one(recs, "b.<locals>.reject")
    assert ra["fn"] == "app/i.py::a.<locals>.reject@2" and ra["lines"] == [3]
    assert rb["fn"] == "app/i.py::b.<locals>.reject@6" and rb["lines"] == [7]
    assert one(recs, "a")["calls"] == [[ra["fn"], 4]] and one(recs, "b")["calls"] == [[rb["fn"], 8]]


def test_other_threads_are_traced(tmp_path):
    recs = run_traced(tmp_path, {"app/th.py": """
        import threading
        def step():
            return 1
        def handler():
            t = threading.Thread(target=step)
            t.start()
            t.join()
            return 0
    """}, "from app.th import handler\ndef test_a():\n    assert handler() == 0\n")
    s = one(recs, "step")
    assert s["lines"] == [3] and s["exit"] == "return" and s["test"].endswith("::test_a")
    assert one(recs, "handler")["calls"] == []  # 经由线程的调用不算直接调用


def test_no_values_recorded(tmp_path):
    recs = run_traced(tmp_path, {"app/s.py": """
        def check(token):
            if token.startswith("SECRET"):
                raise PermissionError(token)
            return token
        def handler(token):
            local_copy = token * 2
            try:
                return check(local_copy)
            except PermissionError:
                return None
    """}, "from app.s import handler\ndef test_a():\n    assert handler('SECRET-TOKEN-123') is None\n")
    assert one(recs, "check")["exc"] == "PermissionError" and one(recs, "handler")["exit"] == "return"
    assert "SECRET-TOKEN-123" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_terminal_summary_says_what_was_recorded(tmp_path):
    """测试结束时，pytest 的汇总里有一行：记了几个函数、写到哪个文件；或者让插件停用的错误。没启用时什么都不说。"""
    files = {"app/m.py": "def f():\n    return g()\ndef g():\n    return 1\n"}
    test = "from app.m import f\ndef test_a():\n    assert f() == 1\n"
    out: list[str] = []
    run_traced(tmp_path / "ok", files, test, output=out)
    assert "pystepmap 记录：2 个函数" in out[0] and str(tmp_path / "ok" / "trace.jsonl") in out[0]
    run_traced(tmp_path / "bad", files, test, graph_text="{}", output=out)
    assert "pystepmap 记录插件已停用" in out[1] and "schema_version" in out[1]
    run_traced(tmp_path / "off", files, test, active=False, output=out)
    assert "pystepmap" not in out[2]


def test_trace_file_has_a_version_header_and_tests_carry_no_parameter_ids(tmp_path):
    """记录文件第一行是格式和版本；参数化测试的 [参数] 后缀可能含取值，记录的 test 里去掉。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    return x\n"}, """
        import pytest
        from app.m import f
        @pytest.mark.parametrize("secret", ["hunter2", "s3cr3t"])
        def test_p(secret):
            assert f(secret) == secret
    """)
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"v": 1, "format": "pystepmap-trace"}
    assert sum(1 for x in lines if '"format"' in x) == 1
    assert {r["test"] for r in recs_of(recs, "f")} == {"tests/test_x.py::test_p"}
    assert "hunter2" not in "".join(lines) and "s3cr3t" not in "".join(lines)


def test_inactive_without_env(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": "def f():\n    return 1\n"},
                      "from app.t import f\ndef test_a():\n    assert f() == 1\n", active=False)
    assert recs == [] and not (tmp_path / "trace.jsonl").exists()


def test_identical_executions_in_one_test_are_counted(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": """
        def f(x):
            if x is None:
                return 0
            return x
    """}, """
        from app.t import f
        def test_a():
            for _ in range(3):
                f(1)
            f(None)
        def test_b():
            f(2)
    """)
    got = sorted((r["test"].rsplit("::", 1)[1], r["lines"], r["n"]) for r in recs_of(recs, "f"))
    assert got == [("test_a", [2, 3], 1), ("test_a", [2, 4], 3), ("test_b", [2, 4], 1)]


def test_long_executions_are_truncated_but_keep_exit_and_calls(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": """
        def helper():
            return 1
        def churn(n):
            total = 0
            for i in range(n):
                total += i
            helper()
            raise ValueError(total)
        def short():
            return 2
    """}, """
        import pytest
        from app.t import churn, short
        def test_a():
            for _ in range(2):
                with pytest.raises(ValueError):
                    churn(1000)
            assert short() == 2
    """, env_extra={"PYSTEPMAP_TRACE_MAX_LINES": "50"})
    c = one(recs, "churn")  # 两次相同的截断执行合并成一条
    assert len(c["lines"]) == 50 and c["lines"][:4] == [4, 5, 6, 5] and c["truncated"] is True
    assert (c["exit"], c["exc"], c["n"]) == ("raise", "ValueError", 2)
    assert c["calls"] == [["app/t.py::helper@1", 7]]  # 截断之后发生的调用照样记下
    assert "truncated" not in one(recs, "short") and "truncated" not in one(recs, "helper")
    assert errors(recs) == []


def test_invalid_max_lines_writes_error_and_tests_still_pass(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": "def f():\n    return 1\n"},
                      "from app.t import f\ndef test_a():\n    assert f() == 1\n",
                      env_extra={"PYSTEPMAP_TRACE_MAX_LINES": "0"})
    assert recs == [{"error": "ValueError: PYSTEPMAP_TRACE_MAX_LINES 必须是正整数：'0'"}]


def test_generators_and_async_generators_do_not_crash(tmp_path):
    recs = run_traced(tmp_path, {"app/g.py": """
        def gen():
            yield 1
            yield 2
        async def agen():
            yield 1
            yield 2
        async def consume():
            return [x async for x in agen()]
    """}, """
        import asyncio
        from app.g import consume, gen
        def test_a():
            assert list(gen()) == [1, 2]
            it = gen()
            assert next(it) == 1
            it.close()
            assert asyncio.run(consume()) == [1, 2]
    """)
    gens = sorted((r["exit"], r["exc"], r["lines"]) for r in recs_of(recs, "gen"))
    assert gens == [("raise", "GeneratorExit", [2]), ("return", None, [2, 3])]
    a = one(recs, "agen")
    assert a["exit"] == "return" and a["lines"] == [5, 6]
    assert one(recs, "consume")["calls"] == []  # 异步生成器的开始不记给任何人（见下一个测试）
    assert errors(recs) == []


def test_generator_starts_are_not_attributed_to_any_caller(tmp_path):
    """make 创建生成器、交给 total 去迭代：生成器的帧在 total 里开始，但 make 和 total 都不记它；协程照常记。"""
    recs = run_traced(tmp_path, {"app/g.py": """
        def numbers():
            yield 1
            yield 2
        def make():
            return numbers()
        def total(it):
            s = 0
            for x in it:
                s += x
            return s
        def run():
            return total(make())
        async def step():
            return 2
        async def co():
            return await step()
    """}, """
        import asyncio
        from app.g import co, run
        def test_a():
            assert run() == 3
            assert asyncio.run(co()) == 2
    """)
    assert one(recs, "make")["calls"] == [] and one(recs, "total")["calls"] == []
    assert one(recs, "numbers")["lines"] == [2, 3]  # 生成器自己的记录照常
    assert one(recs, "run")["calls"] == [["app/g.py::make@4", 12], ["app/g.py::total@6", 12]]
    assert one(recs, "co")["calls"] == [["app/g.py::step@13", 16]]
    assert errors(recs) == []


def test_decorated_functions_methods_and_duplicate_defs_match(tmp_path):
    recs = run_traced(tmp_path, {"app/d.py": """
        import functools
        import sys
        def logged(fn):
            @functools.wraps(fn)
            def wrapper(*a):
                return fn(*a)
            return wrapper
        class Svc:
            @logged
            def run(self):
                return pick()
        if sys.version_info >= (3,):
            def pick():
                return 1
        else:
            def pick():
                return 2
    """}, "from app.d import Svc\ndef test_a():\n    assert Svc().run() == 1\n")
    run = one(recs, "Svc.run")
    assert run["fn"] == "app/d.py::Svc.run@10" and run["lines"] == [11]
    assert run["calls"] == [["app/d.py::pick@13", 11]] and one(recs, "pick")["fn"] == "app/d.py::pick@13"
    assert one(recs, "logged.<locals>.wrapper")["calls"] == [[run["fn"], 6]]
    assert one(recs, "logged")["test"] == ""  # 导入时（收集阶段）的执行不属于任何测试


def test_unreadable_graph_writes_error_and_tests_still_pass(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": "def f():\n    return 1\n"},
                      "from app.t import f\ndef test_a():\n    assert f() == 1\n", graph_text="{not json")
    assert len(recs) == 1 and recs[0]["error"].startswith("JSONDecodeError: ")


def test_relative_paths_survive_a_test_that_changes_directory(tmp_path):
    """文档里的用法就是相对路径（PYSTEPMAP_TRACE_OUT=t.jsonl）；某个测试 chdir 之后，记录仍要写进同一个文件。"""
    run_traced(tmp_path, {"app/t.py": "def f(x):\n    return x\n"}, """
        import os
        from app.t import f
        def test_a():
            f(1)
        def test_b():
            os.makedirs("sub", exist_ok=True)
            os.chdir("sub")
            f(2)
        def test_c():
            f(3)
    """, env_extra={"PYSTEPMAP_GRAPH": "../graph.json", "PYSTEPMAP_TRACE_OUT": "t.jsonl"})
    proj = tmp_path / "proj"
    assert sorted(proj.rglob("*.jsonl")) == [proj / "t.jsonl"]
    recs = [json.loads(line) for line in (proj / "t.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["test"].rsplit("::", 1)[1] for r in recs_of(recs, "f")] == ["test_a", "test_b", "test_c"]
    assert errors(recs) == []


def test_records_survive_os_exit_up_to_the_current_test(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": "def f(x):\n    return x\n"}, """
        import os
        from app.t import f
        def test_a():
            f(1)
        def test_b():
            f(2)
            os._exit(0)
    """)
    assert [r["test"].rsplit("::", 1)[1] for r in recs_of(recs, "f")] == ["test_a"]


def test_plugin_does_not_record_its_own_functions(tmp_path):
    """分析 pystepmap 自己时 trace.py 也在图里，插件自己的钩子、写出函数不能被记录。"""
    graph, out = tmp_path / "graph.json", tmp_path / "trace.jsonl"
    graph.write_text(to_json(analyze_project(CORE_SRC)), encoding="utf-8")
    trace.start(str(graph), str(out))
    try:
        trace.pytest_runtest_logstart("t::a", None)
        parse_key("a.py::f@1")  # pystepmap 自己的普通函数照常记录
        trace.pytest_runtest_logfinish("t::a", None)
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["fn"] for r in recs if "fn" in r] == [f"pystepmap/model.py::parse_key@{parse_key.__code__.co_firstlineno}"]


def test_internal_error_disables_plugin_and_keeps_host_code_working(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    write_files(proj, {"faulty_mod.py": "def f(x):\n    return x + 1\n"})
    graph, out = tmp_path / "graph.json", tmp_path / "trace.jsonl"
    graph.write_text(to_json(analyze_project(proj)), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("pystepmap_faulty_mod", proj / "faulty_mod.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def broken(*args):
        raise RuntimeError("注入的故障")

    trace.start(str(graph), str(out))
    try:
        assert mod.f(1) == 2  # 正常记录，执行外的调用 test 为 ""
        monkeypatch.setattr(trace, "_finish", broken)
        assert mod.f(2) == 3 and mod.f(3) == 4
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert recs[0] == {"v": 1, "format": "pystepmap-trace"}
    assert recs[1]["fn"] == "faulty_mod.py::f@1" and recs[1]["test"] == "" and recs[1]["lines"] == [2]
    assert recs[2:] == [{"error": "RuntimeError: 注入的故障"}]


def test_calls_record_the_line_where_the_call_happened(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": """
        class Svc:
            def handle(self, x):
                return x
        def helper(x):
            return x
        def caller(svc, xs):
            for x in xs:
                helper(x)
            helper(0)
            w = (svc
                 .handle(1))
            return w
    """}, "from app.t import Svc, caller\ndef test_a():\n    caller(Svc(), [1, 2])\n")
    h, s = "app/t.py::helper@4", "app/t.py::Svc.handle@2"
    # 循环里同一行的两次调用只记一次；同一函数在另一行再调用另记一对；多行语句里记调用实际发生的那一行（这里是续行）
    assert one(recs, "caller")["calls"] == [[h, 8], [h, 9], [s, 11]]
    assert errors(recs) == []


def test_coroutine_created_elsewhere_is_not_recorded_as_a_call(tmp_path):
    """协程在一处创建、在别处或之后才 await：帧在 await 的地方开始，但创建它的不是那里，不记进任何人的 calls
    （真实项目里的两种写法：存进变量隔两行再 await；当参数传进来再 await）。被调函数自己的记录照常。"""
    recs = run_traced(tmp_path, {"app/c.py": """
        import asyncio
        async def step(x):
            return x
        async def stored(starter=None):
            result = (starter or step)(1)
            if asyncio.iscoroutine(result):
                return await result
        async def via(coro):
            return await coro
        async def passer():
            return await via(step(2))
    """}, """
        import asyncio
        from app.c import passer, stored
        def test_a():
            assert asyncio.run(stored()) == 1
            assert asyncio.run(passer()) == 2
    """)
    assert one(recs, "stored")["calls"] == []
    assert one(recs, "via")["calls"] == []
    assert one(recs, "passer")["calls"] == [["app/c.py::via@8", 11]]  # via(...) 在同一个表达式里创建并 await
    step = one(recs, "step")  # 两次执行完全相同，合并成一条
    assert (step["lines"], step["exit"], step["n"]) == ([3], "return", 2)
    assert errors(recs) == []


def test_coroutines_awaited_where_they_are_created_are_still_recorded(tmp_path):
    recs = run_traced(tmp_path, {"app/c.py": """
        import asyncio
        class Svc:
            async def method(self, x):
                return x
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
        class It:
            def __init__(self):
                self.n = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                self.n += 1
                if self.n > 2:
                    raise StopAsyncIteration
                return self.n
        async def f(x):
            return x
        def plain(x):
            return x
        async def a_method(svc):
            return await svc.method(1)
        async def a_multiline(args, kw):
            v = await f(
                1,
            )
            return v + await f(*args) + await f(**kw)
        async def a_with(svc):
            async with svc:
                return 1
        async def a_for():
            return [x async for x in It()] + [x async for x in It()]
        def a_sync():
            return plain(1)
        async def a_wrapped():
            return await asyncio.wait_for(f(1), 1) + sum(await asyncio.gather(f(1), f(2)))
        async def a_kw():
            return await f(x=1)
        async def a_with_exc(svc):
            try:
                async with svc:
                    raise ValueError
            except ValueError:
                return 1
        async def a_walrus():
            return await (v := f(1))
    """}, """
        import asyncio
        from app.c import Svc, a_for, a_kw, a_method, a_multiline, a_sync, a_walrus, a_with, a_with_exc, a_wrapped
        def test_a():
            assert asyncio.run(a_method(Svc())) == 1
            assert asyncio.run(a_multiline([1], {"x": 1})) == 3
            assert asyncio.run(a_with(Svc())) == 1
            assert asyncio.run(a_for()) == [1, 2, 1, 2]
            assert a_sync() == 1
            assert asyncio.run(a_wrapped()) == 4
            assert asyncio.run(a_kw()) == 1
            assert asyncio.run(a_with_exc(Svc())) == 1
            assert asyncio.run(a_walrus()) == 1
    """)
    k = "app/c.py::"
    assert one(recs, "a_method")["calls"] == [[k + "Svc.method@3", 24]]
    assert one(recs, "a_multiline")["calls"] == [[k + "f@19", 26], [k + "f@19", 29]]  # 多行的 await 记在它开头那一行
    assert one(recs, "a_with")["calls"] == [[k + "Svc.__aenter__@5", 31], [k + "Svc.__aexit__@7", 31]]
    assert one(recs, "a_sync")["calls"] == [[k + "plain@21", 36]]
    assert one(recs, "a_for")["calls"] == [[k + "It.__init__@10", 34], [k + "It.__aiter__@12", 34],
                                           [k + "It.__anext__@14", 34]]
    # wait_for / gather 里的协程由 asyncio 自己启动，开始它的帧不是 a_wrapped：和改动前一样不记
    assert one(recs, "a_wrapped")["calls"] == []
    assert one(recs, "a_kw")["calls"] == [[k + "f@19", 40]]  # 3.13 起是 CALL_KW
    assert one(recs, "a_with_exc")["calls"] == [[k + "Svc.__aenter__@5", 43], [k + "Svc.__aexit__@7", 43]]  # 异常退出
    assert one(recs, "a_walrus")["calls"] == [[k + "f@19", 48]]
    assert errors(recs) == []


def _existing_await_lines(src: str) -> set[int]:
    """src 里各函数中"await 的是已有对象"（不记调用）的 SEND 所在的行。"""
    import dis
    out: set[int] = set()

    def walk(code):
        offsets = trace._existing_await_offsets(code)
        out.update(i.positions.lineno for i in dis.get_instructions(code) if i.opname == "SEND" and i.offset in offsets)
        for c in code.co_consts:
            if hasattr(c, "co_code"):
                walk(c)

    walk(compile(src, "<s>", "exec"))
    return out


def test_await_of_an_existing_object_is_told_apart_by_bytecode():
    """只有 await 的对象是读出来的已有对象（局部变量、闭包变量、全局、属性、下标）时不记调用；其余（调用、关键字调用、
    *args、async with / async for、海象赋值、两支汇合的条件表达式）都照记。各 Python 版本的指令不同（3.13 的 CALL_KW、
    同一行先存后读合并成的 STORE_FAST_LOAD_FAST，3.15 在 SEND 前多出 PUSH_NULL / LOAD_COMMON_CONSTANT、下标是 BINARY_OP），
    这个测试在哪个版本上跑都应当通过。"""
    src = """
G = None
async def g(f, o, cm, it, coro, d):
    await f(x=1)
    await o.m(1, k=2)
    await f(1)
    await f(*d)
    async with cm:
        pass
    async for _ in it:
        pass
    await coro
    await o.c
    await G
    await d[0]
    await (v := f(1))
    await (coro if o else f(2))
    try:
        async with cm:
            raise ValueError
    except ValueError:
        pass
async def h(coro):
    async def inner():
        await coro
    return inner
async def k(coros, make):
    out = [await c for c in coros]
    for c in coros: await c
    c = make(); await c
    return out
"""
    assert _existing_await_lines(src) == {12, 13, 14, 15, 25, 28, 29, 30}


# ---------- 阶段 6：每个行事件的耗时、每个测试的结果 ----------

def _times_ok(r: dict) -> bool:
    return len(r["ns"]) == len(r["lines"]) and all(type(x) is int and x >= 0 for x in r["ns"])


def test_line_times_are_recorded_and_await_time_goes_to_the_awaiting_line(tmp_path):
    recs = run_traced(tmp_path, {"app/c.py": """
        import asyncio
        async def step():
            return 2
        async def handler():
            x = 1
            await asyncio.sleep(0.05)
            await step()
            return x
    """}, "import asyncio\nfrom app.c import handler\ndef test_a():\n    assert asyncio.run(handler()) == 1\n")
    h = one(recs, "handler")
    assert [k for k, _ in groupby(h["lines"])] == [5, 6, 7, 8] and _times_ok(h)
    # 协程在 await 挂起的时间算在挂起它的那一行上（3.13 起恢复时这一行可能再有一个行事件：按行相加）
    assert sum(ns for ln, ns in zip(h["lines"], h["ns"]) if ln == 6) >= 40_000_000
    assert _times_ok(one(recs, "step")) and errors(recs) == []


def _in_process(tmp_path, src: str, name: str = "timed_mod"):
    """在本进程里加载 src（写成 proj/<name>.py）并分析出图；返回 (模块, 图文件, 记录文件)。"""
    proj = tmp_path / "proj"
    write_files(proj, {f"{name}.py": src})
    graph, out = tmp_path / "graph.json", tmp_path / "trace.jsonl"
    graph.write_text(to_json(analyze_project(proj)), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"pystepmap_{name}", proj / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, graph, out


def test_identical_executions_add_up_their_times(tmp_path, monkeypatch):
    """两次相同的执行合并成一条：n 为 2，ns 逐项是两次之和。时钟换成每读一次走 10 纳秒，结果是确定的。"""
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    y = x + 1\n    return y\n")
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(trace, "_CLOCK", [lambda: next(ticks)])
    trace.start(str(graph), str(out))
    try:
        trace.pytest_runtest_logstart("t.py::a", None)
        mod.f(1)
        trace.pytest_runtest_logfinish("t.py::a", None)
        trace.pytest_runtest_logstart("t.py::b", None)
        mod.f(2)
        mod.f(3)
        trace.pytest_runtest_logfinish("t.py::b", None)
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    a, b = [r for r in recs if "fn" in r]
    assert (a["test"], a["lines"], a["n"], b["test"], b["lines"], b["n"]) == ("t.py::a", [2, 3], 1, "t.py::b", [2, 3], 2)
    assert _times_ok(a) and all(x > 0 for x in a["ns"])
    assert b["ns"] == [2 * x for x in a["ns"]]


def test_many_identical_executions_add_up_in_batches(tmp_path, monkeypatch):
    """相同的执行攒一批再逐项相加（跨过几批、最后剩半批）：结果和一次一次加完全一样。"""
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    y = x + 1\n    return y\n", "batch_mod")
    ticks = itertools.count(0, 7)
    monkeypatch.setattr(trace, "_CLOCK", [lambda: next(ticks)])
    trace.start(str(graph), str(out))
    try:
        for name, k in (("t.py::one", 1), ("t.py::many", 5000)):
            trace.pytest_runtest_logstart(name, None)
            for i in range(k):
                mod.f(i)
            trace.pytest_runtest_logfinish(name, None)
    finally:
        trace.stop()
    one_, many = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if '"fn"' in line]
    assert (one_["n"], many["n"]) == (1, 5000) and 5000 % trace._MERGE_RUNS != 0
    assert many["ns"] == [5000 * x for x in one_["ns"]] and all(x > 0 for x in one_["ns"])


def test_truncated_records_truncate_times_too(tmp_path):
    recs = run_traced(tmp_path, {"app/t.py": """
        def churn(n):
            total = 0
            for i in range(n):
                total += i
            return total
    """}, "from app.t import churn\ndef test_a():\n    churn(1000)\n    churn(1000)\n",
        env_extra={"PYSTEPMAP_TRACE_MAX_LINES": "50"})
    c = one(recs, "churn")
    assert c["truncated"] is True and c["n"] == 2 and len(c["lines"]) == 50 and _times_ok(c)


def test_one_result_line_per_test_and_no_values(tmp_path):
    """每个测试结束时一条结果行；参数化的用例 id 相同、各一条；失败、出错、跳过各有 outcome；记录文件里搜不到断言、
    参数、异常信息、跳过原因里的任何字面量。"""
    recs = run_traced(tmp_path, {"app/m.py": """
        def check(x):
            if x == "never":
                return 0
            raise ValueError("ZQX-EXC-5506 " + x)
    """}, """
        import time
        import pytest
        from app.m import check
        def test_ok():
            time.sleep(0.05)
        @pytest.mark.parametrize("secret", ["ZQX-PARAM-7731", "ZQX-PARAM-7732"])
        def test_p(secret):
            assert secret.startswith("ZQX")
        def test_fail():
            assert check("ZQX-ARG-3318") == "ZQX-ASSERT-4412", "ZQX-MSG-9087"
        def test_fail_assert():
            v = "ZQX-LOCAL-2209"
            assert v == "ZQX-ASSERT-4413"
        @pytest.mark.skip(reason="ZQX-SKIP-1104")
        def test_skip():
            pass
        @pytest.fixture
        def broken():
            raise RuntimeError("ZQX-FIXTURE-6620")
        def test_error(broken):
            pass
    """, returncode=1, results=True)
    results = [r for r in recs if "outcome" in r]
    got = sorted((r["test"], r["outcome"], r["n"]) for r in results)
    t = "tests/test_x.py::"
    assert got == [(t + "test_error", "error", 1), (t + "test_fail", "failed", 1), (t + "test_fail_assert", "failed", 1),
                   (t + "test_ok", "passed", 1), (t + "test_p", "passed", 1), (t + "test_p", "passed", 1),
                   (t + "test_skip", "skipped", 1)]
    assert all(set(r) == {"test", "outcome", "seconds", "n"} | ({"exc"} if r["outcome"] in ("failed", "error") else set())
               and type(r["seconds"]) is float and r["seconds"] >= 0 for r in results)
    assert {r["test"].rsplit("::", 1)[1]: r.get("exc") for r in results if "exc" in r} == {
        "test_fail": "ValueError", "test_fail_assert": "AssertionError", "test_error": "RuntimeError"}
    assert next(r for r in results if r["test"].endswith("test_ok"))["seconds"] >= 0.04  # 各阶段时长之和
    c = one(recs, "check")
    assert (c["exit"], c["exc"], c["test"]) == ("raise", "ValueError", t + "test_fail")
    text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "ZQX" not in text
    # 结果行跟在这个测试的执行记录之后
    assert [r.get("outcome", "fn") for r in recs if r.get("test") == t + "test_fail"] == ["fn", "failed"]
    assert errors(recs) == []


def test_error_while_timing_writes_only_an_error_line_and_tests_still_pass(tmp_path):
    """插件自己出错（这里让时钟抛异常）：写一行 error 后停用，之后不再写执行记录和结果行；被测测试照常通过。"""
    recs = run_traced(tmp_path, {"app/t.py": "def f(x):\n    return x\n"}, """
        from pystepmap import trace
        from app.t import f
        def test_a():
            f(1)
        def test_b():
            trace._CLOCK[0] = lambda: 1 // 0
            f(2)
        def test_c():
            f(3)
    """, results=True)
    assert [(r["test"].rsplit("::", 1)[1], "outcome" in r) for r in recs if "test" in r] == [("test_a", False),
                                                                                            ("test_a", True)]
    assert len(errors(recs)) == 1 and recs[-1]["error"].startswith("ZeroDivisionError: ")


def test_error_in_result_hook_disables_plugin_without_raising(tmp_path):
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    return x\n", "result_mod")
    trace.start(str(graph), str(out))
    try:
        trace.pytest_runtest_logstart("t.py::a", None)
        mod.f(1)
        trace.pytest_runtest_logreport(object())  # 不像 pytest 报告的对象：插件出错，不抛给 pytest
        trace.pytest_runtest_logfinish("t.py::a", None)
        mod.f(2)
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["lines"] for r in recs if "fn" in r] == [[2]] and not any("outcome" in r for r in recs)
    assert recs[-1]["error"].startswith("AttributeError: ")


def test_plugin_module_imports_without_pytest():
    """插件模块不导入 pytest：在没有 pytest 的环境里 import pystepmap.trace 也不出错。"""
    code = "import sys, pystepmap.trace; print('pytest' in sys.modules, '_pytest' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(CORE_SRC)},
                       capture_output=True, text=True, timeout=60, check=True)
    assert r.stdout.split() == ["False", "False"]


# ---------- 评审第 1 轮：参数 id 的各种写法、子测试、插件自己第一次调用的耗时 ----------

def test_test_id_cuts_the_parameter_suffix_after_the_first_double_colon():
    cases = {
        "tests/t.py::test_f[fe80::ZQX-1]": "tests/t.py::test_f",
        "tests/t.py::test_f[::ZQX-2]": "tests/t.py::test_f",
        "tests/t.py::TestC::test_m[pre::ZQX-3]": "tests/t.py::TestC::test_m",
        "tests/t.py::test_f[]ZQX::[]": "tests/t.py::test_f",
        "tests/a[1]/t.py::test_f[x]": "tests/a[1]/t.py::test_f",  # 目录名里的 [ 在第一个 :: 之前，保留
        "tests/t.py::test_plain": "tests/t.py::test_plain",
        "app/m.py::app.m.check": "app/m.py::app.m.check",
    }
    assert {k: trace._test_id(k) for k in cases} == cases


def test_parameter_ids_of_any_shape_leave_no_trace(tmp_path):
    """参数 id 里有 ::、[ ]、空格、非 ASCII 时，记录文件里也搜不到任何参数取值。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    return x\n"}, """
        import pytest
        from app.m import f
        @pytest.mark.parametrize("addr", ["fe80::ZQX-IPV6-1", "::ZQX-IPV6-2", "a]b[ZQX-BR", "空格 ZQX 中文"])
        def test_p(addr):
            assert f(addr) == addr
        @pytest.mark.parametrize("v", [pytest.param(1, id="pre::ZQX-ID-COLON"), pytest.param(2, id="中文 ZQX-ID-CN [x]"),
                                       pytest.param(3, id="]ZQX-ID-BR::[")])
        def test_ids(v):
            f(v)
        @pytest.mark.parametrize("v", ["ZQX-CLS-1"])
        class TestCls:
            @pytest.mark.parametrize("w", ["ZQX-CLS::2"])
            def test_m(self, v, w):
                f(v)
    """, results=True)
    assert "ZQX" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    t = "tests/test_x.py::"
    assert {r["test"] for r in recs if "test" in r} == {t + "test_p", t + "test_ids", t + "TestCls::test_m"}
    assert sorted(r["test"] for r in recs if "outcome" in r) == [t + "TestCls::test_m"] + [t + "test_ids"] * 3 + \
        [t + "test_p"] * 4


def test_subtests_count_toward_the_outcome_but_not_twice_toward_the_time(tmp_path):
    """unittest 的 subTest 和 pytest 的 subtests：有一个子测试失败，这个测试就是 failed（后到的父测试报告不把它改回
    passed）；子测试的时长已经含在 call 阶段里，不再加一遍。子测试的 msg / 参数不进记录。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    return x\n"}, """
        import time
        import unittest
        from app.m import f
        class TestUnit(unittest.TestCase):
            def test_sub(self):
                for i in ("ZQX-USUB-1", "ZQX-USUB-2"):
                    with self.subTest(msg="ZQX-UMSG", i=i):
                        f(i)
                        self.assertTrue(i.endswith("1"), "ZQX-UFAIL")
            def test_sub_ok(self):
                for i in (1, 2):
                    with self.subTest(i=i):
                        time.sleep(0.1)
        def test_fixture(subtests):
            for i in ("ZQX-SUBP-1", "ZQX-SUBP-2"):
                with subtests.test(msg="ZQX-SUBMSG", i=i):
                    assert i.endswith("1"), "ZQX-SUBFAIL"
        def test_fixture_ok(subtests):
            for i in (1, 2):
                with subtests.test(i=i):
                    time.sleep(0.1)
    """, returncode=1, results=True)
    got = {r["test"].rsplit("::", 1)[1]: r for r in recs if "outcome" in r}
    assert {k: v["outcome"] for k, v in got.items()} == {"test_sub": "failed", "test_sub_ok": "passed",
                                                        "test_fixture": "failed", "test_fixture_ok": "passed"}
    assert sum(1 for r in recs if "outcome" in r) == 4
    for name in ("test_sub_ok", "test_fixture_ok"):
        assert 0.19 <= got[name]["seconds"] < 0.35, got[name]  # 两个子测试各 0.1 秒：不加两遍
    assert "ZQX" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_outcome_never_gets_better_within_a_test():
    """一个测试的几份报告按出现顺序合并：call 阶段只会变坏；setup / teardown 出错为 error（已经 failed 的不变）；
    skipped 只在什么都没跑时。"""
    def merged(*reports):
        trace._results.clear()
        for when, outcome, *sub in reports:
            trace._note_result("t", when, outcome, 0.5, bool(sub))
        outcome, seconds, _ = trace._results.pop("t")
        return outcome, seconds

    assert merged(("setup", "passed"), ("call", "failed", "sub"), ("call", "passed"), ("teardown", "passed")) == \
        ("failed", 1.5)
    assert merged(("setup", "passed"), ("call", "passed"), ("teardown", "failed")) == ("error", 1.5)
    assert merged(("setup", "passed"), ("call", "failed"), ("teardown", "failed")) == ("failed", 1.5)
    assert merged(("setup", "failed"), ("teardown", "passed")) == ("error", 1.0)
    assert merged(("setup", "skipped"), ("teardown", "passed")) == ("skipped", 1.0)
    assert merged(("setup", "passed"), ("call", "passed", "sub"), ("call", "skipped"), ("teardown", "passed")) == \
        ("passed", 1.5)
    assert merged(("setup", "passed"), ("call", "rerun"), ("call", "passed"), ("teardown", "passed")) == \
        ("passed", 2.0)


def test_plugin_first_call_work_is_not_counted_in_the_callers_time(tmp_path, monkeypatch):
    """插件第一次见到一个函数时的慢活（找图里的函数、反汇编调用方）不算进正在计时的那一段：时钟换成可控的，让这两样
    各"花"1 毫秒，调用方那一段仍然只有几十纳秒。"""
    mod, graph, out = _in_process(tmp_path, """
        def inner(x):
            return x
        def outer(x):
            y = inner(x)
            return y
        async def step():
            return 1
        async def co():
            x = await step()
            return x
    """, "slow_mod")
    now = [0]

    def tick():
        now[0] += 10
        return now[0]

    def slowly(fn):
        def wrapper(*a):
            now[0] += 1_000_000
            return fn(*a)
        return wrapper

    monkeypatch.setattr(trace, "_CLOCK", [tick])
    monkeypatch.setattr(trace, "_match", slowly(trace._match))
    monkeypatch.setattr(trace, "_existing_await_offsets", slowly(trace._existing_await_offsets))
    trace.start(str(graph), str(out))
    try:
        for name in ("t.py::first", "t.py::again"):
            trace.pytest_runtest_logstart(name, None)
            mod.outer(1)
            asyncio.run(mod.co())
            trace.pytest_runtest_logfinish(name, None)
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if '"fn"' in line]
    for q in ("outer", "co"):
        first, again = (r["ns"] for r in recs if r["fn"].split("::")[1].startswith(q))
        assert max(first) < 1_000 and max(again) < 1_000, (q, first, again)


# ---------- S12：失败的测试记下让它失败的异常的类型名 ----------

def test_failed_tests_carry_only_the_exception_type_name(tmp_path):
    """失败 / 出错的测试的结果行带 exc：让它失败的那个异常的类型名（setup / teardown 出错时是那里抛的；几个阶段都失败、
    或有子测试时取第一个失败的）。通过、跳过、xfail 的没有 exc。信息、repr 都不记：标记串在文件里搜不到；动态创建的
    异常类的名字是类型名，可以出现。"""
    recs = run_traced(tmp_path, {"app/m.py": """
        def check(x):
            if x == "boom":
                raise ValueError("ZQX-VAL " + x)
            return x
    """}, """
        import unittest
        import pytest
        from app.m import check
        def test_pass():
            check("ok")
        def test_assert():
            assert check("ok") == "ZQX-ASSERT", "ZQX-MSG"
        def test_value():
            check("boom")
        def test_dyn():
            raise type("DynErr", (Exception,), {})("ZQX-DYN")
        def test_fail_call():
            pytest.fail("ZQX-FAIL")
        @pytest.fixture
        def bad_setup():
            raise RuntimeError("ZQX-SETUP")
        def test_setup_error(bad_setup):
            pass
        @pytest.fixture
        def bad_teardown():
            yield
            raise KeyError("ZQX-TEARDOWN")
        def test_teardown_error(bad_teardown):
            pass
        def test_fail_then_teardown(bad_teardown):
            assert False, "ZQX-BOTH"
        def test_skip():
            pytest.skip("ZQX-SKIP")
        @pytest.mark.xfail(reason="ZQX-XFAIL")
        def test_xfail():
            raise ZeroDivisionError("ZQX-XF")
        class TestUnit(unittest.TestCase):
            def test_sub(self):
                for i in (1, 2):
                    with self.subTest(i="ZQX-SUBP"):
                        if i == 2:
                            raise LookupError("ZQX-SUB")
        def test_subtests(subtests):
            with subtests.test(msg="ZQX-SUBMSG"):
                raise IndexError("ZQX-SUBIDX")
    """, returncode=1, results=True)
    got = {r["test"].rsplit("::", 1)[1]: (r["outcome"], r.get("exc")) for r in recs if "outcome" in r}
    assert got == {
        "test_pass": ("passed", None), "test_assert": ("failed", "AssertionError"),
        "test_value": ("failed", "ValueError"), "test_dyn": ("failed", "DynErr"), "test_fail_call": ("failed", "Failed"),
        "test_setup_error": ("error", "RuntimeError"), "test_teardown_error": ("error", "KeyError"),
        "test_fail_then_teardown": ("failed", "AssertionError"), "test_skip": ("skipped", None),
        "test_xfail": ("skipped", None), "test_sub": ("failed", "LookupError"), "test_subtests": ("failed", "IndexError"),
    }
    assert all(set(r) <= {"test", "outcome", "seconds", "n", "exc"} for r in recs if "outcome" in r)
    assert "ZQX" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_reading_the_exception_type_never_changes_the_report_or_raises(tmp_path):
    """makereport 钩子只读 call.excinfo 的类型、总是返回 None（不替 pytest 生成报告）；读不了时插件停用、不抛给 pytest。"""
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    return x\n", "exc_mod")

    class Call:
        def __init__(self, exc):
            self.when, self.excinfo = "call", exc

    class Info:
        def __init__(self, e):
            self.value = e

    class Item:
        nodeid = "t.py::a"

    trace.start(str(graph), str(out))
    try:
        trace.pytest_runtest_logstart("t.py::a", None)
        mod.f(1)
        assert trace.pytest_runtest_makereport(Item(), Call(Info(ValueError("ZQX")))) is None
        assert trace.pytest_runtest_makereport(Item(), Call(None)) is None
        assert trace.pytest_runtest_makereport(Item(), object()) is None  # 不像 CallInfo：停用，不抛
        trace.pytest_runtest_logfinish("t.py::a", None)
    finally:
        trace.stop()
    text = out.read_text(encoding="utf-8")
    assert "ZQX" not in text and json.loads(text.splitlines()[-1])["error"].startswith("AttributeError: ")


# ---------- S14：经项目以外的帧（库的包装函数、contextmanager、回调）调用的间接调用 icalls ----------

# 代替第三方库（DBOS.step、tenacity.retry、回调注册表……）：放在 tests/ 下，不进图，所以它的帧不是目标函数
FAKELIB = """
    import functools

    def lib_step(f):
        @functools.wraps(f)
        def wrapper(*a, **k):
            return f(*a, **k)
        return wrapper

    def lib_async_step(f):
        @functools.wraps(f)
        async def wrapper(*a, **k):
            return await f(*a, **k)
        return wrapper

    def call_back(fn, x):
        return fn(x)

    def deep(fn, x, depth):
        if depth:
            return deep(fn, x, depth - 1)
        return fn(x)
"""

INDIRECT = {"tests/fakelib.py": FAKELIB, "app/__init__.py": "", "app/w.py": """
    import asyncio
    import threading
    from contextlib import contextmanager

    from fakelib import call_back, deep, lib_async_step, lib_step

    @lib_step
    def load(x):
        return x

    @lib_async_step
    async def aload(x):
        return x

    @contextmanager
    def tx():
        yield 1

    def cb(x):
        return x

    def far(x):
        return x

    def near(x):
        return x

    def plain(x):
        return x

    def flow(x):
        a = load(x)
        with tx():
            b = call_back(cb, a)
        c = plain(b)
        d = deep(near, c, 10)
        return deep(far, d, 70)

    async def aflow(x):
        return await aload(x)

    async def child(x):
        return x

    async def gathered(x):
        return await asyncio.gather(child(x), child(x + 1))

    def worker(x):
        return x

    def threaded(x):
        t = threading.Thread(target=worker, args=(x,))
        t.start()
        t.join()
        return x

    def numbers(n):
        yield from range(n)

    def consume(n):
        total = 0
        for v in numbers(n):
            total += v
        return total
"""}


def test_calls_through_non_project_frames_are_recorded_as_indirect_calls(tmp_path):
    """被调函数的直接调用方不是目标函数（库的包装函数、contextlib、回调）时，往上找最近的、正在执行的目标函数，记一条
    间接调用 icalls [被调函数 key, 那一帧所在的行]；calls 照旧只有直接调用。往上最多找 64 帧。"""
    recs = run_traced(tmp_path, INDIRECT, """
        import asyncio
        from app import w
        def test_a():
            assert w.flow(1) == 1
            assert asyncio.run(w.aflow(2)) == 2
            assert asyncio.run(w.gathered(3)) == [3, 4]
            assert w.threaded(4) == 4
            assert w.consume(3) == 3
            assert w.plain(5) == 5
    """)
    key = {q: f"app/w.py::{q}@{ln}" for q, ln in (("load", 8), ("aload", 12), ("tx", 16), ("cb", 19), ("far", 22),
                                                   ("near", 25), ("plain", 28))}
    flow = one(recs, "flow")
    assert flow["calls"] == [[key["plain"], 35]]  # 直接调用照旧；经包装、contextlib、回调的不在 calls 里
    # 经包装函数、contextmanager、回调、10 层库帧的都记成间接调用；隔了 70 层库帧的找不到（只往上找 64 帧）
    assert flow["icalls"] == [[key["load"], 32], [key["tx"], 33], [key["cb"], 34], [key["near"], 36]]
    assert one(recs, "aflow")["icalls"] == [[key["aload"], 40]] and one(recs, "aflow")["calls"] == []
    # 事件循环调度的任务、别的线程、生成器：栈上没有调用它的目标函数，什么都不记
    for q in ("gathered", "threaded", "consume"):
        r = one(recs, q)
        assert r["calls"] == [] and "icalls" not in r, q
    # 测试直接调用的：没有任何人记它
    assert all(key["plain"] not in json.dumps(r.get("icalls", [])) for r in recs if "fn" in r)
    assert errors(recs) == []


def test_indirect_calls_do_not_change_verify(tmp_path):
    """核对只看 calls：带 icalls 的记录和去掉 icalls 的记录核对出来的报告一样。"""
    from pystepmap.verify import verify

    recs = run_traced(tmp_path, INDIRECT, "from app import w\ndef test_a():\n    assert w.flow(1) == 1\n")
    assert any(r.get("icalls") for r in recs)
    graph = analyze_project(tmp_path / "proj")
    stripped = [{k: v for k, v in r.items() if k != "icalls"} for r in recs]
    assert verify(graph, recs) == verify(graph, stripped)


# ---------- 进度行（运行中看进度用）：收集完的用例数、每个测试开始 ----------

PROGRESS_TESTS = """
    import pytest
    from app.m import f
    def test_a():
        assert f(1) == 1
    @pytest.mark.parametrize("secret", ["ZQX-PARAM-1", "ZQX-PARAM-2"])
    def test_p(secret):
        assert f(secret) == secret
    def test_b():
        assert f(2) == 2
"""


def test_progress_lines_say_how_many_were_collected_and_when_each_test_starts(tmp_path):
    """收集完写一行 {"collected": 用例数}；每个测试（参数化的每个用例）开始时写一行 {"start": 测试 id}，同样去掉参数后缀，
    写在这个测试的执行记录和结果行之前。两种行只有这一个字段：没有 fn、test、outcome、error。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    return x\n"}, PROGRESS_TESTS, results=True, progress=True)
    t = "tests/test_x.py::"
    assert recs[0] == {"collected": 4}
    kinds = [("start", r["start"]) if "start" in r else ("result", r["test"]) if "outcome" in r
             else ("fn", r["test"]) for r in recs[1:]]
    assert kinds == [("start", t + "test_a"), ("fn", t + "test_a"), ("result", t + "test_a"),
                     ("start", t + "test_p"), ("fn", t + "test_p"), ("result", t + "test_p"),
                     ("start", t + "test_p"), ("fn", t + "test_p"), ("result", t + "test_p"),
                     ("start", t + "test_b"), ("fn", t + "test_b"), ("result", t + "test_b")]
    assert all(set(r) in ({"collected"}, {"start"}) for r in recs if "start" in r or "collected" in r)
    assert "ZQX" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert errors(recs) == []


def test_deselected_tests_are_not_counted_as_collected(tmp_path):
    """收集完之后被 -k / deselect 去掉的不算：pytest_collection_finish 在挑选之后。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    return x\n"}, PROGRESS_TESTS, progress=True,
                      env_extra={"PYTEST_ADDOPTS": "-k test_p"})
    assert [r for r in recs if "collected" in r] == [{"collected": 2}]
    assert [r["start"] for r in recs if "start" in r] == ["tests/test_x.py::test_p"] * 2


def test_no_progress_lines_after_the_plugin_disabled_itself(tmp_path):
    """插件自己出错停用之后不再写开始行（同执行记录、结果行）；被测测试照常通过。"""
    recs = run_traced(tmp_path, {"app/t.py": "def f(x):\n    return x\n"}, """
        from pystepmap import trace
        from app.t import f
        def test_a():
            f(1)
        def test_b():
            trace._CLOCK[0] = lambda: 1 // 0
            f(2)
        def test_c():
            f(3)
    """, results=True, progress=True)
    assert [r["start"].rsplit("::", 1)[1] for r in recs if "start" in r] == ["test_a", "test_b"]
    assert len(errors(recs)) == 1


class _FakePlugins:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def has_plugin(self, name: str) -> bool:
        return name in self.names


class _FakeConfig:
    def __init__(self, names: set[str], worker: bool = False) -> None:
        self.pluginmanager = _FakePlugins(names)
        if worker:
            self.workerinput = {"workerid": "gw0"}


class _FakeSession:
    def __init__(self, config: _FakeConfig, items: int = 0) -> None:
        self.config = config
        self.items = [object()] * items


def _hooks_in_process(tmp_path, monkeypatch, config: _FakeConfig) -> list[dict]:
    """本进程里按 pytest 的顺序调用插件的钩子（configure、sessionstart、收集完、一个测试开始和结束），返回记录。"""
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    return x\n", "progress_mod")
    monkeypatch.setenv("PYSTEPMAP_GRAPH", str(graph))
    monkeypatch.setenv("PYSTEPMAP_TRACE_OUT", str(out))
    monkeypatch.delattr(sys, trace._ACTIVE, raising=False)
    trace.pytest_configure(config)
    try:
        trace.pytest_sessionstart(_FakeSession(config))
        trace.pytest_collection_finish(_FakeSession(config, 3))
        trace.pytest_runtest_logstart("t.py::a[1]", None)
        mod.f(1)
        trace.pytest_runtest_logfinish("t.py::a[1]", None)
    finally:
        trace.pytest_unconfigure(config)
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


def test_the_xdist_controller_writes_no_progress_lines(tmp_path, monkeypatch):
    """pytest-xdist 的主进程：工作进程的 logstart 转到它这里（它不收集、不跑测试），开始行由工作进程自己写——主进程再写
    一行就成了两个"开始"、一个结束。工作进程（有 workerinput）、没有 xdist 的运行照写。"""
    controller = _hooks_in_process(tmp_path / "c", monkeypatch, _FakeConfig({"dsession"}))
    assert not any("start" in r or "collected" in r for r in controller)
    worker = _hooks_in_process(tmp_path / "w", monkeypatch, _FakeConfig({"dsession"}, worker=True))
    plain = _hooks_in_process(tmp_path / "p", monkeypatch, _FakeConfig(set()))
    for recs in (worker, plain):
        assert [r for r in recs if "start" in r or "collected" in r] == [{"collected": 3}, {"start": "t.py::a"}]
        assert [r["fn"].rsplit(":", 1)[0] for r in recs if "fn" in r] and not errors(recs)


def test_an_error_in_a_progress_hook_disables_the_plugin_without_raising(tmp_path, monkeypatch):
    """收集完的钩子里出错（session 不像 pytest 的）：写一行 error 后停用，不抛给 pytest。"""
    mod, graph, out = _in_process(tmp_path, "def f(x):\n    return x\n", "progress_err_mod")
    trace.start(str(graph), str(out))
    try:
        trace.pytest_collection_finish(object())
        trace.pytest_runtest_logstart("t.py::a", None)
        mod.f(1)
    finally:
        trace.stop()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r for r in recs if "format" not in r] == [recs[-1]] and recs[-1]["error"].startswith("AttributeError: ")


def test_readers_ignore_progress_lines(tmp_path, capsys):
    """pystepmap verify、runpath.build_run_paths 都不看进度行：有没有它们，核对的报告、命令行的输出、运行路径一模一样。"""
    from pystepmap.cli import main
    from pystepmap.runpath import build_run_paths, run_paths_to_json
    from pystepmap.verify import read_records, verify

    run_traced(tmp_path, {"app/m.py": "def f(x):\n    if x:\n        return x\n    raise ValueError('no')\n"}, """
        import pytest
        from app.m import f
        @pytest.mark.parametrize("x", [1, 2])
        def test_p(x):
            assert f(x) == x
        def test_fail():
            f(0)
    """, returncode=1)
    with_progress = tmp_path / "trace.jsonl"
    text = with_progress.read_text(encoding="utf-8")
    assert '"start"' in text and '"collected"' in text
    without = tmp_path / "plain.jsonl"
    without.write_text("".join(line for line in text.splitlines(keepends=True)
                               if not is_progress(json.loads(line))), encoding="utf-8")
    graph = analyze_project(tmp_path / "proj")
    graph_file = tmp_path / "graph.json"
    assert verify(graph, read_records(with_progress)) == verify(graph, read_records(without))
    assert run_paths_to_json(build_run_paths(graph, read_records(with_progress))) == \
        run_paths_to_json(build_run_paths(graph, read_records(without)))
    outputs = []
    for path in (with_progress, without):
        rc = main(["verify", str(graph_file), str(path)])
        outputs.append((rc, capsys.readouterr().out))
    assert outputs[0] == outputs[1] and outputs[0][1]


def test_only_tests_runs_just_the_listed_tests_and_fails_closed(tmp_path):
    """PYSTEPMAP_ONLY_TESTS 指着的名单：只跑名单里的（参数化的几个用例一起），别的算取消选择；名单里找不到的不出错，
    {"only", "kept"} 说了个数；名单读不了时一个测试都不跑（退出码 4）；记录没启用时照样挑。"""
    files = {"app/m.py": "def f(x):\n    return x\n"}
    t = "tests/test_x.py::"
    only = tmp_path / "only.json"
    only.write_text(json.dumps([t + "test_p", t + "test_b", t + "test_gone"]), encoding="utf-8")
    out: list[str] = []
    recs = run_traced(tmp_path / "a", files, PROGRESS_TESTS, results=True, progress=True, output=out,
                      env_extra={"PYSTEPMAP_ONLY_TESTS": str(only)})
    assert recs[0] == {"only": 3, "kept": 2} and recs[1] == {"collected": 3}
    assert [r["test"] for r in recs if "outcome" in r] == [t + "test_p", t + "test_p", t + "test_b"]
    assert "3 passed, 1 deselected" in out[0]
    # 记录没启用：照样挑
    out = []
    assert run_traced(tmp_path / "b", files, PROGRESS_TESTS, active=False, output=out,
                      env_extra={"PYSTEPMAP_ONLY_TESTS": str(only)}) == []
    assert "3 passed, 1 deselected" in out[0]
    # 名单读不了、不是字符串的列表：不跑
    for i, text in enumerate(("{坏的", json.dumps({"a": 1}), json.dumps(["x", 3]))):
        only.write_text(text, encoding="utf-8")
        out = []
        recs = run_traced(tmp_path / f"c{i}", files, PROGRESS_TESTS, results=True, output=out, returncode=4,
                          env_extra={"PYSTEPMAP_ONLY_TESTS": str(only)})
        assert [r for r in recs if "outcome" in r] == [] and "passed" not in out[0]
    out = []
    run_traced(tmp_path / "d", files, PROGRESS_TESTS, output=out, returncode=4,
               env_extra={"PYSTEPMAP_ONLY_TESTS": str(tmp_path / "nope.json")})
    # 名单里一个都找不到：pytest 的"没有收集到测试"（退出码 5）
    only.write_text(json.dumps([t + "test_gone"]), encoding="utf-8")
    recs = run_traced(tmp_path / "e", files, PROGRESS_TESTS, results=True, progress=True, returncode=5,
                      env_extra={"PYSTEPMAP_ONLY_TESTS": str(only)})
    assert {"only": 1, "kept": 0} in recs and not [r for r in recs if "outcome" in r]
