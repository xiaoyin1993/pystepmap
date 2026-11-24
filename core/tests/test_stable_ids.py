"""稳定标识 sid：函数、节点、功能线各有一个不含行号的标识，重新分析后用来找回同一个东西。"""

import hashlib
import re

import pytest
from conftest import CORE_SRC, analyze_files, fg_of, find_fn, node_by
from pystepmap.analyze import analyze_project
from pystepmap.build import _head_tokens


def _h(tokens: str) -> str:
    """头部记号流（记号之间一个空格）的 sha1 前 8 位。"""
    return hashlib.sha1(tokens.encode("utf-8")).hexdigest()[:8]


def _line_of(g, qualname):
    fg = find_fn(g, qualname)
    [line] = [ln for ln in g.lines if ln.root.function == fg.key]
    return line


def assert_unique_sids(g):
    """整个图里函数 sid 互不相同、每个函数内节点 sid 互不相同、功能线 sid 互不相同，且都不含行号。"""
    fn_sids = [fg.sid for fg in g.functions.values()]
    assert all(fn_sids) and len(set(fn_sids)) == len(fn_sids)
    assert g.sids == {fg.sid: fg.key for fg in g.functions.values()}
    assert list(g.sids) == sorted(g.sids)
    for fg in g.functions.values():
        assert fg.sid == f"{fg.file}::{fg.qualname}" or re.fullmatch(re.escape(f"{fg.file}::{fg.qualname}") + r"#\d+",
                                                                     fg.sid)
        node_sids = [n.sid for n in fg.nodes]
        assert all(node_sids) and len(set(node_sids)) == len(node_sids), fg.key
        for n in fg.nodes:
            assert n.sid in ("start", "end:implicit", "end:uncaught") \
                or re.fullmatch(rf"{n.type}:[0-9a-f]{{8}}(#\d+)?", n.sid), (fg.key, n.sid)
    line_sids = [ln.sid for ln in g.lines]
    assert all(line_sids) and len(set(line_sids)) == len(line_sids)
    for ln in g.lines:
        root_sid = g.functions[ln.root.function].sid
        assert ln.sid == f"line:{root_sid}" or re.fullmatch(re.escape(f"line:{root_sid}") + r"#\d+", ln.sid)


ORDERS = '''
@router.post("/orders")
async def place(order):
    """下单"""
    # 先查库存
    if not await in_stock(order):
        raise OutOfStock(order)
    for item in order.items:
        await reserve(item)
    try:
        await pay(order)
    except PayError:
        await cancel(order)
        return {"status": "failed"}
    match order.kind:
        case "gift":
            await wrap(order)
        case _:
            pass
    await notify.kiq(order)
    return {"status": "ok"}


@broker.task()
async def notify(order):
    await send(order)


class OutOfStock(Exception): ...
class PayError(Exception): ...
async def in_stock(order): ...
async def reserve(item): ...
async def pay(order): ...
async def cancel(order): ...
async def wrap(order): ...
async def send(order): ...
'''

INSERTED = '''


async def audit_log(order):
    """在上面插进来的新函数"""
    await send(order)

'''


def test_sids_survive_lines_and_a_function_inserted_above(tmp_path):
    a = analyze_files(tmp_path / "a", {"app/orders.py": ORDERS})
    b = analyze_files(tmp_path / "b", {"app/orders.py": INSERTED + ORDERS})
    for name in ("place", "notify"):
        fa, fb = find_fn(a, name), find_fn(b, name)
        assert fa.key != fb.key  # key 里有行号，变了
        assert fa.sid == fb.sid == f"app/orders.py::{name}"
        assert [n.sid for n in fa.nodes] == [n.sid for n in fb.nodes]
        assert [n.line for n in fa.nodes if n.line] != [n.line for n in fb.nodes if n.line]
        assert a.sids[fa.sid] == fa.key and b.sids[fb.sid] == fb.key
    la, lb = _line_of(a, "place"), _line_of(b, "place")  # notify 由 place 派发，在 place 的线里
    assert la.id != lb.id
    assert la.sid == lb.sid == "line:app/orders.py::place"
    # 原有的每个函数都按 sid 找得回来，节点 sid 一个不差
    nodes_a = {fg.sid: [n.sid for n in fg.nodes] for fg in a.functions.values()}
    nodes_b = {fg.sid: [n.sid for n in fg.nodes] for fg in b.functions.values()}
    assert set(nodes_b) - set(nodes_a) == {"app/orders.py::audit_log"}
    assert all(nodes_b[s] == nodes_a[s] for s in nodes_a)
    assert_unique_sids(a)
    assert_unique_sids(b)


