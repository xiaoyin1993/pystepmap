import ast

import pytest
from conftest import LOOKALIKES, find_fn, make_project
from pystepmap.config import ProjectConfig
from pystepmap.index import build_index
from pystepmap.resolve import Resolver
from pystepmap.rules import ActiveRules

SRC = """
    from pystepkit import workflow, node, condition
    @router.post("/pubsub")
    async def push(): ...
    @broker.task(task_name="t", retry_on_error=True, priority=2)
    async def t(): ...
    @DBOS.workflow()
    def wf(): ...
    @DBOS.step()
    def st(): ...
    @workflow(name="W", title="账户转账", group="转账")
    class W:
        async def run(self, ctx): ...
        @node(title="参数校验")
        async def validate(self, ctx): ...
        @condition()
        def ok(self, ctx): ...
    def plain(): ...
"""


def _rules(tmp_path, files=None, config=None):
    config = config or ProjectConfig()
    idx = build_index(make_project(tmp_path, files or {"app/m.py": SRC}), config)
    return idx, ActiveRules(idx, config)


def test_classify_each_builtin(tmp_path):
    idx, r = _rules(tmp_path)
    c = lambda q: r.classify(find_fn(idx, q))
    assert (c("push").kind, c("push").route) == ("route", "POST /pubsub")
    assert (c("t").kind, c("t").retries) == ("task", True)
    assert c("wf").kind == "workflow" and c("st").kind == "step"
    assert (c("W.run").kind, c("W.run").title, c("W.run").group, c("W.run").marked) == (
        "workflow", "账户转账", "转账", True)
    assert (c("W.validate").kind, c("W.validate").title) == ("step", "参数校验")
    assert c("W.ok").kind == "condition" and c("plain").kind == "function"
    assert sorted(r.names) == ["dbos", "fastapi", "pystepkit", "taskiq"]


def test_tasks_map(tmp_path):
    idx, r = _rules(tmp_path)
    assert r.tasks["t"] == find_fn(idx, "t").key
    assert r.tasks["wf"] == find_fn(idx, "wf").key
    assert r.tasks["W"] == find_fn(idx, "W.run").key
    assert "st" not in r.tasks and "plain" not in r.tasks
    assert "run" not in r.task_names  # 类形式工作流以类名登记，入口方法的裸名不登记


def test_class_name_is_a_task_alias_only_for_pystepkit_class_workflows(tmp_path):
    idx, r = _rules(tmp_path, {"a.py": """
        class Svc:
            @DBOS.workflow()
            def wf(self): ...
            @DBOS.workflow()
            def other(self): ...
        @broker.task
        async def dup(): ...
    """, "b.py": "@broker.task\nasync def dup(): ...\n"})
    assert "Svc" not in r.task_names
    assert r.tasks["wf"] == find_fn(idx, "Svc.wf").key
    assert "dup" in r.task_names and "dup" not in r.tasks  # 两个任务同名：不按名字对应到任何一个


ROUTES = """
    from fastapi import APIRouter, FastAPI
    import fastapi
    from celery import Celery
    api = FastAPI()
    v1 = APIRouter()
    orders = fastapi.APIRouter(prefix="/orders", tags=["o"])
    router = APIRouter(prefix="/r")
    worker = Celery()
    @v1.delete("/x/{id}")
    async def d(): ...
    @api.get("/health")
    async def h(): ...
    @api.websocket("/ws")
    async def ws(sock): ...
    @orders.api_route("/multi", methods=["GET", "POST"])
    async def multi(): ...
    @orders.head("/h")
    async def head(): ...
    @router.options("/o")
    async def opt(): ...
    @router.api_route("/any")
    async def any_route(): ...
    @router.post(path="/kw")
    async def kw(): ...
    @worker.get("/not-fastapi")
    def nope(): ...
"""


