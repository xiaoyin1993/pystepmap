"""平台的存储：平台数据目录下的一个 SQLite 文件（DB_NAME）。数据目录默认 ~/.pystepmap，环境变量 PYSTEPMAP_HOME 可以改
（default_home）——放在这里而不是 app.py：pystepmap mcp 要用它们，又不能把平台接口和运行测试的代码导入进来。

五张表（结构版本记在 PRAGMA user_version）：

- projects：项目。id 是短的随机串；root 是解析过的绝对路径，唯一；config_json 是项目的分析配置（引擎的配置 JSON，
  用户给的原样，{} 表示全用默认值）。结构版本 3 加的两列：test_command 测试命令（用户填的原文，NULL = 没填）、
  test_confirmed 用户确认过这条命令的影响（0 / 1；命令一改就清零，见 set_test_command、confirm_test_command）。
- analyses：每次分析。summary_json 是摘要（见 worker.summarize）；graph_gz 是 gzip 压缩的图 JSON（引擎输出原样），
  graph_size 是压缩前的字节数；graph_gz 放在最后一列（不取它时 SQLite 不读它的溢出页）。删项目时一起删。
  结构版本 2 加的五列（版本 1 留下的行：config_json、file_lines_json、diff_json 为 NULL，graph_sha、base_id 升级时补上）：
    config_json      这次分析用的配置
    graph_sha        图 JSON 字节的 sha256
    base_id          对比基准的分析 id；NULL = 没有可比的；等于自己的 id = "看过的状态"（标为已看的那次，或之后流程
                     没变、接着带记号的重新分析），对外报成没有基准
    file_lines_json  query.file_lines(图)：相对路径 → 涉及这个文件的功能线 sid
    diff_json        diff_to_json(diff_graphs(基准, 这次))；没有基准或差异为空时 NULL
  保存时定基准、算差异的规则在 flows.compare_with_previous（对比基准只在标为已看时移动）；清理旧分析时另外留下
  最新一次的基准那次。
- reviews（结构版本 5）：变更记录。每次"标为已看"时，把那次的流程差异（diff_json，diff_to_json 的结果原样）连同
  标的时间、是哪次分析相对哪次（head / base 的 id 和时间）、概要（summary_json，见 review_summary）存一条；每个项目
  留最近 KEEP_REVIEWS 条。删项目时一起删。结构版本 6 加 graph_size、graph_gz：head 那次分析的图（压缩好的原样，
  放在最后一列），在画布上回看这条记录用。
- line_overrides：手工改的功能线标题和分组，按功能线的 sid 存；title / group_name 为空表示这一项不改。删项目时一起删。
- runs（结构版本 3）：在平台上运行测试的结果，全量的、只跑一条功能线的各留最近几次（KEEP_RUNS，见 prune_runs）。
  删项目时一起删。结构版本 4 加 scope_json（这次运行的范围，NULL = 全部测试），projects 加 mcp_tests（允许 MCP 请平台
  运行单条功能线的测试，0 / 1）。
    analysis_id   这次运行用的那次分析（不是外键：那次分析以后被清理掉，这一行照样在）
    started_at、finished_at、command（运行时的测试命令原文）
    status        ok 命令退出码 0 / tests_failed 有测试失败 / error 命令没跑成或没收到记录 / timeout / cancelled
    exit_code     命令的退出码；没跑完时 NULL
    summary_json  汇总（形状见 flows.run_project_tests）：计数、耗时、核对结果、覆盖、不符的前几条、中文说明
    paths_size    运行路径 JSON 压缩前的字节数；没有路径时 NULL
    tests_json    每个测试一项 [{test, outcome, seconds, cases, functions}]（取一次运行时用，列运行时不读）
    paths_index   {"meta": {键: [起, 止]}, "tests": {测试 node id: [起, 止]}}：运行一级的各项、每个测试在 paths_gz
                  里的那一段（见 _pack_paths）
    functions_gz  函数索引 {"functions": [运行里的函数 sid，每个一次], "tests": {测试 node id: [下标…]}} 的 gzip（按功能
                  线数一个测试执行了几个函数用，不用解开整份路径）；没跑完时 NULL。本阶段较早的代码建的版本 3 的库没有
                  这一列，打开时补上（_fix_early_v3）
    paths_gz      run_paths_to_json 的结果，一个标准的 gzip（没跑完时 NULL），每个测试前后做了 Z_FULL_FLUSH，一个测试
                  可以只读出、解开它那一段（run_test_paths）；放在最后一列，不取它时 SQLite 不读它的溢出页
  存的只有标识、次数、耗时、退出方式、异常类型名、测试的 node id（去掉参数化后缀）和通过 / 失败——没有任何取值，也
  没有 pytest 的输出（那个只在 runner 的内存里）。

打开时：新库直接建最新版本的表；旧版本的库先复制一份成同目录的 pystepmap.db.bak-v<旧版本>（0600；已经有这个文件时不再
复制、不覆盖；复制失败就不升级，报错），再按 _MIGRATIONS 逐级升级（每一级一个函数），全部在一个写事务里，中途出错
整个回滚、库保持原样；更新版本建的库拒绝打开，且在改任何东西（包括切换 WAL）之前就拒绝。副本是旧版本的 pystepmap
能直接打开的库（把它改名回 pystepmap.db 就回到升级前）。

每次读写各开一个连接（FastAPI 在线程池里调用，连接不跨线程）；写操作用 BEGIN IMMEDIATE 一次拿到写锁，库是 WAL
模式，读不被写挡住；平台服务和 MCP 两个进程可以同时打开同一个库。另外 Store 从打开到 close()（或被回收）一直开着
一个闲着的连接，不用它读写：SQLite 在所有进程里最后一个连接关上时做检查点并删掉 -wal、-shm，下次再建；两个进程
各自每次调用都开关连接时，删和建正好撞上另一个进程在开库、读库，偶尔会读出 disk I/O error（SQLITE_IOERR_SHORT_READ）
之类的错误（库本身没坏）。有这个连接在，-wal、-shm 就一直在。sqlite3 的错误都转成 StoreError（中文信息）。
这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import string
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 6
KEEP_REVIEWS = 10  # 每个项目留最近几条变更记录（标为已看时存的那次流程变化）
DB_NAME = "pystepmap.db"  # 平台数据目录里的库文件
_ID_ALPHABET = string.ascii_lowercase + string.digits
_ID_LENGTH = 8

logger = logging.getLogger(__name__)


def default_home() -> Path:
    """平台数据目录：环境变量 PYSTEPMAP_HOME，没有时 ~/.pystepmap。"""
    return Path(os.environ.get("PYSTEPMAP_HOME") or "~/.pystepmap").expanduser()

SERVE_LOCK = "pystepmap.lock"  # pystepmap serve 的锁文件（cli.LOCK_NAME 同名）：两行，进程号、端口


def serve_port(home: Path) -> int | None:
    """这个数据目录上正在运行的 pystepmap serve 监听的端口；没有在运行（锁文件没人锁着）、读不出端口时 None。
    只读：试着拿一下锁，拿得到说明没有服务在跑，马上放掉。没有 flock 的系统上只看文件里写的端口。"""
    try:
        fd = os.open(Path(home) / SERVE_LOCK, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass  # 有人锁着：服务在跑
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return None
        lines = os.read(fd, 100).decode("utf-8", "replace").split("\n")
        port = int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 0
        return port if 0 < port < 65536 else None
    finally:
        os.close(fd)


_PROJECTS = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    test_command TEXT,
    test_confirmed INTEGER NOT NULL DEFAULT 0,
    mcp_tests INTEGER NOT NULL DEFAULT 0
)"""
_ANALYSES = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    graph_size INTEGER NOT NULL,
    config_json TEXT,
    graph_sha TEXT,
    base_id INTEGER,
    file_lines_json TEXT,
    diff_json TEXT,
    graph_gz BLOB NOT NULL
)"""
_ANALYSES_INDEX = "CREATE INDEX analyses_by_project ON analyses(project_id, id)"
_OVERRIDES = """
CREATE TABLE line_overrides (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    line_sid TEXT NOT NULL,
    title TEXT,
    group_name TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, line_sid)
)"""
_RUNS = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    analysis_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    summary_json TEXT NOT NULL,
    paths_size INTEGER,
    tests_json TEXT,
    paths_index TEXT,
    functions_gz BLOB,
    paths_gz BLOB,
    scope_json TEXT
)"""
_RUNS_INDEX = "CREATE INDEX runs_by_project ON runs(project_id, id)"
_REVIEWS = """
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    acked_at TEXT NOT NULL,
    head_id INTEGER NOT NULL,
    head_at TEXT NOT NULL,
    base_id INTEGER,
    base_at TEXT,
    summary_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    graph_size INTEGER,
    graph_gz BLOB
)"""
_REVIEWS_INDEX = "CREATE INDEX reviews_by_project ON reviews(project_id, id)"
_REVIEW_META = "id, acked_at, head_id, head_at, base_id, base_at, summary_json, graph_size"
# 运行的信息列（不取 tests_json、paths_gz：列运行时不读每个测试的那一列和大字段）
_RUN_META = "id, analysis_id, started_at, finished_at, command, status, exit_code, summary_json, paths_size, scope_json"


