import json

import pytest
from pystepmap.model import (
    CallRef,
    Edge,
    FlowLine,
    FunctionGraph,
    LineTask,
    Node,
    ProjectGraph,
    SelfCheck,
    UnresolvedCall,
    fn_key,
    from_json,
    parse_key,
    to_json,
)


def sample_graph() -> ProjectGraph:
    k1 = fn_key("app/t.py", "handle", 10)
    k2 = fn_key("app/t.py", "send_mail", 40)
    fg = FunctionGraph(
        key=k1, file="app/t.py", qualname="handle", line=10, end_line=30,
        kind="route", title="处理请求", is_async=True, marked=True,
        group="邮件", route="POST /pubsub", retries=False,
        sid="app/t.py::handle",
        nodes=[
            Node(id="n1", type="start", title="开始", sid="start"),
            Node(id="n2", type="cond", title="有附件？", sid="cond:5d2a0c1e", subtitle="msg.attachments", line=12,
                 end_line=12),
            Node(
                id="n3", type="step", title="发邮件", sid="step:0b7d3f42", subtitle="send_mail", line=14, end_line=14,
                calls=[CallRef(name="svc.send_mail", target=k2, resolution="exact",
                               is_dispatch=True, conditional=True, dispatches=[k2])],
                passthrough=k2,
            ),
            Node(id="n4", type="end", title="完成", sid="end:implicit", end_kind="ok", fields={"status": "ok"}),
        ],
        edges=[
            Edge("n1", "n2", "next"),
            Edge("n2", "n3", "yes", "是", branch="yes"),
            Edge("n2", "n4", "no", branch="no"),
            Edge("n3", "n4", "next"),
        ],
        call_targets=[k2],
        unresolved=[UnresolvedCall(14, "other.mystery"), UnresolvedCall(14, "派发 未知任务"),
                    UnresolvedCall(15, "other.mystery")],
    )
    lines = [
        FlowLine(
            id="L1", title="收邮件", group="邮件", kind="route",
            root=LineTask(function=k1, children=[
                LineTask(function=k2, children=[LineTask(function=k1, goto=True)]),
            ]),
            sid="line:app/t.py::handle",
        )
    ]
    return ProjectGraph(
        schema_version=1, root="/proj", functions={k1: fg}, lines=lines,
        common_tasks=[k2],
        selfcheck=SelfCheck(
            unreachable=[(k1, "n9")], dead_ends=[(k1, "n8")],
            unresolved_calls=2, guessed_calls=1,
            skipped_files=[("app/bad.py", "语法错误")],
        ),
        file_hashes={"app/t.py": "abc123"},
        sids={"app/t.py::handle": k1},
    )


def test_key_roundtrip():
    k = fn_key("app/t.py", "f.<locals>.g", 12)
    assert k == "app/t.py::f.<locals>.g@12" and parse_key(k) == ("app/t.py", "f.<locals>.g", 12)


def test_json_roundtrip_preserves_everything():
    g = sample_graph()
    assert from_json(to_json(g)) == g


def test_rejects_other_schema_version():
    with pytest.raises(ValueError, match="2"):
        from_json(to_json(sample_graph()).replace('"schema_version": 1', '"schema_version": 2'))


def test_unresolved_follows_call_targets_in_json():
    data = json.loads(to_json(sample_graph()))
    fn = next(iter(data["functions"].values()))
    keys = list(fn)
    assert keys.index("unresolved") == keys.index("call_targets") + 1
    assert fn["unresolved"] == [{"line": 14, "name": "other.mystery"}, {"line": 14, "name": "派发 未知任务"},
                                {"line": 15, "name": "other.mystery"}]
    assert from_json(to_json(sample_graph())).functions[fn["key"]].unresolved[0] == UnresolvedCall(14, "other.mystery")


def test_optional_function_fields_are_written_only_when_set():
    g = sample_graph()
    fg = next(iter(g.functions.values()))
    data = json.loads(to_json(g))
    fn = data["functions"][fg.key]
    assert not {"rule", "name", "version"} & set(fn)
    fg.rule, fg.name, fg.version = "pystepkit", "Handle", "2"
    fn = json.loads(to_json(g))["functions"][fg.key]
    assert (fn["rule"], fn["name"], fn["version"]) == ("pystepkit", "Handle", "2")
    assert from_json(to_json(g)) == g


def test_branch_is_written_only_when_set():
    g = sample_graph()
    fg = next(iter(g.functions.values()))
    edges = json.loads(to_json(g))["functions"][fg.key]["edges"]
    assert [e.get("branch") for e in edges] == [None, "yes", "no", None]
    assert [list(e) for e in edges] == [["src", "dst", "kind", "label"], ["src", "dst", "kind", "label", "branch"],
                                        ["src", "dst", "kind", "label", "branch"], ["src", "dst", "kind", "label"]]
    assert from_json(to_json(g)).functions[fg.key].edges == fg.edges


def test_old_graph_without_branch_is_readable_and_differs_only_on_branch_edges():
    new = json.loads(to_json(sample_graph()))
    old = sample_graph()
    for fg in old.functions.values():
        for e in fg.edges:
            e.branch = ""
    old_text = to_json(old)
    assert '"branch"' not in old_text  # 没有分支的图里不出现 branch 字段
    # 旧图（没有 branch）可读，branch 为空串
    assert from_json(old_text) == old
    # 新图去掉 branch 字段就是旧图：只在有分支的边上不同
    for fn in new["functions"].values():
        for e in fn["edges"]:
            e.pop("branch", None)
    assert json.dumps(new, ensure_ascii=False, indent=2) == old_text


def test_unknown_keys_are_ignored_and_bad_top_level_is_rejected():
    data = json.loads(to_json(sample_graph()))
    data["future"] = 1
    fn = next(iter(data["functions"].values()))
    fn["future_field"] = [1]
    fn["nodes"][2]["future"] = {}
    fn["nodes"][2]["calls"][0]["future"] = True
    fn["edges"][0]["future"] = "x"
    data["lines"][0]["future"] = 0
    data["selfcheck"]["future"] = 0
    assert from_json(json.dumps(data)) == sample_graph()
    for text in ("[]", "1", '"x"'):
        with pytest.raises(ValueError):
            from_json(text)


def test_sids_are_written_and_read_back():
    g = sample_graph()
    data = json.loads(to_json(g))
    fn = data["functions"][fn_key("app/t.py", "handle", 10)]
    assert fn["sid"] == "app/t.py::handle"
    assert [n["sid"] for n in fn["nodes"]] == ["start", "cond:5d2a0c1e", "step:0b7d3f42", "end:implicit"]
    assert data["lines"][0]["sid"] == "line:app/t.py::handle"
    assert data["sids"] == {"app/t.py::handle": fn["key"]}
    assert from_json(to_json(g)) == g


def test_old_graph_without_sids_is_readable():
    data = json.loads(to_json(sample_graph()))
    del data["sids"]
    for fn in data["functions"].values():
        del fn["sid"]
        for n in fn["nodes"]:
            del n["sid"]
    for ln in data["lines"]:
        del ln["sid"]
    g = from_json(json.dumps(data))
    fg = next(iter(g.functions.values()))
    assert g.sids == {} and fg.sid == "" and {n.sid for n in fg.nodes} == {""} and g.lines[0].sid == ""
    expected = sample_graph()
    expected.sids = {}
    efg = next(iter(expected.functions.values()))
    efg.sid = ""
    for n in efg.nodes:
        n.sid = ""
    expected.lines[0].sid = ""
    assert g == expected
