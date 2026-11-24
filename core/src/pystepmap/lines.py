"""功能线组装：沿任务派发关系，把每个入口及其派发的后台任务整理成主列表的行。"""

from __future__ import annotations

from fnmatch import fnmatchcase

from pystepmap.config import ProjectConfig
from pystepmap.entries import AUTO_GROUPS
from pystepmap.model import FlowLine, FunctionGraph, LineTask, numbered

ENTRY_KINDS = ("route", "task", "workflow")
GROUP_TASK = "后台任务"
GROUP_OTHER = "其他入口"


def _dispatch_refs(fg: FunctionGraph):
    for node in fg.nodes:
        for ref in node.calls:
            if ref.is_dispatch:
                yield ref


def direct_dispatches(fg: FunctionGraph, functions: dict[str, FunctionGraph]) -> list[str]:
    """fg 自己以及沿 call_targets（不穿过别的入口）能到达的函数所派发的任务 key，按首次出现去重。"""
    result: list[str] = []
    seen_fns = {fg.key}

    def visit(cur: FunctionGraph) -> None:
        for ref in _dispatch_refs(cur):
            if ref.target is not None and ref.target not in result:
                result.append(ref.target)
        for key in cur.call_targets:
            callee = functions.get(key)
            if callee is None or key in seen_fns or callee.kind in ENTRY_KINDS:
                continue
            seen_fns.add(key)
            visit(callee)
    visit(fg)
    return result


def annotate_dispatches(functions: dict[str, FunctionGraph]) -> None:
    """填写每个普通 CallRef.dispatches：其 target 任意深度能派发到的任务 key，排序去重。"""
    cache: dict[str, list[str]] = {}
    for fg in functions.values():
        for node in fg.nodes:
            for ref in node.calls:
                if ref.is_dispatch or ref.target is None or ref.target not in functions:
                    continue
                if ref.target not in cache:
                    cache[ref.target] = sorted(direct_dispatches(functions[ref.target], functions))
                ref.dispatches = list(cache[ref.target])


def _is_other_entry(fg: FunctionGraph, called: set[str], referenced: set[str], dispatched: list[str]) -> bool:
    """没有被识别为入口、但会派发任务的顶层函数：项目里没有人调用它，也没有被当作值交出去（回调、注册表）。"""
    return fg.kind == "function" and fg.parent is None and bool(dispatched) and fg.key not in called \
        and fg.key not in referenced


def _extra_group(fg: FunctionGraph, config: ProjectConfig) -> str | None:
    """fg 是配置里 extra_entries 点名的入口时，它的分组（没写分组时"其他入口"）；不是时 None。只看普通函数——已经是
    入口的、步骤、判断，以及嵌套在别的函数里的函数不受影响。"""
    if fg.kind != "function" or fg.parent is not None:  # 嵌套在别的函数里的不算（模式里的 * 也会对上它们）
        return None
    name = f"{fg.file}::{fg.qualname}"
    for e in config.extra_entries:
        if fnmatchcase(name, e["match"]):
            return e.get("group") or GROUP_OTHER
    return None


def extra_entry_keys(functions: dict[str, FunctionGraph], config: ProjectConfig) -> set[str]:
    """配置的 extra_entries 点了名的函数。"""
    return {k for k, fg in functions.items() if _extra_group(fg, config) is not None} if config.extra_entries else set()


def _group(fg: FunctionGraph, extra: str | None = None) -> str:
    if fg.group:
        return fg.group
    if extra:
        return extra
    if fg.kind == "function" and fg.rule in AUTO_GROUPS:  # 不靠装饰器规则认出来的入口，见 entries.py
        return AUTO_GROUPS[fg.rule]
    if fg.kind == "route" or fg.route:  # 接口，以及挂在路由上的工作流、任务：按文件名
        return fg.file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if fg.kind in ("task", "workflow"):
        return GROUP_TASK
    return GROUP_OTHER


