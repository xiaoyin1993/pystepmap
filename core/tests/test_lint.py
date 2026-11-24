from __future__ import annotations

import json

from conftest import LOOKALIKES, make_project
from pystepmap.cli import main
from pystepmap.lint import check_project

GOOD = '''
from pystepkit import workflow, step, condition, outcome
@step(title="取回原文")
async def load(m): ...
@condition(title="开关打开？")
async def switch_on(): ...
@workflow(title="入站")
async def inbound(m):
    if await switch_on():
        return outcome("skipped")
    for _ in range(3):
        try:
            raw = await load(m)
        except TimeoutError:
            continue
        break
    await label.kiq(m)
    return outcome("ok")
@broker.task()
async def label(m): ...
'''

BAD = GOOD + '''
@step
async def bad_step(m):
    await load(m)
@workflow
async def bad(m):
    logger.info("x")
    if compute(m) > 1:
        pass
    helper = lambda: 1
    await plain(m)
async def plain(m): ...
def compute(m): ...
'''


def run(tmp_path, src, **kw):
    return check_project(make_project(tmp_path, {"app/f.py": src}), **kw).violations


def codes(vs):
    return sorted(v.code for v in vs)


def test_clean_workflow_has_no_violations(tmp_path):
    assert run(tmp_path, GOOD) == []


def test_each_rule(tmp_path):
    vs = run(tmp_path, BAD)
    assert codes(vs) == ["SM001", "SM001", "SM002", "SM003", "SM004"]
    lines = BAD.splitlines()
    for v in vs:
        assert v.file == "app/f.py"
    by = {(v.code, lines[v.line - 1].strip()) for v in vs}
    assert ("SM003", "await load(m)") in by
    assert ("SM001", 'logger.info("x")') in by
    assert ("SM002", "if compute(m) > 1:") in by
    assert ("SM004", "helper = lambda: 1") in by
    assert ("SM001", "await plain(m)") in by
    assert vs == sorted(vs, key=lambda v: (v.file, v.line, v.code))


def test_lookalike_marks_are_never_checked(tmp_path):
    """装饰器名字和 pystepkit 的一样、但不是从 pystepkit 导入的（Temporal、Metaflow、项目自己的）：不检查。"""
    assert run(tmp_path, LOOKALIKES) == []


def test_outcome_counts_only_when_imported_from_pystepkit(tmp_path):
    src = '''
import pystepkit as fk
from app.results import outcome
@fk.workflow
def w(x):
    if x:
        return fk.outcome("skipped")
    return outcome("failed")
'''
    vs = run(tmp_path, src)
    assert [(v.code, v.message) for v in vs] == [("SM001", "编排函数只能调用步骤：outcome(…) 不是步骤")]


def test_unmarked_code_is_never_checked(tmp_path):
    src = '''
    from dbos import DBOS
    @broker.task()
    async def t(m):
        logger.info("x")
    @DBOS.workflow()
    async def wf(m):
        logger.info("x")
        await other(m)
    @DBOS.step()
    async def st(m):
        await other(m)
    @DBOS.step()
    async def other(m): ...
    '''
    assert run(tmp_path, src) == []
    vs = run(tmp_path, src, treat_as_marked=frozenset({"dbos"}))
    assert codes(vs) == ["SM001", "SM003"]  # other(m) in wf is a step: allowed


def test_allowed_calls_in_workflow(tmp_path):
    src = '''
    from pystepkit import workflow, step, outcome
    @step
    async def load(m): ...
    @workflow
    async def wf(m, wait):
        attempted = []
        attempted.append(1)
        n = len(attempted)
        v = wait.get("k")
        h = await load(m)
        r = h.get_result()
        d = {}
        d["a"].setdefault("b", 1)
        if isinstance(m, str) and m.startswith("x"):
            raise ValueError("bad")
        x = Decimal(v)
        return outcome("ok")
    '''
    assert run(tmp_path, src) == []


