"""pystepmap serve 在 core 这一侧：平台后端（pystepmap-server）没装时打印安装提示、退出码 2。core 只依赖标准库，
这里不导入后端包。真正启动服务的测试在 server/tests/test_serve.py。"""

import sys

from pystepmap.cli import main


def test_serve_without_backend_prints_install_hint(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pystepmap_server", None)  # 让 import pystepmap_server 失败，像没装一样
    monkeypatch.setitem(sys.modules, "pystepmap_server.cli", None)
    assert main(["serve", "--no-open"]) == 2
    err = capsys.readouterr().err
    assert "没有安装平台后端" in err and "pystepmap_server" in err and "uv sync" in err


def test_mcp_without_backend_prints_install_hint(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pystepmap_server", None)
    monkeypatch.setitem(sys.modules, "pystepmap_server.mcp_server", None)
    assert main(["mcp"]) == 2
    captured = capsys.readouterr()
    assert "没有安装平台后端" in captured.err and "pystepmap_server" in captured.err and "uv sync" in captured.err
    assert "pystepmap mcp" in captured.err
    assert captured.out == ""  # 标准输出只给协议消息


def test_serve_rejects_bad_port(capsys):
    assert main(["serve", "--no-open", "--port", "70000"]) == 2
    assert "65535" in capsys.readouterr().err
