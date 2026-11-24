from conftest import analyze_files, fg_of, node_by
from pystepmap.model import parse_key
from pystepmap.selfcheck import run_selfcheck


def _edges(g):
    return {(e.src, e.dst, e.kind) for e in g.edges}


def _incoming(g, nid):
    return [e for e in g.edges if e.dst == nid]


def _reaches(g, src, dst):
    seen, stack = {src}, [src]
    while stack:
        cur = stack.pop()
        for e in g.edges:
            if e.src == cur and e.dst not in seen:
                seen.add(e.dst)
                stack.append(e.dst)
    return dst in seen


def test_for_with_break_and_continue(tmp_path):
    src = '''
def wait(i): ...
def start(i): ...
def done(): ...
def wf(n):
    for i in range(n):
        w = wait(i)
        if w != "start":
            break
        if w == "skip":
            continue
        start(i)
    done()
'''
    g = fg_of(tmp_path, src, "wf")
    loop, wait_n, c1, c2, start_n, done_n = [node_by(g, line=l) for l in (6, 7, 8, 10, 12, 13)]
    E = _edges(g)
    assert (loop.id, wait_n.id, "loop_each") in E and (start_n.id, loop.id, "loop_back") in E
    assert (c1.id, done_n.id, "break") in E            # 满足条件直接跳出循环
    assert (c2.id, loop.id, "loop_back") in E          # continue
    assert (loop.id, done_n.id, "loop_exit") in E
    assert (loop.type, loop.subtitle, loop.end_line) == ("loop", "for i in range(n)", 6)
    labels = {(e.src, e.dst, e.kind): e.label for e in g.edges}
    assert labels[(c1.id, done_n.id, "break")] == "是"   # 原来的"是"挪到 label 上
    assert labels[(c2.id, loop.id, "loop_back")] == "是"
    assert (c1.id, c2.id, "no") in E and (c2.id, start_n.id, "no") in E


def test_while_true_poll_loop_is_drawn_when_header_has_call(tmp_path):
    src = '''
import time
def poll(j): ...
def work(): ...
def wf(j):
    while (s := poll(j)) == "running":
        time.sleep(1)
    return s
def bare(xs):
    for x in xs:
        continue
    while True:
        break
    work()
def spin():
    while True:
        work()
'''
    g = fg_of(tmp_path, src, "wf")
    loop, ret = node_by(g, type="loop"), node_by(g, line=8)
    assert loop.calls[0].name == "poll"
    assert (loop.subtitle, loop.line, loop.end_line) == ('while (s := poll(j)) == "running"', 6, 6)
    assert {(loop.id, loop.id, "loop_back"), (loop.id, ret.id, "loop_exit")} <= _edges(g)
    # 循环体里只有 break / continue、循环头里也没有有意义的调用：循环不画
    b = fg_of(tmp_path, src, "bare")
    assert [n.type for n in b.nodes] == ["start", "step", "end", "end"]
    # while True 也有 loop_exit（多一条走不到的连线可以接受，少一条真实路径不行）
    s = fg_of(tmp_path, src, "spin")
    loop, work_n, fell = node_by(s, type="loop"), node_by(s, type="step"), s.nodes[-2]
    assert fell.title == "执行到函数末尾"
    assert {(loop.id, work_n.id, "loop_each"), (work_n.id, loop.id, "loop_back"),
            (loop.id, fell.id, "loop_exit")} <= _edges(s)


def test_nested_loop_keeps_inner_loop_exit(tmp_path):
    src = '''
def rows(): ...
def cols(r): ...
def put(c): ...
def wf():
    for r in rows():
        for c in cols(r):
            put(c)
def after_loop(xs):
    for x in xs:
        put(x)
    try:
        n = int(x)
    except ValueError:
        put(None)
'''
    g = fg_of(tmp_path, src, "wf")
    outer, inner, put_n = (node_by(g, line=l) for l in (6, 7, 8))
    # 内层循环是外层循环体的最后一句：内层的 loop_exit 回到外层循环节点，仍是 loop_exit
    assert {(outer.id, inner.id, "loop_each"), (inner.id, put_n.id, "loop_each"), (put_n.id, inner.id, "loop_back"),
            (inner.id, outer.id, "loop_exit"), (outer.id, g.nodes[-2].id, "loop_exit")} <= _edges(g)
    # try 体没有节点、前驱是循环的 loop_exit：进入捕获节点的连线仍是 raise，循环自己的 loop_exit 照常往下走
    a = fg_of(tmp_path, src, "after_loop")
    loop, catch, fell = node_by(a, type="loop"), node_by(a, type="catch"), a.nodes[-2]
    assert {(loop.id, catch.id, "raise"), (loop.id, fell.id, "loop_exit")} <= _edges(a)
    assert (loop.id, catch.id, "loop_exit") not in _edges(a)


def test_try_except_finally(tmp_path):
    src = '''
def work(): ...
def recover(e): ...
def release(): ...
def wf():
    try:
        work()
        return "ok"
    except ValueError as e:
        recover(e)
        return "failed"
    except KeyError:
        logger.warning("ignored")
    finally:
        release()
'''
    g = fg_of(tmp_path, src, "wf")
    work, ret_ok, catch, rec, ret_failed, rel = [node_by(g, line=l) for l in (7, 8, 9, 10, 11, 15)]
    E = _edges(g)
    assert catch.type == "catch" and catch.subtitle == "ValueError"
    assert (work.id, catch.id, "raise") in E
    assert (ret_ok.id, rel.id, "finally") in E and (ret_failed.id, rel.id, "finally") in E
    assert [n for n in g.nodes if n.type == "catch"] == [catch]      # 只写日志的 except 不画
    assert (catch.id, rec.id, "next") in E and (rec.id, ret_failed.id, "next") in E
    # 只写日志的 except 吞掉异常后照常往下走（经过 finally）：这条路径不能丢
    assert (work.id, rel.id, "raise") in E
    fell = g.nodes[-2]
    assert fell.title == "执行到函数末尾" and (rel.id, fell.id, "next") in E
    assert len(g.edges) == len({(e.src, e.dst, e.kind) for e in g.edges})   # 同一对节点之间同一类型的连线只有一条


def test_conditional_calls(tmp_path):
    src = '''
def fallback(r): ...
def policy_check(v): ...
def first(a): ...
def deep(a): ...
def other(a): ...
def h(r, risky, a, xs):
    v = r.details if r.details is not None else fallback(r)
    hit = risky and policy_check(v)
    x = first(a)
    y = a or (risky and (deep(a) if a else other(a)))
    ok = first(a) and deep(a) and other(a)
    z = [first(i) for i in xs if policy_check(i)]
'''
    g = fg_of(tmp_path, src, "h")
    calls = {n.line: [(c.name, c.conditional) for c in n.calls] for n in g.nodes if n.type == "step"}
    assert calls == {
        8: [("fallback", True)],
        9: [("policy_check", True)],
        10: [("first", False)],
        11: [("deep", True), ("other", True)],                       # 嵌套任意深度
        12: [("first", False), ("deep", True), ("other", True)],      # 连写的 and / or
        13: [("first", False), ("policy_check", False)],              # 推导式的元素和条件不算
    }


def test_passthrough_return(tmp_path):
    src = '''
async def finish(x): ...
def plain(x): ...
async def wf(x):
    async def short():
        if x:
            return 1
        return 2
    judged = await short()
    if judged == 3:
        return judged
    return await finish(x)
def direct(x):
    return plain(x)
def twice(x):
    y = plain(x)
    if x:
        y = plain(y)
    return y
def param(y):
    if y is None:
        y = plain(1)
    return y
def ext(x):
    v = len(x)
    return v
def inner_assign(x):
    def f():
        v = 1
    v = plain(x)
    return v
'''
    key = {name: fg_of(tmp_path, src, name).key for name in ("finish", "plain", "wf.<locals>.short")}
    g = fg_of(tmp_path, src, "wf")
    assert node_by(g, type="end", line=11).passthrough == key["wf.<locals>.short"]
    assert node_by(g, type="end", line=12).passthrough == key["finish"]
    assert all(n.passthrough is None for n in fg_of(tmp_path, src, "wf.<locals>.short").nodes)
    assert node_by(fg_of(tmp_path, src, "direct"), type="end", line=14).passthrough == key["plain"]
    # 内部函数里的赋值不算本函数的赋值
    assert node_by(fg_of(tmp_path, src, "inner_assign"), type="end", line=31).passthrough == key["plain"]
    # 赋值两次、是参数、右边不是项目内函数：都不算转交
    for name, line in (("twice", 19), ("param", 23), ("ext", 26)):
        assert node_by(fg_of(tmp_path, src, name), type="end", line=line).passthrough is None, name


