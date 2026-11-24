"""执行路径核对：真实记录（子进程里跑 pytest 插件）和手工构造的记录放到图上逐步走，检查报告。"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from conftest import make_project, one, run_traced
from pystepmap.analyze import analyze_project
from pystepmap.cli import main
from pystepmap.config import ProjectConfig
from pystepmap.index import build_index
from pystepmap.model import from_json, parse_key, to_json
from pystepmap.verify import (
    DynamicCall,
    ImplicitCall,
    RecordError,
    StaleGraphError,
    Walked,
    read_records,
    verify,
    walk_record,
)

# ---------- 辅助 ----------

def traced(tmp_path, files, test_src):
    """真实记录：建临时项目、分析、带插件跑测试，返回 (图, 记录)。"""
    recs = run_traced(tmp_path, files, test_src)
    return from_json((tmp_path / "graph.json").read_text(encoding="utf-8")), recs


def project(tmp_path, src):
    """手工记录用的项目：src 去缩进后写成 app/mod.py（保留开头的空行，行号与说明一致），返回 (图, 源码)。"""
    text = textwrap.dedent(src)
    make_project(tmp_path, {"app/mod.py": text})
    return analyze_project(tmp_path), text


def line(text, needle, nth=1):
    """源码里第 nth 个（去掉缩进后）以 needle 开头的行的行号（从 1 起）。"""
    return [i for i, row in enumerate(text.splitlines(), 1) if row.strip().startswith(needle)][nth - 1]


def key(graph, qualname):
    [k] = [k for k, f in graph.functions.items() if f.qualname == qualname]
    return k


def rec(graph, qualname, lines, exit="return", calls=(), n=1, **extra):
    """手工记录；calls 是 (被调函数 key, 调用方当时所在的行) 的序列。"""
    return {"fn": key(graph, qualname), "test": "tests/t.py::t", "lines": list(lines), "exit": exit,
            "exc": None if exit == "return" else "ValueError", "calls": [list(c) for c in calls], "n": n, **extra}


def kinds(report):
    return sorted((m.kind, m.from_line, m.to_line) for m in report.mismatches)


def write_inputs(tmp_path, graph, records):
    g, t = tmp_path / "g.json", tmp_path / "t.jsonl"
    g.write_text(to_json(graph), encoding="utf-8")
    t.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return str(g), str(t)


# ---------- 任务 7 的两个示例：真实路径都在图上 ----------

CONTROL = {"app/t.py": """
    import logging
    logger = logging.getLogger(__name__)
    def work(x):
        if x == "value":
            raise ValueError(x)
        if x == "key":
            raise KeyError(x)
        if x == "type":
            raise TypeError(x)
    def recover(e): ...
    def release(): ...
    def wf(x):
        try:
            work(x)
            return "ok"
        except ValueError as e:
            recover(e)
            return "failed"
        except KeyError:
            logger.warning("ignored")
        finally:
            release()
    def wait(i):
        return "start" if i < 2 else "stop"
    def start(i): ...
    def done(): ...
    def lp(n):
        for i in range(n):
            w = wait(i)
            if w != "start":
                break
            if w == "skip":
                continue
            start(i)
        done()
"""}


def test_real_paths_all_on_graph(tmp_path):
    graph, recs = traced(tmp_path, CONTROL, """
        import pytest
        from app.t import lp, wf
        def test_ok():
            assert wf("ok") == "ok"
        def test_value():
            assert wf("value") == "failed"
        def test_key():
            assert wf("key") is None
        def test_type():
            with pytest.raises(TypeError):
                wf("type")
        def test_loop():
            lp(0)
            lp(1)
            lp(4)
    """)
    report = verify(graph, recs)
    assert report.ok, report.mismatches
    assert report.steps_total == report.steps_on_graph + report.approx_entries
    assert report.steps_total > 20
    assert report.uncaught_exits == 1  # wf("type")：TypeError 没被捕获，经过 finally 离开
    assert (report.functions_in_graph, report.functions_seen) == (8, 8)
    assert report.distinct_paths >= 10 and report.plugin_errors == 0 and report.unknown_functions == 0


def test_functions_run_in_another_thread(tmp_path):
    """asyncio.to_thread / run_in_executor 执行的函数在线程池的线程里开始：它的帧的上一帧是线程池的代码，记录插件不把它
    记成调用方的调用（它自己的记录照常）。图上调用方有这次调用（call_targets），核对不因此报任何东西。"""
    graph, recs = traced(tmp_path, {"app/t.py": """
        import asyncio
        def load(x):
            return helper(x)
        def helper(x):
            return x + 1
        async def h(x):
            a = await asyncio.to_thread(load, x)
            return await asyncio.get_running_loop().run_in_executor(None, load, a)
    """}, """
        import asyncio
        from app.t import h
        def test_a():
            assert asyncio.run(h(1)) == 3
    """)
    hk, lk, helper = key(graph, "h"), key(graph, "load"), key(graph, "helper")
    assert graph.functions[hk].call_targets == [lk]
    assert one(recs, "h")["calls"] == []
    loads = [r for r in recs if r.get("fn") == lk]
    assert sum(r["n"] for r in loads) == 2 and all(c[0] == helper for r in loads for c in r["calls"])
    report = verify(graph, recs)
    assert report.ok and report.functions_seen == 3 and report.dynamic_calls == 0


def test_multiline_statements_map_to_statement_start(tmp_path):  # Review Focus 3
    src = '''
def a(): ...
def b(x, y): ...
def h(p):
    a()
    if (
        p.x
        and p.y
    ):
        b(
            1,
            2,
        )
    return 1
'''
    graph, recs = traced(tmp_path, {"app/m.py": src}, """
        from types import SimpleNamespace
        from app.m import h
        def test_a():
            assert h(SimpleNamespace(x=1, y=1)) == 1
    """)
    text = src.lstrip("\n")
    h = one(recs, "h")
    # 多行语句只在后面的行上产生事件：if 的条件行、b(...) 的参数行
    assert line(text, "p.x") in h["lines"] and line(text, "if (") not in h["lines"]
    report = verify(graph, recs)
    assert report.ok, report.mismatches
    h_report = verify(graph, [h])
    assert h_report.steps_total == 3 and h_report.steps_on_graph == 3  # a、if、b；后面的 return 算在结局里


# ---------- 不符 ----------

MISSING = '''
def a(): ...
def b(): ...
def h(p):
    a()
    if p:
        b()
        b()
        b()
        b()
        b()
        return 2
    b()
    return 1
def c(): ...
'''


def test_missing_edge_is_reported(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    r = verify(graph, [rec(graph, "h", [5, 13, 14])])
    assert not r.ok and [(m.kind, m.from_line, m.to_line) for m in r.mismatches] == [("edge", 5, 13)]
    assert r.mismatches[0].fn == key(graph, "h") and r.mismatches[0].count == 1
    assert (r.steps_total, r.steps_on_graph) == (2, 1)  # a 在图上，b 不在；return 沿连线到达，算在结局里


def test_missing_end_and_missing_call_are_reported(tmp_path):
    graph, text = project(tmp_path, MISSING)
    # 以 return 离开，但停在普通步骤 a，a 后面不是函数末尾；在第 5 行调用了图上没有的 c
    r = verify(graph, [rec(graph, "h", [5], calls=[(key(graph, "c"), 5)])])
    assert not r.ok
    assert kinds(r) == [("call", 5, line(text, "def c")), ("end", 5, 0)]
    call = next(m for m in r.mismatches if m.kind == "call")
    assert call.detail == key(graph, "c") and r.calls_total == 1


def test_uncaught_exit_is_not_a_mismatch(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    r = verify(graph, [rec(graph, "h", [5], exit="raise")])
    assert r.ok and r.uncaught_exits == 1
    # 停在 raise 语句生成的结局上不算未捕获
    graph2, _ = project(tmp_path / "p2", "def f(x):\n    if x:\n        raise ValueError(x)\n    return 1\n")
    r2 = verify(graph2, [rec(graph2, "f", [2, 3], exit="raise")])
    assert r2.ok and r2.uncaught_exits == 0


# ---------- 裁决 1：按 n 计重 ----------

def test_counts_are_weighted_by_n(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    good = [rec(graph, "h", [5, 6, 13, 14], n=3), rec(graph, "h", [5, 6, 13, 14], n=2) | {"test": "t::other"}]
    r = verify(graph, good + [rec(graph, "h", [5, 13, 14], n=4), rec(graph, "h", [5], exit="raise", n=7)])
    assert r.distinct_paths == 3  # 两条相同的路径只算一条
    assert r.steps_total == 5 * 3 + 4 * 2 + 7 * 1
    assert r.uncaught_exits == 7
    [m] = r.mismatches
    assert (m.kind, m.from_line, m.to_line, m.count) == ("edge", 5, 13, 4)


# ---------- 裁决 2：截断的记录 ----------

def test_truncated_record_checks_prefix_but_not_outcome(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    r = verify(graph, [rec(graph, "h", [5, 6], truncated=True, n=2)])
    assert r.ok and r.truncated_records == 2 and r.uncaught_exits == 0
    r2 = verify(graph, [rec(graph, "h", [5, 13], exit="raise", truncated=True)])
    assert kinds(r2) == [("edge", 5, 13)] and r2.uncaught_exits == 0


# ---------- 异常与收尾的近似（裁决 G1–G5）----------

APPROX = {"app/x.py": """
    def a(): ...
    def b(x):
        if x == "boom":
            raise ValueError(x)
    def c(): ...
    def d(): ...
    def handled(x):
        try:
            a()
            b(x)
        except ValueError:
            c()
        d()
    def fin_break(xs):
        for x in xs:
            try:
                a()
                b(x)
                break
            finally:
                c()
        d()
"""}


def test_approximate_entries_are_located_and_break_through_finally_is_on_graph(tmp_path):
    graph, recs = traced(tmp_path, APPROX, """
        from app.x import fin_break, handled
        def test_a():
            handled("ok")
            handled("boom")
            fin_break(["ok"])
    """)
    text = textwrap.dedent(APPROX["app/x.py"]).lstrip("\n")
    r = verify(graph, recs)
    # handled("boom")：b 不是 try 体入口，b → 捕获节点没有连线 → 入口近似（try 体 → 捕获节点）；
    # fin_break：break 经过 finally 的 c 再到循环外的 d，b → c → d 都有连线
    assert r.ok, r.mismatches
    assert r.approx_entries == 1
    assert r.steps_total == r.steps_on_graph + r.approx_entries
    assert [(a.fn, a.from_line, a.to_line, a.kind, a.count) for a in r.approx_list] == [
        (key(graph, "handled"), line(text, "b(x)"), line(text, "except ValueError"), "entry", 1)]


def test_real_mismatches_are_not_forgiven(tmp_path):
    text = textwrap.dedent(APPROX["app/x.py"]).lstrip("\n")
    graph = analyze_project(make_project(tmp_path, {"app/x.py": text}))
    a, d = line(text, "a()"), line(text, "d()")  # handled 里的
    loop = line(text, "for x in xs")
    a2, b2, c2, d2 = (line(text, s, 2) for s in ("a()", "b(x)", "c()", "d()"))  # fin_break 里的
    # b → c → d 是 break 经过 finally 的连线；之后 d → a 的起点既不在 try 里也不在 finally 里：不符
    r = verify(graph, [rec(graph, "fin_break", [loop, a2, b2, c2, d2, a2], exit="raise")])
    assert kinds(r) == [("edge", d2, a2)] and r.approx_entries == 0
    # a 在 try 体里，但 d 不是这个 try 的落脚点（它画了捕获节点、没有 finally）：不符
    r2 = verify(graph, [rec(graph, "handled", [a, d])])
    assert kinds(r2) == [("edge", a, d)] and r2.approx_entries == 0


RETRY = {"app/rt.py": """
    def a(x): ...
    def b(x): ...
    def c(): ...
    def d(x): ...
    def e(): ...
    def retry(xs):
        for x in xs:
            try:
                v = int(x)
                break
            except ValueError:
                a(x)
            finally:
                c()
            d(x)
        e()
    def skip(xs):
        for x in xs:
            try:
                v = int(x)
                continue
            except ValueError:
                a(x)
            finally:
                c()
            d(x)
        e()
    def skip_else(xs):
        for x in xs:
            a(x)
            try:
                v = int(x)
            except ValueError:
                b(x)
            else:
                continue
            finally:
                c()
            d(x)
        e()
