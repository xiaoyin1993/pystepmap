from conftest import analyze_files, fg_of, node_by
from pystepmap.config import ProjectConfig
from pystepmap.model import UnresolvedCall, from_json, parse_key, to_json
from pystepmap.titles import clean, comment_above

SRC = '''
async def load(x):
    """取回邮件原文:优先读 Redis"""
async def save(x): ...
async def handler(x):
    """处理一封邮件"""
    logger.info("start")
    raw = await load(x)
    # 原文取不到
    if raw is None:
        return {"status": "skipped", "reason": "raw_unavailable"}
    if x.debug:
        logger.info("debug")
    async with Session() as s:
        await save(raw)
    return {"status": "ok"}
'''
CFG = ProjectConfig(outcome_kinds={"status": {"skipped": "skipped", "failed": "failed"}})


def _edges(g):
    return {(e.src, e.dst, e.kind) for e in g.edges}


def test_linear_branch_and_folding(tmp_path):
    g = fg_of(tmp_path, SRC, "handler", CFG)
    assert [n.type for n in g.nodes] == ["start", "step", "cond", "end", "step", "end", "end"]
    load_n, cond, skip, save_n, ok, uncaught = g.nodes[1:]
    assert (load_n.title, load_n.subtitle) == ("取回邮件原文:优先读 Redis", "load")
    assert (cond.title, cond.subtitle) == ("原文取不到", "raw is None")
    assert (skip.end_kind, skip.fields) == ("skipped", {"status": "skipped", "reason": "raw_unavailable"})
    assert (save_n.subtitle, save_n.line) == ("save", 15)  # async with 不出节点，里面的语句接在主线上
    assert ok.end_kind == "ok"
    assert (uncaught.line, uncaught.end_kind, uncaught.title) == (0, "exception", "未捕获异常退出")
    assert _edges(g) == {
        ("n1", "n2", "next"), ("n2", "n3", "next"), ("n3", "n4", "yes"), ("n3", "n5", "no"), ("n5", "n6", "next")}
    assert g.title == "处理一封邮件"


def test_condition_with_call_but_empty_branches_becomes_step(tmp_path):
    src = '''
async def is_dup(x): ...
async def h(x):
    if await is_dup(x):
        logger.info("dup")
'''
    g = fg_of(tmp_path, src, "h")
    assert [n.type for n in g.nodes] == ["start", "step", "end", "end"]
    step = node_by(g, type="step")
    assert (step.subtitle, step.line, step.end_line) == ("is_dup", 4, 4)


def test_return_with_call_emits_step_then_end(tmp_path):
    src = '''
async def finish(x): ...
async def h(x):
    return await finish(x)
'''
    g = fg_of(tmp_path, src, "h")
    assert [n.type for n in g.nodes] == ["start", "step", "end", "end"]
    step, end = node_by(g, type="step"), node_by(g, type="end", line=4)
    assert step.calls[0].name == "finish" and step.line == end.line == 4
    assert (step.id, end.id, "next") in _edges(g)
    assert (end.title, end.end_kind) == ("返回 await finish(x)", "ok")


def test_implicit_end_when_falling_off(tmp_path):
    src = '''
def work(): ...
def h():
    work()
'''
    g = fg_of(tmp_path, src, "h")
    work_n, last = g.nodes[-3], g.nodes[-2]  # 最后一个是未捕获异常退出
    assert (last.type, last.line, last.title, last.end_kind) == ("end", 0, "执行到函数末尾", "ok")
    assert (work_n.id, last.id, "next") in _edges(g)


def test_calls_in_comprehension_walrus_match_are_kept(tmp_path):
    src = '''
async def a(x): ...
async def b(x): ...
async def c(x): ...
def deco(v): return lambda f: f
@deco(a(1))
async def h(xs, cmd):
    rows = [await a(x) for x in xs]
    if (r := await b(cmd)) is None:
        return None
    match cmd:
        case "go":
            await c(r)
'''
    g = fg_of(tmp_path, src, "h")
    assert [n.subtitle for n in g.nodes if n.type == "step"] == ["a", "c"]
    assert [n.subtitle for n in g.nodes if n.type == "cond"] == ["(r := await b(cmd)) is None", "match cmd"]
    assert node_by(g, subtitle="(r := await b(cmd)) is None").calls[0].name == "b"
    assert sorted(parse_key(k)[1] for k in g.call_targets) == ["a", "b", "c"]


