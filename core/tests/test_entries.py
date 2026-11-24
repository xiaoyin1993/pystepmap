"""不靠标记、不靠配置认入口（entries.py），以及 Flask / Celery 的识别规则。"""

from __future__ import annotations

from conftest import analyze_files, make_project
from pystepmap.analyze import analyze_with_notes
from pystepmap.config import parse_config
from pystepmap.entries import entry_notes, unlisted_notes, unlisted_roots


def rows(g) -> list[tuple[str, str, str]]:
    return sorted((ln.group, ln.title, ln.kind) for ln in g.lines)


def test_flask_routes_are_entries_with_method_and_path(tmp_path):
    g = analyze_files(tmp_path, {"web.py": '''
        from flask import Flask, Blueprint, jsonify, request
        app = Flask(__name__)
        bp = Blueprint("todos", __name__, url_prefix="/api")

        @app.route("/health")
        def health():
            return "ok"

        @bp.route("/todos", methods=["POST", "PUT"])
        def add_todo():
            """新增待办"""
            data = request.get_json()
            if not data.get("title"):
                return jsonify(error="标题不能为空"), 400
            if data.get("secret"):
                return {"error": "不允许"}, 403
            if data.get("weird"):
                return "", 404
            return jsonify(ok=True), 201
    '''})
    assert rows(g) == [("web", "GET /health", "route"), ("web", "新增待办", "route")]
    add = next(fg for fg in g.functions.values() if fg.qualname == "add_todo")
    assert add.route == "POST,PUT /api/todos" and add.rule == "flask"
    ends = [(n.title, n.end_kind) for n in add.nodes if n.type == "end" and n.line]
    # 状态码 400 及以上的 `return 响应, 状态码` 是失败的结局，标题是"状态码 说明"；201 照旧是正常返回
    assert ends == [("400 标题不能为空", "failed"), ("403 不允许", "failed"), ("404 Not Found", "failed"),
                    ("返回 jsonify(ok=True), 201", "ok")]
    # 取请求、拼响应的框架函数不算步骤
    assert not [n for n in add.nodes if n.type == "step"]


def test_status_tuples_only_count_in_routes(tmp_path):
    g = analyze_files(tmp_path, {"m.py": '''
        def pair():
            return "x", 404
        if __name__ == "__main__":
            pair()
    '''})
    [fg] = g.functions.values()
    assert [(n.title, n.end_kind) for n in fg.nodes if n.type == "end" and n.line] == [('返回 "x", 404', "ok")]


def test_celery_tasks_and_their_dispatch(tmp_path):
    g = analyze_files(tmp_path, {"jobs.py": '''
        from celery import Celery, shared_task
        app = Celery("jobs")

        @shared_task
        def send_mail(uid):
            """发邮件"""

        @app.task(bind=True)
        def settle(self, day):
            """日终结算"""
            send_mail.delay(day)
            send_mail.apply_async(args=[day])
    '''})
    assert {fg.qualname: (fg.kind, fg.rule) for fg in g.functions.values()} == {
        "send_mail": ("task", "celery"), "settle": ("task", "celery")}
    # 被别的入口派发的任务并进那条线
    assert rows(g) == [("后台任务", "日终结算", "task")]
    settle = next(fg for fg in g.functions.values() if fg.qualname == "settle")
    send = next(k for k, fg in g.functions.items() if fg.qualname == "send_mail")
    assert [(r.is_dispatch, r.target) for n in settle.nodes for r in n.calls] == [(True, send), (True, send)]


def test_taskiq_projects_keep_their_rule(tmp_path):
    g = analyze_files(tmp_path, {"t.py": '''
        from taskiq import InMemoryBroker
        broker = InMemoryBroker()
        @broker.task
        async def job(): ...
    '''})
    assert [(fg.kind, fg.rule) for fg in g.functions.values()] == [("task", "taskiq")]


SCRIPT = {"tool.py": '''
    import asyncio
    import click
    import requests
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler()

    def fetch(code):
        return requests.get(code)

    @sched.scheduled_job("cron", hour=9)
    def morning_report():
        """每天早上发行情日报"""
        fetch("A")

    @click.command()
    def backfill():
        fetch("B")

    # pystepmap: 入口 手工对账
    def reconcile(day):
        return fetch(day)

    # pystepmap：entry
    @sched.scheduled_job("cron", hour=1)
    def nightly():
        fetch("N")

    async def main():
        fetch("M")

    def now():
        return "2026"

    def unused():
        fetch("X")

    if __name__ == "__main__":
        print(now())
        asyncio.run(main())
'''}


def test_commands_handlers_main_and_comment_marks_are_entries(tmp_path):
    g = analyze_files(tmp_path, SCRIPT)
    assert rows(g) == [
        ("其他入口", "nightly（tool.py）", "function"), ("其他入口", "手工对账", "function"),
        ("其他入口", "每天早上发行情日报", "function"), ("命令行", "backfill（tool.py）", "function"),
        ("脚本入口", "main（tool.py）", "function")]
    how = {fg.qualname: fg.rule for fg in g.functions.values()}
    assert how == {"fetch": None, "morning_report": "handler", "backfill": "command", "reconcile": "comment",
                   "nightly": "comment", "main": "main", "now": None, "unused": None}  # now()：没有步骤的小工具不算入口
    # 认出了入口就不做兜底：没有人调用的 unused 不成线，但在"不在功能线上的函数"里提到，带上怎么点名
    assert entry_notes(g) == []
    [note] = unlisted_notes(g)
    assert "函数 1 个：unused（tool.py:" in note and "# pystepmap: 入口 <标题>" in note
    start = next(fg for fg in g.functions.values() if fg.qualname == "reconcile").nodes[0]
    assert (start.type, start.title) == ("start", "手工对账")


