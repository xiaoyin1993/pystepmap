"""运行路径：执行记录 → 每个测试走过的节点、次数、耗时、停点，以及没有测试走到的结局（tmp_path 里的小项目 + 手写记录）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import CORE_SRC, make_project
from pystepmap.analyze import analyze_project
from pystepmap.model import FunctionGraph, Node, ProjectGraph, SelfCheck, to_json
from pystepmap.runpath import (
    FunctionRun,
    FunctionShape,
    NodeStat,
    Outcome,
    RunPaths,
    TestRun,
    _base,
    build_run_paths,
    covered_summary,
    entry_functions,
    function_shape,
    locate_function,
    locate_test_run,
    match_function_run,
    run_paths_from_json,
    run_paths_to_json,
    uncovered_outcomes,
)
from pystepmap.verify import read_records, verify

SRC = '''
from app.api_helpers import check, helper

@router.post("/a")
def entry_a(x):
    if check(x):
        return "ok"
    return "bad"

@router.post("/b")
def entry_b(x):
    helper(x)
    return "done"
'''

HELPERS = '''
def check(x):
    return x > 0

def helper(x):
    if x > 1:
        raise ValueError("big")

def a(): ...
def b(): ...
def c(): ...

def straight():
    a()
    x = 1
    b()
    return x

def timed():
    a()
    x = 1
    y = 2
    b()

def loop(xs):
    for x in xs:
        a()
    b()

def branch(p):
    if p:
        a()
    else:
        b()
    c()

def boom():
    a()
    b()
'''

T = "tests/test_x.py::"


def project(tmp_path, shift: int = 0):
    """两条功能线（/a、/b）加几个手写记录用的函数；shift：在两个文件开头多加几行注释（函数整体往下挪）。"""
    pad = "# 挪一行\n" * shift
    make_project(tmp_path, {"app/__init__.py": "", "app/api.py": pad + textwrap.dedent(SRC),
                            "app/api_helpers.py": pad + textwrap.dedent(HELPERS)})
    return analyze_project(tmp_path)


def fn_of(graph, qualname):
    [f] = [f for f in graph.functions.values() if f.qualname == qualname]
    return f


def ln(graph, qualname, needle, nth=1):
    """函数里第 nth 个（去掉缩进后）以 needle 开头的行的行号。"""
    f = fn_of(graph, qualname)
    rows = (graph_root_text(graph, f.file)).splitlines()
    hits = [i for i in range(f.line, f.end_line + 1) if rows[i - 1].strip().startswith(needle)]
    return hits[nth - 1]


def fn_node(graph, qualname, needle):
    [n] = [n for n in fn_of(graph, qualname).nodes if n.line == ln(graph, qualname, needle) and n.type != "start"]
    return n


def graph_root_text(graph, rel):
    return (Path(graph.root) / rel).read_text(encoding="utf-8")


def sid(graph, qualname, needle=None, *, end_kind=None, nth=1):
    """节点 sid：needle 给出时是那一行上的节点（一行一个），否则是开始节点 / 隐含结局（按 end_kind）。"""
    f = fn_of(graph, qualname)
    if needle is not None:
        [n] = [n for n in f.nodes if n.line == ln(graph, qualname, needle, nth) and n.type != "start"]
    elif end_kind is not None:
        [n] = [n for n in f.nodes if n.line == 0 and n.end_kind == end_kind]
    else:
        n = f.nodes[0]
    return n.sid


def rec(graph, qualname, lines, *, exit="return", exc=None, test=T + "test_a", n=1, ns=None, **extra):
    r = {"fn": fn_of(graph, qualname).key, "test": test, "lines": list(lines), "exit": exit, "exc": exc,
         "calls": [], "n": n, **extra}
    if ns is not None:
        r["ns"] = list(ns)
    return r


def result(test, outcome="passed", seconds=0.25, n=1):
    return {"test": test, "outcome": outcome, "seconds": seconds, "n": n}


def run_of(r: RunPaths, test: str, graph, qualname) -> FunctionRun:
    [t] = [t for t in r.tests if t.test == test]
    return t.functions[fn_of(graph, qualname).sid]


def test_one_function_run_once(tmp_path):
    g = project(tmp_path)
    q = "straight"
    lines = [ln(g, q, "a()"), ln(g, q, "x = 1"), ln(g, q, "b()"), ln(g, q, "return x")]
    r = build_run_paths(g, [rec(g, q, lines, ns=[5, 6, 7, 8]), result(T + "test_a")])
    f = run_of(r, T + "test_a", g, q)
    start, a, b, end = sid(g, q), sid(g, q, "a()"), sid(g, q, "b()"), sid(g, q, "return x")
    assert (f.function, f.executions, f.off_graph, f.has_time) == (fn_of(g, q).sid, 1, 0, True)
    assert f.nodes == {start: NodeStat(1, 0), a: NodeStat(1, 11), b: NodeStat(1, 7), end: NodeStat(1, 8)}
    assert f.edges == {f"{start}|{a}": 1, f"{a}|{b}": 1, f"{b}|{end}": 1}
    assert f.exits == {f"{end}|return|": 1}
    assert f.first_path == [start, a, b, end]
    [t] = r.tests
    assert (t.test, t.outcome, t.seconds, t.cases) == (T + "test_a", "passed", 0.25, 1)


def test_loop_three_rounds(tmp_path):
    """循环体 3 次；回边（循环体 → 循环节点，图上是 loop_back）走 3 次：每圈结束都回到循环节点，第 4 次到循环节点时退出。"""
    g = project(tmp_path)
    q = "loop"
    f_, body = ln(g, q, "for x in xs:"), ln(g, q, "a()")
    r = build_run_paths(g, [rec(g, q, [f_, body, f_, body, f_, body, f_, ln(g, q, "b()")])])
    f = run_of(r, T + "test_a", g, q)
    lp, a, b, end = sid(g, q, "for x in xs:"), sid(g, q, "a()"), sid(g, q, "b()"), sid(g, q, end_kind="ok")
    assert {k: v.visits for k, v in f.nodes.items()} == {sid(g, q): 1, lp: 4, a: 3, b: 1, end: 1}
    assert f.edges == {f"{sid(g, q)}|{lp}": 1, f"{lp}|{a}": 3, f"{a}|{lp}": 3, f"{lp}|{b}": 1, f"{b}|{end}": 1}
    kinds = {(e.src, e.dst): e.kind for e in fn_of(g, q).edges}
    ids = {n.sid: n.id for n in fn_of(g, q).nodes}
    assert kinds[(ids[a], ids[lp])] == "loop_back"
    assert f.exits == {f"{end}|return|": 1} and f.has_time is False  # 没有 ns 的旧记录


def test_time_goes_to_the_node_and_the_statements_after_it(tmp_path):
    """ns 为 [10, 20, 30, 40]，第 2、3 行不出节点：第一个节点得 60；函数开头到第一个节点之前的算在开始节点上。"""
    g = project(tmp_path)
    q = "timed"
    lines = [ln(g, q, "a()"), ln(g, q, "x = 1"), ln(g, q, "y = 2"), ln(g, q, "b()")]
    r = build_run_paths(g, [rec(g, q, lines, ns=[10, 20, 30, 40], n=2)])
    f = run_of(r, T + "test_a", g, q)
    assert f.nodes[sid(g, q, "a()")] == NodeStat(2, 60) and f.nodes[sid(g, q, "b()")] == NodeStat(2, 40)
    assert f.nodes[sid(g, q)] == NodeStat(2, 0) and f.nodes[sid(g, q, end_kind="ok")] == NodeStat(2, 0)
    assert f.executions == 2 and f.has_time
    # 函数开头有不出节点的行：算在开始节点上
    r = build_run_paths(g, [rec(g, "straight", [ln(g, "straight", "x = 1"), ln(g, "straight", "b()"),
                                                ln(g, "straight", "return x")], ns=[3, 4, 5])])
    f = run_of(r, T + "test_a", g, "straight")
    assert f.nodes[sid(g, "straight")].ns == 3 and f.off_graph == 1  # 跳过了 a()：走不上图，照样汇总


def test_two_executions_on_different_branches(tmp_path):
    g = project(tmp_path)
    q = "branch"
    cond, c = ln(g, q, "if p:"), ln(g, q, "c()")
    r = build_run_paths(g, [rec(g, q, [cond, ln(g, q, "a()"), c]), rec(g, q, [cond, ln(g, q, "b()"), c])])
    f = run_of(r, T + "test_a", g, q)
    visits = {k: v.visits for k, v in f.nodes.items()}
    assert f.executions == 2 and visits[sid(g, q, "if p:")] == 2 and visits[sid(g, q, "c()")] == 2
    assert visits[sid(g, q, "a()")] == 1 and visits[sid(g, q, "b()")] == 1
    assert f.first_path == [sid(g, q), sid(g, q, "if p:"), sid(g, q, "a()"), sid(g, q, "c()"), sid(g, q, end_kind="ok")]


def test_exception_exit(tmp_path):
    g = project(tmp_path)
    r = build_run_paths(g, [rec(g, "boom", [ln(g, "boom", "a()")], exit="raise", exc="ValueError", n=3),
                            rec(g, "helper", [ln(g, "helper", "if x > 1:"), ln(g, "helper", "raise")],
                                exit="raise", exc="ValueError")])
    f = run_of(r, T + "test_a", g, "boom")
    assert f.exits == {"end:uncaught|raise|ValueError": 3} and f.off_graph == 0
    assert sid(g, "boom", end_kind="exception") == "end:uncaught" and "end:uncaught" not in f.nodes
    h = run_of(r, T + "test_a", g, "helper")
    assert h.exits == {f"{sid(g, 'helper', 'raise')}|raise|ValueError": 1}


def test_unknown_functions_are_skipped_and_off_graph_runs_are_counted(tmp_path):
    g = project(tmp_path)
    ghost = {"fn": "app/gone.py::f@1", "test": T + "test_a", "lines": [2], "exit": "return", "exc": None,
             "calls": [], "n": 4}
    q = "straight"
    bad = rec(g, q, [ln(g, q, "b()"), ln(g, q, "return x")], n=2)  # 跳过了 a()：开始 → b() 没有连线
    r = build_run_paths(g, [ghost, {"error": "RuntimeError: x"}, bad, result(T + "test_a")])
    [t] = r.tests
    assert list(t.functions) == [fn_of(g, q).sid]
    f = t.functions[fn_of(g, q).sid]
    assert f.off_graph == 2 and f.executions == 2
    assert f.nodes[sid(g, q, "b()")].visits == 2 and f.exits == {f"{sid(g, q, 'return x')}|return|": 2}


def test_results_are_summed_per_test_and_executions_outside_tests_are_kept(tmp_path):
    g = project(tmp_path)
    q = "straight"
    good = [ln(g, q, "a()"), ln(g, q, "b()"), ln(g, q, "return x")]
    r = build_run_paths(g, [
        rec(g, q, good, test=""),  # 收集阶段（导入时）的执行
        rec(g, q, good, test=T + "test_p"), result(T + "test_p", "passed", 0.5),
        rec(g, q, good, test=T + "test_p"), result(T + "test_p", "failed", 0.25),
        result(T + "test_q", "skipped", 0.0), result(T + "test_r", "error", 0.125),
    ])
    assert [(t.test, t.outcome, t.cases, t.seconds) for t in r.tests] == [
        ("", "", 0, 0.0), (T + "test_p", "failed", 2, 0.75), (T + "test_q", "skipped", 1, 0.0),
        (T + "test_r", "error", 1, 0.125)]
    assert r.tests[1].functions[fn_of(g, q).sid].executions == 2 and r.tests[2].functions == {}


def test_first_path_is_capped_and_size_does_not_grow_with_rounds(tmp_path):
    g = project(tmp_path)
    q = "loop"
    f_, body, b = ln(g, q, "for x in xs:"), ln(g, q, "a()"), ln(g, q, "b()")

    def paths(rounds):
        return build_run_paths(g, [rec(g, q, [f_, body] * rounds + [f_, b], ns=[1] * (2 * rounds + 2))])

    f = run_of(paths(1000), T + "test_a", g, q)
    assert len(f.first_path) == 500 and f.first_path[-1] == "…" and f.nodes[sid(g, q, "a()")].visits == 1000
    sizes = [len(json.dumps(run_paths_to_json(paths(k)), ensure_ascii=False)) for k in (1000, 5000)]
    assert abs(sizes[0] - sizes[1]) < 20  # 只有数字变长


def test_uncovered_outcomes(tmp_path):
    g = project(tmp_path)
    r = build_run_paths(g, [
        rec(g, "entry_a", [ln(g, "entry_a", "if check(x):"), ln(g, "entry_a", 'return "ok"')]),
        rec(g, "check", [ln(g, "check", "return")]),
        rec(g, "entry_b", [ln(g, "entry_b", "helper(x)"), ln(g, "entry_b", "return")]),
        rec(g, "helper", [ln(g, "helper", "if x > 1:")]),  # 执行到函数末尾
    ])
    got = uncovered_outcomes(g, r)
    assert [(o.function, o.node) for o in got] == [("app/api.py::entry_a", sid(g, "entry_a", 'return "bad"')),
                                                  ("app/api_helpers.py::helper", sid(g, "helper", "raise"))]
    bad, boom = got
    assert isinstance(bad, Outcome) and bad.title == fn_node(g, "entry_a", 'return "bad"').title
    assert (bad.file, bad.line, bad.end_kind) == ("app/api.py", ln(g, "entry_a", 'return "bad"'), "ok")
    assert (boom.file, boom.end_kind) == ("app/api_helpers.py", "failed")
    assert all(o.node != "end:uncaught" for o in got)  # 未捕获异常退出不算
    line_a, line_b = (next(x for x in g.lines if x.id == fn_of(g, q).key) for q in ("entry_a", "entry_b"))
    assert [o.node for o in uncovered_outcomes(g, r, line_a)] == [sid(g, "entry_a", 'return "bad"')]
    assert [o.node for o in uncovered_outcomes(g, r, line_b)] == [sid(g, "helper", "raise")]
    # 一个测试都没跑：线上每个函数的每个结局（含执行到函数末尾）都没走到
    everything = uncovered_outcomes(g, RunPaths([]))
    assert len(everything) == 6 and sid(g, "helper", end_kind="ok") in {o.node for o in everything}
    assert covered_summary(g, r) == {"functions_run": 4, "functions_on_lines": 4, "outcomes": 6,
                                     "outcomes_covered": 4, "tests": 1, "failed": 0, "outcomes_uncertain": 0}


def test_summary_counts_tests_and_failures(tmp_path):
    g = project(tmp_path)
    r = build_run_paths(g, [result(T + "a", "passed"), result(T + "b", "failed"), result(T + "c", "error"),
                            rec(g, "straight", [ln(g, "straight", "a()")], test="")])
    s = covered_summary(g, r)
    # failed 含出错的测试；functions_run 是执行过、图里有的函数（不管在不在功能线上）
    assert (s["tests"], s["failed"], s["functions_run"], s["functions_on_lines"]) == (3, 2, 1, 4)


def test_json_round_trip_and_bad_input(tmp_path):
    g = project(tmp_path)
    q = "loop"
    f_, body = ln(g, q, "for x in xs:"), ln(g, q, "a()")
    r = build_run_paths(g, [rec(g, q, [f_, body, f_, ln(g, q, "b()")], ns=[1, 2, 3, 4]), result(T + "test_a")])
    d = run_paths_to_json(r)
    assert json.loads(json.dumps(d)) == d and d["version"] == 1
    assert run_paths_from_json(json.loads(json.dumps(d))) == r
    fr = d["tests"][0]["functions"][fn_of(g, q).sid]
    assert set(fr) == {"function", "executions", "nodes", "edges", "exits", "first_path", "off_graph", "has_time"}
    assert set(d) == {"version", "tests", "shapes", "function_dups"}
    assert set(d["shapes"][fn_of(g, q).sid]) == {"shape", "dups"}
    assert set(next(iter(fr["nodes"].values()))) == {"visits", "ns"}
    for bad in (None, [], {"version": 2, "tests": []}, {"version": 1}, {"version": 1, "tests": [{"test": 1}]},
                {"version": 1, "tests": [d["tests"][0] | {"outcome": None}]},
                {"version": 1, "tests": [d["tests"][0] | {"functions": {"x": {"function": "x"}}}]},
                {"version": 1, "tests": [d["tests"][0] | {"functions": {"x": fr | {"nodes": {"n": {"visits": -1,
                                                                                                    "ns": 0}}}}}]}):
        with pytest.raises(ValueError, match="运行路径"):
            run_paths_from_json(bad)


def test_run_paths_are_found_again_after_the_code_moves(tmp_path):
    """按 sid 存：两个文件开头各多三行、函数整体往下挪三行，重新分析后旧的运行路径里的每个函数 sid、节点 sid 都在新图里
    找得到同样的节点（类型、标题相同），没走到的结局还是那两个，只是行号多了 3。"""
    g1 = project(tmp_path)
    records = [rec(g1, "entry_a", [ln(g1, "entry_a", "if check(x):"), ln(g1, "entry_a", 'return "ok"')]),
               rec(g1, "check", [ln(g1, "check", "return")]),
               rec(g1, "loop", [ln(g1, "loop", "for x in xs:"), ln(g1, "loop", "a()"), ln(g1, "loop", "for x in xs:"),
                                ln(g1, "loop", "b()")]),
               rec(g1, "entry_b", [ln(g1, "entry_b", "helper(x)"), ln(g1, "entry_b", "return")]),
               rec(g1, "helper", [ln(g1, "helper", "if x > 1:")])]
    r = run_paths_from_json(json.loads(json.dumps(run_paths_to_json(build_run_paths(g1, records)))))
    g2 = project(tmp_path, shift=3)
    assert fn_of(g2, "loop").line == fn_of(g1, "loop").line + 3
    for t in r.tests:
        for fsid, f in t.functions.items():
            old = {n.sid: (n.type, n.title) for n in g1.functions[g1.sids[fsid]].nodes}
            new = {n.sid: (n.type, n.title) for n in g2.functions[g2.sids[fsid]].nodes}
            assert {s: new[s] for s in f.nodes} == {s: old[s] for s in f.nodes}
            new_ids = {n.sid: n.id for n in g2.functions[g2.sids[fsid]].nodes}
            new_edges = {(e.src, e.dst) for e in g2.functions[g2.sids[fsid]].edges}
            for pair in f.edges:
                a, b = pair.split("|")
                assert (new_ids[a], new_ids[b]) in new_edges, pair
    u1, u2 = uncovered_outcomes(g1, r), uncovered_outcomes(g2, r)
    assert [(o.function, o.node, o.title) for o in u1] == [(o.function, o.node, o.title) for o in u2]
    assert [o.line + 3 for o in u1] == [o.line for o in u2]


def test_dataclass_shapes():
    """字段名和顺序是给后续任务（服务端、界面）的约定。"""
    assert list(NodeStat.__dataclass_fields__) == ["visits", "ns"]
    assert list(FunctionRun.__dataclass_fields__) == ["function", "executions", "nodes", "edges", "exits",
                                                      "first_path", "off_graph", "has_time", "calls", "icalls"]
    assert list(TestRun.__dataclass_fields__) == ["test", "outcome", "seconds", "cases", "functions", "exc", "excs"]
    assert list(RunPaths.__dataclass_fields__) == ["tests", "version", "shapes", "function_dups"]
    assert list(FunctionShape.__dataclass_fields__) == ["shape", "dups"]
    assert list(Outcome.__dataclass_fields__) == ["function", "node", "title", "end_kind", "file", "line", "uncertain", "added"]


# ---------- 端到端：转账示例的测试挂上插件跑一遍 ----------

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "transfer"


def test_transfer_example_end_to_end(tmp_path):
    """复制示例、分析、子进程里带插件跑它自己的测试（python -m pytest tests），读记录：核对没有不符；成功的测试经过
    "执行转账"节点，余额不足的没有；没有测试走到的结局正好是故意留着的"超过单笔限额，已拒绝"。"""
    proj = tmp_path / "transfer"
    shutil.copytree(EXAMPLE, proj, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    graph = analyze_project(proj)
    assert all(not f.file.startswith("tests/") for f in graph.functions.values())  # tests 目录默认不分析
    graph_file, out = tmp_path / "graph.json", tmp_path / "trace.jsonl"
    graph_file.write_text(to_json(graph), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYSTEPMAP_", "PYTEST_"))}
    env |= {"PYSTEPMAP_GRAPH": str(graph_file), "PYSTEPMAP_TRACE_OUT": str(out), "PYTHONPATH": str(CORE_SRC),
            "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "pystepmap.trace", "-p", "no:cacheprovider", "-q", "tests"],
                       cwd=proj, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "pystepmap 记录：" in p.stdout
    records = list(read_records(out))
    report = verify(graph, records)
    assert [m.kind for m in report.mismatches] == [] and report.plugin_errors == 0 and report.unknown_functions == 0
    r = build_run_paths(graph, records)
    t = "tests/test_transfer.py::"
    by_test = {x.test: x for x in r.tests}
    assert by_test[t + "test_invalid_request_is_rejected"].cases == 4
    assert all(x.outcome == "passed" for x in r.tests if x.test)
    run = fn_of(graph, "TransferWorkflow.run")
    [execute] = [n.sid for n in run.nodes if n.title == "执行转账"]
    ok = by_test[t + "test_transfer_succeeds"].functions[run.sid]
    poor = by_test[t + "test_insufficient_balance_is_rejected"].functions[run.sid]
    assert ok.nodes[execute].visits == 1 and ok.has_time and ok.nodes[execute].ns > 0
    assert execute not in poor.nodes
    [end_ok] = [k for k in ok.exits]
    assert end_ok.endswith("|return|") and next(n.title for n in run.nodes if n.sid == end_ok.split("|")[0]) == "转账完成"
    [left] = uncovered_outcomes(graph, r)
    assert (left.function, left.title, left.end_kind) == (run.sid, "超过单笔限额，已拒绝", "failed")
    # 测试自己调用的（入口）：建 FakeBank、建工作流、asyncio.run 启动 run；步骤、判断、fee_of 都是流程里调用的
    entries = entry_functions(by_test[t + "test_transfer_succeeds"])
    assert entries == sorted(entries) and set(entries) == {
        "transfer_workflow.py::FakeBank.sample", "transfer_workflow.py::TransferWorkflow.__init__", run.sid}
    assert ok.calls["transfer_workflow.py::TransferWorkflow.execute_transfer"] == 1
    s = covered_summary(graph, r)
    assert s["outcomes_covered"] == s["outcomes"] - 1 and s["failed"] == 0 and s["tests"] == 6


# ---------- 评审第 1 轮：同一行上几个节点的耗时；#n 的配对；JSON 的新字段 ----------

SAME_LINE = {"app/__init__.py": "", "app/s.py": """
def a(): ...
def ret_call():
    return a()
