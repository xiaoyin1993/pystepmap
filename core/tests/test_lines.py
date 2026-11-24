from conftest import analyze_files, find_fn, make_project, node_by
from pystepmap.config import ProjectConfig
from pystepmap.model import parse_key


def test_chain_common_task_and_cycle(tmp_path):
    files = {"app/tasks.py": """
        @broker.task()
        async def alert(): ...
        @broker.task()
        async def label():
            await alert.kiq()
        @broker.task()
        async def analyze():
            await _fanout()
            await alert.kiq()
        async def _fanout():
            await label.kiq()
        @broker.task()
        async def scan():
            await analyze.kiq()
            await scan.kiq()
        @router.post("/pubsub")
        async def push():
            await analyze.kiq()
            await alert.kiq()
    """}
    g = analyze_files(tmp_path, files, ProjectConfig(common_task_threshold=3))
    by_title = {l.title: l for l in g.lines}
    assert sorted(by_title) == ["POST /pubsub", "alert", "scan"]
    assert [parse_key(k)[1] for k in g.common_tasks] == ["alert"]
    push = by_title["POST /pubsub"].root
    assert [parse_key(c.function)[1] for c in push.children] == ["analyze"]
    assert [parse_key(c.function)[1] for c in push.children[0].children] == ["label"]
    scan = by_title["scan"].root
    assert [(parse_key(c.function)[1], c.goto) for c in scan.children] == [("analyze", False), ("scan", True)]
    fan = node_by(find_fn(g, "analyze"), subtitle="_fanout")
    assert [parse_key(k)[1] for k in fan.calls[0].dispatches] == ["label"]


def test_groups_and_other_entries(tmp_path):
    files = {
        "app/api/gmail.py": """
            @router.get("/ping")
            async def ping():
                \"\"\"探活\"\"\"
            @router.post("/push")
            async def push():
                pass
        """,
        "app/tasks.py": """
            @broker.task()
            async def lonely(): ...
        """,
        "app/smtp.py": """
            class Handler:
                async def handle_DATA(self, server):
                    await lonely_job.kiq()
            @broker.task()
            async def lonely_job(): ...
        """,
        "app/pay.py": """
            from pystepkit import workflow
            @workflow(title="转账", group="转账")
            def transfer(): ...
        """,
    }
    g = analyze_files(tmp_path, files)
    rows = {l.title: (l.group, l.kind) for l in g.lines}
    assert rows == {
        "探活": ("gmail", "route"),
        "POST /push": ("gmail", "route"),
        "lonely": ("后台任务", "task"),
        "handle_DATA": ("其他入口", "function"),
        "转账": ("转账", "workflow"),
    }
    assert [(l.group, l.title) for l in g.lines] == sorted((l.group, l.title) for l in g.lines)
    other = next(l for l in g.lines if l.group == "其他入口")
    assert [parse_key(c.function)[1] for c in other.root.children] == ["lonely_job"]
    assert not [l for l in g.lines if l.title == "lonely_job"]


def test_functions_referenced_as_values_are_not_other_entries(tmp_path):
    """被当作值交出去的函数（调用的参数、字典的值、赋值的右边）是别处在调用它：不单独成"其他入口"线。
    只在函数自己里面引用自己不算。"""
    g = analyze_files(tmp_path, {"app/t.py": """
        import asyncio
        @broker.task()
        async def job(): ...
        async def as_arg():
            await job.kiq()
        async def in_dict():
            await job.kiq()
        async def assigned():
            await job.kiq()
        class Svc:
            async def method(self):
                await job.kiq()
            def wire(self, app):
                app.add(self.method)
        async def threaded():
            await job.kiq()
        async def lonely():
            await job.kiq()
            me = lonely
        HANDLERS = {"a": in_dict}
        CALLBACKS = [assigned]
        def register(app):
            app.add(handler=as_arg)
        async def runner():
            await asyncio.to_thread(threaded)
    """})
    assert sorted(l.title for l in g.lines if l.group == "其他入口") == ["lonely", "runner"]
    runner = next(l for l in g.lines if l.title == "runner")
    assert [parse_key(c.function)[1] for c in runner.root.children] == ["job"]  # 经 to_thread 调用的 threaded 派发的


def test_dispatch_to_a_function_that_is_not_an_entry_still_expands(tmp_path):
    """派发表达式解析到一个没被识别为任务、也不单独成线的项目函数时（比如任务装饰器没被认出来），功能线照样展开
    它派发的任务。"""
    g = analyze_files(tmp_path, {
        "app/a.py": "@broker.task()\nasync def notify(): ...\n@broker.task()\nasync def job(): ...\n",
        "app/b.py": """
            from app.a import job
            async def notify():
                await job.kiq()
            async def helper():
                await notify()
            @router.post("/go")
            async def go():
                await notify.kiq()
        """})
    line = next(l for l in g.lines if l.title == "POST /go")
    [child] = line.root.children
    assert child.function == "app/b.py::notify@3" and [c.function for c in child.children] == ["app/a.py::job@4"]


