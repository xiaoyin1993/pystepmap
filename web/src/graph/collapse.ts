// 折叠规则：对一个函数图找出主干和可折叠的区域，决定默认状态。
//
// 1. 功能线的根任务卡片默认展开，其余任务卡片默认折叠（cardExpandedByDefault）。
// 2. 提前结束的短分支默认折叠：从判断节点的一条分支出发，只有这条分支能进入的那些节点（"独占区域"）不超过
//    shortBranchMax 个（含结局）、全部以结局收尾、不含主干上的节点，就折叠成一个占位节点。
// 3. 异常处理默认折叠：捕获节点和只有它能进入的处理体，其中的结局都是失败 / 异常（或者没有结局、汇回后面的
//    节点），折叠成一个占位节点（捕获节点本身也在里面，占位节点上写异常类型）。
// 4. 每个折叠区域的展开状态各自独立（界面状态按 id 记，见 viewmodel）。
// 补充（简报之外，见 sideBranchMin、longTrunk）：汇回的长分支。判断节点的一条分支，独占区域至少 sideBranchMin
//    个节点、走完后往下汇回同一个节点，可以折叠成一个占位节点，判断节点留着当这一段的标题。主干超过 longTrunk 个
//    节点的长函数里默认折叠，其余函数里默认展开（界面仍可收起）。没有这一条时，规则 2、3 只收掉主干旁边的东西，
//    主干一个节点也不少：process_inbound_email 默认折叠后仍有 58 层、5700 多像素高。
//
// 主干：从开始节点到"正常结局"的最长路径（按节点数）；不走异常连线（raise）、不走回边、不穿过结局节点。
// 正常结局优先取 ok，没有时依次取 skipped、failed、exception，再没有就取最长的路径。

import type { EndKind, FunctionGraph, GraphEdge, GraphNode } from './types'

export interface CollapseOptions {
  /** 提前结束的短分支：独占区域最多几个节点（含结局）算"短"。默认 4 */
  shortBranchMax: number
  /** 少于几个节点的区域不折叠：只有一个结局节点的分支，占位节点不比它小，还藏起了结局。默认 2 */
  minFoldNodes: number
  /** 异常处理是否默认折叠。默认 true */
  foldCatches: boolean
  /** 汇回的长分支：独占区域至少几个节点、走完后往下汇回同一个节点的分支可以折叠。0 = 不提供这种折叠。默认 3 */
  sideBranchMin: number
  /**
   * 主干超过几个节点算长函数：长函数里汇回的长分支默认折叠，其余函数里默认展开。默认 30（约 3000 像素高）。
   * Infinity = 只按简报的规则 2、3 决定默认状态。
   */
  longTrunk: number
}

export const DEFAULT_COLLAPSE: CollapseOptions = {
  shortBranchMax: 4,
  minFoldNodes: 2,
  foldCatches: true,
  sideBranchMin: 3,
  longTrunk: 30,
}

/** 规则 1：功能线的根任务默认展开，其余任务卡片默认折叠；goto 卡片没有内容，总是折叠。 */
export function cardExpandedByDefault(card: { isRoot: boolean; goto: boolean }): boolean {
  return card.isRoot && !card.goto
}

/** exit：提前结束的短分支（规则 2）；side：汇回的长分支（补充规则）；catch：异常处理（规则 3） */
export type FoldKind = 'exit' | 'side' | 'catch'

/** 一个可折叠区域（函数图内）。 */
export interface FoldRegion {
  /** 函数内唯一、跨两次分析稳定："<判断节点 sid>:<分支>"（分支经 encodeURIComponent）或 "<捕获节点 sid>" */
  key: string
  kind: FoldKind
  /** 判断节点（exit / side）或捕获节点（catch）的图内 id */
  anchor: string
  /** exit / side：分支（yes / no / 模式 / else）；catch：null */
  branch: string | null
  /** 区域里的节点（图内 id），按节点顺序；catch 区域含捕获节点本身 */
  members: string[]
  defaultCollapsed: boolean
  /** 包含它的更大的区域的 key（区域之间只会嵌套或不相交） */
  parent: string | null
}

export interface FunctionAnalysis {
  fn: FunctionGraph
  nodes: Map<string, GraphNode>
  /** 节点在 fn.nodes 里的位置 */
  order: Map<string, number>
  out: Map<string, GraphEdge[]>
  in: Map<string, GraphEdge[]>
  /** 回边：深度优先遍历时指回祖先的连线（循环的 loop_back、自环等） */
  back: Set<GraphEdge>
  /** 判断节点里属于 match 的 */
  matchNodes: Set<string>
  /** 主干上的节点，按路径顺序 */
  trunk: string[]
  trunkSet: Set<string>
  folds: FoldRegion[]
}

