"""平台的四条流程：添加项目、重新分析、删除项目、标记流程变化已看过。平台服务（api.py）和 MCP 都走这里。第五条
"运行测试并核对"在 testflows.py：它要执行项目代码（runner.py），单独放一个模块，只有平台接口导入它；这里不导入
runner，MCP、分析子进程导入这个模块也带不进执行项目代码的那条路。

按 flow-style 写（.claude/skills/flow-style/SKILL.md）：编排函数只写走向——先后调用哪些步骤、按哪个判断分支、以什么
结局结束；读目录、读写数据库、调用引擎、标记正在分析这些干活的事都在步骤里，判断函数只看步骤的结果。

每个流程返回 outcome(...)：kind 是 "ok" 或 "failed"。failed 带 reason（给用户看的中文）和 code，接口层按 code 选状态码：

    invalid（目录不可用）、config（分析配置不合法）、
    no_command（没有填测试命令，testflows）                     → 400
    duplicate（目录已经添加过）、overlap（和已添加的项目重叠）、
    busy（这个项目正在分析或运行测试——测试运行拿同一把锁）、
    moved（要标为看过的那次已经不是最新一次）、
    confirm（测试命令还没确认过，testflows）                    → 409
    not_found（项目不存在）                                     → 404
    analysis（引擎出错）、storage（数据库读写失败）、
    prepare（准备运行的临时文件写不了，testflows）              → 500
    timeout（分析超时，已中止）                                 → 504

分析在子进程里跑（worker.run_in_subprocess，当前目录是数据目录 store.path.parent），超过 PYSTEPMAP_ANALYZE_TIMEOUT 秒
（默认 120）就杀掉；测试给了 analyzer 时在本进程里跑。

同一个项目的分析互斥：本进程里记着哪些项目正在分析（按数据库文件和项目 id），另外拿项目的文件锁（locks.py，
<数据目录>/locks/<项目 id>.lock），挡住别的进程（pystepmap mcp 和 pystepmap serve 共用一个数据目录）；分析期间再来一次
直接返回"正在分析"。标记从运行引擎一直拿到新的分析存好、旧的清理完：别的进程看到 analyzing 变假时，最新一次一定已经
是新结果；比较时用的"上一次"在保存前不会被别人换掉或清理掉。"标记流程变化已看过"拿同一把锁，不会插进比较和保存
之间。流程里不用 try … finally（图上每个结局都是终点）：拿到标记之后的每个结局前都先调用"取消正在分析的标记"；
拿着标记的步骤被 Exception 以外的异常（Ctrl+C 等）打断时，由步骤自己取消标记再往外抛（clear_mark_if_aborted）。

保存一次重新分析之前先定这次的对比基准（base_id）和流程差异（diff）（compare_with_previous）。对比基准只在"标为已看"
时移动（R1b）："有未看的流程变化"是上次标为已看以来（没标过就是第一次分析以来）的全部变化，后面的分析不会把前面
没看的变化挤掉。设上一次分析是 P：
- 没有 P：都没有；
- 图和 P 逐字节相同：沿用 P 的基准、差异和"看过的状态"记号；
- P 有对比基准 B（B 还在库里）：基准仍是 B，差异重新算 B 到这次（行号、节点 id 都变了，不能照抄 P 的）；为空时（改回去了）
  基准仍是 B、没有差异；
- P 没有对比基准（第一次分析、标为已看之后，或者之后一直没改流程）：和 P 比——有变化，基准就是 P；没变化，没有基准，
  P 是"看过的状态"时这次接着带这个记号；
- 读不出旧图、算差异出错：记日志，按没有基准保存，不让这次分析失败。
"标记流程变化已看过"清掉最新一次的差异，并把它记成"看过的状态"（存成以自己为基准，对外报成没有基准，见
Store.clear_base）：/diff 据此说对比基准是"上次标为已看时"还是"第一次分析"。B 被旧版本的服务清理掉了时当作 P 没有基准。
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pystepkit import condition, outcome, step, workflow
from pystepmap.config import ConfigError, parse_config
from pystepmap.diff import diff_graphs, diff_to_json
from pystepmap.model import from_json

from .locks import project_locked, remove_lock, try_lock_project, unlock_project
from .shared import identity
from .sources import root_problem
from .store import (
    DuplicateRootError,
    NewAnalysis,
    OverlapRootError,
    ProjectMissingError,
    Store,
    StoreError,
)
from .worker import Analyzer, analyze_directory, analyze_timeout, run_in_subprocess

logger = logging.getLogger(__name__)

KEEP_ANALYSES = 5  # 每个项目最多保留最近几次分析（另外留最新一次的对比基准那次，见 Store.prune_analyses）
# 标记里的 title、group 要写成字面量：引擎只认字面量，写成常量名时图上取不到（分组会变成默认的"后台任务"）

_busy: set[tuple[str, str]] = set()  # 本进程里正在分析的 (数据库文件, 项目 id)
_busy_lock = threading.Lock()
_add_lock = threading.Lock()  # 同一进程里的"保存新项目"排队

_NO_BASE = {"base_id": None, "diff": None, "acknowledged": False}


# ---------- 普通函数：步骤共用的计算 ----------

def is_analyzing(store: Store, project_id: str) -> bool:
    """这个项目此刻有没有一次分析在进行：本进程在分析，或者别的进程（MCP、另一个服务）拿着它的分析锁。"""
    with _busy_lock:
        if (str(store.path), project_id) in _busy:
            return True
    return project_locked(store.path.parent, project_id)


def clear_mark(mark: dict | None) -> None:
    """取消"正在分析"的标记、释放项目锁。可以重复调用（只释放一次）。"""
    if mark is None:
        return
    with _busy_lock:
        if not mark["marked"]:
            return
        mark["marked"] = False
        _busy.discard(mark["key"])
        fd, mark["fd"] = mark["fd"], None
    unlock_project(fd)


@contextmanager
def clear_mark_if_aborted(mark: dict | None) -> Iterator[None]:
    """拿着标记的步骤被异常打断、异常要往流程外抛时（KeyboardInterrupt、SystemExit，或者流程没有接住的错误），先取消
    "正在分析"的标记、释放项目锁再往外抛：流程后面的"取消正在分析的标记"走不到了，不清掉的话这个项目就再也分析不了。
    流程接住的错误（StoreError 等）也会在这里先取消标记，流程随后再取消一次也没关系（clear_mark 只释放一次）。"""
    try:
        yield
    except BaseException:
        clear_mark(mark)
        raise


def take_mark(store: Store, project_id: str) -> dict:
    """本进程里先占上（挡住本进程的另一次），再拿项目的文件锁（挡住别的进程）。任一个已经被占着时不标记，返回
    {"marked": False}；标记了返回 {"marked": True, "key", "fd"}（fd 是锁文件的描述符，取消标记时释放）。
    锁文件建不了（数据目录出了问题）时只在本进程内互斥，记一条警告。"""
    key = (str(store.path), project_id)
    with _busy_lock:
        if key in _busy:
            return {"marked": False, "key": key, "fd": None}
        _busy.add(key)
    try:
        fd = try_lock_project(store.path.parent, project_id)
    except OSError as e:
        logger.warning("拿不到项目 %s 的分析锁（%s），这次只在本进程内互斥", project_id, e.strerror or e)
        fd = -1
    except BaseException:
        with _busy_lock:
            _busy.discard(key)
        raise
    if fd is None:  # 别的进程正在分析这个项目
        with _busy_lock:
            _busy.discard(key)
        return {"marked": False, "key": key, "fd": None}
    return {"marked": True, "key": key, "fd": fd}


def _has_python_files(root: Path, exclude: set[str]) -> bool:
    """root 下（跳过排除的目录，和引擎建索引时一样）有没有 .py 文件；找到第一个就停。"""
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        if any(f.endswith(".py") for f in filenames):
            return True
    return False


def _inside(child: str, parent: str) -> bool:
    return child != parent and os.path.commonpath([child, parent]) == parent


def _ancestors(path: str) -> set[tuple[int, int]]:
    """path 的每一级上级目录（不含它自己）的 (设备, inode)。"""
    return {i for parent in Path(path).parents if (i := identity(parent)) is not None}


def _overlap_with(projects: list[dict], root: str) -> dict:
    """root 和已添加的项目比：同一个目录（"same"）、在某个项目里面或包含某个项目（"nested"）。路径字符串和 inode
    都比（inode 认得出大小写不同的写法）。返回 {"same", "nested", "reason"}，没有的为 None。
    目录已经不是添加时那个真实目录的项目（被删、被移走或换成了符号链接：root_problem 不为 None）不参与比较：顺着
    换上的链接比会误判（链接指到 / 时任何目录都"在它里面"）；这样的项目重新分析、读源码都会被拒绝，要删除后重新添加。"""
    projects = [p for p in projects if root_problem(p["root"]) is None]
    me, my_ancestors = identity(root), _ancestors(root)
    for p in projects:
        if p["root"] == root or (me is not None and identity(p["root"]) == me):
            return {"same": p, "nested": None, "reason": f"这个目录已经添加过（项目「{p['name']}」）：{root}"}
    for p in projects:
        other = identity(p["root"])
        if _inside(root, p["root"]) or (other is not None and other in my_ancestors):
            return {"same": None, "nested": p,
                    "reason": f"这个目录在已添加的项目「{p['name']}」（{p['root']}）里面，不用再单独添加"}
        if _inside(p["root"], root) or (me is not None and me in _ancestors(p["root"])):
            return {"same": None, "nested": p,
                    "reason": f"这个目录包含已添加的项目「{p['name']}」（{p['root']}）；要分析整个目录，先删除那个项目"}
    return {"same": None, "nested": None, "reason": ""}


def _conflict_in_store(root: str, projects: list[dict]) -> StoreError | None:
    """保存时在写事务里再查一次（Store.add_project 的 conflict）：两次添加同时通过了查重，后保存的在这里被挡下。"""
    found = _overlap_with(projects, root)
    if found["same"] is not None:
        return DuplicateRootError(root, found["same"]["id"])
    if found["nested"] is not None:
        return OverlapRootError(found["reason"], found["nested"]["id"])
    return None


def home_conflict(resolved: Path) -> bool:
    """resolved 是根目录、用户主目录，或者包含用户主目录（/Users、经 /System/Volumes/Data 到达的主目录也算）。"""
    if resolved == Path(resolved.anchor):
        return True
    me, home = identity(resolved), Path.home()
    return me is not None and me in {identity(home), *_ancestors(str(home.resolve()))}


def _compare(store: Store, project_id: str, graph_json: str) -> dict:
    """compare_with_previous 的实际工作（规则见模块说明），返回 {"base_id", "diff", "acknowledged"}。出错时往外抛，
    由步骤接住。"""
    previous = store.latest_analysis(project_id)
    extra = None if previous is None else store.analysis_extra(previous["id"])
    if extra is None:
        return dict(_NO_BASE)
    if extra["graph_sha"] == hashlib.sha256(graph_json.encode("utf-8")).hexdigest():
        return {"base_id": extra["base_id"], "diff": extra["diff"], "acknowledged": extra["acknowledged"]}
    new = from_json(graph_json)
    base_text = None if extra["base_id"] is None else store.graph_json(extra["base_id"])
    if base_text is not None:  # P 有对比基准 B：基准不动，重新算 B 到这次
        diff = diff_graphs(from_json(base_text), new)
        return {"base_id": extra["base_id"], "diff": None if diff.empty else diff_to_json(diff), "acknowledged": False}
    old_text = store.graph_json(previous["id"])
    if old_text is None:  # 刚被别处清理掉了
        return dict(_NO_BASE)
    diff = diff_graphs(from_json(old_text), new)
    if not diff.empty:
        return {"base_id": previous["id"], "diff": diff_to_json(diff), "acknowledged": False}
    return {**_NO_BASE, "acknowledged": extra["acknowledged"]}


# ---------- 步骤 ----------

@step(title="校验分析配置")
def check_config(config: dict | None) -> dict:
    """返回 {"ok", "config"（规整后的配置 JSON，{} 表示全用默认值）, "exclude", "reason"}。"""
    data = {} if config is None else config
    try:
        parsed = parse_config(data)
    except ConfigError as e:
        return {"ok": False, "reason": f"分析配置不合法：{e}"}
    return {"ok": True, "config": dict(data), "exclude": sorted(parsed.exclude_dirs), "reason": ""}


@step(title="规整并检查目录")
def inspect_directory(root: str, checked: dict) -> dict:
    """展开 ~、去掉结尾斜杠、解析符号链接，再看它能不能当项目：存在、是目录、按配置的排除规则看里面有 .py 文件。
    返回 {"ok", "root"（解析后的绝对路径）, "name"（目录名）, "reason"}。"""
    text = root.strip() if isinstance(root, str) else ""
    if not text:
        return {"ok": False, "reason": "请填写项目目录"}
    if "\0" in text:
        return {"ok": False, "reason": "目录路径里不能有空字符（NUL）"}
    try:
        return _examine(Path(text).expanduser(), text, checked["exclude"])
    except (OSError, ValueError) as e:  # 路径太长、某一级没有权限等
        return {"ok": False, "reason": f"目录路径不合法：{getattr(e, 'strerror', None) or e}：{text[:200]}"}


def _examine(path: Path, text: str, exclude: list[str]) -> dict:
    if not path.is_absolute():
        return {"ok": False, "reason": f"请填写绝对路径（可以用 ~ 开头）：{text}"}
    resolved = path.resolve()
    if home_conflict(resolved):
        return {"ok": False, "reason": f"不能把根目录、用户主目录或包含用户主目录的目录当作项目，请选具体的代码目录：{resolved}"}
    if not resolved.exists():
        return {"ok": False, "reason": f"目录不存在：{resolved}"}
    if not resolved.is_dir():
        return {"ok": False, "reason": f"不是目录：{resolved}"}
    if not os.access(resolved, os.R_OK | os.X_OK):
        return {"ok": False, "reason": f"没有权限读取这个目录：{resolved}"}
    if not _has_python_files(resolved, set(exclude)):
        return {"ok": False, "reason": f"目录里没有 .py 文件（排除的目录不算：{', '.join(exclude)}）：{resolved}"}
    return {"ok": True, "root": str(resolved), "name": resolved.name, "reason": ""}


@step(title="查找已添加的同一目录或上下级目录")
def find_overlap(store: Store, root: str) -> dict:
    """返回 {"same": 同一个目录的项目, "nested": 上级或下级目录的项目, "reason"}，没有的为 None（按 inode 比）。"""
    return _overlap_with(store.list_projects(), root)


@step(title="运行分析")
def run_analysis(store: Store, root: str, config: dict, analyzer: Analyzer | None = None,
                 mark: dict | None = None) -> dict:
    """调用引擎分析目录（只读），默认在子进程里、带超时（见 worker）。任何错都在这里接住，变成结果：
    {"ok": True, "graph_json", "summary", "engine_version", "file_lines"} 或 {"ok": False, "code", "reason"}
    （code：invalid、config、analysis、timeout）。mark 是"标记这个项目正在分析"的结果：被 Exception 以外的异常
    （Ctrl+C 等）打断时由这里取消标记（见 clear_mark_if_aborted）。"""
    with clear_mark_if_aborted(mark):
        return _run_engine(store.path.parent, root, config, analyzer)


def _run_engine(home: Path, root: str, config: dict, analyzer: Analyzer | None) -> dict:
    """run_analysis 的实际工作。任何 Exception 都变成结果，不往外抛（标记要靠流程后面的步骤取消）。"""
    try:
        problem = root_problem(root)  # 兜底：检查之后、分析之前目录又被换掉了
        if problem is not None:
            return {"ok": False, "code": "invalid", "reason": problem[1]}
        if analyzer is not None:
            return analyze_directory(root, config, analyzer)
        return run_in_subprocess(root, config, timeout=analyze_timeout(), cwd=home)
    except Exception as e:  # 不让它带走整个请求，项目和上一次的结果保持原样
        logger.exception("分析 %s 失败", root)
        return {"ok": False, "code": "analysis", "reason": f"分析失败：{type(e).__name__}: {e}"}


@step(title="保存项目和这次分析")
def save_new_project(store: Store, target: dict, name: str | None, checked: dict, result: dict) -> dict:
    """项目和它的第一次分析在一个事务里写入，写之前在同一个事务里再查一次重叠（这段时间里别的添加可能已经存进去
    了）。同一目录已经有了时抛 DuplicateRootError，和已有项目重叠时抛 OverlapRootError，写不进去时抛 StoreError。
    同一进程里的添加在这里排队（库的写锁另外挡住别的进程）。"""
    shown = (name or "").strip() or target["name"]
    analysis = NewAnalysis(result["engine_version"], result["summary"], result["graph_json"], config=checked["config"],
                           file_lines=result["file_lines"])  # 第一次分析：没有对比基准
    with _add_lock:
        project_id, analysis_id = store.add_project(name=shown, root=target["root"], config=checked["config"],
                                                    analysis=analysis,
                                                    conflict=functools.partial(_conflict_in_store, target["root"]))
    return {"project_id": project_id, "analysis_id": analysis_id}


@step(title="检查项目目录")
def check_project_directory(root: str) -> dict:
    """添加时存下的根目录现在还是不是同一个真实目录：没被删掉，没被移走后原地换成符号链接（顺着链接分析会读到项目
    之外）。返回 {"ok", "reason"}。"""
    problem = root_problem(root)
    return {"ok": problem is None, "reason": "" if problem is None else problem[1]}


@step(title="标记这个项目正在分析")
def mark_analyzing(store: Store, project_id: str) -> dict:
    """见 take_mark：{"marked", "key", "fd"}。"""
    return take_mark(store, project_id)


@step(title="取消正在分析的标记")
def unmark_analyzing(mark: dict) -> None:
    clear_mark(mark)


@step(title="读取项目")
def load_project(store: Store, project_id: str) -> dict:
    """返回 {"found", "project", "reason"}。"""
    project = store.get_project(project_id)
    if project is None:
        return {"found": False, "project": None, "reason": f"项目不存在（可能已经删除了）：{project_id}"}
    return {"found": True, "project": project, "reason": ""}


@step(title="和上一次分析比较流程")
def compare_with_previous(store: Store, project_id: str, result: dict, mark: dict | None = None) -> dict:
    """定这次的对比基准、算流程差异（规则见模块说明），返回 {"base_id", "diff", "acknowledged"}。读不出旧图、算差异
    出错时记日志，按没有基准处理（都为 None），不让这次分析失败。mark：拿着的标记（见 clear_mark_if_aborted）。"""
    with clear_mark_if_aborted(mark):
        try:
            return _compare(store, project_id, result["graph_json"])
        except Exception:
            logger.exception("项目 %s 和上一次分析比较失败，这次按没有对比基准保存", project_id)
            return dict(_NO_BASE)


@step(title="保存这次分析")
def save_analysis(store: Store, project_id: str, result: dict, config: dict, compared: dict,
                  mark: dict | None = None) -> int:
    """连同这次用的配置、功能线涉及的文件、对比基准和差异一起存。项目不存在了（分析期间被删）时抛
    ProjectMissingError，写不进去时抛 StoreError。"""
    with clear_mark_if_aborted(mark):
        return store.add_analysis(project_id, NewAnalysis(
            result["engine_version"], result["summary"], result["graph_json"], config=config,
            file_lines=result["file_lines"], base_id=compared["base_id"], diff=compared["diff"],
            acknowledged=compared.get("acknowledged", False)))


@step(title="清理旧分析，只留最近 5 次、对比基准和最近一次全量运行用的那次")  # 标题要写成字面量，图上才认得出；改 KEEP_ANALYSES 时一起改
def prune_old_analyses(store: Store, project_id: str, mark: dict | None = None) -> int:
    """最新一次的对比基准那次、最近一次跑完的全量运行用的那次也留着（重新算差异、分出运行之后才有的结局要用它们的图）。清理失败不影响这次分析的结果：记下来，下次再清。"""
    with clear_mark_if_aborted(mark):
        try:
            return store.prune_analyses(project_id, keep=KEEP_ANALYSES)
        except StoreError:
            logger.exception("清理项目 %s 的旧分析失败", project_id)
            return 0


@step(title="删除平台里的项目记录")
def delete_project_records(store: Store, project_id: str) -> bool:
    """删除项目、它的全部分析、手工改的标题分组和测试运行。只删平台里的记录，不动项目的代码。"""
    return store.delete_project(project_id)


@step(title="删掉项目的锁文件")
def delete_lock_file(store: Store, project_id: str) -> bool:
    """<数据目录>/locks/<项目 id>.lock：没人拿着时删掉，有人拿着（还在分析、跑测试）时留着（见 locks.remove_lock）。"""
    return remove_lock(store.path.parent, project_id)


@step(title="存下这次变化，清掉对比基准")
def clear_diff_base(store: Store, project_id: str, analysis_id: int | None = None, mark: dict | None = None) -> dict:
    """最新一次分析的对比基准和流程差异置空：以后的分析从这次起比。analysis_id 是用户看过的那次：最新一次已经不是
    它时不清（Store.clear_base 在同一条语句里判断）。清掉之前，这次的流程差异先存成一条变更记录（同一个事务里，
    每个项目留最近 10 条，见 Store.clear_base）。返回 {"cleared", "reason"}；写不进去时抛 StoreError。"""
    with clear_mark_if_aborted(mark):
        cleared = store.clear_base(project_id, analysis_id=analysis_id)
    return {"cleared": cleared, "reason": "" if cleared else "分析结果已经更新，请先看新的变化"}


# ---------- 判断：只看步骤的结果 ----------

@condition(title="配置不合法？")
def config_is_invalid(checked: dict) -> bool:
    return not checked["ok"]


@condition(title="目录不可用？")
def directory_is_unusable(target: dict) -> bool:
    return not target["ok"]


@condition(title="这个目录已经添加过？")
def already_added(overlap: dict) -> bool:
    return overlap["same"] is not None


@condition(title="和已添加的项目目录重叠？")
def overlaps_project(overlap: dict) -> bool:
    return overlap["nested"] is not None


@condition(title="分析失败？")
def analysis_failed(result: dict) -> bool:
    return not result["ok"]


@condition(title="这个项目正在分析？")
def already_analyzing(mark: dict) -> bool:
    return not mark["marked"]


@condition(title="项目不存在？")
def project_is_missing(found: dict) -> bool:
    return not found["found"]


@condition(title="最新一次已经不是看过的那次？")
def latest_has_moved(cleared: dict) -> bool:
    return not cleared["cleared"]


# ---------- 流程 ----------

@workflow(name="add_project", title="添加项目", group="项目管理")
def add_project(store: Store, root: str, name: str | None = None, config: dict | None = None, *,
                analyzer: Analyzer | None = None) -> dict:
    """校验 → 查重 → 分析 → 入库。分析或保存失败时什么都不留下。"""
    checked = check_config(config)
    if config_is_invalid(checked):
        return outcome("failed", title="配置不合法，没有添加", reason=checked["reason"], code="config")
    target = inspect_directory(root, checked)
    if directory_is_unusable(target):
        return outcome("failed", title="目录不可用，没有添加", reason=target["reason"], code="invalid")
    overlap = find_overlap(store, target["root"])
    if already_added(overlap):
        return outcome("failed", title="目录已经添加过", reason=overlap["reason"], code="duplicate",
                       project_id=overlap["same"]["id"])
    if overlaps_project(overlap):
        return outcome("failed", title="和已添加的项目重叠，没有添加", reason=overlap["reason"], code="overlap")
    result = run_analysis(store, target["root"], checked["config"], analyzer)
    if analysis_failed(result):
        return outcome("failed", title="分析失败，没有添加", reason=result["reason"], code=result["code"])
    try:
        saved = save_new_project(store, target, name, checked, result)
    except DuplicateRootError as e:  # 两次添加同时通过了查重：后保存的这次在写事务里被挡下
        return outcome("failed", title="同时添加了同一目录，这次没有添加", reason=str(e), code="duplicate",
                       project_id=e.project_id)
    except OverlapRootError as e:
        return outcome("failed", title="同时添加了重叠的目录，这次没有添加", reason=str(e), code="overlap")
    except StoreError as e:
        return outcome("failed", title="保存失败，没有添加", reason=str(e), code="storage")
    return outcome("ok", title="项目已添加", project_id=saved["project_id"], analysis_id=saved["analysis_id"],
                   summary=result["summary"])


@workflow(name="reanalyze_project", title="重新分析", group="项目管理")
def reanalyze_project(store: Store, project_id: str, *, analyzer: Analyzer | None = None) -> dict:
    """取项目 → 检查目录 → 标记正在分析 → 分析 → 和上一次比 → 存一次新分析 → 清理旧分析 → 取消标记。失败（包括
    超时）时项目和上一次的结果保持原样；手工改的标题分组不受影响。"""
    found = load_project(store, project_id)
    if project_is_missing(found):
        return outcome("failed", title="项目不存在", reason=found["reason"], code="not_found")
    directory = check_project_directory(found["project"]["root"])
    if directory_is_unusable(directory):
        return outcome("failed", title="项目目录不可用，保留上一次的结果", reason=directory["reason"], code="invalid")
    mark = mark_analyzing(store, project_id)
    if already_analyzing(mark):
        return outcome("failed", title="正在分析，这次没有做", reason="正在分析", code="busy")
    result = run_analysis(store, found["project"]["root"], found["project"]["config"], analyzer, mark)
    if analysis_failed(result):
        unmark_analyzing(mark)
        return outcome("failed", title="分析失败，保留上一次的结果", reason=result["reason"], code=result["code"])
    compared = compare_with_previous(store, project_id, result, mark)
    try:
        analysis_id = save_analysis(store, project_id, result, found["project"]["config"], compared, mark)
    except ProjectMissingError as e:
        unmark_analyzing(mark)
        return outcome("failed", title="项目在分析期间被删除了", reason=str(e), code="not_found")
    except StoreError as e:
        unmark_analyzing(mark)
        return outcome("failed", title="保存失败，保留上一次的结果", reason=str(e), code="storage")
    prune_old_analyses(store, project_id, mark)
    unmark_analyzing(mark)
    return outcome("ok", title="分析完成", analysis_id=analysis_id, summary=result["summary"])


@workflow(name="remove_project", title="删除项目", group="项目管理")
def remove_project(store: Store, project_id: str) -> dict:
    """只删平台里的记录（项目、分析、手工改的标题分组、测试运行）和项目的锁文件，不动项目的代码。"""
    found = load_project(store, project_id)
    if project_is_missing(found):
        return outcome("failed", title="项目不存在", reason=found["reason"], code="not_found")
    try:
        delete_project_records(store, project_id)
    except StoreError as e:
        return outcome("failed", title="删除失败", reason=str(e), code="storage")
    delete_lock_file(store, project_id)
    return outcome("ok", title="项目已删除", project_id=project_id)


@workflow(name="acknowledge_changes", title="标记流程变化已看过", group="项目管理")
def acknowledge_changes(store: Store, project_id: str, analysis_id: int | None = None) -> dict:
    """取项目 → 拿项目的分析锁 → 清掉最新一次分析的对比基准和流程差异 → 放开锁。以后的分析从这次起比；不删也不
    新增分析。analysis_id 是用户看过差异的那次分析（界面总是给出）：最新一次已经换了时不清，结局 moved。正在分析
    （本进程或别的进程）时不做，结局 busy：那次分析存下的会是新的最新一次。"""
    found = load_project(store, project_id)
    if project_is_missing(found):
        return outcome("failed", title="项目不存在", reason=found["reason"], code="not_found")
    mark = mark_analyzing(store, project_id)
    if already_analyzing(mark):
        return outcome("failed", title="正在分析，这次没有标记", reason="正在分析，等分析完再标为看过", code="busy")
    try:
        cleared = clear_diff_base(store, project_id, analysis_id, mark)
    except StoreError as e:
        unmark_analyzing(mark)
        return outcome("failed", title="保存失败，变化仍标着", reason=str(e), code="storage")
    unmark_analyzing(mark)
    if latest_has_moved(cleared):
        return outcome("failed", title="分析结果已经更新，这次没有标记", reason=cleared["reason"], code="moved")
    return outcome("ok", title="流程变化已标为看过", project_id=project_id)
