"""被截断文字的完整形式：Node.title_full / subtitle_full、FunctionGraph.title_full、FlowLine.title_full、
Edge.label_full。只在显示形式被截断时才有（JSON 里也只在这时出现），显示形式本身和以前一样。"""

import json
from pathlib import Path

from conftest import analyze_files, fg_of, find_fn, node_by
from pystepmap.analyze import analyze_project
from pystepmap.model import from_json, to_json
from pystepmap.titles import FULL_LIMIT, clean_full

FIXTURES = Path(__file__).parent / "fixtures"
FULL_KEYS = ("title_full", "subtitle_full", "label_full")


def cut(s: str, limit: int) -> str:
    return s[: limit - 1] + "…"


def strip_full(data: dict) -> dict:
    """去掉图 JSON（已解析）里所有的 *_full 字段。"""
    for fn in data["functions"].values():
        for d in (fn, *fn["nodes"], *fn["edges"]):
            for k in FULL_KEYS:
                d.pop(k, None)
    for line in data["lines"]:
        line.pop("title_full", None)
    return data


def test_clean_full():
    assert clean_full("一二三四五", 4) == "一二三四五" and clean_full("一二三四", 4) == ""
    assert clean_full("  **加粗**  `code`\n   换行 ", 5) == "加粗 code 换行"  # 和 clean 一样清理，只是不截断
    assert clean_full("x" * 3000, 60) == "x" * (FULL_LIMIT - 1) + "…"


def test_long_comment_title(tmp_path):
    comment = "先看这封邮件是不是已经处理过：同一个 message_id 可能被 Gmail 推送好几次，重复处理会让下游的统计和通知都多出一份"
    src = f"""
        def seen(x): ...
        def f(x):
            # {comment}
            seen(x)
    """
    n = node_by(fg_of(tmp_path, src, "f"), type="step")
    assert len(comment) > 60
    assert (n.title, n.title_full) == (cut(comment, 60), comment)
    assert (n.subtitle, n.subtitle_full) == ("seen", "")


def test_long_docstring_title_of_function_start_node_line_and_callers(tmp_path):
    doc = "处理 Gmail 推送过来的一封入站邮件：读邮件、检查附件和发件人、按内容类型分别归档，失败时通知账户所有者、记审计日志并交给重试任务"
    store_doc = "把这封邮件连同它的全部附件一起归档到冷存储里，并在归档索引里记下原因、时间和处理它的工作进程编号，方便以后审计时按原因查找"
    files = {"app/api.py": f'''
        @router.post("/inbound")
        async def inbound(x):
            """{doc}

            第二段不进标题。
            """
            await store(x)

        async def store(x):
            """{store_doc}"""
    '''}
    g = analyze_files(tmp_path, files)
    fg = find_fn(g, "inbound")
    assert (fg.title, fg.title_full) == (cut(doc, 60), doc)
    start = fg.nodes[0]
    assert (start.type, start.title, start.title_full) == ("start", cut(doc, 60), doc)
    line = next(ln for ln in g.lines if ln.id == fg.key)
    assert (line.title, line.title_full) == (cut(doc, 60), doc)
    step = node_by(fg, type="step")  # 标题取被调用函数的文档注释
    assert (step.title, step.title_full) == (cut(store_doc, 60), store_doc)
    assert find_fn(g, "store").title_full == store_doc


def test_long_condition_title_and_subtitle_are_one_line(tmp_path):
    src = """
        def archive(m): ...
        def f(message):
            if (message.attachments and message.sender_is_trusted
                    and message.size_in_bytes < 10_000_000 and not message.is_spam):
                archive(message)
    """
    cond = "message.attachments and message.sender_is_trusted and message.size_in_bytes < 10_000_000 and not message.is_spam"
    n = node_by(fg_of(tmp_path, src, "f"), type="cond")
    assert (n.title, n.title_full) == (cut(cond, 60), cond)  # 标题 60 字
    assert (n.subtitle, n.subtitle_full) == (cut(cond, 80), cond)  # 条件原文 80 字


