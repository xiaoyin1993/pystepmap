"""约束钩子：编辑后检查（kit/hooks/check_edited_file.py）和提交前检查（kit/hooks/pre-commit）。

两个脚本都在子进程里运行。检查器命令用 PYSTEPMAP_HOOK_CMD 指成"当前解释器 -m pystepmap.cli"，测试结果因此不取决于
uv 选哪个解释器；脚本默认的 uv run pystepmap 由手工验收覆盖。

钩子的退出码：2 = 退回给 AI（有违规、解析不了）；1 = 检查没做成（检查器跑不起来、出错、超时、钩子自身出错），不阻断，
Claude Code 把标准错误的第一行显示给用户；0 = 没问题或与检查无关（安静）。
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "kit" / "hooks" / "check_edited_file.py"
PRE_COMMIT = REPO / "kit" / "hooks" / "pre-commit"
EXAMPLE = REPO / "examples" / "transfer"
EXAMPLE_REL = "examples/transfer/transfer_workflow.py"
CHECKER = shlex.join([sys.executable, "-m", "pystepmap.cli"])
SYSTEM_PYTHON = Path("/usr/bin/python3")  # macOS 自带（3.9）：Claude Code 的 PATH 上可能只有它

MARKED_BAD = '''
import logging
from pystepkit import workflow, step, outcome

logger = logging.getLogger(__name__)


@step(title="取订单")
async def load_order(order_id):
    return {"id": order_id}


@workflow(title="处理订单")
async def handle_order(order_id):
    order = await load_order(order_id)
    logger.info("loaded %s", order_id)
    return outcome("ok", order=order)
'''
MARKED_GOOD = MARKED_BAD.replace('    logger.info("loaded %s", order_id)\n', "")
PLAIN = '''
import logging

logger = logging.getLogger(__name__)


def tidy(text):
    logger.info("tidy")
    return text.strip()
'''


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYSTEPMAP_", "GIT_"))}
    env["PYSTEPMAP_HOOK_CMD"] = CHECKER
    env.update(extra)
    return env


# ---------- 编辑后检查 ----------

def run_hook(payload=None, *, raw: str | None = None, python: str = sys.executable, **env: str):
    text = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    r = subprocess.run([python, str(HOOK)], input=text.encode("utf-8"), capture_output=True,
                       env=_clean_env(**env), timeout=120, check=False)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def edit(path, cwd=None) -> dict:
    """Claude Code 在 Edit 之后传给 PostToolUse 钩子的 JSON。"""
    return {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(cwd or REPO),
            "permission_mode": "default", "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b", "replace_all": False},
            "tool_response": {"filePath": str(path), "success": True}, "tool_use_id": "toolu_x"}


def copy_example(root: Path, folder: str = "examples/transfer") -> Path:
    """把示例复制到一个假仓库 root 下，返回副本里的 transfer_workflow.py。"""
    (root / "pyproject.toml").parent.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "fake"\nversion = "0"\n', encoding="utf-8")
    shutil.copytree(EXAMPLE, root / folder)
    return root / folder / "transfer_workflow.py"


def add_log_to_run(path: Path) -> int:
    """在编排方法 run 的第一条语句前插一行 logger.info("x")，返回它的行号。"""
    src = path.read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "TransferWorkflow")
    run = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    body = run.body[1:] if ast.get_docstring(run) else run.body
    first = body[0]
    lines = src.splitlines(keepends=True)
    lines.insert(first.lineno - 1, " " * first.col_offset + 'logger.info("x")\n')
    path.write_text("".join(lines), encoding="utf-8")
    return first.lineno


def test_clean_example_passes_quietly():
    """真实仓库里的示例：项目根按脚本位置找到（向上第一个含 .git 的目录），零违规，什么都不说。"""
    assert run_hook(edit(EXAMPLE / "transfer_workflow.py")) == (0, "", "")


def test_logging_in_orchestration_is_returned_to_the_ai(tmp_path):
    f = copy_example(tmp_path)
    line = add_log_to_run(f)
    code, out, err = run_hook(edit(f, tmp_path), PYSTEPMAP_HOOK_ROOT=str(tmp_path))
    assert code == 2 and out == ""
    assert err.splitlines()[0].startswith("这些是流程约束检查的违规，请按 flow-style 修改后再继续")
    assert f"{EXAMPLE_REL}:{line}: SM001 编排函数只能调用步骤：logger.info(…) 不是步骤" in err.splitlines()


def test_syntax_error_in_edited_file_is_returned(tmp_path):
    f = copy_example(tmp_path)
    f.write_text(f.read_text(encoding="utf-8") + "\ndef broken(:\n", encoding="utf-8")
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path))
    assert code == 2
    assert "transfer_workflow.py" in err and "解析" in err


def test_only_the_edited_file_is_judged(tmp_path):
    """被编辑的是没有标记的文件（或干净的标记文件），项目里另一个文件有违规、另一个文件解析不了：都不拦。"""
    add_log_to_run(copy_example(tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    for name, src in (("plain.py", PLAIN), ("good.py", MARKED_GOOD)):
        (tmp_path / "app" / name).write_text(src, encoding="utf-8")
        assert run_hook(edit(tmp_path / "app" / name), PYSTEPMAP_HOOK_ROOT=str(tmp_path)) == (0, "", "")


def test_links_pointing_outside_do_not_block_edits(tmp_path):
    """项目里有指向项目之外的 .py 链接：编辑别的文件照常检查、没问题就安静放行；编辑这个链接（Claude Code 给的是
    链接的路径）时，解析后在项目之外，不检查。"""
    f = copy_example(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def broken(:\n", encoding="utf-8")
    link = tmp_path / "app" / "outside_link.py"
    link.parent.mkdir()
    link.symlink_to(outside)
    assert run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path)) == (0, "", "")
    assert run_hook(edit(link), PYSTEPMAP_HOOK_ROOT=str(tmp_path)) == (0, "", "")
    add_log_to_run(f)
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path))
    assert code == 2 and "SM001" in err


def test_relative_path_is_resolved_against_cwd(tmp_path):
    f = copy_example(tmp_path)
    assert run_hook(edit(EXAMPLE_REL, cwd=tmp_path), PYSTEPMAP_HOOK_ROOT=str(tmp_path)) == (0, "", "")
    add_log_to_run(f)
    code, _, err = run_hook(edit(EXAMPLE_REL, cwd=tmp_path), PYSTEPMAP_HOOK_ROOT=str(tmp_path))
    assert code == 2 and "SM001" in err


def test_path_with_spaces_and_chinese(tmp_path):
    root = tmp_path / "我的 仓库"
    f = copy_example(root, "examples/转账 示例")
    assert run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(root)) == (0, "", "")
    line = add_log_to_run(f)
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(root))
    assert code == 2 and f"examples/转账 示例/transfer_workflow.py:{line}: SM001" in err


@pytest.mark.parametrize("case", ["not_py", "outside", "missing", "missing_relative"])
def test_irrelevant_files_pass_quietly(tmp_path, case):
    root = tmp_path / "repo"
    add_log_to_run(copy_example(root))
    outside = tmp_path / "elsewhere" / "flow.py"
    outside.parent.mkdir()
    outside.write_text(MARKED_BAD, encoding="utf-8")  # 有违规，但不在项目里
    (root / "notes.md").write_text("# x\n", encoding="utf-8")
    payload = {"not_py": edit(root / "notes.md"), "outside": edit(outside),
               "missing": edit(root / "gone.py"), "missing_relative": edit("gone/x.py", cwd=root)}[case]
    assert run_hook(payload, PYSTEPMAP_HOOK_ROOT=str(root)) == (0, "", "")


@pytest.mark.parametrize("raw", [
    "", "not json", "[1, 2]", '"text"', "{}", '{"tool_name": "Edit"}', '{"tool_input": null}',
    '{"tool_input": {"file_path": 123}}', '{"tool_input": {"file_path": ""}}',
    '{"tool_input": {"notebook_path": "/x/a.ipynb"}}', '{"tool_input": {"file_path": "a\\u0000b.py"}}',
    '{"cwd": 5, "tool_input": {"file_path": "rel.py"}}',
])
def test_bad_input_passes_quietly(raw):
    assert run_hook(raw=raw) == (0, "", "")


def test_checker_not_available_exits_1_with_one_line(tmp_path):
    """检查器跑不起来：以 1 退出（不阻断；Claude Code 把这一行显示给用户），不假装检查过了。"""
    f = copy_example(tmp_path)
    add_log_to_run(f)
    code, out, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path),
                              PYSTEPMAP_HOOK_CMD=str(tmp_path / "no-such-pystepmap"), PATH="")
    assert code == 1 and out == ""
    assert len(err.splitlines()) == 1 and "no-such-pystepmap" in err


def test_checker_error_is_not_a_violation(tmp_path):
    """检查器自己失败（这里：退出码 3、没有违规行）：一行说明，以 1 退出。"""
    f = copy_example(tmp_path)
    fake = shlex.join([sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"])
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path), PYSTEPMAP_HOOK_CMD=fake)
    assert code == 1 and len(err.splitlines()) == 1 and "退出码 3" in err and "boom" in err


def test_usage_error_exit_2_is_not_a_violation(tmp_path):
    """退出码 2 但不是"解析不了"（参数、配置写错）：检查器出错，以 1 退出。"""
    f = copy_example(tmp_path)
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path),
                            PYSTEPMAP_HOOK_CMD=CHECKER + " --no-such-option")
    assert code == 1 and len(err.splitlines()) == 1


def test_timeout_counts_as_checker_error(tmp_path):
    """超时（这里 1 秒）：连同检查器派生的子进程一起结束，按检查器出错处理。"""
    f = copy_example(tmp_path)
    slow = shlex.join(["sh", "-c", "sleep 30; :", "sh"])
    start = time.monotonic()
    code, _, err = run_hook(edit(f), PYSTEPMAP_HOOK_ROOT=str(tmp_path), PYSTEPMAP_HOOK_CMD=slow,
                            PYSTEPMAP_HOOK_TIMEOUT="1")
    assert time.monotonic() - start < 15
    assert code == 1 and len(err.splitlines()) == 1 and "超时" in err


def test_unexpected_error_in_the_hook_exits_1(tmp_path):
    """钩子自身的意外错误（这里：进程的当前目录已被删除，相对路径没法补全）：一行说明，以 1 退出，没有 traceback。"""
    gone = tmp_path / "gone"
    gone.mkdir()
    r = subprocess.run(["sh", "-c", 'cd "$1" && rmdir "$1" && exec "$2" "$3"', "sh", str(gone), sys.executable,
                        str(HOOK)], input=b'{"tool_input": {"file_path": "x.py"}}', capture_output=True,
                       env=_clean_env(), timeout=120, check=False)
    err = r.stderr.decode("utf-8")
    assert r.returncode == 1 and len(err.splitlines()) == 1 and "Traceback" not in err


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "pystepmap").exists(), reason="仓库里没有 .venv")
def test_without_uv_on_path_falls_back_to_repo_venv(tmp_path):
    """Claude Code 的 PATH 上没有 uv 时，用仓库 .venv 里的 pystepmap，检查照样做。"""
    f = copy_example(tmp_path)
    add_log_to_run(f)
    env = _clean_env(PYSTEPMAP_HOOK_ROOT=str(tmp_path), PATH="")
    del env["PYSTEPMAP_HOOK_CMD"]
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(edit(f)).encode(), capture_output=True,
                       env=env, timeout=120, check=False)
    assert r.returncode == 2 and "SM001" in r.stderr.decode("utf-8")


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="没有 /usr/bin/python3")
def test_runs_under_system_python3(tmp_path):
    """settings.json 用 PATH 上的 python3 运行钩子，可能是 macOS 自带的旧版本：脚本只用它也有的写法。"""
    f = copy_example(tmp_path)
    add_log_to_run(f)
    code, _, err = run_hook(edit(f), python=str(SYSTEM_PYTHON), PYSTEPMAP_HOOK_ROOT=str(tmp_path))
    assert code == 2 and "SM001" in err
    assert run_hook(raw="not json", python=str(SYSTEM_PYTHON))[0] == 0


def test_settings_register_the_hook():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings) == {"hooks"}  # 不放任何权限放行项
    (entry,) = settings["hooks"]["PostToolUse"]
    assert {"Edit", "Write"} <= set(entry["matcher"].split("|"))
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == 'python3 "$CLAUDE_PROJECT_DIR/kit/hooks/check_edited_file.py"'


def test_settings_command_runs_as_claude_code_would(tmp_path):
    """按 Claude Code 的方式执行 settings.json 里的命令（sh -c，带 CLAUDE_PROJECT_DIR）：违规照样退回。"""
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    f = copy_example(tmp_path)
    add_log_to_run(f)
    r = subprocess.run(["sh", "-c", command], input=json.dumps(edit(f)).encode(), capture_output=True,
                       env=_clean_env(CLAUDE_PROJECT_DIR=str(REPO), PYSTEPMAP_HOOK_ROOT=str(tmp_path)),
                       timeout=120, check=False)
    assert r.returncode == 2 and "SM001" in r.stderr.decode("utf-8")


# ---------- 提交前检查 ----------

@pytest.fixture
def repo(tmp_path):
    """临时 git 仓库，core.hooksPath 指向 pre-commit 的副本所在目录；不读用户的全局 git 配置。"""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(PRE_COMMIT, hooks / "pre-commit")
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "tmp").mkdir()  # 提交时的 TMPDIR：导出暂存区用，每次提交后检查它被清理干净
    git(root, "init", "-q")
    git(root, "config", "core.hooksPath", str(hooks))
    return root


def git(root: Path, *args: str, check: bool = True, **env: str) -> subprocess.CompletedProcess:
    env = _clean_env(**{"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", **env})
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, env=env, timeout=120, check=False)
    if check:
        assert r.returncode == 0, r.stdout.decode() + r.stderr.decode()
    return r


def write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")


def commit(root: Path, *paths: str, **env: str) -> tuple[int, str]:
    """暂存 paths（给出时）后提交，返回 (退出码, 输出)。断言导出暂存区用的临时目录已被清理，输出里没有临时路径。"""
    if paths:
        git(root, "add", "--", *paths)
    tmp = root.parent / "tmp"
    r = git(root, "commit", "-q", "-m", "x", check=False, **{"TMPDIR": str(tmp), **env})
    out = r.stdout.decode("utf-8") + r.stderr.decode("utf-8")
    assert list(tmp.iterdir()) == []
    assert str(tmp) not in out and os.path.realpath(tmp) not in out
    return r.returncode, out


def commits(root: Path) -> int:
    r = git(root, "rev-list", "--count", "HEAD", check=False)
    return int(r.stdout) if r.returncode == 0 else 0


def test_pre_commit_is_executable():
    assert PRE_COMMIT.stat().st_mode & stat.S_IXUSR
    assert PRE_COMMIT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")


def test_pre_commit_passes_without_python_files(repo):
    """暂存区里没有 .py 改动：不导出、不运行检查器（检查器命令不存在也照样通过）。"""
    write(repo, "README.md", "# x\n")
    code, _ = commit(repo, "README.md", PYSTEPMAP_HOOK_CMD=str(repo / "no-such-pystepmap"))
    assert code == 0 and commits(repo) == 1


def test_pre_commit_rejects_a_violation(repo):
    write(repo, "app/flow.py", MARKED_BAD)
    code, out = commit(repo, "app/flow.py")
    assert code != 0 and commits(repo) == 0
    assert "app/flow.py:16: SM001" in out


def test_pre_commit_passes_unmarked_and_clean_files(repo):
    write(repo, "app/plain.py", PLAIN)
    write(repo, "app/flow.py", MARKED_GOOD)
    assert commit(repo, "app")[0] == 0 and commits(repo) == 1


def test_pre_commit_ignores_unstaged_violations_elsewhere(repo):
    """工作区里有违规、但没有暂存的文件（未跟踪的新文件、已跟踪但改动没暂存的文件）不影响这次提交。"""
    write(repo, "app/other.py", MARKED_GOOD)
    assert commit(repo, "app/other.py")[0] == 0
    write(repo, "app/other.py", MARKED_BAD)  # 已跟踪，改动没暂存
    write(repo, "app/draft.py", MARKED_BAD)  # 未跟踪
    write(repo, "app/flow.py", MARKED_GOOD)
    assert commit(repo, "app/flow.py")[0] == 0 and commits(repo) == 2


def test_pre_commit_judges_staged_content_not_the_working_tree(repo):
    """暂存的是违规版本、工作区已改好但没重新 git add（A）：拒绝；暂存的是干净版本、违规只在工作区（C）：放行。"""
    write(repo, "app/flow.py", MARKED_BAD)
    git(repo, "add", "app/flow.py")
    write(repo, "app/flow.py", MARKED_GOOD)
    code, out = commit(repo)
    assert code != 0 and "app/flow.py:16: SM001" in out and commits(repo) == 0
    git(repo, "add", "app/flow.py")
    write(repo, "app/flow.py", MARKED_BAD)
    assert commit(repo)[0] == 0 and commits(repo) == 1


def test_pre_commit_staged_violation_deleted_from_working_tree(repo):
    """暂存了违规后把文件从工作区删掉（D）：暂存区里还有它，照样拒绝。"""
    write(repo, "app/flow.py", MARKED_BAD)
    git(repo, "add", "app/flow.py")
    (repo / "app" / "flow.py").unlink()
    code, out = commit(repo)
    assert code != 0 and "app/flow.py:16: SM001" in out and commits(repo) == 0


STEPS = """
from pystepkit import step


