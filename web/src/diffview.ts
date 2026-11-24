// 流程差异（后端 GET /diff 的 diff，字段见 core/src/pystepmap/diff.py）在界面上怎么显示：左侧列表上哪些功能线有变化，
// 画布上哪些节点 / 连线是新增、改动的，折叠着的卡片、分支里有几处变化，差异栏的"上一处 / 下一处"依次去哪里，详情栏
// 的"原来是"。纯函数，测试见 diffview.test.ts。
//
// 差异和图是分开取的，可能短暂地属于不同的两次分析（中间别的进程又分析了一次）：这里只认在图里找得到的东西——函数按
// sid 找（找不到再试 new_key），节点按 id 找、sid 对不上时按 sid 找，连线要图里真有这一条；找不到的都忽略，不抛异常。
// state.ts 发现两者不是同一次分析时会把图和差异都重取一遍。
//
// "一处变化"：新增 / 改动的节点各算一处；删掉的节点算一处（落在它原来控制流上前面的那个节点上；旧版存下的差异里
// 那可能是前一个分支的结局节点，那时落在所在函数的卡片上）；连线只算两端都留下来的（新增连线的一端是新增节点时，
// 节点的新增已经说明了它，只画成绿色、不单算）；新增的函数整个算一处（节点全标新增）。
// 不能钻入的调用（判断节点调用的判断函数、结局里调用的函数……）：被调函数（及它再调用的函数）里的变化算在调用它的
// 节点上——这个节点标"改动"（原因是调用的函数变了），详情栏列出被调函数里变了什么；这个节点自己是新增 / 改动的，
// 不另算一处。

import type { DiffResult, EdgeChangeJson, FunctionChangeJson, GraphDiffJson, NodeChangeJson } from './api'
import {
  buildLineView,
  drillChainOf,
  drillId,
  setOpen,
  type FunctionGraph,
  type GraphNode,
  type LineTask,
  type LineView,
  type NodeType,
  type ProjectGraph,
  type ViewState,
} from './graph'
import { nodeSid } from './graph/collapse'
import { NODE_TYPE_NAME } from './theme'

export type Mark = 'added' | 'changed'
export type ChangeKind = 'added' | 'changed' | 'removed'

/** 删掉的节点 / 连线（只显示成文字） */
export interface RemovedItem {
  what: 'node' | 'edge'
  /** 节点类型；连线是 kind */
  type: string
  /** 节点的标题；连线是"起点标题 → 终点标题" */
  title: string
  /** 节点的副标题（条件原文、调用的函数）；连线是分支 / 走向（是、否、模式……） */
  subtitle: string
  /** 节点原来的行号；连线是起点的行号 */
  line: number
  /** 节点：它原来控制流上前面那个仍然在的节点（图内 id；没有、或只知道在这个函数里时为空）；连线：起点 */
  after: string
  /** 节点：挂在 after 的哪条出边上（是、否、模式、每次、循环结束），没有时为空串；连线为空串 */
  label: string
}

/** 改动的节点原来的样子 */
export interface OldNode {
  title: string
  subtitle: string
  line: number
}

/** 一个函数自己的一处变化 */
export interface FnChange {
  kind: ChangeKind
  /** 落在哪个节点上（图内 id）：新增 / 改动的节点自己，删掉的节点原来前面的节点，连线的起点；null = 整个函数 */
  at: string | null
  /** 同一个节点上的先后：0 节点自己（含"调用的函数变了"），2 从它出发的连线，3 它后面删掉的节点 */
  order: number
  /** 排先后用的节点（一般同 at；落在函数卡片上的删除是原来行号之前最近的那个节点） */
  near: string | null
  text: string
  /** 删掉的：原来在哪里（"删除了什么"里写）；别的为空串 */
  where: string
  /** "调用的函数变了"：是哪些被调函数（这条线上能钻入到它们时不再另算） */
  callees?: string[]
}

/** 一个节点调用的、不能钻入的函数里变了什么（详情栏） */
export interface CalleeNote {
  /** 被调函数 key */
  fn: string
  title: string
  /** 是判断函数（@condition） */
  condition: boolean
  status: 'added' | 'changed'
  /** 里面的每一处变化，一句一条（不带前缀，见 calleeNoteLines） */
  lines: string[]
}

interface LineInfo {
  status: 'added' | 'changed'
  /** 这条线涉及的、有变化的函数 sid（差异原样） */
  functions: string[]
  /** 派发树上多出的任务（图里的函数 key） */
  tasksAdded: string[]
  /** 派发树上少掉的任务 */
  tasksRemoved: { sid: string; title: string }[]
}

/** 有变化、但不在任何有变化的功能线上的函数 */
export interface OffLineFunction {
  sid: string
  title: string
  file: string
  /** 函数名（带类名）；差异里没有 key 时为空 */
  name: string
  status: ChangeKind
  /** 变了几处（节点加连线）；新增、删除的函数整个算，为 0 */
  count: number
}

