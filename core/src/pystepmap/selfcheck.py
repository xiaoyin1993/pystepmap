"""图自检：走不到的节点、没有出路的节点，以及目标不明（unresolved）/ 猜测的调用数量和分类。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pystepmap.config import NOISE_RECEIVERS
from pystepmap.model import FunctionGraph, SelfCheck
from pystepmap.resolve import BUILTIN_METHOD_NAMES, COMMON_NAMES


def _reachable(fg: FunctionGraph) -> set[str]:
    out: dict[str, list[str]] = {}
    for e in fg.edges:
        out.setdefault(e.src, []).append(e.dst)
    seen = {n.id for n in fg.nodes if n.type == "start"}
    stack = list(seen)
    while stack:
        for dst in out.get(stack.pop(), []):
            if dst not in seen:
                seen.add(dst)
                stack.append(dst)
    return seen


def _closing(fg: FunctionGraph) -> set[str]:
    """结局、没被捕获的异常进入的 finally 体里的节点（从结局出发的 finally 连线、raise 连线的终点往后能走到的）。
    try / except / else 没有正常出口时，出了 finally 体函数就结束，finally 体最后一个节点没有出边：不算没有出路。"""
    ends = {n.id for n in fg.nodes if n.type == "end"}
    out: dict[str, list[str]] = {}
    for e in fg.edges:
        out.setdefault(e.src, []).append(e.dst)
    seen = {e.dst for e in fg.edges if e.kind == "raise" or (e.kind == "finally" and e.src in ends)}
    stack = list(seen)
    while stack:
        for dst in out.get(stack.pop(), []):
            if dst not in seen:
                seen.add(dst)
                stack.append(dst)
    return seen


def run_selfcheck(functions: dict[str, FunctionGraph], skipped_files: list[tuple[str, str]]) -> SelfCheck:
    unreachable: list[tuple[str, str]] = []
    dead_ends: list[tuple[str, str]] = []
    unresolved = guessed = 0
    for key, fg in functions.items():
        unresolved += len(fg.unresolved)  # 函数体里所有无法判断目标的调用（每个函数按 (行, 名字) 去重），不只是留在节点上的
        reach = _reachable(fg)
        has_out = {e.src for e in fg.edges}
        closing = _closing(fg)
        for n in fg.nodes:
            implicit_exc = n.type == "end" and n.line == 0 and n.end_kind == "exception"
            if n.id not in reach and not implicit_exc:
                unreachable.append((key, n.id))
            if n.type != "end" and n.id not in has_out and n.id not in closing:
                dead_ends.append((key, n.id))
            for ref in n.calls:
                guessed += ref.resolution == "guessed"
    return SelfCheck(unreachable=sorted(unreachable), dead_ends=sorted(dead_ends),
                     unresolved_calls=unresolved, guessed_calls=guessed, skipped_files=list(skipped_files))


# ---------- 目标不明的调用：分类 ----------


@dataclass
class UnresolvedSite:
    """一处无法判断目标的调用。"""

    key: str  # 所在函数的 key
    file: str
    line: int
    name: str  # 显示名，如 "svc.render"、"load_raw"


@dataclass
class UnresolvedGroups:
    """目标不明的调用按"有没有可能落在项目代码上"分的四类。"""

    blind: list[UnresolvedSite] = field(default_factory=list)  # 名字在项目里有定义：可能就是项目内的调用，是真盲点
    common: list[UnresolvedSite] = field(default_factory=list)  # 名字太常见（get、append……）：项目里虽有同名定义，多半是字典、列表的方法
    outside: list[UnresolvedSite] = field(default_factory=list)  # 项目里没有这个名字：第三方、标准库、内置，解析不了也不必解析
    logs: list[UnresolvedSite] = field(default_factory=list)  # 日志


def classify_unresolved(functions: dict[str, FunctionGraph],
                        noise_receivers: Sequence[str] = NOISE_RECEIVERS) -> UnresolvedGroups:
    """把每处目标不明的调用分成 blind / common / outside / logs（四类之和等于 SelfCheck.unresolved_calls）。

    判断只看显示名：接收者是日志的算 logs；被调的名字（最后一段）项目里没有同名的函数或方法定义就算 outside
    （第三方、标准库，不可能落在项目代码上）；名字在引擎有意不推测的两份名单里（COMMON_NAMES、
    BUILTIN_METHOD_NAMES：get、append 这些）就算 common，项目里碰巧有同名定义也多半是字典、列表的方法；
    剩下的才是 blind——真正可能落在项目代码上、值得逐条看的。
    """
    defined = {fg.qualname.rsplit(".", 1)[-1] for fg in functions.values()}
    noise = set(noise_receivers)
    groups = UnresolvedGroups()
    for key, fg in functions.items():
        for u in fg.unresolved:
            site = UnresolvedSite(key, fg.file, u.line, u.name)
            receiver, _, called = u.name.rpartition(".")
            if receiver in noise:
                groups.logs.append(site)
            elif called not in defined:
                groups.outside.append(site)
            elif called in COMMON_NAMES or called in BUILTIN_METHOD_NAMES:
                groups.common.append(site)
            else:
                groups.blind.append(site)
    return groups
