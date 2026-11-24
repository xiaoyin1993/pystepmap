"""业务需求：先设计流程，实现后对照（设计见 docs/superpowers/specs/2026-11-18-requirements-design.md）。

需求存在被分析的项目里：<项目>/.pystepmap/requirements/<编号>-<短标题>.md，一条一个文件：

    # 大额转账要人工复核

    - 编号：R003
    - 状态：已确认
    - 功能线：账户转账

    ## 说明

    单笔超过 5 万的转账先交人工复核。

    ## 设计的流程

    ```flow
    步骤：参数校验
    判断：需要人工复核？
      是：
        步骤：提交人工复核
        结局（跳过）：已提交人工复核
    步骤：执行转账
    结局（成功）：转账完成
    ```

流程的写法（缩进文本，行首关键字，冒号、括号全角半角都认，缩进用空格）：
- `步骤：标题`，下面可以缩进一个 `失败时：`，带一段这一步出错（抛出异常被接住）之后的内容；`判断：标题`，下面缩进的
  `是：` / `否：` 各带一段更深一层的内容；`循环：标题`，下面缩进的是循环体；`结局（成功 | 跳过 | 失败）：标题`。
- 一段内容从上到下依次执行；判断的分支里没有走到结局的，走完回到判断后面的那一行；没写的分支就是直接往下走。
- 标题就是图上节点的标题。不写变量、参数、代码。

对照（compare）：设计的节点和实现的图按"类型 + 标题"配对（标题去掉空白和句末标点，问号不分全角半角），报告：
- missing：设计里有、实现里没有的步骤 / 判断 / 循环 / 结局（在这条功能线的根函数和它派发的任务里都找不到）；
- extra：功能线的根函数里有、设计里没有的（只看带中文标题的步骤 / 判断 / 循环 / 结局；"执行到函数末尾"、
  "未捕获异常退出"这类隐含结局和捕获节点不算）；
- kind：结局的分类不一样（设计是失败，实现是跳过）；
- order：走向不一致——（几条路都走不通、而且拿掉同一个节点之后都走得通时，只报这一个节点"位置和设计不一样"，
  见 _misplaced）设计里到某个结局的一条路上依次经过的节点（判断带着走的是"是"还是"否"），在根函数里到
  同一个结局的任何一条路上都不是按这个顺序、这些分支出现的。路上配不上的步骤 / 循环（已经在 missing 里）先拿掉再比；
  路上有配不上的判断时这条路不比（实现里没有它，两个分支分不开），在 notes 里说明。
功能线找不到时所有设计的节点都算 missing，并说明。局限：靠标题配对，标题改了就配不上；循环只比有没有、不比次数；
走向只在根函数里比（派发出去的任务里的顺序不比）。

项目用 OpenSpec 管需求时不用另写需求文件：OpenSpec 的 Markdown 文件（openspec/changes/<变更>/ 下的；openspec/specs/ 下的；
已归档的 openspec/changes/archive/<变更>/ 下的也读，编号是 archive/<变更名>，状态"已归档"，排在最后——那是当时的设计，
和现在的代码不一致不算问题）里每个 ```flow 代码块就是一条需求，写法同上，块里第一行写 `功能线：账户转账`。编号是变更名
（一个变更里有几块时第二块起是 `变更名#2`；specs 下的是 `specs/<能力名>`），标题是变更名加代码块上方最近的标题（同一个小节里放了
几块时改用各自写的功能线，免得重名）。
状态不存在文件里（变更的进度由 OpenSpec 管）：进行中的变更按"已确认"对照，specs 下的按"已实现"对照。

只用标准库；只读项目文件——写文件只有 save_requirement，由调用方（MCP 的保存工具）明确调用，只写
.pystepmap/requirements/，不写 OpenSpec 的文件。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .model import FunctionGraph, ProjectGraph

DIR = ".pystepmap/requirements"
ARCHIVE_DIR = "archive"  # openspec/changes/archive/：已归档的变更
ARCHIVED = "已归档"  # 已归档的变更里的流程的状态（不在 STATUSES 里：不能存成这个状态，对照结果也不改它）
OPENSPEC_DIR = "openspec"  # 项目用 OpenSpec 时，它的文件里的 ```flow 代码块也是需求
MAX_OPENSPEC_FILES = 5000  # openspec/ 下最多看这么多个 Markdown 文件
STATUSES = ("草稿", "已确认", "已实现", "已完成")
END_KINDS = {"成功": "ok", "跳过": "skipped", "失败": "failed"}
END_NAMES = {v: k for k, v in END_KINDS.items()}
TYPE_NAMES = {"step": "步骤", "cond": "判断", "loop": "循环", "end": "结局"}
MAX_PATHS = 2000  # 设计的流程最多展开这么多条路；再多就不比走向（判断一个接一个、都不带结局时路数翻倍）
MAX_FILE = 200_000  # 需求文件最大字节数（再大不读）
_ID = re.compile(r"^R\d{3,}$")
_CJK = re.compile(r"[一-鿿]")
_COLON = "：:"


class RequirementError(ValueError):
    """需求写不了：编号、状态不合法，或者流程解析不了。"""


@dataclass
class Item:
    """设计流程里的一个节点。kind：step / cond / loop / end。"""

    kind: str
    title: str
    line: int  # 在流程文本里的行号（从 1 起）
    end_kind: str = ""  # 结局：ok / skipped / failed
    yes: list[Item] = field(default_factory=list)  # 判断的"是"分支
    no: list[Item] = field(default_factory=list)  # 判断的"否"分支
    body: list[Item] = field(default_factory=list)  # 循环体
    fail: list[Item] = field(default_factory=list)  # 步骤的"失败时"


@dataclass
class Requirement:
    id: str
    title: str
    status: str
    line: str  # 功能线：标题、sid 或入口函数的限定名（空 = 没写）
    description: str
    flow_text: str
    file: str = ""  # 相对项目根目录的路径
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # 文件、流程哪里读不懂（有的话不对照）
    text: str = ""  # 文件的原文（从文件读出来的才有；给界面看）
    source: str = "pystepmap"  # pystepmap：.pystepmap/requirements/ 里的文件；openspec：OpenSpec 文件里的 flow 代码块
    archived: bool = False  # OpenSpec 已归档的变更（openspec/changes/archive/<变更>/）里的：当时的设计，列出来但和正在做的分开


@dataclass
class Difference:
    kind: str  # missing / extra / kind / order / no_line
    text: str  # 给人看的一句
    type: str = ""  # 节点类型 step / cond / loop / end
    title: str = ""
    node: str = ""  # extra、kind：实现里那个节点的 sid


@dataclass
class Comparison:
    line_sid: str = ""  # 对上的功能线（没找到时空）
    line_title: str = ""
    differences: list[Difference] = field(default_factory=list)
    outcomes: list[tuple[str, str, str]] = field(default_factory=list)  # 设计的结局：(标题, 分类, 实现里的节点 sid 或 "")
    notes: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return bool(self.line_sid) and not self.differences


# ---------- 读文件 ----------

def _norm(title: str) -> str:
    """配对用的标题：去掉空白、反引号，问号不分全角半角，去掉句末的句号、分号。"""
    s = "".join(title.replace("`", "").split()).replace("?", "？")
    return s.rstrip("。；;.")


def parse_flow(text: str) -> tuple[list[Item], list[str]]:
    """流程文本 → (节点树, 读不懂的地方)。有读不懂的地方时节点树不可靠，调用方不该拿去对照。"""
    errors: list[str] = []
    rows: list[tuple[int, int, str]] = []  # (行号, 缩进, 内容)
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            errors.append(f"第 {no} 行：缩进要用空格，不要用制表符")
            continue
        rows.append((no, len(raw) - len(raw.lstrip(" ")), raw.strip()))
    pos = 0

    def head(content: str) -> tuple[str, str, str]:
        """(关键字, 括号里的, 冒号后面的)；不是"关键字：……"的样子时关键字为空。"""
        m = re.match(r"^(步骤|判断|循环|结局|失败时|是|否)\s*(?:[（(]\s*([^）)]*)\s*[）)])?\s*[：:]\s*(.*)$", content)
        return (m.group(1), (m.group(2) or "").strip(), m.group(3).strip()) if m else ("", "", "")

    def block(indent: int) -> list[Item]:
        nonlocal pos
        out: list[Item] = []
        while pos < len(rows):
            no, ind, content = rows[pos]
            if ind < indent:
                break
            if ind > indent:
                errors.append(f"第 {no} 行：缩进多了（上一行不是判断的分支、也不是循环）")
                pos += 1
                continue
            key, paren, rest = head(content)
            if key in ("", "是", "否", "失败时"):
                errors.append(f"第 {no} 行：读不懂 {content!r}——每行以 步骤： / 判断： / 循环： / 结局（成功|跳过|失败）： 开头"
                              if key == "" else f"第 {no} 行：{key}： 只能写在{'步骤' if key == '失败时' else '判断'}的下面")
                pos += 1
                continue
            if not rest:
                errors.append(f"第 {no} 行：{key} 没有写标题")
            pos += 1
            if out and out[-1].kind == "end":
                errors.append(f"第 {no} 行：前面已经是结局，这一行走不到")
            if key == "步骤":
                item = Item("step", rest, no)
                if pos < len(rows) and rows[pos][1] > indent and head(rows[pos][2])[0] == "失败时":
                    fno, find, fcontent = rows[pos]
                    if head(fcontent)[2]:
                        errors.append(f"第 {fno} 行：失败时： 后面不要接内容，内容写在下一行并再缩进一层")
                    pos += 1
                    item.fail = block(_deeper(find))
                    if not item.fail:
                        errors.append(f"第 {fno} 行：失败时： 下面没有内容")
                out.append(item)
            elif key == "结局":
                if paren not in END_KINDS:
                    errors.append(f"第 {no} 行：结局要写成 结局（成功）： / 结局（跳过）： / 结局（失败）：")
                out.append(Item("end", rest, no, end_kind=END_KINDS.get(paren, "ok")))
            elif key == "循环":
                item = Item("loop", rest, no)
                item.body = block(_deeper(indent))
                out.append(item)
            else:
                item = Item("cond", rest, no)
                seen: set[str] = set()
                while pos < len(rows) and rows[pos][1] > indent:
                    bno, bind, bcontent = rows[pos]
                    bkey, _, brest = head(bcontent)
                    if bkey not in ("是", "否"):
                        errors.append(f"第 {bno} 行：判断的下面只能是 是： 或 否：，然后再缩进一层写内容")
                        pos += 1
                        continue
                    if bkey in seen:
                        errors.append(f"第 {bno} 行：{bkey}： 写了两次")
                    seen.add(bkey)
                    if brest:
                        errors.append(f"第 {bno} 行：{bkey}： 后面不要接内容，内容写在下一行并再缩进一层")
                    pos += 1
                    inner = block(_deeper(bind))
                    if bkey == "是":
                        item.yes = inner
                    else:
                        item.no = inner
                out.append(item)
        return out

    def _deeper(indent: int) -> int:
        return rows[pos][1] if pos < len(rows) and rows[pos][1] > indent else indent + 1

    items = block(rows[0][1] if rows else 0)
    if not rows:
        errors.append("流程是空的")
    return items, errors


def parse_requirement(text: str, file: str = "") -> Requirement:
    """需求文件的内容 → Requirement。读不懂的地方记在 errors 里，不抛异常。"""
    title, meta, sections, current, fence = "", {}, {}, "", False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            fence = not fence
            if current == "设计的流程":
                continue
        if not fence and raw.startswith("# ") and not title:
            title = raw[2:].strip()
        elif not fence and raw.startswith("## "):
            current = raw[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(raw)
        else:
            m = re.match(r"^\s*[-*]\s*(编号|状态|功能线)\s*[：:]\s*(.*)$", raw)
            if m:
                meta[m.group(1)] = m.group(2).strip()
    flow_text = "\n".join(sections.get("设计的流程", [])).strip("\n")
    r = Requirement(id=meta.get("编号", ""), title=title, status=meta.get("状态", ""), line=meta.get("功能线", ""),
                    description="\n".join(sections.get("说明", [])).strip(), flow_text=flow_text, file=file)
    if not title:
        r.errors.append("没有标题（第一行写 # 标题）")
    if not _ID.match(r.id):
        r.errors.append(f"编号要写成 R 加至少三位数字（- 编号：R001），现在是 {r.id!r}")
    if r.status not in STATUSES:
        r.errors.append(f"状态要是 {' / '.join(STATUSES)} 之一，现在是 {r.status!r}")
    if "设计的流程" not in sections:
        r.errors.append("没有「## 设计的流程」一节")
    else:
        r.items, errors = parse_flow(flow_text)
        r.errors += [f"设计的流程 {e}" for e in errors]
    return r


_FENCE = re.compile(r"^(`{3,}|~{3,})\s*([^\s`]*)")
_LINE_HEAD = re.compile(r"^\s*功能线\s*[：:]\s*(.*)$")


def flow_blocks(text: str) -> list[tuple[str, str]]:
    """Markdown 里的 ```flow 代码块（~~~ 也认）：[(代码块上方最近的标题，没有时 "", 块里的文本)]。别的代码块里的
    标题、围栏不算。"""
    out: list[tuple[str, str]] = []
    heading, fence, is_flow, buf = "", "", False, []
    for raw in text.splitlines():
        s = raw.strip()
        if fence:
            if s.startswith(fence) and not s.strip(fence[0]):
                if is_flow:
                    out.append((heading, "\n".join(buf)))
                fence = ""
            else:
                buf.append(raw)
        elif m := _FENCE.match(s):
            fence, is_flow, buf = m.group(1), m.group(2).lower() == "flow", []
        elif s.startswith("#"):
            heading = s.lstrip("#").strip()
    return out


def _openspec_files(root: Path) -> list[tuple[Path, str]]:
    """OpenSpec 里要看的 Markdown 文件：[(路径, 所属的编号前缀)]。进行中的变更（openspec/changes/<变更>/）前缀是变更名；
    当前规格（openspec/specs/<能力>/）前缀是 specs/<能力>；已归档的变更（openspec/changes/archive/<变更>/）前缀是
    archive/<变更>，排在最后。链接不跟。"""
    base = root / OPENSPEC_DIR
    if not base.is_dir() or base.is_symlink():
        return []
    groups: list[tuple[Path, str]] = []
    for kind in ("changes", "specs"):
        top = base / kind
        if not top.is_dir() or top.is_symlink():
            continue
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and not sub.is_symlink() and not (kind == "changes" and sub.name == ARCHIVE_DIR):
                groups.append((sub, sub.name if kind == "changes" else f"specs/{sub.name}"))
    archive = base / "changes" / ARCHIVE_DIR
    if archive.is_dir() and not archive.is_symlink():
        groups += [(sub, f"{ARCHIVE_DIR}/{sub.name}") for sub in sorted(archive.iterdir())
                   if sub.is_dir() and not sub.is_symlink()]
    out: list[tuple[Path, str]] = []
    for folder, prefix in groups:
        for path in sorted(folder.rglob("*.md")):
            if len(out) >= MAX_OPENSPEC_FILES:
                return out
            if path.is_file() and not path.is_symlink() and not any((root / p).is_symlink() for p in path.relative_to(root).parents):
                out.append((path, prefix))
    return out


def load_openspec(root: Path) -> list[Requirement]:
    """OpenSpec 文件里的 flow 代码块，每块一条需求（见模块说明）。没有 openspec/ 目录、没有 flow 代码块时空。"""
    out: list[Requirement] = []
    counts: dict[str, int] = {}
    for path, prefix in _openspec_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            if path.stat().st_size > MAX_FILE:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue  # OpenSpec 的文件读不了不是需求的错：不列
        if "flow" not in text:
            continue
        blocks = flow_blocks(text)
        shared = {h for h, _ in blocks if sum(1 for x, _ in blocks if x == h) > 1}  # 一个小节里放了几块：标题会重名
        for heading, body in blocks:
            line, rows = "", []
            for raw in body.splitlines():
                m = _LINE_HEAD.match(raw)
                if m and not line:
                    line = " ".join(m.group(1).split())
                    rows.append("")  # 留一个空行：流程里报错的行号还是代码块里的行号
                else:
                    rows.append(raw)
            counts[prefix] = counts.get(prefix, 0) + 1
            rid = prefix if counts[prefix] == 1 else f"{prefix}#{counts[prefix]}"
            archived = prefix.startswith(ARCHIVE_DIR + "/")
            name = prefix.split("/", 1)[1] if archived else prefix
            # 标题：变更名加代码块上方最近的小节标题；同一个小节里有几块时小节标题分不开它们，改用各自写的功能线
            said = line if line and (heading in shared or not heading) else heading
            r = Requirement(id=rid, title=f"{name}：{said}" if said else name,
                            status=ARCHIVED if archived else "已实现" if prefix.startswith("specs/") else "已确认",
                            line=line, description="", flow_text="\n".join(rows).strip("\n"), file=rel, text=text,
                            source="openspec", archived=archived)
            r.items, errors = parse_flow(r.flow_text)
            r.errors = [f"设计的流程 {e}" for e in errors]
            if not line:
                r.errors.append("flow 代码块里没有写功能线（块里第一行写 功能线：<功能线的标题>）")
            out.append(r)
    return out


def uses_openspec(root: Path) -> bool:
    """项目是不是用 OpenSpec 管需求（根目录下有 openspec/ 目录）。"""
    return (root / OPENSPEC_DIR).is_dir()


def load_requirements(root: Path) -> list[Requirement]:
    """项目里的全部需求：.pystepmap/requirements/ 里的文件（按编号、文件名排序），后面是 OpenSpec 文件里的 flow 代码块
    （load_openspec）。都没有时空；读不了、太大的需求文件也列出来（errors 里说明）。链接到别处的文件不读。"""
    return _load_own(root) + load_openspec(root)


def _load_own(root: Path) -> list[Requirement]:
    folder = root / DIR
    if not folder.is_dir() or folder.is_symlink():
        return []
    out: list[Requirement] = []
    for path in sorted(folder.glob("*.md")):
        rel = f"{DIR}/{path.name}"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_FILE:
                raise ValueError(f"文件太大（超过 {MAX_FILE} 字节）")
            text = path.read_text(encoding="utf-8")
            out.append(parse_requirement(text, rel))
            out[-1].text = text
        except (OSError, ValueError) as e:
            out.append(Requirement(id="", title=path.name, status="", line="", description="", flow_text="", file=rel,
                                   errors=[f"读不了：{e}"]))
    ids: dict[str, str] = {}
    for r in out:
        if r.id and r.id in ids:
            r.errors.append(f"编号 {r.id} 和 {ids[r.id]} 重复")
        ids.setdefault(r.id, r.file)
    return sorted(out, key=lambda r: (r.id or "~", r.file))


# ---------- 写文件 ----------

def render_requirement(r: Requirement) -> str:
    body = f"# {r.title}\n\n- 编号：{r.id}\n- 状态：{r.status}\n- 功能线：{r.line}\n\n## 说明\n\n{r.description.strip()}\n\n"
    return body + f"## 设计的流程\n\n```flow\n{r.flow_text.strip(chr(10))}\n```\n"


def next_id(existing: list[Requirement]) -> str:
    numbers = [int(r.id[1:]) for r in existing if _ID.match(r.id)]
    return f"R{max(numbers, default=0) + 1:03d}"


def _slug(title: str) -> str:
    s = re.sub(r'[\s/\\:*?"<>|.\x00-\x1f]+', "-", title).strip("-")
    return s[:40] or "需求"


def save_requirement(root: Path, *, title: str, line: str, description: str, flow: str, status: str = "草稿",
                     id: str | None = None) -> Requirement:
    """新建（id 不给）或整条改写（id 给出、已经存在）一条需求，写进 <root>/.pystepmap/requirements/。这是唯一写
    项目文件的地方。标题、状态、流程不合法时抛 RequirementError，什么都不写。返回写下的需求。"""
    title, line = " ".join(title.split()), " ".join(line.split())
    if not title:
        raise RequirementError("需求没有标题")
    if status not in STATUSES:
        raise RequirementError(f"状态要是 {' / '.join(STATUSES)} 之一，现在是 {status!r}")
    _, errors = parse_flow(flow)
    if errors:
        raise RequirementError("设计的流程读不懂：" + "；".join(errors[:8]))
    if "```" in flow or "```" in description:
        raise RequirementError("说明和流程里不要写 ``` 代码块围栏")
    existing = _load_own(root)
    if id is not None and not any(r.id == id for r in existing) and any(r.id == id for r in load_openspec(root)):
        raise RequirementError(f"{id} 写在 OpenSpec 的文件里：直接改那个文件里的 flow 代码块，save_requirement 不写 OpenSpec 的文件")
    folder = root / DIR
    if folder.is_symlink() or (root / ".pystepmap").is_symlink():
        raise RequirementError(f"{DIR} 是一个链接，不往里写")
    if id is None:
        new = Requirement(next_id(existing), title, status, line, description, flow)
        target = folder / f"{new.id}-{_slug(title)}.md"
    else:
        found = [r for r in existing if r.id == id]
        if not found:
            raise RequirementError(f"没有编号是 {id} 的需求（新建时不要给编号）")
        new = Requirement(id, title, status, line, description, flow)
        target = root / found[0].file
    folder.mkdir(parents=True, exist_ok=True)
    target.write_text(render_requirement(new), encoding="utf-8")
    new.file = str(target.relative_to(root))
    new.items, _ = parse_flow(flow)
    return new


