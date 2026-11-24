"""平台的第五条流程：运行测试并核对（run_project_tests）。只有平台接口（api.py 的 POST /api/projects/{id}/runs）导入
这个模块；pystepmap mcp 的进程（mcp_server.py 只导入 store、mcp_tools）、分析子进程（worker.py）、另外四条流程（flows.py）
都不导入它，所以也带不进执行项目代码的 runner.py——分析那条只读的路和"运行项目测试"这条执行的路按导入分开（测试真的
走一遍 pystepmap mcp 和分析子进程的入口，再查 sys.modules）。

按 flow-style 写（.claude/skills/flow-style/SKILL.md）：编排函数只写走向，干活的事都在步骤里，判断函数只看步骤的结果。
共用的步骤（读取项目、检查项目目录、运行分析、比较、保存分析、清理旧分析）和标记 / 项目锁来自 flows.py。

结局：kind "ok" 或 "failed"，failed 带 reason（中文）和 code（状态码见 flows 的说明）。流程在后台线程里跑
（runner.launch），拿到项目的锁之前的结局（not_found、no_command、confirm、invalid、busy）由接口直接回给界面；之后的
失败接口已经回过 202 了，由 runner.last_failure 告诉界面。测试命令没跑完（超时、取消、没能启动）时结局是 failed，code
为 timeout / cancelled / failed_to_start，同时已经存下一条运行（带 run_id）。

执行项目代码的只有它调用的 runner.run_tests（见 runner.py 的说明），和分析的子进程（worker.py，只读）是分开的两条路。
它拿和分析同一个标记、同一把项目锁（mark_busy），一直拿到这次运行存好：测试跑着的时候分析、标为已看、另一次运行都得到
busy；/status、项目列表照常应答（它们只是探一下锁）。
- 图过期（和 /status 同一个口径）时先用重新分析的那几个步骤分析并保存，再跑测试，这次运行用新的那次分析；
- 临时文件（图的副本、记录文件）在 <数据目录>/runs/tmp-<随机>（0700，文件 0600），每个结局之前都删掉（被 Exception
  以外的异常打断时由步骤删，_clean_up_if_aborted；服务被强杀时由 runner 的看门狗删）；
- 状态：有失败或出错的用例 tests_failed；否则命令退出码 0 ok，其余 error；记录文件读不了 error；一条记录（执行记录和
  测试结果都算）都没收到 error；超时 timeout、取消 cancelled（服务停止时被中止的也是 cancelled，说明不同）、没能启动
  error（这几种没有路径）。记录读不了、一条记录都没收到是编排函数里的两个判断，图上看得见；
- 说明（note）在命令以非 0 退出、或者没收到记录时先说退出码的意思（127 / 126 命令找不到 / 不能执行，5 pytest 一个
  测试都没收集到，4 参数或路径不对，3 pytest 内部错误（常见原因：被测项目的 Python 低于 3.12，记录插件装不上），2 被
  中断或收集测试时出错，其余"命令以退出码 N 结束"），没收到记录、命令里给 PYTHONPATH 赋了值又没接上原来的时说
  "命令里设了 PYTHONPATH，把记录插件挡住了（写成 PYTHONPATH=src:$PYTHONPATH）"，再说别的可能，最后"原文见运行输出"；
- 运行期间代码变了（核对时图里的文件和分析时不同）：照样核对（allow_stale），stale 里列出这些文件，说明里写明；
- 被测项目自己又挂了一份不看单份启用记号的旧版插件、两份都在记时，插件写一行 SecondPlugin 记号，说明里提醒
  "记录文件里有两份记录插件的输出……数字可能重复"；
- 库里不存任何取值：pytest 的输出只在 runner 的内存里；插件 error 行只存类型名和个数（原文进内存里的输出末尾）；
  记录读不了、核对或汇总出错时说明里只写异常的类型名。
汇总（runs.summary_json）：
    {"tests", "passed", "failed", "skipped", "errors"（用例数，按测试结果行；errors 是 setup / teardown 出错的）,
     "seconds"（测试命令跑了几秒）,
     "verify": {"ok"（核对没有不符；一条记录都没收到时 null——什么都没核对）, "mismatches": {"edge", "end", "call"}（各类
                不符的次数）, "functions_seen", "distinct_paths", "steps_total", "plugin_errors"（个数）,
                "plugin_error_types": {类型名: 个数}, "truncated_records", "stale": [运行期间变了的文件，最多 20 个]}
               | null（没跑完、记录读不了时）,
     "coverage": runpath.covered_summary 的结果 | null,
     "mismatch_list": [{kind, function（函数 sid）, from_line, to_line, detail（引擎给的结构性说明）, count}]，最多 30 条,
     "note": 中文说明，没有就是 ""}
每个测试一项（runs.tests_json）：{test（node id，去掉参数化后缀；"" 是测试之外的执行）, outcome, seconds, cases,
functions（这个测试执行到的图里的函数个数）}。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pystepkit import condition, outcome, step, workflow
from pystepmap.model import ProjectGraph, from_json
from pystepmap.runpath import build_run_paths, covered_summary, run_paths_to_json
from pystepmap.verify import (
    RecordError,
    VerifyReport,
    read_records,
    stale_files,
    verify,
)

from . import runner
from .flows import (
    analysis_failed,
    check_project_directory,
    clear_mark,
    clear_mark_if_aborted,
    compare_with_previous,
    directory_is_unusable,
    load_project,
    project_is_missing,
    prune_old_analyses,
    run_analysis,
    save_analysis,
    take_mark,
)
from .live import LiveRun
from .shared import load_facts, tests_on_line
from .status import staleness
from .store import NewRun, ProjectMissingError, Store, StoreError, private_file
from .worker import Analyzer

logger = logging.getLogger(__name__)

KEEP_RUNS = 5  # 每个项目最多保留最近几次测试运行
MAX_STALE_FILES = 20  # 运行的汇总里最多列几个运行期间变了的文件
MAX_MISMATCHES = 30  # 运行的汇总里最多列几条不符
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,99}")
# 标记里的 title、group 要写成字面量：引擎只认字面量，写成常量名时图上取不到

# 退出码的意思（pytest 的约定，127 / 126 是 shell 的）
_EXIT_MEANING = {
    127: "命令找不到（退出码 127）",
    126: "命令不能执行（退出码 126）",
    5: "pytest 一个测试都没收集到（退出码 5）",
    4: "pytest 的参数或路径不对（退出码 4）",
    3: "pytest 内部错误（退出码 3；常见原因：被测项目的 Python 低于 3.12，记录插件装不上）",
    2: "测试被中断了，或者收集测试时出错（退出码 2；常见原因：测试文件导入失败）",
}
SHUTDOWN_NOTE = "服务停止时测试还在跑，被中止了"
SECOND_PLUGIN = "SecondPlugin"  # 插件写的记号（core/src/pystepmap/trace.py 的 pytest_sessionstart）
SECOND_PLUGIN_NOTE = "记录文件里有两份记录插件的输出（被测项目的环境里装了旧版 pystepmap 的插件？），数字可能重复。"
SHUTDOWN_BEFORE_START = "服务停止时正在重新分析，测试还没开始"
SHUTDOWN_PREPARING = "服务停止时测试还没开始"
_MAYBE = ("其他可能：命令没有运行 pytest；被测项目的 Python 低于 3.12（记录插件要用 sys.monitoring）；记录插件被禁用了"
          "（比如 PYTEST_ADDOPTS 被命令覆盖、-p no:pystepmap_trace_plugin）。")
_PYTHONPATH_HINT = "命令里设了 PYTHONPATH，把记录插件挡住了（写成 PYTHONPATH=src:$PYTHONPATH）。"
_SETS_PYTHONPATH = re.compile(r"(?<![\w$])PYTHONPATH=(\S*)")
# 全量运行（不挑测试）时"挑出来的测试"的样子：没有名单、没有范围
ALL_TESTS = {"ok": True, "tests": None, "scope": None, "reason": "", "code": ""}
_NONE_FOUND = "名单里的测试一个都没找到（可能改名或删掉了）：重新运行一次全部测试，再跑这条线。"


# ---------- 普通函数：步骤共用的计算 ----------

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _clean_up_if_aborted(mark: dict | None, prepared: dict | None) -> Iterator[None]:
    """同 flows.clear_mark_if_aborted，另外删掉这次运行的临时目录（图的副本、记录文件）。"""
    try:
        yield
    except BaseException:
        if prepared is not None:
            runner.remove_scratch(prepared.get("scratch"))
        clear_mark(mark)
        raise


def pythonpath_hint(command: str) -> str:
    """命令里给 PYTHONPATH 赋了值、又没接上原来的（$PYTHONPATH）：平台放在 PYTHONPATH 里的记录插件目录被挡住了，给一句
    怎么写；否则空串。"""
    sets = [m.group(1) for m in _SETS_PYTHONPATH.finditer(command)]
    return _PYTHONPATH_HINT if any("PYTHONPATH" not in value for value in sets) else ""


def exit_text(code: int | None) -> str:
    """退出码说成中文（见模块说明）。"""
    if code is None:
        return "命令没有退出码"
    if code in _EXIT_MEANING:
        return _EXIT_MEANING[code]
    if code == 0:
        return "命令正常结束（退出码 0）"
    if code < 0:
        return f"命令被信号 {-code} 结束"
    return f"命令以退出码 {code} 结束"


def _error_type(text: str) -> str:
    """插件 error 行（"类型: 信息"）的类型名；信息不要（可能带插件自己的异常信息），认不出类型名时"未知"。"""
    name = text.split(":", 1)[0].strip()
    return name if _ERROR_TYPE.fullmatch(name) else "未知"


def _counted(records: Iterable[dict], counts: Counter, errors: list[str]) -> Iterator[dict]:
    """原样给出记录，顺便数：执行记录几条（executions）、测试结果按 outcome 各几个用例（results 是总数）、插件 error 行。"""
    for rec in records:
        if "fn" in rec:
            counts["executions"] += 1
        elif "outcome" in rec:
            n = rec.get("n", 1)
            counts[rec["outcome"]] += n
            counts["results"] += n
        elif "error" in rec:
            errors.append(str(rec["error"]))
        yield rec


def _verify_records(prepared: dict) -> dict:
    """read_and_verify 的实际工作：读图的副本，边读记录边数、边核对（图过期也照样核对），再看哪些文件和分析时不同。"""
    graph = from_json(Path(prepared["graph_path"]).read_text(encoding="utf-8"))
    counts: Counter = Counter()
    errors: list[str] = []
    report = verify(graph, _counted(read_records(prepared["trace_path"]), counts, errors), allow_stale=True)
    changed, unreadable = stale_files(graph)
    return {"ok": True, "graph": graph, "report": report, "stale": sorted(set(changed) | set(unreadable)),
            "counts": counts, "errors": errors, "error_types": Counter(_error_type(e) for e in errors),
            "received": bool(counts["executions"] or counts["results"]), "problem": ""}


def _empty_summary(seconds: float, note: str) -> dict:
    """没有记录可汇总时（没跑完、记录读不了）的汇总：计数都是 0，verify、coverage 为 None。"""
    return {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0, "seconds": seconds, "verify": None,
            "coverage": None, "mismatch_list": [], "note": note}


def _verify_summary(checked: dict) -> dict:
    report: VerifyReport = checked["report"]
    kinds = Counter({"edge": 0, "end": 0, "call": 0})
    for m in report.mismatches:
        kinds[m.kind] += m.count
    return {"ok": report.ok if checked["received"] else None, "mismatches": dict(kinds),
            "functions_seen": report.functions_seen, "distinct_paths": report.distinct_paths,
            "steps_total": report.steps_total, "plugin_errors": report.plugin_errors,
            "plugin_error_types": dict(checked["error_types"]), "truncated_records": report.truncated_records,
            "stale": checked["stale"][:MAX_STALE_FILES]}


def _mismatch_list(graph: ProjectGraph, report: VerifyReport) -> list[dict]:
    """不符的前 MAX_MISMATCHES 条（按次数从多到少）：函数按 sid；detail 是引擎给的结构性说明（节点标题、函数 key）。"""
    out = []
    for m in report.mismatches[:MAX_MISMATCHES]:
        fg = graph.functions.get(m.fn)
        out.append({"kind": m.kind, "function": (fg.sid or m.fn) if fg is not None else m.fn,
                    "from_line": m.from_line, "to_line": m.to_line, "detail": m.detail, "count": m.count})
    return out


def _plugin_note(checked: dict) -> str:
    """插件 error 行的说明。SecondPlugin 是新版插件写的记号：同一个进程里还有一份不看单份启用记号的旧版插件也在记录
    （被测项目自己挂的），两份的记录都在文件里——单独说成"数字可能重复"，不当作插件出错。"""
    types = Counter(checked["error_types"])
    note = SECOND_PLUGIN_NOTE if types.pop(SECOND_PLUGIN, 0) else ""
    if types:
        note += (f"记录插件报错：{'、'.join(f'{k} ×{n}' for k, n in sorted(types.items()))}（原文见运行输出），"
                 f"这次的记录可能不全。")
    return note


def _files_text(files: list[str]) -> str:
    shown = "、".join(files[:5])
    return shown + (f" 等 {len(files)} 个文件" if len(files) > 5 else "")


def _summarize(prepared: dict, ran: dict, checked: dict) -> dict:
    """summarize_run 的实际工作（收到了记录、记录读得了），返回要保存的 {"status", "exit_code", "summary", "tests",
    "paths", "function_index"}。function_index：运行里的函数 sid 每个一次（排序）、每个测试的下标——按功能线数一个测试
    执行了几个函数时用，不用解开整份路径。"""
    seconds, exit_code = ran["seconds"], ran["exit_code"]
    graph, report, counts = checked["graph"], checked["report"], checked["counts"]
    paths = build_run_paths(graph, read_records(prepared["trace_path"]))
    failed = counts["failed"] + counts["error"]
    status = "tests_failed" if failed else ("ok" if exit_code == 0 else "error")
    notes = []
    if status == "error":
        notes.append(f"{exit_text(exit_code)}，但没有失败的测试：可能是收集测试时出错，或者命令本身出错，原文见运行输出。")
    if checked["stale"]:
        notes.append(f"运行期间代码有改动（{_files_text(checked['stale'])}），路径可能对不上图，核对里的不符不一定是图的"
                     f"问题。")
    if not counts["executions"]:
        notes.append("测试没有执行到图里的任何函数（没有执行记录），运行路径是空的。")
    notes.append(_plugin_note(checked))
    tests = [{"test": t.test, "outcome": t.outcome, "seconds": round(t.seconds, 6), "cases": t.cases,
              "functions": len(t.functions)} for t in paths.tests]
    names = sorted({f for t in paths.tests for f in t.functions})
    at = {f: i for i, f in enumerate(names)}
    function_index = {"functions": names, "tests": {t.test: sorted(at[f] for f in t.functions) for t in paths.tests}}
    summary = {"tests": counts["results"], "passed": counts["passed"], "failed": counts["failed"],
               "skipped": counts["skipped"], "errors": counts["error"], "seconds": seconds,
               "verify": _verify_summary(checked), "coverage": covered_summary(graph, paths),
               "mismatch_list": _mismatch_list(graph, report), "note": "".join(notes)}
    return {"status": status, "exit_code": exit_code, "summary": summary, "tests": tests,
            "paths": run_paths_to_json(paths), "function_index": function_index}


def _no_paths(status: str, exit_code: int | None, summary: dict) -> dict:
    return {"status": status, "exit_code": exit_code, "summary": summary, "tests": None, "paths": None,
            "function_index": None}


# ---------- 步骤 ----------

@step(title="标记这个项目正在忙")
def mark_busy(store: Store, project_id: str) -> dict:
    """和分析用同一个标记、同一把锁（flows.take_mark）：测试跑着的时候分析、标为已看、另一次运行都得到"正在忙"。拿到
    了再记下开始的时间（started_at、t0），并告诉进程内的运行表"开始了"（runner.announce：接口据此回 202）。"""
    mark = take_mark(store, project_id)
    if mark["marked"]:
        mark["started_at"], mark["t0"] = _now(), time.monotonic()
        runner.announce(store.path.parent, project_id)
    return mark


@step(title="取消正在忙的标记")
def unmark_busy(mark: dict) -> None:
    """运行测试用的标记就是分析的那个：同样取消、放开项目锁。"""
    clear_mark(mark)


@step(title="说明正在运行前重新分析")
def note_reanalysis(store: Store, project_id: str, mark: dict | None = None) -> None:
    """图过期、运行前要先重新分析：运行表里的阶段改成 analyzing（这时取消说"正在重新分析，分析完就停"，项目列表说正在分析）。
    之前一直是 preparing：没有重新分析的运行不会报成"正在分析"。"""
    with clear_mark_if_aborted(mark):
        runner.set_phase(store.path.parent, project_id, "analyzing")


@step(title="检查图是否过期")
def check_graph_freshness(store: Store, project: dict, mark: dict | None = None) -> dict:
    """最新一次分析之后项目里的 .py 有没有改过（改、增、删，和 /status 同一个口径：按那次分析的配置和指纹比）。返回
    {"stale", "analysis_id"}；没有分析、没法检查时算过期（重新分析时会说清原因）。"""
    with clear_mark_if_aborted(mark):
        latest = store.latest_analysis(project["id"])
        facts = None if latest is None else load_facts(store, latest["id"])
        if facts is None:
            return {"stale": True, "analysis_id": None}
        config = facts.config if facts.config is not None else project["config"]
        st = staleness(project["root"], config, facts.hashes, facts.file_lines)
        return {"stale": not st.get("checked") or bool(st["stale"]), "analysis_id": latest["id"]}


def _cannot_select(code: str, reason: str) -> dict:
    return {"ok": False, "tests": None, "scope": None, "reason": reason, "code": code}


def pick_line_tests(store: Store, project_id: str, analysis_id: int | None, line: str) -> dict:
    """从最近一次跑完的**全量**运行里，找出执行过这条功能线上函数的测试（线上的函数按 analysis_id 那次分析的图）。
    返回 {"ok", "tests": [测试 id], "scope": {"line", "line_title", "tests": 个数, "base_run"}, "reason", "code"}；
    挑不出来时 ok 为假：no_full_run 还没有跑完的全量运行 / no_line 图里没有这条线 / no_index 那次全量运行没有记下每个
    测试走过的函数 / no_tests 没有测试走过这条线 / storage 读库出错。接口（开始运行之前先问一遍、给界面数个数）和
    流程的步骤都用它。局限：全量运行之后新写的测试不在名单里（平台还不知道它走哪条线）。"""
    try:
        base = store.latest_full_run(project_id)
        if base is None:
            return _cannot_select("no_full_run", "还没有跑完的全量测试运行：先运行一次全部测试，平台才知道哪些测试走过这条功能线")
        text = None if analysis_id is None else store.graph_json(analysis_id)
        if text is None:
            return _cannot_select("storage", "要用的分析不在了")
        graph = from_json(text)
        try:
            tests = tests_on_line(store, project_id, base["id"], graph, line)
        except LookupError:
            return _cannot_select("no_line", f"最新的图里没有这条功能线：{line}")
    except (StoreError, ValueError, OSError) as e:
        return _cannot_select("storage", f"读全量运行的记录出错：{type(e).__name__}")
    if tests is None:
        return _cannot_select("no_index", f"全量运行 #{base['id']} 没有记下每个测试走过哪些函数：重新运行一次全部测试")
    if not tests:
        return _cannot_select("no_tests", f"全量运行 #{base['id']} 里没有测试走过这条功能线：先给它写测试，再运行一次全部测试")
    title = next(ln.title for ln in graph.lines if (ln.sid or ln.id) == line)
    return {"ok": True, "tests": tests, "reason": "", "code": "",
            "scope": {"line": line, "line_title": title, "tests": len(tests), "base_run": base["id"]}}


@step(title="挑出走过这条功能线的测试")
def select_line_tests(store: Store, project_id: str, analysis_id: int | None, line: str, mark: dict | None = None) -> dict:
    """只跑一条功能线的测试时：按这次运行要用的分析的图挑（pick_line_tests）。"""
    with clear_mark_if_aborted(mark):
        return pick_line_tests(store, project_id, analysis_id, line)


@step(title="准备运行（插件、图的副本、记录文件）")
def prepare_run(store: Store, analysis_id: int | None, mark: dict | None = None, selected: dict | None = None) -> dict:
    """在 <数据目录>/runs/tmp-<随机>（0700）下放这次分析的图的副本（插件按它记录）和空的记录文件（0600），写好插件
    目录；selected（select_line_tests 的结果）里有名单时再放一个名单文件 only.json（插件只跑名单里的测试）。返回
    {"ok", "scratch", "graph_path", "trace_path", "plugin_dir", "analysis_id", "only_path", "scope", "reason"}；写不了时
    ok 为假，已经建的临时目录删掉。"""
    with clear_mark_if_aborted(mark):
        home = store.path.parent
        scratch = None
        try:
            text = None if analysis_id is None else store.graph_json(analysis_id)
            if text is None:
                return {"ok": False, "reason": "准备运行失败：这次运行要用的分析不在了"}
            scratch = runner.make_scratch(home)
            graph_path, trace_path = scratch / "graph.json", scratch / "trace.jsonl"
            private_file(graph_path)
            graph_path.write_text(text, encoding="utf-8")
            private_file(trace_path)
            only_path = None
            if selected is not None and selected["tests"] is not None:
                only_path = scratch / "only.json"
                private_file(only_path)
                only_path.write_text(json.dumps(selected["tests"], ensure_ascii=False), encoding="utf-8")
            plugin_dir = runner.write_plugin_dir(home)
        except (OSError, StoreError) as e:
            runner.remove_scratch(scratch)
            return {"ok": False, "reason": f"准备运行失败：{getattr(e, 'strerror', None) or e}"}
        except BaseException:
            runner.remove_scratch(scratch)
            raise
    return {"ok": True, "scratch": scratch, "graph_path": graph_path, "trace_path": trace_path,
            "plugin_dir": plugin_dir, "analysis_id": analysis_id, "only_path": only_path,
            "scope": None if selected is None else selected["scope"], "reason": ""}


@step(title="运行测试命令")
def run_test_command(store: Store, project: dict, prepared: dict, cancel: threading.Event, timeout: float | None,
                     mark: dict | None = None) -> dict:
    """在项目根目录里用 /bin/sh -c 跑测试命令（runner.run_tests：挂记录插件、自成进程组，超时、取消时结束整个进程组；
    超时默认取 PYSTEPMAP_TEST_TIMEOUT）。输出边跑边放进进程内的运行表（GET …/runs/output），只在内存里；跑着的时候运行表
    上挂着这次运行的进度（live.LiveRun：GET …/runs 的 running.progress），命令一结束就取下。服务退出时被
    中止的（runner.cancel_reason 为 shutdown）说明写"服务停止时测试还在跑，被中止了"（测试还没开始的：在运行前的重新分析
    里时写"服务停止时正在重新分析，测试还没开始"，否则"服务停止时测试还没开始"）。返回
    {"status", "exit_code", "seconds", "reason", "output", "command"（跑的命令原文，只在这次运行的内存里）}。"""
    with _clean_up_if_aborted(mark, prepared):
        home = store.path.parent
        before = (runner.running(home, project["id"]) or {}).get("phase")  # 测试开始之前在哪个阶段（服务停止时的说法用）
        runner.set_live(home, project["id"], LiveRun(prepared["trace_path"], prepared["graph_path"]))
        runner.set_phase(home, project["id"], "testing")
        live: deque[str] = deque(maxlen=runner.TAIL_LINES)
        runner.keep_output(home, project["id"], live)
        try:
            result = runner.run_tests(project["root"], project["test_command"], graph_path=prepared["graph_path"],
                                      trace_path=prepared["trace_path"], plugin_dir=prepared["plugin_dir"],
                                      timeout=runner.run_timeout() if timeout is None else timeout, cancel=cancel,
                                      output=live, scratch=prepared["scratch"], only_path=prepared.get("only_path"))
        finally:
            runner.set_live(home, project["id"], None)  # 测试命令结束了：运行中的进度不再给
        reason = result.reason
        if result.status == "cancelled" and runner.cancel_reason(home, project["id"]) == "shutdown":  # 和用户点的取消分开说
            if result.reason != runner.CANCELLED_BEFORE_START:
                reason = SHUTDOWN_NOTE
            else:
                reason = SHUTDOWN_BEFORE_START if before == "analyzing" else SHUTDOWN_PREPARING
        if reason:
            live.append(f"[pystepmap] {reason}")
        return {"status": result.status, "exit_code": result.exit_code, "seconds": result.seconds,
                "reason": reason, "output": live, "command": project["test_command"]}


@step(title="记下没跑完的运行")
def describe_unfinished_run(ran: dict, prepared: dict, mark: dict | None = None) -> dict:
    """超时、被取消、没能启动：状态 timeout / cancelled / error，没有路径，说明写原因。"""
    with _clean_up_if_aborted(mark, prepared):
        status = {"timeout": "timeout", "cancelled": "cancelled"}.get(ran["status"], "error")
        return _no_paths(status, None, _empty_summary(ran["seconds"], ran["reason"]))


@step(title="读记录并核对")
def read_and_verify(store: Store, project_id: str, prepared: dict, ran: dict, mark: dict | None = None) -> dict:
    """读记录文件、数用例和插件错误、沿这次用的图核对（verify，图过期也照样核对），再看哪些文件在运行期间变了。返回
    {"ok", "received", "graph", "report", "stale", "counts", "errors", "error_types", "problem"}：ok 为假是记录读不了
    （或核对出错），received 为假是一条记录都没收到。problem 里只有中文说明和异常的类型名；原文（插件 error 行、读不了
    的那一行，可能带记录里的内容）只进内存里的输出末尾。"""
    with _clean_up_if_aborted(mark, prepared):
        runner.set_phase(store.path.parent, project_id, "processing")
        try:
            checked = _verify_records(prepared)
        except RecordError as e:
            ran["output"].append(f"[pystepmap] 记录文件读不了：{e}")
            return {"ok": False, "received": False, "problem": "记录文件读不了（有一行格式不对，原文见运行输出）。"}
        except Exception as e:  # 引擎的缺陷等：变成一次 error 的运行，不让流程带着锁出去
            logger.exception("核对项目 %s 的执行记录时出错", project_id)
            ran["output"].append(f"[pystepmap] 核对时出错：{type(e).__name__}")
            return {"ok": False, "received": False, "problem": f"核对执行记录时出错（{type(e).__name__}）。"}
        for e in checked["errors"]:  # 插件 error 行的原文只进内存里的输出末尾
            ran["output"].append(f"[pystepmap] 记录插件报错：{e}")
        return checked


@step(title="记下记录读不了的运行")
def describe_unreadable_records(ran: dict, checked: dict, prepared: dict, mark: dict | None = None) -> dict:
    """状态 error，没有路径，说明写读不了的原因（只有类型名，原文在运行输出里）。"""
    with _clean_up_if_aborted(mark, prepared):
        return _no_paths("error", ran["exit_code"], _empty_summary(ran["seconds"], checked["problem"]))


@step(title="记下没收到记录的运行")
def describe_run_without_records(ran: dict, checked: dict, prepared: dict, mark: dict | None = None) -> dict:
    """一条记录都没收到：状态 error，没有路径；说明先说退出码的意思，命令里覆盖了 PYTHONPATH 时说是它挡住了记录插件，
    再说别的可能，最后"原文见运行输出"；verify 照带（插件报错的个数和类型），ok 为 null（什么都没核对）。"""
    with _clean_up_if_aborted(mark, prepared):
        none_found = _NONE_FOUND if prepared.get("scope") and ran["exit_code"] == 5 else ""
        note = (f"{exit_text(ran['exit_code'])}，没有收到任何记录（执行记录和测试结果都没有）。{none_found}"
                f"{pythonpath_hint(ran.get('command') or '')}{_MAYBE}{_plugin_note(checked)}原文见运行输出。")
        summary = {**_empty_summary(ran["seconds"], note), "verify": _verify_summary(checked)}
        return _no_paths("error", ran["exit_code"], summary)


@step(title="汇总运行路径")
def summarize_run(prepared: dict, ran: dict, checked: dict, mark: dict | None = None) -> dict:
    """把记录汇总成运行路径（build_run_paths），定这次的状态（有失败的测试 tests_failed；命令退出码 0 ok；否则
    error），算覆盖、整理不符的前几条、写中文说明。返回 {"status", "exit_code", "summary", "tests", "paths",
    "function_index"}。出错时状态 error、没有路径，说明里只有异常的类型名。"""
    with _clean_up_if_aborted(mark, prepared):
        try:
            return _summarize(prepared, ran, checked)
        except Exception as e:
            logger.exception("汇总运行路径时出错")
            ran["output"].append(f"[pystepmap] 汇总运行路径时出错：{type(e).__name__}")
            return _no_paths("error", ran["exit_code"],
                             _empty_summary(ran["seconds"], f"汇总运行路径时出错（{type(e).__name__}）。"))


@step(title="清理这次运行的临时文件")
def remove_run_files(prepared: dict) -> None:
    runner.remove_scratch(prepared["scratch"])


@step(title="保存这次运行")
def save_run(store: Store, project: dict, prepared: dict, record: dict, mark: dict | None = None) -> int:
    """连同用的哪次分析、开始和结束的时间、命令原文一起存。项目不存在了（运行期间被删）时抛 ProjectMissingError，
    写不进去时抛 StoreError。"""
    with clear_mark_if_aborted(mark):
        return store.add_run(project["id"], NewRun(
            analysis_id=prepared["analysis_id"], started_at=mark["started_at"], finished_at=_now(),
            command=project["test_command"], status=record["status"], exit_code=record["exit_code"],
            summary=record["summary"], tests=record["tests"], paths=record["paths"],
            function_index=record["function_index"], scope=prepared.get("scope")))


@step(title="清理旧的运行（全量、单条线各留最近 5 次）")  # 标题要写成字面量；改 KEEP_RUNS 时一起改
def prune_old_runs(store: Store, project_id: str, mark: dict | None = None) -> int:
    """清理失败不影响这次运行的结果：记下来，下次再清。"""
    with clear_mark_if_aborted(mark):
        try:
            return store.prune_runs(project_id, keep=KEEP_RUNS)
        except StoreError:
            logger.exception("清理项目 %s 的旧运行失败", project_id)
            return 0


# ---------- 判断：只看步骤的结果 ----------

@condition(title="没有填测试命令？")
def lacks_test_command(found: dict) -> bool:
    return not found["project"]["test_command"]


@condition(title="测试命令还没确认过？")
def command_is_unconfirmed(found: dict) -> bool:
    return not found["project"]["test_confirmed"]


@condition(title="这个项目正在分析或运行测试？")
def project_is_busy(mark: dict) -> bool:
    return not mark["marked"]


@condition(title="只跑一条功能线的测试？")
def scoped_to_line(line: str | None) -> bool:
    return line is not None


@condition(title="挑不出这条功能线的测试？")
def selection_failed(selected: dict) -> bool:
    return not selected["ok"]


@condition(title="图过期了？")
def graph_is_outdated(fresh: dict) -> bool:
    return fresh["stale"]


@condition(title="准备失败？")
def preparation_failed(prepared: dict) -> bool:
    return not prepared["ok"]


@condition(title="测试命令没有跑完？")
def command_did_not_finish(ran: dict) -> bool:
    return ran["status"] != "finished"


@condition(title="记录读不了？")
def records_unreadable(checked: dict) -> bool:
    return not checked["ok"]


@condition(title="一条记录都没收到？")
def nothing_recorded(checked: dict) -> bool:
    return not checked["received"]


# ---------- 流程 ----------

@workflow(name="run_project_tests", title="运行测试并核对", group="项目管理")
def run_project_tests(store: Store, project_id: str, *, cancel: threading.Event, timeout: float | None = None,
                      analyzer: Analyzer | None = None, line: str | None = None) -> dict:
    """取项目 → 有测试命令、确认过 → 检查目录 → 标记正在忙（和分析同一把锁，一直拿到存好；阶段 preparing）→ 图过期
    就先重新分析并保存（阶段 analyzing）→ 准备临时文件 → 在项目根目录里跑测试命令（挂记录插件）→ 读记录、核对（读不了、一条记录都没收到时各自记下）
    → 汇总运行路径 → 删临时文件 → 存这次运行、只留最近 5 次 → 取消标记。命令没跑完（超时、取消、没能启动）时照样存
    一条没有路径的运行。cancel：置位时结束测试命令；timeout：秒，None 时取 PYSTEPMAP_TEST_TIMEOUT（默认 1800）；
    analyzer：测试注入的假引擎。line：给出时（功能线的 sid）只跑走过这条线的测试——名单取自最近一次跑完的全量运行
    （select_line_tests），命令不改，由记录插件按名单挑；存下的运行带着范围（scope）。"""
    found = load_project(store, project_id)
    if project_is_missing(found):
        return outcome("failed", title="项目不存在", reason=found["reason"], code="not_found")
    if lacks_test_command(found):
        return outcome("failed", title="没有填测试命令，没有运行", reason="还没有填测试命令：先在项目的测试命令里填一条",
                       code="no_command")
    if command_is_unconfirmed(found):
        return outcome("failed", title="测试命令还没确认过，没有运行", reason="第一次运行这条测试命令之前要先确认它的影响",
                       code="confirm")
    directory = check_project_directory(found["project"]["root"])
    if directory_is_unusable(directory):
        return outcome("failed", title="项目目录不可用，没有运行", reason=directory["reason"], code="invalid")
    mark = mark_busy(store, project_id)
    if project_is_busy(mark):
        return outcome("failed", title="正在分析或运行测试，这次没有运行", reason="这个项目正在分析或运行测试，等它结束再运行",
                       code="busy")
    fresh = check_graph_freshness(store, found["project"], mark)
    analysis_id = fresh["analysis_id"]
    if graph_is_outdated(fresh):
        note_reanalysis(store, project_id, mark)
        result = run_analysis(store, found["project"]["root"], found["project"]["config"], analyzer, mark)
        if analysis_failed(result):
            unmark_busy(mark)
            return outcome("failed", title="运行前的分析失败，没有运行测试", reason=result["reason"], code=result["code"])
        compared = compare_with_previous(store, project_id, result, mark)
        try:
            analysis_id = save_analysis(store, project_id, result, found["project"]["config"], compared, mark)
        except ProjectMissingError as e:
            unmark_busy(mark)
            return outcome("failed", title="项目在运行前的分析期间被删除了", reason=str(e), code="not_found")
        except StoreError as e:
            unmark_busy(mark)
            return outcome("failed", title="保存运行前的分析失败，没有运行测试", reason=str(e), code="storage")
        prune_old_analyses(store, project_id, mark)
    selected = ALL_TESTS
    if scoped_to_line(line):
        selected = select_line_tests(store, project_id, analysis_id, line, mark)
        if selection_failed(selected):
            unmark_busy(mark)
            return outcome("failed", title="挑不出这条功能线的测试，没有运行", reason=selected["reason"],
                           code=selected["code"])
    prepared = prepare_run(store, analysis_id, mark, selected)
    if preparation_failed(prepared):
        unmark_busy(mark)
        return outcome("failed", title="准备运行失败，没有运行测试", reason=prepared["reason"], code="prepare")
    ran = run_test_command(store, found["project"], prepared, cancel, timeout, mark)
    if command_did_not_finish(ran):
        record = describe_unfinished_run(ran, prepared, mark)
    else:
        checked = read_and_verify(store, project_id, prepared, ran, mark)
        if records_unreadable(checked):
            record = describe_unreadable_records(ran, checked, prepared, mark)
        elif nothing_recorded(checked):
            record = describe_run_without_records(ran, checked, prepared, mark)
        else:
            record = summarize_run(prepared, ran, checked, mark)
    remove_run_files(prepared)
    try:
        run_id = save_run(store, found["project"], prepared, record, mark)
    except ProjectMissingError as e:
        unmark_busy(mark)
        return outcome("failed", title="项目在运行测试期间被删除了", reason=str(e), code="not_found")
    except StoreError as e:
        unmark_busy(mark)
        return outcome("failed", title="保存运行结果失败", reason=str(e), code="storage")
    prune_old_runs(store, project_id, mark)
    unmark_busy(mark)
    if command_did_not_finish(ran):
        return outcome("failed", title="测试没有跑完，已记下这次运行", reason=ran["reason"], code=ran["status"],
                       run_id=run_id)
    return outcome("ok", title="测试已运行并核对", run_id=run_id, status=record["status"])
