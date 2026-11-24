"""转账示例工作流（examples/transfer）：约束检查零违规、分析出一条功能线、代码本身能跑。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

from pystepmap.cli import main
from pystepmap.lint import check_project
from pystepmap.model import from_json

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "transfer"

# 编排方法 run 里的步骤、判断和结局，按源码顺序；标题都来自标记（@node / @condition 的 title、outcome 的 title）
RUN_NODES = [
    ("step", "查询请求状态"),
    ("cond", "重复提交的请求？"),
    ("end", "重复请求，不再处理"),
    ("step", "参数校验"),
    ("cond", "参数不合法？"),
    ("step", "拒绝并说明原因"),
    ("end", "参数不合法，已拒绝"),
    ("cond", "超过单笔限额？"),
    ("step", "拒绝并说明原因"),
    ("end", "超过单笔限额，已拒绝"),
    ("step", "查询可用余额"),
    ("cond", "余额不足？"),
    ("step", "拒绝并说明原因"),
    ("end", "余额不足，已拒绝"),
    ("cond", "需要人工复核？"),
    ("step", "提交人工复核"),
    ("end", "已转人工复核"),
    ("step", "冻结资金"),
    ("step", "执行转账"),
    ("step", "解冻资金"),
    ("step", "发通知"),
    ("end", "转账失败，资金已解冻"),
    ("step", "生成记账分录"),
    ("step", "记一笔账"),
    ("step", "发通知"),
    ("end", "转账完成"),
]


def test_example_has_no_violations(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    assert capsys.readouterr().out == "共 0 处违规\n"


def test_example_analyzes_into_one_feature_line(tmp_path, capsys):
    out = tmp_path / "g.json"
    assert main(["analyze", str(EXAMPLE), "-o", str(out)]) == 0
    summary = capsys.readouterr().out.splitlines()
    for line in ("功能线: 1", "跳过的文件: 0", "走不到的节点: 0", "无出口的节点: 0"):
        assert line in summary
    g = from_json(out.read_text(encoding="utf-8"))
    assert [(ln.title, ln.kind) for ln in g.lines] == [("账户转账", "workflow")]
    run = next(f for f in g.functions.values() if f.qualname == "TransferWorkflow.run")
    assert (run.kind, run.title, run.name, run.version, run.marked) == (
        "workflow", "账户转账", "TransferWorkflow", "1.0", True)
    assert [(n.type, n.title) for n in run.nodes if n.type in ("step", "cond", "end") and n.line] == RUN_NODES
    assert {n.type for n in run.nodes} >= {"loop", "catch"}  # 逐条记账的循环、执行转账失败后的捕获


def test_conditions_only_judge_their_inputs():
    """判断函数只看传进来的参数（步骤的结果、请求本身），不自己去读会变的状态（self.bank）：读状态的是步骤。"""
    src = (EXAMPLE / "transfer_workflow.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "TransferWorkflow")
    conditions = [f for f in cls.body if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
                  and any("condition" in ast.unparse(d) for d in f.decorator_list)]
    assert len(conditions) == 5
    for f in conditions:
        assert "self.bank" not in ast.unparse(f), f.name


def test_whole_repository_has_no_violations():
    """`uv run pystepmap check .` 在仓库根目录要保持零违规（示例在 examples/ 下，不在排除的目录里）。"""
    result = check_project(REPO)
    assert result.violations == [] and result.skipped == []


def _load_example():
    spec = importlib.util.spec_from_file_location("transfer_workflow_example", EXAMPLE / "transfer_workflow.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 需要按模块名找到自己
    try:
        spec.loader.exec_module(mod)
    finally:
        del sys.modules[spec.name]
    return mod


def test_example_runs_to_every_outcome():
    """示例是能跑的业务代码：每种结局都走得到，失败后资金解冻，成功时逐条记账。"""
    m = _load_example()
    bank = m.FakeBank.sample()
    flow = m.TransferWorkflow(bank)
    before = dict(bank.balances)

    def run(*args):
        return asyncio.run(flow.run(m.TransferRequest(*args)))

    ok = run("R1", "A001", "A002", Decimal("1500.00"), "房租")
    assert (ok["kind"], ok["title"]) == ("ok", "转账完成")
    assert len(bank.journal) == 4  # 本金借贷两笔 + 手续费借贷两笔
    assert bank.balances["A002"] == before["A002"] + Decimal("1500.00")
    assert bank.balances["A001"] == before["A001"] - Decimal("1500.00") - Decimal("1.50")

    assert run("R1", "A001", "A002", Decimal("1500.00"))["kind"] == "skipped"  # 重复提交
    assert run("R2", "A001", "A001", Decimal("10.00"))["title"] == "参数不合法，已拒绝"
    assert run("R3", "A001", "A002", Decimal("60000.00"))["title"] == "超过单笔限额，已拒绝"
    assert run("R4", "A002", "A001", Decimal("9000.00"))["title"] == "余额不足，已拒绝"
    review = run("R5", "A001", "B001", Decimal("30000.00"))
    assert (review["kind"], review["title"]) == ("skipped", "已转人工复核")

    a001 = bank.balances["A001"]
    failed = run("R6", "A001", "Z999", Decimal("100.00"))
    assert (failed["kind"], failed["title"]) == ("failed", "转账失败，资金已解冻")
    assert bank.balances["A001"] == a001 and bank.holds == {}  # 冻结的钱退回去了