def test_fastapi_routes_on_any_app_or_router_instance(tmp_path):
    """模块级的 FastAPI() / APIRouter() 实例上的路由装饰器，不论变量叫什么；含 head、options、websocket、api_route；
    APIRouter(prefix=...) 的字面量前缀进标题。"""
    idx, r = _rules(tmp_path, {"app/api.py": ROUTES})
    got = {q: (r.classify(find_fn(idx, q)).kind, r.classify(find_fn(idx, q)).route)
           for q in ("d", "h", "ws", "multi", "head", "opt", "any_route", "kw", "nope")}
    assert got == {
        "d": ("route", "DELETE /x/{id}"), "h": ("route", "GET /health"), "ws": ("route", "WS /ws"),
        "multi": ("route", "GET,POST /orders/multi"), "head": ("route", "HEAD /orders/h"),
        "opt": ("route", "OPTIONS /r/o"), "any_route": ("route", "GET /r/any"), "kw": ("route", "POST /r/kw"),
        "nope": ("function", None)}


def test_fastapi_rule_is_enabled_by_an_instance_route_alone(tmp_path):
    idx, r = _rules(tmp_path, {"app/api.py": """
        from fastapi import FastAPI
        api = FastAPI()
        @api.get("/health")
        async def h(): ...
    """})
    assert r.names == ["fastapi"] and r.classify(find_fn(idx, "h")).route == "GET /health"


def test_fastapi_routes_on_an_instance_local_to_the_enclosing_function(tmp_path):
    """create_app() 里的局部实例（不论变量名）上的路由也算；APIRouter 的前缀进标题；外层的外层也找；最近一层把名字
    赋成别的东西、或者根本不是 FastAPI 的实例时不算；单凭这种路由也能启用 fastapi 规则。"""
    idx, r = _rules(tmp_path, {"app/api.py": """
        from fastapi import APIRouter, FastAPI
        import fastapi
        def create_api(store):
            api = FastAPI(title="x")
            sub: APIRouter = fastapi.APIRouter(prefix="/v1")
            other = make_thing()
            @api.get("/health")
            def health(): ...
            @sub.post("/items")
            async def add_item(): ...
            @other.get("/no")
            def no(): ...
            def inner():
                @api.delete("/deep/{id}")
                def deep(): ...
                api2 = FastAPI()
                @api2.put("/near")
                def near(): ...
            def shadow():
                api = object()
                @api.get("/shadowed")
                def shadowed(): ...
            return api
        @api.get("/outside")
        def outside(): ...
    """})
    fns = {f.qualname.rpartition(".")[2]: f for f in idx.functions.values()}
    got = {q: (r.classify(fns[q]).kind, r.classify(fns[q]).route)
           for q in ("health", "add_item", "no", "deep", "near", "shadowed", "outside")}
    assert r.names == ["fastapi"]
    assert got == {
        "health": ("route", "GET /health"), "add_item": ("route", "POST /v1/items"), "no": ("function", None),
        "deep": ("route", "DELETE /deep/{id}"), "near": ("route", "PUT /near"), "shadowed": ("function", None),
        "outside": ("function", None)}


def test_fastapi_local_instance_follows_the_nearest_binding_of_the_name(tmp_path):
    """最近绑定这个名字的那一层说了算：参数、解包赋值、循环变量把名字绑成别的东西时不是路由（模块级有同名的实例也
    一样）；x = y = FastAPI()、海象赋值算实例；后来又被解包赋值盖掉的不算。"""
    idx, r = _rules(tmp_path, {"app/api.py": """
        from fastapi import FastAPI
        api = FastAPI()
        def by_param(api):
            @api.get("/param")
            def param(): ...
        def by_object():
            api = object()
            @api.get("/object")
            def obj(): ...
        def outer():
            api = FastAPI()
            def inner(api):
                @api.get("/inner-param")
                def inner_param(): ...
        def chained():
            a = b = FastAPI()
            @b.get("/chained")
            def chain(): ...
            if (w := FastAPI()):
                @w.get("/walrus")
                def walrus(): ...
        def overwritten():
            a = FastAPI()
            a, y = object(), 1
            @a.get("/unpacked")
            def unpacked(): ...
            for b in range(3):
                pass
            b = FastAPI()
            for b in range(3):
                @b.get("/loop")
                def loop(): ...
        def uses_module_level():
            @api.get("/module")
            def module(): ...
    """})
    fns = {f.qualname.rpartition(".")[2]: f for f in idx.functions.values()}
    got = {q: r.classify(fns[q]).route for q in ("param", "obj", "inner_param", "chain", "walrus", "unpacked", "loop", "module")}
    assert got == {"param": None, "obj": None, "inner_param": None, "chain": "GET /chained", "walrus": "GET /walrus",
                   "unpacked": None, "loop": None, "module": "GET /module"}


