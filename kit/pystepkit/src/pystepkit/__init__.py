"""pystepkit：流程标记。只给对象挂属性并原样返回，不做任何包装，运行时零开销。"""

from typing import Literal

__all__ = ["condition", "node", "outcome", "step", "workflow"]


def _mark(obj, kind: str, meta: dict):
    obj.__flow_kind__ = kind
    obj.__flow_meta__ = {k: v for k, v in meta.items() if v is not None}
    return obj


def workflow(
    fn_or_cls=None,
    *,
    name: str | None = None,
    title: str | None = None,
    group: str | None = None,
    version: str | None = None,
    entry: str = "run",
):
    """标记一个工作流（函数或类）。`entry` 为类形式时的入口方法名。"""
    meta = {"name": name, "title": title, "group": group, "version": version, "entry": entry}

    def deco(obj):
        return _mark(obj, "workflow", meta)

    return deco if fn_or_cls is None else deco(fn_or_cls)


def step(fn=None, *, title: str | None = None):
    """标记一个步骤。"""

    def deco(obj):
        return _mark(obj, "step", {"title": title})

    return deco if fn is None else deco(fn)


node = step


def condition(fn=None, *, title: str | None = None):
    """标记一个判断条件。"""

    def deco(obj):
        return _mark(obj, "condition", {"title": title})

    return deco if fn is None else deco(fn)


def outcome(
    kind: Literal["ok", "skipped", "failed"] = "ok", title: str | None = None, **fields
) -> dict:
    """构造步骤结果：普通字典。"""
    result: dict = {"kind": kind}
    if title is not None:
        result["title"] = title
    result.update(fields)
    return result
