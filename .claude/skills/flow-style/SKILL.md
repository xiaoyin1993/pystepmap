---
name: flow-style
description: 在本仓库（pystepmap）里新增或修改带 pystepkit 标记的流程代码时使用：写或改编排函数（@workflow 标的函数、@workflow 标的类里的 run 方法、标了 @workflow 的 FastAPI 接口函数）、@step / @node 步骤、@condition 判断函数，或者编辑后的钩子、提交前检查退回了 SM001–SM004 违规时。内容：五个标记的用法、四条约束规则各自禁止和允许什么、检查器查不出来的写法、怎么拆步骤、改逻辑时怎么改、改完必须跑的检查。
---

# flow-style：带 pystepkit 标记的流程代码怎么写

**一句话：编排函数只写走向（先后调用哪些步骤、按哪个判断分支、以什么结局结束），所有干活的事都放进步骤。**
日志、计数、读写数据库、网络请求、读文件、取时间、读配置、生成编号，都是"干活"。

为什么：pystepmap 把编排函数画成流程图，图上的每个节点是一次步骤调用或一次判断。编排函数里直接干的活在图上
看不见；会重放工作流的引擎（DBOS 等）还会重放编排函数体，里面的日志、写库、请求会重复发生，只有步骤的结果被记录。

谁来判定：`pystepmap check`（SM001–SM004，代码在 `core/src/pystepmap/lint.py`）。
你用 Edit / Write 改完一个 `.py` 文件后，钩子 `kit/hooks/check_edited_file.py` 只检查这个文件，有违规或解析不了就以
退出码 2 退回给你；`git commit` 时 `kit/hooks/pre-commit` 按暂存区的内容检查整个仓库，有违规就拒绝提交。检查器只认得
出一部分违规，通过检查不等于写对了（见"检查器查不出来的写法"）。完整的示例是 `examples/transfer/transfer_workflow.py`。

## 检查什么、不检查什么

- 只检查带 pystepkit 标记的函数。`@workflow` 标的函数、`@workflow` 标的类的编排方法（默认 `run`）按**编排函数**
  检查（SM001、SM002、SM004）；`@step` / `@node` 标的函数按**步骤**检查，只查 SM003。`@condition` 判断函数和
  没有标记的代码不检查。
- 标记必须从 pystepkit 导入：`from pystepkit import workflow, step, node, condition, outcome`，或 `import pystepkit as fk`
  后写 `@fk.step`。名字相同但来自别处的装饰器（Temporal 的 `workflow`、Metaflow 的 `step`、项目自己的）不算标记。
- 默认排除的目录（`tests`、`.venv`、`build`、`dist` 等）不检查。
- 控制结构不受限制（`if`、`for`、`while`、`try`、`match`、`return`、`raise`、推导式、f-string、下标、属性访问）；
  规则限制的是"调用了什么"。

## 五个标记

| 标记 | 写在哪 | 参数 |
|---|---|---|
| `@workflow` | 函数，或类（类的编排方法默认叫 `run`） | `name`（功能线标识，默认函数名或类名）、`title`、`group`（主列表分组）、`version`、`entry`（类写法的编排方法名） |
| `@step` / `@node` | 步骤；`node` 是 `step` 的别名 | `title`；`@step` 和 `@step(title="…")` 都行 |
| `@condition` | 只返回是或否的判断函数（判断里有逻辑时才写，见 SM002 的"什么时候写判断函数"） | `title`，写成问句 |
| `outcome(kind, title=…, **字段)` | 编排函数的 `return` | `kind` 是 `"ok"`、`"skipped"`、`"failed"` 之一；原样返回一个字典 |

同步函数、异步函数、类的方法都可以标。`title` 就是图上节点显示的中文。
`title`、`group` 等参数写字符串字面量，或者模块级只赋值一次的字符串常量（`GROUP = "资金"`，本模块的或导入的都行）。
算出来的值（f-string、函数调用、拼接）工具读不出来，会当作没写——分析结果里会指出是哪一行。

函数写法：