"""}


def test_jump_before_any_node_in_try_goes_through_finally(tmp_path):
    """重试循环：try 里跳转之前没有带节点的语句。break / continue 经过 finally 再到目标的真实路径都在图上，
    不需要任何近似；删掉或改道"finally 出口 → 跳转目标"的连线，真实记录要报出来。"""
    graph, recs = traced(tmp_path, RETRY, """
        from app.rt import retry, skip, skip_else
        def test_a():
            retry(["x", "1"])
            retry(["1"])
            skip(["1", "x"])
            skip_else(["1", "x"])
    """)
    t = textwrap.dedent(RETRY["app/rt.py"]).lstrip("\n")
    rows = t.splitlines()

    def at(fn, needle):
        start = rows.index(f"def {fn}(xs):")
        return next(i + 1 for i in range(start, len(rows)) if rows[i].strip().startswith(needle))

    def only(qualname):
        return [x for x in recs if x.get("fn") == key(graph, qualname)]

    def fresh():
        return from_json(to_json(graph))

    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert (r.approx_entries, r.approx_list) == (0, [])
    c, e, d = at("retry", "c()"), at("retry", "e()"), at("retry", "d(x)")
    # break 经过 finally 之后去 e()：这条连线漏画、或画到了 d(x) 上
    assert kinds(verify(without_edge(fresh(), "retry", c, e), only("retry"))) == [("edge", c, e)]
    assert kinds(verify(rerouted(fresh(), "retry", c, e, d), only("retry"))) == [("edge", c, e)]
    # continue 经过 finally 之后回到循环节点：这条连线漏画
    for fn in ("skip", "skip_else"):
        c, loop = at(fn, "c()"), at(fn, "for x")
        assert kinds(verify(without_edge(fresh(), fn, c, loop), only(fn))) == [("edge", c, loop)], fn


REVIEW = {"app/r.py": """
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def boom(x):
        if x:
            raise ValueError(x)
    def b(): ...
    def c(): ...
    def d(): ...
    def e(): ...
    def g(x):
        try:
            a()
            boom(x)
        except ValueError:
            c()
        if x:
            d()
    def h(n):
        try:
            a()
            e()
        except ValueError:
            logger.warning("x")
        while n > 0:
            n -= 1
            b()
        d()
"""}
REVIEW_TEXT = textwrap.dedent(REVIEW["app/r.py"]).lstrip("\n")


def without_edge(graph, qualname, src_line, dst_line):
    """模拟建图缺陷：去掉函数图里一条连线。"""
    fg = graph.functions[key(graph, qualname)]
    line_of = {n.id: n.line for n in fg.nodes}
    kept = [e for e in fg.edges if (line_of[e.src], line_of[e.dst]) != (src_line, dst_line)]
    assert len(kept) == len(fg.edges) - 1
    fg.edges = kept
    return graph


def test_caught_exception_does_not_excuse_a_missing_edge_further_down(tmp_path):
    """评审的第一个场景：捕获过一次异常之后，后面真正缺的连线必须报出来。"""
    graph, recs = traced(tmp_path, REVIEW, "from app.r import g\ndef test_a():\n    g(1)\n")
    t = REVIEW_TEXT
    a, boom, catch, c, d = (line(t, s) for s in ("a()", "boom(x)", "except ValueError", "c()", "d()"))
    cond = line(t, "if x:", 2)  # g 里的 if（第一个 if x: 在 boom 里）
    r = verify(graph, recs)
    assert r.ok and r.approx_entries == 1  # boom → except：try 体 → 捕获节点
    broken = verify(without_edge(graph, "g", cond, d), recs)
    assert kinds(broken) == [("edge", cond, d)] and broken.approx_entries == 1
    # 手写记录（原图）：从 except 体里的 c 跳过 if 直接到 d
    fresh = from_json((tmp_path / "graph.json").read_text(encoding="utf-8"))
    hand = verify(fresh, [rec(fresh, "g", [a, boom, catch, c, d])])
    assert kinds(hand) == [("edge", c, d)] and hand.approx_entries == 1


def test_log_only_except_landing_does_not_excuse_unrelated_transitions(tmp_path):
    """评审的第二个场景：while 是只写日志的 except 的落脚点，但从循环体回到 while 的缺连线与这个 try 无关。"""
    graph, recs = traced(tmp_path, REVIEW, "from app.r import h\ndef test_a():\n    h(2)\n")
    t = REVIEW_TEXT
    e, loop, b = (line(t, s) for s in ("e()", "while n > 0", "b()"))
    a, d = line(t, "a()", 2), line(t, "d()", 2)  # h 里的（第一个 a()、d() 在 g 里）
    r = verify(graph, recs)
    assert r.ok and r.approx_entries == 0  # 没有抛异常
    # 手写记录（原图）：循环结束、走到 d 之后又回到 while
    hand = verify(graph, [rec(graph, "h", [a, e, loop, b, loop, d, loop], exit="raise")])
    assert kinds(hand) == [("edge", d, loop)] and hand.approx_entries == 0
    broken = verify(without_edge(graph, "h", b, loop), recs)  # 去掉 b → while 的 loop_back
    assert kinds(broken) == [("edge", b, loop)] and broken.mismatches[0].count == 2
    assert broken.approx_entries == 0


SOURCES = {"app/s.py": """
    def a(): ...
    def boom(x):
        if x:
            raise ValueError(x)
    def c(): ...
    def d(): ...
    def from_handler(x):
        try:
            a()
            boom(x)
        except ValueError:
            boom(x)
            c()
        finally:
            d()
    def from_else(x):
        try:
            a()
        except KeyError:
            c()
        else:
            boom(x)
            c()
        finally:
            d()
    def jumps(xs):
        for x in xs:
            try:
                a()
                if x == 1:
                    continue
                if x == 2:
                    break
                a()
            finally:
                c()
            d()
        return a()
    def ret(xs):
        for x in xs:
            try:
                if x:
                    return a()
            finally:
                c()
        d()
"""}


def test_each_allowed_entry_source_and_jumps_through_finally(tmp_path):
    graph, recs = traced(tmp_path, SOURCES, """
        import pytest
        from app.s import from_else, from_handler, jumps, ret
        def test_a():
            with pytest.raises(ValueError):
                from_handler(1)
            with pytest.raises(ValueError):
                from_else(1)
            jumps([1, 2])
            ret([1])
    """)
    t = textwrap.dedent(SOURCES["app/s.py"]).lstrip("\n")
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    got = sorted((parse_key(a.fn)[1], a.from_line, a.to_line, a.kind) for a in r.approx_list)
    assert got == sorted([
        ("from_handler", line(t, "boom(x)"), line(t, "except ValueError"), "entry"),  # try 体 → 捕获节点
        ("from_handler", line(t, "boom(x)", 2), line(t, "d()"), "entry"),  # except 体 → finally 入口
        ("from_else", line(t, "boom(x)", 3), line(t, "d()", 2), "entry"),  # else 体 → finally 入口
    ])  # jumps：continue / break 经过 finally 再到循环节点 / 循环之后，连线都画在图上，不需要近似
    # ret：return 经过 finally，经过结局后只走 finally 里的节点，结局合法
    assert r.uncaught_exits == 2  # from_handler、from_else：异常经过 finally 离开


G2_CASES = {"app/g2.py": """
    def a(): ...
    def boom(x):
        if x:
            raise ValueError(x)
    def b(): ...
    def c(): ...
    def d(): ...
    def gf(x):
        try:
            a()
            boom(x)
        finally:
            c()
        if x:
            d()
    def gl(ys):
        try:
            a()
        finally:
            for y in ys:
                b()
            c()
"""}


def test_missing_edges_out_of_a_finally_are_reported(tmp_path):
    """复审的四个例子：从 finally 体出发的缺连线都是不符（没有 finally 出口的近似）。"""
    graph, recs = traced(tmp_path, G2_CASES, "from app.g2 import gf, gl\ndef test_a():\n    gf(0)\n    gl([1])\n")
    t = textwrap.dedent(G2_CASES["app/g2.py"]).lstrip("\n")
    a, boom, c, cond, d = line(t, "a()"), line(t, "boom(x)"), line(t, "c()"), line(t, "if x:", 2), line(t, "d()")
    loop, c2 = line(t, "for y in ys"), line(t, "c()", 2)  # gl 里的
    assert verify(graph, recs).ok
    # 1. 手写：finally 里的 c() 跳过 if x 直接到 d()
    r1 = verify(graph, [rec(graph, "gf", [a, boom, c, d])])
    assert kinds(r1) == [("edge", c, d)]
    # 3. 手写：从 finally 里的 c() 跳回 try 体的 a()
    r3 = verify(graph, [rec(graph, "gf", [a, boom, c, a], exit="raise")])
    assert kinds(r3) == [("edge", c, a)]
    # 2. 真实记录 gf(0)，去掉 c() → if x 的连线
    r2 = verify(without_edge(graph, "gf", c, cond), recs)
    assert kinds(r2) == [("edge", c, cond)]
    # 4. 真实记录 gl([1])，去掉 finally 体里 for → c() 的 loop_exit：两端都在同一个 finally 体里
    r4 = verify(without_edge(graph, "gl", loop, c2), recs)
    assert kinds(r4) == sorted([("edge", c, cond), ("edge", loop, c2)])


NESTED = {"app/n.py": """
    def a(): ...
    def c(): ...
    def d(): ...
    def e(): ...
    def brk(xs):
        for x in xs:
            try:
                try:
                    a()
                    break
                finally:
                    c()
            finally:
                d()
        e()
    def cont(xs):
        for x in xs:
            try:
                try:
                    a()
                    continue
                finally:
                    c()
            finally:
                d()
            e()
        e()
    def ret(xs):
        for x in xs:
            try:
                try:
                    return a()
                finally:
                    c()
            finally:
                d()
        e()
    def to_end(xs):
        for x in xs:
            try:
                a()
                break
            finally:
                c()
"""}


def test_break_continue_return_through_two_nested_finallies(tmp_path):
    graph, recs = traced(tmp_path, NESTED, """
        from app.n import brk, cont, ret, to_end
        def test_a():
            brk([1])
            cont([1, 2])
            ret([1])
            to_end([1])
    """)
    r = verify(graph, recs)
    # brk / cont：跳转 → 内层 finally → 外层 finally → break 目标 / 循环节点；to_end：break 经过 finally 到函数末尾；
    # ret：经过 return 之后只走了两层 finally，结局合法。都在图上，不需要近似
    assert r.ok, r.mismatches
    assert r.approx_list == [] and r.steps_total == r.steps_on_graph


SWALLOW = {"app/w.py": """
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def boom(x):
        if x:
            raise ValueError(x)
    def d(): ...
    def p1(flag, x):
        try:
            if flag:
                boom(x)
                a()
        finally:
            d()
    def p2(flag, x):
        try:
            if flag:
                boom(x)
                a()
        except ValueError:
            logger.warning("x")
        d()
    def p3(x):
        try:
            a()
            boom(x)
            a()
        except ValueError:
            logger.warning("x")