# ---------- 对照 ----------

Step = tuple[str, str, str]  # (类型, 配对用的标题, 判断走的分支 yes / no；其余为空)


def design_paths(items: list[Item]) -> tuple[list[tuple[list[Step], Item | None]], bool]:
    """设计的流程里的每一条路：(依次经过的节点, 走到的结局；走到末尾没有结局时 None)。循环体按走一遍算，再加一条
    不进循环体的路。第二项：路太多（超过 MAX_PATHS）被截断了没有。"""
    done: list[tuple[list[Step], Item | None]] = []
    cut = False

    def walk(seq: list[Item], prefix: list[Step], rest: list[list[Item]]) -> None:
        nonlocal cut
        if len(done) >= MAX_PATHS:
            cut = True
            return
        for i, it in enumerate(seq):
            after = [seq[i + 1:], *rest]
            if it.kind == "end":
                done.append((prefix, it))
                return
            if it.kind == "step":
                prefix = [*prefix, ("step", _norm(it.title), "")]
                if it.fail:
                    walk(it.fail, prefix, after)
            elif it.kind == "loop":
                here = [*prefix, ("loop", _norm(it.title), "")]
                if it.body:
                    walk(it.body, here, after)
                prefix = here
            else:
                walk(it.yes, [*prefix, ("cond", _norm(it.title), "yes")], after)
                walk(it.no, [*prefix, ("cond", _norm(it.title), "no")], after)
                return
        if rest:
            walk(rest[0], prefix, rest[1:])
        else:
            done.append((prefix, None))

    walk(items, [], [])
    return done, cut


