"""需求模块：需求文件、设计流程的文本、和实现的对照。"""

import shutil
from pathlib import Path

import pytest
from pystepmap import requirements as rq
from pystepmap.analyze import analyze_project
from pystepmap.cli import main

TRANSFER = Path(__file__).resolve().parents[2] / "examples" / "transfer"


@pytest.fixture(scope="module")
def graph():
    return analyze_project(TRANSFER)


@pytest.fixture(scope="module")
def shipped():
    [r] = rq.load_requirements(TRANSFER)
    return r


def variant(shipped, old: str, new: str, **kw) -> rq.Requirement:
    assert old in shipped.flow_text
    r = rq.parse_requirement(rq.render_requirement(rq.Requirement(
        "R009", "改过的", kw.get("status", "已确认"), kw.get("line", "账户转账"), "", shipped.flow_text.replace(old, new))))
    assert r.errors == []
    return r


def kinds(cmp: rq.Comparison) -> list[tuple[str, str]]:
    return [(d.kind, d.title) for d in cmp.differences]


def test_the_example_requirement_matches_the_example_code(shipped, graph):
    assert (shipped.id, shipped.title, shipped.status, shipped.line, shipped.errors) == ("R001", "账户转账", "已确认", "账户转账", [])
    assert shipped.file == ".pystepmap/requirements/R001-账户转账.md" and "重复提交" in shipped.description
    cmp = rq.compare(shipped, graph)
    assert cmp.matches and cmp.differences == [] and cmp.notes == []
    assert cmp.line_sid == "line:transfer_workflow.py::TransferWorkflow.run"
    assert [(t, k) for t, k, sid in cmp.outcomes if sid] == [
        ("重复请求，不再处理", "skipped"), ("参数不合法，已拒绝", "failed"), ("超过单笔限额，已拒绝", "failed"),
        ("余额不足，已拒绝", "failed"), ("已转人工复核", "skipped"), ("转账失败，资金已解冻", "failed"), ("转账完成", "ok")]
    assert rq.reachable_status(shipped, cmp) == "已实现" and rq.reachable_status(shipped, cmp, covered=True) == "已完成"


def test_each_kind_of_difference_is_reported(shipped, graph):
    more = rq.compare(variant(shipped, "步骤：查询可用余额\n", "步骤：查询可用余额\n步骤：风控打分\n"), graph)
    assert kinds(more) == [("missing", "风控打分")] and not more.matches
    assert rq.reachable_status(variant(shipped, "步骤：冻结资金\n", ""), more) == "已确认"
    fewer = rq.compare(variant(shipped, "步骤：冻结资金\n", ""), graph)
    assert kinds(fewer) == [("extra", "冻结资金")] and fewer.differences[0].node.startswith("step:")
    wrong = rq.compare(variant(shipped, "结局（跳过）：已转人工复核", "结局（失败）：已转人工复核"), graph)
    assert kinds(wrong) == [("kind", "已转人工复核")] and "设计的是失败，实现的是跳过" in wrong.differences[0].text
    swapped = rq.compare(variant(shipped, "步骤：冻结资金\n步骤：执行转账", "步骤：执行转账\n步骤：冻结资金"), graph)
    assert {k for k, _ in kinds(swapped)} == {"order"} and "执行转账 → 冻结资金" in swapped.differences[0].text
    # 路上有一个没实现的步骤时，调换顺序照样报得出来
    both = rq.compare(variant(shipped, "步骤：冻结资金\n步骤：执行转账", "步骤：风控打分\n步骤：执行转账\n步骤：冻结资金"), graph)
    assert {k for k, _ in kinds(both)} == {"missing", "order"}
    # 判断走错了分支：设计说"是"之后执行转账，实现里"是"是去人工复核
    branch = rq.compare(variant(
        shipped, "判断：需要人工复核？\n  是：\n    步骤：提交人工复核\n    结局（跳过）：已转人工复核",
        "判断：需要人工复核？\n  否：\n    步骤：提交人工复核\n    结局（跳过）：已转人工复核"), graph)
    assert "order" in {k for k, _ in kinds(branch)}
    assert any("需要人工复核？（否） → 提交人工复核" in d.text for d in branch.differences)


