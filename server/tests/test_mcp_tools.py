"""MCP 工具（mcp_tools.Tools）：直接调用每个工具的方法，看返回给 AI 的中文文本。

小示例项目 shop 写在 tmp_path 里：两条功能线（工作流"下单"、任务"下单后结算"），一个派发（结算派发"发送回执"），
一个被调函数（"创建订单"步骤调用 make_order_no）。分析在本进程里跑（把 flows.run_in_subprocess 换成
worker.analyze_directory）；真起子进程的在 test_worker.py、test_mcp_stdio.py。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from pystepmap.diff import diff_from_json, diff_text
from pystepmap.model import from_json
from pystepmap.outline import function_outline, line_mermaid, line_outline
from pystepmap.query import find_by_location
from pystepmap_server import flows, mcp_tools, procinfo, testflows, worker
from pystepmap_server.flows import acknowledge_changes, reanalyze_project
from pystepmap_server.locks import try_lock_project, unlock_project
from pystepmap_server.mcp_tools import (
    MAX_CHARS,
    TOOL_SPECS,
    ToolError,
    Tools,
    _summary_text,
)
from pystepmap_server.store import Store

REPO = Path(__file__).resolve().parents[2]
TRANSFER = REPO / "examples" / "transfer"

LIB = '''\
def make_order_no(order):
    """生成订单号"""
    return f"O-{order['id']}"


def notify(order_no):
    """通知用户"""
    print(order_no)


def charge(order):
    """扣款"""
    return order.get("paid")


def unused(x):
    if x:
        return 1
    return 2
'''

ORDERS = '''\
from pystepkit import condition, outcome, step, workflow

from app.lib import make_order_no


@step(title="查询库存")
def load_stock(order: dict) -> dict:
    return {"left": 3, "wanted": order["count"]}


@condition(title="库存不够？")
def lacks_stock(stock: dict) -> bool:
    return stock["left"] < stock["wanted"]


@step(title="创建订单")
def create_order(order: dict) -> str:
    return make_order_no(order)


@workflow(title="下单", group="订单")
def place_order(order: dict):
    stock = load_stock(order)
    if lacks_stock(stock):
        return outcome("failed", title="库存不够，没有下单")
    order_no = create_order(order)
    return outcome("ok", title="下单完成", order_no=order_no)
'''

TASKS = '''\
from app.lib import charge, notify


@broker.task()
async def send_receipt(order_no):
    """发送回执"""
    notify(order_no)


@broker.task()
async def settle(order):
    """下单后结算"""
    paid = charge(order)
    if paid is None:
        return "unpaid"
    await send_receipt.kiq(order)
'''

PLACE = "line:app/orders.py::place_order"
SETTLE = "line:app/tasks.py::settle"
STALE = "⚠ 代码已变更"


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def line_no(path: Path, text: str) -> int:
    """文件里第一处含 text 的行号（从 1 起）。"""
    return next(i for i, row in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if text in row)


@pytest.fixture(autouse=True)
def in_process_analysis(monkeypatch):
    monkeypatch.setattr(flows, "run_in_subprocess",
                        lambda root, config, *, timeout, cwd: worker.analyze_directory(root, config))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home" / "pystepmap.db")


@pytest.fixture
def shop(tmp_path) -> Path:
    return write(tmp_path / "work" / "shop", {"app/__init__.py": "", "app/lib.py": LIB, "app/orders.py": ORDERS,
                                              "app/tasks.py": TASKS})


@pytest.fixture
def tools(store, shop) -> Tools:
    """shop 已经添加；当前目录是 shop。"""
    t = Tools(store, cwd=shop)
    assert "已添加" in t.analyze_project(path=str(shop))
    return t


def pid(store: Store) -> str:
    [p] = store.list_projects()
    return p["id"]


def graph_of(store: Store):
    return from_json(store.graph_json(store.latest_analysis(pid(store))["id"]))


def line_of(graph, sid: str):
    return next(ln for ln in graph.lines if ln.sid == sid)


def touch(shop: Path) -> None:
    """改一个文件（只加注释）：代码变了，流程没变。"""
    p = shop / "app" / "orders.py"
    p.write_text(p.read_text(encoding="utf-8") + "# 改了一下\n", encoding="utf-8")


def add_vip_branch(shop: Path) -> None:
    p = shop / "app" / "orders.py"
    text = p.read_text(encoding="utf-8")
    text = text.replace('@workflow(title="下单"', '@condition(title="是会员？")\ndef is_vip(order: dict) -> bool:\n'
                        '    return order.get("vip", False)\n\n\n@workflow(title="下单"')
    text = text.replace("    order_no = create_order(order)",
                        '    if is_vip(order):\n        return outcome("skipped", title="会员走专属通道")\n'
                        "    order_no = create_order(order)")
    p.write_text(text, encoding="utf-8")


# ---------- 工具表 ----------

def test_tool_specs_name_the_fifteen_tools_with_chinese_descriptions():
    names = [s["name"] for s in TOOL_SPECS]
    assert names == ["list_projects", "analyze_project", "project_status", "list_flows", "get_flow", "find_nodes",
                     "paths_to_outcome", "get_node", "diff_flows", "check", "uncovered_outcomes",
                     "list_requirements", "get_requirement", "save_requirement", "run_line_tests"]
    for s in TOOL_SPECS:
        assert any("\u4e00" <= ch <= "\u9fff" for ch in s["description"]), s["name"]
        schema = s["inputSchema"]
        assert schema["type"] == "object" and isinstance(schema["properties"], dict)
        for prop in schema["properties"].values():
            assert any("\u4e00" <= ch <= "\u9fff" for ch in prop["description"])
    get_flow = next(s for s in TOOL_SPECS if s["name"] == "get_flow")
    assert "required" not in get_flow["inputSchema"]  # flow 和 function 至少给一个：只给 function 时看任何一个函数


def test_call_checks_names_and_arguments(tools):
    assert "下单" in tools.call("list_flows", {})
    with pytest.raises(ToolError, match="没有这个工具"):
        tools.call("run_tests", {})
    with pytest.raises(ToolError, match="不认识的参数：bogus"):
        tools.call("get_flow", {"flow": "下单", "bogus": 1})
    with pytest.raises(ToolError, match="要给 flow（功能线的 sid 或标题），或者只给 function"):
        tools.call("get_flow", {})
    with pytest.raises(ToolError, match="line 应该是整数"):
        tools.call("find_nodes", {"file": "app/orders.py", "line": "第三行"})
    with pytest.raises(ToolError, match="files 应该是字符串列表"):
        tools.call("check", {"files": "app/orders.py"})
    assert tools.call("find_nodes", {"file": "app/orders.py", "line": "23"}) == \
        tools.find_nodes(file="app/orders.py", line=23)


# ---------- list_projects ----------

def test_list_projects_shows_lines_and_staleness(tools, shop, store):
    out = tools.list_projects()
    assert "shop" in out and pid(store) in out and str(shop) in out
    assert "功能线 2 条" in out and "未过期" in out and "已过期" not in out
    assert "当前目录" in out
    touch(shop)
    out = tools.list_projects()
    assert "已过期（改 1、增 0、删 0 个文件）" in out
    assert not out.startswith("⚠")


def test_list_projects_when_empty(store, tmp_path):
    out = Tools(store, cwd=tmp_path).list_projects()
    assert "还没有添加项目" in out and "analyze_project" in out


# ---------- 怎么指项目 ----------

def test_project_from_cwd_id_name_or_directory(tools, store, shop, tmp_path):
    inner = Tools(store, cwd=shop / "app")
    assert "下单后结算" in inner.list_flows()
    for ref in (pid(store), "shop", str(shop), str(shop / "app"), str(shop / "app" / "orders.py")):
        assert "下单后结算" in Tools(store, cwd=tmp_path).list_flows(project=ref)
    outside = Tools(store, cwd=tmp_path)
    with pytest.raises(ToolError) as e:
        outside.list_flows()
    assert "shop" in str(e.value) and pid(store) in str(e.value) and "analyze_project" in str(e.value)
    with pytest.raises(ToolError, match="找不到项目「nope」"):
        outside.list_flows(project="nope")
    # 写错的名字不能按相对路径落进当前目录所在的项目（当前目录在项目里时也一样）；真有的子目录照旧认
    with pytest.raises(ToolError, match="找不到项目「shopp」"):
        Tools(store, cwd=shop).list_flows(project="shopp")
    assert "下单后结算" in Tools(store, cwd=shop).list_flows(project="app")


# ---------- list_flows ----------

def test_list_flows_of_a_project_without_lines(store, tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "util.py").write_text("X = 1\n", encoding="utf-8")  # 一个函数都没有：兜底也找不到入口
    t = Tools(store, cwd=root)
    added = t.analyze_project(path=str(root))
    assert "没有功能线：项目里没有函数" in added
    out = t.list_flows()
    assert "这个项目没有功能线" in out and "项目里没有函数" in out and "（）" not in out
    filtered = t.list_flows(query="下单")
    assert "这个项目没有功能线" in filtered and "（）" not in filtered


def test_a_project_without_recognised_entries_still_gets_lines(store, tmp_path):
    """路由、任务、工作流一个都没有：没有人调用的顶层函数当入口，分析结果里说明入口是推断的；不在功能线上的函数
    用 get_flow 只给 function 也能看。"""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "util.py").write_text(
        "import requests\n\n\ndef helper(x):\n    return requests.get(x)\n\n\n"
        "def sync_all(urls):\n    for u in urls:\n        helper(u)\n", encoding="utf-8")
    t = Tools(store, cwd=root)
    added = t.analyze_project(path=str(root))
    assert "功能线 1 条" in added and "入口是推断的" in added and "# pystepmap: 入口" in added
    out = t.list_flows()
    assert "## 其他入口（1 条）" in out and "sync_all（util.py）" in out
    only = t.get_flow(function="helper")
    assert "requests.get" in only and "helper" in only
    with pytest.raises(ToolError, match="function 只能和 outline 一起用"):
        t.get_flow(function="helper", format="mermaid")


def test_list_flows_with_a_filter_that_matches_nothing(tools):
    out = tools.list_flows(query="不存在的东西")
    assert "没有匹配的功能线（query=不存在的东西）" in out and "分组有：" in out


def test_list_flows_groups_query_and_overrides(tools, store):
    out = tools.list_flows()
    assert "## 订单" in out and "## 后台任务" in out
    assert "下单  [line:app/orders.py::place_order]  工作流  app/orders.py:22" in out
    assert "下单后结算  [line:app/tasks.py::settle]  任务  app/tasks.py:11" in out
    assert SETTLE in tools.list_flows(query="结算") and PLACE not in tools.list_flows(query="结算")
    assert PLACE in tools.list_flows(query="orders.py") and SETTLE not in tools.list_flows(query="orders.py")
    assert PLACE in tools.list_flows(query="place_order") and SETTLE not in tools.list_flows(query="place_order")
    assert SETTLE in tools.list_flows(group="后台任务") and PLACE not in tools.list_flows(group="后台任务")
    assert "没有匹配的功能线" in tools.list_flows(query="退款")
    store.set_override(pid(store), PLACE, "提交订单", "交易")
    out = tools.list_flows()
    assert "## 交易" in out and "提交订单  [line:app/orders.py::place_order]" in out and "## 订单" not in out
    assert PLACE in tools.list_flows(query="提交")


# ---------- get_flow ----------

def test_get_flow_is_the_line_outline(tools, store):
    graph = graph_of(store)
    line = line_of(graph, PLACE)
    assert tools.get_flow(flow=PLACE) == line_outline(graph, line)
    assert tools.get_flow(flow="下单") == line_outline(graph, line)  # 完全相同的标题优先于片段
    assert tools.get_flow(flow="结算") == line_outline(graph, line_of(graph, SETTLE))  # 唯一的片段
    assert tools.get_flow(flow=PLACE, depth=1) == line_outline(graph, line, depth=1)
    assert tools.get_flow(flow=PLACE, depth=9) == line_outline(graph, line, depth=3)  # 限制在 0～3
    mermaid = tools.get_flow(flow=PLACE, format="mermaid")
    assert mermaid.startswith("flowchart TD") and mermaid == line_mermaid(graph, line)
    with pytest.raises(ToolError) as e:
        tools.get_flow(flow="单")
    assert PLACE in str(e.value) and SETTLE in str(e.value)
    with pytest.raises(ToolError, match="没有这条功能线"):
        tools.get_flow(flow="退款")
    with pytest.raises(ToolError, match="format"):
        tools.get_flow(flow=PLACE, format="svg")


def test_flow_by_root_function_name_and_sid_candidates(tools):
    by_sid = tools.get_flow(flow=PLACE)
    for flow in ("place_order", "app/orders.py::place_order", "orders.py::place_order"):
        assert tools.get_flow(flow=flow) == by_sid
    with pytest.raises(ToolError) as e:
        tools.get_flow(flow="order")  # 不是标题、不是函数名：列出 sid 里含它的
    assert "没有这条功能线" in str(e.value) and "sid 里含「order」的功能线" in str(e.value) and PLACE in str(e.value)


def test_get_flow_uses_the_overridden_title(tools, store):
    store.set_override(pid(store), PLACE, "提交订单", "交易")
    out = tools.get_flow(flow="提交订单")
    assert out.splitlines()[0] == "# 提交订单  [line:app/orders.py::place_order]  分组：交易"
    graph = graph_of(store)
    assert out.splitlines()[1:] == line_outline(graph, line_of(graph, PLACE)).splitlines()[1:]
    assert tools.get_flow(flow="下单") == out  # 原来的标题也算数


def test_reads_the_latest_analysis_made_elsewhere(tools, store, shop):
    assert "is_vip" not in tools.get_flow(flow=PLACE)
    add_vip_branch(shop)
    assert reanalyze_project(store, pid(store))["kind"] == "ok"  # 平台界面那边重新分析的
    assert "判断 is_vip(order)（是会员？）" in tools.get_flow(flow=PLACE)


# ---------- find_nodes ----------

def test_find_nodes_by_file_and_line(tools, store, shop):
    n = line_no(shop / "app" / "orders.py", "stock = load_stock(order)")
    graph = graph_of(store)
    [hit] = find_by_location(graph, "app/orders.py", n)
    sid = next(x.sid for x in graph.functions[hit.function].nodes if x.id == hit.node)
    out = tools.find_nodes(file="app/orders.py", line=n)
    assert f"[{sid}] L{n} 查询库存（load_stock）" in out
    assert "这一行在这个节点上" in out
    assert "所在函数：工作流 place_order「下单」  app/orders.py:22  [app/orders.py::place_order]" in out
    assert "入口链：就在功能线的根函数里" in out
    assert f"下单  [{PLACE}]" in out
    assert tools.find_nodes(file=str(shop / "app" / "orders.py"), line=n) == out  # 绝对路径也行
    assert tools.find_nodes(file="./app/orders.py", line=n) == out


def test_find_nodes_nearest_node_and_misses(tools, shop):
    n = line_no(shop / "app" / "lib.py", '"""生成订单号"""')
    out = tools.find_nodes(file="app/lib.py", line=n)
    assert "这一行不出节点，前面最近的节点是它" in out and "make_order_no" in out
    assert f"下单  [{PLACE}]" in out  # 创建订单步骤调用它
    assert "不在图里" in tools.find_nodes(file="app/nope.py", line=1)
    assert "不在任何函数里" in tools.find_nodes(file="app/orders.py", line=1)


def test_find_nodes_shows_the_function_title_and_how_each_line_reaches_it(tools, shop):
    out = tools.find_nodes(file="app/tasks.py", line=line_no(shop / "app" / "tasks.py", "notify(order_no)"))
    assert "所在函数：任务 send_receipt「发送回执」  app/tasks.py:5  [app/tasks.py::send_receipt]" in out
    assert "入口链：任务 settle 在 [step:63670d7d] L16 派发 → 任务 send_receipt" in out
    out = tools.find_nodes(function="make_order_no")
    assert "所在函数：函数 make_order_no「生成订单号」" in out
    assert "入口链：工作流 place_order 在 [step:e27a1f7e] L26 调用 → 步骤 create_order 在" in out


def test_find_nodes_bounds_lines_and_chains_and_keeps_the_closing_line(tmp_path, store):
    """被很多功能线用到的函数（get_settings 那种）：每处命中最多列 20 条功能线、前 3 条带入口链；命中多到超过 4 万字时
    在命中之间截断，最后那行"…还有 N 处命中没有列出"留着。"""
    jobs = "".join(f'@broker.task()\nasync def job{i}(x):\n    """{"很长的任务说明" * 140}{i}"""\n    helper(x)\n\n\n'
                   for i in range(40))
    root = write(tmp_path / "shared", {"app/__init__.py": "", "app/h.py": "def helper(x):\n    return x\n",
                                       "app/jobs.py": f"from app.h import helper\n\n\n{jobs}"})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.find_nodes(function="helper")
    assert len(out) <= MAX_CHARS
    blocks = out.split("\n\n")
    definition = next(b for b in blocks if "怎么命中：定义" in b)
    assert definition.count("入口链：") == 3 and definition.count("\n     - ") == 20
    assert "…还有 20 条功能线（用 list_flows / get_flow 看）" in definition
    last = out.splitlines()[-1]
    shown = out.count("怎么命中：")
    assert shown < 30 and last == f"…还有 {41 - shown} 处命中没有列出（给具体的 file + line，或者用函数 sid 代替函数名）"


def test_find_nodes_says_which_definition_a_call_site_calls(tmp_path, store):
    root = write(tmp_path / "twins", {"app/__init__.py": "", "app/a.py": "def helper():\n    return 1\n",
                                      "app/b.py": "def helper():\n    return 2\n",
                                      "app/c.py": "from app import a, b\n\n\ndef use():\n    a.helper()\n    b.helper()\n"})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.find_nodes(function="helper")
    assert "怎么命中：调用处（调用 [app/a.py::helper]）" in out and "怎么命中：调用处（调用 [app/b.py::helper]）" in out


def test_find_nodes_by_function(tools):
    out = tools.find_nodes(function="make_order_no")
    assert "定义" in out and "调用处" in out
    assert "步骤 create_order" in out  # 调用处所在的函数
    assert f"下单  [{PLACE}]" in out
    assert "没有叫「nope」的函数" in tools.find_nodes(function="nope")
    with pytest.raises(ToolError, match="file 和 line"):
        tools.find_nodes()
    with pytest.raises(ToolError, match="file 和 line"):
        tools.find_nodes(file="app/orders.py")
    with pytest.raises(ToolError, match="只给一种"):
        tools.find_nodes(file="app/orders.py", line=3, function="place_order")
    with pytest.raises(ToolError, match="行号从 1 起"):
        tools.find_nodes(file="app/orders.py", line=0)


def test_find_nodes_lists_at_most_30_hits(tmp_path, store):
    body = "".join(f"    helper({i})\n" for i in range(40))
    root = write(tmp_path / "many", {"app/__init__.py": "", "app/m.py": f"def helper(x):\n    pass\n\n\ndef many():\n{body}"})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.find_nodes(function="helper")
    assert out.count("调用处") == 29 and "定义" in out
    assert "还有 11 处命中没有列出" in out


# ---------- paths_to_outcome ----------

def test_paths_to_failed_outcomes(tools, store):
    out = tools.paths_to_outcome(flow="下单", outcome="failed")
    assert "结局·失败：库存不够，没有下单" in out
    assert "判断 lacks_stock(stock)（库存不够？）：是 → 结局·失败：库存不够，没有下单" in out
    assert "就在功能线的根函数里" in out
    graph = graph_of(store)
    ok = next(n for n in graph.functions["app/orders.py::place_order@22"].nodes if n.title == "下单完成")
    for outcome in (ok.sid, "下单完成", "ok"):
        out = tools.paths_to_outcome(flow=PLACE, outcome=outcome)
        assert "判断 lacks_stock(stock)（库存不够？）：否 → 结局·正常：下单完成" in out
    with pytest.raises(ToolError, match="没有匹配「退款」的结局") as e:
        tools.paths_to_outcome(flow=PLACE, outcome="退款")
    assert "结局·失败：库存不够，没有下单" in str(e.value)  # 列出可选的结局
    with pytest.raises(ToolError, match="没有匹配「exception」的结局") as e:  # 未捕获异常退出没有入边，不算
        tools.paths_to_outcome(flow=PLACE, outcome="exception")
    assert "结局·正常：返回 make_order_no(order)" in str(e.value)  # 列出这条线上（含被调函数里）的结局
    spec = next(s for s in TOOL_SPECS if s["name"] == "paths_to_outcome")
    assert "未捕获异常退出" in spec["inputSchema"]["properties"]["outcome"]["description"]


def test_kind_words_match_the_outcome_kind_not_the_title(tmp_path, store):
    """ok / skipped / failed / exception 按结局类型找：标题里碰巧有 failed、token（含 ok）的不算。"""
    root = write(tmp_path / "kinds", {"app/__init__.py": "", "app/t.py": '''\
        @broker.task()
        async def report(x):
            """汇总"""
            if x:
                return {"failed": 0}
            raise ValueError("token 无效")
        '''})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    failed = t.paths_to_outcome(flow="汇总", outcome="failed")  # 任务里的 raise 是失败结局
    assert "结局·失败：抛出 ValueError(\"token 无效\")" in failed and "结局·正常" not in failed
    ok = t.paths_to_outcome(flow="汇总", outcome="ok")
    assert "结局·正常：返回 {\"failed\": 0}" in ok and "结局·失败" not in ok
    with pytest.raises(ToolError, match="没有匹配「exception」的结局"):
        t.paths_to_outcome(flow="汇总", outcome="exception")
    assert "结局·失败" in t.paths_to_outcome(flow="汇总", outcome="token")  # 别的文字仍按标题片段找


RETRY = '''\
from pystepkit import outcome, step, workflow


@step(title="读取配置")
def load(job: dict) -> dict:
    return dict(job)


@workflow(title="重试一次", group="演示")
def retry_once(job: dict):
    cfg = load(job)
    attempts = cfg["attempts"]
    if attempts >= 3:
        return outcome("failed", title="超过重试次数")
    attempts += 1
    if attempts >= 3:
        return outcome("skipped", title="这是最后一次重试")
    return outcome("ok", title="还能再试")
'''
CAVEAT = "下面列的是图上能走到各个结局的路径：只按连线，不判断前后条件是否互相矛盾，可能含实际走不到的组合；转述前对照源码核对。"


def test_paths_are_never_dropped_and_carry_the_caveat(tmp_path, store):
    """两个判断之间的赋值、+= 不出节点：同样的条件先"否"后"是"是真能走到的，一条都不能略去。"""
    root = write(tmp_path / "retry", {"app/__init__.py": "", "app/r.py": RETRY})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.paths_to_outcome(flow="重试一次", outcome="这是最后一次重试")
    assert out.count(CAVEAT) == 1 and "略去" not in out
    assert t.paths_to_outcome(flow="重试一次", outcome="ok").count("转述前对照源码核对") == 1  # 结局再多也只说一次
    assert "1. 判断 attempts >= 3：否 → 判断 attempts >= 3：是 → 结局·跳过：这是最后一次重试" in out
    spec = next(s for s in TOOL_SPECS if s["name"] == "paths_to_outcome")
    assert "可能含实际走不到的组合" in spec["description"] and "略去" not in spec["description"]


UNCAUGHT = "这是隐含的结局：函数里任何没被捕获的异常都从这里离开，图上不画进它的连线，所以没有路径可列。"


def test_paths_to_the_implicit_uncaught_exit(tools):
    """大纲不写隐含的 [end:uncaught]，但这个 sid 照样认：拿它来问时不说"走不到"，说清楚它是隐含的结局。"""
    out = tools.paths_to_outcome(flow=PLACE, outcome="end:uncaught")
    assert UNCAUGHT in out and "没有走到它的路径" not in out


def test_paths_to_an_outcome_in_a_dispatched_task(tools):
    out = tools.paths_to_outcome(flow=SETTLE, outcome="ok")
    assert "结局·正常：返回 \"unpaid\"" in out
    assert "判断 paid is None：是 → 结局·正常：返回 \"unpaid\"" in out
    # 派发的任务 send_receipt 里的"执行到函数末尾"：从功能线入口到它的链
    assert "settle 在 [step:" in out and "派发 → 任务 send_receipt" in out


# ---------- get_node ----------

def test_get_node_shows_source_calls_edges_and_lines(tools, store, shop):
    n = line_no(shop / "app" / "orders.py", "stock = load_stock(order)")
    graph = graph_of(store)
    [hit] = find_by_location(graph, "app/orders.py", n)
    sid = next(x.sid for x in graph.functions[hit.function].nodes if x.id == hit.node)
    out = tools.get_node(function="app/orders.py::place_order", node=sid)
    assert "标题：查询库存" in out and "类型：步骤" in out and f"位置：app/orders.py:{n}" in out
    assert f"→ {n} |     stock = load_stock(order)" in out
    assert f"  {n - 3} | " in out and f"  {n + 3} | " in out  # 前后各 3 行
    assert "load_stock → 步骤 load_stock  app/orders.py:7  [app/orders.py::load_stock]（精确）" in out
    assert "开始" in out and "判断 lacks_stock(stock)（库存不够？）" in out  # 进出的连线
    assert f"下单  [{PLACE}]" in out
    with pytest.raises(ToolError, match="没有这个函数"):
        tools.get_node(function="app/orders.py::nope", node=sid)
    with pytest.raises(ToolError, match="没有这个节点"):
        tools.get_node(function="app/orders.py::place_order", node="step:00000000")


# ---------- analyze_project、diff_flows ----------

def test_analyze_add_then_reanalyze_with_a_new_branch(store, shop):
    t = Tools(store, cwd=shop)
    out = t.analyze_project(path=str(shop), name="商店")
    assert "已添加项目「商店」" in out and "功能线 2 条" in out and "第一次分析" in out
    assert "相对上一次：流程没有变化" in t.analyze_project()  # 不给参数：当前目录所在的项目，重新分析
    inner = t.analyze_project(path=str(shop / "app"))  # 项目里面的目录：重新分析整个项目，并说明
    assert "已重新分析项目「商店」" in inner and "在项目「商店」里面，重新分析的是整个项目" in inner
    add_vip_branch(shop)
    out = t.analyze_project(project="商店")
    assert "已重新分析项目「商店」" in out and "新增" in out and "is_vip(order)" in out
    state = store.latest_diff(pid(store))
    expected = diff_text(diff_from_json(state["diff"]))
    # analyze_project 里新增的函数只写一行（diff_flows 照旧逐个节点列）
    assert diff_text(diff_from_json(state["diff"]), brief="added") in out and expected not in out
    assert "另有新增的函数 1 个（步骤、判断函数等）：是会员？" in out and "### 函数 [新增] 是会员？" not in out
    assert "新增的函数只写了一行：要看节点用 get_flow" in out
    shown = t.diff_flows()
    assert expected in shown and not shown.startswith("⚠")
    assert diff_text(diff_from_json(state["diff"]), line_sid=PLACE) in t.diff_flows(flow="下单")
    assert acknowledge_changes(store, pid(store))["kind"] == "ok"
    assert "没有未看的流程变化" in t.diff_flows() and "重新分析过、流程没有变化" in t.diff_flows()
    # 标为已看之后：那次变化留成一条变更记录，diff_flows 末尾列出来，用 record 看明细
    [rec] = store.list_reviews(pid(store))
    after = t.diff_flows()
    assert "变更记录（以前在平台上标为已看的变化，最近 1 条；用 record 看明细）" in after
    assert f"- #{rec['id']}" in after and "1 条功能线（下单）" in after
    one = t.call("diff_flows", {"record": rec["id"]})
    assert f"变更记录 #{rec['id']}" in one and "这是当时的变化" in one and expected in one
    assert diff_text(diff_from_json(state["diff"]), line_sid=PLACE) in t.diff_flows(record=rec["id"], flow="下单")
    with pytest.raises(ToolError, match=f"没有变更记录 #999（只留最近 10 条）。现有的：#{rec['id']}"):
        t.diff_flows(record=999)
    with pytest.raises(ToolError, match="这条记录里没有功能线「不存在」"):
        t.diff_flows(record=rec["id"], flow="不存在")


def remove_guard(shop: Path) -> None:
    """删掉下单流程里"库存不够？"这个守卫。"""
    path = shop / "app" / "orders.py"
    text = path.read_text(encoding="utf-8")
    guard = '    if lacks_stock(stock):\n        return outcome("failed", title="库存不够，没有下单")\n'
    assert guard in text
    path.write_text(text.replace(guard, ""), encoding="utf-8")


def test_unseen_changes_add_up_and_analyze_says_what_changed_since_the_last_analysis(tools, store, shop):
    """R1b：两次改流程都没标为已看。analyze_project 说的是相对上一次分析的变化（只有这一次的），另外说还有更早的没看；
    diff_flows 给全部（相对第一次分析）；标为已看以后，下一次变化单独算（相对上次标为已看时）。"""
    add_vip_branch(shop)
    first = tools.analyze_project()
    assert "相对上一次分析的流程变化：" in first and "is_vip(order)" in first and "更早的未看变化" not in first
    remove_guard(shop)
    second = tools.analyze_project()
    assert "相对上一次分析的流程变化：" in second and "lacks_stock(stock)" in second and "is_vip(order)" not in second
    assert "另有更早的未看变化，用 diff_flows 看全部" in second
    created = store.get_project(pid(store))["created_at"]
    whole = tools.diff_flows()
    assert whole.startswith("最新一次分析（#") and f"相对第一次分析（{mcp_tools._when(created)}）的流程变化：" in whole
    assert "is_vip(order)" in whole and "lacks_stock(stock)" in whole
    touch(shop)  # 只改格式：相对上一次没变，之前的变化还在
    third = tools.analyze_project()
    assert "相对上一次：流程没有变化（改的是格式、注释或流程以外的代码）" in third and "之前还有没看的流程变化" in third
    head = store.latest_analysis(pid(store))
    assert acknowledge_changes(store, pid(store), head["id"])["kind"] == "ok"
    orders = shop / "app" / "orders.py"  # 把守卫加回去
    orders.write_text(orders.read_text(encoding="utf-8").replace(
        '    order_no = create_order(order)\n',
        '    if lacks_stock(stock):\n        return outcome("failed", title="库存不够，没有下单")\n'
        '    order_no = create_order(order)\n'), encoding="utf-8")
    fourth = tools.analyze_project()
    assert "lacks_stock(stock)" in fourth and "更早的未看变化" not in fourth
    later = tools.diff_flows()
    assert f"相对上次标为已看时（{mcp_tools._when(head['created_at'])}）的流程变化：" in later
    assert "is_vip(order)" not in later and "lacks_stock(stock)" in later


def test_change_reverted_before_ack_is_not_called_a_comparison_failure(tools, store, shop):
    """标为已看之后改了流程、分析，又改回去、再分析：存着的差异为空是因为又和对比基准一样了，不是比较出错。"""
    assert acknowledge_changes(store, pid(store), store.latest_analysis(pid(store))["id"])["kind"] == "ok"
    orders = shop / "app" / "orders.py"
    original = orders.read_text(encoding="utf-8")
    add_vip_branch(shop)
    assert "is_vip(order)" in tools.analyze_project()
    orders.write_text(original, encoding="utf-8")
    out = tools.analyze_project()
    assert "流程又和对比基准一样了，没有未看的变化" in out
    assert "没能和上一次比较" not in out and "比较时出错" not in out
    assert "相对上一次分析的流程变化：" in out and "is_vip(order)" in out  # 这一次相对上一次：删掉了那个分支
    assert tools.diff_flows().endswith("的流程变化：\n流程没有变化")


def test_diffs_show_overridden_line_titles(tools, store, shop):
    store.set_override(pid(store), PLACE, "提交订单", "交易")
    add_vip_branch(shop)
    out = tools.analyze_project()
    assert "## 功能线 [改动] 提交订单（原标题：下单）（交易）" in out
    assert "## 功能线 [改动] 提交订单（原标题：下单）（交易）" in tools.diff_flows()
    assert "## 功能线 [改动] 提交订单（原标题：下单）（交易）" in tools.diff_flows(flow="提交订单")


def test_reanalyze_says_when_the_comparison_failed(tools, store, shop, monkeypatch):
    touch(shop)  # 只加了注释：图变了（指纹），流程没变
    assert "相对上一次：流程没有变化（改的是格式、注释或流程以外的代码）" in tools.analyze_project()

    def broken(*args):
        raise RuntimeError("比较坏了")

    monkeypatch.setattr(flows, "_compare", broken)
    add_vip_branch(shop)
    out = tools.analyze_project()
    assert "这次没能和上一次比较" in out and "流程没有变化" not in out
    assert "is_vip(order)" in out  # 当场重新比了一次，变化照样写出来


def test_analyze_without_path_or_project_outside_any_project(store, shop, tmp_path):
    """path、project 都不给，当前目录又不在任何项目里：不把当前目录悄悄添加成项目，说要给哪个参数并列出已有的项目。"""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.py").write_text("x = 1\n", encoding="utf-8")
    t = Tools(store, cwd=elsewhere)
    with pytest.raises(ToolError, match=r"没有指定要分析的目录：给 path（要添加的代码目录）或 project（已添加的项目）"):
        t.analyze_project()
    assert store.list_projects() == []
    t.analyze_project(path=str(shop))
    with pytest.raises(ToolError, match=r"没有指定要分析的目录[\s\S]*shop"):
        t.analyze_project()
    assert [p["root"] for p in store.list_projects()] == [str(shop.resolve())]


def test_analyze_names_files_that_became_unparseable(tools, shop):
    """这次有文件解析不了：点名（最多 10 个），并说报成删除的函数可能只是因为文件解析不了。"""
    (shop / "app" / "lib.py").write_text("def broken(:\n", encoding="utf-8")
    out = tools.analyze_project()
    assert "这次有 1 个文件解析不了（图里没有它们）：app/lib.py" in out
    assert "报成删除的函数可能只是因为所在的文件解析不了" in out
    assert "解析不了" not in tools.analyze_project().split("分析摘要")[0]  # 再分析一次：不是"这次新出现"的，不再说


def test_analyze_reports_flow_failures(store, shop, tmp_path):
    t = Tools(store, cwd=tmp_path)
    with pytest.raises(ToolError, match="目录不存在"):
        t.analyze_project(path=str(tmp_path / "nope"))
    t.analyze_project(path=str(shop))
    with pytest.raises(ToolError, match="包含已添加的项目"):
        t.analyze_project(path=str(shop.parent))
    with pytest.raises(ToolError, match="只给一个"):
        t.analyze_project(path=str(shop), project="shop")


def test_analyze_while_this_server_is_still_analysing(tools, store, monkeypatch):
    """客户端取消了上一次 analyze_project（notifications/cancelled），那次分析还在后台跑：再调时说是自己的那次没结束。"""
    release, entered = threading.Event(), threading.Event()

    def slow(root, config, *, timeout, cwd):
        entered.set()
        release.wait(30)
        return worker.analyze_directory(root, config)

    monkeypatch.setattr(flows, "run_in_subprocess", slow)
    first = threading.Thread(target=tools.analyze_project)
    first.start()
    try:
        assert entered.wait(10)
        with pytest.raises(ToolError, match=r"这个项目正在分析（上一次 analyze_project 调用还没结束），稍后再试"):
            tools.analyze_project()
    finally:
        release.set()
        first.join(30)
    assert "相对上一次" in tools.analyze_project()


BUSY_ELSEWHERE = "这个项目正在分析或正在跑测试（平台界面上点的），稍后再试"


def test_analyze_while_another_process_analyzes(tools, store):
    """项目锁被别的进程拿着（平台上的分析或测试运行，MCP 分不出是哪个）：被拒绝的工具这样说；列表里也这样标。"""
    fd = try_lock_project(store.path.parent, pid(store))
    try:
        with pytest.raises(ToolError) as e:
            tools.analyze_project()
        assert str(e.value) == BUSY_ELSEWHERE
        assert "正在分析或正在跑测试（平台界面上点的）" in tools.list_projects()
    finally:
        unlock_project(fd)
    assert "正在跑测试" not in tools.list_projects()


def test_diff_flows_with_changes_outside_any_line(tools, store, shop):
    p = shop / "app" / "lib.py"
    p.write_text(p.read_text(encoding="utf-8").replace("    if x:\n", "    if x > 1:\n"), encoding="utf-8")
    tools.analyze_project()
    out = tools.diff_flows()
    assert "没有变化" not in out and "其他有变化的函数" in out and "app/lib.py::unused" in out


# ---------- 过期提示、project_status ----------

def test_graph_tools_start_with_the_stale_warning(tools, store, shop):
    for out in (tools.list_flows(), tools.get_flow(flow=PLACE), tools.project_status(), tools.diff_flows()):
        assert not out.startswith("⚠")
    touch(shop)
    graph = graph_of(store)
    sid = next(n.sid for n in graph.functions["app/orders.py::place_order@22"].nodes if n.type == "cond")
    outs = [tools.list_flows(), tools.get_flow(flow=PLACE), tools.find_nodes(file="app/orders.py", line=23),
            tools.paths_to_outcome(flow=PLACE, outcome="failed"), tools.get_node(function="app/orders.py::place_order",
                                                                                 node=sid),
            tools.diff_flows(), tools.project_status()]
    for out in outs:
        assert out.startswith("⚠ 代码已变更（改 1、增 0、删 0 个文件），图可能过期。涉及功能线：下单 [line:app/orders.py::place_order]。"
                              "先调用 analyze_project 重新分析。"), out[:200]
    assert not tools.list_projects().startswith("⚠") and not tools.check().startswith("⚠")
    assert "这个文件在分析之后改过" in tools.find_nodes(file="app/orders.py", line=23)


def test_project_status_selfcheck_and_stale_details(tools, store, shop):
    out = tools.project_status()
    assert f"MCP 服务：{procinfo.STARTED.astimezone():%Y-%m-%d %H:%M} 启动，代码 {procinfo.LOADED}" in out
    assert "走不到的节点：0 个" in out and "没有出路的节点：0 个" in out
    assert "目标不明的调用：" in out and "推测的调用：0 处" in out and "跳过的文件：0 个" in out
    assert "代码没有变更" in out
    touch(shop)
    (shop / "app" / "new.py").write_text("x = 1\n", encoding="utf-8")
    out = tools.project_status()
    assert "改：app/orders.py" in out and "增：app/new.py" in out and f"下单 [{PLACE}]" in out


def test_every_tool_says_so_when_the_process_runs_old_code(tools, monkeypatch):
    """磁盘上的代码在进程启动之后更新过（分析在子进程里跑，引擎标识是新的，看不出进程是旧的）：经 call 调用的每个工具
    结果最前面提醒重启；代码没变时不提。"""
    assert not tools.call("list_projects", {}).startswith("⚠")
    assert procinfo.LOADED == procinfo.code_fingerprint() and len(procinfo.LOADED) == 8
    monkeypatch.setattr(procinfo, "LOADED", "0000aaaa")
    for name in ("list_projects", "project_status", "analyze_project", "list_flows"):
        out = tools.call(name, {})
        assert out.startswith(f"⚠ pystepmap 的代码在 MCP 服务 启动之后更新过（启动时 0000aaaa，现在 {procinfo.code_fingerprint()}）"), name
        assert "重启 MCP 服务" in out.split("\n")[0]


def test_code_fingerprint_follows_every_py_file_of_the_packages(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sub").mkdir(parents=True)
    b.mkdir()
    (a / "x.py").write_text("x = 1\n", encoding="utf-8")
    (a / "sub" / "y.py").write_text("y = 1\n", encoding="utf-8")
    assert procinfo.code_fingerprint((tmp_path / "none",)) == ""
    first = procinfo.code_fingerprint((a, b))
    (a / "note.txt").write_text("不算", encoding="utf-8")
    assert procinfo.code_fingerprint((a, b)) == first
    (a / "sub" / "y.py").write_text("y = 2\n", encoding="utf-8")
    second = procinfo.code_fingerprint((a, b))
    (b / "z.py").write_text("", encoding="utf-8")
    assert len({first, second, procinfo.code_fingerprint((a, b))}) == 3


def test_project_status_lists_unresolved_calls_on_lines_first(tmp_path, store):
    root = write(tmp_path / "mixed", {
        "app/__init__.py": "",
        # 同名定义有两处：引擎不推测目标，但这两个名字在项目里有定义，调用可能就落在它们上面
        "app/model.py": "class A:\n    def create_table(self): ...\nclass B:\n    def create_table(self): ...\n"
                        "class C:\n    def render(self): ...\nclass D:\n    def render(self): ...\n",
        "app/aaa.py": "def migrate(op):\n    op.create_table()\n",
        "app/zzz.py": "@broker.task()\nasync def job(client):\n    client.render()\n"})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.project_status()
    assert out.index("app/zzz.py:3 client.render（在功能线上）") < out.index("app/aaa.py:2 op.create_table")


def test_analyze_summary_separates_blind_spots_from_third_party_calls(tmp_path, store):
    """analyze_project 的摘要里，目标不明的调用后面跟着可能落在项目代码上的那部分。"""
    root = write(tmp_path / "sum", {"app/__init__.py": "",
                                    "app/a.py": "def f(session, c):\n    session.commit()\n    c.render()\n"
                                                "class C:\n    def render(self): ...\nclass D:\n    def render(self): ...\n"})
    t = Tools(store, cwd=root)
    assert "目标不明的调用 2 处（可能落在项目代码上的 1 处）" in t.analyze_project(path=str(root))


def test_analyze_summary_of_an_older_analysis_only_has_the_total():
    """旧的分析没有记"可能落在项目代码上的"那个数：只写总数，不写括号。"""
    text = _summary_text({"functions": 3, "unresolved_calls": 7, "guessed_calls": 0})
    assert "目标不明的调用 7 处；" in text


def test_project_status_folds_calls_that_cannot_be_project_code(tmp_path, store):
    """第三方 / 标准库的调用和日志不逐条列出（它们不可能落在项目代码上）：各折一行，只列可能是项目内的。"""
    root = write(tmp_path / "noisy", {
        "app/__init__.py": "",
        "app/model.py": "class C:\n    def render(self): ...\nclass D:\n    def render(self): ...\n"
                        "class Cache:\n    def get(self): ...\n",
        "app/zzz.py": "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n@broker.task()\n"
                      "async def job(session, client, d):\n    logger.info('x')\n    session.commit()\n"
                      "    d.get('k')\n    client.render()\n"})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.project_status()
    assert "目标不明的调用：4 处" in out
    assert "app/zzz.py:11 client.render（在功能线上）" in out  # 可能落在项目代码上：列出来
    # 第三方、太常见的名字、日志：只计数，不占屏
    assert "session.commit" not in out and "logger.info" not in out and "d.get" not in out
    assert "第三方 / 标准库 1 处" in out and "名字太常见 1 处" in out and "日志 1 处" in out


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="要能让文件读不了")
def test_project_status_mentions_unreadable_files(tools, shop):
    p = shop / "app" / "tasks.py"
    p.chmod(0)
    try:
        out = tools.project_status()
        assert "读不了" in out and "app/tasks.py" in out
        assert "读不了" not in tools.list_flows()
    finally:
        p.chmod(0o644)


# ---------- check ----------

def test_check_examples_and_violations(store, tmp_path, tools, shop):
    t = Tools(store, cwd=tmp_path)
    assert "共 0 处违规" in t.check(path=str(TRANSFER))
    bad = write(tmp_path / "bad", {"app/flow.py": """\
        import time
        from pystepkit import outcome, workflow


        @workflow(title="坏流程")
        def bad():
            started = time.time()
            return outcome("ok", title="完成", started=started)
        """})
    out = t.check(path=str(bad))
    assert "SM001" in out and "app/flow.py:7" in out and "共 1 处违规" in out
    assert "共 0 处违规" in t.check(path=str(bad), files=["app/other.py"])
    assert "不对应项目里的任何 .py 文件" in t.check(path=str(bad), files=["app/other.py"])
    assert "共 0 处违规" in tools.check()  # 当前目录所在的项目
    assert "共 0 处违规" in t.check(project="shop")
    with pytest.raises(ToolError, match="不是目录"):
        t.check(path=str(bad / "app" / "flow.py"))
    assert "用的配置：默认配置" in out


BAD_FLOW = """\
    import time
    from pystepkit import outcome, workflow


    @workflow(title="坏流程")
    def bad():
        started = time.time()
        return outcome("ok", title="完成", started=started)
    """


@pytest.mark.parametrize("path", ["/", "~", str(Path.home().parent)])
def test_check_refuses_root_and_home(store, tmp_path, path):
    with pytest.raises(ToolError, match="不能检查根目录、用户主目录或包含用户主目录的目录"):
        Tools(store, cwd=tmp_path).check(path=path)


def test_check_inside_a_project_uses_the_projects_config(tools, store, shop):
    """项目里面的目录：用项目的分析配置检查（这里配置排除了 legacy 目录），只报告这个目录里的，路径相对项目根目录。"""
    write(shop, {"app/legacy/old.py": BAD_FLOW, "app/new.py": BAD_FLOW, "other/bad.py": BAD_FLOW})
    store.set_config(pid(store), {"extra_exclude": ["legacy"]})
    out = tools.check(path=str(shop / "app"))
    assert "app/new.py:7: SM001" in out and "app/legacy/old.py" not in out and "other/bad.py" not in out
    assert "共 1 处违规" in out and "用的配置：项目「shop」的分析配置" in out
    assert "app/new.py:7" in tools.check(path=str(shop / "app"), files=["new.py"])  # files 相对检查的目录
    missing = tools.check(path=str(shop / "app"), files=["other.py"])
    assert "共 0 处违规" in missing and "files 里的 other.py 不对应" in missing and "app/other.py" not in missing
    whole = tools.check()
    assert "共 2 处违规" in whole and "other/bad.py:7" in whole and "app/legacy" not in whole


def test_check_runs_in_the_worker_with_a_timeout(tools, store, shop, monkeypatch):
    """检查在分析子进程里跑：配置里的正则灾难性回溯时按 PYSTEPMAP_ANALYZE_TIMEOUT 中止，不卡住 MCP 进程。"""
    (shop / "app" / "slow.py").write_text(f"@{'a' * 40}()\ndef handler():\n    return 1\n", encoding="utf-8")
    store.set_config(pid(store), {"extra_rules": [{"name": "slow", "entry_decorators": [["(a+)+$", "route"]]}]})
    monkeypatch.setenv("PYSTEPMAP_ANALYZE_TIMEOUT", "2")
    started = time.monotonic()
    with pytest.raises(ToolError, match="检查超过 2 秒，已中止"):
        tools.check()
    assert time.monotonic() - started < 10


def test_check_never_imports_the_checked_directory(store, tmp_path):
    proj = write(tmp_path / "victim", {"app/flow.py": BAD_FLOW})
    for rel in ("json.py", "pystepmap/__init__.py", "pystepmap_server/__init__.py", "sitecustomize.py"):
        write(proj, {rel: 'raise SystemExit("被导入了")\n'})
    out = Tools(store, cwd=proj).check(path=str(proj))
    assert "app/flow.py:7: SM001" in out and "被导入了" not in out


# ---------- 输出上限 ----------

def test_long_outputs_are_cut_with_a_hint(tmp_path, store):
    steps = "".join(f"    s({i})\n" for i in range(1500))
    root = write(tmp_path / "big", {"app/__init__.py": "", "app/w.py": (
        "from pystepkit import step, workflow\n\n\n@step(title=\"一步\")\ndef s(i):\n    return i\n\n\n"
        f"@workflow(title=\"很长的流程\")\ndef long_flow():\n{steps}")})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.get_flow(flow="很长的流程")
    assert len(out) <= MAX_CHARS
    assert out.splitlines()[-1].startswith("…（输出超过 40000 字") and "depth" in out.splitlines()[-1]


# ---------- 测试运行：uncovered_outcomes、project_status 里的那一段 ----------
# 运行测试只能在平台界面上点：这里直接调用流程（testflows.run_project_tests）在 tmp_path 里转账示例的副本上真的跑 pytest

PYTEST = f"{shlex.quote(sys.executable)} -m pytest tests -q"
NEVER_RAN = "还没有在平台上运行过测试（运行测试只能在平台界面上点）"


def test_only_run_line_tests_can_ask_for_a_run_and_nothing_here_executes_tests():
    """MCP 里只有 run_line_tests 能让测试跑起来，而且是请平台跑（经 HTTP，带 via=mcp）：别的工具的名字、说明、参数都
    不是运行 / 取消测试或填测试命令的；run_line_tests 自己也没有命令、确认、取消、超时这些参数；mcp_tools、mcp_server、
    worker 都不碰执行测试的那条路（runner、run_project_tests）。"""
    import ast
    import inspect

    from pystepmap_server import mcp_server, worker

    for spec in TOOL_SPECS:
        props = set(spec["inputSchema"]["properties"])
        assert not {"command", "confirm", "timeout", "cancel", "test_command"} & props, spec["name"]
        if spec["name"] == "run_line_tests":
            assert props == {"project", "flow", "start", "wait_seconds"}
            assert "不能运行全部测试、不能改或确认测试命令" in spec["description"]
            continue
        assert not re.search(r"run|pytest|exec|cancel|command", spec["name"]), spec["name"]
        assert "运行测试" not in spec["description"] and "取消" not in spec["description"], spec["name"]
    forbidden = {"runner", "run_project_tests", "run_tests", "launch"}
    for module in (mcp_tools, mcp_server, worker):  # 导入的、用到的名字（不看说明文字）
        tree = ast.parse(inspect.getsource(module))
        used = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom) for a in n.names}
        used |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not forbidden & used, (module, forbidden & used)


@pytest.fixture
def example(tmp_path, store) -> tuple[Tools, Path, str]:
    """转账示例的副本，用 analyze_project 添加；当前目录是它。返回 (工具, 目录, 项目 id)。"""
    root = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    t = Tools(store, cwd=root)
    assert "已添加" in t.analyze_project(path=str(root))
    [p] = store.list_projects()
    store.set_test_command(p["id"], PYTEST)
    store.confirm_test_command(p["id"], PYTEST)
    return t, root, p["id"]


def run_example(store, pid: str) -> dict:
    r = testflows.run_project_tests(store, pid, cancel=threading.Event())
    assert r["kind"] == "ok", r
    return store.get_run(pid, r["run_id"])


def test_uncovered_outcomes_before_any_run(tools):
    out = tools.uncovered_outcomes()
    assert NEVER_RAN in out and "下单" not in out


def test_uncovered_outcomes_after_a_run(example, store):
    t, root, pid = example
    run = run_example(store, pid)
    out = t.uncovered_outcomes()
    assert not out.startswith("⚠")
    assert f"#{run['id']}" in out and "9 个用例" in out and "通过 9" in out and "失败 0" in out
    assert "## 账户转账 [line:transfer_workflow.py::TransferWorkflow.run]" in out
    line_no = next(i for i, row in enumerate((root / "transfer_workflow.py").read_text(encoding="utf-8").splitlines(), 1)
                   if 'title="超过单笔限额，已拒绝"' in row)
    assert re.search(r"- \[end:[0-9a-f]+\] 超过单笔限额，已拒绝（失败）.*transfer_workflow\.py:" + str(line_no), out), out
    assert "transfer_workflow.py::TransferWorkflow.run" in out
    assert "转账完成" not in out  # 走到了的结局不列
    one = t.uncovered_outcomes(flow="账户转账")
    assert "功能线「账户转账」里没有测试走到的结局 1 个" in one
    assert [r for r in one.splitlines() if r.startswith(("- ", "## "))] == \
        [r for r in out.splitlines() if r.startswith(("- ", "## "))]
    with pytest.raises(ToolError, match="没有这条功能线"):
        t.uncovered_outcomes(flow="不存在的流程")
    assert t.call("uncovered_outcomes", {"project": pid}) == out


def test_uncovered_outcomes_warns_when_the_run_is_older_than_the_analysis(example, store):
    t, root, pid = example
    run = run_example(store, pid)
    p = root / "transfer_workflow.py"
    p.write_text("# 加一行\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert STALE in t.uncovered_outcomes()  # 还没重新分析：图过期的提醒
    t.analyze_project()
    out = t.uncovered_outcomes()
    assert out.startswith(f"⚠ 最近一次运行（#{run['id']}）用的是分析 #{run['analysis_id']}") and "重新分析过" in out
    assert "超过单笔限额，已拒绝" in out
    # 同文本的结局复制一份：配不上，单列"需要重新运行测试才知道的结局"
    text = p.read_text(encoding="utf-8")
    block = text[text.index("        if self.exceeds_single_limit(req):"):text.index("        balance = await")]
    p.write_text(text.replace(block, block + block), encoding="utf-8")
    t.analyze_project()
    out = t.uncovered_outcomes()
    head, _, unsure = out.partition("需要重新运行测试才知道的结局")
    assert unsure and unsure.count("超过单笔限额，已拒绝") == 2 and "超过单笔限额" not in head.split("\n", 2)[-1]


NEW_BRANCH = """\
        if req.amount > SINGLE_LIMIT * 2:
            return outcome("failed", title="金额离谱，直接拒绝")

