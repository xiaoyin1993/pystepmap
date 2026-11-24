"""五条流程：添加项目、重新分析、删除项目、标记流程变化已看过、运行测试并核对。每种添加失败都有中文说明且不留半截数据；分析或保存
失败（包括超时）时项目和上一次的结果保持原样；重新分析保留手工项、只留最近 5 次（另留对比基准那次）；保存时定对比
基准、算流程差异（只改了格式时沿用上一次的基准）；同一项目的分析在进程内、进程间都互斥。最后用 pystepmap 分析它自己，
确认这五个流程各是一条功能线、节点标题是中文、约束检查零违规、每个结局都是终点。

运行测试并核对：用 tmp_path 里转账示例的副本（含它的测试）真的跑 pytest（挂记录插件）：正常、测试失败、命令不跑
测试、运行前图过期、运行期间改代码、超时、取消、正在分析、各种结局之后锁和临时目录都放掉、库里没有任何取值。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pystepmap.analyze import analyze_project, analyze_with_notes
from pystepmap.lint import check_project
from pystepmap.model import from_json, to_json
from pystepmap.query import file_lines
from pystepmap_server import flows, runner, testflows, worker
from pystepmap_server.flows import (
    KEEP_ANALYSES,
    acknowledge_changes,
    add_project,
    is_analyzing,
    reanalyze_project,
    remove_project,
)
from pystepmap_server.locks import project_locked
from pystepmap_server.store import Store, StoreError
from pystepmap_server.testflows import KEEP_RUNS, run_project_tests

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRANSFER = REPO / "examples" / "transfer"


# 夹具写在这里，不放 conftest.py：core/tests 的测试 `from conftest import …`，再有一个同名模块会把它顶掉
@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "pystepmap-home" / "pystepmap.db")


@pytest.fixture
def shop(tmp_path) -> Path:
    """夹具项目 shop 的一份临时拷贝（测试会改它的代码再重新分析）。"""
    dst = tmp_path / "work" / "shop"
    shutil.copytree(FIXTURES / "shop", dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def snapshot(store) -> str:
    """库里能看到的全部内容（项目、每个项目的分析和手工项），用来断言"什么都没变"。"""
    projects = store.list_projects()
    return json.dumps([{**p, "analyses": store.list_analyses(p["id"]), "overrides": store.get_overrides(p["id"])}
                       for p in projects], ensure_ascii=False, sort_keys=True)


def failed(result: dict, code: str, pattern: str) -> None:
    assert result["kind"] == "failed", result
    assert result["code"] == code, result
    assert re.search(pattern, result["reason"]), result["reason"]


# ---------- 添加项目 ----------

def test_add_transfer_example(store):
    r = add_project(store, str(TRANSFER), None, None)
    assert r["kind"] == "ok" and r["title"] == "项目已添加"
    s = r["summary"]
    assert s["lines"] == 1 and s["functions"] > 10 and s["skipped_files"] == [] and "pystepkit" in s["rules"]
    assert s["unreachable"] == 0 and s["dead_ends"] == 0
    p = store.get_project(r["project_id"])
    assert p["root"] == str(TRANSFER.resolve()) and p["name"] == "transfer" and p["config"] == {}
    latest = store.latest_analysis(p["id"])
    assert latest["id"] == r["analysis_id"] and latest["engine_version"] == worker.ENGINE_VERSION
    graph = analyze_project(TRANSFER)
    text = to_json(graph)
    assert store.graph_json(latest["id"]) == text  # 引擎输出原样（子进程里分析的）
    # 第一次分析：没有对比基准；其余新列照填
    assert store.analysis_extra(latest["id"]) == {"config": {}, "graph_sha": hashlib.sha256(text.encode()).hexdigest(),
                                                  "file_lines": file_lines(graph), "base_id": None, "diff": None,
                                                  "acknowledged": False}
    assert (latest["base_id"], latest["has_diff"]) == (None, False)


def test_add_with_name_config_tilde_and_trailing_slash(store, shop, monkeypatch):
    monkeypatch.setenv("HOME", str(shop.parent))
    r = add_project(store, "~/shop/", "  我的商店  ", {"exclude": ["tests"]})
    assert r["kind"] == "ok", r
    p = store.get_project(r["project_id"])
    assert p["root"] == str(shop.resolve()) and p["name"] == "我的商店" and p["config"] == {"exclude": ["tests"]}
    assert r["summary"]["lines"] == 1


@pytest.mark.parametrize(("root", "code", "pattern"), [
    ("", "invalid", "请填写项目目录"),
    ("   ", "invalid", "请填写项目目录"),
    ("relative/dir", "invalid", "绝对路径"),
    ("/", "invalid", "根目录、用户主目录"),
    ("/tmp/x\x00y", "invalid", "空字符"),
    ("/" + "a" * 5000, "invalid", "目录路径不合法"),
    ("/" + "/".join(["b" * 200] * 10), "invalid", "目录"),
])
def test_add_refuses_bad_paths(store, root, code, pattern):
    failed(add_project(store, root, None, None), code, pattern)
    assert store.list_projects() == []


def test_add_refuses_missing_file_and_empty_dirs(store, tmp_path):
    failed(add_project(store, str(tmp_path / "nope"), None, None), "invalid", "目录不存在")
    (tmp_path / "f.py").write_text("x = 1\n")
    failed(add_project(store, str(tmp_path / "f.py"), None, None), "invalid", "不是目录")
    failed(add_project(store, str(FIXTURES / "no_python"), None, None), "invalid", "没有 .py 文件")
    # .py 只在默认排除的目录里：按配置的排除规则看，也是没有
    failed(add_project(store, str(FIXTURES / "only_excluded"), None, None), "invalid", "没有 .py 文件")
    assert store.list_projects() == []


def test_add_counts_extra_exclude_when_looking_for_python_files(store, tmp_path):
    """extra_exclude 里的目录和默认排除的一样不算：.py 只在那里时说没有 .py 文件。"""
    root = tmp_path / "gen_only"
    (root / "generated").mkdir(parents=True)
    (root / "generated" / "x.py").write_text("x = 1\n")
    failed(add_project(store, str(root), None, {"extra_exclude": ["generated"]}), "invalid", "没有 .py 文件.*generated")
    assert add_project(store, str(root), None, {})["kind"] == "ok"


def test_add_refuses_home_directory_and_what_contains_it(store, tmp_path, monkeypatch):
    """主目录本身、它的任何上级，按 inode 比：经符号链接、固定链接（/System/Volumes/Data/…）、大小写不同的写法到达的
    也拒绝。主目录里面的代码目录照常可以添加。"""
    home = tmp_path / "fakehome" / "user"
    (home / "code").mkdir(parents=True)
    (home / "code" / "a.py").write_text("x = 1\n")
    (home / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("HOME", str(home))
    os.symlink(home, tmp_path / "home-link")
    spellings = ["~", "~/", str(home), f"{home}/../user", str(home.parent), str(tmp_path), str(tmp_path / "home-link")]
    data_volume = Path("/System/Volumes/Data" + str(home.resolve()))
    if data_volume.is_dir():  # macOS：数据卷上的同一个目录，路径解析不回 /private/var/…，只能按 inode 认出来
        spellings.append(str(data_volume))
    if case_insensitive(tmp_path):
        spellings.append(str(home.parent / "USER"))
    for spelling in spellings:
        failed(add_project(store, spelling, None, None), "invalid", "根目录、用户主目录或包含用户主目录的目录")
    assert store.list_projects() == []
    assert add_project(store, "~/code", None, None)["kind"] == "ok"


def test_add_refuses_bad_config(store, shop):
    failed(add_project(store, str(shop), None, {"nope": 1}), "config", "未知字段")
    failed(add_project(store, str(shop), None, {"exclude": "tests"}), "config", "exclude")
    failed(add_project(store, str(shop), None, {"source_roots": ["no/such/dir"]}), "config", "no/such/dir")
    assert store.list_projects() == []


def test_add_refuses_the_same_directory_in_any_spelling(store, shop, tmp_path, monkeypatch):
    first = add_project(store, str(shop), None, None)
    before = snapshot(store)
    monkeypatch.setenv("HOME", str(shop.parent))
    os.symlink(shop, tmp_path / "shop-link")
    for spelling in [str(shop), str(shop) + "/", "~/shop", f"{shop}/app/..", str(tmp_path / "shop-link")]:
        r = add_project(store, spelling, "另一个名字", None)
        failed(r, "duplicate", "已经添加过")
        assert r["project_id"] == first["project_id"]
    assert snapshot(store) == before


def test_add_refuses_overlapping_directories(store, shop):
    add_project(store, str(shop / "app"), None, None)
    before = snapshot(store)
    failed(add_project(store, str(shop), None, None), "overlap", "包含已添加的项目")
    (shop / "app" / "inner").mkdir()
    (shop / "app" / "inner" / "x.py").write_text("x = 1\n")
    failed(add_project(store, str(shop / "app" / "inner"), None, None), "overlap", "在已添加的项目.*里面")
    assert snapshot(store) == before


def test_overlap_check_ignores_projects_whose_directory_was_replaced(store, shop, tmp_path):
    """已添加的项目目录被移走、原地换成了符号链接（这里指向 /）：查重叠时不顺着换上的链接比——否则任何目录都"在它
    里面"。换成指向新目录的链接时，新目录也能正常添加（不算"已经添加过"）。"""
    add_project(store, str(shop), None, None)
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("def f():\n    return 1\n")
    os.rename(shop, tmp_path / "shop-moved")
    os.symlink("/", shop)
    r = add_project(store, str(other), None, None)
    assert r["kind"] == "ok", r
    os.unlink(shop)
    os.symlink(tmp_path / "shop-moved", shop)
    r = add_project(store, str(tmp_path / "shop-moved"), None, None)
    assert r["kind"] == "ok", r
    assert len(store.list_projects()) == 3
    # 换上的链接去掉、目录搬回原处：那个项目又算数了，再加同一目录或它里面的目录照样被拦住
    os.unlink(shop)
    os.rename(tmp_path / "shop-moved", shop)
    failed(add_project(store, str(shop), None, None), "duplicate", "已经添加过")
    failed(add_project(store, str(shop / "app"), None, None), "overlap", "在已添加的项目.*里面")


def test_engine_exception_leaves_nothing(store, shop):
    def broken(root, config):
        raise RuntimeError("引擎坏了")

    failed(add_project(store, str(shop), None, None, analyzer=broken), "analysis", "RuntimeError: 引擎坏了")
    assert store.list_projects() == []