<!-- ok -->
```python
from pystepkit import condition, node, outcome, step, workflow

@step(title="参数校验")
async def validate(req) -> dict:
    return {"ok": req.amount > 0, "reason": "" if req.amount > 0 else "金额必须大于 0"}

@condition(title="参数不合法？")
def is_invalid(check: dict) -> bool:
    return not check["ok"]

@node(title="执行转账")  # node 是 step 的别名
async def execute_transfer(req) -> dict:
    return {"receipt_no": f"TR-{req.request_id}"}

@workflow(title="账户转账", group="资金")
async def transfer(req):
    check = await validate(req)
    if is_invalid(check):
        return outcome("failed", title="参数不合法，已拒绝", reason=check["reason"])
    receipt = await execute_transfer(req)
    return outcome("ok", title="转账完成", receipt_no=receipt["receipt_no"])
```

类写法（节选自示例；步骤和判断是类的方法，编排方法里用 `self.方法(...)` 调用它们）。完整的写法——冻结资金、
`try` 里执行转账失败后解冻、逐条记账的循环、三种结局——看 `examples/transfer/transfer_workflow.py`：

<!-- ok -->
```python
from pystepkit import condition, node, outcome, workflow

@workflow(name="TransferWorkflow", title="账户转账", version="1.0")
class TransferWorkflow:
    async def run(self, req):  # 编排方法：默认叫 run，可以用 @workflow(entry="方法名") 换
        balance = await self.load_balance(req)
        if self.lacks_balance(req, balance):
            return outcome("failed", title="余额不足，已拒绝")
        return outcome("ok", title="转账完成")

    @node(title="查询可用余额")
    async def load_balance(self, req) -> dict: ...

    @condition(title="余额不足？")
    def lacks_balance(self, req, balance: dict) -> bool:
        return balance["available"] < req.amount
```

## 四条规则

每组代码都是完整的文件，`# ← SMxxx` 标出检查器报违规的行；测试 `kit/tests/test_skill.py` 会把每个代码块放进
临时项目跑检查，确认违规写法恰好报这些行、改法零违规。

### SM001 编排函数只能调用步骤

报：`SM001 编排函数只能调用步骤：X(…) 不是步骤`。编排函数体里的每个调用必须是下面之一：

1. 步骤、判断函数、别的工作流（带 pystepkit 标记的；同模块的、导入的、类写法里的 `self.方法(...)` 都认）；
2. `outcome(...)`；
3. 构造对象：名字大写开头的调用（`Decimal("0.01")`、`TransferRequest(...)`、`raise TransferError("…")`），或本模块
   里定义的类。检查器只看首字母，所以大写开头的工厂、客户端、会话（`httpx.AsyncClient()`、`SessionLocal()`）
   也不报——它们照样不该出现在编排函数里；
4. Python 内置函数，直接按名字调用：`len`、`str`、`min`、`range`、`isinstance`、`sorted` 等。所有内置函数都不报，
   包括有副作用的 `print`、`open`、`input`、`exec`、`eval`——这几个照样不该出现在编排函数里；
5. 在局部变量或参数上直接调用方法，且**不 `await`**、不是项目里的函数：`entries.append(e)`、`req.memo.strip()`、
   `(check or {}).get("reason")`；
6. 方法链：从步骤结果开始的（`(await validate(req)).get("reason")`、`r.get("reason").strip()`，`r` 来自步骤），或链上
   全是取值方法的（`get`、`pop`、`items`、`keys`、`values`、`copy`、`strip`、`split`、`splitlines`、`lower`、`upper`、
   `startswith`、`endswith`、`replace`），如 `req.memo.strip().lower()`；
7. 派发任务（taskiq 的 `任务.kiq(...)`）和框架编排原语（项目用 DBOS 时白名单上的 `DBOS.sleep` 等，见 [dbos.md](dbos.md)）；
8. `asyncio.to_thread(f, …)` 按调用 `f(…)` 判断：`f` 是步骤就放行，是普通函数照报。在线程里跑同步步骤就用它
   （`asyncio.get_running_loop()` 本身报 SM001，所以别在编排函数里取 loop 再 `run_in_executor`）。

