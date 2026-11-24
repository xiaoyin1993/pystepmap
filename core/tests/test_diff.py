"""两次分析之间的流程差异：函数、功能线按 sid 配对，函数里的节点按源码顺序做序列对齐（docs/graph-schema.md
"两种标识：key 与 sid"）。每个用例把两份小源码写进临时目录各分析一次再比。"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
from conftest import analyze_files, find_fn, node_by
from pystepmap.config import ProjectConfig
from pystepmap.diff import (
    MAX_BLOCK_PAIRS,
    EdgeChange,
    FunctionChange,
    GraphDiff,
    LineChange,
    NodeChange,
    diff_from_json,
    diff_graphs,
    diff_text,
    diff_to_json,
)

FILE = "app/orders.py"

BASE = '''
from pystepkit import outcome


@router.post("/orders")
async def place(order, a):
    """下单"""
    check(order)
    if a > 1:
        notify(order)
    charge(order)
    return outcome("ok", title="完成")


def check(order):
    """校验订单"""


def charge(order):
    """扣款"""


def notify(order):
    """通知用户"""


def seen(order):
    """处理过了"""
'''


def graphs(tmp_path, old: str | dict[str, str], new: str | dict[str, str]):
    """把两份源码（一个字符串是 app/orders.py 的内容，或 {相对路径: 内容}）各写进一个临时目录分析，返回 (旧图, 新图)。"""
    def files(src):
        return {FILE: src} if isinstance(src, str) else src
    return analyze_files(tmp_path / "old", files(old)), analyze_files(tmp_path / "new", files(new))


def diff_of(tmp_path, old, new) -> GraphDiff:
    return diff_graphs(*graphs(tmp_path, old, new))


def sid_of(g, qualname: str) -> str:
    return find_fn(g, qualname).sid


def line_of(g, qualname: str):
    key = find_fn(g, qualname).key
    [line] = [ln for ln in g.lines if ln.id == key]
    return line


def statuses(changes) -> list[tuple[str, str]]:
    return [(c.status, c.type) for c in changes]


def test_no_change_when_reformatted(tmp_path):
    new = '''
    # 订单接口
    # 上方多了几行注释
    from pystepkit import outcome


    def check(order):
        """校验订单"""


    def charge(order):
        """扣款"""


    def notify(order):
        """通知用户"""


    def seen(order):
        """处理过了"""


    @router.post("/orders")
    async def place(order, a):
        """下单"""
        # 先校验
        check(order)
        if a > 1:  # 金额大才通知
            notify(order)
        charge(
            order
        )
        return outcome("ok", title="完成")
    '''
    old_g, new_g = graphs(tmp_path, BASE, new)
    assert find_fn(old_g, "place").key != find_fn(new_g, "place").key  # 行号都变了
    assert old_g.file_hashes != new_g.file_hashes
    d = diff_graphs(old_g, new_g)
    assert d.empty
    assert d == GraphDiff(functions={}, lines=[])


def test_added_branch(tmp_path):
    new = BASE.replace("    check(order)\n", '    check(order)\n    if seen(order):\n'
                                             '        return outcome("skipped", title="已处理")\n')
    old_g, new_g = graphs(tmp_path, BASE, new)
    d = diff_graphs(old_g, new_g)
    place = sid_of(new_g, "place")
    assert list(d.functions) == [place]
    fc = d.functions[place]
    assert (fc.status, fc.title, fc.file) == ("changed", "下单", FILE)
    assert (fc.new_key, fc.old_key) == (find_fn(new_g, "place").key, find_fn(old_g, "place").key)
    assert sorted(statuses(fc.nodes)) == [("added", "cond"), ("added", "end")]
    fg = find_fn(new_g, "place")
    cond = node_by(fg, type="cond", subtitle="seen(order)")
    added = {c.type: c for c in fc.nodes}
    assert (added["cond"].new_id, added["cond"].new_sid, added["cond"].new_line) == (cond.id, cond.sid, cond.line)
    assert (added["cond"].old_sid, added["cond"].old_line, added["cond"].after) == ("", 0, "")
    assert added["end"].new_title == "已处理"
    [line] = d.lines
    assert (line.status, line.sid, line.title, line.group) == ("changed", line_of(new_g, "place").sid, "下单", "orders")
    assert (line.new_id, line.root) == (fg.key, place)
    assert line.functions == [place]
    assert (line.tasks_added, line.tasks_removed) == ([], [])
    assert not d.empty
    # "校验订单"到原来的判断那条直连没了，但新图里经新增的判断仍然走得到：节点的新增已经说明了，不单列；
    # 新增的判断自己的两个分支报成新增连线
    old_cond = node_by(fg, type="cond", subtitle="a > 1")
    end = node_by(fg, type="end", title="已处理")
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "yes", cond.id, end.id), ("added", "no", cond.id, old_cond.id)])


def test_removed_step(tmp_path):
    new = BASE.replace("    charge(order)\n", "")
    old_g, new_g = graphs(tmp_path, BASE, new)
    d = diff_graphs(old_g, new_g)
    fc = d.functions[sid_of(new_g, "place")]
    [removed] = [c for c in fc.nodes if c.status == "removed"]
    assert [c.status for c in fc.nodes] == ["removed"]
    old_step = node_by(find_fn(old_g, "place"), subtitle="charge")
    assert (removed.type, removed.old_sid, removed.old_title, removed.old_subtitle, removed.old_line) == \
        ("step", old_step.sid, "扣款", "charge", old_step.line)
    assert (removed.new_id, removed.new_sid, removed.new_line) == ("", "", 0)
    # 它原来前面那个节点是 if 里的"通知用户"，仍然在：after 是那个节点在新图里的 id
    assert removed.after == node_by(find_fn(new_g, "place"), subtitle="notify").id
    # 新多出的"通知用户 → 完成""判断 —否→ 完成"在旧图里经删掉的"扣款"走得到：不单列
    assert fc.edges == []


def test_removed_first_step_points_after_start(tmp_path):
    d = diff_of(tmp_path, BASE, BASE.replace("    check(order)\n", ""))
    [removed] = next(iter(d.functions.values())).nodes
    assert (removed.status, removed.old_subtitle, removed.after, removed.after_title, removed.after_label) == \
        ("removed", "check", "n1", "下单", "")
    assert "（原来在「下单」之后）" in diff_text(d)


TRANSFER = (Path(__file__).resolve().parents[2] / "examples/transfer/transfer_workflow.py").read_text(encoding="utf-8")
LIMIT_GUARD = """        if self.exceeds_single_limit(req):
            await self.reject(req, "超过单笔限额")
            return outcome("failed", title="超过单笔限额，已拒绝", reason="超过单笔限额")

