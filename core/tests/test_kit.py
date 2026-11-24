"""pystepmap kit install / uninstall：往别的项目里装约束套件（只在临时目录里试）。"""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pystepmap import kit
from pystepmap.cli import main

BAD_FLOW = '''
import logging
from pystepkit import step, workflow

logger = logging.getLogger(__name__)


@step(title="做一件事")
def do():
    return 1


@workflow(title="示例流程")
def run():
    logger.info("开始")
    return do()
'''


MCP_LINE = f"修改  {kit.MCP_FILE}"


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git/" not in str(p.relative_to(root)) + "/"}


def install(root: Path, **kw) -> kit.Plan:
    plan = kit.plan_install(root, **kw)
    kit.apply_install(plan)
    return plan


def test_install_writes_skill_hook_settings_mcp_and_manifest(tmp_path):
    plan = install(tmp_path)
    cmd = kit.default_command()
    skill = (tmp_path / kit.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    dbos = (tmp_path / kit.SKILL_DIR / "dbos.md").read_text(encoding="utf-8")
    req = (tmp_path / kit.SKILL_DIR / "requirements.md").read_text(encoding="utf-8")
    routes = (tmp_path / kit.SKILL_DIR / "routes.md").read_text(encoding="utf-8")
    assert "raise HTTPException" in routes and not [w for w in kit.FORBIDDEN if w in routes] and kit.CMD not in routes
    assert f"`{kit.SKILL_DIR}/{kit.OPENSPEC_NAME}`" in req and "get_requirement" in req
    for text in (skill, dbos, req):
        assert not [w for w in kit.FORBIDDEN if w in text] and kit.CMD not in text
    assert "在本项目里" in skill and f"`{kit.HOOK_FILE}`" in skill and f"{cmd[0]}" in skill
    assert f"{kit.SKILL_DIR}/{kit.EXAMPLE_NAME}" in skill and (tmp_path / kit.SKILL_DIR / kit.EXAMPLE_NAME).is_file()
    settings = json.loads((tmp_path / kit.SETTINGS).read_text(encoding="utf-8"))
    [group] = settings["hooks"]["PostToolUse"]
    assert group["matcher"] == "Edit|Write|MultiEdit" and group["hooks"] == [{"type": "command", "command": plan.hook_command}]
    assert 'PYSTEPMAP_HOOK_ROOT="$CLAUDE_PROJECT_DIR"' in plan.hook_command and kit.HOOK_FILE in plan.hook_command
    mcp = json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8"))
    assert mcp == {"mcpServers": {"pystepmap": {"command": cmd[0], "args": [*cmd[1:], "mcp"]}}}
    manifest = json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))
    assert sorted(manifest["files"]) == sorted([f"{kit.SKILL_DIR}/SKILL.md", f"{kit.SKILL_DIR}/dbos.md", f"{kit.SKILL_DIR}/requirements.md",
                                                f"{kit.SKILL_DIR}/routes.md",
                                                f"{kit.SKILL_DIR}/{kit.EXAMPLE_NAME}",
                                                f"{kit.SKILL_DIR}/{kit.OPENSPEC_NAME}", kit.HOOK_FILE])
    assert manifest["mcp_previous"] is None and manifest["mcp_file_created"] is True
    # 再装一次：什么都不用写
    again = kit.plan_install(tmp_path)
    assert again.files == {} and again.settings is None and again.mcp is None and len(again.unchanged) == 7
    before = tree(tmp_path)
    kit.apply_install(again)
    assert tree(tmp_path) == before