const isEnd = (n: GraphNode | undefined): boolean => n?.type === 'end'

/**
 * 结局经过 finally 离开函数的那一段（结局节点 → finally 体）。判断、步骤节点的 finally 连线不是：那是 break /
 * continue 穿过 finally（graph-schema.md "读连线"第 3 点），走完 finally 还要接着往下。
 */
export const endsThroughFinally = (a: { nodes: Map<string, GraphNode> }, e: GraphEdge): boolean =>
  e.kind === 'finally' && isEnd(a.nodes.get(e.src))

/** 节点在显示和 id 里用的稳定标识：sid（旧图没有时退回图内 id）。 */
export const nodeSid = (n: GraphNode): string => n.sid || n.id

/** 判断节点是不是 match（graph-schema.md "怎样认出分支"）。 */
function isMatchNode(n: GraphNode, outs: GraphEdge[]): boolean {
  if (n.type !== 'cond' || !n.subtitle.startsWith('match ')) return false
  return !outs.some(
    (e) => e.kind !== 'raise' && (e.kind === 'yes' || e.kind === 'no' || e.label === '是' || e.label === '否'),
  )
}

/**
 * 连线是哪个分支：判断节点的非 raise 出边才有。有 branch 字段时用它；旧图从 kind / label 推断：
 * kind yes / no → yes / no；改了 kind 的 label 是 / 否 → yes / no；match 的 label 是模式，都不匹配 → else。
 */
export function edgeBranch(src: GraphNode | undefined, e: GraphEdge, isMatch: boolean): string | null {
  if (!src || src.type !== 'cond' || e.kind === 'raise') return null
  if (e.branch) return e.branch
  if (isMatch) {
    if (e.label === '都不匹配') return 'else'
    return e.label || null
  }
  if (e.kind === 'yes' || e.label === '是') return 'yes'
  if (e.kind === 'no' || e.label === '否') return 'no'
  return null
}

/** 分支的显示文字：if 的是 / 否，match 的模式原文，没有兜底的"都不匹配"。 */
export function branchLabel(branch: string | null, isMatch: boolean): string {
  if (branch === null) return ''
  if (!isMatch) return branch === 'yes' ? '是' : branch === 'no' ? '否' : branch
  return branch === 'else' ? '都不匹配' : branch
}

function findBackEdges(fn: FunctionGraph, out: Map<string, GraphEdge[]>): Set<GraphEdge> {
  const back = new Set<GraphEdge>()
  const state = new Map<string, 1 | 2>() // 1 = 在栈上，2 = 完成
  const visit = (root: string) => {
    // 迭代式深度优先，避免大函数爆栈
    const stack: { id: string; i: number }[] = [{ id: root, i: 0 }]
    state.set(root, 1)
    while (stack.length) {
      const top = stack[stack.length - 1]
      const edges = out.get(top.id) ?? []
      if (top.i < edges.length) {
        const e = edges[top.i++]
        const s = state.get(e.dst)
        if (s === 1) back.add(e)
        else if (s === undefined && out.has(e.dst)) {
          state.set(e.dst, 1)
          stack.push({ id: e.dst, i: 0 })
        }
      } else {
        state.set(top.id, 2)
        stack.pop()
      }
    }
  }
  const start = fn.nodes.find((n) => n.type === 'start')
  if (start) visit(start.id)
  for (const n of fn.nodes) if (!state.has(n.id)) visit(n.id)
  return back
}

const END_PREFERENCE: EndKind[] = ['ok', 'skipped', 'failed', 'exception']