def _title(fg: FunctionGraph) -> tuple[str, str]:
    """（标题，完整形式）：没有标题的接口（含挂在路由上的工作流、任务）用 "方法 路径"（不截断，没有完整形式），其余用
    函数的标题和 title_full。"""
    if fg.route and fg.title == fg.qualname.rsplit(".", 1)[-1]:
        return fg.route, ""
    if fg.kind == "function" and fg.rule in AUTO_GROUPS and fg.title == fg.qualname.rsplit(".", 1)[-1]:
        return f"{fg.qualname}（{fg.file}）", ""  # 没有文档注释的 main、run：带上文件才分得清是哪一个
    return fg.title, fg.title_full


def assemble_lines(functions: dict[str, FunctionGraph], config: ProjectConfig,
                   referenced: set[str] = frozenset(), auto: dict[str, str] | None = None) -> tuple[list[FlowLine], list[str]]:
    """返回 (功能线列表, 公共任务 key 列表)。referenced：项目里被当作值交出去的函数（见 Resolver.value_references）。
    auto：不靠装饰器规则认出来的入口（函数 key -> 认出它的方式，见 entries.auto_entries），记到函数的 rule 上。"""
    for key, how in (auto or {}).items():
        if functions[key].kind == "function":
            functions[key].rule = how
    called = {k for fg in functions.values() for k in fg.call_targets}
    direct: dict[str, list[str]] = {}
    entries: list[FunctionGraph] = []
    for fg in functions.values():
        d = direct_dispatches(fg, functions)
        if fg.kind in ENTRY_KINDS or _is_other_entry(fg, called, referenced, d) \
                or _extra_group(fg, config) is not None or (fg.kind == "function" and fg.rule in AUTO_GROUPS):
            direct[fg.key] = d
            entries.append(fg)

    dispatchers: dict[str, set[str]] = {}
    for entry in entries:
        for t in direct[entry.key]:
            dispatchers.setdefault(t, set()).add(entry.key)
    common = [k for k in functions
              if functions[k].kind in ("task", "workflow") and k in dispatchers
              and len(dispatchers[k]) >= config.common_task_threshold]
    common_set = set(common)

    def expand(key: str, expanded: set[str]) -> LineTask:
        """key 及其派发的任务（递归）；本条线里已经展开过的只留跳转（含成环的）。"""
        if key in expanded:
            return LineTask(function=key, goto=True)
        expanded.add(key)
        # 被派发的通常是入口（任务、工作流），direct 里有；派发表达式解析到的普通项目函数不一定在 direct 里，现算
        dispatched = direct[key] if key in direct else direct_dispatches(functions[key], functions)
        children = [expand(c, expanded) for c in dispatched if c in functions and c not in common_set]
        return LineTask(function=key, children=children)

    lines: list[FlowLine] = []
    covered: set[str] = set()

    def add_line(fg: FunctionGraph) -> None:
        root = expand(fg.key, set())
        title, title_full = _title(fg)
        lines.append(FlowLine(id=fg.key, title=title, group=_group(fg, _extra_group(fg, config)), kind=fg.kind, root=root, title_full=title_full))
        stack = [root]
        while stack:
            t = stack.pop()
            if not t.goto:
                covered.add(t.function)
                stack.extend(t.children)

    for fg in entries:
        contained = fg.kind in ("task", "workflow") and fg.key not in common_set \
            and bool(dispatchers.get(fg.key, set()) - {fg.key})
        if not contained:
            add_line(fg)
    # 纯任务环（互相派发、没有入口能到达）会让每个任务都被当成"已被包含"：补一条线，保证每个任务都出现
    for fg in sorted((f for f in entries if f.kind in ("task", "workflow")), key=lambda f: f.key):
        if fg.key not in covered:
            add_line(fg)
    lines.sort(key=lambda l: (l.group, l.title))
    for line, sid in zip(lines, numbered(f"line:{functions[l.root.function].sid}" for l in lines)):
        line.sid = sid
    return lines, common
