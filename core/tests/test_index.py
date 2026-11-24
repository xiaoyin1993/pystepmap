import ast
import json

import pytest
from conftest import find_fn, make_project
from pystepmap.analyze import analyze_project
from pystepmap.cli import _Source, main
from pystepmap.config import (
    DEFAULT_EXCLUDE,
    ConfigError,
    ProjectConfig,
    load_config,
    parse_config,
)
from pystepmap.index import build_index, source_lines
from pystepmap.resolve import Resolver


def test_qualnames_and_keys(tmp_path):
    root = make_project(tmp_path, {"app/t.py": """
        import os
        from app.db import SessionLocal as S
        class Svc:
            async def run(self): ...
        def outer():
            def reject(): ...
        def other():
            def reject(): ...
        svc = Svc()
    """, "app/db.py": "SessionLocal = None\n"})
    idx = build_index(root, ProjectConfig())
    quals = sorted(f.qualname for f in idx.functions.values())
    assert quals == ["Svc.run", "other", "other.<locals>.reject", "outer", "outer.<locals>.reject"]
    m = idx.modules["app/t.py"]
    assert m.imports["S"] == "app.db.SessionLocal" and m.instances["svc"] == "app.t.Svc"
    assert len({f.key for f in idx.functions.values()}) == 5  # Review Focus 2：同名内部函数标识不同


def test_bad_files_are_skipped_not_fatal(tmp_path):  # Review Focus 1
    root = make_project(tmp_path, {"app/ok.py": "def f(): ...\n", "app/bad.py": "def (:\n"})
    (root / "app/latin.py").write_bytes("x = 'caf\xe9'\n".encode("latin-1"))
    idx = build_index(root, ProjectConfig())
    assert {f.qualname for f in idx.functions.values()} == {"f"}
    assert sorted(p for p, _ in idx.skipped) == ["app/bad.py", "app/latin.py"]


def test_default_excludes(tmp_path):
    root = make_project(tmp_path, {"app/a.py": "def a(): ...\n", ".venv/x.py": "def v(): ...\n",
                                   "tests/test_a.py": "def t(): ...\n", ".history/a_2026.py": "def h(): ...\n"})
    assert {f.qualname for f in build_index(root, ProjectConfig()).functions.values()} == {"a"}


def test_the_installed_kit_under_dot_claude_is_excluded_by_default(tmp_path):
    """kit install 把 skill 的示例（带 @workflow 的 transfer_workflow.py）和钩子装进 .claude/：默认不分析，不然别的
    项目里会凭空多出一条"账户转账"功能线。"""
    root = make_project(tmp_path, {"app/a.py": "def a(): ...\n",
                                   ".claude/skills/flow-style/transfer_workflow.py": "def run(): ...\n",
                                   ".claude/hooks/check_edited_file.py": "def main(): ...\n"})
    index = build_index(root, ProjectConfig())
    assert {f.qualname for f in index.functions.values()} == {"a"}
    assert index.excluded == {".claude": 1}


def test_extra_exclude_adds_to_the_resolved_exclude_list(tmp_path):
    """extra_exclude 在 exclude 的结果之外再排除几个目录：不写 exclude 时默认的排除目录（.venv、tests……）仍然排除；
    exclude 写了就取代默认的，extra_exclude 加在它后面。"""
    root = make_project(tmp_path, {"app/a.py": "def a(): ...\n", ".venv/x.py": "def v(): ...\n",
                                   "migrations/m.py": "def m(): ...\n", "tests/test_a.py": "def t(): ...\n"})

    def names(cfg: dict) -> set[str]:
        return {f.qualname for f in build_index(root, parse_config(cfg)).functions.values()}

    assert names({}) == {"a", "m"}
    assert names({"extra_exclude": ["migrations"]}) == {"a"}
    assert names({"exclude": ["migrations"]}) == {"a", "v", "t"}  # exclude 取代默认列表：.venv、tests 都进来了
    assert names({"exclude": ["migrations"], "extra_exclude": ["tests"]}) == {"a", "v"}
    cfg = parse_config({"extra_exclude": ["migrations", ".venv"]})
    assert cfg.exclude_dirs == [*DEFAULT_EXCLUDE, "migrations"]  # 重复的只算一次，顺序是 exclude 在前
    assert ProjectConfig().exclude_dirs == DEFAULT_EXCLUDE
    for bad in ("migrations", [1], None):
        with pytest.raises(ConfigError, match="extra_exclude"):
            parse_config({"extra_exclude": bad})


def test_imports_relative_and_dotted(tmp_path):
    root = make_project(tmp_path, {
        "app/__init__.py": "from .x import y\n",
        "app/sub/m.py": """
            import os
            import a.b
            import a.c as c
            from . import z
            from ..x import w as ww
            def f():
                import json
        """,
    })
    idx = build_index(root, ProjectConfig())
    assert idx.modules["app/__init__.py"].dotted == "app"
    assert idx.modules["app/__init__.py"].imports == {"y": "app.x.y"}
    assert idx.modules["app/sub/m.py"].imports == {
        "os": "os", "a": "a", "c": "a.c", "z": "app.sub.z", "ww": "app.x.w", "json": "json"}
    assert idx.by_dotted["app.sub.m"] == "app/sub/m.py"


