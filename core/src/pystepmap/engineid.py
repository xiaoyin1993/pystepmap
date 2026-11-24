"""引擎标识：包的版本号加上"出图用到的源码"的指纹，平台把它和每次分析一起保存（engine_version）。

版本号（__version__）不是每次改引擎都会升：从源码仓库运行时，修一处画法（比如 finally 的连线）之后重新分析，没改过的
项目代码也会冒出流程变化。指纹跟着源码变，所以两次分析的引擎标识不同，就说明中间引擎改过——流程差异据此提醒
"所在文件没改的函数，变化是引擎引起的"（diff.diff_text 的 engines）。

指纹只算出图用到的模块：从 analyze.py 起，顺着 `from pystepmap.x import …` / `from .x import …` 找到的包内模块
（build、resolve、rules……）。命令行、套件安装、记录插件、差异和查询的文字这些改了不影响图，不算。
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from pystepmap import __version__

_PACKAGE = Path(__file__).resolve().parent


def _imports(source: str) -> set[str]:
    """这段源码导入了包里的哪些模块（模块名，不带包名）。"""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                out.update([node.module.split(".")[0]] if node.module else [a.name for a in node.names])
            elif node.level == 0 and node.module and node.module.split(".")[0] == "pystepmap":
                parts = node.module.split(".")
                out.update([parts[1]] if len(parts) > 1 else [a.name for a in node.names])
        elif isinstance(node, ast.Import):
            out.update(a.name.split(".")[1] for a in node.names if a.name.startswith("pystepmap."))
    return out


def engine_modules(package: Path = _PACKAGE, start: str = "analyze") -> list[str]:
    """出图用到的包内模块名（排好序）：从 start 起顺着包内的导入找。读不了、解析不了的模块算进去但不往下找。"""
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        path = package / f"{name}.py"
        if name in seen or not path.is_file():
            continue
        seen.add(name)
        try:
            todo += _imports(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
    return sorted(seen)


def engine_fingerprint(package: Path = _PACKAGE) -> str:
    """出图用到的源码的指纹（8 位十六进制）；一个模块都读不到时空串。"""
    h = hashlib.sha256()
    found = False
    for name in engine_modules(package):
        try:
            data = (package / f"{name}.py").read_bytes()
        except OSError:
            continue
        found = True
        h.update(name.encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:8] if found else ""


def _engine_version() -> str:
    fp = engine_fingerprint()
    return f"{__version__}+{fp}" if fp else __version__


ENGINE_VERSION = _engine_version()  # 例如 0.1.0+3fa9c1d2
