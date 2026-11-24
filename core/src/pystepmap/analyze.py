"""把索引、规则、解析器、函数图、功能线和自检串成一次完整的项目分析。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pystepmap.build import build_function_graph
from pystepmap.config import ProjectConfig
from pystepmap.entries import auto_entries, unlisted_roots
from pystepmap.index import build_index
from pystepmap.lines import annotate_dispatches, assemble_lines, extra_entry_keys
from pystepmap.model import SCHEMA_VERSION, ProjectGraph, function_sids
from pystepmap.resolve import Resolver
from pystepmap.rules import ActiveRules
from pystepmap.selfcheck import run_selfcheck


@dataclass
class AnalysisNotes:
    """命令行摘要用到的、图里没有的信息。"""

    rules: list[str]  # 启用的识别规则名，排序
    source_roots: list[str]  # 源码根（相对路径，"" 是分析的目录本身）
    excluded: dict[str, int]  # 实际排除掉的目录名 -> 个数
    package: str = ""  # 分析的目录本身是包时它的点分名（见 ProjectIndex.package）
    marks: list[str] = field(default_factory=list)  # 标记里读不出来的参数，每处一句（见 ActiveRules.mark_notes）
    # 不在功能线上、没有人直接调用、但项目里别处提到过的函数（交给别处调用的回调、依赖注入、注册表、模块顶层调用的
    # 启动代码）：列"不在任何功能线上的函数"时不算它们（见 entries.unlisted_roots）
    mentioned: set[str] = field(default_factory=set)


def analyze_project(root: Path, config: ProjectConfig | None = None) -> ProjectGraph:
    """分析 root 下的项目（只读），为每个项目内函数生成函数图。"""
    return analyze_with_notes(root, config)[0]


def analyze_with_notes(root: Path, config: ProjectConfig | None = None) -> tuple[ProjectGraph, AnalysisNotes]:
    """同 analyze_project，另外返回命令行摘要用到的信息。"""
    config = config or ProjectConfig()
    index = build_index(Path(root).resolve(), config)
    rules, resolver = ActiveRules(index, config), Resolver(index)
    skipped = list(index.skipped)
    functions = {}
    for key in sorted(index.functions):
        fn = index.functions[key]
        try:
            functions[key] = build_function_graph(fn, index, rules, resolver, config)
        except Exception as e:  # noqa: BLE001  单个函数出错不拖垮整体分析
            skipped.append((fn.file, f"函数 {fn.qualname} 构建失败：{type(e).__name__}: {e}"))
    sids = function_sids(index.functions)  # 按全部定义编号：某个函数构建失败不影响别的函数的 sid
    for key, fg in functions.items():
        fg.sid = sids[key]
    annotate_dispatches(functions)
    referenced = resolver.value_references()
    auto = auto_entries(functions, index, resolver, rules.external, referenced, config.fallback_entries,
                        extra_entry_keys(functions, config))
    lines, common = assemble_lines(functions, config, referenced, auto)
    graph = ProjectGraph(
        schema_version=SCHEMA_VERSION, root=str(index.root), functions=functions, lines=lines,
        common_tasks=common, selfcheck=run_selfcheck(functions, skipped), file_hashes=dict(sorted(index.hashes.items())),
        sids={fg.sid: key for key, fg in sorted(functions.items(), key=lambda kv: kv[1].sid)})
    notes = AnalysisNotes(sorted(rules.names), list(index.source_roots), dict(sorted(index.excluded.items())),
                          index.package, rules.mark_notes())
    loose = unlisted_roots(graph, limit=None)[0]
    if loose:
        notes.mentioned = resolver.mentions({fg.qualname for fg in loose}) & {fg.key for fg in loose}
    return graph, notes