def test_one_misplaced_node_is_reported_once_not_once_per_path(shipped, graph):
    """一个判断放错了位置会让好几条路都走不通：只报这一个节点"位置和设计不一样"，说清设计里它前后是谁、实现里在
    第几行，附一条走不通的路当例子。"""
    moved = rq.compare(variant(
        shipped,
        "判断：需要人工复核？\n  是：\n    步骤：提交人工复核\n    结局（跳过）：已转人工复核\n步骤：冻结资金\n",
        "步骤：冻结资金\n判断：需要人工复核？\n  是：\n    步骤：提交人工复核\n    结局（跳过）：已转人工复核\n"), graph)
    [d] = moved.differences
    assert (d.kind, d.type, d.title) == ("order", "step", "冻结资金") and d.node.startswith("step:")
    assert "步骤「冻结资金」的位置和设计不一样" in d.text
    assert "设计里它在「余额不足？」之后、「需要人工复核？」之前" in d.text and "实现里它在第 " in d.text
    assert "因此设计的 3 条路按顺序走不通，例如：" in d.text


def test_line_not_found_means_nothing_is_implemented(shipped, graph):
    for name in ("还没有的流程", ""):
        cmp = rq.compare(variant(shipped, "步骤：冻结资金\n", "步骤：冻结资金\n", line=name), graph)
        assert not cmp.matches and cmp.line_sid == "" and cmp.differences[0].kind == "no_line"
        assert sum(1 for d in cmp.differences if d.kind == "missing") == len(rq._all_items(shipped.items))
    assert rq.find_line(graph, "line:transfer_workflow.py::TransferWorkflow.run").title == "账户转账"
    assert rq.find_line(graph, "TransferWorkflow.run").title == "账户转账"


def test_flow_text_problems_are_reported_with_line_numbers():
    def errors(text: str) -> list[str]:
        return rq.parse_flow(text)[1]

    assert errors("") == ["流程是空的"]
    assert "第 1 行：读不懂" in errors("先做点什么")[0]
    assert errors("步骤：a\n\t步骤：b") == ["第 2 行：缩进要用空格，不要用制表符"]
    assert errors("步骤：a\n  步骤：b") == ["第 2 行：缩进多了（上一行不是判断的分支、也不是循环）"]
    assert errors("结局（好）：x") == ["第 1 行：结局要写成 结局（成功）： / 结局（跳过）： / 结局（失败）："]
    assert errors("结局（成功）：x\n步骤：y") == ["第 2 行：前面已经是结局，这一行走不到"]
    assert errors("判断：c？\n  步骤：x") == ["第 2 行：判断的下面只能是 是： 或 否：，然后再缩进一层写内容"]
    assert errors("是：\n  步骤：x")[0] == "第 1 行：是： 只能写在判断的下面"
    assert errors("步骤：") == ["第 1 行：步骤 没有写标题"]
    assert errors("步骤：a\n  失败时：") == ["第 2 行：失败时： 下面没有内容"]
    items, errs = rq.parse_flow("判断: c?\n  是:\n    结局(失败): 不行\n  否:\n    步骤: 继续\n循环：每一项\n  步骤：处理\n结局（成功）：好")
    assert errs == [] and [i.kind for i in items] == ["cond", "loop", "end"]
    assert items[0].yes[0].end_kind == "failed" and items[0].no[0].title == "继续" and items[1].body[0].title == "处理"
    paths, cut = rq.design_paths(items)
    assert not cut and len(paths) == 3  # 是 → 不行；否 → 进循环体；否 → 不进循环体


def test_requirement_file_problems_are_listed_not_raised(tmp_path):
    folder = tmp_path / rq.DIR
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("没有标题\n- 编号：X1\n- 状态：进行中\n", encoding="utf-8")
    (folder / "b.md").write_text("# 好的\n- 编号：R002\n- 状态：草稿\n- 功能线：x\n## 设计的流程\n```flow\n步骤：a\n```\n", encoding="utf-8")
    (folder / "c.md").write_text("# 重号\n- 编号：R002\n- 状态：草稿\n## 设计的流程\n```flow\n步骤：a\n```\n", encoding="utf-8")
    (folder / "d.md").write_bytes(b"\xff\xfe")
    (folder / "link.md").symlink_to(folder / "b.md")
    got = {Path(r.file).name: r for r in rq.load_requirements(tmp_path)}
    assert sorted(got) == ["a.md", "b.md", "c.md", "d.md"]
    assert len(got["a.md"].errors) == 4 and got["b.md"].errors == [] and got["b.md"].items[0].title == "a"
    assert got["c.md"].errors == ["编号 R002 和 .pystepmap/requirements/b.md 重复"] and "读不了" in got["d.md"].errors[0]
    assert rq.load_requirements(tmp_path / "nope") == []


