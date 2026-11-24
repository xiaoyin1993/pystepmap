"""重新生成 pystepmap.resolve.BUILTIN_METHOD_NAMES（那里是写死的清单，不随运行的 Python 版本变）。

清单 = 下面这些常见内置 / 标准库类型上可调用、不以 _ 开头的属性名，在 Python 3.12 和 3.13 上各算一次取并集。
更新清单：分别用 3.12、3.13 运行 `python core/tests/builtin_method_names.py`，把两次输出的并集写回 resolve.py；
测试 test_builtin_method_names_literal 核对写死的清单和当前解释器算出的一致。
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import pathlib
import re
import subprocess
import threading
import types

TYPES = (str, bytes, list, dict, set, tuple, int, float, io.IOBase, io.TextIOWrapper, io.BytesIO,
         pathlib.Path, re.Pattern, re.Match, datetime.datetime, datetime.date, datetime.timedelta,
         asyncio.Future, asyncio.Task, asyncio.Queue, asyncio.Lock, asyncio.Event, logging.Logger,
         threading.Thread, subprocess.Popen, types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType)

# 只在某个版本上算得出来的名字（清单是两个版本的并集）；3.12 上没有自己独有的
VERSION_ONLY: dict[tuple[int, int], frozenset[str]] = {(3, 13): frozenset({"from_uri", "full_match", "shutdown"})}


def regenerate() -> set[str]:
    """当前解释器上算出的名字。"""
    names: set[str] = set()
    for t in TYPES:
        for n in dir(t):
            if n.startswith("_"):
                continue
            try:
                attr = getattr(t, n)
            except AttributeError:
                continue
            if callable(attr):
                names.add(n)
    return names


if __name__ == "__main__":
    print(sorted(regenerate()))