function computeTrunk(a: Omit<FunctionAnalysis, 'trunk' | 'trunkSet' | 'folds'>): string[] {
  const { fn, out, back, nodes, order } = a
  const start = fn.nodes.find((n) => n.type === 'start')
  if (!start) return []
  const usable = (e: GraphEdge) => e.kind !== 'raise' && !back.has(e) && !isEnd(nodes.get(e.src)) && nodes.has(e.dst)
  // 从开始节点能走到的节点，以及它们之间的入度（只算可用的连线）
  const reach = new Set<string>([start.id])
  const queue = [start.id]
  while (queue.length) {
    const v = queue.shift()!
    for (const e of out.get(v) ?? []) {
      if (usable(e) && !reach.has(e.dst)) {
        reach.add(e.dst)
        queue.push(e.dst)
      }
    }
  }
  const indeg = new Map<string, number>()
  for (const v of reach) for (const e of out.get(v) ?? []) if (usable(e)) indeg.set(e.dst, (indeg.get(e.dst) ?? 0) + 1)
  const dist = new Map<string, number>([[start.id, 1]])
  const pred = new Map<string, string>()
  const ready = [start.id]
  while (ready.length) {
    const v = ready.shift()!
    for (const e of out.get(v) ?? []) {
      if (!usable(e)) continue
      const d = dist.get(v)! + 1
      const cur = dist.get(e.dst)
      // 同样长时取源码里靠后的前驱（顺着往下写的那条），结果与连线顺序无关
      if (cur === undefined || d > cur || (d === cur && order.get(v)! > order.get(pred.get(e.dst)!)!)) {
        dist.set(e.dst, d)
        pred.set(e.dst, v)
      }
      const left = indeg.get(e.dst)! - 1
      indeg.set(e.dst, left)
      if (left === 0) ready.push(e.dst)
    }
  }
  const pickBest = (cands: string[]) =>
    cands.reduce<string | null>((best, v) => {
      if (best === null) return v
      const dv = dist.get(v)!
      const db = dist.get(best)!
      return dv > db || (dv === db && order.get(v)! > order.get(best)!) ? v : best
    }, null)
  let target: string | null = null
  for (const kind of END_PREFERENCE) {
    target = pickBest([...dist.keys()].filter((v) => nodes.get(v)!.end_kind === kind))
    if (target) break
  }
  target ??= pickBest([...dist.keys()])
  const path: string[] = []
  for (let v: string | undefined = target ?? undefined; v !== undefined; v = pred.get(v)) path.push(v)
  return path.reverse()
}

/**
 * 从 entry 起只有经过 entry 才能进入的节点（"独占区域"，不算回边）。超过 limit 个就放弃，返回 null。
 * entry 自己的入边不检查（由调用方决定）。
 */
function exclusiveRegion(a: Omit<FunctionAnalysis, 'folds'>, entry: string, limit: number): Set<string> | null {
  const region = new Set<string>([entry])
  let grew = true
  while (grew) {
    grew = false
    for (const v of [...region]) {
      for (const e of a.out.get(v) ?? []) {
        const w = e.dst
        if (region.has(w) || !a.nodes.has(w)) continue
        const ins = (a.in.get(w) ?? []).filter((x) => !a.back.has(x))
        if (ins.length > 0 && ins.every((x) => region.has(x.src))) {
          region.add(w)
          grew = true
          if (region.size > limit) return null
        }
      }
    }
  }
  return region
}

function branchFold(
  a: Omit<FunctionAnalysis, 'folds'>,
  anchor: GraphNode,
  e: GraphEdge,
  branch: string,
  opts: CollapseOptions,
): FoldRegion | null {
  const t = e.dst
  if (t === anchor.id || !a.nodes.has(t)) return null
  // 分支入口只能从这个判断节点的这条分支进入（同一判断节点并行的 raise 连线可以）
  const entryIns = (a.in.get(t) ?? []).filter((x) => !a.back.has(x))
  if (!entryIns.every((x) => x.src === anchor.id && (x === e || x.kind === 'raise'))) return null
  const region = exclusiveRegion(a, t, Number.POSITIVE_INFINITY)!
  if (region.size < opts.minFoldNodes) return null
  let reachesEnd = false
  let onTrunk = false
  const rejoin = new Set<string>()
  let loopsBack = false
  for (const v of region) {
    if (a.trunkSet.has(v)) onTrunk = true
    if (isEnd(a.nodes.get(v))) reachesEnd = true
    for (const x of a.out.get(v) ?? []) {
      // 异常连线、结局进入 finally 体的连线不算离开（异常往外抛、结局经过 finally 离开函数）。
      // 别的节点的 finally 连线是 break / continue 穿过 finally：接着往下走，算离开
      if (region.has(x.dst) || x.kind === 'raise' || endsThroughFinally(a, x)) continue
      // 到别的（不在主干上的）结局节点也算以结局收尾
      if (isEnd(a.nodes.get(x.dst)) && !a.trunkSet.has(x.dst)) reachesEnd = true
      else rejoin.add(x.dst)
      if (a.back.has(x)) loopsBack = true
    }
  }
  const fold = (kind: FoldKind): FoldRegion => ({
    key: `${nodeSid(anchor)}:${encodeURIComponent(branch)}`,
    kind,
    anchor: anchor.id,
    branch,
    members: sortByOrder(a, region),
    defaultCollapsed: kind === 'exit' || a.trunk.length > opts.longTrunk,
    parent: null,
  })
  if (rejoin.size === 0) {
    // 规则 2：全部以结局收尾、不含主干上的节点、不超过 N 个
    return reachesEnd && !onTrunk && region.size <= opts.shortBranchMax ? fold('exit') : null
  }
  // 补充规则：往下汇回同一个节点的长分支。经回边回到循环头的（continue、循环体走完）不算：那是循环体本身，
  // 折起来就把整个循环的内容藏了
  return opts.sideBranchMin > 0 && rejoin.size === 1 && !loopsBack && region.size >= opts.sideBranchMin
    ? fold('side')
    : null
}