export interface FlowDiffView {
  /** 功能线 sid → 状态（列表上的标记）；只有图里有的线 */
  lineStatus: Map<string, 'added' | 'changed'>
  /** 被删掉的功能线（图里没有了，只列文字） */
  removedLines: { sid: string; title: string; group: string }[]
  /** 有变化、但不在任何有变化的功能线上的函数（画布上到不了，只列文字）；按差异里的顺序 */
  offLine: OffLineFunction[]
  /** 函数 key → 节点 id → 标记 */
  nodeMarks: Map<string, Map<string, Mark>>
  /** 函数 key → 新增连线的 "src|dst|kind|label"（图内节点 id） */
  edgeMarks: Map<string, Set<string>>
  /** 函数 key → 删掉的节点 / 连线 */
  removed: Map<string, RemovedItem[]>
  /** 函数 key → 节点 id → 改动的节点原来的样子 */
  changedFrom: Map<string, Map<string, OldNode>>
  /** 函数 key → 状态 */
  functionStatus: Map<string, 'added' | 'changed'>
  /** 函数 key → 它自己的每一处变化，按在函数图里的位置排好 */
  changes: Map<string, FnChange[]>
  /** 功能线 sid → 差异里关于它的信息（lineChangeList 用） */
  lines: Map<string, LineInfo>
  /** 删掉的函数：sid → 标题 */
  removedFunctions: Map<string, string>
  /** 函数 key → 节点 id → 它调用的、不能钻入的函数里的变化 */
  calleeNotes: Map<string, Map<string, CalleeNote[]>>
  /** 函数 key → 只因为调用的函数变了才标"改动"的节点 */
  calleeMarked: Map<string, Set<string>>
  /** 自己的节点没变、只是调用的函数变了的函数（不在 functionStatus 里："调用的函数有改动"） */
  calleeOnly: Set<string>
  /** 有变化的函数 key → 不能钻入地调用它（或经它调用到有变化的函数）的节点：[函数 key, 节点 id] */
  callSites: Map<string, [string, string][]>
}

/** 差异栏上的一处变化 */
export interface ChangeStop {
  functionKey: string
  /** 图内节点 id；null = 卡片本身（整个函数、派发树上的变化） */
  nodeId: string | null
  kind: ChangeKind
  text: string
  /** 删掉的：原来在哪里；别的为空串 */
  where: string
  /** 在功能线任务树里哪个任务的卡片上（任务函数 key） */
  task: string
  /** 从任务函数一步步钻入到 functionKey 经过的步骤；空 = 就在任务函数里 */
  path: { fn: string; node: string }[]
  /** "调用的函数变了"：是哪些被调函数 */
  callees?: string[]
}

export function emptyDiffView(): FlowDiffView {
  return {
    lineStatus: new Map(),
    removedLines: [],
    offLine: [],
    nodeMarks: new Map(),
    edgeMarks: new Map(),
    removed: new Map(),
    changedFrom: new Map(),
    functionStatus: new Map(),
    changes: new Map(),
    lines: new Map(),
    removedFunctions: new Map(),
    calleeNotes: new Map(),
    calleeMarked: new Map(),
    calleeOnly: new Set(),
    callSites: new Map(),
  }
}

// ---------------------------------------------------------------------------------------------------------------
// 文字

const fullTitle = (x: { title: string; title_full?: string }): string => x.title_full || x.title
const typeName = (t: string): string => NODE_TYPE_NAME[t as NodeType] ?? '节点'
const atLine = (line: number): string => (line > 0 ? `（原第 ${line} 行）` : '')

const EDGE_KIND_TEXT: Record<string, string> = {
  yes: '是',
  no: '否',
  loop_each: '每一项',
  loop_back: '回到循环开头',
  loop_exit: '循环结束',
  break: '跳出循环',
  raise: '抛出异常',
  finally: '进入 finally',
}

const edgeWay = (e: EdgeChangeJson): string => e.label || EDGE_KIND_TEXT[e.kind] || ''

function edgeText(e: EdgeChangeJson): string {
  const way = edgeWay(e)
  return `${e.status === 'added' ? '新增' : '删除'}连线：${e.src_title} → ${e.dst_title}${way ? `（${way}）` : ''}`
}

function nodeText(c: NodeChangeJson): string {
  const t = typeName(c.type)
  if (c.status === 'added') return `新增${t}：${c.new_title}`
  if (c.status === 'removed') return `删除${t}：${c.old_title}${atLine(c.old_line)}`
  return c.old_title !== c.new_title ? `改动${t}：${c.old_title} → ${c.new_title}` : `${t}「${c.new_title}」语句有改动${atLine(c.old_line)}`
}

/**
 * 详情栏里改动的节点"原来是"什么：标题变了写原来的标题（条件 / 调用原文也变了时 code 是原来的原文）；标题没变、
 * 原文变了写原来的原文；都没变（只是语句别处改了）写"语句有改动"。
 */
export function changedNote(from: OldNode, now: { titleFull: string; subtitleFull: string }): { text: string; code: string | null } {
  const sub = from.subtitle && from.subtitle !== now.subtitleFull ? from.subtitle : null
  if (from.title !== now.titleFull) return { text: `原来是：${from.title}${atLine(from.line)}`, code: sub }
  if (sub) return { text: `原来是${atLine(from.line)}：`, code: sub }
  return { text: `语句有改动${atLine(from.line)}`, code: null }
}

// ---------------------------------------------------------------------------------------------------------------
// 函数图里的位置、钻入

const IMPLICIT_END = new Set(['end:implicit', 'end:uncaught'])
const positionCache = new WeakMap<FunctionGraph, Map<string, number>>()

/** 节点在画布上从上到下的先后（同差异算法的顺序）：开始节点最前，隐含的结局最后，其余按（行，生成顺序） */
function positions(fn: FunctionGraph): Map<string, number> {
  let m = positionCache.get(fn)
  if (m) return m
  const rank = (n: GraphNode, i: number): [number, number, number] => {
    if (n.type === 'start') return [0, 0, i]
    if (IMPLICIT_END.has(n.sid) || (n.type === 'end' && n.line === 0)) return [2, 0, i]
    return [1, n.line, i]
  }
  const ranked = fn.nodes.map((n, i) => ({ n, r: rank(n, i) }))
  ranked.sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || a.r[2] - b.r[2])
  m = new Map(ranked.map((x, i) => [x.n.id, i]))
  positionCache.set(fn, m)
  return m
}

const changeKey = (c: { near: string | null; order: number }, pos: Map<string, number>): number =>
  (c.near === null ? -1 : (pos.get(c.near) ?? -1)) + c.order / 10

