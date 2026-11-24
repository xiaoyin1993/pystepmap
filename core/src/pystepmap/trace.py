"""pytest 执行记录插件：被测项目的测试运行时，记下图里每个函数每次执行实际经过的行、离开方式和它（直接、经项目以外的
帧间接）调用的项目内函数。

用法：PYSTEPMAP_GRAPH=g.json PYSTEPMAP_TRACE_OUT=t.jsonl python -m pytest -p pystepmap.trace …
两个环境变量都设置时才工作，否则什么都不做；工作时 pytest 的汇总里有一行：记了几个函数、写到哪个文件，
或者让插件停用的错误。输出每行一条 JSON，有六种：
- 执行记录：
  {"fn": key, "test": node id, "lines": [...], "ns": [...], "exit": "return"|"raise", "exc": 类型名|null,
   "calls": [[被调函数 key, 行号], ...], "icalls": [[被调函数 key, 行号], ...], "n": 次数}
  （icalls 只在非空时才有）
- 测试结果（每个测试结束时一条，跟在这个测试的执行记录之后）：
  {"test": node id, "outcome": "passed"|"failed"|"skipped"|"error", "seconds": 秒, "n": 1, "exc": 异常类型名}
  （exc 只在失败 / 出错、并且知道是哪个异常时才有）
- 插件错误：{"error": "类型: 信息"}
- 挑选（只在给了 PYSTEPMAP_ONLY_TESTS 时）：{"only": 名单里的个数, "kept": 名单里找到了的个数}，见
  pytest_collection_modifyitems；核对、运行路径都不看。
- 进度（平台在测试跑着的时候看进度用，pystepmap.progress 读；核对、运行路径都不看）：收集完一行 {"collected": 用例数}
  （pytest_collection_finish，挑选之后的；pytest-xdist 的每个工作进程各写一行，读的时候取最大的）；每个测试（参数化
  的每个用例）开始时一行 {"start": node id}（pytest_runtest_logstart，同样去掉参数化后缀）。只有这一个字段。xdist 的
  主进程不写（它不收集、不跑测试，转来的 logstart 由真正跑测试的工作进程自己写过了）。每个测试多一次整块追加。
文件的第一行是格式和版本 {"v": 1, "format": "pystepmap-trace"}（文件是空的时候才写；几个进程同时开始时可能有几行）。
格式只加不改：读的时候不认识的字段忽略，没有 fn 的行不是执行记录（核对跳过）；没有 ns 的执行记录（这个字段之前的
插件写的）就是没有耗时。阶段 6 之前的 pystepmap verify 读不了带测试结果行的文件（它把没有 fn 的行当成坏行），要用
同一版本的 pystepmap 读（进度行之前的阶段 6 版本读得了带进度行的文件：没有 fn、error 又不是测试结果的行跳过）。
test 是 pytest 的 node id，去掉参数化的 [参数] 后缀（参数 id 里可能有取值，也可能有 ::、[、]）：第一个 :: 之后的
第一个 [ 起都去掉——类名、函数名里不会有 [，目录名可以有，但在第一个 :: 之前。测试之外的执行 test 为 ""。
calls 的行号是被调函数的帧开始那一刻调用方帧所在的行（只读帧对象的 f_lineno；读不到时为 0），按 (key, 行号) 去重。
icalls 是间接调用：目标函数的帧开始时，它的直接调用方不是正在执行的目标函数（是项目以外的帧：第三方库的包装函数如
@DBOS.step()、contextlib.contextmanager 的 __enter__、库调用的回调、lambda、生成器表达式），就从调用方往上最多再找
64 帧（_INDIRECT_DEPTH），找到最近的一个正在执行的目标函数，记在它的 icalls 里：[被调函数 key, 那一帧当时所在的行]，
同样按 (key, 行号) 去重、参与合并。由项目以外的帧开始的生成器（contextmanager 的 __enter__、生成器表达式迭代的）也这样
记给栈上最近的目标函数——它就是驱动这个生成器的那一个。只读帧对象的 f_back、f_lineno，不读局部变量。找不到就不记：
事件循环调度的协程和任务（asyncio.gather、create_task 开始的）、别的线程里执行的函数，栈上没有调用它的目标函数；
直接调用方就是目标函数、只是不记进 calls 的（目标函数直接迭代的生成器、await 一个已有的协程对象，见下）也不记 icalls。
没有正在执行的目标函数时不往上找。有的时候就要找——不只是流程在执行时，只要有一个目标函数的生成器 / 协程挂起着
（fixture 里用的 contextmanager、应用的 lifespan）、或者别的线程里有目标函数在跑，每个由项目以外的帧（测试本身、框架）
开始的目标函数都往上找：经一层包装函数找一帧就到；找不到时一直找到栈底或找满 64 帧——pytest 里平常的栈深约多 1.4 微秒
一次，找满 64 帧约多 2.5 微秒（人造的 10 万次找满的调用比阶段 6 之前的插件慢 4 倍；真实项目上一次运行六十多万次
函数执行，就算每次都多 2.5 微秒也只多 1.7 秒左右）。其余负载不变。核对（verify）只看 calls，不看 icalls。
ns 和 lines 等长，非负整数（纳秒）。只在"计时点"读时钟：这次执行的第一个行事件，以及落在图上节点头部的行（节点的
line 到 end_line）上的行事件。计时点的 ns 是从它到这一帧的下一个计时点（或返回、异常离开、截断后的第一个行事件）
经过的时间，包括其间调用的函数的时间；不是计时点的行事件 ns 为 0（它的时间算在前面最近的计时点上）。所以从节点 A
的第一个行事件起、到下一个节点的第一个行事件之前，这些行事件的 ns 之和就是这一段的耗时（A 之后不出节点的语句算在
A 上，函数开头到第一个节点之前的算在开始节点上）。协程、生成器在 await / yield 挂起的时间算在挂起它的那一行所在的
一段上。（为什么不是每个行事件都读时钟：读一次时钟约 30 纳秒，和记一个行事件本身差不多，行事件密集、几乎不出节点的
循环会比不计时的插件慢 1.6 倍；只在计时点读、相同执行攒一批再相加之后，人造的行事件密集的循环不超过 1.45 倍，
转账示例约 1.25 倍。）
每个测试结束时写出这段时间里结束的执行，其中 fn、test、lines、exit、exc、calls、icalls 完全相同的合并成一条，n 是次数，
ns 逐项相加；跨过测试结束才结束的执行（协程、线程）记在下一次写出里，所以同一身份可能出现在多条记录里，读的时候
按 n 累加。
一次执行最多记 20000 个行号（环境变量 PYSTEPMAP_TRACE_MAX_LINES 可改，正整数）：超出后不再追加，
记录多一个 "truncated": true，lines 和 ns 只有前 N 个，exit / exc / calls / icalls 仍是完整、真实的；截断标记也参与合并。
测试结果：同一个测试（参数化的每个用例）一条，test 同样去掉参数化后缀，所以参数化的几个用例 test 相同、各一条
（读的时候按 test 汇总）。outcome 由这个测试的各份报告按出现顺序合并，只会变坏（failed > error > passed > skipped）：
call 阶段的报告（含 unittest subTest、pytest subtests 的子测试报告，xfail 是 skipped）给出 passed / failed / skipped，
setup、teardown 失败为 error（已经 failed 的不变），setup 跳过为 skipped——所以有一个子测试失败就是 failed，
skipped 只在什么都没跑时。seconds 是 setup、call、teardown 的时长之和（子测试的时长已含在 call 里，不再加）。
exc 是让这个测试失败的异常的**类型名**（如 AssertionError、ValueError、pytest.fail 的 Failed；动态创建的异常类就是
它的类名）：第一份 outcome 为 failed 的报告（setup、call、teardown 或子测试）生成时 call.excinfo 里异常的类型名——
setup / teardown 出错时就是那里抛的。从 pytest_runtest_makereport 读（普通的钩子实现，只读 excinfo 的类型、总是
返回 None，不替 pytest 生成报告、不改结果；别的插件抢先生成了报告、钩子没被调用时就没有 exc）。通过、跳过（含
xfail）的没有 exc。
只记这几项：不记失败信息、异常信息和 repr、断言展开、捕获的输出、参数 id、子测试的 msg 和参数。pytest-xdist 的
主进程转来的报告（带 node 属性）不记，由真正跑测试的工作进程记。

插件自己第一次见到一个函数时的慢活（按文件和限定名找图里的函数、反汇编调用方的字节码）不算进任何一段：做完后把
这条调用栈上每个正在计时的帧的上一个时刻往后挪这么久。

插件跑在别人的测试里，所以：只依赖标准库、不导入 pystepmap 的其他模块（也不导入 pytest：钩子按名字被 pytest 找到）；
不读帧的局部变量，不记录参数、返回值和异常信息（只记异常的类型名）；
自身出任何异常都不让被测测试失败——写一行 {"error": "类型: 信息"} 后本次运行停用，之后不再写执行记录和测试结果。
同一个进程里以不同的模块名挂上了两份（项目自己的 -p pystepmap.trace 和平台改了名字的副本）时，只有先启用的那一份记录，
另一份不启用、在 pytest 的汇总里说一句（标记在 sys 上，见 _ACTIVE）。没有这个记号的旧版插件：它先启用时（占着名叫
pystepmap 的 sys.monitoring 工具编号）这一份不启用；这一份先启用、它后启用时两份都在记，这一份在记录文件里写一行
{"error": "SecondPlugin: …"}、在汇总里说一句，读的一方据此提醒数字可能重复。

实现用 Python 3.12 的 sys.monitoring（不占 sys.settrace，与覆盖率、调试器共存，且覆盖所有线程，包括已经在跑的线程）：
- PY_START 全局开启：非目标函数第一次进入就返回 DISABLE，此后零开销；目标函数的代码对象单独开 LINE 和 PY_RETURN。
- PY_UNWIND（异常离开帧）只能全局开启，直接给出"异常离开"，不必从最后一个事件推断。
- 协程、生成器在 await / yield 挂起时不产生 PY_RETURN，一条记录自然跨越挂起；被取消的协程以 PY_UNWIND(CancelledError) 结束。
- 生成器、异步生成器的帧在第一次被迭代时才开始，迭代它的往往不是创建它的函数，所以不记进任何人的 calls。
- 协程的帧在第一次被 await 时才开始，开始它的帧不一定是创建它的帧。只有一种情况不记进调用方的 calls：await 的对象是
  从变量、闭包变量、全局、属性或下标里读出来的已有对象（await coro、await self.task、await d[k]）——协程先存起来再
  await、或当参数传给别的函数去 await，创建它的不在这里。其余一律照记（await f(...)、关键字调用、async with、async for、
  海象赋值等），字节码里出现没见过的写法时宁可多记、在核对里显出来，也不悄悄丢掉。被调函数自己的记录总是照常。
  区分的办法：调用方此刻停在 await 的 SEND 上，从 SEND 往回跳过各版本夹在中间的指令（LOAD_CONST、PUSH_NULL、
  LOAD_COMMON_CONSTANT 等），看得到被 await 对象的那条指令；中间有跳转落点（条件表达式两支汇合）时照记。
  注意"得到它的是一次调用"不等于"在这里创建"：同步函数返回别处创建的协程（def run(x): return _impl(x)，再 await run(1)）
  会记成调用方调用了 _impl。
"""

