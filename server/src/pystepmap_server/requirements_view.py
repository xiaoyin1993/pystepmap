"""需求列表给界面的形状：项目里的需求（现读文件）、各自和最新一次分析的图的对照、设计的每个结局有没有测试走到，
以及把设计的流程画出来用的图。只读项目文件。"""

from __future__ import annotations

import json
from pathlib import Path

from pystepmap import requirements as rq
from pystepmap.model import ProjectGraph, to_json

from .shared import Uncovered


def _coverage(cmp: rq.Comparison, missed: Uncovered | None) -> list[dict]:
    """设计的每个结局：covered（有测试走到）/ uncovered / uncertain（运行之后代码改过，对不上）/ added（那次运行时还没有这个结局）/ missing（实现里还
    没有）/ unknown（没有跑完的测试运行，或者功能线对不上）。"""
    state: dict[str, str] = {}
    known = False
    if missed is not None and cmp.line_sid:
        try:
            state = {o.node: ("added" if o.added else "uncertain" if o.uncertain else "uncovered")
                     for o in missed.outcomes(cmp.line_sid)}
            known = True
        except LookupError:
            pass
    return [{"title": title, "kind": kind, "node": sid,
             "covered": "missing" if not sid else state.get(sid, "covered") if known else "unknown"}
            for title, kind, sid in cmp.outcomes]


def requirements_view(root: Path, graph: ProjectGraph, missed: Uncovered | None, run_id: int | None) -> dict:
    """{"requirements": [...], "graph": 设计流程的图（和分析结果同一个格式；读不懂的需求不在里面）, "run": 算测试覆盖
    用的运行 id 或 None}。每条需求：id、title、status、line、description、flow、file、text（文件原文；读不了的文件为空）、errors、
    archived（OpenSpec 已归档的变更里的：当时的设计）、design_line（它在
    graph 里的功能线 id，读不懂时 None）、comparison（读不懂时 None）、reachable_status。comparison：line_sid、
    line_title、matches、differences（kind、text、type、title、node——实现里的节点 sid、design_nodes——设计图里对应的
    节点 sid）、notes、outcomes。"""
    found = rq.load_requirements(root)
    design = rq.design_graph(found)
    out = []
    for r in found:
        key = f"{r.file}::{r.id}"
        item = {"id": r.id, "title": r.title, "status": r.status, "line": r.line, "description": r.description,
                "flow": r.flow_text, "file": r.file, "text": r.text, "errors": r.errors, "archived": r.archived,
                "design_line": key if key in design.functions else None, "comparison": None, "reachable_status": r.status}
        if not r.errors:
            cmp = rq.compare(r, graph)
            sids: dict[tuple[str, str], list[str]] = {}
            for it in rq._all_items(r.items):
                sids.setdefault((it.kind, rq._norm(it.title)), []).append(f"{it.kind}:L{it.line}")
            outcomes = _coverage(cmp, missed)
            covered = None if any(o["covered"] == "unknown" for o in outcomes) or not outcomes else \
                all(o["covered"] == "covered" for o in outcomes)
            item["comparison"] = {
                "line_sid": cmp.line_sid, "line_title": cmp.line_title, "matches": cmp.matches, "notes": cmp.notes,
                "differences": [{"kind": d.kind, "text": d.text, "type": d.type, "title": d.title, "node": d.node,
                                 "design_nodes": sids.get((d.type, rq._norm(d.title)), []) if d.kind != "extra" else []}
                                for d in cmp.differences],
                "outcomes": outcomes}
            item["reachable_status"] = rq.reachable_status(r, cmp, covered)
        out.append(item)
    return {"requirements": out, "graph": json.loads(to_json(design)), "run": run_id if missed is not None else None}
