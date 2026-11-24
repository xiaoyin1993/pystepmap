# 约束套件

skill、检查器、钩子，以及装进别的项目的 `pystepmap kit install`。标记怎么写、四条约束各自禁止什么，见 skill 的
[SKILL.md](../.claude/skills/flow-style/SKILL.md)。回到 [README](../README.md)。


让 AI（和人）写的流程代码稳定遵守编排约束。分三层；本仓库自己在用，也可以装进别的项目（见下面的 `pystepmap kit install`）：

| 层 | 文件 | 作用 |
|---|---|---|
| skill | `.claude/skills/flow-style/SKILL.md`（DBOS 部分在同目录的 `dbos.md`，接口函数当编排函数在 `routes.md`） | 告诉 AI 怎么写：五个标记、SM001–SM004 各自禁止和允许什么（每条配"违规写法 → 改法"）、检查器查不出来的写法、怎么拆步骤、改逻辑时先改编排函数、改完必须跑的检查。Claude Code 在本仓库里改带 pystepkit 标记的代码时使用它。 |
| 检查器 | `pystepmap check`，接进 `kit/hooks/pre-commit` | 判定是否违规，结果确定。提交时只要暂存区里有 `.py` 改动，就把整个暂存区导出到临时目录检查（判定的是暂存的内容，不是工作区），有违规或解析不了就拒绝提交。 |
| 钩子 | `kit/hooks/check_edited_file.py`，在 `.claude/settings.json` 注册 | Claude Code 每次用 Edit / Write 改完文件后自动运行，只检查被编辑的那个文件，把违规退回给 AI。 |

启用提交前检查（每个克隆做一次，只改本仓库的设置）：

```bash
git config core.hooksPath kit/hooks
```

编辑后的钩子随 `.claude/settings.json` 生效，不用另外启用（在 Claude Code 里用 `/hooks` 可以看到它）。它在两种情况下
以退出码 2 把信息退回给 AI：被编辑的 `.py` 文件有违规（信息以"这些是流程约束检查的违规，请按 flow-style 修改后再
继续"开头，后面是 `文件:行: 代码 说明`），或这个文件解析不了。其余情况都不挡住编辑：没有违规、不是 `.py`、不在本仓库、
文件已删除、输入不是预期的 JSON 时以 0 退出，什么都不说；检查没做成（找不到 uv、检查器出错、30 秒没结束、钩子自身出错）
时以 1 退出，在标准错误留一行说明——不阻断，Claude Code 把这一行显示给用户，这样检查失效时有人知道。项目里别的文件
有问题不影响这次编辑。

两个脚本都只用标准库，兼容 macOS 自带的 `python3`；检查器默认用 `uv run pystepmap`，PATH 上没有 uv 时用仓库的
`.venv/bin/pystepmap`。测试里用环境变量 `PYSTEPMAP_HOOK_CMD`（检查器命令）、`PYSTEPMAP_HOOK_ROOT`（项目根）、
`PYSTEPMAP_HOOK_TIMEOUT`（超时秒数）替换默认值。

示例：`examples/transfer/`（类写法的"账户转账"，11 个步骤、5 个判断、循环、`try`、三种结局），是约束写法的示范，
也是约束套件的验收用例，必须保持零违规：

```bash
uv run pystepmap check examples/transfer
uv run pystepmap analyze examples/transfer -o /tmp/transfer.json
```

另附 `kit/openspec/flow-rules.yaml`：给使用 OpenSpec 的项目贴进 `openspec/config.yaml` 的规则（修改编排函数的变更
必须列出流程变化，design.md 里把改完之后的流程写成 `flow` 代码块，任务清单里必须有"检查零违规"、"重新生成流程图"和"对照
设计的流程"三项）。pystepmap 自己的开发不强制使用 OpenSpec。

## 装进别的项目：`pystepmap kit install`

想让 Claude Code 在另一个项目里也按这套约束写流程代码时：

```bash
uv run pystepmap kit install /路径/到/你的项目            # 先列出要写的文件，回答 y 才写
uv run pystepmap kit install /路径/到/你的项目 --dry-run  # 只看要写什么
uv run pystepmap kit uninstall /路径/到/你的项目          # 照清单撤销
```

它往目标项目里写这些（这是 pystepmap 唯一往别的项目写文件的功能，只在你运行这条命令时发生）：

| 写到哪里 | 是什么 |
|---|---|
| `.claude/skills/flow-style/` | skill：标记怎么用、四条约束、接到需求时的顺序（`requirements.md`）、接口函数当编排函数（`routes.md`）、示例、给 OpenSpec 用的规则片段 |
| `.claude/hooks/pystepmap_check.py` 和 `.claude/settings.json` 里的一条钩子 | 编辑 `.py` 之后自动检查这个文件，有违规退回给 AI |
| `.mcp.json` 里的 `pystepmap` | MCP 服务：查功能线、反查节点、重新分析、看流程差异、没走到的结局 |
| `.claude/pystepmap-kit.json` | 清单：写了哪些文件、原来有没有同名的 MCP 服务，撤销时用 |

- 已有的 `.claude/settings.json`、`.mcp.json` 只加自己的那一项；不是合法 JSON 时什么都不写。
- 已有同名的 skill 或脚本、内容又不一样时不覆盖（上一次自己装的原样除外，再装一次就是更新）。
- 撤销时只删装了之后没改过的文件，改过的留下并说明；`.mcp.json` 里原来的同名服务换回去。
- 加 `--no-mcp` 不碰 `.mcp.json`：pystepmap 的 MCP 已经在用户级配过时用，免得项目里再写一份重复的；项目里已有的同名
  服务是你自己的，撤销时也不动。
- 加 `--local-settings` 把钩子写进 `.claude/settings.local.json`：钩子命令里是本机 pystepmap 的绝对路径，项目有别的协作者时用它，免得入库的 settings.json 里是一条别人跑不起来的钩子。
- 加 `--pre-commit` 同时装 git 的提交前检查；已经有提交前钩子、或设了 `core.hooksPath` 的仓库不装，只说明怎么接。
- 钩子和 MCP 用的是运行这条命令的那个 pystepmap 的绝对路径；`--command "…"` 可以换。
- 标记包 pystepkit 不替你装：在目标项目的环境里自己装上（现在还没有发布到 PyPI，用本机路径：
  `uv add --editable <本仓库>/kit/pystepkit`）。MCP 需要装了平台后端的 pystepmap（本仓库 `uv sync` 之后就有）。
- 装好后重启 Claude Code。