from __future__ import annotations

import atexit
import dis
import json
import os
import sys
import threading
import time
from contextlib import suppress
from inspect import CO_ASYNC_GENERATOR, CO_COROUTINE, CO_GENERATOR
from operator import add, sub
from types import CodeType, FrameType

_MON = sys.monitoring
_EV = _MON.events
_TOOL_IDS = (4, 3)  # 0/1/2/5 是调试器、覆盖率、性能分析、优化器的约定编号
_TOOL_NAME = "pystepmap"
_GLOBAL_EVENTS = _EV.PY_START | _EV.PY_UNWIND
_LOCAL_EVENTS = _EV.LINE | _EV.PY_RETURN
_SELF = os.path.realpath(__file__)  # 分析 pystepmap 自己时本文件也在图里，但插件不记录自己
_DEFAULT_MAX_LINES = 20000
_GENERATOR_FLAGS = CO_GENERATOR | CO_ASYNC_GENERATOR
_HEADER = {"v": 1, "format": "pystepmap-trace"}  # 记录文件的第一行
_ONLY_ENV = "PYSTEPMAP_ONLY_TESTS"  # 只跑名单里的测试：名单文件（JSON，测试 id 的列表）的路径
_INDIRECT_DEPTH = 64  # 间接调用：直接调用方之上最多再找几帧
# 相同执行的时刻攒一批再一起逐项相加，比每次执行都加一遍省：攒到 64 次、或者攒下的整数到 4096 个时加一次
_MERGE_RUNS, _MERGE_INTS = 64, 4096
_OUTCOMES = frozenset({"passed", "failed", "skipped"})  # pytest 报告的 outcome（别的插件可能加别的，如 rerun：不改结果）
_WORSE = {"": 0, "skipped": 1, "passed": 2, "error": 3, "failed": 4}  # 一个测试的结果只会往大的变
# 计时用的时钟放在列表里：freezegun 之类冻结时间的库会把各模块里指向 time.perf_counter_ns 的属性换成假的，放在列表里
# 它找不到；测试也可以换掉它
_CLOCK = [time.perf_counter_ns]
# 读出一个已有对象的指令：await 的对象由它们得到时，协程不是在这里创建的。含 3.13 起后半是读局部变量的合并指令
# （LOAD_FAST_LOAD_FAST、STORE_FAST_LOAD_FAST——同一行先存后读，如 [await c for c in coros]、c = make(); await c——
# 以及 3.14 起的借用变体）；后半不是读取的合并指令（STORE_FAST_STORE_FAST）不在其中
_LOADS = frozenset({"LOAD_FAST", "LOAD_FAST_CHECK", "LOAD_FAST_AND_CLEAR", "LOAD_FAST_LOAD_FAST", "STORE_FAST_LOAD_FAST",
                    "LOAD_FAST_BORROW", "LOAD_FAST_BORROW_LOAD_FAST_BORROW", "LOAD_DEREF", "LOAD_FROM_DICT_OR_DEREF",
                    "LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS", "LOAD_ATTR", "LOAD_SUPER_ATTR",
                    "BINARY_SUBSCR"})
