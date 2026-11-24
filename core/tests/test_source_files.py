"""收集源文件时不跟随指向项目之外的符号链接，不读非普通文件（FIFO、设备），也不读项目之内指向非 .py 文件的链接：
这些记进跳过的文件，从不打开；pystepmap check 把它们列成警告，不算解析不了。符号链接的目录本来就不进入（os.walk
默认不跟随）。项目之内指向 .py 普通文件的符号链接照旧按链接的路径分析。"""

from __future__ import annotations

import os
import threading

import pytest
from conftest import write_files
from pystepmap.analyze import analyze_project
from pystepmap.cli import main

OUTSIDE = 'def leaked():\n    TOKEN = "SECRET-TOKEN-123"\n    return TOKEN\n'


@pytest.fixture
def proj(tmp_path):
    root, outside = tmp_path / "proj", tmp_path / "outside"
    write_files(root, {"app.py": "def handle():\n    return 1\n", "pkg/__init__.py": "", "pkg/mod.py": "def f():\n    return 2\n"})
    write_files(outside, {"secret.py": OUTSIDE, "private.txt": "PRIVATE-TEXT-456\n"})
    return root


def skipped_of(graph) -> dict[str, str]:
    return dict(graph.selfcheck.skipped_files)


def test_links_leading_outside_are_skipped_and_never_read(proj):
    os.symlink("../outside/secret.py", proj / "escape.py")
    os.symlink(str(proj.parent / "outside" / "private.txt"), proj / "abs_escape.py")
    os.symlink("/dev/null", proj / "devnull.py")
    os.symlink("../nowhere.py", proj / "dangling_out.py")
    g = analyze_project(proj)
    skipped = skipped_of(g)
    for name in ("escape.py", "abs_escape.py", "devnull.py", "dangling_out.py"):
        assert skipped[name] == "符号链接指向项目之外"
        assert name not in g.file_hashes
    assert not any(f.qualname == "leaked" for f in g.functions.values())
    assert set(g.file_hashes) == {"app.py", "pkg/__init__.py", "pkg/mod.py"}


def test_fifo_is_skipped_without_hanging(proj):
    os.mkfifo(proj / "pipe.py")
    os.mkfifo(proj / "real_pipe")
    os.symlink("real_pipe", proj / "pipe_link.py")  # 项目之内指向 FIFO 的链接：同样不读
    done: list = []
    t = threading.Thread(target=lambda: done.append(analyze_project(proj)), daemon=True)
    t.start()
    t.join(10)
    if t.is_alive():  # 卡住了：往正在被读的 FIFO 里写一点让分析线程结束，再判失败
        for name in ("pipe.py", "real_pipe"):
            try:
                fd = os.open(proj / name, os.O_WRONLY | os.O_NONBLOCK)  # 没有读者时 ENXIO，跳过
            except OSError:
                continue
            os.write(fd, b"x = 1\n")
            os.close(fd)
        pytest.fail("分析在 FIFO 上卡住了")
    skipped = skipped_of(done[0])
    assert skipped["pipe.py"] == "不是普通文件" and skipped["pipe_link.py"] == "不是普通文件"
    assert "pipe.py" not in done[0].file_hashes


def test_linked_directories_are_not_followed(proj):
    os.symlink("../outside", proj / "linkdir")
    os.symlink("pkg", proj / "pkg_alias")
    g = analyze_project(proj)
    assert set(g.file_hashes) == {"app.py", "pkg/__init__.py", "pkg/mod.py"}
    assert skipped_of(g) == {}


def test_link_to_a_file_inside_the_project_is_analysed_as_before(proj):
    """项目之内的链接照旧按链接自己的路径分析（目标文件也照常分析）：改成跳过链接会改变已有项目的图。"""
    os.symlink("pkg/mod.py", proj / "alias.py")
    g = analyze_project(proj)
    assert {"alias.py", "pkg/mod.py"} <= set(g.file_hashes)
    assert g.file_hashes["alias.py"] == g.file_hashes["pkg/mod.py"]
    assert skipped_of(g) == {}


def test_dangling_link_inside_the_project_is_reported_as_before(proj):
    os.symlink("nowhere.py", proj / "gone.py")
    reason = skipped_of(analyze_project(proj))["gone.py"]
    assert reason.startswith("无法解析：FileNotFoundError")


def test_analyze_cli_lists_the_skipped_links(proj, tmp_path, capsys):
    os.symlink("../outside/secret.py", proj / "escape.py")
    assert main(["analyze", str(proj), "-o", str(tmp_path / "g.json")]) == 0
    out = capsys.readouterr().out
    assert "跳过的文件: 1" in out and "escape.py: 符号链接指向项目之外" in out


def test_link_inside_the_project_to_a_non_py_file_is_skipped(proj):
    """项目之内、名字是 .py 却指向别的文件（README.txt）的链接：读源码的接口只给 .py 文件，分析了也看不到源码，
    所以和指到外面的链接一样跳过、不读。链到 .py 的链（a.py → b.py → pkg/mod.py）照常分析。"""
    (proj / "README.txt").write_text("def hidden():\n    return 3\n", encoding="utf-8")
    os.symlink("README.txt", proj / "notes.py")
    os.symlink("pkg/mod.py", proj / "b.py")
    os.symlink("b.py", proj / "a.py")
    g = analyze_project(proj)
    assert skipped_of(g) == {"notes.py": "符号链接指向的不是 .py 文件"}
    assert "notes.py" not in g.file_hashes
    assert not any(f.qualname == "hidden" for f in g.functions.values())
    assert {"a.py", "b.py", "pkg/mod.py"} <= set(g.file_hashes)


def test_check_reports_links_and_special_files_as_warnings(proj, capsys):
    """pystepmap check：没读的链接和特殊文件不是"解析不了"，单独列成警告（stderr），不改变退出码；真的解析不了的
    文件仍然让退出码为 2。"""
    os.symlink("../outside/secret.py", proj / "escape.py")
    os.mkfifo(proj / "pipe.py")
    (proj / "README.txt").write_text("x\n", encoding="utf-8")
    os.symlink("README.txt", proj / "notes.py")
    assert main(["check", str(proj)]) == 0
    cap = capsys.readouterr()
    assert "共 0 处违规" in cap.out
    assert "解析不了" not in cap.err
    assert "警告: 以下 3 个 .py 路径不是项目里的源文件，没有读，也没有检查:" in cap.err
    for line in ("  escape.py: 符号链接指向项目之外", "  pipe.py: 不是普通文件", "  notes.py: 符号链接指向的不是 .py 文件"):
        assert line in cap.err.splitlines()
    # --files 只选了别的文件：不提这些路径
    assert main(["check", str(proj), "--files", "app.py"]) == 0
    assert capsys.readouterr().err == ""
    # 再加一个真的解析不了的文件：退出码 2，两种分开列
    (proj / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    assert main(["check", str(proj)]) == 2
    err = capsys.readouterr().err
    assert "以下 1 个文件解析不了，没有检查:" in err and "  broken.py: 无法解析" in err
    assert "警告: 以下 3 个 .py 路径" in err