def _all_items(items: list[Item]) -> list[Item]:
    out: list[Item] = []
    for it in items:
        out.append(it)
        out += _all_items(it.yes) + _all_items(it.no) + _all_items(it.body) + _all_items(it.fail)
    return out


def find_line(graph: ProjectGraph, name: str):
    """需求里写的功能线 → 图上的功能线：sid、标题（含手工没改过的完整标题）、入口函数的限定名，按这个顺序找；
    找不到、或者对上不止一条时 None。"""
    name = name.strip()
    if not name:
        return None
    for pick in (lambda ln: ln.sid == name,
                 lambda ln: _norm(name) in (_norm(ln.title), _norm(ln.title_full or "")),
                 lambda ln: graph.functions[ln.root.function].qualname == name):
        hits = [ln for ln in graph.lines if pick(ln)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
    return None


def _line_functions(graph: ProjectGraph, line) -> list[FunctionGraph]:
    out, stack = [], [line.root]
    while stack:
        task = stack.pop()
        if task.function in graph.functions and not task.goto:
            out.append(graph.functions[task.function])
        stack += task.children
    return out


def _shown(node) -> bool:
    """实现里算数的节点：步骤、判断、循环、写出来的结局（隐含的"执行到函数末尾""未捕获异常退出"不算）。"""
    if node.type == "end":
        return node.sid not in ("end:implicit", "end:uncaught") and node.end_kind != "exception"
    return node.type in ("step", "cond", "loop")


def _order_holds(fg: FunctionGraph, steps: list[Step], end: Item) -> bool:
    """根函数里有没有一条路：按 steps 的顺序经过这些节点（判断从写明的那个分支出去），最后到 end 这个结局。实现里
    多出来的节点可以夹在中间。状态 (节点, i)：到了这个节点，它前面已经按顺序对上了 steps 的前 i 个。"""
    by_id = {n.id: n for n in fg.nodes}
    outs: dict[str, list] = {}
    for e in fg.edges:
        outs.setdefault(e.src, []).append(e)
    start = next((n.id for n in fg.nodes if n.type == "start"), None)
    if start is None:
        return False
    want_end = _norm(end.title)
    seen = {(start, 0)}
    queue = deque(seen)
    while queue:
        nid, i = queue.popleft()
        node = by_id[nid]
        if node.type == "end":
            if i == len(steps) and _norm(node.title) == want_end:
                return True
            continue
        moves = [(e.dst, i) for e in outs.get(nid, [])]  # 不把这个节点算作设计里的下一个
        if i < len(steps) and (node.type, _norm(node.title)) == steps[i][:2]:
            branch = steps[i][2]  # 是设计里的下一个：判断只从写明的分支出去
            moves += [(e.dst, i + 1) for e in outs.get(nid, []) if not branch or e.branch == branch]
        for state in moves:
            if state not in seen and state[0] in by_id:
                seen.add(state)
                queue.append(state)
    return False


def _path_text(steps: list[Step], end: Item) -> str:
    parts = [f"{t}{'（是）' if b == 'yes' else '（否）' if b == 'no' else ''}" for _, t, b in steps]
    return " → ".join([*parts, f"结局「{end.title}」"])


def _misplaced(fg: FunctionGraph, broken: list[tuple[list[Step], Item]]) -> tuple[str, str] | None:
    """几条路都走不通时，找那个"放错了位置"的节点：每条走不通的路上都有它，把它从路上拿掉之后每条路都走得通。
    返回它的（类型, 归一化的标题）；找不到这样的一个节点时 None（各条路照旧分别报）。按第一条路上的顺序找。"""
    shared = set.intersection(*({st[:2] for st in steps} for steps, _ in broken))
    for kind, title, _ in broken[0][0]:
        if (kind, title) in shared and all(
                _order_holds(fg, [st for st in steps if st[:2] != (kind, title)], end) for steps, end in broken):
            return kind, title
    return None


def _misplaced_difference(moved: tuple[str, str], in_root: dict, designed: list[Item],
                          broken: dict[str, tuple[list[Step], Item]]) -> Difference:
    """"放错位置"的一句话：设计里它前后是谁、实现里它在第几行、因此几条路走不通（附最短的一条当例子）。"""
    kind, key = moved
    title = next((it.title for it in designed if (it.kind, _norm(it.title)) == moved), key)
    names = {(it.kind, _norm(it.title)): it.title for it in designed}
    steps = next(steps for steps, _ in broken.values() if moved in {st[:2] for st in steps})
    at = next(i for i, st in enumerate(steps) if st[:2] == moved)
    before = f"「{names.get(steps[at - 1][:2], steps[at - 1][1])}」之后" if at else "最前面"
    after = f"、「{names.get(steps[at + 1][:2], steps[at + 1][1])}」之前" if at + 1 < len(steps) else ""
    node = in_root[moved]
    example = min(broken, key=len)
    return Difference(
        "order", f"走向不一致：{TYPE_NAMES[kind]}「{title}」的位置和设计不一样——设计里它在{before}{after}，实现里它在第 {node.line} 行；"
                 f"因此设计的 {len(broken)} 条路按顺序走不通，例如：{example}", kind, title, node.sid)


def compare(req: Requirement, graph: ProjectGraph) -> Comparison:
    """需求的设计流程和图上的实现对照。req.errors 不为空时不该调用（设计读不懂，没法比）。"""
    out = Comparison()
    designed = _all_items(req.items)
    for it in designed:
        if it.kind == "end":
            out.outcomes.append((it.title, it.end_kind, ""))
    line = find_line(graph, req.line)
    if line is None:
        why = "需求里没有写功能线" if not req.line.strip() else f"图上找不到功能线「{req.line}」（还没有实现，或者名字对不上、对上不止一条）"
        out.differences.append(Difference("no_line", why))
        out.differences += [Difference("missing", f"{TYPE_NAMES[it.kind]}「{it.title}」还没有实现", it.kind, it.title)
                            for it in designed]
        return out
    out.line_sid, out.line_title = line.sid, line.title
    root = graph.functions[line.root.function]
    everywhere: dict[tuple[str, str], list] = {}
    for fg in _line_functions(graph, line):
        for n in fg.nodes:
            if _shown(n):
                everywhere.setdefault((n.type, _norm(n.title)), []).append(n)
    in_root = {(n.type, _norm(n.title)): n for n in reversed(root.nodes) if _shown(n)}
    wanted = {(it.kind, _norm(it.title)) for it in designed}
    # 入口函数没有写 return 的那条路（没有返回内容的接口）：结局是隐含的"执行到函数末尾"。设计里照这个标题写了才配它；
    # 没写时不算实现里多出来的（_shown 不认它）
    fell = next((n for n in root.nodes if n.sid == "end:implicit"), None)
    if fell is not None and ("end", _norm(fell.title)) in wanted:
        everywhere.setdefault(("end", _norm(fell.title)), []).append(fell)
        in_root.setdefault(("end", _norm(fell.title)), fell)

    missing: set[tuple[str, str]] = set()
    for it in designed:
        key = (it.kind, _norm(it.title))
        hits = everywhere.get(key, [])
        if not hits:
            if key not in missing:
                # 实现里成功的那条路没有写 return：标题对不上多半是因为这个，说明两种写法
                hint = (f"（实现里入口函数走到末尾没有写 return，那个结局的标题是「{fell.title}」：设计里写成 "
                        f"`结局（成功）：{fell.title}`，或者在代码末尾写 return outcome(\"ok\", title=\"{it.title}\")）"
                        if it.kind == "end" and it.end_kind == "ok" and fell is not None
                        and ("end", _norm(fell.title)) not in wanted else "")
                out.differences.append(Difference("missing", f"设计里的{TYPE_NAMES[it.kind]}「{it.title}」在实现里没有{hint}",
                                                  it.kind, it.title))
            missing.add(key)
        elif it.kind == "end" and all(n.end_kind != it.end_kind for n in hits):
            have = hits[0]
            out.differences.append(Difference(
                "kind", f"结局「{it.title}」设计的是{END_NAMES[it.end_kind]}，实现的是{END_NAMES.get(have.end_kind, have.end_kind)}",
                "end", it.title, have.sid))
    out.outcomes = [(t, k, next((n.sid for n in everywhere.get(("end", _norm(t)), [])), "")) for t, k, _ in out.outcomes]
    for key, n in in_root.items():
        if key not in wanted and _CJK.search(n.title):
            out.differences.append(Difference("extra", f"实现里的{TYPE_NAMES[n.type]}「{n.title}」设计里没有", n.type, n.title, n.sid))

    paths, cut = design_paths(req.items)
    if cut:
        out.notes.append(f"设计的流程展开超过 {MAX_PATHS} 条路，走向没有比全")
    broken: dict[str, tuple[list[Step], Item]] = {}  # 走不通的路：文字 -> (路上的节点, 结局)，按设计里的顺序
    for steps, end in paths:
        if end is None:
            out.notes.append("设计里有一条路走到末尾没有写结局，这条路的走向没有比")
            continue
        if ("end", _norm(end.title)) not in in_root:
            continue  # 这个结局配不上（已经在 missing 里），或者在派发出去的任务里：走向只在根函数里比
        # 路上有实现里没有的判断时不比：实现里没有它，它的两个分支分不开，拿掉它再比会把两个分支的内容都要求在同一条路上，
        # 走不通又会算到它前面那个位置没错的判断头上。这个判断已经报了 missing，实现之后再对照
        gone = next((st for st in steps if st[0] == "cond" and st[:2] in missing), None)
        if gone is not None:
            title = next(it.title for it in designed if (it.kind, _norm(it.title)) == gone[:2])
            out.notes.append(f"设计里的判断「{title}」在实现里没有，经过它的路走向没有比（实现了这个判断再对照）")
            continue
        # 路上配不上的步骤 / 循环（已经在 missing 里）、在派发出去的任务里的节点先拿掉，剩下的照样要按顺序走得通
        steps = [st for st in steps if (st[0], st[1]) in in_root]
        if not _order_holds(root, steps, end):
            broken.setdefault(_path_text(steps, end), (steps, end))
    moved = _misplaced(root, list(broken.values())) if len(broken) > 1 else None
    if moved is not None:
        out.differences.append(_misplaced_difference(moved, in_root, designed, broken))
    else:
        out.differences += [Difference("order", f"走向不一致：设计的这条路在实现里走不通——{text}", "end", end.title)
                            for text, (_, end) in broken.items()]
    if designed and len(missing) == len(wanted):
        out.notes.insert(0, "设计的节点一个都没配上：对照按标题配对，多半不是没实现，而是标题对不上——这条功能线的代码没有用 pystepkit "
                            "标记时，图上的标题来自文档注释或代码原文；用 get_flow 看图上现在的标题，把设计里的标题写成和它一样"
                            "（或者给代码加上 @step / @condition / outcome 的 title）")
    out.notes = list(dict.fromkeys(out.notes))
    return out


def reachable_status(req: Requirement, cmp: Comparison | None, covered: bool | None = None) -> str:
    """按对照结果，这条需求现在够得上的状态：设计读不懂、或者还是草稿 → 原样；对照有差异 → 已确认；没有差异 →
    已实现；并且设计的每个结局都有测试走到（covered 为 True）→ 已完成。covered 为 None 表示不知道（没跑过测试）。"""
    if req.errors or cmp is None or req.status not in STATUSES or req.status == "草稿":
        return req.status
    if not cmp.matches:
        return "已确认"
    return "已完成" if covered else "已实现"


# ---------- 设计的流程 → 图（给平台的画布画）----------

NO_END_TITLE = "（设计里没有写结局）"


def design_graph(reqs: list[Requirement]) -> ProjectGraph:
    """把读得懂的需求的设计流程变成一张图：每条需求一个函数图、一条"功能线"（分组"需求"），节点、连线的类型和代码
    分析出来的图一样，平台的画布可以直接画。函数的 key / sid 是"<需求文件>::<编号>"，节点的 sid 是"<类型>:L<流程
    文本里的行号>"（start、end:implicit 同代码的图），节点的行号都是 0（没有源码可看）。读不懂的需求不在图里。"""
    from .model import SCHEMA_VERSION, Edge, FlowLine, LineTask, Node, SelfCheck

    functions: dict[str, FunctionGraph] = {}
    lines: list[FlowLine] = []
    for r in reqs:
        if r.errors or not r.id:
            continue
        nodes: list[Node] = [Node("n1", "start", r.title, sid="start")]
        edges: list[Edge] = []

        def add(node_type: str, title: str, sid: str, pending: list[tuple[str, str, str]], *, nodes=nodes, edges=edges,
                **attrs) -> str:
            nid = f"n{len(nodes) + 1}"
            nodes.append(Node(nid, node_type, title, sid=sid, **attrs))
            edges.extend(Edge(src, nid, kind, branch=branch) for src, kind, branch in pending)
            return nid

        def emit(items: list[Item], pending: list[tuple[str, str, str]], *, add=add,
                 edges=edges) -> list[tuple[str, str, str]]:
            """依次放 items，pending（来源节点、连线类型、分支）接到第一个节点上；返回走完之后还悬空的出边。"""
            for it in items:
                sid = f"{it.kind}:L{it.line}"
                if it.kind == "end":
                    add("end", it.title, sid, pending, end_kind=it.end_kind)
                    return []
                if it.kind == "step":
                    nid = add("step", it.title, sid, pending)
                    pending = [(nid, "next", ""), *(emit(it.fail, [(nid, "raise", "")]) if it.fail else [])]
                elif it.kind == "loop":
                    nid = add("loop", it.title, sid, pending)
                    if it.body:
                        edges.extend(Edge(src, nid, "loop_back") for src, _, _ in emit(it.body, [(nid, "loop_each", "")]))
                    pending = [(nid, "loop_exit", "")]
                else:
                    nid = add("cond", it.title, sid, pending)
                    pending = [*emit(it.yes, [(nid, "yes", "yes")]), *emit(it.no, [(nid, "no", "no")])]
            return pending

        left = emit(r.items, [("n1", "next", "")])
        if left:
            add("end", NO_END_TITLE, "end:implicit", left, end_kind="ok")
        key = f"{r.file}::{r.id}"
        functions[key] = FunctionGraph(key=key, file=r.file, qualname=r.id, line=0, end_line=0, kind="workflow",
                                       title=r.title, is_async=False, sid=key, marked=True, group="需求", nodes=nodes,
                                       edges=edges)
        lines.append(FlowLine(id=key, title=r.title, group="需求", kind="workflow", root=LineTask(function=key),
                              sid=f"line:{key}"))
    return ProjectGraph(schema_version=SCHEMA_VERSION, root="", functions=functions, lines=lines, common_tasks=[],
                        selfcheck=SelfCheck([], [], 0, 0, []), file_hashes={}, sids={k: k for k in sorted(functions)})
