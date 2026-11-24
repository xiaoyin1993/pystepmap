"""过期检查：没改不过期；改、增、删的文件各归各类，lines 是涉及改了 / 删了的文件的功能线；读不了的文件（没有权限、
所在目录读不了、换成了指到项目外的链接）单列为 unreadable，不算删除；目录没了、被换成符号链接、读不了时"没法检查"
并说明原因；旧版本存的分析（没有 file_lines）lines 为 None；每类最多列 200 个；按给的配置排除目录。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from pystepmap.analyze import analyze_project
from pystepmap.config import parse_config
from pystepmap.query import file_lines
from pystepmap.stale import current_hashes
from pystepmap_server import status
from pystepmap_server.status import MAX_FILES, staleness

FLOWS = '''
from pystepkit import outcome, workflow
from steps import load_stock, refund_money


@workflow(title="下单")
def place_order(order):
    load_stock(order)
    return outcome("ok", title="下单完成")


@workflow(title="退款")
def refund(order):
    refund_money(order)
    return outcome("ok", title="退款完成")
'''

STEPS = '''
from pystepkit import step


@step(title="查询库存")
def load_stock(order):
    return 1


@step(title="退钱")
def refund_money(order):
    return 2
'''

AUDIT = '''
from pystepkit import outcome, workflow


@workflow(title="审计")
def audit(x):
    return outcome("ok", title="审计完成")
'''


@pytest.fixture
def project(tmp_path) -> tuple[Path, dict]:
    """三个文件：flows.py（下单、退款两条线）、steps.py（两条线的步骤）、audit.py（审计一条线）。返回 (根目录, 分析)。"""
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in (("flows.py", FLOWS), ("steps.py", STEPS), ("audit.py", AUDIT)):
        (root / name).write_text(text, encoding="utf-8")
    graph = analyze_project(root)
    return root.resolve(), {"hashes": graph.file_hashes, "file_lines": file_lines(graph),
                            "sids": {ln.title: ln.sid for ln in graph.lines}}


def check(root: Path, facts: dict, config: dict | None = None, *, lines: bool = True) -> dict:
    return staleness(str(root), config or {}, facts["hashes"], facts["file_lines"] if lines else None)


def test_nothing_changed(project):
    root, facts = project
    assert check(root, facts) == {"checked": True, "stale": False, "changed": [], "added": [], "removed": [],
                                  "unreadable": [], "lines": [],
                                  "total": {"changed": 0, "added": 0, "removed": 0, "unreadable": 0}}


def test_fingerprints_are_the_same_as_cores(project):
    """读文件、算指纹和 core 的 current_hashes 同口径（另外记下它默默跳过的）。"""
    root, _ = project
    (root / "gen").mkdir()
    (root / "gen" / "x.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink("/etc/hosts", root / "outside.py")  # 指到项目外：两边都不读
    cfg = parse_config({"extra_exclude": ["gen"]})
    hashes, unreadable = status._scan(root, cfg)
    assert hashes == current_hashes(root, cfg) and set(unreadable) == {"outside.py"}


needs_permissions = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="需要非 root 的 POSIX 权限")


@needs_permissions
def test_unreadable_file_is_not_reported_as_removed(project):
    root, facts = project
    os.chmod(root / "steps.py", 0)
    try:
        r = check(root, facts)
    finally:
        os.chmod(root / "steps.py", 0o644)
    assert (r["changed"], r["added"], r["removed"], r["unreadable"]) == ([], [], [], ["steps.py"])
    assert r["stale"] is False and r["lines"] == []
    assert r["total"] == {"changed": 0, "added": 0, "removed": 0, "unreadable": 1}
    assert r["note"] == "有 1 个文件读不了，没有比对"


@needs_permissions
@pytest.mark.parametrize("mode", [0, 0o100])  # 进不去；进得去但列不出里面的文件
def test_files_under_an_unreadable_directory(project, mode):
    root, facts = project
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    graph = analyze_project(root)
    facts = {"hashes": graph.file_hashes, "file_lines": file_lines(graph)}
    (root / "audit.py").unlink()  # 真删掉的照样算删除
    os.chmod(root / "sub", mode)
    try:
        r = check(root, facts)
    finally:
        os.chmod(root / "sub", 0o755)
    assert (r["removed"], r["unreadable"]) == (["audit.py"], ["sub/b.py"])
    assert r["stale"] is True and r["note"] == "有 1 个文件读不了，没有比对"


def test_file_replaced_by_a_link_outside_the_project(project):
    """分析时是普通文件，现在换成了指到项目外的符号链接：不去读它，单列为读不了；新出现的这种链接不报（分析也不读）。"""
    root, facts = project
    (root / "audit.py").unlink()
    os.symlink("/etc/hosts", root / "audit.py")
    os.symlink("/etc/hosts", root / "new_link.py")
    r = check(root, facts)
    assert (r["removed"], r["added"], r["unreadable"]) == ([], [], ["audit.py"]) and r["stale"] is False


def test_changed_file_names_the_lines_that_touch_it(project):
    root, facts = project
    (root / "steps.py").write_text(STEPS + "\n# 改了\n", encoding="utf-8")
    r = check(root, facts)
    assert r["checked"] is True and r["stale"] is True
    assert (r["changed"], r["added"], r["removed"]) == (["steps.py"], [], [])
    assert sorted(r["lines"]) == sorted([facts["sids"]["下单"], facts["sids"]["退款"]])
    assert r["total"] == {"changed": 1, "added": 0, "removed": 0, "unreadable": 0}


def test_added_and_removed_files(project):
    root, facts = project
    (root / "new.py").write_text("x = 1\n", encoding="utf-8")
    (root / "audit.py").unlink()
    r = check(root, facts)
    assert (r["changed"], r["added"], r["removed"]) == ([], ["new.py"], ["audit.py"])
    assert r["lines"] == [facts["sids"]["审计"]]  # 新文件不在任何线里；删掉的文件涉及的线算在内
    assert r["stale"] is True


def test_old_analysis_without_file_lines(project):
    root, facts = project
    (root / "flows.py").write_text(FLOWS + "\n", encoding="utf-8")
    r = check(root, facts, lines=False)
    assert r["changed"] == ["flows.py"] and r["lines"] is None


def test_config_is_respected(project):
    root, facts = project
    (root / "gen").mkdir()
    (root / "gen" / "big.py").write_text("x = 1\n", encoding="utf-8")
    assert check(root, facts)["added"] == ["gen/big.py"]
    assert check(root, facts, {"extra_exclude": ["gen"]})["stale"] is False


def test_long_lists_are_capped(project):
    root, facts = project
    for i in range(MAX_FILES + 50):
        (root / f"m{i:03}.py").write_text("x = 1\n", encoding="utf-8")
    r = check(root, facts)
    assert MAX_FILES == 200 and len(r["added"]) == 200 and r["added"] == sorted(r["added"])
    assert r["total"] == {"changed": 0, "added": 250, "removed": 0, "unreadable": 0} and "note" not in r


def test_directory_removed(project):
    root, facts = project
    shutil.rmtree(root)
    r = check(root, facts)
    assert r["checked"] is False and "项目目录不存在了" in r["reason"] and set(r) == {"checked", "reason"}


def test_directory_replaced_by_a_symlink(project, tmp_path):
    root, facts = project
    os.rename(root, tmp_path / "moved")
    os.symlink(tmp_path / "moved", root)  # 指向原来的内容也不行：不顺着换上的链接去读
    r = check(root, facts)
    assert r["checked"] is False and "不是添加时的那个目录" in r["reason"]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="需要非 root 的 POSIX 权限")
def test_directory_that_cannot_be_read(project):
    root, facts = project
    os.chmod(root, 0)
    try:
        r = check(root, facts)
    finally:
        os.chmod(root, 0o755)
    assert r["checked"] is False and "没有权限" in r["reason"]


def test_bad_config(project):
    root, facts = project
    r = check(root, facts, {"nope": 1})
    assert r["checked"] is False and "分析配置不合法" in r["reason"]


def test_results_match_the_engine_rules(project):
    """和图的 file_hashes 同口径：用同一个配置重新分析，指纹完全一致（不会把没改的报成改了）。"""
    root, _ = project
    cfg = {"exclude": [".venv"]}
    graph = analyze_project(root, parse_config(cfg))
    r = staleness(str(root), cfg, graph.file_hashes, file_lines(graph))
    assert r["stale"] is False
