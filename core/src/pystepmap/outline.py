"""给 AI 读的流程文本：一个函数、一条功能线写成缩进的大纲（function_outline / line_outline），或画成 Mermaid 流程图
（line_mermaid）。纯函数，只读图。

大纲的写法：
- 函数头一行"<种类> <限定名>  <文件>:<def 行>  [<函数 sid>]"（种类：接口 / 任务 / 工作流 / 函数），开始节点就是这一行。
- 其余每个节点恰好一行"[节点 sid] L行号 文字"（隐含节点没有 L）：步骤写标题（副标题不同时括号里写调用名），判断、循环、
  捕获写"判断 / 循环 / 捕获 <源码原文>"（另有标题时括号里写标题），结局写"结局·正常 / 跳过 / 失败 / 异常：标题"。
  隐含的"未捕获异常退出"不写（每个函数都有，它没有入边，任何节点都可能从它离开；[end:uncaught] 这个 sid 照样能
  用）；只有"未捕获异常退出（任务会重试）"写在函数段的最后。再是"走不到的节点："一块（有的话）。
- 缩进表示分支：判断、循环的每条出路、异常的落脚点（raise 连线，画在连线的起点下面）另起一行，以"是 →""否 →"
  "`模式` →""都不匹配 →""每次 →""循环结束 →""出错 →"开头并缩进一级，接着的节点和这一行同样缩进；分支之后的主干回到
  分出它的节点的缩进。主干在汇合点接上：分出去的几条路都走到的第一个节点（后支配点）；一条路提前结束（return、raise、
  continue、break）时不算它，汇合点就是另一条路的去处；几条路都提前结束时没有汇合点，每条都缩进着画完。循环之后的主干
  只从"循环结束"和 break 的去处算，不会落在循环体里。elif 写成"否则 →"，和第一个 if 的分支同一缩进。match 的每个
  非兜底分支都有自己的一行。
- **读者推断的"接下来"必须是真实的后继**：一块（一条分支、循环体、"出错 →"的处理）写完时，读者会以为接着的是这一块
  之后同一缩进的那一行（"每次 →"块是回到循环节点）；真实的去处不是它时写一行"↪"：已经画过的"↪ 回到 [sid] 文字"，
  还没画到的"↪ 到 …"，break、进入 finally 体分别写"↪ 跳出循环，到 …""↪ 进入 finally，到 …"；break / continue
  穿过 finally 时写明出 finally 体之后做什么："↪ 进入 finally 再跳出循环，到 …""↪ 进入 finally 再回到循环，到 …"
  （分不清时"再跳出循环或回到循环"）。节点除了往下走还有 break / continue / 进 finally 的出路时（如 finally 体的出口），
  那几条写成它下面缩进一级的"↪"行。判断的两条分支走到同一个节点、走法不同时（一条直接、一条穿过 finally），两条都算分支。
- match 的分支按分支体所在行排；没有节点的分支（pass、直接 break / continue）图里没有它在源码里的位置，排在后面，前面
  先写一行"（以下分支没有节点，原顺序未知）"，读者不要从这几行的先后推断哪个先匹配；兜底的（_、裸名字）和"都不匹配"
  最后。
- depth（限制在 0～5）：depth > 0 时步骤调用的项目内函数在这一步下面缩进一级展开（以"调用 函数名  文件:行  [函数 sid]"
  起头），被调函数里再减一；同一份大纲里一个函数只展开一次，之后写"（上面已展开）"。depth = 0（以及判断、循环上的调用）
  只在行尾标"⇒ 可展开 [函数 sid]"，被调函数里会派发任务时再注"（内部派发 …）"。派发写"⇒ 见下面 / 上面的任务 …"（功能
  线里）、"⇒ 公共任务 …（不并入本线）"、"⇒ 派发的任务 [sid]"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .build import UNCAUGHT_TITLE
from .model import Edge, FlowLine, FunctionGraph, LineTask, Node, ProjectGraph
from .query import line_functions

INDENT = "  "
MAX_DEPTH = 5
_FN_KIND = {"route": "接口", "task": "任务", "workflow": "工作流"}
_END = {"ok": "正常", "skipped": "跳过", "failed": "失败", "exception": "异常"}
_HEAD_WORD = {"cond": "判断", "loop": "循环", "catch": "捕获"}
_PLAIN = frozenset({"next", "yes", "no", "case", "loop_each", "loop_exit"})  # 走到汇合点时不用说的连线
_SIDE = frozenset({"break", "finally", "loop_back"})  # 非判断、循环节点的另外的出路：写成"↪"行
_KIND_WORD = {"next": "接着", "yes": "是", "no": "否", "case": "分支", "loop_each": "每次", "loop_exit": "循环结束",
              "loop_back": "回到循环", "break": "跳出循环", "finally": "进入 finally", "raise": "出错"}
_JUMP_LEAD = {"break": "跳出循环，", "finally": "进入 finally，"}
_ELIF = "否则 → "
_AGAIN = "\0again"  # 合流分析里"又回到了出发的节点"（循环体回到循环节点）：当作一个提前结束的去处
_IRREFUTABLE = re.compile(r"[A-Za-z_]\w*")  # 不带守卫的 _ 或裸名字：兜底的 case（None、True、False 除外）
_VALUE_NAMES = frozenset({"None", "True", "False"})
_QUIET = {"yes": "是", "no": "否"}
_UNORDERED = "（以下分支没有节点，原顺序未知）"


def node_ref(fg: FunctionGraph, node: Node) -> str:
    """节点在大纲里的引用："[<节点 sid>]"（没有 sid 的旧图用节点 id）。"""
    return f"[{node.sid or node.id}]"


def node_text(node: Node) -> str:
    """节点行里 sid、行号之后的文字（完整形式，不截断）。"""
    title = node.title_full or node.title
    sub = node.subtitle_full or node.subtitle
    if node.type == "start":
        return f"开始：{title}"
    if node.type == "end":
        return f"结局·{_END[node.end_kind]}：{title}" if node.end_kind in _END else f"结局：{title}"
    if node.type in _HEAD_WORD:
        word = _HEAD_WORD[node.type]
        if not sub:
            return title if title.startswith(word) else f"{word} {title}"
        own = title not in (sub, f"{word} {sub}")
        return f"{word} {sub}" + (f"（{title}）" if own else "")
    if node.calls and all(ref.is_dispatch for ref in node.calls):  # 派发：标题已是"派发：任务标题"，行尾再写任务名
        return title
    return title + (f"（{sub}）" if sub and sub != title else "")


def _head(fg: FunctionGraph) -> str:
    name = fg.qualname + (f"（{fg.route}）" if fg.route else "")  # 接口，以及挂在路由上的工作流、任务
    return f"{_FN_KIND.get(fg.kind, '函数')} {name}  {fg.file}:{fg.line}  [{fg.sid or fg.key}]"


def _plain(e: Edge) -> bool:
    """顺着结构走的连线；break、进入 finally、continue（带分支的 loop_back）、raise 不算。"""
    return e.kind in _PLAIN or (e.kind == "loop_back" and not (e.branch or e.label))


def _early(e: Edge) -> bool:
    """判断的一条分支直接 continue / 穿过 finally 跳走（带分支的 loop_back、finally）：合流分析里算提前结束。"""
    return e.kind in ("loop_back", "finally") and bool(e.branch or e.label)


def _dominators(root: str, succ: dict[str, list[str]]) -> dict[str, str]:
    """root 出发的图里每个节点的直接支配点（Cooper–Harvey–Kennedy 迭代）；走不到的节点不在结果里。"""
    order: list[str] = []
    seen = {root}
    stack = [(root, iter(succ.get(root, ())))]
    while stack:
        node, it = stack[-1]
        for nxt in it:
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(succ.get(nxt, ()))))
                break
        else:
            stack.pop()
            order.append(node)
    rpo = order[::-1]
    index = {n: i for i, n in enumerate(rpo)}
    preds: dict[str, list[str]] = {n: [] for n in rpo}
    for n in rpo:
        for m in succ.get(n, ()):
            preds[m].append(n)
    idom = {root: root}

    def meet(a: str, b: str) -> str:
        while a != b:
            while index[a] > index[b]:
                a = idom[a]
            while index[b] > index[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for n in rpo[1:]:
            new = None
            for p in preds[n]:
                if p in idom:
                    new = p if new is None else meet(p, new)
            if new is not None and idom.get(n) != new:
                idom[n] = new
                changed = True
    return idom


class _Fn:
    """一个函数图的索引：节点、出边、入边、生成顺序、从开始节点走得到的节点、主干的自然出口、循环体。"""

    def __init__(self, fg: FunctionGraph) -> None:
        self.fg = fg
        self.nodes = {n.id: n for n in fg.nodes}
        self.order = {n.id: i for i, n in enumerate(fg.nodes)}
        self.outs: dict[str, list[Edge]] = {n.id: [] for n in fg.nodes}
        self.ins: dict[str, list[Edge]] = {n.id: [] for n in fg.nodes}
        for e in fg.edges:
            if e.src in self.nodes and e.dst in self.nodes:
                self.outs[e.src].append(e)
                self.ins[e.dst].append(e)
        self.start = next((n.id for n in fg.nodes if n.type == "start"), None)
        self.reach: set[str] = set()
        stack = [self.start] if self.start else []
        while stack:
            cur = stack.pop()
            if cur not in self.reach:
                self.reach.add(cur)
                stack += [e.dst for e in self.outs[cur]]
        self.uncaught = [n.id for n in fg.nodes if n.type == "end" and n.end_kind == "exception" and not n.line]
        ends = [n.id for n in fg.nodes if n.type == "end" and n.id in self.reach and n.id not in self.uncaught]
        implicit = [nid for nid in ends if not self.nodes[nid].line]
        # 主干的自然出口：执行到函数末尾；没有时（每条路都 return / raise）是最后一个结局
        self.natural = implicit[0] if implicit else max(ends, key=self.order.__getitem__, default=None)
        if not implicit:  # 每条路都以结局结束、最后进同一个 finally 体（它的出口不往后接）：主干落在这个 finally 体上
            closing = [e.dst for e in fg.edges if e.kind == "finally" and e.src in ends and not self._ends_after(e.dst)]
            if closing:
                self.natural = max(closing, key=self.order.__getitem__)
        self._bodies: dict[str, set[str]] = {}
        self._idom: dict[str, str] | None = None

    def _ends_after(self, nid: str) -> bool:
        """从 nid 往后还走得到结局。"""
        seen, stack = set(), [nid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if self.nodes[cur].type == "end":
                return True
            stack += [e.dst for e in self.outs[cur]]
        return False

    def normal(self, nid: str) -> list[Edge]:
        """nid 不是 raise 的出边（自环不算）。"""
        return [e for e in self.outs[nid] if e.kind != "raise" and e.dst != nid]

    def only_from(self, nid: str, src: str) -> bool:
        """nid 只从 src 进来（不算 raise 连线）。"""
        ins = [e for e in self.ins[nid] if e.kind != "raise"]
        return bool(ins) and all(e.src == src for e in ins)

    def dominated(self, node: str, head: str) -> bool:
        """从开始节点到 node 的每条路都经过 head。"""
        if self._idom is None:
            succ = {n: [e.dst for e in es] for n, es in self.outs.items()}
            self._idom = _dominators(self.start, succ) if self.start else {}
        cur = node
        while cur in self._idom:
            if cur == head:
                return True
            nxt = self._idom[cur]
            if nxt == cur:
                return False
            cur = nxt
        return False

    def body(self, loop: str) -> set[str]:
        """循环 loop 的循环体（自然循环）：回到它的连线中起点受它支配的（回边），从这些起点倒着走、不经过它能到的节点。
        内层循环的 break 落在外层循环体末尾时画成到外层循环节点的 loop_back，外层循环节点不受内层支配，不算内层的循环体。"""
        if loop not in self._bodies:
            stack = [e.src for e in self.ins[loop] if e.src != loop and self.dominated(e.src, loop)]
            body: set[str] = set()
            while stack:
                u = stack.pop()
                if u != loop and u not in body:
                    body.add(u)
                    stack += [e.src for e in self.ins[u]]
            self._bodies[loop] = body
        return self._bodies[loop]

    def loop_exits(self, loop: str) -> list[str]:
        """循环之后的路从哪里开始：loop_exit 的去处（else 体或循环之后），和循环体里 break 跳到的循环体外的节点。"""
        body = self.body(loop)
        out = [e.dst for e in self.normal(loop) if e.kind in ("loop_exit", "break")]
        out += [x.dst for b in sorted(body, key=self.order.__getitem__) for x in self.outs[b]
                if x.kind == "break" and x.dst not in body]
        return list(dict.fromkeys(out))

    def is_match(self, node: Node) -> bool:
        """match 的判断节点（docs/graph-schema.md"读连线"第 1 点）。"""
        if node.type != "cond" or not node.subtitle.startswith("match "):
            return False
        return not any(e.kind in ("yes", "no") or e.label in ("是", "否") for e in self.outs[node.id] if e.kind != "raise")

    def is_else(self, node: Node, e: Edge) -> bool:
        """match 没有兜底分支时越过整个 match 的那一条（都不匹配）。"""
        return e.branch == "else" or (not e.branch and e.label == "都不匹配")

    def branch_word(self, node: Node, e: Edge, quoted: bool) -> str:
        """判断节点一条分支的说法：是 / 否 / `模式` / 都不匹配（没有 branch 的旧图按 kind、label 回退）。"""
        if self.is_match(node):
            if self.is_else(node, e):
                return "都不匹配"
            text = e.label_full or e.label or e.branch
            return f"`{text}`" if quoted else text
        which = e.branch or {"yes": "yes", "no": "no"}.get(e.kind) or {"是": "yes", "否": "no"}.get(e.label, "")
        return {"yes": "是", "no": "否"}.get(which, _KIND_WORD.get(e.kind, e.kind))

    def label(self, node: Node, e: Edge) -> str:
        if e.kind == "raise":
            return "出错"
        if node.type == "cond":
            return self.branch_word(node, e, quoted=True)
        if node.type == "loop":
            return "循环结束" if e.kind == "loop_exit" else "每次"
        return _KIND_WORD.get(e.kind, e.kind)

    def case_bodies(self, node: Node) -> set[str]:
        """match 节点各个非兜底分支自己的分支体的第一个节点（只从这个 match 进来的节点）。"""
        return {e.dst for e in self.normal(node.id) if not self.is_else(node, e) and self.only_from(e.dst, node.id)}

    def is_fallback(self, node: Node, e: Edge) -> bool:
        """match 的兜底分支：不带守卫的 _ 或裸名字（None、True、False 是值，不算），以及"都不匹配"。"""
        label = e.label_full or e.label
        return self.is_else(node, e) or (bool(_IRREFUTABLE.fullmatch(label)) and label not in _VALUE_NAMES)

    def case_line(self, node: Node, e: Edge, own: set[str]) -> int | None:
        """match 一个分支的分支体第一个节点所在行；分支没有自己的节点时为 None。"""
        return self.nodes[e.dst].line or None if e.dst in own else None

    def branch_order(self, node: Node, edges: list[Edge]) -> list[Edge]:
        """分支的先后：循环先"每次"后"循环结束"；if 先"是"后"否"；match 有分支体的按分支体所在行，没有的（pass、直接
        break、continue）按连线顺序排在它们后面，兜底的（_、裸名字）和"都不匹配"最后。"""
        if node.type == "loop":
            return sorted(edges, key=lambda e: e.kind == "loop_exit")
        if node.type != "cond":
            return edges
        if not self.is_match(node):
            rank = {"是": 0, "否": 1}
            return sorted(edges, key=lambda e: rank.get(self.branch_word(node, e, quoted=False), 0))
        own = self.case_bodies(node)

        def key(item: tuple[int, Edge]) -> tuple[bool, float, int]:
            i, e = item
            line = self.case_line(node, e, own)
            return self.is_fallback(node, e), float("inf") if line is None else line, i

        return [e for _, e in sorted(enumerate(edges), key=key)]

    def unordered_from(self, node: Node, ordered: list[Edge]) -> Edge | None:
        """match 里第一个没有节点的非兜底分支：从它起的几个分支在源码里的先后不知道（至少有两个非兜底分支时才要说）。"""
        if not self.is_match(node):
            return None
        own = self.case_bodies(node)
        refutable = [e for e in ordered if not self.is_fallback(node, e)]
        if len(refutable) < 2:
            return None
        return next((e for e in refutable if self.case_line(node, e, own) is None), None)

    def finally_leg(self, fin: str, many: bool = False, seen: frozenset[str] = frozenset()) -> str:
        """break / continue 穿过 finally 进入 finally 体 fin 之后做什么："跳出循环""回到循环""跳出循环或回到循环"，
        看不出来时为空。从 fin 顺着唯一的 next 走到 finally 体的出口（有 break / loop_back / finally 出边的节点）：
        出口有 next（finally 之后还有语句）时，它的 break / loop_back 出边就是穿过它的跳转的后半段；没有 next 时
        loop_back 是正常走完，穿过它的 continue 和它合成了一条。只有一个跳转穿过（many 为假）时，有 break 的后半段
        就是 break，否则是 continue。出口再进外层 finally 时接着看外层的。"""
        jumps = [e for e in self.ins[fin] if e.kind == "finally" and self.nodes[e.src].type != "end"]
        many = many or len(jumps) > 1
        cur = fin
        while True:
            if cur in seen:
                return ""
            seen = seen | {cur}
            outs = self.normal(cur)
            legs = [e for e in outs if e.kind in ("break", "loop_back", "finally") and not (e.branch or e.label)]
            if legs:
                break
            if len(outs) != 1 or outs[0].kind != "next":
                return ""
            cur = outs[0].dst
        if any(e.kind == "finally" for e in legs):  # 还要再进外层的 finally
            if any(e.kind != "finally" for e in legs):
                return ""
            outer = {e.dst for e in legs}
            return self.finally_leg(outer.pop(), many, seen) if len(outer) == 1 else ""
        brk = any(e.kind == "break" for e in legs)
        back = any(e.kind == "loop_back" for e in legs)
        if not many:
            return "跳出循环" if brk else "回到循环" if back else ""
        return "或".join(word for word, has in (("跳出循环", brk), ("回到循环", back)) if has)

    def merge(self, b: str, edges: list[Edge], stops: frozenset[str], natural: str | None,
              keep: set[str] = frozenset()) -> str | None:
        """b 的这几条出边分开的路在哪里汇合（后支配点），None 表示没有汇合点（各条路都提前结束）。

        在"从这几条出边出发、不越过 stops"的区域里找：stops 和结局是终点（结局之后进 finally 体的连线不算），回到 b
        算提前结束。先看能不能走到这一段的自然出口 natural（先不算 continue，走不到再算上），能走到就只看走得到它的
        那些路——提前结束的路不算；走不到时取生成得最晚的终点（通常是最后一个结局）。汇合点就是那个终点时：它不在
        stops 里（这一段接着画它）就返回它，否则返回 None。keep 里的节点不当汇合点（往后支配树上走）。"""
        succ: dict[str, list[tuple[str, bool]]] = {}

        def link(e: Edge) -> tuple[str, bool]:
            return (_AGAIN if e.dst == b else e.dst), _early(e)

        succ[b] = [link(e) for e in edges if e.dst != b]
        todo = [d for d, _ in succ[b]]
        while todo:
            u = todo.pop()
            if u in succ:
                continue
            if u == _AGAIN or u in stops or self.nodes[u].type == "end":  # 结局是终点：进 finally 体不算接着走
                succ[u] = []
                continue
            succ[u] = [link(e) for e in self.outs[u] if e.dst != u]
            todo += [d for d, _ in succ[u]]

        def alive(target: str, with_early: bool) -> set[str]:
            rev: dict[str, list[str]] = {}
            for u, outs in succ.items():
                if u == target:
                    continue
                for d, early in outs:
                    if with_early or not early:
                        rev.setdefault(d, []).append(u)
            seen, stack = {target}, [target]
            while stack:
                for p in rev.get(stack.pop(), []):
                    if p not in seen:
                        seen.add(p)
                        stack.append(p)
            return seen

        target, with_early, live = None, True, set()
        if natural is not None and natural in succ:
            for with_early in (False, True):
                live = alive(natural, with_early)
                if b in live:
                    target = natural
                    break
        if target is None:
            sinks = [u for u, outs in succ.items() if u not in (b, _AGAIN) and (not outs or u in stops)]
            if not sinks:
                return None
            target, with_early = max(sinks, key=self.order.__getitem__), True
            live = alive(target, True)
        rsucc: dict[str, list[str]] = {u: [] for u in live}
        for u in live:
            if u != target:
                for d, early in succ[u]:
                    if d in live and (with_early or not early):
                        rsucc[d].append(u)
        idom = _dominators(target, rsucc)
        m = idom.get(b)
        while m is not None and m in keep and m != target:
            m = idom.get(m)
        if m is None or m == b:
            return None
        if m == target:  # 汇合在终点上：是这一段接着画的节点（自然出口、最后一个结局）时就是它，是 stops 时没有
            return None if target in stops else target
        return m


@dataclass
class _Part:
    """一份大纲里待写的一个函数（功能线的一段，或步骤下展开的被调函数）：写到它时才走它的图，所以展开不递归。"""

    key: str
    level: int  # 函数头的缩进
    depth: int
    head: str  # 函数头（不含缩进）
    segment: int | None = None  # 功能线的第几段


@dataclass
class _Ctx:
    """一份大纲共用的状态。"""

    graph: ProjectGraph
    expanded: set[str] = field(default_factory=set)  # 已经写出过的函数（展开过、或是功能线的一段）
    segments: dict[str, int] | None = None  # 功能线：任务 key -> 段的序号（派发树先序，goto 不算）；单个函数时为 None
    common: frozenset[str] = frozenset()
    segment: int = 0  # 正在写第几段

    def dispatch_note(self, target: str) -> str:
        fg = self.graph.functions.get(target)
        name = fg.qualname if fg else target
        if self.segments is not None:
            if target in self.segments:
                return f"见{'下' if self.segments[target] > self.segment else '上'}面的任务 {name}"
            if target in self.common:
                return f"公共任务 {name}（不并入本线）"
        return f"派发的任务 [{fg.sid if fg and fg.sid else target}]"

    def name(self, key: str) -> str:
        fg = self.graph.functions.get(key)
        return fg.qualname if fg else key

    def write(self, parts: list[str | _Part]) -> list[str]:
        """按出现的先后写出各行：遇到待写的函数时走它的图，它里面再展开的接着按先后写（用栈，不递归）。"""
        out: list[str] = []
        stack = [iter(parts)]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
            elif isinstance(item, str):
                out.append(item)
            elif item.key in self.expanded:
                out.append(INDENT * item.level + item.head + "（上面已展开）")
            else:
                self.expanded.add(item.key)
                if item.segment is not None:
                    self.segment = item.segment
                out.append(INDENT * item.level + item.head)
                stack.append(iter(_Walk(self, self.graph.functions[item.key], item.depth, item.level + 1).run()))
        return out


class _Walk:
    """把一个函数图写成大纲的节点行（不含函数头），从缩进 base 级开始；展开的被调函数留成 _Part，由 _Ctx.write 接着写。

    chain 画一串：stops 是外层接着画的节点（走到就停），natural 是这一段的自然出口（合流分析用），after 是这一串写完后
    读者以为接着的节点（None：说不准，走到哪都要明写）。"""

    def __init__(self, ctx: _Ctx, fg: FunctionGraph, depth: int, base: int) -> None:
        self.ctx, self.fg, self.depth, self.base = ctx, fg, depth, base
        self.fn = _Fn(fg)
        self.out: list[str | _Part] = []
        self.rendered: set[str] = set()

    def run(self) -> list[str | _Part]:
        fn = self.fn
        if fn.start is not None:
            self.chain(fn.start, frozenset(), fn.natural, None, self.base, quiet=True)
        missed = [n.id for n in self.fg.nodes if n.id in fn.reach and n.id not in self.rendered]
        if missed:  # 走得到却没排进分支里的（不该有）：照样各写一次，不丢节点
            self.emit(self.base, "（以下节点没能按分支排进去）")
            for nid in missed:
                if nid not in self.rendered:
                    self.chain(nid, frozenset(), fn.natural, None, self.base)
        for nid in fn.uncaught:  # 每个函数都有的那一行不写（没有信息）；任务会重试的那种才写
            if nid not in self.rendered:
                self.rendered.add(nid)
                if fn.nodes[nid].title != UNCAUGHT_TITLE:
                    self.node_line(nid, self.base, "")
        rest = [n.id for n in self.fg.nodes if n.id not in self.rendered]
        if rest:
            self.emit(self.base, "走不到的节点：")
            for nid in rest:
                self.rendered.add(nid)
                self.node_line(nid, self.base + 1, "")
        return self.out

    def emit(self, level: int, text: str) -> None:
        self.out.append(INDENT * level + text)

    # ---------- 走图 ----------

    def chain(self, cur: str | None, stops: frozenset[str], natural: str | None, after: str | None, level: int,
              prefix: str = "", quiet: bool = False, elif_row: bool = False) -> None:
        """从 cur（还没画过）起一路画下去，直到走到 stops、画过的节点或尽头；本层的汇合点画完一段再接着画（它们在这一串
        里也算 stops）。prefix 加在第一行前面（分支的"是 → "）；quiet：第一个节点不写行（开始节点，由函数头代表）；
        elif_row：第一个节点是"否则 →"链上的一行。"""
        pending: list[str] = []
        while True:
            if cur is None:
                while pending and pending[0] in self.rendered:
                    pending.pop(0)
                if not pending:
                    return
                cur = pending.pop(0)
            if cur in self.rendered:
                return
            self.rendered.add(cur)
            if not quiet:
                self.node_line(cur, level, prefix)
            quiet, prefix = False, ""
            here = stops | frozenset(pending)
            implied = next((p for p in pending if p not in self.rendered), after)
            cur = self.leave(cur, here, natural, implied, level, pending, elif_row)
            elif_row = False

    def leave(self, cur: str, stops: frozenset[str], natural: str | None, after: str | None, level: int,
              pending: list[str], elif_row: bool) -> str | None:
        """画 cur 的出路，返回同一层接着画的节点（None：这一串到头了）。after：cur 这一组之后读者以为接着的。"""
        fn = self.fn
        node = fn.nodes[cur]
        outs = [e for e in fn.outs[cur] if e.dst != cur]  # 自环（循环体为空）不画
        normal = [e for e in outs if e.kind != "raise"]
        raises = list({e.dst: e for e in reversed([e for e in outs if e.kind == "raise"])}.values())[::-1]
        catch_all = None  # 连异常的落脚点一起算的汇合点
        if raises:  # 循环只从循环之后的路和异常的落脚点算，不让汇合点落进循环体
            base = [Edge(cur, t, "next") for t in fn.loop_exits(cur)] if node.type == "loop" else normal
            catch_all = fn.merge(cur, base + raises, stops, natural)
            if catch_all is not None and fn.nodes[catch_all].type == "catch" and any(e.dst == catch_all for e in raises):
                catch_all = None  # try 体正常走都提前结束了：捕获之后的路缩进在"出错 →"下面画完，不挪到主干上
        side_stops = stops | {catch_all} if catch_all else stops
        side_natural = catch_all or natural
        side_after = catch_all or after
        dsts = list(dict.fromkeys(e.dst for e in normal))
        # 判断的两条出边即使到同一个节点，走法不同（一条直接、一条穿过 finally）也要各写一行
        branching = node.type == "loop" or len(dsts) >= 2 or (node.type == "cond" and len(normal) >= 2)
        if node.type == "loop":
            m = self.loop_branches(cur, normal, side_stops, side_natural, side_after, level)
        elif node.type != "cond" and len(normal) >= 2 and len({e.dst for e in normal if e.kind not in _SIDE}) <= 1:
            # finally 体的出口之类：break / continue / 进 finally 写成下面的"↪"行（和正常的路去同一个节点也写），正常的路
            # 接着往下
            for e in normal:
                if e.kind in _SIDE:
                    self.jump(e, level + 1, "")
            normal = [e for e in normal if e.kind not in _SIDE]
            dsts = list(dict.fromkeys(e.dst for e in normal))
            branching, m = False, None
        elif branching:
            keep = fn.case_bodies(node) if fn.is_match(node) else set()
            m = fn.merge(cur, normal, side_stops, side_natural, keep)
            self.cond_branches(cur, normal, m, side_stops, side_natural, side_after, level, elif_row)
        if branching:
            self.raise_branches(raises, dsts, catch_all, side_stops, side_natural, m or side_after, level)
            if catch_all:
                pending.insert(0, catch_all)
            return m
        e = next((x for x in normal if _plain(x)), normal[0]) if normal else None
        self.raise_branches(raises, dsts, catch_all, side_stops, side_natural,
                            self.next_after(e, catch_all, stops, side_stops, after), level)
        if e is None:
            return catch_all if catch_all and catch_all not in self.rendered else None
        if catch_all is not None and e.dst != catch_all:  # try 体其余的语句：同一缩进，到汇合点为止
            self.follow(e, side_stops, side_natural, catch_all, level, "")
            return None if catch_all in self.rendered else catch_all
        return self.step(e, stops, after, level)

    def next_after(self, e: Edge | None, catch_all: str | None, stops: frozenset[str], side_stops: frozenset[str],
                   after: str | None) -> str | None:
        """只有一条（或没有）正常出路的节点：它这一组（"出错 →"那几块）之后读者以为接着的是什么。下一行是跳转行时
        说不准（None）。"""
        if e is None:
            return catch_all or after
        if catch_all is not None and e.dst != catch_all:
            return e.dst if _plain(e) and e.dst not in side_stops and e.dst not in self.rendered else None
        if e.dst in stops or e.dst in self.rendered:
            return after if e.dst == after and _plain(e) else None
        return e.dst if _plain(e) else None

    def loop_branches(self, cur: str, normal: list[Edge], stops: frozenset[str], natural: str | None,
                      after: str | None, level: int) -> str | None:
        """循环节点的出路，返回循环之后的主干从哪接上：只从"循环结束"和 break 的去处（循环体外）算，不会落在循环体里。
        "每次 →"块写完读者以为回到循环节点。"""
        fn = self.fn
        node = fn.nodes[cur]
        out = fn.loop_exits(cur)
        m = fn.merge(cur, [Edge(cur, t, "next") for t in out], stops, natural) if out else None
        branch_stops = stops | {cur} | ({m} if m else set())
        for e in fn.branch_order(node, normal):
            if e.kind in ("loop_exit", "break"):
                self.follow(e, branch_stops, m or natural, m or after, level + 1, fn.label(node, e) + " → ")
            else:
                self.follow(e, branch_stops, cur, cur, level + 1, "每次 → ")
        return m

    def cond_branches(self, cur: str, normal: list[Edge], m: str | None, stops: frozenset[str], natural: str | None,
                      after: str | None, level: int, elif_row: bool) -> None:
        """判断（或别的有几条正常出路的节点）的各条分支，cur 那一行在 level。if 的"否"是 elif（只从这里进来的 if）时
        写成"否则 →"：cur 本身是"否则 →"链上的一行、又没有汇合点时，和 cur 同级接着写（一个循环，不递归），否则在分支
        那一级起一条新的链。match 的非兜底分支直接到汇合点时也写一行。"""
        fn = self.fn
        c, edges, row_level = cur, normal, level
        while True:
            node = fn.nodes[c]
            b_stops = stops | {m} if m else stops
            if m is not None:  # continue / break 穿过 finally 的分支：它进的 finally 体主干后面还会画到，只写跳转
                early = {e.dst for e in edges if _early(e) and e.dst != m}
                b_stops |= early & self.reach_from(m, b_stops)
            b_natural, b_after = m or natural, m or after
            match = fn.is_match(node)
            nxt = None
            ordered = fn.branch_order(node, edges)
            unordered = fn.unordered_from(node, ordered)
            for e in ordered:
                if e is unordered:
                    self.emit(row_level + 1, _UNORDERED)
                if self.elif_of(node, e, b_stops):
                    if elif_row and m is None:
                        nxt = e
                    else:
                        self.follow(e, b_stops, b_natural, b_after, row_level + 1, _ELIF, elif_row=True)
                    continue
                force = match and not fn.is_else(node, e)
                self.follow(e, b_stops, b_natural, b_after, row_level + 1, fn.label(node, e) + " → ", force)
            if c != cur and m is not None:  # 同级写的 elif 有自己的汇合点：接在它那一块里
                self.chain(m, stops, natural, after, row_level)
            if nxt is None:
                return
            c = nxt.dst
            self.rendered.add(c)
            self.node_line(c, row_level, _ELIF)
            edges = fn.normal(c)
            m = fn.merge(c, edges, stops, natural)
            elif_row = True

    def reach_from(self, start: str, stops: frozenset[str]) -> set[str]:
        """从 start 出发、不经过 stops 能走到的节点（含 start）。"""
        seen, todo = {start}, [start]
        while todo:
            for e in self.fn.outs[todo.pop()]:
                if e.dst not in seen and e.dst not in stops:
                    seen.add(e.dst)
                    todo.append(e.dst)
        return seen

    def elif_of(self, node: Node, e: Edge, stops: frozenset[str]) -> bool:
        """if 节点 node 的"否"出路 e 是不是 elif：走到另一个只从这里进来、不是 try 体入口的 if（还没画过）。"""
        fn = self.fn
        if node.type != "cond" or fn.is_match(node) or e.kind != "no":
            return False
        d = fn.nodes[e.dst]
        if d.type != "cond" or fn.is_match(d) or d.id in stops or d.id in self.rendered:
            return False
        if any(x.kind == "raise" for x in fn.outs[d.id]) or not fn.only_from(d.id, node.id):
            return False
        return len(fn.normal(d.id)) >= 2  # 两条出边到同一个节点也算（走法不同）

    def raise_branches(self, raises: list[Edge], dsts: list[str], catch_all: str | None,
                       stops: frozenset[str], natural: str | None, after: str | None, level: int) -> None:
        """"出错 →"分支（after：它们之后读者以为接着的）。异常落到汇合点、落到正常的路也去的地方（只写日志的 except 吞掉
        异常后照常往下、没被捕获的异常穿过 finally 体）时不写：主干上会画到那里。"""
        for e in raises:
            quiet = e.dst == catch_all or e.dst in dsts or (e.dst == natural and e.dst in stops)
            if not quiet:
                self.follow(e, stops, natural, after, level + 1, "出错 → ")

    def step(self, e: Edge, stops: frozenset[str], after: str | None, level: int) -> str | None:
        """同一串里顺着 e 往下走：返回下一个要画的节点；走到 stops、画过的节点时，正是读者以为接着的就不写，否则写一行
        跳转，返回 None。"""
        d = e.dst
        if d in stops or d in self.rendered:
            if not (d == after and _plain(e)):
                self.jump(e, level, "")
            return None
        if not _plain(e):
            self.jump(e, level, "")
        return d

    def follow(self, e: Edge, stops: frozenset[str], natural: str | None, after: str | None, level: int,
               prefix: str, force: bool = False, elif_row: bool = False) -> None:
        """一条分支：直接到读者以为接着的节点时不写（force 时照写）；到 stops 里、画过的节点时写一行跳转；否则从它的
        终点画一串。"""
        d = e.dst
        if d in stops or d in self.rendered:
            if not (d == after and _plain(e) and not force):
                self.jump(e, level, prefix)
            return
        if not _plain(e) and e.kind != "raise":  # "出错 →"已经说了是异常
            self.jump(e, level, prefix)
            prefix = ""
        self.chain(d, stops, natural, after, level, prefix, elif_row=elif_row)

    def jump(self, e: Edge, level: int, prefix: str) -> None:
        fn = self.fn
        node = fn.nodes[e.dst]
        verb = "回到" if e.dst in self.rendered else "到"
        lead = _JUMP_LEAD.get(e.kind, "")
        if e.kind == "finally" and fn.nodes[e.src].type != "end":  # break / continue 穿过 finally：说出来之后做什么
            leg = fn.finally_leg(e.dst)
            lead = f"进入 finally 再{leg}，" if leg else lead
        self.emit(level, f"{prefix}↪ {lead}{verb} {node_ref(self.fg, node)} {node_text(node)}")

    # ---------- 节点行 ----------

    def node_line(self, nid: str, level: int, prefix: str) -> None:
        node = self.fn.nodes[nid]
        callees: dict[str, bool] = {}  # 解析到的项目内函数 -> 是否推测
        for ref in node.calls:
            if not ref.is_dispatch and ref.target in self.ctx.graph.functions and ref.resolution in ("exact", "guessed"):
                callees.setdefault(ref.target, ref.resolution == "guessed")
        expand = self.depth > 0 and node.type == "step" and bool(callees)
        loc = f"L{node.line} " if node.line else ""
        self.emit(level, f"{prefix}{node_ref(self.fg, node)} {loc}{node_text(node)}{self.suffix(node, callees, expand)}")
        for branch, text in node.quiet.items():  # 没有节点、但改了变量的分支：不写的话读大纲的人不知道有这条规则
            self.emit(level + 1, f"{_QUIET[branch]} →（这条分支没有节点，只赋值：{text}）接着往下")
        if expand:
            for key, guessed in callees.items():
                fg = self.ctx.graph.functions[key]
                head = f"调用 {fg.qualname}{'（推测）' if guessed else ''}  {fg.file}:{fg.line}  [{fg.sid or key}]"
                self.out.append(_Part(key, level + 1, self.depth - 1, head))

    def suffix(self, node: Node, callees: dict[str, bool], expand: bool) -> str:
        out = ""
        if callees and not expand:
            graph = self.ctx.graph
            refs = " ".join(f"[{graph.functions[k].sid or k}]" + ("（推测）" if g else "") for k, g in callees.items())
            out += f" ⇒ 可展开 {refs}"
            inner = dict.fromkeys(t for ref in node.calls if not ref.is_dispatch and ref.target in callees
                                  for t in ref.dispatches)
            if inner:
                out += f"（内部派发 {'、'.join(self.ctx.name(t) for t in inner)}）"
        for target in dict.fromkeys(ref.target for ref in node.calls if ref.is_dispatch and ref.target):
            out += f" ⇒ {self.ctx.dispatch_note(target)}"
        return out


def _depth(depth: int) -> int:
    return max(0, min(depth, MAX_DEPTH))


def function_outline(graph: ProjectGraph, function: str, *, depth: int = 0) -> list[str]:
    """函数 function（key）的大纲：函数头一行，接着节点行（缩进一级）。写法见模块说明；depth 限制在 0～MAX_DEPTH。"""
    fg = graph.functions[function]
    return _Ctx(graph).write([_Part(function, 0, _depth(depth), _head(fg))])


def _preorder(task: LineTask) -> list[LineTask]:
    out, stack = [], [task]
    while stack:
        t = stack.pop()
        out.append(t)
        stack.extend(reversed(t.children))
    return out


def _segments(graph: ProjectGraph, line: FlowLine) -> list[str]:
    """功能线的各段：派发树先序上的任务（goto 的、不在图里的不算），不重复。"""
    keys = [t.function for t in _preorder(line.root) if not t.goto and t.function in graph.functions]
    return list(dict.fromkeys(keys))


def _common_names(graph: ProjectGraph, line: FlowLine) -> list[str]:
    """这条线涉及的函数派发到的公共任务的限定名，按出现顺序。"""
    common = set(graph.common_tasks) - {line.root.function}
    found = dict.fromkeys(ref.target for key in line_functions(graph, line) for n in graph.functions[key].nodes
                          for ref in n.calls if ref.is_dispatch and ref.target in common)
    return [graph.functions[k].qualname if k in graph.functions else k for k in found]


def line_outline(graph: ProjectGraph, line: FlowLine, *, depth: int = 0) -> str:
    """功能线的大纲：标题行，派发树上每个任务一段（先序；goto 的只写"回到任务 …"），最后一行列出派发到的公共任务。
    depth 限制在 0～MAX_DEPTH。"""
    segments = _segments(graph, line)
    ctx = _Ctx(graph, segments={k: i for i, k in enumerate(segments)}, common=frozenset(graph.common_tasks))
    parts: list[str | _Part] = [f"# {line.title_full or line.title}  [{line.sid or line.id}]  分组：{line.group}"]
    for task in _preorder(line.root):
        fg = graph.functions.get(task.function)
        if fg is None:
            continue
        if task.goto:
            parts.append(f"回到任务 {fg.qualname}  [{fg.sid or fg.key}]")
        else:
            parts.append(_Part(task.function, 0, _depth(depth), _head(fg), ctx.segments[task.function]))
    names = _common_names(graph, line)
    if names:
        parts.append(f"公共任务（不并入本线，各自成线）：{'、'.join(names)}")
    return "\n".join(ctx.write(parts))


# ---------- Mermaid ----------

_MM_ESCAPE = {"#": "#35;", '"': "#quot;", "<": "#lt;", ">": "#gt;", "|": "#124;", "`": "#96;", "&": "#amp;"}
_MM_KIND = {"loop_each": "每次", "loop_exit": "循环结束", "loop_back": "下一轮", "break": "跳出循环",
            "finally": "进入 finally", "raise": "出错", "yes": "是", "no": "否"}
_MM_SHAPE = {"cond": ("{", "}"), "end": ("(", ")")}


def _mm(text: str) -> str:
    """放进 Mermaid 引号里的文字：换行变空格，引号、|、尖括号、#、&、反引号转成 Mermaid 的实体写法。"""
    return "".join(_MM_ESCAPE.get(ch, ch) for ch in text.replace("\r", " ").replace("\n", " "))