def test_a_main_that_only_drives_a_recognised_entry_is_not_a_line(tmp_path):
    g = analyze_files(tmp_path, {"flow.py": '''
        import asyncio
        from pystepkit import workflow, step

        @step(title="干活")
        async def work(): ...

        @workflow(title="主流程")
        async def run():
            await work()

        async def _demo():
            await run()

        if __name__ == "__main__":
            asyncio.run(_demo())
    '''})
    assert rows(g) == [("后台任务", "主流程", "workflow")]


def test_fallback_gives_lines_when_nothing_is_recognised(tmp_path):
    files = {"lib.py": '''
        def load(path):
            return open(path).read()

        def report(path):
            """生成报表"""
            return load(path)

        def _private():
            return load("x")

        def handler(event):
            return load(event)

        HANDLERS = {"x": handler}
    '''}
    g = analyze_files(tmp_path, files)
    # 没有人调用、也没有被当作值交出去的顶层函数各成一条线（被 report 调用的 load、放进注册表的 handler 不算）
    assert rows(g) == [("其他入口", "_private（lib.py）", "function"), ("其他入口", "生成报表", "function")]
    [note] = entry_notes(g)
    assert note.startswith("入口是推断的：") and "2 个顶层函数" in note and "# pystepmap: 入口 <标题>" in note
    off = analyze_files(tmp_path / "off", files, parse_config({"fallback_entries": False}))
    assert off.lines == [] and entry_notes(off)[0].startswith("没有功能线：没有认出")


def test_fallback_widens_until_something_is_a_line(tmp_path):
    cycle = analyze_files(tmp_path / "a", {"m.py": '''
        def ping(n):
            return pong(n)
        def pong(n):
            return ping(n)
    '''})
    assert [ln.title for ln in cycle.lines] == ["ping（m.py）", "pong（m.py）"]
    methods = analyze_files(tmp_path / "b", {"m.py": '''
        class Svc:
            def run(self):
                """跑一遍"""
                self._step()
            def _step(self): ...
    '''})
    assert [ln.title for ln in methods.lines] == ["跑一遍"]
    empty = analyze_files(tmp_path / "c", {"m.py": "X = 1\n"})
    assert empty.lines == [] and entry_notes(empty)[0].startswith("没有功能线：项目里没有函数")


def test_no_fallback_once_any_real_entry_exists(tmp_path):
    g = analyze_files(tmp_path, {"api.py": '''
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/x")
        def x(): ...
        def orphan():
            return x()
    '''})
    assert rows(g) == [("api", "GET /x", "route")] and entry_notes(g) == []


def test_config_rejects_non_boolean_switches():
    import pytest
    from pystepmap.config import ConfigError

    for bad in ({"fallback_entries": "no"}, {"external_calls": 1}):
        with pytest.raises(ConfigError, match="必须是 true 或 false"):
            parse_config(bad)


def test_functions_mentioned_elsewhere_are_not_listed_as_unlisted(tmp_path):
    """不在功能线上、没人直接调用的函数里，项目里别处提到过的不列：当依赖 / 回调 / 注册表的值交出去的、当装饰器用的、
    模块顶层调用的、算出来再调的——有人用它们，不是没认出来的入口。只有哪儿都没提到的才列。"""
    root = make_project(tmp_path, {"app.py": '''
        from fastapi import Depends, FastAPI
        import jobs
        from jobs import deco, pause, resume

        app = FastAPI()

        def get_session():
            if app:
                return 1

        @app.get("/x")
        async def read(s=Depends(get_session)):
            await (resume if s else pause)("a")
            return s

        @deco
        def wrapped(): ...

        KINDS = {"a": (jobs.user_ids, 1)}
        jobs.migrate()
        ''', "jobs.py": '''
        def deco(f):
            if f:
                return f

        def user_ids(q):
            if q:
                return [1]

        def migrate():
            if deco:
                return 2

        def pause(code):
            if code:
                return 3

        def resume(code):
            if code:
                return 4

        def forgotten(x):
            if x:
                return [forgotten]  # 只在自己里面提到自己：不算
        '''})
    g, notes = analyze_with_notes(root)
    assert [fg.qualname for fg in unlisted_roots(g, limit=None)[0]] == [
        "get_session", "deco", "user_ids", "migrate", "pause", "resume", "forgotten"]
    assert [fg.qualname for fg in unlisted_roots(g, mentioned=notes.mentioned)[0]] == ["forgotten"]
    [note] = unlisted_notes(g, notes.mentioned)
    assert "项目里也没有别处用到的函数 1 个：forgotten（jobs.py:" in note
