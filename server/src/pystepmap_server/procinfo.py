"""这个进程跑的是哪一版代码：启动时间和启动时代码的指纹，以及磁盘上的代码在启动之后有没有变过。

长时间开着的进程（pystepmap mcp）不会跟着代码更新：仓库里改了代码、拉了新版本，进程里跑的还是启动时读进来的那一份。
分析在子进程里跑（worker.py），子进程每次都读磁盘上的新代码——所以"最新一次分析的引擎标识"是新的，并不说明这个
进程是新的。这里给进程自己一个标识：两个包（pystepmap、pystepmap_server）下全部 .py 文件的指纹，启动时算一次
（LOADED），之后随时可以再算磁盘上的（code_fingerprint）来比。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pystepmap

PACKAGES = (Path(pystepmap.__file__).resolve().parent, Path(__file__).resolve().parent)


def code_fingerprint(packages: tuple[Path, ...] = PACKAGES) -> str:
    """磁盘上两个包的全部 .py 文件的指纹（8 位十六进制）；一个文件都读不到时空串。"""
    h = hashlib.sha256()
    found = False
    for i, package in enumerate(packages):
        try:
            files = sorted(p for p in package.rglob("*.py") if "__pycache__" not in p.parts)
        except OSError:
            continue
        for path in files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            found = True
            h.update(f"{i}/{path.relative_to(package).as_posix()}".encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:8] if found else ""


STARTED = datetime.now(UTC)  # 进程（导入这个模块）的时间
LOADED = code_fingerprint()  # 进程启动时磁盘上的代码：进程里跑的就是这一份


def stale_code() -> str | None:
    """磁盘上的代码和进程启动时不一样了：返回现在的指纹；一样（或者读不到）时 None。"""
    now = code_fingerprint()
    return now if now and LOADED and now != LOADED else None


def process_line(name: str = "MCP 服务") -> str:
    """一行：这个进程什么时候启动的、跑的是哪一版代码（project_status 用）。"""
    return f"{name}：{STARTED.astimezone().strftime('%Y-%m-%d %H:%M')} 启动，代码 {LOADED or '指纹读不到'}"


def stale_warning(name: str = "MCP 服务") -> str:
    """代码在进程启动之后更新过时的一句提醒（没有时空串）：这个进程跑的还是旧代码，输出可能和新版本不一样。"""
    now = stale_code()
    if now is None:
        return ""
    return (f"⚠ pystepmap 的代码在 {name} 启动之后更新过（启动时 {LOADED}，现在 {now}）：这个进程跑的还是旧代码，"
            f"结果的写法可能和新版本不一样。请用户重启 {name}（在 Claude Code 里重新连接 pystepmap）之后再看。")