/** 一个步骤能钻入的被调用函数（同 viewmodel 的规则：非派发、确定或推测的项目内函数、函数图有节点） */
export function drillTargets(graph: ProjectGraph, n: GraphNode): string[] {
  if (n.type !== 'step') return []
  const out: string[] = []
  for (const c of n.calls) {
    if (c.is_dispatch || !c.target || (c.resolution !== 'exact' && c.resolution !== 'guessed')) continue
    const callee = graph.functions[c.target]
    if (!callee || callee.nodes.length === 0 || out.includes(callee.key)) continue
    out.push(callee.key)
  }
  return out
}

const callersCache = new WeakMap<ProjectGraph, Map<string, string[]>>()

/** 被调用函数 key → 能钻入到它的函数 key（反向的钻入关系） */
function callersOf(graph: ProjectGraph): Map<string, string[]> {
  let m = callersCache.get(graph)
  if (m) return m
  m = new Map()
  for (const fn of Object.values(graph.functions)) {
    for (const n of fn.nodes) {
      for (const t of drillTargets(graph, n)) {
        const list = m.get(t) ?? []
        if (!list.includes(fn.key)) list.push(fn.key)
        m.set(t, list)
      }
    }
  }
  callersCache.set(graph, m)
  return m
}

// ---------------------------------------------------------------------------------------------------------------
// 建

function functionKey(graph: ProjectGraph, sid: string, newKey: string): string | null {
  const k = graph.sids[sid]
  if (k && graph.functions[k]) return k
  if (newKey && graph.functions[newKey]?.sid === sid) return newKey
  return null
}

function getOrSet<K extends object | string, V>(m: Map<K, V> | WeakMap<K & object, V>, k: K, make: () => V): V {
  const map = m as Map<K, V>
  let v = map.get(k)
  if (v === undefined) map.set(k, (v = make()))
  return v
}

/** 原来的行号之前（含）最近的那个节点：落在函数卡片上的删除按它排先后；没有时为 null */
function nodeBeforeLine(fn: FunctionGraph, line: number): string | null {
  const pos = positions(fn)
  let best: GraphNode | null = null
  for (const n of fn.nodes) {
    if (n.line <= 0 || n.line > line || IMPLICIT_END.has(n.sid)) continue
    if (!best || pos.get(n.id)! > pos.get(best.id)!) best = n
  }
  return best?.id ?? null
}

/** 被调函数里一处变化的说法（详情栏，"判断函数里："之类的前缀由 calleeNoteLines 加） */
function calleeLine(c: NodeChangeJson): string {
  if (c.status === 'added') return `新增「${c.new_title}」`
  if (c.status === 'removed') return `删掉「${c.old_title}」`
  return c.old_title !== c.new_title ? `「${c.old_title}」改成「${c.new_title}」` : `「${c.new_title}」语句有改动`
}

/** 一个函数自己的变化进 out；返回给调用它的节点用的说明（新增的函数为空列表；什么都对不上时为 null） */
function addFunction(out: FlowDiffView, fn: FunctionGraph, fc: FunctionChangeJson): string[] | null {
  const key = fn.key
  const status = fc.status === 'added' ? 'added' : 'changed'
  out.functionStatus.set(key, status)
  const byId = new Map(fn.nodes.map((n) => [n.id, n]))
  const bySid = new Map(fn.nodes.map((n) => [n.sid, n]))
  const find = (id: string, sid: string): GraphNode | null => {
    const n = byId.get(id)
    if (n && (!sid || n.sid === sid)) return n
    return sid ? (bySid.get(sid) ?? null) : null
  }
  const changes: FnChange[] = []
  const notes: string[] = []
  const added = new Set<string>()
  if (status === 'added') changes.push({ kind: 'added', at: null, near: null, order: 0, text: `新增函数：${fullTitle(fn)}`, where: '' })
  for (const c of fc.nodes) {
    if (c.status === 'removed') {
      // 控制流上前面那个还在的节点；旧版存下的差异给的是源码顺序上前面那个，可能是前一个分支的结局（结局后面不会
      // 再接节点）——那时、以及没有 after 时，只说在这个函数里
      const anchor = c.after ? byId.get(c.after) : undefined
      const ok = anchor !== undefined && anchor.type !== 'end'
      const after = ok ? anchor.id : ''
      const label = ok ? c.after_label : ''
      getOrSet(out.removed, key, () => []).push({
        what: 'node',
        type: c.type,
        title: c.old_title,
        subtitle: c.old_subtitle,
        line: c.old_line,
        after,
        label,
      })
      const where = ok
        ? `原来在「${fullTitle(anchor)}」之后${label ? `（${label}）` : ''}`
        : `原来在函数「${fullTitle(fn)}」里${c.old_line > 0 ? `（第 ${c.old_line} 行）` : ''}`
      if (status === 'changed') {
        const near = ok ? after : nodeBeforeLine(fn, c.old_line)
        changes.push({ kind: 'removed', at: after || null, near, order: 3, text: nodeText(c), where })
      }
      notes.push(calleeLine(c))
      continue
    }
    const n = find(c.new_id, c.new_sid)
    if (!n) continue
    getOrSet(out.nodeMarks, key, () => new Map()).set(n.id, c.status)
    if (c.status === 'added') added.add(n.id)
    else getOrSet(out.changedFrom, key, () => new Map()).set(n.id, { title: c.old_title, subtitle: c.old_subtitle, line: c.old_line })
    if (status === 'changed') changes.push({ kind: c.status, at: n.id, near: n.id, order: 0, text: nodeText(c), where: '' })
    notes.push(calleeLine(c))
  }
  for (const e of fc.edges) {
    const src = byId.get(e.src)
    if (!src || !byId.has(e.dst)) continue
    if (e.status === 'added') {
      if (!fn.edges.some((g) => g.src === e.src && g.dst === e.dst && g.kind === e.kind && g.label === e.label)) continue
      getOrSet(out.edgeMarks, key, () => new Set()).add(`${e.src}|${e.dst}|${e.kind}|${e.label}`)
      if (status === 'changed' && !added.has(e.src) && !added.has(e.dst)) {
        changes.push({ kind: 'added', at: e.src, near: e.src, order: 2, text: edgeText(e), where: '' })
        notes.push(edgeText(e))
      }
      continue
    }
    getOrSet(out.removed, key, () => []).push({
      what: 'edge',
      type: e.kind,
      title: `${e.src_title} → ${e.dst_title}`,
      subtitle: edgeWay(e),
      line: e.src_line,
      after: e.src,
      label: '',
    })
    if (status === 'changed') {
      changes.push({ kind: 'removed', at: e.src, near: e.src, order: 2, text: edgeText(e), where: `原来从「${fullTitle(src)}」出发` })
      notes.push(edgeText(e))
    }
  }
  sortChanges(fn, changes)
  out.changes.set(key, changes)
  if (status === 'added') return []
  return notes.length ? notes : null
}