def test_raise_and_uncaught_exception_end(tmp_path):
    src = '''
class EngineError(Exception): ...
def check(x): ...
@broker.task(retry_on_error=True)
async def job(x):
    if check(x):
        raise EngineError("x")
    try:
        check(x)
    except ValueError:
        raise
def plain(x):
    def inner():
        check(x)
    inner()
async def describe(x): ...
async def job2(x):
    raise EngineError(await describe(x)) from None
'''
    g = fg_of(tmp_path, src, "job")
    err, rer = node_by(g, line=7), node_by(g, line=11)
    assert (err.type, err.end_kind) == ("end", "failed") and err.title.startswith("抛出 EngineError")
    assert (rer.type, rer.end_kind, rer.title) == ("end", "failed", "抛出 原异常")
    assert node_by(g, type="catch").subtitle == "ValueError"
    last = g.nodes[-1]
    assert (last.type, last.line, last.end_kind, last.title) == ("end", 0, "exception", "未捕获异常退出（任务会重试）")
    assert _incoming(g, last.id) == []
    for name in ("plain", "plain.<locals>.inner"):
        p = fg_of(tmp_path, src, name)
        assert [(n.type, n.line, n.title, n.end_kind) for n in p.nodes[-2:]] == [
            ("end", 0, "执行到函数末尾", "ok"), ("end", 0, "未捕获异常退出", "exception")]
        assert _incoming(p, p.nodes[-1].id) == []
    # 异常表达式里有有意义的调用：先出步骤再出结局；标题只取异常表达式，不含 from 部分
    j = fg_of(tmp_path, src, "job2")
    assert [(n.type, n.line, n.title) for n in j.nodes] == [
        ("start", 17, "job2"), ("step", 18, "describe"), ("end", 18, "抛出 EngineError(await describe(x))"),
        ("end", 0, "未捕获异常退出")]
    assert ("n2", "n3", "next") in _edges(j)


def test_loop_else_try_else_and_break_inside_try(tmp_path):
    src = '''
def a(x): ...
def b(): ...
def c(): ...
def d(): ...
def wf(xs):
    for x in xs:
        if a(x):
            break
    else:
        b()
    c()
def tw():
    try:
        a(1)
    except ValueError:
        b()
    else:
        c()
    d()
def tb(xs):
    for x in xs:
        try:
            a(x)
            break
        finally:
            c()
    d()
'''
    g = fg_of(tmp_path, src, "wf")
    loop, cond, b_n, c_n = (node_by(g, line=l) for l in (7, 8, 11, 12))
    E = _edges(g)
    assert {(loop.id, cond.id, "loop_each"), (cond.id, c_n.id, "break"), (cond.id, loop.id, "loop_back"),
            (loop.id, b_n.id, "loop_exit"), (b_n.id, c_n.id, "next")} <= E
    assert not [e for e in g.edges if e.src == cond.id and e.dst == b_n.id]   # break 越过 else
    t = fg_of(tmp_path, src, "tw")
    a_n, catch, b_n, c_n, d_n = (node_by(t, line=l) for l in (15, 16, 17, 19, 20))
    assert _edges(t) >= {(a_n.id, catch.id, "raise"), (a_n.id, c_n.id, "next"), (catch.id, b_n.id, "next"),
                         (b_n.id, d_n.id, "next"), (c_n.id, d_n.id, "next")}
    assert not [e for e in t.edges if e.src == catch.id and e.dst == c_n.id]  # 进了 except 就不走 else
    # try / finally 里的 break 先经过 finally，再从 finally 的出口跳出循环
    k = fg_of(tmp_path, src, "tb")
    loop, a_n, c_n, d_n = (node_by(k, line=l) for l in (22, 24, 27, 28))
    assert {(a_n.id, c_n.id, "finally"), (c_n.id, d_n.id, "break"), (a_n.id, c_n.id, "raise"),
            (c_n.id, loop.id, "loop_back"), (loop.id, d_n.id, "loop_exit")} <= _edges(k)
    assert not [e for e in k.edges if e.src == a_n.id and e.dst == d_n.id]   # 不再直连循环之后


def test_handler_reachable_from_every_entry_into_try_body(tmp_path):
    src = '''
def save(x): ...
def default(): ...
def rollback(): ...
def a(): ...
def b(): ...
def r(): ...
def nested(s):
    try:
        try:
            x = int(s)
        except ValueError:
            x = default()
        save(x)
    except KeyError:
        rollback()
def matched(cmd):
    try:
        match cmd:
            case 1:
                pass
            case 2:
                a()
        b()
    except ValueError:
        r()
def inner_returns(s):
    try:
        try:
            x = int(s)
        except ValueError:
            return default()
        save(x)
    except KeyError:
        rollback()
'''
    # try 体以"try 体没有节点"的内层 try 开头：第一个创建的节点是内层的捕获节点，不是所有路径的入口
    g = fg_of(tmp_path, src, "nested")
    inner, save_n, outer = node_by(g, line=12), node_by(g, line=14), node_by(g, line=15)
    assert (inner.type, outer.type) == ("catch", "catch")
    assert ("n1", save_n.id, "next") in _edges(g)                  # 不抛 ValueError：直接到 save
    assert _reaches(g, save_n.id, outer.id)                        # save 抛 KeyError → rollback
    assert {(save_n.id, outer.id, "raise"), (inner.id, outer.id, "raise")} <= _edges(g)
    # try 体以 match 开头：判断节点是 try 体的入口，异常连线从它画出；第一个分支为空、都不匹配的路径从判断节点直接到 b
    m = fg_of(tmp_path, src, "matched")
    cond, a_n, b_n, catch = node_by(m, line=19), node_by(m, line=23), node_by(m, line=24), node_by(m, type="catch")
    assert cond.type == "cond"
    assert {(e.src, e.dst, e.kind, e.label) for e in m.edges} >= {
        ("n1", cond.id, "next", ""), (cond.id, b_n.id, "case", "1"), (cond.id, a_n.id, "case", "2"),
        (cond.id, b_n.id, "case", "都不匹配"), (a_n.id, b_n.id, "next", "")}
    assert sorted((e.src, e.kind) for e in _incoming(m, catch.id)) == [(cond.id, "raise")]
    # 内层 except 以 return 结束：走内层 except 的路径到不了 save，内层捕获节点自己也要连到外层 except
    # （default() 抛 KeyError 时进外层 except）
    k = fg_of(tmp_path, src, "inner_returns")
    inner, save_n, outer = node_by(k, line=31), node_by(k, line=33), node_by(k, line=34)
    assert (inner.type, outer.type) == ("catch", "catch") and not _reaches(k, inner.id, save_n.id)
    assert _reaches(k, inner.id, outer.id) and _reaches(k, save_n.id, outer.id)


