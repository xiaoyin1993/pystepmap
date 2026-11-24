"""项目级的分析锁：同一进程里第二次拿不到、释放后拿得到；别的进程拿着时 project_locked 为真、它退出后为假；锁文件
只对本人可读写；查询不建文件；项目 id 不合法时拒绝。"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import threading
import time

import pytest
from pystepmap_server import locks
from pystepmap_server.locks import project_locked, try_lock_project, unlock_project


def test_second_lock_in_the_same_process_fails_until_released(tmp_path):
    home = tmp_path / "home"
    fd = try_lock_project(home, "abc123")
    assert fd is not None and fd >= 0
    try:
        assert try_lock_project(home, "abc123") is None
        assert project_locked(home, "abc123") is True
        other = try_lock_project(home, "other1")  # 别的项目不受影响
        assert other is not None
        unlock_project(other)
    finally:
        unlock_project(fd)
    assert project_locked(home, "abc123") is False
    again = try_lock_project(home, "abc123")
    assert again is not None
    unlock_project(again)


HOLDER = """
import sys
from pathlib import Path
from pystepmap_server.locks import try_lock_project
fd = try_lock_project(Path(sys.argv[1]), sys.argv[2])
print("locked" if fd is not None else "busy", flush=True)
sys.stdin.read()  # 一直拿着，直到测试关掉标准输入
"""


def test_lock_held_by_another_process(tmp_path):
    home = tmp_path / "home"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home), "proj01"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert project_locked(home, "proj01") is True
        assert try_lock_project(home, "proj01") is None
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    assert project_locked(home, "proj01") is False  # 进程退出：系统释放了锁
    fd = try_lock_project(home, "proj01")
    assert fd is not None
    unlock_project(fd)


def test_lock_survives_a_killed_holder(tmp_path):
    """拿着锁的进程被杀掉（不是正常退出）：锁照样释放，不会留下"永远在分析"。"""
    home = tmp_path / "home"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home), "proj02"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    holder.kill()
    holder.wait(10)
    holder.stdin.close()
    holder.stdout.close()
    assert project_locked(home, "proj02") is False


def test_a_quick_probe_does_not_make_a_real_lock_fail(tmp_path):
    """project_locked 查询时会短暂拿一下共享锁：正好撞上的 try_lock_project 稍等一下照样拿到，不谎报"正在分析"。"""
    import fcntl

    home = tmp_path / "home"
    unlock_project(try_lock_project(home, "proj03"))  # 建好锁文件
    probe = os.open(home / "locks" / "proj03.lock", os.O_RDONLY)
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    threading.Timer(0.03, os.close, args=(probe,)).start()  # 30 毫秒后放开
    started = time.monotonic()
    fd = try_lock_project(home, "proj03")
    assert fd is not None and time.monotonic() - started < 1
    unlock_project(fd)


def test_query_does_not_create_anything(tmp_path):
    home = tmp_path / "home"
    assert project_locked(home, "nothing") is False
    assert not home.exists()


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_lock_files_are_private(tmp_path, loose_umask):
    home = tmp_path / "home"
    unlock_project(try_lock_project(home, "proj04"))
    assert mode(home / "locks") == 0o700 and mode(home / "locks" / "proj04.lock") == 0o600
    os.chmod(home / "locks", 0o755)
    os.chmod(home / "locks" / "proj04.lock", 0o644)
    unlock_project(try_lock_project(home, "proj04"))
    assert mode(home / "locks") == 0o700 and mode(home / "locks" / "proj04.lock") == 0o600


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".", "..", "x" * 65, "a b", "名字", "a.lock"])
def test_bad_project_ids_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        try_lock_project(tmp_path, bad)
    with pytest.raises(ValueError):
        project_locked(tmp_path, bad)
    assert not (tmp_path / "locks").exists()


def test_without_fcntl_there_is_no_lock(tmp_path, monkeypatch):
    """没有 flock 的系统（Windows）：不加锁，try_lock_project 返回 -1，unlock_project(-1) 什么都不做。"""
    monkeypatch.setattr(locks, "fcntl", None)
    assert try_lock_project(tmp_path, "proj05") == -1
    assert try_lock_project(tmp_path, "proj05") == -1
    assert project_locked(tmp_path, "proj05") is False
    unlock_project(-1)


def open_fds() -> int:
    where = "/dev/fd" if os.path.isdir("/dev/fd") else f"/proc/{os.getpid()}/fd"
    return len(os.listdir(where))


@pytest.mark.parametrize("err", [errno.ENOTSUP, errno.ENOLCK, errno.EOPNOTSUPP])
def test_file_system_without_flock(tmp_path, monkeypatch, err):
    """数据目录在不支持 flock 的文件系统上（某些网络盘）：查询算"没人拿着"，加锁和没有 fcntl 时一样返回 -1（只在
    本进程内互斥），都不抛错、不漏描述符——列项目的接口不能因此 500。"""
    import fcntl

    home = tmp_path / "home"
    unlock_project(try_lock_project(home, "proj06"))  # 锁文件已经在了

    class NoFlock:
        LOCK_EX, LOCK_SH, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_NB, fcntl.LOCK_UN

        @staticmethod
        def flock(fd, op):
            raise OSError(err, os.strerror(err))

    monkeypatch.setattr(locks, "fcntl", NoFlock)
    before = open_fds()
    assert project_locked(home, "proj06") is False
    assert try_lock_project(home, "proj06") == -1
    assert try_lock_project(home, "proj07") == -1  # 新建锁文件的也一样
    unlock_project(-1)
    assert open_fds() == before


def test_unlock_tolerates_a_failing_unlock(tmp_path, monkeypatch):
    """放锁时 flock(LOCK_UN) 出错：照样关掉描述符（关掉就放开了），不往外抛。"""
    import fcntl

    fd = try_lock_project(tmp_path, "proj08")
    real = fcntl.flock

    class FailingUnlock:
        LOCK_EX, LOCK_SH, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_NB, fcntl.LOCK_UN

        @staticmethod
        def flock(f, op):
            if op == fcntl.LOCK_UN:
                raise OSError(errno.ENOLCK, "no locks")
            return real(f, op)

    monkeypatch.setattr(locks, "fcntl", FailingUnlock)
    unlock_project(fd)
    with pytest.raises(OSError):
        os.fstat(fd)  # 已经关掉了
    monkeypatch.undo()
    assert project_locked(tmp_path, "proj08") is False


def test_remove_lock_deletes_a_free_lock_file(tmp_path):
    """删项目时删掉它的锁文件：没人拿着时删掉；没有锁文件时什么都不做。"""
    home = tmp_path / "home"
    unlock_project(try_lock_project(home, "gone01"))
    path = locks.lock_path(home, "gone01")
    assert path.exists()
    assert locks.remove_lock(home, "gone01") is True
    assert not path.exists()
    assert locks.remove_lock(home, "gone01") is False  # 已经没有了
    assert locks.remove_lock(tmp_path / "never", "gone01") is False
    with pytest.raises(ValueError):
        locks.remove_lock(home, "../x")


def test_remove_lock_leaves_a_lock_that_is_held(tmp_path):
    """有人拿着（还在分析、跑测试）时不删：删了再建，两个进程会各锁一个不同的文件。"""
    home = tmp_path / "home"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home), "held01"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert locks.remove_lock(home, "held01") is False
        assert locks.lock_path(home, "held01").exists() and project_locked(home, "held01")
    finally:
        holder.stdin.close()
        holder.wait(10)
        holder.stdout.close()
    fd = try_lock_project(home, "held01")
    try:
        assert locks.remove_lock(home, "held01") is False  # 本进程拿着也一样
    finally:
        unlock_project(fd)
    assert locks.remove_lock(home, "held01") is True


def test_remove_lock_without_fcntl(tmp_path, monkeypatch):
    home = tmp_path / "home"
    unlock_project(try_lock_project(home, "nofc01"))
    monkeypatch.setattr(locks, "fcntl", None)
    assert locks.remove_lock(home, "nofc01") is True and not locks.lock_path(home, "nofc01").exists()
