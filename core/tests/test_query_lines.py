"""功能线涉及的函数：派发树上的每个任务，加上从它们出发沿 call_targets 能到的所有函数。"""

from __future__ import annotations

from conftest import analyze_files, find_fn
from pystepmap.config import ProjectConfig
from pystepmap.query import file_lines, line_functions, lines_of_functions

FILES = {
    "app/__init__.py": "",
    "app/api.py": """
        from app.tasks import common_c, job_b
        from app.util import h

        @router.post("/a")
        async def entry_a():
            h()
            await job_b.kiq()
            await common_c.kiq()

        @router.post("/other")
        async def entry_other():
            await common_c.kiq()

        @router.post("/third")
        async def entry_third():
            await common_c.kiq()
    """,
    "app/util.py": """
        def h():
            g()

        def g():
            h()

        def unused():
            g()
    """,
    "app/tasks.py": """
        from app.util import g

        @broker.task()
        async def job_b():
            g()
            await job_b.kiq()

        @broker.task()
        async def common_c():
            pass
    """,
}


def _graph(tmp_path):
    return analyze_files(tmp_path, FILES, ProjectConfig(common_task_threshold=3))


def _line_of(g, qualname):
    key = find_fn(g, qualname).key
    [line] = [ln for ln in g.lines if ln.id == key]
    return line


def _names(g, keys):
    return [g.functions[k].qualname for k in keys]


def test_line_functions_follow_dispatch_tree_and_calls(tmp_path):
    g = _graph(tmp_path)
    common = find_fn(g, "common_c").key
    assert g.common_tasks == [common]  # 公共任务不并入入口 A 的线
    line_a = _line_of(g, "entry_a")
    assert _names(g, line_functions(g, line_a)) == ["entry_a", "h", "g", "job_b"]
    assert _names(g, line_functions(g, _line_of(g, "entry_other"))) == ["entry_other"]
    assert _names(g, line_functions(g, _line_of(g, "common_c"))) == ["common_c"]


def test_lines_of_functions(tmp_path):
    g = _graph(tmp_path)
    line_a = _line_of(g, "entry_a")
    by_fn = lines_of_functions(g)
    assert by_fn[find_fn(g, "g").key] == [line_a.id]
    assert by_fn[find_fn(g, "job_b").key] == [line_a.id]
    assert by_fn[find_fn(g, "common_c").key] == [_line_of(g, "common_c").id]
    assert find_fn(g, "unused").key not in by_fn  # 没有线涉及它
    # 每条线涉及的每个函数都在，线按 graph.lines 的顺序
    for fn_key, line_ids in by_fn.items():
        assert line_ids == [ln.id for ln in g.lines if fn_key in line_functions(g, ln)]


def test_file_lines(tmp_path):
    g = _graph(tmp_path)
    line_a = _line_of(g, "entry_a")
    by_file = file_lines(g)
    assert by_file["app/util.py"] == [line_a.sid]
    assert by_file["app/tasks.py"] == [ln.sid for ln in g.lines if ln.sid in (line_a.sid, _line_of(g, "common_c").sid)]
    assert by_file["app/api.py"] == [ln.sid for ln in g.lines if ln.id != _line_of(g, "common_c").id]
    assert "app/__init__.py" not in by_file


def test_cycles_terminate(tmp_path):
    """调用成环（h 调 g、g 调 h）、任务派发自己、两个任务互相派发：都不死循环，每个函数只出现一次。"""
    g = analyze_files(tmp_path, {"app/t.py": """
        def h():
            g()
        def g():
            h()
        @broker.task()
        async def ping():
            h()
            await pong.kiq()
            await ping.kiq()
        @broker.task()
        async def pong():
            g()
            await ping.kiq()
    """})
    for line in g.lines:
        keys = line_functions(g, line)
        assert len(keys) == len(set(keys))
        assert set(_names(g, keys)) == {"ping", "pong", "h", "g"}