def test_outcome_helper_fields(tmp_path):
    src = '''
from pystepkit import outcome
def h(x):
    return outcome("skipped", title="已放行", reason="whitelisted")
'''
    g = fg_of(tmp_path, src, "h")
    assert [n.type for n in g.nodes] == ["start", "end", "end"]  # outcome(...) 本身不是步骤
    end = node_by(g, type="end", line=4)
    assert (end.end_kind, end.title) == ("skipped", "已放行")
    assert end.fields == {"kind": "skipped", "reason": "whitelisted"}


def test_outcome_kind_does_not_depend_on_config(tmp_path):
    src = '''
from pystepkit import outcome
def h(x, k):
    if x == 1:
        return outcome("skipped", reason="x")
    if x == 2:
        return outcome(reason="y")
    if x == 3:
        return outcome(k, status="failed")
    return outcome(kind=k)
'''
    g = fg_of(tmp_path, src, "h", CFG)  # CFG.outcome_kinds 里没有 "kind"
    assert [n.end_kind for n in g.nodes if n.type == "end"] == ["skipped", "ok", "failed", "ok", "exception"]


def test_outcome_helper_must_come_from_pystepkit(tmp_path):
    """只有从 pystepkit 导入的 outcome 是结局辅助函数；别处的同名函数是普通调用。"""
    src = '''
import pystepkit as fk
from app.results import outcome
def h(x):
    if x:
        return fk.outcome("skipped", title="跳过")
    return outcome("failed", title="不是 pystepkit 的")
'''
    g = fg_of(tmp_path, src, "h")
    ends = [(n.title, n.end_kind) for n in g.nodes if n.type == "end" and n.line]
    assert ends == [("跳过", "skipped"), ('返回 outcome("failed", title="不是 pystepkit 的")', "ok")]


def test_condition_marker_title(tmp_path):
    src = '''
from pystepkit import condition
@condition(title="余额充足？")
def has_balance(ctx): ...
def pay(ctx): ...
def wf(ctx):
    if has_balance(ctx):
        pay(ctx)
'''
    g = fg_of(tmp_path, src, "wf")
    cond = node_by(g, type="cond")
    assert (cond.title, cond.subtitle) == ("余额充足？", "has_balance(ctx)")
    assert [c.name for c in cond.calls] == ["has_balance"]


def test_dispatch_noise_functions_and_call_targets(tmp_path):
    src = '''
@broker.task()
async def notify(x): ...
def get_settings(): ...
def kicker(x): ...
def manual_kicker(t): ...
def other(): ...
async def h(x):
    cfg = get_settings()
    if x.debug:
        get_settings()
    await notify.kiq(x)
    await kicker(x).kiq()
    await manual_kicker(notify).kiq(x)
    def inner():
        other()
'''
    g = fg_of(tmp_path, src, "h", ProjectConfig(noise_functions=["get_settings"]))
    steps = [n for n in g.nodes if n.type == "step"]
    assert [[(c.name, c.is_dispatch, c.resolution) for c in n.calls] for n in steps] == [
        [("派发 notify", True, "exact")], [("派发 未知任务", True, "unresolved")],
        [("派发 notify", True, "exact")]]  # 接收者链上的 kicker(x) / manual_kicker(notify) 不单独列出
    assert parse_key(steps[0].calls[0].target)[1] == "notify" and steps[1].calls[0].target is None
    assert [n.subtitle for n in steps] == ["派发 notify", "派发 未知任务", "派发 notify"]
    # 噪音名单里的、被折叠掉的、派发接收者链上的调用都计入；派发目标和内部函数里的调用不计入
    assert [parse_key(k)[1] for k in g.call_targets] == ["get_settings", "kicker", "manual_kicker"]


