"""源码根：分析的目录不是导入根时（src 布局、仓库根下的 backend/app），项目内的绝对导入照样解析到项目里的函数。"""

import ast
import json

import pytest
from conftest import find_fn, make_project
from pystepmap.analyze import analyze_project
from pystepmap.cli import main
from pystepmap.config import ConfigError, ProjectConfig, load_config
from pystepmap.index import build_index
from pystepmap.resolve import Resolver

SHOP = {
    "src/shop/__init__.py": "",
    "src/shop/repo.py": "def load(x): ...\n",
    "src/shop/orders.py": (
        "from shop.repo import load\n"
        "from nspkg.sub.helpers import ns_helper\n"
        "def place(x):\n"
        "    load(x)\n"
        "    return ns_helper(x)\n"),
    "src/nspkg/sub/helpers.py": "def ns_helper(x): ...\n",  # 命名空间包：没有 __init__.py
}

REPO = {
    "backend/app/__init__.py": "",
    "backend/app/services/__init__.py": "",
    "backend/app/services/billing.py": "def charge(x): ...\n",
    "backend/app/api.py": (
        "from app.services.billing import charge\n"
        "from backend.app.services import billing\n"
        "from .services.billing import charge as rel_charge\n"
        "from nspkg.helpers import h\n"
        "def pay(x):\n"
        "    charge(x)\n"
        "    billing.charge(x)\n"
        "    rel_charge(x)\n"
        "    h(x)\n"),
    "backend/nspkg/helpers.py": "def h(x): ...\n",  # backend 下的命名空间包
    "backend/alembic/env.py": "from app.services.billing import charge\ndef run():\n    charge(1)\n",
    "scripts/tool.py": "def main(): ...\n",
}


def _resolved(root, qualname, config=None):
    """函数体里每个调用的 (显示名, 解析方式, 目标 key)，按源码顺序。"""
    config = config or ProjectConfig()
    index = build_index(root, config)
    fn = find_fn(index, qualname)
    resolver = Resolver(index)
    calls = sorted((n for st in fn.node.body for n in ast.walk(st) if isinstance(n, ast.Call)),
                   key=lambda c: (c.lineno, c.col_offset))
    return [(r.display, r.how, r.target) for r in (resolver.resolve(c, fn) for c in calls)]


def test_src_layout(tmp_path):
    root = make_project(tmp_path, SHOP)
    fg = find_fn(analyze_project(root), "place")
    assert fg.call_targets == ["src/shop/repo.py::load@1", "src/nspkg/sub/helpers.py::ns_helper@1"]
    index = build_index(root, ProjectConfig())
    assert index.source_roots == ["", "src"]
    assert index.modules["src/shop/orders.py"].dotted == "shop.orders"
    assert index.by_dotted["shop.orders"] == index.by_dotted["src.shop.orders"] == "src/shop/orders.py"
    # 直接分析 src：同样的调用，key 相对分析的目录
    fg2 = find_fn(analyze_project(root / "src"), "place")
    assert fg2.call_targets == ["shop/repo.py::load@1", "nspkg/sub/helpers.py::ns_helper@1"]


def test_backend_app_analysed_from_repo_root(tmp_path):
    root = make_project(tmp_path, REPO)
    index = build_index(root, ProjectConfig())
    assert index.source_roots == ["", "backend"]
    charge, h = "backend/app/services/billing.py::charge@1", "backend/nspkg/helpers.py::h@1"
    assert _resolved(root, "pay") == [("charge", "exact", charge), ("billing.charge", "exact", charge),
                                      ("rel_charge", "exact", charge), ("h", "exact", h)]
    assert _resolved(root, "run") == [("charge", "exact", charge)]
    whole, part = analyze_project(root), analyze_project(root / "backend")
    inside = {k.removeprefix("backend/"): fg for k, fg in whole.functions.items() if k.startswith("backend/")}
    assert sorted(inside) == sorted(part.functions)
    for k, fg in part.functions.items():  # 同一个函数解析到同一批调用（key 只差 backend/ 前缀）
        assert [t.removeprefix("backend/") for t in inside[k].call_targets] == fg.call_targets