def test_flagged_calls_in_workflow(tmp_path):
    src = '''
    import time
    from pystepkit import workflow
    @workflow
    async def wf(m):
        time.sleep(1)
        get_redis().delete(m)
        await m.fetch()
    '''
    assert codes(run(tmp_path, src)) == ["SM001"] * 4  # get_redis() 与 .delete(...) 各一处


def test_nested_def_is_single_sm004(tmp_path):
    src = '''
    from pystepkit import workflow
    @workflow
    async def wf(m):
        async def inner():
            logger.info(1)
            helper()
        return inner
    '''
    assert codes(run(tmp_path, src)) == ["SM004"]


def test_steps_may_call_plain_functions_and_conditions(tmp_path):
    src = '''
    from pystepkit import step, condition
    @condition
    def ok(): ...
    @step
    def s(m):
        helper(m)
        ok()
    @condition
    def c():
        s(1)
    def helper(m): ...
    '''
    assert run(tmp_path, src) == []


def test_class_workflow(tmp_path):
    src = '''
    from pystepkit import workflow, step
    @workflow(entry="go")
    class Flow:
        @step
        async def validate(self, ctx): ...
        def helper(self): ...
        async def go(self, ctx):
            await self.validate(ctx)
            self.helper()
    '''
    vs = run(tmp_path, src)
    assert codes(vs) == ["SM001"]


def test_if_test_sm002_only_once(tmp_path):
    src = '''
    from pystepkit import workflow, step
    @step
    def s(m): ...
    @workflow
    def wf(m, xs):
        while s(m) and len(xs) and xs.pop() and time.time():
            pass
    '''
    assert codes(run(tmp_path, src)) == ["SM002"]


def test_only_files(tmp_path):
    root = make_project(tmp_path, {"app/a.py": BAD, "app/b.py": BAD})
    vs = check_project(root, only_files=["app/b.py"]).violations
    assert {v.file for v in vs} == {"app/b.py"} and len(vs) == 5
    vs = check_project(root, only_files=[str(root / "app/a.py")]).violations
    assert {v.file for v in vs} == {"app/a.py"}


def test_cli_exit_codes_and_format(tmp_path, capsys):
    root = make_project(tmp_path / "bad", {"app/f.py": BAD})
    assert main(["check", str(root)]) == 1
    out = capsys.readouterr().out
    assert "app/f.py:" in out and "共 5 处违规" in out
    assert out.splitlines()[0].startswith("app/f.py:")
    good = make_project(tmp_path / "good", {"app/f.py": GOOD})
    assert main(["check", str(good)]) == 0
    assert "共 0 处违规" in capsys.readouterr().out
    assert main(["check", str(tmp_path / "nope")]) == 2
    assert main(["check", str(good), "--config", str(tmp_path / "nope.json")]) == 2
    root2 = make_project(tmp_path / "bad2", {"app/f.py": BAD})
    assert main(["check", str(root2), "--files", "app/other.py"]) == 0


UNPARSABLE = "from pystepkit import workflow, step\n@step\ndef a(): ...\n@workflow\ndef w():\n    a(\n"


def test_unparsable_files_in_scope_make_check_exit_2(tmp_path, capsys):
    """解析不了的文件没有检查：在检查范围内（全部文件，或 --files 选中的）时列在 stderr，退出码 2。"""
    root = make_project(tmp_path, {"app/flow.py": UNPARSABLE, "app/ok.py": GOOD})
    r = check_project(root)
    assert r.violations == [] and [f for f, _ in r.skipped] == ["app/flow.py"]
    assert main(["check", str(root)]) == 2
    cap = capsys.readouterr()
    assert "app/flow.py" in cap.err and "共 0 处违规" in cap.out
    assert check_project(root, only_files=["app/ok.py"]).skipped == []
    assert main(["check", str(root), "--files", "app/ok.py"]) == 0
    assert main(["check", str(root), "--files", "app/flow.py"]) == 2


