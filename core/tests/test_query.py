"""反查节点（按文件:行、按函数名）、函数图里到某个节点的路径、功能线里到某个函数的调用链、找结局。"""

from __future__ import annotations

import textwrap

import pytest
from conftest import analyze_files, find_fn, node_by
from pystepmap.query import (
    ChainLink,
    NodeHit,
    PathStep,
    entry_chain,
    find_by_function,
    find_by_location,
    find_outcomes,
    lines_of_functions,
    paths_to,
)

SVC = """
import functools


def helper(x):
    return x


class Svc:
    limit = 3

    @functools.cache
    def run(self, x):
        helper(x)
        y = 1
        helper(
            y,
        )
        if x:
            helper(x)

    def inner_host(self):
        def inner():
            helper(1)
            helper(2)
        inner()


def run():
    helper(0)
"""

TASKS = """
from app.svc import run


@broker.task()
async def job():
    run()
"""

FILES = {"app/__init__.py": "", "app/svc.py": SVC, "app/tasks.py": TASKS}


def lineno(src: str, needle: str, nth: int = 1) -> int:
    """（去缩进后的）源码里去掉首尾空白后等于 needle 的第 nth 行的行号。"""
    hits = [i for i, text in enumerate(textwrap.dedent(src).splitlines(), 1) if text.strip() == needle]
    return hits[nth - 1]


def _graph(tmp_path):
    return analyze_files(tmp_path, FILES)


def hits_of(hits: list[NodeHit]) -> list[tuple[str, str, str]]:
    """(限定名, 节点 id, how)，测试里好比较。"""
    return [(h.function.split("::")[1].rsplit("@", 1)[0], h.node, h.how) for h in hits]


# ---------- find_by_location ----------


def test_location_on_a_step_line_covers_it(tmp_path):
    g = _graph(tmp_path)
    run = find_fn(g, "Svc.run")
    step = node_by(run, type="step", line=lineno(SVC, "helper(x)"))
    assert find_by_location(g, "app/svc.py", lineno(SVC, "helper(x)")) == [
        NodeHit(run.key, step.id, "covers", ())]


def test_location_inside_a_call_split_over_three_lines(tmp_path):
    g = _graph(tmp_path)
    run = find_fn(g, "Svc.run")
    step = node_by(run, type="step", line=lineno(SVC, "helper("))
    assert step.end_line == lineno(SVC, ")") == step.line + 2
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "y,"))) == [("Svc.run", step.id, "covers")]


def test_location_on_an_assignment_gives_the_nearest_node_above(tmp_path):
    g = _graph(tmp_path)
    run = find_fn(g, "Svc.run")
    before = node_by(run, type="step", line=lineno(SVC, "helper(x)"))
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "y = 1"))) == [("Svc.run", before.id, "nearest")]


def test_location_on_a_decorator_line_is_the_start_node(tmp_path):
    g = _graph(tmp_path)
    run = find_fn(g, "Svc.run")
    start = node_by(run, type="start")
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "@functools.cache"))) == [
        ("Svc.run", start.id, "covers")]
    # def 行本身也是开始节点
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "def run(self, x):"))) == [
        ("Svc.run", start.id, "covers")]


def test_location_in_a_class_body_outside_methods_is_empty(tmp_path):
    g = _graph(tmp_path)
    assert find_by_location(g, "app/svc.py", lineno(SVC, "limit = 3")) == []
    assert find_by_location(g, "app/svc.py", lineno(SVC, "class Svc:")) == []
    assert find_by_location(g, "app/svc.py", lineno(SVC, "import functools")) == []


def test_location_in_an_inner_function_belongs_to_the_inner_function(tmp_path):
    g = _graph(tmp_path)
    inner = find_fn(g, "Svc.inner_host.<locals>.inner")
    step = node_by(inner, type="step", line=lineno(SVC, "helper(2)"))
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "helper(2)"))) == [
        ("Svc.inner_host.<locals>.inner", step.id, "covers")]
    host = find_fn(g, "Svc.inner_host")
    call = node_by(host, type="step", line=lineno(SVC, "inner()"))
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "inner()"))) == [
        ("Svc.inner_host", call.id, "covers")]


def test_location_absolute_and_relative_paths_are_the_same(tmp_path):
    g = _graph(tmp_path)
    line = lineno(SVC, "helper(0)")
    rel = find_by_location(g, "app/svc.py", line)
    assert rel and find_by_location(g, f"{g.root}/app/svc.py", line) == rel
    assert find_by_location(g, "./app/svc.py", line) == rel


