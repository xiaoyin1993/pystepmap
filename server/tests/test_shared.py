"""shared.py：flows、接口、MCP 共用的 identity 和判断过期用的 Facts 缓存——同一个目录不同写法 inode 相同；缓存按分析 id
命中、给了解析好的图就不再读图、不存在的分析不缓存、超过个数丢最早的。原来各模块里那几份重复的已经去掉。"""

from __future__ import annotations

import json

import pytest
from pystepmap.analyze import analyze_project
from pystepmap.model import from_json, to_json
from pystepmap_server import api, flows, mcp_tools
from pystepmap_server.shared import NO_FACTS, Facts, FactsCache, identity, load_facts
from pystepmap_server.store import NewAnalysis, Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home" / "pystepmap.db")


@pytest.fixture
def graph_text(tmp_path) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    return to_json(analyze_project(root))


def add(store: Store, text: str, n: int) -> list[int]:
    pid, first = store.add_project(name="p", root="/p", config={},
                                   analysis=NewAnalysis("0.1.0", {}, text, config={"exclude": ["gen"]},
                                                        file_lines={"a.py": ["line:a.py::f"]}))
    return [first] + [store.add_analysis(pid, NewAnalysis("0.1.0", {}, text)) for _ in range(n - 1)]


def test_identity_sees_through_spellings(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    (tmp_path / "link").symlink_to(d)
    assert identity(d) is not None and identity(d) == identity(tmp_path / "link") == identity(str(d) + "/.")
    assert identity(tmp_path / "nope") is None and identity("a\0b") is None


def test_load_facts(store, graph_text):
    [aid] = add(store, graph_text, 1)
    hashes = json.loads(graph_text)["file_hashes"]
    assert sorted(hashes) == ["a.py", "b.py"]
    facts = load_facts(store, aid)
    assert facts == Facts(hashes, {"a.py": ["line:a.py::f"]}, {"exclude": ["gen"]})
    assert load_facts(store, 999) is None


def test_facts_cache_hits_uses_a_parsed_graph_and_evicts(store, graph_text, monkeypatch):
    ids = add(store, graph_text, 4)
    hashes = json.loads(graph_text)["file_hashes"]
    reads: list[int] = []
    real = store.graph_json
    monkeypatch.setattr(store, "graph_json", lambda aid: reads.append(aid) or real(aid))
    cache = FactsCache(store, size=2)
    assert cache.get(ids[0]).hashes == hashes
    assert cache.get(ids[0]) is cache.get(ids[0])
    assert reads == [ids[0]]  # 第二、三次命中缓存
    graph = from_json(real(ids[1]))
    assert cache.get(ids[1], graph).hashes == hashes and reads == [ids[0]]  # 给了图：不再读图
    looked: list[int] = []
    real_extra = store.analysis_extra
    monkeypatch.setattr(store, "analysis_extra", lambda aid: looked.append(aid) or real_extra(aid))
    assert cache.get(999) is None and cache.get(999) is None and looked == [999, 999]  # 不存在：不缓存
    cache.get(ids[2])  # 第三个：丢掉最早的
    reads.clear()
    cache.get(ids[0])
    assert reads == [ids[0]]
    assert NO_FACTS == Facts({}, None, None)


def test_the_old_copies_are_gone():
    assert not hasattr(flows, "_identity") and not hasattr(mcp_tools, "_identity")
    assert not hasattr(api, "_FileHashes") and not hasattr(api, "_Facts") and not hasattr(mcp_tools, "_Facts")


def test_uncovered_grouped_by_function_equals_cores_per_line_answer(tmp_path):
    """Uncovered 算一次全部、按函数分好再按线拼：和逐条线调用 core 的 uncovered_outcomes 一样（转账示例的真实记录）。"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from pystepmap.runpath import build_run_paths, uncovered_outcomes
    from pystepmap.verify import read_records
    from pystepmap_server.runner import write_plugin_dir
    from pystepmap_server.shared import Uncovered

    root = tmp_path / "transfer"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples" / "transfer", root,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "extra.py").write_text(EXTRA, encoding="utf-8")  # 第二条功能线，和第一条共用一个函数
    graph = analyze_project(root)
    assert len(graph.lines) == 2
    (tmp_path / "g.json").write_text(to_json(graph), encoding="utf-8")
    env = {**__import__("os").environ, "PYSTEPMAP_GRAPH": str(tmp_path / "g.json"),
           "PYSTEPMAP_TRACE_OUT": str(tmp_path / "t.jsonl"), "PYTHONPATH": str(write_plugin_dir(tmp_path / "home")),
           "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "pystepmap.trace", "-p", "no:cacheprovider", "tests"],
                   cwd=root, env=env, check=True, capture_output=True)
    paths = build_run_paths(graph, read_records(tmp_path / "t.jsonl"))
    found = Uncovered(graph, paths)
    assert found.outcomes() == uncovered_outcomes(graph, paths)
    for ln in graph.lines:
        assert found.outcomes(ln.sid) == uncovered_outcomes(graph, paths, ln), ln.sid
    with pytest.raises(LookupError):
        found.outcomes("line:nope.py::x")


EXTRA = """
from pystepkit import outcome, workflow
from transfer_workflow import fee_of


@workflow(title="算手续费")
def quote_fee(amount):
    if fee_of(amount) > 10:
        return outcome("ok", title="手续费较高")
    return outcome("ok", title="手续费正常")
"""


TWIN_V1 = """
import sys


def g(v):
    return v


if sys.version_info >= (3, 0):
    def f(x):
        return g(x)
else:
    def f(x):
        return g(x)


def run(x):
    return f(x)
"""
TWIN_V2 = TWIN_V1.replace("    def f(x):\n        return g(x)\nelse:", "    def f(x):\n        if x:\n            return g(x)\n"
                          "        return 0\nelse:", 1)


def traced(tmp_path, files: dict[str, str], test_src: str):
    import os
    import subprocess
    import sys

    from pystepmap.runpath import build_run_paths
    from pystepmap.verify import read_records
    from pystepmap_server.runner import PYTEST_PLUGIN_OPTIONS, write_plugin_dir

    root = tmp_path / "proj"
    for rel, text in {**files, "tests/test_it.py": test_src}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    graph = analyze_project(root)
    (tmp_path / "g.json").write_text(to_json(graph), encoding="utf-8")
    env = {**os.environ, "PYSTEPMAP_GRAPH": str(tmp_path / "g.json"), "PYSTEPMAP_TRACE_OUT": str(tmp_path / "t.jsonl"),
           "PYTHONPATH": f"{write_plugin_dir(tmp_path / 'home')}{os.pathsep}{root}", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "pytest", "-q", *PYTEST_PLUGIN_OPTIONS.split(), "tests"], cwd=root, env=env,
                   check=True, capture_output=True)
    return root, graph, build_run_paths(graph, read_records(tmp_path / "t.jsonl"))


def test_placing_run_functions_in_one_go_equals_locate_function(tmp_path):
    """place_run_functions 一次放好一次运行的全部函数（每个只看同一个基础 sid 的那一组）：和对整张图、整份路径逐个调用
    core 的 locate_function 一样——同一张图上、重名组改了以后的新图上都是。"""
    from pystepmap.runpath import RunPaths, locate_function
    from pystepmap_server.shared import place_run_functions

    root, graph, paths = traced(tmp_path, {"twin.py": TWIN_V1}, "from twin import run\n\ndef test_r():\n    run(1)\n")
    sids = sorted({f for t in paths.tests for f in t.functions} | set(paths.shapes) | {"twin.py::nope", "x.py::f#3"})
    meta = RunPaths([], paths.version, paths.shapes, paths.function_dups)  # 只要运行一级的项
    (root / "twin.py").write_text(TWIN_V2, encoding="utf-8")
    newer = analyze_project(root)
    for g in (graph, newer):
        expected = [None if (fg := locate_function(g, paths, s)) is None else fg.key for s in sids]
        assert place_run_functions(g, meta, sids) == expected
    assert place_run_functions(graph, meta, ["twin.py::f"])[0] is not None  # 同一张图上：重名组也放得上
