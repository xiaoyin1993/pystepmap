"""pystepmap serve：子进程里用空闲端口启动，响应 /api/health，结束时关掉；端口被占用时提示并退出码 2。"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from pystepmap.cli import main


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp_path, home, port) -> subprocess.Popen:
    env = {**os.environ, "PYSTEPMAP_HOME": str(home), "PYSTEPMAP_WEB_DIST": str(tmp_path / "no-web")}
    return subprocess.Popen([sys.executable, "-m", "pystepmap.cli", "serve", "--no-open", "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_healthy(proc: subprocess.Popen, port: int) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.1)
    raise AssertionError(proc.stderr.read() if proc.poll() is not None else "没有响应")


def stop(proc: subprocess.Popen) -> tuple[str, str]:
    proc.send_signal(signal.SIGINT)  # 和用户按 Ctrl+C 一样
    try:
        return proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def test_serve_starts_and_answers_health(tmp_path):
    port = free_port()
    home = tmp_path / "home"
    proc = start_server(tmp_path, home, port)
    try:
        assert wait_healthy(proc, port)["ok"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            assert "npm run build" in r.read().decode("utf-8")
        assert (home / "pystepmap.db").is_file()
        # 真实服务上也只接受本机的 Host、本机页面的修改请求
        for method, path, headers in [("GET", "/api/projects", {"Host": "attacker.example"}),
                                      ("POST", "/api/projects", {"Origin": "http://evil.example"})]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(method, path, body=b"{}" if method == "POST" else None,
                         headers={**headers, "Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 403 and "本机" in json.loads(resp.read())["error"]
            conn.close()
    finally:
        out, err = stop(proc)
    assert proc.returncode == 0, err
    assert f"http://127.0.0.1:{port}/" in out and str(home) in out


def test_serve_reports_a_port_in_use(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PYSTEPMAP_HOME", str(tmp_path / "home"))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert main(["serve", "--no-open", "--port", str(port)]) == 2
    err = capsys.readouterr().err
    assert f"端口 {port} 已被占用" in err and "--port" in err
    assert not (tmp_path / "home").exists()  # 没启动就不建数据目录


def test_one_server_per_data_directory(tmp_path):
    """同一个数据目录上的第二个 pystepmap serve（换了端口）提示并退出码 2；第一个停掉以后可以再启动。"""
    home = tmp_path / "home"
    first_port, second_port = free_port(), free_port()
    first = start_server(tmp_path, home, first_port)
    try:
        wait_healthy(first, first_port)
        second = start_server(tmp_path, home, second_port)
        _, err = second.communicate(timeout=30)
        assert second.returncode == 2
        assert "已经有一个 pystepmap serve 在用" in err and f"进程 {first.pid}" in err and "PYSTEPMAP_HOME" in err
        other = start_server(tmp_path, tmp_path / "other-home", second_port)  # 别的数据目录不受影响
        try:
            assert wait_healthy(other, second_port)["ok"] is True
        finally:
            stop(other)
    finally:
        stop(first)
    assert first.returncode == 0
    again = start_server(tmp_path, home, first_port)
    try:
        assert wait_healthy(again, first_port)["ok"] is True
    finally:
        stop(again)


def test_lock_file_is_private(tmp_path):
    """锁文件（里面是进程号）只对本人可读写：新建的 0600，以前建的 0644 收紧；数据目录 0700。"""
    import stat

    from pystepmap_server.cli import LOCK_NAME, _lock_home, _unlock

    old = os.umask(0o022)
    try:
        home = tmp_path / "home"
        fd = _lock_home(home)
        _unlock(fd)
        assert stat.S_IMODE(os.stat(home / LOCK_NAME).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
        os.chmod(home / LOCK_NAME, 0o644)
        os.chmod(home, 0o755)
        _unlock(_lock_home(home))
        assert stat.S_IMODE(os.stat(home / LOCK_NAME).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    finally:
        os.umask(old)


def local_port() -> int:
    """5391–5399 里一个空闲的端口（本阶段自己起的服务只用这几个）。"""
    for port in range(5391, 5400):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise AssertionError("5391–5399 都被占用了")


def call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


def test_stopping_the_server_ends_running_tests(tmp_path):
    """测试跑着的时候 pystepmap serve 收到 Ctrl+C：测试的进程组被结束，这次运行记成已取消，服务照常以退出码 0 停下。"""
    import shutil
    from pathlib import Path

    from pystepmap_server.store import Store

    root = tmp_path / "work" / "transfer"
    shutil.copytree(Path(__file__).resolve().parents[2] / "examples" / "transfer", root,
                    ignore=shutil.ignore_patterns("__pycache__"))
    pidfile = tmp_path / "pgid"
    port, home = local_port(), tmp_path / "home"
    proc = start_server(tmp_path, home, port)
    try:
        wait_healthy(proc, port)
        status, project = call(port, "POST", "/api/projects", {"root": str(root)})
        assert status == 201, project
        pid = project["id"]
        command = f"echo $$ > {pidfile}; sleep 60 & sleep 60"
        assert call(port, "PUT", f"/api/projects/{pid}/test-command", {"command": command})[0] == 200
        assert call(port, "POST", f"/api/projects/{pid}/runs", {"confirm": True, "command": command}) == \
            (202, {"run": "started"})
        deadline = time.monotonic() + 15
        while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        pgid = int(pidfile.read_text())
    finally:
        _out, err = stop(proc)
    assert proc.returncode == 0, err
    try:
        os.killpg(pgid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    assert not alive
    store = Store(home / "pystepmap.db")
    try:
        assert [r["status"] for r in store.list_runs(pid)] == ["cancelled"]
    finally:
        store.close()
    assert not list((home / "runs").glob("tmp-*"))


def test_serve_port_is_read_from_the_lock_file_only_while_the_lock_is_held(tmp_path):
    """服务把端口写进锁文件的第二行；serve_port 只在锁被人拿着（服务在跑）时给出它。"""
    from pystepmap_server.cli import LOCK_NAME, _lock_home, _unlock
    from pystepmap_server.store import serve_port

    home = tmp_path / "home"
    assert serve_port(home) is None  # 没有锁文件
    fd = _lock_home(home, 5391)
    try:
        assert (home / LOCK_NAME).read_text().split() == [str(os.getpid()), "5391"]
        assert serve_port(home) == 5391
    finally:
        _unlock(fd)
    assert serve_port(home) is None  # 服务停了：文件还在，锁没人拿
    fd = _lock_home(home)  # 没给端口（0）
    try:
        assert serve_port(home) is None
    finally:
        _unlock(fd)
