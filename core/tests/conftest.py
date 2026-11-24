"""后续任务的测试共用的辅助函数。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from pystepmap.analyze import analyze_project
from pystepmap.build import build_function_graph
from pystepmap.config import ProjectConfig
from pystepmap.index import build_index
from pystepmap.model import FunctionGraph, ProjectGraph, parse_key, to_json
from pystepmap.resolve import Resolver
from pystepmap.rules import ActiveRules

# 装饰器、辅助函数和 pystepkit 同名但来自别处（Temporal、Metaflow、项目自己的、没导入的）：都不是 pystepkit 标记
LOOKALIKES = """
    import logging
    from temporalio import workflow
    from metaflow import step
    from myproj.decorators import condition

    log = logging.getLogger()


    @workflow.defn
    class Greeting:
        @workflow.run
        async def run(self, name):
            log.info("hi")
            return await workflow.execute_activity(name)


    class Flow:
        @step
        def start(self):
            self.next(self.end)

        @step
        def end(self):
            self.start()


    @condition
    def cond(x):
        return x

    @workflow
    def bare(x):
        return outcome("failed")
"""


def make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """按相对路径写入文件（内容自动 dedent），返回项目根目录。"""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
    return tmp_path


def find_fn(index_or_graph, qualname: str):
    """按限定名取唯一的函数（ProjectIndex 与 ProjectGraph 通用）；不唯一或不存在时抛错。"""
    hits = [f for f in index_or_graph.functions.values() if f.qualname == qualname]
    if len(hits) != 1:
        raise AssertionError(f"限定名 {qualname!r} 应匹配唯一函数，实际 {len(hits)} 个")
    return hits[0]


def node_by(fg, **attrs):
    """按 type / title / subtitle / line 等属性取唯一节点。"""
    hits = [n for n in fg.nodes if all(getattr(n, k) == v for k, v in attrs.items())]
    if len(hits) != 1:
        raise AssertionError(f"属性 {attrs} 应匹配唯一节点，实际 {len(hits)} 个")
    return hits[0]


def fg_of(tmp_path: Path, src: str, qualname: str, config: ProjectConfig | None = None) -> FunctionGraph:
    """把 src 写成 app/mod.py，建索引、规则、解析器，返回指定限定名的函数图。"""
    config = config or ProjectConfig()
    index = build_index(make_project(tmp_path, {"app/mod.py": src}), config)
    fn = find_fn(index, qualname)
    return build_function_graph(fn, index, ActiveRules(index, config), Resolver(index), config)


def analyze_files(tmp_path: Path, files: dict[str, str], config: ProjectConfig | None = None) -> ProjectGraph:
    """建项目并调用 analyze_project，返回 ProjectGraph。"""
    return analyze_project(make_project(tmp_path, files), config)


# ---------- 执行记录（子进程里带插件跑 pytest）----------

CORE_SRC = Path(__file__).resolve().parents[1] / "src"


def write_files(root: Path, files: dict[str, str]) -> None:
    """按相对路径写入文件（去缩进、去掉开头的空行）。"""
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src).lstrip("\n"), encoding="utf-8")


def run_traced(tmp_path: Path, files: dict[str, str], test_src: str, *, active: bool = True,
               graph_text: str | None = None, env_extra: dict[str, str] | None = None,
               output: list[str] | None = None, returncode: int = 0, results: bool = False,
               progress: bool = False) -> list[dict]:
    """建临时项目（测试文件放在 tests/ 下，不进图），分析出图，子进程里带插件跑 pytest，返回 JSONL 记录
    （版本头那一行不算；测试结果行只在 results 为真时、进度行 {"collected"} / {"start"} 只在 progress 为真时给出）。
    output 给出时把 pytest 的标准输出追加进去；returncode 是 pytest 应有的退出码。"""
    proj = tmp_path / "proj"
    write_files(proj, {**files, "tests/test_x.py": test_src})
    graph = tmp_path / "graph.json"
    graph.write_text(to_json(analyze_project(proj)) if graph_text is None else graph_text, encoding="utf-8")
    out = tmp_path / "trace.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYSTEPMAP_", "PYTEST_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(CORE_SRC), str(proj)])
    if active:
        env |= {"PYSTEPMAP_GRAPH": str(graph), "PYSTEPMAP_TRACE_OUT": str(out)}
    env |= env_extra or {}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pystepmap.trace", "-p", "no:cacheprovider", "-q", "tests/test_x.py"],
        cwd=proj, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == returncode, r.stdout + r.stderr
    if output is not None:
        output.append(r.stdout)
    if not out.exists():
        return []
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    return [r for r in records if "format" not in r and (results or "outcome" not in r)
            and (progress or not is_progress(r))]


def is_progress(rec: dict) -> bool:
    """进度行：收集完的 {"collected": N}、测试开始时的 {"start": 测试 id}、挑选的 {"only": N, "kept": K}。"""
    return set(rec) in ({"collected"}, {"start"}, {"only", "kept"})


def recs_of(recs: list[dict], qualname: str) -> list[dict]:
    return [r for r in recs if "fn" in r and parse_key(r["fn"])[1] == qualname]


def one(recs: list[dict], qualname: str) -> dict:
    """按限定名取唯一一条记录。"""
    hits = recs_of(recs, qualname)
    assert len(hits) == 1, f"{qualname}: {hits}"
    return hits[0]