def test_namespace_package_without_init(tmp_path):
    root = make_project(tmp_path, {
        "nspkg/sub/helpers.py": "def h(x): ...\n",
        "app/__init__.py": "",
        "app/main.py": "from nspkg.sub.helpers import h\nimport nspkg.sub.helpers as hh\ndef f():\n    h(1)\n    hh.h(2)\n",
    })
    key = "nspkg/sub/helpers.py::h@1"
    assert _resolved(root, "f") == [("h", "exact", key), ("hh.h", "exact", key)]


def test_script_directory_with_sibling_imports(tmp_path):
    """不是包的目录里的模块被别处按顶层名导入：这个目录是源码根。"""
    root = make_project(tmp_path, {
        "tools/common.py": "def shared(): ...\n",
        "tools/run.py": "import common\nfrom common import shared\ndef main():\n    common.shared()\n    shared()\n",
        "tools/unused.py": "def other(): ...\n",
    })
    assert build_index(root, ProjectConfig()).source_roots == ["", "tools"]
    key = "tools/common.py::shared@1"
    assert _resolved(root, "main") == [("common.shared", "exact", key), ("shared", "exact", key)]


def test_source_roots_config_replaces_detection(tmp_path):
    root = make_project(tmp_path, {
        "code/pkg/mod.py": "def f(): ...\n",  # pkg 没有 __init__.py，code 也不是包：检测不出 code 是源码根
        "code/main.py": "from pkg.mod import f\nfrom code.pkg.mod import f as g\ndef run():\n    f()\n    g()\n",
    })
    key = "code/pkg/mod.py::f@1"
    assert _resolved(root, "run") == [("f", "external", None), ("g", "exact", key)]
    config = ProjectConfig(source_roots=["code"])
    assert build_index(root, config).source_roots == ["code"]
    assert _resolved(root, "run", config) == [("f", "exact", key), ("g", "external", None)]


def test_same_dotted_name_under_two_roots_is_not_linked(tmp_path):
    """两个源码根下有同名模块：这个点分名不确定指向哪个文件，不连线（记为无法判断），用带前缀的名字照常解析。"""
    root = make_project(tmp_path, {
        "svc_a/app/__init__.py": "", "svc_a/app/util.py": "def u(): ...\n",
        "svc_b/app/__init__.py": "", "svc_b/app/util.py": "def u(): ...\n",
        "svc_a/app/main.py": "from app.util import u\nfrom svc_a.app.util import u as ua\ndef run():\n    u()\n    ua()\n",
    })
    index = build_index(root, ProjectConfig())
    assert index.source_roots == ["", "svc_a", "svc_b"]
    assert "app.util" not in index.by_dotted
    assert _resolved(root, "run") == [("u", "unresolved", None), ("ua", "exact", "svc_a/app/util.py::u@1")]


def test_source_roots_config_errors(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"source_roots": "src"}))
    with pytest.raises(ConfigError):
        load_config(p)
    root = make_project(tmp_path / "p", {"a.py": "def f(): ...\n"})
    for bad in (["missing"], ["../p"], ["/abs"]):
        with pytest.raises(ConfigError):
            build_index(root, ProjectConfig(source_roots=bad))


def test_summary_lists_source_roots(tmp_path, capsys):
    root = make_project(tmp_path, SHOP)
    assert main(["analyze", str(root), "-o", str(tmp_path / "g.json")]) == 0
    assert "源码根: ., src" in capsys.readouterr().out
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"source_roots": ["missing"]}))
    assert main(["analyze", str(root), "--config", str(cfg), "-o", str(tmp_path / "g2.json")]) == 2
    assert "missing" in capsys.readouterr().err