# 得到被 await 对象的指令与 SEND 之间、各版本可能夹着的指令
_FILLERS = frozenset({"GET_AWAITABLE", "LOAD_CONST", "LOAD_COMMON_CONSTANT", "PUSH_NULL", "EXTENDED_ARG", "NOP",
                      "CACHE", "RESUME", "RESUME_CHECK"})


class _Run:
    """一次执行（一帧）的进行中记录。"""

    __slots__ = ("calls", "icalls", "key", "lines", "marks", "test", "truncated", "ts")

    def __init__(self, key: str, test: str, marks: frozenset[int]) -> None:
        self.key = key
        self.test = test
        self.marks = marks  # 这个函数的节点头部所在的行：落在上面的行事件是计时点
        self.lines: list[int] = []
        # 每个计时点的时刻，再加一个结束时刻（截断后的第一个行事件、或者帧结束时）：相邻两项之差是前一个计时点的 ns。
        # 行事件里只追加时刻（最热的路径上少做事）；相同的执行合并时时刻逐项相加（差之和等于和之差），写出时才算差
        self.ts: list[int] = []
        self.calls: dict[tuple[str, int], None] = {}  # (被调函数 key, 调用方所在行)，保持首次出现的顺序
        self.icalls: dict[tuple[str, int], None] = {}  # 间接调用：(被调函数 key, 这一帧所在行)，同上
        self.truncated = False