"""


def test_outcomes_written_after_the_run_are_not_called_uncovered(example, store):
    """运行之后才写的结局，在那次运行时还不存在：重新分析之后不算"没有测试走到"，单列在"需要重新运行测试才知道"里；
    project_status 的测试运行一节也提醒这次运行比分析旧。运行时那次分析的图清理掉之后分不出来，警告里说明白。"""
    t, root, pid = example
    run = run_example(store, pid)
    assert "⚠" not in t.project_status().split("## 测试运行", 1)[1]
    p = root / "transfer_workflow.py"
    text = p.read_text(encoding="utf-8")
    at = text.index("        if self.exceeds_single_limit(req):")
    p.write_text(text[:at] + NEW_BRANCH + text[at:], encoding="utf-8")
    t.analyze_project()
    out = t.uncovered_outcomes()
    assert "运行之后才有的函数和结局、对不上的结局单列在最后（不算没走到）" in out.split("\n", 1)[0]
    head, _, unsure = out.partition("需要重新运行测试才知道的结局 1 个")
    assert "金额离谱，直接拒绝" in unsure and "金额离谱" not in head
    assert "功能线上没有测试走到的结局 1 个" in head and "超过单笔限额，已拒绝" in head  # 当时就没走到的照旧
    section = t.project_status().split("## 测试运行", 1)[1]
    now = store.latest_analysis(pid)["id"]
    assert f"⚠ 这次运行用的是分析 #{run['analysis_id']}，之后重新分析过（现在是 #{now}）" in section
    # 运行时那次分析没了（旧版留下的数据）：分不出来，照旧都算没走到，警告里说明
    with store._write() as con:
        con.execute("DELETE FROM analyses WHERE id = ?", (run["analysis_id"],))
    out = Tools(store, cwd=root).uncovered_outcomes()
    assert "那次分析的图已经清理掉了" in out.split("\n", 1)[0]
    assert "功能线上没有测试走到的结局 2 个" in out and "需要重新运行测试才知道的结局" not in out


def test_uncovered_outcomes_cut_at_the_cap_suggests_flow_only_when_not_given(example, store, monkeypatch):
    """输出超过上限被截断时：没给 flow 时说用 flow 只看一条功能线；已经给了 flow 时不再这么说，改说去平台的运行路径页签
    看、或者用别的工具只看具体的函数。"""
    t, _root, _pid = example
    run_example(store, _pid)
    monkeypatch.setattr(mcp_tools, "MAX_CHARS", 320)
    whole = t.uncovered_outcomes()
    assert "后面" in whole and "用 flow 只看一条功能线" in whole
    one = t.uncovered_outcomes(flow="账户转账")
    assert "后面" in one and "用 flow" not in one
    assert "运行路径" in one and "get_flow" in one


def test_uncovered_outcomes_uses_the_latest_run_with_paths(example, store):
    t, _root, pid = example
    good = run_example(store, pid)
    store.set_test_command(pid, "sleep 5")
    store.confirm_test_command(pid, "sleep 5")
    r = testflows.run_project_tests(store, pid, cancel=threading.Event(), timeout=0.2)
    assert r["code"] == "timeout"
    out = t.uncovered_outcomes()
    assert f"最近一次运行（#{r['run_id']}）没有跑完（超时）" in out and f"#{good['id']}" in out
    assert "超过单笔限额，已拒绝" in out


def test_project_status_ends_with_the_latest_run(example, store, tools):
    assert tools.project_status().rstrip().endswith(NEVER_RAN)
    t, _root, pid = example
    run = run_example(store, pid)
    out = t.project_status()
    section = out[out.index("## 测试运行"):]
    assert f"#{run['id']}" in section and "通过 9、失败 0" in section and "状态：通过" in section
    assert "核对：连线不符 0、结局不符 0、调用不符 0" in section
    assert "没有测试走到的结局 1 个" in section and "uncovered_outcomes" in section
    # 之后只跑了一条功能线的测试：结局个数仍按全量那次算（和 uncovered_outcomes 一个口径），单条线的运行另说一句
    sid = next(ln.sid for ln in t._view(None).graph.lines)
    r = testflows.run_project_tests(store, pid, cancel=threading.Event(), line=sid)
    assert r["kind"] == "ok", r
    section = t.project_status().split("## 测试运行", 1)[1]
    assert f"最近一次全量运行：#{run['id']}" in section and "没有测试走到的结局 1 个" in section
    assert f"之后还有只跑功能线「账户转账」的运行 #{r['run_id']}" in section


def test_project_status_does_not_call_a_run_without_records_verified(example, store):
    t, _root, pid = example
    store.set_test_command(pid, "true")
    store.confirm_test_command(pid, "true")
    testflows.run_project_tests(store, pid, cancel=threading.Event())
    section = t.project_status().split("## 测试运行", 1)[1]
    assert "核对：没有（测试没有跑完，或者没有收到记录）" in section and "连线不符" not in section


# ---------- 需求 ----------

def test_requirements_list_read_and_compare(example, store, tools):
    t, _root, pid = example
    assert "里还没有需求" in tools.list_requirements() and "save_requirement" in tools.list_requirements()
    out = t.list_requirements()
    assert "的需求 1 条" in out and "- R001  账户转账  [已确认]  功能线：账户转账" in out and "设计和实现对得上" in out
    one = t.get_requirement(requirement="R001")
    assert "需求 R001「账户转账」  状态：已确认" in one and "文件：.pystepmap/requirements/R001-账户转账.md" in one
    assert "步骤：查询请求状态" in one and "对照：设计和实现对得上（功能线 账户转账 [line:transfer_workflow.py::TransferWorkflow.run]）" in one
    assert "平台上还没有跑完的全量测试运行" in one and "够得上的状态是「已实现」" in one
    assert t.get_requirement(requirement="转账") == one  # 标题的片段
    with pytest.raises(ToolError, match="找不到需求「R999」。现有的：R001 账户转账"):
        t.get_requirement(requirement="R999")
    run_example(store, pid)
    covered = t.get_requirement(requirement="R001")
    assert "- 失败「超过单笔限额，已拒绝」：没有测试走到" in covered and "- 成功「转账完成」：有测试走到" in covered
    assert "够得上的状态是「已实现」" in covered  # 有一个结局没测到：到不了"已完成"


def test_requirement_outcome_written_after_the_run_says_the_run_did_not_have_it(example, store):
    """设计里的结局是运行之后才实现的：不说"不确定（运行之后代码改过）"，说那次运行里还没有它。"""
    t, root, pid = example
    run_example(store, pid)
    p = root / "transfer_workflow.py"
    text = p.read_text(encoding="utf-8")
    at = text.index("        if self.exceeds_single_limit(req):")
    p.write_text(text[:at] + NEW_BRANCH + text[at:], encoding="utf-8")
    req = root / ".pystepmap" / "requirements" / "R001-账户转账.md"
    design = req.read_text(encoding="utf-8")
    mark = "判断：超过单笔限额？\n"
    assert mark in design
    req.write_text(design.replace(mark, "判断：金额离谱？\n  是：\n    结局（失败）：金额离谱，直接拒绝\n" + mark, 1), encoding="utf-8")
    t.analyze_project()
    one = t.get_requirement(requirement="R001")
    assert "- 失败「金额离谱，直接拒绝」：那次运行里还没有这个结局（运行之后才写的，重新运行测试才知道）" in one
    assert "- 失败「超过单笔限额，已拒绝」：没有测试走到" in one and "不确定（" not in one


def test_save_requirement_writes_into_the_project_and_reports_the_comparison(example):
    t, root, _ = example
    flow = "步骤：查询请求状态\n步骤：风控打分\n判断：风险太高？\n  是：\n    结局（失败）：风险太高，已拒绝\n步骤：执行转账\n结局（成功）：转账完成"
    out = t.save_requirement(title="转账前先过风控", flow=flow, line="账户转账", description="风险高的转账直接拒绝。")
    file = root / ".pystepmap" / "requirements" / "R002-转账前先过风控.md"
    assert f"已保存到 {root}/.pystepmap/requirements/R002-转账前先过风控.md（新建）" in out and file.is_file()
    assert "状态：草稿" in out and "- 设计里的步骤「风控打分」在实现里没有" in out
    assert "- 设计里的判断「风险太高？」在实现里没有" in out and "- 设计里的结局「风险太高，已拒绝」在实现里没有" in out
    assert "实现里的步骤「冻结资金」设计里没有" in out
    again = t.save_requirement(id="R002", title="转账前先过风控", flow=flow, line="账户转账", status="已确认")
    assert "（改写）" in again and "状态：已确认" in again and "- 状态：已确认" in file.read_text(encoding="utf-8")
    assert sorted(p.name for p in file.parent.iterdir()) == ["R001-账户转账.md", "R002-转账前先过风控.md"]
    assert "- R002  转账前先过风控  [已确认]" in t.list_requirements() and "处不一致" in t.list_requirements()
    # 功能线还没有实现的需求：不数成"几处不一致"，和界面一样说找不到功能线
    new = t.save_requirement(title="退款", flow="步骤：查订单\n步骤：退钱\n结局（成功）：已退款", line="退款")
    assert "对照：图上找不到功能线「退款」（还没有实现，或者名字对不上、对上不止一条）；设计的 3 个节点都还没有实现。" in new
    assert "处不一致" not in new and "还没有实现\n- " not in new
    [row] = [ln for ln in t.list_requirements().splitlines() if "找不到功能线" in ln]
    assert row == "    图上找不到功能线「退款」（还没有实现，或者名字对不上、对上不止一条）；设计的 3 个节点都还没有实现"
    for kw, match in (({"flow": "随便写"}, "没有保存：设计的流程读不懂"), ({"id": "R050"}, "没有保存：没有编号是 R050"),
                      ({"title": "  "}, "缺少参数|没有保存")):
        with pytest.raises(ToolError, match=match):
            t.call("save_requirement", {"title": "x", "flow": flow, **kw})
    with pytest.raises(ToolError, match="没有保存：状态要是"):
        t.call("save_requirement", {"title": "x", "flow": flow, "status": "搞定"})


# ---------- run_line_tests ----------

@pytest.fixture
def platform(example, store, monkeypatch):
    """example 的工具，它发给平台的请求转给同一个数据目录上的平台接口（进程内）。返回 (工具, 项目 id, 发过的请求)。"""
    from fastapi.testclient import TestClient
    from pystepmap_server.app import create_app

    t, _, pid = example
    sent: list[tuple[str, str, dict | None]] = []
    monkeypatch.setenv("PYSTEPMAP_WEB_DIST", str(store.path.parent / "no-web"))
    with TestClient(create_app(store.path.parent), base_url="http://127.0.0.1:5179") as client:
        def ask(method: str, path: str, body: dict | None = None):
            sent.append((method, path, body))
            r = client.request(method, "/api" + path, json=body)
            return r.status_code, r.json()

        monkeypatch.setattr(t, "_platform", ask)
        yield t, pid, sent


def test_run_line_tests_needs_a_running_platform(example):
    t, _, _ = example
    with pytest.raises(ToolError, match="平台没有在运行"):
        t.run_line_tests(flow="账户转账")


def test_run_line_tests_asks_the_platform_and_reports_the_result(platform, store):
    t, pid, sent = platform
    with pytest.raises(ToolError, match="没有运行功能线「账户转账」.*没有允许 AI 经 MCP 运行测试"):
        t.run_line_tests(flow="账户转账")
    store.set_mcp_tests(pid, True)
    with pytest.raises(ToolError, match="先运行一次全部测试"):
        t.run_line_tests(flow="账户转账")
    assert "还没有单独运行过测试的结果" in t.run_line_tests(flow="账户转账", start=False)
    full = run_example(store, pid)
    sent.clear()
    out = t.call("run_line_tests", {"flow": "账户转账", "wait_seconds": 60})
    line = "line:transfer_workflow.py::TransferWorkflow.run"
    assert sent[0] == ("POST", f"/projects/{pid}/runs", {"line": line, "via": "mcp"})
    assert [m for m, _, _ in sent if m != "GET"] == ["POST"]  # 只发了这一个会改东西的请求
    assert f"名单 6 个测试（取自全量运行 #{full['id']}），跑了 9 个用例，通过 9、失败 0" in out and "（通过）" in out
    assert "图和实际执行一致" in out and "没有走到的结局 1 个" in out and "- 超过单笔限额，已拒绝（失败）" in out
    assert "之后新写的测试不在名单里" in out
    runs = store.list_runs(pid)
    assert [r["scope"] is None for r in runs] == [False, True]
    sent.clear()
    again = t.run_line_tests(flow="账户转账", start=False)
    assert f"测试运行 #{runs[0]['id']}" in again and not [m for m, _, _ in sent if m != "GET"]
    assert len(store.list_runs(pid)) == 2  # start=false 不启动


# ---------- 挂在路由上的工作流 ----------

def test_workflow_on_a_route_shows_method_and_path(store, tmp_path):
    """标了 @workflow 的接口：list_flows 在标题后面写（方法 路径），按路径也搜得到；get_flow、find_nodes 的函数头也写。
    没写标题的（标题就是"方法 路径"）不重复写。"""
    root = write(tmp_path / "work" / "admin", {"app/users.py": '''\
        from fastapi import APIRouter
        from pystepkit import workflow

        router = APIRouter(prefix="/users")


        @workflow(title="查看用户")
        @router.get("/{uid}")
        def get_user(uid):
            return uid


        @workflow
        @router.post("/")
        def create_user(body):
            return body
    '''})
    t = Tools(store, cwd=root)
    t.analyze_project(path=str(root))
    out = t.list_flows()
    assert "## users" in out and "后台任务" not in out
    assert "- 查看用户（GET /users/{uid}）  [line:app/users.py::get_user]  工作流  app/users.py:9" in out
    assert "- POST /users/  [line:app/users.py::create_user]  工作流  app/users.py:15" in out
    found = t.list_flows(query="/users/{uid}")
    assert "查看用户" in found and "create_user" not in found
    assert "工作流 get_user（GET /users/{uid}）  app/users.py:9" in t.get_flow(flow="查看用户")
    assert "工作流 get_user（GET /users/{uid}）「查看用户」" in t.find_nodes(file="app/users.py", line=10)


# ---------- 配合 OpenSpec、输出放得下 ----------

def test_requirements_written_in_openspec_files_are_listed_and_compared(example):
    t, root, _pid = example
    own = (root / ".pystepmap" / "requirements" / "R001-账户转账.md").read_text(encoding="utf-8")
    flow = own.split("```flow\n", 1)[1].split("```", 1)[0]
    change = root / "openspec" / "changes" / "add-review"
    change.mkdir(parents=True)
    (change / "design.md").write_text(f"## 流程变化\n\n```flow\n功能线：账户转账\n{flow}```\n", encoding="utf-8")
    out = t.list_requirements()
    assert "需求 2 条（.pystepmap/requirements/ 里 1 条、OpenSpec 文件里 1 条）" in out
    assert "- add-review  add-review：流程变化  [OpenSpec]  功能线：账户转账\n    设计和实现对得上" in out
    one = t.get_requirement(requirement="add-review")
    assert "状态：由 OpenSpec 的变更流程管" in one and "文件：openspec/changes/add-review/design.md（OpenSpec" in one
    assert "按现在的对照结果：设计和实现对得上" in one and "save_requirement" not in one
    with pytest.raises(ToolError, match="OpenSpec"):
        t.save_requirement(title="改", flow="步骤：参数校验", line="账户转账", id="add-review")


def test_a_project_that_uses_openspec_is_pointed_at_openspec(shop, tools):
    from pystepmap_server.mcp_server import INSTRUCTIONS, instructions_for

    assert "save_requirement 新建一条" in tools.list_requirements()
    assert instructions_for(shop) == instructions_for(None) == INSTRUCTIONS and "先 list_requirements" in INSTRUCTIONS
    (shop / "openspec" / "changes").mkdir(parents=True)
    (shop / "app" / "deep").mkdir()
    out = tools.list_requirements()
    assert "用 OpenSpec 管需求" in out and "```flow" in out and "save_requirement" not in out
    for cwd in (shop, shop / "app" / "deep"):
        text = instructions_for(cwd)
        assert "不要用 save_requirement" in text and "design.md" in text and "先 list_requirements" not in text
        assert text.startswith(INSTRUCTIONS[:40]) and "find_nodes" in text


def test_get_flow_of_one_function_and_falling_back_to_a_depth_that_fits(tools, store, monkeypatch):
    graph = graph_of(store)
    line = line_of(graph, PLACE)
    key = next(k for k, f in graph.functions.items() if f.qualname == "create_order")
    assert tools.get_flow(flow=PLACE, function="create_order") == "\n".join(function_outline(graph, key))
    assert tools.get_flow(flow=PLACE, function=graph.functions[key].sid, depth=1) == "\n".join(function_outline(graph, key, depth=1))
    with pytest.raises(ToolError, match="没有叫"):
        tools.get_flow(flow=PLACE, function="nope")
    with pytest.raises(ToolError, match="outline"):
        tools.get_flow(flow=PLACE, function="create_order", format="mermaid")
    deep, flat = line_outline(graph, line, depth=1), line_outline(graph, line)
    assert len(deep) > len(flat)
    monkeypatch.setattr(mcp_tools, "FLOW_BUDGET", len(flat))
    out = tools.get_flow(flow=PLACE, depth=3)
    head, body = out.split("\n", 1)
    assert body == flat and "depth=3" in head and "depth=0" in head and "function" in head
    assert tools.get_flow(flow=PLACE) == flat  # depth=0 没有更小的可退


CAUGHT = '''
class TargetError(RuntimeError): ...


def guard(conn):
    if conn.bad:
        raise TargetError("拒绝写入")
    if conn.gone:
        raise KeyError("没有这个库")


def check(conn):
    try:
        guard(conn)
    except TargetError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "reason": ""}


def main(conn):
    c = check(conn)
    if not c["ok"]:
        return None
    return 1


if __name__ == "__main__":
    main(None)
'''


def test_paths_to_outcome_does_not_list_a_caught_exception_as_an_outcome(tmp_path, store):
    """被调函数里 raise、调用它的函数接住了的异常不是功能线的结局：单独列在最后，说在哪里被接住；没被接住的照常列。"""
    root = tmp_path / "caught"
    root.mkdir()
    (root / "m.py").write_text(CAUGHT, encoding="utf-8")
    t = Tools(store, cwd=root)
    assert "已添加" in t.analyze_project(path=str(root))
    out = t.paths_to_outcome(flow="main", outcome="failed")
    head, _, tail = out.partition("### 不算这条线的结局：被接住的异常 1 个")
    assert "匹配「failed」的结局 1 个" in head and '抛出 KeyError("没有这个库")' in head and "TargetError" not in head
    assert '抛出 TargetError("拒绝写入")' in tail and "在 函数 check 的" in tail and "「捕获 TargetError」被接住" in tail
    only = t.paths_to_outcome(flow="main", outcome="拒绝写入")
    assert "里没有以「拒绝写入」结束的结局" in only and "被接住" in only
    # 带 reason 的字典不再一律叫"结束"
    flow = t.get_flow(function="check")
    assert '返回 {"ok": False, "reason": str(e)}' in flow and '返回 {"ok": True, "reason": ""}' in flow


def test_analyze_says_which_mark_arguments_it_could_not_read(tmp_path, store):
    """group 写成模块级字符串常量时照样分组；写成读不出来的值时不悄悄落到默认分组：analyze_project 和 project_status
    都指出是哪一行。"""
    root = write(tmp_path / "keys", {"keys.py": (
        "from pystepkit import workflow\nGROUP = 'Key 与场景'\n"
        "@workflow(title='删除 Key', group=GROUP)\ndef delete_key(): ...\n"
        "@workflow(title='列出 Key', group=GROUP.strip())\ndef list_keys(): ...\n")})
    t = Tools(store, cwd=root)
    out = t.analyze_project(path=str(root))
    assert "⚠ keys.py:5 标记里的 group=GROUP.strip() 读不出来，当作没写" in out and "keys.py:3" not in out
    assert "Key 与场景" in t.list_flows() and "标记里读不出来的参数：1 处\n  keys.py:5" in t.project_status()
    assert "读不出来" not in _summary_text({"functions": 1})  # 旧的分析没有这一项


def test_requirement_text_points_the_user_at_the_picture_on_the_platform(example, monkeypatch):
    """设计的流程在对话里只有缩进文本：get_requirement / save_requirement 的结果里给出平台上这条需求的地址（画着图），
    平台没在运行时说怎么打开。"""
    from pystepmap_server import mcp_tools
    t, root, pid = example
    off = t.get_requirement(requirement="R001")
    assert "设计的流程图：平台没有在运行" in off and "pystepmap serve" in off and "http://" not in off
    monkeypatch.setattr(mcp_tools, "serve_port", lambda home: 5179)
    on = t.get_requirement(requirement="R001")
    assert f"http://127.0.0.1:5179/#/{pid}/req%3AR001" in on and "给用户" in on
    saved = t.save_requirement(title="新的", flow="步骤：查询请求状态\n结局（成功）：转账完成", line="账户转账")
    assert f"http://127.0.0.1:5179/#/{pid}/req%3AR002" in saved
    # OpenSpec 文件里的第二块：编号带 #2，地址里转义
    change = root / "openspec" / "changes" / "add-x"
    change.mkdir(parents=True)
    block = "```flow\n功能线：账户转账\n步骤：查询请求状态\n结局（成功）：转账完成\n```\n"
    (change / "design.md").write_text("## 流程\n\n" + block + "\n" + block, encoding="utf-8")
    assert f"/#/{pid}/req%3Aadd-x%232" in t.get_requirement(requirement="add-x#2")


NEW_LINE = """