def test_check_warns_about_files_and_rules_it_does_not_know(tmp_path, capsys):
    root = make_project(tmp_path, {"app/f.py": GOOD})
    assert check_project(root, only_files=["app/nope.py", "app/f.py"]).unmatched == ["app/nope.py"]
    assert main(["check", str(root), "--files", "app/nope.py"]) == 0
    assert "app/nope.py" in capsys.readouterr().err
    assert main(["check", str(root), "--treat-as-marked", "dbso"]) == 2
    assert "dbso" in capsys.readouterr().err
    assert main(["check", str(root), "--treat-as-marked", "dbos"]) == 0  # 已知的规则，项目里没用到也行


def test_constructing_a_project_class_is_still_allowed(tmp_path):
    """构造调用现在解析到项目内的 __init__，编排函数里构造对象仍然允许。"""
    src = '''
    from pystepkit import workflow, step, outcome
    class Payload:
        def __init__(self, m):
            self.m = m
    @step
    async def load(p): ...
    @workflow
    async def wf(m):
        p = Payload(m)
        await load(p)
        return outcome("ok")
    '''
    assert run(tmp_path, src) == []


# ---------- 校准（在一个真实的 DBOS 项目上）----------

def _lines_of(vs, src):
    lines = src.splitlines()
    return [(v.code, lines[v.line - 1].strip()) for v in vs]


def test_dbos_orchestration_calls_are_allowed(tmp_path):
    """DBOS 的持久化编排原语（含在它返回的句柄上调用方法、判断条件里的 patch）放行；直接读写系统库、不记成步骤的
    DBOS 接口（list_queues、delete_queue、read_stream……）和 DBOS.logger 之类的属性链照报。"""
    src = '''
from dbos import DBOS
@DBOS.step()
def st(x): ...
@DBOS.workflow()
def child(x): ...
@DBOS.workflow()
async def wf(x):
    if DBOS.patch("v2"):
        st(x)
    DBOS.sleep(5)
    msg = DBOS.recv("topic", timeout_seconds=5)
    verdict = DBOS.start_workflow(child, x).get_result()
    DBOS.cancel_workflow(x)
    await DBOS.close_stream_async("s")
    DBOS.set_event("k", 1)
    DBOS.logger.info("x")
    if DBOS.list_queues():
        DBOS.delete_queue(x)
    for v in DBOS.read_stream(x, "k"):
        pass
    return verdict
'''
    vs = run(tmp_path, src, treat_as_marked=frozenset({"dbos"}))
    assert _lines_of(vs, src) == [("SM001", 'DBOS.logger.info("x")'), ("SM002", "if DBOS.list_queues():"),
                                  ("SM001", "DBOS.delete_queue(x)"), ("SM001", 'for v in DBOS.read_stream(x, "k"):')]


def test_dbos_name_not_imported_from_dbos_is_still_flagged(tmp_path):
    root = make_project(tmp_path, {
        "app/real.py": '''
            from dbos import DBOS
            @DBOS.workflow()
            def other(): ...
        ''',
        "app/f.py": '''
            from pystepkit import workflow
            from app.fake import DBOS
            @workflow
            def wf():
                DBOS.sleep(1)
                if DBOS.patch("v2"):
                    pass
        '''})
    assert codes(check_project(root, treat_as_marked=frozenset({"dbos"})).violations) == ["SM001", "SM002"]