def _mm_edge_word(fn: _Fn, e: Edge) -> str:
    node = fn.nodes[e.src]
    if e.kind != "raise" and node.type == "cond" and (e.branch or e.label or e.kind in ("yes", "no", "case")):
        word = fn.branch_word(node, e, quoted=False)
        return word if e.kind in ("yes", "no", "case") else f"{word} · {_MM_KIND.get(e.kind, e.kind)}"
    return _MM_KIND.get(e.kind, "")


def _dispatch_sources(fg: FunctionGraph, child: str) -> list[str]:
    """fg 里派发 child 的节点：直接派发的；没有时是调用了会派发它的函数的；都没有时是开始节点。"""
    direct = [n.id for n in fg.nodes if any(r.is_dispatch and r.target == child for r in n.calls)]
    inner = [n.id for n in fg.nodes if any(not r.is_dispatch and child in r.dispatches for r in n.calls)]
    return direct or inner or [n.id for n in fg.nodes if n.type == "start"][:1]


def line_mermaid(graph: ProjectGraph, line: FlowLine) -> str:
    """功能线的 Mermaid 流程图（flowchart TD）：派发树上每个任务一个 subgraph（f1、f2……按先序），节点 id 是"f序号_节点
    id"，判断是菱形，结局是圆角，其余是方框；连线带标签（raise 用虚线）；派发画成从派发处到被派发任务开始节点的虚线。
    不展开被调函数。公共任务写在最后的注释行里。"""
    segments = _segments(graph, line)
    index = {k: i for i, k in enumerate(segments, 1)}
    out = ["flowchart TD"]
    for i, key in enumerate(segments, 1):
        fg = graph.functions[key]
        fn = _Fn(fg)
        head = _head(fg).rsplit("  [", 1)[0]
        out.append(f'  subgraph f{i}["{_mm(head)}"]')
        for n in fg.nodes:
            left, right = _MM_SHAPE.get(n.type, ("[", "]"))
            text = (f"L{n.line} " if n.line and n.type != "start" else "") + node_text(n)
            out.append(f'    f{i}_{n.id}{left}"{_mm(text)}"{right}')
        for e in fg.edges:
            if e.src in fn.nodes and e.dst in fn.nodes:
                word = _mm_edge_word(fn, e)
                arrow = "-.->" if e.kind == "raise" else "-->"
                out.append(f"    f{i}_{e.src} {arrow}" + (f'|"{_mm(word)}"|' if word else "") + f" f{i}_{e.dst}")
        out.append("  end")
    for parent in _preorder(line.root):
        if parent.goto or parent.function not in index:
            continue
        for child in parent.children:
            if child.function not in index:
                continue
            start = next((n.id for n in graph.functions[child.function].nodes if n.type == "start"), None)
            if start is None:
                continue
            for src in _dispatch_sources(graph.functions[parent.function], child.function):
                out.append(f'  f{index[parent.function]}_{src} -.->|"派发"| f{index[child.function]}_{start}')
    names = _common_names(graph, line)
    if names:
        out.append(f"  %% 公共任务（不并入本线，各自成线）：{'、'.join(names)}")
    return "\n".join(out)