"""


def test_removed_guard_clause_hangs_after_the_previous_condition(tmp_path):
    """连着几个守卫子句（流程写法里最常见）时删掉一个：按源码顺序，它前面那个留下的节点是上一个守卫提前返回的结局
    ——那个分支没变，不能怪它。按旧图的控制流往回找：删掉的三个节点都挂在"参数不合法？"的"否"上。"""
    assert TRANSFER.count(LIMIT_GUARD) == 1
    old_g, new_g = graphs(tmp_path, TRANSFER, TRANSFER.replace(LIMIT_GUARD, ""))
    d = diff_graphs(old_g, new_g)
    fc = d.functions["app/orders.py::TransferWorkflow.run"]
    invalid = node_by(find_fn(new_g, "TransferWorkflow.run"), type="cond", title="参数不合法？")
    assert statuses(fc.nodes) == [("removed", "cond"), ("removed", "step"), ("removed", "end")]
    assert {(c.after, c.after_title, c.after_label) for c in fc.nodes} == {(invalid.id, "参数不合法？", "否")}
    assert "（原来在「参数不合法？」之后，否）" in diff_text(d)


def test_removed_chain_after_a_kept_guard(tmp_path):
    """守卫之后连着删掉三句：三个都挂在留下的判断的"否"上（不是源码里紧挨着的那个提前返回的结局）。"""
    old = STEPS.replace("    check(order)\n    charge(order)\n    notify(order)\n",
                        "    check(order)\n    if order.big:\n        return None\n"
                        "    audit(order)\n    charge(order)\n    notify(order)\n    check(order, 2)\n")
    new = old.replace("    audit(order)\n    charge(order)\n    notify(order)\n", "")
    old_g, new_g = graphs(tmp_path, old, new)
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::run"]
    cond = node_by(find_fn(new_g, "run"), type="cond")
    assert statuses(fc.nodes) == [("removed", "step")] * 3
    assert {(c.after, c.after_label) for c in fc.nodes} == {(cond.id, "否")}


def test_removed_first_statement_of_a_loop_body(tmp_path):
    loop = STEPS.replace("    check(order)\n", "    for item in order.items:\n        audit(item)\n        check(item)\n")
    old_g, new_g = graphs(tmp_path, loop, loop.replace("        audit(item)\n", ""))
    [removed] = diff_graphs(old_g, new_g).functions["app/orders.py::run"].nodes
    loop_node = node_by(find_fn(new_g, "run"), type="loop")
    assert (removed.status, removed.old_subtitle, removed.after, removed.after_label) == \
        ("removed", "audit", loop_node.id, "每次")


def test_changed_condition(tmp_path):
    old_g, new_g = graphs(tmp_path, BASE, BASE.replace("if a > 1:", "if a > 2:"))
    d = diff_graphs(old_g, new_g)
    fc = d.functions[sid_of(new_g, "place")]
    [changed] = fc.nodes
    assert (changed.status, changed.type) == ("changed", "cond")
    assert (changed.old_subtitle, changed.new_subtitle) == ("a > 1", "a > 2")
    old_cond = node_by(find_fn(old_g, "place"), type="cond")
    new_cond = node_by(find_fn(new_g, "place"), type="cond")
    assert (changed.old_sid, changed.old_line, changed.new_sid, changed.new_id, changed.new_line) == \
        (old_cond.sid, old_cond.line, new_cond.sid, new_cond.id, new_cond.line)
    assert changed.old_sid != changed.new_sid
    assert fc.edges == []  # 改了的节点是配上对的：它的连线照常比，没有变


def test_insert_next_to_a_changed_condition(tmp_path):
    """紧挨着改了的条件前面插一个步骤（两者落在对齐的同一段里）：按类型和文字相似配对，不按位置——条件是改动，
    步骤是新增，没有删除。"""
    old_g, new_g = graphs(tmp_path, BASE, BASE.replace("    if a > 1:\n", "    seen(order)\n    if a > 2:\n"))
    fc = diff_graphs(old_g, new_g).functions[sid_of(new_g, "place")]
    assert statuses(fc.nodes) == [("added", "step"), ("changed", "cond")]
    assert fc.nodes[0].new_subtitle == "seen"
    assert (fc.nodes[1].old_subtitle, fc.nodes[1].new_subtitle) == ("a > 1", "a > 2")
    assert fc.edges == []


def test_insert_branch_next_to_a_changed_condition(tmp_path):
    """同上，插的是一个判断加结局（和改了的条件同类型）：仍然配给文字相像的那个。"""
    new = BASE.replace("    if a > 1:\n", '    if seen(order):\n        return outcome("skipped", title="已处理")\n'
                                          "    if a > 2:\n")
    old_g, new_g = graphs(tmp_path, BASE, new)
    fc = diff_graphs(old_g, new_g).functions[sid_of(new_g, "place")]
    assert statuses(fc.nodes) == [("added", "cond"), ("added", "end"), ("changed", "cond")]
    assert (fc.nodes[0].new_subtitle, fc.nodes[2].old_subtitle, fc.nodes[2].new_subtitle) == \
        ("seen(order)", "a > 1", "a > 2")


STEPS = '''
@router.post("/run")
async def run(order):
    """跑一遍"""
    check(order)
    charge(order)
    notify(order)


def check(order):
    """校验订单"""


def charge(order):
    """扣款"""


def notify(order):
    """通知用户"""


def audit(order):
    """审计"""
'''


def test_bridged_edges_are_not_reported(tmp_path):
    """两个留下的步骤之间插进 / 删掉一个步骤：它们之间直连的有无已经由节点的增删说明了，连线不单列。"""
    inserted = STEPS.replace("    charge(order)\n", "    audit(order)\n    charge(order)\n")
    fc = diff_of(tmp_path / "ins", STEPS, inserted).functions["app/orders.py::run"]
    assert statuses(fc.nodes) == [("added", "step")] and fc.edges == []
    deleted = STEPS.replace("    charge(order)\n", "")
    fc = diff_of(tmp_path / "del", STEPS, deleted).functions["app/orders.py::run"]
    assert statuses(fc.nodes) == [("removed", "step")] and fc.edges == []


def test_bridging_goes_through_added_nodes_only(tmp_path):
    """在循环体里的分支上插一个步骤：分支原来直接回到循环（loop_back），现在经新增的步骤回去——说明了，不单列。
    改了的节点不算"新增"：经过它走到的不算被说明。"""
    loop = STEPS.replace("    check(order)\n", "    for item in order.items:\n        if item.ok:\n"
                                                "            continue\n        check(item)\n")
    inserted = loop.replace("            continue\n", "            audit(item)\n            continue\n")
    old_g, new_g = graphs(tmp_path / "loop", loop, inserted)
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::run"]
    fg = find_fn(new_g, "run")
    # 原来的"是 → 回到循环"由新增的审计说明了，不报；报的只有留下的判断进新增节点的那个分支（看得到审计在"是"上）
    assert statuses(fc.nodes) == [("added", "step")]
    assert [(e.status, e.kind, e.src, e.dst) for e in fc.edges] == \
        [("added", "yes", node_by(fg, type="cond").id, node_by(fg, subtitle="audit").id)]
    # 扣款从"是"分支挪到"否"分支、顺手改了参数（改动）：改了的节点是留下的节点，不是"新增"，经过它走得到的直连
    # 照常报——判断 —否→ 通知（删除）、判断 —是→ 通知（新增）都在
    old = STEPS.replace("    charge(order)\n", "    if order.big:\n        charge(order)\n")
    new = STEPS.replace("    charge(order)\n", "    if order.big:\n        pass\n    else:\n        charge(order, 2)\n")
    old_g, new_g = graphs(tmp_path / "chg", old, new)
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::run"]
    assert statuses(fc.nodes) == [("changed", "step")]
    fg = find_fn(new_g, "run")
    cond, charge, notify = (node_by(fg, type="cond").id, node_by(fg, subtitle="charge").id,
                            node_by(fg, subtitle="notify").id)
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "yes", cond, notify), ("added", "no", cond, charge),
        ("removed", "yes", cond, charge), ("removed", "no", cond, notify)])


HELPERS = '''

def a(x):
    """甲"""


def b(x):
    """乙"""


def z(x):
    """新加的"""


def log(x):
    """记日志"""
'''

TRY = '''
from pystepkit import outcome


@router.post("/t")
async def entry(x):
    """入口"""
    try:
        a(x)
        b(x)
    except ValueError:
        log(x)
        return outcome("failed", title="坏了")
    return outcome("ok", title="好了")
''' + HELPERS


def test_first_statement_of_a_try_body(tmp_path):
    """try 体的第一句插进 / 删掉一句：异常连线从 try 体的入口出发，入口换了，原来那条 raise 连线由新增 / 删除的入口
    节点说明了（新图里新入口连到同一个捕获、并且走得到原来的入口），不单列。新入口自己的异常连线报成新增。"""
    inserted = TRY.replace("    try:\n        a(x)\n", "    try:\n        z(x)\n        a(x)\n")
    old_g, new_g = graphs(tmp_path, TRY, inserted)
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::entry"]
    fg = find_fn(new_g, "entry")
    assert statuses(fc.nodes) == [("added", "step")]
    assert [(e.status, e.kind, e.src, e.dst) for e in fc.edges] == \
        [("added", "raise", node_by(fg, subtitle="z").id, node_by(fg, type="catch").id)]
    fc = diff_graphs(new_g, old_g).functions["app/orders.py::entry"]  # 反过来就是删掉 try 体的第一句
    assert statuses(fc.nodes) == [("removed", "step")] and fc.edges == []


def test_raise_edge_is_bridged_only_through_raise(tmp_path):
    """少掉的异常连线要由"第一步也是异常连线"的路径说明：甲挪到 try 之前、try 体第一句换成新加的 z——甲不再在 try
    里，"甲 —异常→ 捕获"没了要报（甲 → z 是顺序，不算）；新入口 z 的异常连线报成新增。反过来同样。"""
    moved = TRY.replace("    try:\n        a(x)\n", "    a(x)\n    try:\n        z(x)\n")
    old_g, new_g = graphs(tmp_path, TRY, moved)
    fg = find_fn(new_g, "entry")
    a_id, z_id, catch = node_by(fg, subtitle="a").id, node_by(fg, subtitle="z").id, node_by(fg, type="catch").id
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::entry"]
    assert statuses(fc.nodes) == [("added", "step")]
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "raise", z_id, catch), ("removed", "raise", a_id, catch)])
    back = diff_graphs(new_g, old_g).functions["app/orders.py::entry"]
    old_fg = find_fn(old_g, "entry")
    assert statuses(back.nodes) == [("removed", "step")]
    assert [(e.status, e.kind, e.src, e.dst) for e in back.edges] == \
        [("added", "raise", node_by(old_fg, subtitle="a").id, node_by(old_fg, type="catch").id)]


def test_widened_try_shows_the_new_entry(tmp_path):
    """try 往前扩了一句（原来在 try 之前的甲也进了 try，最前面还加了 z）：新入口 z 的异常连线报成新增（列在 z 下面），
    原来"乙 —异常→ 捕获"报删除。"""
    old = TRY.replace("    try:\n        a(x)\n", "    a(x)\n    try:\n")
    new = TRY.replace("    try:\n        a(x)\n", "    try:\n        z(x)\n        a(x)\n")
    old_g, new_g = graphs(tmp_path, old, new)
    d = diff_graphs(old_g, new_g)
    fc = d.functions["app/orders.py::entry"]
    fg = find_fn(new_g, "entry")
    z, b_step, catch = node_by(fg, subtitle="z"), node_by(fg, subtitle="b"), node_by(fg, type="catch")
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "raise", z.id, catch.id), ("removed", "raise", b_step.id, catch.id)])
    rows = diff_text(d).splitlines()
    i = next(k for k, row in enumerate(rows) if "步骤「新加的」" in row)
    assert rows[i + 1] == f"      异常 → 捕获「{catch.title}」L{catch.line}"
    assert f"  - 连线删除：步骤「乙」L{b_step.line} —异常→ 捕获「{catch.title}」L{catch.line}" in rows


def test_new_branch_of_a_kept_condition_is_shown(tmp_path):
    """留下的判断多了一个分支、通向新增的节点（match 加一个 case、if 加 else）：这条连线报成新增，看得到模式 / 否。"""
    match = '''
    @router.post("/t")
    async def entry(x):
        """入口"""
        match x.kind:
            case "a":
                a(x)
        log(x)
    ''' + textwrap.indent(HELPERS, "    ")
    old_g, new_g = graphs(tmp_path / "m", match, match.replace("                a(x)\n",
                                                             '                a(x)\n            case "z":\n'
                                                             "                z(x)\n"))
    d = diff_graphs(old_g, new_g)
    fg = find_fn(new_g, "entry")
    cond, z = node_by(fg, type="cond"), node_by(fg, subtitle="z")
    fc = d.functions["app/orders.py::entry"]
    assert [(e.status, e.kind, e.label, e.src, e.dst) for e in fc.edges] == [("added", "case", '"z"', cond.id, z.id)]
    assert f'连线新增：判断「{cond.title}」L{cond.line} —"z"→ 步骤「新加的」L{z.line}' in diff_text(d)

    plain = match.replace('        match x.kind:\n            case "a":\n                a(x)\n',
                          "        if x.ok:\n            a(x)\n")
    old_g, new_g = graphs(tmp_path / "i", plain, plain.replace("            a(x)\n",
                                                             "            a(x)\n        else:\n            z(x)\n"))
    fg = find_fn(new_g, "entry")
    cond, z = node_by(fg, type="cond"), node_by(fg, subtitle="z")
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::entry"]
    assert [(e.status, e.kind, e.src, e.dst) for e in fc.edges] == [("added", "no", cond.id, z.id)]


GUARD = '''
from pystepkit import outcome


@router.post("/t")
async def entry(x):
    """入口"""
    if x.ok:
        return outcome("skipped", title="跳过")
    b(x)
    log(x)
''' + HELPERS


def test_bridging_respects_the_branch(tmp_path):
    """判断节点出发的连线：经新增节点"走得到"必须从同一个分支出发。原来"否"直接到乙；现在"是"分支经新增的 z 到乙、
    "否"分支去了跳过——"否 → 乙"没了，要报。"""
    new = GUARD.replace('    if x.ok:\n        return outcome("skipped", title="跳过")\n',
                        '    if x.ok:\n        z(x)\n    else:\n        return outcome("skipped", title="跳过")\n')
    old_g, new_g = graphs(tmp_path, GUARD, new)
    fc = diff_graphs(old_g, new_g).functions["app/orders.py::entry"]
    fg = find_fn(new_g, "entry")
    cond, b_step, skip = node_by(fg, type="cond"), node_by(fg, subtitle="b"), node_by(fg, title="跳过")
    z_step = node_by(fg, subtitle="z")
    assert statuses(fc.nodes) == [("added", "step")]
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("removed", "yes", cond.id, skip.id), ("removed", "no", cond.id, b_step.id), ("added", "no", cond.id, skip.id),
        ("added", "yes", cond.id, z_step.id)])
    text = diff_text(diff_graphs(old_g, new_g))
    assert f"连线删除：判断「{cond.title}」L{cond.line} —否→ 步骤「乙」L{b_step.line}" in text


def test_added_branch_node_lists_its_branches(tmp_path):
    """新增的判断 / 循环 / 捕获节点：它的出边（一端是新增节点）照样报成新增连线，文本里列在这个节点下面。"""
    loop = '''
    @router.post("/t")
    async def entry(xs):
        """入口"""
        for x in xs:
            if x.ok:
                continue
            a(x)
        log(xs)
    ''' + textwrap.indent(HELPERS, "    ")
    new = loop.replace("            a(x)\n", "            a(x)\n            if x.bad:\n                break\n")
    old_g, new_g = graphs(tmp_path, loop, new)
    d = diff_graphs(old_g, new_g)
    fc = d.functions["app/orders.py::entry"]
    fg = find_fn(new_g, "entry")
    bad, loop_node, log_step = (node_by(fg, type="cond", subtitle="x.bad"), node_by(fg, type="loop"),
                                node_by(fg, subtitle="log"))
    assert statuses(fc.nodes) == [("added", "cond")]
    assert sorted((e.status, e.kind, e.label, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "break", "是", bad.id, log_step.id), ("added", "loop_back", "否", bad.id, loop_node.id)])
    rows = diff_text(d).splitlines()
    i = next(k for k, row in enumerate(rows) if "判断「x.bad」" in row)
    assert sorted(rows[i + 1:i + 3]) == sorted([
        f"      跳出循环 是 → 步骤「记日志」L{log_step.line}", f"      回到循环 否 → 循环「{loop_node.title}」L{loop_node.line}"])
    assert "连线" not in rows  # 都列在节点下面了，没有单独的连线小节


def test_duplicate_statements(tmp_path):
    old = '''
    @router.post("/ping")
    async def ping(order):
        notify(order)
        charge(order)
        notify(order)
        if order.big:
            notify(order)

    def notify(order):
        """通知用户"""

    def charge(order):
        """扣款"""
    '''
    new = old.replace("async def ping(order):\n", "async def ping(order):\n        notify(order)\n")
    old_g, new_g = graphs(tmp_path, old, new)
    assert [n.sid.partition("#")[2] for n in find_fn(new_g, "ping").nodes if n.subtitle == "notify"] == ["", "2", "3", "4"]
    fc = diff_graphs(old_g, new_g).functions[sid_of(new_g, "ping")]
    assert statuses(fc.nodes) == [("added", "step")]
    assert fc.nodes[0].new_subtitle == "notify"
    assert fc.edges == []  # 开始节点到原来第一句的直连，经新增的那句仍然走得到


def test_edge_change_between_kept_nodes(tmp_path):
    old = BASE.replace("        notify(order)\n", "        notify(order)\n    else:\n        pass\n")
    new = BASE.replace("        notify(order)\n", "        pass\n    else:\n        notify(order)\n")
    old_g, new_g = graphs(tmp_path, old, new)
    d = diff_graphs(old_g, new_g)
    fc = d.functions[sid_of(new_g, "place")]
    assert fc.status == "changed" and fc.nodes == []
    fg = find_fn(new_g, "place")
    cond, notify, charge = (node_by(fg, type="cond").id, node_by(fg, subtitle="notify").id,
                            node_by(fg, subtitle="charge").id)
    assert sorted((e.status, e.kind, e.src, e.dst) for e in fc.edges) == sorted([
        ("added", "yes", cond, charge), ("added", "no", cond, notify),
        ("removed", "yes", cond, notify), ("removed", "no", cond, charge)])
    assert all(e.label == "" for e in fc.edges)
    assert [ln.status for ln in d.lines] == ["changed"]
    # 连线带两端节点（新图里）的类型、标题、行号，文本里按标题写
    c, s = node_by(fg, type="cond"), node_by(fg, subtitle="charge")
    yes = next(e for e in fc.edges if e.status == "added" and e.kind == "yes")
    assert (yes.src_type, yes.src_title, yes.src_line, yes.dst_type, yes.dst_title, yes.dst_line) == \
        ("cond", c.title, c.line, "step", "扣款", s.line)
    text = diff_text(d)
    assert f"连线新增：判断「{c.title}」L{c.line} —是→ 步骤「扣款」L{s.line}" in text
    assert f"连线删除：判断「{c.title}」L{c.line} —否→ 步骤「扣款」L{s.line}" in text
    assert _section_of(text, "连线新增") == "连线" and _section_of(text, "连线删除") == "连线"


def test_function_added_removed_renamed(tmp_path):
    added = BASE.replace("    charge(order)\n", "    charge(order)\n    audit(order)\n") + '''

def audit(order):
    """审计"""
'''
    old_g, new_g = graphs(tmp_path, BASE, added)
    d = diff_graphs(old_g, new_g)
    audit, place = sid_of(new_g, "audit"), sid_of(new_g, "place")
    assert sorted(d.functions) == sorted([audit, place])
    fa = d.functions[audit]
    assert (fa.status, fa.title, fa.file, fa.new_key, fa.old_key, fa.edges) == \
        ("added", "审计", FILE, find_fn(new_g, "audit").key, "", [])
    assert [c.new_id for c in fa.nodes] == [n.id for n in find_fn(new_g, "audit").nodes]
    assert all(c.status == "added" for c in fa.nodes)
    assert statuses(d.functions[place].nodes) == [("added", "step")]
    [line] = d.lines
    assert line.status == "changed" and line.functions == [place, audit]

    renamed = BASE.replace("charge", "bill")
    old_g, new_g = graphs(tmp_path / "r", BASE, renamed)
    d = diff_graphs(old_g, new_g)
    charge, bill = sid_of(old_g, "charge"), sid_of(new_g, "bill")
    assert (d.functions[charge].status, d.functions[charge].old_key, d.functions[charge].new_key) == \
        ("removed", find_fn(old_g, "charge").key, "")
    assert d.functions[charge].title == "扣款" and d.functions[charge].nodes == []
    assert d.functions[bill].status == "added"
    # "扣款 charge" 和 "扣款 bill" 不够像（相似度 < 0.6）：不算改动，是删一个、加一个
    assert statuses(d.functions[sid_of(new_g, "place")].nodes) == [("removed", "step"), ("added", "step")]
    [line] = d.lines
    assert line.functions == [sid_of(new_g, "place"), bill, charge]  # 新图里涉及的在前，旧图里才涉及的在后


def test_function_outside_any_line_does_not_change_lines(tmp_path):
    new = BASE + '''

def helper():
    notify(None)
'''
    d = diff_of(tmp_path, BASE, new)
    assert [f.status for f in d.functions.values()] == ["added"]
    assert d.lines == []
    assert not d.empty


def test_line_added_removed(tmp_path):
    old = BASE + '''

@router.post("/refund")
async def refund(order):
    """退款"""
    charge(order)
'''
    new = BASE + '''

@router.post("/cancel")
async def cancel(order):
    """取消订单"""
    notify(order)
'''
    old_g, new_g = graphs(tmp_path, old, new)
    d = diff_graphs(old_g, new_g)
    assert [(ln.status, ln.title, ln.group) for ln in d.lines] == [("added", "取消订单", "orders"),
                                                                   ("removed", "退款", "orders")]
    added, removed = d.lines
    assert (added.sid, added.new_id, added.functions) == \
        (line_of(new_g, "cancel").sid, find_fn(new_g, "cancel").key, [sid_of(new_g, "cancel")])
    assert (removed.sid, removed.new_id, removed.functions) == (line_of(old_g, "refund").sid, "", [])
    assert (added.root, removed.root) == (sid_of(new_g, "cancel"), sid_of(old_g, "refund"))  # 删除的线取旧图
    assert {s: f.status for s, f in d.functions.items()} == {sid_of(new_g, "cancel"): "added",
                                                            sid_of(old_g, "refund"): "removed"}
    # 文本里删掉的线下面列它的根函数，不放进"其他有变化的函数"
    rows = diff_text(d).splitlines()
    i = rows.index(f"## 功能线 [删除] 退款（orders）  {removed.sid}")
    assert rows[i + 1].startswith(f"### 函数 [删除] 退款  {sid_of(old_g, 'refund')}")
    assert not any("其他有变化的函数" in row for row in rows)


def test_text_changed_node_with_the_same_text(tmp_path):
    """改了语句、标题和副标题都没变的节点：文本说"标题没变，语句变了"，带旧、新行号。"""
    new = BASE.replace("    check(order)\n", "    # 先校验\n    check(order)\n").replace("    charge(order)\n",
                                                                                   "    charge(order, 2)\n")
    old_g, new_g = graphs(tmp_path, BASE, new)
    d = diff_graphs(old_g, new_g)
    [changed] = d.functions[sid_of(new_g, "place")].nodes
    assert (changed.status, changed.old_title, changed.new_title) == ("changed", "扣款", "扣款")
    old_line = node_by(find_fn(old_g, "place"), subtitle="charge").line
    new_line = node_by(find_fn(new_g, "place"), subtitle="charge").line
    assert new_line == old_line + 1
    assert f"步骤「扣款」（charge）（标题没变，语句变了）  {FILE}:{old_line} → {FILE}:{new_line}" in diff_text(d)


TASKS = '''
@router.post("/orders")
async def place(order):
    """下单"""
    await send_mail.kiq(order)


@broker.task()
async def send_mail(order):
    """发邮件"""


@broker.task()
async def audit_task(order):
    """审计"""
'''


def test_task_tree_change(tmp_path):
    more = TASKS.replace("    await send_mail.kiq(order)\n", "    await send_mail.kiq(order)\n    await audit_task.kiq(order)\n")
    old_g, new_g = graphs(tmp_path, TASKS, more)
    d = diff_graphs(old_g, new_g)
    place_line = next(ln for ln in d.lines if ln.sid == line_of(new_g, "place").sid)
    assert place_line.status == "changed"
    assert (place_line.tasks_added, place_line.tasks_removed) == ([sid_of(new_g, "audit_task")], [])
    assert place_line.functions == [sid_of(new_g, "place")]  # 派发多了一步
    # 审计原来没人派发、自己成线；现在并进了下单的线
    assert [(ln.status, ln.title) for ln in d.lines if ln is not place_line] == [("removed", "审计")]

    back = diff_graphs(new_g, old_g)
    place_line = next(ln for ln in back.lines if ln.sid == line_of(new_g, "place").sid)
    assert (place_line.tasks_added, place_line.tasks_removed) == ([], [sid_of(new_g, "audit_task")])


SHARED = '''
@router.post("/a")
async def entry_a(order):
    """入口甲"""
    await send_mail.kiq(order)


@router.post("/b")
async def entry_b(order):
    """入口乙"""
    await send_mail.kiq(order)


@router.post("/c")
async def entry_c(order):
    """入口丙"""
    notify(order)


@broker.task()
async def send_mail(order):
    """发邮件"""


def notify(order):
    """通知用户"""
'''


def test_task_tree_change_alone_marks_the_line(tmp_path):
    """派发树变了、但这条线涉及的函数都没变（第三个入口也派发了发邮件，它成了公共任务，不再并入入口甲的线）：
    入口甲的线照样 changed。"""
    more = SHARED.replace('    """入口丙"""\n', '    """入口丙"""\n    await send_mail.kiq(order)\n')
    cfg = ProjectConfig(common_task_threshold=3)
    old_g, new_g = analyze_files(tmp_path / "old", {FILE: SHARED}, cfg), analyze_files(tmp_path / "new", {FILE: more}, cfg)
    assert new_g.common_tasks == [find_fn(new_g, "send_mail").key] and old_g.common_tasks == []
    d = diff_graphs(old_g, new_g)
    assert list(d.functions) == [sid_of(new_g, "entry_c")]
    by_title = {ln.title: ln for ln in d.lines}
    mail = sid_of(new_g, "send_mail")
    for title in ("入口甲", "入口乙"):
        line = by_title[title]
        assert (line.status, line.functions, line.tasks_added, line.tasks_removed) == ("changed", [], [], [mail])
    assert (by_title["入口丙"].status, by_title["入口丙"].functions) == ("changed", [sid_of(new_g, "entry_c")])
    assert (by_title["发邮件"].status, by_title["发邮件"].functions) == ("added", [])  # 公共任务自己成线


def test_json_roundtrip(tmp_path):
    new = BASE.replace("if a > 1:", "if a > 2:").replace("    charge(order)\n", "") + '''

@router.post("/cancel")
async def cancel(order):
    """取消订单"""
    notify(order)
'''
    d = diff_of(tmp_path, BASE, new)
    data = diff_to_json(d)
    assert data["version"] == 1
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data  # 纯 JSON
    fc = next(iter(data["functions"].values()))
    assert set(fc) == {"status", "sid", "title", "file", "new_key", "old_key", "nodes", "edges", "file_same"}
    assert set(fc["nodes"][0]) == {"status", "type", "new_id", "new_sid", "new_title", "new_subtitle", "new_line",
                                   "old_sid", "old_title", "old_subtitle", "old_line", "after", "after_title",
                                   "after_label"}  # 空字段照留
    assert set(data["lines"][0]) == {"status", "sid", "title", "group", "new_id", "functions", "tasks_added",
                                     "tasks_removed", "root"}
    back = diff_from_json(data)
    assert back == d
    assert isinstance(next(iter(back.functions.values())), FunctionChange)
    assert isinstance(back.lines[0], LineChange)
    assert diff_from_json(diff_to_json(GraphDiff(functions={}, lines=[]))).empty


def test_json_roundtrip_with_edges():
    d = GraphDiff(functions={"a.py::f": FunctionChange(
        status="changed", sid="a.py::f", title="f", file="a.py", new_key="a.py::f@1", old_key="a.py::f@2",
        nodes=[NodeChange(status="removed", type="step", old_sid="step:12345678", old_title="t", old_line=3,
                          after="n1")],
        edges=[EdgeChange(status="added", kind="case", label="1", src="n2", dst="n3")])}, lines=[])
    assert diff_from_json(json.loads(json.dumps(diff_to_json(d)))) == d


def test_json_reads_diffs_stored_before_after_label():
    """早于 after_title / after_label 存下来的差异照样读得出来（取空串）。"""
    data = diff_to_json(GraphDiff(functions={"a.py::f": FunctionChange(
        status="changed", sid="a.py::f", title="f", file="a.py",
        nodes=[NodeChange(status="removed", type="step", old_line=3, after="n1", after_title="开始", after_label="否")])}))
    assert diff_from_json(json.loads(json.dumps(data))) == diff_from_json(data)
    del data["functions"]["a.py::f"]["nodes"][0]["after_title"]
    del data["functions"]["a.py::f"]["nodes"][0]["after_label"]
    node = diff_from_json(data).functions["a.py::f"].nodes[0]
    assert (node.after, node.after_title, node.after_label) == ("n1", "", "")


def test_json_rejects_other_versions_and_ignores_unknown_fields():
    for bad in ([], "x", {}, {"version": 2, "functions": {}, "lines": []}):
        with pytest.raises(ValueError, match="流程差异"):
            diff_from_json(bad)
    data = diff_to_json(GraphDiff(lines=[LineChange(status="removed", sid="line:a.py::f", title="f", group="g")]))
    data["future"] = 1
    data["lines"][0]["future"] = 2
    assert diff_from_json(data).lines == [LineChange(status="removed", sid="line:a.py::f", title="f", group="g")]


def _section_of(text: str, *needles: str) -> str:
    """第一个同时含有每个 needle 的行属于哪个小标题（新增 / 删除 / 改动 / 连线）。needle 后面紧跟数字的不算（行号 1 不匹配
    11）。"""
    heading = ""
    for row in text.splitlines():
        if row.strip().rstrip("：:") in ("新增", "删除", "改动", "连线"):
            heading = row.strip().rstrip("：:")
        elif all(re.search(re.escape(n) + r"(?!\d)", row) for n in needles):
            return heading
    raise AssertionError(f"{needles!r} 不在文本里：\n{text}")


def test_text(tmp_path):
    # 三处修改之间隔着没变的节点（各在自己的一段里）：开头插一个分支，改一个条件，删一个步骤
    new = BASE.replace("if a > 1:", "if a > 2:").replace("    charge(order)\n", "").replace(
        "    check(order)\n", '    if seen(order):\n        return outcome("skipped", title="已处理")\n    check(order)\n')
    old_g, new_g = graphs(tmp_path, BASE, new)
    d = diff_graphs(old_g, new_g)
    text = diff_text(d)
    fg_new, fg_old = find_fn(new_g, "place"), find_fn(old_g, "place")
    seen_cond = node_by(fg_new, type="cond", subtitle="seen(order)")
    skipped_end = node_by(fg_new, type="end", title="已处理")
    removed_step = node_by(fg_old, subtitle="charge")
    changed_cond = node_by(fg_new, type="cond", subtitle="a > 2")
    assert _section_of(text, seen_cond.title, f"{FILE}:{seen_cond.line}") == "新增"
    assert _section_of(text, "已处理", f"{FILE}:{skipped_end.line}") == "新增"
    assert _section_of(text, "扣款", f"{FILE}:{removed_step.line}") == "删除"  # 删除的用旧图的行号
    assert _section_of(text, "a > 2", f"{FILE}:{changed_cond.line}") == "改动"  # 改动的用新图的行号
    assert _section_of(text, "a > 1") == "改动"
    assert "下单" in text and line_of(new_g, "place").sid in text
    assert diff_text(GraphDiff(functions={}, lines=[])) == "流程没有变化"


def test_text_for_one_line(tmp_path):
    old = BASE + '''

@router.post("/refund")
async def refund(order):
    """退款"""
    charge(order)
'''
    new = BASE.replace("if a > 1:", "if a > 2:") + textwrap.dedent('''

    @router.post("/refund")
    async def refund(order):
        """退款"""
        charge(order)
        notify(order)
    ''')
    old_g, new_g = graphs(tmp_path, old, new)
    d = diff_graphs(old_g, new_g)
    assert len(d.lines) == 2
    place, refund = line_of(new_g, "place").sid, line_of(new_g, "refund").sid
    only_place = diff_text(d, line_sid=place)
    assert "a > 2" in only_place and "退款" not in only_place and refund not in only_place
    only_refund = diff_text(d, line_sid=refund)
    assert "通知用户" in only_refund and "a > 2" not in only_refund
    assert "流程没有变化" in diff_text(d, line_sid="line:app/orders.py::nowhere")
    assert "退款" in diff_text(d) and "a > 2" in diff_text(d)


# ---------- diff_text 的上限 ----------

# 超出上限时的一行提示；量词和数的东西一致：功能线按"条功能线"，节点 + 连线按"处"，整段截断按"行"
MORE = re.compile(r"…还有 (\d+) (条功能线|处|行)未列出（用 flow 参数只看一条功能线）")


def _fn(sid: str, n_nodes: int, n_edges: int = 0, title_len: int = 4) -> FunctionChange:
    nodes = [NodeChange(status="added", type="step", new_id=f"n{i + 2}", new_sid=f"step:{i:08x}",
                        new_title=f"节点{i}" + "长" * title_len, new_subtitle=f"call{i}", new_line=i + 2)
             for i in range(n_nodes)]
    edges = [EdgeChange(status="added", kind="next", label="", src=f"m{i}", dst=f"m{i + 1}", src_type="step",
                        src_title=f"起点{i}", src_line=i, dst_type="step", dst_title=f"终点{i}", dst_line=i + 1)
             for i in range(n_edges)]
    return FunctionChange(status="changed", sid=sid, title=sid.rpartition("::")[2], file="a.py",
                          new_key=f"{sid}@1", old_key=f"{sid}@1", nodes=nodes, edges=edges)


def test_text_caps_changed_lines():
    lines = [LineChange(status="removed", sid=f"line:a.py::f{i:02}", title=f"线{i:02}", group="g") for i in range(35)]
    text = diff_text(GraphDiff(lines=lines))
    assert sum(row.startswith("## 功能线") for row in text.splitlines()) == 30
    assert "线29" in text and "线30" not in text
    assert [m.groups() for m in MORE.finditer(text)] == [("5", "条功能线")]


def test_text_caps_items_per_function():
    fc = _fn("a.py::f", 30, 20)
    d = GraphDiff(functions={fc.sid: fc},
                  lines=[LineChange(status="changed", sid="line:a.py::f", title="f", group="g", functions=[fc.sid])])
    text = diff_text(d)
    assert "节点29" in text and "起点9" in text and "起点10" not in text  # 30 个节点 + 10 条连线 = 40 处
    assert [m.groups() for m in MORE.finditer(text)] == [("10", "处")]
    assert MORE.search(diff_text(d, line_sid="line:a.py::f"))  # 只看一条线时同样有上限


def test_text_hard_cap():
    fns = {f"a.py::f{i:02}": _fn(f"a.py::f{i:02}", 40, title_len=60) for i in range(30)}
    lines = [LineChange(status="changed", sid=f"line:{sid}", title=sid, group="g", functions=[sid]) for sid in fns]
    text = diff_text(GraphDiff(functions=fns, lines=lines))
    assert len(text) <= 20_000
    last = text.splitlines()[-1]
    m = MORE.fullmatch(last)
    assert m and int(m.group(1)) > 0 and m.group(2) == "行"
    assert len(diff_text(GraphDiff(functions=dict(list(fns.items())[:1]), lines=lines[:1]))) < 20_000


def test_text_function_in_several_lines_is_detailed_once():
    fc = _fn("a.py::shared", 2)
    lines = [LineChange(status="changed", sid=f"line:a.py::e{i}", title=f"入口{i}", group="g", functions=[fc.sid])
             for i in range(3)]
    text = diff_text(GraphDiff(functions={fc.sid: fc}, lines=lines))
    assert text.count("节点0") == 1
    assert text.count(f"同上：{fc.sid}") == 2
    assert "其他有变化的函数" not in text


def test_big_replace_block_falls_back_to_removed_and_added(tmp_path):
    """对不上的一段太大（两边节点数之积超过 MAX_BLOCK_PAIRS）时不逐对比相似，全算删除和新增——比相似的开销有上限。
    这里 200 个步骤的引号全换了（sid 全变、文字没变）。"""
    def requoted(n: int) -> list[str]:
        helpers = "".join(f'\n\ndef step_{i}(x, y):\n    """第{i}步"""\n' for i in range(n))
        body = "".join(f"    step_{i}(x, 'k{i}')\n" for i in range(n))
        old = f'@router.post("/t")\nasync def entry(x):\n    """入口"""\n{body}{helpers}'
        new = old.replace("x, 'k", 'x, "k').replace("')\n", '")\n')
        return diff_graphs(*graphs(tmp_path / str(n), old, new)).functions["app/orders.py::entry"].nodes

    assert 200 * 200 > MAX_BLOCK_PAIRS
    assert sorted(c.status for c in requoted(200)) == ["added"] * 200 + ["removed"] * 200
    assert [c.status for c in requoted(20)] == ["changed"] * 20  # 小的一段照常逐对配成改动


def test_json_rejects_malformed_input():
    good = diff_to_json(GraphDiff(
        functions={"a.py::f": FunctionChange(status="changed", sid="a.py::f", title="f", file="a.py", nodes=[
            NodeChange(status="added", type="step", new_id="n2", new_line=3)])},
        lines=[LineChange(status="changed", sid="line:a.py::f", title="f", group="g", functions=["a.py::f"])]))
    assert diff_from_json(good).functions["a.py::f"].nodes[0].new_line == 3

    def broken(edit):
        data = json.loads(json.dumps(good))
        edit(data)
        return data

    cases = [
        lambda d: d.pop("functions"),
        lambda d: d.update(lines={}),
        lambda d: d.update(functions=[]),
        lambda d: d["functions"]["a.py::f"].pop("sid"),
        lambda d: d["functions"]["a.py::f"].update(status="moved"),
        lambda d: d["functions"]["a.py::f"].update(nodes={}),
        lambda d: d["functions"]["a.py::f"]["nodes"][0].update(new_line="3"),
        lambda d: d["functions"]["a.py::f"]["nodes"][0].update(new_line=True),
        lambda d: d["functions"]["a.py::f"]["nodes"][0].pop("type"),
        lambda d: d["functions"]["a.py::f"].update(edges=[{"status": "added"}]),
        lambda d: d["lines"][0].update(functions="a.py::f"),
        lambda d: d["lines"][0].update(tasks_added=[1]),
        lambda d: d["lines"].append(None),
    ]
    for edit in cases:
        with pytest.raises(ValueError, match="流程差异"):
            diff_from_json(broken(edit))


def test_text_removed_line_uses_its_root():
    """删除的线下面列的根函数取 LineChange.root，不从线的 sid 猜（同一根函数的第二条线、带 #n 的函数 sid 都猜不准）。"""
    fc = FunctionChange(status="removed", sid="a.py::f#2", title="第二处 f", file="a.py", old_key="a.py::f@9")
    line = LineChange(status="removed", sid="line:a.py::f#2#2", title="线", group="g", root="a.py::f#2")
    rows = diff_text(GraphDiff(functions={fc.sid: fc}, lines=[line])).splitlines()
    i = rows.index("## 功能线 [删除] 线（g）  line:a.py::f#2#2")
    assert rows[i + 1] == "### 函数 [删除] 第二处 f  a.py::f#2（原在 a.py:9）"
    assert not any("其他有变化的函数" in row for row in rows)


