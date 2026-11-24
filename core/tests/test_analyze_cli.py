import json

import pytest
from conftest import make_project
from pystepmap import analyze as analyze_mod
from pystepmap.analyze import analyze_project
from pystepmap.cli import main
from pystepmap.model import from_json, to_json

SRC = '@router.get("/x")\nasync def x():\n    await y()\nasync def y(): ...\n'


def test_analyze_writes_json_and_summary(tmp_path, capsys):
    root = make_project(tmp_path, {"app/a.py": SRC})
    out = tmp_path / "g.json"
    assert main(["analyze", str(root), "-o", str(out)]) == 0
    g = from_json(out.read_text())
    assert g.schema_version == 1 and len(g.functions) == 2 and [l.title for l in g.lines] == ["GET /x"]
    assert set(g.file_hashes) == {"app/a.py"}
    text = capsys.readouterr().out
    assert "启用的识别规则: fastapi" in text and "函数: 2" in text and "功能线: 1" in text
    for label in ("公共任务: 0", "跳过的文件: 0", "目标不明的调用: ", "推测的调用: ", "走不到的节点: 0", "无出口的节点: 0"):
        assert label in text


def test_summary_separates_blind_spots_from_third_party_calls(tmp_path, capsys):
    """摘要里的"目标不明的调用"要分出可能落在项目代码上的那部分：其余是第三方、标准库和日志，不是盲点。"""
    root = make_project(tmp_path, {"app/a.py": '@router.get("/x")\nasync def x(session, c):\n    session.commit()\n'
                                              "    c.render()\n"
                                              "class C:\n    def render(self): ...\nclass D:\n    def render(self): ...\n"})
    assert main(["analyze", str(root), "-o", str(tmp_path / "o.json")]) == 0
    assert "目标不明的调用: 2（可能落在项目代码上的 1）" in capsys.readouterr().out


def test_stdout_json_summary_stderr(tmp_path, capsys):
    root = make_project(tmp_path, {"app/a.py": SRC})
    assert main(["analyze", str(root)]) == 0
    cap = capsys.readouterr()
    assert from_json(cap.out).schema_version == 1
    assert "函数: 2" in cap.err


def test_no_rules_message(tmp_path, capsys):
    root = make_project(tmp_path, {"a.py": "def f(): ...\n"})
    assert main(["analyze", str(root), "-o", str(tmp_path / "o.json")]) == 0
    assert "启用的识别规则: 无" in capsys.readouterr().out


def test_missing_dir_exits_2(tmp_path, capsys):
    assert main(["analyze", str(tmp_path / "nope")]) == 2
    assert "找不到目录" in capsys.readouterr().err


def test_bad_config_exits_2(tmp_path, capsys):
    root = make_project(tmp_path / "p", {"a.py": "def f(): ...\n"})
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"nope": 1}')
    assert main(["analyze", str(root), "--config", str(cfg)]) == 2
    assert str(cfg) in capsys.readouterr().err
    assert main(["analyze", str(root), "--config", str(tmp_path / "missing.json")]) == 2
    assert "missing.json" in capsys.readouterr().err


def test_no_subcommand_returns_2(capsys):
    assert main([]) == 2


def test_same_source_same_output(tmp_path):
    root = make_project(tmp_path, {"app/a.py": SRC, "app/b.py": "def z():\n    y()\ndef y(): ...\n"})
    assert to_json(analyze_project(root)) == to_json(analyze_project(root))


def test_root_is_absolute(tmp_path):
    make_project(tmp_path, {"a.py": "def f(): ...\n"})
    assert analyze_project(tmp_path).root == str(tmp_path.resolve())


def test_builder_failure_is_recorded_not_fatal(tmp_path, monkeypatch):
    root = make_project(tmp_path, {"app/a.py": SRC})
    real = analyze_mod.build_function_graph

    def flaky(fn, *a, **kw):
        if fn.qualname == "y":
            raise RuntimeError("boom")
        return real(fn, *a, **kw)

    monkeypatch.setattr(analyze_mod, "build_function_graph", flaky)
    g = analyze_project(root)
    assert len(g.functions) == 1
    [(rel, reason)] = g.selfcheck.skipped_files
    assert rel == "app/a.py" and "y" in reason and "RuntimeError" in reason