@step(title="查退款单")
def load_refund(order_no: str) -> dict:
    return {}


@condition(title="可以退款？")
def refundable(refund: dict) -> bool:
    return bool(refund)


@step(title="原路退回")
def pay_back(refund: dict) -> None:
    pass


@workflow(title="退款", group="订单")
def refund_order(order_no: str):
    refund = load_refund(order_no)
    if refundable(refund):
        pay_back(refund)
        return outcome("ok", title="已退款")
    return outcome("failed", title="不能退款")
"""


def test_analyze_detail_controls_how_much_of_the_flow_change_is_spelled_out(store, shop):
    """新增一条功能线之后重新分析：默认新增的函数各一行（节点个数、结局的标题），改动的函数照旧逐个节点列；summary 每个
    函数都一行；full 全部逐个节点列。三种写法说的是同一批功能线和函数。"""
    t = Tools(store, cwd=shop)
    t.analyze_project(path=str(shop))
    path = shop / "app" / "orders.py"

    def again(extra: str, **kw) -> str:
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
        return t.analyze_project(**kw)

    normal = again(NEW_LINE)
    assert "## 功能线 [新增] 退款（订单）" in normal
    assert "refund_order  app/orders.py:" in normal and "——步骤 2、判断 1、结局 2（「已退款」「不能退款」）" in normal
    assert "另有新增的函数 3 个（步骤、判断函数等）：查退款单、可以退款？、原路退回" in normal
    assert "步骤「查退款单」" not in normal and "### 函数 [新增] 查退款单" not in normal and "新增的函数只写了一行" in normal
    path.write_text(path.read_text(encoding="utf-8").replace(NEW_LINE, ""), encoding="utf-8")
    t.analyze_project()
    full = again(NEW_LINE, detail="full")
    assert "步骤「查退款单」" in full and "只写了一行" not in full and len(normal) < len(full) * 0.6
    path.write_text(path.read_text(encoding="utf-8").replace(NEW_LINE, ""), encoding="utf-8")
    add_vip_branch(shop)
    t.analyze_project()
    summary = again(NEW_LINE, detail="summary")
    assert "## 功能线 [新增] 退款（订单）" in summary and "每个函数只写了一行" in summary and "行号" not in summary
    remove_guard(shop)
    changed = t.analyze_project(detail="summary")
    assert "### 函数 [改动] 下单  app/orders.py::place_order" in changed and "——节点删除 2" in changed
    assert "原来在「" not in changed  # 删除的节点不逐个列
    with pytest.raises(ToolError, match="detail 要是 normal、summary、full 之一"):
        t.call("analyze_project", {"detail": "tiny"})