"""}


def test_exceptions_landing_after_an_if_and_at_the_end_of_the_function(tmp_path, capsys):
    """H7：try 体以 if 开头时异常连线和"条件不成立"连线并存；H8：只写日志的 except 是函数最后一句，
    异常吞掉后落到函数末尾。"""
    graph, recs = traced(tmp_path, SWALLOW, """
        import pytest
        from app.w import p1, p2, p3
        def test_a():
            with pytest.raises(ValueError):
                p1(True, 1)
            p2(True, 1)
            p3(1)
    """)
    t = textwrap.dedent(SWALLOW["app/w.py"]).lstrip("\n")
    boom1, boom2, boom3 = (line(t, "boom(x)", i) for i in (1, 2, 3))
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert sorted((parse_key(x.fn)[1], x.from_line, x.to_line, x.kind) for x in r.approx_list) == [
        ("p1", boom1, line(t, "d()"), "entry"),  # try 体 → finally 入口
        ("p2", boom2, line(t, "d()", 2), "entry"),  # try 体 → 只写日志的 except 之后
        ("p3", boom3, 0, "entry"),  # try 体 → 函数末尾（只写日志的 except 是最后一句）
    ]
    assert r.steps_total == r.steps_on_graph + r.approx_entries
    assert main(["verify", *write_inputs(tmp_path, graph, recs)]) == 0
    out = capsys.readouterr().out
    listing = out[out.index("== 异常与收尾的近似（共 3 处"):]
    p1, p3 = key(graph, "p1"), key(graph, "p3")
    assert f"  1×  {p1}  第 {boom1} 行 → 第 {line(t, 'd()')} 行（入口）\n      {boom1} | boom(x)\n" \
           f"      {line(t, 'd()')} | d()" in listing
    assert f"  1×  {p3}  第 {boom3} 行 → 函数末尾（入口）\n      {boom3} | boom(x)\n" in listing


INNER_LOOPS = {"app/il.py": """
    def it(xs):
        return iter(xs)
    def a(): ...
    def c(): ...
    def d(): ...
    def e(): ...
    def r1(xs):
        for x in xs:
            try:
                for y in it(xs):
                    a()
                break
            finally:
                c()
        e()
    def r2(xs):
        for x in xs:
            try:
                for y in it(xs):
                    if y:
                        break
                    a()
                break
            finally:
                c()
        e()
    def r3(xs):
        while True:
            try:
                for y in it(xs):
                    a()
                break
            finally:
                c()
        e()
    def r4(xs):
        for x in xs:
            try:
                for y in it(xs):
                    a()
                break
            finally:
                c()
    def outer_jump(xs, ys):
        for x in xs:
            for y in ys:
                try:
                    a()
                finally:
                    c()
                d()
            e()
    def stop_mid(xs):
        for x in xs:
            try:
                a()
                break
            finally:
                c()
                d()
"""}


def test_break_after_an_inner_loop_goes_through_finally(tmp_path):
    """复审的四种形状：try 体里先有内层循环再 break（break 的出边沿用内层循环的 loop_exit 类型，或从内层循环体里
    的 break 出来）。break 经过 finally 再跳出外层循环（或到函数末尾），真实路径都在图上。"""
    graph, recs = traced(tmp_path, INNER_LOOPS, """
        from app.il import r1, r2, r3, r4
        def test_a():
            r1([0, 1])
            r2([0, 1])
            r3([0, 1])
            r4([0, 1])
    """)
    t = textwrap.dedent(INNER_LOOPS["app/il.py"]).lstrip("\n")
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.approx_list == []
    # 内层循环迭代完、接着 break：进入 finally 的连线保持 loop_exit（每个循环节点都有自己的 loop_exit）；
    # 内层循环体里的 break 出来、接着 break：进入 finally 的连线是 finally，"是"留在 label
    for fn, src, kind, label in (("r1", "for y in it(xs)", "loop_exit", ""), ("r2", "if y:", "finally", "是")):
        fg = graph.functions[key(graph, fn)]
        line_of = {n.id: n.line for n in fg.nodes}
        fin = next(n.line for n in fg.nodes if n.subtitle == "c")
        assert (kind, label) in {(x.kind, x.label) for x in fg.edges
                                 if (line_of[x.src], line_of[x.dst]) == (line(t, src), fin)}, fn


def test_hand_records_leaving_a_finally_the_wrong_way_are_reported(tmp_path):
    t = textwrap.dedent(INNER_LOOPS["app/il.py"]).lstrip("\n")
    graph = analyze_project(make_project(tmp_path, {"app/il.py": t}))
    outer, inner = line(t, "for x in xs", 4), line(t, "for y in ys")  # r3 用 while，outer_jump 是第 4 个 for x
    a, c = line(t, "a()", 5), line(t, "c()", 5)
    # finally 之后跳到外层循环（不是包住这个 try 的最内层循环）的循环节点：不符
    r = verify(graph, [rec(graph, "outer_jump", [outer, inner, a, c, outer])])
    assert kinds(r) == [("edge", c, outer)] and r.approx_list == []
    # 停在 finally 体的 c()：c() 之后还有 d()，break 要走完 finally 才到函数末尾
    loop, a2, c2 = line(t, "for x in xs", 5), line(t, "a()", 6), line(t, "c()", 6)
    r2 = verify(graph, [rec(graph, "stop_mid", [loop, a2, c2])])
    assert kinds(r2) == [("end", c2, 0)] and r2.approx_list == []


BREAK_EDGES = {"app/be.py": """
    def a(): ...
    def b(): ...
    def c(): ...
    def d(): ...
    def e(): ...
    def boom(v):
        if v:
            raise ValueError(v)
    def g1(xs):
        for x in xs:
            try:
                a()
                if x:
                    break
                b()
            finally:
                c()
        e()
    def g2(xs):
        for x in xs:
            try:
                a()
                if x:
                    break
                b()
            finally:
                c()
    def g4(xs):
        for x in xs:
            try:
                a()
                if x:
                    break
                b()
            finally:
                c()
        else:
            d()
        e()
    def g5(oss):
        for xs in oss:
            for x in xs:
                try:
                    a()
                finally:
                    c()
            else:
                break
            d()
        e()
    def last(oss):
        for xs in oss:
            for x in xs:
                try:
                    a()
                    if x:
                        break
                    b()
                finally:
                    c()
        e()
    def g3(xs):
        for x in xs:
            try:
                a()
                if x:
                    continue
                b()
            finally:
                c()
            d()
        e()
    def hb(xs):
        for x in xs:
            try:
                boom(x)
            except ValueError:
                d()
                break
            finally:
                c()
        e()
    def hc(xs):
        for x in xs:
            try:
                boom(x)
            except ValueError:
                d()
                continue
            finally:
                c()
            b()
        e()
    def eb(xs):
        for x in xs:
            try:
                boom(0)
            except ValueError:
                d()
            else:
                if x:
                    break
            finally:
                c()
        e()
    def ec(xs):
        for x in xs:
            try:
                boom(0)
            except ValueError:
                d()
            else:
                if x:
                    continue
            finally:
                c()
            b()
        e()
"""}


def rerouted(graph, qualname, src_line, dst_line, new_dst_line):
    """模拟建图缺陷：把函数图里一条连线接到别的节点上。"""
    fg = graph.functions[key(graph, qualname)]
    line_of = {n.id: n.line for n in fg.nodes}
    [edge] = [e for e in fg.edges if (line_of[e.src], line_of[e.dst]) == (src_line, dst_line)]
    [edge.dst] = [n.id for n in fg.nodes if n.line == new_dst_line]
    return graph


def test_missing_or_misrouted_break_after_finally_is_reported(tmp_path):
    """break 经过 finally 的两段连线（跳转处 → finally、finally 出口 → 循环之后）漏画或画错时，真实记录要报出来，
    不能靠循环自己的 loop_exit 或别的连线蒙混过去。"""
    test_src = """
        from app.be import g1, g2, g4, last
        def test_a():
            g1([0, 1])
            g2([0, 1])
            g4([0, 1])
            last([[0, 1], [0]])
    """
    t = textwrap.dedent(BREAK_EDGES["app/be.py"]).lstrip("\n")
    graph, recs = traced(tmp_path, BREAK_EDGES, test_src)

    def fresh():
        return from_json(to_json(graph))

    def only(qualname):
        return [x for x in recs if x.get("fn") == key(graph, qualname)]

    loop, cond, c, e = (line(t, s) for s in ("for x in xs", "if x:", "c()", "e()"))
    # 图完好时：真实路径都在图上，不需要近似
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.approx_list == []
    # D1：finally 出口 → 循环之后（c() → e()）漏画
    r1 = verify(without_edge(fresh(), "g1", c, e), only("g1"))
    assert kinds(r1) == [("edge", c, e)] and r1.approx_list == []
    # D2：这条连线画到了循环节点上
    r2 = verify(rerouted(fresh(), "g1", c, e, loop), only("g1"))
    assert kinds(r2) == [("edge", c, e)] and r2.approx_list == []
    # 跳转处 → finally（if x → c()）漏画：if x 在 try 体里、c() 是 finally 的入口，与"try 体里抛出的异常进入
    # finally"分不开，按（不变的）入口近似放过，但逐处列出
    r1b = verify(without_edge(fresh(), "g1", cond, c), only("g1"))
    assert kinds(r1b) == [] and [(x.from_line, x.to_line, x.kind) for x in r1b.approx_list] == [(cond, c, "entry")]
    # D3：循环是函数最后一句，finally 出口 → 函数末尾漏画
    c2 = line(t, "c()", 2)
    r3 = verify(without_edge(fresh(), "g2", c2, 0), only("g2"))
    assert kinds(r3) == [("end", c2, 0)] and r3.approx_list == []
    # D5：for-else，finally 出口 → 循环之后漏画（循环自己的出口去的是 else 体）
    c4, e4 = line(t, "c()", 3), line(t, "e()", 2)
    r5 = verify(without_edge(fresh(), "g4", c4, e4), only("g4"))
    assert kinds(r5) == [("edge", c4, e4)] and r5.approx_list == []
    # D6：L 的 else 体里的 break 属于外层循环，L 的 finally 之后不能直接到 e()（手工记录）
    o5, i5, a5, c5, e5 = (line(t, s, n) for s, n in
                          (("for xs in oss", 1), ("for x in xs", 4), ("a()", 4), ("c()", 4), ("e()", 3)))
    r6 = verify(graph, [rec(graph, "g5", [o5, i5, a5, c5, e5])])
    assert kinds(r6) == [("edge", c5, e5)] and r6.approx_list == []
    # L 是外层循环体的最后一句：L 的 break 经过 finally 之后改成 loop_back 接回外层循环节点，真实路径合法
    outer, c_last = line(t, "for xs in oss", 2), line(t, "c()", 5)
    rl = verify(graph, only("last"))
    assert rl.ok and rl.approx_list == [], rl.mismatches
    # 这条连线漏画时同样要报出来
    rm = verify(without_edge(fresh(), "last", c_last, outer), only("last"))
    assert kinds(rm) == [("edge", c_last, outer)] and rm.approx_list == []


def test_jumps_in_try_handler_or_else_go_through_finally(tmp_path):
    """continue / break 写在 try 体、except 体、else 体里，经过 finally 都合法；finally 出口 → 跳转目标的连线
    漏画或改道都要报出来。"""
    t = textwrap.dedent(BREAK_EDGES["app/be.py"]).lstrip("\n")
    graph, recs = traced(tmp_path, BREAK_EDGES, """
        from app.be import g3, hb, hc, eb, ec
        def test_a():
            g3([1, 0])
            hb([0, 1])
            hc([1, 0])
            eb([0, 1])
            ec([1, 0])
    """)
    rows = t.splitlines()

    def at(fn, needle):
        """函数 fn 里第一个（去掉缩进后）以 needle 开头的行的行号。"""
        start = rows.index(f"def {fn}(xs):")
        return next(i + 1 for i in range(start, len(rows)) if rows[i].strip().startswith(needle))

    def only(qualname):
        return [x for x in recs if x.get("fn") == key(graph, qualname)]

    def fresh():
        return from_json(to_json(graph))

    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.approx_list == []
    # D4：continue 经过 finally 之后回到循环节点的连线（c() → for x）漏画、改道到 d()
    loop, c = at("g3", "for x"), at("g3", "c()")
    r4 = verify(without_edge(fresh(), "g3", c, loop), only("g3"))
    assert kinds(r4) == [("edge", c, loop)] and r4.approx_list == []
    r4b = verify(rerouted(fresh(), "g3", c, loop, at("g3", "d()")), only("g3"))
    assert kinds(r4b) == [("edge", c, loop)] and r4b.approx_list == []
    # except 体里的 break、else 体里的 continue：finally 出口 → 跳转目标漏画
    rb = verify(without_edge(fresh(), "hb", at("hb", "c()"), at("hb", "e()")), only("hb"))
    assert kinds(rb) == [("edge", at("hb", "c()"), at("hb", "e()"))] and rb.approx_list == []
    rc = verify(without_edge(fresh(), "ec", at("ec", "c()"), at("ec", "for x")), only("ec"))
    assert kinds(rc) == [("edge", at("ec", "c()"), at("ec", "for x"))] and rc.approx_list == []


CONTINUE_KINDS = {"app/ck.py": """
    def a(v=None): ...
    def b(v=None): ...
    def c(): ...
    def d(v=None): ...
    def e(): ...
    def boom(v):
        if v:
            raise ValueError(v)
    def fetch(v):
        if v:
            raise TimeoutError(v)
        return v
    def inner_for(xs):
        for x in xs:
            try:
                if x:
                    for y in x:
                        a(y)
                    continue
                b(x)
            finally:
                c()
            d(x)
        e()
    def inner_while(xs):
        for x in xs:
            try:
                if x:
                    i = 0
                    while i < len(x):
                        a(x[i])
                        i += 1
                    continue
                b(x)
            finally:
                c()
            d(x)
        e()
    def while_true(xs):
        it = iter(xs)
        while True:
            try:
                x = next(it, None)
                if x is None:
                    break
                if x:
                    for y in x:
                        a(y)
                    continue
                b(x)
            finally:
                c()
            d(x)
        e()
    def two_finallies(xs):
        for x in xs:
            try:
                try:
                    if x:
                        for y in x:
                            a(y)
                        continue
                    b(x)
                finally:
                    d()
                a()
            finally:
                c()
            b()
        e()
    def fallback(xs):
        for x in xs:
            try:
                boom(x)
            except ValueError:
                try:
                    return fetch(x)
                except TimeoutError:
                    pass
                continue
            finally:
                c()
            d(x)
        e()