_tool: int | None = None  # 占用的 sys.monitoring 工具编号；None 表示没在记录
_out: str | None = None  # 输出文件（绝对路径）；图只在 start() 里读一次，不用固定
_by_name: dict[tuple[str, str], list[tuple[int, str]]] = {}  # (文件真实路径, 限定名) -> [(def 行, key)]，按行排序
_marks: dict[str, frozenset[int]] = {}  # 函数 key -> 节点头部所在的行（计时点）
_real: dict[str, str] = {}  # co_filename -> 真实路径
_targets: dict[int, tuple[CodeType, str, frozenset[int]]] = {}  # id(代码对象) -> (代码对象, key, 计时点的行)；持有引用，id 不会被复用
_live: dict[int, _Run] = {}  # id(帧) -> 进行中的记录
_awaits: dict[int, tuple[CodeType, frozenset[int]]] = {}  # id(调用方代码对象) -> (代码对象, await 已有对象的 SEND 所占的字节偏移)
_buf: dict[tuple, list] = {}  # 已完成的执行 -> [次数, 逐项相加的时刻, 还没加进去的时刻列表]，等测试结束时写出
_results: dict[str, list] = {}  # 正在结束的测试（pytest 的 node id，含参数后缀）-> [outcome, 秒, 异常类型名]，测试结束时写出
_pending_exc: dict[str, str] = {}  # node id -> 刚生成、还没交给 logreport 的那份报告的异常类型名（makereport 记下）
_lock = threading.Lock()
_test = ""  # 正在运行的测试的 node id
_max_lines = _DEFAULT_MAX_LINES  # 一次执行最多记多少个行号
_process_hooks = False
_recorded: set[str] = set()  # 这次记录里有执行记录的函数 key（pytest 汇总里报个数）
_error: str | None = None  # 让这次记录停用的第一个错误
_session = False  # 这次 pytest 运行由环境变量启用了插件（汇总时才说话）
# 同一个进程里只让一份插件记录：插件可能以不同的模块名被挂上两次（项目自己的 -p pystepmap.trace，平台的数据目录里改了
# 名字的副本 pystepmap_trace_plugin），两份都会去占 sys.monitoring、往同一个文件里写。先启用的那一份把自己的模块名记在
# sys 上，后来的看到了就不启用（只在 pytest 的汇总里说一句）。只管 pytest 钩子；直接调用 start() 不受影响
_ACTIVE = "_pystepmap_trace_active"
_skipped: str | None = None  # 这次 pytest 运行里已经在记录的另一份插件（模块名，或它占的工具编号）
_second: int | None = None  # 自己启用之后又有一份不看记号的旧版插件启用了：它占的工具编号
_relay = False  # 这个进程是 pytest-xdist 的主进程（只转工作进程的报告）：不写进度行


# ---- 对外接口 ----

def start(graph_path: str, out_path: str) -> None:
    """读入图，开始记录。图读不了、PYSTEPMAP_TRACE_MAX_LINES 不合法或启用失败时写一行 error，保持不工作；不抛异常。"""
    global _tool, _out, _by_name, _marks, _max_lines, _error
    stop()
    _out = os.path.abspath(out_path)  # 每次写出都会重新打开：固定成绝对路径，被测测试 chdir 也写进同一个文件
    _real.clear()
    _recorded.clear()
    _error = None
    try:
        _write_header()
        _max_lines = _max_lines_from_env()
        _by_name, _marks = _load_graph(graph_path)
        _tool = _claim_tool()
        for event, callback in ((_EV.PY_START, _on_start), (_EV.LINE, _on_line),
                                (_EV.PY_RETURN, _on_return), (_EV.PY_UNWIND, _on_unwind)):
            _MON.register_callback(_tool, event, callback)
        _MON.restart_events()  # 上一次记录里被 DISABLE 的位置重新生效（目标函数可能变了）
        _MON.set_events(_tool, _GLOBAL_EVENTS)
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _release()
        _write_error(e)
        return
    _install_process_hooks()


def stop() -> None:
    """停止记录并写出缓冲；还没结束的执行丢弃。不抛异常。"""
    _release()
    try:
        _flush()
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _write_error(e)
    _reset_runs()


# ---- pytest 钩子 ----

def pytest_configure(config) -> None:
    global _session, _skipped
    graph, out = os.environ.get("PYSTEPMAP_GRAPH"), os.environ.get("PYSTEPMAP_TRACE_OUT")
    if graph and out:
        owner = getattr(sys, _ACTIVE, None)
        if owner is not None and owner != __name__:  # 另一份插件已经在记录这次运行
            _skipped = owner
            return
        taken = _other_pystepmap_tool()
        if taken is not None:  # 不看记号的旧版插件已经占着名叫 pystepmap 的工具编号
            _skipped = f"另一份 pystepmap 记录插件（sys.monitoring 工具编号 {taken}）"
            return
        setattr(sys, _ACTIVE, __name__)
        _session = True
        start(graph, out)