def test_long_case_patterns_label_full_and_branch_stays_the_short_label(tmp_path):
    p1 = '{"kind": "invoice", "currency": currency, "amount": amount, **rest} if currency in allowed'
    p2 = '"text/plain" | "text/html" | "text/markdown" | "text/csv" | "application/json" | "application/xml"'
    src = f"""
        def archive(m): ...
        def f(items, allowed):
            for item in items:
                match item:
                    case {p1}:
                        archive(item)
                    case {p2}:
                        continue
    """
    fg = fg_of(tmp_path, src, "f")
    m = node_by(fg, type="cond")
    out = [e for e in fg.edges if e.src == m.id]
    case = next(e for e in out if e.kind == "case")
    assert (case.label, case.label_full, case.branch) == (cut(p1, 80), p1, cut(p1, 80))  # ** 不像标题那样去掉
    # 分支直接是 continue：连线改成 loop_back，label 和 label_full 照旧
    back = next(e for e in out if e.kind == "loop_back" and e.branch != "else")
    assert (back.label, back.label_full, back.branch) == (cut(p2, 80), p2, cut(p2, 80))
    no_match = next(e for e in out if e.branch == "else")
    assert (no_match.label, no_match.label_full) == ("都不匹配", "")


def test_case_labels_cut_to_the_same_text_stay_one_edge(tmp_path):
    """两个模式截断后相同、都直接越过 match：和以前一样只有一条连线（label_full 不参与去重），带第一个的完整模式。"""
    a, b = '"' + "a" * 90 + '1"', '"' + "a" * 90 + '2"'
    src = f"""
        def work(): ...
        def f(x):
            match x:
                case {a}:
                    pass
                case {b}:
                    pass
                case _:
                    work()
            work()
    """
    fg = fg_of(tmp_path, src, "f")
    cut_edges = [e for e in fg.edges if e.label == cut(a, 80)]
    assert cut_edges and cut(a, 80) == cut(b, 80)
    assert [(e.kind, e.label_full) for e in cut_edges] == [("case", a)]


def test_full_form_is_capped(tmp_path):
    doc = "很长的文档注释" * 500
    fg = fg_of(tmp_path, f'''
        def f():
            """{doc}"""
    ''', "f")
    assert fg.title == cut(doc, 60)
    assert fg.title_full == doc[: FULL_LIMIT - 1] + "…" and len(fg.title_full) == FULL_LIMIT


def test_short_texts_have_no_full_form(tmp_path):
    files = {"app/api.py": '''
        @router.get("/ping")
        async def ping(x):
            # 看一眼
            await check(x)
            for i in x:
                if i:
                    return 1
            match x:
                case 1:
                    await check(x)
            try:
                await check(x)
            except ValueError:
                await check(x)
                raise
        async def check(x):
            """探活"""
    '''}
    g = analyze_files(tmp_path, files)
    text = to_json(g)
    assert not any(f'"{k}"' in text for k in FULL_KEYS)
    assert all(not n.title_full and not n.subtitle_full for fg in g.functions.values() for n in fg.nodes)
    assert [(ln.title, ln.title_full) for ln in g.lines] == [("GET /ping", "")]


# ---------- 测试项目 fixtures/long_texts：每种会被截断的文字各有一处 ----------


def fixture_graph():
    g = analyze_project(FIXTURES / "long_texts")
    g.root = "<root>"
    return g


def test_fixture_full_forms_are_written_only_where_the_text_was_cut():
    data = json.loads(to_json(fixture_graph()))
    places = [("function", fn, "title") for fn in data["functions"].values()]
    places += [("node", n, name) for fn in data["functions"].values() for n in fn["nodes"] for name in ("title", "subtitle")]
    places += [("edge", e, "label") for fn in data["functions"].values() for e in fn["edges"]]
    places += [("line", ln, "title") for ln in data["lines"]]
    found: dict[str, int] = {}
    for where, d, short in places:
        full = f"{short}_full"
        # 这个项目里以"…"结尾的都是截断的结果；被截断的才有完整形式，完整形式以截断前的部分开头
        assert (full in d) == d[short].endswith("…"), (where, short, d[short])
        if full in d:
            assert d[full].startswith(d[short][:-1]) and len(d[full]) > len(d[short])
            found[f"{where}.{full}"] = found.get(f"{where}.{full}", 0) + 1
    assert found == {
        "function.title_full": 2,  # inbound、archive 的文档注释
        "node.title_full": 12,  # 两个开始节点、注释、条件、循环头、三个 archive 步骤、两个调用合成的那个步骤、捕获、抛出、返回
        "node.subtitle_full": 4,  # 条件、循环头、两个调用的显示名、捕获的异常类型
        "edge.label_full": 2,  # 两个 case 模式（一个改成了 loop_back）
        "line.title_full": 1,  # POST /inbound
    }