def test_dispatch_step_title(tmp_path):
    src = '''
from pystepkit import workflow
@broker.task()
async def notify(x):
    """发送通知"""
@workflow(title="账户转账")
async def transfer(x): ...
@broker.task()
async def plain(x): ...
async def h(x, t):
    # 上方注释不覆盖派发标题
    await notify.kiq(x)
    await transfer.kiq(x)
    await plain.kiq(x)
    await t.kiq(x)
'''
    g = fg_of(tmp_path, src, "h")
    steps = [n for n in g.nodes if n.type == "step"]
    assert [(n.title, n.subtitle) for n in steps] == [
        ("派发：发送通知", "派发 notify"), ("派发：账户转账", "派发 transfer"),
        ("派发：plain", "派发 plain"), ("派发 未知任务", "派发 未知任务")]


def test_guessed_dispatch_title_does_not_use_the_task_docstring(tmp_path):
    """只按任务名对应上的派发（guessed）：标题是"派发 任务名"（和副标题相同），不取任务的文档注释或标记标题；
    exact 的派发照旧取。"""
    src = '''
from pystepkit import workflow
@broker.task()
async def send_email(x):
    """发送邮件"""
@workflow(title="账户转账")
async def transfer(x): ...
async def h(reg, x):
    await reg.send_email.kiq(1)
    await reg.transfer.kiq(x)
    await send_email.kiq(2)
'''
    g = fg_of(tmp_path, src, "h")
    steps = [n for n in g.nodes if n.type == "step"]
    assert [(n.title, n.subtitle, [c.resolution for c in n.calls]) for n in steps] == [
        ("派发 send_email", "派发 send_email", ["guessed"]),
        ("派发 transfer", "派发 transfer", ["guessed"]),
        ("派发：发送邮件", "派发 send_email", ["exact"])]


SAME_NAMED_TASKS = {
    "app/__init__.py": "",
    "app/broker.py": "from taskiq import InMemoryBroker\nbroker = InMemoryBroker()\n",
    "app/tasks.py": """
        from app.broker import broker
        @broker.task
        async def notify(x): ...
        @broker.task
        async def send_receipt(x):
            await notify.kiq(x)
    """,
    "app/other/__init__.py": "",
    "app/other/tasks2.py": """
        from app.broker import broker
        @broker.task
        async def notify(x): ...
        @broker.task
        async def ping(x):
            await notify.kiq(x)
    """,
    "app/cross.py": """
        from app.other import tasks2
        from app.tasks import notify as main_notify
        async def go(x):
            await tasks2.notify.kiq(x)
            await main_notify.kiq(x)
    """,
    "app/once.py": "from app.broker import broker\n@broker.task\nasync def only_once(): ...\n",
    "app/guess.py": """
        async def unknown():
            await notify.kiq(1)
            await only_once.kiq(2)
    """,
}


def test_dispatch_target_is_resolved_not_matched_by_name(tmp_path):
    """同名的任务：派发先按导入和模块级符号解析（exact）；解析不了时只在恰好一个任务叫这个名字时按名字猜（guessed），
    同名的有几个就记为无法判断（计入未解析）。"""
    g = analyze_files(tmp_path, SAME_NAMED_TASKS)
    main, other = "app/tasks.py::notify@4", "app/other/tasks2.py::notify@4"

    def dispatches(qualname, file):
        fg = next(f for f in g.functions.values() if f.qualname == qualname and f.file == file)
        return [(c.target, c.resolution) for n in fg.nodes for c in n.calls if c.is_dispatch], fg

    assert dispatches("send_receipt", "app/tasks.py")[0] == [(main, "exact")]
    assert dispatches("ping", "app/other/tasks2.py")[0] == [(other, "exact")]
    assert dispatches("go", "app/cross.py")[0] == [(other, "exact"), (main, "exact")]
    got, fg = dispatches("unknown", "app/guess.py")
    assert got == [(None, "unresolved"), ("app/once.py::only_once@3", "guessed")]
    assert [u.name for u in fg.unresolved] == ["派发 notify"]