def guard_call(p):
    if p: return a()
    return 1
def guard_const(p):
    if p: return 0
    a()
"""}


def test_nodes_sharing_a_line_give_the_time_to_the_last_non_end_node(tmp_path):
    """return f() 的时间算在步骤上；if c: return f() 也是步骤；if c: return 0 算在判断上；结局只在独占一行时有时间。"""
    make_project(tmp_path, SAME_LINE)
    g = analyze_project(tmp_path)
    cases = (("ret_call", "return a()", "step"), ("guard_call", "if p: return a()", "step"),
             ("guard_const", "if p: return 0", "cond"))
    for q, needle, owner in cases:
        r = build_run_paths(g, [rec(g, q, [ln(g, q, needle)], ns=[100])])
        f = run_of(r, T + "test_a", g, q)
        on_line = [n for n in fn_of(g, q).nodes if n.line == ln(g, q, needle) and n.type != "start"]
        assert len(on_line) >= 2 and any(n.type == "end" for n in on_line)
        assert {n.type: f.nodes[n.sid].ns for n in on_line} == {n.type: 100 if n.type == owner else 0 for n in on_line}
        assert all(v.visits == 1 for v in f.nodes.values()), q
    r = build_run_paths(g, [rec(g, "guard_call", [ln(g, "guard_call", "if p:"), ln(g, "guard_call", "return 1")],
                                ns=[30, 70])])
    f = run_of(r, T + "test_a", g, "guard_call")
    cond = next(n.sid for n in fn_of(g, "guard_call").nodes if n.type == "cond")
    assert f.nodes[sid(g, "guard_call", "return 1")].ns == 70  # 独占一行的结局照常有时间
    assert f.nodes[cond].ns == 30


GUARDS = """
from pystepkit import workflow