def test_engine_change_is_called_out_and_untouched_files_are_tagged(tmp_path):
    """两次分析的引擎标识不一样时，差异的文字开头提醒，所在文件没改的函数标"文件没改：引擎变化引起"；引擎没变时不提。"""
    from pystepmap import engineid
    from pystepmap.analyze import analyze_project

    (tmp_path / "a.py").write_text("def helper(x):\n    return x\n\n\ndef a(x):\n    if x:\n        return helper(x)\n    return 0\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8")
    old = analyze_project(tmp_path)
    (tmp_path / "b.py").write_text("def b(x):\n    if x > 1:\n        return 1\n    return 0\n", encoding="utf-8")
    new = analyze_project(tmp_path)
    # a.py 没改，但图变了：模拟引擎换了画法（旧引擎少画了 a 里的一条连线）
    next(f for f in old.functions.values() if f.qualname == "a").edges.pop()
    d = diff_from_json(diff_to_json(diff_graphs(old, new)))
    same = {fc.sid.split("::")[1]: fc.file_same for fc in d.functions.values()}
    assert same == {"a": True, "b": False}
    plain = diff_text(d)
    assert "引擎" not in plain and "文件没改" not in plain
    assert diff_text(d, engines=("0.1.0+aaaa", "0.1.0+aaaa")) == plain
    text = diff_text(d, engines=("0.1.0", "0.1.0+bbbb"))
    assert "⚠ 两次分析之间引擎也变了（0.1.0 → 0.1.0+bbbb）：流程有改动的 2 个函数里 1 个所在的文件内容没变" in text
    [tagged] = [ln for ln in text.splitlines() if "〔文件没改：引擎变化引起〕" in ln]
    assert "a.py::a" in tagged
    # 旧的差异（没有 file_same）照样读得回来
    raw = diff_to_json(d)
    for fc in raw["functions"].values():
        del fc["file_same"]
    assert all(not fc.file_same for fc in diff_from_json(raw).functions.values())
    # 引擎标识：版本号 + 出图用到的源码的指纹；只算 analyze 顺着导入找得到的模块
    assert engineid.ENGINE_VERSION.startswith(engineid.__version__ + "+") and len(engineid.engine_fingerprint()) == 8
    mods = engineid.engine_modules()
    assert {"analyze", "build", "resolve", "rules", "model"} <= set(mods) and not {"cli", "kit", "trace", "diff"} & set(mods)


