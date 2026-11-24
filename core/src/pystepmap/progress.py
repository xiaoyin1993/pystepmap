"""运行中的进度：测试跑着的时候，读记录插件（trace.py）正在写的记录文件，算出到现在为止跑完了几个测试、哪些正在跑、
最近跑完的几个，以及图里每个函数执行了几次、执行过哪些行。平台在测试命令跑着的时候用它（server 的 live.py）；只用
标准库，读文件、不执行任何东西。

TraceProgress(path).poll()：每次只读上次之后新追加的字节（一次最多 budget 个，积压很多时分几次读完），只处理完整的行
（最后半行留到下次）。容忍：文件还不存在（什么都没有）、后来被删掉（留着读到的）、坏行（不是 JSON、不是对象、字段
不对：跳过——读的是正在写的文件，不报错）、文件被换掉（设备号 / inode 变了）或截短（比读到的位置短）：从头再读。
一行长得没有尽头（超过 max_line 个字节还没有换行）时丢掉它，接着找下一个换行。

认的行（字段见 trace.py 的说明；别的行——版本头、插件错误——不看）：
- {"collected": N}：收集到的用例数；pytest-xdist 每个工作进程一行，取最大的。没有时 None；
- {"start": 测试 id}：开始跑一个测试（参数化的几个用例 id 相同：每个 id 一个计数，开始加一、结束减一，减到 0 不算在跑）；
- 测试结果行 {"test", "outcome", "seconds", "n"}：done 加 n，passed / failed / skipped / errors（outcome error）各加 n，
  这个 id 的计数减 n；最近跑完的 RECENT 个 {test, outcome, seconds}，新的在前；
- 执行记录 {"fn", "lines", "n"}：这个函数执行了几次（n 相加）、执行过的行号（lines 的并集）。给了 functions（图里的
  函数 key）时只留其中的。

只留标识（测试 id、函数 key）、计数、行号、秒数、结果种类；异常类型名、调用、耗时、别的字段都不留。内存跟着图走
（每个函数一个行号集合，执行再多次也不变大），不跟着记录文件的大小走。不是线程安全的：调用的一方加锁。
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from collections.abc import Container
from pathlib import Path

RECENT = 8  # 最近跑完的测试留几个
BUDGET = 16 << 20  # 一次 poll 最多读多少字节
MAX_LINE = 64 << 20  # 一行最长多少字节（再长就丢掉）
_CHUNK = 1 << 20
_OUTCOMES = {"passed": "passed", "failed": "failed", "skipped": "skipped", "error": "errors"}


class TraceProgress:
    """一个记录文件的进度。poll() 读新追加的部分，返回到现在为止的进度（见模块说明）。"""

    def __init__(self, path: str | os.PathLike, *, functions: Container[str] | None = None, budget: int = BUDGET,
                 max_line: int = MAX_LINE) -> None:
        self._path = Path(path)
        self._functions = functions
        self._budget = budget
        self._max_line = max_line
        self._start_over()

    def _start_over(self) -> None:
        self._ident: tuple[int, int] | None = None  # 正在读的文件（设备号，inode）
        self._offset = 0  # 读到了第几个字节
        self._rest = b""  # 还没遇到换行的半行
        self._skipping = False  # 正在丢一行太长的
        self._collected: int | None = None
        self._counts = dict.fromkeys(("done", "passed", "failed", "skipped", "errors"), 0)
        self._running: dict[str, int] = {}  # 测试 id -> 开始了还没结束的个数（按开始的先后）
        self._recent: deque[dict] = deque(maxlen=RECENT)  # 新的在左边
        self._executions: dict[str, int] = {}
        self._lines: dict[str, set[int]] = {}
        self._frozen: dict[str, frozenset[int]] = {}  # 上次给出去的行号集合（没变的不重新复制）
        self._changed: set[str] = set()

    def poll(self) -> dict:
        """读新追加的部分，返回 {"collected", "done", "passed", "failed", "skipped", "errors", "running": [测试 id，
        按开始的先后], "recent": [{test, outcome, seconds}，新的在前], "functions": {函数 key: {"executions",
        "lines": frozenset}}}。"""
        self._read()
        for key in self._changed:
            self._frozen[key] = frozenset(self._lines[key])
        self._changed.clear()
        return {"collected": self._collected, **self._counts, "running": list(self._running),
                "recent": [dict(r) for r in self._recent],
                "functions": {k: {"executions": n, "lines": self._frozen[k]} for k, n in self._executions.items()}}

    # ---- 读 ----

    def _read(self) -> None:
        try:
            f = open(self._path, "rb")  # noqa: SIM115 —— 下面的 with 关
        except OSError:  # 还没有，或者已经删掉了：留着读到的
            return
        with f:
            st = os.fstat(f.fileno())
            ident = (st.st_dev, st.st_ino)
            if self._ident is not None and (ident != self._ident or st.st_size < self._offset):
                self._start_over()  # 换了一个文件，或者截短了
            self._ident = ident
            f.seek(self._offset)
            left = self._budget
            while left > 0:
                chunk = f.read(min(_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                self._offset += len(chunk)
                self._feed(chunk)

    def _feed(self, chunk: bytes) -> None:
        end = chunk.rfind(b"\n")
        if end < 0:
            self._keep(chunk)
            return
        data = self._rest + chunk[:end] if self._rest else chunk[:end]
        rows = data.split(b"\n")
        if self._skipping:  # 第一行是那行太长的结尾
            rows = rows[1:]
            self._skipping = False
        self._rest = b""
        for raw in rows:
            self._take(raw)
        self._keep(chunk[end + 1:])

    def _keep(self, tail: bytes) -> None:
        """还没遇到换行的部分：留到下次；太长了就丢掉，直到下一个换行。"""
        if self._skipping or not tail:
            return
        if len(self._rest) + len(tail) > self._max_line:
            self._rest = b""
            self._skipping = True
        else:
            self._rest += tail

    def _take(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError):  # 不是 JSON（UnicodeDecodeError 也是 ValueError）、嵌套太深
            return
        if not isinstance(rec, dict):
            return
        if "fn" in rec:
            self._execution(rec)
        elif "outcome" in rec:
            self._result(rec)
        elif "start" in rec:
            test = rec["start"]
            if isinstance(test, str):
                self._running[test] = self._running.get(test, 0) + 1
        elif "collected" in rec:
            n = rec["collected"]
            if type(n) is int and n >= 0 and (self._collected is None or n > self._collected):
                self._collected = n

    def _execution(self, rec: dict) -> None:
        key, lines, n = rec.get("fn"), rec.get("lines"), rec.get("n", 1)
        if not (isinstance(key, str) and isinstance(lines, list) and type(n) is int and n > 0
                and all(type(x) is int for x in lines)):
            return
        if self._functions is not None and key not in self._functions:
            return
        self._executions[key] = self._executions.get(key, 0) + n
        seen = self._lines.get(key)
        if seen is None:
            seen = self._lines[key] = set()
        if not seen.issuperset(lines):
            seen.update(lines)
            self._changed.add(key)
        elif key not in self._frozen:
            self._changed.add(key)

    def _result(self, rec: dict) -> None:
        test, outcome, seconds, n = rec.get("test"), rec.get("outcome"), rec.get("seconds"), rec.get("n", 1)
        if not (isinstance(test, str) and outcome in _OUTCOMES and type(seconds) in (int, float)
                and math.isfinite(seconds) and seconds >= 0 and type(n) is int and n > 0):
            return
        self._counts["done"] += n
        self._counts[_OUTCOMES[outcome]] += n
        left = self._running.get(test, 0) - n
        if left > 0:
            self._running[test] = left
        else:
            self._running.pop(test, None)
        self._recent.appendleft({"test": test, "outcome": outcome, "seconds": seconds})