@workflow(name="W", title="W")
class W:
    def run(self, x):
{body}
"""
TWO_GUARDS = """\
        if x == 1:
            return None
        if x == 2:
            return None
        return 0"""


def guards(tmp_path, body):
    make_project(tmp_path, {"wf.py": GUARDS.format(body=body)})
    return analyze_project(tmp_path)


def guard_records(g):
    q = "W.run"
    first, second, last = (ln(g, q, s) for s in ("if x == 1:", "if x == 2:", "return 0"))
    return [rec(g, q, [first, second, second + 1]), rec(g, q, [first, second, last])]  # run(2)、run(3)


def test_outcomes_added_after_the_run_are_uncertain_when_the_graph_of_the_run_is_given(tmp_path):
    """运行之后重新分析：新写的函数、老函数里新加的结局在那次运行时还不存在。给了运行时的图（then）时它们是说不准，
    不算没走到；老函数里当时就没走到的照旧。不给 then 时分不出来，都算没走到。"""
    g1 = project(tmp_path)
    r = build_run_paths(g1, [
        rec(g1, "entry_a", [ln(g1, "entry_a", "if check(x):"), ln(g1, "entry_a", 'return "ok"')]),
        rec(g1, "check", [ln(g1, "check", "return")]),
    ])
    old_missed = sid(g1, "entry_a", 'return "bad"')
    src = textwrap.dedent(SRC).replace('        return "ok"\n', '        if fresh(x):\n            return "new"\n        return "ok"\n', 1)
    assert 'return "new"' in src
    make_project(tmp_path, {"app/api.py": src + "\n\ndef fresh(x):\n    return x > 1\n"})
    g2 = analyze_project(tmp_path)
    line_a = next(x for x in g2.lines if x.id == fn_of(g2, "entry_a").key)
    added, fresh = sid(g2, "entry_a", 'return "new"'), fn_of(g2, "fresh")
    without = {(o.function, o.node): o.uncertain for o in uncovered_outcomes(g2, r, line_a)}
    assert without[(fresh.sid, fresh.nodes[-2].sid)] is False and without[("app/api.py::entry_a", added)] is False
    got = {(o.function, o.node): o.uncertain for o in uncovered_outcomes(g2, r, line_a, then=g1)}
    assert set(got) == set(without)  # 列出来的还是这些，只是分了类
    assert got[("app/api.py::entry_a", added)] is True  # 老函数里新加的结局
    assert all(u for (f, _), u in got.items() if f == fresh.sid) and any(f == fresh.sid for f, _ in got)  # 新函数
    assert got[("app/api.py::entry_a", old_missed)] is False  # 当时就有、当时就没走到
    # 说不准的原因单独记：运行之后才有的是 added，当时就有的不是；不给 then 时没有一个是
    new = {(o.function, o.node): o.added for o in uncovered_outcomes(g2, r, line_a, then=g1)}
    assert new[("app/api.py::entry_a", added)] is True and new[("app/api.py::entry_a", old_missed)] is False
    assert all(a for (f, _), a in new.items() if f == fresh.sid)
    assert not any(o.added for o in uncovered_outcomes(g2, r, line_a))
    assert uncovered_outcomes(g1, r, then=g1) == uncovered_outcomes(g1, r)  # 同一张图：没有区别


def test_old_runs_on_a_newer_graph_never_light_up_the_wrong_same_text_node(tmp_path):
    """在两个同文本的守卫上面再插一个同文本的守卫：#n 全都挪了一位。旧运行里的 end:…#2（x == 2 那个）不能按相等配到新图
    的 #2（x == 1 那个）上：三个"return None"都标成不确定，什么都不会被错标成走到了；return 0 照常算走到。"""
    g1 = guards(tmp_path, TWO_GUARDS)
    r = build_run_paths(g1, guard_records(g1))
    run = r.tests[0].functions[fn_of(g1, "W.run").sid]
    shape = r.shapes[fn_of(g1, "W.run").sid]
    nones = [n for n in fn_of(g1, "W.run").nodes if n.title == fn_node(g1, "W.run", "return None").title and n.type == "end"]
    base = nones[0].sid
    assert shape == FunctionShape(function_shape(fn_of(g1, "W.run")), [base]) and r.function_dups == []
    # 同一张图：逐个按相等配上
    mapping, missing = match_function_run(fn_of(g1, "W.run"), run, shape)
    assert missing == 0 and set(mapping) == set(run.nodes)
    assert [(o.node, o.uncertain) for o in uncovered_outcomes(g1, r)] == [(base, False)]  # x == 1 的那个没走到
    # 插一个同文本的守卫
    g2 = guards(tmp_path, "        if x == 0:\n            return None\n" + TWO_GUARDS)
    fg2 = fn_of(g2, "W.run")
    mapping, missing = match_function_run(fg2, run, shape)
    assert missing == 1 and f"{base}#2" not in mapping  # 旧的 #2 配不上，不算到别的节点上
    assert mapping[sid(g2, "W.run", "return 0")] == fn_node(g2, "W.run", "return 0").id
    got = uncovered_outcomes(g2, r)
    assert [(o.node, o.uncertain) for o in got] == [(base, True), (f"{base}#2", True), (f"{base}#3", True)]
    s = covered_summary(g2, r)
    assert (s["outcomes"], s["outcomes_covered"], s["outcomes_uncertain"]) == (4, 1, 3)