def test_paths_crossing_try_body_without_nodes_reach_handler(tmp_path):
    src = '''
def a(): ...
def b(): ...
def r(): ...
def tail(cmd):
    try:
        match cmd:
            case 2:
                a()
    except ValueError:
        r()
    b()
def brk(xs):
    for x in xs:
        try:
            match x:
                case 2:
                    a()
            break
        except ValueError:
            r()
def cont(xs):
    for x in xs:
        try:
            match x:
                case 2:
                    a()
            continue
        except ValueError:
            r()
def dead_first(xs):
    for x in xs:
        try:
            continue
            a()
        finally:
            r()
def tail_try(s):
    try:
        try:
            v = int(s)
        except KeyError:
            a()
    except ValueError:
        r()
    b()
def brk_try(xs):
    for x in xs:
        try:
            try:
                v = int(x)
            except KeyError:
                a()
            break
        except ValueError:
            r()
def cont_try(xs):
    for x in xs:
        try:
            try:
                v = int(x)
            except KeyError:
                a()
            continue
        except ValueError:
            r()
'''
    # try 体里第一个创建的节点是 continue 之后的死代码：它不是入口，finally 不能挂在它下面
    d = fg_of(tmp_path, src, "dead_first")
    assert _reaches(d, "n1", node_by(d, subtitle="r").id) and not _reaches(d, "n1", node_by(d, subtitle="a").id)
    # match 是判断节点：不匹配任何分支的路径也经过 try 体里的判断节点，异常连线只从这个入口画出
    for name in ("tail", "brk", "cont"):
        g = fg_of(tmp_path, src, name)
        cond, catch = node_by(g, type="cond"), node_by(g, subtitle="ValueError")
        assert sorted((e.src, e.kind) for e in _incoming(g, catch.id)) == [(cond.id, "raise")], name
    # 内层 try 的 try 体没有节点：不抛异常的路径没有经过 try 体里的节点就离开了外层 try 体（落到后面 / break /
    # continue），从它的前驱也连一条 raise 到 except；内层的捕获节点是入口，也连一条
    t = fg_of(tmp_path, src, "tail_try")
    inner, catch = node_by(t, subtitle="KeyError"), node_by(t, subtitle="ValueError")
    assert sorted((e.src, e.kind) for e in _incoming(t, catch.id)) == [("n1", "raise"), (inner.id, "raise")]
    for name in ("brk_try", "cont_try"):
        g = fg_of(tmp_path, src, name)
        loop, inner, catch = (node_by(g, type="loop"), node_by(g, subtitle="KeyError"),
                              node_by(g, subtitle="ValueError"))
        assert sorted((e.src, e.kind) for e in _incoming(g, catch.id)) == [
            (loop.id, "raise"), (inner.id, "raise")], name


def test_continue_crossing_try_body_is_kept_when_its_edge_is_deduplicated(tmp_path):
    src = '''
def a(): ...
def r(): ...
def caught(xs):
    for s in xs:
        try:
            a()
            continue
        except KeyError:
            logger.info("k")
        try:
            x = int(s)
            continue
        except ValueError:
            r()
def final(xs):
    for s in xs:
        try:
            a()
            continue
        except KeyError:
            logger.info("k")
        try:
            x = int(s)
            continue
        finally:
            r()
def swallowed(xs):
    for s in xs:
        try:
            a()
            continue
        except KeyError:
            logger.info("k")
        try:
            x = int(s)
            continue
        except ValueError:
            logger.info("v")
        r()
'''
    # a() 抛 KeyError（只写日志）→ 第二个 try：int(s) 抛 ValueError / 走 finally / 吞掉后往下走。
    # 吞掉异常的出边（raise）随 continue 连回循环时会被去重（a 已经连到循环节点），越过 try 体的路径仍要算上
    g = fg_of(tmp_path, src, "caught")
    a_n, catch, r_n = node_by(g, subtitle="a"), node_by(g, type="catch"), node_by(g, subtitle="r")
    assert _reaches(g, "n1", a_n.id) and _reaches(g, a_n.id, catch.id) and _reaches(g, a_n.id, r_n.id)
    for name in ("final", "swallowed"):
        g = fg_of(tmp_path, src, name)
        a_n, r_n = node_by(g, subtitle="a"), node_by(g, subtitle="r")
        assert _reaches(g, "n1", a_n.id) and _reaches(g, a_n.id, r_n.id), name


def test_log_only_handler_keeps_the_path_after_try(tmp_path):
    src = '''
def load(x): ...
def after(): ...
def close(): ...
def parse(s):
    try:
        return load(s)
    except ValueError:
        logger.warning("bad")
    return None
def cleanup(s):
    try:
        load(s)
        after()
    except Exception:
        logger.exception("x")
    finally:
        close()
def single(s):
    try:
        load(s)
    except ValueError:
        pass
    after()
def ret():
    try:
        return 1
    finally:
        close()
'''
    g = fg_of(tmp_path, src, "parse")
    load_n, ret_none = node_by(g, type="step"), node_by(g, line=10)
    assert (load_n.id, ret_none.id, "raise") in _edges(g)   # 不画 except，但异常被吞掉后的路径还在
    assert [n.title for n in g.nodes if n.line == 0] == ["未捕获异常退出"]   # 所有路径都已结束，不补"执行到函数末尾"
    c = fg_of(tmp_path, src, "cleanup")
    load_n, after_n, close_n = (node_by(c, line=l) for l in (13, 14, 18))
    assert not [n for n in c.nodes if n.type == "catch"]
    assert [(e.src, e.kind) for e in _incoming(c, close_n.id)] == [(after_n.id, "next"), (load_n.id, "raise")]
    # 同一起点已经有普通连线接到同一个节点时，异常连线照样画：它标出异常的落脚点
    s = fg_of(tmp_path, src, "single")
    load_n, after_n = (node_by(s, line=l) for l in (21, 24))
    assert [(e.src, e.kind) for e in _incoming(s, after_n.id)] == [(load_n.id, "next"), (load_n.id, "raise")]
    r = fg_of(tmp_path, src, "ret")
    end_n, close_n = (node_by(r, line=l) for l in (27, 29))
    # return 是 try 体的入口：它到 finally 既有异常连线（求返回值时可能抛出），也有 return 经过 finally 的连线
    assert [(e.src, e.kind) for e in _incoming(r, close_n.id)] == [(end_n.id, "raise"), (end_n.id, "finally")]


def test_dead_code_after_return_gets_no_implicit_end(tmp_path):
    src = '''
def a(): ...
def h(x):
    return 1
    a()
'''
    g = fg_of(tmp_path, src, "h")
    assert [(n.type, n.title) for n in g.nodes] == [
        ("start", "h"), ("end", "返回 1"), ("step", "a"), ("end", "未捕获异常退出")]
    assert _incoming(g, node_by(g, type="step").id) == []


def test_call_targets_cover_loops_handlers_finally_and_conditionals(tmp_path):
    src = '''
def it(): ...
def poll(): ...
def on_err(): ...
def fin(): ...
def alt(): ...
def h(y):
    for x in it():
        pass
    while poll():
        break
    try:
        pass
    except ValueError:
        on_err()
    finally:
        fin()
    return 1 if y else alt()
'''
    g = fg_of(tmp_path, src, "h")
    assert [parse_key(k)[1] for k in g.call_targets] == ["it", "poll", "on_err", "fin", "alt"]
    assert [n.calls[0].name for n in g.nodes if n.type == "loop"] == ["it", "poll"]


def test_raise_edge_is_kept_next_to_an_ordinary_edge(tmp_path):
    """try 体以 if 开头：条件不成立的连线和异常连线都从判断节点接到 finally / try 之后，两条都要画。"""
    src = '''
def a(): ...
def boom(x): ...
def d(): ...
def fin(flag, x):
    try:
        if flag:
            boom(x)
            a()
    finally:
        d()
def swallow(flag, x):
    try:
        if flag:
            boom(x)
            a()
    except ValueError:
        logger.warning("x")
    d()
'''
    for name in ("fin", "swallow"):
        g = fg_of(tmp_path, src, name)
        cond, d_n = node_by(g, type="cond"), node_by(g, subtitle="d")
        assert sorted((e.src, e.kind) for e in _incoming(g, d_n.id) if e.src == cond.id) == [
            (cond.id, "no"), (cond.id, "raise")], name
        assert len(g.edges) == len({(e.src, e.dst, e.kind) for e in g.edges}), name


JUMPS = '''
def a(x): ...
def b(x): ...
def c(): ...
def d(x): ...
def e(): ...
def f(): ...
def brk(xs):
    for x in xs:
        try:
            a(x)
            if b(x):
                break
        finally:
            c()
        d(x)
    e()
def cont(xs):
    for x in xs:
        try:
            a(x)
            if b(x):
                continue
        finally:
            c()
        d(x)
    e()
def two(xs):
    for x in xs:
        try:
            try:
                a(x)
                if b(x):
                    continue
                break
            finally:
                c()
        finally:
            f()
        d(x)
    e()
def in_handler(xs):
    for x in xs:
        try:
            a(x)
        except ValueError:
            b(x)
            break
        finally:
            c()
        d(x)
    e()
def in_else(xs):
    for x in xs:
        try:
            a(x)
        except ValueError:
            b(x)
        else:
            if d(x):
                continue
        finally:
            c()
        e()
    f()
def log_only(xs):
    for x in xs:
        try:
            a(x)
            break
        finally:
            logger.info("x")
    e()
def no_exit(xs):
    for x in xs:
        try:
            a(x)
            break
        finally:
            return c()
    e()
def last(xs):
    for x in xs:
        try:
            a(x)
            break
        finally:
            c()
def partial(xs):
    for x in xs:
        try:
            a(x)
            break
        finally:
            try:
                v = int(x)
            except ValueError:
                c()
        d(x)
    e()
'''