def test_to_thread_is_judged_by_the_function_it_runs(tmp_path):
    src = '''
import asyncio
from asyncio import to_thread
from dbos import DBOS
@DBOS.step()
def st(x): ...
def helper(x): ...
@DBOS.workflow()
def child(x): ...
@DBOS.workflow()
async def wf(x):
    await asyncio.to_thread(st, x)
    await asyncio.to_thread(DBOS.start_workflow, child, x)
    await asyncio.to_thread(helper, x)
    await to_thread(helper, x)
    if await asyncio.to_thread(st, x):
        pass
@DBOS.step()
async def st2(x):
    await asyncio.to_thread(st, x)
'''
    vs = run(tmp_path, src, treat_as_marked=frozenset({"dbos"}))
    assert _lines_of(vs, src) == [("SM001", "await asyncio.to_thread(helper, x)"),
                                  ("SM001", "await to_thread(helper, x)"),
                                  ("SM003", "await asyncio.to_thread(st, x)")]
    assert vs[0].message == "编排函数只能调用步骤：helper(…) 不是步骤"
    assert vs[2].message == "步骤 st2 里不能调用另一个步骤 st"


def test_methods_on_step_results_through_chains_and_fallbacks(tmp_path):
    """方法链只在两种情况下算操作步骤结果：链从步骤 / 派发 / 编排原语 / 工作流的结果开始（局部名的每次绑定都是这样的结果，
    或链最里面的调用本身就是）；或链上每个调用都是容器 / 字符串取值（参数也可以作根）。带字面量兜底的 (x or {}).get(...) 照旧放行。
    局部名或参数上不经方法链的单个方法调用（s.execute(q)、client.get(...)）仍按原规则放行（遗留问题）。"""
    src = '''
from pystepkit import workflow, step
from sqlalchemy.orm import Session
@step
def load(m): ...
@workflow
def wf(m, handles, client, q):
    info = load(m)
    total = info.summary().value()
    v = load(m).normalize()
    name = m.get("name").strip().lower()
    d = (info or {}).get("detail", "")
    e = (info["x"] or {}).get("y")
    if (info or {}).get("ok"):
        pass
    g = (CONFIG or {}).get("k")
    get_redis().delete(m)
    r = handles.pop(0).get_result()
    s = Session(q)
    rows = s.execute(q).scalars().all()
    data = client.get("http://x").json()
'''
    vs = run(tmp_path, src)
    assert _lines_of(vs, src) == [("SM001", 'g = (CONFIG or {}).get("k")'),
                                  ("SM001", "get_redis().delete(m)"), ("SM001", "get_redis().delete(m)"),
                                  ("SM001", "r = handles.pop(0).get_result()"),
                                  ("SM001", "rows = s.execute(q).scalars().all()"),
                                  ("SM001", "rows = s.execute(q).scalars().all()"),
                                  ("SM001", 'data = client.get("http://x").json()')]


def test_treat_as_marked_rejects_rules_that_yield_nothing_to_check(tmp_path, capsys):
    """检查只看工作流和步骤。只识别接口 / 任务的规则（fastapi、taskiq、配置里只产出 route 的）放进 --treat-as-marked
    什么都不会多查：报错并说明要用 @workflow 标记，不让人以为接口被检查了。"""
    root = make_project(tmp_path, {"app/f.py": """
        from fastapi import APIRouter
        import logging
        router = APIRouter()
        @router.get("/x")
        def x():
            logging.info("x")
    """})
    assert main(["check", str(root), "--treat-as-marked", "fastapi"]) == 2
    cap = capsys.readouterr()
    assert "fastapi" in cap.err and "只识别接口" in cap.err and "@workflow" in cap.err and "共" not in cap.out
    assert main(["check", str(root), "--treat-as-marked", "dbos", "taskiq"]) == 2
    err = capsys.readouterr().err
    assert "taskiq" in err and "只识别任务" in err and "dbos 只" not in err
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"extra_rules": [
        {"name": "flask", "entry_decorators": [["^bp\\\\.route", "route"]]},
        {"name": "temporal", "entry_decorators": [["^activity\\\\.defn", "step"]]}]}), encoding="utf-8")
    assert main(["check", str(root), "--config", str(cfg), "--treat-as-marked", "flask"]) == 2
    assert "flask" in capsys.readouterr().err
    assert main(["check", str(root), "--config", str(cfg), "--treat-as-marked", "temporal", "dbos", "pystepkit"]) == 0
