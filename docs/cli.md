# 命令行和记录插件

五个命令（analyze、check、serve、mcp、verify）、分析配置、入口怎么认、哪些调用是步骤，以及 pytest 记录插件。回到 [README](../README.md)。

## 五个命令

### analyze：分析项目，输出图

```bash
uv run pystepmap analyze /path/to/project --config pystepmap.json -o graph.json
```

图写进 `graph.json`，摘要打印到标准输出：启用的识别规则、源码根、排除的目录、函数数、功能线数、跳过的文件
（带原因）、目标不明 / 推测的调用（目标不明的还写出其中可能落在项目代码上的）、走不到 / 无出口的节点。不给 `-o` 时图写到标准输出、摘要写到标准错误。
图的字段（含跨两次分析配对用的稳定标识 `sid`）见 `docs/graph-schema.md`。

配置文件（JSON，全部可选）：`exclude`、`extra_exclude`、`noise_receivers`、`noise_functions`、`outcome_fields`、
`outcome_kinds`、`common_task_threshold`、`extra_rules`、`extra_entries`、`external_calls`、`fallback_entries`、`source_roots`。字段含义见 `core/src/pystepmap/config.py`；例子见
`core/tests/acceptance/*.json`。注意 `exclude` 会取代默认的排除目录（`.venv`、`node_modules`、`.claude`、`tests`……），要保留就
一起写上；只想多排除一个目录时用 `extra_exclude`（加在排除列表后面），例如 `{"extra_exclude": ["migrations"]}`。项目的导入根不是分析的目录时（`src` 布局、从仓库根分析 `backend/app`），
源码根会自动检测，也可以用 `source_roots` 指定。不是用装饰器注册的入口（命令行的子命令、按名字分发的工具方法）可以用
`extra_entries` 点名：`[{"match": "tool/cli.py::_cmd_*", "group": "命令行"}]`，对上的普通函数各成一条功能线。

#### 入口是怎么认出来的

不靠标记、不靠配置；实现在 `core/src/pystepmap/rules.py` 和 `entries.py`：

| 入口 | 认的是什么 | 类型 / 分组 |
|---|---|---|
| 接口 | FastAPI：`@app.get(...)`、`@router.post(...)`；Flask（Quart、Sanic、Bottle 同样的写法）：`@app.route(...)`、`@bp.route(...)`，标题是"方法 路径"（带 `APIRouter(prefix=)` / `Blueprint(url_prefix=)` 的前缀） | 接口，按文件名分组 |
| 后台任务、工作流 | taskiq 的 `@broker.task`；Celery 的 `@shared_task`、`@app.task`（项目导入了 celery 时），派发是 `任务.delay(...)` / `.apply_async(...)`；DBOS 的 `@DBOS.workflow()`；pystepkit 的 `@workflow` | 任务 / 工作流，"后台任务" |
| 命令行 | click / typer 的 `@x.command(...)`、`@x.callback()` | 函数，"命令行" |
| 框架来调的函数 | 顶层函数挂着项目外对象的装饰器（`@scheduler.scheduled_job(...)`、`@bot.message_handler(...)`、`@app.on_event("startup")`），项目里又没有人调用它 | 函数，"其他入口" |
| 脚本 | `if __name__ == "__main__":` 块里调用的项目内函数（`asyncio.run(main())` 也算）；它只是把一个已经认出来的入口跑起来时不算，里面没有步骤、判断、循环的小工具也不算 | 函数，"脚本入口" |
| 注释点名 | 函数的 `def`（有装饰器时是第一个装饰器）上面紧挨着的一行 `# pystepmap: 入口` 或 `# pystepmap: 入口 每日对账`（后面的文字是功能线的标题） | 函数，"其他入口" |
| 兜底 | 上面这些一个都没有时：没有人调用、也没有被当作值交出去的顶层函数各成一条线（还没有就放宽到全部模块级函数、类的公开方法）。摘要里会写"入口是推断的"。配置 `"fallback_entries": false` 关掉 | 函数，"其他入口" |

接口函数里 `return 响应, 状态码`（Flask 的写法）、状态码 400 及以上时，结局算失败，标题是"状态码 说明"（`400 标题不能为空`），
和 `raise HTTPException(...)` 一样。分析结果里另有一行"不在任何功能线上、项目里也没有别处用到的函数"（最多列 8 个；当依赖、回调、注册表的值交出去的，当装饰器用的，模块顶层调用的不算）：其中是入口的，
加上面那行注释。

