"""MCP 工具：pystepmap mcp 给 Claude Code 用的十五个工具，每个返回一段给 AI 读的中文文本。协议层在 mcp_server.py。

- 怎么指项目：project 可以是项目 id、名字或目录（目录、文件在某个项目里面也算）；不给时用 MCP 进程的当前目录去找包含
  它的项目，找不到时 ToolError，信息里列出已有的项目。
- 怎么指功能线：flow 是功能线 sid（或 id），或标题：完全相同的优先，其次片段，唯一才算；手工改过的标题和原来的标题
  都算，显示时用改过的。
- 读图的工具（除 list_projects、analyze_project、check）每次调用算一次过期检查（status.staleness），图过期时结果最前面
  加一段"⚠ 代码已变更……"；读不了的文件只在 project_status 里说。
- 读图的缓存：按分析 id 缓存解析好的图，最多 GRAPH_CACHE 张；每次调用先从库里取最新的分析 id（界面那边可能刚重新
  分析过），所以缓存不会过时（分析 id 不重用）。
- 添加、重新分析走 flows 里的流程（不接受分析配置：配置只能在平台界面上改），失败时把流程给的原因作为 ToolError。
- 每个工具的结果最多 MAX_CHARS 字，超出时在行边界截断，最后一行说明省略了多少、怎么缩小范围。
- 测试运行：uncovered_outcomes 和 project_status 的最后一段读平台上跑过的测试的结果（库里的 runs：计数、核对、运行
  路径），只读。没有任何工具能启动、取消测试或填测试命令——运行测试只能在平台界面上点（执行项目代码的那条路在
  runner.py，这里不导入它）。

只读被分析项目的源码（读源码片段走 sources.py 的路径检查），不导入、不执行项目代码。这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path

from pystepmap import requirements as rq
from pystepmap.diff import GraphDiff, diff_from_json, diff_graphs, diff_text
from pystepmap.entries import entry_notes
from pystepmap.model import (
    Edge,
    FlowLine,
    FunctionGraph,
    LineTask,
    Node,
    ProjectGraph,
    from_json,
)
from pystepmap.outline import (
    function_outline,
    line_mermaid,
    line_outline,
    node_ref,
    node_text,
)
from pystepmap.query import (
    PathStep,
    caught_on_chain,
    entry_chain,
    find_by_function,
    find_by_location,
    find_outcomes,
    line_functions,
    lines_of_functions,
    paths_to,
)
from pystepmap.runpath import Outcome
from pystepmap.selfcheck import NOISE_RECEIVERS, UnresolvedSite, classify_unresolved

from . import procinfo
from .flows import add_project, home_conflict, is_analyzing, reanalyze_project
from .shared import FactsCache, Uncovered, identity, uncovered_of
from .sources import SourceError, read_snippet, resolve_source_path, root_problem
from .status import staleness
from .store import Store, serve_port
from .worker import analyze_timeout, check_in_subprocess

logger = logging.getLogger(__name__)

MAX_CHARS = 40_000  # 每个工具结果的上限（含最后那行提示）
FLOW_BUDGET = MAX_CHARS - 2_000  # get_flow 的大纲超过这么多字就退到小一级的 depth（留给开头的过期提示和说明）
MAX_DEPTH = 3  # get_flow 的 depth 限制在 0～3
DEFAULT_LIMIT = 10  # paths_to_outcome 每个结局默认列几条路径
MAX_LIMIT = 30
MAX_HITS = 30  # find_nodes 最多列几处命中
MAX_OUTCOMES = 30  # paths_to_outcome 最多列几个结局
MAX_CHOICES = 20  # 没有匹配的结局时列出几个可选的
MAX_LINE_ROWS = 20  # 一处命中、一个节点最多列几条功能线
MAX_CHAINS = 3  # find_nodes 每处命中最多给几条功能线写入口链
CLOSING_RESERVE = 300  # 自己控制长度的工具给最后那行提示留的字数
# 路径是图上的连线拼出来的：两个判断之间的赋值、+=、del 不出节点，条件前后是否矛盾判断不了，所以一条都不略去
UNCAUGHT_ANSWER = "这是隐含的结局：函数里任何没被捕获的异常都从这里离开，图上不画进它的连线，所以没有路径可列。"
OPENSPEC_HOW = ("把改完之后的流程写成 OpenSpec 变更的 design.md 里的一个 ```flow 代码块：块里第一行写 `功能线：<功能线的标题>`，"
                "下面每行一个节点（`步骤：标题`、`判断：标题` 下面缩进 `是：` / `否：`、`循环：标题`、`结局（成功|跳过|失败）：标题`），"
                "标题和代码里图上节点的标题一致；入口函数成功的那条路不写 return 时（没有返回内容的接口），成功的结局写 "
                "`结局（成功）：执行到函数末尾`。写好后调 get_requirement（编号就是变更名），把它给的流程图地址交给用户看图确认；"
                "实现并 analyze_project 之后再用 get_requirement 对照。")
PATHS_CAVEAT = "下面列的是图上能走到各个结局的路径：只按连线，不判断前后条件是否互相矛盾，可能含实际走不到的组合；转述前对照源码核对。"
MAX_ITEMS = 30  # project_status 里每类问题最多列几处
MAX_BLIND = 20  # project_status 里最多列几处可能落在项目代码上的调用
MAX_FILES = 50  # project_status 里每类文件最多列几个
MAX_LINE_REFS = 10  # 过期提示里最多列几条功能线
GRAPH_CACHE = 2
FACTS_CACHE = 8
NEVER_RAN = "还没有在平台上运行过测试（运行测试只能在平台界面上点）"
# 项目锁被拿着：分析和平台上的测试运行拿同一把锁，MCP 是另一个进程，分不出是哪个
BUSY_ELSEWHERE = "这个项目正在分析或正在跑测试（平台界面上点的），稍后再试"
SNIPPET_CONTEXT = 3  # get_node 的源码片段：节点前后各几行

_FN_KIND = {"route": "接口", "task": "任务", "workflow": "工作流", "step": "步骤", "condition": "判断函数"}
_LINE_KIND = {"route": "接口", "task": "任务", "workflow": "工作流"}
_TYPE = {"start": "开始", "step": "步骤", "cond": "判断", "loop": "循环", "catch": "捕获", "end": "结局"}
_END = {"ok": "正常", "skipped": "跳过", "failed": "失败", "exception": "异常"}
_KIND_WORD = {"next": "接着", "yes": "是", "no": "否", "case": "分支", "loop_each": "每次", "loop_exit": "循环结束",
              "loop_back": "回到循环", "break": "跳出循环", "finally": "进入 finally", "raise": "出错"}
_RESOLUTION = {"exact": "精确", "guessed": "推测", "unresolved": "目标不明", "external": "项目之外"}
_DIFF_STATUS = {"added": "新增", "removed": "删除", "changed": "改动"}
_HOW = {"covers": "这一行在这个节点上", "nearest": "这一行不出节点，前面最近的节点是它", "definition": "定义",
        "call_site": "调用处"}
_RUN_STATUS = {"ok": "通过", "tests_failed": "有失败的测试", "error": "出错", "timeout": "超时", "cancelled": "已取消"}


class ToolError(Exception):
    """工具没法给出结果：信息是给 AI 看的中文（说清怎么改参数）。mcp_server 把它变成 isError 的文本结果。"""


DEFAULT_WAIT = 120  # run_line_tests 默认最多等几秒
MAX_WAIT = 600

# ---------- 工具表 ----------

_PROJECT = {"type": "string", "description": "项目：项目 id、名字或目录（目录、文件在项目里面也算）。不给时用当前目录所在的项目"}
_FLOW = {"type": "string", "description": "功能线：sid（如 line:app/orders.py::place_order），或标题（完全相同的优先，其次片段，要唯一）"}


def _spec(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


TOOL_SPECS: list[dict] = [
    _spec("list_projects",
          "列出 pystepmap 里已添加的项目：名字、id、目录、功能线条数、分析时间、代码在分析之后改过没有（图是否过期）、"
          "有没有未看的流程变化。不知道项目叫什么、或想确认当前目录在不在某个项目里时先调它。", {}),
    _spec("analyze_project",
          "分析项目：目录没添加过就添加（第一次分析），添加过就重新分析。返回分析摘要和相对上一次分析的流程变化（还有更早"
          "没看过的变化时另外说一句，用 diff_flows 看全部）。流程变化默认这样写：改动、删除的函数逐个列节点和连线；新增的"
          "函数不逐个节点列——新功能线的入口函数写一行（各类节点的个数和结局的标题），它用到的新步骤、判断函数并成一行标题，"
          "要看里面的节点用 get_flow。detail 可以改："
          "\"summary\" 每个函数都只写一行（只想确认成没成线、哪些线变了时用，最省字数），\"full\" 新增的函数也逐个节点列。"
          "改完带标记的流程代码之后调它更新图；别的工具提示\"⚠ 代码已变更\"时也调它。不接受分析配置（配置只能在平台界面上改）。",
          {"path": {"type": "string", "description": "项目目录（绝对路径，或相对当前目录）；在已添加的项目里面时重新分析那个项目"},
           "project": {**_PROJECT, "description": "已添加的项目（id、名字或目录），重新分析它；和 path 只给一个。都不给时重新分析"
                       "当前目录所在的项目；当前目录不在任何项目里时报错（不会自动添加，要添加就给 path）"},
           "name": {"type": "string", "description": "添加时的项目名（默认是目录名）；重新分析时不用"},
           "detail": {"type": "string", "enum": ["normal", "summary", "full"], "default": "normal",
                      "description": "流程变化写多细：normal（默认）新增的函数只写一行，其余逐个节点列；summary 每个函数都只写"
                                     "一行；full 全部逐个节点列"}}),
    _spec("project_status",
          "项目的自检报告：走不到的节点、没有出路的节点、目标不明 / 推测的调用、跳过的文件；以及代码在分析之后改了哪些文件、"
          "涉及哪些功能线、有没有读不了的文件。怀疑图不准、或者想知道图是否过期时用。", {"project": _PROJECT}),
    _spec("list_flows",
          "按分组列出项目的功能线（业务流程）：标题、sid、类型（接口 / 任务 / 工作流）、入口函数的文件和行号、有没有未看的"
          "流程变化。要找某个业务流程的 sid 时先用它；query 按标题、分组、sid、文件名的片段筛选。",
          {"project": _PROJECT,
           "query": {"type": "string", "description": "筛选：标题、分组、sid 或入口函数文件名的片段（不分大小写）"},
           "group": {"type": "string", "description": "只列这个分组（完整的分组名）"}}),
    _spec("get_flow",
          "取一条功能线的流程大纲：缩进文本，每个节点一行，带节点 sid 和行号，判断、循环的各条分支缩进写出，派发的任务各成"
          "一段；或者 Mermaid 流程图。改业务逻辑之前先用它看清要改的地方在流程的哪一步。功能线很长时先用默认的 depth=0 "
          "看全貌，再给 function 只看其中一个函数（行尾标着\"⇒ 可展开\"的那些）。depth 大到结果放不下时，自动退到放得下的"
          "最大深度并在开头说明。flow 和 function 至少给一个：只给 function 时看项目里任何一个函数的流程，它不在功能线上也行。",
          {"project": _PROJECT, "flow": {**_FLOW, "description": _FLOW["description"] + "；只看一个函数时可以不给"},
           "function": {"type": "string",
                        "description": "只看这一个函数的大纲（函数 sid、限定名或函数名）；给了 flow 时先在那条线上找，"
                                       "不给 flow 时在整个项目里找"},
           "depth": {"type": "integer", "minimum": 0, "maximum": MAX_DEPTH, "default": 0,
                     "description": "步骤调用的项目内函数在步骤下面展开几层（0～3，默认 0：只在行尾标\"⇒ 可展开\"）"},
           "format": {"type": "string", "enum": ["outline", "mermaid"], "default": "outline",
                      "description": "outline（缩进大纲，默认）或 mermaid（flowchart TD）"}},
          ()),
    _spec("find_nodes",
          "反查节点：给文件和行号（file + line），找这一行在哪个函数的哪个节点上、这个函数在哪些功能线上；或者给函数名"
          "（function），找它的定义和所有调用处。改某段代码之前用它弄清这段代码属于哪条流程的哪一步。",
          {"project": _PROJECT,
           "file": {"type": "string", "description": "文件：相对项目根目录的路径，或项目里的绝对路径；和 line 一起给"},
           "line": {"type": "integer", "minimum": 1, "description": "行号（从 1 起）"},
           "function": {"type": "string", "description": "函数名、限定名（Class.method）或函数 sid；不和 file、line 一起给"}}),
    _spec("paths_to_outcome",
          "在一条功能线里找结局，列出怎么走到它：从功能线入口经过哪些函数（派发、调用）到结局所在的函数，再列出函数里经过"
          "哪些判断的哪个分支（\"判断 X：是 → 判断 Y：否 → …\"）。回答\"什么情况下会失败 / 会走到某个结局\"时用。"
          "路径只按图上的连线走，不判断前后条件是否互相矛盾，可能含实际走不到的组合；转述前对照源码核对。",
          {"project": _PROJECT, "flow": _FLOW,
           "outcome": {"type": "string",
                       "description": "结局：结局节点的 sid、结局标题的片段，或 ok / skipped / failed / exception（按结局类型；"
                                      "只算图上的结局节点，隐含的\"未捕获异常退出\"不列）"},
           "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT,
                     "description": f"每个结局最多列几条路径（默认 {DEFAULT_LIMIT}，最多 {MAX_LIMIT}）"}},
          ("flow", "outcome")),
    _spec("get_node",
          "看一个节点的详情：标题、类型、文件和行号、节点前后各 3 行源码（带行号）、它的调用（目标和解析等级）、进出的连线、"
          "所在的功能线。function、node 用 get_flow、find_nodes 输出里方括号中的 sid。",
          {"project": _PROJECT,
           "function": {"type": "string", "description": "函数 sid（如 app/orders.py::place_order）"},
           "node": {"type": "string", "description": "节点 sid（如 step:1a2b3c4d、cond:…、end:…、start）"}},
          ("function", "node")),
    _spec("diff_flows",
          "最新一次分析相对对比基准的流程变化——对比基准是上次标为已看时（没标过就是第一次分析），所以是还没看过的全部"
          "变化：哪些功能线、函数、节点、连线新增 / 删除 / 改动了，带文件和行号。改完代码、调过 analyze_project 之后，用它把流程"
          "的变化写进给用户的回复。没有基准时说明原因（只有一次分析、变化已标为已看，或重新分析过流程没有变化）。",
          {"project": _PROJECT, "flow": {**_FLOW, "description": "只看这条功能线（sid 或标题）；不给时看全部"},
           "record": {"type": "integer", "minimum": 1,
                      "description": "看一条以前的变更记录（用户在平台上标为已看时存下的那次变化）的明细；编号见不带 record "
                                     "时结果末尾的\"变更记录\"列表"}}),
    _spec("check",
          "流程约束检查（SM001 编排函数只调用步骤、SM002 判断条件里不做计算、SM003 步骤不调用步骤、SM004 编排函数里不定义"
          "内部函数），列出违规：规则号、文件、行、说明。改了带 pystepkit 标记的流程代码之后用它检查。只读；path 可以是没添加过的"
          "目录（不能是用户主目录、根目录），项目里面的目录按项目的分析配置检查；在子进程里跑，超过 PYSTEPMAP_ANALYZE_TIMEOUT 秒中止。",
          {"project": _PROJECT,
           "path": {"type": "string", "description": "要检查的目录（可以是没添加过的；绝对路径或相对当前目录）；和 project 只给一个"},
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "只报告这些文件里的违规（相对检查的目录）"}}),
    _spec("uncovered_outcomes",
          "最近一次在平台上跑完的测试里，没有任何测试走到的结局：按功能线分组，每个带结局节点 sid、所在函数、文件和行号；"
          "运行之后代码改过、对不上的结局单独列出。想补测试、或者判断某个分支是不是根本没被测到时用它。只读：结果来自用户"
          "在平台界面上启动的那次测试，本工具不跑测试；最近的结果比最新的分析旧时开头会提醒。",
          {"project": _PROJECT, "flow": {**_FLOW, "description": "只看这条功能线（sid 或标题）；不给时看全部"}}),
    _spec("list_requirements",
          "列出项目里的业务需求：项目目录下 .pystepmap/requirements/ 里的文件，以及项目用 OpenSpec 时 OpenSpec 文件"
          "（openspec/changes/<变更>/ 和 openspec/specs/ 下的 Markdown）里的 ```flow 代码块。每条有编号、标题、状态、涉及的"
          "功能线，以及设计的流程和代码现在的流程对不对得上。接到新需求、或者想知道某条需求做到哪一步时先调它。只读。",
          {"project": _PROJECT}),
    _spec("get_requirement",
          "读一条需求并对照：说明、设计的流程（缩进文本）、这份设计画成图的平台地址（给用户看图用）、和代码现在的流程的差异（设计里有实现里没有的、实现里多出来的、"
          "结局分类不一样的、走向不一致的）、设计的每个结局有没有测试走到、按对照结果现在够得上哪个状态。动手实现之前读它；"
          "实现并调过 analyze_project 之后再读一次，把差异告诉用户。只读。",
          {"project": _PROJECT,
           "requirement": {"type": "string",
                           "description": "需求：编号（如 R003；OpenSpec 里的是变更名，如 add-manual-review），或标题的片段（要唯一）"}},
          ("requirement",)),
    _spec("save_requirement",
          "新建或整条改写一条需求，**写进项目目录**（.pystepmap/requirements/<编号>-<标题>.md；这是 pystepmap 的 MCP 里唯一写"
          "项目文件的工具）。项目用 OpenSpec 管需求（有 openspec/ 目录）时不要用它：把设计的流程写成 OpenSpec 变更的 design.md 里"
          "的一个 ```flow 代码块（块里第一行 `功能线：<功能线的标题>`，下面的写法相同），list_requirements / get_requirement "
          "直接读它。不用 OpenSpec、接到业务需求时：先用 get_flow 看现在的流程，把改完之后的流程写成 flow，状态用\"草稿\"保存，"
          "给用户看；用户确认后再用同一个编号保存成\"已确认\"，然后才写代码。flow 的写法：每行一个节点，缩进表示从属——"
          "`步骤：标题`（下面可以缩进 `失败时：` 再缩进写出错之后的内容）、`判断：标题`（下面缩进 `是：` / `否：`，各自再"
          "缩进写内容）、`循环：标题`（下面缩进写循环体）、`结局（成功|跳过|失败）：标题`。标题就是图上节点的中文标题："
          "步骤是 @step 的 title，判断是 @condition 的 title，结局是 outcome 的 title；入口函数成功的那条路不写 return 时"
          "（没有返回内容的接口，比如 204 的删除），成功的结局写 `结局（成功）：执行到函数末尾`。返回保存的文件、这份设计"
          "画成图的平台地址（把它给用户：用户看图确认，不要只贴缩进文本）和对照结果。",
          {"project": _PROJECT,
           "title": {"type": "string", "description": "需求的标题（一句话）"},
           "flow": {"type": "string", "description": "设计的流程（缩进文本，写法见工具说明）"},
           "line": {"type": "string", "description": "涉及的功能线：标题或 sid；要新建的功能线写打算用的标题（@workflow 的 title）"},
           "description": {"type": "string", "description": "业务上要什么、为什么（几句话）"},
           "status": {"type": "string", "enum": ["草稿", "已确认", "已实现", "已完成"], "default": "草稿",
                      "description": "状态：草稿（等用户确认）、已确认、已实现（对照没有差异）、已完成（并且每个结局有测试走到）"},
           "id": {"type": "string", "description": "要改写的需求的编号（如 R003）；新建时不给，编号自动接着排"}},
          ("title", "flow")),
    _spec("run_line_tests",
          "请正在运行的 pystepmap 平台（pystepmap serve）**运行一条功能线的测试**，等它跑完，返回结果：通过 / 失败的测试、"
          "图和实际执行是否一致、这条线上还有哪些结局没有被这些测试走到。只跑上一次全量运行里走过这条线的那些测试，用的是"
          "用户在平台界面上填好并确认过的测试命令；会真的执行项目的测试（可能访问开发数据库等外部服务）。只有用户在平台的"
          "测试命令设置里打开了\"允许 AI 运行单条功能线的测试\"的项目才能用；不能运行全部测试、不能改或确认测试命令。"
          "实现或修改一条功能线之后，用它验证。平台没在运行、项目没开这个开关、还没有跑过全量测试时会说明原因。"
          "start 为 false 时不启动，只看这条线最近一次的结果（或正在跑的进度）。",
          {"project": _PROJECT, "flow": _FLOW,
           "start": {"type": "boolean", "default": True, "description": "true（默认）启动一次运行；false 只看最近一次的结果"},
           "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT, "default": DEFAULT_WAIT,
                            "description": f"最多等多少秒（默认 {DEFAULT_WAIT}，最多 {MAX_WAIT}）；到时间还没跑完就先返回进度，"
                                           "之后用 start=false 再看"}},
          ("flow",)),
]

_SPECS = {s["name"]: s for s in TOOL_SPECS}


# ---------- 文本的小工具 ----------

def _cap(text: str, hint: str) -> str:
    """结果不超过 MAX_CHARS 字：超出时在行边界截断，最后一行说省略了多少、怎么缩小范围。"""
    if len(text) <= MAX_CHARS:
        return text
    budget = MAX_CHARS - len(hint) - 120  # 留给最后那行
    cut = text.rfind("\n", 0, budget + 1)
    if cut <= 0:  # 一行就超了
        cut = budget
    kept = text[:cut]
    rest = text[cut:].lstrip("\n")
    rows = rest.count("\n") + 1 if rest else 0
    tail = f"…（输出超过 {MAX_CHARS} 字，后面 {rows} 行、{len(rest)} 字没有列出。{hint}）"
    return f"{kept}\n{tail}"


def _capped(hint: str) -> Callable:
    """工具方法的结果按 _cap 截断；hint 说怎么缩小范围。"""
    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        @wraps(fn)
        def inner(self: Tools, **kwargs: object) -> str:
            return _cap(fn(self, **kwargs), hint)
        return inner
    return wrap


def _when(iso: str | None) -> str:
    """库里的 UTC 时间写成本机时间。"""
    if not iso:
        return "时间不明"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def fn_label(fg: FunctionGraph, *, titled: bool = False) -> str:
    """函数一行：种类 限定名（路由）  文件:def 行  [函数 sid]——和大纲的函数头同样的写法，种类多分出步骤、判断函数。
    titled 时限定名后面再写「函数的标题」（标题就是函数名时不写）。"""
    name = fg.qualname + (f"（{fg.route}）" if fg.route else "")
    title = fg.title_full or fg.title
    if titled and title and title not in (fg.qualname, fg.qualname.rsplit(".", 1)[-1]):
        name += f"「{title}」"
    return f"{_FN_KIND.get(fg.kind, '函数')} {name}  {fg.file}:{fg.line}  [{fg.sid or fg.key}]"


def node_line(fg: FunctionGraph, node: Node) -> str:
    """节点一行：[节点 sid] L行号 文字——和大纲的节点行同样的写法。"""
    return f"{node_ref(fg, node)} " + (f"L{node.line} " if node.line else "") + node_text(node)


def _no_line(cmp: rq.Comparison) -> rq.Difference | None:
    """对照时图上找不到这条需求的功能线（还没有实现，或者名字对不上）：那一条说明；找到了时 None。"""
    return next((d for d in cmp.differences if d.kind == "no_line"), None)


def _verdict(cmp: rq.Comparison) -> str:
    """一条需求的对照结果一句话，和界面上的标记同一个口径：对得上 / 找不到功能线 / 有几处不一致。找不到功能线时
    设计的每个节点都记了一条"还没有实现"（Comparison.differences），不把它们数成"几处不一致"。"""
    if cmp.matches:
        return "设计和实现对得上"
    no_line = _no_line(cmp)
    if no_line is not None:
        n = len(cmp.differences) - 1
        return no_line.text + (f"；设计的 {n} 个节点都还没有实现" if n else "")
    return f"和实现有 {len(cmp.differences)} 处不一致"


def _summary_text(s: dict) -> str:
    blind = s.get("unresolved_blind")  # 旧的分析没有记这一项：只写总数
    text = (f"分析摘要：函数 {s.get('functions', 0)} 个，功能线 {s.get('lines', 0)} 条，公共任务 {s.get('common_tasks', 0)} 个；"
            f"推测的调用 {s.get('guessed_calls', 0)} 处，目标不明的调用 {s.get('unresolved_calls', 0)} 处"
            + (f"（可能落在项目代码上的 {blind} 处）" if blind is not None else "") + "；"
            f"跳过的文件 {len(s.get('skipped_files') or [])} 个；走不到的节点 {s.get('unreachable', 0)} 个，"
            f"没有出路的节点 {s.get('dead_ends', 0)} 个；用时 {s.get('seconds', 0)} 秒。")
    notes = s.get("entry_notes")
    text += "".join(f"\n{n}" for n in s.get("unlisted_notes") or [])
    text += "".join(f"\n⚠ {n}" for n in (s.get("mark_notes") or [])[:MAX_ITEMS])
    if notes:
        text += "".join(f"\n{n}" for n in notes)
    elif notes is None and s.get("has_entries") is False:  # 旧的分析没有记 entry_notes
        text += "\n没有识别到入口（路由、任务、工作流）：功能线是空的；可以在平台界面上改分析配置（extra_entries）。"
    return text


def _edge_word(src: Node, e: Edge | PathStep) -> str:
    """一条连线怎么走：判断节点的分支写 是 / 否 / `模式` / 都不匹配（continue、break 另注），其余按 kind。"""
    if src.type != "cond" or e.kind == "raise":
        return _KIND_WORD.get(e.kind, e.kind)
    which = e.branch or {"yes": "yes", "no": "no"}.get(e.kind, "")
    if which in ("yes", "no"):
        word = "是" if which == "yes" else "否"
    elif which == "else" or (not which and e.label == "都不匹配"):
        word = "都不匹配"
    elif which or e.label:
        word = f"`{e.label or which}`"
    else:
        return _KIND_WORD.get(e.kind, e.kind)
    if e.kind in ("break", "loop_back", "finally"):
        word += f"（{_KIND_WORD[e.kind]}）"
    return word


def _path_text(fg: FunctionGraph, steps: list[PathStep]) -> str:
    """函数里的一条路径写成"判断 X：是 → 循环 Y：每次 → … → 结局"：只写判断、循环、捕获、出错和跳转，顺序走过的步骤不写。"""
    nodes = {n.id: n for n in fg.nodes}
    words: list[str] = []
    for s in steps[:-1]:
        n = nodes[s.node]
        if s.kind == "raise":
            words.append(f"{node_text(n)} 出错")
        elif n.type in ("cond", "loop"):
            words.append(f"{node_text(n)}：{_edge_word(n, s)}")
        elif n.type == "catch":
            words.append(node_text(n))
        elif s.kind in ("break", "finally") or (s.kind == "loop_back" and (s.branch or s.label)):
            words.append(_KIND_WORD[s.kind])
    end = node_text(nodes[steps[-1].node])
    return " → ".join(words + [end]) if words else f"不经过任何判断 → {end}"


def _size(rows: list[str]) -> int:
    """这些行接在已有的行后面（每行前面一个换行符）占多少字。"""
    return sum(len(r) + 1 for r in rows)


def _fit(rows: list[str], room: int) -> list[str]:
    """从前往后留下放得进 room 字的行；一行就放不下时截这一行。"""
    out: list[str] = []
    for r in rows:
        if _size(out) + len(r) + 1 > room:
            left = room - _size(out) - 1
            if left > 0:
                out.append(r[:left])
            break
        out.append(r)
    return out


def _names_root(g: ProjectGraph, line: FlowLine, name: str) -> bool:
    """name 是这条线根函数的名字、限定名、函数 sid 或 key。"""
    fg = g.functions.get(line.root.function)
    return fg is not None and name in (fg.qualname, fg.qualname.rsplit(".", 1)[-1], fg.sid, fg.key)


def _tasks(task: LineTask) -> list[str]:
    """派发树上的任务 key，先序，不重复。"""
    out: dict[str, None] = {}
    stack = [task]
    while stack:
        t = stack.pop()
        out[t.function] = None
        stack.extend(reversed(t.children))
    return list(out)


def _containing(projects: list[dict], path: Path) -> dict | None:
    """path（解析过的绝对路径）所在的项目：就是项目根目录或在它里面。先按路径字符串比；比不上时按 inode 比（大小写
    不敏感的文件系统上写法不同的同一个目录），这时根目录已经不是添加时那个真实目录的项目不参与（顺着换上的链接比会
    误判，见 flows._overlap_with）。项目之间不会嵌套，最多一个。"""
    text = str(path)
    for p in projects:
        root = p["root"]
        if text == root or text.startswith(root.rstrip(os.sep) + os.sep):
            return p
    ids = {i for i in (identity(path), *(identity(x) for x in path.parents)) if i is not None}
    for p in projects:
        if root_problem(p["root"]) is None and identity(p["root"]) in ids:
            return p
    return None


# ---------- 一次调用读到的东西 ----------

@dataclass
class _View:
    """读图的工具一次调用用到的：项目、最新一次分析、图、手工改的标题分组、过期检查的结果（每次调用只算一次）。"""

    project: dict
    analysis: dict
    graph: ProjectGraph
    overrides: dict[str, dict]
    stale: dict

    def sid(self, line: FlowLine) -> str:
        return line.sid or line.id

    def title(self, line: FlowLine) -> str:
        return self.overrides.get(self.sid(line), {}).get("title") or line.title_full or line.title

    def group(self, line: FlowLine) -> str:
        return self.overrides.get(self.sid(line), {}).get("group") or line.group

    def titles(self, line: FlowLine) -> list[str]:
        """这条线认的标题：改过的、原来的完整标题、原来的显示标题。"""
        return list(dict.fromkeys(t for t in (self.title(line), line.title_full, line.title) if t))

    def ref(self, line: FlowLine) -> str:
        return f"{self.title(line)} [{self.sid(line)}]"

    def row(self, line: FlowLine) -> str:
        return f"{self.title(line)}  [{self.sid(line)}]  分组：{self.group(line)}"

    def by_sid(self, sid: str) -> FlowLine | None:
        return next((ln for ln in self.graph.lines if self.sid(ln) == sid), None)

    def by_ids(self, ids: list[str] | tuple[str, ...]) -> list[FlowLine]:
        """功能线 id（根函数 key，可能重复）对应的线，按图里的顺序，不重复。"""
        wanted = set(ids)
        return [ln for ln in self.graph.lines if ln.id in wanted]


class Tools:
    """pystepmap mcp 的十五个工具。store 是平台的库（和 pystepmap serve 共用一个数据目录）；cwd 是 MCP 进程的当前目录（不给
    project 时用它找项目）。每个工具一个方法，参数全是关键字参数，返回给 AI 读的文本；没法给出结果时抛 ToolError。
    可以在多个线程里同时调用（缓存有锁）。"""

    def __init__(self, store: Store, cwd: Path | None):
        self.store = store
        self.cwd = Path(os.path.realpath(cwd)) if cwd is not None else None
        self._graphs: OrderedDict[int, ProjectGraph] = OrderedDict()
        self._facts_cache = FactsCache(store, FACTS_CACHE)
        self._uncovered: OrderedDict[tuple[int, int], Uncovered] = OrderedDict()  # （分析，运行）→ 算好的结果
        self._lock = threading.Lock()
        self._analysing: set[str] = set()  # 这个进程里正在重新分析的项目 id

    # ---------- 按名字调用 ----------

    def call(self, name: str, arguments: dict | None) -> str:
        """按工具名调用，先按 TOOL_SPECS 检查参数（不认识的、缺的、类型不对的都是 ToolError）。空字符串等于没给。"""
        spec = _SPECS.get(name)
        if spec is None:
            raise ToolError(f"没有这个工具：{name}（有：{'、'.join(_SPECS)}）")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise ToolError("参数应该是一个 JSON 对象")
        props = spec["inputSchema"]["properties"]
        unknown = sorted(set(arguments) - set(props))
        if unknown:
            raise ToolError(f"不认识的参数：{'、'.join(unknown)}（{name} 的参数：{'、'.join(props) or '无'}）")
        kwargs = {}
        for key, value in arguments.items():
            value = _coerce(key, props[key], value)
            if value is not None:
                kwargs[key] = value
        missing = [k for k in spec["inputSchema"].get("required", []) if k not in kwargs]
        if missing:
            raise ToolError(f"缺少参数：{'、'.join(missing)}")
        # 这个进程跑的是旧代码时（磁盘上的代码在启动之后更新过），每个工具的结果最前面都说一句：分析在子进程里跑、用的是
        # 新代码，光看分析的引擎标识看不出这个进程是旧的
        old = procinfo.stale_warning()
        return (old + "\n\n" if old else "") + getattr(self, name)(**kwargs)

    # ---------- 项目、图 ----------

    def _abs(self, text: str, *, resolve: bool = True) -> Path | None:
        """用户给的路径：展开 ~，相对路径按当前目录算；resolve 时再解析符号链接。不合法时 None。"""
        if "\0" in text:
            return None
        try:
            path = Path(text).expanduser()
            if not path.is_absolute():
                if self.cwd is None:
                    return None
                path = self.cwd / path
            return Path(os.path.realpath(path)) if resolve else Path(os.path.normpath(path))
        except (OSError, ValueError, RuntimeError):
            return None

    def _known(self, projects: list[dict]) -> str:
        if not projects:
            return "还没有添加任何项目：用 analyze_project（path 给项目目录）添加。"
        rows = [f"- {p['name']}  [id {p['id']}]  {p['root']}" for p in projects]
        return "已添加的项目：\n" + "\n".join(rows) + \
            "\n用 project 参数指定（id、名字或目录）；要添加新项目，用 analyze_project 给出目录（path）。"

    def _project(self, ref: str | None, projects: list[dict] | None = None) -> dict:
        """按 id、名字、目录（或当前目录）找项目，返回 Store.list_projects 的一项（带最新一次分析的信息）。"""
        projects = self.store.list_projects() if projects is None else projects
        if ref is None:
            found = _containing(projects, self.cwd) if self.cwd is not None else None
            if found is None:
                raise ToolError(f"当前目录（{self.cwd}）不在任何已添加的项目里。{self._known(projects)}")
            return found
        for p in projects:
            if p["id"] == ref:
                return p
        named = [p for p in projects if p["name"] == ref]
        if len(named) == 1:
            return named[0]
        if named:
            rows = "\n".join(f"- {p['name']}  [id {p['id']}]  {p['root']}" for p in named)
            raise ToolError(f"有 {len(named)} 个项目都叫「{ref}」，请用 id 指定：\n{rows}")
        path = self._abs(ref)
        # 路径要真的存在：写错的名字（"abacuss"）按相对路径算总落在当前目录的项目里，不能悄悄用它
        found = _containing(projects, path) if path is not None and path.exists() else None
        if found is None:
            raise ToolError(f"找不到项目「{ref}」：不是项目的 id、名字，也不在哪个项目的目录里。{self._known(projects)}")
        return found

    def _graph(self, analysis_id: int) -> ProjectGraph | None:
        with self._lock:
            if analysis_id in self._graphs:
                self._graphs.move_to_end(analysis_id)
                return self._graphs[analysis_id]
        text = self.store.graph_json(analysis_id)
        if text is None:  # 刚被清理掉了
            return None
        graph = from_json(text)
        with self._lock:
            self._graphs[analysis_id] = graph
            while len(self._graphs) > GRAPH_CACHE:
                self._graphs.popitem(last=False)
        return graph

    def _staleness(self, project: dict, analysis_id: int, graph: ProjectGraph | None = None) -> dict:
        if graph is None:
            with self._lock:
                graph = self._graphs.get(analysis_id)  # 已经解析过的图：不用再解析一次图 JSON
        facts = self._facts_cache.get(analysis_id, graph)
        if facts is None:
            return {"checked": False, "reason": "这次分析刚被清理掉了"}
        config = facts.config if facts.config is not None else project["config"]
        return staleness(project["root"], config, facts.hashes, facts.file_lines)

    def _view(self, ref: str | None) -> _View:
        p = self._project(ref)
        analysis = p["analysis"]
        for _ in range(3):  # 最新一次在取图之前被别的进程清理掉了（极少见）：重新取最新的
            if analysis is None:
                raise ToolError(f"项目「{p['name']}」还没有分析结果：先调用 analyze_project")
            graph = self._graph(analysis["id"])
            if graph is not None:
                break
            analysis = self.store.latest_analysis(p["id"])
        else:
            raise ToolError("分析结果刚被更新，请再调用一次")
        return _View(p, analysis, graph, self.store.get_overrides(p["id"]),
                     self._staleness(p, analysis["id"], graph))

    def _warning(self, v: _View) -> str:
        """读图的工具结果最前面的一段：图过期时说改了几个文件、涉及哪些功能线；没法检查时说原因。"""
        st = v.stale
        if not st.get("checked"):
            return f"⚠ 没法检查代码在分析之后有没有改过：{st.get('reason', '')}\n\n"
        if not st["stale"]:
            return ""
        t = st["total"]
        sids = st["lines"]
        if sids is None:
            involved = "不清楚（这次分析是旧版本存的，没有记下功能线涉及的文件）"
        elif not sids:
            involved = "无（改动的文件不在任何功能线上）"
        else:
            refs = [v.ref(ln) if (ln := v.by_sid(s)) is not None else s for s in sids[:MAX_LINE_REFS]]
            involved = "、".join(refs) + (f" 等 {len(sids)} 条" if len(sids) > MAX_LINE_REFS else "")
        return (f"⚠ 代码已变更（改 {t['changed']}、增 {t['added']}、删 {t['removed']} 个文件），图可能过期。"
                f"涉及功能线：{involved}。先调用 analyze_project 重新分析。\n\n")

    def _line(self, v: _View, flow: str) -> FlowLine:
        """按顺序找功能线，要唯一：sid（或 id）→ 完全相同的标题 → 根函数（名字、限定名、函数 sid，或 sid 的结尾一段，
        如 create_job、finetune.py::create_job）→ 标题的片段。都没有时 ToolError，列出 sid 里含它的功能线。"""
        lines = v.graph.lines
        needle = flow.casefold()
        rules = (lambda ln: flow in (v.sid(ln), ln.id),
                 lambda ln: flow in v.titles(ln),
                 lambda ln: _names_root(v.graph, ln, flow) or any(v.sid(ln).endswith(sep + flow) for sep in ("/", ":")),
                 lambda ln: any(needle in t.casefold() for t in v.titles(ln)))
        hits: list[FlowLine] = []
        for rule in rules:
            hits = [ln for ln in lines if rule(ln)]
            if hits:
                break
        if len(hits) == 1:
            return hits[0]
        if not hits:
            near = [ln for ln in lines if needle in v.sid(ln).casefold()]
            listed = "".join(f"\n- {v.row(ln)}" for ln in near[:MAX_HITS])
            more = f"\n…还有 {len(near) - MAX_HITS} 条" if len(near) > MAX_HITS else ""
            hint = f"sid 里含「{flow}」的功能线：{listed}{more}" if near else "用 list_flows 看有哪些（可以用 query 筛选）。"
            raise ToolError(f"没有这条功能线：「{flow}」（不是 sid、标题、标题的片段，也不是功能线入口函数的名字）。{hint}")
        rows = "\n".join(f"- {v.row(ln)}" for ln in hits[:MAX_HITS])
        more = f"\n…还有 {len(hits) - MAX_HITS} 条" if len(hits) > MAX_HITS else ""
        raise ToolError(f"「{flow}」对应 {len(hits)} 条功能线，请用 sid 指定：\n{rows}{more}")

    def _line_rows(self, v: _View, ids: list[str] | tuple[str, ...], indent: str, chain_to: str | None = None) -> list[str]:
        """功能线一行一条，最多 MAX_LINE_ROWS 条；chain_to（函数 key）给出时前 MAX_CHAINS 条线下面各写一行从入口到这个
        函数的调用链（get_settings 这种到处用的函数，每条线一行链会有几万字）。"""
        lines = v.by_ids(ids)
        if not lines:
            return [f"{indent}不在任何功能线上（没有入口能走到这个函数）"]
        out = []
        for i, ln in enumerate(lines[:MAX_LINE_ROWS]):
            out.append(f"{indent}- {v.row(ln)}")
            if chain_to is not None and i < MAX_CHAINS:
                out.append(f"{indent}  入口链：{_chain_text(v.graph, ln, chain_to)}")
        if len(lines) > MAX_LINE_ROWS:
            out.append(f"{indent}…还有 {len(lines) - MAX_LINE_ROWS} 条功能线（用 list_flows / get_flow 看）")
        if chain_to is not None:
            out.append(f"{indent}（入口链是从入口到这里最短的一条，可能还有别的走法：对链上的函数用 find_nodes 的 function "
                       "看它的全部调用、派发处）")
        if chain_to is not None and len(lines) > MAX_CHAINS:
            out.append(f"{indent}（入口链只写了前 {MAX_CHAINS} 条功能线的，别的用 get_flow 看）")
        return out

    # ---------- list_projects ----------

    @_capped("项目太多：用 project 参数直接指定要用的项目")
    def list_projects(self) -> str:
        """列出已添加的项目"""
        projects = self.store.list_projects()
        if not projects:
            return "还没有添加项目。用 analyze_project（path 给项目目录）添加一个。"
        here = _containing(projects, self.cwd) if self.cwd is not None else None
        out = [f"共 {len(projects)} 个项目："]
        for p in projects:
            mark = "（当前目录在这个项目里）" if here is not None and here["id"] == p["id"] else ""
            out.append(f"- {p['name']}  [id {p['id']}]{mark}  目录 {p['root']}")
            a = p["analysis"]
            if a is None:
                out.append("  还没有分析结果")
                continue
            s = a["summary"]
            out.append(f"  功能线 {s.get('lines', 0)} 条，函数 {s.get('functions', 0)} 个；"
                       f"分析于 {_when(a['created_at'])}（分析 #{a['id']}）")
            st = self._staleness(p, a["id"])
            if not st.get("checked"):
                out.append(f"  没法检查是否过期：{st.get('reason', '')}")
            elif st["stale"]:
                t = st["total"]
                out.append(f"  已过期（改 {t['changed']}、增 {t['added']}、删 {t['removed']} 个文件）：先调用 analyze_project 重新分析")
            else:
                out.append("  未过期：代码在分析之后没有改过")
            out.append("  有未看的流程变化（diff_flows 查看）" if a["has_diff"] else "  没有未看的流程变化")
            if is_analyzing(self.store, p["id"]):
                out.append("  正在分析或正在跑测试（平台界面上点的）")
        return "\n".join(out)

    # ---------- analyze_project ----------

    @_capped("流程变化太多：用 diff_flows 的 flow 参数一条一条看")
    def analyze_project(self, *, path: str | None = None, project: str | None = None, name: str | None = None,
                        detail: str = "normal") -> str:
        """分析（或重新分析）一个项目"""
        if detail not in ("normal", "summary", "full"):
            raise ToolError(f"detail 要是 normal、summary、full 之一，不是 {detail!r}")
        if path is not None and project is not None:
            raise ToolError("path 和 project 只给一个：path 是目录（没添加过就添加），project 是已添加的项目（重新分析）")
        projects = self.store.list_projects()
        if project is not None:
            return self._reanalyze(self._project(project, projects), name, detail)
        where = self._abs(path) if path is not None else self.cwd
        if where is None:
            raise ToolError(f"目录路径不合法：{path}" if path is not None else "不知道当前目录：请用 path 给出项目目录")
        target = _containing(projects, where)
        if target is None and path is None:  # 不把当前目录悄悄添加成项目：要添加就明说 path
            raise ToolError(f"没有指定要分析的目录：给 path（要添加的代码目录）或 project（已添加的项目）。"
                            f"当前目录（{where}）不在任何已添加的项目里。{self._known(projects)}")
        if target is not None:
            out = self._reanalyze(target, name, detail)
            if path is not None and str(where) != target["root"]:
                out += f"\n（{path} 在项目「{target['name']}」里面，重新分析的是整个项目 {target['root']}）"
            return out
        return self._add(str(self._abs(path, resolve=False)), name)

    def _add(self, root: str, name: str | None) -> str:
        result = add_project(self.store, root, name, None)
        if result["kind"] != "ok":
            raise ToolError(result["reason"])
        p = self.store.get_project(result["project_id"]) or {"name": name or root, "id": result["project_id"], "root": root}
        return "\n".join([
            f"已添加项目「{p['name']}」[id {p['id']}]，目录 {p['root']}（分析 #{result['analysis_id']}）。",
            _summary_text(result["summary"]),
            "这是第一次分析，没有可比的上一次。用 list_flows 看它的功能线。"])

    def _reanalyze(self, p: dict, name: str | None, detail: str = "normal") -> str:
        with self._lock:  # 客户端取消了的 analyze_project 还在后台跑（工作线程不能中途停）：是自己这次没结束
            mine = p["id"] in self._analysing
            self._analysing.add(p["id"])
        if mine:
            raise ToolError("这个项目正在分析（上一次 analyze_project 调用还没结束），稍后再试")
        try:
            before = self.store.latest_analysis(p["id"])
            before_extra = {} if before is None else (self.store.analysis_extra(before["id"]) or {})
            before_sha, before_diff = before_extra.get("graph_sha"), before_extra.get("diff")
            result = reanalyze_project(self.store, p["id"])
        finally:
            with self._lock:
                self._analysing.discard(p["id"])
        if result["kind"] != "ok":
            if result.get("code") == "busy":
                raise ToolError(BUSY_ELSEWHERE)
            raise ToolError(result["reason"])
        aid = result["analysis_id"]
        extra = self.store.analysis_extra(aid) or {}
        head = f"已重新分析项目「{p['name']}」[id {p['id']}]（分析 #{aid}）。"
        if name:
            head += "（name 只在添加项目时用，这次是重新分析，没有改名）"
        out = [head, _summary_text(result["summary"])]
        out += _newly_skipped(before, result["summary"])
        if before is None:
            out.append("相对上一次：没有上一次分析。")
            return "\n".join(out)
        same = bool(before_sha) and before_sha == extra.get("graph_sha")
        delta = None if same else self._delta(before["id"], aid)
        earlier = before_diff is not None  # 上一次分析时就有没看的变化（对比基准只在标为已看时移动，它们还在）
        if not same and delta is None:
            out.append("相对上一次：这次没能和上一次比较（比较时出错，日志在标准错误里），流程有没有变化说不准；"
                       "这次的分析照常存好了。")
        elif same or delta.empty:
            why = "和上一次分析出的图完全相同" if same else "改的是格式、注释或流程以外的代码"
            out.append(f"相对上一次：流程没有变化（{why}）。")
            if extra.get("diff") is not None:
                out.append("之前还有没看的流程变化：用 diff_flows 查看。")
        else:
            brief = {"summary": "all", "full": ""}.get(detail, "added")
            out += ["相对上一次分析的流程变化：",
                    diff_text(_with_overrides(delta, self.store.get_overrides(p["id"])), brief=brief,
                              engines=(before["engine_version"], (self.store.latest_analysis(p["id"]) or before)["engine_version"]))]
            hidden = [fc for fc in delta.functions.values() if fc.status != "removed" and (brief == "all" or fc.status == "added")]
            if brief and hidden:
                out.append(f"（{'每个函数' if brief == 'all' else '新增的函数'}只写了一行：要看节点用 get_flow，要看逐个节点的变化用 "
                           "diff_flows 或 detail=\"full\"）")
            if extra.get("diff") is None and extra.get("base_id") is not None:  # 改回去了：和对比基准又一样
                out.append("流程又和对比基准一样了，没有未看的变化（这次把上次标为已看以来、没标过就是第一次分析以来的"
                           "改动又改回去了）。")
            elif extra.get("diff") is None:  # 有变化却既没存下差异也没有基准：保存时的比较出错了（日志里有）
                out.append("这次没能和上一次比较并存下流程差异（比较时出错，日志在标准错误里）：平台界面和 diff_flows 不会"
                           "标出上面这些变化。")
            if earlier and extra.get("diff") is not None:
                out.append("另有更早的未看变化，用 diff_flows 看全部（上次标为已看以来、没标过就是第一次分析以来的变化）。")
        return "\n".join(out)

    def _delta(self, old_id: int, new_id: int) -> GraphDiff | None:
        """上一次分析到这次的流程差异（当场算，不用存着的：存着的是相对对比基准的，可能比上一次更早）。读不出图、
        比较出错时 None。"""
        try:
            old, new = self._graph(old_id), self._graph(new_id)
            return None if old is None or new is None else diff_graphs(old, new)
        except Exception:
            logger.exception("重新比较分析 %s 和 %s 出错", old_id, new_id)
            return None

    # ---------- project_status ----------

    @_capped("内容太多：用 find_nodes、get_node 查具体的位置")
    def project_status(self, *, project: str | None = None) -> str:
        """项目的自检报告和是否过期"""
        v = self._view(project)
        g, sc, a = v.graph, v.graph.selfcheck, v.analysis
        out = [f"项目「{v.project['name']}」[id {v.project['id']}]  目录 {v.project['root']}",
               (f"最新一次分析：#{a['id']}，{_when(a['created_at'])}，引擎 {a['engine_version']}；函数 {len(g.functions)} 个，"
                f"功能线 {len(g.lines)} 条，公共任务 {len(g.common_tasks)} 个"),
               procinfo.process_line(), "", "## 自检"]
        out += self._node_problems("走不到的节点", sc.unreachable, g)
        out += self._node_problems("没有出路的节点", sc.dead_ends, g)
        out += self._unresolved(v, sc.unresolved_calls)
        out += self._guessed(g, sc.guessed_calls)
        out.append(f"跳过的文件：{len(sc.skipped_files)} 个" + ("（解析不了，图里没有它们）" if sc.skipped_files else ""))
        out += [f"  {f}：{why}" for f, why in sc.skipped_files[:MAX_FILES]]
        out += entry_notes(g)
        marks = (a.get("summary") or {}).get("mark_notes") or []
        out += [f"标记里读不出来的参数：{len(marks)} 处", *(f"  {n}" for n in marks[:MAX_ITEMS])] if marks else []
        out += ["", "## 代码在分析之后有没有改过"] + self._stale_details(v)
        out += ["", "## 流程变化",
                "有未看的流程变化：用 diff_flows 查看" if a["has_diff"] else "没有未看的流程变化"]
        out += ["", "## 测试运行", *self._run_details(v)]
        return self._warning(v) + "\n".join(out)

    def _run_details(self, v: _View) -> list[str]:
        """project_status 的最后一段：最近一次**全量**运行的时间、状态、用例数、核对三项、没走到的结局个数（和
        uncovered_outcomes 同一个口径：只跑一条功能线的运行走不到别的线，拿它数"没走到的结局"会多出一大截）。它之后
        还有只跑一条线的运行时另说一句；只有单条线的运行、没有全量的时，只报那次运行，不报结局个数。"""
        runs = self.store.list_runs(v.project["id"])
        if not runs:
            return [NEVER_RAN]
        latest = runs[0]
        run = next((r for r in runs if r.get("scope") is None), None)  # 没跑完的全量运行也算：状态要如实报
        if run is None:
            scope = latest.get("scope") or {}
            return [f"还没有全量的测试运行。最近一次只跑了功能线「{scope.get('line_title', '')}」的测试：{_run_head(latest)}",
                    "没有测试走到的结局要按全量运行算：在平台上运行全部测试之后再看。"]
        s = run["summary"]
        out = [f"最近一次全量运行：{_run_head(run)}"]
        if latest["id"] != run["id"]:
            scope = latest.get("scope") or {}
            out.append(f"（之后还有只跑功能线「{scope.get('line_title', '')}」的运行 {_run_head(latest)}——它只回答那条线，"
                       "下面的核对和结局个数不按它算）")
        if run["analysis_id"] != v.analysis["id"]:
            out.append(f"⚠ 这次运行用的是分析 #{run['analysis_id']}，之后重新分析过（现在是 #{v.analysis['id']}）：下面的核对和"
                       "结局个数都是按那次的图算的，不是现在的图；之后改动的代码有没有测试走到，要在平台上重新运行测试才知道。")
        verify = s.get("verify")
        if verify is None or verify.get("ok") is None:  # 没跑完、记录读不了、一条记录都没收到：什么都没核对
            out.append("核对：没有（测试没有跑完，或者没有收到记录）")
        else:
            m = verify.get("mismatches", {})
            out.append(f"核对：连线不符 {m.get('edge', 0)}、结局不符 {m.get('end', 0)}、调用不符 {m.get('call', 0)}"
                       + ("" if verify.get("ok") else "（不符的位置在平台界面的运行详情里）"))
            if verify.get("stale"):
                out.append(f"运行期间这些文件改过，路径可能对不上图：{'、'.join(verify['stale'])}")
        c = s.get("coverage")
        if c:
            left = c["outcomes"] - c["outcomes_covered"] - c.get("outcomes_uncertain", 0)
            out.append(f"没有测试走到的结局 {left} 个（共 {c['outcomes']} 个；按那次运行用的图算，uncovered_outcomes 查看）")
        if s.get("note"):
            out.append(f"说明：{s['note']}")
        return out

    def _node_problems(self, title: str, items: list[tuple[str, str]], g: ProjectGraph) -> list[str]:
        out = [f"{title}：{len(items)} 个"]
        for key, nid in items[:MAX_ITEMS]:
            fg = g.functions.get(key)
            node = None if fg is None else next((n for n in fg.nodes if n.id == nid), None)
            out.append(f"  - {node_line(fg, node)}（{fg.qualname}，{fg.file}）" if node is not None else f"  - {key} {nid}")
        if len(items) > MAX_ITEMS:
            out.append(f"  …还有 {len(items) - MAX_ITEMS} 个")
        return out

    def _unresolved(self, v: _View, total: int) -> list[str]:
        """目标不明的调用：名字在项目里压根没有定义的（第三方、标准库）、太常见的名字（get、append 这类）和日志
        不逐条列——它们几乎不可能落在项目代码上，列出来只是噪音，各折进一行给个数；可能落在项目代码上的才逐条
        列出，在功能线上的排在前面，再按文件、行。"""
        g = v.graph
        out = [f"目标不明的调用：{total} 处"]
        if not total:
            return out
        noise = (v.project.get("config") or {}).get("noise_receivers") or NOISE_RECEIVERS
        groups = classify_unresolved(g.functions, noise)
        on_lines = lines_of_functions(g)
        sites = sorted((s.key not in on_lines, s.file, s.line, s.name) for s in groups.blind)
        if sites:
            out.append(f"  可能落在项目代码上的 {len(sites)} 处（名字在项目里有同名定义，但判断不出接收者的类型；"
                       "在功能线上的排在前面）：")
            out += [f"    {f}:{line} {name}" + ("" if off else "（在功能线上）") for off, f, line, name in sites[:MAX_BLIND]]
            if len(sites) > MAX_BLIND:
                out.append(f"    …还有 {len(sites) - MAX_BLIND} 处")
        rest = []
        if groups.outside:
            rest.append(f"第三方 / 标准库 {len(groups.outside)} 处（最常见 {_top_names(groups.outside)}）")
        if groups.common:
            rest.append(f"名字太常见 {len(groups.common)} 处（{_top_names(groups.common)} 这类，"
                        "项目里虽有同名定义，多半是字典、列表的方法）")
        if groups.logs:
            rest.append(f"日志 {len(groups.logs)} 处")
        if rest:
            n = len(groups.outside) + len(groups.common) + len(groups.logs)
            out.append(f"  其余 {n} 处不逐条列（几乎不可能落在项目代码上）：" + "、".join(rest))
        return out

    def _guessed(self, g: ProjectGraph, total: int) -> list[str]:
        sites = [(fg.file, n.line, ref.name, ref.target) for fg in g.functions.values() for n in fg.nodes
                 for ref in n.calls if ref.resolution == "guessed"]
        out = [f"推测的调用：{total} 处" + ("（按名字推测的目标，可能不对）" if total else "")]
        for f, line, name, target in sites[:MAX_ITEMS]:
            fg = g.functions.get(target or "")
            out.append(f"  {f}:{line} {name} → {fn_label(fg) if fg is not None else target}")
        if len(sites) > MAX_ITEMS:
            out.append(f"  …还有 {len(sites) - MAX_ITEMS} 处")
        return out

    def _stale_details(self, v: _View) -> list[str]:
        st = v.stale
        if not st.get("checked"):
            return [f"没法检查：{st.get('reason', '')}"]
        t = st["total"]
        out = ["代码已变更：改 {changed} 个、增 {added} 个、删 {removed} 个文件（图可能过期，先调用 analyze_project 重新分析）。"
               .format(**t) if st["stale"] else "代码没有变更：和分析时相同。"]
        for key, word in (("changed", "改"), ("added", "增"), ("removed", "删")):
            files = st[key][:MAX_FILES]
            if files:
                more = f" 等 {t[key]} 个" if t[key] > len(files) else ""
                out.append(f"{word}：{'、'.join(files)}{more}")
        if st["stale"]:
            sids = st["lines"]
            if sids is None:
                out.append("涉及的功能线：不清楚（这次分析是旧版本存的，没有记下功能线涉及的文件）")
            elif not sids:
                out.append("涉及的功能线：无（改动的文件不在任何功能线上）")
            else:
                out.append(f"涉及的功能线（{len(sids)} 条）：")
                out += [f"  - {v.ref(ln) if (ln := v.by_sid(s)) is not None else s}" for s in sids[:MAX_FILES]]
        if st.get("unreadable"):
            files = st["unreadable"][:MAX_FILES]
            more = f" 等 {t['unreadable']} 个" if t["unreadable"] > len(files) else ""
            out.append(f"{st.get('note', '有文件读不了，没有比对')}（没有权限、所在目录进不去，或换成了指到项目外的链接）："
                       f"{'、'.join(files)}{more}")
        return out

    # ---------- list_flows ----------

    @_capped("功能线太多：用 query 或 group 缩小范围")
    def list_flows(self, *, project: str | None = None, query: str | None = None, group: str | None = None) -> str:
        """按分组列出功能线"""
        v = self._view(project)
        changed, removed = self._changed_lines(v)
        needle = query.casefold() if query is not None else None

        def wanted(texts: list[str], grp: str) -> bool:
            if group is not None and grp != group:
                return False
            return needle is None or any(needle in t.casefold() for t in texts)

        groups: dict[str, list[str]] = {}
        for ln in v.graph.lines:
            fg = v.graph.functions.get(ln.root.function)
            file = fg.file if fg is not None else ""
            sid, grp = v.sid(ln), v.group(ln)
            route = (fg.route if fg is not None else None) or ""
            if not wanted([*v.titles(ln), grp, sid, file, route], grp):
                continue
            where = f"{fg.file}:{fg.line}" if fg is not None else ln.root.function
            shown = v.title(ln) + (f"（{route}）" if route and route != v.title(ln) else "")  # 标题旁写"方法 路径"
            row = f"- {shown}  [{sid}]  {_LINE_KIND.get(ln.kind, '函数')}  {where}"
            if sid in changed:
                row += f"  ⚑ 有未看的流程变化（{_DIFF_STATUS.get(changed[sid], changed[sid])}）"
            groups.setdefault(grp, []).append(row)
        gone = [c for c in removed if wanted([c.get("title", ""), c.get("group", ""), c.get("sid", "")], c.get("group", ""))]
        total = sum(len(rows) for rows in groups.values())
        name = v.project["name"]
        if not v.graph.lines and not gone:
            return self._warning(v) + (f"项目「{name}」：这个项目没有功能线。" + "".join(entry_notes(v.graph)) + "。也可以用 "
                                       "get_flow 的 function 参数直接看任何一个函数的流程，或者用 find_nodes 按文件和行号查函数。")
        if not total and not gone:
            known = "、".join(sorted({v.group(ln) for ln in v.graph.lines}))
            filters = "，".join(f"{k}={x}" for k, x in (("query", query), ("group", group)) if x is not None)
            return self._warning(v) + f"项目「{name}」没有匹配的功能线（{filters}）。分组有：{known}。"
        filtered = query is not None or group is not None
        out = [f"项目「{name}」共 {len(v.graph.lines)} 条功能线" + (f"，匹配的 {total} 条" if filtered else "") + "："]
        for grp in sorted(groups):
            out += ["", f"## {grp}（{len(groups[grp])} 条）", *groups[grp]]
        if gone:
            out += ["", "## 相对对比基准删除了的功能线（diff_flows 查看）"]
            out += [f"- {v.overrides.get(c.get('sid', ''), {}).get('title') or c.get('title', '')}  [{c.get('sid', '')}]  "
                    f"分组：{v.overrides.get(c.get('sid', ''), {}).get('group') or c.get('group', '')}" for c in gone]
        return self._warning(v) + "\n".join(out)

    def _changed_lines(self, v: _View) -> tuple[dict[str, str], list[dict]]:
        """最新一次分析存着的流程差异里有变化的功能线：({sid: status}, [删除了的线])。没有差异时都为空。"""
        state = self.store.latest_diff(v.project["id"])
        if state is None or state["diff"] is None or state["head"]["id"] != v.analysis["id"]:
            return {}, []
        lines = [c for c in state["diff"].get("lines", []) if isinstance(c, dict)]
        return ({c.get("sid", ""): c.get("status", "") for c in lines},
                [c for c in lines if c.get("status") == "removed"])

    # ---------- get_flow ----------

    @_capped("要看全：用更小的 depth、指定一条具体的功能线（flow），或用 find_nodes、get_node（function + node）只看要改的节点")
    def get_flow(self, *, project: str | None = None, flow: str | None = None, depth: int = 0, format: str = "outline",
                 function: str | None = None) -> str:
        """一条功能线的流程（大纲或 Mermaid）；只给 function 时是项目里任何一个函数的流程（不管它在不在功能线上）"""
        if format not in ("outline", "mermaid"):
            raise ToolError(f"format 只能是 outline 或 mermaid，这次是 {format}")
        v = self._view(project)
        depth = max(0, min(depth, MAX_DEPTH))
        if flow is None:
            if function is None:
                raise ToolError("要给 flow（功能线的 sid 或标题），或者只给 function（看任何一个函数的流程）")
            if format == "mermaid":
                raise ToolError("function 只能和 outline 一起用（Mermaid 图画的是整条功能线）")
            key = self._function_on_line(v, None, function)
            return self._warning(v) + self._fitting(depth, lambda d: "\n".join(function_outline(v.graph, key, depth=d)))
        line = self._line(v, flow)
        if function is not None:
            if format == "mermaid":
                raise ToolError("function 只能和 outline 一起用（Mermaid 图画的是整条功能线）")
            key = self._function_on_line(v, line, function)
            return self._warning(v) + self._fitting(depth, lambda d: "\n".join(function_outline(v.graph, key, depth=d)))
        if format == "mermaid":
            return self._warning(v) + line_mermaid(v.graph, line)
        return self._warning(v) + self._fitting(depth, lambda d: self._line_text(v, line, d))

    def _fitting(self, depth: int, render: Callable[[int], str]) -> str:
        """render(depth) 放得下就用它；放不下时退到放得下的最大深度（内容完整，比从尾部截断有用），开头说明。depth=0
        还放不下时原样返回，由 _cap 截断。"""
        text = render(depth)
        used = depth
        while len(text) > FLOW_BUDGET and used > 0:
            used -= 1
            text = render(used)
        if used == depth:
            return text
        return (f"（depth={depth} 的大纲超过 {MAX_CHARS} 字的上限，下面是 depth={used} 的，没有截断。要看某个步骤里面："
                "再调用一次 get_flow，用 function 指定行尾\"⇒ 可展开\"后面方括号里的那个函数）\n" + text)

    def _function_on_line(self, v: _View, line: FlowLine | None, name: str) -> str:
        """get_flow 的 function → 函数 key：先在这条功能线涉及的函数里找，找不到再在整个项目里找（没给功能线时直接在
        整个项目里找）；要唯一。"""
        g = v.graph
        keys = [h.function for h in find_by_function(g, name) if h.how == "definition"]
        on_line = set(line_functions(g, line)) if line is not None else set()
        hits = [k for k in keys if k in on_line] or keys
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ToolError(f"没有叫「{name}」的函数（可以给函数名、限定名 Class.method，或函数 sid）")
        rows = "\n".join(f"- {fn_label(g.functions[k])}" for k in hits[:MAX_HITS])
        raise ToolError(f"「{name}」对应 {len(hits)} 个函数，请用方括号里的函数 sid 指定：\n{rows}")

    def _line_text(self, v: _View, line: FlowLine, depth: int) -> str:
        text = line_outline(v.graph, line, depth=depth)
        if v.sid(line) in v.overrides:  # 手工改过标题或分组：标题行用改过的
            rows = text.split("\n")
            rows[0] = f"# {v.title(line)}  [{v.sid(line)}]  分组：{v.group(line)}"
            text = "\n".join(rows)
        return text

    # ---------- find_nodes ----------

    @_capped("命中太多：给具体的 file + line，或者用函数 sid 代替函数名")
    def find_nodes(self, *, project: str | None = None, file: str | None = None, line: int | None = None,
                   function: str | None = None) -> str:
        """按文件和行号、或按函数名反查节点"""
        if function is not None and (file is not None or line is not None):
            raise ToolError("file + line 和 function 只给一种")
        if function is None and (file is None or line is None):
            raise ToolError("要给 file 和 line（文件和行号），或者给 function（函数名）")
        if line is not None and line < 1:
            raise ToolError(f"行号从 1 起，这次是 {line}")
        v = self._view(project)
        g = v.graph
        if function is not None:
            hits = find_by_function(g, function)
            if not hits:
                return self._warning(v) + f"没有叫「{function}」的函数（可以给函数名、限定名 Class.method，或函数 sid）。"
            head = [f"函数「{function}」：{len(hits)} 处命中"]
        else:
            rel, hits = self._locate(v, file, line)
            if not hits:
                if rel is None or not any(fg.file == rel for fg in g.functions.values()):
                    why = (f"文件 {file} 不在图里：不是这个项目里分析过的 .py 文件（文件路径相对项目根目录 "
                           f"{v.project['root']}），或者在排除的目录里，或者里面没有函数。")
                else:
                    why = f"{rel} 第 {line} 行不在任何函数里（import、模块级代码、类体里的语句不出节点）。"
                return self._warning(v) + why
            head = [f"{rel} 第 {line} 行：{len(hits)} 处命中"]
            if rel in v.stale.get("changed", []):
                head.append("注意：这个文件在分析之后改过，行号可能对不上图；先调用 analyze_project 重新分析。")
        warning = self._warning(v)
        out = list(head)
        budget = MAX_CHARS - len(warning) - CLOSING_RESERVE  # 给最后那行"…还有 N 处"留地方，不让截断吃掉它
        size = _size(out)
        defined = [h.function for h in hits if h.how == "definition"]
        shown = 0
        for i, hit in enumerate(hits[:MAX_HITS], 1):
            fg = g.functions[hit.function]
            node = next(n for n in fg.nodes if n.id == hit.node)
            how = _HOW[hit.how]
            if hit.how == "call_site" and len(defined) > 1:  # 同名的定义有几个：说调用的是哪一个
                called = [k for k in defined if any(ref.target == k for ref in node.calls)]
                how += "（调用 " + "、".join(f"[{g.functions[k].sid or k}]" for k in called) + "）"
            block = ["", f"{i}. {node_line(fg, node)}", f"   怎么命中：{how}", f"   所在函数：{fn_label(fg, titled=True)}",
                     "   在这些功能线上：", *self._line_rows(v, hit.lines, "     ", hit.function)]
            if size + _size(block) > budget:
                if shown:  # 在命中之间截断
                    break
                block = _fit(block, budget - size) + ["   …（这一处太长，后面的没有列出）"]  # 第一处就放不下
            out += block
            size += _size(block)
            shown += 1
        if shown < len(hits):
            out += ["", f"…还有 {len(hits) - shown} 处命中没有列出（给具体的 file + line，或者用函数 sid 代替函数名）"]
        return warning + "\n".join(out)

    def _locate(self, v: _View, file: str, line: int) -> tuple[str | None, list]:
        """按 file:line 反查：file 先当作相对项目根目录（或项目里的绝对路径），找不到时再按相对当前目录试一次。返回
        (相对项目根的路径 | None, 命中)。"""
        candidates = [file]
        if not os.path.isabs(file) and self.cwd is not None and str(self.cwd) != v.project["root"]:
            candidates.append(str(self.cwd / file))
        first = None
        for c in candidates:
            rel = _relative(v.graph.root, c)
            first = first or rel
            hits = find_by_location(v.graph, c, line)
            if hits:
                return rel, hits
        return first, []

    # ---------- paths_to_outcome ----------

    @_capped("结局或路径太多：用结局的 sid 只看一个结局，或减小 limit")
    def paths_to_outcome(self, *, project: str | None = None, flow: str, outcome: str,
                         limit: int = DEFAULT_LIMIT) -> str:
        """从入口到某个结局的路径"""
        v = self._view(project)
        g = v.graph
        line = self._line(v, flow)
        limit = max(1, min(limit, MAX_LIMIT))
        targets = self._outcomes(g, line, outcome)
        if not targets and self._is_uncaught_sid(g, line, outcome):
            return self._warning(v) + (f"功能线「{v.title(line)}」[{v.sid(line)}] 里的 [{outcome}]：{UNCAUGHT_ANSWER}"
                                       "要看会抛哪些异常，用 get_flow 看各函数里的步骤和捕获，或者直接读源码。")
        if not targets:
            raise ToolError(f"功能线「{v.title(line)}」[{v.sid(line)}] 里没有匹配「{outcome}」的结局"
                            f"（outcome 可以是结局节点的 sid、标题的片段，或 ok / skipped / failed / exception；"
                            f"未捕获异常退出不算）。{self._outcome_choices(g, line)}")
        targets, caught = self._split_caught(g, line, targets)
        if not targets:
            return self._warning(v) + "\n".join([
                (f"功能线「{v.title(line)}」[{v.sid(line)}] 里没有以「{outcome}」结束的结局：匹配到的 {len(caught)} 个都是被调函数里"
                 "抛出、又在调用它的函数里被接住的异常。"), *caught])
        out = [f"功能线「{v.title(line)}」[{v.sid(line)}] 里匹配「{outcome}」的结局 {len(targets)} 个"
               + (f"（列出前 {MAX_OUTCOMES} 个）" if len(targets) > MAX_OUTCOMES else "") + "。", PATHS_CAVEAT]
        for key, nid in targets[:MAX_OUTCOMES]:
            fg = g.functions[key]
            node = next(n for n in fg.nodes if n.id == nid)
            out += ["", f"### {node_text(node)}  [{node.sid or node.id}]  " + (f"{fg.file}:{node.line}" if node.line else fg.file),
                    f"所在函数：{fn_label(fg, titled=True)}", "从功能线入口到这个函数：" + _chain_text(g, line, key)]
            paths, more = paths_to(g, key, nid, limit=limit)
            if not paths:
                out.append("函数里没有走到它的路径（从函数开始走不到）")
                continue
            out.append(f"函数里走到它的路径（{len(paths)} 条" + (f"，只列前 {limit} 条" if more else "")
                       + "，顺序走过的步骤不写）：")
            out += [f"{i}. {_path_text(fg, p)}" for i, p in enumerate(paths, 1)]
        if len(targets) > MAX_OUTCOMES:
            out += ["", f"…还有 {len(targets) - MAX_OUTCOMES} 个结局没有列出（用结局的 sid 或更具体的标题片段）"]
        if caught:
            out += ["", f"### 不算这条线的结局：被接住的异常 {len(caught)} 个",
                    "它们是被调函数里的 raise，但调用它的函数接住了，功能线从接住的地方接着往下走，不会以它们结束：", *caught]
        return self._warning(v) + "\n".join(out)

    def _split_caught(self, g: ProjectGraph, line: FlowLine,
                      targets: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
        """把匹配到的结局分成两份：真会让功能线结束的，和被调函数里抛出、在调用链上被接住的异常（每个一行说明：是哪个
        结局、在哪个函数的哪个捕获节点被接住）。"""
        kept: list[tuple[str, str]] = []
        caught: list[str] = []
        for key, nid in targets:
            fg = g.functions[key]
            node = next(n for n in fg.nodes if n.id == nid)
            hit = caught_on_chain(g, entry_chain(g, line, key) or [], node)
            if hit is None:
                kept.append((key, nid))
                continue
            by, catch = g.functions[hit[0]], hit[1]
            caught.append(f"- {node_text(node)}  [{node.sid or node.id}]  {fg.file}:{node.line}（函数 {fg.qualname}）"
                          f" —— 在 {_FN_KIND.get(by.kind, '函数')} {by.qualname} 的 {node_ref(by, catch)} L{catch.line}"
                          f"「{catch.title}」被接住")
        return kept, caught

    def _outcomes(self, g: ProjectGraph, line: FlowLine, text: str) -> list[tuple[str, str]]:
        """匹配的结局 [(函数 key, 节点 id)]：节点 sid 完全相同的；ok / skipped / failed / exception 按结局类型（不看标题：
        find_outcomes 对它们也按标题片段找，"返回 {"failed": 0}"、含 token 的标题会混进来）；别的文字按 find_outcomes
        找标题片段。去掉未捕获异常退出（它没有入边，走不到）。派发树上的任务里的排在前面，其余（被调函数里的）在后。"""
        keys = line_functions(g, line)
        by_sid = [(k, n.id) for k in keys for n in g.functions[k].nodes
                  if n.type == "end" and n.sid == text and not _uncaught(n)]
        if by_sid:
            return by_sid
        if text in _END:
            found = [(k, n.id) for k in keys for n in g.functions[k].nodes if n.type == "end" and n.end_kind == text]
        else:
            found = find_outcomes(g, line, text)
        found = [(k, nid) for k, nid in found if not _uncaught(next(n for n in g.functions[k].nodes if n.id == nid))]
        tasks = set(_tasks(line.root))
        return sorted(found, key=lambda item: item[0] not in tasks)

    def _is_uncaught_sid(self, g: ProjectGraph, line: FlowLine, text: str) -> bool:
        """text 是这条线上某个函数的"未捕获异常退出"的 sid（get_flow 里每个函数都有的 [end:uncaught]）。"""
        return any(n.sid == text and _uncaught(n) for k in line_functions(g, line) for n in g.functions[k].nodes)

    def _outcome_choices(self, g: ProjectGraph, line: FlowLine) -> str:
        """这条线上的结局（派发树上的任务里的在前，再是被调函数里的；不含未捕获异常退出），给 ToolError 列可选的。"""
        tasks = set(_tasks(line.root))
        keys = sorted(line_functions(g, line), key=lambda k: k not in tasks)
        rows = [f"- [{n.sid or n.id}] {node_text(n)}  （{g.functions[k].qualname}）"
                for k in keys for n in g.functions[k].nodes if n.type == "end" and not _uncaught(n)]
        if not rows:
            return ""
        more = f"\n…还有 {len(rows) - MAX_CHOICES} 个" if len(rows) > MAX_CHOICES else ""
        return "这条线上的结局（任务里的在前）：\n" + "\n".join(rows[:MAX_CHOICES]) + more

    # ---------- get_node ----------

    @_capped("内容太多：源码片段之外的部分用 get_flow 看")
    def get_node(self, *, project: str | None = None, function: str, node: str) -> str:
        """一个节点的详情"""
        v = self._view(project)
        g = v.graph
        key = g.sids.get(function) or (function if function in g.functions else None)
        if key is None:
            raise ToolError(f"没有这个函数：{function}（function 要给函数 sid，就是 get_flow、find_nodes 输出里函数头"
                            f"方括号中的那个，如 app/orders.py::place_order）")
        fg = g.functions[key]
        n = next((x for x in fg.nodes if x.sid == node), None) or next((x for x in fg.nodes if x.id == node), None)
        if n is None:
            raise ToolError(f"函数 {fg.sid or key} 里没有这个节点：{node}（节点 sid 是大纲每行开头方括号里的，"
                            f"如 step:1a2b3c4d、cond:…、end:…）")
        sub = n.subtitle_full or n.subtitle
        kind = _TYPE.get(n.type, n.type) + (f"（{_END[n.end_kind]}）" if n.type == "end" and n.end_kind in _END else "")
        where = (f"{fg.file}:{n.line}" + (f"–{n.end_line}" if n.end_line > n.line else "")) if n.line \
            else "隐含节点，没有行号（开始、执行到函数末尾、未捕获异常退出）"
        out = [f"节点 {node_line(fg, n)}", f"标题：{n.title_full or n.title}"]
        if sub:
            out.append(f"{'条件 / 原文' if n.type in ('cond', 'loop', 'catch') else '调用名'}：{sub}")
        out += [f"类型：{kind}", f"位置：{where}", f"所在函数：{fn_label(fg, titled=True)}"]
        if n.fields:
            out.append("结局带的字段：" + "、".join(f"{k}={x}" for k, x in n.fields.items()))
        if n.passthrough:
            target = g.functions.get(n.passthrough)
            out.append(f"转交返回：{fn_label(target) if target is not None else n.passthrough}")
        out += ["", *self._snippet(v, fg, n), "", *self._calls(g, n), "", *self._edges(fg, n),
                "", "所在的功能线：", *self._line_rows(v, lines_of_functions(g).get(key, []), "  ")]
        return self._warning(v) + "\n".join(out)

    def _snippet(self, v: _View, fg: FunctionGraph, n: Node) -> list[str]:
        if not n.line:
            return ["源码：隐含节点没有源码"]
        last = max(n.end_line, n.line)
        try:
            path = resolve_source_path(v.project["root"], fg.file)
            snip = read_snippet(path, fg.file, max(1, n.line - SNIPPET_CONTEXT), last + SNIPPET_CONTEXT,
                                v.graph.file_hashes.get(fg.file))
        except SourceError as e:
            return [f"源码：读不了（{e.message}）"]
        except OSError as e:
            return [f"源码：读不了（{e.strerror or e}）"]
        if not snip["lines"]:
            return [f"源码：文件只有 {snip['total']} 行，读不到第 {n.line} 行（文件在分析之后改过）"]
        out = [f"源码（{fg.file} 第 {snip['start']}–{snip['end']} 行，→ 标出节点所在的行）："]
        if snip["stale"]:
            out.append("注意：这个文件在分析之后改过，下面的源码可能和图对不上；先调用 analyze_project 重新分析。")
        width = len(str(snip["end"]))
        for i, text in enumerate(snip["lines"], snip["start"]):
            out.append(f"{'→' if n.line <= i <= last else ' '} {i:>{width}} | {text}")
        return out

    def _calls(self, g: ProjectGraph, n: Node) -> list[str]:
        if not n.calls:
            return ["调用：无"]
        out = ["调用："]
        for ref in n.calls:
            target = g.functions.get(ref.target) if ref.target else None
            if target is not None:
                what = ("派发 " if ref.is_dispatch else "") + fn_label(target)
            elif ref.resolution == "external":
                what = "项目之外的函数"
            else:
                what = ref.target or "目标不明"
            notes = []
            if ref.conditional:
                notes.append("在三元表达式、and / or 或 case 守卫里，不一定执行")
            if ref.dispatches:
                names = [g.functions[k].qualname if k in g.functions else k for k in ref.dispatches]
                notes.append("内部派发 " + "、".join(names))
            out.append(f"  - {ref.name} → {what}（{_RESOLUTION.get(ref.resolution, ref.resolution)}）"
                       + (f"；{'；'.join(notes)}" if notes else ""))
        return out

    def _edges(self, fg: FunctionGraph, n: Node) -> list[str]:
        nodes = {x.id: x for x in fg.nodes}
        ins = [e for e in fg.edges if e.dst == n.id and e.src in nodes]
        outs = [e for e in fg.edges if e.src == n.id and e.dst in nodes]
        out = ["进来的连线：" + ("" if ins else "无")]
        out += [f"  - {node_line(fg, nodes[e.src])} —{_edge_word(nodes[e.src], e)}→ 这里" for e in ins]
        out.append("出去的连线：" + ("" if outs else "无"))
        out += [f"  - {_edge_word(n, e)} → {node_line(fg, nodes[e.dst])}" for e in outs]
        return out

    # ---------- diff_flows ----------

    @_capped("变化太多：用 flow 参数只看一条功能线")
    def diff_flows(self, *, project: str | None = None, flow: str | None = None, record: int | None = None) -> str:
        """流程和对比基准相比的变化"""
        v = self._view(project)
        if record is not None:
            return self._review_text(v, record, flow)
        state = self.store.latest_diff(v.project["id"])
        if state is None:
            raise ToolError(f"项目「{v.project['name']}」还没有分析结果：先调用 analyze_project")
        head, base = state["head"], state["base"]
        past = self._reviews_footer(v)
        if base is None:
            return self._warning(v) + ("没有未看的流程变化：只有一次分析，或者变化已在平台上标为已看，或者重新分析过、流程"
                                       "没有变化。改了代码之后先调用 analyze_project 重新分析，再来看。") + past
        out = [f"最新一次分析（#{head['id']}，{_when(head['created_at'])}）{_baseline_text(state)}的流程变化："]
        if state["diff"] is None:
            out.append("流程没有变化")
            return self._warning(v) + "\n".join(out) + past
        d = _load_diff(state["diff"])
        engines = (base["engine_version"], head["engine_version"])
        if flow is None:
            out.append(diff_text(_with_overrides(d, v.overrides), engines=engines))
        else:
            sid = self._diff_line(v, d, flow)
            out.append(diff_text(_with_overrides(d, v.overrides), line_sid=sid, engines=engines))
            if all(ln.sid != sid for ln in d.lines) and not d.empty:
                out.append("（别的功能线或函数有变化：不给 flow 可以看全部）")
        return self._warning(v) + "\n".join(out) + past

    def _reviews_footer(self, v: _View) -> str:
        """diff_flows 结果末尾的"变更记录"列表：以前标为已看的那些变化（最近 10 条）。没有时空串。"""
        records = self.store.list_reviews(v.project["id"])
        if not records:
            return ""
        rows = ["", "", f"变更记录（以前在平台上标为已看的变化，最近 {len(records)} 条；用 record 看明细）："]
        for r in records:
            s = r["summary"]
            lines = "、".join(ln["title"] for ln in s["lines"][:5]) + ("等" if len(s["lines"]) > 5 else "")
            rows.append(f"- #{r['id']}  {_when(r['acked_at'])} 标为已看：{len(s['lines'])} 条功能线（{lines or '无'}），"
                        f"节点新增 {s['added']}、删除 {s['removed']}、改动 {s['changed']}")
        return "\n".join(rows)

    def _review_text(self, v: _View, record: int, flow: str | None) -> str:
        found = self.store.get_review(v.project["id"], record)
        if found is None:
            have = "、".join(f"#{r['id']}" for r in self.store.list_reviews(v.project["id"])) or "（还没有）"
            raise ToolError(f"没有变更记录 #{record}（只留最近 10 条）。现有的：{have}")
        d = _load_diff(found["diff"])
        head = (f"变更记录 #{found['id']}（{_when(found['acked_at'])} 标为已看）：分析 #{found['head_id']}"
                f"（{_when(found['head_at'])}）相对分析 #{found['base_id']}（{_when(found['base_at'])}）的流程变化。"
                "这是当时的变化，文件和行号是当时的，现在的代码可能已经不同：")
        if flow is None:
            return _cap(head + "\n" + diff_text(d), "这条记录太长：用 flow 只看一条功能线")
        hits = [ln for ln in d.lines if flow in (ln.sid, ln.title)]
        if len(hits) != 1:
            raise ToolError(f"这条记录里没有功能线「{flow}」。有变化的：{'、'.join(ln.title for ln in d.lines) or '无'}")
        return _cap(head + "\n" + diff_text(d, line_sid=hits[0].sid), "这条记录太长：在平台的变更记录里看")

    def _diff_line(self, v: _View, d: GraphDiff, flow: str) -> str:
        """diff_flows 的 flow：先在现在的图里找；找不到时（这条线删掉了）在差异里按 sid、标题找。"""
        try:
            return v.sid(self._line(v, flow))
        except ToolError:
            gone = [ln for ln in d.lines if flow in (ln.sid, ln.title)]
            if len(gone) == 1:
                return gone[0].sid
            raise

    # ---------- check ----------

    @_capped("违规太多：用 files 只检查几个文件")
    def check(self, *, project: str | None = None, path: str | None = None, files: list[str] | None = None) -> str:
        """检查流程代码有没有违反约束"""
        if project is not None and path is not None:
            raise ToolError("project 和 path 只给一个")
        projects = self.store.list_projects()
        if path is not None:
            target = self._abs(path)
            if target is None or not target.is_dir():
                raise ToolError(f"不是目录：{path}")
            if home_conflict(target):  # 和添加项目同样的规矩：走一遍整个主目录要几分钟、几 GB 内存
                raise ToolError(f"不能检查根目录、用户主目录或包含用户主目录的目录，请给具体的代码目录：{target}")
            p = _containing(projects, target)
        else:
            try:
                p = self._project(project, projects)
            except ToolError as e:
                raise ToolError(f"{e}\n检查没添加过的目录：用 path 给出目录。") from None
            target = Path(p["root"])
        prefix = ""
        if p is None:
            root, config_json, used = target, {}, "默认配置（这个目录不在已添加的项目里）"
        else:
            problem = root_problem(p["root"])
            if problem is not None:
                raise ToolError(problem[1])
            root, config_json, used = Path(p["root"]), p["config"], f"项目「{p['name']}」的分析配置"
            if target != root:  # 项目里面的目录：按项目根目录和项目的配置检查，只报告这个目录里的
                try:
                    prefix = target.relative_to(root).as_posix()
                except ValueError:  # 按 inode 认出的（写法大小写不同）：就检查这个目录本身
                    root = target
        given = {posixpath.normpath(posixpath.join(prefix, f)): f for f in files} if files and prefix else {}
        only = list(given) if given else files
        result = check_in_subprocess(str(root), config_json, only, timeout=analyze_timeout(), cwd=self.store.path.parent)
        if not result["ok"]:
            raise ToolError(result["reason"])

        def inside(f: str) -> bool:
            return not prefix or f == prefix or f.startswith(prefix + "/")

        violations = [x for x in result["violations"] if inside(x["file"])]
        skipped = [x for x in result["skipped"] if inside(x[0])]
        ignored = [x for x in result["ignored"] if inside(x[0])]
        out = [f"{x['file']}:{x['line']}: {x['code']} {x['message']}" for x in violations]
        where = f"检查的目录：{target}" + (f"（路径相对项目根目录 {root}）" if prefix else "")
        scope = f"，只报告 {'、'.join(files)} 里的" if files else ""
        out.append(f"共 {len(violations)} 处违规（{where}{scope}；用的配置：{used}）")
        out += [f"警告：files 里的 {f} 不对应项目里的任何 .py 文件（相对检查的目录；排除的目录不算）"
                for f in (given.get(x, x) for x in result["unmatched"])]
        if skipped:
            out.append(f"以下 {len(skipped)} 个文件解析不了，没有检查：")
            out += [f"  {f}：{why}" for f, why in skipped]
        if ignored:
            out.append(f"以下 {len(ignored)} 个 .py 路径不是项目里的源文件，没有检查：")
            out += [f"  {f}：{why}" for f, why in ignored]
        return "\n".join(out)


    # ---------- 需求 ----------

    def list_requirements(self, *, project: str | None = None) -> str:
        """列出项目里的需求和各自的对照结果"""
        v = self._view(project)
        found = rq.load_requirements(Path(v.project["root"]))
        root = Path(v.project["root"])
        if not found and rq.uses_openspec(root):
            return (f"项目「{v.project['name']}」用 OpenSpec 管需求，OpenSpec 的文件里还没有设计的流程。{OPENSPEC_HOW}")
        if not found:
            return (f"项目「{v.project['name']}」里还没有需求（{v.project['root']}/{rq.DIR}/ 下没有 .md 文件）。"
                    "接到业务需求时用 save_requirement 新建一条。")
        own = sum(r.source == "pystepmap" for r in found)
        where = "、".join(w for w, n in ((f"{rq.DIR}/ 里 {own} 条", own), (f"OpenSpec 文件里 {len(found) - own} 条", len(found) - own)) if n)
        out = [self._warning(v) + f"项目「{v.project['name']}」的需求 {len(found)} 条（{where}）：", ""]
        old = sum(r.archived for r in found)
        for i, r in enumerate(found):  # 已归档的排在最后（load_openspec 的顺序）
            if r.archived and (i == 0 or not found[i - 1].archived):
                out += ["", f"已归档的变更里的 {old} 条（当时的设计：和现在的代码不一致不算问题，不用照着改代码）："]
            status = "OpenSpec 已归档" if r.archived else "OpenSpec" if r.source == "openspec" else r.status or "？"
            out.append(f"- {r.id or '（没有编号）'}  {r.title}  [{status}]  功能线：{r.line or '（没写）'}")
            if r.errors:
                out.append(f"    读不懂（{r.file}）：{'；'.join(r.errors[:3])}")
                continue
            cmp = rq.compare(r, v.graph)
            out.append("    " + (_verdict(cmp) + ("" if cmp.matches or _no_line(cmp) else "（get_requirement 看详情）")))
        return _cap("\n".join(out), "需求太多：用 get_requirement 看具体的一条")

    def _requirement(self, v: _View, ref: str) -> rq.Requirement:
        found = rq.load_requirements(Path(v.project["root"]))
        hits = [r for r in found if r.id == ref] or [r for r in found if ref.casefold() in r.title.casefold()]
        if len(hits) != 1:
            have = "、".join(f"{r.id} {r.title}" for r in found[:20]) or "（一条都没有）"
            raise ToolError(f"{'找不到' if not hits else '对上不止一条'}需求「{ref}」。现有的：{have}")
        return hits[0]

    def get_requirement(self, *, project: str | None = None, requirement: str) -> str:
        """读一条需求，把设计的流程和实现对照"""
        v = self._view(project)
        return _cap(self._warning(v) + self._requirement_text(v, self._requirement(v, requirement)),
                    "这条需求太长：直接读项目里的需求文件")

    def _requirement_text(self, v: _View, r: rq.Requirement) -> str:
        external = r.source == "openspec"  # 写在 OpenSpec 文件里的：状态不存在文件里，改它就是改那个文件
        status = "已归档（OpenSpec 归档的变更：这是当时的设计，和现在的代码不一致不算问题）" if r.archived else \
            "由 OpenSpec 的变更流程管（这里不存）" if external else r.status
        out = [f"需求 {r.id}「{r.title}」  状态：{status}  功能线：{r.line or '（没写）'}",
               f"文件：{r.file}" + ("（OpenSpec 文件里的 flow 代码块；要改设计就改这个文件）" if external else ""), ""]
        if r.description:
            out += ["说明：", r.description, ""]
        out += ["设计的流程：", r.flow_text, ""]
        if r.errors:
            return "\n".join([*out, "这条需求读不懂，没有对照：", *(f"- {e}" for e in r.errors)])
        out += [self._picture(v, r), ""]
        cmp = rq.compare(r, v.graph)
        no_line = _no_line(cmp)
        if cmp.matches:
            out.append(f"对照：设计和实现对得上（功能线 {cmp.line_title} [{cmp.line_sid}]）。")
        elif no_line:  # 没有可以对照的实现：不是"几处不一致"，设计的节点也不逐个再列一遍
            out.append(f"对照：{_verdict(cmp)}。")
        else:
            where = f"（功能线 {cmp.line_title} [{cmp.line_sid}]）" if cmp.line_sid else ""
            out.append(f"对照：和实现有 {len(cmp.differences)} 处不一致{where}：")
            out += [f"- {d.text}" + (f"  [{d.node}]" if d.node else "") for d in cmp.differences]
        out += [f"注意：{n}" for n in cmp.notes]
        covered, rows = self._outcome_coverage(v, cmp)
        if rows:
            out += ["", "设计的结局有没有测试走到：", *rows]
        can = rq.reachable_status(r, cmp, covered)
        if r.archived:
            pass  # 当时的设计：不催着改
        elif external:
            done = "，设计的每个结局都有测试走到" if can == "已完成" else ""
            out += ["", f"按现在的对照结果：设计和实现对得上{done}。" if cmp.matches else
                    "按现在的对照结果：这条功能线还没有实现（已经实现了的话，是需求里写的功能线名字和图上的对不上）。" if no_line else
                    "按现在的对照结果：实现和设计还不一致——是代码要改还是设计要改，由用户定；改完再对照一次。"]
        elif can != r.status:
            out += ["", (f"按现在的对照结果，这条需求够得上的状态是「{can}」（现在写的是「{r.status}」）；"
                         "要改就用 save_requirement 带上编号和原来的内容再保存一次。")]
        return "\n".join(out)

    def _picture(self, v: _View, r: rq.Requirement) -> str:
        """设计的流程画成图在哪里看：平台（pystepmap serve）在运行时给这条需求的地址——上面的缩进文本用户不好读，图是
        平台按同一份文件画的，和实现不一致的节点标在图上；没在运行时说怎么打开。地址的写法同界面（#/<项目>/req:<需求>）。"""
        port = serve_port(self.store.path.parent)
        if port is None:
            return ("设计的流程图：平台没有在运行。用户想看图时，请用户启动 pystepmap serve，在这个项目的「需求」列表里选这一条"
                    "（画的就是上面这份设计，和实现不一致的节点标在图上）。")
        same = [x for x in rq.load_requirements(Path(v.project["root"])) if x.id == r.id]
        tail = r.id[r.id.rfind("#"):] if "#" in r.id and r.id[r.id.rfind("#") + 1:].isdigit() else ""
        ref = r.id if r.id and len(same) == 1 else r.file + tail
        q = lambda text: urllib.parse.quote(text, safe="")
        return (f"设计的流程图（把这个地址给用户看图确认，不要只贴上面的缩进文本）：http://127.0.0.1:{port}/#/{q(v.project['id'])}/"
                f"{q('req:' + ref)}")

    def _outcome_coverage(self, v: _View, cmp: rq.Comparison) -> tuple[bool | None, list[str]]:
        """(设计的每个结局是不是都有测试走到——不知道时 None, 每个结局一行)。用最近一次跑完的测试运行。"""
        if not cmp.line_sid or not cmp.outcomes:
            return None, []
        run = self.store.latest_full_run(v.project["id"])  # 只看全量的运行
        if run is None:
            return None, ["- 平台上还没有跑完的全量测试运行，不知道（在平台上运行一次全部测试）"]
        try:
            missed = self._uncovered_of(v, run["id"]).outcomes(cmp.line_sid)
        except LookupError:
            return None, []
        state = {o.node: ("那次运行里还没有这个结局（运行之后才写的，重新运行测试才知道）" if o.added else
                          "不确定（运行之后代码改过，对不上；重新运行测试才知道）" if o.uncertain else "没有测试走到")
                 for o in missed}
        rows, all_covered = [], True
        for title, kind, sid in cmp.outcomes:
            how = "实现里还没有" if not sid else state.get(sid, "有测试走到")
            all_covered = all_covered and how == "有测试走到"
            rows.append(f"- {rq.END_NAMES[kind]}「{title}」：{how}")
        return all_covered, [f"（用的是测试运行 #{run['id']}）", *rows]

    def save_requirement(self, *, project: str | None = None, title: str, flow: str, line: str = "",
                         description: str = "", status: str = "草稿", id: str | None = None) -> str:
        """新建或改写一条需求（写进项目目录）"""
        v = self._view(project)
        try:
            saved = rq.save_requirement(Path(v.project["root"]), title=title, line=line, description=description, flow=flow,
                                        status=status, id=id)
        except rq.RequirementError as e:
            raise ToolError(f"没有保存：{e}") from None
        except OSError as e:
            raise ToolError(f"没有保存：写不进项目目录（{e.strerror or e}）") from None
        head = f"已保存到 {v.project['root']}/{saved.file}（{'改写' if id else '新建'}）。\n\n"
        return _cap(self._warning(v) + head + self._requirement_text(v, saved), "这条需求太长：直接读项目里的需求文件")

    # ---------- run_line_tests：请平台运行一条功能线的测试 ----------

    def _platform(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """向正在运行的 pystepmap serve 发一个请求，返回 (状态码, JSON)。平台没在运行、连不上时抛 ToolError。测试由平台
        进程运行（这个进程不执行项目代码）：用户在界面上看得到进度、可以取消。"""
        port = serve_port(self.store.path.parent)
        if port is None:
            raise ToolError("pystepmap 平台没有在运行：测试由平台运行，请用户先启动 pystepmap serve（同一个数据目录）")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机的请求不走代理
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (OSError, ValueError) as e:
            raise ToolError(f"连不上 pystepmap 平台（127.0.0.1:{port}）：{getattr(e, 'reason', None) or e}") from None

    def run_line_tests(self, *, project: str | None = None, flow: str, start: bool = True,
                       wait_seconds: int = DEFAULT_WAIT) -> str:
        """请平台运行一条功能线的测试，等它跑完"""
        v = self._view(project)
        line = self._line(v, flow)
        sid, pid = v.sid(line), v.project["id"]
        base = f"/projects/{urllib.parse.quote(pid, safe='')}"
        head = f"功能线「{v.title(line)}」[{sid}]"
        last = max((r["id"] for r in self.store.list_runs(pid)), default=0)
        if start:
            status, data = self._platform("POST", f"{base}/runs", {"line": sid, "via": "mcp"})
            if status != 202:
                raise ToolError(f"没有运行{head}的测试：{data.get('error') or f'平台返回 {status}'}")
        deadline = time.monotonic() + max(0, min(int(wait_seconds), MAX_WAIT))
        while True:
            _, now = self._platform("GET", f"{base}/runs")
            if not now.get("running"):
                break
            if time.monotonic() >= deadline:
                p = now["running"].get("progress") or {}
                done = f"，已跑完 {p['done']} / {p.get('collected') or '?'} 个" if p else ""
                return (f"{head}的测试还在平台上跑（{now['running'].get('phase')}{done}）。过一会儿用 start=false 再看结果；"
                        "不要重复启动。")
            time.sleep(1.0)
        run = next((r for r in now.get("runs", []) if (r.get("scope") or {}).get("line") == sid
                    and (not start or r["id"] > last)), None)
        if run is None:
            failure = now.get("last_failure")
            if start and failure:
                raise ToolError(f"{head}的测试没有跑成：{failure.get('title')}——{failure.get('reason')}")
            return f"{head}还没有单独运行过测试的结果。"
        return _cap(self._line_run_text(base, head, sid, run), "结果太长：在平台的\"运行路径\"页签里看这次运行")

    def _line_run_text(self, base: str, head: str, sid: str, run: dict) -> str:
        s, scope = run["summary"], run["scope"]
        out = [(f"{head}的测试运行 #{run['id']}（{_RUN_STATUS.get(run['status'], run['status'])}）：名单 {scope['tests']} 个测试"
                f"（取自全量运行 #{scope['base_run']}），跑了 {s.get('tests', 0)} 个用例，通过 {s.get('passed', 0)}、"
                f"失败 {s.get('failed', 0)}、出错 {s.get('errors', 0)}、跳过 {s.get('skipped', 0)}，用时 {s.get('seconds', 0):.1f} 秒。")]
        if s.get("note"):
            out.append(f"说明：{s['note']}")
        verify = s.get("verify") or {}
        if verify.get("ok") is True:
            out.append("图和实际执行一致。")
        elif verify.get("ok") is False:
            out.append(f"图和实际执行不一致：{verify.get('mismatches')}（在平台上看详情；多半是图过期了，先 analyze_project）。")
        _, detail = self._platform("GET", f"{base}/runs/{run['id']}")
        bad = [t for t in detail.get("tests", []) if t.get("outcome") in ("failed", "error")]
        if bad:
            out += ["", "没有通过的测试（失败的原因平台不存，自己运行这个测试看输出）："]
            out += [f"- {t['test']}（{'失败' if t['outcome'] == 'failed' else '出错'}）" for t in bad[:50]]
        if run.get("paths_size") is not None:
            status, unc = self._platform("GET", f"{base}/runs/{run['id']}/uncovered?line={urllib.parse.quote(sid, safe='')}")
            missed = [o for o in unc.get("outcomes", []) if not o.get("uncertain")] if status == 200 else []
            if missed:
                out += ["", f"这条线上这些测试没有走到的结局 {len(missed)} 个："]
                out += [f"- {o['title']}（{_END.get(o['end_kind'], o['end_kind'])}）  {o['file']}:{o['line']}" for o in missed[:50]]
            elif status == 200:
                out += ["", "这条线上的每个结局都被这些测试走到了。"]
        out += ["", "注意：只跑了全量运行里走过这条线的测试；之后新写的测试不在名单里——让用户在平台上再运行一次全部测试。"]
        return "\n".join(out)

    # ---------- uncovered_outcomes ----------

    def uncovered_outcomes(self, *, project: str | None = None, flow: str | None = None) -> str:
        """列出没有测试走到的结局。结果超过上限时截断（_cap）：没给 flow 时提示用 flow 只看一条功能线；已经给了时提示去平台看、或只看具体的函数。"""
        hint = ("结局太多：用 flow 只看一条功能线" if flow is None
                else "这条功能线的结局太多：在平台的\"运行路径\"页签里看它（\"没走到的结局\"一节），或用 get_flow、find_nodes 只看具体的函数")
        return _cap(self._uncovered_text(project, flow), hint)

    def _uncovered_text(self, project: str | None, flow: str | None) -> str:
        v = self._view(project)
        lines = v.graph.lines if flow is None else [self._line(v, flow)]
        # 新的在前；只有信息，不读路径。只看全量的运行：只跑一条功能线的运行说明不了别的线
        runs = [r for r in self.store.list_runs(v.project["id"]) if r.get("scope") is None]
        if not runs:
            return self._warning(v) + NEVER_RAN
        run = next((r for r in runs if r["paths_size"] is not None), None)
        head: list[str] = []
        if run is not runs[0]:
            latest = runs[0]
            head.append(f"最近一次运行（#{latest['id']}）没有跑完（{_RUN_STATUS.get(latest['status'], latest['status'])}），"
                        + ("没有运行路径。" if run is None else f"下面用的是跑完了的 #{run['id']}。"))
        if run is None:
            return self._warning(v) + "\n".join(head + ["平台上还没有跑完、有运行路径的测试运行。"])
        found_all = self._uncovered_of(v, run["id"])
        warning = self._warning(v)
        if run["analysis_id"] != v.analysis["id"]:
            newer = ("运行之后才有的函数和结局、对不上的结局单列在最后（不算没走到）" if found_all.known_then else
                     "对不上的结局单列在最后；那次分析的图已经清理掉了，分不出哪些函数和结局是运行之后才加的——下面\"没有"
                     "测试走到\"的里面可能有那次运行时还不存在的代码")
            warning += (f"⚠ 最近一次运行（#{run['id']}）用的是分析 #{run['analysis_id']}，之后重新分析过（现在是 "
                        f"#{v.analysis['id']}）：结局按 sid 对到最新的图上，{newer}；之后改动的代码有没有"
                        f"测试走到，要在平台上重新运行测试才知道。\n\n")
        head.append(f"项目「{v.project['name']}」的测试运行 {_run_head(run)}")
        missed: list[str] = []
        unsure: list[str] = []
        seen_missed: set[tuple[str, str]] = set()
        seen_unsure: set[tuple[str, str]] = set()
        for ln in lines:
            found = found_all.outcomes(v.sid(ln))
            rows_missed = [self._outcome_row(v.graph, o) for o in found if not o.uncertain]
            rows_unsure = [self._outcome_row(v.graph, o) for o in found if o.uncertain]
            seen_missed.update((o.function, o.node) for o in found if not o.uncertain)
            seen_unsure.update((o.function, o.node) for o in found if o.uncertain)
            if rows_missed:
                missed += ["", f"## {v.ref(ln)}", *rows_missed]
            if rows_unsure:
                unsure += ["", f"## {v.ref(ln)}", *rows_unsure]
        where = f"功能线「{v.title(lines[0])}」里" if flow is not None else "功能线上"
        out = [*head, ""]
        if seen_missed:
            out.append(f"{where}没有测试走到的结局 {len(seen_missed)} 个（按功能线分组，同一个结局在几条线上时每条都列；"
                       f"未捕获异常退出不算）：")
            out += missed
        else:
            out.append(f"{where}的每个结局都有测试走到（未捕获异常退出不算）。")
        if seen_unsure:
            out += ["", (f"需要重新运行测试才知道的结局 {len(seen_unsure)} 个（运行之后才有的函数、结局，或者运行之后"
                         f"函数的节点变了、同文本的几个结局分不清运行时走的是哪一个；既不算走到也不算没走到）：")]
            out += unsure
        return warning + "\n".join(out)

    def _outcome_row(self, g: ProjectGraph, o: Outcome) -> str:
        fg = g.functions.get(g.sids.get(o.function) or o.function)
        name = fg.qualname if fg is not None else o.function
        where = f"{o.file}:{o.line}" if o.line else f"{o.file}（执行到函数末尾）"
        return f"- [{o.node}] {o.title}（{_END.get(o.end_kind, o.end_kind)}）  {where}  函数 {name}  [{o.function}]"

    def _uncovered_of(self, v: _View, run_id: int) -> Uncovered:
        """这次运行的路径叠到 v 的图（最新一次分析）上算出的没走到的结局；按（分析，运行）缓存最近 GRAPH_CACHE 对。"""
        key = (v.analysis["id"], run_id)
        with self._lock:
            if key in self._uncovered:
                self._uncovered.move_to_end(key)
                return self._uncovered[key]
        try:
            found = uncovered_of(self.store, v.project["id"], run_id, v.analysis["id"], v.graph)
        except (OSError, EOFError, ValueError) as e:
            raise ToolError(f"运行 #{run_id} 存着的运行路径读不出来：{e}") from None
        if found is None:
            raise ToolError("这次运行刚被清理掉了，请再调用一次")
        with self._lock:
            self._uncovered[key] = found
            while len(self._uncovered) > GRAPH_CACHE:
                self._uncovered.popitem(last=False)
        return found


# ---------- 模块级的小工具（要用到上面的类型） ----------

def _top_names(sites: list[UnresolvedSite]) -> str:
    """这些调用里最常见的几个名字："commit 88、execute 60、where 54"。"""
    counts = Counter(s.name.rpartition(".")[2] for s in sites)
    return "、".join(f"{name} {n}" for name, n in counts.most_common(3))


def _run_head(run: dict) -> str:
    """一次运行一句话：#id（时间，状态，用的是分析 #n）：N 个用例，通过 a、失败 b……"""
    s = run["summary"]
    counts = f"{s.get('tests', 0)} 个用例，通过 {s.get('passed', 0)}、失败 {s.get('failed', 0)}"
    if s.get("errors"):
        counts += f"、出错 {s['errors']}"
    if s.get("skipped"):
        counts += f"、跳过 {s['skipped']}"
    return (f"#{run['id']}（{_when(run['finished_at'])}，状态：{_RUN_STATUS.get(run['status'], run['status'])}，用的是分析 "
            f"#{run['analysis_id']}）：{counts}；测试命令跑了 {s.get('seconds', 0)} 秒")


def _uncaught(n: Node) -> bool:
    """"未捕获异常退出"：隐含的异常结局，没有入边。"""
    return n.type == "end" and n.end_kind == "exception" and not n.line


def _chain_text(g: ProjectGraph, line: FlowLine, key: str) -> str:
    chain = entry_chain(g, line, key)
    if chain is None:
        return "（找不到从入口到它的调用链）"
    if not chain:
        return "就在功能线的根函数里"
    parts = []
    for link in chain:
        fg = g.functions[link.function]
        n = next(x for x in fg.nodes if x.id == link.node)
        at = f"L{n.line} " if n.line else ""
        parts.append(f"{_FN_KIND.get(fg.kind, '函数')} {fg.qualname} 在 {node_ref(fg, n)} {at}"
                     f"{'派发' if link.how == 'dispatch' else '调用'}")
    target = g.functions[key]
    return " → ".join([*parts, f"{_FN_KIND.get(target.kind, '函数')} {target.qualname}"])


def _relative(root: str, file: str) -> str | None:
    """给提示用的相对路径：相对路径规范化；绝对路径要在 root 下。"""
    path = file.replace("\\", "/")
    if os.path.isabs(path):
        for base in dict.fromkeys((root.rstrip("/"), os.path.realpath(root).rstrip("/"))):
            if path.startswith(base + "/"):
                return posixpath.normpath(path[len(base) + 1:])
        return None
    path = posixpath.normpath(path)
    return None if path.startswith("../") or path == ".." else path


def _load_diff(data: dict) -> GraphDiff:
    try:
        return diff_from_json(data)
    except ValueError as e:
        raise ToolError(f"存着的流程差异读不出来：{e}") from None


MAX_NEW_SKIPPED = 10


def _newly_skipped(before: dict | None, summary: dict) -> list[str]:
    """这次分析新出现的解析不了的文件（上一次不在跳过的文件里），最多点名 MAX_NEW_SKIPPED 个；没有时空列表。这些文件里的
    函数不在图上，流程差异会把它们报成删除。"""
    old = {item.get("file") for item in ((before or {}).get("summary") or {}).get("skipped_files", []) if isinstance(item, dict)}
    new = [item for item in summary.get("skipped_files", []) if isinstance(item, dict) and item.get("file") not in old]
    if not new:
        return []
    named = "、".join(f"{item.get('file')}（{item.get('reason', '')}）" for item in new[:MAX_NEW_SKIPPED])
    more = f" 等 {len(new)} 个" if len(new) > MAX_NEW_SKIPPED else ""
    return [(f"这次有 {len(new)} 个文件解析不了（图里没有它们）：{named}{more}。下面流程变化里报成删除的函数可能只是因为"
             f"所在的文件解析不了，并没有真的删掉；修好文件再调用 analyze_project。")]


def _baseline_text(state: dict) -> str:
    """对比基准说成什么：相对上次标为已看时（那次分析的时间）/ 相对第一次分析（时间）；旧数据没有说法时按分析编号。"""
    baseline, base = state.get("baseline"), state["base"]
    if baseline and baseline.get("kind") == "acknowledged":
        return f"相对上次标为已看时（{_when(baseline['at'])}）"
    if baseline and baseline.get("kind") == "first":
        return f"相对第一次分析（{_when(baseline['at'])}）"
    return f"相对对比基准（#{base['id']}，{_when(base['created_at'])}）"


def _with_overrides(d: GraphDiff, overrides: dict[str, dict]) -> GraphDiff:
    """差异里的功能线用手工改过的标题和分组（和别的工具一致）；标题改过时括号里带原来的。原地改 d，返回它。"""
    for ln in d.lines:
        item = overrides.get(ln.sid, {})
        if item.get("title") and item["title"] != ln.title:
            ln.title = f"{item['title']}（原标题：{ln.title}）"
        if item.get("group"):
            ln.group = item["group"]
    return d


def _coerce(key: str, prop: dict, value: object) -> object:
    """按参数的 JSON schema 规整值；空字符串、null 当作没给（None）。类型不对时 ToolError。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    kind = prop.get("type")
    if kind == "integer":
        if isinstance(value, bool):
            raise ToolError(f"{key} 应该是整数")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ToolError(f"{key} 应该是整数，这次是 {json.dumps(value, ensure_ascii=False)[:40]}")
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ToolError(f"{key} 应该是 true 或 false")
    if kind == "array":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ToolError(f"{key} 应该是字符串列表")
        return value
    if not isinstance(value, str):
        raise ToolError(f"{key} 应该是字符串")
    return value.strip()
