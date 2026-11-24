"""命令行入口：
pystepmap analyze <项目目录> [--config 配置.json] [-o 图.json]
pystepmap check <项目目录> [--config 配置.json] [--treat-as-marked 规则名...] [--files 文件...]
pystepmap verify <图.json> <记录.jsonl> [--allow-stale]
pystepmap serve [--port 5179] [--no-open]
pystepmap mcp
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from pystepmap.analyze import AnalysisNotes, analyze_with_notes
from pystepmap.config import ConfigError, ProjectConfig, load_config
from pystepmap.entries import entry_notes
from pystepmap.index import decode_source, source_lines
from pystepmap.lint import check_project
from pystepmap.model import ProjectGraph, from_json, parse_key, to_json
from pystepmap.rules import known_rule_names, rule_entry_kinds
from pystepmap.selfcheck import classify_unresolved
from pystepmap.verify import (
    DynamicCall,
    RecordError,
    StaleGraphError,
    VerifyReport,
    read_records,
    verify,
)

_LIST_LIMIT = 30  # 近似、隐式调用、强匹配的动态调用各最多列出几处（弱匹配的动态调用全部列出）
_ENTRY_KINDS = ("route", "task", "workflow")
_KIND_NAMES = {"route": "接口", "task": "任务", "condition": "判断函数"}


class _Stop(Exception):
    """命令没法做下去：main 打印这一行中文说明（stderr），退出码 2。"""


def _project_dir(path: str) -> Path:
    root = Path(path)
    if not root.is_dir():
        raise _Stop(f"找不到目录: {root}")
    return root


def _config(args: argparse.Namespace):
    try:
        return load_config(Path(args.config) if args.config else None)
    except (OSError, ValueError) as e:  # 读不到、不是合法 JSON、字段不合法（ConfigError）
        raise _Stop(f"无法读取配置文件 {args.config}: {e}") from None


def _summary(graph: ProjectGraph, notes: AnalysisNotes, config: ProjectConfig | None = None) -> str:
    sc = graph.selfcheck
    # 目标不明的调用大半是第三方、标准库和日志（名字项目里没有定义过，不可能落在项目代码上）：把真正可能是
    # 项目内调用的那部分单独写出来，这个数才有意义
    blind = len(classify_unresolved(graph.functions, (config or ProjectConfig()).noise_receivers).blind)
    rules = ", ".join(notes.rules) if notes.rules else "无"
    excluded = ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in notes.excluded.items()) or "无"
    here = f".（包 {notes.package}）" if notes.package else "."  # 分析的目录本身是包：模块也按包名登记
    out = [
        f"启用的识别规则: {rules}",
        f"源码根: {', '.join(r or here for r in notes.source_roots)}",
        f"排除的目录: {excluded}",
        f"函数: {len(graph.functions)}",
        f"功能线: {len(graph.lines)}",
        f"公共任务: {len(graph.common_tasks)}",
        f"跳过的文件: {len(sc.skipped_files)}",
        *(f"  {f}: {why}" for f, why in sc.skipped_files),
        f"目标不明的调用: {sc.unresolved_calls}（可能落在项目代码上的 {blind}）",
        f"推测的调用: {sc.guessed_calls}",
        f"走不到的节点: {len(sc.unreachable)}",
        f"无出口的节点: {len(sc.dead_ends)}",
    ]
    out += entry_notes(graph) + notes.marks
    return "\n".join(out) + "\n"


def _cmd_analyze(args: argparse.Namespace) -> int:
    """分析一个项目，输出图 JSON"""
    root = _project_dir(args.project)
    config = _config(args)
    if args.output and not Path(args.output).parent.is_dir():  # 先查：别分析完了才发现写不进去
        raise _Stop(f"输出目录不存在: {Path(args.output).parent}")
    graph, notes = analyze_with_notes(root, config)
    text = to_json(graph)
    summary = _summary(graph, notes, config)
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as e:
            raise _Stop(f"无法写入 {args.output}: {e.strerror or e}") from None
        sys.stdout.write(summary)
    else:
        sys.stdout.write(text + "\n")
        sys.stderr.write(summary)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """检查流程代码有没有违反约束"""
    root = _project_dir(args.project)
    config = _config(args)
    unknown = sorted(set(args.treat_as_marked or []) - known_rule_names(config))
    if unknown:
        raise _Stop(f"--treat-as-marked 里有不认识的识别规则: {', '.join(unknown)}"
                    f"（可用的: {', '.join(sorted(known_rule_names(config)))}）")
    # 检查只看工作流和步骤：只识别接口 / 任务的规则当作已标记也不会多查一个函数，说清楚，不让人以为它们被检查了
    kinds = rule_entry_kinds(config)
    useless = [n for n in dict.fromkeys(args.treat_as_marked or [])
               if n != "pystepkit" and not kinds[n] & {"workflow", "step"}]
    if useless:
        found = "；".join(f"{n} 只识别{'、'.join(_KIND_NAMES.get(k, k) for k in sorted(kinds[n])) or '派发，不识别入口'}"
                         for n in useless)
        raise _Stop(f"--treat-as-marked {' '.join(useless)} 不起作用：检查只看工作流和步骤，{found}。"
                    "接口、任务要用 pystepkit 的 @workflow 标记才会被检查（和路由、任务的装饰器写在同一个函数上）")
    result = check_project(root, config, treat_as_marked=frozenset(args.treat_as_marked or []),
                           only_files=args.files)
    for f in result.unmatched:
        print(f"警告: --files 里的 {f} 不对应项目里的任何 .py 文件（相对项目根目录；排除的目录不算）", file=sys.stderr)
    for v in result.violations:
        print(f"{v.file}:{v.line}: {v.code} {v.message}")
    print(f"共 {len(result.violations)} 处违规")
    if result.ignored:  # 指到项目之外或指到非 .py 的符号链接、FIFO 等：不是源文件，只提醒，不影响退出码
        print(f"警告: 以下 {len(result.ignored)} 个 .py 路径不是项目里的源文件，没有读，也没有检查:", file=sys.stderr)
        for f, why in result.ignored:
            print(f"  {f}: {why}", file=sys.stderr)
    if result.skipped:
        print(f"以下 {len(result.skipped)} 个文件解析不了，没有检查:", file=sys.stderr)
        for f, why in result.skipped:
            print(f"  {f}: {why}", file=sys.stderr)
        return 2
    return 1 if result.violations else 0


class _Source:
    """按需读项目里的源码行，供核对报告引用。"""

    def __init__(self, root: str):
        self.root = Path(root)
        self._lines: dict[str, list[str]] = {}

    def __call__(self, file: str, line: int) -> str:
        if file not in self._lines:
            try:
                self._lines[file] = source_lines(decode_source((self.root / file).read_bytes()))
            except (OSError, SyntaxError, UnicodeDecodeError):  # 读不到、编码声明不认识、按声明的编码解不开
                self._lines[file] = []
        rows = self._lines[file]
        return rows[line - 1].strip()[:100] if 0 < line <= len(rows) else ""


def _verify_text(r: VerifyReport, graph: ProjectGraph) -> str:
    count = {kind: sum(m.count for m in r.mismatches if m.kind == kind) for kind in ("edge", "end", "call")}
    strong = sum(d.count for d in r.dynamic_call_list if d.strong)
    out = [
        f"图中函数: {r.functions_in_graph}",
        f"执行到的函数: {r.functions_seen}",
        f"不同的真实路径: {r.distinct_paths}",
        f"走过的步数: {r.steps_total}",
        f"在图上: {r.steps_on_graph}",
        f"异常与收尾入口（近似）: {r.approx_entries}",
        f"未捕获异常退出: {r.uncaught_exits}",
        f"核对的调用: {r.calls_total}",
        f"隐式调用（属性、魔术方法）: {r.implicit_calls}",
        f"动态调用（静态未确定目标）: {r.dynamic_calls}",
        f"动态调用中的强匹配: {strong}",
        f"动态调用中的弱匹配: {r.dynamic_calls - strong}",
        f"截断的记录: {r.truncated_records}",
        f"图里没有的函数: {r.unknown_functions}",
        f"插件错误: {r.plugin_errors}",
        f"连线不在图上: {count['edge']}",
        f"结局不在图上: {count['end']}",
        f"调用不在图上: {count['call']}",
    ]
    src = _Source(graph.root)
    titles = {"edge": "连线不在图上", "end": "结局不在图上", "call": "调用不在图上"}
    for kind, title in titles.items():
        found = [m for m in r.mismatches if m.kind == kind]
        if not found:
            continue
        out += ["", f"== {title}（{len(found)} 处，按出现次数从多到少）"]
        for m in found:
            file = parse_key(m.fn)[0]
            if kind == "edge":
                out += [f"  {m.count}×  {m.fn}  第 {m.from_line} 行 → 第 {m.to_line} 行（{m.detail}）",
                        f"      {m.from_line} | {src(file, m.from_line)}",
                        f"      {m.to_line} | {src(file, m.to_line)}"]
            elif kind == "end":
                out += [f"  {m.count}×  {m.fn}  第 {m.from_line} 行（{m.detail}）",
                        f"      {m.from_line} | {src(file, m.from_line)}"]
            else:
                callee_file = m.detail.split("::", 1)[0]
                out += [f"  {m.count}×  {m.fn} 第 {m.from_line} 行 → {m.detail}",
                        f"      {file}:{m.from_line} | {src(file, m.from_line)}",
                        f"      {callee_file}:{m.to_line} | {src(callee_file, m.to_line)}"]
    if r.approx_list:
        shown = r.approx_list[:_LIST_LIMIT]
        out += ["", f"== 异常与收尾的近似（共 {len(r.approx_list)} 处，按出现次数列出前 {len(shown)} 处）"]
        for a in shown:
            file = parse_key(a.fn)[0]
            to = f"第 {a.to_line} 行" if a.to_line else "函数末尾"  # 0：隐含的"执行到函数末尾"
            out += [f"  {a.count}×  {a.fn}  第 {a.from_line} 行 → {to}（入口）",
                    f"      {a.from_line} | {src(file, a.from_line)}"]
            if a.to_line:
                out.append(f"      {a.to_line} | {src(file, a.to_line)}")
    if r.implicit_call_list:
        shown = r.implicit_call_list[:_LIST_LIMIT]
        out += ["", f"== 隐式调用（属性、魔术方法，共 {len(r.implicit_call_list)} 处，按出现次数列出前 {len(shown)} 处）"]
        reasons = {"property": "属性", "dunder": "魔术方法"}
        out += [f"  {d.count}×  {d.caller} → {d.callee}（{reasons[d.reason]}）" for d in shown]
    weak = [d for d in r.dynamic_call_list if not d.strong]
    if weak:
        out += ["", f"== 动态调用 · 弱匹配（共 {len(weak)} 处，全部列出，按出现次数）"]
        out += [line for d in weak for line in _dynamic_lines(d, src)]
    strong_list = [d for d in r.dynamic_call_list if d.strong]
    if strong_list:
        shown = strong_list[:_LIST_LIMIT]
        out += ["", f"== 动态调用 · 强匹配（共 {len(strong_list)} 处，按出现次数列出前 {len(shown)} 处）"]
        out += [line for d in shown for line in _dynamic_lines(d, src)]
    if r.plugin_error_list:
        out += ["", "== 插件错误"] + [f"  {e}" for e in r.plugin_error_list]
    return "\n".join(out) + "\n"


def _dynamic_lines(d: DynamicCall, src: _Source) -> list[str]:
    file = parse_key(d.caller)[0]
    mark = "强匹配，回调" if d.callback else "强匹配" if d.strong else "弱匹配"
    note = "同一语句里作为参数传出" if d.callback else "同一语句里无法判断目标的调用"
    return [f"  {d.count}×  {d.caller} 第 {d.line} 行 → {d.callee}（{mark}）",
            f"      {file}:{d.line} | {src(file, d.line)}",
            f"      {note}：{'、'.join(d.names)}"]


def _cmd_verify(args: argparse.Namespace) -> int:
    """用测试的执行记录核对图"""
    try:
        graph = from_json(Path(args.graph).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise _Stop(f"无法读取图文件 {args.graph}: {e}") from None
    try:
        report = verify(graph, read_records(args.trace), allow_stale=args.allow_stale)
    except StaleGraphError as e:
        if e.changed:
            print("图已过期，以下文件在分析后发生了变化:", file=sys.stderr)
            for f in e.changed:
                print(f"  {f}", file=sys.stderr)
        if e.unreadable:
            print(f"图已过期，以下文件读不到（被删除、移动或没有权限；图的根目录 {graph.root}）:", file=sys.stderr)
            for f in e.unreadable:
                print(f"  {f}", file=sys.stderr)
        print("请重新分析并重跑测试；确实要用旧图核对时加 --allow-stale", file=sys.stderr)
        return 2
    except (OSError, RecordError) as e:
        raise _Stop(f"无法读取记录文件 {args.trace}: {e}") from None
    sys.stdout.write(_verify_text(report, graph))
    if report.plugin_errors:
        print(f"核对不算数：记录插件出过 {report.plugin_errors} 次错误，记录不完整（见上面的插件错误）", file=sys.stderr)
        return 2
    if report.functions_seen == 0:
        print("核对不算数：记录里没有一个函数对得上图（记录是空的，或者不是用这张图录的）", file=sys.stderr)
        return 2
    return 0 if report.ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """启动平台"""
    if not 1 <= args.port <= 65535:
        raise _Stop(f"端口要在 1 到 65535 之间，实际是 {args.port}")
    try:  # 平台后端是另一个包（pystepmap-server，依赖 FastAPI）：用到时才导入，core 自己只依赖标准库
        from pystepmap_server.cli import serve
    except ModuleNotFoundError as e:
        raise _Stop(f"没有安装平台后端（缺少 {e.name}）：在 pystepmap 仓库根目录运行 uv sync，再用 uv run pystepmap serve 启动"
                    "（不用 uv 时：pip install ./server）") from None
    return serve(port=args.port, open_browser=not args.no_open)


def _cmd_mcp(args: argparse.Namespace) -> int:
    """启动给 Claude Code 用的 MCP 服务"""
    try:  # 和 serve 一样用到时才导入后端（它依赖 MCP SDK）；导入失败的提示只写标准错误，标准输出留给协议
        from pystepmap_server.mcp_server import run
    except ModuleNotFoundError as e:
        raise _Stop(f"没有安装平台后端（缺少 {e.name}）：在 pystepmap 仓库根目录运行 uv sync，再用 uv run pystepmap mcp 启动"
                    "（不用 uv 时：pip install ./server）") from None
    return run()


def _cmd_requirements(args: argparse.Namespace) -> int:
    """列出项目里的需求，把设计的流程和实现对照"""
    from pystepmap import requirements as rq

    root = _project_dir(args.project)
    found = rq.load_requirements(root)
    if not found:
        print(f"{root} 里没有需求（{rq.DIR}/*.md，或 OpenSpec 文件里的 ```flow 代码块）")
        return 0
    graph, _ = analyze_with_notes(root, _config(args))
    worst = 0
    for r in found:
        print(f"{r.id or '（没有编号）'}  {r.title}  [{r.status or '？'}]  {r.file}")
        if r.errors:
            worst = 2
            print("  这条需求读不懂，没有对照：")
            print("\n".join(f"    {e}" for e in r.errors))
            continue
        cmp = rq.compare(r, graph)
        if cmp.matches:
            print(f"  设计和实现对得上（功能线：{cmp.line_title}）")
        else:
            if r.archived:  # 已归档的是当时的设计：和现在的代码不一致照样列，不算没通过
                print("  （已归档的变更：下面的不一致不影响退出码）")
            else:
                worst = max(worst, 1)
            no_line = next((d for d in cmp.differences if d.kind == "no_line"), None)
            if no_line is not None:  # 没有可以对照的实现：不数成"几处不一致"，设计的节点也不逐个列
                print(f"  {no_line.text}；设计的 {len(cmp.differences) - 1} 个节点都还没有实现")
            else:
                print(f"  和实现有 {len(cmp.differences)} 处不一致（功能线：{cmp.line_title}）：")
                print("\n".join(f"    {d.text}" for d in cmp.differences))
        print("\n".join(f"  注意：{n}" for n in cmp.notes), end="\n" if cmp.notes else "")
    return worst


def _cmd_kit(args: argparse.Namespace) -> int:
    """把约束套件装进别的项目，或者撤销"""
    from pystepmap import kit

    root = Path(args.project)
    try:
        if args.action == "uninstall":
            if not args.yes and not _confirm(f"撤销 {root.expanduser().resolve()} 里装的约束套件？"):
                print("没有改动任何文件")
                return 1
            print("\n".join(kit.uninstall(root)))
            return 0
        plan = kit.plan_install(root, shlex.split(args.command) if args.command else None, args.pre_commit,
                                mcp=not args.no_mcp, local_settings=args.local_settings)
        print("\n".join(plan.lines()))
        if args.dry_run:
            print("（--dry-run：只列出，没有写）")
            return 0
        if not args.yes and not _confirm("写入这些文件？"):
            print("没有改动任何文件")
            return 1
        kit.apply_install(plan)
        print(f"装好了。撤销：pystepmap kit uninstall {shlex.quote(str(plan.root))}")
        return 0
    except kit.KitError as e:
        print(e, file=sys.stderr)
        return 2


def _confirm(question: str) -> bool:
    """在终端上问一句，回答 y / yes 才算同意；不是终端（没法问）时不同意——要用 --yes 明说。"""
    if not sys.stdin.isatty():
        print(f"{question} 这里不是终端，没法确认：加上 --yes 再运行", file=sys.stderr)
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pystepmap", description="把 Python 后端代码静态分析成流程图")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("analyze", help="分析项目并输出图 JSON")
    p.add_argument("project", help="项目目录")
    p.add_argument("--config", help="配置文件（JSON）")
    p.add_argument("-o", "--output", help="图 JSON 的输出路径；省略时写到标准输出")
    p.set_defaults(func=_cmd_analyze)
    c = sub.add_parser("check", help="检查带标记的函数是否符合编排约束")
    c.add_argument("project", help="项目目录")
    c.add_argument("--config", help="配置文件（JSON）")
    c.add_argument("--treat-as-marked", nargs="+", metavar="规则名",
                   help="把这些识别规则（如 dbos）识别出的工作流和步骤也当作已标记；只识别接口、任务的规则（fastapi、taskiq）不能给")
    c.add_argument("--files", nargs="+", metavar="文件", help="只报告这些文件（相对项目根目录）里的违规")
    c.set_defaults(func=_cmd_check)
    v = sub.add_parser("verify", help="把测试中记录的真实执行路径放到图上核对")
    v.add_argument("graph", help="pystepmap analyze 产出的图 JSON")
    v.add_argument("trace", help="pytest -p pystepmap.trace 产出的执行记录（JSONL）")
    v.add_argument("--allow-stale", action="store_true", help="源码在分析之后改过也照样核对（行号可能对不上）")
    v.set_defaults(func=_cmd_verify)
    s = sub.add_parser("serve", help="启动本机平台（只监听 127.0.0.1），在浏览器里浏览项目的功能线")
    s.add_argument("--port", type=int, default=5179, help="端口（默认 5179）")
    s.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    s.set_defaults(func=_cmd_serve)
    m = sub.add_parser("mcp", help="在标准输入输出上提供 MCP 服务，给 Claude Code 查询项目的流程（和 serve 共用数据目录）")
    m.set_defaults(func=_cmd_mcp)
    r = sub.add_parser("requirements", help="列出项目里的需求（.pystepmap/requirements/ 和 OpenSpec 文件里的 flow 代码块），把设计的流程和代码的流程对照")
    r.add_argument("project", help="项目目录")
    r.add_argument("--config", help="配置文件（JSON）")
    r.set_defaults(func=_cmd_requirements)
    k = sub.add_parser("kit", help="把约束套件（skill、编辑后的检查钩子、MCP 配置）装进别的项目，或者撤销")
    k.add_argument("action", choices=["install", "uninstall"])
    k.add_argument("project", help="目标项目的根目录")
    k.add_argument("--yes", action="store_true", help="不问，直接写（不在终端里运行时必须给）")
    k.add_argument("--dry-run", action="store_true", help="只列出要写什么，不写")
    k.add_argument("--pre-commit", action="store_true", help="同时装 git 的提交前检查（已有提交前钩子的仓库不装）")
    k.add_argument("--no-mcp", action="store_true",
                   help="不往项目的 .mcp.json 里写 pystepmap 的 MCP 服务（已经在用户级配过时用）")
    k.add_argument("--local-settings", action="store_true",
                   help="钩子写进 .claude/settings.local.json（不入库，只对本机生效）")
    k.add_argument("--command", help="钩子和 MCP 用来运行 pystepmap 的命令（默认是现在这个 pystepmap 的绝对路径）")
    k.set_defaults(func=_cmd_kit)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        return 2
    try:
        return args.func(args)
    except _Stop as e:
        print(e, file=sys.stderr)
        return 2
    except ConfigError as e:  # 运行中才发现的配置问题（如 source_roots 里的目录不存在）
        print(f"配置不合法: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