def _base_kept(t: str) -> str:
    """SQL：表别名 t 那一行有一个真正可比的对比基准：基准那次还在库里（不在了——旧版本的服务按旧规则清理掉的——就
    当作没有可比的），而且不是它自己（自己做基准是"标为已看"的记号，见 Store.clear_base）。"""
    return f"({t}.base_id IS NOT {t}.id AND EXISTS (SELECT 1 FROM analyses kept WHERE kept.id = {t}.base_id))"


def _meta_columns(t: str, prefix: str = "") -> str:
    """SQL：表别名 t 那一行分析的信息列（列名带 prefix），给 _analysis_meta 读。不取 graph_gz（也不取可能较大的
    file_lines_json、diff_json 本身）。base_id、has_diff 按"有真正可比的基准"算（_base_kept）：基准不在了、或者是
    "标为已看"的记号时 base_id 为 NULL、has_diff 为假。"""
    kept = _base_kept(t)
    return (f"{t}.id AS {prefix}id, {t}.created_at AS {prefix}created_at, {t}.engine_version AS {prefix}engine_version, "
            f"{t}.summary_json AS {prefix}summary_json, {t}.graph_size AS {prefix}graph_size, "
            f"CASE WHEN {kept} THEN {t}.base_id END AS {prefix}base_id, "
            f"({t}.diff_json IS NOT NULL AND {kept}) AS {prefix}has_diff")


_ANALYSIS_META = _meta_columns("a")


def _create_tables(con: sqlite3.Connection) -> None:
    """新库：直接建最新版本的表。"""
    for statement in (_PROJECTS, _ANALYSES.format(name="analyses"), _ANALYSES_INDEX, _OVERRIDES, _RUNS, _RUNS_INDEX,
                      _REVIEWS, _REVIEWS_INDEX):
        con.execute(statement)


def _graph_sha(gz: bytes) -> str | None:
    """压缩存着的图解压后的 sha256；解不开（坏了的数据）时 None。"""
    try:
        return hashlib.sha256(gzip.decompress(gz)).hexdigest()
    except (OSError, EOFError, zlib.error) as e:
        logger.warning("升级时有一张图解不开，这次分析不记指纹：%s", e)
        return None


def _upgrade_1_to_2(con: sqlite3.Connection) -> None:
    """版本 1 → 2：analyses 加五列。按新的列顺序重建这张表（ALTER TABLE ADD COLUMN 只能把列加在 graph_gz 后面，以后
    每次读这些小字段都要先走完大字段的溢出页），id、AUTOINCREMENT 的计数原样保留。已有的每行解压一次算 graph_sha；
    base_id 填"和它之前（同一项目）最近一次 graph_sha 不同的分析"（指纹不明的算不同），没有就 NULL；不算差异
    （diff_json 留 NULL）；config_json、file_lines_json 留 NULL（没有记下来，不猜）。没有对应项目的分析（外键检查
    关着时删了项目留下的）不拷过去，记一条日志：新表有外键约束，拷了会让整个升级失败。"""
    seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'analyses'").fetchone()
    old = "id, project_id, created_at, engine_version, summary_json, graph_size, graph_gz"
    con.execute(_ANALYSES.format(name="analyses_v2"))
    orphans = con.execute("SELECT count(*) FROM analyses WHERE project_id NOT IN (SELECT id FROM projects)").fetchone()[0]
    if orphans:
        logger.warning("升级数据库时丢掉了 %d 次没有对应项目的分析（项目早已删除，分析却留着）", orphans)
    con.execute(f"INSERT INTO analyses_v2 ({old}) SELECT {old} FROM analyses "
                "WHERE project_id IN (SELECT id FROM projects)")
    con.execute("DROP TABLE analyses")  # 它的索引、sqlite_sequence 里的那一行一起删掉
    con.execute("ALTER TABLE analyses_v2 RENAME TO analyses")
    con.execute(_ANALYSES_INDEX)
    if seq is not None:  # 删掉过的最大 id 也不再用（AUTOINCREMENT 的保证）
        cur = con.execute("UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'analyses'", (seq[0],))
        if cur.rowcount == 0:
            con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('analyses', ?)", (seq[0],))
    rows = con.execute("SELECT id, project_id FROM analyses ORDER BY project_id, id").fetchall()
    history: dict[str, list[tuple[int, str | None]]] = {}
    for row in rows:
        gz = con.execute("SELECT graph_gz FROM analyses WHERE id = ?", (row["id"],)).fetchone()["graph_gz"]
        sha = _graph_sha(bytes(gz))
        earlier = history.setdefault(row["project_id"], [])
        base = next((i for i, s in reversed(earlier) if s is None or sha is None or s != sha), None)
        con.execute("UPDATE analyses SET graph_sha = ?, base_id = ? WHERE id = ?", (sha, base, row["id"]))
        earlier.append((row["id"], sha))