def test_nested_qualnames_parent_cls_doc(tmp_path):
    root = make_project(tmp_path, {"m.py": '''
        class Svc(Base, mixins.M):
            def run(self):
                """`Run` **it**
                more"""
                def helper(): ...
        def outer():
            class Inner:
                def method(self): ...
    '''})
    idx = build_index(root, ProjectConfig())
    run = find_fn(idx, "Svc.run")
    helper = find_fn(idx, "Svc.run.<locals>.helper")
    method = find_fn(idx, "outer.<locals>.Inner.method")
    assert run.doc_title == "Run it" and run.cls == "Svc" and run.parent is None
    assert helper.parent == run.key
    assert method.cls == "Inner" and method.parent == find_fn(idx, "outer").key
    cls = idx.modules["m.py"].classes["Svc"]
    assert cls.bases == ["Base", "mixins.M"] and cls.methods == {"run": run.key}
    assert len(idx.hashes["m.py"]) == 64


def test_find_fn_ambiguous(tmp_path):
    root = make_project(tmp_path, {"a.py": "def f(): ...\n", "b.py": "def f(): ...\n"})
    with pytest.raises(AssertionError):
        find_fn(build_index(root, ProjectConfig()), "f")


def test_load_config(tmp_path):
    assert load_config(None) == ProjectConfig()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"common_task_threshold": 3}))
    assert load_config(p).common_task_threshold == 3
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        load_config(p)


def test_utf8_bom_file_is_indexed(tmp_path):
    root = make_project(tmp_path, {"app/ok.py": "x = 1\n"})
    (root / "app/bom.py").write_bytes(b"\xef\xbb\xbfdef f(): ...\n")
    idx = build_index(root, ProjectConfig())
    assert find_fn(idx, "f").file == "app/bom.py"
    assert idx.skipped == []
    assert idx.modules["app/bom.py"].lines[0] == "def f(): ..."


def test_coding_cookie_and_bom_are_honoured(tmp_path):
    """PEP 263 的编码声明和 BOM：按声明的编码解析；行文本按同样的编码解码（标题、源码引用用到）。"""
    root = make_project(tmp_path, {"app/ok.py": "x = 1\n"})
    (root / "app/latin.py").write_bytes("# -*- coding: latin-1 -*-\ndef caf\xe9():\n    return 'd\xe9j\xe0'\n".encode("latin-1"))
    (root / "app/bom.py").write_bytes(b"\xef\xbb\xbf# coding: utf-8\ndef g(): ...\n")
    idx = build_index(root, ProjectConfig())
    assert idx.skipped == []
    assert find_fn(idx, "caf\xe9").file == "app/latin.py"
    assert idx.modules["app/latin.py"].lines[2] == "    return 'd\xe9j\xe0'"
    assert idx.modules["app/bom.py"].lines[0] == "# coding: utf-8"


# ---------- 分析的目录本身是包 ----------


def _resolved(index, qualname):
    """函数体里每个调用的 (显示名, 解析方式, 目标 key)，按源码顺序。"""
    fn = find_fn(index, qualname)
    resolver = Resolver(index)
    calls = sorted((n for st in fn.node.body for n in ast.walk(st) if isinstance(n, ast.Call)),
                   key=lambda c: (c.lineno, c.col_offset))
    return [(r.display, r.how, r.target) for r in (resolver.resolve(c, fn) for c in calls)]


PACKAGE_DIR = {
    "backend/app/__init__.py": "from .x import f\n",
    "backend/app/x.py": "def f(): ...\n",
    "backend/app/sub/__init__.py": "",
    "backend/app/sub/y.py": (
        "from app.x import f\n"
        "from app import f as g\n"
        "import app.x\n"
        "from . import z\n"
        "def run():\n"
        "    f()\n"
        "    g()\n"
        "    app.x.f()\n"
        "    z.h()\n"),
    "backend/app/sub/z.py": "def h(): ...\n",
}


def test_package_directory_as_analysis_root(tmp_path):
    """分析的目录本身含 __init__.py（如直接分析 backend/app）：模块同时登记在"目录名.相对模块名"下，
    from app.x import f 解析到项目里；函数 key 仍相对分析的目录。"""
    make_project(tmp_path, PACKAGE_DIR)
    index = build_index(tmp_path / "backend/app", ProjectConfig())
    assert index.package == "app"
    assert index.source_roots == [""]
    assert index.by_dotted["app.x"] == index.by_dotted["x"] == "x.py"
    assert index.by_dotted["app"] == "__init__.py" and index.by_dotted["app.sub.y"] == "sub/y.py"
    assert sorted(index.functions) == ["sub/y.py::run@5", "sub/z.py::h@1", "x.py::f@1"]
    f = "x.py::f@1"
    assert _resolved(index, "run") == [("f", "exact", f), ("g", "exact", f), ("x.f", "exact", f),
                                       ("z.h", "exact", "sub/z.py::h@1")]


