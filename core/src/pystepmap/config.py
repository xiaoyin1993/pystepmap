"""项目配置：控制索引排除目录、噪音过滤、结局字段识别等。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import get_args

from .model import FnKind, OutcomeKind

NOISE_RECEIVERS = ("logger", "log", "logging")  # 这些名字上的调用是日志（noise_receivers 的默认值）
# .claude：kit install 装进去的 skill 示例（transfer_workflow.py）和钩子脚本不是项目的业务代码
DEFAULT_EXCLUDE = [".venv", "venv", "node_modules", "__pycache__", ".git", ".history", ".claude",
                   "site-packages", "dist", "build", "tests"]

_RULE_KINDS = tuple(k for k in get_args(FnKind) if k != "function")  # extra_rules 里入口装饰器可以给的类型
_RULE_KEYS = {"name", "entry_decorators", "dispatch", "retry_keywords"}
_DISPATCH_KEYS = {"methods", "task_from", "dynamic_ok"}


class ConfigError(ValueError):
    """配置不合法（字段类型、取值、源码根目录等）。信息是给用户看的中文。"""


def _str_list(value: object, where: str) -> None:
    if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
        raise ConfigError(f"{where} 必须是字符串列表，实际是 {json.dumps(value, ensure_ascii=False)}")


def _check_extra_rule(rule: object, where: str) -> None:
    if not isinstance(rule, dict):
        raise ConfigError(f"{where} 必须是对象")
    unknown = sorted(set(rule) - _RULE_KEYS)
    if unknown:
        raise ConfigError(f"{where} 含未知字段：{', '.join(unknown)}（可用 {', '.join(sorted(_RULE_KEYS))}）")
    if not isinstance(rule.get("name", "extra"), str):
        raise ConfigError(f"{where}.name 必须是字符串")
    entries = rule.get("entry_decorators", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{where}.entry_decorators 必须是 [正则, 类型] 的列表")
    for i, pair in enumerate(entries):
        at = f"{where}.entry_decorators[{i}]"
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            raise ConfigError(f"{at} 必须是 [正则, 类型]")
        try:
            re.compile(pair[0])
        except re.error as e:
            raise ConfigError(f"{at} 的正则 {pair[0]!r} 写错了：{e}") from None
        if pair[1] not in _RULE_KINDS:
            raise ConfigError(f"{at} 的类型 {pair[1]!r} 不认识（可用 {', '.join(_RULE_KINDS)}）")
    dispatch = rule.get("dispatch", [])
    if not isinstance(dispatch, list):
        raise ConfigError(f"{where}.dispatch 必须是列表")
    for i, d in enumerate(dispatch):
        at = f"{where}.dispatch[{i}]"
        if not isinstance(d, dict) or set(d) - _DISPATCH_KEYS:
            raise ConfigError(f"{at} 必须是只含 {', '.join(sorted(_DISPATCH_KEYS))} 的对象")
        _str_list(d.get("methods", []), f"{at}.methods")
        if d.get("task_from", "arg0") not in ("receiver", "arg0"):
            raise ConfigError(f"{at}.task_from 只能是 receiver 或 arg0，实际是 {d.get('task_from')!r}")
        if not isinstance(d.get("dynamic_ok", True), bool):
            raise ConfigError(f"{at}.dynamic_ok 必须是 true 或 false")
    _str_list(rule.get("retry_keywords", []), f"{where}.retry_keywords")


@dataclass
class ProjectConfig:
    # 不进入的目录名（任何一层）。给出时取代默认列表 DEFAULT_EXCLUDE（.venv、node_modules、tests……），要保留默认的就
    # 一起写上；只想多排除几个目录时用 extra_exclude
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    extra_exclude: list[str] = field(default_factory=list)  # 在 exclude（默认的或给出的）之外再排除的目录名
    noise_receivers: list[str] = field(default_factory=lambda: list(NOISE_RECEIVERS))  # 以这些名字开头的接收者上的调用是噪音
    noise_functions: list[str] = field(default_factory=list)  # 这些项目内函数不算步骤
    outcome_fields: list[str] = field(default_factory=lambda: ["status", "kind", "verdict", "reason"])
    outcome_kinds: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"kind": {"skipped": "skipped", "failed": "failed"}})
    common_task_threshold: int = 6
    extra_rules: list[dict] = field(default_factory=list)
    # 额外的入口：[{"match": "文件::限定名" 的通配模式（* ? [...]，按 fnmatch 区分大小写）, "group": 分组名（可省略）}]。
    # 对上的普通函数各成一条功能线——给不是用装饰器注册的入口用（命令行的子命令、按名字分发的工具方法）
    extra_entries: list[dict] = field(default_factory=list)
    # 没有 await 的调用落在项目外对象上时（db.execute、requests.post、self.session.commit）也画成步骤，见 external.py；
    # false 时只有项目内函数、派发和被 await 的调用是步骤（2026-11-19 之前的行为）
    external_calls: bool = True
    # 一个入口都没认出来时，把没有人调用的顶层函数当入口，保证项目有功能线，见 lines.py；false 时功能线就是空的
    fallback_entries: bool = True
    source_roots: list[str] | None = None  # 源码根（相对项目根目录的目录，"." 是根目录本身）；给出时取代自动检测

    def __post_init__(self) -> None:
        """检查每个字段的类型和取值，不合法时抛 ConfigError（不论来自配置文件还是代码）。"""
        for name in ("exclude", "extra_exclude", "noise_receivers", "noise_functions", "outcome_fields"):
            _str_list(getattr(self, name), name)
        kinds = get_args(OutcomeKind)
        if not isinstance(self.outcome_kinds, dict) or not all(
                isinstance(k, str) and isinstance(m, dict) for k, m in self.outcome_kinds.items()):
            raise ConfigError("outcome_kinds 必须是 {字段名: {字段值: 结局}} 的对象")
        for name, mapping in self.outcome_kinds.items():
            for value, kind in mapping.items():
                if kind not in kinds:
                    raise ConfigError(f"outcome_kinds.{name}.{value} 的结局 {kind!r} 不认识（可用 {', '.join(kinds)}）")
        if type(self.common_task_threshold) is not int or self.common_task_threshold < 1:
            raise ConfigError(f"common_task_threshold 必须是正整数，实际是 {self.common_task_threshold!r}")
        if not isinstance(self.extra_rules, list):
            raise ConfigError("extra_rules 必须是规则对象的列表")
        for i, rule in enumerate(self.extra_rules):
            _check_extra_rule(rule, f"extra_rules[{i}]")
        if not isinstance(self.extra_entries, list):
            raise ConfigError("extra_entries 必须是 {\"match\": 模式, \"group\": 分组} 对象的列表")
        for i, e in enumerate(self.extra_entries):
            if not isinstance(e, dict) or set(e) - {"match", "group"} or not isinstance(e.get("match"), str) \
                    or not e["match"] or not isinstance(e.get("group", ""), str):
                raise ConfigError(f"extra_entries[{i}] 必须是 {{\"match\": 非空的模式, \"group\": 分组名（可省略）}}")
        for name in ("external_calls", "fallback_entries"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"{name} 必须是 true 或 false")
        if self.source_roots is not None:
            _str_list(self.source_roots, "source_roots")

    @property
    def exclude_dirs(self) -> list[str]:
        """实际排除的目录名：exclude 加上 extra_exclude（重复的只算一次，exclude 在前）。"""
        return list(dict.fromkeys([*self.exclude, *self.extra_exclude]))


def load_config(path: Path | None) -> ProjectConfig:
    """读取 JSON 配置；path 为 None 时返回默认值。文件不是合法 JSON 时抛 json.JSONDecodeError，
    出现未知字段或字段不合法时抛 ConfigError。"""
    if path is None:
        return ProjectConfig()
    return parse_config(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_config(data: object) -> ProjectConfig:
    """把配置 JSON 的内容（已解析成 Python 对象）变成 ProjectConfig；顶层不是对象、出现未知字段或字段不合法时抛
    ConfigError。平台保存的项目配置也走这里，和 --config 文件的规则一致。"""
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是对象")
    known = {f.name for f in fields(ProjectConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"配置文件含未知字段：{', '.join(unknown)}")
    return ProjectConfig(**data)