WORK = '''
def load(x): ...
def save(x): ...
def audit(x): ...
def check(x): ...
def notify(x): ...
def work(x):
    a = load(x)
    save(a)
    if check(a):
        notify(a)
    return a
'''


def test_statement_inserted_in_the_middle_keeps_other_node_sids(tmp_path):
    before = fg_of(tmp_path / "a", WORK, "work")
    after = fg_of(tmp_path / "b", WORK.replace("    save(a)\n", "    save(a)\n    audit(x)\n"), "work")
    new = node_by(after, subtitle="audit")
    assert new.sid == "step:" + _h("audit ( x )")
    assert [n.sid for n in after.nodes if n is not new] == [n.sid for n in before.nodes]
    assert [n.line for n in after.nodes if n is not new] != [n.line for n in before.nodes]


TWICE = '''
def save(x): ...
def check(x): ...
def twice(x):
    save(x)
    if check(x):
        return x
    save(x)
    return x
'''


def test_same_text_gets_a_suffix_that_goes_away_when_the_first_is_deleted(tmp_path):
    g = fg_of(tmp_path / "a", TWICE, "twice")
    save = "step:" + _h("save ( x )")
    assert [n.sid for n in g.nodes if n.type == "step"] == [save, save + "#2"]
    assert node_by(g, type="cond").sid == "cond:" + _h("if check ( x )")
    ends = [n.sid for n in g.nodes if n.type == "end"]
    assert ends == ["end:" + _h("return x"), "end:" + _h("return x") + "#2", "end:uncaught"]
    # 删掉第一条 save(x)：剩下那条不再带后缀（已知性质，写在 Node.sid 的注释里）
    g2 = fg_of(tmp_path / "b", TWICE.replace("    save(x)\n    if check", "    if check"), "twice")
    assert [n.sid for n in g2.nodes if n.type == "step"] == [save]
    assert [n.sid for n in g2.nodes if n.type != "step"] == [n.sid for n in g.nodes if n.type != "step"]


HEADS = '''
def load(x): ...
def check(x): ...
def other(x): ...
def go(x): ...
def fetch(x): ...
def use(x): ...
def more(x): ...
def step(x): ...
def open_db(x): ...
def lock(): ...
def save(x): ...
def call(x): ...
def handle(e): ...
def pick(x): ...
def one(): ...
def finish(x): ...
class Bad(Exception): ...
def f(items, kind):
    # 标题来自这条注释
    x = load(items)   # 行尾注释
    if check(x):  # 条件后的注释
        go(x)
    elif other(x):
        go(x)
    for i in fetch(items):
        use(i)
    while more(x):
        x = step(x)
    with open_db(x) as db, lock():
        save(db)
    try:
        call(
            x,  # 参数注释
        )
    except (KeyError, ValueError) as e:
        handle(e)
    except:
        handle(None)
    match pick(kind):
        case 1:
            one()
    if x:
        raise Bad(x)
    return finish(x)
'''


