"""过期检查：项目目录里的 .py 现在和某次分析时比，哪些改了、新增了、删掉了，哪些功能线因此可能过期。平台接口
（/api/projects/{id}/status）和 MCP 都用它。只读：按出图时同样的规则（同样的排除目录）重新算每个 .py 的 sha256，
不解析源码，不执行任何代码。

先看根目录：已经不是添加时的那个真实目录（被删、被移走或换成符号链接，sources.root_problem）或者读不了时，不去比，
返回 {"checked": False, "reason": 中文}——core 的 current_hashes 对这些情形不报错，只会得到"全删掉了"。

读不了的文件单列（unreadable），不算删除也不算改动：core 的 current_hashes 默默跳过读不了的文件（拿去比就显示成
"删掉了"），所以这里按它同样的口径（同一个 iter_source_files、同样的读法）自己算指纹，另外记下：
- 读的时候出错的（没有权限等）；
- 分析时有指纹、现在引擎不读的路径（换成了指到项目外的符号链接、FIFO 等；新出现的这种路径不报，分析也不读它们）；
- 分析时有指纹、现在列不出来的：所在的目录进不去或列不出内容（真不存在了的照样算删除）。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pystepmap.config import ConfigError, ProjectConfig, parse_config
from pystepmap.index import NOT_SOURCE_REASONS, iter_source_files
from pystepmap.stale import compare_hashes

from .sources import root_problem

MAX_FILES = 200  # 每类文件最多列出几个（另带 total 给出总数）


def _lines_of(file_lines: dict, files: list[str]) -> list[str]:
    """涉及这些文件的功能线 sid，去重；按文件路径的顺序，同一文件里按图里功能线的顺序。"""
    out: dict[str, None] = {}
    for f in sorted(files):
        for sid in file_lines.get(f, []):
            out[sid] = None
    return list(out)


def _scan(root: Path, config: ProjectConfig) -> tuple[dict[str, str], dict[str, str]]:
    """(读得出来的文件 → sha256, 读不了的文件 → 原因)，各按路径排序。指纹和 core 的 current_hashes 同口径；读不了的
    包括引擎不读的路径（iter_source_files 给出原因的那些）和读的时候出错的。"""
    hashes: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    for rel, path, reason in iter_source_files(root, config):
        if path is None:
            unreadable[rel] = reason
            continue
        try:
            raw = path.read_bytes()
        except OSError as e:
            unreadable[rel] = e.strerror or str(e)
            continue
        hashes[rel] = hashlib.sha256(raw).hexdigest()
    return dict(sorted(hashes.items())), dict(sorted(unreadable.items()))


def _hidden(root: str, rel: str) -> bool:
    """分析时有、现在列不出来的文件，是不是因为读不了才看不见：路径本身拿不到状态（某一级目录进不去），或者文件在、
    但某一级目录列不出内容。真不存在了（或者在引擎不进入的地方，比如换成了符号链接的目录）返回 False。"""
    path = os.path.join(root, rel)
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:  # 没有权限进某一级目录等
        return True
    parent = os.path.dirname(rel)
    while parent:
        if not os.access(os.path.join(root, parent), os.R_OK | os.X_OK):
            return True
        parent = os.path.dirname(parent)
    return False


def staleness(root: str, config: dict, recorded: dict[str, str], file_lines: dict | None) -> dict:
    """root：项目根目录（添加时存下的真实绝对路径）；config：那次分析用的配置（引擎的配置 JSON）；recorded：那次分析的
    图里的 file_hashes；file_lines：那次分析的 file_lines（旧版本存的分析没有，为 None）。返回

        {"checked": True, "stale": bool, "changed": [...], "added": [...], "removed": [...], "unreadable": [...],
         "lines": [功能线 sid…] | None, "total": {"changed": n, "added": n, "removed": n, "unreadable": n}}
        有读不了的文件时另带 "note": "有 N 个文件读不了，没有比对"

    四类文件各按路径排序、最多列 MAX_FILES 个；stale 只看改了、新增、删掉的（读不了的不算）；lines 是涉及改了或删掉
    的文件的功能线（新增的文件不在任何线里），file_lines 为 None 时也为 None。没法检查时返回
    {"checked": False, "reason": 中文}。"""
    problem = root_problem(root)
    if problem is not None:
        return {"checked": False, "reason": problem[1]}
    if not os.access(root, os.R_OK | os.X_OK):
        return {"checked": False, "reason": f"没有权限读取项目目录：{root}"}
    try:
        parsed = parse_config(config)
    except ConfigError as e:
        return {"checked": False, "reason": f"分析配置不合法：{e}"}
    try:
        current, skipped = _scan(Path(root), parsed)
    except OSError as e:  # os.walk 自己不报错；兜底
        return {"checked": False, "reason": f"读不了项目目录：{e.strerror or e}：{root}"}
    unreadable = {f for f, why in skipped.items() if f in recorded or why not in NOT_SOURCE_REASONS}
    unreadable |= {f for f in recorded if f not in current and f not in skipped and _hidden(root, f)}
    changes = compare_hashes({f: h for f, h in recorded.items() if f not in unreadable}, current)
    out = {
        "checked": True,
        "stale": changes.any,
        "changed": changes.changed[:MAX_FILES],
        "added": changes.added[:MAX_FILES],
        "removed": changes.removed[:MAX_FILES],
        "unreadable": sorted(unreadable)[:MAX_FILES],
        "lines": None if file_lines is None else _lines_of(file_lines, changes.changed + changes.removed),
        "total": {"changed": len(changes.changed), "added": len(changes.added), "removed": len(changes.removed),
                  "unreadable": len(unreadable)},
    }
    if unreadable:
        out["note"] = f"有 {len(unreadable)} 个文件读不了，没有比对"
    return out