def test_reformatting_keeps_the_shape_and_exact_matching(tmp_path):
    """只改格式（注释、换行）：节点 sid 的序列不变，同文本的 #n 照样按相等配，结果和原图一样确定。"""
    g1 = guards(tmp_path, TWO_GUARDS)
    r = build_run_paths(g1, guard_records(g1))
    run = r.tests[0].functions[fn_of(g1, "W.run").sid]
    shape = r.shapes[fn_of(g1, "W.run").sid]
    g2 = guards(tmp_path, "        # 先看是不是 1\n\n" + TWO_GUARDS.replace("if x == 2:", "if x == 2:  # 是不是 2"))
    assert function_shape(fn_of(g2, "W.run")) == shape.shape
    mapping, missing = match_function_run(fn_of(g2, "W.run"), run, shape)
    assert missing == 0 and len(mapping) == len(run.nodes)
    base = shape.dups[0]
    assert [(o.node, o.uncertain, o.line) for o in uncovered_outcomes(g2, r)] == \
        [(base, False, ln(g2, "W.run", "return None"))]


def test_a_node_whose_sid_changed_is_unmatched_and_counted(tmp_path):
    g1 = project(tmp_path)
    q = "straight"
    r = build_run_paths(g1, [rec(g1, q, [ln(g1, q, "a()"), ln(g1, q, "b()"), ln(g1, q, "return x")])])
    run = r.tests[0].functions[fn_of(g1, q).sid]
    b_sid = sid(g1, q, "b()")
    helpers = (tmp_path / "app/api_helpers.py").read_text(encoding="utf-8")
    (tmp_path / "app/api_helpers.py").write_text(helpers.replace("    b()\n    return x", "    b(1)\n    return x"),
                                                 encoding="utf-8")
    g2 = analyze_project(tmp_path)
    mapping, missing = match_function_run(fn_of(g2, q), run, r.shapes[fn_of(g1, q).sid])
    assert missing == 1 and b_sid not in mapping
    assert set(mapping) == set(run.nodes) - {b_sid}
    assert all(mapping[s] == next(n.id for n in fn_of(g2, q).nodes if n.sid == s) for s in mapping)


