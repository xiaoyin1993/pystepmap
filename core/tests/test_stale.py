"""文件指纹比对：current_hashes 按出图时同样的规则列出、读取每个 .py，结果和图的 file_hashes 同口径；
compare_hashes 比出改过、新增、删掉的文件。"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import write_files
from pystepmap.analyze import analyze_project
from pystepmap.config import ProjectConfig
from pystepmap.stale import FileChanges, compare_hashes, current_hashes


def _project(tmp_path):
    """一个小项目：正常的包、语法错误的文件、编码声明不对的文件、默认排除的目录（tests、.venv）、项目之内指向
    .py 的符号链接、指向项目之外的符号链接、项目之内指向非 .py 文件的链接、失效的链接。"""
    root, outside = tmp_path / "proj", tmp_path / "outside"
    write_files(root, {
        "app/__init__.py": "",
        "app/api.py": "from app.svc import run\n\n\ndef handle():\n    return run()\n",
        "app/svc.py": "def run():\n    return 1\n",
        "app/broken.py": "def broken(:\n",
        "tests/test_api.py": "def test_x():\n    assert True\n",
        ".venv/lib/site.py": "x = 1\n",
        "README.txt": "def hidden():\n    return 3\n",
    })
    (root / "app/latin.py").write_bytes(b"# -*- coding: ascii -*-\nname = '\xe4'\n")
    write_files(outside, {"secret.py": "TOKEN = 'SECRET'\n"})
    os.symlink("app/svc.py", root / "alias.py")
    os.symlink("../outside/secret.py", root / "escape.py")
    os.symlink("README.txt", root / "notes.py")
    os.symlink("nowhere.py", root / "gone.py")
    return root


def test_current_hashes_match_analysis(tmp_path):
    root = _project(tmp_path)
    for cfg in (ProjectConfig(), ProjectConfig(exclude=[".venv"]), ProjectConfig(extra_exclude=["app"])):
        graph = analyze_project(root, cfg)
        assert current_hashes(root, cfg) == graph.file_hashes
    hashes = current_hashes(root, ProjectConfig())
    assert {"app/broken.py", "app/latin.py", "alias.py"} <= set(hashes)  # 解析失败的、链接到项目之内 .py 的照样算
    assert not {"escape.py", "notes.py", "gone.py", "tests/test_api.py", ".venv/lib/site.py"} & set(hashes)
    assert hashes["alias.py"] == hashes["app/svc.py"]
    assert list(hashes) == sorted(hashes)


def test_current_hashes_from_a_relative_or_linked_root(tmp_path, monkeypatch):
    """根目录写成相对路径、或者是指向项目的符号链接时，结果和分析一样（分析先把根目录解析成绝对路径）。"""
    root = _project(tmp_path)
    os.symlink("proj", tmp_path / "proj_link")
    expected = analyze_project(root).file_hashes
    monkeypatch.chdir(tmp_path)
    assert current_hashes(Path("proj"), ProjectConfig()) == expected
    assert current_hashes(tmp_path / "proj_link", ProjectConfig()) == expected


def test_compare():
    changes = compare_hashes({"a.py": "1", "b.py": "2", "c.py": "3"}, {"a.py": "1", "b.py": "9", "d.py": "4"})
    assert changes == FileChanges(changed=["b.py"], added=["d.py"], removed=["c.py"])
    assert changes.any
    same = compare_hashes({"a.py": "1", "b.py": "2"}, {"b.py": "2", "a.py": "1"})
    assert same == FileChanges(changed=[], added=[], removed=[])
    assert not same.any


def test_compare_sorts_each_list():
    changes = compare_hashes({"z.py": "1", "m.py": "1", "b/x.py": "1", "a.py": "1"},
                             {"z.py": "2", "a.py": "2", "y.py": "1", "c.py": "1"})
    assert changes.changed == ["a.py", "z.py"]
    assert changes.added == ["c.py", "y.py"]
    assert changes.removed == ["b/x.py", "m.py"]


def test_extra_exclude_respected(tmp_path):
    root = tmp_path / "proj"
    write_files(root, {"app/a.py": "def f():\n    return 1\n", "generated/b.py": "def g():\n    return 2\n",
                       "app/generated/c.py": "def h():\n    return 3\n"})
    cfg = ProjectConfig(extra_exclude=["generated"])
    hashes = current_hashes(root, cfg)
    assert set(hashes) == {"app/a.py"}
    assert hashes == analyze_project(root, cfg).file_hashes
    assert set(current_hashes(root, ProjectConfig())) == {"app/a.py", "generated/b.py", "app/generated/c.py"}
