"""没有 await 的调用落在项目外对象上时也画成步骤（external.py）。"""

from __future__ import annotations

from conftest import analyze_files
from pystepmap.config import parse_config


def steps(g, qualname: str) -> list[str]:
    fg = next(f for f in g.functions.values() if f.qualname == qualname)
    return [r.name for n in fg.nodes if n.type == "step" for r in n.calls]


def conds(g, qualname: str) -> list[str]:
    fg = next(f for f in g.functions.values() if f.qualname == qualname)
    return [n.subtitle or n.title for n in fg.nodes if n.type == "cond"]


SHOP = {"app/main.py": '''
    import logging
    import sqlite3
    import smtplib
    from fastapi import FastAPI, HTTPException

    app = FastAPI()
    logger = logging.getLogger(__name__)
    db = sqlite3.connect("shop.db")

    def send_mail(user_id, text):
        s = smtplib.SMTP("localhost")
        s.sendmail("a", "b", text)
        s.close()

    @app.post("/orders")
    def create_order(o):
        row = db.execute("select 1").fetchone()
        if not row:
            raise HTTPException(404, "商品不存在")
        total = row[1] * o.qty
        if o.coupon:
            db.execute("update coupon set used=1")
        if total > 5000:
            status = "待审核"
        else:
            status = "已下单"
            db.execute("update product set stock=stock-1")
        cur = db.execute("insert into orders values(?)", (status,))
        db.commit()
        logger.info("done %s", cur.lastrowid)
        send_mail(o.user_id, status)
        return {"id": cur.lastrowid}
'''}


def test_sync_calls_on_external_objects_are_steps(tmp_path):
    g = analyze_files(tmp_path, SHOP)
    # 模块级的外部对象（db = sqlite3.connect(...)）上的调用是步骤；接在调用结果后面的 .fetchone() 由 db.execute 代表；
    # 日志不算
    assert steps(g, "create_order") == ["db.execute", "db.execute", "db.execute", "db.execute", "db.commit", "send_mail"]
    # 只包着这类调用的 if 不再消失
    assert conds(g, "create_order") == ["not row", "o.coupon", "total > 5000"]
    # 构造对象不算步骤，但它的结果是外部对象：上面干活的方法是步骤，close 这类不是
    assert steps(g, "send_mail") == ["s.sendmail"]


def test_the_switch_restores_the_old_graph(tmp_path):
    g = analyze_files(tmp_path, SHOP, parse_config({"external_calls": False}))
    assert steps(g, "create_order") == ["send_mail"] and conds(g, "create_order") == ["not row"]
    assert steps(g, "send_mail") == []


def test_which_objects_count_as_external(tmp_path):
    g = analyze_files(tmp_path, {
        "app/db.py": '''
            import redis
            cache = redis.Redis()
        ''',
        "app/svc.py": '''
            import json
            import os
            import re
            import requests
            from fastapi import Depends, FastAPI
            from sqlalchemy import select, text
            from sqlalchemy.orm import Session
            from app.db import cache

            app = FastAPI()

            def helper(x):
                return x

            @app.get("/a")
            def by_import(url):
                requests.get(url)                 # 直接落在库上：get 也算
                json.dumps({})                    # 纯计算的库
                os.path.join("a", "b")
                re.match("a", "b")
                cache.set("k", 1)                 # 从项目里别的模块导入的外部对象
                cache.get("k")                    # 直接落在外部对象上：读也是一次对外的调用
                cache.pool.get("k")               # 对象上的对象：读数据的方法不算

            @app.get("/b")
            def by_param(db: Session, s: "Session | None", plain, dep=Depends(helper)):
                stmt = select(1).where(text("1=1"))   # 拼查询语句不算
                db.add(plain)
                db.execute(stmt)
                s.commit()
                dep.flush()
                plain.save()                      # 不知道是什么的参数：不算

            @app.get("/c")
            def by_local(url):
                r = requests.post(url)
                r.raise_for_status()              # 读结果的方法不算
                data = r.json()
                data.append(1)
                with requests.Session() as http:
                    http.put(url)
                requests = None                   # 这一行之后 requests 是局部名……整个函数里都按局部名算
        ''',
        "app/cls.py": '''
            import httpx
            from fastapi import FastAPI

            app = FastAPI()

            class Client:
                def __init__(self, http):
                    self.http = http
                    self.seen = set()
                    self.rows = []
                    self.logger = None

                def push(self, item):
                    self.seen.add(item)           # 普通数据
                    self.rows.append(item)
                    self.logger.info("x")         # 日志
                    self.http.post("/x", json=item)
                    self.http.headers.get("a")    # 读数据
                    self.note(item)

                def note(self, item): ...

            @app.post("/d")
            def push(item):
                Client(httpx.Client()).push(item)
        ''',
    })
    assert steps(g, "by_import") == ["requests.get", "cache.set", "cache.get"]
    assert steps(g, "by_param") == ["db.add", "db.execute", "s.commit", "dep.flush"]
    assert steps(g, "by_local") == []  # requests 在函数里被重新绑定了：不再当模块级的那个库
    assert steps(g, "Client.push") == ["http.post", "self.note"]  # 显示名照旧省掉 self


def test_local_results_of_external_calls_carry_on(tmp_path):
    g = analyze_files(tmp_path, {"m.py": '''
        import sqlite3
        import subprocess

        def run(path):
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("select 1")
            rows = cur.fetchall()
            rows.sort()
            with open(path) as f:
                f.read()
            subprocess.run(["ls"])
            conn.close()

        if __name__ == "__main__":
            run("x")
    '''})
    assert steps(g, "run") == ["sqlite3.connect", "conn.cursor", "cur.execute", "subprocess.run"]


def test_awaited_and_project_calls_are_unchanged(tmp_path):
    g = analyze_files(tmp_path, {"m.py": '''
        from fastapi import FastAPI
        app = FastAPI()

        async def load(x):
            return x

        @app.get("/")
        async def h(client, x):
            await client.fetch(x)
            await load(x)
            client.fetch(x)
    '''})
    assert steps(g, "h") == ["client.fetch", "load"]