def test_fixture_json_roundtrip():
    g = fixture_graph()
    back = from_json(to_json(g))
    assert back == g
    inbound = find_fn(back, "inbound")
    assert inbound.title_full.endswith("记审计日志并交给重试任务")
    assert [ln.title_full for ln in back.lines if ln.id == inbound.key] == [inbound.title_full]
    catch = node_by(inbound, type="catch")
    assert catch.subtitle_full == ("(MailboxQuotaExceededError, AttachmentTooLargeForTheConfiguredLimitError, "
                                   "UpstreamAnalysisServiceTemporarilyUnavailableError)")
    assert sorted(e.label_full[:12] for e in inbound.edges if e.label_full) == ['"text/plain"', '{"kind": "in']


def test_old_graph_without_full_forms_is_readable():
    g = fixture_graph()
    old = from_json(json.dumps(strip_full(json.loads(to_json(g)))))
    for fg in g.functions.values():
        fg.title_full = ""
        for n in fg.nodes:
            n.title_full = n.subtitle_full = ""
        for e in fg.edges:
            e.label_full = ""
    for ln in g.lines:
        ln.title_full = ""
    assert old == g


def test_fixture_graph_without_full_forms_is_byte_identical_to_before():
    """fixtures/long_texts.graph.json 是加 *_full 之前的引擎（8ad7def）生成的（root 换成 <root>）。去掉 *_full 字段，
    新图逐字节相同：已有字段的值、顺序、格式都没变。改了引擎、图本来就该变时，用当时的引擎重新生成那份文件（去掉
    *_full 字段）。"""
    data = strip_full(json.loads(to_json(fixture_graph())))
    before = (FIXTURES / "long_texts.graph.json").read_text(encoding="utf-8")
    assert json.dumps(data, ensure_ascii=False, indent=2) == before


def test_first_sentence():
    from pystepmap.titles import first_sentence as fs
    assert fs("校验配置。不合法时抛 ConfigError。") == "校验配置"
    assert fs("在后台线程里跑 target(cancel)（运行测试的流程）。等到流程宣布开始") == "在后台线程里跑 target(cancel)（运行测试的流程）"
    assert fs("新建定时任务;仅 super_admin 可调用") == "新建定时任务"
    assert fs("完成回调的依赖工厂——需要 AsyncSession") == "完成回调的依赖工厂"
    assert fs("Load the raw mail. Then parse it.") == "Load the raw mail"
    assert fs("Read config v1.2 from disk") == "Read config v1.2 from disk"  # 句号后面不是空白：不是句子的结尾
    assert fs("方法名（含类体里的别名；嵌套在类里的也算）/ 模块级函数名") == "方法名（含类体里的别名；嵌套在类里的也算）/ 模块级函数名"
    assert fs("注。后面才是正文") == "注。后面才是正文"  # 断出来不到 4 个字：不断
    assert fs("单题：推理 → 评分。失败隔离") == "单题：推理 → 评分"  # 冒号不断
    assert fs("没有标点的一行") == "没有标点的一行" and fs("") == ""


def test_titles_from_docstrings_take_the_first_sentence_and_keep_the_whole_line_as_full_form(tmp_path):
    files = {"app/api.py": '''
        @router.post("/inbound")
        async def inbound(x):
            """处理入站邮件。读邮件、检查附件，失败时通知。

            第二段不进标题。
            """
            await store(x)
            await plain(x)

        async def store(x):
            """归档邮件；连同附件一起"""

        async def plain(x):
            """只有一句的说明。"""
    '''}
    g = analyze_files(tmp_path, files)
    fg = find_fn(g, "inbound")
    whole = "处理入站邮件。读邮件、检查附件，失败时通知。"
    assert (fg.title, fg.title_full) == ("处理入站邮件", whole)
    assert (fg.nodes[0].title, fg.nodes[0].title_full) == ("处理入站邮件", whole)
    line = next(ln for ln in g.lines if ln.id == fg.key)
    assert (line.title, line.title_full) == ("处理入站邮件", whole)
    steps = [n for n in fg.nodes if n.type == "step"]
    assert [(n.title, n.title_full) for n in steps] == [("归档邮件", "归档邮件；连同附件一起"), ("只有一句的说明", "")]