def test_brief_text_folds_new_functions_and_keeps_changed_ones_spelled_out():
    """diff_text 的 brief："added" 时新增的函数不逐个节点列（功能线的根函数一行个数和结局，别的并成一行标题），改动的
    函数照旧；"all" 时改动的函数也只写一行个数。"""
    def node(status, type_, title, sid):
        return NodeChange(status, type_, new_sid=sid, new_title=title, new_line=3) if status == "added" else \
            NodeChange(status, type_, old_sid=sid, old_title=title, old_line=3)
    root = FunctionChange("added", "a.py::pay", "付款", "a.py", new_key="a.py::pay@1", nodes=[
        node("added", "start", "付款", "start"), node("added", "step", "扣款", "step:1"),
        node("added", "end", "已付款", "end:1"), node("added", "end", "未捕获异常退出", "end:uncaught")])
    helper = FunctionChange("added", "a.py::charge", "扣款", "a.py", new_key="a.py::charge@9",
                            nodes=[node("added", "end", "返回 1", "end:1")])
    old = FunctionChange("changed", "a.py::refund", "退款", "a.py", new_key="a.py::refund@20",
                         nodes=[node("removed", "step", "查单", "step:1")],
                         edges=[EdgeChange("added", "next", "", "n1", "n2", "start", "退款", 20, "end", "完成", 22)])
    d = GraphDiff({fc.sid: fc for fc in (root, helper, old)}, [
        LineChange("added", "line:a.py::pay", "付款", "资金", functions=[root.sid, helper.sid], root=root.sid),
        LineChange("changed", "line:a.py::refund", "退款", "资金", functions=[old.sid, helper.sid], root=old.sid)])
    full, added, every = diff_text(d), diff_text(d, brief="added"), diff_text(d, brief="all")
    assert "  - 步骤「扣款」  a.py:3" in full and "### 函数 [新增] 扣款" in full and "### 同上：a.py::charge" in full
    assert "### 函数 [新增] 付款  a.py::pay  a.py:1  ——步骤 1、结局 1（「已付款」）" in added
    assert added.count("另有新增的函数 1 个（步骤、判断函数等）：扣款") == 1 and "同上" not in added  # 第二条线里不再重复
    assert "  - 步骤「扣款」" not in added and "  - 步骤「查单」  a.py:3" in added and "连线新增" in added
    assert "### 函数 [改动] 退款  a.py::refund  a.py:20  ——节点删除 1、连线变化 1" in every
    assert "查单" not in every and "行号" not in every and "行号" in added
    assert len(every) < len(added) < len(full)