@pytest.mark.parametrize("imports,wf,st", [
    ("from pystepkit import workflow, step", "@workflow(title='W')", "@step"),
    ("import pystepkit as fk", "@fk.workflow(title='W')", "@fk.node()"),
    ("from pystepkit import workflow as wf, condition as cond", "@wf(title='W')", "@cond"),
])
def test_pystepkit_marks_are_recognised_by_import_origin(tmp_path, imports, wf, st):
    idx, r = _rules(tmp_path, {"a.py": f"{imports}\n{wf}\ndef w(): ...\n{st}\ndef s(): ...\n"})
    w, s = r.classify(find_fn(idx, "w")), r.classify(find_fn(idx, "s"))
    assert (w.kind, w.marked, w.title) == ("workflow", True, "W")
    assert s.kind in ("step", "condition") and s.marked and r.names == ["pystepkit"]


def test_decorators_with_pystepkit_names_from_elsewhere_are_not_marks(tmp_path):
    """Temporal 的 workflow、Metaflow 的 step、项目自己的 condition 不是 pystepkit 标记：不标记、不启用 pystepkit 规则。"""
    idx, r = _rules(tmp_path, {"a.py": LOOKALIKES})
    assert "pystepkit" not in r.names
    for q in ("Greeting.run", "Flow.start", "Flow.end", "cond", "bare"):
        info = r.classify(find_fn(idx, q))
        assert (info.kind, info.marked) == ("function", False), q


def test_only_present_rules_active(tmp_path):
    _, r = _rules(tmp_path, {"a.py": "@app.get('/x')\ndef f(): ...\n"})
    assert r.names == ["fastapi"]


def test_extra_rules(tmp_path):
    cfg = ProjectConfig(extra_rules=[{
        "name": "mine", "entry_decorators": [[r"^cron\b", "task"]],
        "dispatch": [{"methods": ["fire"], "task_from": "arg0", "dynamic_ok": False}]}])
    idx, r = _rules(tmp_path, {"a.py": "@cron('* * * * *')\ndef job(): ...\ndef h(bus):\n    bus.fire(job)\n"}, cfg)
    assert "mine" in r.names and r.classify(find_fn(idx, "job")).kind == "task"
    h = find_fn(idx, "h")
    d = r.dispatch(h.node.body[0].value, h, Resolver(idx))
    assert (d.name, d.target, d.how) == ("job", find_fn(idx, "job").key, "exact")


@pytest.mark.parametrize("src,expected", [
    ("apply_label.kiq(a, b)", "apply_label"),
    ("release.kicker().with_labels(delay=5).kiq(x)", "release"),
    ("manual_kicker(process_email).kiq(*args)", "process_email"),
    ("DBOS.start_workflow(stage_workflow, sid)", "stage_workflow"),
    ("DBOS.start_workflow(run_id)", None),
    ("DBOS.enqueue_workflow_async(QUEUE, stage_workflow, sid)", "stage_workflow"),  # DBOS 2.x 的异步入队
    ("DBOS.enqueue_workflow_async(QUEUE, run_id)", False),
    ("kicker.kiq(*args)", None),
    ("queue.enqueue(item)", False),
    ("svc.save(x)", False),
])
def test_dispatch_target(tmp_path, src, expected):
    """派发调用里被派发的任务名；派发但目标未知为 None；不是派发为 False。"""
    idx, r = _rules(tmp_path, {"a.py": f"""
        @broker.task
        async def apply_label(): ...
        @broker.task
        async def release(): ...
        @broker.task
        async def process_email(): ...
        @DBOS.workflow()
        def stage_workflow(): ...
        def h(run_id, QUEUE, kicker, queue, svc, manual_kicker, args, sid, a, b, x, item):
            {src}
    """})
    fn = find_fn(idx, "h")
    d = r.dispatch(fn.node.body[0].value, fn, Resolver(idx))
    assert (False if d is None else d.name) == expected


def test_noise(tmp_path):
    _, r = _rules(tmp_path)
    n = lambda s: r.is_noise(ast.parse(s).body[0].value)
    assert n('logger.bind(a=1).info("x")')
    assert n('email_total.labels(v="x").inc()')
    assert n("len(x)")
    assert not n("svc.save(x)")