def pytest_sessionstart(session) -> None:
    """所有插件都启用过了：看这个进程是不是 pytest-xdist 的主进程（注册了 dsession、自己不是工作进程：不写进度行）；
    看有没有一份不看记号的旧版插件在自己之后也启用了（占着另一个名叫 pystepmap 的工具编号）。有就在记录文件里写一行
    {"error": "SecondPlugin: …"}（读的一方据此提醒数字可能重复），汇总里也说一句。"""
    global _second, _relay
    if not _session or _tool is None:
        return
    try:
        config = session.config
        _relay = getattr(config, "workerinput", None) is None and bool(config.pluginmanager.has_plugin("dsession"))
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)
        return
    _second = _other_pystepmap_tool()
    if _second is not None:
        with suppress(Exception):
            _append(json.dumps({"error": f"SecondPlugin: 另一份 pystepmap 记录插件也在记录这次运行（sys.monitoring 工具编号 "
                                         f"{_second}），记录可能重复"}, ensure_ascii=False) + "\n")


def _other_pystepmap_tool() -> int | None:
    """以 pystepmap 这个名字占着 sys.monitoring 工具编号的另一份插件（不是自己）占的编号；没有时 None。"""
    for tool in range(6):  # sys.monitoring 的工具编号是 0～5
        if tool == _tool:
            continue
        with suppress(Exception):
            if _MON.get_tool(tool) == _TOOL_NAME:
                return tool
    return None


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """pytest 的汇总里说一行：记了几个函数、写到哪个文件，或者让插件停用的错误；没启用（另一份插件在记录）时说一句。"""
    if _skipped is not None:
        terminalreporter.write_line(f"pystepmap 记录插件 {__name__}：这次运行已经由 {_skipped} 在记录，不再重复记录")
        return
    if not _session:
        return
    if _second is not None:
        terminalreporter.write_line(f"pystepmap 记录插件 {__name__}：另一份 pystepmap 记录插件（sys.monitoring 工具编号 "
                                    f"{_second}）也在记录这次运行，记录可能重复")
    if _error is not None:
        terminalreporter.write_line(f"pystepmap 记录插件已停用：{_error}（记录文件 {_out}）")
    else:
        terminalreporter.write_line(f"pystepmap 记录：{len(_recorded)} 个函数，写入 {_out}")


def pytest_unconfigure(config) -> None:
    global _session, _skipped, _second, _relay
    stop()
    if _session and getattr(sys, _ACTIVE, None) == __name__:
        delattr(sys, _ACTIVE)
    _session, _skipped, _second, _relay = False, None, None, False


def pytest_collection_modifyitems(session, config, items) -> None:
    """只跑名单里的测试：环境变量 PYSTEPMAP_ONLY_TESTS 指着一个 JSON 文件（测试 id 的列表，id 同记录里的 test：去掉
    参数化后缀的 node id）时，收集到的用例里只留 id 在名单里的（参数化的几个用例一起留），其余按 pytest 的"取消选择"
    处理（汇总里算 deselected）。不管记录有没有启用都照做——名单是平台给的"只跑这条功能线的测试"，挑不了就不能跑：
    名单文件读不了、不是字符串的列表时以 pytest 的用法错误结束（退出码 4），一个测试都不跑。挑完写一行进度
    {"only": 名单里的个数, "kept": 名单里找到了的个数}（记录启用时）。"""
    path = os.environ.get(_ONLY_ENV)
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            wanted = json.load(f)
        if not isinstance(wanted, list) or not all(isinstance(t, str) for t in wanted):
            raise ValueError("不是测试 id 的列表")
    except Exception as e:  # noqa: BLE001 —— 挑不了就不跑：宁可这次运行失败，也不把全部测试跑一遍
        import pytest

        raise pytest.UsageError(f"pystepmap：读不了要跑的测试名单 {path}（{type(e).__name__}），没有运行任何测试") from None
    names = set(wanted)
    kept = [it for it in items if _test_id(it.nodeid) in names]
    if len(kept) != len(items):
        dropped = [it for it in items if _test_id(it.nodeid) not in names]
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
    if _tool is None or _relay:
        return
    try:
        _append(json.dumps({"only": len(names), "kept": len({_test_id(it.nodeid) for it in kept})}) + "\n")
    except Exception as e:  # noqa: BLE001 —— 写进度出错只停用记录，不影响挑选
        _fail(e)


def pytest_collection_finish(session) -> None:
    """收集完（挑选之后）：写一行 {"collected": 用例数}，运行中看进度用。"""
    if _tool is None or _relay:
        return
    try:
        _append(json.dumps({"collected": len(session.items)}) + "\n")
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)