def test_storage_failure_leaves_nothing(store, shop, monkeypatch):
    def disk_full(*args, **kwargs):
        raise StoreError("数据库读写失败：database or disk is full")

    monkeypatch.setattr(store, "add_project", disk_full)
    failed(add_project(store, str(shop), None, None), "storage", "disk is full")
    monkeypatch.undo()
    assert store.list_projects() == []


def test_concurrent_add_of_the_same_directory_keeps_one(store, shop):
    """两次添加同一目录同时通过了查重：后保存的那次被库的唯一约束挡下，按"已经添加过"失败。"""
    gate = threading.Event()
    arrived = threading.Barrier(2)

    def slow(root, config):
        arrived.wait()
        gate.wait(10)
        return analyze_with_notes(root, config)

    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(add_project(store, str(shop), None, None, analyzer=slow)))
               for _ in range(2)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert sorted(r["kind"] for r in results) == ["failed", "ok"]
    loser = next(r for r in results if r["kind"] == "failed")
    failed(loser, "duplicate", "已经添加过")
    assert loser["title"] == "同时添加了同一目录，这次没有添加"
    assert len(store.list_projects()) == 1


def case_insensitive(where: Path) -> bool:
    probe = where / "CaseProbe"
    probe.mkdir(exist_ok=True)
    return (where / "caseprobe").exists()


def test_case_variants_are_the_same_directory(store, shop):
    if not case_insensitive(shop.parent):
        pytest.skip("文件系统区分大小写")
    upper = shop.parent / "SHOP"
    first = add_project(store, str(shop), None, None)
    failed(add_project(store, str(upper), None, None), "duplicate", "已经添加过")
    failed(add_project(store, str(upper / "app"), None, None), "overlap", "里面")
    remove_project(store, first["project_id"])
    assert add_project(store, str(shop / "app"), None, None)["kind"] == "ok"
    failed(add_project(store, str(upper), None, None), "overlap", "包含已添加的项目")
    assert len(store.list_projects()) == 1


def add_concurrently(store, roots: list[str]) -> list[dict]:
    """几次添加一起通过查重（分析时互相等），再先后保存。"""
    arrived = threading.Barrier(len(roots))

    def slow(root, config):
        arrived.wait(10)
        return analyze_with_notes(root, config)

    results: list[dict] = []
    threads = [threading.Thread(target=lambda r=r: results.append(add_project(store, r, None, None, analyzer=slow)))
               for r in roots]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_concurrent_nested_adds_keep_one(store, shop):
    results = add_concurrently(store, [str(shop), str(shop / "app")])
    assert sorted(r["kind"] for r in results) == ["failed", "ok"]
    loser = next(r for r in results if r["kind"] == "failed")
    failed(loser, "overlap", "里面|包含")
    assert loser["title"] == "同时添加了重叠的目录，这次没有添加"
    assert len(store.list_projects()) == 1


def test_concurrent_case_variant_adds_keep_one(store, shop):
    if not case_insensitive(shop.parent):
        pytest.skip("文件系统区分大小写")
    results = add_concurrently(store, [str(shop), str(shop.parent / "SHOP")])
    assert sorted(r["kind"] for r in results) == ["failed", "ok"]
    loser = next(r for r in results if r["kind"] == "failed")
    failed(loser, "duplicate", "已经添加过")
    assert loser["title"] == "同时添加了同一目录，这次没有添加"
    assert len(store.list_projects()) == 1


# ---------- 重新分析 ----------

