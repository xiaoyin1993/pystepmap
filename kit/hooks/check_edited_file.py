#!/usr/bin/env python3
"""Claude Code 的 PostToolUse 钩子：编辑 Python 文件之后运行流程约束检查，把违规退回给 AI。

在 .claude/settings.json 里注册；也可以单独运行：

    echo '{"tool_input":{"file_path":"/绝对路径/x.py"}}' | python3 kit/hooks/check_edited_file.py

从标准输入读 Claude Code 传入的 JSON，用到 tool_input.file_path（相对路径按 cwd 字段补全）。只检查被编辑的那一个
文件（pystepmap check <项目根> --files <这个文件>），所以项目里别的文件有问题不会拦住这次编辑：

- 这个文件有违规、或者解析不了：说明和检查器的输出写到标准错误，退出码 2（Claude Code 把标准错误交给 AI）；
- 检查没做成（检查器跑不起来、自己出错、超时，或钩子自身出错）：标准错误写一行说明，退出码 1。不阻断，
  Claude Code 把这一行显示给用户（退出码 0 时谁都看不到标准错误，检查失效就没人知道）；
- 没有违规、不是 .py、不在项目里、文件已经不存在、输入不是预期的 JSON：什么都不输出，退出码 0。

只用标准库，并且兼容 macOS 自带的 python3（3.9）：Claude Code 用 PATH 上的 python3 运行它。

环境变量（主要给测试用）：
- PYSTEPMAP_HOOK_ROOT：项目根目录。默认是本脚本所在仓库的根（向上第一个含 .git 的目录）。
- PYSTEPMAP_HOOK_CMD：检查器命令，按 shell 规则拆分，后面接 check <项目根> --files <文件>。默认 uv run pystepmap；
  PATH 上没有 uv 时用本仓库 .venv/bin/pystepmap。
- PYSTEPMAP_HOOK_TIMEOUT：检查器的超时秒数，默认 30。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

NAME = "流程约束检查钩子"
DEFAULT_TIMEOUT = 30.0
VIOLATION_LINE = re.compile(r":\d+: SM\d{3} ", re.MULTILINE)  # pystepmap check 的违规行：文件:行: 代码 说明
PARSE_FAILED = "解析不了"  # pystepmap check 列出解析不了的文件时的标题里有这几个字


class NotChecked(Exception):
    """检查没做成：说明写一行到标准错误，退出码 1（不阻断，用户能看到）。"""


def repo_root(start: Path) -> Path | None:
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return p
    return None


def edited_file(payload: object) -> Path | None:
    """Claude Code 传入的 JSON 里被编辑的文件（绝对路径，未解析符号链接）；没有 tool_input.file_path 时 None。"""
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        cwd = payload.get("cwd")
        path = (Path(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else Path.cwd()) / path
    return path


def checker_command(home: Path | None) -> list[str]:
    custom = os.environ.get("PYSTEPMAP_HOOK_CMD")
    if custom:
        return shlex.split(custom)
    if shutil.which("uv"):
        return ["uv", "run", "pystepmap"]
    venv = home / ".venv" / "bin" / "pystepmap" if home else None
    if venv is not None and os.access(str(venv), os.X_OK):
        return [str(venv)]
    raise NotChecked("找不到 uv（也没有仓库里的 .venv/bin/pystepmap），没有检查")


def timeout_seconds() -> float:
    try:
        value = float(os.environ.get("PYSTEPMAP_HOOK_TIMEOUT", ""))
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def run_checker(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
    """运行检查器，返回 (退出码, 标准输出, 标准错误)。超时时连同它派生的子进程（uv 会再起一个 python）一起结束。"""
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        raise NotChecked(f"没法运行检查器 {shlex.join(cmd)}：{e.strerror or e}，没有检查") from None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        proc.communicate()
        raise NotChecked(f"检查器 {timeout:g} 秒没有结束（超时），没有检查") from None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:  # 含 UnicodeDecodeError：不是 Claude Code 的输入，与检查无关
        return 0
    path = edited_file(payload)
    if path is None or path.suffix != ".py":
        return 0
    try:
        path = path.resolve()
        if not path.is_file():
            return 0
    except (OSError, RuntimeError, ValueError):  # 读不了、符号链接成环、路径里有 NUL
        return 0
    home = repo_root(Path(__file__).resolve().parent)
    root_env = os.environ.get("PYSTEPMAP_HOOK_ROOT")
    root = Path(root_env).resolve() if root_env else home
    if root is None:
        raise NotChecked("找不到项目根目录（脚本不在 git 仓库里），没有检查")
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return 0  # 不在这个项目里
    if rel.startswith("-"):
        rel = "./" + rel  # 别让 argparse 当成选项
    cmd = checker_command(home) + ["check", str(root), "--files", rel]
    code, out, err = run_checker(cmd, home or root, timeout_seconds())
    if code == 0:
        return 0
    if code == 1 and VIOLATION_LINE.search(out):
        print("这些是流程约束检查的违规，请按 flow-style 修改后再继续"
              "（.claude/skills/flow-style/SKILL.md）：", file=sys.stderr)
        print(out.rstrip(), file=sys.stderr)
        return 2
    if code == 2 and PARSE_FAILED in err:
        lines = err.splitlines()
        start = next(i for i, line in enumerate(lines) if PARSE_FAILED in line)  # 之前的行可能是 uv 的提示
        print(f"被编辑的文件解析不了，流程约束检查没法检查它，请先修好再继续：{rel}", file=sys.stderr)
        print("\n".join(lines[start:]), file=sys.stderr)
        return 2
    first = next((line.strip() for line in err.splitlines() if line.strip()), "没有输出")
    raise NotChecked(f"检查器出错（退出码 {code}：{first}），没有检查")


if __name__ == "__main__":
    try:
        status = main()
    except NotChecked as e:
        print(f"{NAME}：{e}", file=sys.stderr)
        status = 1
    except Exception as e:  # noqa: BLE001 —— 钩子自身的意外错误不能挡住干活，但要让用户看到
        print(f"{NAME}：钩子自身出错（{type(e).__name__}: {e}），没有检查", file=sys.stderr)
        status = 1
    sys.exit(status)