def pytest_runtest_logstart(nodeid, location) -> None:
    """一个测试开始：记下它的 id（执行记录归它），写一行 {"start": 测试 id}（运行中看进度用）。"""
    global _test
    _test = _test_id(nodeid)
    if _tool is None or _relay:
        return
    try:
        _append(json.dumps({"start": _test}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)


def pytest_runtest_makereport(item, call) -> None:
    """报告生成之前：只记下 call.excinfo 里异常的类型名，给紧接着的 logreport 用。总是返回 None——不替 pytest 生成
    报告（这是 firstresult 钩子，返回 None 就交给下一个实现），也不碰报告。"""
    if _tool is None:
        return
    try:
        exc = call.excinfo
        if exc is None:
            _pending_exc.pop(item.nodeid, None)
        else:
            _pending_exc[item.nodeid] = type(exc.value).__name__
    except Exception as e:  # noqa: BLE001 —— 同其他钩子：插件出错只停用自己，不让测试失败
        _fail(e)


def pytest_runtest_logreport(report) -> None:
    """测试的 setup / call / teardown 各报告一次：记下结果和时长（不碰报告里的失败信息）。"""
    if _tool is None:
        return
    try:
        if getattr(report, "node", None) is not None:  # pytest-xdist 的主进程转来的工作进程报告：由工作进程自己记
            return
        sub = type(report).__name__.lower() == "subtestreport"  # pytest 9 的 SubtestReport、pytest-subtests 的 SubTestReport
        exc = _pending_exc.pop(report.nodeid, None)
        _note_result(report.nodeid, report.when, report.outcome, report.duration, sub, exc)
    except Exception as e:  # noqa: BLE001 —— 同其他钩子：插件出错只停用自己，不让测试失败
        _fail(e)


def pytest_runtest_logfinish(nodeid, location) -> None:
    global _test
    _test = ""
    if _tool is not None:
        try:
            result = _results.pop(nodeid, None)
            _flush(None if result is None else _result_line(nodeid, result))
        except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
            _fail(e)


# ---- sys.monitoring 回调：每个都兜住异常，出错就停用 ----

def _on_start(code: CodeType, offset: int):
    try:
        hit = _targets.get(id(code))
        if hit is None:  # 第一次见到这个代码对象：慢活，做完不算进调用栈上正在计时的段
            since = _CLOCK[0]()
            key = _match(code)
            if key is None:
                _discount(sys._getframe(1).f_back, since)
                return _MON.DISABLE
            hit = _targets[id(code)] = (code, key, _marks.get(key, frozenset()))
            _MON.set_local_events(_tool, code, _LOCAL_EVENTS)
            _discount(sys._getframe(1).f_back, since)
        key = hit[1]
        frame = sys._getframe(1)
        caller = frame.f_back
        if caller is not None:
            parent = _live.get(id(caller))
            if parent is not None:  # 直接调用方是正在执行的目标函数
                if not (code.co_flags & _GENERATOR_FLAGS
                        or code.co_flags & CO_COROUTINE and _awaits_existing_object(caller)):
                    parent.calls.setdefault((key, caller.f_lineno or 0), None)
            elif _live:  # 经项目以外的帧调用：往上找最近的、正在执行的目标函数（没有正在执行的目标函数时不找）
                get = _live.get
                up = caller.f_back
                left = _INDIRECT_DEPTH
                while up is not None and left:
                    parent = get(id(up))
                    if parent is not None:
                        parent.icalls.setdefault((key, up.f_lineno or 0), None)
                        break
                    up = up.f_back
                    left -= 1
        _live[id(frame)] = _Run(key, _test, hit[2])
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)
    return None


def _on_line(code: CodeType, line: int) -> None:
    # 最热的路径（每个行事件一次）：不调方法、截断之后不读时钟。时钟单调，差值不会是负数
    try:
        run = _live.get(id(sys._getframe(1)))
        if run is not None:
            lines = run.lines
            if len(lines) < _max_lines:
                if line in run.marks or not lines:  # 计时点
                    run.ts.append(_CLOCK[0]())
                lines.append(line)
            elif not run.truncated:  # 截断后的第一个行事件：它的时刻是记下的最后一段的结束
                run.ts.append(_CLOCK[0]())
                run.truncated = True
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)


def _on_return(code: CodeType, offset: int, retval: object) -> None:  # retval 不读
    try:
        run = _live.pop(id(sys._getframe(1)), None)
        if run is not None:
            if not run.truncated:  # 截断时已经补过结束时刻
                run.ts.append(_CLOCK[0]())
            _finish(run, "return", None)
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)


def _on_unwind(code: CodeType, offset: int, exc: BaseException) -> None:
    try:
        if id(code) in _targets:
            run = _live.pop(id(sys._getframe(1)), None)
            if run is not None:
                if not run.truncated:
                    run.ts.append(_CLOCK[0]())
                _finish(run, "raise", type(exc).__name__)
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _fail(e)


# ---- 内部 ----

def _discount(frame: FrameType | None, since: int) -> None:
    """插件从 since 起做的慢活不算进任何一段：frame 起往上，这条调用栈上每个正在计时的帧（有时刻、没截断）的上一个
    时刻往后挪这么久（那一段因此短这么多）。"""
    if not _live:
        return
    d = _CLOCK[0]() - since
    while frame is not None:
        run = _live.get(id(frame))
        if run is not None and run.ts and not run.truncated:
            run.ts[-1] += d
        frame = frame.f_back


def _awaits_existing_object(caller: FrameType) -> bool:
    """调用方此刻是不是停在"await 一个读出来的已有对象"的 SEND 上（见模块说明）。调用方一定是目标函数的帧。"""
    code = caller.f_code
    hit = _awaits.get(id(code))
    if hit is None:  # 第一次：反汇编是慢活，不算进计时
        since = _CLOCK[0]()
        hit = _awaits[id(code)] = (code, _existing_await_offsets(code))
        _discount(caller, since)
    return caller.f_lasti in hit[1]


