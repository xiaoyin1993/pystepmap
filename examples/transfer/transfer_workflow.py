"""账户转账：按 pystepkit 约束写法写的示例工作流（也是阶段 2 约束套件的验收用例）。

编排方法 run 只写流程的走向：先后调用哪些步骤、按哪些判断分支、以什么结局结束。读写账户、记日志、发通知这些
有副作用的事都放在 @node 步骤里；分支条件写成 @condition 判断函数，判断函数只看传给它的参数（请求本身、步骤的
结果），要用到账户里会变的数据，先用一个步骤查出来（"查询请求状态"、"查询可用余额"）再交给它判断。

示例不连任何真实服务：账户、冻结记录、流水、通知都存在内存里（FakeBank）。

    uv run python examples/transfer/transfer_workflow.py   # 跑几笔示例请求，看每种结局
    uv run pystepmap check examples/transfer                 # 约束检查，应为"共 0 处违规"
    uv run pystepmap analyze examples/transfer -o /tmp/transfer.json   # 出图：一条功能线"账户转账"
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from pystepkit import condition, node, outcome, workflow

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
SINGLE_LIMIT = Decimal("50000.00")  # 单笔限额：超过直接拒绝
REVIEW_THRESHOLD = Decimal("20000.00")  # 人工复核线：超过要先人工复核
FEE_RATE = Decimal("0.001")  # 手续费率千分之一
FEE_CAP = Decimal("25.00")  # 手续费封顶
FEE_INCOME_ACCOUNT = "9001-手续费收入"


class TransferError(Exception):
    """核心账务系统拒绝入账（示例里由 FakeBank 的冻结账户模拟）。"""


@dataclass
class TransferRequest:
    request_id: str  # 幂等键：同一个请求重复提交只处理一次
    from_account: str
    to_account: str
    amount: Decimal
    memo: str = ""


@dataclass
class FakeBank:
    """内存里的假数据。"""

    balances: dict[str, Decimal] = field(default_factory=dict)  # 账户可用余额
    blocked: set[str] = field(default_factory=set)  # 被冻结的账户：往里转账时核心系统拒绝入账
    holds: dict[str, tuple[str, Decimal]] = field(default_factory=dict)  # 冻结单号 -> (账户, 冻结金额)
    processed: set[str] = field(default_factory=set)  # 已经有结论的请求号
    reviews: dict[str, TransferRequest] = field(default_factory=dict)  # 复核单号 -> 待复核的请求
    journal: list[dict] = field(default_factory=list)  # 记账流水
    outbox: list[tuple[str, str]] = field(default_factory=list)  # 发出的通知：(账户, 内容)

    @classmethod
    def sample(cls) -> FakeBank:
        return cls(
            balances={"A001": Decimal("80000.00"), "A002": Decimal("1200.00"), "B001": Decimal("0.00"),
                      "Z999": Decimal("0.00"), FEE_INCOME_ACCOUNT: Decimal("0.00")},
            blocked={"Z999"},
        )


def fee_of(amount: Decimal) -> Decimal:
    """手续费：千分之一，按分四舍五入，封顶 25 元。"""
    return min((amount * FEE_RATE).quantize(CENT), FEE_CAP)


def send_message(bank: FakeBank, account: str, text: str) -> None:
    """把通知放进发件箱（真实系统里是短信或 App 推送）。步骤可以调用这样的普通函数，但不调用别的步骤。"""
    bank.outbox.append((account, text))
    logger.info("通知 %s：%s", account, text)


@workflow(name="TransferWorkflow", title="账户转账", version="1.0")
class TransferWorkflow:
    """一笔转账从受理到结束的全过程。"""

    def __init__(self, bank: FakeBank):
        self.bank = bank

    async def run(self, req: TransferRequest):
        """编排方法：只写走向。每个调用都是步骤或判断，每个出口都是 outcome。"""
        status = await self.load_request_status(req)
        if self.is_duplicate(status):
            return outcome("skipped", title="重复请求，不再处理", request_id=req.request_id)

        check = await self.validate(req)
        if self.is_invalid(check):
            await self.reject(req, check["reason"])
            return outcome("failed", title="参数不合法，已拒绝", reason=check["reason"])

        if self.exceeds_single_limit(req):
            await self.reject(req, "超过单笔限额")
            return outcome("failed", title="超过单笔限额，已拒绝", reason="超过单笔限额")

        balance = await self.load_balance(req)
        if self.lacks_balance(req, balance):
            await self.reject(req, "余额不足")
            return outcome("failed", title="余额不足，已拒绝", reason="余额不足")

        if self.needs_manual_review(req):
            ticket = await self.submit_for_review(req)
            return outcome("skipped", title="已转人工复核", ticket=ticket)

        hold_id = await self.freeze_funds(req)
        try:
            receipt = await self.execute_transfer(req, hold_id)
        except TransferError as e:
            await self.unfreeze_funds(hold_id)
            await self.notify(req, f"转账失败，冻结的资金已退回：{e}")
            return outcome("failed", title="转账失败，资金已解冻", reason=str(e))

        entries = await self.build_entries(req, receipt)
        for entry in entries:
            await self.post_entry(entry)
        await self.notify(req, f"转账成功，回单号 {receipt['receipt_no']}")
        return outcome("ok", title="转账完成", receipt_no=receipt["receipt_no"])

    # ---------- 判断：只看参数，不读会变的状态 ----------

    @condition(title="重复提交的请求？")
    def is_duplicate(self, status: dict) -> bool:
        return status["processed"]

    @condition(title="参数不合法？")
    def is_invalid(self, check: dict) -> bool:
        return not check["ok"]

    @condition(title="超过单笔限额？")
    def exceeds_single_limit(self, req: TransferRequest) -> bool:
        return req.amount > SINGLE_LIMIT

    @condition(title="余额不足？")
    def lacks_balance(self, req: TransferRequest, balance: dict) -> bool:
        return balance["available"] < req.amount + balance["fee"]

    @condition(title="需要人工复核？")
    def needs_manual_review(self, req: TransferRequest) -> bool:
        return req.amount > REVIEW_THRESHOLD

    # ---------- 步骤 ----------

    @node(title="查询请求状态")
    async def load_request_status(self, req: TransferRequest) -> dict:
        """幂等检查要用的状态：这个请求号是不是已经有了结论。"""
        return {"request_id": req.request_id, "processed": req.request_id in self.bank.processed}

    @node(title="参数校验")
    async def validate(self, req: TransferRequest) -> dict:
        """返回 {"ok": 是否合法, "reason": 不合法的原因}。"""
        if req.amount <= 0:
            return {"ok": False, "reason": "金额必须大于 0"}
        if req.amount != req.amount.quantize(CENT):
            return {"ok": False, "reason": "金额最多到分"}
        if req.from_account == req.to_account:
            return {"ok": False, "reason": "转出和转入不能是同一个账户"}
        for account in (req.from_account, req.to_account):
            if account not in self.bank.balances:
                return {"ok": False, "reason": f"账户 {account} 不存在"}
        return {"ok": True, "reason": ""}

    @node(title="查询可用余额")
    async def load_balance(self, req: TransferRequest) -> dict:
        """转出账户此刻的可用余额，以及这笔转账要收的手续费。"""
        return {"account": req.from_account, "available": self.bank.balances[req.from_account],
                "fee": fee_of(req.amount)}

    @node(title="拒绝并说明原因")
    async def reject(self, req: TransferRequest, reason: str) -> None:
        self.bank.processed.add(req.request_id)
        send_message(self.bank, req.from_account, f"转账 {req.request_id} 被拒绝：{reason}")
        logger.warning("转账被拒绝 request=%s reason=%s", req.request_id, reason)

    @node(title="提交人工复核")
    async def submit_for_review(self, req: TransferRequest) -> str:
        ticket = f"RV-{len(self.bank.reviews) + 1:04d}"
        self.bank.reviews[ticket] = req
        self.bank.processed.add(req.request_id)
        logger.info("转账待复核 request=%s ticket=%s", req.request_id, ticket)
        return ticket

    @node(title="冻结资金")
    async def freeze_funds(self, req: TransferRequest) -> str:
        """从转出账户冻结本金加手续费，返回冻结单号。"""
        total = req.amount + fee_of(req.amount)
        self.bank.balances[req.from_account] -= total
        hold_id = f"H-{req.request_id}"
        self.bank.holds[hold_id] = (req.from_account, total)
        return hold_id

    @node(title="执行转账")
    async def execute_transfer(self, req: TransferRequest, hold_id: str) -> dict:
        """把冻结的本金划给转入账户、手续费划进收入账户；核心系统拒绝入账时抛 TransferError（冻结不动）。"""
        if req.to_account in self.bank.blocked:
            raise TransferError(f"转入账户 {req.to_account} 已被冻结，不能入账")
        self.bank.holds.pop(hold_id)
        fee = fee_of(req.amount)
        self.bank.balances[req.to_account] += req.amount
        self.bank.balances[FEE_INCOME_ACCOUNT] += fee
        self.bank.processed.add(req.request_id)
        return {"receipt_no": f"TR-{req.request_id}", "amount": req.amount, "fee": fee}

    @node(title="解冻资金")
    async def unfreeze_funds(self, hold_id: str) -> None:
        account, total = self.bank.holds.pop(hold_id)
        self.bank.balances[account] += total

    @node(title="生成记账分录")
    async def build_entries(self, req: TransferRequest, receipt: dict) -> list[dict]:
        """借贷各一笔本金；有手续费时再借贷各一笔手续费。"""
        no, amount, fee = receipt["receipt_no"], receipt["amount"], receipt["fee"]
        entries = [
            {"receipt_no": no, "account": req.from_account, "side": "借", "amount": amount, "summary": req.memo or "转出"},
            {"receipt_no": no, "account": req.to_account, "side": "贷", "amount": amount, "summary": req.memo or "转入"},
        ]
        if fee > 0:
            entries += [
                {"receipt_no": no, "account": req.from_account, "side": "借", "amount": fee, "summary": "手续费"},
                {"receipt_no": no, "account": FEE_INCOME_ACCOUNT, "side": "贷", "amount": fee, "summary": "手续费"},
            ]
        return entries

    @node(title="记一笔账")
    async def post_entry(self, entry: dict) -> None:
        self.bank.journal.append(entry)
        logger.info("记账 %s %s %s %s", entry["receipt_no"], entry["account"], entry["side"], entry["amount"])

    @node(title="发通知")
    async def notify(self, req: TransferRequest, text: str) -> None:
        send_message(self.bank, req.from_account, text)


SAMPLES = [
    TransferRequest("R1", "A001", "A002", Decimal("1500.00"), "房租"),  # 转账完成
    TransferRequest("R1", "A001", "A002", Decimal("1500.00"), "房租"),  # 重复提交
    TransferRequest("R2", "A001", "A001", Decimal("10.00")),  # 参数不合法
    TransferRequest("R3", "A001", "A002", Decimal("60000.00")),  # 超过单笔限额
    TransferRequest("R4", "A002", "A001", Decimal("9000.00")),  # 余额不足
    TransferRequest("R5", "A001", "B001", Decimal("30000.00")),  # 人工复核
    TransferRequest("R6", "A001", "Z999", Decimal("100.00")),  # 转入账户被冻结：执行失败，资金解冻
]


async def _demo() -> None:
    flow = TransferWorkflow(FakeBank.sample())
    for req in SAMPLES:
        result = await flow.run(req)
        print(f"{req.request_id}  {result['kind']:<7}  {result['title']}")


if __name__ == "__main__":
    asyncio.run(_demo())