def _labels(g):
    return {(e.src, e.dst, e.kind): e.label for e in g.edges}


def test_break_and_continue_go_through_finally(tmp_path):
    """try 里的 break / continue 先以 finally 类型的连线进入 finally（原来的"是 / 否"留在 label），
    再从 finally 的出口到目标：break 用 break 类型、continue 用 loop_back 类型。finally 原有的出口照旧。"""
    g = fg_of(tmp_path, JUMPS, "brk")
    loop, a_n, cond, c_n, d_n, e_n = (node_by(g, type="loop"), *(node_by(g, subtitle=s) for s in (
        "a", "b(x)", "c", "d", "e")))
    E = _edges(g)
    assert {(cond.id, c_n.id, "finally"), (c_n.id, e_n.id, "break"), (cond.id, c_n.id, "no"),
            (c_n.id, d_n.id, "next"), (a_n.id, c_n.id, "raise"), (loop.id, e_n.id, "loop_exit")} <= E
    assert _labels(g)[(cond.id, c_n.id, "finally")] == "是"
    assert not [x for x in g.edges if x.src == cond.id and x.dst == e_n.id]
    g = fg_of(tmp_path, JUMPS, "cont")
    loop, cond, c_n, d_n = (node_by(g, type="loop"), *(node_by(g, subtitle=s) for s in ("b(x)", "c", "d")))
    assert {(cond.id, c_n.id, "finally"), (c_n.id, loop.id, "loop_back"), (c_n.id, d_n.id, "next"),
            (d_n.id, loop.id, "loop_back")} <= _edges(g)
    assert _labels(g)[(cond.id, c_n.id, "finally")] == "是"
    assert not [x for x in g.edges if x.src == cond.id and x.dst == loop.id]


def test_jump_through_two_nested_finallies(tmp_path):
    """内层 finally 的出口先连到外层 finally（finally 类型），外层 finally 的出口再到目标；finally 各只画一份。"""
    g = fg_of(tmp_path, JUMPS, "two")
    loop, cond, c_n, f_n, d_n, e_n = (node_by(g, type="loop"), *(node_by(g, subtitle=s) for s in (
        "b(x)", "c", "f", "d", "e")))
    E = _edges(g)
    assert {(c_n.id, f_n.id, "finally"), (f_n.id, e_n.id, "break"), (f_n.id, loop.id, "loop_back"),
            (c_n.id, f_n.id, "next"), (f_n.id, d_n.id, "next")} <= E
    assert sorted(x.label for x in g.edges if (x.src, x.dst, x.kind) == (cond.id, c_n.id, "finally")) == ["否", "是"]
    assert not [x for x in g.edges if x.src in (cond.id, c_n.id) and x.dst in (loop.id, e_n.id)]
    assert len([n for n in g.nodes if n.subtitle in ("c", "f")]) == 2


def test_jump_in_handler_or_else_goes_through_finally(tmp_path):
    g = fg_of(tmp_path, JUMPS, "in_handler")
    b_n, c_n, e_n = (node_by(g, subtitle=s) for s in ("b", "c", "e"))
    assert {(b_n.id, c_n.id, "finally"), (c_n.id, e_n.id, "break")} <= _edges(g)
    assert not [x for x in g.edges if x.src == b_n.id and x.dst == e_n.id]
    g = fg_of(tmp_path, JUMPS, "in_else")
    loop, cond, c_n = node_by(g, type="loop"), node_by(g, subtitle="d(x)"), node_by(g, subtitle="c")
    assert {(cond.id, c_n.id, "finally"), (c_n.id, loop.id, "loop_back")} <= _edges(g)
    assert _labels(g)[(cond.id, c_n.id, "finally")] == "是"
    assert not [x for x in g.edges if x.src == cond.id and x.dst == loop.id]


def test_finally_without_nodes_or_without_exits(tmp_path):
    """finally 体里没有节点：跳转照旧直连目标。finally 体没有出口（总是 return）：不新增通往目标的连线。"""
    g = fg_of(tmp_path, JUMPS, "log_only")
    a_n, e_n = node_by(g, subtitle="a"), node_by(g, subtitle="e")
    assert (a_n.id, e_n.id, "break") in _edges(g) and _labels(g)[(a_n.id, e_n.id, "break")] == ""
    assert not [x for x in g.edges if x.kind == "finally"]
    g = fg_of(tmp_path, JUMPS, "no_exit")
    loop, a_n, c_n, e_n = node_by(g, type="loop"), node_by(g, subtitle="a"), node_by(g, subtitle="c"), \
        node_by(g, subtitle="e")
    assert (a_n.id, c_n.id, "finally") in _edges(g)
    assert [(x.src, x.kind) for x in _incoming(g, e_n.id)] == [(loop.id, "loop_exit")]


def test_break_through_finally_when_loop_is_last_statement(tmp_path):
    g = fg_of(tmp_path, JUMPS, "last")
    loop, a_n, c_n, fell = node_by(g, type="loop"), node_by(g, subtitle="a"), node_by(g, subtitle="c"), g.nodes[-2]
    assert fell.title == "执行到函数末尾"
    assert {(a_n.id, c_n.id, "finally"), (c_n.id, fell.id, "break"), (c_n.id, loop.id, "loop_back"),
            (loop.id, fell.id, "loop_exit")} <= _edges(g)
    assert not [x for x in g.edges if x.src == a_n.id and x.dst == fell.id]


def test_jump_crossing_finally_on_a_path_without_nodes(tmp_path):
    """finally 体以 try 体没有节点的 try 开头：int(x) 不抛异常时跳转没经过 finally 的任何节点，直接到目标；
    抛出时经过捕获节点和 c，再从 c 到目标。"""
    g = fg_of(tmp_path, JUMPS, "partial")
    a_n, catch, c_n, e_n = node_by(g, subtitle="a"), node_by(g, type="catch"), node_by(g, subtitle="c"), \
        node_by(g, subtitle="e")
    E = _edges(g)
    assert {(a_n.id, catch.id, "raise"), (catch.id, c_n.id, "next"), (c_n.id, e_n.id, "break"),
            (a_n.id, e_n.id, "break")} <= E


JUMPS_MORE = '''
def a(x): ...
def b(x): ...
def c(): ...
def d(x): ...
def e(): ...
def f(): ...
def between(xss):
    for xs in xss:
        try:
            for x in xs:
                a(x)
                if b(x):
                    break
                if d(x):
                    continue
            e()
        finally:
            c()
    f()
def cont_pass(xs):
    for x in xs:
        try:
            a(x)
            continue
        finally:
            try:
                v = int(x)
            except ValueError:
                c()
        d(x)
    e()
def while_else(n):
    i = 0
    while i < n:
        i += 1
        a(i)
    else:
        for y in range(3):
            try:
                if b(y):
                    break
            finally:
                c()
            f()
    e()
def outer_else(xss):
    for xs in xss:
        while a(xs):
            f()
        else:
            try:
                if b(xs):
                    break
            finally:
                c()
        d(xs)
    e()
'''


def test_inner_loop_jumps_do_not_go_through_an_outer_finally(tmp_path):
    """try-finally 夹在两层循环之间：内层循环里的 break / continue 不离开这个 try，不经过它的 finally。"""
    g = fg_of(tmp_path, JUMPS_MORE, "between")
    inner = node_by(g, subtitle="for x in xs")
    cb, cd, c_n, e_n = (node_by(g, subtitle=s) for s in ("b(x)", "d(x)", "c", "e"))
    E = _edges(g)
    assert {(cb.id, e_n.id, "break"), (cd.id, inner.id, "loop_back"), (e_n.id, c_n.id, "next")} <= E
    assert not [x for x in g.edges if x.src in (cb.id, cd.id) and x.dst == c_n.id]
    assert not [x for x in g.edges if x.kind == "finally"]


