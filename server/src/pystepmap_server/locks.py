"""项目级的分析锁：同一个项目的分析在进程之间互斥。平台服务（pystepmap serve）和 MCP（pystepmap mcp）是两个进程，读写
同一个数据目录，都走 flows 里的流程；两边同时分析同一个项目时，后来的那个得到"正在分析"。

锁是 <数据目录>/locks/<项目 id>.lock 上的 flock（独占、不阻塞）。flock 跟着打开的文件走：同一进程里另开一次也拿不到；
进程退出（包括被杀掉）时系统自动释放，不会留下"永远在分析"。锁文件用完不删（删了再建，两个进程可能各锁一个不同的
文件）；只在删除项目时删（remove_lock：自己先拿到锁再删，有人拿着时留着）。测试运行（flows.run_project_tests）拿的
也是这把锁。locks 目录 0700、锁文件 0600，和数据目录里别的东西一样只有本人能读写。没有 fcntl 的系统（Windows）不加锁：
try_lock_project 返回 -1，project_locked 总是 False（那里只有进程内的互斥）。数据目录所在的文件系统不支持 flock
（某些网络盘：ENOTSUP、ENOLCK 等）时一样：加锁返回 -1、查询算没人拿着，不抛错（列项目的接口不能因此失败）。

project_locked 查询时会短暂拿一下共享锁；正好撞上的 try_lock_project 等一小会儿（最多 _RETRY_SECONDS）再试，不把这
一瞬当成"正在分析"。这里不是流程代码，不加 pystepkit 标记。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from pathlib import Path

from .store import private_dir

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

logger = logging.getLogger(__name__)

LOCKS_DIR = "locks"
_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RETRY_SECONDS = 0.1
_RETRY_STEP = 0.01


def lock_path(home: Path, project_id: str) -> Path:
    """项目的锁文件。项目 id 只能是字母、数字、_、-（库里的 id 都是这样）：不让它拼出 locks 目录之外的路径。"""
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ValueError(f"项目 id 不合法：{project_id!r}")
    return Path(home) / LOCKS_DIR / f"{project_id}.lock"


def try_lock_project(home: Path, project_id: str) -> int | None:
    """拿项目的分析锁（不阻塞）：拿到时返回锁文件的描述符（用 unlock_project 释放），被别人拿着时 None，没有 fcntl 或
    文件系统不支持 flock 时 -1（只有进程内的互斥）。建不了 locks 目录、锁文件时抛 OSError。"""
    path = lock_path(home, project_id)
    if fcntl is None:
        return -1
    private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "posix" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:  # 以前建的、比这宽的
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + _RETRY_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(_RETRY_STEP)
            except OSError as e:  # 文件系统不支持 flock
                logger.warning("数据目录所在的文件系统不支持文件锁（%s），项目 %s 的分析只在本进程内互斥",
                               e.strerror or e, project_id)
                return -1
    finally:
        if not locked:
            os.close(fd)


def unlock_project(fd: int | None) -> None:
    """释放 try_lock_project 拿到的锁（None、-1 什么都不做）。"""
    if fd is None or fd < 0:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # 关掉描述符就放开了，这一步出错不要紧
        pass
    finally:
        os.close(fd)


def remove_lock(home: Path, project_id: str) -> bool:
    """删除项目时删掉它的锁文件。先拿锁（不阻塞）再删：拿得到说明没人在用，删掉；有人拿着（另一个进程还在分析、
    本进程还在跑测试）时留着，返回 False（删了再建，两个进程会各锁一个不同的文件；留下一个空文件不碍事）。没有锁文件
    时 False。没有 fcntl 时直接删。删不了（权限等）时记一条日志，返回 False。"""
    path = lock_path(home, project_id)
    try:
        if fcntl is None:
            path.unlink()
            return True
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("没能删掉项目 %s 的锁文件：%s", project_id, e.strerror or e)
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except OSError:  # 文件系统不支持 flock：没有跨进程的锁，直接删
            pass
        try:
            path.unlink()  # 拿着锁删：之后来拿锁的会新建一个文件
        except OSError as e:
            logger.warning("没能删掉项目 %s 的锁文件：%s", project_id, e.strerror or e)
            return False
        return True
    finally:
        os.close(fd)


def project_locked(home: Path, project_id: str) -> bool:
    """这个项目的分析锁此刻有没有被拿着（别的进程，或者本进程）。不建任何文件；锁文件打不开时算没有。"""
    path = lock_path(home, project_id)
    if fcntl is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # 还没有锁文件（从没分析过），或者打不开
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:  # 文件系统不支持 flock：没有跨进程的锁，算没人拿着
        return False
    finally:
        os.close(fd)  # 拿到的共享锁随之释放
    return False