其余一律报：日志（`logger.info`）、计数、模块函数（`time.time()`、`json.dumps(...)`、`uuid.uuid4()`）、没有标记的
项目函数（`fee_of(x)`、`self._helper()`）、`await` 局部对象或参数上的方法（`await client.get(url)`）。

<!-- violates: SM001 -->
```python
import asyncio
import logging
import time
from pystepkit import outcome, step, workflow

logger = logging.getLogger(__name__)

def fee_of(amount):
    return min(amount * 0.001, 25)

@step(title="执行转账")
async def execute_transfer(req, fee):
    return {"receipt_no": f"TR-{req.request_id}", "fee": fee}

@workflow(title="跨币种转账")
async def transfer(req, client):
    started = time.time()  # ← SM001
    fee = await asyncio.to_thread(fee_of, req.amount)  # ← SM001
    quote = await client.get(f"/quotes/{req.currency}")  # ← SM001
    receipt = await execute_transfer(req, fee)
    logger.info("转账完成 %s", receipt["receipt_no"])  # ← SM001
    return outcome("ok", title="转账完成", receipt=receipt, started=started, quote=quote)
```

改法：取时间、算手续费、发请求、写日志都挪进步骤。

<!-- ok -->
```python
import logging
import time
from pystepkit import outcome, step, workflow

logger = logging.getLogger(__name__)

def fee_of(amount):
    return min(amount * 0.001, 25)

@step(title="查询汇率报价")
async def fetch_quote(client, currency):
    return await client.get(f"/quotes/{currency}")

@step(title="执行转账")
async def execute_transfer(req, quote):
    receipt = {"receipt_no": f"TR-{req.request_id}", "fee": fee_of(req.amount), "at": time.time()}
    logger.info("转账完成 %s", receipt["receipt_no"])
    return receipt

@workflow(title="跨币种转账")
async def transfer(req, client):
    quote = await fetch_quote(client, req.currency)
    receipt = await execute_transfer(req, quote)
    return outcome("ok", title="转账完成", receipt_no=receipt["receipt_no"])
```

允许的写法汇总（零违规）：

<!-- ok -->
```python
import asyncio
from decimal import Decimal
from pystepkit import outcome, step, workflow

@step(title="参数校验")
def validate(req) -> dict:
    return {"ok": True, "reason": ""}

@step(title="生成记账分录")
async def build_entries(req) -> list:
    return [{"account": req.from_account, "amount": req.amount}]

@workflow(title="账户转账")
async def transfer(req):
    check = await asyncio.to_thread(validate, req)  # 同步步骤放进线程：按被执行的步骤判断
    reason = (await asyncio.to_thread(validate, req)).get("reason")  # 步骤结果上直接取值
    first = check.get("reason").strip()  # 方法链从步骤结果开始
    memo = req.memo.strip().lower()  # 参数上全是取值方法的链
    entries = await build_entries(req)
    amounts = [e["amount"] for e in entries]  # 推导式
    notes = []
    notes.append(f"共 {len(entries)} 笔")  # 局部变量上的方法、内置函数、f-string
    total = Decimal(0) + sum(amounts)  # 构造对象（大写开头）
    if total > 50000:  # 条件里只有比较
        raise ValueError(str(reason))
    return outcome("ok", title="转账完成", first=first, memo=memo, notes=notes)
```

### SM002 判断条件里不能做计算

报：`SM002 判断条件里不能做计算：X(…)，改为引用步骤结果或判断函数`。

`if` / `elif` / `while` 的条件里，调用只能是：步骤或判断函数（可以 `await`）、内置函数（`if len(entries) > 3:`）、
局部变量或参数上的方法（同 SM001 第 5、6 条），以及项目用 DBOS 时白名单上的 DBOS 原语（`if DBOS.patch("…"):` 是
正当写法；白名单上的其他原语写在条件里也不报）。条件里调用普通函数、构造对象、`outcome`、taskiq 的 `.kiq(...)`：
报 SM002。不含调用的比较照常写：`if check["ok"]:`、`if req.amount > SINGLE_LIMIT:`。

为什么：判断决定走向。条件里算出来的值不在图上、也不被记录，重放时可能算出不同的结果。写成 `@condition`，菱形
就有中文标题——但判断函数本身也不被记录，它若自己去读会变的状态（时间、配置、数据库），重放时照样可能走另一条路。
所以**会变的数据由步骤读出来（步骤的结果会被记录），判断函数只判断传给它的参数**。