function sortChanges(fn: FunctionGraph, changes: FnChange[]) {
  const pos = positions(fn)
  // 稳定排序：同一位置上保持差异里的顺序
  changes.sort((a, b) => changeKey(a, pos) - changeKey(b, pos))
}

/** 节点调用的、不能钻入的函数（判断函数、结局里调用的函数、没有节点的函数……）：非派发、确定或推测的项目内函数 */
function plainTargets(graph: ProjectGraph, n: GraphNode): string[] {
  const drill = new Set(drillTargets(graph, n))
  const out: string[] = []
  for (const c of n.calls) {
    if (c.is_dispatch || !c.target || (c.resolution !== 'exact' && c.resolution !== 'guessed')) continue
    if (graph.functions[c.target] && !drill.has(c.target) && !out.includes(c.target)) out.push(c.target)
  }
  return out
}

/** 调用的函数变了的那个节点在差异栏上的一句 */
function calleeText(n: GraphNode, notes: CalleeNote[]): string {
  const what = notes.every((x) => x.condition) ? '判断函数' : '函数'
  const verb = notes.every((x) => x.status === 'added') ? '是新增的' : '改了'
  const head = `${typeName(n.type)}「${fullTitle(n)}」`
  // flow-style 里判断节点的标题就是判断函数的标题：不重复写
  if (notes.length === 1 && notes[0].title === fullTitle(n)) return `${head}的${what}${verb}`
  return `${head}调用的${what}${notes.map((x) => `「${x.title}」`).join('、')}${verb}`
}

/** 详情栏里被调函数变了什么，一句一条："判断函数里：「返回 a > b」改成「返回 a >= b」" */
export function calleeNoteLines(note: CalleeNote): string[] {
  const what = note.condition ? '判断函数' : '函数'
  if (note.status === 'added' || note.lines.length === 0) return [`调用的${what}「${note.title}」是对比基准之后新增的`]
  const prefix = note.condition ? '判断函数里：' : `函数「${note.title}」里：`
  return note.lines.map((l) => `${prefix}${l}`)
}

/**
 * 不能钻入的调用：节点调用的函数（以及它沿 call_targets 再调用到的函数）里有变化时，变化算在这个节点上。新增的节点
 * 不另算（它自己就是新增）；自己改了的节点只写说明、不另算一处；别的节点标"改动"、算一处。
 */
function attributeCallees(out: FlowDiffView, graph: ProjectGraph, notesOf: Map<string, string[]>) {
  if (notesOf.size === 0) return
  // 能沿 call_targets 走到有变化的函数的那些函数（剪枝用）
  const reverse = new Map<string, string[]>()
  for (const f of Object.values(graph.functions)) for (const t of f.call_targets) getOrSet(reverse, t, () => []).push(f.key)
  const leads = new Set<string>()
  const stack = [...notesOf.keys()]
  while (stack.length) {
    const k = stack.pop()!
    if (leads.has(k)) continue
    leads.add(k)
    for (const c of reverse.get(k) ?? []) if (!leads.has(c)) stack.push(c)
  }
  const reached = (starts: string[], exclude: string): string[] => {
    const seen = new Set<string>([exclude])
    const found: string[] = []
    const queue = starts.filter((k) => leads.has(k))
    while (queue.length) {
      const k = queue.shift()!
      if (seen.has(k)) continue
      seen.add(k)
      if (notesOf.has(k)) found.push(k)
      for (const t of graph.functions[k]?.call_targets ?? []) if (leads.has(t) && !seen.has(t)) queue.push(t)
    }
    return found
  }
  const touched = new Set<string>()
  for (const w of Object.values(graph.functions)) {
    for (const n of w.nodes) {
      const fns = reached(plainTargets(graph, n), w.key)
      if (fns.length === 0) continue
      for (const f of fns) getOrSet(out.callSites, f, () => []).push([w.key, n.id])
      const mark = out.nodeMarks.get(w.key)?.get(n.id)
      if (mark === 'added') continue
      const notes: CalleeNote[] = fns.map((f) => ({
        fn: f,
        title: fullTitle(graph.functions[f]),
        condition: graph.functions[f].kind === 'condition',
        status: out.functionStatus.get(f) === 'added' ? 'added' : 'changed',
        lines: notesOf.get(f) ?? [],
      }))
      getOrSet(out.calleeNotes, w.key, () => new Map()).set(n.id, notes)
      if (mark === 'changed') continue
      getOrSet(out.nodeMarks, w.key, () => new Map()).set(n.id, 'changed')
      getOrSet(out.calleeMarked, w.key, () => new Set()).add(n.id)
      if (!out.functionStatus.has(w.key)) out.calleeOnly.add(w.key)
      getOrSet(out.changes, w.key, () => []).push({
        kind: 'changed',
        at: n.id,
        near: n.id,
        order: 0,
        text: calleeText(n, notes),
        where: '',
        callees: fns,
      })
      touched.add(w.key)
    }
  }
  for (const k of touched) sortChanges(graph.functions[k], out.changes.get(k)!)
}

