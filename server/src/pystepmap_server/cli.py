"""pystepmap serve：在 127.0.0.1 上启动平台。core 的命令行（pystepmap.cli）在用户运行 serve 时才导入这里，所以 core
不依赖 FastAPI。

先自己绑好端口再交给 uvicorn：端口被占用时能给出明确的中文提示（退出码 2），绑定和监听也没有空档。
同一个数据目录只允许一个服务进程：启动时对 <数据目录>/pystepmap.lock 加文件锁（flock，进程退出时系统自动释放），
已经被别的 pystepmap serve 锁着时提示并退出码 2。pystepmap mcp 不拿这把锁，和服务同时用一个数据目录；同一个项目的
分析靠项目级的锁（locks.py，<数据目录>/locks/）在进程之间互斥，库的写入靠 SQLite 的写锁。
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app, default_home
from .store import StoreError, private_dir

HOST = "127.0.0.1"  # 只监听本机
DEFAULT_PORT = 5179


LOCK_NAME = "pystepmap.lock"


class PortInUse(Exception):
    pass


class HomeInUse(Exception):
    def __init__(self, holder: str):
        super().__init__(holder)
        self.holder = holder


def _lock_home(home: Path, port: int = 0) -> int:
    """对数据目录加独占的文件锁，返回锁文件的描述符（服务结束前一直开着）；已经被别的进程锁着时抛 HomeInUse。
    锁文件里写两行：进程号、监听的端口（pystepmap mcp 请平台运行测试时按它找到平台，见 store.serve_port）。"""
    try:
        import fcntl
    except ImportError:  # Windows：没有 flock，不加锁
        return -1
    private_dir(home)  # 0700；以前建的更宽的目录收紧
    fd = os.open(home / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:  # 以前建的锁文件是 0644
        os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 100).decode("utf-8", "replace").strip().split("\n")[0]
        os.close(fd)
        raise HomeInUse(holder) from None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n{port}\n".encode())
    return fd


def _bind(port: int) -> socket.socket:
    """在 127.0.0.1:port 上绑一个监听用的套接字；被占用时抛 PortInUse。"""
    # 先连一下：别的进程监听着 0.0.0.0:port 时，macOS 上带 SO_REUSEADDR 的 bind 也可能成功，连得上就算被占用
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((HOST, port)) == 0:
            raise PortInUse
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 和 uvicorn 一样：刚停掉的服务留下的 TIME_WAIT 不算占用
    try:
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise PortInUse from None
    return sock


def _open_when_started(server: uvicorn.Server, url: str) -> None:
    for _ in range(200):  # 最多等 10 秒
        if server.started:
            webbrowser.open(url)
            return
        if server.should_exit:
            return
        time.sleep(0.05)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, home: Path | None = None) -> int:
    """启动平台，直到 Ctrl+C。返回退出码：0 正常停止，2 端口被占用、数据目录已被另一个服务占用或打不开。"""
    home = Path(home) if home is not None else default_home()
    try:
        sock = _bind(port)
    except PortInUse:
        print(f"端口 {port} 已被占用：可能 pystepmap serve 已经在运行（打开 http://{HOST}:{port}/ 看看），"
              f"或者用 --port 换一个端口", file=sys.stderr)
        return 2
    try:
        lock = _lock_home(home, port)
    except HomeInUse as e:
        sock.close()
        who = f"（进程 {e.holder}）" if e.holder else ""
        print(f"数据目录 {home} 已经有一个 pystepmap serve 在用{who}：同一个数据目录只能开一个服务。"
              f"先停掉它，或者用环境变量 PYSTEPMAP_HOME 换一个数据目录", file=sys.stderr)
        return 2
    except OSError as e:
        sock.close()
        print(f"无法打开平台数据目录 {home}：{e.strerror or e}", file=sys.stderr)
        return 2
    try:
        app = create_app(home)
    except StoreError as e:
        sock.close()
        _unlock(lock)
        print(f"无法打开平台数据目录：{e}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{port}/"
    print(f"pystepmap 平台已启动：{url}\n数据目录：{home}\n按 Ctrl+C 停止", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    if open_browser:
        threading.Thread(target=_open_when_started, args=(server, url), daemon=True).start()
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:  # uvicorn 正常收尾之后会把 Ctrl+C 再抛出来：这是用户要停，不是出错
        pass
    finally:
        sock.close()
        _unlock(lock)
    print("pystepmap 平台已停止", flush=True)
    return 0


def _unlock(fd: int) -> None:
    if fd >= 0:
        os.close(fd)  # 关掉描述符就释放了 flock；锁文件留着，下次启动接着用
