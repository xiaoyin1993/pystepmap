# 开发

仓库结构、怎么跑 pystepmap 自己的测试、改前端时的开发模式。回到 [README](../README.md)。

## 仓库结构

仓库里有三个包和一个前端：

- `core`（`pystepmap-core`）：分析引擎、命令行 `pystepmap`、记录插件 `pystepmap.trace`。
- `kit/pystepkit`（`pystepkit`）：给业务代码打流程标记的装饰器（`@workflow`、`@step` / `@node`、`@condition`、
  `outcome(...)`），零依赖、运行时零开销。不打标记的代码照样能分析；标记只影响标题、分组和约束检查。
- `server`（`pystepmap-server`）：平台后端（FastAPI，SQLite 存储，只监听 `127.0.0.1`）和给 Claude Code 用的 MCP 服务
  （`pystepmap mcp`）。引擎不依赖它。
- `web/`：平台的前端（Vue 3 + Vue Flow + elkjs），打包后由 `pystepmap serve` 直接提供。

## 跑测试

需要 Python 3.12 及以上和 [uv](https://docs.astral.sh/uv/)。在仓库根目录：

```bash
uv sync          # 建 .venv，装好 core、pystepkit 和开发依赖
uv run pytest -q # 跑 pystepmap 自己的测试
```

## 开发模式（改前端时）

两个终端：

```bash
uv run pystepmap serve --no-open   # 终端 1：后端，端口 5179
cd web && npm run dev            # 终端 2：Vite 开发服务器，打开 http://localhost:5180（改代码自动刷新）
```

开发服务器把 `/api` 转给 `http://127.0.0.1:5179`（`web/vite.config.ts`）。后端用了别的端口时，启动开发服务器前设
环境变量 `PYSTEPMAP_API`，例如 `PYSTEPMAP_API=http://127.0.0.1:5191 npm run dev`；开发服务器换端口用
`npm run dev -- --port 5181`，页面要用 `http://localhost:<端口>` 打开（它只监听 localhost）。前端的测试：`cd web && npm test`
（设 `PYSTEPMAP_TEST_GRAPHS=<放了图 JSON 的目录>` 时，再对目录里每张真实图的每条功能线做排版检查；设
`PYSTEPMAP_TEST_DIFFS=<目录>` 时，用存下来的图和 `/diff` 响应检查画布标记，见 `web/src/diffview.acceptance.test.ts`；设
`PYSTEPMAP_TEST_API=http://127.0.0.1:<端口>/api` 时，对一个开着的后端最近一次运行的每个测试检查运行路径叠到图上的结果，见
`web/src/runview.acceptance.test.ts`）、`npm run build`。