def _existing_await_offsets(code: CodeType) -> frozenset[int]:
    """code 里 await 的对象由读已有对象的指令得到的 SEND 所占的字节偏移（含它的内联缓存：帧停在 SEND 上时 f_lasti
    指向缓存）。从 SEND 往回跳过夹在中间的指令找到得到被 await 对象的那条；中间有跳转落点时不算（不能确定是哪一支）。"""
    ins = list(dis.get_instructions(code))
    out: set[int] = set()
    for k, i in enumerate(ins):
        if i.opname != "SEND":
            continue
        j = k - 1
        merged = False
        while j >= 0 and ins[j].opname in _FILLERS:
            merged = merged or ins[j].is_jump_target
            j -= 1
        if j < 0 or merged:
            continue
        p = ins[j]
        if p.opname in _LOADS or (p.opname == "BINARY_OP" and p.argrepr == "[]"):  # 3.14 起下标是 BINARY_OP
            end = ins[k + 1].offset if k + 1 < len(ins) else len(code.co_code)
            out.update(range(i.offset, end))
    return frozenset(out)


def _finish(run: _Run, exit_kind: str, exc: str | None) -> None:
    key = (run.key, run.test, tuple(run.lines), exit_kind, exc, tuple(run.calls), tuple(run.icalls), run.truncated)
    with _lock:
        hit = _buf.get(key)
        if hit is None:
            _buf[key] = [1, run.ts, []]
        else:  # 同样的行序列：时刻一样多，攒一批再逐项相加
            hit[0] += 1
            pending = hit[2]
            pending.append(run.ts)
            if len(pending) >= _MERGE_RUNS or len(pending) * len(run.ts) >= _MERGE_INTS:
                hit[1] = _add_up(hit[1], pending)
                pending.clear()
        _recorded.add(run.key)


def _add_up(acc: list[int], pending: list[list[int]]) -> list[int]:
    """acc 和 pending 里的每个列表逐项相加（等长）。"""
    if len(pending) == 1:
        return list(map(add, acc, pending[0]))
    return list(map(sum, zip(acc, *pending)))


def _record(fn: str, test: str, lines: tuple[int, ...], exit_kind: str, exc: str | None,
            calls: tuple[tuple[str, int], ...], icalls: tuple[tuple[str, int], ...], truncated: bool, n: int,
            ts: list[int]) -> dict:
    rec = {"fn": fn, "test": test, "lines": list(lines), "ns": _ns(fn, lines, ts), "exit": exit_kind,
           "exc": exc, "calls": [list(c) for c in calls], "n": n}
    if icalls:
        rec["icalls"] = [list(c) for c in icalls]
    if truncated:
        rec["truncated"] = True
    return rec


def _ns(fn: str, lines: tuple[int, ...], ts: list[int]) -> list[int]:
    """ts 是 n 次执行里各计时点（和结束）的时刻逐项之和：相邻两项之差是前一个计时点 n 次的耗时之和，记在它在 lines
    里的位置上，其余为 0。计时点按行事件里的同一条规则从 lines 重新认出（同样的 lines 认出的一样）。"""
    marks = _marks.get(fn, frozenset())
    ns = [0] * len(lines)
    at = [i for i, ln in enumerate(lines) if ln in marks or not i]
    for i, d in zip(at, map(sub, ts[1:], ts)):
        ns[i] = d
    return ns


def _note_result(nodeid: str, when: str, outcome: str, duration: float, sub: bool = False,
                 exc: str | None = None) -> None:
    """合进这个测试的结果（见模块说明）：只会变坏。call 阶段（含子测试）给出 passed / failed / skipped；setup、teardown
    失败为 error；setup 跳过为 skipped。时长相加，子测试的不加（已含在 call 里）。别的 outcome（插件加的，如 rerun）
    只加时长。exc：这份报告生成时的异常类型名，第一份 failed 的报告的留下。"""
    entry = _results.setdefault(nodeid, ["", 0.0, None])
    if not sub:
        entry[1] += float(duration)
    if outcome == "failed" and entry[2] is None and exc:
        entry[2] = exc
    if outcome not in _OUTCOMES:
        return
    if when == "call":
        new = outcome
    elif outcome == "failed":  # setup / teardown 出错
        new = "error"
    elif when == "setup" and outcome == "skipped":
        new = "skipped"
    else:
        return
    if _WORSE[new] > _WORSE[entry[0]]:
        entry[0] = new


def _result_line(nodeid: str, result: list) -> dict:
    """测试结束时的结果行：只有去掉参数后缀的 id、outcome、秒数，失败 / 出错时还有异常类型名。没有一份报告定出结果
    （只跑了 setup）时为 passed。"""
    outcome, seconds, exc = result
    line = {"test": _test_id(nodeid), "outcome": outcome or "passed", "seconds": round(seconds, 6), "n": 1}
    if exc and line["outcome"] in ("failed", "error"):
        line["exc"] = exc
    return line


def _flush(result: dict | None = None) -> None:
    """写出缓冲里已完成的执行，result 给出时（测试结束）在它们之后写这个测试的结果行。"""
    global _buf
    with _lock:
        buf, _buf = _buf, {}
    text = "".join(json.dumps(_record(*key, n, _add_up(ts, pending) if pending else ts), ensure_ascii=False) + "\n"
                   for key, (n, ts, pending) in buf.items())
    if result is not None:
        text += json.dumps(result, ensure_ascii=False) + "\n"
    if text:
        _append(text)