"""}


def test_continue_after_inner_loop_or_swallowed_exception_goes_through_finally(tmp_path):
    """continue 之前是没有 break 的内层循环（出边沿用内层循环的 loop_exit），或总是 return、except 只 pass 的内层
    try（出边沿用 raise）：进入 finally 的连线保持这两种类型，经过 finally 再回到循环节点，真实路径都在图上。"""
    t = textwrap.dedent(CONTINUE_KINDS["app/ck.py"]).lstrip("\n")
    graph, recs = traced(tmp_path, CONTINUE_KINDS, """
        from app.ck import inner_for, inner_while, while_true, two_finallies, fallback
        def test_a():
            inner_for([[1], []])
            inner_while([[1], []])
            while_true([[1], []])
            two_finallies([[1], []])
            fallback([2, 0])
    """)
    rows = t.splitlines()

    def at(fn, needle):
        start = rows.index(f"def {fn}(xs):")
        return next(i + 1 for i in range(start, len(rows)) if rows[i].strip().startswith(needle))

    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.approx_list == []
    # 进入 finally 的连线确实是 loop_exit / raise（否则这个测试没有测到这两种类型）
    for fn, src, kind in (("inner_for", "for y", "loop_exit"), ("fallback", "return fetch", "raise")):
        fg = graph.functions[key(graph, fn)]
        line_of = {n.id: n.line for n in fg.nodes}
        assert kind in {e.kind for e in fg.edges if (line_of[e.src], line_of[e.dst]) == (at(fn, src), at(fn, "c()"))}


SWALLOW_EVIDENCE = {"app/se.py": """
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def b(x):
        if x:
            raise ValueError(x)
    def d(): ...
    def sw(x):
        try:
            a()
            b(x)
        except ValueError:
            logger.warning("x")
        d()
    def sw_end(x):
        try:
            a()
            b(x)
            a()
        except ValueError:
            logger.warning("x")
"""}


def test_swallowed_exception_needs_the_except_header_in_the_record(tmp_path):
    """吞掉的异常落到 try 之后 / 函数末尾的近似，要求原始行号里在这一步之间出现了那个只写日志的 except 头部行。"""
    graph, recs = traced(tmp_path, SWALLOW_EVIDENCE, """
        from app.se import sw, sw_end
        def test_a():
            sw(0)
            sw_end(0)
            sw_end(1)
    """)
    t = textwrap.dedent(SWALLOW_EVIDENCE["app/se.py"]).lstrip("\n")
    b1, d = line(t, "b(x)"), line(t, "d()")
    b2 = line(t, "b(x)", 2)
    header = line(t, "except ValueError", 2)
    # sw_end(1)：异常被吞掉，except 头部行在原始行号里；sw_end(0) 没有
    assert sorted(header in x["lines"] for x in recs if x.get("fn") == key(graph, "sw_end")) == [False, True]
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert [(parse_key(x.fn)[1], x.from_line, x.to_line, x.kind) for x in r.approx_list] == [
        ("sw_end", b2, 0, "entry")]  # H8：有 except 头部行作证
    # 去掉建图连线 b(x) → d()（try 之后）：sw(0) 没抛异常，记录里没有 except 头部行，不能算吞掉异常的近似
    broken = verify(without_edge(graph, "sw", b1, d), recs)
    assert kinds(broken) == [("edge", b1, d)] and broken.approx_entries == 1
    # 去掉建图连线 a() → 函数末尾（sw_end 的 try 之后）：sw_end(0) 没抛异常，停在最后的 a() 不能靠 H8 放过
    a3 = line(t, "a()", 3)
    broken_end = verify(without_edge(graph, "sw_end", a3, 0), recs)
    assert kinds(broken_end) == sorted([("edge", b1, d), ("end", a3, 0)]) and broken_end.approx_entries == 1


# ---------- try 体第一个节点之前抛出的异常（原始行号作证）----------

EARLY = {"app/e.py": """
    import json
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def save(v): ...
    def h(): ...
    def c(): ...
    def d(): ...
    def load(s):
        a()
        try:
            v = json.loads(s)
            save(v)
        except ValueError:
            h()
    def load_fin(s):
        a()
        try:
            v = json.loads(s)
            save(v)
        finally:
            c()
    def swallow(s):
        a()
        try:
            v = json.loads(s)
            save(v)
        except ValueError:
            logger.warning("bad")
        h()
    def swallow_end(s):
        a()
        try:
            v = json.loads(s)
            save(v)
        except ValueError:
            logger.warning("bad")
    def in_loop(ss):
        for s in ss:
            try:
                v = json.loads(s)
                save(v)
            except ValueError:
                h()
    def nested(s):
        a()
        try:
            try:
                v = json.loads(s)
                save(v)
            except KeyError:
                h()
            c()
        except ValueError:
            d()
    def with_else(s):
        a()
        try:
            v = json.loads(s)
            save(v)
        except ValueError:
            h()
        else:
            v = json.loads(s)
        finally:
            c()
"""}
EARLY_TEXT = textwrap.dedent(EARLY["app/e.py"]).lstrip("\n")
EARLY_TEST = """
    import pytest
    from app.e import in_loop, load, load_fin, nested, swallow, swallow_end
    def test_a():
        load("1")
        load("x")
        load_fin("1")
        with pytest.raises(ValueError):
            load_fin("x")
        swallow("1")
        swallow("x")
        swallow_end("1")
        swallow_end("x")
        in_loop(["1", "x"])
        nested("x")
"""


def at_def(text, fn, needle, nth=1):
    """函数 fn 里第 nth 个（去掉缩进后）以 needle 开头的行的行号。"""
    rows = text.splitlines()
    start = next(i for i, row in enumerate(rows) if row.startswith(f"def {fn}("))
    return [i + 1 for i in range(start, len(rows)) if rows[i].strip().startswith(needle)][nth - 1]


def test_exception_before_the_first_node_of_a_try_body(tmp_path):
    """try 体以不产生节点的语句（json.loads）开头，它抛出的异常让执行从 try 之前的节点直接到落脚点。原始行号里
    这一步之间出现了 try 体里的行：按入口近似认可，逐处列出。"""
    graph, recs = traced(tmp_path, EARLY, EARLY_TEST)
    t = EARLY_TEXT
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert sorted((parse_key(x.fn)[1], x.from_line, x.to_line, x.kind, x.count) for x in r.approx_list) == sorted([
        ("load", at_def(t, "load", "a()"), at_def(t, "load", "except ValueError"), "entry", 1),  # 被 except 接住
        ("load_fin", at_def(t, "load_fin", "a()"), at_def(t, "load_fin", "c()"), "entry", 1),  # 只有 finally：往外传
        ("swallow", at_def(t, "swallow", "a()"), at_def(t, "swallow", "h()"), "entry", 1),  # 只写日志的 except 吞掉
        ("swallow_end", at_def(t, "swallow_end", "a()"), 0, "entry", 1),  # 吞掉后到函数末尾
        ("in_loop", at_def(t, "in_loop", "for s"), at_def(t, "in_loop", "except ValueError"), "entry", 1),  # A 是循环节点
        ("nested", at_def(t, "nested", "a()"), at_def(t, "nested", "except ValueError"), "entry", 1),  # 内层没接住
    ])
    assert r.steps_total == r.steps_on_graph + r.approx_entries and r.uncaught_exits == 1


def test_exception_before_the_first_node_needs_try_body_lines_and_a_landing(tmp_path):
    """反方向：B 没有 raise / finally 入边、或这一步之间原始行号里没有 try 体里的行（只有 try 行、except 头部和
    except 体、finally 体的行），都仍是不符。"""
    graph, recs = traced(tmp_path, EARLY, EARLY_TEST)
    t = EARLY_TEXT
    a, try_, exc, h = (at_def(t, "load", s) for s in ("a()", "try:", "except ValueError", "h()"))
    # 去掉通往捕获节点的全部 raise 入边：B 不再是落脚点
    broken = from_json(to_json(graph))
    fg = broken.functions[key(broken, "load")]
    [catch] = [n.id for n in fg.nodes if n.type == "catch"]
    fg.edges = [e for e in fg.edges if not (e.dst == catch and e.kind == "raise")]
    r1 = verify(broken, [x for x in recs if x.get("fn") == key(graph, "load")])
    assert kinds(r1) == [("edge", a, exc)] and r1.approx_list == []
    # 手工记录：try 之前的 a() 直接到捕获节点，中间只有 try 这一行（不是 try 体里的行）
    r2 = verify(graph, [rec(graph, "load", [a, try_, exc, h])])
    assert kinds(r2) == [("edge", a, exc)] and r2.approx_list == []
    # 只有 except 头部和 except 体的行：有吞掉异常的证据，但没有进入 try 体的证据
    sa, sexc, slog, sh = (at_def(t, "swallow", s) for s in ("a()", "except ValueError", "logger.warning", "h()"))
    r3 = verify(graph, [rec(graph, "swallow", [sa, at_def(t, "swallow", "try:"), sexc, slog, sh])])
    assert kinds(r3) == [("edge", sa, sh)] and r3.approx_list == []
    # 只有 finally 体的行
    fa, fc = at_def(t, "load_fin", "a()"), at_def(t, "load_fin", "c()")
    r4 = verify(graph, [rec(graph, "load_fin", [fa, at_def(t, "load_fin", "try:"), fc], exit="raise")])
    assert kinds(r4) == [("edge", fa, fc)] and r4.approx_list == []
    # 吞掉后到函数末尾：没有 try 体里的行时是结局不符
    ea = at_def(t, "swallow_end", "a()")
    r5 = verify(graph, [rec(graph, "swallow_end", [ea, at_def(t, "swallow_end", "except ValueError"),
                                                   at_def(t, "swallow_end", "logger.warning")])])
    assert kinds(r5) == [("end", ea, 0)] and r5.approx_list == []
    # 只有 else 体的行（else 体里的语句抛出、落到 finally）：else 体的行不是进入 try 体的证据
    wa, wc = at_def(t, "with_else", "a()"), at_def(t, "with_else", "c()")
    r6 = verify(graph, [rec(graph, "with_else", [wa, at_def(t, "with_else", "try:"),
                                                 at_def(t, "with_else", "v = json.loads", 2), wc], exit="raise")])
    assert kinds(r6) == [("edge", wa, wc)] and r6.approx_list == []
    # try 体里的行只出现在上一轮循环里，之后 for 行再次出现（同一节点行合并，证据随之丢掉）：for → 捕获节点仍是不符
    lp, lj, le, lh = (at_def(t, "in_loop", s) for s in ("for s", "v = json.loads", "except ValueError", "h()"))
    r7 = verify(graph, [rec(graph, "in_loop", [lp, lj, lp, le, lh, lp])])
    assert kinds(r7) == [("edge", lp, le)] and r7.approx_list == []


SWALLOWED_RAISE = {"app/sr.py": """
    import logging
    logger = logging.getLogger(__name__)
    def c(): ...
    def s_tail(xs):
        for x in xs:
            try:
                try:
                    raise ValueError(x)
                except ValueError:
                    logger.info("x")
                break
            finally:
                c()
    def t_tail(x):
        try:
            try:
                raise ValueError(x)
            except ValueError:
                pass
        finally:
            c()
    def ret_through(x):
        try:
            return c()
        finally:
            try:
                raise ValueError(x)
            except ValueError:
                logger.info("x")
        c()