def test_function_run_in_a_thread_is_a_call_to_it(tmp_path):
    """asyncio.to_thread(f, …) 和 <loop>.run_in_executor(执行器, f, …) 等于调用 f：目标、call_targets、标题、解析方式都按 f。"""
    src = '''
import asyncio
import asyncio as _asyncio
from asyncio import to_thread
def load(x):
    """读取配置"""
def save(x): ...
@DBOS.workflow()
def child(x): ...
async def h(x, loop, obj):
    a = await asyncio.to_thread(load, x)
    await _asyncio.to_thread(save, a)
    await to_thread(obj.mystery, x)
    await loop.run_in_executor(None, load, x)
    await asyncio.get_running_loop().run_in_executor(None, save, x)
    await asyncio.to_thread(DBOS.start_workflow, child, x)
    await asyncio.to_thread(print, x)
    await asyncio.to_thread(lambda: save(x))
'''
    g = fg_of(tmp_path, src, "h")
    steps = [n for n in g.nodes if n.type == "step"]
    assert [(n.title, [(c.name, c.resolution) for c in n.calls]) for n in steps] == [
        ("读取配置", [("load", "exact")]),
        ("save", [("save", "exact")]),
        ("obj.mystery", [("obj.mystery", "unresolved")]),
        ("读取配置", [("load", "exact")]),
        ("save", [("save", "exact")]),
        ("派发：child", [("派发 child", "exact")]),  # 在线程里启动工作流也是派发
        ("asyncio.to_thread", [("asyncio.to_thread", "external")]),  # lambda 不展开，体里的 save 算本函数的调用
    ]  # to_thread(print, …)：被执行的是内置函数，算噪音
    assert [parse_key(k)[1] for k in g.call_targets] == ["load", "save"]
    assert [u.name for u in g.unresolved] == ["obj.mystery"]


def test_project_function_shadowing_builtin_is_a_step(tmp_path):
    src = '''
async def filter(x): ...
async def h(x):
    y = await filter(x)
    print(y)
'''
    g = fg_of(tmp_path, src, "h")
    assert [n.subtitle for n in g.nodes if n.type == "step"] == ["filter"]  # print 仍是噪音


def test_match_guard_failure_falls_through(tmp_path):
    src = '''
def ok(c): ...
def run(c): ...
def other(c): ...
def done(): ...
def h(cmd):
    match cmd:
        case "go" if ok(cmd):
            run(cmd)
        case "stop":
            other(cmd)
    done()
def h2(cmd):
    match cmd:
        case "go" if ok(cmd):
            run(cmd)
        case _:
            pass
'''
    # 守卫不出节点：守卫不成立时试后面的分支、都不匹配时越过 match，都是判断节点自己的出边
    g = fg_of(tmp_path, src, "h")
    cond, run_n, other_n, done_n = (node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in (
        "run", "other", "done")))
    E = {(e.src, e.dst, e.kind, e.label) for e in g.edges}
    assert {("n1", cond.id, "next", ""), (cond.id, run_n.id, "case", '"go" if ok(cmd)'),
            (cond.id, other_n.id, "case", '"stop"'), (cond.id, done_n.id, "case", "都不匹配")} <= E
    assert not [e for e in g.edges if e.src == run_n.id and e.dst == other_n.id]
    assert [c.name for c in cond.calls] == ["ok"]
    g2 = fg_of(tmp_path, src, "h2")
    cond2, run2, end2 = node_by(g2, type="cond"), node_by(g2, subtitle="run"), g2.nodes[-2]
    assert end2.title == "执行到函数末尾"
    assert {(e.dst, e.kind, e.label) for e in g2.edges if e.src == cond2.id} == {
        (run2.id, "case", '"go" if ok(cmd)'), (end2.id, "case", "_")}


def test_elif_chain_and_end_titles(tmp_path):
    src = '''
def a(): ...
def h(x):
    if x > 1:
        a()
    elif x < 0:
        return
    else:
        return {"status": "done", "extra": 1}
    return x
'''
    g = fg_of(tmp_path, src, "h")
    assert [(n.type, n.title) for n in g.nodes] == [
        ("start", "h"), ("cond", "x > 1"), ("step", "a"), ("cond", "x < 0"),
        ("end", "返回 None"), ("end", '返回 {"status": "done", "extra": 1}'), ("end", "返回 x"), ("end", "未捕获异常退出")]
    assert g.nodes[5].fields == {"status": "done"}
    assert _edges(g) == {("n1", "n2", "next"), ("n2", "n3", "yes"), ("n2", "n4", "no"),
                         ("n4", "n5", "yes"), ("n4", "n6", "no"), ("n3", "n7", "next")}