def _append(text: str) -> None:
    """以 O_APPEND 整块追加到输出文件（xdist 等多进程同时写时各自成行），写完即关：进程被杀也不丢已写出的。"""
    if not _out:
        return
    fd = os.open(_out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        data = memoryview(text.encode("utf-8"))
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def _test_id(nodeid: str) -> str:
    """pytest 的 node id 去掉参数化的 [参数] 后缀：tests/t.py::test_f[secret-1] → tests/t.py::test_f。参数 id 里可能
    有 ::、[、]，所以从第一个 :: 之后的第一个 [ 起都去掉（类名、函数名里不会有 [；目录名可以有，在第一个 :: 之前）。"""
    head, sep, rest = nodeid.partition("::")
    return head + sep + rest.split("[", 1)[0]


def _write_header() -> None:
    """记录文件还是空的（或不存在）时写入版本头。"""
    fd = os.open(_out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, (json.dumps(_HEADER) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _write_error(e: BaseException) -> None:
    global _error
    text = f"{type(e).__name__}: {e}"
    if _error is None:
        _error = text
    with suppress(Exception):
        _append(json.dumps({"error": text}, ensure_ascii=False) + "\n")


def _fail(e: BaseException) -> None:
    """插件自身出错：停用记录，写出已完成的记录和一行 error。不抛异常；只有第一个出错的调用生效。"""
    if not _release():  # 已经停了（出过错，或正被 stop）
        return
    with suppress(Exception):
        _flush()
    _write_error(e)


def _release() -> bool:
    """停止 sys.monitoring 事件并归还工具编号；不抛异常。返回 True 表示这次调用停掉了正在进行的记录。"""
    global _tool
    tool, _tool = _tool, None  # 先置空：并发出错的其他回调看到 None 就直接返回
    if tool is None:
        return False
    with suppress(Exception):
        _MON.set_events(tool, 0)
        for code, *_ in list(_targets.values()):
            _MON.set_local_events(tool, code, 0)
        for event in (_EV.PY_START, _EV.LINE, _EV.PY_RETURN, _EV.PY_UNWIND):
            _MON.register_callback(tool, event, None)
    with suppress(Exception):
        _MON.free_tool_id(tool)
    return True


def _reset_runs() -> None:
    global _targets, _live, _awaits, _results, _pending_exc
    _targets, _live, _awaits, _results, _pending_exc = {}, {}, {}, {}, {}


def _claim_tool() -> int:
    for tool in _TOOL_IDS:
        if _MON.get_tool(tool) is None:
            _MON.use_tool_id(tool, _TOOL_NAME)
            return tool
    raise RuntimeError(f"sys.monitoring 的工具编号 {_TOOL_IDS} 都已被占用")


def _max_lines_from_env() -> int:
    raw = os.environ.get("PYSTEPMAP_TRACE_MAX_LINES")
    if not raw:
        return _DEFAULT_MAX_LINES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"PYSTEPMAP_TRACE_MAX_LINES 必须是正整数：{raw!r}")
    return value


def _load_graph(path: str) -> tuple[dict[tuple[str, str], list[tuple[int, str]]], dict[str, frozenset[int]]]:
    """(按文件和限定名找函数的表, 函数 key -> 节点头部所在的行)。节点头部：开始节点以外、行号不为 0 的节点的
    line 到 end_line。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("不是 schema_version 为 1 的 pystepmap 图")
    root = data["root"]
    by_name: dict[tuple[str, str], list[tuple[int, str]]] = {}
    marks: dict[str, frozenset[int]] = {}
    for key, fn in data["functions"].items():
        path_ = os.path.realpath(os.path.join(root, fn["file"]))
        by_name.setdefault((path_, fn["qualname"]), []).append((fn["line"], key))
        marks[key] = frozenset(ln for n in fn.get("nodes", ()) if n["type"] != "start" and n["line"] > 0
                               for ln in range(n["line"], max(n["line"], n.get("end_line", 0)) + 1))
    for entries in by_name.values():
        entries.sort()
    return by_name, marks


def _match(code: CodeType) -> str | None:
    """按文件真实路径 + co_qualname 找图里的函数；同名的多处定义取 def 行在 co_firstlineno 处或之后最近的一个
    （带装饰器时 co_firstlineno 是第一个装饰器的行）。"""
    path = _real.get(code.co_filename)
    if path is None:
        path = _real[code.co_filename] = os.path.realpath(code.co_filename)
    if path == _SELF:
        return None
    for line, key in _by_name.get((path, code.co_qualname), ()):
        if line >= code.co_firstlineno:
            return key
    return None


def _at_exit() -> None:
    try:
        _flush()
    except Exception as e:  # noqa: BLE001 —— 插件出任何错都只停用自己，不让被测测试失败
        _write_error(e)


def _after_fork_in_child() -> None:
    """fork 出的子进程：换一把新锁（父进程里可能正被别的线程持有），缓冲和进行中的测试结果归父进程写出。"""
    global _lock, _buf, _results, _pending_exc
    _lock = threading.Lock()
    _buf = {}
    _results = {}
    _pending_exc = {}


def _install_process_hooks() -> None:
    global _process_hooks
    if _process_hooks:
        return
    _process_hooks = True
    atexit.register(_at_exit)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)