ROUTE_WORKFLOWS = """
    from fastapi import APIRouter
    from dbos import DBOS
    from pystepkit import step, workflow
    router = APIRouter(prefix="/users")
    @workflow(title="查看用户")
    @router.get("/{uid}")
    def above(uid): ...
    @router.post("/")
    @workflow(title="新建用户")
    def below(body): ...
    @router.put("/{uid}")
    @DBOS.workflow()
    def durable(uid): ...
    @workflow(title="定时清理")
    def no_route(): ...
    @router.delete("/{uid}")
    def plain_route(uid): ...
"""


def test_workflow_on_a_route_keeps_the_route_whichever_decorator_comes_first(tmp_path):
    """同一个函数既有工作流标记又有路由装饰器：类型是工作流（带标记、会被检查），"方法 路径" 照样记在 route 里；
    两个装饰器谁在上面都一样。"""
    idx, r = _rules(tmp_path, {"app/m.py": ROUTE_WORKFLOWS})
    c = lambda q: r.classify(find_fn(idx, q))
    got = {q: (c(q).kind, c(q).rule, c(q).marked, c(q).title, c(q).route)
           for q in ("above", "below", "durable", "no_route", "plain_route")}
    assert got == {
        "above": ("workflow", "pystepkit", True, "查看用户", "GET /users/{uid}"),
        "below": ("workflow", "pystepkit", True, "新建用户", "POST /users/"),
        "durable": ("workflow", "dbos", False, None, "PUT /users/{uid}"),
        "no_route": ("workflow", "pystepkit", True, "定时清理", None),
        "plain_route": ("route", "fastapi", False, None, "DELETE /users/{uid}"),
    }


MARK_CONSTANTS = {
    "app/__init__.py": "",
    "app/names.py": 'GROUP = "接入与治理 · Key 与场景"\nTWICE = "一"\nTWICE = "二"\n',
    "app/m.py": """
        from typing import Final
        from pystepkit import workflow, step
        from app import names
        from app.names import GROUP as G, TWICE
        from elsewhere import FAR
        LOCAL: Final = "本模块的分组"
        TITLE = "删除 Key"
        def pick(): ...
        @workflow(title=TITLE, group=G)
        def imported(): ...
        @workflow(title="查看", group=names.GROUP)
        def dotted(): ...
        @workflow(title="列出", group=LOCAL)
        def local(): ...
        @step(title=TITLE)
        def a_step(): ...
        @workflow(title="甲", group=TWICE)
        def rebound(): ...
        @workflow(title=f"乙{TITLE}", group=pick())
        def computed(): ...
        @workflow(title="丙", group=FAR)
        def outside(): ...
    """,
}


def test_mark_arguments_written_as_module_level_string_constants_are_read(tmp_path):
    """标记的 title / group 写成模块级的字符串常量（本模块的、from 导入的、模块.常量）时照样读得出来。"""
    idx, r = _rules(tmp_path, MARK_CONSTANTS)
    c = lambda q: r.classify(find_fn(idx, q))
    assert (c("imported").title, c("imported").group) == ("删除 Key", "接入与治理 · Key 与场景")
    assert c("dotted").group == "接入与治理 · Key 与场景" and c("local").group == "本模块的分组"
    assert c("a_step").title == "删除 Key"


def test_mark_arguments_that_cannot_be_read_are_reported_not_silently_dropped(tmp_path):
    """读不出来的（绑定了两次的名字、算出来的值、项目外的常量）：值当作没写，但记下是哪一处，分析结果里要说。"""
    idx, r = _rules(tmp_path, MARK_CONSTANTS)
    c = lambda q: r.classify(find_fn(idx, q))
    assert [c(q).group for q in ("rebound", "computed", "outside")] == [None, None, None] and c("computed").title is None
    c("imported"), c("rebound")  # 多调几次不重复记
    notes = r.mark_notes()
    assert len(notes) == 4 and all("app/m.py:" in n and "字符串字面量" in n for n in notes)
    assert "group=TWICE" in notes[0] and "按文件名" in notes[0]
    assert any("title=f'乙{TITLE}'" in n for n in notes) and any("group=pick()" in n for n in notes) and "group=FAR" in notes[-1]
