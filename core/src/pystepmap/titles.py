"""节点标题的辅助：取语句上方的注释、清理要显示的文字。"""

from __future__ import annotations


def comment_above(lines: list[str], lineno: int) -> str:
    """语句（从 1 起的行号 lineno）正上方连续注释块的第一行，去掉 # 和首尾空白；没有则 ""。

    注释块与语句之间隔了空行或代码就不算；块里只有 # 的空注释行跳过。
    """
    block: list[str] = []
    i = lineno - 2
    while 0 <= i < len(lines):
        text = lines[i].strip()
        if not text.startswith("#"):
            break
        block.append(text.lstrip("#").strip())
        i -= 1
    for text in reversed(block):  # 从上往下第一条非空注释
        if text:
            return text
    return ""


FULL_LIMIT = 2000  # 完整形式（Node.title_full 等）的上限：异常长的文档注释、源码不会把图撑大


def cut(s: str, limit: int) -> str:
    """超过 limit 时截断，末尾加 "…"（总长为 limit）。"""
    return s[: limit - 1] + "…" if len(s) > limit else s


def clean(text: str, limit: int = 0) -> str:
    """合并空白、去掉反引号和 **；limit 大于 0 且超长时截断，末尾加 "…"（总长为 limit）。"""
    s = " ".join(text.replace("`", "").replace("**", "").split())
    return cut(s, limit) if limit else s


def clean_full(text: str, limit: int) -> str:
    """clean(text, limit) 截断了时的完整形式：同样清理，不按 limit 截断（最长 FULL_LIMIT）；没截断时为 ""。"""
    s = clean(text)
    return cut(s, FULL_LIMIT) if len(s) > limit else ""


_OPEN, _CLOSE = "（(「『《[【{", "）)」』》]】}"
SENTENCE_MIN = 4  # 第一句至少这么长才在那里断（"注：……" 这样的开头不算一句）


def first_sentence(text: str) -> str:
    """文档注释第一行的第一句：到第一个句号（。）、分号（；;）、破折号（——）或后面跟着空白的英文句号为止，不含它；
    括号、引号里面的不算，断出来不到 SENTENCE_MIN 个字的不算。没有这些标点时就是整行。图上节点的标题用它（整行在
    title_full 里，悬停能看到）。"""
    depth = 0
    for i, ch in enumerate(text):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and i >= SENTENCE_MIN and (
                ch in "。；;" or text.startswith("——", i)
                or (ch == "." and (i + 1 == len(text) or text[i + 1].isspace()))):
            head = text[:i].rstrip()
            if len(head) >= SENTENCE_MIN:
                return head
    return text
