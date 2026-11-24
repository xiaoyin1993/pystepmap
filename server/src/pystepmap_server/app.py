"""组装平台的 FastAPI 应用：/api 下是接口（api.py），/ 是打包好的前端页面。

平台数据目录默认 ~/.pystepmap，可用环境变量 PYSTEPMAP_HOME 改；库文件是其中的 pystepmap.db。
前端默认取仓库里的 web/dist（npm run build 的产物），可用环境变量 PYSTEPMAP_WEB_DIST 指定；没有打包好的前端时，
/ 返回一段中文说明怎么打包，接口照常可用。

收尾（lifespan）：开始服务时删掉上次被强杀的服务留下的运行临时目录（<数据目录>/runs/tmp-…）；正常退出时取消并等完
正在跑的测试（runner.stop_all：结束它们的进程组，这些运行记成已取消，项目锁放掉）。服务被强杀时由 runner 的看门狗
结束测试的进程组。

只接受本机：服务只监听 127.0.0.1；另外 Host 头必须是 127.0.0.1、localhost 或 ::1（带不带端口都行），挡住 DNS 重绑定
（别的网站把自己的域名解析到 127.0.0.1，再用浏览器调接口）；POST / PUT / DELETE 等修改请求带了 Origin 头时，它的主机
也必须是这几个之一，挡住别的网页发来的跨站请求。不带 Origin 的请求（curl、脚本、测试）照常放行。都返回 403
{"error": "中文说明"}。Vite 开发服务器的代理（changeOrigin 开或不开）发来的 Host / Origin 都是本机地址，照常放行。
"""

from __future__ import annotations

import html
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from . import runner
from .api import api_http_error, create_api
from .store import DB_NAME, Store, default_home

__all__ = ["DB_NAME", "create_app", "default_home"]  # DB_NAME、default_home 在 store.py（pystepmap mcp 不导入这里），这里照旧能取
_REPO_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"  # server/src/pystepmap_server/app.py → 仓库根

_NO_FRONTEND = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>pystepmap</title>
<style>body{{background:#16181d;color:#d6d9e0;font:15px/1.7 -apple-system,"PingFang SC",sans-serif;
max-width:720px;margin:12vh auto;padding:0 16px}}code{{background:#262a33;padding:2px 6px;border-radius:4px}}</style>
</head><body>
<h2>pystepmap 平台已启动，但前端还没有打包</h2>
<p>在 pystepmap 仓库的 <code>web</code> 目录下运行 <code>npm install &amp;&amp; npm run build</code>，然后重启
<code>pystepmap serve</code>。</p>
<p>找的位置：<code>{dist}</code>（可用环境变量 <code>PYSTEPMAP_WEB_DIST</code> 指定别的目录）。</p>
<p>接口照常可用：<code>/api/health</code>。</p>
</body></html>
"""


def default_web_dist() -> Path:
    custom = os.environ.get("PYSTEPMAP_WEB_DIST")
    return Path(custom).expanduser() if custom else _REPO_WEB_DIST


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_name(value: str) -> str:
    """Host 头里的主机名（去掉端口，IPv6 去掉方括号），小写。"""
    try:
        return (urlsplit(f"//{value.strip()}").hostname or "").lower()
    except ValueError:
        return ""


class LocalOnly:
    """只接受本机的 Host 和 Origin（见模块说明）。和 Starlette 的 TrustedHostMiddleware 一样按去掉端口的主机名比较，
    另外查修改请求的 Origin，被拒绝时回中文的 {"error": …}。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        message = None
        if _host_name(host) not in LOCAL_HOSTS:
            message = f"只接受用本机地址访问（127.0.0.1 或 localhost），这次的 Host 是 {host or '空的'}"
        elif scope["method"] not in _SAFE_METHODS and "origin" in headers:
            origin = headers["origin"]
            try:
                origin_host = (urlsplit(origin).hostname or "").lower()
            except ValueError:
                origin_host = ""
            if origin_host not in LOCAL_HOSTS:
                message = f"只接受本机页面发来的修改请求，这次来自 {origin}"
        if message is None:
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": message}, status_code=403)(scope, receive, send)


async def _outer_http_error(request: Request, exc: StarletteHTTPException):
    """/api 下没有对应接口的路径（含 /api 本身）回中文的 {"error": …}，挂着前端页面时也一样；接口以外的路径
    （前端页面、静态文件）找不到、方法不对时同样回中文的 {"error": …}，不是框架默认的 {"detail": "Not Found"}。"""
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return await api_http_error(request, exc)
    if exc.status_code in (204, 304):  # 没有响应体的状态码照框架的处理
        return await http_exception_handler(request, exc)
    if exc.status_code == 404:
        message = f"没有这个页面：{path}"
    elif exc.status_code == 405:
        message = f"这个地址不支持 {request.method}：{path}"
    else:
        message = str(exc.detail)
    return JSONResponse({"error": message}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def create_app(home: Path, web_dist: Path | None = None) -> FastAPI:
    """home：平台数据目录（不存在时创建）。web_dist：打包好的前端目录，省略时见 default_web_dist。"""
    store = Store(Path(home) / DB_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 开始服务时：删掉上次被强杀的服务（和没来得及收尾的看门狗）留下的运行临时目录。同一个数据目录只能开一个
        # pystepmap serve，所以这时不会有别的运行在用它们
        runner.clean_leftovers(Path(home))
        yield
        # 正常退出（Ctrl+C、SIGTERM，uvicorn 先收尾再走到这里）：结束在跑的测试进程组，等它们记下"已取消"、放开锁
        await run_in_threadpool(runner.stop_all, Path(home))

    app = FastAPI(title="pystepmap", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.home = Path(home)
    app.add_middleware(LocalOnly)
    app.add_exception_handler(StarletteHTTPException, _outer_http_error)
    app.mount("/api", create_api(store))  # 先挂接口：/api 下的路径不会落到静态文件上
    dist = Path(web_dist) if web_dist is not None else default_web_dist()
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        page = _NO_FRONTEND.format(dist=html.escape(str(dist)))
        # 不用 @app.get 装饰器：引擎会把它认成一个接口入口，pystepmap 自己的图上就多出一条只返回说明页的功能线
        app.add_api_route("/", lambda: page, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    return app