def test_node_sid_hashes_the_statement_head_only(tmp_path):
    g = fg_of(tmp_path, HEADS, "f")
    expected = [  # 头部的记号流：注释、换行、缩进不算，记号之间一个空格
        ("start", "start"),
        ("step", "x = load ( items )"),
        ("cond", "if check ( x )"),
        ("step", "go ( x )"),
        ("cond", "elif other ( x )"),
        ("step", "go ( x )"),
        ("loop", "for i in fetch ( items )"),
        ("step", "use ( i )"),
        ("loop", "while more ( x )"),
        ("step", "x = step ( x )"),
        ("step", "with open_db ( x ) , lock ( )"),  # with 的 as 目标不算
        ("step", "save ( db )"),
        ("step", "call ( x , )"),  # 跨行语句：里面的注释不算
        ("step", "handle ( e )"),
        ("catch", "except ( KeyError , ValueError )"),
        ("step", "handle ( None )"),
        ("catch", "except"),
        ("cond", "match pick ( kind )"),
        ("step", "one ( )"),
        ("cond", "if x"),
        ("end", "raise Bad ( x )"),
        ("step", "return finish ( x )"),
        ("end", "return finish ( x )"),
        ("end", "end:uncaught"),
    ]
    want = {}
    for node_type, text in expected:
        base = text if text in ("start", "end:uncaught") else f"{node_type}:{_h(text)}"
        want[base] = want.get(base, 0) + 1
        want_sid = base if want[base] == 1 else f"{base}#{want[base]}"
        hits = [n for n in g.nodes if n.sid == want_sid]
        assert len(hits) == 1 and hits[0].type == node_type, (node_type, text, [(n.type, n.sid) for n in g.nodes])
    assert len(g.nodes) == len(expected)
    # 两条 go(x) 按源码位置编号：if 分支里的在前
    go = "step:" + _h("go ( x )")
    assert node_by(g, line=23).sid == go and node_by(g, line=25).sid == go + "#2"


def test_titles_and_comments_do_not_change_node_sids(tmp_path):
    g = fg_of(tmp_path / "a", HEADS, "f")
    edited = (HEADS.replace("# 标题来自这条注释", "# 换了一个标题")
              .replace("# 行尾注释", "# 改过")
              .replace("# 条件后的注释", "")
              .replace("# 参数注释", "# 改过的参数注释"))
    g2 = fg_of(tmp_path / "b", edited, "f")
    assert node_by(g, line=21).title == "标题来自这条注释" and node_by(g2, line=21).title == "换了一个标题"
    assert [n.sid for n in g2.nodes] == [n.sid for n in g.nodes]


DUPES = '''
import os

if os.environ.get("FAST"):
    def pick(x):
        return x
else:
    def pick(x):
        return -x


def pick(x):
    return 0


class A:
    def run(self): ...
    def run(self): ...


class B:
    def run(self): ...


def outer():
    def inner(): ...
    def inner(): ...
    return inner


class C:
    def outer(self):
        def inner(): ...
        return inner
'''


def test_function_sids_tell_apart_redefinitions_inner_functions_and_methods(tmp_path):
    g = analyze_files(tmp_path, {"app/mod.py": DUPES})
    by_line = sorted(g.functions.values(), key=lambda fg: fg.line)
    assert [fg.sid for fg in by_line] == [
        "app/mod.py::pick",
        "app/mod.py::pick#2",
        "app/mod.py::pick#3",
        "app/mod.py::A.run",
        "app/mod.py::A.run#2",
        "app/mod.py::B.run",
        "app/mod.py::outer",
        "app/mod.py::outer.<locals>.inner",
        "app/mod.py::outer.<locals>.inner#2",
        "app/mod.py::C.outer",
        "app/mod.py::C.outer.<locals>.inner",
    ]
    assert all(g.sids[fg.sid] == fg.key for fg in by_line)
    assert_unique_sids(g)


def test_sids_unique_in_a_small_project_and_in_pystepmap_itself(tmp_path):
    assert_unique_sids(analyze_files(tmp_path / "small", {"app/orders.py": ORDERS, "app/mod.py": DUPES,
                                                          "app/heads.py": HEADS, "app/twice.py": TWICE}))
    own = analyze_project(CORE_SRC)
    assert len(own.functions) > 100
    assert_unique_sids(own)