<!-- violates: SM002 -->
```python
from pystepkit import outcome, step, workflow
from app.settings import feature_enabled

@step(title="提交人工复核")
async def submit_for_review(req): ...

@workflow(title="账户转账")
async def transfer(req):
    if feature_enabled("manual_review") and req.amount > 20000:  # ← SM002
        await submit_for_review(req)
        return outcome("skipped", title="已转人工复核")
    return outcome("ok", title="转账完成")
```

改法：步骤读开关，判断函数只判断步骤的结果。

<!-- ok -->
```python
from pystepkit import condition, outcome, step, workflow
from app.settings import feature_enabled

@step(title="读取复核开关")
async def load_review_switch() -> dict:
    return {"enabled": feature_enabled("manual_review")}

@condition(title="需要人工复核？")
def needs_review(req, switch: dict) -> bool:
    return switch["enabled"] and req.amount > 20000

@step(title="提交人工复核")
async def submit_for_review(req): ...

@workflow(title="账户转账")
async def transfer(req):
    switch = await load_review_switch()
    if needs_review(req, switch):
        await submit_for_review(req)
        return outcome("skipped", title="已转人工复核")
    return outcome("ok", title="转账完成")
```

判断函数的写法：

- **不含调用的判断直接写在 `if` 里，不要为它包一个函数**：`if job is None or row is None:`、`if not ok:`、`if running:` 都不违规，
  图上照样是一个菱形（显示条件的原文）。`def already_running(running): return running` 这样的包装只让读代码的人多跳一层。
  判断里有真正的逻辑（几项条件的组合、要单独测试、几处共用），或者它要有中文标题（写进了需求的流程）时，才写成 `@condition`。
- 参数是步骤的结果或请求本身，函数体只做判断（示例里"查询可用余额"把数据查出来，"余额不足？"只判断它的结果）。
  检查器不查判断函数内部，这一条靠自觉。
- 在条件里**单独**调用它（可以 `await`）。`if not cond(...)`、`if a(x) and b(x)` 不违规，但菱形只在条件恰好是对判断
  函数的一次调用时才显示它的标题，否则显示代码原文；要反过来就写反向的判断函数（`lacks_balance`，不是 `not has_balance`）。

### SM003 步骤不调用其他步骤

报：`SM003 步骤 A 里不能调用另一个步骤 B`。`asyncio.to_thread(另一个步骤, …)` 也算。步骤里可以调用普通函数、
判断函数，也可以递归调用自己。

为什么：步骤是图上的一个节点，步骤里再调用的步骤藏进了节点里，图上看不到，也不会被单独记录。要先后做两件事，
就在编排函数里先后调用两个步骤；要共用代码，就抽成普通函数让两个步骤各自调用（示例里的 `send_message`）。

<!-- violates: SM003 -->
```python
from pystepkit import outcome, step, workflow

@step(title="发通知")
async def notify(account: str, text: str) -> None:
    print(account, text)

@step(title="拒绝并说明原因")
async def reject(req, reason: str) -> None:
    req.status = "rejected"
    await notify(req.from_account, f"转账被拒绝：{reason}")  # ← SM003

@workflow(title="账户转账")
async def transfer(req):
    await reject(req, "余额不足")
    return outcome("failed", title="余额不足，已拒绝")
```

改法：两个步骤都调用同一个普通函数（类写法里 `self.另一个步骤(...)` 同样报 SM003，改法相同）。

<!-- ok -->
```python
from pystepkit import outcome, step, workflow

def send_message(account: str, text: str) -> None:
    print(account, text)

@step(title="发通知")
async def notify(account: str, text: str) -> None:
    send_message(account, text)

@step(title="拒绝并说明原因")
async def reject(req, reason: str) -> None:
    req.status = "rejected"
    send_message(req.from_account, f"转账被拒绝：{reason}")

@workflow(title="账户转账")
async def transfer(req):
    await reject(req, "余额不足")
    return outcome("failed", title="余额不足，已拒绝")
```