def test_installed_hook_returns_a_violation_in_the_target_project(tmp_path):
    """装好的钩子在目标项目里真的能用：编辑的文件有违规时退出码 2，说明里有文件、行和规则编号。"""
    plan = install(tmp_path)
    (tmp_path / "flow.py").write_text(BAD_FLOW, encoding="utf-8")
    payload = json.dumps({"tool_input": {"file_path": str(tmp_path / "flow.py")}})
    r = subprocess.run(["/bin/sh", "-c", plan.hook_command], input=payload, capture_output=True, text=True, check=False,
                       cwd=tmp_path, env={"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin",
                                          "HOME": str(tmp_path)})
    assert r.returncode == 2 and "flow.py:15: SM001" in r.stderr, r.stderr
    (tmp_path / "flow.py").write_text(BAD_FLOW.replace('    logger.info("开始")\n', ""), encoding="utf-8")
    r = subprocess.run(["/bin/sh", "-c", plan.hook_command], input=payload, capture_output=True, text=True, check=False,
                       cwd=tmp_path, env={"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin",
                                          "HOME": str(tmp_path)})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_existing_settings_and_mcp_are_kept_and_uninstall_restores_them(tmp_path):
    settings = {"permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}],
                          "Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}
    mcp = {"mcpServers": {"other": {"command": "x"}, "pystepmap": {"command": "old", "args": ["mcp"]}}, "extra": 1}
    (tmp_path / ".claude").mkdir()
    (tmp_path / kit.SETTINGS).write_text(json.dumps(settings), encoding="utf-8")
    (tmp_path / kit.MCP_FILE).write_text(json.dumps(mcp), encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    plan = install(tmp_path)
    assert plan.mcp_previous == {"command": "old", "args": ["mcp"]} and "替换原来的同名服务" in "\n".join(plan.lines())
    now = json.loads((tmp_path / kit.SETTINGS).read_text(encoding="utf-8"))
    assert now["permissions"] == settings["permissions"] and now["hooks"]["Stop"] == settings["hooks"]["Stop"]
    assert now["hooks"]["PostToolUse"][0] == settings["hooks"]["PostToolUse"][0] and len(now["hooks"]["PostToolUse"]) == 2
    assert json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8"))["mcpServers"]["other"] == {"command": "x"}
    out = kit.uninstall(tmp_path)
    assert json.loads((tmp_path / kit.SETTINGS).read_text(encoding="utf-8")) == settings
    assert json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8")) == mcp
    assert sorted(tree(tmp_path)) == [".claude/settings.json", ".mcp.json", "keep.txt"]
    assert any("换回了原来的那个" in line for line in out)


def test_uninstall_from_a_fresh_project_leaves_nothing_but_keeps_files_changed_after_install(tmp_path):
    install(tmp_path)
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {} and not (tmp_path / ".claude").exists()
    install(tmp_path)
    skill = tmp_path / kit.SKILL_DIR / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n我自己加的一行\n", encoding="utf-8")
    out = kit.uninstall(tmp_path)
    assert f"保留  {kit.SKILL_DIR}/SKILL.md（装了之后改过）" in out and skill.is_file()
    assert not (tmp_path / kit.HOOK_FILE).exists() and not (tmp_path / kit.MANIFEST).exists()
    with pytest.raises(kit.KitError, match="没有安装清单"):
        kit.uninstall(tmp_path)


@pytest.mark.parametrize("prepare,match", [
    (lambda r: (r / kit.SKILL_DIR).mkdir(parents=True) or (r / kit.SKILL_DIR / "SKILL.md").write_text("我的 skill"), "内容和要装的不一样"),
    (lambda r: (r / ".claude").mkdir() or (r / kit.SETTINGS).write_text("{不是 json"), "不是合法的 JSON"),
    (lambda r: (r / kit.MCP_FILE).write_text("[1]"), "顶层不是对象"),
    (lambda r: (r / ".claude").mkdir() or (r / kit.SETTINGS).write_text('{"hooks": {"PostToolUse": {}}}'), "不是列表"),
])
def test_install_refuses_and_writes_nothing(tmp_path, prepare, match):
    prepare(tmp_path)
    before = tree(tmp_path)
    with pytest.raises(kit.KitError, match=match):
        kit.plan_install(tmp_path)
    assert tree(tmp_path) == before
    with pytest.raises(kit.KitError, match="不是目录"):
        kit.plan_install(tmp_path / "nope")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=root,
                          capture_output=True, text=True, check=False)


def test_pre_commit_refuses_a_commit_with_a_violation_and_is_removed_by_uninstall(tmp_path):
    assert git(tmp_path, "init", "-q").returncode == 0
    plan = install(tmp_path, pre_commit=True)
    assert plan.git_pre_commit is not None and (tmp_path / kit.GIT_PRE_COMMIT).stat().st_mode & 0o111
    (tmp_path / "flow.py").write_text(BAD_FLOW, encoding="utf-8")
    git(tmp_path, "add", "flow.py")
    r = git(tmp_path, "commit", "-q", "-m", "x")
    assert r.returncode != 0 and "SM001" in r.stderr, r.stderr
    (tmp_path / "flow.py").write_text(BAD_FLOW.replace('    logger.info("开始")\n', ""), encoding="utf-8")
    git(tmp_path, "add", "flow.py")
    assert git(tmp_path, "commit", "-q", "-m", "x").returncode == 0
    kit.uninstall(tmp_path)
    assert not (tmp_path / kit.GIT_PRE_COMMIT).exists() and not (tmp_path / kit.PRE_COMMIT_FILE).exists()


def test_pre_commit_is_not_installed_over_an_existing_hook_or_outside_git(tmp_path):
    plan = kit.plan_install(tmp_path, pre_commit=True)
    assert plan.git_pre_commit is None and any("不是 git 仓库" in n for n in plan.notes)
    git(tmp_path, "init", "-q")
    hook = tmp_path / kit.GIT_PRE_COMMIT
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    plan = install(tmp_path, pre_commit=True)
    assert plan.git_pre_commit is None and any("已经有了" in n for n in plan.notes)
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n" and (tmp_path / kit.PRE_COMMIT_FILE).is_file()
    kit.uninstall(tmp_path)
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_cli_asks_first_and_does_nothing_without_a_terminal_or_with_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", open("/dev/null"))  # noqa: SIM115 —— 不是终端
    assert main(["kit", "install", str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert "要写的文件" in out and "加上 --yes" in err and tree(tmp_path) == {}
    assert main(["kit", "install", str(tmp_path), "--dry-run"]) == 0 and tree(tmp_path) == {}
    assert main(["kit", "install", str(tmp_path), "--yes", "--command", "uvx pystepmap"]) == 0
    mcp = json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["pystepmap"] == {"command": "uvx", "args": ["pystepmap", "mcp"]}
    assert "uvx pystepmap check" in (tmp_path / kit.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert main(["kit", "uninstall", str(tmp_path)]) == 1 and (tmp_path / kit.MANIFEST).exists()
    assert main(["kit", "uninstall", str(tmp_path), "--yes"]) == 0 and tree(tmp_path) == {}
    assert main(["kit", "uninstall", str(tmp_path), "--yes"]) == 2


def test_no_mcp_leaves_mcp_json_alone_on_install_and_uninstall(tmp_path, capsys):
    """--no-mcp（pystepmap 的 MCP 已经在用户级配过）：不建、不改 .mcp.json；项目里已有的同名服务是用户自己的，撤销时
    也不动。别的照装。"""
    plan = install(tmp_path, mcp=False)
    assert plan.mcp is None and not (tmp_path / kit.MCP_FILE).exists() and MCP_LINE not in "\n".join(plan.lines())
    assert any("--no-mcp" in n for n in plan.notes) and (tmp_path / kit.SKILL_DIR / "SKILL.md").is_file()
    assert json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))["mcp"] is False
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {}
    # 项目里已有用户自己配的同名服务：装和撤销都原样留着
    own = {"mcpServers": {"pystepmap": {"command": "mine", "args": ["mcp"]}}}
    (tmp_path / kit.MCP_FILE).write_text(json.dumps(own), encoding="utf-8")
    assert main(["kit", "install", str(tmp_path), "--yes", "--no-mcp"]) == 0
    assert MCP_LINE not in capsys.readouterr().out
    assert json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8")) == own
    out = kit.uninstall(tmp_path)
    assert json.loads((tmp_path / kit.MCP_FILE).read_text(encoding="utf-8")) == own and sorted(tree(tmp_path)) == [".mcp.json"]
    assert not any(kit.MCP_FILE in line for line in out)


def test_no_mcp_after_an_install_with_mcp_keeps_that_server_ours(tmp_path):
    """先带 MCP 装过、再用 --no-mcp 重装：这一次不碰 .mcp.json，但那个服务仍是上次装的，撤销时照样去掉。"""
    install(tmp_path)
    before = (tmp_path / kit.MCP_FILE).read_bytes()
    again = install(tmp_path, mcp=False)
    assert again.mcp is None and (tmp_path / kit.MCP_FILE).read_bytes() == before
    assert json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))["mcp"] is True
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {}