"""}


def test_swallowed_raise_then_finally_is_not_a_return_outcome(tmp_path):
    """以 return 离开时，"经过结局之后只走 finally 里的节点"不认 raise 语句的结局：异常被吞掉了，函数是正常返回的，
    最后的 finally 节点要有连线到"执行到函数末尾"。return 正在经过 finally、finally 里 raise 后又被吞掉
    （ret_through）时，认的是先前那个 return 的结局。"""
    graph, recs = traced(tmp_path, SWALLOWED_RAISE, """
        from app.sr import ret_through, s_tail, t_tail
        def test_a():
            s_tail([1, 2])
            t_tail(1)
            ret_through(1)
    """)
    t = textwrap.dedent(SWALLOWED_RAISE["app/sr.py"]).lstrip("\n")
    r = verify(graph, recs)
    assert r.ok and r.approx_list == [], r.mismatches
    for fn in ("s_tail", "t_tail"):
        broken = from_json(to_json(graph))
        fg = broken.functions[key(broken, fn)]
        line_of = {n.id: n.line for n in fg.nodes}
        c = at_def(t, fn, "c()")
        fg.edges = [e for e in fg.edges if not (line_of[e.src] == c and line_of[e.dst] == 0)]  # c → 执行到函数末尾
        r = verify(broken, [x for x in recs if x.get("fn") == key(graph, fn)])
        assert kinds(r) == [("end", c, 0)], fn


RET_ESCAPE = {"app/re.py": """
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def c(): ...
    def d(): ...
    def ret_escape(x):
        try:
            try:
                return a()
            finally:
                raise ValueError(x)
        except ValueError:
            logger.info("x")
    def ret_escape_in_fin(x):
        try:
            d()
        finally:
            try:
                try:
                    return a()
                finally:
                    raise ValueError(x)
            except ValueError:
                c()
    def ret_pass(x):
        try:
            return a()
        finally:
            try:
                raise ValueError(x)
            except ValueError:
                pass
    def ret_inner_fin(x):
        try:
            return a()
        finally:
            try:
                try:
                    raise ValueError(x)
                finally:
                    c()
            except ValueError:
                pass
        d()
"""}


def test_raise_in_finally_that_cancels_a_return_is_not_a_return_outcome(tmp_path):
    """return 正在经过 finally 时 finally 里 raise：异常要在这个 finally 体里被吞掉，return 才继续。异常逃出这个
    finally（被外面只写日志的 except 或 finally 外面的 except 接住）时 return 已被取消，函数是从别处正常返回的：
    不能靠先前那个 return 的结局放过，要么按入口近似列出，要么（连线缺了）报结局不符。"""
    graph, recs = traced(tmp_path, RET_ESCAPE, """
        from app.re import ret_escape, ret_escape_in_fin, ret_inner_fin, ret_pass
        def test_a():
            ret_escape(1)
            ret_escape_in_fin(1)
            ret_pass(1)
            ret_inner_fin(1)
    """)
    t = textwrap.dedent(RET_ESCAPE["app/re.py"]).lstrip("\n")
    raise1, raise2 = at_def(t, "ret_escape", "raise"), at_def(t, "ret_escape_in_fin", "raise")
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    # 不再悄悄放过：吞掉的异常落到函数末尾、raise 结局 → 外层捕获节点，都逐处列出。ret_pass、ret_inner_fin 在 return
    # 正经过的 finally 里吞掉（后者先经过内层 finally 的 c()，再被 except 接住），return 继续，不需要近似
    assert sorted((parse_key(x.fn)[1], x.from_line, x.to_line) for x in r.approx_list) == sorted([
        ("ret_escape", raise1, 0), ("ret_escape_in_fin", raise2, at_def(t, "ret_escape_in_fin", "except ValueError"))])
    for fn, src in (("ret_escape", None), ("ret_escape_in_fin", at_def(t, "ret_escape_in_fin", "c()"))):
        broken = from_json(to_json(graph))
        fg = broken.functions[key(broken, fn)]
        line_of = {n.id: n.line for n in fg.nodes}
        fg.edges = [e for e in fg.edges if not (line_of[e.dst] == 0 and (src is None or line_of[e.src] == src))]
        rb = verify(broken, [x for x in recs if x.get("fn") == key(graph, fn)])
        assert [m.kind for m in rb.mismatches] == ["end"], (fn, rb.mismatches)


ONE_LINE = {"app/ol.py": """
    import logging
    logger = logging.getLogger(__name__)
    def a(): ...
    def c(): ...
    def one_line_pass(x):
        try:
            try:
                return a()
            finally:
                try:
                    raise ValueError(x)
                except KeyError: pass
        except ValueError:
            logger.info("x")
    def one_line_miss(x):
        try:
            try:
                return a()
            finally:
                try:
                    raise ValueError(x)
                except KeyError: c()
        except ValueError:
            logger.info("x")
    def one_line_ok(x):
        try:
            return a()
        finally:
            try:
                raise ValueError(x)
            except ValueError: pass
        c()
    def one_line_ok_node(x):
        try:
            return a()
        finally:
            try:
                raise ValueError(x)
            except ValueError: c()
        c()
"""}


def test_one_line_except_is_evidence_only_when_nothing_shows_the_exception_went_on(tmp_path):
    """写在一行的 except：头部行事件不论是否匹配都会出现。它之后（跳过异常经过的 finally 体和 with 头部）的第一行
    是 except 头部时，异常没被它吞掉，不能作为"异常在 finally 里被吞掉"的证据：return 已被取消，结局要靠连线，
    连线缺了报结局不符。真的在 finally 里吞掉（之后没有 except 头部）的，return 照常继续。"""
    graph, recs = traced(tmp_path, ONE_LINE, """
        from app.ol import one_line_miss, one_line_ok, one_line_ok_node, one_line_pass
        def test_a():
            one_line_pass(1)
            one_line_miss(1)
            one_line_ok(1)
            one_line_ok_node(1)
    """)
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    for fn in ("one_line_ok", "one_line_ok_node"):  # finally 里一行的 except 吞掉了异常：return 继续，函数之后的 c() 走不到
        assert [x for x in r.approx_list if parse_key(x.fn)[1] == fn] == [], fn
    for fn in ("one_line_pass", "one_line_miss"):
        broken = from_json(to_json(graph))
        fg = broken.functions[key(broken, fn)]
        line_of = {n.id: n.line for n in fg.nodes}
        fg.edges = [e for e in fg.edges if line_of[e.dst] != 0]  # 去掉通往"执行到函数末尾"的全部连线
        rb = verify(broken, [x for x in recs if x.get("fn") == key(graph, fn)])
        assert [m.kind for m in rb.mismatches] == ["end"], (fn, rb.mismatches, rb.approx_list)


# ---------- 裁决 4：结局 ----------

FINALLY = {"app/f.py": """
    def load(x):
        return x
    def cleanup(): ...
    def fetch(x):
        try:
            if x:
                return load(x)
        finally:
            cleanup()
        return None
    def plain(x):
        y = x + 1
    def guarded(x):
        y = x + 1
        if y:
            load(y)
"""}


def test_return_inside_try_finally_is_a_legit_outcome(tmp_path):
    graph, recs = traced(tmp_path, FINALLY, """
        from app.f import fetch
        def test_a():
            assert fetch(1) == 1
            assert fetch(0) is None
    """)
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    # 反例：没经过任何 return 就停在 finally 里的 cleanup，而 cleanup 后面是 return None，不是函数末尾
    text = textwrap.dedent(FINALLY["app/f.py"]).lstrip("\n")
    bad = verify(graph, [rec(graph, "fetch", [line(text, "if x:"), line(text, "cleanup()")])])
    assert kinds(bad) == [("end", line(text, "cleanup()"), 0)]


def test_records_without_node_lines_are_checked_by_outcome_only(tmp_path):
    graph, recs = traced(tmp_path, FINALLY, """
        import pytest
        from app.f import plain
        def test_a():
            plain(1)
            with pytest.raises(TypeError):
                plain(None)
    """)
    r = verify(graph, recs)
    assert r.ok and r.uncaught_exits == 1 and r.steps_total == 0
    # 执行到的行上没有节点：起点能走到某个结局就算合法
    text = textwrap.dedent(FINALLY["app/f.py"]).lstrip("\n")
    r2 = verify(graph, [rec(graph, "guarded", [line(text, "y = x + 1", 2)])])
    assert r2.ok and r2.distinct_paths == 1


# ---------- 调用（裁决 D、F）----------

CALLS = {"app/c.py": """
    class Svc:
        def handle(self, x):
            return x
    class Other:
        def handle(self, x):
            return -x
    class Box:
        def __init__(self, v):
            self.v = v
        @property
        def size(self):
            return 1
        def __len__(self):
            return 2
    class Op:
        def __call__(self, x):
            return x
    def helper(x):
        return x
    def unrelated():
        return 0
    def caller(svc, xs):
        def inner(y):
            return y
        helper(xs)
        list(map(inner, xs))
        return svc.handle(xs)
    def chained(svc, xs):
        return (svc
                .handle(xs))
    def measure(b):
        return b.size + len(b)
    def build_and_run(x):
        op = Op()
        return op(Box(x).v)