def test_continue_through_a_finally_with_a_path_without_nodes(tmp_path):
    """continue 经过的 finally 以 try 体没有节点的 try 开头：int(x) 不抛异常时 continue 不经过任何节点直接回到循环节点；
    抛出时经过捕获节点和 c 再回去。finally 原有的出口（c → d）不变。"""
    g = fg_of(tmp_path, JUMPS_MORE, "cont_pass")
    loop, a_n, catch, c_n, d_n = (node_by(g, type="loop"), node_by(g, subtitle="a"), node_by(g, type="catch"),
                                  node_by(g, subtitle="c"), node_by(g, subtitle="d"))
    assert {(a_n.id, loop.id, "loop_back"), (a_n.id, catch.id, "raise"), (catch.id, c_n.id, "next"),
            (c_n.id, loop.id, "loop_back"), (c_n.id, d_n.id, "next")} <= _edges(g)


def test_break_through_finally_inside_while_else(tmp_path):
    """while 的 else 里：for 循环里的 break 经过 finally 跳出这个 for；外层循环的 break 写在 while 的 else 里时，
    经过 finally 跳出外层循环（不是 while）。"""
    g = fg_of(tmp_path, JUMPS_MORE, "while_else")
    cb, c_n, f_n, e_n = (node_by(g, subtitle=s) for s in ("b(y)", "c", "f", "e"))
    E = _edges(g)
    assert {(cb.id, c_n.id, "finally"), (c_n.id, e_n.id, "break"), (c_n.id, f_n.id, "next")} <= E
    assert not [x for x in g.edges if x.src == cb.id and x.dst == e_n.id]
    o = fg_of(tmp_path, JUMPS_MORE, "outer_else")
    outer, wl = node_by(o, subtitle="for xs in xss"), node_by(o, subtitle="while a(xs)")
    cb, c_n, d_n, e_n = (node_by(o, subtitle=s) for s in ("b(xs)", "c", "d", "e"))
    assert {(cb.id, c_n.id, "finally"), (c_n.id, e_n.id, "break"), (c_n.id, d_n.id, "next")} <= _edges(o)
    assert not [x for x in o.edges if x.src == c_n.id and x.dst in (wl.id, outer.id)]


# ---------- match：判断节点，每个 case 一条出边 ----------

MATCHES = '''
from pystepkit import condition
def a(x=None): ...
def b(x=None): ...
def c(x=None): ...
def d(x=None): ...
def ok(x): ...
def done(): ...
def fin(): ...
def kind_of(x): ...
@condition(title="是哪种命令？")
def command(x): ...
def three(cmd):
    match cmd:
        case 1:
            a(1)
        case 2:
            b(2)
        case 3:
            c(3)
        case _:
            d()
    done()
def no_default(cmd):
    match cmd:
        case "go":
            a()
        case "stop":
            b()
    done()
def guarded(cmd):
    match cmd:
        case int(n) if ok(n):
            a(n)
        case int(n) if n < 0:
            b(n)
        case _:
            pass
    done()
def alternatives(cmd):
    match cmd:
        case "a" | "b":
            a()
        case [x, *_] | {"k": x}:
            b(x)
        case {"k": 1, **rest}:
            c(rest)
    done()
def empty_body(cmd):
    match cmd:
        case 1:
            pass
        case 2:
            a()
    done()
def in_loop(xs):
    for x in xs:
        match x:
            case 0:
                break
            case 1:
                continue
            case 2:
                a(x)
                break
            case _:
                b(x)
        done()
    c()
def in_try(cmd):
    try:
        match cmd:
            case 1:
                return a()
            case 2:
                b()
        done()
    finally:
        fin()
def loop_try(xs):
    for x in xs:
        try:
            match x:
                case 0:
                    break
                case 1:
                    continue
                case _:
                    a(x)
        finally:
            fin()
    done()
def in_finally(xs, mode):
    for x in xs:
        try:
            if x:
                continue
        finally:
            match mode:
                case 1:
                    pass
                case 2:
                    fin()
        done()
    c()
def last(cmd):
    a()
    match cmd:
        case 1:
            b(1)
        case 2:
            c(2)
def subject_call(cmd):
    # 按类型分派
    match kind_of(cmd):
        case "x":
            a()
def marked_subject(cmd):
    match command(cmd):
        case "x":
            a()
def all_empty(cmd):
    match kind_of(cmd):
        case 1 if ok(cmd):
            pass
        case _:
            pass
    done()
def irrefutable(cmd):
    match cmd:
        case 1:
            a()
        case 2 | _:
            b()
    done()
def capture_as(cmd):
    match cmd:
        case 1:
            a()
        case _ as other:
            b(other)
    done()
'''


def _outs(g, nid):
    """nid 的出边 (终点, 类型, 标签)，排好序。"""
    return sorted((e.dst, e.kind, e.label) for e in g.edges if e.src == nid)


def _clean_graph(g):
    """走不到的节点、无出口的节点都没有。"""
    sc = run_selfcheck({g.key: g}, [])
    return sc.unreachable == [] and sc.dead_ends == []


def test_match_is_a_decision_node_with_one_edge_per_case(tmp_path):
    g = fg_of(tmp_path, MATCHES, "three")
    cond = node_by(g, type="cond")
    start, done_n = node_by(g, type="start"), node_by(g, subtitle="done")
    a_n, b_n, c_n, d_n = (node_by(g, subtitle=s) for s in "abcd")
    assert [n.type for n in g.nodes] == ["start", "cond", "step", "step", "step", "step", "step", "end", "end"]
    assert (cond.title, cond.subtitle, cond.line, cond.end_line, cond.calls) == (
        "match cmd", "match cmd", start.line + 1, start.line + 1, [])
    assert _outs(g, start.id) == [(cond.id, "next", "")]
    assert _outs(g, cond.id) == sorted([(a_n.id, "case", "1"), (b_n.id, "case", "2"), (c_n.id, "case", "3"),
                                        (d_n.id, "case", "_")])  # 有 case _：没有"都不匹配"
    assert all(_outs(g, n.id) == [(done_n.id, "next", "")] for n in (a_n, b_n, c_n, d_n))
    assert _clean_graph(g)


def test_match_without_catch_all_has_a_no_match_edge(tmp_path):
    g = fg_of(tmp_path, MATCHES, "no_default")
    cond, a_n, b_n, done_n = node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in ("a", "b", "done"))
    assert _outs(g, cond.id) == sorted([(a_n.id, "case", '"go"'), (b_n.id, "case", '"stop"'),
                                        (done_n.id, "case", "都不匹配")])
    assert _clean_graph(g)
    # 最后一条语句是 match：各分支和"都不匹配"都落到"执行到函数末尾"
    g = fg_of(tmp_path, MATCHES, "last")
    cond, b_n, c_n = node_by(g, type="cond"), node_by(g, subtitle="b"), node_by(g, subtitle="c")
    fell = node_by(g, title="执行到函数末尾")
    assert cond.line == node_by(g, subtitle="a").line + 1
    assert _outs(g, cond.id) == sorted([(b_n.id, "case", "1"), (c_n.id, "case", "2"), (fell.id, "case", "都不匹配")])
    assert _outs(g, b_n.id) == _outs(g, c_n.id) == [(fell.id, "next", "")]
    assert _clean_graph(g)


def test_match_guard_is_part_of_the_case_edge(tmp_path):
    """守卫不单独出节点：守卫不成立时落到后面的分支，这条路径就是判断节点到后面分支的出边。守卫里的调用记在
    判断节点上，不一定执行（conditional）。"""
    g = fg_of(tmp_path, MATCHES, "guarded")
    cond, a_n, b_n, done_n = node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in ("a", "b", "done"))
    assert [n.type for n in g.nodes] == ["start", "cond", "step", "step", "step", "end", "end"]
    assert _outs(g, cond.id) == sorted([(a_n.id, "case", "int(n) if ok(n)"), (b_n.id, "case", "int(n) if n < 0"),
                                        (done_n.id, "case", "_")])  # case _: pass 直接越过 match
    assert [(c.name, c.conditional) for c in cond.calls] == [("ok", True)]
    assert _clean_graph(g)


