"""读源码片段：路径安全（..、绝对路径、指到外面的符号链接、非 .py）、行数上限、越界行号、stale。
规则沿用阶段 3 原型的开发服务器接口 /__source（已删除）。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pystepmap_server.sources import (
    MAX_LINES,
    SourceError,
    get_snippet,
    read_snippet,
    resolve_source_path,
    root_problem,
)

APP = "".join(f"{s}\n" for s in [
    "import os", "", "", "def handle(order):", '    """处理一笔订单。"""', "    if not order:",
    "        return None", "    return order", "# end"])


@pytest.fixture
def tree(tmp_path) -> Path:
    """proj 是项目根，outside 在它外面。proj 里的符号链接：escape.py → ../outside/secret.py，linkdir → ../outside，
    sneaky.py → notes.txt，alias.py → pkg/mod.py（项目里指项目里，可以）。"""
    proj, outside = tmp_path / "proj", tmp_path / "outside"
    (proj / "pkg").mkdir(parents=True)
    outside.mkdir()
    (proj / "app.py").write_text(APP, encoding="utf-8")
    (proj / "pkg" / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (proj / "notes.txt").write_text("# 不是 py\n", encoding="utf-8")
    (proj / "dir.py").mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    os.symlink("../outside/secret.py", proj / "escape.py")
    os.symlink("../outside", proj / "linkdir")
    os.symlink("notes.txt", proj / "sneaky.py")
    os.symlink("pkg/mod.py", proj / "alias.py")
    os.symlink("loop.py", proj / "loop.py")
    return proj


def status_of(root, file) -> int:
    with pytest.raises(SourceError) as e:
        resolve_source_path(str(root), file)
    assert e.value.message  # 中文说明
    return e.value.status


def test_files_inside_the_project(tree):
    real = tree.resolve()
    assert resolve_source_path(str(tree), "app.py") == real / "app.py"
    assert resolve_source_path(str(tree), "pkg/mod.py") == real / "pkg" / "mod.py"
    assert resolve_source_path(str(tree), "./pkg/mod.py") == real / "pkg" / "mod.py"
    assert resolve_source_path(str(tree), "alias.py") == real / "pkg" / "mod.py"


def test_parent_segments_are_refused_even_when_they_come_back(tree):
    for f in ["../outside/secret.py", "pkg/../../outside/secret.py", "pkg/../app.py", "..\\outside\\secret.py", ".."]:
        assert status_of(tree, f) == 400, f


def test_absolute_paths_are_refused(tree):
    for f in [str(tree.parent / "outside" / "secret.py"), str(tree / "app.py"), "/etc/hosts.py", "C:\\x.py",
              "\\\\server\\x.py"]:
        assert status_of(tree, f) == 400, f


def test_symlinks_leading_outside_are_refused(tree):
    assert status_of(tree, "escape.py") == 400
    assert status_of(tree, "linkdir/secret.py") == 400


def test_non_py_files_are_refused(tree):
    assert status_of(tree, "notes.txt") == 400
    assert status_of(tree, "sneaky.py") == 400  # 名字是 .py，实际指向 .txt
    assert status_of(tree, "pkg") == 400


def test_missing_and_odd_paths(tree, tmp_path):
    assert status_of(tree, "nope.py") == 404
    assert status_of(tree, "dir.py") == 404  # 目录不是文件
    assert status_of(tree, "loop.py") == 404  # 自己指向自己的链接
    assert status_of(tree, "") == 400
    assert status_of(tree, "app.py\0.py") == 400
    assert status_of(tmp_path / "no-such-root", "app.py") == 404
    assert status_of("relative/root", "app.py") == 404


def test_root_must_still_be_the_directory_that_was_added(tree, tmp_path):
    """存下的根目录是解析后的真实路径：它现在是符号链接（目录被移走、原地换成指向别处的链接）或者不再是目录时拒绝，
    不能顺着链接读到别的目录里去。"""
    os.symlink(tree, tmp_path / "link-to-proj")
    with pytest.raises(SourceError) as e:
        resolve_source_path(str(tmp_path / "link-to-proj"), "app.py")
    assert e.value.status == 400 and "不是添加时的那个目录" in e.value.message
    outside = tmp_path / "outside"
    os.rename(tree, tmp_path / "proj-moved")
    os.symlink(outside, tree)  # 原来的路径换成指向项目之外的链接
    for file in ("secret.py", "app.py"):
        with pytest.raises(SourceError) as e:
            get_snippet(str(tree), {}, file, "1", "5")
        assert e.value.status == 400 and "请删除后重新添加" in e.value.message
    os.unlink(tree)
    (tmp_path / "proj").write_text("现在是个文件\n")
    assert status_of(tree, "app.py") == 400
    assert root_problem(str(tmp_path / "proj-moved")) is None
    assert root_problem(str(tmp_path / "nope"))[0] == 404


def test_read_lines_by_number(tree):
    s = read_snippet(tree / "app.py", "app.py", 4, 6, None)
    assert s == {"file": "app.py", "start": 4, "end": 6, "total": 9, "stale": None,
                 "lines": ["def handle(order):", '    """处理一笔订单。"""', "    if not order:"]}


def test_lines_past_the_end(tree):
    s = read_snippet(tree / "app.py", "app.py", 8, 20, None)
    assert (s["start"], s["end"], s["total"], s["lines"]) == (8, 9, 9, ["    return order", "# end"])
    s = read_snippet(tree / "app.py", "app.py", 30, 40, None)
    assert (s["start"], s["end"], s["total"], s["lines"]) == (30, 29, 9, [])


def test_line_cap(tmp_path):
    p = tmp_path / "big.py"
    p.write_text("".join(f"x{i} = {i}\n" for i in range(1000)), encoding="utf-8")
    s = read_snippet(p, "big.py", 10, 100_000, None)
    assert s["end"] - s["start"] + 1 == MAX_LINES and len(s["lines"]) == MAX_LINES and s["total"] == 1000
    assert s["lines"][0] == "x9 = 9"


def test_stale_by_hash(tree):
    raw = (tree / "app.py").read_bytes()
    assert read_snippet(tree / "app.py", "app.py", 1, 2, hashlib.sha256(raw).hexdigest())["stale"] is False
    assert read_snippet(tree / "app.py", "app.py", 1, 2, "0" * 64)["stale"] is True


def test_lines_split_like_the_parser(tmp_path):
    """只在 \\r\\n、\\r、\\n 处分行（和图里的行号一致）；按编码声明解码、去掉 BOM；解不开的字节不报错。"""
    p = tmp_path / "a.py"
    p.write_bytes(b"a = 1\r\nb = '\x0c'\rc = 3\n")
    assert read_snippet(p, "a.py", 1, 9, None)["lines"] == ["a = 1", "b = '\x0c'", "c = 3"]
    p.write_bytes("# -*- coding: gbk -*-\nname = '中文'\n".encode("gbk"))
    assert read_snippet(p, "a.py", 2, 2, None)["lines"] == ["name = '中文'"]
    p.write_bytes(b"\xef\xbb\xbfx = 1\n")
    assert read_snippet(p, "a.py", 1, 1, None)["lines"] == ["x = 1"]
    p.write_bytes(b"x = '\xff'\n")
    assert read_snippet(p, "a.py", 1, 1, None)["lines"] == ["x = '\ufffd'"]
    p.write_bytes(b"")
    assert read_snippet(p, "a.py", 1, 5, None) == {"file": "a.py", "start": 1, "end": 0, "total": 0, "lines": [],
                                                   "stale": None}


def test_get_snippet_checks_the_query(tree):
    hashes = {"app.py": hashlib.sha256((tree / "app.py").read_bytes()).hexdigest(), "pkg/mod.py": "0" * 64}
    s = get_snippet(str(tree), hashes, "app.py", "4", "5")
    assert s["lines"] == ["def handle(order):", '    """处理一笔订单。"""'] and s["stale"] is False
    assert get_snippet(str(tree), hashes, "pkg/mod.py", "1", "1")["stale"] is True
    assert get_snippet(str(tree), hashes, "alias.py", "1", "1")["stale"] is None  # 图里没有这个文件的指纹
    for file, start, end in [("app.py", "0", "2"), ("app.py", "3", "2"), ("app.py", "x", "2"), ("app.py", None, "2"),
                             ("app.py", "1", "-1"), ("app.py", "1", "1.5"), ("app.py", "1", "9999999999"),
                             (None, "1", "2"), ("../outside/secret.py", "1", "2"), ("escape.py", "1", "2")]:
        with pytest.raises(SourceError) as e:
            get_snippet(str(tree), hashes, file, start, end)
        assert e.value.status == 400, (file, start, end)
    with pytest.raises(SourceError) as e:
        get_snippet(str(tree), hashes, "nope.py", "1", "2")
    assert e.value.status == 404