"""}


def test_calls_targets_inner_implicit_dynamic_and_missing(tmp_path, capsys):
    graph, recs = traced(tmp_path, CALLS, """
        from app.c import Box, Svc, build_and_run, caller, chained, measure
        def test_a():
            assert caller(Svc(), [1, 2]) == [1, 2]
            assert chained(Svc(), [3]) == [3]
            assert measure(Box(1)) == 3
            assert build_and_run(3) == 3
    """)
    text = textwrap.dedent(CALLS["app/c.py"]).lstrip("\n")
    k = {q: key(graph, q) for q in ("caller", "chained", "measure", "build_and_run", "unrelated", "helper",
                                    "caller.<locals>.inner", "Svc.handle", "Box.size", "Box.__len__",
                                    "Box.__init__", "Op.__call__")}
    at = {s: line(text, s) for s in ("helper(xs)", "list(map", "return svc.handle", ".handle(xs))", "return b.size",
                                     "return op(")}
    assert one(recs, "caller")["calls"] == [[k["helper"], at["helper(xs)"]], [k["caller.<locals>.inner"], at["list(map"]],
                                            [k["Svc.handle"], at["return svc.handle"]]]
    # 链式调用：调用实际发生在续行 .handle(xs))，而静态记下的 svc.handle 在语句的第一行
    assert one(recs, "chained")["calls"] == [[k["Svc.handle"], at[".handle(xs))"]]]
    assert at[".handle(xs))"] == line(text, "return (svc") + 1
    assert one(recs, "measure")["calls"] == [[k["Box.size"], at["return b.size"]], [k["Box.__len__"], at["return b.size"]]]
    assert one(recs, "build_and_run")["calls"] == [[k["Box.__init__"], at["return op("]], [k["Op.__call__"], at["return op("]]]
    same_stmt = rec(graph, "caller", [], calls=[(k["unrelated"], at["return svc.handle"])])
    other_stmt = rec(graph, "caller", [], calls=[(k["unrelated"], at["helper(xs)"])])
    r = verify(graph, recs + [same_stmt, other_stmt])
    # helper、构造 Box 的 __init__、op(...) 的 __call__ 在 call_targets 里；inner 是 caller 的内部函数（经 map 回调）；
    # b.size（属性）和 len(b)（__len__）是隐式调用；
    # 调用发生的那个语句里有无法判断目标的 svc.handle → 动态调用（名字对得上为强匹配，unrelated 为弱匹配）；
    # 同一函数里别的语句有无法判断目标的调用不算数：在 helper(xs) 那一句调用 unrelated 是不符
    assert r.calls_total == 10
    assert r.implicit_calls == 2 and r.implicit_call_list == [
        ImplicitCall(k["measure"], k["Box.__len__"], "dunder", 1), ImplicitCall(k["measure"], k["Box.size"], "property", 1)]
    assert r.dynamic_calls == 3 and r.dynamic_call_list == [
        DynamicCall(k["caller"], k["Svc.handle"], at["return svc.handle"], True, ["svc.handle"], False, 1),
        DynamicCall(k["caller"], k["unrelated"], at["return svc.handle"], False, ["svc.handle"], False, 1),
        DynamicCall(k["chained"], k["Svc.handle"], at[".handle(xs))"], True, ["svc.handle"], False, 1)]
    assert kinds(r) == [("call", at["helper(xs)"], line(text, "def unrelated"))]
    assert r.mismatches[0].detail == k["unrelated"]
    assert main(["verify", *write_inputs(tmp_path, graph, recs + [same_stmt])]) == 0
    out = capsys.readouterr().out
    for label in ("隐式调用（属性、魔术方法）: 2", "动态调用（静态未确定目标）: 3", "动态调用中的强匹配: 2",
                  "动态调用中的弱匹配: 1", "调用不在图上: 0"):
        assert label in out, label
    assert f"{k['measure']} → {k['Box.size']}（属性）" in out and f"{k['measure']} → {k['Box.__len__']}（魔术方法）" in out
    assert f"{k['chained']} 第 {at['.handle(xs))']} 行 → {k['Svc.handle']}（强匹配）" in out
    assert f"{k['caller']} 第 {at['return svc.handle']} 行 → {k['unrelated']}（弱匹配）" in out
    assert ".handle(xs))" in out and "同一语句里无法判断目标的调用：svc.handle" in out
    assert out.index("== 动态调用 · 弱匹配") < out.index("== 动态调用 · 强匹配")  # 弱匹配全部列出，放在前面


def test_dynamic_call_needs_an_unresolved_call_in_the_same_statement(tmp_path):
    """手工记录：多行语句的续行上发生的调用，归到整条语句；同一函数别处的无法判断目标的调用不算。"""
    graph, text = project(tmp_path, MULTI)
    handle = key(graph, "Svc.handle")
    cont, after = line(text, "len(xs),"), line(text, "return out")
    assert [(u.line, u.name) for u in graph.functions[key(graph, "multi")].unresolved] == [
        (line(text, "out = svc.handle("), "svc.handle"), (line(text, "svc.close()"), "svc.close")]
    r = verify(graph, [rec(graph, "multi", [], calls=[(handle, cont), (handle, after), (handle, 0)])])
    assert [(d.callee, d.line, d.strong) for d in r.dynamic_call_list] == [(handle, cont, True)]
    # return out 这一句、以及记不到行号（0）的调用：所在语句里没有无法判断目标的调用 → 不符
    assert kinds(r) == sorted([("call", after, line(text, "def handle")), ("call", 0, line(text, "def handle"))])


MULTI = '''
class Svc:
    def handle(self, *a): ...
class Other:
    def handle(self, *a): ...
def multi(svc, xs):
    out = svc.handle(
        xs,
        len(xs),
    )
    svc.close()
    return out
'''


CALLBACKS = {"app/cb.py": """
    def rank(x):
        return -x
    def helper(x):
        return x
    def unrelated():
        return 0
    def deco(f):
        return f
    def default():
        return 1
    def sort_them(xs):
        ordered = sorted(xs, key=rank)
        doubled = list(map(helper, xs))
        return ordered + doubled
    def outer():
        @deco
        def inner(a=default()):
            return a
        return inner()
    def lam(p, xs):
        f = lambda: p.inside()
        return f
"""}


def test_callbacks_nested_headers_and_lambdas(tmp_path, capsys):
    graph, recs = traced(tmp_path, CALLBACKS, """
        from app.cb import outer, sort_them
        def test_a():
            assert sort_them([2, 1]) == [2, 1, 2, 1]
            assert outer() == 1
    """)
    t = textwrap.dedent(CALLBACKS["app/cb.py"]).lstrip("\n")
    k = {q: key(graph, q) for q in ("sort_them", "outer", "lam", "rank", "helper", "unrelated", "deco", "default")}
    ordered, doubled, ret = line(t, "ordered = sorted"), line(t, "doubled = list"), line(t, "return ordered")
    assert {(parse_key(c)[1], ln) for c, ln in one(recs, "sort_them")["calls"]} == {("rank", ordered), ("helper", doubled)}
    # 嵌套定义的装饰器 @deco 和默认值 default() 在 outer 的帧里执行（H2）
    assert {parse_key(c)[1] for c, _ in one(recs, "outer")["calls"]} == {"deco", "default", "outer.<locals>.inner"}
    bad = [rec(graph, "sort_them", [], calls=[(k["unrelated"], ordered), (k["helper"], ret)]),
           rec(graph, "lam", [], calls=[(k["unrelated"], line(t, "f = lambda"))])]
    r = verify(graph, recs + bad)
    # H1：rank、helper 在调用发生的语句里作为参数传了出去 → 动态调用（强匹配，回调）
    assert sorted((parse_key(d.callee)[1], d.line, d.strong, d.callback, tuple(d.names)) for d in r.dynamic_call_list) == [
        ("helper", doubled, True, True, ("helper",)), ("rank", ordered, True, True, ("rank",))]
    # 同一语句传出的是别的名字、或者在别的语句里调用：不符；H3：lambda 体里无法判断目标的 p.inside 不算 lam 帧里的调用
    assert kinds(r) == sorted([("call", ordered, line(t, "def unrelated")), ("call", ret, line(t, "def helper")),
                               ("call", line(t, "f = lambda"), line(t, "def unrelated"))])
    assert graph.functions[k["lam"]].unresolved == []
    assert main(["verify", *write_inputs(tmp_path, graph, recs)]) == 0
    out = capsys.readouterr().out
    assert f"{k['sort_them']} 第 {ordered} 行 → {k['rank']}（强匹配，回调）" in out and "同一语句里作为参数传出：rank" in out


ACCESSORS = '''
import functools
from functools import cached_property
class C:
    @property
    def a(self): ...
    @a.setter
    def a(self, v): ...
    @functools.cached_property
    def b(self): ...
    @cached_property
    def c(self): ...
    def __eq__(self, o): ...
    def __init__(self): ...
    def __call__(self): ...
    def plain(self): ...
def user():
    return 1
'''


def test_accessors_and_dunders_are_implicit_but_init_and_call_are_not(tmp_path):
    graph, _ = project(tmp_path, ACCESSORS)
    getter, setter = sorted((f for f in graph.functions.values() if f.qualname == "C.a"), key=lambda f: f.line)
    implicit = [getter.key, setter.key, key(graph, "C.b"), key(graph, "C.c"), key(graph, "C.__eq__")]
    explicit = [key(graph, q) for q in ("C.__init__", "C.__call__", "C.plain")]
    r = verify(graph, [rec(graph, "user", [], calls=[(c, line(ACCESSORS, "return 1")) for c in implicit + explicit])])
    assert r.implicit_calls == 5 and r.dynamic_calls == 0
    assert sorted((d.callee, d.reason) for d in r.implicit_call_list) == sorted(
        [(getter.key, "property"), (setter.key, "property"), (key(graph, "C.b"), "property"),
         (key(graph, "C.c"), "property"), (key(graph, "C.__eq__"), "dunder")])
    # __init__ / __call__ 有调用语法（C(...)、c(...)），user 没有无法判断目标的调用：三个都是不符
    assert sorted(m.detail for m in r.mismatches) == sorted(explicit)


# ---------- 裁决 6：图过期 ----------

def test_stale_graph_is_refused(tmp_path, capsys):
    graph, _ = project(tmp_path, "def f():\n    return 1\ndef g():\n    return 2\n")
    g, t = write_inputs(tmp_path, graph, [rec(graph, "f", [2])])
    (tmp_path / "app/mod.py").write_text("def f():\n    return 3\ndef g():\n    return 2\n", encoding="utf-8")
    with pytest.raises(StaleGraphError) as e:
        verify(graph, [])
    assert (e.value.changed, e.value.unreadable, e.value.files) == (["app/mod.py"], [], ["app/mod.py"])
    assert main(["verify", g, t]) == 2
    err = capsys.readouterr().err
    assert "图已过期，以下文件在分析后发生了变化:\n  app/mod.py" in err and "读不到" not in err
    assert main(["verify", g, t, "--allow-stale"]) == 0
    (tmp_path / "app/mod.py").unlink()  # 文件没了也算过期，与内容变了分开报告
    with pytest.raises(StaleGraphError) as e:
        verify(graph, [])
    assert (e.value.changed, e.value.unreadable) == ([], ["app/mod.py"])
    capsys.readouterr()
    assert main(["verify", g, t]) == 2
    err = capsys.readouterr().err
    assert "以下文件读不到" in err and "app/mod.py" in err and "发生了变化" not in err
    assert verify(graph, [rec(graph, "f", [2])], allow_stale=True).functions_seen == 1


def test_graph_root_is_absolute_and_verify_works_from_another_directory(tmp_path, monkeypatch, capsys):
    make_project(tmp_path / "proj", {"app/mod.py": "def f():\n    return 1\n"})
    for d in ("work", "other"):
        (tmp_path / d).mkdir()
    monkeypatch.chdir(tmp_path / "work")
    assert main(["analyze", "../proj", "-o", "g.json"]) == 0  # 相对路径
    graph = from_json((tmp_path / "work/g.json").read_text(encoding="utf-8"))
    assert graph.root == str((tmp_path / "proj").resolve())
    assert build_index(Path("../proj"), ProjectConfig()).root == (tmp_path / "proj").resolve()
    (tmp_path / "work/t.jsonl").write_text(json.dumps(rec(graph, "f", [2])) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "other")  # 换一个工作目录核对：图过期检查和源码都按图里的绝对根目录找
    capsys.readouterr()
    assert main(["verify", "../work/g.json", "../work/t.jsonl"]) == 0
    assert "执行到的函数: 1" in capsys.readouterr().out


# ---------- 裁决 7：图里没有的函数、插件错误 ----------

def test_unknown_functions_and_plugin_errors_are_counted(tmp_path, capsys):
    graph, _ = project(tmp_path, MISSING)
    ghost = {"fn": "app/gone.py::nope@1", "test": "", "lines": [2], "exit": "return", "exc": None,
             "calls": [], "n": 5}
    records = [{"error": "RuntimeError: 注入的故障"}, ghost, ghost | {"test": "t::b"},
               {"fn": "app/gone.py::other@9", "test": "", "lines": [], "exit": "return", "exc": None,
                "calls": [], "n": 1},
               rec(graph, "h", [5, 6, 13, 14])]
    r = verify(graph, records)
    assert r.ok and r.unknown_functions == 2 and r.functions_seen == 1
    assert r.plugin_errors == 1 and r.plugin_error_list == ["RuntimeError: 注入的故障"]
    assert main(["verify", *write_inputs(tmp_path, graph, records)]) == 2  # 插件出过错：记录不完整，不能算通过
    cap = capsys.readouterr()
    assert "图里没有的函数: 2" in cap.out and "插件错误: 1" in cap.out and "RuntimeError: 注入的故障" in cap.out
    assert "插件" in cap.err


def test_trace_version_header_is_accepted_and_newer_versions_are_refused(tmp_path, capsys):
    graph, _ = project(tmp_path, MISSING)
    good = rec(graph, "h", [5, 6, 13, 14])
    header = {"v": 1, "format": "pystepmap-trace"}
    for records in ([header, good], [good], [header, good, header]):  # 有没有版本头都行；多个进程同时开头时可能有几行
        assert main(["verify", *write_inputs(tmp_path, graph, records)]) == 0
    capsys.readouterr()
    assert main(["verify", *write_inputs(tmp_path, graph, [{"v": 2, "format": "pystepmap-trace"}, good])]) == 2
    assert "版本" in capsys.readouterr().err
    (tmp_path / "list.json").write_text("[]")
    assert main(["verify", str(tmp_path / "list.json"), str(tmp_path / "t.jsonl")]) == 2
    assert "list.json" in capsys.readouterr().err


def test_verify_does_not_pass_vacuously(tmp_path, capsys):
    """记录是空的、只有插件错误、或者没有一个函数对得上图：核对什么也没核对，退出码 2 并说明原因（一行）。"""
    graph, _ = project(tmp_path, MISSING)
    ghost = {"fn": "app/gone.py::nope@1", "test": "", "lines": [2], "exit": "return", "exc": None, "calls": [], "n": 1}
    for records in ([], [{"error": "RuntimeError: boom"}], [ghost]):
        assert main(["verify", *write_inputs(tmp_path, graph, records)]) == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1, err
    assert main(["verify", *write_inputs(tmp_path, graph, [rec(graph, "h", [5, 6, 13, 14])])]) == 0


# ---------- CPython 在复合语句上的行事件：真实记录不能报出假不符 ----------

TRICKY = {"app/k.py": """
    from contextlib import contextmanager
    @contextmanager
    def ctx():
        yield
    def a(*x):
        return 1
    def boom():
        raise ValueError("x")
    def with_normal():
        with ctx():
            a()
        a()
    def multi_with():
        with (
            ctx() as c1,
            ctx() as c2,
        ):
            a()
        return a()
    def with_return():
        with ctx():
            return a()
    def with_break(xs):
        for x in xs:
            with ctx():
                a(x)
                break
        a()
    def except_nomatch():
        try:
            try:
                boom()
            except KeyError:
                a()
            except (TypeError,
                    IndexError):
                a()
        except ValueError:
            a()
    def one_liners(x, xs):
        if x: return a()
        for y in xs: a(y)
        a()
    def while_true():
        i = 0
        while True:
            i += 1
            a()
            if i > 2:
                break
        a()
    def break_in_finally(xs):
        for x in xs:
            try:
                a(x)
                break
            finally:
                a()
        a()
    def continue_in_finally(xs):
        for x in xs:
            try:
                a(x)
                continue
            finally:
                a()
            a()
        a()
    def return_in_with_try():
        with ctx():
            try:
                return a()
            finally:
                a()
    def match_one_liners(cmd):
        match cmd:
            case 1: return a(1)
            case 2: return a(2)
        a()