def test_save_creates_numbers_and_rewrites_and_refuses_bad_input(tmp_path):
    flow = "步骤：a\n结局（成功）：好"
    first = rq.save_requirement(tmp_path, title="大额  转账/复核", line="账户转账", description="说明", flow=flow)
    assert (first.id, first.status, first.file) == ("R001", "草稿", ".pystepmap/requirements/R001-大额-转账-复核.md")
    second = rq.save_requirement(tmp_path, title="第二条", line="", description="", flow=flow)
    assert second.id == "R002"
    again = rq.save_requirement(tmp_path, id="R001", title="改了标题", line="账户转账", description="新说明", flow=flow, status="已确认")
    assert again.file == first.file  # 整条改写，文件名不变
    loaded = {r.id: r for r in rq.load_requirements(tmp_path)}
    assert (loaded["R001"].title, loaded["R001"].status, loaded["R001"].description, loaded["R001"].errors) == ("改了标题", "已确认", "新说明", [])
    before = sorted(p.name for p in (tmp_path / rq.DIR).iterdir())
    for kw, match in (({"title": " "}, "没有标题"), ({"status": "完成了"}, "状态要是"), ({"flow": "乱写"}, "读不懂"),
                      ({"id": "R077"}, "没有编号是 R077"), ({"description": "```py"}, "不要写")):
        args = {"title": "t", "line": "", "description": "", "flow": flow, **kw}
        with pytest.raises(rq.RequirementError, match=match):
            rq.save_requirement(tmp_path, **args)
    assert sorted(p.name for p in (tmp_path / rq.DIR).iterdir()) == before