def test_reanalyze_adds_a_record_and_keeps_overrides(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    store.set_override(pid, "line:app/orders.py::place_order", "改过的标题", "改过的分组")
    store.set_override(pid, "line:gone.py::old", "孤儿", None)
    (shop / "app" / "extra.py").write_text("def helper():\n    return 1\n")
    first = store.latest_analysis(pid)
    r = reanalyze_project(store, pid)
    assert r["kind"] == "ok" and r["title"] == "分析完成", r
    assert r["analysis_id"] != first["id"] and r["summary"]["functions"] == first["summary"]["functions"] + 1
    assert [a["id"] for a in store.list_analyses(pid)] == [r["analysis_id"], first["id"]]
    assert store.get_overrides(pid) == {"line:app/orders.py::place_order": {"title": "改过的标题", "group": "改过的分组"},
                                        "line:gone.py::old": {"title": "孤儿"}}


def test_reanalyze_uses_the_saved_config(store, shop):
    (shop / "gen").mkdir()
    (shop / "gen" / "big.py").write_text("def a(): ...\ndef b(): ...\n")
    pid = add_project(store, str(shop), None, {"exclude": ["gen"]})["project_id"]
    n = store.latest_analysis(pid)["summary"]["functions"]
    assert reanalyze_project(store, pid)["summary"]["functions"] == n
    store.set_config(pid, {})
    assert reanalyze_project(store, pid)["summary"]["functions"] == n + 2


def test_only_the_latest_five_analyses_are_kept(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    ids = [store.latest_analysis(pid)["id"]] + [reanalyze_project(store, pid)["analysis_id"] for _ in range(5)]
    assert len(ids) == 6 and KEEP_ANALYSES == 5
    assert [a["id"] for a in store.list_analyses(pid)] == list(reversed(ids[1:]))


def test_reanalyze_failures_keep_the_previous_result(store, shop, monkeypatch):
    pid = add_project(store, str(shop), None, None)["project_id"]
    before = snapshot(store)

    def broken(root, config):
        raise ValueError("坏图")

    failed(reanalyze_project(store, pid, analyzer=broken), "analysis", "坏图")
    store.set_config(pid, {"source_roots": ["missing"]})
    failed(reanalyze_project(store, pid), "config", "missing")
    store.set_config(pid, {})
    before = snapshot(store)
    monkeypatch.setattr(store, "add_analysis", lambda *a, **k: (_ for _ in ()).throw(StoreError("磁盘满了")))
    failed(reanalyze_project(store, pid), "storage", "磁盘满了")
    monkeypatch.undo()
    assert snapshot(store) == before
    shutil.rmtree(shop)
    failed(reanalyze_project(store, pid), "invalid", "项目目录不存在了")
    assert snapshot(store) == before
    failed(reanalyze_project(store, "nope"), "not_found", "项目不存在")


def test_reanalyze_refuses_a_root_replaced_by_a_symlink(store, shop, tmp_path):
    """目录被移走、原地换成指向别处的符号链接：不顺着链接去分析，上一次的结果保持原样。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    outside = tmp_path / "outside"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "secret.py").write_text("def leaked():\n    return 1\n")
    before = snapshot(store)
    os.rename(shop, tmp_path / "shop-moved")
    os.symlink(outside, shop)
    failed(reanalyze_project(store, pid), "invalid", "不是添加时的那个目录.*请删除后重新添加")
    assert snapshot(store) == before
    assert not is_analyzing(store, pid)
    called: list = []
    failed(reanalyze_project(store, pid, analyzer=lambda root, config: called.append(root)), "invalid", "请删除后")
    assert called == []  # 引擎根本没被调用


def test_project_removed_while_it_is_being_analyzed(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]

    def remove_midway(root, config):
        assert remove_project(store, pid)["kind"] == "ok"
        return analyze_with_notes(root, config)

    failed(reanalyze_project(store, pid, analyzer=remove_midway), "not_found", "已经删除")
    assert store.list_projects() == [] and store.list_analyses(pid) == []


def test_same_project_is_analyzed_once_at_a_time(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    started, gate = threading.Event(), threading.Event()

    def slow(root, config):
        started.set()
        assert gate.wait(10)
        return analyze_with_notes(root, config)

    results: list[dict] = []
    t = threading.Thread(target=lambda: results.append(reanalyze_project(store, pid, analyzer=slow)))
    t.start()
    assert started.wait(10)
    assert is_analyzing(store, pid)
    second = reanalyze_project(store, pid)
    assert second == {"kind": "failed", "title": "正在分析，这次没有做", "reason": "正在分析", "code": "busy"}
    gate.set()
    t.join()
    assert results[0]["kind"] == "ok" and not is_analyzing(store, pid)
    assert len(store.list_analyses(pid)) == 2  # 第二次没有写任何东西
    assert reanalyze_project(store, pid)["kind"] == "ok"  # 做完以后可以再分析


def test_slot_is_released_after_a_failure(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]

    def broken(root, config):
        raise RuntimeError("x")

    assert reanalyze_project(store, pid, analyzer=broken)["kind"] == "failed"
    assert not is_analyzing(store, pid)
    assert reanalyze_project(store, pid)["kind"] == "ok"


class Abort(BaseException):
    """不是 Exception 的异常（像 KeyboardInterrupt、SystemExit）：运行分析接不住它，只能保证标记被取消。"""


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, Abort])
def test_mark_is_cleared_when_the_analysis_is_aborted(store, shop, exc):
    pid = add_project(store, str(shop), None, None)["project_id"]

    def aborted(root, config):
        assert is_analyzing(store, pid)
        raise exc()

    with pytest.raises(exc):
        reanalyze_project(store, pid, analyzer=aborted)
    assert not is_analyzing(store, pid)
    assert reanalyze_project(store, pid)["kind"] == "ok"


def test_unexpected_engine_errors_do_not_leave_the_mark(store, shop, monkeypatch):
    """运行分析里任何 Exception（包括整理摘要时、启动子进程时出的错）都变成结果，标记随后被取消。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    monkeypatch.setattr(worker, "summarize", lambda *a: 1 / 0)
    failed(reanalyze_project(store, pid, analyzer=analyze_with_notes), "analysis", "ZeroDivisionError")
    assert not is_analyzing(store, pid)

    def cannot_start(*args, **kwargs):
        raise MemoryError("起不来")  # 不是 OSError：run_in_subprocess 自己接不住的那种

    monkeypatch.setattr(flows, "run_in_subprocess", cannot_start)
    failed(reanalyze_project(store, pid), "analysis", "MemoryError: 起不来")
    assert not is_analyzing(store, pid)


def test_slots_are_per_store(tmp_path, shop):
    """两个数据目录里 id 碰巧相同的项目不互相挡（锁按数据库文件和项目 id）。"""
    a, b = Store(tmp_path / "a" / "db"), Store(tmp_path / "b" / "db")
    pid = add_project(a, str(shop), None, None)["project_id"]
    assert not is_analyzing(b, pid)


# ---------- 进程之间互斥、超时 ----------

HOLDER = """
import sys
from pathlib import Path
from pystepmap_server.locks import try_lock_project
fd = try_lock_project(Path(sys.argv[1]), sys.argv[2])
print("locked" if fd is not None else "busy", flush=True)
sys.stdin.read()
"""


def test_another_process_holding_the_project_lock_means_busy(store, shop):
    """另一个进程（MCP、另一个服务）正在分析这个项目：这边的重新分析得到"正在分析"，什么都不写；列表里 analyzing 为真。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    before = snapshot(store)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(store.path.parent), pid], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert is_analyzing(store, pid)
        called: list = []
        r = reanalyze_project(store, pid, analyzer=lambda root, config: called.append(root))
        assert r == {"kind": "failed", "title": "正在分析，这次没有做", "reason": "正在分析", "code": "busy"}
        assert called == [] and snapshot(store) == before
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    assert not is_analyzing(store, pid)
    assert reanalyze_project(store, pid)["kind"] == "ok"


def test_the_lock_is_held_until_the_new_analysis_is_saved_and_pruned(store, shop, monkeypatch):
    """别的进程看到的 analyzing 一直为真，直到新的分析已经是最新一次、旧的也清理完：界面在 analyzing 变假时重新取，
    拿到的一定是新结果。比较、保存、清理都在锁里。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    old = store.latest_analysis(pid)["id"]
    home = store.path.parent
    seen: list[tuple] = []

    def watch(root, config):
        seen.append(("analyze", project_locked(home, pid)))
        return analyze_with_notes(root, config)

    real_compare, real_add, real_prune = flows._compare, store.add_analysis, store.prune_analyses

    def compare(*args):
        seen.append(("compare", project_locked(home, pid), store.latest_analysis(pid)["id"]))
        return real_compare(*args)

    def add(project_id, analysis):
        seen.append(("save", project_locked(home, pid), store.latest_analysis(pid)["id"]))
        return real_add(project_id, analysis)

    def prune(project_id, keep):
        seen.append(("prune", project_locked(home, pid), store.latest_analysis(pid)["id"]))
        return real_prune(project_id, keep)

    monkeypatch.setattr(flows, "_compare", compare)
    monkeypatch.setattr(store, "add_analysis", add)
    monkeypatch.setattr(store, "prune_analyses", prune)
    r = reanalyze_project(store, pid, analyzer=watch)
    assert r["kind"] == "ok"
    assert seen == [("analyze", True), ("compare", True, old), ("save", True, old), ("prune", True, r["analysis_id"])]
    assert not project_locked(home, pid) and not is_analyzing(store, pid)


def ending_of(store, shop, monkeypatch, ending: str) -> tuple[str, dict]:
    """让重新分析走到给定的结局，返回 (项目 id, 结局)。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    if ending == "analysis":
        return pid, reanalyze_project(store, pid, analyzer=lambda root, config: 1 / 0)
    if ending == "not_found":
        def remove_midway(root, config):
            remove_project(store, pid)
            return analyze_with_notes(root, config)
        return pid, reanalyze_project(store, pid, analyzer=remove_midway)
    if ending == "storage":
        monkeypatch.setattr(store, "add_analysis", lambda *a: (_ for _ in ()).throw(StoreError("磁盘满了")))
        return pid, reanalyze_project(store, pid)
    if ending == "busy":
        mark = flows.mark_analyzing(store, pid)
        try:
            return pid, reanalyze_project(store, pid)
        finally:
            flows.unmark_analyzing(mark)
    if ending == "invalid":
        shutil.rmtree(shop)
        return pid, reanalyze_project(store, pid)
    return pid, reanalyze_project(store, pid)


@pytest.mark.parametrize("ending", ["ok", "analysis", "not_found", "storage", "busy", "invalid"])
def test_every_outcome_leaves_the_lock_free(store, shop, monkeypatch, ending):
    pid, r = ending_of(store, shop, monkeypatch, ending)
    assert r["kind"] == ("ok" if ending == "ok" else "failed") and r.get("code", "ok") == ending, r
    assert not project_locked(store.path.parent, pid) and not is_analyzing(store, pid)


@pytest.mark.parametrize("where", ["compare", "save", "prune"])
def test_lock_is_freed_when_saving_is_aborted(store, shop, monkeypatch, where):
    """比较、保存、清理时被 Exception 以外的异常（Ctrl+C 等）打断：标记和锁照样释放。"""
    pid = add_project(store, str(shop), None, None)["project_id"]

    def abort(*args, **kwargs):
        raise KeyboardInterrupt

    target = {"compare": (flows, "_compare"), "save": (store, "add_analysis"), "prune": (store, "prune_analyses")}
    monkeypatch.setattr(*target[where], abort)
    with pytest.raises(KeyboardInterrupt):
        reanalyze_project(store, pid)
    assert not project_locked(store.path.parent, pid) and not is_analyzing(store, pid)
    monkeypatch.undo()
    assert reanalyze_project(store, pid)["kind"] == "ok"


BUSY_ACK = {"kind": "failed", "title": "正在分析，这次没有标记", "reason": "正在分析，等分析完再标为看过", "code": "busy"}


def test_acknowledge_waits_for_a_running_analysis(store, shop, monkeypatch):
    """"标为看过"拿同一把锁：分析期间（包括比较和保存之间）点了，得到"正在分析"，不会被随后的保存盖掉或打乱。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    add_branch(shop)
    seen: list[dict] = []

    def ack_midway(root, config):
        seen.append(acknowledge_changes(store, pid))
        return analyze_with_notes(root, config)

    real_add = store.add_analysis

    def ack_before_save(project_id, analysis):
        seen.append(acknowledge_changes(store, pid))
        return real_add(project_id, analysis)

    monkeypatch.setattr(store, "add_analysis", ack_before_save)
    r = reanalyze_project(store, pid, analyzer=ack_midway)
    assert seen == [BUSY_ACK, BUSY_ACK]
    head = store.latest_analysis(pid)
    assert head["id"] == r["analysis_id"] and head["has_diff"] is True  # 变化仍标着
    assert not project_locked(store.path.parent, pid)
    monkeypatch.undo()
    assert acknowledge_changes(store, pid)["kind"] == "ok" and not project_locked(store.path.parent, pid)


def test_acknowledge_while_another_process_analyzes(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(store.path.parent), pid], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert acknowledge_changes(store, pid) == BUSY_ACK
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    assert acknowledge_changes(store, pid)["kind"] == "ok"


SLOW_RULES = {"extra_rules": [{"name": "slow", "entry_decorators": [["(a+)+$", "route"]]}]}


def test_analysis_that_takes_too_long_is_stopped(store, shop, monkeypatch):
    """配置里的正则灾难性回溯，分析卡住：超时后子进程被杀，结局是 timeout，项目和上一次的结果保持原样。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    (shop / "app" / "slow.py").write_text(f"@{'a' * 40}()\ndef handler():\n    return 1\n", encoding="utf-8")
    store.set_config(pid, SLOW_RULES)
    before = snapshot(store)
    monkeypatch.setenv("PYSTEPMAP_ANALYZE_TIMEOUT", "1")
    failed(reanalyze_project(store, pid), "timeout", "^分析超过 1 秒，已中止")
    assert snapshot(store) == before and not is_analyzing(store, pid)
    store.set_config(pid, {})
    assert reanalyze_project(store, pid)["kind"] == "ok"


def test_the_subprocess_runs_in_the_data_directory_with_the_configured_timeout(store, shop, monkeypatch):
    pid = add_project(store, str(shop), None, None)["project_id"]
    calls: list = []
    real = flows.run_in_subprocess

    def spy(root, config, *, timeout, cwd):
        calls.append((root, config, timeout, cwd))
        return real(root, config, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(flows, "run_in_subprocess", spy)
    monkeypatch.setenv("PYSTEPMAP_ANALYZE_TIMEOUT", "45")
    store.set_config(pid, {"exclude": ["tests"]})
    assert reanalyze_project(store, pid)["kind"] == "ok"
    assert calls == [(str(shop.resolve()), {"exclude": ["tests"]}, 45.0, store.path.parent)]
    assert store.analysis_extra(store.latest_analysis(pid)["id"])["config"] == {"exclude": ["tests"]}


# ---------- 对比基准和流程差异 ----------

BRANCH = """

@condition(title="数量太多？")
def too_many(order: dict) -> bool:
    return order["count"] > 100
"""


def add_branch(shop: Path) -> None:
    """下单流程里加一个判断：查完库存后，数量太多就不下单。"""
    path = shop / "app" / "orders.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace("\n\n@workflow", BRANCH + "\n\n@workflow")
    text = text.replace("    if lacks_stock(stock):",
                        "    if too_many(order):\n        return outcome(\"failed\", title=\"数量太多，没有下单\")\n"
                        "    if lacks_stock(stock):")
    path.write_text(text, encoding="utf-8")


def add_comment(shop: Path) -> None:
    """只改格式：文件开头加两行注释，所有行号都变了，流程没变。"""
    path = shop / "app" / "orders.py"
    path.write_text("# 只是注释\n# 又一行\n" + path.read_text(encoding="utf-8"), encoding="utf-8")


def latest(store, pid) -> dict:
    a = store.latest_analysis(pid)
    return {**a, **store.analysis_extra(a["id"])}


def added_cond(diff: dict) -> dict:
    [fc] = [f for f in diff["functions"].values() if f["title"] == "下单"]
    [node] = [n for n in fc["nodes"] if n["status"] == "added" and n["type"] == "cond"]
    assert node["new_title"] == "数量太多？"
    return node


def test_base_and_diff_follow_the_rules(store, shop):
    added = add_project(store, str(shop), None, None)
    a1, pid = added["analysis_id"], added["project_id"]
    # 没改：沿用上一次的（第一次没有基准）
    a2 = reanalyze_project(store, pid)["analysis_id"]
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (None, None)
    assert latest(store, pid)["graph_sha"] == store.analysis_extra(a1)["graph_sha"]
    # 加一个分支：基准是上一次，差异里有那个新增的判断
    add_branch(shop)
    a3 = reanalyze_project(store, pid)["analysis_id"]
    head = latest(store, pid)
    assert head["id"] == a3 and head["base_id"] == a2 and head["has_diff"] is True
    node = added_cond(head["diff"])
    lines_text = (shop / "app" / "orders.py").read_text(encoding="utf-8").splitlines()
    assert "too_many(order)" in lines_text[node["new_line"] - 1]
    assert [ln["title"] for ln in head["diff"]["lines"]] == ["下单"]
    # 没改再分析：沿用上一次的基准和差异
    a4 = reanalyze_project(store, pid)["analysis_id"]
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (a2, head["diff"])
    # 只加注释：基准仍是加分支之前那次，差异仍报那个分支，行号是新的
    add_comment(shop)
    a5 = reanalyze_project(store, pid)["analysis_id"]
    after = latest(store, pid)
    assert after["id"] == a5 and after["base_id"] == a2 and after["has_diff"] is True
    moved = added_cond(after["diff"])
    assert moved["new_line"] == node["new_line"] + 2
    assert "too_many(order)" in (shop / "app" / "orders.py").read_text(encoding="utf-8").splitlines()[moved["new_line"] - 1]
    assert a4 < a5 and {a["id"] for a in store.list_analyses(pid)} >= {a2, a5}


GUARD = '    if lacks_stock(stock):\n        return outcome("failed", title="库存不够，没有下单")\n'


def remove_guard(shop: Path) -> None:
    """删掉"库存不够？"这个守卫（判断和它的结局）。"""
    path = shop / "app" / "orders.py"
    text = path.read_text(encoding="utf-8")
    assert GUARD in text
    path.write_text(text.replace(GUARD, ""), encoding="utf-8")


def restore_guard(shop: Path) -> None:
    path = shop / "app" / "orders.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("    order_no = create_order(order)\n", GUARD + "    order_no = create_order(order)\n"),
                    encoding="utf-8")


def marks(diff: dict) -> set[tuple[str, str, str]]:
    """差异里每个节点的（状态，类型，标题）。"""
    return {(n["status"], n["type"], n["new_title"] or n["old_title"])
            for f in diff["functions"].values() for n in f["nodes"]}


def test_unseen_changes_add_up_until_acknowledged(store, shop):
    """R1b：对比基准只在"标为已看"时移动。两次改流程都没标为已看：两处变化都还标着（相对第一次分析）；再只改格式：
    内容相同、行号是新的；标为已看之后：下一次变化单独算，从看过的那次起比。"""
    added = add_project(store, str(shop), None, None)
    first, pid = added["analysis_id"], added["project_id"]
    add_branch(shop)
    reanalyze_project(store, pid)
    remove_guard(shop)
    reanalyze_project(store, pid)
    head = latest(store, pid)
    assert head["base_id"] == first and head["has_diff"] is True
    both = marks(head["diff"])
    assert {("added", "cond", "数量太多？"), ("removed", "cond", "库存不够？")} <= both
    add_comment(shop)
    seen = reanalyze_project(store, pid)["analysis_id"]
    after = latest(store, pid)
    assert after["base_id"] == first and marks(after["diff"]) == both
    assert added_cond(after["diff"])["new_line"] == added_cond(head["diff"])["new_line"] + 2
    assert acknowledge_changes(store, pid, seen)["kind"] == "ok"
    restore_guard(shop)
    reanalyze_project(store, pid)
    alone = latest(store, pid)
    assert alone["base_id"] == seen and alone["has_diff"] is True
    assert ("added", "cond", "库存不够？") in marks(alone["diff"])
    assert not any("数量太多" in title for _, _, title in marks(alone["diff"]))


def test_baseline_is_the_first_analysis_or_the_last_acknowledged_state(store, shop):
    """/diff 说对比基准是哪种：没标为已看过是"第一次分析"（时间是项目添加时）；标为已看过是"上次标为已看时"——标为
    已看之后没改流程的重新分析（基准仍为空）一路带着这个记号，下一次改流程以它为基准。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    created = store.get_project(pid)["created_at"]
    reanalyze_project(store, pid)  # 没改：没有基准
    add_branch(shop)
    reanalyze_project(store, pid)
    assert store.latest_diff(pid)["baseline"] == {"kind": "first", "at": created}
    seen = store.latest_analysis(pid)["id"]
    assert acknowledge_changes(store, pid, seen)["kind"] == "ok"
    assert store.latest_diff(pid)["baseline"] is None
    reanalyze_project(store, pid)  # 没改
    add_comment(shop)
    quiet = reanalyze_project(store, pid)["analysis_id"]  # 只改格式
    assert (latest(store, pid)["base_id"], latest(store, pid)["has_diff"]) == (None, False)
    remove_guard(shop)
    reanalyze_project(store, pid)
    state = store.latest_diff(pid)
    assert state["base"]["id"] == quiet  # 和上一次（看过的状态）比
    assert state["baseline"] == {"kind": "acknowledged", "at": state["base"]["created_at"]}
    assert marks(state["diff"]) >= {("removed", "cond", "库存不够？")} and not any(
        "数量太多" in t for _, _, t in marks(state["diff"]))


def test_base_removed_by_an_old_server_falls_back_to_the_previous_analysis(store, shop):
    """对比基准被旧版本的服务清理掉了：当作上一次没有基准，和上一次比。"""
    added = add_project(store, str(shop), None, None)
    first, pid = added["analysis_id"], added["project_id"]
    add_branch(shop)
    second = reanalyze_project(store, pid)["analysis_id"]
    con = sqlite3.connect(store.path)
    con.execute("delete from analyses where id = ?", (first,))
    con.commit()
    con.close()
    remove_guard(shop)
    reanalyze_project(store, pid)
    head = latest(store, pid)
    assert head["base_id"] == second
    assert ("removed", "cond", "库存不够？") in marks(head["diff"]) and not any(
        "数量太多" in t for _, _, t in marks(head["diff"]))


def test_formatting_only_change_without_a_base_has_no_diff(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    add_comment(shop)
    reanalyze_project(store, pid)
    head = latest(store, pid)
    assert (head["base_id"], head["diff"], head["has_diff"]) == (None, None, False)


def test_acknowledge_changes(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    add_branch(shop)
    reanalyze_project(store, pid)
    assert latest(store, pid)["has_diff"] is True
    before = store.list_analyses(pid)
    r = acknowledge_changes(store, pid)
    assert r == {"kind": "ok", "title": "流程变化已标为看过", "project_id": pid}
    head = latest(store, pid)
    assert (head["base_id"], head["diff"], head["has_diff"]) == (None, None, False)
    assert len(store.list_analyses(pid)) == len(before)  # 不删分析、不新增分析
    reanalyze_project(store, pid)  # 没改：仍然没有基准
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (None, None)
    add_comment(shop)  # 只改格式：也没有
    reanalyze_project(store, pid)
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (None, None)
    failed(acknowledge_changes(store, "nope"), "not_found", "项目不存在")


def test_acknowledge_only_what_was_seen(store, shop):
    """界面看的是第 N 次的差异，点"标为看过"时最新一次已经是 N+1（别的进程刚分析完）：不清，结局 moved。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    add_branch(shop)
    seen = reanalyze_project(store, pid)["analysis_id"]
    add_comment(shop)
    newer = reanalyze_project(store, pid)["analysis_id"]
    r = acknowledge_changes(store, pid, seen)
    assert r == {"kind": "failed", "title": "分析结果已经更新，这次没有标记", "reason": "分析结果已经更新，请先看新的变化",
                 "code": "moved"}
    assert latest(store, pid)["has_diff"] is True and not project_locked(store.path.parent, pid)
    assert acknowledge_changes(store, pid, newer)["kind"] == "ok"
    assert latest(store, pid)["has_diff"] is False


def test_acknowledge_storage_failure(store, shop, monkeypatch):
    pid = add_project(store, str(shop), None, None)["project_id"]
    monkeypatch.setattr(store, "clear_base", lambda *a, **k: (_ for _ in ()).throw(StoreError("磁盘满了")))
    failed(acknowledge_changes(store, pid), "storage", "磁盘满了")
    assert not project_locked(store.path.parent, pid) and not is_analyzing(store, pid)


def test_comparison_errors_do_not_fail_the_analysis(store, shop, monkeypatch):
    """读不出旧图、算差异出错：记日志，按没有基准保存，这次分析照常成功。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    add_branch(shop)

    def broken(old, new):
        raise RuntimeError("差异算坏了")

    monkeypatch.setattr(flows, "diff_graphs", broken)
    r = reanalyze_project(store, pid)
    assert r["kind"] == "ok"
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (None, None)
    monkeypatch.undo()
    add_comment(shop)
    monkeypatch.setattr(store, "graph_json", lambda aid: "{不是图")
    assert reanalyze_project(store, pid)["kind"] == "ok"
    assert (latest(store, pid)["base_id"], latest(store, pid)["diff"]) == (None, None)


def test_prune_keeps_the_base(store, shop):
    """基准之后又分析了很多次（都没改）：最近 5 次之外，基准那次也留着。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    first = store.latest_analysis(pid)["id"]
    add_branch(shop)
    for _ in range(KEEP_ANALYSES + 2):
        reanalyze_project(store, pid)
    ids = [a["id"] for a in store.list_analyses(pid)]
    assert len(ids) == KEEP_ANALYSES + 1 and ids[-1] == first
    assert latest(store, pid)["base_id"] == first and latest(store, pid)["has_diff"] is True


# ---------- 删除项目 ----------

def test_remove_project(store, shop):
    pid = add_project(store, str(shop), None, None)["project_id"]
    assert reanalyze_project(store, pid)["kind"] == "ok"  # 分析过：有了锁文件
    assert (store.path.parent / "locks" / f"{pid}.lock").exists()
    store.set_override(pid, "line:x", "t", None)
    r = remove_project(store, pid)
    assert r == {"kind": "ok", "title": "项目已删除", "project_id": pid}
    assert store.list_projects() == [] and store.list_analyses(pid) == [] and store.get_overrides(pid) == {}
    assert (shop / "app" / "orders.py").exists()  # 只删平台里的记录，不动代码
    assert not (store.path.parent / "locks" / f"{pid}.lock").exists()  # 锁文件也删了（分析时建的）
    failed(remove_project(store, pid), "not_found", "项目不存在")
    assert add_project(store, str(shop), None, None)["kind"] == "ok"  # 删了以后可以重新添加


# ---------- 运行测试并核对 ----------
# 测试命令跑的是 tmp_path 里转账示例的副本（含它的测试），用当前的解释器：sys.executable -m pytest tests -q

PYTEST = f"{shlex.quote(sys.executable)} -m pytest tests -q"
EXAMPLE_CASES = 9  # 转账示例的用例数（6 个测试，其中一个参数化成 4 个用例）


@pytest.fixture
def transfer(tmp_path) -> Path:
    dst = tmp_path / "work" / "transfer"
    shutil.copytree(TRANSFER, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst


def with_command(store, root: Path, command: str = PYTEST) -> str:
    """添加项目，填好测试命令并确认过，返回项目 id。"""
    pid = add_project(store, str(root), None, None)["project_id"]
    store.set_test_command(pid, command)
    assert store.confirm_test_command(pid, command)
    return pid


def run_now(store, pid: str, **kwargs) -> dict:
    return run_project_tests(store, pid, cancel=kwargs.pop("cancel", None) or threading.Event(), **kwargs)


def all_clear(store, pid: str) -> None:
    """锁放掉了、标记取消了、这次运行的临时目录删掉了。"""
    home = store.path.parent
    assert not project_locked(home, pid) and not is_analyzing(store, pid)
    assert list((home / "runs").glob("tmp-*")) == [] if (home / "runs").exists() else True


def node_sid(store, pid: str, qualname: str, title: str) -> tuple[str, str]:
    """(函数 sid, 节点 sid)：最新一次分析的图里 qualname 函数中标题为 title 的节点。"""
    graph = from_json(store.graph_json(store.latest_analysis(pid)["id"]))
    fn = next(f for f in graph.functions.values() if f.qualname == qualname)
    return fn.sid, next(n.sid for n in fn.nodes if n.title == title)


def paths_of(store, pid: str, run_id: int) -> dict:
    return json.loads(gzip.decompress(store.run_paths_gz(pid, run_id)))


def test_run_the_tests_of_the_transfer_example(store, transfer):
    pid = with_command(store, transfer)
    aid = store.latest_analysis(pid)["id"]
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "ok" and r["title"] == "测试已运行并核对", r
    run = store.get_run(pid, r["run_id"])
    s = run["summary"]
    assert (run["status"], run["exit_code"], run["analysis_id"], run["command"]) == ("ok", 0, aid, PYTEST)
    assert (s["tests"], s["passed"], s["failed"], s["skipped"], s["errors"]) == (EXAMPLE_CASES, EXAMPLE_CASES, 0, 0, 0)
    v = s["verify"]
    assert v["ok"] is True and v["mismatches"] == {"edge": 0, "end": 0, "call": 0}
    assert v["stale"] == [] and v["plugin_errors"] == 0 and v["truncated_records"] == 0
    assert v["functions_seen"] > 10 and v["distinct_paths"] > 5 and v["steps_total"] > 50
    assert s["mismatch_list"] == [] and s["note"] == "" and s["seconds"] > 0
    c = s["coverage"]
    assert c["tests"] == 6 and c["failed"] == 0 and c["outcomes"] - c["outcomes_covered"] == 1  # 超过单笔限额
    named = [t for t in run["tests"] if t["test"]]
    assert len(named) == 6 and sum(t["cases"] for t in named) == EXAMPLE_CASES
    assert all(t["outcome"] == "passed" and t["functions"] > 0 for t in named)
    # 成功的那个测试经过"执行转账"
    fsid, step = node_sid(store, pid, "TransferWorkflow.run", "执行转账")
    paths = paths_of(store, pid, r["run_id"])
    ok = next(t for t in paths["tests"] if t["test"] == "tests/test_transfer.py::test_transfer_succeeds")
    assert step in ok["functions"][fsid]["nodes"]
    assert run["paths_size"] == len(gzip.decompress(store.run_paths_gz(pid, r["run_id"])))
    # 函数索引：运行里的函数 sid 每个一次，每个测试记下标——和路径里每个测试的函数一致
    index = store.run_function_index(pid, r["run_id"])
    assert len(index["functions"]) == len(set(index["functions"])) and index["functions"] == sorted(index["functions"])
    assert {name: {index["functions"][i] for i in idx} for name, idx in index["tests"].items()} == \
        {t["test"]: set(t["functions"]) for t in paths["tests"]}
    assert any(f"{EXAMPLE_CASES} passed" in line for line in runner.last_output(store.path.parent, pid))
    assert len(store.list_analyses(pid)) == 1  # 图没过期：没有多分析一次
    all_clear(store, pid)
    assert not any(transfer.rglob("__pycache__")) and not (transfer / ".pytest_cache").exists()  # 没在项目里写东西


def test_processing_a_run_ignores_the_progress_lines(store, transfer, monkeypatch, tmp_path):
    """记录文件里的进度行（{"collected"}、{"start"}，运行中看进度用）不影响运行的处理：去掉它们再核对、汇总，存下的
    汇总、每个测试、运行路径、函数索引一个字节都不差。"""
    kept = tmp_path / "kept"
    real = runner.run_tests

    def keep_files(*args, **kwargs):
        result = real(*args, **kwargs)
        kept.mkdir()
        shutil.copy(kwargs["trace_path"], kept / "trace.jsonl")
        shutil.copy(kwargs["graph_path"], kept / "graph.json")
        return result

    monkeypatch.setattr(runner, "run_tests", keep_files)
    pid = with_command(store, transfer)
    assert run_now(store, pid)["status"] == "ok"
    text = (kept / "trace.jsonl").read_text(encoding="utf-8")
    progress = [line for line in text.splitlines(keepends=True) if set(json.loads(line)) in ({"collected"}, {"start"})]
    assert len(progress) == 1 + EXAMPLE_CASES
    (kept / "plain.jsonl").write_text("".join(line for line in text.splitlines(keepends=True) if line not in progress),
                                      encoding="utf-8")

    def processed(trace: str) -> str:
        prepared = {"graph_path": kept / "graph.json", "trace_path": kept / trace}
        checked = testflows._verify_records(prepared)
        ran = {"seconds": 1.0, "exit_code": 0, "output": []}
        return json.dumps(testflows._summarize(prepared, ran, checked), ensure_ascii=False, sort_keys=True)

    assert processed("trace.jsonl") == processed("plain.jsonl")


def test_a_failing_test_is_tests_failed_and_the_paths_are_kept(store, transfer):
    test_file = transfer / "tests" / "test_transfer.py"
    test_file.write_text(test_file.read_text(encoding="utf-8").replace('"TR-R1")', '"TR-XX")'), encoding="utf-8")
    pid = with_command(store, transfer)
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "tests_failed", r
    run = store.get_run(pid, r["run_id"])
    assert run["status"] == "tests_failed" and run["exit_code"] == 1
    assert (run["summary"]["passed"], run["summary"]["failed"]) == (EXAMPLE_CASES - 1, 1)
    assert run["summary"]["verify"]["ok"] is True and run["summary"]["note"] == ""
    failing = next(t for t in run["tests"] if t["test"].endswith("::test_transfer_succeeds"))
    assert failing["outcome"] == "failed"
    assert store.run_paths_gz(pid, r["run_id"]) is not None
    all_clear(store, pid)


def test_a_command_that_runs_no_tests_is_an_error(store, transfer):
    pid = with_command(store, transfer, "true")
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "error", r
    run = store.get_run(pid, r["run_id"])
    assert (run["status"], run["exit_code"], run["tests"], run["paths_size"]) == ("error", 0, [], None)
    note = run["summary"]["note"]
    assert "没有收到任何记录" in note and "pytest" in note and "3.12" in note
    assert run["summary"]["verify"]["ok"] is None  # 什么都没核对：不是"核对通过"
    assert store.run_paths_gz(pid, r["run_id"]) is None
    all_clear(store, pid)


@pytest.mark.parametrize(("command", "code", "opening"), [
    ("no-such-command-pystepmap-xyz", 127, "命令找不到（退出码 127）"),
    ("exit 126", 126, "命令不能执行（退出码 126）"),
    (f"{PYTEST} -k nothing_matches_this", 5, "pytest 一个测试都没收集到（退出码 5）"),
    (f"{shlex.quote(sys.executable)} -m pytest tests/no_such_dir -q", 4, "pytest 的参数或路径不对（退出码 4）"),
    ("exit 3", 3, "pytest 内部错误（退出码 3；常见原因：被测项目的 Python 低于 3.12，记录插件装不上）"),
    ("exit 2", 2, "测试被中断了，或者收集测试时出错（退出码 2；常见原因：测试文件导入失败）"),
    ("exit 9", 9, "命令以退出码 9 结束"),
    ("true", 0, "命令正常结束（退出码 0）"),
])
def test_a_run_without_records_opens_with_what_the_exit_code_means(store, transfer, command, code, opening):
    pid = with_command(store, transfer, command)
    run = store.get_run(pid, run_now(store, pid)["run_id"])
    note = run["summary"]["note"]
    assert (run["status"], run["exit_code"]) == ("error", code)
    assert note.startswith(opening + "，没有收到任何记录"), note
    assert "其他可能" in note and note.endswith("原文见运行输出。")
    assert run["summary"]["verify"]["ok"] is None


def test_a_command_that_overrides_pythonpath_is_told_it_hides_the_plugin(store, transfer):
    """命令里设了 PYTHONPATH（覆盖了平台放进去的插件目录）、一条记录都没收到：说明里说是它把记录插件挡住了，怎么写；
    接在原来的后面（$PYTHONPATH）时、或者没设时不说。"""
    pid = with_command(store, transfer, f"PYTHONPATH=. {PYTEST}")
    run = store.get_run(pid, run_now(store, pid)["run_id"])
    assert run["status"] == "error" and run["summary"]["verify"]["ok"] is None
    assert "命令里设了 PYTHONPATH，把记录插件挡住了（写成 PYTHONPATH=src:$PYTHONPATH）" in run["summary"]["note"]
    assert testflows.pythonpath_hint("PYTHONPATH=src:$PYTHONPATH pytest") == ""
    assert testflows.pythonpath_hint('export PYTHONPATH="src:${PYTHONPATH}"; pytest') == ""
    assert testflows.pythonpath_hint("pytest tests") == ""
    assert testflows.pythonpath_hint("env PYTHONPATH=src pytest") != ""


def test_records_with_a_non_zero_exit_and_no_failures_say_the_exit_code(store, transfer):
    pid = with_command(store, transfer, f"{PYTEST}; exit 7")
    run = store.get_run(pid, run_now(store, pid)["run_id"])
    assert run["status"] == "error" and run["exit_code"] == 7 and run["summary"]["verify"]["ok"] is True
    assert run["summary"]["note"].startswith("命令以退出码 7 结束，但没有失败的测试")


def test_a_graph_that_is_out_of_date_is_analysed_first(store, transfer):
    pid = with_command(store, transfer)
    old = store.latest_analysis(pid)["id"]
    p = transfer / "transfer_workflow.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# 运行前改的\n", encoding="utf-8")
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "ok", r
    new = store.latest_analysis(pid)["id"]
    assert new != old and [a["id"] for a in store.list_analyses(pid)] == [new, old]
    run = store.get_run(pid, r["run_id"])
    assert run["analysis_id"] == new and run["summary"]["verify"]["stale"] == []
    all_clear(store, pid)


def test_code_changed_while_the_tests_run_is_reported(store, transfer):
    command = f"sleep 0.2; printf '\\n# 运行期间改的\\n' >> transfer_workflow.py; {PYTEST}"
    pid = with_command(store, transfer, command)
    r = run_now(store, pid)
    assert r["kind"] == "ok", r
    run = store.get_run(pid, r["run_id"])
    assert run["summary"]["verify"]["stale"] == ["transfer_workflow.py"]
    assert "运行期间代码有改动" in run["summary"]["note"] and "transfer_workflow.py" in run["summary"]["note"]
    assert len(store.list_analyses(pid)) == 1  # 运行之前图没过期
    all_clear(store, pid)


def group_gone(pgid: int, within: float = 2.0) -> bool:
    deadline = time.monotonic() + within
    while True:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)


@pytest.fixture
def quick_interrupt(monkeypatch):
    """命令里 sh 的后台进程（&）不理 SIGINT：不等满 5 秒就接着 SIGTERM，测试快一点。"""
    monkeypatch.setattr(runner, "INT_GRACE", 0.3)


def test_a_run_that_takes_too_long_is_stopped_and_recorded(store, transfer, tmp_path, quick_interrupt):
    pidfile = tmp_path / "pgid"
    pid = with_command(store, transfer, f"echo $$ > {pidfile}; sleep 30 & sleep 30")
    started = time.monotonic()
    r = run_now(store, pid, timeout=1)
    assert time.monotonic() - started < 5
    assert r["kind"] == "failed" and r["code"] == "timeout" and r["run_id"], r
    run = store.get_run(pid, r["run_id"])
    assert (run["status"], run["exit_code"], run["paths_size"], run["tests"]) == ("timeout", None, None, [])
    assert "超过 1 秒" in run["summary"]["note"] and run["summary"]["verify"] is None
    assert store.run_function_index(pid, r["run_id"]) is None
    assert group_gone(int(pidfile.read_text()))
    all_clear(store, pid)


def test_the_timeout_comes_from_the_environment(store, transfer, monkeypatch):
    pid = with_command(store, transfer, "sleep 30")
    monkeypatch.setenv("PYSTEPMAP_TEST_TIMEOUT", "0.5")
    r = run_now(store, pid)
    assert r["code"] == "timeout" and "超过 0.5 秒" in store.get_run(pid, r["run_id"])["summary"]["note"]


def test_a_cancelled_run_is_stopped_and_recorded(store, transfer, tmp_path, quick_interrupt):
    pidfile = tmp_path / "pgid"
    pid = with_command(store, transfer, f"echo $$ > {pidfile}; sleep 30 & sleep 30")
    cancel = threading.Event()

    def cancel_once_started():
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    t = threading.Thread(target=cancel_once_started)
    t.start()
    r = run_now(store, pid, cancel=cancel)
    t.join()
    assert r["kind"] == "failed" and r["code"] == "cancelled" and r["run_id"], r
    run = store.get_run(pid, r["run_id"])
    assert (run["status"], run["exit_code"], run["paths_size"]) == ("cancelled", None, None)
    assert run["summary"]["note"] == "运行被取消了"
    assert group_gone(int(pidfile.read_text()))
    all_clear(store, pid)


def test_a_run_is_refused_while_the_project_is_being_analysed(store, transfer):
    pid = with_command(store, transfer)
    mark = flows.mark_analyzing(store, pid)
    try:
        r = run_now(store, pid)
    finally:
        flows.unmark_analyzing(mark)
    assert r["kind"] == "failed" and r["code"] == "busy", r
    assert store.list_runs(pid) == []
    all_clear(store, pid)


def test_while_the_tests_run_analysis_acknowledge_and_a_second_run_are_busy(store, transfer, tmp_path):
    flag = tmp_path / "go"
    pid = with_command(store, transfer, f"while [ ! -f {flag} ]; do sleep 0.05; done; {PYTEST}")
    results: list[dict] = []
    t = threading.Thread(target=lambda: results.append(run_now(store, pid)))
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not project_locked(store.path.parent, pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert project_locked(store.path.parent, pid) and is_analyzing(store, pid)
        assert reanalyze_project(store, pid)["code"] == "busy"
        assert acknowledge_changes(store, pid)["code"] == "busy"
        assert run_now(store, pid)["code"] == "busy"
    finally:
        flag.write_text("1")
        t.join(60)
    assert results[0]["kind"] == "ok" and results[0]["status"] == "ok", results
    assert len(store.list_runs(pid)) == 1 and len(store.list_analyses(pid)) == 1
    all_clear(store, pid)


def fake_run_tests(status: str, **extra):
    def fake(root, command, **kwargs):
        for f in extra.get("before", ()):
            f()
        return runner.RunResult(status, None, 0.1, [], extra.get("reason", "命令没能启动：No such file or directory"))
    return fake


def run_ending(store, transfer, monkeypatch, ending: str) -> tuple[str, dict]:
    """让运行测试的流程走到给定的结局，返回 (项目 id, 结局)。"""
    pid = with_command(store, transfer)
    if ending == "not_found":
        return pid, run_now(store, "nope1234")
    if ending == "no_command":
        store.set_test_command(pid, None)
    elif ending == "confirm":
        store.set_test_command(pid, "pytest -x")
    elif ending == "invalid":
        shutil.rmtree(transfer)
    elif ending == "busy":
        mark = flows.mark_analyzing(store, pid)
        try:
            return pid, run_now(store, pid)
        finally:
            flows.unmark_analyzing(mark)
    elif ending == "analysis":
        (transfer / "transfer_workflow.py").write_text("x = 1\n", encoding="utf-8")
        return pid, run_now(store, pid, analyzer=lambda root, config: 1 / 0)
    elif ending == "prepare":
        monkeypatch.setattr(runner, "make_scratch", lambda home: (_ for _ in ()).throw(OSError(28, "磁盘满了")))
    elif ending == "failed_to_start":
        monkeypatch.setattr(runner, "run_tests", fake_run_tests("failed_to_start"))
    elif ending == "storage":
        monkeypatch.setattr(store, "add_run", lambda *a: (_ for _ in ()).throw(StoreError("磁盘满了")))
    elif ending == "deleted":
        real = runner.run_tests

        def delete_midway(*args, **kwargs):
            result = real(*args, **kwargs)
            remove_project(store, pid)
            return result
        monkeypatch.setattr(runner, "run_tests", delete_midway)
    return pid, run_now(store, pid)


ENDINGS = {  # 结局 → (kind, code)
    "ok": ("ok", None), "not_found": ("failed", "not_found"), "no_command": ("failed", "no_command"),
    "confirm": ("failed", "confirm"), "invalid": ("failed", "invalid"), "busy": ("failed", "busy"),
    "analysis": ("failed", "analysis"), "prepare": ("failed", "prepare"),
    "failed_to_start": ("failed", "failed_to_start"), "storage": ("failed", "storage"),
    "deleted": ("failed", "not_found"),
}


@pytest.mark.parametrize("ending", list(ENDINGS))
def test_every_ending_leaves_the_lock_free_and_no_scratch(store, transfer, monkeypatch, ending):
    pid, r = run_ending(store, transfer, monkeypatch, ending)
    kind, code = ENDINGS[ending]
    assert r["kind"] == kind and r.get("code") == code, r
    assert isinstance(r["title"], str) and CJK.search(r["title"])
    if kind == "failed":
        assert CJK.search(r["reason"]), r
    all_clear(store, pid)
    saved = store.list_runs(pid)
    if ending == "failed_to_start":  # 没能启动也记一次运行：状态 error，说明原因
        assert [x["status"] for x in saved] == ["error"] and r["run_id"] == saved[0]["id"]
        assert saved[0]["summary"]["note"].startswith("命令没能启动")
    elif ending != "ok":
        assert saved == []


@pytest.mark.parametrize("where", ["run", "verify", "summarize", "save"])
def test_lock_and_scratch_are_freed_when_the_run_is_aborted(store, transfer, monkeypatch, where):
    """跑测试、核对、汇总、保存时被 Exception 以外的异常（Ctrl+C 等）打断：锁和临时目录照样放掉、删掉。"""
    pid = with_command(store, transfer)

    def abort(*args, **kwargs):
        raise KeyboardInterrupt

    target = {"run": (runner, "run_tests"), "verify": (testflows, "verify"), "summarize": (testflows, "build_run_paths"),
              "save": (store, "add_run")}
    monkeypatch.setattr(*target[where], abort)
    with pytest.raises(KeyboardInterrupt):
        run_now(store, pid)
    all_clear(store, pid)
    monkeypatch.undo()
    assert run_now(store, pid)["kind"] == "ok"


def test_errors_while_verifying_become_an_error_run(store, transfer, monkeypatch):
    """核对、汇总出了意外的错（引擎的缺陷）：这次运行记成 error，说明里只有异常的类型名，不让流程带着锁出去。"""
    pid = with_command(store, transfer)
    monkeypatch.setattr(testflows, "verify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("某个取值 12345")))
    r = run_now(store, pid)
    run = store.get_run(pid, r["run_id"])
    assert run["status"] == "error" and "RuntimeError" in run["summary"]["note"]
    assert "12345" not in json.dumps(run, ensure_ascii=False)
    all_clear(store, pid)


def without_command(run: dict) -> str:
    """运行存下的一切，除了命令原文（这几个测试把特别的字符串写在命令里）。"""
    return json.dumps({k: v for k, v in run.items() if k != "command"}, ensure_ascii=False)


def test_a_broken_record_file_is_an_error_run(store, transfer):
    """记录文件里有一行不是合法的 JSON：这次运行记成 error（说明里不带那一行的原文，原文在内存里的输出末尾）。"""
    pid = with_command(store, transfer, f'{PYTEST}; echo "{{坏了 secret-777" >> "$PYSTEPMAP_TRACE_OUT"')
    r = run_now(store, pid)
    run = store.get_run(pid, r["run_id"])
    assert run["status"] == "error" and "记录文件读不了" in run["summary"]["note"]
    assert "secret-777" not in without_command(run)
    assert any("记录文件" in line for line in runner.last_output(store.path.parent, pid))
    all_clear(store, pid)


def test_plugin_errors_keep_only_their_type_name(store, transfer):
    """插件的 error 行带插件自己的异常信息：库里只留类型名和个数，原文只在内存里的输出末尾。"""
    command = f'{PYTEST}; echo \'{{"error": "ValueError: secret-888 不合法"}}\' >> "$PYSTEPMAP_TRACE_OUT"'
    pid = with_command(store, transfer, command)
    r = run_now(store, pid)
    run = store.get_run(pid, r["run_id"])
    v = run["summary"]["verify"]
    assert v["plugin_errors"] == 1 and v["plugin_error_types"] == {"ValueError": 1}
    assert "secret-888" not in without_command(run)
    assert "ValueError" in run["summary"]["note"]
    assert any("secret-888" in line for line in runner.last_output(store.path.parent, pid))


def test_only_the_latest_runs_are_kept(store, transfer):
    pid = with_command(store, transfer, "true")
    ids = [run_now(store, pid)["run_id"] for _ in range(KEEP_RUNS + 2)]
    assert [x["id"] for x in store.list_runs(pid)] == list(reversed(ids[2:]))


SECRET = "zqv7SECRETvalue4d1e"  # 很特别的字符串：测试的参数、参数化的 id、断言内容、异常信息里都有它

SECRET_TESTS = f'''
import asyncio
from decimal import Decimal

import pytest
from transfer_workflow import FakeBank, TransferRequest, TransferWorkflow

SECRET = "{SECRET}"


def run(bank, req):
    return asyncio.run(TransferWorkflow(bank).run(req))


@pytest.mark.parametrize("memo", [SECRET], ids=[SECRET])
def test_secret_as_an_argument_and_in_the_assertion(memo):
    result = run(FakeBank.sample(), TransferRequest("R-" + memo, "A001", "A002", Decimal("1.00"), memo))
    assert result["receipt_no"] == memo, "断言内容 " + SECRET


def test_secret_in_an_exception_message():
    bank = FakeBank.sample()
    bank.balances[SECRET] = Decimal("0")
    bank.blocked.add(SECRET)  # 往这个账户转账时，步骤"执行转账"抛出带着它的 TransferError
    result = run(bank, TransferRequest("R9", "A001", SECRET, Decimal("5.00")))
    raise RuntimeError("异常信息 " + SECRET + " " + result["reason"])
'''


def every_stored_byte(home: Path) -> bytes:
    """数据目录里所有文件的内容，库里每个压缩的列另外解开一份。"""
    out = bytearray()
    for f in sorted(home.rglob("*")):
        if f.is_file():
            out += f.read_bytes()
    con = sqlite3.connect(home / "pystepmap.db")
    try:
        for (gz,) in con.execute("select graph_gz from analyses").fetchall() + \
                con.execute("select paths_gz from runs where paths_gz is not null").fetchall():
            out += gzip.decompress(gz)
        for row in con.execute("select * from runs").fetchall():
            out += repr(row).encode("utf-8")
    finally:
        con.close()
    return bytes(out)


def test_nothing_stored_contains_a_value(store, transfer):
    """测试的参数、参数化的 id、断言内容、异常信息里的字符串：整个数据目录（含解开的压缩列）里都没有，只在内存里的
    输出末尾。"""
    (transfer / "tests" / "test_secret.py").write_text(SECRET_TESTS, encoding="utf-8")
    pid = with_command(store, transfer)
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "tests_failed", r
    run = store.get_run(pid, r["run_id"])
    assert run["summary"]["failed"] == 2 and run["summary"]["verify"]["ok"] is True
    tests = {t["test"] for t in run["tests"]}
    assert "tests/test_secret.py::test_secret_as_an_argument_and_in_the_assertion" in tests  # 参数化后缀去掉了
    paths = paths_of(store, pid, r["run_id"])
    exits = [k for t in paths["tests"] for f in t["functions"].values() for k in f["exits"]]
    assert any(k.endswith("|raise|TransferError") for k in exits)  # 异常只留类型名
    output = "\n".join(runner.last_output(store.path.parent, pid))
    assert SECRET in output  # pytest 的输出里有：只在内存里
    stored = every_stored_byte(store.path.parent)
    assert stored and SECRET.encode() not in stored
    assert SECRET.lower().encode() not in stored.lower()


OWN_CONFTEST = """
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
"""

OWN_TESTS = """
import os

import pystepmap
import pystepmap.core
import pystepmap.trace


def test_its_own_package_is_imported():
    root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert os.path.realpath(pystepmap.__file__).startswith(root + os.sep)
    assert pystepmap.core.double(2) == 4


def test_its_own_trace_module():
    assert pystepmap.trace._test_id("t.py::f[x]") == "t.py::f"
"""


def test_a_project_with_its_own_pystepmap_package(store, tmp_path):
    """被测项目自己有顶层包 pystepmap（像 pystepmap 仓库自己：里面还有一份 trace.py）：测试导入的是项目自己的包，照样有
    记录；平台的插件（数据目录里的 pystepmap_trace_plugin）不记录自己，项目自己的 trace.py 也只记一次。"""
    root = tmp_path / "work" / "ownpystepmap"
    (root / "src" / "pystepmap").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pystepmap" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pystepmap" / "core.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    (root / "src" / "pystepmap" / "trace.py").write_bytes(Path(flows.__file__).parents[3].joinpath(
        "core", "src", "pystepmap", "trace.py").read_bytes())
    (root / "tests" / "conftest.py").write_text(OWN_CONFTEST, encoding="utf-8")
    (root / "tests" / "test_own.py").write_text(OWN_TESTS, encoding="utf-8")
    pid = with_command(store, root)
    r = run_now(store, pid)
    assert r["kind"] == "ok" and r["status"] == "ok", (r, runner.last_output(store.path.parent, pid))
    run = store.get_run(pid, r["run_id"])
    assert (run["summary"]["tests"], run["summary"]["passed"]) == (2, 2)
    paths = paths_of(store, pid, r["run_id"])
    by_test = {t["test"]: t for t in paths["tests"]}
    own = by_test["tests/test_own.py::test_its_own_package_is_imported"]["functions"]
    assert own["src/pystepmap/core.py::double"]["executions"] == 1
    traced = by_test["tests/test_own.py::test_its_own_trace_module"]["functions"]
    assert traced["src/pystepmap/trace.py::_test_id"]["executions"] == 1  # 项目自己的那份：一次，不重复
    sids = {f for t in paths["tests"] for f in t["functions"]}
    assert all(f.startswith("src/pystepmap/") for f in sids), sids  # 插件自己（数据目录里的副本）不在记录里


# ---------- pystepmap 自己的图 ----------

FLOWS_FILE = "server/src/pystepmap_server/flows.py"
API_FILE = "server/src/pystepmap_server/api.py"
TESTFLOWS_FILE = "server/src/pystepmap_server/testflows.py"
CJK = re.compile(r"[一-鿿]")


def test_flows_follow_the_style_and_read_well_on_pystepmaps_own_canvas():
    result = check_project(REPO)
    assert result.violations == [] and result.skipped == []
    graph = analyze_project(REPO)
    assert not any("tests/" in f for f in graph.file_hashes), "测试目录（含夹具项目）不该进 pystepmap 自己的图"
    mine = {graph.functions[ln.root.function].qualname: ln for ln in graph.lines
            if graph.functions[ln.root.function].file in (FLOWS_FILE, TESTFLOWS_FILE)}
    # 后端出功能线的只有流程代码和平台的接口（阶段 7：create_api 里注册的接口也是入口，标题是函数说明的第一行）；
    # 存储、启动等别的文件不出。例外：worker.py 的 `if __name__ == "__main__":` 调用的 main 是分析子进程的入口，
    # 不用标记也认得出来（分组"脚本入口"，见 core 的 entries.py）
    server_lines = [ln for ln in graph.lines if graph.functions[ln.root.function].file.startswith("server/")]
    scripts = [ln for ln in server_lines if ln.group == "脚本入口"]
    assert [(graph.functions[ln.root.function].file, ln.kind) for ln in scripts] == [
        ("server/src/pystepmap_server/worker.py", "function")]
    server_lines = [ln for ln in server_lines if ln not in scripts]
    assert sorted({graph.functions[ln.root.function].file for ln in server_lines}) == [API_FILE, FLOWS_FILE, TESTFLOWS_FILE]
    routes = [ln for ln in server_lines if graph.functions[ln.root.function].file == API_FILE]
    assert len(routes) >= 25 and all(ln.kind == "route" and ln.group == "api" and CJK.search(ln.title) for ln in routes), \
        [(ln.title, ln.kind, ln.group) for ln in routes if not CJK.search(ln.title)]
    assert len(server_lines) - len(routes) == 5
    assert {q: (ln.title, ln.group, ln.kind) for q, ln in mine.items()} == {
        "add_project": ("添加项目", "项目管理", "workflow"),
        "reanalyze_project": ("重新分析", "项目管理", "workflow"),
        "remove_project": ("删除项目", "项目管理", "workflow"),
        "acknowledge_changes": ("标记流程变化已看过", "项目管理", "workflow"),
        "run_project_tests": ("运行测试并核对", "项目管理", "workflow"),
    }
    for ln in mine.values():
        fg = graph.functions[ln.root.function]
        assert ln.sid.startswith((f"line:{FLOWS_FILE}::", f"line:{TESTFLOWS_FILE}::"))
        shown = [n for n in fg.nodes if n.type in ("step", "cond", "end") and n.line]
        assert len(shown) >= 4
        for n in shown:
            assert CJK.search(n.title), f"{fg.qualname} 第 {n.line} 行的节点标题不是中文：{n.title!r}"
        assert not any(k == fg.key for k, _ in graph.selfcheck.unreachable + graph.selfcheck.dead_ends)
        # 每个结局都是终点：结局节点没有出边；走得到的结局只有代码里写的 outcome（没有"执行到函数末尾"）
        ends = {n.id: n for n in fg.nodes if n.type == "end"}
        assert not [e for e in fg.edges if e.src in ends], f"{fg.qualname} 的结局有出边"
        reached = {e.dst for e in fg.edges if e.dst in ends}
        assert reached and all(ends[i].line > 0 and ends[i].end_kind in ("ok", "failed") for i in reached), \
            [(ends[i].title, ends[i].end_kind) for i in reached]
        assert not any(n.sid == "end:implicit" and n.id in reached for n in ends.values())
    # 运行测试的流程：记录读不了、一条记录都没收到是编排函数里的判断，图上看得见，各自通到记下这次运行的步骤
    fg = graph.functions[mine["run_project_tests"].root.function]
    nodes = {n.id: n for n in fg.nodes}
    for cond, then in (("记录读不了？", "记下记录读不了的运行"), ("一条记录都没收到？", "记下没收到记录的运行")):
        c = next(n for n in fg.nodes if n.type == "cond" and n.title == cond)
        assert any(e.src == c.id and e.kind == "yes" and nodes[e.dst].title == then for e in fg.edges), cond


def test_removing_a_project_whose_lock_is_held_keeps_the_lock_file(store, shop):
    """删除时这个项目的锁正被拿着（另一个进程在分析）：照样删掉记录，锁文件留着（删了再建，两个进程会各锁一个文件）。"""
    pid = add_project(store, str(shop), None, None)["project_id"]
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(store.path.parent), pid], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert remove_project(store, pid)["kind"] == "ok"
        assert (store.path.parent / "locks" / f"{pid}.lock").exists()
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    assert store.get_project(pid) is None


def test_pystepmap_mcp_and_the_analysis_worker_never_load_the_test_runner(tmp_path):
    """分析（只读）和运行项目测试（执行项目代码）按导入分开。真的走一遍两个入口，之后 sys.modules 里都没有 runner、
    testflows、运行中的进度（live、core 的 progress）（也没有平台接口 app、api）：pystepmap mcp（pystepmap.cli.main(["mcp"])，标准输入是空的、服务马上结束）和分析
    子进程（python -E -P 跑 pystepmap_server.worker，分析、检查一个小项目）。只有平台接口（api.py）导入运行测试的代码。"""
    watched = ["pystepmap_server.runner", "pystepmap_server.testflows", "pystepmap_server.live", "pystepmap.progress",
               "pystepmap_server.app", "pystepmap_server.api"]
    out = tmp_path / "mcp.json"
    code = ("import json, sys\n"
            "from pystepmap.cli import main\n"
            "rc = main(['mcp'])\n"
            f"json.dump({{'rc': rc, 'tools': 'pystepmap_server.mcp_tools' in sys.modules, "
            f"'loaded': [m for m in {watched!r} if m in sys.modules]}}, open(sys.argv[1], 'w'))\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYSTEPMAP_", "PYTEST_"))} | {"PYSTEPMAP_HOME": str(tmp_path / "home")}
    subprocess.run([sys.executable, "-c", code, str(out)], stdin=subprocess.DEVNULL, env=env, capture_output=True, timeout=60,
                   check=True)
    assert json.loads(out.read_text()) == {"rc": 0, "tools": True, "loaded": []}
    proj = tmp_path / "proj"
    (proj / "app").mkdir(parents=True)
    (proj / "app" / "m.py").write_text("def f(x):\n    return x\n")
    code = ("import json, runpy, sys\n"
            "try:\n"
            "    runpy.run_module('pystepmap_server.worker', run_name='__main__', alter_sys=True)\n"
            "except SystemExit as e:\n"
            "    rc = e.code\n"
            f"json.dump({{'rc': rc, 'loaded': [m for m in {watched!r} if m in sys.modules]}}, open(sys.argv[1], 'w'))\n")
    for mode in ("analyze", "check"):
        request = json.dumps({"root": str(proj), "config": {}, "timeout": 60, "mode": mode})
        r = subprocess.run([sys.executable, "-E", "-P", "-c", code, str(out)], input=request, cwd=tmp_path,
                           capture_output=True, text=True, timeout=60, check=True)
        assert json.loads(r.stdout)["ok"] is True, mode
        assert json.loads(out.read_text()) == {"rc": 0, "loaded": []}, mode
    code = "import sys, pystepmap_server.api; print('pystepmap_server.runner' in sys.modules, 'pystepmap_server.live' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["True", "True"]


@pytest.fixture
def old_plugin_dir(tmp_path) -> Path:
    r = subprocess.run(["git", "show", "642bebe~1:core/src/flowmap/trace.py"], cwd=REPO, capture_output=True, check=False)
    if r.returncode != 0:
        pytest.skip("取不到旧版插件")
    old = tmp_path / "old"
    old.mkdir()
    (old / "pystepmap_old_trace.py").write_bytes(r.stdout.replace(b"flowmap", b"pystepmap").replace(b"FLOWMAP", b"PYSTEPMAP"))
    return old


def test_an_old_plugin_in_the_projects_addopts_is_mentioned_in_the_note(store, transfer, old_plugin_dir):
    """被测项目的 pytest 配置（addopts）里挂着一份旧版插件（不看单份启用的记号）：平台的这份先启用，旧版后启用，两份都
    在记。这次运行的说明里提醒记录有两份、数字可能重复。"""
    (transfer / "pytest.ini").write_text("[pytest]\naddopts = -p pystepmap_old_trace\n", encoding="utf-8")
    pid = with_command(store, transfer, f'PYTHONPATH="$PYTHONPATH:{old_plugin_dir}" {PYTEST}')
    run = store.get_run(pid, run_now(store, pid)["run_id"])
    assert "记录文件里有两份记录插件的输出（被测项目的环境里装了旧版 pystepmap 的插件？），数字可能重复" in run["summary"]["note"]
    assert "SecondPlugin" not in run["summary"]["note"] and run["summary"]["tests"] == 2 * EXAMPLE_CASES


def test_an_old_plugin_on_the_command_line_records_alone(store, transfer, old_plugin_dir):
    """测试命令自己写了 -p 旧版插件：它先启用，平台的这份看到名叫 pystepmap 的工具编号已被占用，不再启用——记录只有一份。"""
    pid = with_command(store, transfer, f'PYTHONPATH="$PYTHONPATH:{old_plugin_dir}" {PYTEST} -p pystepmap_old_trace')
    run = store.get_run(pid, run_now(store, pid)["run_id"])
    assert run["summary"]["tests"] == EXAMPLE_CASES and run["summary"]["note"] == ""
    assert any("不再重复记录" in line for line in runner.last_output(store.path.parent, pid))


# ---------- 只跑一条功能线的测试 ----------

TWO_FLOWS = '''
from pystepkit import outcome, step, workflow


@step(title="做甲")
def do_a(x):
    return x


@workflow(title="甲流程")
def flow_a(x):
    do_a(x)
    return outcome("ok", title="甲好了")


@step(title="做乙")
def do_b(x):
    return x


@workflow(title="乙流程")
def flow_b(x):
    do_b(x)
    return outcome("ok", title="乙好了")
'''
TWO_FLOWS_TESTS = '''
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flows2 import flow_a, flow_b  # noqa: E402


def test_a_once():
    assert flow_a(1)["title"] == "甲好了"


@pytest.mark.parametrize("x", [1, 2])
def test_a_twice(x):
    flow_a(x)


def test_b():
    flow_b(1)


def test_nothing():
    assert True
'''
LINE_A, LINE_B = "line:flows2.py::flow_a", "line:flows2.py::flow_b"
T = "tests/test_two.py::"


@pytest.fixture
def two(tmp_path) -> Path:
    root = tmp_path / "work" / "two"
    (root / "tests").mkdir(parents=True)
    (root / "flows2.py").write_text(TWO_FLOWS, encoding="utf-8")
    (root / "tests" / "test_two.py").write_text(TWO_FLOWS_TESTS, encoding="utf-8")
    return root


def test_line_run_needs_a_full_run_first_and_a_line_with_tests(store, two):
    pid = with_command(store, two)
    r = run_now(store, pid, line=LINE_A)
    assert (r["kind"], r["title"], r["code"]) == ("failed", "挑不出这条功能线的测试，没有运行", "no_full_run")
    assert "先运行一次全部测试" in r["reason"] and store.list_runs(pid) == []
    all_clear(store, pid)
    assert run_now(store, pid)["kind"] == "ok"
    r = run_now(store, pid, line="line:flows2.py::nope")
    assert r["code"] == "no_line" and len(store.list_runs(pid)) == 1
    all_clear(store, pid)


def test_line_run_runs_only_the_tests_that_walked_the_line_and_stores_the_scope(store, two):
    pid = with_command(store, two)
    full = run_now(store, pid)["run_id"]
    assert [t["test"] for t in store.get_run(pid, full)["tests"]] == [T + "test_a_once", T + "test_a_twice", T + "test_b",
                                                                      T + "test_nothing"]
    r = run_now(store, pid, line=LINE_A)
    assert (r["kind"], r["status"]) == ("ok", "ok")
    run = store.get_run(pid, r["run_id"])
    assert run["scope"] == {"line": LINE_A, "line_title": "甲流程", "tests": 2, "base_run": full}
    assert [(t["test"], t["cases"]) for t in run["tests"]] == [(T + "test_a_once", 1), (T + "test_a_twice", 2)]
    assert run["summary"]["tests"] == 3 and run["summary"]["verify"]["ok"] is True
    assert store.get_run(pid, full)["scope"] is None and run["command"] == PYTEST  # 命令没有改
    assert store.latest_full_run(pid)["id"] == full  # 单条线的运行不当作全量
    b = store.get_run(pid, run_now(store, pid, line=LINE_B)["run_id"])
    assert [t["test"] for t in b["tests"]] == [T + "test_b"] and b["scope"]["line_title"] == "乙流程"
    all_clear(store, pid)


def test_line_run_whose_tests_are_gone_is_stored_as_an_error_with_a_hint(store, two):
    pid = with_command(store, two)
    run_now(store, pid)
    test_file = two / "tests" / "test_two.py"
    test_file.write_text(test_file.read_text(encoding="utf-8").replace("def test_b()", "def test_b_renamed()"),
                         encoding="utf-8")
    r = run_now(store, pid, line=LINE_B)
    run = store.get_run(pid, r["run_id"])
    assert run["status"] == "error" and run["exit_code"] == 5 and run["scope"]["tests"] == 1
    assert "名单里的测试一个都没找到" in run["summary"]["note"] and run["tests"] == []
    all_clear(store, pid)


SHARED_HELPER = '''
from pystepkit import outcome, step, workflow


def audit(x):
    """记审计（两条流程共用的普通函数）"""
    return x


@step(title="做甲")
def do_a(x):
    return audit(x)


@workflow(title="甲流程")
def flow_a(x):
    do_a(x)
    return outcome("ok", title="甲好了")


@step(title="做乙")
def do_b(x):
    return audit(x)


@workflow(title="乙流程")
def flow_b(x):
    do_b(x)
    return outcome("ok", title="乙好了")
'''
SHARED_TESTS = '''
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flows2 import audit, do_a, flow_a, flow_b  # noqa: E402


def test_a():
    flow_a(1)


def test_b():
    flow_b(1)


def test_helper_only():
    assert audit(3) == 3


def test_step_only():
    assert do_a(2) == 2
'''


def test_a_test_walks_a_line_only_when_it_ran_the_lines_entry(store, tmp_path):
    """共用的深层函数（audit）两条线都涉及：只调用了它、或者只调用了线上一个步骤的测试不算走过这条线；只有执行过
    入口（工作流函数）的才算——挑测试和接口的 on_line 是同一个标准。"""
    from pystepmap.model import from_json
    from pystepmap_server.shared import line_entries, tests_on_line

    root = tmp_path / "work" / "shared"
    (root / "tests").mkdir(parents=True)
    (root / "flows2.py").write_text(SHARED_HELPER, encoding="utf-8")
    (root / "tests" / "test_two.py").write_text(SHARED_TESTS, encoding="utf-8")
    pid = with_command(store, root)
    full = run_now(store, pid)["run_id"]
    graph = from_json(store.graph_json(store.latest_analysis(pid)["id"]))
    line_a = next(ln for ln in graph.lines if ln.sid == LINE_A)
    assert [graph.functions[k].qualname for k in line_entries(graph, line_a)] == ["flow_a"]
    assert tests_on_line(store, pid, full, graph, LINE_A) == [T + "test_a"]
    assert tests_on_line(store, pid, full, graph, LINE_B) == [T + "test_b"]
    r = run_now(store, pid, line=LINE_A)
    run = store.get_run(pid, r["run_id"])
    assert [t["test"] for t in run["tests"]] == [T + "test_a"] and run["scope"]["tests"] == 1