def _copy_database(source: Path, target: Path) -> None:
    """把库复制到 target（用 SQLite 的备份接口：-wal 里已提交的内容也在，副本是一个完整的库）。先写到临时文件再改名，
    中途失败不留下半截的 target。target 0600。失败时抛 OSError 或 sqlite3.Error。"""
    tmp = target.with_name(target.name + ".tmp")
    try:
        private_file(tmp)
        src = sqlite3.connect(source)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _upgrade_2_to_3(con: sqlite3.Connection) -> None:
    """版本 2 → 3：projects 加 test_command、test_confirmed 两列（加在最后，和新库的列顺序相同；已有的项目都是没填
    命令、没确认），建 runs 表。已有的数据一行不动。"""
    con.execute("ALTER TABLE projects ADD COLUMN test_command TEXT")
    con.execute("ALTER TABLE projects ADD COLUMN test_confirmed INTEGER NOT NULL DEFAULT 0")
    con.execute(_RUNS)
    con.execute(_RUNS_INDEX)


def _fix_early_v3(con: sqlite3.Connection) -> None:
    """本阶段较早的代码建的结构版本 3 的库（没有发布过）：runs 还没有 functions_gz 这一列时补上（加在最后；已有的
    运行这一列为空，按功能线数函数时报 line_known: false）。"""
    if "functions_gz" not in {r[1] for r in con.execute("PRAGMA table_info(runs)")}:
        con.execute("ALTER TABLE runs ADD COLUMN functions_gz BLOB")


def _upgrade_3_to_4(con: sqlite3.Connection) -> None:
    """版本 3 → 4：projects 加 mcp_tests（允许 MCP 请平台运行单条功能线的测试；已有的项目都是不允许），runs 加
    scope_json（这次运行的范围：NULL = 全部测试；{"line", "line_title", "tests", "base_run"} = 只跑了一条功能线的
    测试）。已有的运行都是全量。已有的数据一行不动。"""
    _fix_early_v3(con)
    con.execute("ALTER TABLE projects ADD COLUMN mcp_tests INTEGER NOT NULL DEFAULT 0")
    # 从版本 1、2 一路升上来的库：runs 是刚才按现在的建表语句建的，已经有这一列
    if "scope_json" not in {r[1] for r in con.execute("PRAGMA table_info(runs)")}:
        con.execute("ALTER TABLE runs ADD COLUMN scope_json TEXT")


def _upgrade_4_to_5(con: sqlite3.Connection) -> None:
    """版本 4 → 5：建 reviews 表（变更记录：每次"标为已看"时存下的那次流程变化）。已有的数据一行不动；升级之前标过
    已看的变化没有存，记录从这以后才有。"""
    con.execute(_REVIEWS)
    con.execute(_REVIEWS_INDEX)


def _upgrade_5_to_6(con: sqlite3.Connection) -> None:
    """版本 5 → 6：reviews 加 graph_size、graph_gz（标为已看的那次分析的图，压缩好的原样；在画布上回看这条记录用）。
    已有的记录这两列为空：只能看文字明细。"""
    have = {r[1] for r in con.execute("PRAGMA table_info(reviews)")}
    for column, kind in (("graph_size", "INTEGER"), ("graph_gz", "BLOB")):
        if column not in have:  # 从更早的版本一路升上来的库：reviews 是刚按现在的建表语句建的，已经有这两列
            con.execute(f"ALTER TABLE reviews ADD COLUMN {column} {kind}")


def review_summary(diff: dict) -> dict:
    """一份流程差异（diff_to_json 的结果）的概要：{"lines": [{"sid", "title", "group", "status"}]（有变化的功能线）,
    "functions": 有变化的函数个数, "added" / "removed" / "changed": 新增 / 删除 / 改动的节点个数}。格式不认识的地方
    当作没有。"""
    lines = [{"sid": ln.get("sid", ""), "title": ln.get("title", ""), "group": ln.get("group", ""),
              "status": ln.get("status", "")} for ln in diff.get("lines") or [] if isinstance(ln, dict)]
    functions = [f for f in (diff.get("functions") or {}).values() if isinstance(f, dict)]
    counts = {"added": 0, "removed": 0, "changed": 0}
    for f in functions:
        for node in f.get("nodes") or []:
            if isinstance(node, dict) and node.get("status") in counts:
                counts[node["status"]] += 1
    return {"lines": lines, "functions": len(functions), **counts}


def _review_meta(row: sqlite3.Row) -> dict:
    """变更记录的信息（没有差异本身）：{id, acked_at, head_id, head_at, base_id, base_at, summary, graph_size}。head 是
    标为已看的那次分析，base 是它当时的对比基准（上一次标为已看时，或第一次分析）；summary 见 review_summary；
    graph_size 是存着的当时的图（head 那次的）压缩前的字节数，没有存图的记录（结构版本 5 时存的）为 None。"""
    return {"id": row["id"], "acked_at": row["acked_at"], "head_id": row["head_id"], "head_at": row["head_at"],
            "base_id": row["base_id"], "base_at": row["base_at"], "summary": json.loads(row["summary_json"]),
            "graph_size": row["graph_size"]}


# 从版本 n 升到 n + 1 的函数。以后加一个版本：SCHEMA_VERSION 加一，这里加一项，_create_tables 建新版本的表
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _upgrade_1_to_2,
    2: _upgrade_2_to_3,
    3: _upgrade_3_to_4,
    4: _upgrade_4_to_5,
    5: _upgrade_5_to_6,
}


class StoreError(Exception):
    """存储读写失败。信息是给用户看的中文。"""


class DuplicateRootError(StoreError):
    """同一个目录已经添加过。"""

    def __init__(self, root: str, project_id: str | None):
        super().__init__(f"这个目录已经添加过：{root}")
        self.root = root
        self.project_id = project_id


class OverlapRootError(StoreError):
    """目录和一个已添加的项目重叠（在它里面，或者包含它）。"""

    def __init__(self, reason: str, project_id: str):
        super().__init__(reason)
        self.project_id = project_id


class ProjectMissingError(StoreError):
    """项目不存在（没有添加过，或者已经删除了）。"""

    def __init__(self, project_id: str):
        super().__init__(f"项目不存在（可能已经删除了）：{project_id}")
        self.project_id = project_id


@dataclass(frozen=True)
class NewAnalysis:
    """一次分析要保存的内容。config 等四项不给时存 NULL（graph_sha 总是按 graph_json 算）。acknowledged 为真时这次分析
    记成"看过的状态"（存成以自己为基准，同 clear_base；base_id、diff 要为 None）：标为已看之后、流程没变的重新分析
    接着带这个记号，下一次改流程时就知道对比基准是"上次标为已看时"。"""

    engine_version: str
    summary: dict
    graph_json: str  # 引擎输出的图 JSON 原文（to_json 的结果）
    config: dict | None = None  # 这次分析用的配置
    file_lines: dict | None = None  # query.file_lines(图)
    base_id: int | None = None  # 对比基准的分析 id
    diff: dict | None = None  # diff_to_json(...)；没有基准或差异为空时 None
    acknowledged: bool = False