def test_manifest_without_the_mcp_field_still_uninstalls_the_server(tmp_path):
    """旧版本写的清单没有 mcp 字段：那时总是装 MCP 的，撤销照样去掉。"""
    install(tmp_path)
    manifest = json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))
    del manifest["mcp"]
    (tmp_path / kit.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {}


def test_local_settings_puts_the_hook_in_settings_local_json(tmp_path):
    """--local-settings：钩子写进不入库的 .claude/settings.local.json，入库的 settings.json 不建；撤销照清单撤回。"""
    plan = install(tmp_path, local_settings=True)
    assert not (tmp_path / kit.SETTINGS).exists()
    local = json.loads((tmp_path / kit.SETTINGS_LOCAL).read_text(encoding="utf-8"))
    [group] = local["hooks"]["PostToolUse"]
    assert group["hooks"] == [{"type": "command", "command": plan.hook_command}]
    assert kit.SETTINGS_LOCAL in "\n".join(plan.lines()) and any("--local-settings" in n for n in plan.notes)
    assert json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))["settings"] == kit.SETTINGS_LOCAL
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {}


def test_switching_settings_file_moves_the_hook_and_keeps_other_hooks(tmp_path):
    """先装进 settings.json、再用 --local-settings 重装：钩子挪过去，settings.json 里别人的钩子留着；反过来也一样。"""
    other = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}
    (tmp_path / ".claude").mkdir()
    (tmp_path / kit.SETTINGS).write_text(json.dumps(other), encoding="utf-8")
    install(tmp_path)
    install(tmp_path, local_settings=True)
    assert json.loads((tmp_path / kit.SETTINGS).read_text(encoding="utf-8")) == other
    assert "pystepmap_check.py" in (tmp_path / kit.SETTINGS_LOCAL).read_text(encoding="utf-8")
    install(tmp_path)
    assert not (tmp_path / kit.SETTINGS_LOCAL).exists()  # 只剩我们的钩子、去掉后空了：删掉
    kit.uninstall(tmp_path)
    assert json.loads((tmp_path / kit.SETTINGS).read_text(encoding="utf-8")) == other