def test_match_or_patterns_and_empty_case_body(tmp_path):
    g = fg_of(tmp_path, MATCHES, "alternatives")
    cond, a_n, b_n, c_n, done_n = node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in ("a", "b", "c", "done"))
    assert _outs(g, cond.id) == sorted([(a_n.id, "case", '"a" | "b"'), (b_n.id, "case", '[x, *_] | {"k": x}'),
                                        (c_n.id, "case", '{"k": 1, **rest}'), (done_n.id, "case", "都不匹配")])
    assert _clean_graph(g)
    # 分支体为空：这个 case 的出边直接越过 match，和"都不匹配"落在同一处，标签不同、各画一条
    g = fg_of(tmp_path, MATCHES, "empty_body")
    cond, a_n, done_n = node_by(g, type="cond"), node_by(g, subtitle="a"), node_by(g, subtitle="done")
    assert _outs(g, cond.id) == sorted([(done_n.id, "case", "1"), (a_n.id, "case", "2"),
                                        (done_n.id, "case", "都不匹配")])
    assert _clean_graph(g)


def test_match_catch_all_forms(tmp_path):
    """兜底分支：case _、不带守卫的捕获，以及 case 2 | _、case _ as other 这样同样必然匹配的模式。"""
    for name in ("irrefutable", "capture_as"):
        g = fg_of(tmp_path, MATCHES, name)
        cond = node_by(g, type="cond")
        assert [label for _, _, label in _outs(g, cond.id)] == (
            ["1", "2 | _"] if name == "irrefutable" else ["1", "_ as other"]), name
        assert _clean_graph(g), name


def test_match_in_loop_with_break_and_continue(tmp_path):
    """case 里直接 break / continue：判断节点的这条出边改成 break / loop_back（同 if），模式留在 label。"""
    g = fg_of(tmp_path, MATCHES, "in_loop")
    loop, cond = node_by(g, type="loop"), node_by(g, type="cond")
    a_n, b_n, c_n, done_n = (node_by(g, subtitle=s) for s in ("a", "b", "c", "done"))
    assert _outs(g, loop.id) == sorted([(cond.id, "loop_each", ""), (c_n.id, "loop_exit", "")])
    assert _outs(g, cond.id) == sorted([(c_n.id, "break", "0"), (loop.id, "loop_back", "1"), (a_n.id, "case", "2"),
                                        (b_n.id, "case", "_")])
    assert _outs(g, a_n.id) == [(c_n.id, "break", "")]
    assert _outs(g, b_n.id) == [(done_n.id, "next", "")] and _outs(g, done_n.id) == [(loop.id, "loop_back", "")]
    assert _clean_graph(g)
    # 再套一层 try … finally：跳转先进 finally，再从 finally 出来到目标
    g = fg_of(tmp_path, MATCHES, "loop_try")
    loop, cond = node_by(g, type="loop"), node_by(g, type="cond")
    a_n, fin_n, done_n = (node_by(g, subtitle=s) for s in ("a", "fin", "done"))
    assert _outs(g, cond.id) == sorted([(fin_n.id, "finally", "0"), (fin_n.id, "finally", "1"),
                                        (a_n.id, "case", "_"), (fin_n.id, "raise", "")])
    assert _outs(g, fin_n.id) == sorted([(done_n.id, "break", ""), (loop.id, "loop_back", "")])
    assert _clean_graph(g)
    # match 是 finally 体的第一句、continue 穿过这个 finally：continue 先进判断节点，判断节点的每个出口（空分支、
    # "都不匹配"也算）既照常往下走，也接回循环节点
    g = fg_of(tmp_path, MATCHES, "in_finally")
    loop, test, cond = node_by(g, type="loop"), node_by(g, subtitle="x"), node_by(g, subtitle="match mode")
    fin_n, done_n, c_n = (node_by(g, subtitle=s) for s in ("fin", "done", "c"))
    assert _outs(g, test.id) == sorted([(cond.id, "no", ""), (cond.id, "finally", "是"), (cond.id, "raise", "")])
    assert _outs(g, cond.id) == sorted([(done_n.id, "case", "1"), (fin_n.id, "case", "2"), (done_n.id, "case", "都不匹配"),
                                        (loop.id, "loop_back", "1"), (loop.id, "loop_back", "都不匹配")])
    assert _outs(g, fin_n.id) == sorted([(done_n.id, "next", ""), (loop.id, "loop_back", "")])
    assert _outs(g, loop.id) == sorted([(test.id, "loop_each", ""), (c_n.id, "loop_exit", "")])
    assert _clean_graph(g)


def test_match_in_try_finally_with_return_in_a_case(tmp_path):
    g = fg_of(tmp_path, MATCHES, "in_try")
    cond, a_n, b_n, done_n, fin_n = (node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in (
        "a", "b", "done", "fin")))
    ret = node_by(g, type="end", line=a_n.line)
    fell = node_by(g, title="执行到函数末尾")
    # 判断节点是 try 体的入口：异常从它画出
    assert _outs(g, cond.id) == sorted([(a_n.id, "case", "1"), (b_n.id, "case", "2"), (done_n.id, "case", "都不匹配"),
                                        (fin_n.id, "raise", "")])
    assert _outs(g, a_n.id) == [(ret.id, "next", "")] and _outs(g, ret.id) == [(fin_n.id, "finally", "")]
    assert _outs(g, b_n.id) == [(done_n.id, "next", "")] and _outs(g, done_n.id) == [(fin_n.id, "next", "")]
    assert _outs(g, fin_n.id) == [(fell.id, "next", "")]
    assert _clean_graph(g)


def test_match_title_subject_calls_and_folding(tmp_path):
    """标题按条件节点的规则：主语是对 condition 类型函数的单个调用时取它的标题 → 上方注释 → match 原文。
    主语里的调用记在判断节点上。所有分支体都为空时不出判断节点，只剩主语和守卫里的调用（同 if）。"""
    g = fg_of(tmp_path, MATCHES, "subject_call")
    cond = node_by(g, type="cond")
    assert (cond.title, cond.subtitle) == ("按类型分派", "match kind_of(cmd)")
    assert [(c.name, c.conditional) for c in cond.calls] == [("kind_of", False)]
    assert [n.subtitle for n in g.nodes if n.type == "step"] == ["a"]  # 主语的调用不再单独出步骤
    g = fg_of(tmp_path, MATCHES, "marked_subject")
    assert (node_by(g, type="cond").title, node_by(g, type="cond").subtitle) == ("是哪种命令？", "match command(cmd)")
    g = fg_of(tmp_path, MATCHES, "all_empty")
    assert [n.type for n in g.nodes] == ["start", "step", "step", "end", "end"]
    head = g.nodes[1]
    assert (head.line, head.end_line) == (node_by(g, type="start").line + 1,) * 2
    assert [(c.name, c.conditional) for c in head.calls] == [("kind_of", False), ("ok", True)]


# ---------- 分支（branch）：判断节点的每条分支出边标明是哪个分支 ----------

BRANCHES = '''
def a(x=None): ...
def b(x=None): ...
def c(x=None): ...
def d(x=None): ...
def done(): ...
def both(x):
    if a(x):
        b()
    else:
        c()
    done()
def no_else(x):
    if a(x):
        b()
    done()
def chain(x):
    if a(x):
        b()
    elif c(x):
        d()
    else:
        done()
def ends(xs):
    for x in xs:
        if a(x):
            break
        if b(x):
            continue
        if c(x):
            return d(x)
        done()
def while_cond(n):
    while a(n):
        if b(n):
            c(n)
    done()
def no_default(cmd):
    match cmd:
        case int(n) if a(n):
            b(n)
        case "x":
            c()
    done()
def crossed(x):
    if a(x):
        try:
            v = int(x)
        except ValueError:
            b()
    done()
def through_empty(xs):
    for x in xs:
        try:
            if a(x):
                continue
        finally:
            try:
                v = int(x)
            except ValueError:
                c()
        d(x)
'''

_YES_NO = {"是": "yes", "否": "no"}


def _branches(g, nid):
    """nid 的出边 (终点, 类型, 标签, 分支)，排好序。"""
    return sorted((e.dst, e.kind, e.label, e.branch) for e in g.edges if e.src == nid)


