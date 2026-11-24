"""给 AI 读的文本大纲（function_outline / line_outline）和 Mermaid 图（line_mermaid）。期望文本按大纲的写法逐行写出；
节点 sid 由语句的记号流决定（docs/graph-schema.md），和分析结果一致。"""

from __future__ import annotations

import re

from conftest import analyze_files, find_fn
from outline_check import misreadings
from pystepmap.config import ProjectConfig
from pystepmap.outline import function_outline, line_mermaid, line_outline, node_ref

LIB = '''\
def load(x):
    """读取"""


def save(x):
    """保存"""


def notify(x):
    """通知"""


def check(x):
    """检查"""
'''


def outline_of(tmp_path, src: str, qualname: str, depth: int = 0) -> str:
    """把 src 写成 app/c.py（app/lib.py 是共用的几个被调函数），返回 qualname 的大纲全文。"""
    g = analyze_files(tmp_path, {"app/__init__.py": "", "app/lib.py": LIB, "app/c.py": src})
    return "\n".join(function_outline(g, find_fn(g, qualname).key, depth=depth))


def test_straight_three_steps(tmp_path):
    src = """\
from app.lib import load, notify, save


def straight(x):
    load(x)
    save(x)
    notify(x)
"""
    assert outline_of(tmp_path, src, "straight") == """\
函数 straight  app/c.py:4  [app/c.py::straight]
  [step:983bbc52] L5 读取（load） ⇒ 可展开 [app/lib.py::load]
  [step:f1e476a6] L6 保存（save） ⇒ 可展开 [app/lib.py::save]
  [step:7758c294] L7 通知（notify） ⇒ 可展开 [app/lib.py::notify]
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_the_uncaught_exit_is_written_only_when_the_task_retries(tmp_path):
    """每个函数都有的"未捕获异常退出"不写（没有信息）；任务会重试的那种说出了点什么，写在函数段的最后。"""
    src = """\
from taskiq import InMemoryBroker

from app.lib import load

broker = InMemoryBroker()


@broker.task(retry_on_error=True)
async def job(x):
    load(x)


def plain(x):
    load(x)
"""
    assert outline_of(tmp_path, src, "job").splitlines()[-1] == "  [end:uncaught] 结局·异常：未捕获异常退出（任务会重试）"
    assert "未捕获" not in outline_of(tmp_path, src, "plain")


def test_if_returning_early_keeps_the_trunk_unindented(tmp_path):
    src = """\
from app.lib import load, save


def early(x):
    load(x)
    if x is None:
        return "skip"
    save(x)
"""
    assert outline_of(tmp_path, src, "early") == """\
函数 early  app/c.py:4  [app/c.py::early]
  [step:983bbc52] L5 读取（load） ⇒ 可展开 [app/lib.py::load]
  [cond:bfa9cf7d] L6 判断 x is None
    是 → [end:3770c1ce] L7 结局·正常：返回 "skip"
  [step:f1e476a6] L8 保存（save） ⇒ 可展开 [app/lib.py::save]
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_if_else_branches_merge_back(tmp_path):
    src = """\
from app.lib import load, notify, save


def branches(x):
    if x:
        load(x)
    else:
        save(x)
    notify(x)
"""
    assert outline_of(tmp_path, src, "branches") == """\
函数 branches  app/c.py:4  [app/c.py::branches]
  [cond:bff223c8] L5 判断 x
    是 → [step:983bbc52] L6 读取（load） ⇒ 可展开 [app/lib.py::load]
    否 → [step:f1e476a6] L8 保存（save） ⇒ 可展开 [app/lib.py::save]
  [step:7758c294] L9 通知（notify） ⇒ 可展开 [app/lib.py::notify]
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_for_loop_with_continue_in_a_condition(tmp_path):
    """continue 是循环体的提前结束：写成"↪ 回到"循环节点，循环体的主干（load）不缩进；循环体正常走完回到循环节点不写。"""
    src = """\
from app.lib import check, load, save


def each(items):
    for it in items:
        if check(it):
            continue
        load(it)
    save(items)
"""
    assert outline_of(tmp_path, src, "each") == """\
函数 each  app/c.py:4  [app/c.py::each]
  [loop:247aa223] L5 循环 for it in items
    每次 → [cond:efec4843] L6 判断 check(it) ⇒ 可展开 [app/lib.py::check]
      是 → ↪ 回到 [loop:247aa223] 循环 for it in items
    [step:89e1d288] L8 读取（load） ⇒ 可展开 [app/lib.py::load]
  [step:c7e19fc6] L9 保存（save） ⇒ 可展开 [app/lib.py::save]
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_match_with_three_branches(tmp_path):
    src = """\
from app.lib import load, notify, save


def route(ev):
    match ev.kind:
        case "a":
            load(ev)
        case "b" | "c":
            save(ev)
        case _:
            return None
    notify(ev)
"""
    assert outline_of(tmp_path, src, "route") == """\
函数 route  app/c.py:4  [app/c.py::route]
  [cond:56e2db75] L5 判断 match ev.kind
    `"a"` → [step:bb60d13b] L7 读取（load） ⇒ 可展开 [app/lib.py::load]
    `"b" | "c"` → [step:21cb5f3f] L9 保存（save） ⇒ 可展开 [app/lib.py::save]
    `_` → [end:6b056bb6] L11 结局·正常：返回 None
  [step:b33793c1] L12 通知（notify） ⇒ 可展开 [app/lib.py::notify]
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_try_except_with_a_handler_body(tmp_path):
    """异常的落脚点画在 raise 连线的起点（try 体的入口）下面，以"出错 →"开头；try 体其余的语句接着在主干上。处理体走完
    去的不是紧跟在后面的 try 体语句，所以明写"↪ 到"。"""
    src = """\
from app.lib import load, notify, save


def guarded(x):
    try:
        load(x)
        save(x)
    except ValueError:
        notify(x)
    return x