def test_json_without_the_new_fields_still_loads_and_functions_are_sorted(tmp_path):
    g = project(tmp_path)
    r = build_run_paths(g, [rec(g, "straight", [ln(g, "straight", "a()")]), rec(g, "loop", [ln(g, "loop", "b()")]),
                            rec(g, "boom", [ln(g, "boom", "a()")])])
    d = run_paths_to_json(r)
    assert list(d["tests"][0]["functions"]) == sorted(d["tests"][0]["functions"])
    assert list(d["shapes"]) == sorted(d["shapes"])
    assert run_paths_from_json(json.loads(json.dumps(d))) == r
    # 第 1 轮的形状：每个函数自己带 shape / dups（读的时候忽略），运行一级没有 shapes / function_dups
    first_round = json.loads(json.dumps(d))
    del first_round["shapes"], first_round["function_dups"]
    for f in first_round["tests"][0]["functions"].values():
        f |= {"shape": "0" * 40, "dups": []}
    # 最早的形状：两样都没有
    oldest = json.loads(json.dumps(d))
    del oldest["shapes"], oldest["function_dups"]
    for data in (first_round, oldest):
        old = run_paths_from_json(data)
        assert (old.shapes, old.function_dups) == ({}, []) and old.tests == r.tests
        # 不知道运行时的形状：不重名的函数、节点照样按相等配上
        placed, missing = locate_test_run(g, old, old.tests[0])
        assert missing == 0 and set(placed) == {fn_of(g, q).key for q in ("straight", "loop", "boom")}
    for bad in ({"shapes": {"x": {"shape": 1, "dups": []}}}, {"shapes": {"x": {"shape": "s"}}}, {"shapes": []},
                {"function_dups": "x"}, {"function_dups": [1]}):
        with pytest.raises(ValueError, match="运行路径"):
            run_paths_from_json(json.loads(json.dumps(d)) | bad)