# 重排前后记号相同的头部：sid 不变。每项是同一个函数体的两种写法（函数体缩进 4 格）。
SAME_TOKENS = {
    "wrap": ("    notify(order, reason)\n",
             "    notify(\n        order, reason\n    )\n"),
    "collapse": ("    notify(\n        order,\n        reason\n    )\n",
                 "    notify(order, reason)\n"),
    "reindent": ("    x = notify(order,\n               reason)\n",
                 "    x = notify(order,\n        reason)\n"),
    "backslash": ("    x = notify(order, reason) + \\\n        notify(reason, order)\n",
                  "    x = notify(order, reason) + notify(reason, order)\n"),
    "comment": ("    notify(\n        order,  # 订单\n        reason,  # 原因\n    )\n",
                "    notify(\n        order,\n        reason,\n    )\n"),
    "hash_in_string": ("    notify(\n        \"a#b\"  # 真注释\n    )\n",
                       "    notify(\"a#b\")\n"),
    "unclosed_with": ("    with (ctx(),  # c1\n          ctx()) as y:\n        notify(y)\n",
                      "    with (ctx(), ctx()) as y:\n        notify(y)\n"),
    "unclosed_if": ("    if (notify(order) and  # c2\n            notify(reason)):\n        notify(1)\n",
                    "    if (notify(order) and notify(reason)):\n        notify(1)\n"),
    "except_tuple": (("    try:\n        notify(1)\n    except (KeyError,  # c3\n            ValueError) as e:\n"
                      "        notify(e)\n"),
                     "    try:\n        notify(1)\n    except (KeyError, ValueError) as e:\n        notify(e)\n"),
    "fstring": ("    notify(f\"{\n        order  # 在 f-string 里\n    }\")\n",
                "    notify(f\"{order}\")\n"),
}
TOKENS_PRELUDE = "def notify(*a): ...\ndef ctx(): ...\n\n\ndef f(order, reason):\n"


@pytest.mark.parametrize("name", sorted(SAME_TOKENS))
def test_rewrapping_reindenting_and_comments_keep_node_sids(tmp_path, name):
    one, other = SAME_TOKENS[name]
    a = fg_of(tmp_path / "a", TOKENS_PRELUDE + one, "f")
    b = fg_of(tmp_path / "b", TOKENS_PRELUDE + other, "f")
    assert len(a.nodes) > 3 and [n.sid for n in a.nodes] == [n.sid for n in b.nodes]


WITH_PRELUDE = "def h(x): ...\ndef use(x): ...\nasync def f(self, d):\n"


def _with_sid(tmp_path, name, head):
    """函数体是 head 加一条 use(1) 时，with 那个步骤节点的 sid。"""
    fg = fg_of(tmp_path / name, f"{WITH_PRELUDE}    {head}\n        use(1)\n", "f")
    return next(n.sid for n in fg.nodes if n.type == "step")


def test_multi_item_with_sid_ignores_every_as_target(tmp_path):
    """多项 with：每一项的 as 目标都不参与 sid（最后一项的本来就在头部之外），改名不改变 sid。"""
    heads = {
        "xy": "with h(1) as x, h(2) as y:",
        "xz": "with h(1) as x, h(2) as z:",
        "wy": "with h(1) as w, h(2) as y:",
        "bare": "with h(1), h(2):",
        "tuple": "with h(1) as (a, b), h(2) as y:",
        "wrapped": "with h(1) as ((a)), h(2) as y:",
        "attr": "with h(1) as self.a, h(2) as y:",
        "subscript": "with h(1) as d[h(9)], h(2) as y:",
        "async": "async with h(1) as x, h(2) as y:",
    }
    sids = {name: _with_sid(tmp_path, name, head) for name, head in heads.items()}
    assert sids["xy"] == sids["xz"] == sids["wy"] == "step:" + _h("with h ( 1 ) , h ( 2 )")
    assert {sids[n] for n in ("bare", "tuple", "wrapped", "attr", "subscript")} == {sids["xy"]}
    assert sids["async"] == "step:" + _h("async with h ( 1 ) , h ( 2 )")
    # 包住上下文表达式的括号照算；目标里的非 ASCII 字符不影响跳过的位置
    assert _with_sid(tmp_path, "ctx_paren", "with (h(1)) as x, h(2) as y:") == "step:" + _h("with ( h ( 1 ) ) , h ( 2 )")
    assert _with_sid(tmp_path, "unicode", 'with h("é") as 变量, h(2) as y:') == "step:" + _h('with h ( "é" ) , h ( 2 )')
    three = [_with_sid(tmp_path, f"three{i}", f"with h(1) as {a}, h(2) as {b}, h(3) as {c}:")
             for i, (a, b, c) in enumerate(["xyz", "pqr"])]
    assert three[0] == three[1] == "step:" + _h("with h ( 1 ) , h ( 2 ) , h ( 3 )")
    # 带括号的写法：括号照旧算记号，换行和注释不算
    paren = [_with_sid(tmp_path, f"paren{i}", head) for i, head in enumerate([
        "with (h(1) as x, h(2) as y):", "with (h(1) as w, h(2) as z):",
        "with (\n        h(1) as x,  # 说明\n        h(2) as y,\n    ):"])]
    assert paren[0] == paren[1] == paren[2] == "step:" + _h("with ( h ( 1 ) , h ( 2 )")