**哪些调用是步骤**：解析到项目内函数的调用、派发、被 `await` 的调用；另外，没有 `await` 的调用落在项目外对象上时也是
（`core/src/pystepmap/external.py`）——从项目外、又不是纯计算的库导入的名字（`requests.post`、`subprocess.run`），用这类库
建出来的模块级对象（`db = sqlite3.connect(...)` 之后的 `db.execute`），注解是这类库里的类的参数（`db: Session` 的
`db.add`、`db.commit`），这些对象的调用结果赋给的局部变量（`cur = conn.cursor()` 的 `cur.execute`），以及方法里
`self.某属性.方法(...)`（`self.http.post`）。不算的：构造对象；纯计算的库（`json`、`re`、`datetime`……）；拼查询语句的函数
（sqlalchemy 的 `select`、`text`）；日志和打点；读数据、摆弄容器和字符串的方法（`get`、`append`、`fetchone`、`json`……，
直接落在库上的 `requests.get` 不受影响）；取请求、拼响应的框架函数（`jsonify`、`Depends`）。同步写法的代码（没有
`await`）靠这一条才有步骤：否则 `db.execute(...)` 不出节点，只包着它的 `if` 也跟着消失。配置 `"external_calls": false`
回到只认项目内函数、派发和 `await` 的行为（2026-11-19 之前的图）；升级后第一次重新分析，已有项目的图上会多出这类步骤
（在三个真实项目上步骤多了 5%～8%），流程差异里标成"新增"，看过点一次"标为已看"。

### check：检查编排约束

```bash
uv run pystepmap check /path/to/project --treat-as-marked dbos --files app/workflows/order.py
```

只检查带 pystepkit 标记的函数（`--treat-as-marked dbos` 把 DBOS 的工作流和步骤也当作已标记）：编排函数只调用
步骤，步骤之间不互相调用。接口函数、任务函数要被检查，就在同一个函数上加 `@workflow`（和路由、任务的装饰器谁在上面
都行；图里类型是工作流，"方法 路径" 照样显示）；只识别接口、任务的规则（`fastapi`、`taskiq`）放进
`--treat-as-marked` 不会多查任何函数，会报错（退出码 2）。每处违规一行 `文件:行: 代码 说明`。退出码：0 没有违规，1 有违规，2 没法完整检查
（检查范围内有解析不了的文件、参数或配置写错了）。不是项目源文件的 `.py` 路径（指向项目之外或指向非 `.py` 文件的
符号链接、FIFO 等）不读，只在标准错误里列成警告，不影响退出码。

### serve：启动本机平台

见 [平台：在浏览器里看功能线](platform.md)。

### mcp：给 Claude Code 用的 MCP 服务

见 [在 Claude Code 里使用](claude-code.md)。启动时打不开数据目录会在标准错误说明原因，退出码 2；标准输出只有协议消息。

### verify：用真实执行核对图

先带记录插件跑被分析项目的测试，再核对：

```bash
PYSTEPMAP_GRAPH=graph.json PYSTEPMAP_TRACE_OUT=trace.jsonl \
  PYTHONPATH=/path/to/pystepmap/core/src python -m pytest -p pystepmap.trace -q
uv run pystepmap verify graph.json trace.jsonl
```

报告列出真实走过、图上却没有的连线、结局和调用。退出码：0 全部在图上，1 有不符，2 核对不算数（图已过期、
记录插件出过错、记录里没有一个函数对得上图、文件读不了）。

## 记录插件

插件由 `-p pystepmap.trace` 显式加载（不会自动装进别的环境），两个环境变量都设置时才工作：

- `PYSTEPMAP_GRAPH`：`pystepmap analyze` 产出的图；
- `PYSTEPMAP_TRACE_OUT`：记录文件（JSONL，追加写；第一行是格式版本）。

被测项目的环境里不需要装 pystepmap，把 `core/src` 加进 `PYTHONPATH` 即可。插件用 `sys.monitoring`（被测项目的 Python
要 3.12 以上），只记函数 key、行号、节点上的耗时（纳秒）、离开方式和异常类型名、它调用了哪些项目内函数（`calls` 是直接
调用；`icalls` 是隔着项目以外的帧——第三方库的包装函数如 `@DBOS.step()`、`contextlib.contextmanager`、库调用的回调——的
间接调用，记给栈上最近的项目函数，只记函数 key 和行号），以及每个测试的结果（node id 去掉参数化后缀、通过 / 失败、时长、
失败时异常的类型名）；另有两种进度行（收集到几个用例、每个测试开始，平台在测试跑着的时候看进度用），不记参数、返回值、
变量内容和失败信息；pytest 汇总里有一行说明记了几个函数、写到哪里（用 pytest-xdist
时这一行在主进程里写"0 个函数"，记录是各个工作进程写的，照样完整）。事件循环调度的任务（`asyncio.gather`、`create_task`）、
别的线程里执行的函数、项目函数直接迭代的生成器记不到是谁调用的；界面在失败停点上对这些只说"没有记录到流程里的函数调用它"。
可选的 `PYSTEPMAP_TRACE_MAX_LINES` 限制一次执行最多记多少行（默认 20000）。记录格式写在 `core/src/pystepmap/trace.py` 的文件头：
格式只加不改，但阶段 6 之前的 `pystepmap verify` 读不了带测试结果行的新文件（用同一版本的插件和 `verify`）。平台跑测试时
用的是这个插件改名复制到数据目录里的 `pystepmap_trace_plugin`；同一个进程里只启用一份（项目自己也挂了 `-p pystepmap.trace`
时只记一次）。