"""}


def test_line_events_of_compound_statements_stay_on_graph(tmp_path):
    graph, recs = traced(tmp_path, TRICKY, """
        from app import k
        def test_a():
            k.with_normal()
            k.multi_with()
            k.with_return()
            k.with_break([1, 2])
            k.except_nomatch()
            k.one_liners(False, [1, 2])
            k.one_liners(True, [])
            k.while_true()
            k.break_in_finally([1, 2])
            k.continue_in_finally([1, 2])
            k.return_in_with_try()
            for cmd in (1, 2, 3):
                k.match_one_liners(cmd)
    """)
    # with 的 __exit__ 回到头部行、没匹配上的 except / case 头部行、一行里有多个节点：都不能报成不符
    assert [r for r in recs if "error" in r] == []
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.uncaught_exits == 0
    # 没匹配上的 except 头部不算进入捕获节点：否则会多出近似入口，打开的近似区域还会原谅后面真正的缺连线；
    # break_in_finally、continue_in_finally 经过 finally 再到跳转目标，连线都在图上
    assert r.approx_entries == 0 and r.approx_list == []


# ---------- match：判断节点，每个 case 一条出边 ----------

MATCHES = {"app/m.py": """
    def a(*x):
        return 1
    def ok(x):
        return x > 0
    def boom(x):
        raise ValueError(x)
    def three(cmd):
        match cmd:
            case 1:
                a(1)
            case 2:
                a(2)
            case 3:
                a(3)
            case _:
                a(0)
        a()
    def no_default(cmd):
        match cmd:
            case "go":
                a(1)
            case "stop":
                a(2)
        a()
    def guarded(cmd):
        match cmd:
            case int(n) if ok(n):
                a(1)
            case int(n) if n < -5:
                a(2)
            case str():
                a(3)
        a()
    def long_guard(cmd):
        match cmd:
            case [x, y] if (
                ok(x)
            ):
                a(1)
            case _:
                a(2)
    def alternatives(cmd):
        match cmd:
            case "a" | "b":
                a(1)
            case [x, *_] | {"k": x}:
                a(x)
        a()
    def empty_body(cmd):
        match cmd:
            case 1:
                pass
            case 2:
                a(2)
        a()
    def in_loop(xs):
        for x in xs:
            match x:
                case 0:
                    break
                case 1:
                    continue
                case 2:
                    a(2)
                    break
                case _:
                    a(x)
            a()
        a(0)
    def in_try(cmd):
        try:
            match cmd:
                case 1:
                    return a(1)
                case 2:
                    a(2)
            a()
        finally:
            a(0)
    def loop_try(xs):
        for x in xs:
            try:
                match x:
                    case 0:
                        break
                    case 1:
                        continue
                    case _:
                        a(x)
            finally:
                a(0)
        a()
    def in_finally(xs, mode):
        for x in xs:
            try:
                if x:
                    continue
            finally:
                match mode:
                    case 1:
                        pass
                    case 2:
                        a(2)
            a()
        a(0)
    def last(cmd):
        a()
        match cmd:
            case 1:
                a(1)
            case 2:
                a(2)
    def one_line(cmd):
        match cmd:
            case 1: return a(1)
            case int(n) if n > 10: return a(n)
            case int(n) if ok(n): a(n)
        a()
    def subject_call(cmd):
        match ok(cmd):
            case True:
                a(1)
            case False:
                a(2)
    def guard_raises(cmd):
        try:
            match cmd:
                case 1 if boom(cmd):
                    a(1)
                case _:
                    a(2)
        except ValueError:
            a(0)
"""}
MATCHES_TEXT = textwrap.dedent(MATCHES["app/m.py"]).lstrip("\n")
MATCHES_TEST = """
    from app import m
    def test_a():
        for cmd in (1, 2, 3, 9):
            m.three(cmd)
        for cmd in ("go", "stop", "x"):
            m.no_default(cmd)
        for cmd in (3, -1, -9, "s", 2.0):  # 第一个守卫成立 / 两个守卫都不成立 / 第二个守卫成立 / 第三个分支 / 都不匹配
            m.guarded(cmd)
        for cmd in ([1, 2], [-1, 2], 5):
            m.long_guard(cmd)
        for cmd in ("a", "b", [7], {"k": 1}, 0):
            m.alternatives(cmd)
        for cmd in (1, 2, 3):
            m.empty_body(cmd)
        m.in_loop([3, 1, 0])
        m.in_loop([1, 2, 5])
        m.in_loop([])
        for cmd in (1, 2, 3):
            m.in_try(cmd)
        m.loop_try([5, 1, 0])
        m.loop_try([1])
        m.in_finally([0, 1], 1)
        m.in_finally([1, 0], 2)
        m.in_finally([0], 3)
        for cmd in (1, 2, 3):
            m.last(cmd)
        for cmd in (1, 11, 5, -1, "s"):
            m.one_line(cmd)
        for cmd in (1, -1):
            m.subject_call(cmd)
        for cmd in (1, 2):
            m.guard_raises(cmd)
