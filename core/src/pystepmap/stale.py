"""图是否过期：按出图时同样的规则重新算项目里每个 .py 的指纹，和图里记的 file_hashes 比对。只读，不解析源码。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .index import iter_source_files


def current_hashes(root: Path, config: ProjectConfig) -> dict[str, str]:
    """相对路径 → 文件内容的 sha256，按路径排序；和图的 file_hashes 同口径：同样的排除目录、同样不读的路径（指到项目
    之外的链接、FIFO 等），读不出来的文件（失效的链接、没有权限）不在里面，解析不了的照样在。
    和分析一样不报错：root 不存在（或不是目录）时得到 {}，读不了的文件直接不在结果里——拿去和记录比对，会显示成
    "删掉了"。要区分"没法检查"的调用方（平台的过期检查）先自己检查 root 是不是一个目录，再来比对。"""
    out: dict[str, str] = {}
    for rel, path, _ in iter_source_files(root, config):
        if path is None:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        out[rel] = hashlib.sha256(raw).hexdigest()
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class FileChanges:
    """两份指纹的差别，各自按路径排序。"""

    changed: list[str]  # 两边都有、内容变了
    added: list[str]  # 只在现在有
    removed: list[str]  # 只在记录里有

    @property
    def any(self) -> bool:
        return bool(self.changed or self.added or self.removed)


def compare_hashes(recorded: dict[str, str], current: dict[str, str]) -> FileChanges:
    """recorded：图里记的指纹；current：现在算出来的（current_hashes）。"""
    return FileChanges(
        changed=sorted(p for p in recorded.keys() & current.keys() if recorded[p] != current[p]),
        added=sorted(current.keys() - recorded.keys()),
        removed=sorted(recorded.keys() - current.keys()),
    )