"""
    assert outline_of(tmp_path, src, "guarded") == """\
函数 guarded  app/c.py:4  [app/c.py::guarded]
  [step:983bbc52] L6 读取（load） ⇒ 可展开 [app/lib.py::load]
    出错 → [catch:a0745129] L8 捕获 ValueError
    [step:7758c294] L9 通知（notify） ⇒ 可展开 [app/lib.py::notify]
    ↪ 到 [end:03c38773] 结局·正常：返回 x
  [step:f1e476a6] L7 保存（save） ⇒ 可展开 [app/lib.py::save]
  [end:03c38773] L10 结局·正常：返回 x"""


def test_depth_one_expands_a_callee_once(tmp_path):
    src = """\
from app.lib import load


def prepare(x):
    \"\"\"准备\"\"\"
    load(x)


def outer(x):
    prepare(x)
    prepare(x)
"""
    assert outline_of(tmp_path, src, "outer", depth=1) == """\
函数 outer  app/c.py:9  [app/c.py::outer]
  [step:87d3346a] L10 准备（prepare）
    调用 prepare  app/c.py:4  [app/c.py::prepare]
      [step:983bbc52] L6 读取（load） ⇒ 可展开 [app/lib.py::load]
      [end:implicit] 结局·正常：执行到函数末尾
  [step:87d3346a#2] L11 准备（prepare）
    调用 prepare  app/c.py:4  [app/c.py::prepare]（上面已展开）
  [end:implicit] 结局·正常：执行到函数末尾"""
    # depth=0：只在行尾标可展开
    assert outline_of(tmp_path, src, "outer").splitlines()[1] == \
        "  [step:87d3346a] L10 准备（prepare） ⇒ 可展开 [app/c.py::prepare]"


TASKS = '''\
from app.lib import load, save


@broker.task()
async def child(x):
    """分类"""
    save(x)


@broker.task()
async def parent(x):
    """入站处理"""
    load(x)
    await child.kiq(x)
    await audit.kiq(x)


@broker.task()
async def audit(x):
    pass


@router.post("/other")
async def other(x):
    await audit.kiq(x)
'''


def _tasks(tmp_path):
    g = analyze_files(tmp_path, {"app/__init__.py": "", "app/lib.py": LIB, "app/t.py": TASKS},
                      ProjectConfig(common_task_threshold=2))
    [line] = [ln for ln in g.lines if ln.id == find_fn(g, "parent").key]
    return g, line


def test_line_with_a_dispatched_task_and_a_common_task(tmp_path):
    g, line = _tasks(tmp_path)
    assert g.common_tasks == [find_fn(g, "audit").key]
    assert line_outline(g, line) == """\
# 入站处理  [line:app/t.py::parent]  分组：后台任务
任务 parent  app/t.py:11  [app/t.py::parent]
  [step:983bbc52] L13 读取（load） ⇒ 可展开 [app/lib.py::load]
  [step:897f4afa] L14 派发：分类 ⇒ 见下面的任务 child
  [step:6f7b1157] L15 派发：audit ⇒ 公共任务 audit（不并入本线）
  [end:implicit] 结局·正常：执行到函数末尾
任务 child  app/t.py:5  [app/t.py::child]
  [step:f1e476a6] L7 保存（save） ⇒ 可展开 [app/lib.py::save]
  [end:implicit] 结局·正常：执行到函数末尾
公共任务（不并入本线，各自成线）：audit"""


def test_route_line_head_and_depth_in_a_line(tmp_path):
    g, _ = _tasks(tmp_path)
    [other] = [ln for ln in g.lines if ln.id == find_fn(g, "other").key]
    assert line_outline(g, other, depth=1) == """\
# POST /other  [line:app/t.py::other]  分组：t
接口 other（POST /other）  app/t.py:24  [app/t.py::other]
  [step:6f7b1157] L25 派发：audit ⇒ 公共任务 audit（不并入本线）
  [end:implicit] 结局·正常：执行到函数末尾
公共任务（不并入本线，各自成线）：audit"""


def test_goto_task_is_one_line(tmp_path):
    src = """\
@broker.task()
async def ping(x):
    await pong.kiq(x)


@broker.task()
async def pong(x):
    await ping.kiq(x)
"""
    g = analyze_files(tmp_path, {"app/r.py": src})
    [line] = g.lines
    assert line_outline(g, line) == """\
# ping  [line:app/r.py::ping]  分组：后台任务
任务 ping  app/r.py:2  [app/r.py::ping]
  [step:59f9152f] L3 派发：pong ⇒ 见下面的任务 pong
  [end:implicit] 结局·正常：执行到函数末尾
任务 pong  app/r.py:7  [app/r.py::pong]
  [step:dec6c262] L8 派发：ping ⇒ 见上面的任务 ping
  [end:implicit] 结局·正常：执行到函数末尾
回到任务 ping  [app/r.py::ping]"""


# ---------- 大纲没有明说、这里定下来的写法 ----------

DECISIONS = """\
async def tryfin():
    try:
        await a()
        return 1
    except KeyError:
        await b()
        raise
    finally:
        await c()
    await d()


async def whiler():
    while await a():
        if await b():
            break
        await c()
    else:
        await d()


async def always(x):
    if x:
        return 1
    else:
        return 2


async def forever():
    while True:
        if await a():
            break
        if await b():
            return 3
"""


def _decisions(tmp_path, qualname: str) -> str:
    g = analyze_files(tmp_path, {"app/x.py": DECISIONS})
    return "\n".join(function_outline(g, find_fn(g, qualname).key))


def test_finally_hops_are_written_as_jumps(tmp_path):
    """结局进入 finally 体：结局之后写"↪ 进入 finally，到 …"；没被捕获的异常进 finally 体（落脚点就是汇合点）不写。
    try / except 的每条路都以结局结束：出了 finally 体函数就结束了，finally 体的出口不往后接，后面的语句走不到。"""
    assert _decisions(tmp_path, "tryfin") == """\
函数 tryfin  app/x.py:1  [app/x.py::tryfin]
  [step:b9186630] L3 a
    出错 → [catch:ebc4d25e] L5 捕获 KeyError
    [step:3ed9c5d3] L6 b
    [end:657db1e0] L7 结局·失败：抛出 原异常
    ↪ 进入 finally，到 [step:89224e37] c
  [end:e0e1f9fa] L4 结局·正常：返回 1
  ↪ 进入 finally，到 [step:89224e37] c
  [step:89224e37] L9 c
  走不到的节点：
    [step:ca199892] L10 d"""


def test_break_and_loop_else(tmp_path):
    """break 写成"↪ 跳出循环，到 …"；循环正常结束走的 else 体以"循环结束 →"开头。"""
    assert _decisions(tmp_path, "whiler") == """\
函数 whiler  app/x.py:13  [app/x.py::whiler]
  [loop:13d7ec36] L14 循环 while await a()
    每次 → [cond:3308f9d8] L15 判断 await b()
      是 → ↪ 跳出循环，到 [end:implicit] 结局·正常：执行到函数末尾
    [step:89224e37] L17 c
    循环结束 → [step:ca199892] L19 d
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_both_branches_return_the_last_outcome_is_the_trunk(tmp_path):
    """没有"执行到函数末尾"时，最后一个结局算主干：另一支是提前结束的分支。"""
    assert _decisions(tmp_path, "always") == """\
函数 always  app/x.py:22  [app/x.py::always]
  [cond:bff223c8] L23 判断 x
    是 → [end:e0e1f9fa] L24 结局·正常：返回 1
  [end:7f923f79] L26 结局·正常：返回 2"""


def test_loop_body_ending_in_a_condition(tmp_path):
    """循环体最后一句是判断、一支走到循环体末尾（改写成带分支的 loop_back）：仍算循环体的出口，前面 break 的判断之后
    是循环体的主干。"""
    assert _decisions(tmp_path, "forever") == """\
函数 forever  app/x.py:29  [app/x.py::forever]
  [loop:6aacfe99] L30 循环 while True
    每次 → [cond:2dd3c25b] L31 判断 await a()
      是 → ↪ 跳出循环，到 [end:implicit] 结局·正常：执行到函数末尾
    [cond:3308f9d8] L33 判断 await b()
      是 → [end:09d3822d] L34 结局·正常：返回 3
      否 → ↪ 回到 [loop:6aacfe99] 循环 while True
  [end:implicit] 结局·正常：执行到函数末尾"""


ENDINGS = """\
async def logged():
    try:
        await a()
    except Exception:
        log = 1
    await b()


async def locked(ok):
    try:
        if not ok:
            raise ValueError(ok)
        await a()
        return 1
    finally:
        await c()


async def scan(items, mode):
    if mode:
        for it in items:
            await a(it)
        return 1
    return 2
"""


def _endings(tmp_path, qualname: str) -> str:
    g = analyze_files(tmp_path, {"app/y.py": ENDINGS})
    return "\n".join(function_outline(g, find_fn(g, qualname).key))


def test_swallowed_exception_continues_on_the_trunk(tmp_path):
    """只写日志的 except：异常落到正常的路也去的节点，不另写"出错 →"。"""
    assert _endings(tmp_path, "logged") == """\
函数 logged  app/y.py:1  [app/y.py::logged]
  [step:b9186630] L3 a
  [step:3ed9c5d3] L6 b
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_try_finally_whose_paths_all_end_in_outcomes(tmp_path):
    """try 体里每条路都以结局结束、再进 finally 体：结局进 finally 不算接着走，抛出的那支是提前结束，主干接着另一支。
    finally 体画在最后，它的出口不往后接（没有"执行到函数末尾"：return 已经生效，或者异常接着往外抛）。"""
    assert _endings(tmp_path, "locked") == """\
函数 locked  app/y.py:9  [app/y.py::locked]
  [cond:067c1557] L11 判断 not ok
    是 → [end:74f6b6b1] L12 结局·失败：抛出 ValueError(ok)
    ↪ 进入 finally，到 [step:89224e37] c
  [step:b9186630] L13 a
  [end:e0e1f9fa] L14 结局·正常：返回 1
  ↪ 进入 finally，到 [step:89224e37] c
  [step:89224e37] L16 c"""


def test_loop_followed_only_by_an_outcome_inside_a_branch(tmp_path):
    """分支里循环之后只有一个结局：结局接在循环之后（不写成"循环结束 →"）。"""
    assert _endings(tmp_path, "scan") == """\
函数 scan  app/y.py:19  [app/y.py::scan]
  [cond:3a4b5299] L20 判断 mode
    是 → [loop:247aa223] L21 循环 for it in items
      每次 → [step:6cb3555f] L22 a
    [end:e0e1f9fa] L23 结局·正常：返回 1
  [end:7f923f79] L24 结局·正常：返回 2"""


# ---------- 复查第 1 轮：读者推断的"接下来"必须是真实的后继 ----------

ROUND1 = """\
async def find_or_raise(xs):
    for x in xs:
        if await match(x):
            break
    else:
        raise LookupError(xs)
    await use(x)


async def retry():
    for attempt in range(3):
        try:
            r = await call()
            break
        except OSError:
            await nap()
    else:
        raise RuntimeError("x")
    await use(r)


async def skip_marked(rows):
    for row in rows:
        try:
            await alert(row)
        except Exception:
            continue
        await mark(row)
    await done()


async def m2(kind):
    match kind:
        case "x":
            return 1
        case "y":
            await handle()
        case _:
            raise ValueError(kind)
    await after()


async def elifchain(x):
    if await a(x):
        await b()
    elif await c(x):
        await d()
    elif x > 3:
        return 1
    else:
        await e()
    await f()


async def cont_finally(xs):
    for x in xs:
        try:
            if await a(x):
                continue
            await b(x)
        finally:
            await c(x)
        await d(x)
    await e()


async def ordered(evs):
    for ev in evs:
        match ev:
            case 1:
                break
            case 2:
                await a()
            case _:
                await b()
"""


def _round1(tmp_path, qualname: str) -> str:
    g = analyze_files(tmp_path, {"app/r1.py": ROUND1})
    return "\n".join(function_outline(g, find_fn(g, qualname).key))


def test_for_else_raise_with_the_rest_reached_by_break(tmp_path):
    """循环正常结束就抛出、之后的语句只由 break 走到：循环体留在"每次 →"下面，break 明写跳到哪，循环之后接着主干。"""
    assert _round1(tmp_path, "find_or_raise") == """\
函数 find_or_raise  app/r1.py:1  [app/r1.py::find_or_raise]
  [loop:0d8a2d32] L2 循环 for x in xs
    每次 → [cond:3286dfe9] L3 判断 await match(x)
      是 → ↪ 跳出循环，到 [step:202bca4f] use
      否 → ↪ 回到 [loop:0d8a2d32] 循环 for x in xs
    循环结束 → [end:96c9f597] L6 结局·失败：抛出 LookupError(xs)
  [step:202bca4f] L7 use
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_retry_loop(tmp_path):
    """重试：成功就 break，出错睡一下再来一轮，三次都不行就抛出。"""
    assert _round1(tmp_path, "retry") == """\
函数 retry  app/r1.py:10  [app/r1.py::retry]
  [loop:27a376db] L11 循环 for attempt in range(3)
    每次 → [step:0c62e4ff] L13 call
      出错 → [catch:5bcf987c] L15 捕获 OSError
      [step:8e00b38f] L16 nap
      ↪ 回到 [loop:27a376db] 循环 for attempt in range(3)
    ↪ 跳出循环，到 [step:30beb520] use
    循环结束 → [end:c79e2762] L18 结局·失败：抛出 RuntimeError("x")
  [step:30beb520] L19 use
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_except_continue(tmp_path):
    """except 里 continue：处理体之后明写回到循环，不能读成"出错之后接着 mark"。"""
    assert _round1(tmp_path, "skip_marked") == """\
函数 skip_marked  app/r1.py:22  [app/r1.py::skip_marked]
  [loop:ad7f33a7] L23 循环 for row in rows
    每次 → [step:9703c13d] L25 alert
      出错 → [catch:09fc15c2] L26 捕获 Exception
      ↪ 回到 [loop:ad7f33a7] 循环 for row in rows
    [step:5b03248f] L28 mark
  [step:0e417ab5] L29 done
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_match_case_that_carries_on_keeps_its_label(tmp_path):
    """只有一个 case 接着往下走：它的分支行照样写（分支体在它的分支名下面），match 之后的语句接在主干上。"""
    assert _round1(tmp_path, "m2") == """\
函数 m2  app/r1.py:32  [app/r1.py::m2]
  [cond:c58dc44a] L33 判断 match kind
    `"x"` → [end:e0e1f9fa] L35 结局·正常：返回 1
    `"y"` → [step:d2f3f48c] L37 handle
    `_` → [end:dbd7c11d] L39 结局·失败：抛出 ValueError(kind)
  [step:b01d1655] L40 after
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_elif_chain_stays_at_one_level(tmp_path):
    """elif 写成"否则 →"，和第一个 if 的分支同一缩进，不一层层往里缩；最后一个 elif 之后的路接在它那一块里。"""
    assert _round1(tmp_path, "elifchain") == """\
函数 elifchain  app/r1.py:43  [app/r1.py::elifchain]
  [cond:5bb7e3c9] L44 判断 await a(x)
    是 → [step:3ed9c5d3] L45 b
    否则 → [cond:002fd42d] L46 判断 await c(x)
      是 → [step:ca199892] L47 d
    否则 → [cond:dc0c8b44] L48 判断 x > 3
      是 → [end:e0e1f9fa] L49 结局·正常：返回 1
    [step:9335846c] L51 e
  [step:9edb440c] L52 f
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_finally_body_reached_by_continue(tmp_path):
    """continue 穿过 finally：finally 体之后回到循环的那条出路明写在它下面，正常的路接着往下。"""
    assert _round1(tmp_path, "cont_finally") == """\
函数 cont_finally  app/r1.py:55  [app/r1.py::cont_finally]
  [loop:0d8a2d32] L56 循环 for x in xs
    每次 → [cond:5bb7e3c9] L58 判断 await a(x)
      是 → ↪ 进入 finally 再回到循环，到 [step:ef100c91] c
    [step:e994d9a4] L60 b
    [step:ef100c91] L62 c
      ↪ 回到 [loop:0d8a2d32] 循环 for x in xs
    [step:66280e08] L63 d
  [step:9335846c] L64 e
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_match_branch_order(tmp_path):
    """match 的分支：有分支体的按分支体所在行；没有节点的（直接 break、continue、pass）在源码里的先后图里没有，写在一行
    "（以下分支没有节点，原顺序未知）"下面，按连线顺序；兜底的最后。"""
    assert _round1(tmp_path, "ordered") == """\
函数 ordered  app/r1.py:67  [app/r1.py::ordered]
  [loop:44e08ddf] L68 循环 for ev in evs
    每次 → [cond:e0fe9e84] L69 判断 match ev
      `2` → [step:b9186630] L73 a
      （以下分支没有节点，原顺序未知）
      `1` → ↪ 跳出循环，到 [end:implicit] 结局·正常：执行到函数末尾
      `_` → [step:3ed9c5d3] L75 b
  [end:implicit] 结局·正常：执行到函数末尾"""


# ---------- 复查第 2 轮 ----------

ROUND2 = """\
async def wt():
    while True:
        try:
            r = await get()
            if await bad(r):
                break
        finally:
            await cleanup()
        await use(r)
    await end()


async def wt_cont(xs):
    for x in xs:
        try:
            if await skip(x):
                continue
        finally:
            await cleanup()
        await use(x)


async def lock_loop():
    async with lock:
        while True:
            if await done():
                break
            await step()
"""

ORDER = """\
async def order(ev):
    match ev:
        case Ping():
            pass
        case Msg(kind="x"):
            await x(ev)
        case Msg():
            pass
        case Event():
            await generic(ev)
    await done()
"""

NONES = """\
async def nones(x):
    match x:
        case None:
            await empty()
        case int():
            await num(x)
        case other:
            await rest(other)
"""


def _outline_in(tmp_path, src: str, qualname: str) -> str:
    g = analyze_files(tmp_path, {"app/r2.py": src})
    return "\n".join(function_outline(g, find_fn(g, qualname).key))


def test_break_through_finally_keeps_its_branch_row(tmp_path):
    """try 体以"if …: break"结束、外面有 finally：两条分支都到 finally 体，但走法不同——"是"那一行写出来，说清进
    finally 之后跳出循环。"""
    assert _outline_in(tmp_path, ROUND2, "wt") == """\
函数 wt  app/r2.py:1  [app/r2.py::wt]
  [loop:6aacfe99] L2 循环 while True
    每次 → [step:87f495d4] L4 get
    [cond:1c48d46a] L5 判断 await bad(r)
      是 → ↪ 进入 finally 再跳出循环，到 [step:72a2ebe5] cleanup
    [step:72a2ebe5] L8 cleanup
      ↪ 跳出循环，到 [step:7b9cbc6c] end
    [step:30beb520] L9 use
  [step:7b9cbc6c] L10 end
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_continue_through_finally_keeps_its_branch_row(tmp_path):
    assert _outline_in(tmp_path, ROUND2, "wt_cont") == """\
函数 wt_cont  app/r2.py:13  [app/r2.py::wt_cont]
  [loop:0d8a2d32] L14 循环 for x in xs
    每次 → [cond:7a2fcc8e] L16 判断 await skip(x)
      是 → ↪ 进入 finally 再回到循环，到 [step:72a2ebe5] cleanup
    [step:72a2ebe5] L19 cleanup
      ↪ 回到 [loop:0d8a2d32] 循环 for x in xs
    [step:202bca4f] L20 use
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_match_cases_without_nodes_go_under_a_marker(tmp_path):
    """没有节点的 case（pass）排在有节点的后面，先写一行"原顺序未知"；读者不能从先后推断哪个先匹配。"""
    assert _outline_in(tmp_path, ORDER, "order") == """\
函数 order  app/r2.py:1  [app/r2.py::order]
  [cond:e0fe9e84] L2 判断 match ev
    `Msg(kind="x")` → [step:eec31b81] L6 x
    `Event()` → [step:9d755e5b] L10 generic
    （以下分支没有节点，原顺序未知）
    `Ping()` → ↪ 到 [step:0e417ab5] done
    `Msg()` → ↪ 到 [step:0e417ab5] done
  [step:0e417ab5] L11 done
  [end:implicit] 结局·正常：执行到函数末尾"""


def test_none_true_false_patterns_are_not_fallbacks(tmp_path):
    """case None / True / False 是值模式，不是兜底：照分支体所在行排。"""
    assert _outline_in(tmp_path, NONES, "nones") == """\
函数 nones  app/r2.py:1  [app/r2.py::nones]
  [cond:612d878b] L2 判断 match x
    `None` → [step:81dc119e] L4 empty
    `int()` → [step:512ac7ab] L6 num
    `other` → [step:e5da5858] L8 rest
  [end:implicit] 结局·正常：执行到函数末尾"""


ROUND3 = """\
async def nested_fin(xs):
    for x in xs:
        try:
            try:
                if await a(x):
                    break
                await b(x)
            finally:
                await c(x)
        finally:
            await d(x)
        await e(x)


async def last_fin_cont(xs):
    for x in xs:
        try:
            if await a(x):
                continue
            await b(x)
        finally:
            await c(x)


async def last_fin_break(xs):
    for x in xs:
        try:
            if await a(x):
                break
            await b(x)
        finally:
            await c(x)
    await z()


async def ret_and_cont(xs):
    for x in xs:
        try:
            if await a(x):
                return 1
            if await b(x):
                continue
        finally:
            await c(x)
        await d(x)


async def fin_branch(xs):
    for x in xs:
        try:
            if await a(x):
                continue
        finally:
            if await c(x):
                await log(x)
        await d(x)


async def step_then_break(xs):
    for x in xs:
        try:
            await a(x)
            break
        finally:
            await c(x)
    await z()
"""


def test_break_through_two_finally_bodies(tmp_path):
    """内层 finally 体的出口既正常往下走到外层 finally 体、又带着 break 进外层 finally：两条都写（进外层 finally 的那条写成
    它下面的"↪"行，说明之后跳出循环）；"是"那一行只跳到内层 finally 体，内层 finally 体留在主干上。"""
    assert _outline_in(tmp_path, ROUND3, "nested_fin") == """\
函数 nested_fin  app/r2.py:1  [app/r2.py::nested_fin]
  [loop:0d8a2d32] L2 循环 for x in xs
    每次 → [cond:5bb7e3c9] L5 判断 await a(x)
      是 → ↪ 进入 finally 再跳出循环，到 [step:ef100c91] c
    [step:e994d9a4] L7 b
    [step:ef100c91] L9 c
      ↪ 进入 finally 再跳出循环，到 [step:66280e08] d
    [step:66280e08] L11 d
      ↪ 跳出循环，到 [end:implicit] 结局·正常：执行到函数末尾
    [step:bc9ba494] L12 e
  [end:implicit] 结局·正常：执行到函数末尾"""


_SWAP = {"是": "否", "否": "是", "每次": "循环结束", "循环结束": "每次"}
_AFTER_FINALLY = {"再跳出循环": "再回到循环", "再回到循环": "再跳出循环", "再跳出循环或回到循环": "再跳出循环"}


def _mutants(lines: list[str]):
    """一份大纲读法上不同的改法：分支名 是↔否、每次↔循环结束；跳转的 回到↔到；去掉 / 加上"跳出循环，"；去掉"进入
    finally …，"。"""
    for i, row in enumerate(lines[1:], 1):
        m = re.match(r"^(\s*)(是|否|每次|循环结束) → (.*)$", row)
        if m:
            yield f"分支名 {row.strip()}", [*lines[:i], f"{m[1]}{_SWAP[m[2]]} → {m[3]}", *lines[i + 1:]]
        m = re.match(r"^(.*)↪ (跳出循环，|进入 finally[^，]*，)?(回到|到) (.*)$", row)
        if m:
            verb = "到" if m[3] == "回到" else "回到"
            yield f"动词 {row.strip()}", [*lines[:i], f"{m[1]}↪ {m[2] or ''}{verb} {m[4]}", *lines[i + 1:]]
            lead = "" if m[2] else "跳出循环，"
            yield f"说法 {row.strip()}", [*lines[:i], f"{m[1]}↪ {lead}{m[3]} {m[4]}", *lines[i + 1:]]
            f = re.match(r"进入 finally (再[^，]*)，", m[2] or "")
            if f:  # 穿过 finally 之后做什么：换成另一种、或者不说
                for lead in (f"进入 finally {_AFTER_FINALLY[f[1]]}，", "进入 finally，"):
                    yield f"finally 之后 {row.strip()}", [*lines[:i], f"{m[1]}↪ {lead}{m[3]} {m[4]}", *lines[i + 1:]]


def test_the_checker_catches_wrong_labels_and_jump_wording(tmp_path):
    """读法检查本身：把正确的大纲改错一处（分支名、跳转的动词或说法），misreadings 都要报出来。"""
    g = analyze_files(tmp_path, {"app/r1.py": ROUND1, "app/r2.py": ROUND2, "app/r3.py": ROUND3, "app/d.py": DECISIONS,
                                 **REVIEW})
    total = 0
    for key, fg in g.functions.items():
        lines = function_outline(g, key)
        assert misreadings(fg, lines) == [], "\n".join(lines)
        for what, mutant in _mutants(lines):
            total += 1
            assert misreadings(fg, mutant), f"{key}: 改了 {what} 没查出来\n" + "\n".join(mutant)
    assert total > 60


# 复查给出的复现用例，原样收进来（每个文件各分析一份）
REVIEW = {
    "app/c1.py": """\
async def elifchain(x):
    if await a(x):
        await b()
    elif await c(x):
        await d()
    elif x > 3:
        return 1
    else:
        await e()
    await f()


async def nested(x, y):
    if await a(x):
        if await b(y):
            await c()
        await d()
    await e()


async def loopelse(xs):
    for x in xs:
        if await a(x):
            break
        if await b(x):
            continue
        await c(x)
    else:
        await d()
    await e()


async def trye(x):
    try:
        await a()
    except KeyError:
        await b()
    else:
        await c()
    finally:
        await d()
    await e()


async def match_noguard(ev):
    match ev:
        case 1:
            await a()
        case 2 if await g(ev):
            await b()
        case 3:
            return 5
    await c()
""",
    "app/c2.py": """\
async def poll():
    while await ready():
        if await done():
            break
        await work()
    else:
        return 1
    await finish()


async def ret_in_try_finally(x):
    try:
        if await a(x):
            return 1
        await b(x)
    finally:
        await c(x)
    await d(x)
""",
    "app/c3.py": """\
async def last_elif(xs):
    for x in xs:
        await pre(x)
        if await a(x):
            await b(x)
        elif await c(x):
            await d(x)
    await e()


async def match_in_loop(xs):
    async for x in xs:
        match x:
            case 1:
                await a()
            case 2:
                continue
    await b()


async def withs(x):
    async with await lock() as l:
        await a()
        if x:
            return 1
    await b()


async def outer_break(rows):
    for r in rows:
        for c in r:
            if await a(c):
                break
            await b(c)
        await d(r)
    await e()
""",
    "app/c4.py": """\
async def m3(kind):
    match kind:
        case "x":
            return 1
        case "y" if await ok():
            await handle()
    await after()
""",
    "app/c5.py": """\
async def serve():
    await setup()
    while True:
        msg = await recv()
        if msg is None:
            return
        await handle(msg)


async def worker():
    while True:
        try:
            await job()
        except Exception:
            await log_it()


async def gen_loop(xs):
    for x in xs:
        if await a(x):
            return x
    raise LookupError(xs)
""",
    "app/c6.py": """\
async def cont_after_step(xs):
    for x in xs:
        if await a(x):
            await b(x)
            continue
        await c(x)
        await d(x)
""",
}


# ---------- 稳健：每个节点恰好一行，任何图都画得完 ----------

TANGLED = """\
async def tangled(items, mode):
    for it in items:
        try:
            if await a(it):
                continue
            while await b(it):
                match it.kind:
                    case 1:
                        break
                    case 2 if await c(it):
                        continue
                    case _:
                        await d(it)
            else:
                return 1
        except KeyError:
            await e(it)
            break
        except Exception:
            pass
        finally:
            await f(it)
    else:
        if mode:
            raise ValueError(mode)
    with await g():
        await h()
    return 2


async def empty_body():
    for _ in await a():
        pass
    while True:
        pass


def only_raise():
    raise ValueError("x")


async def swallow():
    try:
        if await a():
            await b()
    except Exception:
        x = 1
    await c()
"""

def node_lines(text: str) -> list[str]:
    """大纲里作为"节点行"出现的节点 sid（分支行"是 → [sid] …"也算；"↪"跳转行、函数头不算）。"""
    out = []
    for row in text.splitlines():
        if "↪" in row or row.startswith(("函数 ", "任务 ", "接口 ", "工作流 ", "# ", "回到任务", "公共任务")):
            continue
        if row.strip().startswith(("调用 ", "走不到的节点")):
            continue
        m = re.match(r"^\s*(?:\[([^\]]+)\]|.+? → \[([^\]]+)\])", row)  # 节点行，或"分支名 → 节点行"
        if m:
            out.append(m.group(1) or m.group(2))
    return out


def _fixture_graph(tmp_path):
    files = {"app/x.py": TANGLED, "app/d.py": DECISIONS, "app/y.py": ENDINGS, "app/r1.py": ROUND1, "app/r2.py": ROUND2,
             "app/r3.py": ROUND3, "app/o.py": ORDER, "app/n.py": NONES, **REVIEW}
    return analyze_files(tmp_path, files)


def test_every_node_once_and_the_outline_reads_like_the_graph(tmp_path):
    """所有用例里的每个函数：开始节点、隐含的"未捕获异常退出"之外的每个节点恰好一行；照读者的读法推出的"接下来"都是图里真实的后继
    （outline_check.misreadings 为空）。"""
    g = _fixture_graph(tmp_path)
    assert len(g.functions) > 30
    for key, fg in g.functions.items():
        rows = function_outline(g, key)
        text = "\n".join(rows)
        assert sorted(node_lines(text)) == sorted(n.sid for n in fg.nodes if n.type != "start" and n.sid != "end:uncaught"), text
        assert misreadings(fg, rows) == [], text


def test_unreachable_nodes_go_into_a_closing_block(tmp_path):
    """走不到的节点（自检会报的那种）列在最后的"走不到的节点"里；手工造一张这样的图。"""
    g = analyze_files(tmp_path, {"app/x.py": TANGLED})
    fg = find_fn(g, "swallow")
    first = next(e for e in fg.edges if e.src == "n1")
    fg.edges.remove(first)  # 开始节点之后全都走不到
    text = "\n".join(function_outline(g, fg.key))
    rows = text.splitlines()
    assert rows[0] == "函数 swallow  app/x.py:42  [app/x.py::swallow]"
    assert rows[1] == "  走不到的节点："  # 隐含的"未捕获异常退出"不写
    assert sorted(node_lines(text)) == sorted(n.sid for n in fg.nodes if n.type != "start" and n.sid != "end:uncaught")
    assert all(r.startswith("    [") for r in rows[2:])


def test_cycles_and_self_loops_terminate(tmp_path):
    """手工造的环（不是循环节点的环、自环）也画得完，每个节点仍只有一行。"""
    g = analyze_files(tmp_path, {"app/x.py": TANGLED})
    fg = find_fn(g, "swallow")
    ids = [n.id for n in fg.nodes]
    from pystepmap.model import Edge
    fg.edges += [Edge(ids[3], ids[1], "next"), Edge(ids[2], ids[2], "next"), Edge(ids[4], ids[2], "no", branch="no")]
    text = "\n".join(function_outline(g, fg.key))
    assert sorted(node_lines(text)) == sorted(n.sid for n in fg.nodes if n.type != "start" and n.sid != "end:uncaught"), text


def test_node_ref(tmp_path):
    g = analyze_files(tmp_path, {"app/x.py": TANGLED})
    fg = find_fn(g, "only_raise")
    assert node_ref(fg, fg.nodes[1]) == f"[{fg.nodes[1].sid}]"


# ---------- Mermaid ----------

MERMAID = '''\
from app.lib import load


def weird(x):
    """计算 (a|b) 的 "总和" [x] <y> #1"""


@broker.task()
async def worker(ev):
    """处理事件"""
    match ev.kind:
        case "b" | "c":
            weird(ev)
        case _:
            raise ValueError("bad")
    await helper_task.kiq(ev)


@broker.task()
async def helper_task(ev):
    load(ev)
'''


def test_line_mermaid(tmp_path):
    g = analyze_files(tmp_path, {"app/__init__.py": "", "app/lib.py": LIB, "app/m.py": MERMAID})
    [line] = [ln for ln in g.lines if ln.id == find_fn(g, "worker").key]
    assert line_mermaid(g, line) == """\
flowchart TD
  subgraph f1["任务 worker  app/m.py:9"]
    f1_n1["开始：处理事件"]
    f1_n2{"L11 判断 match ev.kind"}
    f1_n3["L13 计算 (a#124;b) 的 #quot;总和#quot; [x] #lt;y#gt; #35;1（weird）"]
    f1_n4("L15 结局·失败：抛出 ValueError(#quot;bad#quot;)")
    f1_n5["L16 派发：helper_task"]
    f1_n6("结局·正常：执行到函数末尾")
    f1_n7("结局·异常：未捕获异常退出")
    f1_n1 --> f1_n2
    f1_n2 -->|"#quot;b#quot; #124; #quot;c#quot;"| f1_n3
    f1_n2 -->|"_"| f1_n4
    f1_n3 --> f1_n5
    f1_n5 --> f1_n6
  end
  subgraph f2["任务 helper_task  app/m.py:20"]
    f2_n1["开始：helper_task"]
    f2_n2["L21 读取（load）"]
    f2_n3("结局·正常：执行到函数末尾")
    f2_n4("结局·异常：未捕获异常退出")
    f2_n1 --> f2_n2
    f2_n2 --> f2_n3
  end
  f1_n5 -.->|"派发"| f2_n1"""


MERMAID_ROW = re.compile(
    r'^(?:flowchart TD'
    r'|  subgraph f\d+\["[^"]*"\]'
    r'|    f\d+_n\d+(?:\["[^"]*"\]|\{"[^"]*"\}|\("[^"]*"\))'
    r'|    f\d+_n\d+ (?:-->|-\.->)(?:\|"[^"|]*"\|)? f\d+_n\d+'
    r'|  end'
    r'|  f\d+_n\d+ -\.->\|"派发"\| f\d+_n\d+'
    r'|  %% .*)$')


def _tasks_in_order(task):
    """派发树先序上的任务（goto 的不算），即 subgraph 的顺序。"""
    out = [] if task.goto else [task.function]
    for child in task.children:
        out += _tasks_in_order(child)
    return out


def test_line_mermaid_rows_follow_the_rules(tmp_path):
    """每一行都是规定的几种之一；判断是菱形、结局是圆角、其余是方框；引号、|、<>、# 都转成了实体。"""
    g, _ = _tasks(tmp_path / "a")
    tangled_tasks = TANGLED.replace("async def", "@broker.task()\nasync def").replace(
        "def only_raise", "@broker.task()\ndef only_raise")
    g2 = analyze_files(tmp_path / "b", {"app/__init__.py": "", "app/lib.py": LIB, "app/m.py": MERMAID,
                                        "app/y.py": tangled_tasks})
    shapes = {"cond": "{", "end": "("}
    seen = set()
    for graph in (g, g2):
        for line in graph.lines:
            order = _tasks_in_order(line.root)
            for row in line_mermaid(graph, line).splitlines():
                assert MERMAID_ROW.match(row), row
                m = re.match(r"    f(\d+)_(n\d+)([\[{(])", row)
                if m:
                    fg = graph.functions[order[int(m.group(1)) - 1]]
                    node = next(n for n in fg.nodes if n.id == m.group(2))
                    assert m.group(3) == shapes.get(node.type, "["), row
                    seen.add(node.type)
    assert seen == {"start", "step", "cond", "loop", "catch", "end"}
    rows = line_mermaid(g, _tasks(tmp_path / "c")[1]).splitlines()
    assert "  %% 公共任务（不并入本线，各自成线）：audit" in rows
    assert '    f1_n1["开始：入站处理"]' in rows


def test_depth_is_clamped_and_expansion_is_not_recursive(tmp_path):
    """depth 限制在 0～5：一串 8 个函数（每个嵌套 30 层 if，最里面调用下一个）depth=50 和 depth=5 写出来一样，第 6 个
    起只标"可展开"；负数当 0。"""
    def fn(j: int) -> str:
        rows, ind = [f"async def f{j}(x):"], "    "
        for i in range(30):
            rows += [f"{ind}if await c{i}(x):", f"{ind}    await a{i}()"]
            ind += "    "
        rows.append(f"{ind}await f{j + 1}(x)" if j < 7 else f"{ind}pass")
        return "\n".join(rows) + "\n\n\n"

    g = analyze_files(tmp_path, {"app/chain.py": "".join(fn(j) for j in range(8))})
    key = find_fn(g, "f0").key
    deep = function_outline(g, key, depth=50)
    assert deep == function_outline(g, key, depth=5)
    assert function_outline(g, key, depth=-3) == function_outline(g, key)
    heads = [r.strip() for r in deep if r.strip().startswith("调用 ")]
    assert [h.split()[1] for h in heads] == ["f1", "f2", "f3", "f4", "f5"]
    assert any("⇒ 可展开 [app/chain.py::f6]" in r for r in deep)


def test_deeply_nested_blocks(tmp_path):
    """嵌套到 Python 允许的最深（95 层 if）也画得完，每个节点一行、读法不错。"""
    rows, ind = ["async def deep(x):"], "    "
    for i in range(95):
        rows += [f"{ind}if await c{i}(x):", f"{ind}    await a{i}()"]
        ind += "    "
    g = analyze_files(tmp_path, {"app/deep.py": "\n".join(rows) + "\n"})
    fg = find_fn(g, "deep")
    lines = function_outline(g, fg.key)
    assert sorted(node_lines("\n".join(lines))) == sorted(n.sid for n in fg.nodes if n.type != "start" and n.sid != "end:uncaught")
    assert misreadings(fg, lines) == []


def test_workflow_on_a_route_shows_method_and_path_in_its_head(tmp_path):
    """挂在路由上的工作流：函数头里限定名后面照样写（方法 路径），和普通接口一样。"""
    g = analyze_files(tmp_path, {"app/users.py": '''
from fastapi import APIRouter
from pystepkit import workflow
router = APIRouter(prefix="/users")
@workflow(title="查看用户")
@router.get("/{uid}")
def get_user(uid):
    return uid
'''})
    [line] = g.lines
    rows = line_outline(g, line).splitlines()
    assert rows[0].startswith("# 查看用户  [line:")
    assert rows[1].startswith("工作流 get_user（GET /users/{uid}）  app/users.py:7  [")
    assert function_outline(g, find_fn(g, "get_user").key)[0] == rows[1]


def test_a_branch_that_only_assigns_gets_a_line(tmp_path):
    """没有节点、只改变量的分支也是业务规则：判断（或折叠成的步骤）下面写一行，说它赋了什么。"""
    g = analyze_files(tmp_path, {"app/mod.py": LIB + """

def handler(x):
    if check(x):
        status = "skipped"
    elif load(x):
        status = "late"
    else:
        status = "pending"
    save(status)
"""})
    rows = function_outline(g, find_fn(g, "handler").key)
    assert "判断 check(x)" in rows[1] and "load" in rows[3] and "save" in rows[6]
    assert rows[2] == '    是 →（这条分支没有节点，只赋值：status = "skipped"）接着往下'
    assert rows[4:6] == ['      是 →（这条分支没有节点，只赋值：status = "late"）接着往下',
                         '      否 →（这条分支没有节点，只赋值：status = "pending"）接着往下']