def test_single_item_with_sid_is_unchanged_and_context_changes_count(tmp_path):
    """单项 with 的 sid 与改动前相同（下面的值是改动前算出来的）；上下文表达式变了 sid 就变。"""
    assert _with_sid(tmp_path, "one", "with h(1) as x:") == "step:cfe9731d" == "step:" + _h("with h ( 1 )")
    assert _with_sid(tmp_path, "one_bare", "with h(1):") == "step:cfe9731d"
    assert _with_sid(tmp_path, "one_paren", "with (h(1) as x):") == "step:b713f147"
    assert _with_sid(tmp_path, "async_one", "async with h(1) as x:") == "step:efa3593a"
    base = _with_sid(tmp_path, "two", "with h(1) as x, h(2) as y:")
    assert _with_sid(tmp_path, "changed_first", "with h(3) as x, h(2) as y:") != base
    assert _with_sid(tmp_path, "changed_last", "with h(1) as x, h(4) as y:") != base
    assert _with_sid(tmp_path, "changed_one", "with h(3) as x:") != "step:cfe9731d"


def test_changed_argument_quotes_and_trailing_comma_change_the_sid(tmp_path):
    bodies = ["    notify(order, 'x')\n", "    notify(\n        order, 'x'\n    )\n",  # 前两种记号相同
              "    notify(reason, 'x')\n", "    notify(order, \"x\")\n", "    notify(order, 'x',)\n"]
    sids = [[n.sid for n in fg_of(tmp_path / str(i), TOKENS_PRELUDE + body, "f").nodes if n.type == "step"]
            for i, body in enumerate(bodies)]
    assert sids[1] == sids[0]
    assert all(other != sids[0] for other in sids[2:])  # 改了参数、引号、末尾逗号：记号不同


def test_head_tokens_fall_back_to_source_without_comments():
    """分词在头部中途出错（解析过的源码里不会发生）：退回到去掉已找到的注释、压缩空白的源码。分词到末尾才出错
    （少右括号）时照常用记号。"""
    assert _head_tokens("notify(order,  # 注释\n       '''未结束") == "notify(order, '''未结束"
    assert _head_tokens("with (ctx(),  # c1\n      ctx()") == "with ( ctx ( ) , ctx ( )"
    assert _head_tokens("except") == "except"


TRY_DUPES = '''
try:
    import ujson

    def dumps(x):
        return ujson.dumps(x)
except ImportError:
    import json

    def dumps(x):
        return json.dumps(x)


class Box:
    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        self._size = value
'''

OVERLOADS = '''
from typing import overload


@overload
def conv(x: int) -> int: ...
@overload
def conv(x: str) -> str: ...
def conv(x):
    return x
'''


def test_try_except_definitions_and_property_accessors_get_numbered(tmp_path):
    g = analyze_files(tmp_path, {"app/mod.py": TRY_DUPES})
    by_line = sorted(g.functions.values(), key=lambda fg: fg.line)
    assert [fg.sid for fg in by_line] == ["app/mod.py::dumps", "app/mod.py::dumps#2",
                                          "app/mod.py::Box.size", "app/mod.py::Box.size#2"]
    assert_unique_sids(g)


def test_overload_stubs_number_the_implementation_and_removing_one_repoints_it(tmp_path):
    g = analyze_files(tmp_path / "a", {"app/mod.py": OVERLOADS})
    assert [fg.sid for fg in sorted(g.functions.values(), key=lambda fg: fg.line)] == [
        "app/mod.py::conv", "app/mod.py::conv#2", "app/mod.py::conv#3"]
    # 删掉第一个 @overload 桩：实现从 conv#3 变成 conv#2（已知性质，写在 graph-schema.md 里）
    fewer = OVERLOADS.replace("@overload\ndef conv(x: int) -> int: ...\n", "", 1)
    g2 = analyze_files(tmp_path / "b", {"app/mod.py": fewer})
    assert max(g2.functions.values(), key=lambda fg: fg.line).sid == "app/mod.py::conv#2"
