"""没有装饰器规则可认的入口：不靠标记、不靠配置，尽量把"会被外面调起来的函数"找出来，各成一条功能线。

识别规则（rules.py）只认路由、任务、工作流的装饰器。新手让 AI 写的项目常常是一个脚本、一个命令行工具、一个机器人：
没有这些装饰器，功能线就是空的。这里补四种，类型都还是普通函数（kind 不变），只是给它一个分组和 rule：

- `main`：`if __name__ == "__main__":` 块里调用的项目内函数（含 `asyncio.run(main())` 这种包了一层的）——分组"脚本入口"。它只是把一个已经认出来的
  入口跑起来时（演示、启动代码：沿调用能走到路由、任务或工作流）不算，里面没有步骤、判断、循环的小工具也不算；
- `command`：命令行框架的子命令，装饰器是 `@x.command(...)` / `@click.command()` / `@x.callback()`——分组"命令行"；
- `handler`：顶层函数挂着项目外对象的装饰器（`@scheduler.scheduled_job(...)`、`@bot.message_handler(...)`、
  `@app.on_event("startup")`），项目里又没有人调用它：是框架来调的——分组"其他入口"；
- `comment`：函数定义（有装饰器时是第一个装饰器）上面紧挨着的一行注释 `# pystepmap: 入口` 或
  `# pystepmap: 入口 每日对账`（冒号全角半角都行，"入口"也可以写 entry）。后面的文字是功能线的标题。这是不装任何包、
  不改配置就能点名入口的办法：别的规则都没认出来时，写这一行就一定成线——分组"其他入口"；
- `fallback`：上面这些加上路由、任务、工作流一个都没有时（`fallback_entries` 没关），把没有人调用、也没有被当作值
  交出去的顶层函数当入口；还是没有，就放宽到全部模块级函数、再到类的公开方法。只要项目里有函数，就有功能线。

`extra_entries`（配置里点名的入口）照旧，优先于这里。
"""

from __future__ import annotations

import ast
import re

from .external import ExternalObjects
from .index import FunctionInfo, ProjectIndex
from .model import FunctionGraph, ProjectGraph
from .resolve import Resolver

GROUP_SCRIPT = "脚本入口"
GROUP_COMMAND = "命令行"
GROUP_OTHER = "其他入口"
ENTRY_KINDS = ("route", "task", "workflow")
AUTO_GROUPS = {"main": GROUP_SCRIPT, "command": GROUP_COMMAND, "handler": GROUP_OTHER, "comment": GROUP_OTHER,
               "fallback": GROUP_OTHER}
ENTRY_COMMENT = "# pystepmap: 入口"  # 给人和 AI 看的写法（提示里引用）

_COMMAND = re.compile(r"(^|\.)(command|callback)$")
_MARK = re.compile(r"^#\s*pystepmap\s*[:：]\s*(?:入口|entry)\b\s*[:：]?\s*(.*)$", re.IGNORECASE)


def comment_mark(fn: FunctionInfo, index: ProjectIndex) -> str | None:
    """函数上面紧挨着的入口注释里的标题（没写标题时 ""）；没有这行注释时 None。"""
    first = min([fn.node.lineno, *(d.lineno for d in fn.node.decorator_list)])
    lines = index.modules[fn.file].lines
    if first < 2 or first - 2 >= len(lines):
        return None
    m = _MARK.match(lines[first - 2].strip())
    return m.group(1).strip() if m else None


def _is_main_guard(st: ast.stmt) -> bool:
    """if __name__ == "__main__":（两边谁在前都行）。"""
    if not isinstance(st, ast.If) or not isinstance(st.test, ast.Compare) or len(st.test.ops) != 1 \
            or not isinstance(st.test.ops[0], ast.Eq):
        return False
    sides = [st.test.left, st.test.comparators[0]]
    return any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides) \
        and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)


def _main_calls(index: ProjectIndex, resolver: Resolver) -> list[str]:
    """各模块 __main__ 块里调用的项目内函数的 key。"""
    found: dict[str, None] = {}
    for mod in index.modules.values():
        for st in mod.tree.body:
            if not _is_main_guard(st):
                continue
            for n in ast.walk(st):
                if isinstance(n, ast.Call):
                    sym = resolver.symbol(n.func, mod, None)
                    if sym and sym[0] == "func":
                        found.setdefault(sym[1], None)
    return list(found)


def _decorator_func(dec: ast.expr) -> ast.expr:
    return dec.func if isinstance(dec, ast.Call) else dec


def _is_command(fn: FunctionInfo) -> bool:
    return any(_COMMAND.search(ast.unparse(_decorator_func(d))) for d in fn.decorators)


def _is_handler(fn: FunctionInfo, index: ProjectIndex, objects: ExternalObjects) -> bool:
    """装饰器是项目外对象上的方法（@x.方法 / @x.方法(...)，x 是外部导入的名字或用外部库建出来的模块级对象）。"""
    roots = objects.module_roots(index.modules[fn.file])
    for dec in fn.decorators:
        func = _decorator_func(dec)
        if isinstance(func, ast.Attribute):
            base = func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in roots:
                return True
    return False


def _drives_entry(key: str, functions: dict[str, FunctionGraph]) -> bool:
    """从这个函数沿调用能走到一个已经认出来的入口（路由、任务、工作流）：它只是把那条功能线跑起来的演示 / 启动代码。"""
    seen, stack = {key}, [key]
    while stack:
        for k in functions[stack.pop()].call_targets:
            if k in functions and k not in seen:
                if functions[k].kind in ENTRY_KINDS:
                    return True
                seen.add(k)
                stack.append(k)
    return False