def test_with_header_call_and_match_cases(tmp_path):
    src = '''
async def lease(x): ...
def go(): ...
def stop(): ...
def done(): ...
async def h(cmd):
    async with lease(cmd):
        match cmd:
            case "go":
                go()
            case "stop":
                stop()
            case "noop":
                pass
    done()
async def total(cmd):
    match cmd:
        case "go":
            go()
        case _:
            stop()
'''
    g = fg_of(tmp_path, src, "h")
    lease_n, cond, go_n, stop_n, done_n = (node_by(g, subtitle=s) for s in ("lease", "match cmd", "go", "stop", "done"))
    assert (lease_n.line, lease_n.end_line) == (7, 7)
    assert {(e.src, e.dst, e.kind, e.label) for e in g.edges} == {
        ("n1", lease_n.id, "next", ""), (lease_n.id, cond.id, "next", ""),
        (cond.id, go_n.id, "case", '"go"'), (cond.id, stop_n.id, "case", '"stop"'),
        (cond.id, done_n.id, "case", '"noop"'), (cond.id, done_n.id, "case", "都不匹配"),
        (go_n.id, done_n.id, "next", ""), (stop_n.id, done_n.id, "next", ""), (done_n.id, g.nodes[-2].id, "next", "")}
    t = fg_of(tmp_path, src, "total")
    assert [(e.src, e.dst, e.kind, e.label) for e in t.edges] == [
        ("n1", "n2", "next", ""), ("n2", "n3", "case", '"go"'), ("n2", "n4", "case", "_"), ("n3", "n5", "next", ""),
        ("n4", "n5", "next", "")]


def test_titles_helpers():
    lines = ["def f():", "    #", "    # 第一行", "    # 第二行", "    x()", "", "    y()"]
    assert comment_above(lines, 5) == "第一行"
    assert comment_above(lines, 7) == "" and comment_above(lines, 1) == ""
    assert clean("  **加粗**  `code`\n   换行 ") == "加粗 code 换行"
    assert clean("一二三四五", 4) == "一二三…" and clean("一二三四", 4) == "一二三四"


def test_constructors_are_call_targets_not_steps(tmp_path):
    src = '''
class Svc:
    def __init__(self, x): ...
    def __call__(self, y): ...
def load(x): ...
def h(x):
    s = Svc(x)
    v = Svc(load(x))
    s(v)
    return Svc(v)
'''
    g = fg_of(tmp_path, src, "h")
    # 构造对象不是业务步骤：只进 call_targets；同一句里别的有意义调用照常成步骤；调用实例（__call__）是普通步骤
    assert [(n.type, n.line, n.subtitle) for n in g.nodes] == [
        ("start", 6, ""), ("step", 8, "load"), ("step", 9, "s"), ("end", 10, ""), ("end", 0, "")]
    assert [parse_key(k)[1] for k in g.call_targets] == ["Svc.__init__", "load", "Svc.__call__"]
    assert node_by(g, line=10).passthrough is None  # 返回新构造的对象不是转交返回
    assert g.unresolved == []


def test_unresolved_calls_are_listed(tmp_path):
    src = '''
def known(): ...
async def h(p, xs):
    known()
    p.mystery()
    await p.other()
    xs.append(1)
    p.mystery(); p.mystery()
    f = lambda: p.inside() or known()
    def inner():
        p.nested()
    return len(xs)
'''
    g = fg_of(tmp_path, src, "h")
    # 不论是否被 await、是否留在节点上；按 (行, 名字) 去重。lambda 体和内部函数体在自己的帧里执行，不算
    assert g.unresolved == [UnresolvedCall(5, "p.mystery"), UnresolvedCall(6, "p.other"), UnresolvedCall(7, "xs.append"),
                            UnresolvedCall(8, "p.mystery")]
    assert [parse_key(k)[1] for k in g.call_targets] == ["known"]  # lambda 里解析到的调用照旧进 call_targets