def test_output_into_missing_directory_exits_2(tmp_path, capsys):
    root = make_project(tmp_path, {"app/a.py": SRC})
    assert main(["analyze", str(root), "-o", str(tmp_path / "no" / "g.json")]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and str(tmp_path / "no") in err[0]


@pytest.mark.parametrize("cfg,needle", [
    ({"exclude": "tests"}, "exclude"),
    ({"noise_receivers": ["logger", 3]}, "noise_receivers"),
    ({"outcome_kinds": {"status": {"x": "boom"}}}, "boom"),
    ({"common_task_threshold": "6"}, "common_task_threshold"),
    ({"extra_rules": [{"entry_decorators": [["(", "task"]]}]}, "("),
    ({"extra_rules": [{"entry_decorators": [["^cron", "job"]]}]}, "job"),
    ({"extra_rules": [{"dispatch": [{"methods": ["fire"], "task_from": "args"}]}]}, "task_from"),
    ({"extra_rules": [{"entry": []}]}, "entry"),
    ({"extra_rules": {"name": "x"}}, "extra_rules"),
])
def test_config_errors_exit_2_with_one_line(tmp_path, capsys, cfg, needle):
    """配置的字段类型、取值、正则写错时，analyze / check 都打印一行中文说明、退出码 2，不抛出异常栈。"""
    root = make_project(tmp_path / "p", {"a.py": "def f(): ...\n"})
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    for cmd in (["analyze", str(root), "--config", str(path)], ["check", str(root), "--config", str(path)]):
        assert main(cmd) == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1 and needle in err[0], err


def test_summary_lists_skipped_files_excluded_dirs_and_missing_entries(tmp_path, capsys):
    root = make_project(tmp_path, {"a.py": "def f(): ...\n", "bad.py": "def (:\n", "tests/t.py": "def t(): ...\n",
                                   "pkg/tests/u.py": "def u(): ...\n", ".venv/x.py": "def v(): ...\n"})
    assert main(["analyze", str(root), "-o", str(tmp_path / "g.json")]) == 0
    out = capsys.readouterr().out
    assert "跳过的文件: 1" in out and "  bad.py: 无法解析" in out
    assert "排除的目录: .venv, tests ×2" in out
    # 一个入口都没认出来：没有人调用的顶层函数当入口（兜底），摘要里说明
    assert "功能线: 1" in out and "入口是推断的：" in out and "# pystepmap: 入口" in out
    root2 = make_project(tmp_path / "r2", {"app/a.py": SRC})
    assert main(["analyze", str(root2), "-o", str(tmp_path / "g2.json")]) == 0
    out = capsys.readouterr().out
    assert "入口是推断的" not in out and "没有功能线" not in out and "排除的目录: 无" in out


CLASS_FLOW = """
    from pystepkit import workflow, node, condition, outcome
    def helper(ctx): ...
    @workflow(name="TransferWorkflow", title="账户转账", group="转账", version="1.0")
    class TransferWorkflow:
        async def run(self, ctx):
            await self.validate(ctx)
            if self.has_balance(ctx):
                await self.execute(ctx)
            helper(ctx)
            return outcome("ok", title="转账完成")

        @node(title="参数校验")
        async def validate(self, ctx): ...

        @condition(title="余额充足？")
        def has_balance(self, ctx):
            return ctx.ok

        @node(title="执行转账")
        async def execute(self, ctx):
            await self.validate(ctx)
"""


def test_class_form_pystepkit_workflow_end_to_end(tmp_path, capsys):
    """@workflow(name=…, title=…, version=…) class 加 @node / @condition 方法：入口、标题、name、version 进图；
    约束检查也检查它。"""
    root = make_project(tmp_path, {"app/transfer.py": CLASS_FLOW})
    out = tmp_path / "g.json"
    assert main(["analyze", str(root), "-o", str(out)]) == 0
    g = from_json(out.read_text())
    run = next(f for f in g.functions.values() if f.qualname == "TransferWorkflow.run")
    assert (run.kind, run.title, run.group, run.marked, run.name, run.version, run.rule) == (
        "workflow", "账户转账", "转账", True, "TransferWorkflow", "1.0", "pystepkit")
    assert [(n.type, n.title) for n in run.nodes if n.type in ("step", "cond", "end") and n.line] == [
        ("step", "参数校验"), ("cond", "余额充足？"), ("step", "执行转账"), ("step", "helper"),
        ("end", "转账完成")]
    assert [(l.title, l.group, l.kind) for l in g.lines] == [("账户转账", "转账", "workflow")]
    data = json.loads(out.read_text())
    validate = next(f for f in data["functions"].values() if f["qualname"] == "TransferWorkflow.validate")
    assert "name" not in validate and "version" not in validate and validate["rule"] == "pystepkit"
    capsys.readouterr()
    assert main(["check", str(root)]) == 1
    assert [line.split(": ", 1)[1] for line in capsys.readouterr().out.splitlines()[:-1]] == [
        "SM001 编排函数只能调用步骤：helper(…) 不是步骤", "SM003 步骤 execute 里不能调用另一个步骤 validate"]