# ---------- 评审第 2 轮：同名函数（#n）在两次分析之间的配对 ----------

OVERLOADS = """
from typing import overload

from pystepkit import workflow


@workflow(name="W", title="W")
class W:
    def run(self, x):
        return self.step(x)
{stubs}
    def step(self, x):
        if x == 1:
            return 1
        return 2
"""
STUB = """
    @overload
    def step(self, x: {t}) -> {t}: ...
"""


def overloads(tmp_path, types):
    make_project(tmp_path, {"wf.py": OVERLOADS.format(stubs="".join(STUB.format(t=t) for t in types))})
    return analyze_project(tmp_path)


def by_sid(graph, fsid):
    return graph.functions[graph.sids[fsid]]


def rec_of(fg, lines, test=T + "test_a", **kw):
    return {"fn": fg.key, "test": test, "lines": list(lines), "exit": "return", "exc": None, "calls": [], "n": 1, **kw}


def test_overload_stubs_inserted_before_the_implementation_do_not_take_its_run(tmp_path):
    """评审的 fdup：两个 @overload 桩后面是真正的实现（W.step#3），跑过它的 return 1。前面再加一个桩重新分析，实现变成
    W.step#4、W.step#3 成了桩：运行要按形状找回实现，不能叠到桩上；实现的 return 1 仍算走到。"""
    g1 = overloads(tmp_path, ["bytes", "int"])
    impl1 = by_sid(g1, "wf.py::W.step#3")
    first = next(n.line for n in impl1.nodes if n.type == "cond")
    r = build_run_paths(g1, [rec_of(impl1, [first, first + 1]),
                             rec_of(fn_of(g1, "W.run"), [fn_of(g1, "W.run").line + 1])])
    assert r.function_dups == ["wf.py::W.step"]
    assert set(r.shapes) == {"wf.py::W.run", "wf.py::W.step", "wf.py::W.step#2", "wf.py::W.step#3"}
    # 同一张图：都按相等放上
    placed, missing = locate_test_run(g1, r, r.tests[0])
    assert missing == 0 and set(placed) == {fn_of(g1, "W.run").key, impl1.key}
    left1 = [(o.function, o.title, o.uncertain) for o in uncovered_outcomes(g1, r)]
    assert ("wf.py::W.step#3", "返回 2", False) in left1 and not any(t == "返回 1" for _, t, _ in left1)

    g2 = overloads(tmp_path, ["bytes", "int", "str"])
    impl2 = by_sid(g2, "wf.py::W.step#4")
    assert locate_function(g2, r, "wf.py::W.step#3") is impl2  # 按形状找回实现
    assert locate_function(g2, r, "wf.py::W.run") is fn_of(g2, "W.run")
    placed, missing = locate_test_run(g2, r, r.tests[0])
    assert missing == 0 and set(placed) == {fn_of(g2, "W.run").key, impl2.key}  # 桩上什么都不亮
    fg, mapping = placed[impl2.key]
    assert fg is impl2 and {impl2.nodes[0].id} <= set(mapping.values())
    left2 = [(o.function, o.title, o.uncertain) for o in uncovered_outcomes(g2, r)]
    assert ("wf.py::W.step#4", "返回 2", False) in left2
    assert not any(f == "wf.py::W.step#4" and t == "返回 1" for f, t, _ in left2)  # 走到的不会成了"确定没走到"


def test_a_setter_is_found_again_after_a_deleter_is_inserted_before_it(tmp_path):
    """property 的 getter（C.v）和 setter（C.v#2），只跑了 setter。在 setter 前面加一个 deleter：deleter 成了 C.v#2、
    setter 成了 C.v#3；运行按形状找回 setter，不会落到 deleter 上。"""
    src = """
class C:
    @property
    def v(self):
        return self._v
{deleter}
    @v.setter
    def v(self, x):
        if x < 0:
            raise ValueError("负数")
        self._v = x
"""
    deleter = """
    @v.deleter
    def v(self):
        del self._v
"""
    make_project(tmp_path, {"app/__init__.py": "", "app/p.py": src.format(deleter="")})
    g1 = analyze_project(tmp_path)
    setter1 = by_sid(g1, "app/p.py::C.v#2")
    cond = next(n.line for n in setter1.nodes if n.type == "cond")
    r = build_run_paths(g1, [rec_of(setter1, [cond, cond + 2])])
    assert r.function_dups == ["app/p.py::C.v"]
    make_project(tmp_path, {"app/p.py": src.format(deleter=deleter)})
    g2 = analyze_project(tmp_path)
    setter2 = by_sid(g2, "app/p.py::C.v#3")
    assert by_sid(g2, "app/p.py::C.v#2").line < setter2.line  # deleter 在前
    assert locate_function(g2, r, "app/p.py::C.v#2") is setter2
    placed, missing = locate_test_run(g2, r, r.tests[0])
    assert missing == 0 and list(placed) == [setter2.key]
    _, mapping = placed[setter2.key]
    assert set(mapping) == set(r.tests[0].functions["app/p.py::C.v#2"].nodes)


ROUTES = """
import sys
{extra}
if sys.version_info >= (3,):
    @router.post("/p")
    def entry(x):
        return 1
else:
    @router.post("/p")
    def entry(x):
        return 1
"""


def test_identical_sibling_definitions_are_exact_until_the_group_changes(tmp_path):
    """同一文件里两处定义完全一样（形状相同）：图没变（这一组同名定义的 sid 和形状都一样）时按相等放上；这一组变了
    （再加一个一样的）时分不清是哪一个——放不上，这一组每个函数的结局都标成说不准。"""
    make_project(tmp_path, {"app/__init__.py": "", "app/d.py": ROUTES.format(extra="")})
    g1 = analyze_project(tmp_path)
    first, second = by_sid(g1, "app/d.py::entry"), by_sid(g1, "app/d.py::entry#2")
    assert function_shape(first) == function_shape(second)
    r = build_run_paths(g1, [rec_of(first, [first.line + 1])])
    placed, missing = locate_test_run(g1, r, r.tests[0])
    assert missing == 0 and list(placed) == [first.key]
    assert [(o.function, o.uncertain) for o in uncovered_outcomes(g1, r)] == [("app/d.py::entry#2", False)]
    s = covered_summary(g1, r)
    assert (s["functions_run"], s["outcomes"], s["outcomes_covered"], s["outcomes_uncertain"]) == (1, 2, 1, 0)

    extra = 'if sys.version_info >= (4,):\n    @router.post("/p")\n    def entry(x):\n        return 1\n'
    make_project(tmp_path, {"app/d.py": ROUTES.format(extra=extra)})
    g2 = analyze_project(tmp_path)
    assert locate_function(g2, r, "app/d.py::entry") is None
    placed, missing = locate_test_run(g2, r, r.tests[0])
    assert (placed, missing) == ({}, 1)
    got = sorted((o.function, o.uncertain) for o in uncovered_outcomes(g2, r))  # 同标题的几条线按图里的顺序
    assert got == [("app/d.py::entry", True), ("app/d.py::entry#2", True), ("app/d.py::entry#3", True)]
    s = covered_summary(g2, r)
    assert (s["functions_run"], s["outcomes_covered"], s["outcomes_uncertain"]) == (0, 0, 3)