def test_location_in_a_missing_file_or_outside_the_root_is_empty(tmp_path):
    g = _graph(tmp_path)
    assert find_by_location(g, "app/nope.py", 3) == []
    assert find_by_location(g, "/elsewhere/app/svc.py", lineno(SVC, "helper(0)")) == []
    assert find_by_location(g, "../app/svc.py", lineno(SVC, "helper(0)")) == []


def test_location_hits_carry_the_lines_of_the_function(tmp_path):
    g = _graph(tmp_path)
    [line] = [ln for ln in g.lines if ln.id == find_fn(g, "job").key]
    [hit] = find_by_location(g, "app/svc.py", lineno(SVC, "helper(0)"))
    assert hit.lines == (line.id,)
    assert lines_of_functions(g)[hit.function] == [line.id]


def test_location_on_a_match_guard_line_is_the_match_node(tmp_path):
    src = """
        def check(x):
            return x


        def route(ev):
            match ev:
                case 1:
                    check(1)
                case 2 if check(
                    ev,
                ):
                    check(2)
    """
    g = analyze_files(tmp_path, {"app/m.py": src})
    route = find_fn(g, "route")
    match = node_by(route, type="cond")
    assert hits_of(find_by_location(g, "app/m.py", lineno(src, "ev,"))) == [("route", match.id, "covers")]
    # case 行本身（没有守卫的）不出节点：最近的是上一个分支里的步骤
    one = node_by(route, type="step", line=lineno(src, "check(1)"))
    assert hits_of(find_by_location(g, "app/m.py", lineno(src, "case 1:"))) == [("route", match.id, "nearest")]
    assert hits_of(find_by_location(g, "app/m.py", lineno(src, "check(1)"))) == [("route", one.id, "covers")]


def test_location_ignores_decorators_when_the_file_changed_since_analysis(tmp_path):
    """文件在分析之后改了：图里的行号对不上现在的文件，不读装饰器，只按图里的范围找。"""
    g = _graph(tmp_path)
    (tmp_path / "app/svc.py").write_text("# changed\n" + textwrap.dedent(SVC), encoding="utf-8")
    assert find_by_location(g, "app/svc.py", lineno(SVC, "@functools.cache")) == []
    run = find_fn(g, "Svc.run")
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "y = 1")))[0][:2] == ("Svc.run", node_by(
        run, type="step", line=lineno(SVC, "helper(x)")).id)


# ---------- find_by_function ----------


def test_function_by_sid_qualname_and_short_name(tmp_path):
    g = _graph(tmp_path)
    run = find_fn(g, "Svc.run")
    start = node_by(run, type="start")
    assert hits_of(find_by_function(g, "app/svc.py::Svc.run")) == [("Svc.run", start.id, "definition")]
    assert hits_of(find_by_function(g, "Svc.run")) == [("Svc.run", start.id, "definition")]
    assert hits_of(find_by_function(g, run.key)) == [("Svc.run", start.id, "definition")]
    assert find_by_function(g, "no_such_function") == []


def test_function_by_short_name_returns_every_function_with_that_name(tmp_path):
    g = _graph(tmp_path)
    job = find_fn(g, "job")
    call = node_by(job, type="step")
    hits = hits_of(find_by_function(g, "run"))
    assert hits == [("Svc.run", "n1", "definition"), ("run", "n1", "definition"), ("job", call.id, "call_site")]


def test_function_call_sites_point_at_the_calling_steps(tmp_path):
    g = _graph(tmp_path)
    helper = find_fn(g, "helper")
    run, inner, mod_run = find_fn(g, "Svc.run"), find_fn(g, "Svc.inner_host.<locals>.inner"), find_fn(g, "run")
    expected = [("helper", "n1", "definition")]
    for fg, needles in ((inner, ["helper(1)", "helper(2)"]), (run, ["helper(x)", "helper(", "helper(x)#2"]),
                        (mod_run, ["helper(0)"])):
        for needle in needles:
            text, _, nth = needle.partition("#")
            expected.append((fg.qualname, node_by(fg, type="step", line=lineno(SVC, text, int(nth or 1))).id,
                             "call_site"))
    assert sorted(hits_of(find_by_function(g, "helper"))) == sorted(expected)
    hits = find_by_function(g, "helper")
    assert hits[0].how == "definition"
    [line] = [ln for ln in g.lines if ln.id == find_fn(g, "job").key]
    assert {h.lines for h in hits if h.function == mod_run.key} == {(line.id,)}
    assert {h.lines for h in hits if h.function == run.key} == {()}
    assert helper.key  # 被调函数自己的节点只有 definition


