"""运行中的进度（pystepmap.progress.TraceProgress）：测试跑着的时候一次次读记录文件新追加的部分。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import run_traced
from pystepmap.progress import RECENT, TraceProgress

T = "tests/test_x.py::"


def line(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False) + "\n"


def execution(fn: str, test: str, lines: list[int], n: int = 1, **extra) -> dict:
    return {"fn": fn, "test": test, "lines": lines, "ns": [0] * len(lines), "exit": "return", "exc": None,
            "calls": [], "n": n, **extra}


def result(test: str, outcome: str, seconds: float = 0.5, **extra) -> dict:
    return {"test": test, "outcome": outcome, "seconds": seconds, "n": 1, **extra}


def append(path: Path, *recs: dict | str | bytes) -> None:
    with open(path, "ab") as f:
        f.writelines(r if isinstance(r, bytes) else (r if isinstance(r, str) else line(r)).encode("utf-8") for r in recs)


def counts(p: dict) -> tuple:
    return p["collected"], p["done"], p["passed"], p["failed"], p["skipped"], p["errors"]


def test_counts_running_tests_recent_results_and_reached_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    append(path, {"v": 1, "format": "pystepmap-trace"}, {"collected": 4}, {"start": T + "test_a"},
           execution("m.py::f@1", T + "test_a", [2, 3]), execution("m.py::g@5", T + "test_a", [6], n=2),
           result(T + "test_a", "passed", 0.25), {"start": T + "test_b"})
    tp = TraceProgress(path)
    p = tp.poll()
    assert counts(p) == (4, 1, 1, 0, 0, 0)
    assert p["running"] == [T + "test_b"]
    assert p["recent"] == [{"test": T + "test_a", "outcome": "passed", "seconds": 0.25}]
    assert p["functions"] == {"m.py::f@1": {"executions": 1, "lines": frozenset({2, 3})},
                              "m.py::g@5": {"executions": 2, "lines": frozenset({6})}}
    append(path, execution("m.py::f@1", T + "test_b", [2, 4], n=3), result(T + "test_b", "failed", 1.5, exc="ValueError"),
           {"start": T + "test_c"}, result(T + "test_c", "skipped", 0.0), {"start": T + "test_d"},
           result(T + "test_d", "error", 0.1, exc="RuntimeError"))
    p = tp.poll()
    assert counts(p) == (4, 4, 1, 1, 1, 1) and p["running"] == []
    assert [r["test"].rsplit("::", 1)[1] for r in p["recent"]] == ["test_d", "test_c", "test_b", "test_a"]
    assert p["functions"]["m.py::f@1"] == {"executions": 4, "lines": frozenset({2, 3, 4})}


def test_parametrised_cases_share_an_id_and_a_counter(tmp_path):
    """参数化的几个用例 id 相同：开始了几次、结束了几次分开数，都结束了才不算在跑。"""
    path = tmp_path / "t.jsonl"
    append(path, {"start": T + "test_p"}, {"start": T + "test_q"}, {"start": T + "test_p"})
    tp = TraceProgress(path)
    assert tp.poll()["running"] == [T + "test_p", T + "test_q"]
    append(path, result(T + "test_p", "passed"))
    assert tp.poll()["running"] == [T + "test_p", T + "test_q"]
    append(path, result(T + "test_p", "passed"))
    assert tp.poll()["running"] == [T + "test_q"]
    append(path, {"start": T + "test_p"})  # 又开始：排到后面
    assert tp.poll()["running"] == [T + "test_q", T + "test_p"]
    append(path, result(T + "test_x", "passed"))  # 没见过开始的（旧版插件写的文件）：只算结果
    p = tp.poll()
    assert p["done"] == 3 and p["running"] == [T + "test_q", T + "test_p"]


def test_only_the_last_few_finished_tests_are_kept_newest_first(tmp_path):
    path = tmp_path / "t.jsonl"
    append(path, *[result(f"{T}test_{i}", "passed", i) for i in range(20)])
    p = TraceProgress(path).poll()
    assert [r["seconds"] for r in p["recent"]] == list(range(19, 19 - RECENT, -1)) and p["done"] == 20


def test_collected_takes_the_largest(tmp_path):
    """pytest-xdist 的每个工作进程各写一行收集到的用例数：取最大的。没有这一行时 None。"""
    path = tmp_path / "t.jsonl"
    append(path, {"start": T + "a"})
    tp = TraceProgress(path)
    assert tp.poll()["collected"] is None
    append(path, {"collected": 12}, {"collected": 30}, {"collected": 7})
    assert tp.poll()["collected"] == 30


def test_only_the_appended_bytes_are_read(tmp_path):
    """第二次只读新追加的部分：已经读过的那一段之后被改掉（同一个文件、长度不变）也不再读。"""
    path = tmp_path / "t.jsonl"
    append(path, result(T + "a", "passed"))
    tp = TraceProgress(path)
    assert tp.poll()["done"] == 1
    size = path.stat().st_size
    with open(path, "r+b") as f:  # 原地改掉读过的那一行
        f.write(b"x" * (size - 1))
    append(path, result(T + "b", "failed"))
    p = tp.poll()
    assert (p["done"], p["passed"], p["failed"]) == (2, 1, 1)


def test_a_partial_last_line_waits_for_the_rest(tmp_path):
    path = tmp_path / "t.jsonl"
    text = line(result(T + "a", "passed"))
    append(path, text[:10])
    tp = TraceProgress(path)
    assert tp.poll()["done"] == 0
    append(path, text[10:-1])
    assert tp.poll()["done"] == 0
    append(path, "\n")
    assert tp.poll()["done"] == 1


def test_bad_lines_are_skipped(tmp_path):
    """不是 JSON、不是对象、字段不对的行跳过（读的是正在写的文件，不报错），后面的照读。"""
    path = tmp_path / "t.jsonl"
    append(path, "not json\n", "[1, 2]\n", "\n", b"\xff\xfe\n", "{" * 5000 + "\n",
           {"collected": -1}, {"collected": "9"}, {"start": 5}, result(T + "a", "maybe"), result(T + "a", "passed", -1),
           {"test": T + "a", "outcome": "passed", "seconds": 1, "n": 0},
           {"fn": "m.py::f@1", "lines": "2,3", "n": 1}, {"fn": "m.py::f@1", "lines": [2, "3", 4.0, True], "n": 1},
           {"fn": "m.py::f@1", "lines": [9], "n": 0}, {"fn": 7, "lines": [1], "n": 1}, {"error": "ValueError: x"},
           {"fn": "m.py::f@1", "lines": [2, 5], "n": 2},
           result(T + "b", "passed"))
    p = TraceProgress(path).poll()
    assert counts(p) == (None, 1, 1, 0, 0, 0) and p["running"] == []
    assert p["functions"] == {"m.py::f@1": {"executions": 2, "lines": frozenset({2, 5})}}


def test_a_missing_file_is_empty_until_it_appears(tmp_path):
    path = tmp_path / "t.jsonl"
    tp = TraceProgress(path)
    assert tp.poll() == {"collected": None, "done": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0,
                         "running": [], "recent": [], "functions": {}}
    append(path, result(T + "a", "passed"))
    assert tp.poll()["done"] == 1
    path.unlink()  # 删掉了（运行结束、临时目录清掉）：留着读到的
    assert tp.poll()["done"] == 1


def test_a_replaced_or_truncated_file_is_read_from_the_start(tmp_path):
    path = tmp_path / "t.jsonl"
    append(path, result(T + "a", "passed"), result(T + "b", "passed"), execution("m.py::f@1", T + "b", [2]))
    tp = TraceProgress(path)
    assert tp.poll()["done"] == 2
    other = tmp_path / "new.jsonl"
    append(other, result(T + "c", "failed"))
    os.replace(other, path)  # 换了一个文件
    p = tp.poll()
    assert counts(p)[1:4] == (1, 0, 1) and p["functions"] == {}
    append(path, result(T + "d", "passed"))
    assert tp.poll()["done"] == 2
    with open(path, "r+b") as f:  # 截短了
        f.truncate(0)
    append(path, {"start": T + "e"})
    p = tp.poll()
    assert p["done"] == 0 and p["running"] == [T + "e"]


def test_a_large_backlog_is_read_over_several_polls(tmp_path):
    """一次最多读 budget 个字节：积压很多时分几次读完，每次都只算完整的行。"""
    path = tmp_path / "t.jsonl"
    append(path, *[result(f"{T}test_{i}", "passed") for i in range(100)])
    tp = TraceProgress(path, budget=1000)
    seen = [tp.poll()["done"] for _ in range(20)]
    assert seen[0] < 100 and seen == sorted(seen) and seen[-1] == 100


def test_memory_is_bounded_by_the_graph_not_by_the_trace(tmp_path):
    """同一个函数记了很多次：只留一个计数和行号的集合；给了图里的函数时，别的函数 key 不留；一行长得没有尽头（没有
    换行）时丢掉它，不在内存里攒着。"""
    path = tmp_path / "t.jsonl"
    append(path, *[execution("m.py::f@1", f"{T}test_{i}", [2, 3, 2, 3]) for i in range(2000)],
           execution("junk.py::x@1", T + "a", [1]))
    tp = TraceProgress(path, functions={"m.py::f@1"}, max_line=10_000)
    p = tp.poll()
    assert p["functions"] == {"m.py::f@1": {"executions": 2000, "lines": frozenset({2, 3})}}
    append(path, "x" * 50_000)
    tp.poll()
    assert len(tp._rest) <= 10_000
    append(path, "x" * 10 + "\n", result(T + "b", "passed"))
    assert tp.poll()["done"] == 1


def test_nothing_but_ids_counts_durations_and_outcome_kinds_is_kept(tmp_path):
    """记录里的异常类型名、调用、耗时、不认识的字段都不留：给出去的只有测试 id、函数 key、行号、计数、秒数、结果种类。"""
    path = tmp_path / "t.jsonl"
    append(path, {"start": T + "a", "note": "ZQX-1"}, execution("m.py::f@1", T + "a", [2], exc="ZQX-2", calls=[["ZQX-3", 1]],
                                                               extra="ZQX-4"),
           result(T + "a", "failed", exc="ZQX-5", message="ZQX-6"), {"error": "ZQX-7"})
    p = TraceProgress(path).poll()
    assert "ZQX" not in repr(p)
    assert p["recent"] == [{"test": T + "a", "outcome": "failed", "seconds": 0.5}]


def test_a_real_run_read_while_it_grows(tmp_path):
    """真的跑一遍插件：读完时的数和结果行一致，读到的函数和行号就是执行记录里的；分几段喂进去（每次截在一行中间）
    和一次读完一样。"""
    recs = run_traced(tmp_path, {"app/m.py": "def f(x):\n    if x:\n        return x\n    raise ValueError('no')\n"}, """
        import pytest
        from app.m import f
        @pytest.mark.parametrize("x", [1, 2])
        def test_p(x):
            assert f(x) == x
        def test_fail():
            f(0)
        @pytest.mark.skip
        def test_skip():
            pass
    """, returncode=1, results=True, progress=True)
    full = (tmp_path / "trace.jsonl").read_bytes()
    whole = TraceProgress(tmp_path / "trace.jsonl").poll()
    assert counts(whole) == (4, 4, 2, 1, 1, 0) and whole["running"] == []
    lines: dict[str, set[int]] = {}
    for r in recs:
        if "fn" in r:
            lines.setdefault(r["fn"], set()).update(r["lines"])
    assert {k: set(v["lines"]) for k, v in whole["functions"].items()} == lines
    grow = tmp_path / "grow.jsonl"
    tp = TraceProgress(grow)
    for cut in range(0, len(full), 37):
        with open(grow, "ab") as f:
            f.write(full[cut:cut + 37])
        tp.poll()
    assert tp.poll() == whole