### SM004 编排函数里不能定义内部函数

报：`SM004 编排函数里不能定义内部函数`。编排函数体里的 `def`、`async def`、`lambda` 都算，包括
`sorted(xs, key=lambda e: …)` 里的 lambda。为什么：内部函数被当作数据传给别处执行，里面的逻辑在图上看不到。

<!-- violates: SM004 -->
```python
from pystepkit import outcome, step, workflow

@step(title="生成记账分录")
async def build_entries(req) -> list:
    return [{"seq": 2, "side": "贷"}, {"seq": 1, "side": "借"}]

@step(title="记一笔账")
async def post_entry(entry: dict) -> None: ...

@workflow(title="账户转账")
async def transfer(req):
    entries = await build_entries(req)
    for entry in sorted(entries, key=lambda e: e["seq"]):  # ← SM004
        await post_entry(entry)
    return outcome("ok", title="转账完成")
```

改法：排序、过滤放进产出数据的步骤里，编排函数只做循环。

<!-- ok -->
```python
from pystepkit import outcome, step, workflow

@step(title="生成记账分录")
async def build_entries(req) -> list:
    entries = [{"seq": 2, "side": "贷"}, {"seq": 1, "side": "借"}]
    return sorted(entries, key=lambda e: e["seq"])

@step(title="记一笔账")
async def post_entry(entry: dict) -> None: ...

@workflow(title="账户转账")
async def transfer(req):
    entries = await build_entries(req)
    for entry in entries:
        await post_entry(entry)
    return outcome("ok", title="转账完成")
```

## 检查器查不出来的写法

检查器宁可漏报也不误报。下面这些在编排函数里**不报，但同样不允许**——通过检查不等于写对了，照样放进步骤（另外，
判断函数内部不检查，步骤内部只查 SM003）：

- 有副作用的内置函数：`print`、`open`、`input`、`exec`、`eval`（所有内置函数都不报）；
- 大写开头的调用：工厂、客户端、会话（`httpx.AsyncClient()`、`SessionLocal()`），检查器当作构造数据；
- 局部变量、参数（包括 `self`）上**不 `await`** 的单个方法调用：`f.read()`、`session.execute(q)`、`session.commit()`、
  `self.repo.save(x)`。检查器分不出这个变量是数据还是文件、会话、客户端。

下面这个编排函数检查器一处都不报，但标了 `# ← 漏报` 的每一行都该挪进步骤：

<!-- missed: 有副作用的内置函数、大写开头的客户端和会话、局部变量上的方法 -->
```python
import httpx
from pystepkit import outcome, workflow
from app.db import SessionLocal

@workflow(title="账户转账")
async def transfer(req):
    print("开始转账", req.request_id)  # ← 漏报
    with open("/etc/transfer-limits.json") as f:  # ← 漏报
        limits = f.read()  # ← 漏报
    client = httpx.AsyncClient()  # ← 漏报
    with SessionLocal() as session:  # ← 漏报
        session.execute("update accounts set frozen = 1")  # ← 漏报
        session.commit()  # ← 漏报
    return outcome("ok", title="转账完成", limits=limits, client=client)
```

反过来，有两类误报（检查器报了，其实没有重放风险）。换写法避开，不要为此关掉检查、
去掉标记或改钩子：**纯函数辅助**（K2、K3，如 `_error_text(e)`、`_attempts(total, interval)`）照报 SM001——能用内置函数
就用（`str(e)`），只依赖常量的在模块级算成常量，其余在步骤里算；**装满 DBOS 句柄的列表上的方法链**（R3）见 [dbos.md](dbos.md)。

## 怎么拆步骤、怎么改逻辑（约定，检查器不查）

- 一个步骤做一件能用一句中文说清的事，这句话就是它的 `title`："冻结资金"、"生成记账分录"、"拒绝并说明原因"
  （说明原因是拒绝的一部分）。标题里要并列两个互不相干的动作才说得清的（"扣款并发通知"），拆成两个步骤。
