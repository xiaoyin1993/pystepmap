"""OpenSpec 规则片段：能贴进 openspec/config.yaml 的 rules，要求列出流程变化、任务清单里有检查和重新出图两项。"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNIPPET = REPO / "kit" / "openspec" / "flow-rules.yaml"


def test_snippet_shape():
    text = SNIPPET.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = [ln for ln in lines if ln.startswith("#")]
    body = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    assert any("config.yaml" in ln for ln in header) and any("不强制" in ln for ln in header)
    assert body[0] == "rules:"
    # rules 下面是产物名（两格缩进）和它的规则列表（四格缩进的 "- "，规则写成一行带引号的字符串）
    artifacts = [ln.strip().rstrip(":") for ln in body[1:] if re.fullmatch(r"  [a-z]+:", ln)]
    items = [ln for ln in body[1:] if not re.fullmatch(r"  [a-z]+:", ln)]
    assert {"proposal", "tasks"} <= set(artifacts) <= {"proposal", "specs", "design", "tasks"}
    assert all(re.fullmatch(r'    - "[^"]+"', ln) for ln in items), items
    section = text.split("  tasks:", 1)[1]
    assert "零违规" in section and "重新生成流程图" in section
    assert "流程变化" in text.split("  proposal:", 1)[1].split("\n  ", 1)[1]
