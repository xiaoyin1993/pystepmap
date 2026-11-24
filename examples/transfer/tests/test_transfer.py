"""转账示例的测试：用标准库的 asyncio.run 驱动工作流（不依赖 pytest 插件），每个测试一个新的 FakeBank。

覆盖了除"超过单笔限额，已拒绝"之外的每个结局（故意留着，给平台上"没有测试走到的结局"做示范）。在示例目录里：

    python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from transfer_workflow import (
    FEE_INCOME_ACCOUNT,
    FakeBank,
    TransferRequest,
    TransferWorkflow,
)


def run(bank: FakeBank, req: TransferRequest) -> dict:
    return asyncio.run(TransferWorkflow(bank).run(req))


def test_transfer_succeeds():
    bank = FakeBank.sample()
    result = run(bank, TransferRequest("R1", "A001", "A002", Decimal("1500.00"), "房租"))
    assert (result["kind"], result["title"], result["receipt_no"]) == ("ok", "转账完成", "TR-R1")
    assert bank.balances["A001"] == Decimal("78498.50") and bank.balances["A002"] == Decimal("2700.00")
    assert bank.balances[FEE_INCOME_ACCOUNT] == Decimal("1.50") and bank.holds == {}
    assert len(bank.journal) == 4 and "R1" in bank.processed


def test_duplicate_request_is_skipped():
    bank = FakeBank.sample()
    req = TransferRequest("R1", "A001", "A002", Decimal("1500.00"))
    assert run(bank, req)["kind"] == "ok"
    again = run(bank, req)
    assert (again["kind"], again["title"]) == ("skipped", "重复请求，不再处理")
    assert bank.balances["A002"] == Decimal("2700.00")  # 只转了一次


@pytest.mark.parametrize(("from_account", "to_account", "amount", "reason"), [
    ("A001", "A002", Decimal("0.00"), "金额必须大于 0"),
    ("A001", "A002", Decimal("10.005"), "金额最多到分"),
    ("A001", "A001", Decimal("10.00"), "转出和转入不能是同一个账户"),
    ("A001", "C404", Decimal("10.00"), "账户 C404 不存在"),
], ids=["non-positive", "sub-cent", "same-account", "unknown-account"])
def test_invalid_request_is_rejected(from_account, to_account, amount, reason):
    bank = FakeBank.sample()
    result = run(bank, TransferRequest("R2", from_account, to_account, amount))
    assert (result["kind"], result["title"], result["reason"]) == ("failed", "参数不合法，已拒绝", reason)
    assert bank.outbox == [("A001", f"转账 R2 被拒绝：{reason}")] and bank.journal == []


def test_insufficient_balance_is_rejected():
    bank = FakeBank.sample()
    result = run(bank, TransferRequest("R4", "A002", "A001", Decimal("9000.00")))
    assert (result["kind"], result["title"]) == ("failed", "余额不足，已拒绝")
    assert bank.balances["A002"] == Decimal("1200.00") and bank.holds == {}


def test_large_transfer_goes_to_manual_review():
    bank = FakeBank.sample()
    req = TransferRequest("R5", "A001", "B001", Decimal("30000.00"))
    result = run(bank, req)
    assert (result["kind"], result["title"], result["ticket"]) == ("skipped", "已转人工复核", "RV-0001")
    assert bank.reviews == {"RV-0001": req} and bank.balances["A001"] == Decimal("80000.00")


def test_failed_transfer_unfreezes_funds():
    bank = FakeBank.sample()
    result = run(bank, TransferRequest("R6", "A001", "Z999", Decimal("100.00")))
    assert (result["kind"], result["title"]) == ("failed", "转账失败，资金已解冻")
    assert bank.balances["A001"] == Decimal("80000.00") and bank.holds == {}  # 冻结的本金和手续费都退回了
    assert bank.outbox[-1][0] == "A001" and bank.journal == []