function catchFold(a: Omit<FunctionAnalysis, 'folds'>, c: GraphNode, opts: CollapseOptions): FoldRegion | null {
  const region = exclusiveRegion(a, c.id, Number.POSITIVE_INFINITY)
  if (!region || region.size < opts.minFoldNodes) return null
  for (const v of region) {
    const n = a.nodes.get(v)!
    if (a.trunkSet.has(v)) return null
    if (isEnd(n) && n.end_kind !== 'failed' && n.end_kind !== 'exception') return null
  }
  return {
    key: nodeSid(c),
    kind: 'catch',
    anchor: c.id,
    branch: null,
    members: sortByOrder(a, region),
    defaultCollapsed: opts.foldCatches,
    parent: null,
  }
}

const sortByOrder = (a: { order: Map<string, number> }, ids: Iterable<string>): string[] =>
  [...ids].sort((x, y) => a.order.get(x)! - a.order.get(y)!)

/** 分析一个函数图：回边、主干、可折叠区域。纯函数，结果只由函数图和选项决定。 */
export function analyzeFunction(fn: FunctionGraph, options: Partial<CollapseOptions> = {}): FunctionAnalysis {
  const opts = { ...DEFAULT_COLLAPSE, ...options }
  const nodes = new Map(fn.nodes.map((n) => [n.id, n]))
  const order = new Map(fn.nodes.map((n, i) => [n.id, i]))
  const out = new Map<string, GraphEdge[]>(fn.nodes.map((n) => [n.id, []]))
  const inn = new Map<string, GraphEdge[]>(fn.nodes.map((n) => [n.id, []]))
  for (const e of fn.edges) {
    if (!nodes.has(e.src) || !nodes.has(e.dst)) continue // 指向不存在节点的连线：忽略，不报错
    out.get(e.src)!.push(e)
    inn.get(e.dst)!.push(e)
  }
  const back = findBackEdges(fn, out)
  const matchNodes = new Set(fn.nodes.filter((n) => isMatchNode(n, out.get(n.id)!)).map((n) => n.id))
  const base = { fn, nodes, order, out, in: inn, back, matchNodes }
  const trunk = computeTrunk(base)
  const a = { ...base, trunk, trunkSet: new Set(trunk) }

  const candidates: FoldRegion[] = []
  for (const n of fn.nodes) {
    if (n.type === 'cond') {
      const isMatch = matchNodes.has(n.id)
      for (const e of out.get(n.id)!) {
        const branch = edgeBranch(n, e, isMatch)
        if (branch === null) continue
        const f = branchFold(a, n, e, branch, opts)
        if (f) candidates.push(f)
      }
    } else if (n.type === 'catch') {
      const f = catchFold(a, n, opts)
      if (f) candidates.push(f)
    }
  }
  return { ...a, folds: laminar(candidates, order) }
}

/** 只留互相嵌套或不相交的区域（大的先留），并填上 parent。 */
function laminar(cands: FoldRegion[], order: Map<string, number>): FoldRegion[] {
  const sorted = [...cands].sort((x, y) => y.members.length - x.members.length)
  const kept: { f: FoldRegion; set: Set<string> }[] = []
  const seenKeys = new Set<string>()
  for (const f of sorted) {
    if (seenKeys.has(f.key)) continue
    const set = new Set(f.members)
    let ok = true
    let parent: { f: FoldRegion; set: Set<string> } | null = null
    for (const k of kept) {
      const inter = f.members.filter((m) => k.set.has(m)).length
      if (inter === 0) continue
      if (inter === f.members.length && f.members.length < k.set.size) {
        if (!parent || k.set.size < parent.set.size) parent = k
        continue
      }
      ok = false // 部分重叠或与已有区域相同：不要
      break
    }
    if (!ok) continue
    seenKeys.add(f.key)
    kept.push({ f: { ...f, parent: parent ? parent.f.key : null }, set })
  }
  // 输出顺序：按锚点在函数里的位置，同一锚点按 key，稳定
  return kept
    .map((k) => k.f)
    .sort((x, y) => order.get(x.anchor)! - order.get(y.anchor)! || (x.key < y.key ? -1 : x.key > y.key ? 1 : 0))
}
