# 转账示例：约束写法

`transfer_workflow.py` 用 pystepkit 标记的类写法实现"账户转账"：编排方法 `run` 只写流程走向（先查重复提交、
校验参数、查限额和余额、大额转人工复核，然后冻结资金、执行转账、逐条记账、发通知），每个调用都是 `@node`
步骤或 `@condition` 判断，每个出口都是 `outcome(...)`；日志、读写账户、发通知都在步骤里。判断函数只看传给它的
参数：要用到账户里会变的数据（请求是否处理过、可用余额），先由步骤查出来再交给判断。执行转账失败时在 `except`
里解冻资金。数据全在内存里（`FakeBank`），不连任何真实服务。

它既是写法的示范，也是约束套件（`.claude/skills/flow-style/SKILL.md`、`kit/hooks/`）的验收用例：仓库里的这份
必须保持零违规。

在仓库根目录：

```bash
uv run python examples/transfer/transfer_workflow.py                 # 跑七笔示例请求，每种结局各看一次
uv run pystepmap check examples/transfer                               # 约束检查：共 0 处违规
uv run pystepmap analyze examples/transfer -o /tmp/transfer.json       # 出图：功能线 1 条（账户转账），走不到 / 无出口的节点 0
```

想看检查器怎么拦：在 `run` 里随便加一行 `logger.info("x")` 再运行 `pystepmap check`，会报
`SM001 编排函数只能调用步骤：logger.info(…) 不是步骤`。看完记得改回来。

## 试一下流程差异

在一份拷贝上改（仓库里这份要保持零违规；添加了仓库里面的目录以后，也不能再添加 pystepmap 仓库本身）：

```bash
cp -r examples/transfer ~/transfer-try
```

在平台上"添加项目"填 `~/transfer-try`（或者在 Claude Code 里让它用 pystepmap MCP 的 `analyze_project` 添加）。然后在
`~/transfer-try/transfer_workflow.py` 的 `run` 里、"超过单笔限额，已拒绝"那个 `return` 后面加一个守卫：

```python
        if self.fee_too_high(req):
            return outcome("failed", title="手续费太高，已拒绝", reason="手续费太高")
```

在 `exceeds_single_limit` 方法后面加它的判断函数：

```python
    @condition(title="手续费太高？")
    def fee_too_high(self, req: TransferRequest) -> bool:
        return fee_of(req.amount) > Decimal("20.00")
```

保存后平台上方先出现"代码已变更，图可能过期"；点"重新分析"："账户转账"带"有变化"，差异栏"新增 2"，新的判断和结局画着
绿色外圈，"下一处"依次定位，看完点"标为已看"。在 Claude Code 里：`analyze_project` 之后 `diff_flows` 在"新增"下面
列出判断「手续费太高？」和结局「手续费太高，已拒绝」，带行号。再试试只改格式（加注释、把一个调用拆成几行）：重新分析后流程
没有变化，不会多标任何东西。没标为已看就再改一处流程：两处变化都还标着（对比基准只在标为已看时移动），差异栏写"相对第一次分析"；标为已看之后再改，只标新的那处，写"相对上次标为已看时"。

## 试一下运行路径

`tests/test_transfer.py` 有 6 个测试（参数化展开后 9 个用例），用 `asyncio.run` 驱动工作流、不用 pytest 插件。它们走到了
除「超过单笔限额，已拒绝」之外的每个结局——这一个是**故意留着**的，给平台上"没走到的结局"做示范。在示例目录里直接跑：

```bash
cd examples/transfer && ../../.venv/bin/python -m pytest tests -q   # 9 passed
```

在平台上（同样用一份拷贝，比如上一节的 `~/transfer-try`；测试会在项目目录里跑）：点画布上方的"运行路径"页签，右侧栏里
"填写"测试命令，写 pystepmap 仓库 `.venv` 里的解释器（示例只用标准库和 pytest），例如
`<pystepmap 仓库>/.venv/bin/python -m pytest tests -q`；点"运行测试"，第一次会先弹确认框。几秒后：

- 运行列表里一条"9 通过"，核对一行绿色"图和实际执行一致"（三项不符都是 0）；
- 点 `test_transfer_succeeds`：画布上它走过的节点有蓝条和耗时，"逐条记账"那个循环写"×N"，没走到的分支变暗；
- 取消选择、打开"没走到的结局"的"在图上标出"：只有「超过单笔限额，已拒绝」一个。
- 加上上一节的守卫（「手续费太高？」）再运行：运行前平台先自动重新分析；这次 1 个失败——3 万元的转账手续费封顶 25 元，
  超过 20 元，于是走到了「手续费太高，已拒绝」而不是转人工复核（`test_large_transfer_goes_to_manual_review` 在取 `ticket`
  时 `KeyError`）；没走到的结局变成「超过单笔限额，已拒绝」、「已转人工复核」，以及没再执行的步骤 `submit_for_review`
  里的「返回 ticket」（画布上标在调用它的那一步上）。

Claude Code 里：`uncovered_outcomes` 列出同样的结局，带文件和行号。想看失败的测试停在哪里，在拷贝里加一个会失败的测试再运行、
点开它：红圈画在异常抛出的地方。会失败的测试只加在拷贝里，仓库里这份的测试要保持全部通过。