def test_cli_local_settings(tmp_path):
    assert main(["kit", "install", str(tmp_path), "--yes", "--no-mcp", "--local-settings"]) == 0
    assert (tmp_path / kit.SETTINGS_LOCAL).is_file() and not (tmp_path / kit.SETTINGS).exists()


def test_unreadable_other_settings_file_does_not_block_install_or_uninstall(tmp_path):
    """另一个设置文件（这里是 settings.local.json）不是合法 JSON：安装、撤销都不碰它、不报错；安装时提一句没检查。"""
    (tmp_path / ".claude").mkdir()
    bad = tmp_path / kit.SETTINGS_LOCAL
    bad.write_bytes(b"{not json")
    plan = install(tmp_path)
    assert bad.read_bytes() == b"{not json" and any(kit.SETTINGS_LOCAL in n and "JSON" in n for n in plan.notes)
    out = kit.uninstall(tmp_path)
    assert bad.read_bytes() == b"{not json" and any(kit.SETTINGS_LOCAL in line and "保留" in line for line in out)
    assert not (tmp_path / kit.SETTINGS).exists() and not (tmp_path / kit.MANIFEST).exists()


def test_uninstall_old_manifest_without_settings_field_still_removes_local_hook(tmp_path):
    """旧清单没有 settings 字段、钩子却在 settings.local.json 里：撤销照样撤干净。"""
    install(tmp_path, local_settings=True)
    manifest = json.loads((tmp_path / kit.MANIFEST).read_text(encoding="utf-8"))
    del manifest["settings"]
    (tmp_path / kit.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    kit.uninstall(tmp_path)
    assert tree(tmp_path) == {}


def test_hook_in_both_settings_files_ends_up_only_in_the_target(tmp_path):
    """两个设置文件里都有我们的钩子时用 --local-settings 安装：settings.json 里去掉，settings.local.json 里恰好一条。"""
    install(tmp_path)
    (tmp_path / kit.SETTINGS_LOCAL).write_bytes((tmp_path / kit.SETTINGS).read_bytes())
    install(tmp_path, local_settings=True)
    assert not (tmp_path / kit.SETTINGS).exists()
    local = json.loads((tmp_path / kit.SETTINGS_LOCAL).read_text(encoding="utf-8"))
    assert len(local["hooks"]["PostToolUse"]) == 1


def test_uninstall_with_a_broken_mcp_json_changes_nothing(tmp_path):
    """.mcp.json 坏了：在动任何文件之前报错，"没有改动任何文件"是真的；修好之后照常撤销。"""
    install(tmp_path)
    (tmp_path / kit.MCP_FILE).write_text("{坏的", encoding="utf-8")
    before = tree(tmp_path)
    with pytest.raises(kit.KitError, match="没有改动任何文件"):
        kit.uninstall(tmp_path)
    assert tree(tmp_path) == before
    (tmp_path / kit.MCP_FILE).write_text("{}", encoding="utf-8")
    kit.uninstall(tmp_path)
    assert not (tmp_path / kit.MANIFEST).exists() and not (tmp_path / kit.HOOK_FILE).exists()


def test_installed_skill_self_check_commands_really_check_the_example(tmp_path):
    """装出来的 SKILL.md"改完必须做的事"里的命令照原样运行：检查到了示例文件（没有"不对应任何 .py 文件"的警告），
    出图之后的打印脚本找得到示例的编排函数。示例装在 .claude 下，从项目根分析会被排除，所以命令把 skill 目录当项目目录。"""
    install(tmp_path, command=[sys.executable, "-m", "pystepmap.cli"])
    skill = (tmp_path / kit.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    tail = skill.split("## 改完必须做的事", 1)[1]
    blocks = [textwrap.dedent(b) for b in re.findall(r"```bash\n(.*?)```", tail, flags=re.DOTALL)][:2]
    flow = tmp_path / "flow.json"
    check, draw = (b.replace("/tmp/flow.json", str(flow)) for b in blocks)
    assert " check " in check and " analyze " in draw
    r = subprocess.run(check, shell=True, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert (r.returncode, r.stdout.strip()) == (0, "共 0 处违规") and "警告" not in r.stderr
    r = subprocess.run(draw, shell=True, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "step 查询请求状态" in r.stdout and "-->" in r.stdout
