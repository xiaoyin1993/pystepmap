"""运行中的进度（live.py）：测试跑着的时候 GET …/runs 的 running.progress 怎么从记录文件和这次运行用的图算出来。"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from pystepmap.analyze import analyze_project
from pystepmap.model import to_json
from pystepmap_server import live as live_mod
from pystepmap_server.live import LiveRun

WF = """
    from pystepkit import outcome, step, workflow


    @step(title="检查")
    def check(x):
        if x < 0:
            raise ValueError("neg")
        return x


    @step(title="保存")
    def save(x):
        return x * 2


    @workflow(name="flow", title="流程")
    def flow(x):
        try:
            y = check(x)
        except ValueError:
            return outcome("failed", title="拒绝")
        except KeyError:
            return outcome("failed", title="缺键")
        return save(y)


    @workflow(name="other", title="另一个")
    def other(x):
        return save(x)
"""
FLOW, CHECK, SAVE, OTHER = "app/wf.py::flow@17", "app/wf.py::check@5", "app/wf.py::save@12", "app/wf.py::other@28"
FLOW_LINE, OTHER_LINE = "line:app/wf.py::flow", "line:app/wf.py::other"
T = "tests/test_wf.py::"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def files(tmp_path) -> tuple[Path, Path]:
    """(图的副本, 记录文件)：和 testflows 准备的一样，图是这次运行用的那次分析。"""
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "app" / "wf.py").write_text(textwrap.dedent(WF).lstrip("\n"), encoding="utf-8")
    graph = tmp_path / "graph.json"
    graph.write_text(to_json(analyze_project(proj)), encoding="utf-8")
    return graph, tmp_path / "trace.jsonl"


def append(path: Path, *recs: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


def execution(fn: str, test: str, lines: list[int], n: int = 1) -> dict:
    return {"fn": fn, "test": test, "lines": lines, "ns": [0] * len(lines), "exit": "return", "exc": None, "calls": [],
            "n": n}


def result(test: str, outcome: str, seconds: float = 0.25) -> dict:
    return {"test": test, "outcome": outcome, "seconds": seconds, "n": 1}


def test_counts_running_recent_and_lines_reached(files):
    graph, trace = files
    append(trace, {"v": 1, "format": "pystepmap-trace"}, {"collected": 7},
           *[{"start": f"{T}test_{i}"} for i in range(5)],
           execution(FLOW, T + "test_0", [19, 24]), execution(CHECK, T + "test_0", [6, 8]),
           result(T + "test_0", "passed", 0.5), result(T + "test_1", "failed", 1.25))
    p = LiveRun(trace, graph).view()
    assert p == {"collected": 7, "done": 2, "passed": 1, "failed": 1, "skipped": 0, "errors": 0,
                 "running": [T + "test_2", T + "test_3", T + "test_4"], "running_more": 0,
                 "recent": [{"test": T + "test_1", "outcome": "failed", "seconds": 1.25},
                            {"test": T + "test_0", "outcome": "passed", "seconds": 0.5}],
                 "lines": {FLOW_LINE: {"functions": 2, "total": 3}}}


def test_at_most_three_running_tests_are_listed(files, tmp_path):
    graph, trace = files
    append(trace, *[{"start": f"{T}test_{i}"} for i in range(5)])
    p = LiveRun(trace, graph).view()
    assert p["running"] == [T + "test_0", T + "test_1", T + "test_2"] and p["running_more"] == 2


def test_nodes_reached_on_a_line(files):
    """?live_line=：这条线上每个执行过的函数里，第一行（语句头部）执行过的节点。开始节点在函数执行过时算；捕获节点（except
    那一行在异常只是经过、没被它接住时也执行）、和调用写在同一行的结局（调用抛出时这一行也执行过）、行号为 0 的结局
    光看执行过的行分不清，不给。"""
    graph, trace = files
    append(trace, execution(FLOW, T + "a", [19, 24]), execution(FLOW, T + "b", [19, 20, 22, 23]),
           execution(CHECK, T + "a", [6, 8]), result(T + "a", "passed"))
    lr = LiveRun(trace, graph)
    p = lr.view(FLOW_LINE)
    assert p["nodes"] == {FLOW: ["start", "step:bc28721f", "end:3fa99770", "step:13f06cea"],
                          CHECK: ["start", "cond:156f07c8", "end:03c38773"]}
    assert p["lines"] == {FLOW_LINE: {"functions": 2, "total": 3}}
    assert "nodes" not in lr.view("line:app/wf.py::nope") and "nodes" not in lr.view()
    assert lr.view(OTHER_LINE)["nodes"] == {}  # 这条线上的函数都还没执行过


def test_polls_at_most_once_per_second_however_many_requests(files, monkeypatch):
    graph, trace = files
    polls = []
    real = live_mod.TraceProgress.poll

    def counting(self):
        polls.append(1)
        return real(self)

    monkeypatch.setattr(live_mod.TraceProgress, "poll", counting)
    clock = Clock()
    lr = LiveRun(trace, graph, clock=clock)
    append(trace, result(T + "a", "passed"))
    assert lr.view()["done"] == 1
    append(trace, result(T + "b", "passed"), execution(SAVE, T + "b", [13]))
    for line in (None, FLOW_LINE, OTHER_LINE, None):
        assert lr.view(line)["done"] == 1  # 一秒之内：用上次读到的
    assert len(polls) == 1
    clock.now += 1.0
    p = lr.view(OTHER_LINE)
    assert p["done"] == 2 and p["nodes"] == {SAVE: ["start", "end:f785b825"]} and len(polls) == 2
    assert p["lines"] == {FLOW_LINE: {"functions": 1, "total": 3}, OTHER_LINE: {"functions": 1, "total": 2}}


def test_an_unreadable_graph_still_gives_the_counts(files, caplog):
    graph, trace = files
    graph.write_text("not a graph", encoding="utf-8")
    append(trace, {"start": T + "a"}, execution(FLOW, T + "a", [19]), result(T + "b", "passed"))
    lr = LiveRun(trace, graph)
    p = lr.view(FLOW_LINE)
    assert (p["done"], p["running"], p["lines"]) == (1, [T + "a"], {}) and "nodes" not in p
    assert "读不了这次运行的图" in caplog.text


def test_functions_not_in_the_runs_graph_are_not_kept(files):
    graph, trace = files
    append(trace, execution("junk.py::x@1", T + "a", [1]), execution(SAVE, T + "a", [13]))
    lr = LiveRun(trace, graph)
    assert lr.view(OTHER_LINE)["nodes"] == {SAVE: ["start", "end:f785b825"]}
    assert set(lr._progress.poll()["functions"]) == {SAVE}


def test_nothing_from_the_file_but_ids_counts_seconds_and_outcomes(files):
    graph, trace = files
    append(trace, {"start": T + "a", "x": "ZQX-1"}, {**execution(FLOW, T + "a", [19]), "exc": "ZQX-2"},
           {**result(T + "a", "failed"), "exc": "ZQX-3"}, {"error": "ZQX-4: boom"})
    p = LiveRun(trace, graph).view(FLOW_LINE)
    assert "ZQX" not in json.dumps(p, ensure_ascii=False)