def test_cli_lists_requirements_with_the_comparison(tmp_path, capsys):
    assert main(["requirements", str(TRANSFER)]) == 0
    out = capsys.readouterr().out
    assert "R001  账户转账  [已确认]" in out and "设计和实现对得上（功能线：账户转账）" in out
    work = tmp_path / "transfer"
    shutil.copytree(TRANSFER, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    f = work / rq.DIR / "R001-账户转账.md"
    f.write_text(f.read_text(encoding="utf-8").replace("步骤：冻结资金\n", "步骤：冻结资金\n步骤：风控打分\n"), encoding="utf-8")
    assert main(["requirements", str(work)]) == 1
    assert "和实现有 1 处不一致（功能线：账户转账）" in capsys.readouterr().out
    text = f.read_text(encoding="utf-8")
    f.write_text(text.replace("账户转账", "退款"), encoding="utf-8")  # 功能线还没有实现：不数成几处不一致
    assert main(["requirements", str(work)]) == 1
    out = capsys.readouterr().out
    assert "  图上找不到功能线「退款」（还没有实现，或者名字对不上、对上不止一条）；设计的 " in out and "处不一致" not in out
    f.write_text("乱写", encoding="utf-8")
    assert main(["requirements", str(work)]) == 2
    assert main(["requirements", str(tmp_path)]) == 0 and "里没有需求" in capsys.readouterr().out


def test_design_graph_has_the_same_shape_as_an_analysed_graph(shipped):
    """设计的流程变成的图：节点、连线的类型和代码的图一样（平台的画布直接画），能写成 JSON 再读回来。"""
    from pystepmap.model import from_json, to_json

    bad = rq.parse_requirement("# 坏的\n- 编号：R002\n- 状态：草稿\n## 设计的流程\n```flow\n乱写\n```\n", "x.md")
    g = rq.design_graph([shipped, bad])
    assert list(g.functions) == [".pystepmap/requirements/R001-账户转账.md::R001"] and len(g.lines) == 1
    line = g.lines[0]
    assert (line.title, line.group, line.sid) == ("账户转账", "需求", "line:.pystepmap/requirements/R001-账户转账.md::R001")
    fg = g.functions[line.id]
    assert [n.type for n in fg.nodes[:4]] == ["start", "step", "cond", "end"] and fg.nodes[1].sid == "step:L1"
    assert sorted({e.kind for e in fg.edges}) == ["next", "no", "raise", "yes"]
    [fail] = [e for e in fg.edges if e.kind == "raise"]
    by_id = {n.id: n for n in fg.nodes}
    assert (by_id[fail.src].title, by_id[fail.dst].title) == ("执行转账", "解冻资金")
    assert [(n.title, n.end_kind) for n in fg.nodes if n.type == "end"][-1] == ("转账完成", "ok")
    assert not [e for e in fg.edges if by_id[e.src].type == "end"]  # 结局没有出边
    assert to_json(from_json(to_json(g))) == to_json(g)
    loop = rq.design_graph([rq.parse_requirement(
        "# 循环\n- 编号：R003\n- 状态：草稿\n## 设计的流程\n```flow\n循环：每一项\n  步骤：处理\n步骤：收尾\n```\n", "y.md")])
    fg = next(iter(loop.functions.values()))
    assert [(e.kind) for e in fg.edges] == ["next", "loop_each", "loop_back", "loop_exit", "next"]
    assert (fg.nodes[-1].title, fg.nodes[-1].sid) == (rq.NO_END_TITLE, "end:implicit")


# ---------- 需求写在 OpenSpec 的文件里 ----------

def openspec_project(tmp_path, shipped, design: str) -> Path:
    root = tmp_path / "transfer"
    shutil.copytree(TRANSFER, root)
    change = root / "openspec" / "changes" / "add-review"
    change.mkdir(parents=True)
    (change / "design.md").write_text(design.replace("FLOW", shipped.flow_text), encoding="utf-8")
    return root


def test_flow_blocks_in_openspec_files_are_requirements(tmp_path, shipped, graph):
    """进行中的变更、当前规格、归档的变更里的 flow 代码块各是一条需求（归档的排在最后，状态"已归档"）；别的代码块不读。"""
    root = openspec_project(tmp_path, shipped, """# Design

## 流程变化

~~~python
# 不是标题
```flow 也不是流程
~~~

```flow
功能线：账户转账
FLOW
```

## 另一条线

```flow
功能线: 还没有的功能线
步骤：没写完
```
""")
    block = f"```flow\n功能线：账户转账\n{shipped.flow_text}\n```\n"
    for rel in ("openspec/changes/archive/2026-01-01-old/design.md", "openspec/specs/transfer/spec.md"):
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_text("# 转账\n\n" + block, encoding="utf-8")
    found = rq.load_requirements(root)
    assert [(r.id, r.source, r.status, r.line) for r in found] == [
        ("R001", "pystepmap", "已确认", "账户转账"),
        ("add-review", "openspec", "已确认", "账户转账"),
        ("add-review#2", "openspec", "已确认", "还没有的功能线"),
        ("specs/transfer", "openspec", "已实现", "账户转账"),
        ("archive/2026-01-01-old", "openspec", "已归档", "账户转账")]
    change, other, spec, old = found[1:]
    assert old.archived and old.title == "2026-01-01-old：转账" and not any(r.archived for r in found[:-1])
    assert (change.title, change.file, change.errors) == ("add-review：流程变化", "openspec/changes/add-review/design.md", [])
    assert change.flow_text == shipped.flow_text and rq.compare(change, graph).matches and rq.compare(spec, graph).matches
    assert spec.title == "specs/transfer：转账" and other.title == "add-review：另一条线"
    assert [d.kind for d in rq.compare(other, graph).differences] == ["no_line", "missing"]
    assert len({f"{r.file}::{r.id}" for r in found}) == 5  # 界面拿它当键


def test_an_openspec_flow_block_must_name_its_line_and_is_never_written(tmp_path, shipped):
    root = openspec_project(tmp_path, shipped, "```flow\nFLOW\n```\n")
    [_, r] = rq.load_requirements(root)
    assert (r.id, r.title) == ("add-review", "add-review") and "功能线" in r.errors[0]
    before = (root / r.file).read_text(encoding="utf-8")
    with pytest.raises(rq.RequirementError, match="OpenSpec"):
        rq.save_requirement(root, title="改", line="账户转账", description="", flow="步骤：参数校验", id="add-review")
    saved = rq.save_requirement(root, title="新的", line="账户转账", description="", flow="步骤：参数校验")
    assert saved.id == "R002" and (root / r.file).read_text(encoding="utf-8") == before
    assert rq.uses_openspec(root) and not rq.uses_openspec(TRANSFER) and rq.load_openspec(TRANSFER) == []


def test_when_nothing_pairs_the_comparison_says_titles_are_the_likely_cause(shipped, graph):
    r = rq.parse_requirement(rq.render_requirement(rq.Requirement(
        "R009", "措辞不一样", "已确认", "账户转账", "", "步骤：检查参数\n结局（成功）：转完了")))
    cmp = rq.compare(r, graph)
    assert {d.title for d in cmp.differences if d.kind == "missing"} == {"检查参数", "转完了"}
    assert "一个都没配上" in cmp.notes[0] and "get_flow" in cmp.notes[0]
    assert not any("一个都没配上" in n for n in rq.compare(variant(shipped, "步骤：参数校验", "步骤：检查参数"), graph).notes)


REFUND = '''\
from pystepkit import condition, outcome, step, workflow


@step(title="查询订单")
def load_order(order_id): ...


@step(title="提交人工审核")
def submit_review(order): ...


@step(title="原路退款")
def do_refund(order): ...


@condition(title="金额超过自动退款上限？")
def over_limit(order): ...


@condition(title="退款成功？")
def refund_ok(result): ...


@workflow(title="订单退款")
def refund(order_id):
    order = load_order(order_id)
    if over_limit(order):
        submit_review(order)
        return outcome("skipped", title="已转人工审核")
    result = do_refund(order)
    if refund_ok(result):
        return outcome("ok", title="退款完成")
    return outcome("failed", title="退款失败")
'''
REFUND_FLOW = """\
步骤：查询订单
判断：金额超过自动退款上限？
  是：
    步骤：提交人工审核
    结局（跳过）：已转人工审核
步骤：原路退款
判断：退款成功？
  否：
    结局（失败）：退款失败
结局（成功）：退款完成
"""


def test_a_missing_condition_does_not_blame_the_condition_above_it(tmp_path):
    """设计里在一个判断的分支下新加一个还没实现的判断：只报这个新判断没有实现，经过它的路不比走向——实现里没有它，
    它的两个分支分不开，硬比会把走不通算到它上面那个位置没错的判断头上。"""
    (tmp_path / "refund.py").write_text(REFUND, encoding="utf-8")
    graph = analyze_project(tmp_path)

    def cmp_of(flow: str) -> rq.Comparison:
        r = rq.parse_requirement(rq.render_requirement(rq.Requirement("R001", "退款", "已确认", "订单退款", "", flow)))
        assert r.errors == []
        return rq.compare(r, graph)

    same = cmp_of(REFUND_FLOW)
    assert same.matches and same.notes == []
    old = "    步骤：提交人工审核\n    结局（跳过）：已转人工审核\n"
    for new in ("    判断：VIP 用户？\n      否：\n        步骤：提交人工审核\n        结局（跳过）：已转人工审核\n",  # VIP 落回主线
                "    判断：VIP 用户？\n      否：\n        结局（失败）：金额过大，已拒绝\n"):  # 两个分支都和实现不一样
        vip = cmp_of(REFUND_FLOW.replace(old, new))
        assert "order" not in {k for k, _ in kinds(vip)}
        assert ("missing", "VIP 用户？") in kinds(vip)
        assert vip.notes == ["设计里的判断「VIP 用户？」在实现里没有，经过它的路走向没有比（实现了这个判断再对照）"]
    # 不经过这个判断的路照样比：设计里"退款成功？"的"是"写成了失败，报得出来
    wrong = cmp_of(REFUND_FLOW.replace(old, new).replace("  否：\n    结局（失败）：退款失败", "  是：\n    结局（失败）：退款失败"))
    assert "order" in {k for k, _ in kinds(wrong)} and ("missing", "VIP 用户？") in kinds(wrong)


def test_openspec_archived_changes_are_listed_last_and_blocks_under_one_heading_get_their_own_titles(tmp_path, capsys):
    """已归档的变更（openspec/changes/archive/）里的 flow 代码块也读：编号 archive/<变更名>、状态"已归档"、排在最后，
    和现在的代码不一致不影响 requirements 命令的退出码。同一个小节里放了几块时，标题用各自的功能线而不是重名的小节标题。"""
    (tmp_path / "refund.py").write_text(REFUND, encoding="utf-8")
    block = "```flow\n功能线：{line}\n" + REFUND_FLOW + "```\n"
    doing = tmp_path / "openspec/changes/add-vip"
    doing.mkdir(parents=True)
    (doing / "design.md").write_text(
        "## D1 设计的流程\n\n" + block.format(line="订单退款") + "\n" + block.format(line="订单退款") + "\n## D2 另一节\n\n"
        + block.format(line="订单退款"), encoding="utf-8")
    old = tmp_path / "openspec/changes/archive/2026-11-01-add-refund"
    old.mkdir(parents=True)
    (old / "design.md").write_text("## 流程\n\n" + block.format(line="早就改名的流程"), encoding="utf-8")
    found = rq.load_requirements(tmp_path)
    assert [(r.id, r.title, r.status, r.archived) for r in found] == [
        ("add-vip", "add-vip：订单退款", "已确认", False),
        ("add-vip#2", "add-vip：订单退款", "已确认", False),
        ("add-vip#3", "add-vip：D2 另一节", "已确认", False),
        ("archive/2026-11-01-add-refund", "2026-11-01-add-refund：流程", "已归档", True)]
    cmp = rq.compare(found[-1], analyze_project(tmp_path))
    assert not cmp.matches and rq.reachable_status(found[-1], cmp) == "已归档"
    assert main(["requirements", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[已归档]" in out and "已归档的变更：下面的不一致不影响退出码" in out


NO_RETURN = '''
from pystepkit import workflow, step, condition, outcome

@step(title="查 Key")
def load_key(key_id): return {}

@condition(title="Key 存在？")
def key_exists(key): return bool(key)

@step(title="删除 Key")
def remove_key(key_id): ...

@workflow(title="删除 Key")
def delete_key(key_id):
    key = load_key(key_id)
    if key_exists(key):
        remove_key(key_id)
    else:
        return outcome("failed", title="Key 不存在")
'''
NO_RETURN_FLOW = "步骤：查 Key\n判断：Key 存在？\n  否：\n    结局（失败）：Key 不存在\n步骤：删除 Key\n"


def test_a_function_without_return_matches_a_designed_fall_off_outcome(tmp_path):
    """没有 return 的入口（204 的删除接口）：成功的结局是隐含的"执行到函数末尾"。设计里照这个标题写就对得上，走向也比；
    设计里不写它不算实现多出来；写成别的标题时提示怎么写。"""
    (tmp_path / "keys.py").write_text(NO_RETURN, encoding="utf-8")
    graph = analyze_project(tmp_path)

    def cmp_of(flow: str) -> rq.Comparison:
        r = rq.parse_requirement(rq.render_requirement(rq.Requirement("R001", "删除", "已确认", "删除 Key", "", flow)))
        assert r.errors == []
        return rq.compare(r, graph)

    ok = cmp_of(NO_RETURN_FLOW + "结局（成功）：执行到函数末尾")
    assert ok.matches and ok.notes == [] and ok.outcomes[-1] == ("执行到函数末尾", "ok", "end:implicit")
    assert cmp_of(NO_RETURN_FLOW + "结局（跳过）：执行到函数末尾").differences[0].kind == "kind"
    # 走向照样比：设计里把删除放到了"否"那一支，到不了函数末尾
    wrong = cmp_of("步骤：查 Key\n判断：Key 存在？\n  否：\n    步骤：删除 Key\n    结局（成功）：执行到函数末尾\n结局（失败）：Key 不存在")
    assert "order" in {k for k, _ in kinds(wrong)}
    # 不写成功结局：隐含的结局不算实现里多出来的（原来就是这样）
    assert all(d.kind != "extra" for d in cmp_of(NO_RETURN_FLOW).differences)
    named = cmp_of(NO_RETURN_FLOW + "结局（成功）：已删除")
    assert kinds(named) == [("missing", "已删除")]
    assert "执行到函数末尾" in named.differences[0].text and "outcome" in named.differences[0].text