def test_shapes_are_stored_once_per_function(tmp_path):
    """1000 个测试 × 23 个函数：每个函数运行时的形状在 JSON 里只出现一次（不随测试个数重复）。"""
    body = "".join(f"def f{i}(x):\n    if x:\n        g()\n    return x\n\n" for i in range(23))
    make_project(tmp_path, {"app/__init__.py": "", "app/many.py": "def g(): ...\n\n" + body})
    g = analyze_project(tmp_path)
    fns = [fn_of(g, f"f{i}") for i in range(23)]
    records = [rec_of(f, [f.line + 1, f.line + 2, f.line + 3], test=f"{T}test_{k}", ns=[5, 7, 9])
               for k in range(1000) for f in fns]
    r = build_run_paths(g, records)
    text = json.dumps(run_paths_to_json(r), ensure_ascii=False)
    assert len(r.tests) == 1000 and len(r.shapes) == 23
    assert all(f.off_graph == 0 for t in r.tests for f in t.functions.values())
    assert text.count('"shape":') == 23  # 每个函数一份（23 个函数的形状恰好都一样，所以数键）
    per_test = len(json.dumps(run_paths_to_json(RunPaths(r.tests[:1])), ensure_ascii=False))
    assert len(text) < 1000 * per_test + 23 * 200


# ---------- 评审收尾：没有语句节点的"空"形状不能用来找回 ----------

TRIVIAL_V1 = """from pystepkit import workflow


def norm(v):
    return v


def audit(v):
    return v


@workflow(name="W", title="W")
class W:
    def __init__(self):
        self._v = 0

    def run(self, x):
        self.v = x
        return self.v

    @property
    def v(self):
        if self._v:
            return norm(self._v)
        return 0

    @v.setter
    def v(self, x):
        self._v = x
"""
TRIVIAL_V2 = """from pystepkit import workflow


def norm(v):
    return v


def audit(v):
    return v


@workflow(name="W", title="W")
class W:
    def __init__(self):
        self._v = 0

    def run(self, x):
        self.v = x
        return self.v

    @property
    def v(self):
        if self._v:
            return norm(self._v)
        return 0

    @v.deleter
    def v(self):
        self._v = None

    @v.setter
    def v(self, x):
        self._v = audit(x)
"""


def test_a_run_of_a_function_without_statement_nodes_is_not_relocated_by_shape(tmp_path):
    """评审的 triv：只跑了一个没有语句节点的 setter（形状只有开始、执行到函数末尾、未捕获异常退出——所有这样的函数都
    一样）。之后 setter 加了校验（形状变了），前面插了一个同样"空"的 deleter：不能凭空形状把旧运行放到 deleter 上，
    放不上、计数；这一组没变时（同一张图）照常按相等放。"""
    make_project(tmp_path, {"wf.py": TRIVIAL_V1})
    g1 = analyze_project(tmp_path)
    setter1 = by_sid(g1, "wf.py::W.v#2")
    assert {n.sid for n in setter1.nodes} == {"start", "end:implicit", "end:uncaught"}
    r = build_run_paths(g1, [rec_of(setter1, [setter1.line + 1])])
    placed, missing = locate_test_run(g1, r, r.tests[0])
    assert missing == 0 and list(placed) == [setter1.key]  # 同一张图：这一组没变，照常放上
    make_project(tmp_path, {"wf.py": TRIVIAL_V2})
    g2 = analyze_project(tmp_path)
    deleter2, setter2 = by_sid(g2, "wf.py::W.v#2"), by_sid(g2, "wf.py::W.v#3")
    assert function_shape(deleter2) == r.shapes["wf.py::W.v#2"].shape != function_shape(setter2)
    assert locate_function(g2, r, "wf.py::W.v#2") is None
    assert locate_test_run(g2, r, r.tests[0]) == ({}, 1)
    s = covered_summary(g2, r)
    assert s["functions_run"] == 0


# ---------- S12：记录里真实的调用、让测试失败的异常类型、测试的入口函数 ----------

CALLS = {"app/__init__.py": "", "app/c.py": '''
def helper(x):
    return x

def step(x):
    return helper(x)

def flow(x):
    y = step(x)
    z = helper(y)
    return helper(z)

def countdown(n):
    if n:
        return countdown(n - 1)
    return 0
'''}


def test_real_calls_exceptions_and_entry_functions(tmp_path):
    make_project(tmp_path, CALLS)
    g = analyze_project(tmp_path)
    flow, step, helper, countdown = (fn_of(g, q) for q in ("flow", "step", "helper", "countdown"))
    line = {q: ln(g, q, s) for q, s in (("flow", "y = step"), ("step", "return helper"), ("countdown", "if n:"))}
    records = [
        # flow 在两行调用 helper：同一条记录里只算一次；n = 2
        rec_of(flow, [line["flow"], line["flow"] + 1, line["flow"] + 2], n=2,
               calls=[[step.key, line["flow"]], [helper.key, line["flow"] + 1], [helper.key, line["flow"] + 2],
                      ["app/gone.py::x@1", line["flow"]]]),
        rec_of(step, [line["step"]], n=2, calls=[[helper.key, line["step"]]]),
        rec_of(helper, [helper.line + 1], n=6),
        rec_of(countdown, [line["countdown"], line["countdown"] + 1], calls=[[countdown.key, line["countdown"] + 1]]),
        rec_of(countdown, [line["countdown"], line["countdown"] + 2]),
        result(T + "test_a", "failed", 0.1) | {"exc": "ValueError"},
        result(T + "test_a", "failed", 0.1) | {"exc": "AssertionError"},
        result(T + "test_a", "passed", 0.1),
        result(T + "test_b", "error", 0.1) | {"exc": "RuntimeError"},
        result(T + "test_c", "passed", 0.1),
    ]
    r = build_run_paths(g, records)
    a, b, c = r.tests
    fs = a.functions
    assert fs[flow.sid].calls == {step.sid: 2, helper.sid: 2}  # 图里没有的被调函数不记
    assert fs[step.sid].calls == {helper.sid: 2} and fs[helper.sid].calls == {}
    assert fs[countdown.sid].calls == {countdown.sid: 1}
    assert (a.outcome, a.exc, a.excs) == ("failed", "ValueError", ["AssertionError", "ValueError"])
    assert (b.outcome, b.exc, b.excs) == ("error", "RuntimeError", ["RuntimeError"])
    assert (c.outcome, c.exc, c.excs) == ("passed", "", [])
    # 入口：执行过、却没有别的执行过的函数记录过调用它的（自己调自己不算）
    assert entry_functions(a) == sorted([countdown.sid, flow.sid])
    assert entry_functions(b) == entry_functions(c) == []
    # JSON：来回一致；空的 calls / exc / excs 不写出来；没有这几项的旧数据照样读
    d = run_paths_to_json(r)
    assert run_paths_from_json(json.loads(json.dumps(d))) == r
    ta, _, tc = d["tests"]
    assert (ta["exc"], ta["excs"]) == ("ValueError", ["AssertionError", "ValueError"])
    assert "exc" not in tc and "excs" not in tc and "calls" not in ta["functions"][helper.sid]
    assert ta["functions"][flow.sid]["calls"] == {step.sid: 2, helper.sid: 2}
    old = json.loads(json.dumps(d))
    for t in old["tests"]:
        t.pop("exc", None)
        t.pop("excs", None)
        for f in t["functions"].values():
            f.pop("calls", None)
    back = run_paths_from_json(old)
    assert all((t.exc, t.excs) == ("", []) for t in back.tests)
    assert all(f.calls == {} for t in back.tests for f in t.functions.values())
    for bad in ({"exc": 1}, {"excs": "ValueError"}, {"excs": [1]}):
        broken = json.loads(json.dumps(d))
        broken["tests"][0] |= bad
        with pytest.raises(ValueError, match="运行路径"):
            run_paths_from_json(broken)
    broken = json.loads(json.dumps(d))
    broken["tests"][0]["functions"][flow.sid]["calls"] = {step.sid: -1}
    with pytest.raises(ValueError, match="运行路径"):
        run_paths_from_json(broken)


