"""大纲的读法检查（test_outline 和真实项目的检查脚本共用，不依赖 outline 模块的内部）。

照读者的读法把大纲的每一行对回图里的连线，读错的列出来。每个节点（开始节点除外）的每条不是 raise 的出边，都要按
（终点、类型、分支）对上大纲里的一处写法：
- 带分支名的行（"是 →""否 →""否则 →""`模式` →""都不匹配 →""每次 →""循环结束 →"……）：分支名要和它所属那一行的节点
  （上一级那一行；同级链上的"否则 →"属于前一个"否则 →"行；挂在函数头下面的属于开始节点）到这一行终点的某条连线
  相符，是跳转行时跳转的说法也要相符。"出错 →"行对的是 raise 连线（终点相符即可）。
- 节点下面紧跟着的、深一级的不带分支名的"↪"行：它另外的出路（"出错 →"块末尾的"↪"属于那一块，不算）。
- 节点那一组之后同一缩进的不带分支名的"↪"行：它的去处。
- 不写的：只有顺着结构走的连线（next、yes、no、case、loop_each、loop_exit、不带分支的 loop_back）可以不写，而且终点
  必须是读者推断的"接下来"：那一组之后同一缩进、没有分支名的下一行；这一块到头时，"每次 →"块回到循环节点，别的块接到
  它所属那一行那一组之后（再往外推），挂在函数头下面的块接到主干上的下一行。match 的非兜底分支必须写出来。
跳转的说法："跳出循环，"对 break，"进入 finally …，"对 finally（break / continue 穿过时"再跳出循环 / 再回到循环"要和
finally 体出口的 break / loop_back 相符），其余不带说法；"回到"的终点在前面画过，"到"的在后面。
每个不带分支名的"↪"行都要归到某个节点。另外：读者推断的"接下来"必须是真实的后继；捕获只能写在"出错 →"后面；循环体
（自然循环）的节点要写在循环那一组里。异常（raise 连线）只查写出来的"出错 →"行对不对，不查没写的。
"走不到的节点"一块和"未捕获异常退出"那一行不检查；"（以下分支没有节点，原顺序未知）"这样的说明行跳过。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pystepmap.model import Edge, FunctionGraph

_JUMP = re.compile(r"↪ (跳出循环，|进入 finally(?: 再(?:跳出循环|回到循环|跳出循环或回到循环))?，)?(回到|到) \[([^\]]+)\]")
_NODE = re.compile(r"\[([^\]]+)\]")
_NOTES = ("（以下分支没有节点，原顺序未知）",)  # 说明行：不是节点、不是跳转，读的时候跳过
_STRUCTURAL = frozenset({"next", "yes", "no", "case", "loop_each", "loop_exit"})


@dataclass
class Row:
    level: int
    prefix: str | None  # 分支名（是、否、出错、每次、否则、`模式`……）；没有为 None
    kind: str  # node / jump / other
    sid: str | None
    lead: str = ""  # 跳转的说法："跳出循环，""进入 finally …，"或空
    verb: str = ""  # 跳转的动词：回到 / 到


def parse(lines: list[str]) -> list[Row]:
    """函数大纲（function_outline 的结果，depth=0）的各行，不含函数头和说明行。"""
    rows = []
    for raw in lines[1:]:
        level = (len(raw) - len(raw.lstrip(" "))) // 2
        text = raw.strip()
        if text in _NOTES:
            continue
        prefix = None
        if not text.startswith(("[", "↪")):
            head, sep, rest = text.partition(" → ")
            if not sep or not rest.startswith(("[", "↪")):
                rows.append(Row(level, None, "other", None))
                continue
            prefix, text = head, rest
        if text.startswith("↪"):
            m = _JUMP.match(text)
            if m:
                rows.append(Row(level, prefix, "jump", m.group(3), m.group(1) or "", m.group(2)))
            else:
                rows.append(Row(level, prefix, "jump", None))
        else:
            m = _NODE.match(text)
            rows.append(Row(level, prefix, "node", m.group(1) if m else None))  # sid 为 None：看不懂的节点行
    return rows


def _natural_loop(fg: FunctionGraph, loop: str) -> set[str]:
    """循环 loop 的循环体（自然循环）：起点受 loop 支配的、回到 loop 的连线（回边），从回边起点倒着走、不经过 loop
    能到的节点。支配关系按从开始节点出发的所有连线算（集合迭代，用例不大）。"""
    ids = [n.id for n in fg.nodes]
    start = next(n.id for n in fg.nodes if n.type == "start")
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in fg.edges:
        preds[e.dst].add(e.src)
    dom = {i: set(ids) for i in ids}
    dom[start] = {start}
    changed = True
    while changed:
        changed = False
        for i in ids:
            if i == start:
                continue
            ps = [dom[p] for p in preds[i]]
            new = ({i} | set.intersection(*ps)) if ps else {i}
            if new != dom[i]:
                dom[i], changed = new, True
    body: set[str] = set()
    stack = [p for p in preds[loop] if p != loop and loop in dom[p]]
    while stack:
        u = stack.pop()
        if u != loop and u not in body:
            body.add(u)
            stack += list(preds[u])
    return body


def _is_match(fg: FunctionGraph, node) -> bool:
    if node.type != "cond" or not node.subtitle.startswith("match "):
        return False
    return not any(e.src == node.id and e.kind != "raise" and (e.kind in ("yes", "no") or e.label in ("是", "否"))
                   for e in fg.edges)


def _is_else(e: Edge) -> bool:
    return e.branch == "else" or (not e.branch and e.label == "都不匹配")


def _label_fits(fg: FunctionGraph, src, e: Edge, prefix: str) -> bool:
    """带分支名的行的分支名和 src 的出边 e 是否相符。"""
    if prefix == "出错":
        return e.kind == "raise"
    if e.kind == "raise":
        return False
    if src.type == "loop":
        return {"每次": e.kind != "loop_exit", "循环结束": e.kind == "loop_exit"}.get(prefix, False)
    if src.type == "cond" and _is_match(fg, src):
        if prefix == "都不匹配":
            return _is_else(e)
        return not _is_else(e) and prefix == f"`{e.label_full or e.label}`"
    if src.type == "cond":
        which = e.branch or {"yes": "yes", "no": "no"}.get(e.kind) or {"是": "yes", "否": "no"}.get(e.label, "")
        return {"是": "yes", "否": "no", "否则": "no"}.get(prefix) == which
    words = {"next": "接着", "loop_each": "每次", "loop_exit": "循环结束", "loop_back": "回到循环", "break": "跳出循环",
             "finally": "进入 finally", "yes": "是", "no": "否"}
    return words.get(e.kind) == prefix


def _after_finally(fg: FunctionGraph, outs: dict[str, list[Edge]], fin: str, many: bool = False,
                   seen: frozenset[str] = frozenset()) -> str:
    """break / continue 穿过 finally、进入 finally 体 fin 之后做什么（"跳出循环""回到循环""跳出循环或回到循环"，说不准
    为空）：顺着唯一的 next 走到 finally 体的出口，看它不带分支的 break / loop_back 出边；出口没有 next 时 loop_back 是
    正常走完（continue 和它合成一条），只有一个跳转穿过时有 break 就是 break；出口再进外层 finally 时看外层的。"""
    nodes = {n.id: n for n in fg.nodes}
    jumps = [e for e in fg.edges if e.dst == fin and e.kind == "finally" and nodes[e.src].type != "end"]
    many = many or len(jumps) > 1
    cur = fin
    while True:
        if cur in seen:
            return ""
        seen = seen | {cur}
        normal = [e for e in outs[cur] if e.kind != "raise"]
        legs = [e for e in normal if e.kind in ("break", "loop_back", "finally") and not (e.branch or e.label)]
        if legs:
            break
        if len(normal) != 1 or normal[0].kind != "next":
            return ""
        cur = normal[0].dst
    if any(e.kind == "finally" for e in legs):
        outer = {e.dst for e in legs if e.kind == "finally"}
        if len(outer) != 1 or any(e.kind != "finally" for e in legs):
            return ""
        return _after_finally(fg, outs, outer.pop(), many, seen)
    brk = any(e.kind == "break" for e in legs)
    back = any(e.kind == "loop_back" for e in legs)
    if not many:
        return "跳出循环" if brk else "回到循环" if back else ""
    return "或".join(word for word, has in (("跳出循环", brk), ("回到循环", back)) if has)


def _wording_fits(fg: FunctionGraph, outs: dict[str, list[Edge]], e: Edge, row: Row) -> bool:
    """跳转行的说法和连线 e 是否相符："跳出循环，"对 break；"进入 finally …，"对 finally，break / continue 穿过 finally
    时"再…"要和 finally 体出口的去处相符（return / raise 穿过的不说）；其余不带说法。"""
    if row.lead.startswith("跳出循环"):
        return e.kind == "break"
    if row.lead.startswith("进入 finally"):
        if e.kind != "finally":
            return False
        src = next(n for n in fg.nodes if n.id == e.src)
        after = "" if src.type == "end" else _after_finally(fg, outs, e.dst)
        return row.lead == (f"进入 finally 再{after}，" if after else "进入 finally，")
    return e.kind not in ("break", "finally")


def _quiet_ok(e: Edge) -> bool:
    """可以不写的连线：顺着结构走的。"""
    return e.kind in _STRUCTURAL or (e.kind == "loop_back" and not (e.branch or e.label))


def misreadings(fg: FunctionGraph, lines: list[str]) -> list[str]:
    """function_outline(depth=0) 的结果 lines 里读者会读错的地方，每处一句。"""
    sid_of = {n.id: n.sid or n.id for n in fg.nodes}
    by_sid = {sid_of[n.id]: n for n in fg.nodes}
    start = next((n for n in fg.nodes if n.type == "start"), None)
    uncaught = {sid_of[n.id] for n in fg.nodes if n.type == "end" and n.end_kind == "exception" and not n.line}
    rows = parse(lines)
    cut = next((i for i, r in enumerate(rows) if r.kind == "other"), len(rows))  # "走不到的节点"之后不看
    rows = [r for r in rows[:cut] if not (r.kind == "node" and r.sid in uncaught)]
    first = {}
    for i, r in enumerate(rows):
        if r.kind == "node":
            first.setdefault(r.sid, i)
    outs: dict[str, list[Edge]] = {n.id: [] for n in fg.nodes}
    for e in fg.edges:
        if e.dst != e.src:
            outs[e.src].append(e)
    problems: list[str] = []

    def group_end(i: int) -> int:
        j = i + 1
        while j < len(rows) and rows[j].level > rows[i].level:
            j += 1
        return j

    def opener(i: int) -> int | None:
        k = i
        while k >= 0 and rows[k].level >= rows[i].level:
            if rows[k].level == rows[i].level and rows[k].prefix is not None:
                return k
            k -= 1
        return None

    def up(i: int) -> int | None:
        k = i - 1
        while k >= 0 and rows[k].level >= rows[i].level:
            k -= 1
        return k if k >= 0 else None

    def after_group(i: int) -> tuple[str, str | None, bool]:
        """第 i 行那一组之后读者以为接着的：(种类, sid, 是不是紧跟在这一组之后同一缩进的那一行)。种类是 node / jump，
        或 end（函数结束、说不准）。"""
        own = True
        for _ in range(len(rows) + 1):
            j = group_end(i)
            if j < len(rows) and rows[j].level == rows[i].level and rows[j].prefix is None:
                return rows[j].kind, rows[j].sid, own
            own = False
            s = opener(i)
            if s is None:
                return "end", None, own
            p = up(s)
            if p is None:  # 挂在函数头（开始节点）下面的块：接着的是它之后主干上的第一行
                k = s + 1
                while k < len(rows) and rows[k].level >= rows[s].level:
                    k += 1
                if k < len(rows) and rows[k].level == rows[s].level - 1 and rows[k].prefix is None:
                    return rows[k].kind, rows[k].sid, own
                return "end", None, own
            if rows[s].prefix == "每次":
                return "node", rows[p].sid, own
            i = p
        return "end", None, own

    def owner(i: int):
        """带分支名的第 i 行属于哪个节点（同级链上的"否则 →"属于前一个"否则 →"行；挂在函数头下面的属于开始节点）。"""
        r = rows[i]
        if r.prefix == "否则":
            k = i - 1
            while k >= 0 and rows[k].level > r.level:
                k -= 1
            if k >= 0 and rows[k].level == r.level and rows[k].prefix == "否则" and rows[k].kind == "node":
                return by_sid.get(rows[k].sid)
        p = up(i)
        if p is None:
            return start
        return by_sid.get(rows[p].sid) if rows[p].kind == "node" else None

    accounted: dict[str, set[int]] = {n.id: set() for n in fg.nodes}  # 节点 -> 已经对上的出边（outs 里的下标）
    used_jumps: set[int] = set()

    def claim(src, target: str | None, fits) -> bool:
        """在 src 的出边里找一条终点是 target、fits 成立、还没对上的，记下来。"""
        for k, e in enumerate(outs[src.id]):
            if k not in accounted[src.id] and sid_of[e.dst] == target and fits(e):
                accounted[src.id].add(k)
                return True
        return False

    # 1. 带分支名的行
    for i, r in enumerate(rows):
        if r.prefix is None or r.kind not in ("node", "jump"):
            continue
        src = owner(i)
        if src is None:
            problems.append(f"第 {i + 2} 行“{r.prefix} →”找不到属于哪个节点")
            continue
        ok = claim(src, r.sid, lambda e, r=r, src=src: _label_fits(fg, src, e, r.prefix)
                   and (r.kind != "jump" or r.prefix == "出错" or _wording_fits(fg, outs, e, r)))
        if not ok:
            problems.append(f"{sid_of[src.id]}: “{r.prefix} → {r.sid}”对不上它的连线")
        if r.kind == "jump":
            used_jumps.add(i)

    # 2. 节点行：另外的出路、去处、不写的
    for i, r in enumerate(rows):
        if r.kind != "node" or r.sid not in by_sid:
            continue
        x = by_sid[r.sid]
        if x.type == "catch" and r.prefix != "出错":
            problems.append(f"{r.sid}: 捕获没有写在“出错 →”后面")
        j = i + 1
        while j < len(rows) and rows[j].level == r.level + 1 and rows[j].prefix is None and rows[j].kind == "jump":
            if not claim(x, rows[j].sid, lambda e, row=rows[j]: e.kind != "raise" and _wording_fits(fg, outs, e, row)):
                problems.append(f"{r.sid}: 下面的“↪ {rows[j].verb} {rows[j].sid}”对不上它的连线")
            used_jumps.add(j)
            j += 1
        kind, nxt, own = after_group(i)
        if kind == "jump" and own:
            k = group_end(i)
            if not claim(x, nxt, lambda e, row=rows[k]: e.kind != "raise" and _wording_fits(fg, outs, e, row)):
                problems.append(f"{r.sid}: 之后的“↪ {rows[k].verb} {nxt}”对不上它的连线")
            used_jumps.add(k)
        inferred = nxt if kind == "node" else None
        match = _is_match(fg, x)
        rest = [k for k, e in enumerate(outs[x.id]) if e.kind != "raise" and k not in accounted[x.id]]
        for k in rest:
            e = outs[x.id][k]
            quiet = _quiet_ok(e) and sid_of[e.dst] == inferred and not (match and not _is_else(e))
            if not quiet:
                problems.append(f"{r.sid}: 出路 {e.kind}/{e.branch or e.label or '-'} → {sid_of[e.dst]} 没写出来"
                                f"（读者以为接着 {inferred}）")
        real = {sid_of[e.dst] for e in outs[x.id] if e.kind != "raise"}
        if x.type not in ("cond", "loop", "end") and real and inferred is not None and inferred not in real:
            problems.append(f"{r.sid}: 读者以为接着 {inferred}，实际是 {sorted(real)}")
        if x.type == "loop":
            body = {sid_of[b] for b in _natural_loop(fg, x.id)}
            inside = {rows[c].sid for c in range(i + 1, group_end(i)) if rows[c].kind == "node"}
            placed = {row.sid for row in rows if row.kind == "node"}
            for b in sorted(body & placed - inside):
                problems.append(f"{r.sid}: 循环体节点 {b} 写在了循环外面")

    # 3. 跳转行：都归到了节点；回到 / 到 和画的先后相符；看不懂的行
    for i, r in enumerate(rows):
        if r.kind == "node" and r.sid not in by_sid:
            problems.append(f"第 {i + 2} 行看不懂（不是这个函数的节点）")
        if r.kind != "jump":
            continue
        if r.sid is None:
            problems.append(f"第 {i + 2} 行的“↪”看不懂")
            continue
        if i not in used_jumps:
            problems.append(f"第 {i + 2} 行“↪ {r.verb} {r.sid}”不属于任何节点")
        drawn = first.get(r.sid)
        if drawn is not None and (r.verb == "回到") != (drawn < i):
            problems.append(f"第 {i + 2} 行“↪ {r.verb} {r.sid}”：终点{'在后面' if drawn > i else '已经画过'}")
    return problems