def auto_entries(functions: dict[str, FunctionGraph], index: ProjectIndex, resolver: Resolver,
                 objects: ExternalObjects, referenced: set[str], fallback: bool,
                 named: set[str] = frozenset()) -> dict[str, str]:
    """函数 key -> 认出它的方式（comment / main / command / handler / fallback）。只给普通函数；已经是路由、任务、
    工作流、步骤、判断的不动。入口注释里写了标题的，标题直接记到函数图上。named：配置的 extra_entries 点了名的函数——
    它们照配置来，这里不碰；有点名的入口时也不做兜底。"""
    called = {k for fg in functions.values() for k in fg.call_targets}
    plain = {k: fg for k, fg in functions.items() if fg.kind == "function" and k not in named}
    found: dict[str, str] = {}
    for key, fg in plain.items():
        title = comment_mark(index.functions[key], index)
        if title is not None:
            found[key] = "comment"
            if title:
                fg.title, fg.title_full = title[:60], (title if len(title) > 60 else "")
                if fg.nodes and fg.nodes[0].type == "start":
                    fg.nodes[0].title = fg.title
    for key in _main_calls(index, resolver):
        # 里面没有步骤、判断、循环的（now()、usage() 这类顺手调用的小工具）不是一条流程
        if key in plain and not _drives_entry(key, functions) \
                and any(n.type in ("step", "cond", "loop") for n in plain[key].nodes):
            found.setdefault(key, "main")
    for key, fg in plain.items():
        fn = index.functions[key]
        if key in found or fg.parent is not None or not fn.decorators or key in called:
            continue
        if _is_command(fn):
            found[key] = "command"
        elif fn.cls is None and _is_handler(fn, index, objects):
            found[key] = "handler"
    if found or named or not fallback or any(fg.kind in ENTRY_KINDS for fg in functions.values()):
        return found
    top = [k for k, fg in plain.items() if fg.parent is None and index.functions[k].cls is None]
    methods = [k for k, fg in plain.items() if fg.parent is None and index.functions[k].cls is not None
               and not fg.qualname.rsplit(".", 1)[-1].startswith("_")]
    for pool in ([k for k in top if k not in called and k not in referenced],
                 [k for k in top if k not in called],
                 top,
                 [k for k in methods if k not in called and k not in referenced],
                 methods):
        if pool:
            return {k: "fallback" for k in pool}
    return found


def entry_notes(graph: ProjectGraph) -> list[str]:
    """功能线是怎么来的，值得说一句的情况：一条都没有；全靠兜底（没有人调用的顶层函数）。别的情况不说话。"""
    if not graph.lines:
        if not graph.functions:
            return [("没有功能线：项目里没有函数（代码都直接写在文件顶层时画不出流程，把主要的过程放进一个函数里，"
                     "比如 def main()，再重新分析）")]
        return [(f"没有功能线：没有认出路由、任务、工作流、命令或脚本入口；在入口函数上面加一行注释 `{ENTRY_COMMENT} <标题>`，"
                 "或者在配置的 extra_entries 里点名")]
    roots = [graph.functions[ln.root.function] for ln in graph.lines if ln.root.function in graph.functions]
    if roots and all(fg.rule == "fallback" for fg in roots):
        return [(f"入口是推断的：没有认出路由、任务、工作流、命令或脚本入口，把没有人调用的 {len(roots)} 个顶层函数各当作"
                 f"一条功能线（分组「其他入口」）；不对时在真正的入口函数上面加一行注释 `{ENTRY_COMMENT} <标题>`，"
                 "或者在配置的 extra_entries 里点名")]
    return []


def unlisted_roots(graph: ProjectGraph, limit: int | None = 8,
                   mentioned: set[str] = frozenset()) -> tuple[list[FunctionGraph], int]:
    """没有人直接调用、也不在任何功能线上的模块级公开函数（里面至少有一个步骤、判断或循环）：可能是没认出来的入口。
    mentioned：项目里别处提到过的函数（AnalysisNotes.mentioned：当作值交出去的回调、依赖、注册表里的函数，装饰器，
    模块顶层调用的启动代码）——有人用它们，不是没认出来的入口，不算。返回（前 limit 个，总数），按文件和行号排。"""
    from .query import line_functions  # 避免模块之间循环导入

    on_line = {k for ln in graph.lines for k in line_functions(graph, ln)}
    called = {k for fg in graph.functions.values() for k in fg.call_targets}
    found = [fg for k, fg in graph.functions.items()
             if fg.kind == "function" and fg.parent is None and "." not in fg.qualname and not fg.qualname.startswith("_")
             and k not in on_line and k not in called and k not in mentioned
             and any(n.type in ("step", "cond", "loop") for n in fg.nodes)]
    found.sort(key=lambda fg: (fg.file, fg.line))
    return found[:limit], len(found)


def unlisted_notes(graph: ProjectGraph, mentioned: set[str] = frozenset()) -> list[str]:
    shown, total = unlisted_roots(graph, mentioned=mentioned)
    if not total:
        return []
    rows = "、".join(f"{fg.qualname}（{fg.file}:{fg.line}）" for fg in shown)
    return [(f"不在任何功能线上、项目里也没有别处用到的函数 {total} 个：{rows}{'……' if total > len(shown) else ''}。其中是业务入口的，"
             f"在它的 def（有装饰器时是第一个装饰器）上面加一行注释 `{ENTRY_COMMENT} <标题>` 再重新分析，就会成一条功能线")]