# ---------- paths_to ----------

FLOWS = """
def a(*args): pass
def b(*args): pass
def c(*args): pass


def straight():
    a()
    b()
    c()


def branches(x):
    if x:
        a()
    else:
        b()
    c()


def looper(xs):
    for x in xs:
        a(x)
    b()
"""


def _flows(tmp_path):
    return analyze_files(tmp_path, {"app/f.py": FLOWS})


def nodes_of(path: list[PathStep]) -> list[str]:
    return [s.node for s in path]


def test_paths_straight_function_has_one_path(tmp_path):
    g = _flows(tmp_path)
    fg = find_fn(g, "straight")
    end = node_by(fg, sid="end:implicit")
    paths, truncated = paths_to(g, fg.key, end.id)
    assert not truncated
    assert paths == [[PathStep("n1", "next"), PathStep("n2", "next"), PathStep("n3", "next"), PathStep("n4", "next"),
                      PathStep(end.id)]]
    assert paths_to(g, fg.key, "n1") == ([[PathStep("n1")]], False)


def test_paths_through_both_branches_of_an_if(tmp_path):
    g = _flows(tmp_path)
    fg = find_fn(g, "branches")
    cond = node_by(fg, type="cond")
    after = node_by(fg, type="step", line=lineno(FLOWS, "c()", 2))
    paths, truncated = paths_to(g, fg.key, after.id)
    assert not truncated and len(paths) == 2
    assert [p[1] for p in paths] == [PathStep(cond.id, "yes", "", "yes"), PathStep(cond.id, "no", "", "no")]
    assert all(p[-1] == PathStep(after.id) for p in paths)


def test_paths_into_a_loop_body_and_past_the_loop(tmp_path):
    g = _flows(tmp_path)
    fg = find_fn(g, "looper")
    loop = node_by(fg, type="loop")
    body = node_by(fg, type="step", line=lineno(FLOWS, "a(x)"))
    after = node_by(fg, type="step", line=lineno(FLOWS, "b()", 3))
    [into], truncated = paths_to(g, fg.key, body.id)
    assert not truncated
    assert into == [PathStep("n1", "next"), PathStep(loop.id, "loop_each"), PathStep(body.id)]
    paths, truncated = paths_to(g, fg.key, after.id)
    assert not truncated
    assert [nodes_of(p) for p in paths] == [["n1", loop.id, after.id],  # 一次都不进循环体
                                            ["n1", loop.id, body.id, loop.id, after.id]]  # 走一圈再出来
    assert [s.kind for s in paths[1]] == ["next", "loop_each", "loop_back", "loop_exit", ""]
    shorter, truncated = paths_to(g, fg.key, after.id, limit=1)
    assert truncated and shorter == paths[:1]


def test_paths_to_an_unreachable_node_are_empty(tmp_path):
    g = _flows(tmp_path)
    fg = find_fn(g, "straight")
    uncaught = node_by(fg, sid="end:uncaught")
    assert paths_to(g, fg.key, uncaught.id) == ([], False)
    assert paths_to(g, fg.key, "n999") == ([], False)


# ---------- entry_chain ----------

CHAIN = {
    "app/__init__.py": "",
    "app/flow.py": """
        from app.tasks import child


        @router.post("/go")
        async def go():
            prep()
            await child.kiq()


        def prep():
            pass
    """,
    "app/tasks.py": """
        @broker.task()
        async def child():
            work()


        def work():
            pass


        def unrelated():
            pass
    """,
}


def test_entry_chain(tmp_path):
    g = analyze_files(tmp_path, CHAIN)
    go, child, prep, work = (find_fn(g, q) for q in ("go", "child", "prep", "work"))
    [line] = [ln for ln in g.lines if ln.id == go.key]
    dispatch = node_by(go, type="step", subtitle="派发 child")
    call_prep = node_by(go, type="step", subtitle="prep")
    call_work = node_by(child, type="step", subtitle="work")
    assert entry_chain(g, line, go.key) == []
    assert entry_chain(g, line, child.key) == [ChainLink(go.key, dispatch.id, "dispatch")]
    assert entry_chain(g, line, prep.key) == [ChainLink(go.key, call_prep.id, "call")]
    assert entry_chain(g, line, work.key) == [ChainLink(go.key, dispatch.id, "dispatch"),
                                              ChainLink(child.key, call_work.id, "call")]
    assert entry_chain(g, line, find_fn(g, "unrelated").key) is None
    assert entry_chain(g, line, "app/nope.py::x@1") is None


