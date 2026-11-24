# flow-style：项目用 DBOS 时

`SKILL.md` 的补充。只在项目里有 `from dbos import DBOS`、`@DBOS.workflow()` / `@DBOS.step()` 时才相关；本仓库目前
不用 DBOS。依据是 `core/src/pystepmap/lint.py` 的 `_DBOS_PRIMITIVES`。

## 放行的 DBOS 调用

项目里有 DBOS 的工作流或步骤时，编排函数里直接调用白名单上的 DBOS 方法放行（它们由 DBOS 自己记录，重放安全）：

- 启动、入队子工作流：`start_workflow`、`enqueue_workflow`、`enqueue_workflow_with_options`；
- 把函数当步骤执行：`run_step`；
- 消息与事件：`send`、`send_bulk`、`recv`、`set_event`、`get_event`、`get_all_events`；
- 等待：`sleep`、`asyncio_wait`；
- 别的工作流的结果与状态：`retrieve_workflow`、`get_result`、`get_workflow_status`、`wait_first`；
- 本工作流的输出流：`write_stream`、`close_stream`；
- 版本守护：`patch`、`deprecate_patch`；
- 取消：`cancel_workflow`、`cancel_workflows`。

都含 `_async` 形式。在它们返回的句柄上继续调用也放行：`DBOS.start_workflow(…).get_result()`。这些原语写在 `if` /
`while` 的条件里同样不报（`if DBOS.patch("…"):` 是 DBOS 规定的写法）。

照报 SM001 的：直接读写系统库、不记成步骤的接口（`list_queues`、`retrieve_queue`、`delete_queue`、`read_stream`、
`launch`、`destroy` 等）、管理和查询接口（`list_workflows`、`resume_workflow`、`fork_workflow` 等，需要时写进步骤），
以及先取属性再调用的 `DBOS.logger.info(...)`。

只用 DBOS 装饰器、没有 pystepkit 标记的项目，检查时加 `--treat-as-marked dbos`，把 DBOS 的工作流和步骤当作已标记。

## 句柄列表的误报（R3）

`inflight.pop(0).get_result()`：`inflight` 里装的是 `DBOS.start_workflow(...)` 返回的句柄，等结果是重放安全的，但
检查器不追踪列表里装了什么，`get_result` 也不是取值方法，所以照报 SM001。写法上避开：列表里存子工作流的编号，用
`DBOS.retrieve_workflow(编号).get_result()` 等结果。

违规写法：查队列、`DBOS.logger`、句柄列表上的方法链（最后一处是误报）。

<!-- violates: SM001 -->
```python
from dbos import DBOS
from pystepkit import outcome, step, workflow

@step(title="对账一个分片")
@DBOS.step()
def reconcile_shard(shard: int) -> dict:
    return {"shard": shard, "ok": True}

@DBOS.workflow()
def reconcile_shard_workflow(shard: int) -> dict:
    return reconcile_shard(shard)

@workflow(title="日终对账")
@DBOS.workflow()
def daily_reconcile(shards: list):
    DBOS.list_queues()  # ← SM001
    DBOS.logger.info("开始对账")  # ← SM001
    inflight = []
    for shard in shards:
        inflight.append(DBOS.start_workflow(reconcile_shard_workflow, shard))
    results = []
    while inflight:
        results.append(inflight.pop(0).get_result())  # ← SM001
    return outcome("ok", title="对账完成", results=results)
```

改法：日志进步骤；列表里存子工作流的编号，用 `DBOS.retrieve_workflow(编号).get_result()` 等结果。

<!-- ok -->
```python
from dbos import DBOS
from pystepkit import outcome, step, workflow

@step(title="对账一个分片")
@DBOS.step()
def reconcile_shard(shard: int) -> dict:
    DBOS.logger.info("对账分片 %s", shard)
    return {"shard": shard, "ok": True}

@DBOS.workflow()
def reconcile_shard_workflow(shard: int) -> dict:
    return reconcile_shard(shard)

@workflow(title="日终对账")
@DBOS.workflow()
def daily_reconcile(shards: list):
    pending = []
    for shard in shards:
        pending.append(DBOS.start_workflow(reconcile_shard_workflow, shard).workflow_id)
    results = []
    while pending:
        results.append(DBOS.retrieve_workflow(pending.pop(0)).get_result())
    return outcome("ok", title="对账完成", results=results)
```