@step(title="取订单")
async def load_order(order_id):
    return {"id": order_id}
"""
FLOW = """
from pystepkit import workflow, outcome

from app.steps import load_order


@workflow(title="处理订单")
async def handle_order(order_id):
    order = await load_order(order_id)
    return outcome("ok", order=order)
"""


def test_pre_commit_judges_the_whole_staged_tree(repo):
    """只暂存了 steps.py（去掉了 @step），违规出现在没改动的 flow.py 里：照样拒绝，信息指向 flow.py；
    删掉 steps.py 同理。"""
    write(repo, "app/steps.py", STEPS)
    write(repo, "app/flow.py", FLOW)
    assert commit(repo, "app")[0] == 0
    write(repo, "app/steps.py", STEPS.replace('@step(title="取订单")\n', ""))
    code, out = commit(repo, "app/steps.py")
    assert code != 0 and "app/flow.py:9: SM001 编排函数只能调用步骤：load_order(…) 不是步骤" in out
    git(repo, "checkout", "HEAD", "--", "app/steps.py")
    git(repo, "rm", "-q", "app/steps.py")
    code, out = commit(repo)
    assert code != 0 and "app/flow.py:9: SM001" in out and commits(repo) == 1


def test_pre_commit_file_names_with_spaces_and_chinese(repo):
    write(repo, "app/my flow.py", MARKED_GOOD)
    write(repo, "app/订单 流程.py", MARKED_GOOD)
    assert commit(repo, "app")[0] == 0
    write(repo, "app/订单 流程.py", MARKED_BAD)
    code, out = commit(repo, "app/订单 流程.py")
    assert code != 0 and "app/订单 流程.py:16: SM001" in out and commits(repo) == 1


def test_pre_commit_deleted_and_renamed_files(repo):
    write(repo, "app/flow.py", MARKED_GOOD)
    write(repo, "app/old.py", PLAIN)
    assert commit(repo, "app")[0] == 0
    git(repo, "rm", "-q", "app/old.py")  # 只删除：暂存区里剩下的都没问题
    assert commit(repo)[0] == 0 and commits(repo) == 2
    git(repo, "mv", "app/flow.py", "app/renamed.py")  # 改名后又改出了违规：改名的文件也要检查
    write(repo, "app/renamed.py", MARKED_BAD)
    code, out = commit(repo, "app/renamed.py")
    assert code != 0 and "app/renamed.py:16: SM001" in out


def test_pre_commit_output_uses_repository_paths(repo):
    """检查器的输出里带着导出目录的路径时，换成相对仓库根的路径（这里用一个回显项目目录的假检查器）。"""
    fake = shlex.join([sys.executable, "-c",
                       "import sys; print(sys.argv[2] + '/app/flow.py:3: SM001 x'); print(sys.argv[2]); sys.exit(1)"])
    write(repo, "app/flow.py", MARKED_GOOD)
    code, out = commit(repo, "app/flow.py", PYSTEPMAP_HOOK_CMD=fake)
    assert code != 0 and "\napp/flow.py:3: SM001 x\n（暂存区）" in out


def test_pre_commit_rejects_unparsable_file(repo):
    write(repo, "app/broken.py", "def broken(:\n")
    code, out = commit(repo, "app/broken.py")
    assert code != 0 and "app/broken.py" in out and "解析" in out


def test_pre_commit_passes_staged_links_that_point_outside(repo):
    """暂存了指向仓库之外的 .py 符号链接，和指向仓库里文件的绝对路径链接（导出到临时目录后它指向导出目录之外）：
    检查器只把它们列成警告，不拦提交；同时暂存的违规照样拦住。"""
    outside = repo.parent / "outside.py"
    outside.write_text("def leaked():\n    return 1\n", encoding="utf-8")
    write(repo, "app/flow.py", MARKED_GOOD)
    (repo / "app" / "outside_link.py").symlink_to(outside)
    (repo / "app" / "abs_alias.py").symlink_to(repo / "app" / "flow.py")
    code, out = commit(repo, "app")
    assert code == 0 and commits(repo) == 1, out
    assert git(repo, "ls-files", "-s", "app/outside_link.py").stdout.startswith(b"120000")  # 进库的是链接本身
    write(repo, "app/flow.py", MARKED_BAD)
    code, out = commit(repo, "app/flow.py")
    assert code != 0 and "app/flow.py:16: SM001" in out and commits(repo) == 1


def test_pre_commit_refuses_when_checker_cannot_run(repo):
    """检查器没法运行时不放行（提交前检查是"结果确定"的那一层），并说明是哪个命令。"""
    write(repo, "app/flow.py", MARKED_GOOD)
    code, out = commit(repo, "app/flow.py", PYSTEPMAP_HOOK_CMD=str(repo / "no-such-pystepmap"))
    assert code != 0 and "no-such-pystepmap" in out and commits(repo) == 0


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="没有 /usr/bin/python3")
def test_pre_commit_runs_under_system_python3(repo, tmp_path):
    """git 用 PATH 上的 python3 运行 pre-commit（#!/usr/bin/env python3）：换成 macOS 自带的旧版本也能用。"""
    bin_dir = tmp_path / "oldpy"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(SYSTEM_PYTHON)
    path = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    write(repo, "app/flow.py", MARKED_BAD)
    code, out = commit(repo, "app/flow.py", PATH=path)
    assert code != 0 and "app/flow.py:16: SM001" in out


@pytest.mark.parametrize("script", [HOOK, PRE_COMMIT], ids=["hook", "pre-commit"])
def test_scripts_use_only_the_standard_library(script):
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}
