"""HTTP 接口：全部在 /api 下，JSON。错误统一为 {"error": "中文说明"} 加合适的状态码；流程失败时另带 "code"
（见 flows 的说明：400 / 404 / 409 / 500，分析超时 504），"目录已经添加过"时还带 "project_id"。

    GET    /api/health                              {"ok": true, "version", "engine_version"}
    GET    /api/projects                            [项目]
    POST   /api/projects        {root, name?, config?}  201 项目；400 / 409 / 500 / 504
    GET    /api/projects/{id}                       项目
    DELETE /api/projects/{id}                       204
    POST   /api/projects/{id}/analyze               项目（带新的一次分析）；404 / 409 正在分析或运行测试 / 500 / 504 超时
    GET    /api/projects/{id}/graph                 最新一次的图 JSON 原样，gzip 传输；头 X-Pystepmap-Analysis、X-Pystepmap-Graph-Size
    GET    /api/projects/{id}/status                {analysis_id, analyzing, stale, base_id, has_diff, run, running}
    GET    /api/projects/{id}/diff                  {head: 分析, base: 分析 | null, diff: 流程差异 | null, baseline}
    POST   /api/projects/{id}/diff/ack?analysis=<看过的那次分析 id>  标记流程变化已看过；返回同上的形状（base、diff
                                                    为 null）；400 缺 analysis / 404 / 409 busy 正在分析、moved 最新一次已经换了
    GET    /api/projects/{id}/overrides             {line_sid: {title?, group?}}
    PUT    /api/projects/{id}/overrides?sid=…  {title?, group?}  整条改写，两项都空 = 删除；返回全部手工项
    GET    /api/projects/{id}/source?file=&start=&end=[&analysis=]  {file, start, end, total, lines, stale}
    GET    /api/projects/{id}/config                项目的分析配置（引擎的配置 JSON）
    PUT    /api/projects/{id}/config                改配置（不自动重新分析）；返回改后的配置
  测试命令和运行（运行测试走 testflows.run_project_tests，在后台线程里；只有这几个接口能启动、取消测试）：
    GET    /api/projects/{id}/test-command          {"command": str | null, "confirmed": bool}；404
    PUT    /api/projects/{id}/test-command  {"command": str | null}  去掉首尾空白，空 = 清掉，最长 2000 字，不能有
                                                    NUL；命令改了 confirmed 变 false；返回同上的形状；400 / 404
    GET    /api/projects/{id}/interpreters          {"interpreters": [{"cwd": 项目目录下的子目录（"" = 项目目录）,
                                                    "python": ".venv/bin/python"}]}：项目目录和它的直接子目录里找到
                                                    的虚拟环境（只看文件在不在，不执行；给测试命令的表单用）；404
    GET    /api/projects/{id}/reviews               变更记录：{"reviews": [{id, acked_at, head_id, head_at, base_id, base_at,
                                                    summary}]}，新的在前；每次标为已看（POST …/diff/ack）时存下那次的流程变化，
                                                    留最近 10 条。summary：{"lines": [{sid, title, group, status}], "functions",
                                                    "added", "removed", "changed"（节点个数）}
    GET    /api/projects/{id}/reviews/{review_id}   一条记录：同上，加 "diff"（存下的流程差异原样）和 "text"（差异写成的
                                                    文字，同 MCP 的 diff_flows）；404。每条的 graph_size 不为 null 时存着
                                                    当时的图
    GET    /api/projects/{id}/reviews/{review_id}/graph
                                                    这条记录当时的图（标为已看的那次分析的图，和 /graph 同一个格式、同样的
                                                    头和 gzip 传输）：diff 里节点的 new_id 就是这张图里的节点 id。404：没有
                                                    这条记录 / 没有存图（code no_graph：结构版本 5 时存的记录）
    GET    /api/projects/{id}/requirements          项目里的需求（现读 .pystepmap/requirements/）、各自和最新一次分析的对照、
                                                    设计流程的图（requirements_view.py 说明了形状）；404
    GET    /api/projects/{id}/line-tests?line=<功能线 sid>
                                                    只跑这条线的话会跑哪些测试：{"ok", "code", "reason", "tests": [测试 id],
                                                    "base_run": 名单取自哪次全量运行}；还没有全量运行（no_full_run）、没有测试
                                                    走过这条线（no_tests）、那次运行没有函数索引（no_index）时 ok 为假、200；
                                                    图里没有这条线 404
    PUT    /api/projects/{id}/mcp-tests  {"allow": bool}  允许 / 不允许 AI 经 MCP 运行单条功能线的测试（项目的 mcp_tests）
    POST   /api/projects/{id}/runs  {"confirm"?: bool, "command"?: str, "line"?: 功能线 sid, "via"?: "mcp"}
                                                    line：只跑走过这条线的测试（名单同 line-tests；挑不出来 409 / 404）。
                                                    via "mcp"（pystepmap mcp 替 AI 发的）：项目没开 mcp_tests 403 mcp_off；
                                                    没给 line、或带了 confirm 403 mcp_scope；命令没确认过 409 confirm。
                                                    这只挡得住 MCP 的工具本身：本机上能发 HTTP 请求的程序都可以不带 via
                                                    202 {"run": "started"}；400 no_command 没填命令 / 400 invalid 目录
                                                    不可用 / 404 / 409 confirm 命令还没确认过（带 "command"、"root"；
                                                    用户同意后带 {"confirm": true, "command": 弹窗里那一条} 再发：和现在
                                                    存着的相同才记下确认并运行，否则再回 409 confirm 带现在的命令）/
                                                    409 busy 正在分析或运行测试
    GET    /api/projects/{id}/runs[?live_line=<功能线 sid>]
                                                    {"running": null | {started_at, seconds, phase, progress?}, "runs":
                                                    [运行，新的在前], "last_failure": null | {title, reason, code, at}}；
                                                    progress 只在测试命令跑着（phase testing）时有，见下"运行中的进度"
    POST   /api/projects/{id}/runs/cancel           200 {"cancelling": true}（测试命令在跑：结束它）；运行前的重新分析
                                                    期间 200 {"cancelling": true, "note": "正在重新分析，分析完就停"}；
                                                    409 code too_late "测试已经跑完，正在整理结果，取消不了"（processing）；
                                                    409 code not_running 没有在跑的；404
    GET    /api/projects/{id}/runs/output           {"lines": [...]}：本进程内存里这个项目最近一次运行的输出末尾（最多
                                                    200 行；运行中是到现在为止的）；没有时空表。唯一带出 pytest 输出的接口
    GET    /api/projects/{id}/runs/{run_id}[?line=<功能线 sid>]
                                                    运行 + "tests": [{test, outcome, seconds, cases, functions}]；404。
                                                    给 line 时每个测试多 "on_line"（这个测试执行了这条线上的几个函数：线上的
                                                    函数按最新一次分析的图，运行的函数按 core 的规则 locate_function 放到这张
                                                    图上；**没有执行这条线的入口函数**——根任务或它派发的任务的函数——时为 0：
                                                    只碰到线上深处共用的函数不算走过这条线），顶层多 "line_known": true；没有函数索引的运行（本阶段较早的代码
                                                    存的）"line_known": false、不给 on_line；最新的图里没有这条线 404
    GET    /api/projects/{id}/runs/{run_id}/paths[?test=<node id>]
                                                    不带 test：run_paths_to_json 的结果原样，gzip 传输；头 X-Pystepmap-Run、
                                                    X-Pystepmap-Analysis（这次运行用的分析）、X-Pystepmap-Paths-Size。带 test：
                                                    和整份同一个形状，tests 里只有这一个测试（test= 空是测试之外的执行），
                                                    运行一级的项照带（shapes 只留这个测试里的函数）；大于 1 KB 时 gzip
                                                    传输。404：没有这次运行 / 没有路径（没跑完）/ 没有这个测试
    GET    /api/projects/{id}/runs/{run_id}/uncovered[?line=<功能线 sid>]
                                                    {"outcomes": [{function, node, title, end_kind, file, line, uncertain, added}],
                                                     "stale": bool}；404：没有这次运行 / 没有路径 / 最新的图里没有这条线

项目：{id, name, root, created_at, updated_at, analyzing, analysis}。
  analyzing：本进程在分析它，或者别的进程（pystepmap mcp 等）拿着它的分析锁。锁一直拿到新的分析存好、旧的清理完：
  analyzing 从 true 变成 false 之后再取，拿到的一定是新结果。运行测试拿的是同一把锁，但本进程在跑这个项目的测试
  （阶段 testing、processing）时 analyzing 为 false——那时看 /status 的 running；运行前的重新分析（阶段 analyzing）算分析。
分析（项目的 analysis、diff 的 head / base）：{id, created_at, engine_version, summary, graph_size, base_id, has_diff}；
  base_id 是对比基准的分析 id（null = 没有可比的：第一次分析、刚标为看过、之后流程一直没变，或基准那次已不在库里），
  has_diff 是存着流程差异且基准那次还在。对比基准只在"标为已看"时移动：has_diff 为 true 说的是上次标为已看以来
  （没标过就是第一次分析以来）有没看的流程变化，不只是和上一次分析比。

status：analysis_id 是最新一次分析（没有时 null），base_id、has_diff 同它的；stale 是 status.staleness 的结果，按最新一次
  分析当时的配置（结构版本 1 留下的分析没有记，用项目现在的配置）和指纹比：
    {"checked": true, "stale", "changed", "added", "removed", "unreadable"（各按路径排序，最多 200 个）,
     "lines": 涉及改了 / 删了的文件的功能线 sid | null（旧版本存的分析没有这项信息）,
     "total": {changed, added, removed, unreadable}, "note"?: "有 N 个文件读不了，没有比对"（有读不了的文件时才有）}
    unreadable：读不了、没有比对的文件（没有权限、所在目录读不了、分析时是普通文件现在换成了指到项目外的链接），
    不算删除；stale 只看 changed / added / removed。
    或 {"checked": false, "reason": 中文}（项目目录没了、被换成符号链接、读不了）——仍是 200。
  run：最近一次测试运行 {id, status, finished_at, failed（失败和出错的用例数）, verify_ok（核对没有不符；没跑完时 null）}
  | null；running：本进程正在跑这个项目的测试。
diff：diff 是 pystepmap.diff.diff_to_json 的结果原样（{"version": 1, "functions": {函数 sid: 变化}, "lines": [变化]}，
  字段见 core/src/pystepmap/diff.py），没有基准或差异为空时 null。head、base、diff 一条语句读出，是同一个时刻的。
  baseline 说对比基准代表什么（base 为 null 时也是 null）：{"kind": "acknowledged", "at": 基准那次分析的时间}——相对上次
  标为已看时；{"kind": "first", "at": 第一次分析（添加项目）的时间}——没标为已看过，相对第一次分析。界面按它写
  "相对上次标为已看时（<时间>）" / "相对第一次分析（<时间>）"，不写"相对上一次分析"（基准可能比上一次分析早）。
  基准那次已经不在库里时（旧版本的服务按旧规则清理掉的）当作没有可比的：base、diff 都是 null，head 和项目的 analysis
  里 base_id 为 null、has_diff 为 false——不会出现"base 为 null 而 diff 不为 null"。
diff/ack：analysis 必填，是界面上正在看的那次（head.id）。最新一次已经不是它（别的进程刚分析完）时不清，409
  {"error": "分析结果已经更新，请先看新的变化", "code": "moved"}；正在分析（本进程或别的进程）时 409 code busy
  {"error": "正在分析，等分析完再标为看过"}。
source：stale 是这个文件和某次分析时比变了没有（图里没有它的指纹时 null）；analysis=<分析 id> 指定按哪次分析比，
  那次不存在（已被清理）或不是这个项目的，按最新一次。
运行：{id, analysis_id, started_at, finished_at, command, status, exit_code, summary, paths_size}。status：ok 命令退出码 0 /
  tests_failed 有失败的用例 / error 命令没跑成、没收到记录或核对出错 / timeout / cancelled；summary 的形状见
  testflows 的模块说明（"汇总"）；paths_size 是运行路径 JSON 的字节数，没有路径时 null。
  running.phase：preparing（拿到锁、检查图是否过期、准备临时文件）→ analyzing（只在图过期、运行前先重新分析时）→ testing
  （跑测试命令）→ processing（读记录、核对、汇总、保存）。
  运行中的进度（running.progress，live.py 从记录插件正在写的记录文件里读，最多每秒读一次，不入库）：
    {"collected": 收集到的用例数 | null, "done", "passed", "failed", "skipped", "errors"（跑完的用例数，按结果行）,
     "running": [正在跑的测试 id，最多 3 个，先开始的在前], "running_more": 另外还有几个在跑,
     "recent": [{test, outcome, seconds}，最近跑完的 8 个，新的在前],
     "lines": {功能线 sid: {"functions": 执行过的函数, "total": 涉及的函数}}（按这次运行用的那次分析；只列有函数执行过的）}；
    给了 ?live_line= 时多 "nodes": {函数 key: [走到过的节点 sid]}（这条线涉及的执行过的函数；节点的语句头部执行过算走到过，
    开始节点在函数执行过时算，捕获节点、和别的节点同一行的结局、行号为 0 的结局不给）；图里没有这条线时没有 nodes。
    执行记录在每个测试结束时才写出：说的都是已经跑完的测试。旧版界面不看这一项。
  last_failure：202 之后、存下运行之前就失败了（运行前的分析失败、保存失败等）时的原因，只在本进程内存里；下一次
  运行开始时清掉。超时、取消、命令没能启动都会存一条运行（没有路径），不算在这里。
  POST /runs 的确认：第一次运行一条命令前 409 confirm，error 里说"这条命令会在你的项目目录里执行；如果测试会连开发
  数据库等外部服务，它们会被真的访问"，界面据此弹窗；带 {"confirm": true, "command": 弹窗里的命令} 再发时，只在项目的
  命令还是这一条时记下确认（没带 command、或命令中间被改了：再回 409 confirm，带现在的命令）。
uncovered：按最新一次分析的图和这次运行的路径算（core 的 runpath.uncovered_outcomes，按 sid 经 match_function_run 叠到
  新图上），按（最新分析，运行）缓存；stale：最新一次分析不是这次运行用的那次。uncertain 为 true：运行之后这个函数的
  节点变了、同文本的几个结局分不清，或者这个函数、这个结局是运行之后才有的（对着运行时那次分析的图看；那次分析已经
  清理掉时分不出来，照旧算没走到），既不算走到也不算没走到（界面写"运行之后代码改过，这几处对不上"）。

添加、重新分析、删除、标为看过走 flows 里的流程；端点都是同步函数，FastAPI 在线程池里跑，分析不挡住事件循环。
运行测试的流程在 runner.launch 起的后台线程里跑，POST 等它拿到项目的锁（或在那之前就失败）再回。删除项目时取消它
正在跑的测试。
库可能同时被另一个进程（pystepmap mcp）写：每次请求都从库里读最新的，只按分析 id 缓存不会变的东西（指纹、配置）。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import gzip
import json
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pystepmap.config import ConfigError, parse_config
from pystepmap.diff import diff_from_json, diff_text
from pystepmap.engineid import ENGINE_VERSION
from pystepmap.index import (
    _configured_source_roots as check_source_roots,  # 引擎建索引时检查源码根用的同一个函数
)
from pystepmap.model import from_json
from pystepmap.query import line_functions
from pystepmap.runpath import run_paths_from_json
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, runner
from .flows import (
    acknowledge_changes,
    add_project,
    is_analyzing,
    reanalyze_project,
    remove_project,
)
from .requirements_view import requirements_view
from .shared import (
    NO_FACTS,
    FactsCache,
    Uncovered,
    line_entries,
    place_run_functions,
    uncovered_of,
)
from .sources import SourceError, get_snippet
from .status import staleness
from .store import ProjectMissingError, Store, StoreError
from .testflows import (  # 运行测试（执行项目代码）的流程：只有这里导入
    pick_line_tests,
    run_project_tests,
)

MAX_TITLE = 200
MAX_GROUP = 100
MAX_SID = 2000
MAX_COMMAND = 2000
MAX_ROW_ID = 2 ** 63 - 1  # SQLite 整数的上限
SLICE_GZIP_FROM = 1024  # 单个测试的路径大于这么多字节时压缩了再发（客户端接受 gzip 时）

# 流程失败的 code → 状态码
_STATUS = {"invalid": 400, "config": 400, "duplicate": 409, "overlap": 409, "busy": 409, "not_found": 404,
           "analysis": 500, "storage": 500, "timeout": 504, "moved": 409, "no_command": 400, "confirm": 409,
           "prepare": 500, "internal": 500, "no_full_run": 409, "no_tests": 409, "no_index": 409, "no_line": 404}

MCP_TESTS_OFF = ("这个项目没有允许 AI 经 MCP 运行测试：要允许的话，由用户在平台界面的测试命令设置里打开"
                 "\"允许 AI 运行单条功能线的测试\"")
MCP_NEEDS_CONFIRMED = "测试命令还没有填、或者用户还没有在平台界面上确认过它：经 MCP 不能确认测试命令"
NO_COMMAND = "还没有填测试命令：先在项目的测试命令里填一条（比如 .venv/bin/python -m pytest）"

_VALIDATION_MESSAGES = {
    "string_type": "必须是字符串", "dict_type": "必须是对象", "model_attributes_type": "必须是 JSON 对象",
    "extra_forbidden": "是不认识的字段", "json_invalid": "不是合法的 JSON", "int_parsing": "必须是整数",
}


class ApiError(Exception):
    """接口返回的错误：status 状态码，message 中文说明，extra 并进返回体。"""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class AddProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    name: str | None = None
    config: dict | None = None


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    group: str | None = Field(default=None, max_length=MAX_GROUP)


class TestCommandBody(BaseModel):
    __test__ = False  # 名字以 Test 开头：别让 pytest 当成测试类去收集
    model_config = ConfigDict(extra="forbid")
    command: str | None  # 必填（可以是 null）；去掉首尾空白后为空 = 清掉


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    command: str | None = None  # confirm 为真时必填：确认的是弹窗里给用户看的那一条（要和现在存着的相同）
    line: str | None = None  # 功能线的 sid：只跑走过这条线的测试（名单取自最近一次跑完的全量运行）；不给 = 全部测试
    via: str | None = None  # "mcp"：这次请求是 pystepmap mcp 替 AI 发的（要项目开了 mcp_tests，只能跑一条线，不能确认命令）


class McpTestsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool


def _confirm_text(project: dict) -> str:
    """运行前确认框里的那句说明"""
    return (f"第一次运行这条测试命令之前要确认：这条命令会在你的项目目录（{project['root']}）里执行；如果测试会连开发"
            f"数据库等外部服务，它们会被真的访问。")


def _flow_error(result: dict) -> ApiError:
    """把流程的失败结局变成接口错误"""
    extra = {"code": result["code"]}
    if result.get("project_id"):
        extra["project_id"] = result["project_id"]
    return ApiError(_STATUS.get(result["code"], 500), result["reason"], **extra)


def _accepts_gzip(request: Request) -> bool:
    for part in request.headers.get("accept-encoding", "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "*") and params.replace(" ", "") not in ("q=0", "q=0.0", "q=0.00", "q=0.000"):
            return True
    return False


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class _Overlay:
    """没走到的结局：按（最新一次分析，运行）缓存算好的结果（shared.Uncovered，只留结果；真实项目的路径 JSON 几十
    MB，解析一次要零点几秒到一秒多）。最近 size 对。分析 id、运行 id 都不重用，缓存不会过时。"""

    def __init__(self, store: Store, size: int = 4):
        self._store = store
        self._size = size
        self._items: OrderedDict[tuple[int, int], Uncovered] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, project_id: str, run_id: int, analysis_id: int) -> Uncovered | None:
        """None：分析或运行刚被清理掉了。"""
        key = (analysis_id, run_id)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        found = uncovered_of(self._store, project_id, run_id, analysis_id)
        if found is None:
            return None
        with self._lock:
            self._items[key] = found
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return found


class _Graphs:
    """最近一次用到的分析的图（解析好的）：需求对照每次都要用，真实项目的图十几 MB，不想每个请求解析一遍。"""

    def __init__(self, store: Store):
        self._store = store
        self._item: tuple[int, object] | None = None
        self._lock = threading.Lock()

    def get(self, analysis_id: int):
        with self._lock:
            if self._item is not None and self._item[0] == analysis_id:
                return self._item[1]
        text = self._store.graph_json(analysis_id)
        if text is None:
            return None
        graph = from_json(text)
        with self._lock:
            self._item = (analysis_id, graph)
        return graph


@dataclass(frozen=True)
class _Placed:
    """一次运行放到一张图上：每条功能线涉及的函数（图里的函数 key），运行的函数索引（store.run_function_index），以及
    索引里每个函数放到了图里的哪个函数（放不上为 None）。没有函数索引时后两项为 None。"""

    lines: dict[str, frozenset[str]]
    entries: dict[str, frozenset[str]]  # 每条功能线的入口函数（shared.line_entries）
    index: dict | None
    placed: list[str | None] | None


class _OnLine:
    """按功能线数每个测试执行了几个函数：按（最新一次分析，运行）缓存 _Placed，最近 size 对。只读函数索引和压缩包里
    运行一级的那几段（shapes、function_dups），不解开整份路径；图解析一次、算完就丢。"""

    def __init__(self, store: Store, size: int = 4):
        self._store = store
        self._size = size
        self._items: OrderedDict[tuple[int, int], _Placed] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, project_id: str, run_id: int, analysis_id: int) -> _Placed | None:
        """None：那次分析刚被清理掉了。"""
        key = (analysis_id, run_id)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        text = self._store.graph_json(analysis_id)
        if text is None:
            return None
        graph = from_json(text)
        lines = {(ln.sid or ln.id): frozenset(line_functions(graph, ln)) for ln in graph.lines}
        entries = {(ln.sid or ln.id): line_entries(graph, ln) for ln in graph.lines}
        index = self._store.run_function_index(project_id, run_id)
        meta = None if index is None else self._store.run_paths_meta(project_id, run_id)
        if index is None or meta is None:
            found = _Placed(lines, entries, None, None)
        else:  # 运行一级的项交给 core 解析，按 core 的规则放（shared.place_run_functions）
            run_meta = run_paths_from_json({**meta, "tests": []})
            found = _Placed(lines, entries, index, place_run_functions(graph, run_meta, index["functions"]))
        with self._lock:
            self._items[key] = found
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return found


async def api_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """框架自己报的 404 / 405 等（没有这个接口、方法不对）：回中文的 {"error": …}。app 也用它处理 /api 下的路径。"""
    if exc.status_code == 404:
        message = f"没有这个接口：{request.method} {request.url.path}"
    elif exc.status_code == 405:
        message = f"这个接口不支持 {request.method}：{request.url.path}"
    else:
        message = str(exc.detail)
    return JSONResponse({"error": message}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def _install_error_handlers(api: FastAPI) -> None:
    @api.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status)

    api.add_exception_handler(StarletteHTTPException, api_http_error)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors()[:3]:
            where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query")) or "请求体"
            kind = err.get("type", "")
            if kind == "json_invalid":  # 位置是解析出错的字符下标，对用户没有意义
                parts.append("请求体不是合法的 JSON")
            elif kind == "missing":
                parts.append(f"缺少 {where}")
            elif kind == "string_too_long":
                parts.append(f"{where} 太长了（最多 {err.get('ctx', {}).get('max_length')} 个字）")
            else:
                parts.append(f"{where} {_VALIDATION_MESSAGES.get(kind, err.get('msg', '不合法'))}")
        return JSONResponse({"error": "请求参数不对：" + "；".join(parts)}, status_code=400)

    @api.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=500)

    @api.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": f"服务器内部错误：{type(exc).__name__}: {exc}"}, status_code=500)


VENV_NAMES = (".venv", "venv", "env")
MAX_INTERPRETER_DIRS = 200


def find_interpreters(root: Path) -> list[dict]:
    """项目目录和它的直接子目录（不看隐藏目录、符号链接，最多 200 个）里的虚拟环境解释器。只看文件在不在，不执行。"""
    try:
        subdirs = sorted(d for d in root.iterdir() if not d.name.startswith(".") and d.is_dir() and not d.is_symlink())
    except OSError:
        return []
    found = []
    for d in [root, *subdirs[:MAX_INTERPRETER_DIRS]]:
        for venv in VENV_NAMES:
            if (d / venv / "bin" / "python").is_file():
                found.append({"cwd": "" if d == root else d.name, "python": f"{venv}/bin/python"})
    return found


def create_api(store: Store) -> FastAPI:
    """/api 下的全部接口（挂在 app 的 /api 上）。"""
    api = FastAPI(title="pystepmap 平台接口", version=__version__, docs_url=None, redoc_url=None)
    api.state.store = store
    home = store.path.parent
    facts_cache = FactsCache(store, size=4)
    overlay = _Overlay(store)
    design_base = _Graphs(store)
    on_line = _OnLine(store)
    _install_error_handlers(api)

    def facts_of(analysis_id: int):
        """取一次分析的图上事实（带缓存）"""
        return facts_cache.get(analysis_id) or NO_FACTS

    def project_or_404(project_id: str) -> dict:
        """取项目，不存在时报 404"""
        project = store.get_project(project_id)
        if project is None:
            raise ApiError(404, f"项目不存在（可能已经删除了）：{project_id}")
        return project

    def analyzing(project_id: str) -> bool:
        """在分析：项目锁被拿着，而且不是因为本进程在跑它的测试（跑测试时拿同一把锁；运行前的重新分析算在分析）。"""
        if not is_analyzing(store, project_id):
            return False
        now = runner.running(home, project_id)
        return now is None or now["phase"] == "analyzing"

    def busy_error(result: dict, project_id: str, doing: str) -> ApiError:
        """流程说 busy 时：本进程在跑这个项目的测试就说"正在运行测试"。"""
        if result.get("code") == "busy" and runner.running(home, project_id) is not None:
            return ApiError(409, f"正在运行测试，等测试跑完再{doing}", code="busy")
        return _flow_error(result)

    def view(project: dict, analysis: dict | None) -> dict:
        return {"id": project["id"], "name": project["name"], "root": project["root"],
                "created_at": project["created_at"], "updated_at": project["updated_at"],
                "analyzing": analyzing(project["id"]), "analysis": analysis, "mcp_tests": project["mcp_tests"]}

    def project_view(project_id: str) -> dict:
        """项目和它最新一次分析的摘要"""
        return view(project_or_404(project_id), store.latest_analysis(project_id))

    @api.get("/health")
    def health() -> dict:
        """服务是否正常、版本号"""
        return {"ok": True, "version": __version__, "engine_version": ENGINE_VERSION}

    @api.get("/projects")
    def list_projects() -> list[dict]:
        """列出全部项目"""
        return [view(p, p["analysis"]) for p in store.list_projects()]

    @api.post("/projects", status_code=201)
    def create_project(body: AddProjectBody) -> JSONResponse:
        """添加项目并做第一次分析"""
        result = add_project(store, body.root, body.name, body.config)
        if result["kind"] != "ok":
            raise _flow_error(result)
        return JSONResponse(project_view(result["project_id"]), status_code=201)

    @api.get("/projects/{project_id}")
    def get_project(project_id: str) -> dict:
        """查看一个项目"""
        return project_view(project_id)

    @api.delete("/projects/{project_id}", status_code=204)
    def delete_project(project_id: str) -> Response:
        """删除项目（在跑的测试一并结束）"""
        result = remove_project(store, project_id)
        if result["kind"] != "ok":
            raise _flow_error(result)
        runner.cancel_run(home, project_id)  # 在跑的测试：结束它（流程随后存运行时发现项目没了）
        runner.forget(home, project_id)
        return Response(status_code=204)

    @api.post("/projects/{project_id}/analyze")
    def analyze(project_id: str) -> dict:
        """重新分析项目"""
        result = reanalyze_project(store, project_id)
        if result["kind"] != "ok":
            raise busy_error(result, project_id, "分析")
        return project_view(project_id)

    @api.get("/projects/{project_id}/status")
    def status(project_id: str) -> dict:
        """代码是否已变更、有没有在分析或跑测试"""
        project = project_or_404(project_id)
        latest = store.latest_analysis(project_id)
        busy = analyzing(project_id)
        tests = {"run": run_brief(store.latest_run(project_id)), "running": runner.running(home, project_id) is not None}
        if latest is None:
            return {"analysis_id": None, "analyzing": busy,
                    "stale": {"checked": False, "reason": "这个项目还没有分析结果"}, "base_id": None, "has_diff": False,
                    **tests}
        facts = facts_of(latest["id"])
        config = facts.config if facts.config is not None else project["config"]
        return {"analysis_id": latest["id"], "analyzing": busy,
                "stale": staleness(project["root"], config, facts.hashes, facts.file_lines),
                "base_id": latest["base_id"], "has_diff": latest["has_diff"], **tests}

    def run_brief(run: dict | None) -> dict | None:
        """/status 里最近一次运行的那一段：{id, status, finished_at, failed（失败和出错的用例数）, verify_ok}。"""
        if run is None:
            return None
        s = run["summary"]
        return {"id": run["id"], "status": run["status"], "finished_at": run["finished_at"],
                "failed": s.get("failed", 0) + s.get("errors", 0),
                "verify_ok": None if s.get("verify") is None else s["verify"].get("ok")}

    def diff_view(project_id: str) -> dict:
        """最新分析和对比基准之间的流程差异"""
        project_or_404(project_id)
        state = store.latest_diff(project_id)  # head 和它的差异一条语句读出：别的进程的写入插不进中间
        if state is None:
            raise ApiError(404, "这个项目还没有分析结果")
        return state

    @api.get("/projects/{project_id}/diff")
    def diff(project_id: str) -> dict:
        """查看流程差异"""
        return diff_view(project_id)

    @api.post("/projects/{project_id}/diff/ack")
    def acknowledge(project_id: str, analysis: Annotated[int, Query()]) -> dict:
        """把流程变化标为已看"""
        result = acknowledge_changes(store, project_id, analysis)
        if result["kind"] != "ok":
            raise busy_error(result, project_id, "标为看过")
        return diff_view(project_id)

    @api.get("/projects/{project_id}/reviews")
    def list_reviews(project_id: str) -> dict:
        """变更记录：每次标为已看时存下的流程变化"""
        project_or_404(project_id)
        return {"reviews": store.list_reviews(project_id)}

    @api.get("/projects/{project_id}/reviews/{review_id}")
    def get_review(project_id: str, review_id: int) -> dict:
        """一条变更记录的明细"""
        project_or_404(project_id)
        found = store.get_review(project_id, review_id)
        if found is None:
            raise ApiError(404, f"没有这条变更记录：{review_id}（只留最近 10 条，可能已经清掉了）")
        try:
            text = diff_text(diff_from_json(found["diff"]))
        except ValueError as e:
            text = f"这条记录存着的流程差异读不出来：{e}"
        return {**found, "text": text}

    @api.get("/projects/{project_id}/reviews/{review_id}/graph")
    def review_graph(project_id: str, review_id: int, request: Request) -> Response:
        """一条变更记录当时的图"""
        project_or_404(project_id)
        found = store.get_review(project_id, review_id)
        if found is None:
            raise ApiError(404, f"没有这条变更记录：{review_id}（只留最近 10 条，可能已经清掉了）")
        gz = store.review_graph_gz(project_id, review_id)
        if gz is None:
            raise ApiError(404, "这条变更记录没有存当时的图（旧版本存的记录），只能看文字明细", code="no_graph")
        headers = {"X-Pystepmap-Analysis": str(found["head_id"]), "X-Pystepmap-Graph-Size": str(found["graph_size"]),
                   "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if _accepts_gzip(request):
            return Response(gz, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
        return Response(gzip.decompress(gz), media_type="application/json", headers=headers)

    @api.get("/projects/{project_id}/graph")
    def graph(project_id: str, request: Request) -> Response:
        """取最新一次分析的图"""
        project_or_404(project_id)
        latest = store.latest_analysis(project_id)
        gz = None if latest is None else store.graph_gz(latest["id"])
        if gz is None:
            raise ApiError(404, "这个项目还没有分析结果")
        headers = {"X-Pystepmap-Analysis": str(latest["id"]), "X-Pystepmap-Graph-Size": str(latest["graph_size"]),
                   "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if _accepts_gzip(request):  # 存的就是压缩好的字节：原样发出去，不在每次请求时重新压缩
            return Response(gz, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
        return Response(gzip.decompress(gz), media_type="application/json", headers=headers)

    @api.get("/projects/{project_id}/overrides")
    def get_overrides(project_id: str) -> dict:
        """查看功能线的手工标题和分组"""
        project_or_404(project_id)
        return store.get_overrides(project_id)

    @api.put("/projects/{project_id}/overrides")
    def put_override(project_id: str, body: OverrideBody, sid: Annotated[str | None, Query()] = None) -> dict:
        """改一条功能线的手工标题和分组"""
        if not sid or not sid.strip():
            raise ApiError(400, "缺少功能线标识：放在查询参数 sid 里（?sid=…）")
        if len(sid) > MAX_SID:
            raise ApiError(400, f"功能线标识太长了（最多 {MAX_SID} 个字）")
        try:
            store.set_override(project_id, sid, _blank_to_none(body.title), _blank_to_none(body.group))
        except ProjectMissingError as e:
            raise ApiError(404, str(e)) from None
        return store.get_overrides(project_id)

    @api.get("/projects/{project_id}/source")
    def source(project_id: str, file: str | None = None, start: str | None = None, end: str | None = None,
               analysis: int | None = None) -> dict:
        """读一段源码"""
        project = project_or_404(project_id)
        ids = [a["id"] for a in store.list_analyses(project_id)]  # 新的在前
        chosen = analysis if analysis in ids else (ids[0] if ids else None)  # 指定的那次不在了：按最新一次
        try:
            return get_snippet(project["root"], {} if chosen is None else facts_of(chosen).hashes, file, start, end)
        except SourceError as e:
            raise ApiError(e.status, e.message) from None

    @api.get("/projects/{project_id}/config")
    def get_config(project_id: str) -> dict:
        """查看分析配置"""
        return project_or_404(project_id)["config"]

    @api.put("/projects/{project_id}/config")
    def put_config(project_id: str, config: Annotated[Any, Body()]) -> dict:
        """修改分析配置"""
        project = project_or_404(project_id)
        try:
            parsed = parse_config(config)
            if parsed.source_roots is not None:  # 和分析时同一个检查：现在就报，不等下次分析才失败
                check_source_roots(Path(project["root"]), parsed.source_roots)
        except ConfigError as e:
            raise ApiError(400, f"分析配置不合法：{e}") from None
        if not store.set_config(project_id, config):
            raise ApiError(404, f"项目不存在（可能已经删除了）：{project_id}")
        return config

    # ---------- 测试命令和运行 ----------

    @api.get("/projects/{project_id}/test-command")
    def get_test_command(project_id: str) -> dict:
        """查看测试命令"""
        state = store.get_test_command(project_id)
        if state is None:
            raise ApiError(404, f"项目不存在（可能已经删除了）：{project_id}")
        return state

    @api.put("/projects/{project_id}/test-command")
    def put_test_command(project_id: str, body: TestCommandBody) -> dict:
        """修改测试命令"""
        text = (body.command or "").strip()
        if "\0" in text:
            raise ApiError(400, "测试命令里不能有空字符（NUL）")
        if len(text) > MAX_COMMAND:
            raise ApiError(400, f"测试命令太长了（最多 {MAX_COMMAND} 个字）")
        try:
            return store.set_test_command(project_id, text or None)
        except ProjectMissingError as e:
            raise ApiError(404, str(e)) from None

    @api.get("/projects/{project_id}/interpreters")
    def get_interpreters(project_id: str) -> dict:
        """列出项目里找到的虚拟环境解释器"""
        return {"interpreters": find_interpreters(Path(project_or_404(project_id)["root"]))}

    @api.post("/projects/{project_id}/runs", status_code=202)
    def start_run(project_id: str, body: RunBody | None = None) -> JSONResponse:
        """开始运行测试（第一次要确认命令）"""
        project = project_or_404(project_id)
        command = project["test_command"]
        line = None if body is None else body.line or None
        if body is not None and body.via is not None:
            if body.via != "mcp":
                raise ApiError(400, f"不认识的 via：{body.via}")
            if not project["mcp_tests"]:
                raise ApiError(403, MCP_TESTS_OFF, code="mcp_off")
            if line is None or body.confirm:
                raise ApiError(403, "经 MCP 只能运行一条功能线的测试，不能运行全部测试，也不能确认测试命令", code="mcp_scope")
            if not project["test_confirmed"]:
                raise ApiError(409, MCP_NEEDS_CONFIRMED, code="confirm")
        if not command:
            raise ApiError(400, NO_COMMAND, code="no_command")
        if not project["test_confirmed"]:
            # 确认的必须是弹窗里给用户看的那一条：没说是哪条、或者中间被改了，都按现在的命令重新问
            shown = None if body is None or not body.confirm else body.command
            if shown is None or not store.confirm_test_command(project_id, shown):
                now = project_or_404(project_id)
                raise ApiError(409, _confirm_text(now), code="confirm", command=now["test_command"], root=now["root"])
        scope = None
        if line is not None:  # 先问一遍挑不挑得出来：挑不出来现在就说，不等后台的流程
            latest = store.latest_analysis(project_id)
            picked = pick_line_tests(store, project_id, None if latest is None else latest["id"], line)
            if not picked["ok"]:
                raise _flow_error(picked)
            scope = {k: picked["scope"][k] for k in ("line", "line_title", "tests")}
        result = runner.launch(home, project_id,
                               lambda cancel: run_project_tests(store, project_id, cancel=cancel, line=line), scope=scope)
        if result is None:
            return JSONResponse({"run": "started"}, status_code=202)
        if result.get("code") == "confirm":  # 确认之后、开始之前命令又被改了：按新命令重新问
            now = project_or_404(project_id)
            raise ApiError(409, _confirm_text(now), code="confirm", command=now["test_command"], root=now["root"])
        raise _flow_error(result)

    @api.get("/projects/{project_id}/line-tests")
    def line_tests(project_id: str, line: str) -> dict:
        """只跑这条功能线的话会跑哪些测试"""
        project_or_404(project_id)
        latest = store.latest_analysis(project_id)
        picked = pick_line_tests(store, project_id, None if latest is None else latest["id"], line)
        if picked["code"] in ("no_line", "storage"):
            raise _flow_error(picked)
        scope = picked["scope"] or {}
        return {"ok": picked["ok"], "code": picked["code"], "reason": picked["reason"], "tests": picked["tests"] or [],
                "base_run": scope.get("base_run")}

    @api.put("/projects/{project_id}/mcp-tests")
    def put_mcp_tests(project_id: str, body: McpTestsBody) -> dict:
        """允许或不允许 AI 经 MCP 运行单条功能线的测试"""
        try:
            return {"allow": store.set_mcp_tests(project_id, body.allow)}
        except ProjectMissingError as e:
            raise ApiError(404, str(e)) from None

    @api.get("/projects/{project_id}/runs")
    def list_runs(project_id: str, live_line: str | None = None) -> dict:
        """运行列表和正在进行的运行（含实时进度）"""
        project_or_404(project_id)
        now = runner.running(home, project_id)
        if now is not None and now["phase"] == "testing":
            live = runner.live(home, project_id)  # 测试命令跑着：带上进度（读记录文件，最多每秒一次）
            progress = None if live is None else live.view(live_line or None)
            if progress is not None:
                now = {**now, "progress": progress}
        return {"running": now, "runs": store.list_runs(project_id), "last_failure": runner.last_failure(home, project_id)}

    @api.post("/projects/{project_id}/runs/cancel")
    def cancel_run(project_id: str) -> dict:
        """取消正在进行的运行"""
        project_or_404(project_id)
        phase = runner.cancel_run(home, project_id)
        if phase is None:
            raise ApiError(409, "这个项目现在没有在运行测试", code="not_running")
        if phase == "processing":
            raise ApiError(409, "测试已经跑完，正在整理结果，取消不了", code="too_late")
        if phase == "analyzing":
            return {"cancelling": True, "note": "正在重新分析，分析完就停"}
        return {"cancelling": True}

    @api.get("/projects/{project_id}/runs/output")
    def run_output(project_id: str) -> dict:
        """最近一次运行输出的末尾"""
        project_or_404(project_id)
        return {"lines": runner.last_output(home, project_id)}

    def run_or_404(project_id: str, run_id: int, *, with_paths: bool = False) -> dict:
        """取一次运行，不存在时报 404"""
        project_or_404(project_id)
        if not 0 < run_id <= MAX_ROW_ID:  # 库里放不下的编号：一定没有（不要让 SQLite 抛溢出变成 500）
            raise ApiError(404, f"没有这次运行（可能已经清理掉了）：#{run_id}")
        run = store.run_info(project_id, run_id)
        if run is None:
            raise ApiError(404, f"没有这次运行（可能已经清理掉了）：#{run_id}")
        if with_paths and run["paths_size"] is None:
            raise ApiError(404, f"运行 #{run_id} 没有运行路径（测试没有跑完，或者没有收到记录）")
        return run

    @api.get("/projects/{project_id}/runs/{run_id}")
    def get_run(project_id: str, run_id: int, line: str | None = None) -> dict:
        """一次运行的结果和测试列表（给了 line 时只列走过这条线的测试）"""
        run_or_404(project_id, run_id)
        run = store.get_run(project_id, run_id)
        if run is None:  # 刚被清理掉了
            raise ApiError(404, f"没有这次运行（可能已经清理掉了）：#{run_id}")
        if not line:
            return run
        latest = store.latest_analysis(project_id)
        if latest is None:
            raise ApiError(404, "这个项目还没有分析结果")
        found = on_line.get(project_id, run_id, latest["id"])
        if found is None:
            raise ApiError(404, "分析结果刚被更新了，请再取一次")
        keys = found.lines.get(line)
        if keys is None:
            raise ApiError(404, f"最新一次分析的图里没有这条功能线：{line}")
        if found.placed is None:  # 本阶段较早的代码存的运行：没有函数索引
            return {**run, "line_known": False}
        tests = found.index["tests"]
        entries = found.entries[line]
        for row in run["tests"]:
            ran = {found.placed[i] for i in tests.get(row["test"], ())}
            # 没有执行这条线的入口函数：不算走过这条线（只碰到了线上深处共用的函数），记 0
            row["on_line"] = len(ran & keys) if ran & entries else 0
        return {**run, "line_known": True}

    @api.get("/projects/{project_id}/runs/{run_id}/paths")
    def run_paths(project_id: str, run_id: int, request: Request, test: str | None = None) -> Response:
        """一次运行里各测试走过的路径"""
        run = run_or_404(project_id, run_id, with_paths=True)
        headers = {"X-Pystepmap-Run": str(run_id), "X-Pystepmap-Analysis": str(run["analysis_id"]),
                   "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if test is not None:  # 一个测试：只读出、解开它那一段（加上运行一级的小项）
            sliced = store.run_test_paths(project_id, run_id, test)
            if sliced is None:
                raise ApiError(404, f"运行 #{run_id} 里没有这个测试：{test}")
            data = json.dumps(sliced, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > SLICE_GZIP_FROM and _accepts_gzip(request):
                return Response(gzip.compress(data, compresslevel=6, mtime=0), media_type="application/json",
                                headers={**headers, "Content-Encoding": "gzip"})
            return Response(data, media_type="application/json", headers=headers)
        gz = store.run_paths_gz(project_id, run_id)
        if gz is None:
            raise ApiError(404, f"运行 #{run_id} 没有运行路径（可能刚被清理掉了）")
        headers["X-Pystepmap-Paths-Size"] = str(run["paths_size"])
        if _accepts_gzip(request):  # 存的就是压缩好的字节：原样发出去
            return Response(gz, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
        return Response(gzip.decompress(gz), media_type="application/json", headers=headers)

    @api.get("/projects/{project_id}/runs/{run_id}/uncovered")
    def uncovered(project_id: str, run_id: int, line: str | None = None) -> dict:
        """没有测试走到的结局"""
        run = run_or_404(project_id, run_id, with_paths=True)
        latest = store.latest_analysis(project_id)
        if latest is None:
            raise ApiError(404, "这个项目还没有分析结果")
        scope = run.get("scope")
        if scope is not None and (line or None) != scope["line"]:  # 只跑了一条线：别的线上"没走到"说明不了什么
            raise ApiError(409, f"运行 #{run_id} 只跑了功能线「{scope['line_title']}」的测试，别的功能线上哪些结局没走到要看"
                                "全量的运行", code="scoped", line=scope["line"])
        found = overlay.get(project_id, run_id, latest["id"])
        if found is None:
            raise ApiError(404, "分析结果或这次运行刚被清理掉了，请再取一次")
        try:
            outcomes = found.outcomes(line or None)
        except LookupError:
            raise ApiError(404, f"最新一次分析的图里没有这条功能线：{line}") from None
        return {"outcomes": [asdict(o) for o in outcomes], "stale": latest["id"] != run["analysis_id"]}

    @api.get("/projects/{project_id}/requirements")
    def requirements(project_id: str) -> dict:
        """项目里的需求和各自的对照结果"""
        project = project_or_404(project_id)
        latest = store.latest_analysis(project_id)
        if latest is None:
            raise ApiError(404, "这个项目还没有分析结果")
        graph = design_base.get(latest["id"])
        if graph is None:
            raise ApiError(404, "分析结果刚被清理掉了，请再取一次")
        run = store.latest_full_run(project_id)  # 结局的测试覆盖只看全量的运行
        missed = overlay.get(project_id, run["id"], latest["id"]) if run is not None else None
        return requirements_view(Path(project["root"]), graph, missed, run["id"] if run is not None else None)

    return api