def test_nested_definition_headers_run_in_the_enclosing_frame(tmp_path):
    src = '''
def deco(f): return f
def register(name): return deco
def default(): return 1
def base(): return object
def outer(reg):
    @deco
    @register("x")
    def inner(a=default(), *, b=reg.fallback()):
        return later()
    class Local(base()):
        def m(self, c=default()): ...
    @reg.hook
    def other(): ...
    return inner
'''
    g = fg_of(tmp_path, src, "outer")
    # 嵌套定义的装饰器（含 @deco 本身的应用）、参数默认值、类的基类在外层帧执行：进外层的 call_targets / unresolved，
    # 不进节点；嵌套定义的体（later()）和类体里方法的默认值（在类体的帧里执行）不算
    assert [parse_key(k)[1] for k in g.call_targets] == ["deco", "register", "default", "base"]
    assert g.unresolved == [UnresolvedCall(9, "reg.fallback"), UnresolvedCall(13, "reg.hook")]
    assert [(n.type, n.line) for n in g.nodes] == [("start", 6), ("end", 15), ("end", 0)]


ACCESSOR_SRC = '''
def get_redis():
    """进程内单例的 async Redis 客户端"""
def lock_key(x):
    """分析锁的键"""
async def load_user(x):
    """读取用户"""
async def release(x):
    await get_redis().delete(lock_key(x))
    # 删掉分析锁
    await get_redis().delete(lock_key(x))
    name = (await load_user(x)).get("name")
    await get_redis().aclose()
'''


def test_step_title_does_not_borrow_the_docstring_of_an_accessor_on_the_receiver(tmp_path):
    """await 取对象().方法(参数)：做事的是目标不明的那个方法，取对象的函数、算参数的函数的文档注释不当标题。"""
    g = fg_of(tmp_path, ACCESSOR_SRC, "release")
    first, commented, user, close = [n for n in g.nodes if n.type == "step"]
    assert first.title == "get_redis.delete + get_redis + lock_key"  # 没有注释：调用的显示名
    assert (commented.title, commented.subtitle) == ("删掉分析锁", "get_redis.delete + get_redis + lock_key")
    assert user.title == "读取用户"  # 步骤结果上取值（.get 不是有意义的调用）：照旧用被调函数的文档注释
    assert close.title == "get_redis.aclose + get_redis"


QUIET_SRC = '''
async def is_denied(x): ...
async def is_watched(x): ...
async def register(x, status, reason): ...
async def handler(x):
    if await is_denied(x):
        status, reason = "skipped", "denylisted"
    elif not await is_watched(x):
        status, reason = "skipped", "unsubscribed"
    else:
        status: str = "pending"
        reason = None
    if x.debug:
        logger.info("debug")
    await register(x, status, reason)
'''


def test_assignments_in_a_folded_branch_are_kept_on_the_node(tmp_path):
    """分支里没有节点、但改了变量：图上看不到这条分支，赋值记在判断（或折叠成的步骤）的 quiet 上。只写日志的不记。"""
    g = fg_of(tmp_path, QUIET_SRC, "handler")
    assert [n.type for n in g.nodes] == ["start", "cond", "step", "step", "end", "end"]
    cond, watched, register = g.nodes[1:4]
    assert cond.quiet == {"yes": 'status, reason = "skipped", "denylisted"'}
    assert watched.quiet == {"yes": 'status, reason = "skipped", "unsubscribed"', "no": 'status: str = "pending"；reason = None'}
    assert register.quiet == {} and all(n.quiet == {} for n in fg_of(tmp_path, SRC, "handler", CFG).nodes)
    back = from_json(to_json(analyze_files(tmp_path / "p", {"app/mod.py": QUIET_SRC})))
    assert [n.quiet for n in next(f for f in back.functions.values() if f.qualname == "handler").nodes[1:3]] == [cond.quiet, watched.quiet]