def _check_branches(g) -> int:
    """判断节点每条不是 raise 的出边都有 branch，并与旧的认法一致（kind 为 yes / no；改了 kind 之后 label 为 是 / 否；
    match 的 label 是模式，"都不匹配"的是 else）；其余连线（判断节点的 raise 出边、别的节点的出边）都没有。加上
    branch 不多出连线：(起点, 终点, kind, label) 仍然互不相同。返回带 branch 的连线条数。"""
    conds = {n.id: n for n in g.nodes if n.type == "cond"}
    for e in g.edges:
        cond = conds.get(e.src)
        if cond is None or e.kind == "raise":
            expected = ""
        elif cond.subtitle.startswith("match "):
            expected = "else" if e.label == "都不匹配" else e.label
        else:
            expected = e.kind if e.kind in ("yes", "no") else _YES_NO[e.label]
        assert e.branch == expected, (g.qualname, e)
    assert len({(e.src, e.dst, e.kind, e.label) for e in g.edges}) == len(g.edges), g.qualname
    return sum(1 for e in g.edges if e.branch)


def test_if_branches_are_marked(tmp_path):
    g = fg_of(tmp_path, BRANCHES, "both")
    cond, b_n, c_n = node_by(g, type="cond"), node_by(g, subtitle="b"), node_by(g, subtitle="c")
    assert _branches(g, cond.id) == sorted([(b_n.id, "yes", "", "yes"), (c_n.id, "no", "", "no")])
    assert {e.branch for e in g.edges if e.src != cond.id} == {""}
    # 没有 else：越过分支体的那条连线是"否"分支
    g = fg_of(tmp_path, BRANCHES, "no_else")
    cond, b_n, done_n = node_by(g, type="cond"), node_by(g, subtitle="b"), node_by(g, subtitle="done")
    assert _branches(g, cond.id) == sorted([(b_n.id, "yes", "", "yes"), (done_n.id, "no", "", "no")])
    assert _branches(g, b_n.id) == [(done_n.id, "next", "", "")]
    # elif 链：嵌套的判断节点，各有自己的是 / 否
    g = fg_of(tmp_path, BRANCHES, "chain")
    c1, c2 = node_by(g, subtitle="a(x)"), node_by(g, subtitle="c(x)")
    b_n, d_n, done_n = (node_by(g, subtitle=s) for s in ("b", "d", "done"))
    assert _branches(g, c1.id) == sorted([(b_n.id, "yes", "", "yes"), (c2.id, "no", "", "no")])
    assert _branches(g, c2.id) == sorted([(d_n.id, "yes", "", "yes"), (done_n.id, "no", "", "no")])


def test_branch_ending_in_break_continue_or_return_keeps_its_branch(tmp_path):
    """分支体直接是 break / continue：连线改成 break / loop_back，branch 不变；以 return 结尾的分支照常。循环节点的
    loop_each / loop_exit 不带 branch。"""
    g = fg_of(tmp_path, BRANCHES, "ends")
    loop = node_by(g, type="loop")
    c1, c2, c3 = (node_by(g, subtitle=s) for s in ("a(x)", "b(x)", "c(x)"))
    d_n, done_n = node_by(g, subtitle="d"), node_by(g, subtitle="done")
    ret, fell = node_by(g, type="end", line=d_n.line), node_by(g, title="执行到函数末尾")
    assert _branches(g, c1.id) == sorted([(fell.id, "break", "是", "yes"), (c2.id, "no", "", "no")])
    assert _branches(g, c2.id) == sorted([(loop.id, "loop_back", "是", "yes"), (c3.id, "no", "", "no")])
    assert _branches(g, c3.id) == sorted([(d_n.id, "yes", "", "yes"), (done_n.id, "no", "", "no")])
    assert _branches(g, d_n.id) == [(ret.id, "next", "", "")]
    assert _branches(g, done_n.id) == [(loop.id, "loop_back", "", "")]
    assert _branches(g, loop.id) == sorted([(c1.id, "loop_each", "", ""), (fell.id, "loop_exit", "", "")])


def test_while_is_a_loop_node_without_branches(tmp_path):
    """while 画成循环节点（不是判断节点）：条件成立 / 不成立是 loop_each / loop_exit，不带 branch。循环体最后一句是
    if 时，"否"分支改成回到循环节点的 loop_back，branch 仍是 no。"""
    g = fg_of(tmp_path, BRANCHES, "while_cond")
    loop, cond = node_by(g, type="loop"), node_by(g, type="cond")
    c_n, done_n = node_by(g, subtitle="c"), node_by(g, subtitle="done")
    assert loop.subtitle == "while a(n)"
    assert _branches(g, loop.id) == sorted([(cond.id, "loop_each", "", ""), (done_n.id, "loop_exit", "", "")])
    assert _branches(g, cond.id) == sorted([(c_n.id, "yes", "", "yes"), (loop.id, "loop_back", "否", "no")])
    assert _branches(g, c_n.id) == [(loop.id, "loop_back", "", "")]


def test_match_branches_are_the_patterns_and_else(tmp_path):
    g = fg_of(tmp_path, BRANCHES, "no_default")
    cond, b_n, c_n, done_n = node_by(g, type="cond"), *(node_by(g, subtitle=s) for s in ("b", "c", "done"))
    assert _branches(g, cond.id) == sorted([(b_n.id, "case", "int(n) if a(n)", "int(n) if a(n)"),
                                            (c_n.id, "case", '"x"', '"x"'), (done_n.id, "case", "都不匹配", "else")])
    # 有兜底分支：没有 else
    g = fg_of(tmp_path, MATCHES, "three")
    assert sorted(e.branch for e in g.edges if e.src == node_by(g, type="cond").id) == ["1", "2", "3", "_"]
    # case 里直接 break / continue：改成 break / loop_back，branch 仍是模式
    g = fg_of(tmp_path, MATCHES, "in_loop")
    loop, cond = node_by(g, type="loop"), node_by(g, type="cond")
    a_n, b_n, c_n = (node_by(g, subtitle=s) for s in ("a", "b", "c"))
    assert _branches(g, cond.id) == sorted([(c_n.id, "break", "0", "0"), (loop.id, "loop_back", "1", "1"),
                                            (a_n.id, "case", "2", "2"), (b_n.id, "case", "_", "_")])


def test_branch_through_finally_keeps_its_branch(tmp_path):
    """break / continue 经过 finally：进入 finally 体的那段（finally）带原来的 branch；从 finally 体出口到目标的那段
    起点不是判断节点，不带。判断节点的 raise 出边不带 branch。"""
    g = fg_of(tmp_path, JUMPS, "brk")
    cond, c_n, e_n = node_by(g, subtitle="b(x)"), node_by(g, subtitle="c"), node_by(g, subtitle="e")
    assert _branches(g, cond.id) == sorted([(c_n.id, "finally", "是", "yes"), (c_n.id, "no", "", "no")])
    assert (e_n.id, "break", "", "") in _branches(g, c_n.id)
    # 两层 finally：continue（是）和 break（否）都先进内层 finally
    g = fg_of(tmp_path, JUMPS, "two")
    cond, c_n = node_by(g, subtitle="b(x)"), node_by(g, subtitle="c")
    assert _branches(g, cond.id) == sorted([(c_n.id, "finally", "是", "yes"), (c_n.id, "finally", "否", "no")])
    # finally 体以 try 体没有节点的 try 开头：continue 不经过节点、直接回到循环节点的那一段也带 branch
    g = fg_of(tmp_path, BRANCHES, "through_empty")
    loop, cond, catch, d_n = (node_by(g, type="loop"), node_by(g, type="cond"), node_by(g, type="catch"),
                              node_by(g, subtitle="d"))
    assert _branches(g, cond.id) == sorted([(loop.id, "loop_back", "是", "yes"), (d_n.id, "no", "", "no"),
                                            (catch.id, "raise", "否", ""), (d_n.id, "raise", "", "")])
    # match 的 case 里 break / continue 经过 finally；判断节点是 try 体的入口，它的 raise 出边不带 branch
    g = fg_of(tmp_path, MATCHES, "loop_try")
    cond, a_n, fin_n = node_by(g, type="cond"), node_by(g, subtitle="a"), node_by(g, subtitle="fin")
    assert _branches(g, cond.id) == sorted([(fin_n.id, "finally", "0", "0"), (fin_n.id, "finally", "1", "1"),
                                            (a_n.id, "case", "_", "_"), (fin_n.id, "raise", "", "")])
    # continue 穿过的 finally 以 match 开头：match 的各分支（含"都不匹配"）从 finally 体出口回到循环节点，branch 照带
    g = fg_of(tmp_path, MATCHES, "in_finally")
    loop, test, cond = node_by(g, type="loop"), node_by(g, subtitle="x"), node_by(g, subtitle="match mode")
    fin_n, done_n = node_by(g, subtitle="fin"), node_by(g, subtitle="done")
    assert _branches(g, test.id) == sorted([(cond.id, "no", "", "no"), (cond.id, "finally", "是", "yes"),
                                            (cond.id, "raise", "", "")])
    assert _branches(g, cond.id) == sorted([
        (done_n.id, "case", "1", "1"), (fin_n.id, "case", "2", "2"), (done_n.id, "case", "都不匹配", "else"),
        (loop.id, "loop_back", "1", "1"), (loop.id, "loop_back", "都不匹配", "else")])