def test_package_prefix_continues_up_through_parent_packages(tmp_path):
    """分析的目录的上级目录也含 __init__.py 时继续向上拼前缀，直到不含为止；包名链的每一段后缀都登记
    （outer.app.x 和 app.x：sys.path 上可以是 repo，也可以是 repo/outer）。"""
    make_project(tmp_path, {
        "repo/outer/__init__.py": "",
        "repo/outer/app/__init__.py": "",
        "repo/outer/app/x.py": "def f(): ...\n",
        "repo/outer/app/y.py": "from outer.app.x import f\nfrom app.x import f as g\ndef run():\n    f()\n    g()\n",
    })
    index = build_index(tmp_path / "repo/outer/app", ProjectConfig())
    assert index.package == "outer.app"
    assert index.by_dotted["outer.app.x"] == index.by_dotted["app.x"] == index.by_dotted["x"] == "x.py"
    assert index.by_dotted["outer.app"] == index.by_dotted["app"] == "__init__.py"
    assert "outer" not in index.by_dotted  # 上级目录自己的 __init__.py 不在分析范围里
    assert _resolved(index, "run") == [("f", "exact", "x.py::f@1"), ("g", "exact", "x.py::f@1")]
    assert build_index(tmp_path / "repo", ProjectConfig()).package == ""  # repo 本身不是包：没有前缀


def test_parent_package_layout_links_imports_by_the_inner_package_name(tmp_path, capsys):
    """backend 和 backend/app 都是包、分析 backend/app 时，from app.x import f 连到项目里（不是项目外、调用不消失）；
    摘要照旧写完整的包名链。"""
    make_project(tmp_path, {
        "backend/__init__.py": "",
        "backend/app/__init__.py": "",
        "backend/app/x.py": "def f(): ...\n",
        "backend/app/y.py": "from app.x import f\ndef g():\n    f()\n",
    })
    g = analyze_project(tmp_path / "backend/app")
    fg = find_fn(g, "g")
    assert fg.call_targets == ["x.py::f@1"] and fg.unresolved == []
    [step] = [n for n in fg.nodes if n.type == "step"]
    assert [(c.name, c.resolution, c.target) for c in step.calls] == [("f", "exact", "x.py::f@1")]
    assert main(["analyze", str(tmp_path / "backend/app"), "-o", str(tmp_path / "g.json")]) == 0
    assert "源码根: .（包 backend.app）\n" in capsys.readouterr().out


def test_summary_notes_package_directory(tmp_path, capsys):
    make_project(tmp_path, PACKAGE_DIR)
    assert main(["analyze", str(tmp_path / "backend/app"), "-o", str(tmp_path / "g.json")]) == 0
    assert "源码根: .（包 app）\n" in capsys.readouterr().out
    assert main(["analyze", str(tmp_path / "backend"), "-o", str(tmp_path / "g2.json")]) == 0
    assert "源码根: .\n" in capsys.readouterr().out  # backend 不是包：不注明


def test_source_lines_split_like_the_parser():
    """只在 \\r\\n、\\r、\\n 处分行（解析器的规则）；换页符、\\x1c、\\u2028 等留在行里。末尾的换行不多出空行。"""
    assert source_lines("a\r\nb\rc\nd\x0ce\x0bf\x1cg\x85h\u2028i\u2029j\n") == \
        ["a", "b", "c", "d\x0ce\x0bf\x1cg\x85h\u2028i\u2029j"]
    assert source_lines("a\n\n") == ["a", ""] and source_lines("") == [] and source_lines("a") == ["a"]


ODD_LINES = "X = 1\n\x0c\n# 说明\u2028续\ndef g(x): ...\ndef h(x): ...\n\n\ndef f(x):\n    # 查库存\n    if g(x):\n        h(x)\n    h(1)\n"


def test_form_feed_and_line_separators_do_not_shift_lines(tmp_path):
    """换页符独占的一行、注释里的 \\u2028：节点的行、标题、副标题、sid 与没有这些字符的同一文件相同。"""
    plain = ODD_LINES.replace("\x0c", "").replace("\u2028", " ")
    graphs = []
    for name, text in (("odd", ODD_LINES), ("plain", plain)):
        (tmp_path / name / "app").mkdir(parents=True)
        (tmp_path / name / "app/m.py").write_bytes(text.encode("utf-8"))
        g = analyze_project(tmp_path / name)
        graphs.append([(fg.key, fg.sid, fg.title, [(n.type, n.sid, n.title, n.subtitle, n.line, n.end_line)
                                                     for n in fg.nodes]) for fg in g.functions.values()])
    assert graphs[0] == graphs[1]
    f = {key.split("::")[1]: nodes for key, _, _, nodes in graphs[0]}["f@8"]
    assert [(t, title, sub) for t, _, title, sub, _, _ in f if t == "cond"] == [("cond", "查库存", "g(x)")]
    src = _Source(str(tmp_path / "odd"))  # 核对报告引用的源码行
    assert src("app/m.py", 10) == "if g(x):" and src("app/m.py", 3) == "# 说明\u2028续"