- 有副作用、会变的事（日志、计数、数据库、网络、文件、时间、配置、编号）都放进步骤；步骤返回编排函数分支要用的
  数据；简单的判断直接在 `if` 里比较步骤的结果，有逻辑的判断写成标题是问句的 `@condition`，只判断传给它的参数（见 SM002）。
- 每个出口都 `return outcome(kind, title="…")`：`ok` 做成了，`skipped` 这次没做（重复提交、转人工复核），`failed`
  失败或被拒绝（接口函数当编排函数时出口不用 `outcome`，用 `return` 响应和 `raise HTTPException`，见 [routes.md](routes.md)）。会失败的步骤用 `try` / `except` 接住，在 `except` 里调用补偿步骤（示例：失败后"解冻资金"）。
- 改逻辑先改编排函数的走向：加分支就是加一个 `if`（判断里有逻辑时再加一个 `@condition`），加环节就是加一次步骤调用；改动大时整段重写编排函数，
  让它读起来就是流程图。不要在已有步骤里叠加判断来绕（把"大额要人工复核"塞进"执行转账"），分支藏进步骤，图上就看不到了。
- 不要为了过检查而去掉标记、把逻辑挪进没有标记的普通函数再从编排函数调用（那也是 SM001），或改钩子配置。
- 改带标记的流程之前，先用 pystepmap MCP 的 `find_nodes`（给文件和行号）和 `get_flow` 看清要改的位置在哪条功能线的哪个节点。
- 改完后调用 `analyze_project`，再用 `diff_flows` 把流程的变化写进给用户的回复。只想确认新写的入口成没成线、哪些线变了时，`analyze_project` 带 `detail="summary"`（每个函数一行），不必再调 `diff_flows`。没有配置 pystepmap MCP 时这两条跳过。接到需求时完整的顺序（先列流程变化给用户确认、实现、再对照）见 [requirements.md](requirements.md)。

## 改完必须做的事

1. 在仓库根目录检查改过的文件（路径相对仓库根，可以列多个）：

   ```bash
   uv run pystepmap check . --files examples/transfer/transfer_workflow.py
   ```

   必须输出"共 0 处违规"、退出码 0。路径写错或文件在排除的目录里时也输出"共 0 处违规"，但标准错误多一行
   `警告: --files 里的 … 不对应项目里的任何 .py 文件`——那就是什么都没检查。退出码 1 有违规；2 解析不了或参数写错。

2. 重新出图，看一眼这条功能线：摘要里"走不到的节点: 0"、"无出口的节点: 0"，再按连线顺序把图打印出来，核对走向
   和你想的一样（把目录和编排函数的限定名换成你改的那个）：

   ```bash
   uv run pystepmap analyze examples/transfer -o /tmp/flow.json
   python3 - /tmp/flow.json TransferWorkflow.run <<'EOF'
   import json, sys
   g = json.load(open(sys.argv[1], encoding="utf-8"))
   fn = next(f for f in g["functions"].values() if f["qualname"] == sys.argv[2])
   name = {n["id"]: f'{n["type"]} {n["title"]}' for n in fn["nodes"]}
   for e in fn["edges"]:
       print(f'{name[e["src"]]}  --{e["kind"]}-->  {name[e["dst"]]}')
   EOF
   ```

3. 钩子以退出码 2 退回信息时（以"这些是流程约束检查的违规"或"被编辑的文件解析不了"开头），按 `文件:行: 代码` 找到
   那一行照本 skill 改，下次编辑时钩子会重新检查；检查器跑不起来时钩子以退出码 1 结束，那行提示只有用户看得到、你看不到，
   所以不管钩子有没有动静，改完都自己运行第 1 步。不要绕过钩子：不改 `.claude/settings.json`，不用 `git commit --no-verify`，不去掉标记。
4. `git commit` 时提交前检查按暂存区的内容查整个仓库；被拒绝就照信息改，重新 `git add` 再提交（只改工作区不暂存，
   还会被拒绝）。

## 项目用 DBOS 时

本仓库目前不用 DBOS。项目里有 `from dbos import DBOS`、`@DBOS.workflow()` / `@DBOS.step()` 时读 [dbos.md](dbos.md)：哪些
DBOS 调用在编排函数里放行、哪些照报，以及句柄列表那类误报怎么避开。
