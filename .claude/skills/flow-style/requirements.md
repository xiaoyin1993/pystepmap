# 接到需求时的顺序（可以配合 superpowers、OpenSpec，也可以单独用）

`SKILL.md` 的补充：改带标记的流程代码之前和之后各做什么。需求存在项目里的 `.pystepmap/requirements/`，一条一个文件，
主体是一份**设计的流程**；实现后工具把它和代码的流程对照。没有配置 pystepmap MCP 时，直接读写这些文件，用
`pystepmap requirements <项目目录>` 看对照结果。

1. **先看现状**：`list_requirements` 看有没有对应的需求；`list_flows` 找到相关的功能线，`get_flow` 读它现在的流程，
   `find_nodes` 确认要改的代码在哪条线的哪个节点。
2. **先设计，再动代码**：把改完之后的流程写成一条需求，用 `save_requirement` 保存，状态"草稿"。流程的写法：

   ```
   步骤：参数校验
   判断：需要人工复核？
     是：
       步骤：提交人工复核
       结局（跳过）：已提交人工复核
   步骤：执行转账
     失败时：
       步骤：解冻资金
       结局（失败）：转账失败，资金已解冻
   结局（成功）：转账完成
   ```

   每行一个节点，缩进表示从属。标题就是之后代码里的中文标题：步骤是 `@step` 的 `title`，判断是 `@condition` 的
   `title`，结局是 `outcome` 的 `title`——**设计里写什么，代码里的 title 就写什么**，对照是按标题配对的。
   入口函数成功的那条路不写 `return` 时（没有返回内容的接口，比如 204 的删除），图上成功的结局是隐含的"执行到函数末尾"：
   设计里写 `结局（成功）：执行到函数末尾`（想要自己的标题，就在代码末尾 `return outcome("ok", title="……")`）。
   把设计给用户看：`save_requirement` / `get_requirement` 的结果里有这份设计**画成图**的平台地址，把地址给用户（缩进文本
   用户不好读；平台没在运行时请用户启动 `pystepmap serve`，在项目的"需求"里选这一条）。用户确认后用同一个编号再保存
   一次，状态改成"已确认"。
3. **按本 skill 的规则实现**。任务清单里必须有两项："`pystepmap check` 零违规"和"重新分析，对照需求"。
4. **实现后对照**：调用 `analyze_project`，再 `get_requirement`。把它列出的差异（设计里有实现里没有的、实现里多出来
   的、结局分类不一样的、走向不一致的）逐条告诉用户：是代码要改，还是设计要改，由用户定。没有差异时把状态保存成
   "已实现"；设计的每个结局都有测试走到（`get_requirement` 会列出来）时保存成"已完成"。不要只说"做完了"。

配合 superpowers 时，第 2 步的设计就是计划里的"流程变化"。两者都不用时，上面四步照做。

## 项目用 OpenSpec 时（根目录有 `openspec/`）

需求、变更、任务照 OpenSpec 的流程走（propose → apply → archive），**不要用 `save_requirement`**，也不往 `.pystepmap/requirements/`
里写——同一条流程只写一份。上面四步变成：

1. 先看现状不变（`list_flows`、`get_flow`、`find_nodes`）。
2. 设计写在 OpenSpec 变更的 `design.md` 里：一个围栏是 ```` ```flow ```` 的代码块，块里第一行 `功能线：<功能线的标题>`，下面
   就是上面那种流程文本。proposal 的 What Changes 里照旧单列"流程变化"。写好后 `get_requirement`（编号就是变更名）取这份
   设计画成图的平台地址给用户看。用户确认变更就是确认这份设计。
3. 实现；任务清单里除了"`pystepmap check` 零违规"和"重新分析"，再加一项"对照设计的流程"。
4. `analyze_project` 之后 `get_requirement`，编号就是变更名（一个变更里有几个代码块时第二个起是 `变更名#2`）；没有配置
   MCP 时用 `pystepmap requirements <项目目录>`。差异逐条告诉用户。归档后 design.md 跟着变更搬进
   `openspec/changes/archive/`，里面的流程照样读得出来，列在"已归档"里（当时的设计：之后代码再改、和它不一致不算问题）。

把 `kit/openspec/flow-rules.yaml` 里的规则加进项目的 `openspec/config.yaml`，OpenSpec 写 proposal / design / tasks 时就会带上这几条。