@dataclass(frozen=True)
class NewRun:
    """一次测试运行要保存的内容（见模块说明里 runs 表的各列）。tests、paths_json 在没跑完（没有路径）时为 None。"""

    analysis_id: int | None  # 这次运行用的那次分析
    started_at: str
    finished_at: str
    command: str
    status: str  # ok / tests_failed / error / timeout / cancelled
    exit_code: int | None
    summary: dict
    tests: list[dict] | None = None  # [{test, outcome, seconds, cases, functions}]
    paths: dict | None = None  # run_paths_to_json(...) 的结果
    function_index: dict | None = None  # {"functions": [函数 sid…], "tests": {测试 node id: [下标…]}}
    scope: dict | None = None  # None = 全部测试；只跑一条功能线时 {"line", "line_title", "tests", "base_run"}


@dataclass(frozen=True)
class _Packed:
    engine_version: str
    summary_json: str
    graph_size: int
    graph_gz: bytes
    config_json: str | None
    graph_sha: str
    base_id: int | None
    file_lines_json: str | None
    diff_json: str | None
    acknowledged: bool


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


def _dumps(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(text: str | None) -> object:
    return None if text is None else json.loads(text)


def _pack(a: NewAnalysis) -> _Packed:
    """在拿写锁之前压缩好：压缩一张 10 MB 的图要几十毫秒，不该占着锁做。mtime=0：同样的图压出同样的字节。"""
    raw = a.graph_json.encode("utf-8")
    return _Packed(a.engine_version, json.dumps(a.summary, ensure_ascii=False), len(raw),
                   gzip.compress(raw, compresslevel=6, mtime=0), _dumps(a.config), hashlib.sha256(raw).hexdigest(),
                   a.base_id, _dumps(a.file_lines), _dumps(a.diff), a.acknowledged)


def _analysis_meta(row: sqlite3.Row, prefix: str = "") -> dict:
    """分析的信息（没有图数据）：{id, created_at, engine_version, summary, graph_size, base_id, has_diff}。"""
    return {"id": row[prefix + "id"], "created_at": row[prefix + "created_at"],
            "engine_version": row[prefix + "engine_version"], "summary": json.loads(row[prefix + "summary_json"]),
            "graph_size": row[prefix + "graph_size"], "base_id": row[prefix + "base_id"],
            "has_diff": bool(row[prefix + "has_diff"])}


def _project(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "root": row["root"], "config": json.loads(row["config_json"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"], "test_command": row["test_command"],
            "test_confirmed": bool(row["test_confirmed"]), "mcp_tests": bool(row["mcp_tests"])}


def _pack_paths(paths: dict) -> tuple[bytes, int, dict]:
    """运行路径 → (压缩包, 压缩前的字节数, 索引)。压缩包是一个标准的 gzip（解开就是整个 JSON，接口原样发出去）：
    {"<运行一级的项>": 值, …, "tests": [测试, …]}。每个运行一级的值（version、shapes、function_dups……除 tests 以外
    run_paths_to_json 给的每一项）和每个测试的 JSON 前后各做一次 Z_FULL_FLUSH：压缩状态在那里重新开始、按字节对齐，
    所以 [起, 止) 这一段可以单独用原始 deflate 解开，正好是那个值的 JSON（取一个测试时不用解开、解析整个几十 MB 的
    JSON）。索引：{"meta": {键: [起, 止]}, "tests": {测试 node id: [起, 止]}}。mtime 为 0：同样的路径压出同样的字节。
    这里只认 tests 和每个测试的 test；其余内容不看（形状由 pystepmap.runpath 定）。"""
    co = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)  # gzip 头和尾由 zlib 写
    out = bytearray()
    size = 0
    index: dict = {"meta": {}, "tests": {}}

    def put(data: bytes) -> None:
        nonlocal size
        out.extend(co.compress(data))
        size += len(data)

    def piece(data: bytes) -> list[int]:
        out.extend(co.flush(zlib.Z_FULL_FLUSH))
        start = len(out)
        put(data)
        out.extend(co.flush(zlib.Z_FULL_FLUSH))
        return [start, len(out)]

    def dumps(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    put(b"{")
    for key, value in paths.items():
        if key == "tests":
            continue
        put(dumps(key) + b":")
        index["meta"][key] = piece(dumps(value))
        put(b",")
    put(b'"tests":[')
    for i, test in enumerate(paths.get("tests", [])):
        if i:
            put(b",")
        index["tests"][test["test"]] = piece(dumps(test))
    put(b"]}")
    out.extend(co.flush())
    return bytes(out), size, index


def _base_sid(sid: str) -> str:
    """去掉结尾的重名编号 "#数字"（pystepmap.runpath 模块说明里的 base(sid)）。"""
    head, sep, tail = sid.rpartition("#")
    return head if sep and tail.isdigit() else sid


def _run_meta(row: sqlite3.Row) -> dict:
    """运行的信息（没有每个测试的那一列、没有路径）：{id, analysis_id, started_at, finished_at, command, status,
    exit_code, summary, paths_size, scope}；paths_size 是路径 JSON 压缩前的字节数，没有路径时 None；scope 是这次运行的
    范围：None = 全部测试，{"line": 功能线 sid, "line_title", "tests": 名单里的个数, "base_run": 名单取自哪次全量运行} =
    只跑了一条功能线的测试。"""
    return {"id": row["id"], "analysis_id": row["analysis_id"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "command": row["command"], "status": row["status"],
            "exit_code": row["exit_code"], "summary": json.loads(row["summary_json"]), "paths_size": row["paths_size"],
            "scope": None if row["scope_json"] is None else json.loads(row["scope_json"])}


def private_dir(path: Path) -> None:
    """建平台数据目录，只有本人能进（0700）；已有的目录比这宽时收紧（不是自己的目录不动）。里面存着被分析项目的
    源码路径、图和手工改的标题，不该让本机别的用户读到。"""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "posix":
        return
    st = path.stat()
    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) != 0o700:
        path.chmod(0o700)


def private_file(path: Path, create: bool = True) -> None:
    """让文件只有本人能读写（0600）：不存在时（create 为真）新建一个空文件，已有的比这宽时收紧。"""
    if not create and not path.exists():
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "posix" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


class Store:
    """包住一个 SQLite 文件。线程安全：每次调用各开各的连接；另外开着一个闲着的连接（见模块说明），用 close() 关，
    或者 Store 被回收时自动关。数据目录 0700，库文件 0600；SQLite 建 -wal、-shm 时照库文件的权限，所以它们也是 0600。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._keeper: sqlite3.Connection | None = None
        try:
            private_dir(self.path.parent)
            private_file(self.path)  # 先建好空的库文件（0600），SQLite 打开时照用；-wal、-shm 跟着它的权限
            for side in ("-wal", "-shm"):  # 以前的版本留下的：收紧
                private_file(self.path.with_name(self.path.name + side), create=False)
        except OSError as e:
            raise StoreError(f"无法创建平台数据目录 {self.path.parent}：{e.strerror or e}") from None
        with self._connect() as con:
            self._refuse_newer(con)  # 先看版本：更新版本建的库一个字节都不动（切换 WAL 也会改文件头）
            con.execute("PRAGMA journal_mode = WAL")  # 持久设置；不能在事务里改
        with self._write() as con:  # 拿着写锁再看一次：另一个进程（MCP、另一个服务）可能刚升级完
            version = self._refuse_newer(con)
            if version == 0:
                _create_tables(con)
            elif version == SCHEMA_VERSION:
                _fix_early_v3(con)
            else:
                if version < SCHEMA_VERSION:
                    self._keep_old_copy(version)  # 拿着写锁复制：复制期间没人能改库
                for start in range(version, SCHEMA_VERSION):
                    _MIGRATIONS[start](con)
            if version != SCHEMA_VERSION:
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._keeper = self._open_keeper()

    def _keep_old_copy(self, version: int) -> None:
        """升级之前把库复制成 pystepmap.db.bak-v<version>；已经有了就不动。复制不了时抛 StoreError（这次不升级）。"""
        target = self.path.with_name(f"{self.path.name}.bak-v{version}")
        if target.exists():
            return
        try:
            _copy_database(self.path, target)
        except (OSError, sqlite3.Error) as e:
            raise StoreError(f"升级之前没能备份数据库到 {target}：{getattr(e, 'strerror', None) or e}；这次没有升级，"
                             f"库保持原样（结构版本 {version}）") from None
        logger.warning("数据库从结构版本 %d 升级到 %d，升级前的库复制在 %s", version, SCHEMA_VERSION, target)

    def _open_keeper(self) -> sqlite3.Connection:
        """开那个闲着的连接：读一次（连上 -wal、-shm），读完不留读事务（不挡检查点）。可以在任何线程里关。"""
        try:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as e:
            raise StoreError(f"无法打开数据库 {self.path}：{e}") from None
        try:
            cur = con.execute("SELECT count(*) FROM sqlite_master")
            cur.fetchall()
            cur.close()
        except sqlite3.Error as e:
            con.close()
            raise StoreError(f"数据库读写失败：{e}") from None
        return con

    def close(self) -> None:
        """关掉闲着的连接（可以重复调用）。之后照常能读写，只是又回到每次调用开关连接、-wal 可能被删了再建。"""
        keeper, self._keeper = getattr(self, "_keeper", None), None
        if keeper is not None:
            keeper.close()

    def __del__(self) -> None:
        self.close()

    def _refuse_newer(self, con: sqlite3.Connection) -> int:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(f"数据库 {self.path} 是更新版本的 pystepmap 建的（结构版本 {version}，"
                             f"这个版本只认识 {SCHEMA_VERSION}），请升级 pystepmap 或换一个数据目录（PYSTEPMAP_HOME）")
        return version

    # ---------- 连接 ----------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # 自己管事务
        except sqlite3.Error as e:
            raise StoreError(f"无法打开数据库 {self.path}：{e}") from None
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys = ON")
            yield con
        except sqlite3.Error as e:
            raise StoreError(f"数据库读写失败：{e}") from None
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """一个写事务：正常结束时提交，出任何异常都回滚。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                if con.in_transaction:  # 有的错误（磁盘满等）SQLite 已经自己回滚了，再回滚会盖掉原来的错误
                    con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    # ---------- 项目 ----------

    def add_project(self, *, name: str, root: str, config: dict, analysis: NewAnalysis,
                    conflict: Callable[[list[dict]], StoreError | None] | None = None) -> tuple[str, int]:
        """新项目和它的第一次分析，在同一个事务里写入：要么都在，要么都不在。返回 (项目 id, 分析 id)。
        同一个 root 已经有了时抛 DuplicateRootError。conflict 给出时，在写事务里（拿着写锁，别的写入进不来）用全部
        已有项目（[{"id", "name", "root"}]）调用它，它返回的错误照抛：查重和写入之间不会插进别的项目。"""
        packed = _pack(analysis)
        config_json = json.dumps(config, ensure_ascii=False)
        with self._write() as con:
            same = con.execute("SELECT id FROM projects WHERE root = ?", (root,)).fetchone()
            if same is not None:
                raise DuplicateRootError(root, same["id"])
            if conflict is not None:
                rows = con.execute("SELECT id, name, root FROM projects ORDER BY created_at, rowid").fetchall()
                error = conflict([dict(r) for r in rows])
                if error is not None:
                    raise error
            project_id = _new_id()
            while con.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                project_id = _new_id()
            now = _now()
            con.execute("INSERT INTO projects (id, name, root, config_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (project_id, name, root, config_json, now, now))
            analysis_id = self._insert_analysis(con, project_id, packed, now)
        return project_id, analysis_id

    def get_project(self, project_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return None if row is None else _project(row)

    def find_project_by_root(self, root: str) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM projects WHERE root = ?", (root,)).fetchone()
        return None if row is None else _project(row)

    def list_projects(self) -> list[dict]:
        """全部项目（按添加的先后），每个带最新一次分析的信息（"analysis"，没有图数据）。"""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT p.*, {_meta_columns('a', 'a_')} "
                "FROM projects p LEFT JOIN analyses a "
                "ON a.id = (SELECT max(id) FROM analyses WHERE project_id = p.id) "
                "ORDER BY p.created_at, p.rowid").fetchall()
        out = []
        for row in rows:
            item = _project(row)
            item["analysis"] = None if row["a_id"] is None else _analysis_meta(row, "a_")
            out.append(item)
        return out

    def delete_project(self, project_id: str) -> bool:
        """删除项目和它的全部分析、手工项、测试运行。项目不存在时返回 False。"""
        with self._write() as con:
            return con.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0

    def set_config(self, project_id: str, config: dict) -> bool:
        with self._write() as con:
            cur = con.execute("UPDATE projects SET config_json = ?, updated_at = ? WHERE id = ?",
                              (json.dumps(config, ensure_ascii=False), _now(), project_id))
            return cur.rowcount > 0

    # ---------- 分析 ----------

    def _insert_analysis(self, con: sqlite3.Connection, project_id: str, packed: _Packed, now: str) -> int:
        cur = con.execute(
            "INSERT INTO analyses (project_id, created_at, engine_version, summary_json, graph_size, config_json, "
            "graph_sha, base_id, file_lines_json, diff_json, graph_gz) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, now, packed.engine_version, packed.summary_json, packed.graph_size, packed.config_json,
             packed.graph_sha, packed.base_id, packed.file_lines_json, packed.diff_json, packed.graph_gz))
        analysis_id = int(cur.lastrowid)
        if packed.acknowledged:  # "看过的状态"：以自己为基准（id 插入后才知道，同一个事务里补上）
            con.execute("UPDATE analyses SET base_id = id, diff_json = NULL WHERE id = ?", (analysis_id,))
        return analysis_id

    def add_analysis(self, project_id: str, analysis: NewAnalysis) -> int:
        """给已有项目存一次新分析，返回分析 id。项目不存在（比如分析期间被删了）时抛 ProjectMissingError。"""
        packed = _pack(analysis)
        with self._write() as con:
            if con.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ProjectMissingError(project_id)
            now = _now()
            analysis_id = self._insert_analysis(con, project_id, packed, now)
            con.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        return analysis_id

    def list_analyses(self, project_id: str) -> list[dict]:
        """项目的全部分析（不含图数据），新的在前。"""
        with self._connect() as con:
            rows = con.execute(f"SELECT {_ANALYSIS_META} FROM analyses a WHERE a.project_id = ? ORDER BY a.id DESC",
                               (project_id,)).fetchall()
        return [_analysis_meta(r) for r in rows]

    def latest_analysis(self, project_id: str) -> dict | None:
        """项目最新一次分析的信息（没有时 None）"""
        with self._connect() as con:
            row = con.execute(f"SELECT {_ANALYSIS_META} FROM analyses a WHERE a.project_id = ? ORDER BY a.id DESC LIMIT 1",
                              (project_id,)).fetchone()
        return None if row is None else _analysis_meta(row)

    def analysis_extra(self, analysis_id: int) -> dict | None:
        """结构版本 2 加的那些：{"config", "graph_sha", "file_lines", "base_id", "diff", "acknowledged"}（JSON 的已解析；
        版本 1 留下的行 config、file_lines、diff 为 None）。没有真正可比的基准（基准那次已经不在了，或者这次是"标为已看"
        的记号）时 base_id、diff 都为 None；acknowledged：这次分析是"看过的状态"（标为已看的那次，或者之后流程没变、接着
        带记号的重新分析）。分析不存在（已被清理）时 None。"""
        kept = _base_kept("a")
        with self._connect() as con:
            row = con.execute(f"SELECT a.config_json, a.graph_sha, a.file_lines_json, "
                              f"CASE WHEN {kept} THEN a.base_id END AS base_id, "
                              f"CASE WHEN {kept} THEN a.diff_json END AS diff_json, "
                              "a.base_id IS a.id AS acknowledged "
                              "FROM analyses a WHERE a.id = ?", (analysis_id,)).fetchone()
        if row is None:
            return None
        return {"config": _loads(row["config_json"]), "graph_sha": row["graph_sha"],
                "file_lines": _loads(row["file_lines_json"]), "base_id": row["base_id"], "diff": _loads(row["diff_json"]),
                "acknowledged": bool(row["acknowledged"])}

    def latest_diff(self, project_id: str) -> dict | None:
        """最新一次分析的流程差异：{"head": 分析信息, "base": 基准那次的分析信息 | None, "diff": 差异 | None,
        "baseline": {"kind", "at"} | None}，一条语句读出（同一个快照，不会一半是别的进程写之前、一半是写之后）。没有
        真正可比的基准（基准那次已经不在了，或者最新一次就是标为已看的那次）时 base、diff、baseline 都为 None。项目
        没有分析时 None。
        baseline 说对比基准代表的是什么状态：kind "acknowledged"——上次标为已看时（基准那次是"看过的状态"），at 是
        基准那次分析的时间；kind "first"——没标为已看过，相对第一次分析，at 是第一次分析的时间（添加项目的时间）。"""
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_meta_columns('h', 'h_')}, {_meta_columns('b', 'b_')}, h.diff_json AS diff_json, "
                "b.base_id IS b.id AS b_acknowledged, p.created_at AS p_created_at "
                "FROM analyses h JOIN projects p ON p.id = h.project_id "
                "LEFT JOIN analyses b ON b.id = h.base_id AND h.base_id IS NOT h.id "
                "WHERE h.id = (SELECT max(id) FROM analyses WHERE project_id = ?)", (project_id,)).fetchone()
        if row is None:
            return None
        base = None if row["b_id"] is None else _analysis_meta(row, "b_")
        baseline = None if base is None else (
            {"kind": "acknowledged", "at": base["created_at"]} if row["b_acknowledged"]
            else {"kind": "first", "at": row["p_created_at"]})
        return {"head": _analysis_meta(row, "h_"), "base": base,
                "diff": None if base is None else _loads(row["diff_json"]), "baseline": baseline}

    def clear_base(self, project_id: str, analysis_id: int | None = None) -> bool:
        """把项目最新一次分析标为已看：清掉它的流程差异，记成"看过的状态"（以自己为基准：base_id = 自己的 id，对外报
        成没有基准）。以后的分析从这个状态起比，界面能说"相对上次标为已看时"。analysis_id 给出时（用户看的是哪一次），
        只在最新一次还是它时才清——同一条语句里判断，别的进程刚存了新的一次时不会误清。清了返回 True；最新一次已经
        不是 analysis_id，或者项目没有分析（不存在）时 False。
        清掉之前，这次的流程差异（有、而且基准那次还在时）先存成一条变更记录（reviews），同一个事务里；每个项目只留
        最近 KEEP_REVIEWS 条。"""
        with self._write() as con:
            head = con.execute(
                "SELECT a.id, a.created_at, a.base_id, a.diff_json, a.graph_size, a.graph_gz, b.created_at AS base_at "
                "FROM analyses a "
                "LEFT JOIN analyses b ON b.id = a.base_id "
                "WHERE a.id = (SELECT max(id) FROM analyses WHERE project_id = ?) AND (? IS NULL OR a.id = ?)",
                (project_id, analysis_id, analysis_id)).fetchone()
            if head is not None and head["diff_json"] is not None and head["base_id"] != head["id"] \
                    and head["base_at"] is not None:
                try:
                    summary = review_summary(json.loads(head["diff_json"]))
                except ValueError:
                    summary = review_summary({})
                con.execute("INSERT INTO reviews (project_id, acked_at, head_id, head_at, base_id, base_at, summary_json, "
                            "diff_json, graph_size, graph_gz) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (project_id, _now(), head["id"], head["created_at"], head["base_id"], head["base_at"],
                             json.dumps(summary, ensure_ascii=False), head["diff_json"], head["graph_size"],
                             head["graph_gz"]))
                con.execute("DELETE FROM reviews WHERE project_id = ? AND id NOT IN "
                            "(SELECT id FROM reviews WHERE project_id = ? ORDER BY id DESC LIMIT ?)",
                            (project_id, project_id, KEEP_REVIEWS))
            cur = con.execute("UPDATE analyses SET base_id = id, diff_json = NULL "
                              "WHERE id = (SELECT max(id) FROM analyses WHERE project_id = ?) AND (? IS NULL OR id = ?)",
                              (project_id, analysis_id, analysis_id))
            return cur.rowcount > 0

    def list_reviews(self, project_id: str) -> list[dict]:
        """项目的变更记录（信息，不含差异本身），新的在前。"""
        with self._connect() as con:
            rows = con.execute(f"SELECT {_REVIEW_META} FROM reviews WHERE project_id = ? ORDER BY id DESC",
                               (project_id,)).fetchall()
        return [_review_meta(r) for r in rows]

    def get_review(self, project_id: str, review_id: int) -> dict | None:
        """一条变更记录的信息加 "diff"（diff_to_json 的结果，存的时候的原样）。不是这个项目的、不存在的：None。"""
        with self._connect() as con:
            row = con.execute(f"SELECT {_REVIEW_META}, diff_json FROM reviews WHERE id = ? AND project_id = ?",
                              (review_id, project_id)).fetchone()
        return None if row is None else {**_review_meta(row), "diff": json.loads(row["diff_json"])}

    def review_graph_gz(self, project_id: str, review_id: int) -> bytes | None:
        """一条变更记录存着的当时的图（压缩好的原样）。记录不存在、不是这个项目的、没有存图时 None。"""
        with self._connect() as con:
            row = con.execute("SELECT graph_gz FROM reviews WHERE id = ? AND project_id = ?", (review_id, project_id)).fetchone()
        return None if row is None or row["graph_gz"] is None else bytes(row["graph_gz"])

    def prune_analyses(self, project_id: str, keep: int) -> int:
        """只留项目最近的 keep 次分析，另外留下最新一次的 base_id 指向的那次（重新算差异要用它的图），以及最近一次
        跑完的全量运行用的那次（没走到的结局要用它的图分出哪些函数、结局是运行之后才有的）。返回删掉了几次。"""
        with self._write() as con:
            cur = con.execute(
                "DELETE FROM analyses WHERE project_id = ? AND id NOT IN "
                "(SELECT id FROM analyses WHERE project_id = ? ORDER BY id DESC LIMIT ?) "
                "AND id IS NOT (SELECT base_id FROM analyses WHERE project_id = ? ORDER BY id DESC LIMIT 1) "
                "AND id IS NOT (SELECT analysis_id FROM runs WHERE project_id = ? AND scope_json IS NULL "
                "AND paths_size IS NOT NULL ORDER BY id DESC LIMIT 1)",
                (project_id, project_id, keep, project_id, project_id))
            return cur.rowcount

    def graph_gz(self, analysis_id: int) -> bytes | None:
        """存着的压缩图数据，原样（接口直接带 Content-Encoding: gzip 发出去）。"""
        with self._connect() as con:
            row = con.execute("SELECT graph_gz FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        return None if row is None else bytes(row["graph_gz"])

    def graph_json(self, analysis_id: int) -> str | None:
        gz = self.graph_gz(analysis_id)
        return None if gz is None else gzip.decompress(gz).decode("utf-8")

    # ---------- 手工改的标题和分组 ----------

    def get_overrides(self, project_id: str) -> dict[str, dict]:
        """{功能线 sid: {"title"?: …, "group"?: …}}，只列出改了的项。"""
        with self._connect() as con:
            rows = con.execute("SELECT line_sid, title, group_name FROM line_overrides WHERE project_id = ? "
                               "ORDER BY line_sid", (project_id,)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            item = {}
            if r["title"] is not None:
                item["title"] = r["title"]
            if r["group_name"] is not None:
                item["group"] = r["group_name"]
            out[r["line_sid"]] = item
        return out

    def set_override(self, project_id: str, line_sid: str, title: str | None, group: str | None) -> None:
        """整条改写一条手工项；title、group 都是 None 时删掉这条。项目不存在时抛 ProjectMissingError。"""
        with self._write() as con:
            if con.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ProjectMissingError(project_id)
            if title is None and group is None:
                con.execute("DELETE FROM line_overrides WHERE project_id = ? AND line_sid = ?", (project_id, line_sid))
                return
            con.execute(
                "INSERT INTO line_overrides (project_id, line_sid, title, group_name, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (project_id, line_sid) DO UPDATE SET title = excluded.title, "
                "group_name = excluded.group_name, updated_at = excluded.updated_at",
                (project_id, line_sid, title, group, _now()))

    # ---------- 测试命令 ----------

    def get_test_command(self, project_id: str) -> dict | None:
        """{"command": 测试命令 | None, "confirmed": 用户确认过这条命令}；项目不存在时 None。"""
        with self._connect() as con:
            row = con.execute("SELECT test_command, test_confirmed FROM projects WHERE id = ?", (project_id,)).fetchone()
        return None if row is None else {"command": row["test_command"], "confirmed": bool(row["test_confirmed"])}

    def set_test_command(self, project_id: str, command: str | None) -> dict:
        """改测试命令（None = 清掉）。和原来的不同时确认清零；相同时保持。返回改后的 {"command", "confirmed"}。项目
        不存在时抛 ProjectMissingError。"""
        with self._write() as con:
            row = con.execute("SELECT test_command, test_confirmed FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise ProjectMissingError(project_id)
            confirmed = command is not None and command == row["test_command"] and bool(row["test_confirmed"])
            con.execute("UPDATE projects SET test_command = ?, test_confirmed = ?, updated_at = ? WHERE id = ?",
                        (command, int(confirmed), _now(), project_id))
        return {"command": command, "confirmed": confirmed}

    def confirm_test_command(self, project_id: str, command: str | None) -> bool:
        """用户确认了 command 的影响：只在项目现在的测试命令还是它时记下（同一条语句里判断，别处刚改了命令时不会把
        新命令当成确认过）。记下了返回 True。"""
        if command is None:
            return False
        with self._write() as con:
            cur = con.execute("UPDATE projects SET test_confirmed = 1 WHERE id = ? AND test_command = ?",
                              (project_id, command))
            return cur.rowcount > 0

    def set_mcp_tests(self, project_id: str, allow: bool) -> bool:
        """允许 / 不允许 MCP 请平台运行这个项目单条功能线的测试。返回改后的值；项目不存在时抛 ProjectMissingError。"""
        with self._write() as con:
            cur = con.execute("UPDATE projects SET mcp_tests = ? WHERE id = ?", (int(bool(allow)), project_id))
            if cur.rowcount == 0:
                raise ProjectMissingError(project_id)
        return bool(allow)

    def latest_full_run(self, project_id: str) -> dict | None:
        """最近一次跑完、有运行路径的**全量**运行的信息（没有时 None）：没走到的结局、单条线要跑哪些测试都用它。"""
        with self._connect() as con:
            row = con.execute(f"SELECT {_RUN_META} FROM runs WHERE project_id = ? AND scope_json IS NULL "
                              "AND paths_size IS NOT NULL ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        return None if row is None else _run_meta(row)

    # ---------- 测试运行 ----------

    def add_run(self, project_id: str, run: NewRun) -> int:
        """存一次运行，返回运行 id。路径在拿写锁之前压缩好（_pack_paths）。项目不存在（运行期间被删）时抛
        ProjectMissingError。"""
        gz, size, index = (None, None, None) if run.paths is None else _pack_paths(run.paths)
        tests = None if run.tests is None else json.dumps(run.tests, ensure_ascii=False)
        summary = json.dumps(run.summary, ensure_ascii=False)
        functions = None if run.function_index is None else gzip.compress(
            json.dumps(run.function_index, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6, mtime=0)
        with self._write() as con:
            if con.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise ProjectMissingError(project_id)
            cur = con.execute(
                "INSERT INTO runs (project_id, analysis_id, started_at, finished_at, command, status, exit_code, "
                "summary_json, paths_size, tests_json, paths_index, functions_gz, paths_gz, scope_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, run.analysis_id, run.started_at, run.finished_at, run.command, run.status, run.exit_code,
                 summary, size, tests, _dumps(index), functions, gz,
                 None if run.scope is None else json.dumps(run.scope, ensure_ascii=False)))
            return int(cur.lastrowid)

    def list_runs(self, project_id: str) -> list[dict]:
        """项目的全部运行（信息，不含每个测试和路径），新的在前。"""
        with self._connect() as con:
            rows = con.execute(f"SELECT {_RUN_META} FROM runs WHERE project_id = ? ORDER BY id DESC",
                               (project_id,)).fetchall()
        return [_run_meta(r) for r in rows]

    def latest_run(self, project_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(f"SELECT {_RUN_META} FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                              (project_id,)).fetchone()
        return None if row is None else _run_meta(row)

    def run_info(self, project_id: str, run_id: int) -> dict | None:
        """一次运行的信息（同 list_runs 的一项，不读每个测试的那一列）。不是这个项目的、不存在的：None。"""
        with self._connect() as con:
            row = con.execute(f"SELECT {_RUN_META} FROM runs WHERE id = ? AND project_id = ?",
                              (run_id, project_id)).fetchone()
        return None if row is None else _run_meta(row)

    def get_run(self, project_id: str, run_id: int) -> dict | None:
        """一次运行的信息加 "tests"（每个测试一项；没跑完的运行为空表）。不是这个项目的、不存在的：None。"""
        with self._connect() as con:
            row = con.execute(f"SELECT {_RUN_META}, tests_json FROM runs WHERE id = ? AND project_id = ?",
                              (run_id, project_id)).fetchone()
        if row is None:
            return None
        return {**_run_meta(row), "tests": [] if row["tests_json"] is None else json.loads(row["tests_json"])}

    def run_paths_gz(self, project_id: str, run_id: int) -> bytes | None:
        """存着的压缩运行路径，原样（接口直接带 Content-Encoding: gzip 发出去）。运行不存在、不是这个项目的、没有路径
        （没跑完）时 None。"""
        with self._connect() as con:
            row = con.execute("SELECT paths_gz FROM runs WHERE id = ? AND project_id = ?", (run_id, project_id)).fetchone()
        return None if row is None or row["paths_gz"] is None else bytes(row["paths_gz"])

    def _segments(self, project_id: str, run_id: int, test: str | None) -> tuple[dict, dict] | None:
        """按 paths_index 只读出压缩包里运行一级的各段（和 test 给出时那个测试的一段），各自解开、解析：
        ({键: 值}, {"test": 那个测试} 或 {})。运行不存在、不是这个项目的、没有路径、没有这个测试时 None。"""
        with self._connect() as con:
            con.execute("BEGIN")  # 索引和压缩包在同一个读事务里读：中间别的连接清理掉这次运行，看到的也是同一个快照
            try:
                row = con.execute("SELECT paths_index FROM runs WHERE id = ? AND project_id = ?",
                                  (run_id, project_id)).fetchone()
                index = None if row is None or row["paths_index"] is None else json.loads(row["paths_index"])
                if index is None or (test is not None and test not in index["tests"]):
                    return None
                spans = {("meta", k): v for k, v in index["meta"].items()}
                if test is not None:
                    spans[("test", test)] = index["tests"][test]
                pieces: dict[tuple[str, str], bytes] = {}
                with con.blobopen("runs", "paths_gz", run_id, readonly=True) as blob:
                    for name, (start, end) in spans.items():
                        blob.seek(start)
                        pieces[name] = blob.read(end - start)
            finally:
                con.execute("COMMIT")
        try:
            values = {name: json.loads(zlib.decompressobj(-zlib.MAX_WBITS).decompress(data))
                      for name, data in pieces.items()}
        except (zlib.error, ValueError) as e:
            raise StoreError(f"运行 #{run_id} 存着的路径解不开：{e}") from None
        meta = {k: values[("meta", k)] for k in index["meta"]}
        return meta, ({} if test is None else {"test": values[("test", test)]})

    def run_paths_meta(self, project_id: str, run_id: int) -> dict | None:
        """运行一级的各项（version、shapes、function_dups……，就是 run_paths_to_json 里 tests 以外的那些），只读那几段。
        没有路径时 None。"""
        found = self._segments(project_id, run_id, None)
        return None if found is None else found[0]

    def run_test_paths(self, project_id: str, run_id: int, test: str) -> dict | None:
        """一次运行里一个测试的路径，和整份同一个形状：{运行一级的各项, "tests": [这个测试]}；其中 shapes（每个函数
        运行时的形状）只留这个测试里有的函数和重名组（基础 sid 在 function_dups 里）的每个定义——core 的放置规则要用
        整组判断"这一组变没变"（pystepmap.runpath 模块说明"叠到图上"）；别的项（version、function_dups……）原样。
        按 paths_index 只读出压缩包里这几段（SQLite 的增量读，不读整个大字段）单独解开。运行不存在、不是这个项目的、
        没有路径、没有这个测试时 None。test 为 "" 是测试之外的执行（收集测试时导入模块等）。"""
        found = self._segments(project_id, run_id, test)
        if found is None:
            return None
        meta, picked = found
        one = picked["test"]
        dups = set(meta.get("function_dups") or ())
        out = {}
        for key, value in meta.items():
            if key == "shapes" and isinstance(value, dict):  # 这个测试的函数，加上重名组的每个定义（core 判断这一组变没变要用）
                mine = one.get("functions", {})
                value = {f: v for f, v in value.items() if f in mine or _base_sid(f) in dups}
            out[key] = value
        out["tests"] = [one]
        return out

    def run_function_index(self, project_id: str, run_id: int) -> dict | None:
        """这次运行的函数索引 {"functions": [函数 sid…], "tests": {测试 node id: [下标…]}}（存的时候算好的，见
        NewRun.function_index）。没有（没跑完、本阶段较早的代码存的运行）时 None。"""
        with self._connect() as con:
            row = con.execute("SELECT functions_gz FROM runs WHERE id = ? AND project_id = ?",
                              (run_id, project_id)).fetchone()
        if row is None or row["functions_gz"] is None:
            return None
        return json.loads(gzip.decompress(row["functions_gz"]))

    def prune_runs(self, project_id: str, keep: int) -> int:
        """全量的运行、只跑一条功能线的运行各留项目最近的 keep 次（分开数：单条线的运行跑得勤，不该把全量的挤掉——
        没走到的结局、单条线要跑哪些测试都靠全量的那次），返回删掉了几次。"""
        with self._write() as con:
            cur = con.execute(
                "DELETE FROM runs WHERE project_id = ? AND id NOT IN "
                "(SELECT id FROM runs WHERE project_id = ? AND scope_json IS NULL ORDER BY id DESC LIMIT ?) AND id NOT IN "
                "(SELECT id FROM runs WHERE project_id = ? AND scope_json IS NOT NULL ORDER BY id DESC LIMIT ?)",
                (project_id, project_id, keep, project_id, keep))
            return cur.rowcount