# ---------- find_outcomes ----------

OUTCOMES = {
    "app/__init__.py": "",
    "app/mail.py": """
        from pystepkit import outcome


        @broker.task()
        async def process(mail):
            if mail is None:
                return outcome("skipped", title="邮件不存在")
            check(mail)
            return outcome("ok", title="处理完成")


        def check(mail):
            if not mail.ok:
                raise ValueError("Bad Mail")
    """,
}


def test_find_outcomes_by_title_fragment_and_end_kind(tmp_path):
    g = analyze_files(tmp_path, OUTCOMES)
    process, check = find_fn(g, "process"), find_fn(g, "check")
    [line] = [ln for ln in g.lines if ln.id == process.key]
    skipped = node_by(process, type="end", title="邮件不存在")
    failed = node_by(check, type="end", end_kind="failed")
    assert find_outcomes(g, line, "不存在") == [(process.key, skipped.id)]
    assert find_outcomes(g, line, "failed") == [(check.key, failed.id)]
    assert find_outcomes(g, line, "bad mail") == [(check.key, failed.id)]  # 不分大小写
    assert find_outcomes(g, line, "skipped") == [(process.key, skipped.id)]
    assert find_outcomes(g, line, "没有这个结局") == []


# ---------- 复查第 1 轮 ----------


def test_paths_through_many_balanced_diamonds_are_found_quickly(tmp_path):
    """60 个前后相接的 if / else：路径数是 2 的 60 次方，前 20 条最短的照样很快给出，第二项为真。"""
    body = "".join(f"    if x > {i}:\n        a()\n    else:\n        b()\n" for i in range(60))
    g = analyze_files(tmp_path, {"app/d.py": "def a(): pass\ndef b(): pass\n\n\ndef diamonds(x):\n" + body})
    fg = find_fn(g, "diamonds")
    end = node_by(fg, sid="end:implicit")
    paths, truncated = paths_to(g, fg.key, end.id)
    assert truncated and len(paths) == 20
    assert len({len(p) for p in paths}) == 1  # 一样长（每个菱形都要走一边）
    assert len({tuple(s.node for s in p) for p in paths}) == 20


def test_paths_limit_must_be_positive(tmp_path):
    g = _flows(tmp_path)
    fg = find_fn(g, "straight")
    with pytest.raises(ValueError):
        paths_to(g, fg.key, "n2", limit=0)


def test_find_outcomes_with_empty_text_is_empty(tmp_path):
    g = analyze_files(tmp_path, OUTCOMES)
    [line] = [ln for ln in g.lines if ln.id == find_fn(g, "process").key]
    assert find_outcomes(g, line, "") == []


def test_location_through_a_symlinked_root(tmp_path):
    """图里的 root 是解析过的真实路径；经过指向它的符号链接给出的绝对路径一样认得出来。"""
    g = analyze_files(tmp_path / "proj", FILES)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "proj", target_is_directory=True)
    line = lineno(SVC, "@functools.cache")
    rel = find_by_location(g, "app/svc.py", line)
    assert rel and rel[0].how == "covers"
    assert find_by_location(g, f"{alias}/app/svc.py", line) == rel


def test_location_does_not_read_the_file_when_a_node_covers_the_line(tmp_path, monkeypatch):
    from pystepmap import query

    g = _graph(tmp_path)

    def boom(*args):
        raise AssertionError("不该读源文件")

    monkeypatch.setattr(query, "_source_lines", boom)
    run = find_fn(g, "Svc.run")
    step = node_by(run, type="step", line=lineno(SVC, "helper(x)"))
    assert hits_of(find_by_location(g, "app/svc.py", lineno(SVC, "helper(x)"))) == [("Svc.run", step.id, "covers")]


def test_location_ignores_a_source_file_over_the_size_limit(tmp_path):
    """超过上限的源文件不读：装饰器行认不出来（按图里的范围找，不在函数里）。"""
    big = "import functools\n\n\n@functools.cache\ndef f():\n    g()\n\n\ndef g():\n    pass\n" + "# x\n" * 600_000
    g = analyze_files(tmp_path, {"app/big.py": big})
    assert len(big.encode()) > 2 * 1024 * 1024
    assert find_by_location(g, "app/big.py", 4) == []
    assert hits_of(find_by_location(g, "app/big.py", 5)) == [("f", "n1", "covers")]