/** 差异 JSON → 界面要的标注。graph 是画布上那张图（新的那次分析）；diff 为 null（没有基准、差异为空）时全空。 */
export function buildDiffView(graph: ProjectGraph | null, diff: GraphDiffJson | null): FlowDiffView {
  const out = emptyDiffView()
  if (!graph || !diff) return out
  /** 有变化的函数 → 给调用它的节点用的说明 */
  const notesOf = new Map<string, string[]>()
  for (const [sid, fc] of Object.entries(diff.functions ?? {})) {
    if (fc.status === 'removed') {
      out.removedFunctions.set(sid, fc.title)
      continue
    }
    const key = functionKey(graph, sid, fc.new_key)
    if (!key) continue
    const notes = addFunction(out, graph.functions[key], fc)
    if (notes) notesOf.set(key, notes)
  }
  attributeCallees(out, graph, notesOf)
  const inGraph = new Set(graph.lines.map((l) => l.sid))
  // 和 core 的 diff_text 里"其他有变化的函数"同一个算法：差异里的函数，去掉有变化的功能线涉及的
  const onLines = new Set((diff.lines ?? []).flatMap((lc) => lc.functions))
  for (const [sid, fc] of Object.entries(diff.functions ?? {})) {
    if (onLines.has(sid)) continue
    const count = fc.status === 'changed' ? fc.nodes.length + fc.edges.length : 0
    const name = (fc.new_key || fc.old_key).split('::')[1] ?? ''
    out.offLine.push({ sid, title: fc.title || name || sid, file: fc.file, name, status: fc.status, count })
  }
  for (const lc of diff.lines ?? []) {
    if (lc.status === 'removed') {
      if (!inGraph.has(lc.sid)) out.removedLines.push({ sid: lc.sid, title: lc.title, group: lc.group })
      continue
    }
    if (!inGraph.has(lc.sid)) continue
    out.lineStatus.set(lc.sid, lc.status)
    const titleOf = (sid: string): string => {
      const k = functionKey(graph, sid, '')
      return diff.functions?.[sid]?.title || (k ? fullTitle(graph.functions[k]) : sid)
    }
    out.lines.set(lc.sid, {
      status: lc.status,
      functions: lc.functions,
      tasksAdded: lc.tasks_added.map((sid) => functionKey(graph, sid, '')).filter((k): k is string => k !== null),
      tasksRemoved: lc.tasks_removed.map((sid) => ({ sid, title: titleOf(sid) })),
    })
  }
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 数

const leadsCache = new WeakMap<FlowDiffView, Set<string>>()

/** 自己有变化、或者能钻入到有变化的函数的那些函数 */
function leadsToChange(dv: FlowDiffView, graph: ProjectGraph): Set<string> {
  let s = leadsCache.get(dv)
  if (s) return s
  s = new Set()
  const stack = [...dv.changes.entries()].filter(([, c]) => c.length > 0).map(([k]) => k)
  const callers = callersOf(graph)
  while (stack.length) {
    const k = stack.pop()!
    if (s.has(k)) continue
    s.add(k)
    for (const c of callers.get(k) ?? []) if (!s.has(c)) stack.push(c)
  }
  leadsCache.set(dv, s)
  return s
}

/** 从 starts 出发沿钻入能到的函数（含 starts 自己，不含 exclude）一共几处变化；每个函数只算一次，有环不死循环 */
function reachCount(dv: FlowDiffView, graph: ProjectGraph, starts: string[], exclude: string | null = null): number {
  const leads = leadsToChange(dv, graph)
  const seen = new Set<string>(exclude ? [exclude] : [])
  const stack = starts.filter((k) => leads.has(k))
  let n = 0
  while (stack.length) {
    const k = stack.pop()!
    if (seen.has(k)) continue
    seen.add(k)
    n += dv.changes.get(k)?.length ?? 0
    for (const node of graph.functions[k]?.nodes ?? []) {
      for (const t of drillTargets(graph, node)) if (leads.has(t) && !seen.has(t)) stack.push(t)
    }
  }
  return n
}

const countCache = new WeakMap<FlowDiffView, Map<string, number>>()

/**
 * 这个函数自己加上它里面能钻入到的函数一共有几处变化（每个函数只算一次）；派发的子任务不算（它们有自己的卡片）。
 * 和功能线无关的数；画布上的数字角标按这条线的每一处落在哪里数（viewMarks），两者在同一张任务卡片上一般相同。
 */
export function cardChangeCount(dv: FlowDiffView, graph: ProjectGraph, functionKey: string): number {
  if (!graph.functions[functionKey] || dv.changes.size === 0) return 0
  const cache = getOrSet(countCache, dv, () => new Map<string, number>())
  return getOrSet(cache, functionKey, () => reachCount(dv, graph, [functionKey]))
}

// ---------------------------------------------------------------------------------------------------------------
// 差异栏：这条线上的每一处变化

/**
 * 这条线上的每一处变化，按画布上从上到下的顺序：任务树先序（根任务在前、子任务在后，goto 不算），同一函数里按节点的
 * 位置（行号）；步骤能钻入的被调函数里的变化排在这个步骤之后（同一张任务卡片里每个函数只列一次，在第一个能钻入它的
 * 步骤之后；两张卡片都能钻入到的函数在两张卡片里各列一次——两处都看得到）。不能钻入的调用（判断函数等）里的变化
 * 落在调用它的节点上（buildDiffView 已经算好），被调函数另能钻入到也照样算：画布上标了圈的节点都是一处。派发树上
 * 少掉的任务、没有任何调用处的有变化的函数放在根任务最后，定位到根任务的卡片。差异里没有这条线（有变化的函数没有
 * 一条线涉及）时为空。
 */
export function lineChangeList(view: FlowDiffView, graph: ProjectGraph, lineId: string): ChangeStop[] {
  const info = view.lines.get(lineId)
  const line = graph.lines.find((l) => l.sid === lineId)
  if (!info || !line) return []
  const tasks: string[] = []
  const visit = (t: LineTask) => {
    if (!t.goto && graph.functions[t.function] && !tasks.includes(t.function)) tasks.push(t.function)
    t.children.forEach(visit)
  }
  visit(line.root)
  if (tasks.length === 0) return []
  /** 这条线上走到过的函数（找不到调用处的放根卡片时用） */
  const seen = new Set<string>()

  /** inCard：这张任务卡片里已经列过的函数（钻入链上不会重复，同一张卡片里也只列一次） */
  const walk = (key: string, task: string, path: ChangeStop['path'], inCard: Set<string>): ChangeStop[] => {
    seen.add(key)
    inCard.add(key)
    const fn = graph.functions[key]
    const pos = positions(fn)
    const entries: { k: number; stops: ChangeStop[] }[] = (view.changes.get(key) ?? []).map((c) => ({
      k: changeKey(c, pos),
      stops: [{ functionKey: key, nodeId: c.at, kind: c.kind, text: c.text, where: c.where, task, path, callees: c.callees }],
    }))
    const ordered = [...fn.nodes].sort((a, b) => pos.get(a.id)! - pos.get(b.id)!)
    for (const n of ordered) {
      for (const callee of drillTargets(graph, n)) {
        if (inCard.has(callee)) continue
        const inner = walk(callee, task, [...path, { fn: key, node: n.id }], inCard)
        if (inner.length) entries.push({ k: pos.get(n.id)! + 0.1, stops: inner })
      }
    }
    entries.sort((a, b) => a.k - b.k)
    return entries.flatMap((e) => e.stops)
  }

  const sections = tasks.map((task) => {
    const out: ChangeStop[] = []
    if (info.tasksAdded.includes(task) && view.functionStatus.get(task) !== 'added') {
      const text = `这条线新派发的任务：${fullTitle(graph.functions[task])}`
      out.push({ functionKey: task, nodeId: null, kind: 'added', text, where: '', task, path: [] })
    }
    out.push(...walk(task, task, [], new Set()))
    return out
  })

  // 根任务最后：派发树上少掉的任务、画布上展开不到的有变化的函数
  const root = tasks[0]
  const extra = (functionKey: string, kind: ChangeKind, text: string, where = ''): ChangeStop => ({
    functionKey,
    nodeId: null,
    kind,
    text,
    where,
    task: root,
    path: [],
  })
  const removedTasks = new Set(info.tasksRemoved.map((t) => t.sid))
  for (const t of info.tasksRemoved) sections[0].push(extra(root, 'removed', `这条线不再派发任务：${t.title}`))
  /** 这条线上看得到它的变化：钻入到了，或者它有调用处在这条线走过的函数里 */
  const covered = (key: string) => seen.has(key) || (view.callSites.get(key) ?? []).some(([w]) => seen.has(w))
  for (const sid of info.functions) {
    const key = functionKey(graph, sid, '')
    if (key) {
      const status = view.functionStatus.get(key)
      if (covered(key) || !status || (view.changes.get(key)?.length ?? 0) === 0) continue
      const f = graph.functions[key]
      const what = status === 'added' ? '是新增的' : '有改动'
      sections[0].push(extra(key, status, `调用的函数「${fullTitle(f)}」${what}（画布上钻入不到，在 ${f.file}）`))
    } else if (!removedTasks.has(sid) && view.removedFunctions.has(sid)) {
      sections[0].push(extra(root, 'removed', `删除了调用的函数：${view.removedFunctions.get(sid)}`))
    }
  }
  return sections.flat()
}

/**
 * 差异栏说"相对什么"：上次标为已看时、第一次分析，或者（旧版后端没说是哪种时）对比基准；when 是显示用的时间
 * （format.ts 的 whenText），不知道时为空串。
 */
export interface BaselineRef {
  kind: 'acknowledged' | 'first' | 'base'
  when: string
}

/** 从 /diff 的结果得出差异栏说"相对什么"；没有对比基准时 null。when 把时间（ISO）变成显示用的文字。 */
export function baselineRef(result: DiffResult | null, when: (iso: string) => string): BaselineRef | null {
  if (!result?.base) return null
  if (result.baseline) return { kind: result.baseline.kind, when: when(result.baseline.at) }
  return { kind: 'base', when: when(result.base.created_at) }
}

/**
 * 两次分析的引擎不一样时差异栏多说的一句（和 core 的 diff.engine_note 同一个意思）：这条线上流程有改动的函数里，
 * 几个所在的文件没改——它们的变化是引擎的画法变了引起的，不是代码改动。引擎没变、这条线上没有这样的函数时 null。
 * lineFunctions：这条线涉及的有变化的函数 sid（差异里这条线的 functions）。
 */
export function engineNote(result: DiffResult | null, lineFunctions: readonly string[]): string | null {
  if (!result?.base || !result.diff || result.base.engine_version === result.head.engine_version) return null
  const changed = lineFunctions.map((sid) => result.diff!.functions?.[sid]).filter((fc) => fc?.status === 'changed')
  const same = changed.filter((fc) => fc!.file_same).length
  if (same === 0) return null
  const which = same === changed.length ? '所在的文件都没改' : `里 ${same} 个所在的文件没改`
  return `引擎也变了（${result.base.engine_version} → ${result.head.engine_version}）：这条线上流程有改动的 ${changed.length} 个函数${which}，是引擎的画法变了引起的，不是代码改动`
}

const SINCE: Record<BaselineRef['kind'], string> = { acknowledged: '上次标为已看时', first: '第一次分析', base: '对比基准' }
const AFTER: Record<BaselineRef['kind'], string> = { acknowledged: '上次标为已看', first: '第一次分析', base: '对比基准' }

/**
 * 差异栏的第一句："这条线相对上次标为已看时（<时间>）：新增 3、改动 1、删除 2"（没标为已看过时"相对第一次分析"；没有的
 * 种类不写）；新增的线写"这条线是上次标为已看（<时间>）之后新增的"。对比基准只在标为已看时移动，所以不说"相对上一次
 * 分析"。ref 不知道时（null）说"对比基准"。
 */
export function diffHeadline(stops: ChangeStop[], status: 'added' | 'changed' | undefined, ref: BaselineRef | null): string {
  const kind = ref?.kind ?? 'base'
  const when = ref?.when ? `（${ref.when}）` : ''
  if (status === 'added') return `这条线是${AFTER[kind]}${when}之后新增的`
  const n = (k: ChangeKind) => stops.filter((s) => s.kind === k).length
  const parts = (
    [
      ['新增', n('added')],
      ['改动', n('changed')],
      ['删除', n('removed')],
    ] as const
  )
    .filter(([, c]) => c > 0)
    .map(([w, c]) => `${w} ${c}`)
  return `这条线相对${SINCE[kind]}${when}：${parts.join('、') || '有变化'}`
}

/**
 * 项目菜单里怎么说未看的流程变化（上次标为已看以来、没标过就是第一次分析以来的）。ackHere：差异栏不会出现（没有功能线
 * 是"有变化 / 新增"的：只删掉了功能线，或者变化都在不属于任何功能线的函数里），只能在菜单里"标为已看"（菜单里现在总是给）。
 */
export function projectDiffNote(dv: FlowDiffView): { text: string; ackHere: boolean } {
  const changed = dv.lineStatus.size
  const removed = dv.removedLines.length
  if (changed) {
    const gone = removed ? `、删掉了 ${removed} 条` : ''
    return { text: `有未看的流程变化：${changed} 条功能线有变化${gone}，左侧列表里标着"有变化""新增"，选中后画布上方可以逐处查看`, ackHere: false }
  }
  if (removed) return { text: `有未看的流程变化：删掉了 ${removed} 条功能线（列在左侧列表最下面）`, ackHere: true }
  const where = dv.offLine.length ? `（${dv.offLine.length} 个，列在左侧列表最下面）` : ''
  return { text: `有未看的流程变化，但都在不属于任何功能线的函数里${where}`, ackHere: true }
}

// ---------------------------------------------------------------------------------------------------------------
// 画布上的标注

export interface ViewMarks {
  /** 画布节点 id → 新增 / 改动 */
  nodes: Map<string, Mark>
  /** 新增的连线（画布连线 id） */
  edges: Set<string>
  /**
   * 数字角标：折叠着的任务卡片、折叠占位、没钻入的步骤里面有这条线的几处变化；步骤换成了钻入链、变化在它还没展开的
   * 被调函数里时，键是那个钻入选项的 id（画在链上卡片的"再展开 N 个"上）。按每一处（lineChangeList）在现在的
   * 展开状态下落在哪里数，所以角标上的数加上看得见的标了圈的那些，正好是差异栏的处数。
   */
  counts: Map<string, number>
  /** 卡片 id（任务卡片、钻入卡片，展开没展开都算）→ 这条线有几处变化在它里面（详情栏写） */
  cardStops: Map<string, number>
  /** 卡片 id → 它的函数是新增的 / 自己的节点改了 / 只是调用的函数变了（goto 卡片不标） */
  cards: Map<string, Mark | 'callee'>
  /** 只因为调用的函数变了才标"改动"的节点（画布节点 id） */
  callee: Set<string>
}

/** 一处变化在现在的视图里落在哪里：要写在哪个数字角标上（看得见、不用写数时为 null），以及经过的卡片 */
function locateStop(
  view: LineView,
  graph: ProjectGraph,
  stop: ChangeStop,
  foldOf: Map<string, string>,
  cardIds: Set<string>,
  nodeIds: Set<string>,
): { badge: string | null; cards: string[] } {
  const card = view.cards.find((c) => c.kind === 'task' && !c.goto && c.fn === stop.task)
  if (!card) return { badge: null, cards: [] }
  const cards = [card.id]
  if (!card.expanded) return { badge: card.expandable ? card.id : null, cards }
  const viewId = (host: string, fnKey: string, nodeId: string): string | null => {
    const n = graph.functions[fnKey]?.nodes.find((x) => x.id === nodeId)
    return n ? `${host}/${nodeSid(n)}` : null
  }
  let host = card.id
  for (let i = 0; i < stop.path.length; i++) {
    const vid = viewId(host, stop.path[i].fn, stop.path[i].node)
    if (!vid) return { badge: null, cards }
    const fold = foldOf.get(vid)
    if (fold) return { badge: fold, cards }
    const callee = graph.functions[stop.path[i + 1]?.fn ?? stop.functionKey]
    if (!callee) return { badge: null, cards }
    const drill = drillId(vid, callee)
    if (!cardIds.has(drill)) {
      // 还没钻入：数写在这个步骤上。步骤已经换成了钻入链（钻入了它调用的别的函数）时写在这个被调函数的钻入选项上
      // （链上卡片的"再展开 N 个"按它标）；它在这里钻入不了（已经在钻入链上）时写在链的第一张上
      if (nodeIds.has(vid)) return { badge: vid, cards }
      const chain = drillChainOf(view, vid)
      if (!chain.length) return { badge: null, cards }
      return { badge: chain[0].anchor?.drills.some((d) => d.id === drill) ? drill : chain[0].id, cards }
    }
    cards.push(drill)
    host = drill
  }
  if (stop.nodeId === null) return { badge: null, cards }
  const vid = viewId(host, stop.functionKey, stop.nodeId)
  return { badge: (vid && foldOf.get(vid)) || null, cards }
}

/**
 * 一条线的视图上，哪些东西要标出来（节点组件、连线、小地图按它画；改动不影响排版）。stops 是这条线的每一处变化
 * （lineChangeList），数字角标按它们落在哪里数。
 */
export function viewMarks(view: LineView | null, dv: FlowDiffView, graph: ProjectGraph | null, stops: ChangeStop[]): ViewMarks {
  const out: ViewMarks = { nodes: new Map(), edges: new Set(), counts: new Map(), cardStops: new Map(), cards: new Map(), callee: new Set() }
  if (!view || !graph || (dv.functionStatus.size === 0 && dv.calleeOnly.size === 0)) return out
  const cardFn = new Map(view.cards.map((c) => [c.id, c.fn]))
  for (const c of view.cards) {
    if (!c.fn || c.goto) continue
    const st = dv.functionStatus.get(c.fn) ?? (dv.calleeOnly.has(c.fn) ? 'callee' : undefined)
    if (st) out.cards.set(c.id, st)
  }
  for (const n of view.nodes) {
    const fn = cardFn.get(n.card)
    if (!fn) continue
    const m = dv.nodeMarks.get(fn)?.get(n.key)
    if (m) out.nodes.set(n.id, m)
    if (dv.calleeMarked.get(fn)?.has(n.key)) out.callee.add(n.id)
  }
  // 折叠着的、看得见的折叠占位：里面每个节点（含更里层折叠里的）→ 占位
  const foldOf = new Map<string, string>()
  for (const f of view.folds) if (f.collapsed) for (const m of f.members) foldOf.set(m, f.id)
  const cardIds = new Set(view.cards.map((c) => c.id))
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  for (const stop of stops) {
    const { badge, cards } = locateStop(view, graph, stop, foldOf, cardIds, nodeIds)
    for (const c of cards) out.cardStops.set(c, (out.cardStops.get(c) ?? 0) + 1)
    if (badge) out.counts.set(badge, (out.counts.get(badge) ?? 0) + 1)
  }
  for (const e of view.edges) {
    const fn = cardFn.get(e.card)
    const set = fn ? dv.edgeMarks.get(fn) : undefined
    if (set && e.keys.some((k) => set.has(k))) out.edges.add(e.id)
  }
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 定位到一处变化

export interface Reveal {
  /** 让目标露出来的展开状态（不用改时就是传进来的那个对象） */
  state: ViewState
  /** 画布上要选中、移到中间的元素 id */
  target: string
  kind: 'node' | 'card'
}

/**
 * 差异栏的"上一处 / 下一处"：要看到 stop 需要怎样的展开状态（展开它所在的任务卡片、钻入路上的步骤、展开把节点
 * 收起来的分支），以及画布上的目标元素。删掉的那些定位到它原来前面的节点（stop.nodeId 已经是它）。任务不在这条线
 * 上时为 null。
 */
export function revealStop(graph: ProjectGraph, lineSid: string, state: ViewState, stop: ChangeStop): Reveal | null {
  let s = state
  let v: LineView
  try {
    v = buildLineView(graph, lineSid, s)
  } catch {
    return null
  }
  const card = v.cards.find((c) => c.kind === 'task' && !c.goto && c.fn === stop.task)
  if (!card) return null
  if (stop.nodeId === null && stop.path.length === 0) return { state: s, target: card.id, kind: 'card' }
  if (!card.expanded && card.expandable) s = setOpen(s, 'cards', card.id, true)

  /** 节点在画布上的 id：所在卡片 id + 节点 sid */
  const viewId = (host: string, fnKey: string, nodeId: string): string | null => {
    const n = graph.functions[fnKey]?.nodes.find((x) => x.id === nodeId)
    return n ? `${host}/${nodeSid(n)}` : null
  }
  /** 看得见：画成了节点，或者钻入着（被钻入链代替） */
  const shown = (id: string) => v.nodes.some((n) => n.id === id) || drillChainOf(v, id).length > 0
  /** 一层层展开把它收起来的分支 */
  const reveal = (id: string): boolean => {
    for (let round = 0; round < 50; round++) {
      v = buildLineView(graph, lineSid, s)
      if (shown(id)) return true
      const f = v.folds.find((x) => x.collapsed && x.members.includes(id))
      if (!f) return false
      s = setOpen(s, 'folds', f.id, true)
    }
    return false
  }

  let host = card.id
  for (let i = 0; i < stop.path.length; i++) {
    const step = stop.path[i]
    const callee = graph.functions[stop.path[i + 1]?.fn ?? stop.functionKey]
    const id = viewId(host, step.fn, step.node)
    if (!id || !callee || !reveal(id)) return { state: s, target: host, kind: 'card' }
    const drill = drillId(id, callee)
    // 钻入这一个被调函数；这个步骤已经钻入着的别的函数照旧开着（钻入链上多一张）
    if (s.drills[drill] !== true) s = setOpen(s, 'drills', drill, true)
    host = drill
  }
  if (stop.nodeId === null) return { state: s, target: host, kind: 'card' }
  const target = viewId(host, stop.functionKey, stop.nodeId)
  if (!target || !reveal(target)) return { state: s, target: host, kind: 'card' }
  if (v.nodes.some((n) => n.id === target)) return { state: s, target, kind: 'node' }
  // 这个步骤钻入着：定位到代替它的钻入链的第一张
  return { state: s, target: drillChainOf(v, target)[0]?.id ?? host, kind: 'card' }
}