def test_indirect_calls_are_kept_apart_and_count_for_entry_functions(tmp_path):
    """S14：icalls（经项目以外的帧的调用）和 calls 分开汇总，同样的规则；入口函数 = 没有执行过的函数直接或间接调用过的。
    JSON 里空的 icalls 不写出来；没有 icalls 的旧数据照样读。"""
    make_project(tmp_path, CALLS)
    g = analyze_project(tmp_path)
    flow, step, helper = (fn_of(g, q) for q in ("flow", "step", "helper"))
    line = ln(g, "flow", "y = step")
    records = [
        # flow 经库的包装函数调用 step（间接），直接调用 helper
        rec_of(flow, [line, line + 1, line + 2], n=3, calls=[[helper.key, line + 1]],
               icalls=[[step.key, line], [step.key, line + 2], ["app/gone.py::x@1", line]]),
        rec_of(step, [ln(g, "step", "return helper")], n=3),
        rec_of(helper, [helper.line + 1], n=3),
        result(T + "test_a", "failed", 0.1) | {"exc": "ValueError"},
    ]
    r = build_run_paths(g, records)
    (a,) = r.tests
    assert a.functions[flow.sid].calls == {helper.sid: 3}
    assert a.functions[flow.sid].icalls == {step.sid: 3}  # 在两行间接调用也只算一次；图里没有的不记
    assert a.functions[step.sid].icalls == {}
    assert entry_functions(a) == [flow.sid]  # step 只被间接调用过：不是入口
    d = run_paths_to_json(r)
    assert run_paths_from_json(json.loads(json.dumps(d))) == r
    fs = d["tests"][0]["functions"]
    assert fs[flow.sid]["icalls"] == {step.sid: 3} and "icalls" not in fs[step.sid]
    old = json.loads(json.dumps(d))
    for f in old["tests"][0]["functions"].values():
        f.pop("icalls", None)
    back = run_paths_from_json(old)
    assert all(f.icalls == {} for f in back.tests[0].functions.values())
    assert entry_functions(back.tests[0]) == sorted([flow.sid, step.sid])  # 旧数据：只按直接调用
    broken = json.loads(json.dumps(d))
    broken["tests"][0]["functions"][flow.sid]["icalls"] = {step.sid: "3"}
    with pytest.raises(ValueError, match="运行路径"):
        run_paths_from_json(broken)
    # 写坏了的 icalls（不是 [key, 行号]）忽略，不让整份运行读不了
    r2 = build_run_paths(g, [rec_of(flow, [line], icalls=[["x"], 5, [step.key, line]])])
    assert r2.tests[0].functions[flow.sid].icalls == {step.sid: 1}


# ---------- 和界面共用的用例（core/tests/fixtures/placement/*.json、entries.json；web 的 runplace.test.ts 读同一批文件） ----------

FIXTURES = Path(__file__).parent / "fixtures"


def _node_type(sid: str) -> str:
    head = sid.split(":", 1)[0].split("#", 1)[0]
    return head if head in ("start", "end", "step", "cond", "loop", "catch") else "step"


def _corpus_graph(functions: dict) -> ProjectGraph:
    """用例里压缩过的图（函数 key → sid、文件、限定名、行、节点 [id, sid]）→ 只够放置用的 ProjectGraph。"""
    fns = {k: FunctionGraph(k, f["file"], f["qualname"], f["line"], f["line"], "function", f["qualname"], False,
                            sid=f["sid"], nodes=[Node(i, _node_type(s), i, sid=s) for i, s in f["nodes"]])
           for k, f in functions.items()}
    return ProjectGraph(1, "", fns, [], [], SelfCheck([], [], 0, 0, []), {}, {f.sid: k for k, f in fns.items()})


@pytest.mark.parametrize("path", sorted((FIXTURES / "placement").glob("*.json")), ids=lambda p: p.stem)
def test_shared_placement_corpus(path):
    """重名组的 8 个用例：运行（在 g1 上记的）叠到 g1、g2 上的放置和用例里写的一样；按测试切开（shapes 只留这个测试
    的函数和重名组的全部定义）也一样。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    paths = run_paths_from_json(doc["run"])
    dups = set(paths.function_dups)
    for g, functions in doc["graphs"].items():
        graph = _corpus_graph(functions)
        for t in paths.tests:
            want = doc["expect"][f"{g}|{t.test}"]
            sliced = RunPaths([t], paths.version, {s: v for s, v in paths.shapes.items()
                                                   if s in t.functions or _base(s) in dups}, paths.function_dups)
            for p in (paths, sliced):
                placed, missing = locate_test_run(graph, p, t)
                got = {"unmatched": missing, "placed": {k: dict(sorted(m.items())) for k, (_, m) in sorted(placed.items())}}
                assert got == want, (doc["name"], g)


def test_shared_entry_functions_corpus():
    doc = json.loads((FIXTURES / "entries.json").read_text(encoding="utf-8"))
    assert len(doc["cases"]) >= 5
    for case in doc["cases"]:
        r = run_paths_from_json({"version": 1, "tests": [case["test"]]})
        assert entry_functions(r.tests[0]) == case["entries"], case["name"]
