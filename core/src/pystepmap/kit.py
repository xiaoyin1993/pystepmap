"""把约束套件装进别的项目（pystepmap kit install / uninstall）。这是 pystepmap 唯一往别的项目写文件的功能，只在
用户明确运行这条命令时发生：先列出要写什么，确认之后才写；写了什么记在清单里，uninstall 照清单撤销。

装进目标项目的东西：
- .claude/skills/flow-style/   流程代码怎么写的 skill（SKILL.md、dbos.md、requirements.md、routes.md、示例 transfer_workflow.py、给 OpenSpec 用的
  规则片段 openspec-flow-rules.yaml）
- .claude/hooks/pystepmap_check.py   编辑 .py 之后的检查脚本；.claude/settings.json 里加一条 PostToolUse 钩子运行它
  （给了 --local-settings 时写进不入库的 .claude/settings.local.json；另一个设置文件读不了时不碰它、不挡安装和撤销）
- .mcp.json   加一个 pystepmap 的 MCP 服务（Claude Code 查流程、重新分析、看流程差异）；给了 --no-mcp 时不碰这个文件
  （pystepmap 的 MCP 已经在用户级配过的，项目里再写一份就重复了）
- 给了 --pre-commit 时：.claude/hooks/pystepmap_pre_commit.py 和 .git/hooks/pre-commit（已经有提交前钩子、
  或者设了 core.hooksPath 的仓库不装，并说明）
- .claude/pystepmap-kit.json   清单：写了哪些文件（内容的 sha256）、加了哪条钩子、MCP 服务是不是自己装的、.mcp.json 里
  原来有没有同名服务

已有的 .claude/settings.json、.mcp.json 只加自己的那一项，别的不动；不是合法 JSON 时什么都不写。已有的同名文件
（skill、脚本）内容不一样时不覆盖，除非是上一次自己装的（清单里的 sha256 对得上）。撤销时只删内容没被改过的文件。

钩子和 MCP 用的命令是运行这条命令的那个 pystepmap 的绝对路径（--command 可以换，比如发布之后的 "uvx pystepmap"）。
pystepkit（标记包）不替项目安装：只说明怎么装。只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = ".claude/pystepmap-kit.json"
SKILL_DIR = ".claude/skills/flow-style"
HOOK_FILE = ".claude/hooks/pystepmap_check.py"
PRE_COMMIT_FILE = ".claude/hooks/pystepmap_pre_commit.py"
GIT_PRE_COMMIT = ".git/hooks/pre-commit"
SETTINGS = ".claude/settings.json"
SETTINGS_LOCAL = ".claude/settings.local.json"
MCP_FILE = ".mcp.json"
MCP_NAME = "pystepmap"
HOOK_MATCHER = "Edit|Write|MultiEdit"
EXAMPLE_NAME = "transfer_workflow.py"
OPENSPEC_NAME = "openspec-flow-rules.yaml"
CMD = "{cmd}"  # skill 文本里放检查器命令的位置


class KitError(Exception):
    """装不了 / 撤销不了：说明原因，什么都没写。"""


# skill 里只对 pystepmap 自己的仓库成立的说法 → 装进别的项目时的说法。每一条都必须在源文件里对得上（测试保证），
# 换完之后不能再出现 FORBIDDEN 里的字样。
SKILL_SUBS: list[tuple[str, str]] = [
    ("在本仓库（pystepmap）里", "在本项目里"),
    ("（SM001–SM004，代码在 `core/src/pystepmap/lint.py`）", "（SM001–SM004）"),
    ("`kit/hooks/check_edited_file.py`", f"`{HOOK_FILE}`"),
    ("`kit/hooks/pre-commit`", "提交前检查（装了的话）"),
    ("；测试 `kit/tests/test_skill.py` 会把每个代码块放进\n临时项目跑检查，确认违规写法恰好报这些行、改法零违规。", "。"),
    # 示例装在 .claude 下，从项目根检查 / 分析时这个目录是排除的（什么都查不到）：演示用的命令把 skill 目录当项目目录
    ("1. 在仓库根目录检查改过的文件（路径相对仓库根，可以列多个）：",
     (f"1. 检查改过的文件：在项目根目录运行 `{CMD} check . --files <改过的文件>`（路径相对项目根，可以列多个）。下面拿本 skill\n"
      f"   自带的示例演示；它在 `.claude` 下，从项目根检查时这个目录是排除的，所以演示里把 skill 目录当项目目录：")),
    ("uv run pystepmap check . --files examples/transfer/transfer_workflow.py", f"{CMD} check {SKILL_DIR} --files {EXAMPLE_NAME}"),
    ("uv run pystepmap analyze examples/transfer ", f"{CMD} analyze {SKILL_DIR} "),
    ("examples/transfer/transfer_workflow.py", f"{SKILL_DIR}/{EXAMPLE_NAME}"),
    ("本仓库目前不用 DBOS。", ""),
]
REQUIREMENTS_SUBS: list[tuple[str, str]] = [
    ("`kit/openspec/flow-rules.yaml`", f"`{SKILL_DIR}/{OPENSPEC_NAME}`"),
]
DBOS_SUBS: list[tuple[str, str]] = [
    ("；本仓库目前\n不用 DBOS。依据是 `core/src/pystepmap/lint.py` 的 `_DBOS_PRIMITIVES`。", "。"),
]
FORBIDDEN = ("本仓库", "kit/", "examples/", "docs/", "core/src", "uv run pystepmap")  # 最后一个：命令都要换成 CMD


def _package_assets() -> Path:
    return Path(__file__).resolve().parent / "kit_assets"


def asset_sources() -> dict[str, Path]:
    """套件的源文件：装成包时在包里的 kit_assets/（打包时放进去的）；从源码仓库运行时在仓库里原来的位置。"""
    packed = _package_assets()
    if (packed / "SKILL.md").is_file():
        return {"skill": packed / "SKILL.md", "dbos": packed / "dbos.md", "requirements": packed / "requirements.md",
                "routes": packed / "routes.md", "example": packed / EXAMPLE_NAME,
                "hook": packed / "check_edited_file.py", "pre_commit": packed / "pre-commit",
                "openspec": packed / OPENSPEC_NAME}
    repo = Path(__file__).resolve().parents[3]
    found = {"skill": repo / ".claude/skills/flow-style/SKILL.md", "dbos": repo / ".claude/skills/flow-style/dbos.md",
             "requirements": repo / ".claude/skills/flow-style/requirements.md",
             "routes": repo / ".claude/skills/flow-style/routes.md",
             "example": repo / "examples/transfer" / EXAMPLE_NAME, "hook": repo / "kit/hooks/check_edited_file.py",
             "pre_commit": repo / "kit/hooks/pre-commit", "openspec": repo / "kit/openspec/flow-rules.yaml"}
    missing = [str(p) for p in found.values() if not p.is_file()]
    if missing:
        raise KitError(f"找不到套件的文件：{'、'.join(missing)}")
    return found


def render(text: str, subs: list[tuple[str, str]], command: str) -> str:
    """把 skill 文本换成装进别的项目的说法；有一条对不上、或者换完还有只对本仓库成立的字样时抛 KitError。"""
    for old, new in subs:
        if old not in text:
            raise KitError(f"skill 的文本变了，安装器的替换表没跟上（找不到：{old[:40]!r}）")
        text = text.replace(old, new)
    left = [w for w in FORBIDDEN if w in text]
    if left:
        raise KitError(f"skill 换完之后还有只对 pystepmap 仓库成立的字样：{'、'.join(left)}")
    return text.replace(CMD, command)


def default_command() -> list[str]:
    """运行这条命令的那个 pystepmap：解释器旁边的 pystepmap 可执行文件，没有时 `解释器 -m pystepmap.cli`。"""
    exe = Path(sys.executable).parent / "pystepmap"
    return [str(exe)] if exe.is_file() else [sys.executable, "-m", "pystepmap.cli"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, what: str) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise KitError(f"{what}（{path}）读不了或不是合法的 JSON：{e}。没有改动任何文件") from None
    if not isinstance(data, dict):
        raise KitError(f"{what}（{path}）的顶层不是对象。没有改动任何文件")
    return data


def _dump(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclass
class Plan:
    root: Path
    files: dict[str, bytes] = field(default_factory=dict)  # 相对路径 -> 要写的内容（新建，或覆盖上次自己装的）
    unchanged: list[str] = field(default_factory=list)  # 已经是这个内容，不用写
    hook_command: str = ""
    settings_file: str = SETTINGS  # 钩子写进哪个设置文件（--local-settings 时是 SETTINGS_LOCAL）
    settings: dict | None = None  # 要写回的 settings_file（None = 钩子已经在里面）
    other_settings: dict | None = None  # 另一个设置文件里去掉我们的钩子之后的内容（{} = 空了、删掉；None = 那里没有我们的钩子）
    mcp: dict | None = None  # 要写回的 .mcp.json（None = 已经是这个服务）
    mcp_previous: object = None  # .mcp.json 里原来的同名服务（没有时 None）
    skip_mcp: bool = False  # --no-mcp：这一次不碰 .mcp.json
    git_pre_commit: bytes | None = None
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"目标项目：{self.root}", "要写的文件："]
        out += [f"  新建 / 更新  {rel}" for rel in self.files]
        if self.settings is not None:
            out.append(f"  修改  {self.settings_file}：加一条 PostToolUse 钩子（{HOOK_MATCHER} 之后运行 {HOOK_FILE}）")
        if self.other_settings is not None:
            out.append(f"  修改  {_other(self.settings_file)}：去掉原来的编辑后检查钩子（挪到 {self.settings_file}）")
        if self.mcp is not None:
            how = "替换原来的同名服务" if self.mcp_previous is not None else "加一个 MCP 服务"
            out.append(f"  修改  {MCP_FILE}：{how} {MCP_NAME}")
        if self.git_pre_commit is not None:
            out.append(f"  新建  {GIT_PRE_COMMIT}：提交前检查")
        out.append(f"  新建 / 更新  {MANIFEST}（清单，撤销时用）")
        if self.unchanged:
            out.append(f"已经是最新、不用写的：{'、'.join(self.unchanged)}")
        out += [f"注意：{n}" for n in self.notes]
        return out


def plan_install(root: Path, command: list[str] | None = None, pre_commit: bool = False, mcp: bool = True,
                 local_settings: bool = False) -> Plan:
    """算出要往 root 里写什么（不写）。装不了时抛 KitError。mcp=False（--no-mcp）：不碰 .mcp.json。
    local_settings=True：钩子写进 .claude/settings.local.json 而不是 settings.json。"""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise KitError(f"不是目录：{root}")
    cmd = shlex.join(command or default_command())
    src = asset_sources()
    old = _read_json(root / MANIFEST, "上一次安装的清单").get("files", {})
    wanted = {
        f"{SKILL_DIR}/SKILL.md": render(src["skill"].read_text(encoding="utf-8"), SKILL_SUBS, cmd).encode("utf-8"),
        f"{SKILL_DIR}/dbos.md": render(src["dbos"].read_text(encoding="utf-8"), DBOS_SUBS, cmd).encode("utf-8"),
        f"{SKILL_DIR}/requirements.md": render(src["requirements"].read_text(encoding="utf-8"), REQUIREMENTS_SUBS,
                                               cmd).encode("utf-8"),
        f"{SKILL_DIR}/routes.md": render(src["routes"].read_text(encoding="utf-8"), [], cmd).encode("utf-8"),
        f"{SKILL_DIR}/{EXAMPLE_NAME}": src["example"].read_bytes(),
        f"{SKILL_DIR}/{OPENSPEC_NAME}": src["openspec"].read_bytes(),
        HOOK_FILE: src["hook"].read_bytes(),
    }
    plan = Plan(root)
    if pre_commit:
        _plan_pre_commit(plan, root, cmd, src["pre_commit"].read_bytes(), wanted)
    for rel, data in wanted.items():
        path = root / rel
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise KitError(f"{rel} 已经存在，而且不是普通文件。没有改动任何文件")
        if path.exists():
            have = path.read_bytes()
            if have == data:
                plan.unchanged.append(rel)
                continue
            if old.get(rel) != _sha(have):  # 不是上次自己装的原样：是用户的文件，或者用户改过
                raise KitError(f"{rel} 已经存在，内容和要装的不一样（不是上次安装留下的原样）。"
                               "先挪开或删掉它再装。没有改动任何文件")
        plan.files[rel] = data

    plan.hook_command = (f'PYSTEPMAP_HOOK_ROOT="$CLAUDE_PROJECT_DIR" PYSTEPMAP_HOOK_CMD={shlex.quote(cmd)} '
                         f'python3 "$CLAUDE_PROJECT_DIR/{HOOK_FILE}"')
    plan.settings_file = SETTINGS_LOCAL if local_settings else SETTINGS
    settings = _read_json(root / plan.settings_file, "Claude Code 的项目设置")
    if _add_hook(settings, plan.hook_command, plan.settings_file):
        plan.settings = settings
    try:  # 另一个设置文件不是我们要写的：读不了就不碰，不挡安装
        other = _read_json(root / _other(plan.settings_file), "Claude Code 的项目设置")
    except KitError:
        plan.notes.append(f"{_other(plan.settings_file)} 不是合法的 JSON 对象，没有检查里面有没有原来装的 pystepmap 钩子，也没有改它")
    else:
        if _remove_hook(other):  # 上一次装在另一个文件里：挪过来
            plan.other_settings = other
    if local_settings:
        plan.notes.append(f"--local-settings：钩子写在 {SETTINGS_LOCAL}（Claude Code 约定不入库），只对这台机器生效；"
                          f"不想让 {HOOK_FILE} 和清单入库的话，把它们加进 .gitignore")
    if mcp:
        _plan_mcp(plan, root, cmd)
    else:
        plan.skip_mcp = True
        plan.notes.append(f"--no-mcp：没有碰 {MCP_FILE}。skill 里说的 pystepmap MCP 用你已经配好的那个（比如用户级的）；"
                          "还没配过的话，find_nodes、get_flow、diff_flows 这些工具用不了")
    plan.notes.append("标记包 pystepkit 不会替你安装：在项目的环境里装上它（还没有发布到 PyPI 时用本机路径，"
                      "例如 uv add --editable <pystepmap 仓库>/kit/pystepkit），代码里才能 from pystepkit import workflow, step")
    plan.notes.append("装好后重启 Claude Code（或重新打开这个项目），skill 和钩子才生效" if plan.skip_mcp else
                      "装好后重启 Claude Code（或重新打开这个项目），skill、钩子和 MCP 才生效；MCP 第一次用时在 Claude Code 里同意它")
    return plan


def _plan_mcp(plan: Plan, root: Path, cmd: str) -> None:
    mcp = _read_json(root / MCP_FILE, "MCP 配置")
    servers = mcp.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise KitError(f"{MCP_FILE} 里的 mcpServers 不是对象。没有改动任何文件")
    parts = shlex.split(cmd)
    server = {"command": parts[0], "args": [*parts[1:], "mcp"]}
    if servers.get(MCP_NAME) != server:
        plan.mcp_previous = servers.get(MCP_NAME)
        servers[MCP_NAME] = server
        plan.mcp = mcp


def _plan_pre_commit(plan: Plan, root: Path, cmd: str, script: bytes, wanted: dict[str, bytes]) -> None:
    if not (root / ".git").is_dir():
        plan.notes.append("不是 git 仓库的根目录，提交前检查没有装")
        return
    r = subprocess.run(["git", "config", "core.hooksPath"], cwd=root, capture_output=True, text=True, check=False)
    if r.stdout.strip():
        plan.notes.append(f"这个仓库设了 core.hooksPath（{r.stdout.strip()}），提交前检查没有装：在你自己的钩子里运行 "
                          f"PYSTEPMAP_HOOK_CMD={shlex.quote(cmd)} python3 {PRE_COMMIT_FILE}")
        wanted[PRE_COMMIT_FILE] = script
        return
    wrapper = (f"#!/bin/sh\n# pystepmap kit install 装的提交前检查（pystepmap kit uninstall 撤销）\n"
               f"PYSTEPMAP_HOOK_CMD={shlex.quote(cmd)} exec python3 \"$(git rev-parse --show-toplevel)/{PRE_COMMIT_FILE}\"\n"
               ).encode()
    target = root / GIT_PRE_COMMIT
    if target.exists() and target.read_bytes() != wrapper:
        plan.notes.append(f"{GIT_PRE_COMMIT} 已经有了，提交前检查没有装：在它里面加一行 "
                          f"PYSTEPMAP_HOOK_CMD={shlex.quote(cmd)} python3 {PRE_COMMIT_FILE}")
        wanted[PRE_COMMIT_FILE] = script
        return
    wanted[PRE_COMMIT_FILE] = script
    if not target.exists():
        plan.git_pre_commit = wrapper


def _other(settings_file: str) -> str:
    return SETTINGS if settings_file == SETTINGS_LOCAL else SETTINGS_LOCAL


def _our_hook(hook: object) -> bool:
    return isinstance(hook, dict) and isinstance(hook.get("command"), str) and HOOK_FILE in hook["command"]


def _add_hook(settings: dict, command: str, name: str = SETTINGS) -> bool:
    """在 settings 的 hooks.PostToolUse 里放上（或换成）我们的钩子；已经是这一条时返回 False。"""
    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", []) if isinstance(hooks, dict) else None
    if not isinstance(post, list):
        raise KitError(f"{name} 里的 hooks.PostToolUse 不是列表。没有改动任何文件")
    entry = {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]}
    if entry in post:
        return False
    _remove_hook(settings)
    settings.setdefault("hooks", {}).setdefault("PostToolUse", []).append(entry)
    return True


def _remove_hook(settings: dict) -> bool:
    """去掉 settings 里我们的钩子（别的钩子不动）；空下来的列表、对象一并去掉。返回有没有去掉东西。"""
    hooks = settings.get("hooks")
    post = hooks.get("PostToolUse") if isinstance(hooks, dict) else None
    if not isinstance(post, list):
        return False
    removed = False
    for group in list(post):
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list) or not any(_our_hook(h) for h in inner):
            continue
        removed = True
        inner[:] = [h for h in inner if not _our_hook(h)]
        if not inner:
            post.remove(group)
    if removed and not post:
        del hooks["PostToolUse"]
        if not hooks:
            del settings["hooks"]
    return removed


def _write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)


def apply_install(plan: Plan) -> None:
    root = plan.root
    old = _read_json(root / MANIFEST, "上一次安装的清单")
    files = dict(old.get("files", {}))
    for rel, data in plan.files.items():
        _write(root / rel, data)
        files[rel] = _sha(data)
    for rel in plan.unchanged:
        files.setdefault(rel, _sha((root / rel).read_bytes()))
    if plan.settings is not None:
        _write(root / plan.settings_file, _dump(plan.settings))
    if plan.other_settings is not None:
        other_path = root / _other(plan.settings_file)
        if plan.other_settings:
            _write(other_path, _dump(plan.other_settings))
        else:
            other_path.unlink()
    # mcp：.mcp.json 里的 pystepmap 是不是我们装的（撤销时才动它）。这一次没碰（--no-mcp）时照上一次的；旧清单没有
    # 这个字段，那时总是装的
    manifest = {"version": 1, "files": files, "hook": True, "settings": plan.settings_file,
                "mcp": not plan.skip_mcp or (bool(old) and old.get("mcp", True)),
                "mcp_previous": old.get("mcp_previous") if plan.mcp is None else plan.mcp_previous,
                "mcp_file_created": old.get("mcp_file_created", False),
                "git_pre_commit": old.get("git_pre_commit", False)}
    if plan.mcp is not None:
        if not (root / MCP_FILE).exists():
            manifest["mcp_file_created"] = True
        _write(root / MCP_FILE, _dump(plan.mcp))
    if plan.git_pre_commit is not None:
        _write(root / GIT_PRE_COMMIT, plan.git_pre_commit, 0o755)
        manifest["git_pre_commit"] = _sha(plan.git_pre_commit)
    _write(root / MANIFEST, _dump(manifest))


def uninstall(root: Path) -> list[str]:
    """照清单撤销，返回说明做了什么的几行。没有清单时抛 KitError。"""
    root = root.expanduser().resolve()
    if not (root / MANIFEST).is_file():
        raise KitError(f"{root} 里没有安装清单（{MANIFEST}）：这里没有用 pystepmap kit install 装过")
    manifest = _read_json(root / MANIFEST, "安装清单")
    out: list[str] = []
    # 先把设置文件读好、算好：清单里记的那个读不了就在动任何东西之前报错；另一个读不了就留着不动，不让撤销做一半
    target = manifest.get("settings") if manifest.get("settings") in (SETTINGS, SETTINGS_LOCAL) else SETTINGS
    settings_edits: list[tuple[Path, dict, str]] = []
    for rel in (target, _other(target)):  # 旧清单没有 settings 字段：当作 settings.json
        settings_path = root / rel
        if not settings_path.exists():
            continue
        try:
            settings = _read_json(settings_path, "Claude Code 的项目设置")
        except KitError:
            if rel == target:
                raise
            out.append(f"保留  {rel}（不是合法的 JSON 对象，没有检查里面有没有 pystepmap 的钩子）")
            continue
        if _remove_hook(settings):
            settings_edits.append((settings_path, settings, rel))
    # .mcp.json 也先读好：读不了就在动任何东西之前报错（"没有改动任何文件"才是真的）
    mcp_path = root / MCP_FILE
    mcp = None
    if manifest.get("mcp", True) and mcp_path.exists():  # --no-mcp 装的：里面的同名服务是用户自己的，不动
        mcp = _read_json(mcp_path, "MCP 配置")
    targets = dict(manifest.get("files", {}))
    if isinstance(manifest.get("git_pre_commit"), str):
        targets[GIT_PRE_COMMIT] = manifest["git_pre_commit"]
    for rel, sha in targets.items():
        path = root / rel
        if not path.is_file():
            continue
        if _sha(path.read_bytes()) != sha:
            out.append(f"保留  {rel}（装了之后改过）")
            continue
        path.unlink()
        out.append(f"删除  {rel}")
    for rel in (SKILL_DIR, ".claude/hooks", ".claude/skills"):  # 空下来的目录（.claude 本身留到最后看）
        d = root / rel
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    for settings_path, settings, rel in settings_edits:
        if settings:
            _write(settings_path, _dump(settings))
        else:
            settings_path.unlink()
        out.append(f"修改  {rel}：去掉了编辑后的检查钩子")
    if mcp is not None:
        servers = mcp.get("mcpServers")
        if isinstance(servers, dict) and MCP_NAME in servers:
            if manifest.get("mcp_previous") is not None:
                servers[MCP_NAME] = manifest["mcp_previous"]
                out.append(f"修改  {MCP_FILE}：{MCP_NAME} 换回了原来的那个")
            else:
                del servers[MCP_NAME]
                out.append(f"修改  {MCP_FILE}：去掉了 {MCP_NAME}")
            if not servers:
                del mcp["mcpServers"]
            if mcp or not manifest.get("mcp_file_created"):
                _write(mcp_path, _dump(mcp))
            else:
                mcp_path.unlink()
    (root / MANIFEST).unlink()
    out.append(f"删除  {MANIFEST}")
    if (root / ".claude").is_dir() and not any((root / ".claude").iterdir()):
        (root / ".claude").rmdir()
    return out