def test_raise_edge_of_a_decision_node_has_no_branch(tmp_path):
    """分支越过一个 try 体没有节点的 try：这条分支上抛出的异常从判断节点连到捕获节点（raise，label 照旧是"是"），
    不带 branch；这条分支本身照常带 yes。"""
    g = fg_of(tmp_path, BRANCHES, "crossed")
    cond, catch, done_n = node_by(g, type="cond"), node_by(g, type="catch"), node_by(g, subtitle="done")
    assert _branches(g, cond.id) == sorted([(catch.id, "raise", "是", ""), (done_n.id, "yes", "", "yes"),
                                            (done_n.id, "no", "", "no")])


def test_every_branch_edge_is_marked(tmp_path):
    """本文件各段样例里的每个函数：判断节点的分支出边都有 branch、与 kind / label 一致，其余连线都没有。"""
    marked = 0
    for i, src in enumerate((BRANCHES, JUMPS, JUMPS_MORE, MATCHES)):
        for g in analyze_files(tmp_path / str(i), {"app/mod.py": src}).functions.values():
            marked += _check_branches(g)
    assert marked > 50


def test_raise_http_exception_end_is_titled_by_status_and_detail(tmp_path):
    """raise HTTPException(...)（FastAPI / Starlette 的，按 import 认）的结局标题是"状态码 说明"，不是整段代码：
    状态码认整数和 status.HTTP_404_NOT_FOUND 这样的常量；说明认字符串、f-string，别的表达式写源码，没给时写状态码的
    标准说法。状态码看不出来的、名字相同但不是来自 FastAPI / Starlette 的，照旧写"抛出 源码"。"""
    src = '''
from fastapi import HTTPException, status
from fastapi import HTTPException as Http
import fastapi
import starlette.exceptions
from starlette import status as st
def find(x): ...
def a(uid):
    raise HTTPException(status_code=404, detail="用户不存在")
def b(uid):
    raise HTTPException(409, "用户已存在")
def c(uid):
    raise Http(status.HTTP_403_FORBIDDEN, detail=f"用户 {uid} 无权操作")
def d(uid):
    raise fastapi.HTTPException(status_code=st.HTTP_401_UNAUTHORIZED)
def e(uid, code, why):
    if uid:
        raise starlette.exceptions.HTTPException(400, detail=why)
    raise HTTPException(code, "看不出状态码")
def f(uid):
    raise HTTPException(status_code=422, detail={"msg": "参数不合法"}, headers={"X-A": "1"})
def g(uid):
    raise HTTPException(404, detail=str(find(uid)))
def h(uid):
    raise HTTPException(404, "一" * 3) from None
'''
    other = '''
class HTTPException(Exception): ...
def own(uid):
    raise HTTPException(404, "自己的异常")
'''
    ends = lambda q: [(n.title, n.title_full, n.end_kind) for n in fg_of(tmp_path, src, q).nodes
                      if n.type == "end" and n.line]
    assert ends("a") == [("404 用户不存在", "", "failed")]
    assert ends("b") == [("409 用户已存在", "", "failed")]
    assert ends("c") == [("403 用户 {uid} 无权操作", "", "failed")]
    assert ends("d") == [("401 Unauthorized", "", "failed")]
    assert ends("e") == [("400 why", "", "failed"), ('抛出 HTTPException(code, "看不出状态码")', "", "failed")]
    assert ends("f") == [('422 {"msg": "参数不合法"}', "", "failed")]
    g = fg_of(tmp_path, src, "g")  # 说明里有有意义的调用：照旧先出步骤再出结局
    assert [(n.type, n.title) for n in g.nodes if n.line][1:] == [("step", "find"), ("end", "404 str(find(uid))")]
    assert ends("h") == [('404 "一" * 3', "", "failed")]
    [own] = [f for f in analyze_files(tmp_path / "o", {"app/other.py": other}).functions.values() if f.qualname == "own"]
    assert [n.title for n in own.nodes if n.type == "end" and n.line] == ['抛出 HTTPException(404, "自己的异常")']


TITLED = '''
from fastapi import HTTPException
from pystepkit import step, workflow, condition
@step(title="列出待办")
async def list_todos(uid): ...
@step(title="处理待办")
async def handle(t): ...
@step(title="还有下一页")
def has_next(uid): ...
@step(title="组装结果")
async def build_out(uid): ...
def plain(uid):
    """没有标记的函数。"""
@workflow(title="处理全部待办")
async def run(uid):
    for t in await list_todos(uid):
        await handle(t)
    while has_next(uid):
        await handle(uid)
    if has_next(uid):
        return plain(uid)
    # 自己写的说明
    if has_next(uid):
        return None
    return await build_out(uid)
def errs(uid, kind):
    msg = "用户不存在"
    twice = "第一次"
    twice = "第二次"
    if kind == 1:
        raise HTTPException(status_code=404, detail=msg)
    if kind == 2:
        raise HTTPException(409, twice)
    name = f"用户 {uid} 已停用"
    raise HTTPException(410, detail=name)
'''


def test_loop_condition_and_return_borrow_the_title_of_the_marked_function_they_call(tmp_path):
    """循环头、判断条件、return 的值整个就是对一个标了标题的函数的调用时，节点标题用那个标题（源码留在副标题里）；
    上方有注释时照旧用注释，没有标记的函数（只有文档注释）照旧写源码。"""
    g = fg_of(tmp_path, TITLED, "run")
    rows = [(n.type, n.title, n.subtitle) for n in g.nodes if n.type in ("loop", "cond")]
    assert rows == [("loop", "列出待办", "for t in await list_todos(uid)"),
                    ("loop", "还有下一页", "while has_next(uid)"),
                    ("cond", "还有下一页", "has_next(uid)"),
                    ("cond", "自己写的说明", "has_next(uid)")]
    assert [n.title for n in g.nodes if n.type == "end" and n.line] == [
        "返回 plain(uid)", "返回 None", "返回「组装结果」的结果"]


def test_http_exception_detail_given_as_a_variable_assigned_once_uses_its_value(tmp_path):
    g = fg_of(tmp_path, TITLED, "errs")
    assert [n.title for n in g.nodes if n.type == "end" and n.line] == [
        "404 用户不存在", "409 twice", "410 用户 {uid} 已停用"]


def test_finally_body_entered_only_by_returns_and_exceptions_does_not_continue(tmp_path):
    """try 体每条路都 return / raise 时，出了 finally 体函数就结束：finally 体的出口不往后接，不多出"执行到函数末尾"，
    finally 体的最后一个节点也不算没有出路。try 体有正常出口时照旧往后接。"""
    src = '''
def first(): ...
def second(): ...
def close(): ...
def f():
    try:
        first()
        second()
        return 1
    finally:
        close()
def g():
    try:
        first()
    finally:
        close()
    second()
'''
    graph = analyze_files(tmp_path, {"app/mod.py": src})
    f, g = (next(x for x in graph.functions.values() if x.qualname == q) for q in ("f", "g"))
    close = next(n for n in f.nodes if n.subtitle == "close")
    assert [n.sid for n in f.nodes if n.type == "end"] == [next(n.sid for n in f.nodes if n.line == 9), "end:uncaught"]
    assert not [e for e in f.edges if e.src == close.id]
    assert sorted(e.kind for e in f.edges if e.dst == close.id) == ["finally", "raise"]
    assert not [k for k, _ in graph.selfcheck.dead_ends]
    g_close = next(n for n in g.nodes if n.subtitle == "close")
    assert [next(n for n in g.nodes if n.id == e.dst).subtitle for e in g.edges if e.src == g_close.id] == ["second"]