"""
MATCH_SHAPES = ("three", "no_default", "guarded", "long_guard", "alternatives", "empty_body", "in_loop", "in_try",
                "loop_try", "in_finally", "last", "one_line", "subject_call", "guard_raises")


def test_match_paths_from_real_records_are_on_graph(tmp_path):
    """match 是判断节点：match 行是节点行，没匹配上的 case 头部行照旧去掉（守卫不成立的也是），守卫不成立后落到
    后面的分支、case 写在一行等真实路径都在图上，不需要近似。"""
    graph, recs = traced(tmp_path, MATCHES, MATCHES_TEST)
    assert [r for r in recs if "error" in r] == []
    r = verify(graph, recs)
    assert r.ok, r.mismatches
    assert r.approx_entries == 0 and r.approx_list == [] and r.uncaught_exits == 0
    assert r.steps_total == r.steps_on_graph
    seen = {parse_key(x["fn"])[1] for x in recs}
    assert set(MATCH_SHAPES) <= seen
    # 每种形状都走了不止一条路径
    paths = {}
    for x in recs:
        paths.setdefault(parse_key(x["fn"])[1], set()).add(tuple(x["lines"]))
    assert all(len(paths[name]) >= 2 for name in MATCH_SHAPES), paths
    # 守卫不成立：没进入的 case 头部行出现在原始行号里（与 case 写在一行的节点行一样可以不经过）
    t = MATCHES_TEXT
    m_line, c1, c2, body2 = (at_def(t, "guarded", s) for s in ("match cmd", "case int(n) if ok", "case int(n) if n",
                                                                  "a(2)"))
    [neg9] = [x for x in recs if x["fn"] == key(graph, "guarded") and body2 in x["lines"]]
    assert neg9["lines"][:4] == [m_line, c1, c2, body2]


def test_missing_case_edges_are_reported_on_real_records(tmp_path):
    """反方向：删掉判断节点的某条出边（case、"都不匹配"、守卫不成立后进入的分支、改成 break 的 case），走到那个
    分支的真实记录报 edge 不符，不能被近似或别的连线放过。"""
    graph, recs = traced(tmp_path, MATCHES, MATCHES_TEST)
    t = MATCHES_TEXT

    def fresh():
        return from_json(to_json(graph))

    def only(qualname):
        return [x for x in recs if x.get("fn") == key(graph, qualname)]

    cases = [
        ("three", at_def(t, "three", "match cmd"), at_def(t, "three", "a(2)")),  # case 2
        ("no_default", at_def(t, "no_default", "match cmd"), at_def(t, "no_default", "a()")),  # 都不匹配
        ("guarded", at_def(t, "guarded", "match cmd"), at_def(t, "guarded", "a(2)")),  # 第一个守卫不成立、第二个成立
        ("in_loop", at_def(t, "in_loop", "match x"), at_def(t, "in_loop", "a(0)")),  # case 0: break
        ("in_try", at_def(t, "in_try", "match cmd"), at_def(t, "in_try", "return a(1)")),  # case 1: return（try 体里）
        ("in_finally", at_def(t, "in_finally", "match mode"), at_def(t, "in_finally", "a(2)")),  # finally 体里
    ]
    for qualname, src, dst in cases:
        r = verify(without_edge(fresh(), qualname, src, dst), only(qualname))
        assert kinds(r) == [("edge", src, dst)] and r.approx_list == [], (qualname, r.mismatches, r.approx_list)


# ---------- 命令行 ----------

def test_cli_output_and_exit_code(tmp_path, capsys):
    graph, _ = project(tmp_path, MISSING)
    assert main(["verify", *write_inputs(tmp_path, graph, [rec(graph, "h", [5, 6, 13, 14])])]) == 0
    out = capsys.readouterr().out
    for label in ("图中函数: 4", "执行到的函数: 1", "不同的真实路径: 1", "走过的步数: 3", "在图上: 3",
                  "异常与收尾入口（近似）: 0", "未捕获异常退出: 0",
                  "隐式调用（属性、魔术方法）: 0", "动态调用（静态未确定目标）: 0", "截断的记录: 0", "图里没有的函数: 0", "插件错误: 0",
                  "连线不在图上: 0", "结局不在图上: 0", "调用不在图上: 0"):
        assert label in out, label
    assert "出口（近似）" not in out
    bad = [rec(graph, "h", [5, 13, 14], n=2), rec(graph, "h", [5], calls=[(key(graph, "c"), 5)])]
    assert main(["verify", *write_inputs(tmp_path, graph, bad)]) == 1
    out = capsys.readouterr().out
    assert "连线不在图上: 2" in out and "结局不在图上: 1" in out and "调用不在图上: 1" in out
    edge_part = out[out.index("== 连线不在图上"):]
    assert "第 5 行 → 第 13 行" in edge_part and "a()" in edge_part and "b()" in edge_part
    assert out.index("== 连线不在图上") < out.index("== 结局不在图上") < out.index("== 调用不在图上")


def test_cli_unreadable_inputs_exit_2(tmp_path, capsys):
    graph, _ = project(tmp_path, MISSING)
    g, t = write_inputs(tmp_path, graph, [rec(graph, "h", [5, 6, 13, 14])])
    assert main(["verify", str(tmp_path / "nope.json"), t]) == 2
    assert "无法读取图文件" in capsys.readouterr().err
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert main(["verify", str(tmp_path / "bad.json"), t]) == 2
    assert "无法读取图文件" in capsys.readouterr().err
    assert main(["verify", g, str(tmp_path / "nope.jsonl")]) == 2
    assert "无法读取记录文件" in capsys.readouterr().err
    with open(t, "a", encoding="utf-8") as f:
        f.write('{"fn": "x", "lines": [1], "ex')
    assert main(["verify", g, t]) == 2
    err = capsys.readouterr().err
    assert "无法读取记录文件" in err and "第 2 行" in err


# ---------- 阶段 6：新格式的记录（ns、测试结果行）；walk_record ----------


def stripped(records):
    """去掉这一阶段加的东西：执行记录的 ns、测试结果行。"""
    return [{k: v for k, v in r.items() if k != "ns"} for r in records if "fn" in r or "error" in r]


def test_new_format_records_give_the_same_report_as_without_the_additions(tmp_path, capsys):
    recs = run_traced(tmp_path, CONTROL, """
        import pytest
        from app.t import lp, wf
        @pytest.mark.parametrize("x", ["ok", "value", "key"])
        def test_wf(x):
            wf(x)
        def test_type():
            with pytest.raises(TypeError):
                wf("type")
        def test_loop():
            lp(4)
        def test_fails():
            assert wf("type") == "ok"
    """, returncode=1, results=True)
    graph = from_json((tmp_path / "graph.json").read_text(encoding="utf-8"))
    assert {r["outcome"] for r in recs if "outcome" in r} == {"passed", "failed"}
    assert all("ns" in r for r in recs if "fn" in r)
    old = stripped(recs)
    assert verify(graph, recs) == verify(graph, old) and verify(graph, recs).ok
    # 记录文件经 read_records 读进来、经命令行核对：输出逐字相同
    (tmp_path / "old").mkdir()
    g_new, t_new = write_inputs(tmp_path, graph, [{"v": 1, "format": "pystepmap-trace"}, *recs])
    g_old, t_old = write_inputs(tmp_path / "old", graph, old)
    assert list(read_records(t_new)) == recs
    capsys.readouterr()
    assert main(["verify", g_new, t_new]) == main(["verify", g_old, t_old]) == 0
    out = capsys.readouterr().out
    half = len(out) // 2
    assert out[:half] == out[half:]


def test_read_records_accepts_new_lines_and_checks_them(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    good = rec(graph, "h", [5, 6, 13, 14], ns=[1, 0, 2, 3])
    result = {"test": "tests/t.py::t", "outcome": "failed", "seconds": 0.5, "n": 1}
    future = {"kind": "something-new", "x": 1}  # 以后加的、没有 fn 的行：跳过
    _, t = write_inputs(tmp_path, graph, [good, result, future, good | {"extra": [1]}])
    assert list(read_records(t)) == [good, result, good | {"extra": [1]}]
    for bad in (good | {"ns": [1, 2]}, good | {"ns": [1, -1, 2, 3]}, good | {"ns": [1, 0.5, 2, 3]},
                good | {"ns": None}, [1, 2], "text", 3):
        _, t = write_inputs(tmp_path, graph, [good, bad])
        with pytest.raises(RecordError, match="第 2 行"):
            list(read_records(t))
    # 只加不改：不认识的结果（以后加的 outcome、字段不像结果行的）和没有 fn / error 的对象都跳过，不报错
    for unknown in (result | {"outcome": "rerun"}, result | {"seconds": -1}, result | {"seconds": True},
                    result | {"test": None}, result | {"n": 0}, {"outcome": "passed"}, {}):
        _, t = write_inputs(tmp_path, graph, [good, unknown, result])
        assert list(read_records(t)) == [good, result]
    _, t = write_inputs(tmp_path, graph, [good])
    with open(t, "a", encoding="utf-8") as f:
        f.write("not json\n")
    with pytest.raises(RecordError, match="第 2 行不是合法的 JSON"):
        list(read_records(t))
    assert verify(graph, [result, good]) == verify(graph, [stripped([good])[0]])


WALK = '''
def a(): ...
def b(): ...
def c(): ...
def straight():
    a()
    x = 1
    b()
    return x
def branch(p):
    if p:
        a()
    else:
        b()
    c()
def loop(xs):
    for x in xs:
        a()
    b()
def caught():
    try:
        a()
    except ValueError:
        b()
        return 0
    return 1
def uncaught():
    a()
    b()
'''


def node(graph, qualname, line_no):
    """函数里第 line_no 行上唯一的节点（开始节点除外）的 id。"""
    [n] = [n.id for n in graph.functions[key(graph, qualname)].nodes if n.line == line_no and n.type != "start"]
    return n


def implicit_end(graph, qualname, end_kind):
    """隐含的结局：ok 是执行到函数末尾，exception 是未捕获异常退出。"""
    [n] = [n.id for n in graph.functions[key(graph, qualname)].nodes if n.line == 0 and n.end_kind == end_kind]
    return n


def start(graph, qualname):
    return graph.functions[key(graph, qualname)].nodes[0].id


def test_walk_record_gives_nodes_line_indexes_and_exit_node(tmp_path):
    graph, text = project(tmp_path, WALK)
    def L(needle, nth=1):
        return line(text, needle, nth)

    def implicit(q):
        return implicit_end(graph, q, "ok")

    def uncaught_end(q):
        return implicit_end(graph, q, "exception")

    w = walk_record(graph, rec(graph, "straight", [L("a()"), L("x = 1"), L("b()"), L("return x")]))
    assert isinstance(w, Walked)
    assert w.nodes == (start(graph, "straight"), node(graph, "straight", L("a()")), node(graph, "straight", L("b()")),
                       node(graph, "straight", L("return x")))
    assert w.line_index == (0, 0, 2, 3)  # x = 1 不出节点：算在 a() 那一段里
    assert (w.exit_node, w.on_graph, w.truncated) == (w.nodes[-1], True, False)

    w = walk_record(graph, rec(graph, "branch", [L("if p:"), L("b()", 2), L("c()")]))
    assert w.nodes == (start(graph, "branch"), node(graph, "branch", L("if p:")), node(graph, "branch", L("b()", 2)),
                       node(graph, "branch", L("c()")), implicit("branch"))
    assert w.line_index == (0, 0, 1, 2, 3) and w.exit_node == implicit("branch") and w.on_graph

    f, body = L("for x in xs:"), L("a()", 3)
    w = walk_record(graph, rec(graph, "loop", [f, body, f, body, f, body, f, L("b()", 3)]))
    lp, a_ = node(graph, "loop", f), node(graph, "loop", body)
    assert w.nodes == (start(graph, "loop"), lp, a_, lp, a_, lp, a_, lp, node(graph, "loop", L("b()", 3)),
                       implicit("loop"))
    assert w.line_index == (0, 0, 1, 2, 3, 4, 5, 6, 7, 8) and w.exit_node == implicit("loop") and w.on_graph

    w = walk_record(graph, rec(graph, "caught", [L("try:"), L("a()", 4), L("except ValueError"), L("b()", 4),
                                                 L("return 0")]))
    assert w.nodes == (start(graph, "caught"), node(graph, "caught", L("a()", 4)),
                       node(graph, "caught", L("except ValueError")), node(graph, "caught", L("b()", 4)),
                       node(graph, "caught", L("return 0")))
    assert w.line_index == (0, 1, 2, 3, 4) and w.exit_node == w.nodes[-1] and w.on_graph

    w = walk_record(graph, rec(graph, "uncaught", [L("a()", 5)], exit="raise"))
    assert w.nodes == (start(graph, "uncaught"), node(graph, "uncaught", L("a()", 5)))
    assert w.exit_node == uncaught_end("uncaught") and w.on_graph  # 未捕获异常退出没有入边，不放进 nodes

    # 截断：不知道停在哪里；走不上图：on_graph 为假，能走的照走
    w = walk_record(graph, rec(graph, "loop", [f, body, f, body], truncated=True))
    assert (w.nodes, w.exit_node, w.truncated, w.on_graph) == ((start(graph, "loop"), lp, a_, lp, a_), "", True, True)
    w = walk_record(graph, rec(graph, "straight", [L("b()"), L("return x")]))
    assert not w.on_graph and w.nodes[-1] == w.exit_node == node(graph, "straight", L("return x"))
    assert walk_record(graph, {**rec(graph, "straight", []), "fn": "app/gone.py::f@1"}) is None

    # cache：批量调用复用；结果和不带 cache 时相同
    cache: dict = {}
    r = rec(graph, "loop", [f, body, f, L("b()", 3)])
    assert walk_record(graph, r, cache) == walk_record(graph, r, cache) == walk_record(graph, r)


def test_read_records_accepts_the_exception_type_on_result_lines(tmp_path):
    graph, _ = project(tmp_path, MISSING)
    good = rec(graph, "h", [5, 6, 13, 14])
    failed = {"test": "tests/t.py::t", "outcome": "failed", "seconds": 0.5, "n": 1, "exc": "ValueError"}
    _, t = write_inputs(tmp_path, graph, [good, failed, failed | {"exc": 3}, failed | {"exc": ["x"]}])
    assert list(read_records(t)) == [good, failed]  # exc 不是字符串的结果行不认识：跳过
    assert verify(graph, [good, failed]) == verify(graph, [good])
