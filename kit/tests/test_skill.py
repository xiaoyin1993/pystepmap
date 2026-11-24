"""flow-style skill 与检查器一致。

skill 目录里的每个 .md 文件中，每个 python 代码块（```python 或 ```py）的上一行都有标注：
- `<!-- violates: SM001 -->`（违规写法，可以写几个代码）：报出的恰好是标注的规则，而且恰好在代码里标了 `# ← FMxxx`
  的那几行；后面紧跟一个改法；
- `<!-- ok -->`（改法、允许的写法）：零违规；
- `<!-- missed: 说明 -->`（检查器查不出来、但约定不允许的写法）：零违规，并用 `# ← 漏报` 标出是哪几行。哪天检查器
  开始报它们，这里会失败，提醒更新 skill。
SM001–SM004 每条都要有违规写法。
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
from pystepmap.lint import check_project

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / ".claude" / "skills" / "flow-style"
SKILL = SKILL_DIR / "SKILL.md"
TAG = re.compile(r"<!--\s*(ok|missed:[^>]*|violates:((?:\s*SM\d{3})+))\s*-->")
MARK = re.compile(r"# ← (SM\d{3})")
MISSED = "# ← 漏报"
RULES = {"SM001", "SM002", "SM003", "SM004"}
PY_FENCE = re.compile(r"```\s*(python|py)\s*")


def skill_blocks(path: Path) -> list[tuple[str, str | None, set[str], str]]:
    """[(文件名:代码块起始行号, "ok" / "violates" / "missed" / None, 标注的规则, 代码)]，按出现顺序。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    i = 0
    while i < len(lines):
        if PY_FENCE.fullmatch(lines[i].strip()):
            j = i + 1
            while lines[j].strip() != "```":
                j += 1
            k = i - 1
            while k >= 0 and not lines[k].strip():
                k -= 1
            m = TAG.fullmatch(lines[k].strip()) if k >= 0 else None
            kind = None if m is None else m.group(1).split(":")[0]
            codes = set(m.group(2).split()) if m and m.group(2) else set()
            out.append((f"{path.name}:{i + 1}", kind, codes, textwrap.dedent("\n".join(lines[i + 1:j])) + "\n"))
            i = j
        i += 1
    return out


FILES = sorted(SKILL_DIR.glob("*.md")) if SKILL_DIR.exists() else []
BLOCKS_BY_FILE = {f.name: skill_blocks(f) for f in FILES}
BLOCKS = [b for blocks in BLOCKS_BY_FILE.values() for b in blocks]


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    head = text.split("---\n")[1]
    assert text.startswith("---\nname: flow-style\ndescription: ")
    assert "pystepkit" in head and "\n" not in head.split("description: ", 1)[1].strip()  # description 写成一行
    assert "uv run pystepmap check . --files" in text and "uv run pystepmap analyze" in text


def test_skill_is_split_and_short():
    """SKILL.md 控制在 455 行以内；DBOS 的部分在 dbos.md，SKILL.md 里有指向它的链接。"""
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) <= 455
    assert {"SKILL.md", "dbos.md"} <= set(BLOCKS_BY_FILE)
    assert "(dbos.md)" in SKILL.read_text(encoding="utf-8")


def test_route_functions_as_workflows_have_their_own_page():
    """接口函数当编排函数（@workflow 写在路由装饰器上面，出口用 return 响应和 raise HTTPException，不用 outcome）
    在 routes.md；SKILL.md 讲出口的那一条和 description 里有指向它的说明。"""
    skill = SKILL.read_text(encoding="utf-8")
    assert "routes.md" in BLOCKS_BY_FILE and "(routes.md)" in skill
    assert "接口函数" in skill.split("---\n")[1]
    text = (SKILL_DIR / "routes.md").read_text(encoding="utf-8")
    for needle in ("@workflow", "raise HTTPException", "outcome", "404 用户不存在", "--treat-as-marked"):
        assert needle in text
    [code] = [code for n, kind, _, code in BLOCKS_BY_FILE["routes.md"] if kind == "ok" and "@router." in code][:1]
    rows = code.splitlines()
    mark = next(i for i, r in enumerate(rows) if r.startswith("@workflow"))
    assert rows[mark + 1].startswith("@router.")  # 示例里 @workflow 写在路由装饰器上面
    assert "return outcome(" not in code and "raise HTTPException(" in code


def test_every_python_block_is_tagged_and_paired():
    assert BLOCKS, "skill 里没有代码块"
    assert [n for n, kind, _, _ in BLOCKS if kind is None] == []  # 每个 python 代码块都标注了
    for blocks in BLOCKS_BY_FILE.values():
        for (n, kind, _, _), nxt in zip(blocks, [*blocks[1:], None]):
            if kind == "violates":
                assert nxt is not None and nxt[1] == "ok", f"{n} 的违规写法后面要紧跟改法"
    assert set().union(*(codes for _, kind, codes, _ in BLOCKS if kind == "violates")) == RULES
    assert any(kind == "missed" for _, kind, _, _ in BLOCKS)


def test_untagged_py_fence_is_caught(tmp_path):
    """```py 也算 python 代码块：没有标注的会被上面的检查发现。"""
    f = tmp_path / "x.md"
    f.write_text("说明\n\n```py\nprint(1)\n```\n\n<!-- ok -->\n```python\nx = 1\n```\n", encoding="utf-8")
    assert [kind for _, kind, _, _ in skill_blocks(f)] == [None, "ok"]


@pytest.mark.parametrize("block", BLOCKS, ids=[f"line{n}-{kind}" for n, kind, _, _ in BLOCKS])
def test_block_agrees_with_checker(tmp_path, block):
    n, kind, codes, code = block
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "flow.py").write_text(code, encoding="utf-8")
    result = check_project(tmp_path)
    assert result.skipped == [], f"{n} 的代码块解析不了"
    got = {(v.line, v.code) for v in result.violations}
    marked = {(i, m.group(1)) for i, line in enumerate(code.splitlines(), 1) for m in MARK.finditer(line)}
    if kind == "ok":
        assert got == set() and marked == set(), f"{n} 的改法：{result.violations}"
    elif kind == "missed":
        assert got == set() and marked == set(), f"{n} 标成漏报，检查器却报了：{result.violations}"
        assert MISSED in code, f"{n} 要用 {MISSED} 标出漏报的行"
    else:
        assert {c for _, c in got} == codes, f"{n}：标注 {codes}，检查器报 {result.violations}"
        assert got == marked, f"{n}：标了 ← 的行 {sorted(marked)}，检查器报在 {sorted(got)}"
