"""读源码片段：给详情栏看节点的源码。规则沿用阶段 3 原型的开发服务器接口 /__source（原型并入平台时已删除）：

- 只读已添加项目根目录之内的 .py 文件：拒绝空路径、含 NUL、绝对路径（含 Windows 盘符、反斜杠开头）、任何一段是
  ".."、不是 .py、解析后在根目录外面、经符号链接指到根目录外面（或指到非 .py）；不存在、不是普通文件的 404；
- 根目录必须还是添加时的那个真实目录：存下的根是解析后的真实路径，现在解析出来不一样（目录被移走后原地换成了
  符号链接）或者不再是目录时拒绝（400），不顺着换上的链接读到别处去；根目录不存在了 404；
- 行号从 1 起、含两端，一次最多 MAX_LINES 行，超出文件末尾的部分不返回；
- stale：文件内容和生成图时不一样了（按那次分析的图里 file_hashes 的 sha256 比；接口默认用最新一次分析，可以用
  analysis=<id> 指定）；图里没有这个文件的指纹时为 None。

分行和解码与引擎一致（只在 \\r\\n、\\r、\\n 处分行，按编码声明解码、去掉 BOM），行号才和图对得上。
只读，不执行被分析项目的任何代码。这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from pystepmap.index import decode_source, source_lines

MAX_LINES = 400
_LINE_NUMBER = re.compile(r"\d{1,9}")
_QUERY_HELP = "需要 file、start、end（行号从 1 起，end ≥ start）"


class SourceError(Exception):
    """请求不能满足：status 是 HTTP 状态码（400 / 404），message 是给用户看的中文。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


REPLACED_ROOT = "项目目录已经不是添加时的那个目录（被移走或换成了符号链接），请删除后重新添加"


def root_problem(root: str) -> tuple[int, str] | None:
    """root 是添加项目时存下的根目录（解析后的真实绝对路径）。它现在还是同一个真实目录时返回 None，否则返回
    (状态码, 中文说明)：不存在了 404；解析出来不是它自己（换成了符号链接）或者不再是目录 400。重新分析、过期检查
    （status.staleness）也用它。"""
    if not os.path.isabs(root):
        return 404, "项目根不是绝对路径"
    try:
        real = os.path.realpath(root, strict=True)
    except OSError:
        return 404, f"项目目录不存在了：{root}"
    if real != root or not os.path.isdir(real):
        return 400, f"{REPLACED_ROOT}：{root}"
    return None


def _within(root: str, path: str) -> bool:
    """path 在 root 里面（不等于 root 本身）；两个都是解析过的绝对路径。"""
    return path != root and os.path.commonpath([root, path]) == root


def resolve_source_path(root: str, file: str) -> Path:
    """把图里的相对路径解析成 root 下的一个真实的 .py 文件，返回它的真实路径；不行时抛 SourceError。"""
    if not file or "\0" in file:
        raise SourceError(400, "缺少文件路径")
    if os.path.isabs(file) or re.match(r"[a-zA-Z]:", file) or file.startswith("\\"):
        raise SourceError(400, "只接受项目内的相对路径")
    if ".." in re.split(r"[\\/]+", file):
        raise SourceError(400, "路径里不能有 ..")
    if not file.endswith(".py"):
        raise SourceError(400, "只读 .py 文件")
    problem = root_problem(root)
    if problem is not None:
        raise SourceError(*problem)
    real_root = root
    target = os.path.normpath(os.path.join(real_root, file))
    if not _within(real_root, target):
        raise SourceError(400, "路径在项目之外")
    try:
        real = os.path.realpath(target, strict=True)
    except OSError:  # 不存在、链接成环、没有权限
        raise SourceError(404, "文件不存在") from None
    if not _within(real_root, real):
        raise SourceError(400, "路径经符号链接指到了项目之外")
    if not real.endswith(".py"):
        raise SourceError(400, "只读 .py 文件")
    if not os.path.isfile(real):
        raise SourceError(404, "不是文件")
    return Path(real)


def _decode(raw: bytes) -> str:
    try:
        return decode_source(raw)
    except (SyntaxError, UnicodeDecodeError, LookupError):  # 编码声明不认识、按声明的编码解不开
        return raw.decode("utf-8", errors="replace")


def read_snippet(path: Path, file: str, start: int, end: int, expected_hash: str | None) -> dict:
    """读第 start 到 end 行（start ≥ 1、end ≥ start）。返回 {file, start, end, total, lines, stale}：end 是实际返回到
    的行，start 超过文件末尾时 lines 为空、end = start - 1；total 是文件一共几行。"""
    raw = Path(path).read_bytes()
    rows = source_lines(_decode(raw))
    total = len(rows)
    last = min(end, start + MAX_LINES - 1, total)
    inside = start <= total
    stale = None if expected_hash is None else hashlib.sha256(raw).hexdigest() != expected_hash
    return {"file": file, "start": start, "end": last if inside else start - 1, "total": total,
            "lines": rows[start - 1:last] if inside else [], "stale": stale}


def _line_number(value: str | None) -> int | None:
    if value is None or not _LINE_NUMBER.fullmatch(value) or int(value) < 1:
        return None
    return int(value)


def get_snippet(root: str, hashes: dict[str, str], file: str | None, start: str | None, end: str | None) -> dict:
    """处理一次读源码请求（参数是查询串里的原文）：root 是项目根目录，hashes 是拿来比的那次分析的图里的 file_hashes。"""
    first, last = _line_number(start), _line_number(end)
    if not file or first is None or last is None or last < first:
        raise SourceError(400, _QUERY_HELP)
    path = resolve_source_path(root, file)
    try:
        return read_snippet(path, file, first, last, hashes.get(file))
    except OSError as e:
        raise SourceError(404, f"读不了这个文件：{e.strerror or e}") from None