def test_task_only_dispatching_itself_still_listed(tmp_path):
    g = analyze_files(tmp_path, {"app/t.py": """
        @broker.task()
        async def loop():
            await loop.kiq()
    """})
    assert [l.title for l in g.lines] == ["loop"]
    assert [(c.goto, parse_key(c.function)[1]) for c in g.lines[0].root.children] == [(True, "loop")]


def test_two_task_mutual_cycle_gets_one_line(tmp_path):
    g = analyze_files(tmp_path, {"app/t.py": """
        @broker.task()
        async def a():
            await b.kiq()
        @broker.task()
        async def b():
            await a.kiq()
    """})
    assert len(g.lines) == 1
    root = g.lines[0].root
    assert parse_key(root.function)[1] == "a" and g.lines[0].id == root.function
    assert [parse_key(c.function)[1] for c in root.children] == ["b"]
    inner = root.children[0].children
    assert [(parse_key(c.function)[1], c.goto) for c in inner] == [("a", True)]


def test_three_task_cycle_gets_one_line(tmp_path):
    g = analyze_files(tmp_path, {"app/t.py": """
        @broker.task()
        async def a():
            await b.kiq()
        @broker.task()
        async def b():
            await c.kiq()
        @broker.task()
        async def c():
            await a.kiq()
    """})
    assert len(g.lines) == 1
    assert [parse_key(g.lines[0].root.function)[1]] == ["a"]


def test_extra_entries_turn_named_plain_functions_into_lines(tmp_path):
    """配置里 extra_entries 点名的普通函数各成一条线（分组用配置给的，没给时"其他入口"），标题取函数说明的第一句；
    嵌套在别的函数里的、已经是入口或步骤的不受影响；没点名的不成线。"""
    import pytest
    from pystepmap.config import ConfigError, parse_config

    files = {"tool/cli.py": '''
        def _cmd_build(args):
            """构建产物。细节在后面。"""
            helper()
            def _cmd_inner(): ...
        def _cmd_clean(args): ...
        def helper(): ...
        class Tools:
            def list_things(self):
                """列出东西"""
            def _private(self): ...
    '''}
    cfg = parse_config({"extra_entries": [{"match": "tool/cli.py::_cmd_*", "group": "命令行"},
                                          {"match": "*::Tools.[!_]*"}]})
    g = analyze_files(tmp_path, files, cfg)
    assert sorted((ln.group, ln.title, ln.kind) for ln in g.lines) == [
        ("其他入口", "列出东西", "function"), ("命令行", "_cmd_clean", "function"), ("命令行", "构建产物", "function")]
    # 没点名时：关掉兜底就没有功能线；默认（兜底开着）把没有人调用的顶层函数当入口，见 test_entries.py
    assert analyze_files(tmp_path / "again", files, parse_config({"fallback_entries": False})).lines == []
    for bad in ({"extra_entries": "x"}, {"extra_entries": [{"match": ""}]}, {"extra_entries": [{"group": "g"}]},
                {"extra_entries": [{"match": "a", "other": 1}]}, {"extra_entries": [{"match": "a", "group": 3}]}):
        with pytest.raises(ConfigError, match="extra_entries"):
            parse_config(bad)


def test_workflow_on_a_route_is_grouped_and_titled_like_a_route(tmp_path):
    """标了 @workflow 的接口：没写分组时和别的接口一样按文件名分组（不进"后台任务"）；没写标题时标题用 "方法 路径"。"""
    g = analyze_files(tmp_path, {"app/users.py": """
        from fastapi import APIRouter
        from pystepkit import workflow
        router = APIRouter(prefix="/users")
        @workflow(title="查看用户")
        @router.get("/{uid}")
        def get_user(uid): ...
        @router.post("/")
        @workflow
        def create_user(body): ...
        @workflow(title="删除用户", group="系统管理")
        @router.delete("/{uid}")
        def delete_user(uid): ...
        @workflow(title="定时清理")
        def sweep(): ...
    """})
    assert sorted((ln.title, ln.kind, ln.group) for ln in g.lines) == [
        ("POST /users/", "workflow", "users"), ("删除用户", "workflow", "系统管理"),
        ("定时清理", "workflow", "后台任务"), ("查看用户", "workflow", "users")]
    assert find_fn(g, "get_user").route == "GET /users/{uid}" and find_fn(g, "sweep").route is None


def test_a_group_written_as_a_constant_groups_the_line_and_an_unreadable_one_is_reported(tmp_path):
    """group=GROUP（模块级字符串常量）和字面量一样分组；读不出来的写法落到默认的分组（接口按文件名，这里不是接口），分析的说明里指出是哪一行。"""
    from pystepmap.analyze import analyze_with_notes
    make_project(tmp_path, {"keys.py": """
        from pystepkit import workflow
        GROUP = "Key 与场景"
        @workflow(title="删除 Key", group=GROUP)
        def delete_key(): ...
        @workflow(title="列出 Key", group=GROUP.strip())
        def list_keys(): ...
    """})
    g, notes = analyze_with_notes(tmp_path)
    assert {ln.title: ln.group for ln in g.lines} == {"删除 Key": "Key 与场景", "列出 Key": "后台任务"}
    assert len(notes.marks) == 1 and "keys.py:6" in notes.marks[0] and "group=GROUP.strip()" in notes.marks[0]
