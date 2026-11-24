"""覆盖核对（验收用，独立于引擎）：用另一套遍历方式列出指定函数（含内部函数）里的
return、raise、await 调用、派发、if、循环、except，逐项检查其所属语句的起始行是否是图上某个节点的行。

用法：python core/tests/acceptance/coverage_check.py <图.json> <函数限定名>...

只读图 JSON 和项目源码，不导入 pystepmap，也不导入被分析的项目。
按类别输出"在图上 / 不在图上"的数量，并打印每个不在图上的项的源码行。
return、raise、await 调用、派发四类有任何一项不在图上时退出码 1；参数或图有问题时退出码 2。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

CATEGORIES = ["return", "raise", "await 调用", "派发", "if", "循环", "except"]
STRICT = {"return", "raise", "await 调用", "派发"}  # 这几类不允许缺
# 派发方法名（taskiq 的 kiq；DBOS 的启动与入队），独立列出，不读引擎的规则表
DISPATCH_METHODS = {"kiq", "start_workflow", "start_workflow_async", "enqueue", "enqueue_async", "enqueue_workflow",
                    "enqueue_workflow_async"}
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_dispatch(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr in DISPATCH_METHODS


def collect(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[tuple[str, int]], list[ast.AST]]:
    """返回 (本函数自己的 [(类别, 所属语句起始行)], 直接内嵌的函数定义)。内部函数的项归内部函数自己。"""
    items: list[tuple[str, int]] = []
    inner: list[ast.AST] = []

    def visit(node: ast.AST, stmt_line: int) -> None:
        if isinstance(node, _DEFS):
            inner.append(node)
            # 装饰器和默认参数在外层函数里求值，算外层函数所在语句（def 行）
            for sub in [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
                visit(sub, node.lineno)
            return
        if isinstance(node, (ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.stmt):
            stmt_line = node.lineno
            if isinstance(node, ast.Return):
                items.append(("return", stmt_line))
            elif isinstance(node, ast.Raise):
                items.append(("raise", stmt_line))
            elif isinstance(node, ast.If):
                items.append(("if", stmt_line))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                items.append(("循环", stmt_line))
        elif isinstance(node, ast.ExceptHandler):
            stmt_line = node.lineno
            items.append(("except", stmt_line))
        elif isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            items.append(("派发" if _is_dispatch(node.value) else "await 调用", stmt_line))
            for child in ast.iter_child_nodes(node.value):  # 被 await 的调用本身已计入，只看它的参数和接收者
                visit(child, stmt_line)
            return
        elif isinstance(node, ast.Call) and _is_dispatch(node):
            items.append(("派发", stmt_line))
        for child in ast.iter_child_nodes(node):
            visit(child, stmt_line)

    for stmt in fn.body:
        visit(stmt, stmt.lineno)
    return items, inner


def _find_def(tree: ast.AST, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, _DEFS) and node.lineno == line:
            return node
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(next(s for s in __doc__.splitlines() if s.startswith("用法")), file=sys.stderr)
        return 2
    graph = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    root = Path(graph["root"])
    functions = graph["functions"]
    totals: dict[str, Counter] = {c: Counter() for c in CATEGORIES}
    missing_all: list[tuple[str, str, int, str]] = []  # (函数 key, 类别, 行号, 源码)
    sources: dict[str, tuple[list[str], ast.AST]] = {}

    def load(file: str) -> tuple[list[str], ast.AST]:
        if file not in sources:
            raw = (root / file).read_bytes()
            if hashlib.sha256(raw).hexdigest() != graph["file_hashes"].get(file):
                print(f"图已过期：{file} 在分析后改过，请重新分析", file=sys.stderr)
                raise SystemExit(2)
            text = raw.decode("utf-8-sig")
            rows = re.split(r"\r\n|\r|\n", text)  # 同 pystepmap.index.source_lines：只按解析器的换行规则分行
            sources[file] = (rows[:-1] if rows[-1] == "" else rows, ast.parse(text))
        return sources[file]

    def check(key: str, qualname: str, fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        f = functions.get(key)
        file = key.split("::", 1)[0]
        rows = load(file)[0]
        items, inner = collect(fn_node)
        node_lines = {n["line"] for n in f["nodes"]} if f else set()
        counts: dict[str, Counter] = {c: Counter() for c in CATEGORIES}
        missing = []
        for cat, line in sorted(set(items), key=lambda x: (x[1], CATEGORIES.index(x[0]))):
            ok = line in node_lines
            counts[cat]["在图上" if ok else "不在图上"] += 1
            totals[cat]["在图上" if ok else "不在图上"] += 1
            if not ok:
                missing.append((cat, line, rows[line - 1].strip()))
        print(f"\n== {key}" + ("" if f else "（图里没有这个函数）"))
        print("   " + "  ".join(f"{c} {counts[c]['在图上']}/{counts[c]['不在图上']}"
                                for c in CATEGORIES if counts[c]) + "   （在图上/不在图上）")
        for cat, line, text in missing:
            print(f"   不在图上  {cat:8s} 第 {line} 行: {text[:100]}")
            missing_all.append((key, cat, line, text))
        for sub in inner:
            sub_q = f"{qualname}.<locals>.{sub.name}"
            check(f"{file}::{sub_q}@{sub.lineno}", sub_q, sub)

    for name in argv[1:]:
        keys = sorted(k for k, f in functions.items() if f["qualname"] == name)
        if not keys:
            print(f"图里没有限定名为 {name} 的函数", file=sys.stderr)
            return 2
        for key in keys:
            file, rest = key.split("::", 1)
            fn_node = _find_def(load(file)[1], int(rest.rsplit("@", 1)[1]))
            if fn_node is None:
                print(f"源码里找不到 {key} 的定义", file=sys.stderr)
                return 2
            check(key, name, fn_node)

    print("\n== 合计（在图上 / 不在图上）")
    for cat in CATEGORIES:
        print(f"   {cat:8s} {totals[cat]['在图上']:4d} / {totals[cat]['不在图上']:d}")
    bad = sum(totals[c]["不在图上"] for c in STRICT)
    print(f"不在图上共 {len(missing_all)} 项；return、raise、await 调用、派发四类不在图上 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
