// 视图模型：把图 JSON 变成画布要画的东西（功能线 → 任务卡片 → 节点和连线），折叠规则见 collapse.ts。
// buildLineView 是纯函数：结果只由图、功能线、界面状态和选项决定；id 都由 sid 拼成，重新排版、切换状态后不变。
//
// id 的拼法（界面用它们做 Vue Flow 的节点 / 连线 id）：
//   任务卡片   <功能线 sid>|<根任务函数 sid>><……>><本任务函数 sid>      （任务树里从根到它的路径）
//   节点       <卡片 id>/<节点 sid>
//   折叠占位   <卡片 id>/fold:<判断节点 sid>:<分支>  或  <卡片 id>/fold:<捕获节点 sid>
//   钻入卡片   <被钻入的步骤节点 id>><被调用函数 sid>
//   连线       e:<起点 id>-><终点 id>:<kind>[:<分支>]
//   钻入链     chain:<前一张钻入卡片 id>-><后一张钻入卡片 id>
//   派发连线   link:<起点 id>=><任务卡片 id>
//
// 钻入：一个步骤调用了几个能钻入的函数时，可以同时钻入几个。这个步骤由一串钻入卡片（钻入链）代替，一个开着的被调函数
// 一张，按步骤的钻入选项的顺序（= 图里 calls 的顺序，源码里出现的先后，外层的调用在前；不一定是执行的先后）。进这个
// 步骤的连线接到第一张，出这个步骤的连线从最后一张出发，相邻两张之间是一条钻入链的连接线（不是图里的连线）。

import {
  analyzeFunction,
  branchLabel,
  cardExpandedByDefault,
  edgeBranch,
  endsThroughFinally,
  nodeSid,
  type CollapseOptions,
  type FoldKind,
  type FunctionAnalysis,
} from './collapse'
import {
  type EdgeKind,
  type EndKind,
  type FlowLine,
  type FnKind,
  type FunctionGraph,
  type GraphNode,
  type LineTask,
  type NodeType,
  type ProjectGraph,
  type Resolution,
} from './types'

// ---------------------------------------------------------------------------------------------------------------
// 界面状态

/**
 * 界面状态：只记用户改过的地方，没记的按默认（collapse.ts）。每处各自独立，改一处不影响别处。
 * 值都是"是否展开"：cards 任务卡片，folds 折叠占位（true = 展开），drills 钻入卡片（true = 钻入）。
 */
export interface ViewState {
  cards: Record<string, boolean>
  folds: Record<string, boolean>
  drills: Record<string, boolean>
}

export const emptyState = (): ViewState => ({ cards: {}, folds: {}, drills: {} })

/** 返回改了一处的新状态（不改原对象）。 */
export function setOpen(state: ViewState, what: keyof ViewState, id: string, open: boolean): ViewState {
  return { ...state, [what]: { ...state[what], [id]: open } }
}

// ---------------------------------------------------------------------------------------------------------------
// 视图
//
// 文字有两种形式。画在画布上的（节点、卡片标题栏、折叠占位、连线标签）是引擎截断过的显示形式（title、subtitle、
// label），尺寸按它们算；另带一份完整的（titleFull、subtitleFull、labelFull：图里有 *_full 就用它，否则同显示形式），
// 给悬停浮层、详情栏、"复制给 AI" 用。只在详情栏和浮层里列出的（调用的目标、派发的任务、折叠里的入口和结局）直接是
// 完整的。

export interface SourceLoc {
  /** 相对项目根的路径 */
  file: string
  line: number
  endLine: number
  /** 绝对路径（项目根 + file），给"在编辑器中打开" */
  path: string
}

export interface CallView {
  name: string
  target: string | null
  /** 被调用函数（派发时是任务）的完整标题；没有目标时为 null */
  targetTitle: string | null
  resolution: Resolution
  isDispatch: boolean
  conditional: boolean
  /** 非派发调用：目标内部会派发的任务 key */
  dispatches: string[]
}

export interface DispatchView {
  /** 被派发任务的 key */
  task: string
  /** 被派发任务的完整标题 */
  title: string
  /** 节点自己的调用就是派发（否则是它调用的函数里派发的） */
  direct: boolean
  /** 公共任务：不并入功能线，没有卡片 */
  common: boolean
  /** 派发调用（或带出派发的调用）的解析等级，最弱的那个 */
  resolution: Resolution
  /** 本条功能线里这个任务的卡片 id（goto 卡片除外，跳到第一次出现的那张）；不在线里时为空 */
  card: string | null
}

export interface DrillOption {
  /** 钻入卡片的 id，也是 ViewState.drills 的键 */
  id: string
  fn: string
  title: string
  name: string
  resolution: Resolution
  open: boolean
}

export type ImplicitKind = 'start' | 'end' | 'uncaught'

export interface NodeView {
  id: string
  /** 所在卡片（任务卡片或钻入卡片）的 id */
  card: string
  /** 图内 id（n1、n2……），对应 selfcheck、执行记录 */
  key: string
  sid: string
  type: NodeType
  title: string
  subtitle: string
  /** 完整的标题、副标题（被引擎截断时取图里的 title_full / subtitle_full，否则同 title / subtitle） */
  titleFull: string
  subtitleFull: string
  endKind: EndKind | null
  /** 源码位置；隐含的"执行到函数末尾""未捕获异常退出"没有（行号为 0） */
  loc: SourceLoc | null
  calls: CallView[]
  /** 调用里有引擎推测的（guessed） */
  guessed: boolean
  /** 调用里有无法判断目标的（unresolved） */
  unresolved: boolean
  /** 这一步会派发的任务（直接派发或经被调用的函数派发） */
  dispatches: DispatchView[]
  fields: Record<string, string>
  /** 转交返回：被调用函数的 key */
  passthrough: string | null
  /** 可以钻入的被调用函数 */
  drills: DrillOption[]
  /** 在主干上 */
  trunk: boolean
  implicit: ImplicitKind | null
  /** 包含它的、已展开的最内层折叠区域的 id（界面可在那里放"收起"） */
  fold: string | null
  /** 在函数图里的位置（fn.nodes 的下标），排版用来定同层的先后 */
  order: number
}

export interface FoldSummary {
  /** 里面几个节点 */
  nodes: number
  /** 里面的结局（按节点顺序），完整标题 */
  ends: { kind: EndKind | null; title: string }[]
  /** 结局种类，去重，按 ok / skipped / failed / exception */
  endKinds: EndKind[]
  /** 走完后汇回后面的节点（异常处理常见） */
  rejoins: boolean
  /** 第一个节点的完整标题 */
  first: string
  /** 里面派发的任务的完整标题 */
  dispatches: string[]
  /** 里面有引擎推测的调用（折叠占位上标"推测"） */
  guessed: boolean
  /** 里面有无法判断目标的调用（折叠占位上标"目标不明"） */
  unresolved: boolean
}

export interface FoldView {
  id: string
  card: string
  kind: FoldKind
  /** exit / side：判断节点的 id（占位节点挂在它的这条分支上）；catch：捕获节点的 id（它在区域里） */
  anchor: string
  branch: string | null
  /** exit / side：分支的显示文字（是 / 否 / 模式 / 都不匹配）；catch：捕获的异常类型 */
  label: string
  /** label 的完整形式（模式、异常类型被引擎截断时） */
  labelFull: string
  /** exit / side：入口节点的标题；catch：捕获节点的标题（显示形式，画在占位上） */
  title: string
  /** title 的完整形式 */
  titleFull: string
  collapsed: boolean
  /** 包含它的折叠区域（已展开的）id */
  parent: string | null
  /** 展开后里面的节点 id */
  members: string[]
  /** 里面的节点在函数图里的 id（n1、n2……，和 members 一一对应），流程差异的标注按它找 */
  keys: string[]
  summary: FoldSummary
  /** 第一个成员在函数图里的位置 */
  order: number
  /** 里面有主干上的节点（主干穿过这个折叠） */
  trunk: boolean
}

export interface EdgeView {
  id: string
  /** 两端所在的卡片（排版时连线放在这里） */
  card: string
  /** 起点、终点：节点、折叠占位或钻入卡片的 id */
  src: string
  dst: string
  kind: EdgeKind
  /** 判断节点的分支（yes / no / 模式 / else）；不是分支时为 null */
  branch: string | null
  /**
   * 显示用的标签：分支是 是 / 否 / 模式 / 都不匹配；循环节点的 loop_each / loop_exit 出边是 每一项（while 为
   * 条件成立）/ 循环结束；其余为空串
   */
  label: string
  /** label 的完整形式（case 模式被引擎截断时取图里的 label_full，否则同 label） */
  labelFull: string
  /** 回边（循环回到循环头等）：排版时逆着层次走 */
  back: boolean
  /** 在主干上：图里连着主干上相邻的两个节点（两端被折叠、钻入时照旧） */
  trunk: boolean
  /**
   * 它代表的图里的连线，"起点 id|终点 id|kind|label"（图内节点 id）；两端被折叠时几条并成一条，都列上。
   * 流程差异的标注（新增的连线）按它找。钻入链的连接线没有（不是图里的连线）。
   */
  keys: string[]
  /**
   * 钻入链的连接线：同一个步骤的相邻两张钻入卡片之间（kind 为 next，没有标签、keys 为空）。不是图里的连线，不算走过、
   * 新增、删除；画成一条细的中性的线（同一条语句里调用的几个函数，按源码里出现的先后排，不一定是执行的先后）。
   */
  chain: boolean
}

/** 钻入链的连接线、钻入卡片上说"几个函数按什么先后排"的话 */
export const CHAIN_NOTE = '同一条语句里调用的几个函数，按源码里出现的先后排，不一定是执行的先后'

export interface LinkView {
  id: string
  /** 发出派发的节点（被折叠时是占位节点、被钻入时是钻入卡片里的节点）；所在卡片折叠时是卡片本身 */
  src: string
  /** 被派发任务的卡片 id */
  dst: string
  /** 起点所在的任务卡片 */
  srcCard: string
  direct: boolean
  resolution: Resolution
}

export interface CardSummary {
  /** 函数图的节点数 */
  nodes: number
  /** 各种结局各几个（不算隐含的"未捕获异常退出"） */
  ends: Partial<Record<EndKind, number>>
}

export interface CardView {
  id: string
  /** task：功能线任务树里的任务；drill：钻入的被调用函数（内嵌在 parent 卡片里，替代那个步骤节点） */
  kind: 'task' | 'drill'
  /** 包含它的卡片：任务卡片为 null，钻入卡片为步骤所在的卡片 */
  parent: string | null
  /** 任务卡片：任务树里派发它的那张卡片；根任务和钻入卡片为 null */
  dispatcher: string | null
  /** 任务树里的深度（根为 0）；钻入卡片为嵌套层数（从 1 起） */
  depth: number
  /** 函数 key；图里没有这个函数时为 null */
  fn: string | null
  fnSid: string
  title: string
  /** 完整的标题（函数标题被引擎截断时取图里的 title_full，否则同 title） */
  titleFull: string
  /** 函数名（限定名） */
  name: string
  fnKind: FnKind | null
  route: string | null
  retries: boolean
  loc: SourceLoc | null
  expanded: boolean
  /** 有内容可展开（不是 goto、函数在图里且有节点） */
  expandable: boolean
  goto: boolean
  /** goto 卡片：这个任务在本条线里第一次出现的卡片 id */
  gotoTarget: string | null
  /** 图里没有这个函数 */
  missing: boolean
  summary: CardSummary
  /**
   * 钻入卡片：被它替代的步骤节点（同一个步骤的钻入链上的卡片是同一个；它的 drills 里 open 的就是链上的卡片，没 open 的
   * 是还没展开的被调函数）
   */
  anchor: NodeView | null
  /** 钻入卡片：在这个步骤的钻入链上排第几（从 0 起）；任务卡片为 0 */
  chainIndex: number
}

export interface LineView {
  line: { id: string; sid: string; title: string; titleFull: string; group: string; kind: FnKind }
  /** 任务卡片按任务树先序，钻入卡片跟在所在卡片之后 */
  cards: CardView[]
  /** 展开的卡片里看得见的节点 */
  nodes: NodeView[]
  /** 看得见的折叠区域：collapsed 的画成占位节点；已展开的也列出，供界面放"收起" */
  folds: FoldView[]
  edges: EdgeView[]
  links: LinkView[]
}

// ---------------------------------------------------------------------------------------------------------------
// 构建

const RESOLUTION_RANK: Record<Resolution, number> = { exact: 0, guessed: 1, external: 2, unresolved: 3 }
const weakest = (a: Resolution, b: Resolution): Resolution => (RESOLUTION_RANK[b] > RESOLUTION_RANK[a] ? b : a)
const END_ORDER: EndKind[] = ['ok', 'skipped', 'failed', 'exception']

const analysisCache = new WeakMap<FunctionGraph, Map<string, FunctionAnalysis>>()

/** 完整标题：被引擎截断时是 title_full，否则是 title（函数图、节点、功能线通用） */
const fullTitle = (x: { title: string; title_full?: string }): string => x.title_full || x.title
/** 函数 key 的完整标题；图里没有这个函数时是 key */
const fullTitleOf = (project: ProjectGraph, key: string): string => {
  const f = project.functions[key]
  return f ? fullTitle(f) : key
}

function analysisOf(fn: FunctionGraph, options: Partial<CollapseOptions>): FunctionAnalysis {
  const key = JSON.stringify(options)
  let m = analysisCache.get(fn)
  if (!m) analysisCache.set(fn, (m = new Map()))
  let a = m.get(key)
  if (!a) m.set(key, (a = analyzeFunction(fn, options)))
  return a
}

export function implicitKind(n: GraphNode): ImplicitKind | null {
  if (n.type === 'start') return 'start'
  if (n.type !== 'end') return null
  if (n.sid === 'end:uncaught' || (!n.sid && n.line === 0 && n.end_kind === 'exception')) return 'uncaught'
  if (n.sid === 'end:implicit' || (!n.sid && n.line === 0)) return 'end'
  return null
}

const enc = encodeURIComponent

interface Ctx {
  project: ProjectGraph
  state: ViewState
  options: Partial<CollapseOptions>
  common: Set<string>
  /** 任务函数 key → 它第一次出现（非 goto）的卡片 id */
  firstCard: Map<string, string>
  out: LineView
}

/**
 * 一张展开的卡片里的内容：每个图内节点在画布上由谁代表——连线的终点接 entry、起点接 exit（节点、占位节点两个都是它
 * 自己；被钻入的步骤是钻入链的第一张、最后一张卡片），以及被钻入的步骤的钻入链（按链上的顺序）。
 */
interface Content {
  fn: FunctionGraph
  entry: Map<string, string>
  exit: Map<string, string>
  drilled: Map<string, { card: string; fn: string; content: Content }[]>
}

function locOf(project: ProjectGraph, file: string, line: number, endLine: number): SourceLoc | null {
  if (line <= 0) return null
  const root = project.root.replace(/\/+$/, '')
  return { file, line, endLine: Math.max(line, endLine), path: root ? `${root}/${file}` : file }
}

function cardSummary(fn: FunctionGraph | undefined): CardSummary {
  const ends: Partial<Record<EndKind, number>> = {}
  if (fn) {
    for (const n of fn.nodes) {
      if (n.type === 'end' && n.end_kind && implicitKind(n) !== 'uncaught') ends[n.end_kind] = (ends[n.end_kind] ?? 0) + 1
    }
  }
  return { nodes: fn?.nodes.length ?? 0, ends }
}

function dispatchKeys(n: GraphNode): { task: string; direct: boolean; resolution: Resolution }[] {
  const found = new Map<string, { task: string; direct: boolean; resolution: Resolution }>()
  for (const c of n.calls) {
    const tasks = c.is_dispatch ? (c.target ? [c.target] : []) : c.dispatches
    for (const t of tasks) {
      const prev = found.get(t)
      if (!prev) found.set(t, { task: t, direct: c.is_dispatch, resolution: c.resolution })
      else found.set(t, { task: t, direct: prev.direct || c.is_dispatch, resolution: weakest(prev.resolution, c.resolution) })
    }
  }
  return [...found.values()]
}

const nodeDispatches = (n: GraphNode, task: string): boolean =>
  n.calls.some((c) => (c.is_dispatch ? c.target === task : c.dispatches.includes(task)))

/** 循环节点出边的标签：loop_each 为 每一项（while 循环为 条件成立），loop_exit 为 循环结束；别的为空串。 */
function loopLabel(n: GraphNode | undefined, kind: EdgeKind): string {
  if (n?.type !== 'loop') return ''
  if (kind === 'loop_each') return /^while\b/.test(n.subtitle) ? '条件成立' : '每一项'
  if (kind === 'loop_exit') return '循环结束'
  return ''
}

export function drillId(nodeId: string, callee: FunctionGraph): string {
  return `${nodeId}>${callee.sid || callee.key}`
}

/**
 * 展开一张卡片的内容（节点、折叠、连线、钻入卡片），追加到 ctx.out。children：所在任务卡片在任务树里派发的
 * 子任务（任务 key → 卡片 id，goto 的换成第一次出现的卡片）；drillChain：一层层钻进来的路上已有的函数（所在任务
 * 卡片的函数和外层的钻入卡片的函数），不再提供钻入，不会绕圈（和同一个步骤的几张钻入卡片排成的钻入链不是一回事）。
 */
function buildContent(
  ctx: Ctx,
  card: CardView,
  fn: FunctionGraph,
  children: Map<string, string>,
  drillChain: Set<string>,
): Content {
  const { project, state, out } = ctx
  const a = analysisOf(fn, ctx.options)
  const cardId = card.id
  const nodeIdOf = (n: GraphNode) => `${cardId}/${nodeSid(n)}`
  const content: Content = { fn, entry: new Map(), exit: new Map(), drilled: new Map() }
  const represent = (key: string, entry: string, exit = entry) => {
    content.entry.set(key, entry)
    content.exit.set(key, exit)
  }
  /** 钻入链的连接线（排在这张卡片的连线最后） */
  const connectors: EdgeView[] = []

  // 折叠区域：每个区域的 id、是否折叠；从外到内的祖先链
  const regionById = new Map(a.folds.map((f) => [f.key, f]))
  const foldId = (key: string) => `${cardId}/fold:${key}`
  const collapsed = new Map(
    a.folds.map((f) => {
      const open = state.folds[foldId(f.key)] ?? !f.defaultCollapsed
      return [f.key, !open] as const
    }),
  )
  const chainOf = (key: string): string[] => {
    const chain: string[] = []
    for (let k: string | null = key; k !== null; k = regionById.get(k)!.parent) chain.unshift(k)
    return chain
  }
  // 每个节点所在的区域（最内层）
  const innermost = new Map<string, string>()
  for (const f of [...a.folds].sort((x, y) => y.members.length - x.members.length)) {
    for (const m of f.members) innermost.set(m, f.key) // 小的后写，覆盖大的
  }
  const hiddenBy = (key: string | undefined): string | null => {
    if (key === undefined) return null
    for (const k of chainOf(key)) if (collapsed.get(k)) return k
    return null
  }

  // 折叠区域的视图
  const visibleFolds = new Set<string>()
  for (const f of a.folds) {
    const chain = chainOf(f.key)
    const outer = chain.slice(0, -1).find((k) => collapsed.get(k))
    if (outer !== undefined) continue // 在折叠着的更大区域里，看不见
    visibleFolds.add(f.key)
  }

  // 节点
  for (const n of fn.nodes) {
    const hidden = hiddenBy(innermost.get(n.id))
    if (hidden !== null) {
      represent(n.id, foldId(hidden))
      continue
    }
    const id = nodeIdOf(n)
    const calls: CallView[] = n.calls.map((c) => ({
      name: c.name,
      target: c.target,
      targetTitle: c.target && project.functions[c.target] ? fullTitle(project.functions[c.target]) : null,
      resolution: c.resolution,
      isDispatch: c.is_dispatch,
      conditional: c.conditional,
      dispatches: c.dispatches,
    }))
    const drills: DrillOption[] = []
    if (n.type === 'step') {
      for (const c of n.calls) {
        if (c.is_dispatch || !c.target || (c.resolution !== 'exact' && c.resolution !== 'guessed')) continue
        const callee = project.functions[c.target]
        if (!callee || callee.nodes.length === 0 || drillChain.has(callee.key) || drills.some((d) => d.fn === callee.key)) continue
        const did = drillId(id, callee)
        drills.push({
          id: did,
          fn: callee.key,
          title: callee.title,
          name: callee.qualname,
          resolution: c.resolution,
          open: state.drills[did] === true,
        })
      }
    }
    const fold = innermost.get(n.id)
    const view: NodeView = {
      id,
      card: cardId,
      key: n.id,
      sid: n.sid,
      type: n.type,
      title: n.title,
      subtitle: n.subtitle,
      titleFull: fullTitle(n),
      subtitleFull: n.subtitle_full || n.subtitle,
      endKind: n.end_kind,
      loc: locOf(project, fn.file, n.line, n.end_line),
      calls,
      guessed: n.calls.some((c) => c.resolution === 'guessed'),
      unresolved: n.calls.some((c) => c.resolution === 'unresolved'),
      dispatches: dispatchKeys(n).map((d) => ({
        ...d,
        title: fullTitleOf(project, d.task),
        common: ctx.common.has(d.task),
        card: children.get(d.task) ?? null,
      })),
      fields: n.fields,
      passthrough: n.passthrough,
      drills,
      trunk: a.trunkSet.has(n.id),
      implicit: implicitKind(n),
      fold: fold !== undefined ? foldId(fold) : null,
      order: a.order.get(n.id)!,
    }
    const open = drills.filter((d) => d.open)
    if (open.length) {
      // 钻入：这个步骤由一串内嵌卡片（钻入链）代替，每个开着的被调函数一张，卡片里是被调用函数的图
      const chain: { card: string; fn: string; content: Content }[] = []
      open.forEach((opt, i) => {
        const callee = project.functions[opt.fn]
        const drillCard: CardView = {
          id: opt.id,
          kind: 'drill',
          parent: cardId,
          dispatcher: null,
          depth: (card.kind === 'drill' ? card.depth : 0) + 1,
          fn: callee.key,
          fnSid: callee.sid,
          title: callee.title,
          titleFull: fullTitle(callee),
          name: callee.qualname,
          fnKind: callee.kind,
          route: callee.route,
          retries: callee.retries,
          loc: locOf(project, callee.file, callee.line, callee.line),
          expanded: true,
          expandable: true,
          goto: false,
          gotoTarget: null,
          missing: false,
          summary: cardSummary(callee),
          anchor: view,
          chainIndex: i,
        }
        out.cards.push(drillCard)
        // 链上相邻的卡片是同一条语句里的并列调用：各自往下钻的路（drillChain）只加上自己
        const inner = buildContent(ctx, drillCard, callee, children, new Set([...drillChain, callee.key]))
        chain.push({ card: drillCard.id, fn: callee.key, content: inner })
        if (i > 0) {
          const prev = chain[i - 1].card
          connectors.push({
            id: `chain:${prev}->${drillCard.id}`,
            card: cardId,
            src: prev,
            dst: drillCard.id,
            kind: 'next',
            branch: null,
            label: '',
            labelFull: '',
            back: false,
            trunk: false,
            keys: [],
            chain: true,
          })
        }
      })
      represent(n.id, chain[0].card, chain[chain.length - 1].card)
      content.drilled.set(n.id, chain)
    } else {
      represent(n.id, id)
      out.nodes.push(view)
    }
  }

  // 折叠占位
  const foldViewIds = new Set<string>()
  for (const f of a.folds) {
    if (!visibleFolds.has(f.key)) continue
    const members = f.members.map((m) => a.nodes.get(m)!)
    const memberSet = new Set(f.members)
    // 结局：区域里的结局节点，加上从区域直接走到的、和别处共用的结局节点（不在主干上的；同 collapse.ts 的判定）
    const leaving = f.members.flatMap((m) =>
      (a.out.get(m) ?? []).filter((e) => !memberSet.has(e.dst) && e.kind !== 'raise' && !endsThroughFinally(a, e)),
    )
    const sharedEnds = leaving
      .map((e) => a.nodes.get(e.dst)!)
      .filter((n, i, all) => n.type === 'end' && !a.trunkSet.has(n.id) && all.indexOf(n) === i)
    const ends = [...members.filter((m) => m.type === 'end'), ...sharedEnds]
      .sort((x, y) => a.order.get(x.id)! - a.order.get(y.id)!)
      .map((m) => ({ kind: m.end_kind, title: fullTitle(m) }))
    const endKinds = END_ORDER.filter((k) => ends.some((e) => e.kind === k))
    // 提前结束的分支不汇回；汇回的长分支、异常处理看有没有往下走出区域的连线（异常、结局经 finally 离开的不算）
    const rejoins =
      f.kind !== 'exit' && leaving.some((e) => !(a.nodes.get(e.dst)!.type === 'end' && !a.trunkSet.has(e.dst)))
    const dispatchTitles: string[] = []
    for (const m of members) {
      for (const d of dispatchKeys(m)) {
        const t = fullTitleOf(project, d.task)
        if (!dispatchTitles.includes(t)) dispatchTitles.push(t)
      }
    }
    const anchorNode = a.nodes.get(f.anchor)!
    const parentKey = f.parent
    const id = foldId(f.key)
    foldViewIds.add(id)
    const label = f.kind !== 'catch' ? branchLabel(f.branch, a.matchNodes.has(f.anchor)) : anchorNode.subtitle
    // 分支的完整模式在这条分支的出边上（case 连线的 label_full）
    const branchFull = fn.edges.find((e) => e.src === f.anchor && e.branch === f.branch && e.label_full)?.label_full
    out.folds.push({
      id,
      card: cardId,
      kind: f.kind,
      anchor: nodeIdOf(anchorNode),
      branch: f.branch,
      label,
      labelFull: f.kind !== 'catch' ? (branchFull ?? label) : anchorNode.subtitle_full || label,
      title: f.kind !== 'catch' ? members[0].title : anchorNode.title,
      titleFull: fullTitle(f.kind !== 'catch' ? members[0] : anchorNode),
      collapsed: collapsed.get(f.key)!,
      parent: parentKey !== null ? foldId(parentKey) : null,
      members: members.map(nodeIdOf),
      keys: members.map((m) => m.id),
      summary: {
        nodes: members.length,
        ends,
        endKinds,
        rejoins,
        first: fullTitle(members[0]),
        dispatches: dispatchTitles,
        guessed: members.some((m) => m.calls.some((c) => c.resolution === 'guessed')),
        unresolved: members.some((m) => m.calls.some((c) => c.resolution === 'unresolved')),
      },
      order: a.order.get(f.members[0])!,
      trunk: members.some((m) => a.trunkSet.has(m.id)),
    })
  }

  // 连线：两端换成画布上代表它们的元素；折叠区域内部的连线不画
  const seen = new Map<string, EdgeView>()
  const trunkNext = new Map(a.trunk.slice(0, -1).map((v, i) => [v, a.trunk[i + 1]]))
  for (const e of fn.edges) {
    const s = content.exit.get(e.src)
    const d = content.entry.get(e.dst)
    if (s === undefined || d === undefined) continue
    if (s === d && foldViewIds.has(s)) continue
    const trunk = trunkNext.get(e.src) === e.dst && e.kind !== 'raise' && !a.back.has(e)
    // 起点被折叠时，从占位节点出发的连线不再是那个判断节点的分支
    const fromFold = foldViewIds.has(s)
    const branch = fromFold ? null : edgeBranch(a.nodes.get(e.src), e, a.matchNodes.has(e.src))
    const id = `e:${s}->${d}:${e.kind}${branch !== null ? `:${enc(branch)}` : ''}`
    const key = `${e.src}|${e.dst}|${e.kind}|${e.label}`
    const prev = seen.get(id)
    if (prev) {
      prev.trunk ||= trunk
      if (!prev.keys.includes(key)) prev.keys.push(key)
      continue
    }
    const label = branch !== null ? branchLabel(branch, a.matchNodes.has(e.src)) : fromFold ? '' : loopLabel(a.nodes.get(e.src), e.kind)
    const view: EdgeView = {
      id,
      card: cardId,
      src: s,
      dst: d,
      kind: e.kind,
      branch,
      label,
      labelFull: branch !== null && e.label_full ? e.label_full : label,
      back: a.back.has(e),
      trunk,
      keys: [key],
      chain: false,
    }
    seen.set(id, view)
    out.edges.push(view)
  }
  out.edges.push(...connectors)
  return content
}

/** 一张任务卡片里，派发 task 的那些画布元素（递归进钻入卡片）。 */
function dispatchSources(content: Content, task: string): { id: string; direct: boolean; resolution: Resolution }[] {
  const found: { id: string; direct: boolean; resolution: Resolution }[] = []
  for (const n of content.fn.nodes) {
    if (!nodeDispatches(n, task)) continue
    const chain = content.drilled.get(n.id) ?? []
    const deeper = chain.flatMap((x) => dispatchSources(x.content, task))
    if (deeper.length) {
      found.push(...deeper)
      continue
    }
    const d = dispatchKeys(n).find((x) => x.task === task)!
    // 钻入着、派发不在开着的卡片里画成的节点上：经哪个被调函数派发的就从它的卡片出发，否则（直接派发、经没展开的
    // 被调函数派发）从钻入链的最后一张出发
    const via = n.calls.find((c) => !c.is_dispatch && c.dispatches.includes(task) && chain.some((x) => x.fn === c.target))
    const id = (via && chain.find((x) => x.fn === via.target)?.card) || content.exit.get(n.id)!
    const prev = found.find((x) => x.id === id)
    if (prev) {
      prev.direct ||= d.direct
      prev.resolution = weakest(prev.resolution, d.resolution)
    } else found.push({ id, direct: d.direct, resolution: d.resolution })
  }
  return found
}

function findLine(project: ProjectGraph, lineSid: string): FlowLine {
  const line = project.lines.find((l) => l.sid === lineSid) ?? project.lines.find((l) => l.id === lineSid)
  if (!line) throw new Error(`图里没有这条功能线：${lineSid}`)
  return line
}

const lineRef = (line: FlowLine): string => line.sid || `line:${line.id}`

/**
 * 一条功能线的视图。lineSid 是功能线的 sid（旧图没有 sid 时也接受功能线 id）。纯函数。
 * 卡片按任务树先序；每张展开的卡片的节点、折叠、连线；卡片之间的派发连线。
 */
export function buildLineView(
  project: ProjectGraph,
  lineSid: string,
  state: ViewState = emptyState(),
  options: Partial<CollapseOptions> = {},
): LineView {
  return buildView(project, findLine(project, lineSid), state, options)
}

/** 单个函数的视图（一张展开的卡片，没有派发连线），给"只看这个函数"和规模测试用。 */
export function buildFunctionView(
  project: ProjectGraph,
  fnKey: string,
  state: ViewState = emptyState(),
  options: Partial<CollapseOptions> = {},
): LineView {
  const fn = project.functions[fnKey]
  if (!fn) throw new Error(`图里没有这个函数：${fnKey}`)
  return buildView(project, functionLine(fn), state, options)
}

/** 单个函数当成一条只有根任务的线；sid 用 "fn:" 开头，和真正的功能线区分。 */
const functionLine = (fn: FunctionGraph): FlowLine => ({
  id: fn.key,
  sid: `fn:${fn.sid || fn.key}`,
  title: fn.title,
  title_full: fn.title_full,
  group: '',
  kind: fn.kind,
  root: { function: fn.key, goto: false, children: [] },
})

function buildView(project: ProjectGraph, line: FlowLine, state: ViewState, options: Partial<CollapseOptions>): LineView {
  const out: LineView = {
    line: { id: line.id, sid: lineRef(line), title: line.title, titleFull: fullTitle(line), group: line.group, kind: line.kind },
    cards: [],
    nodes: [],
    folds: [],
    edges: [],
    links: [],
  }
  const ctx: Ctx = { project, state, options, common: new Set(project.common_tasks), firstCard: new Map(), out }
  const ref = lineRef(line)

  // 先给任务树里每个任务定卡片 id
  interface Slot {
    task: LineTask
    id: string
    dispatcher: Slot | null
    depth: number
    children: Slot[]
  }
  const sidOf = (key: string) => project.functions[key]?.sid || key
  const makeSlot = (task: LineTask, path: string[], dispatcher: Slot | null, depth: number): Slot => {
    const chain = [...path, sidOf(task.function)]
    const slot: Slot = { task, id: `${ref}|${chain.join('>')}`, dispatcher, depth, children: [] }
    if (!task.goto && !ctx.firstCard.has(task.function)) ctx.firstCard.set(task.function, slot.id)
    slot.children = task.children.map((c) => makeSlot(c, chain, slot, depth + 1))
    return slot
  }
  const root = makeSlot(line.root, [], null, 0)

  // 先序遍历：卡片、展开的卡片的内容
  const contents = new Map<string, Content>()
  const visit = (slot: Slot) => {
    const fn = project.functions[slot.task.function] as FunctionGraph | undefined
    const goto = slot.task.goto
    const expandable = !goto && fn !== undefined && fn.nodes.length > 0
    const expanded = expandable && (state.cards[slot.id] ?? cardExpandedByDefault({ isRoot: slot.depth === 0, goto }))
    // 没有标题的接口（标题就是函数名；含挂在路由上的工作流、任务）写"方法 路径"
    const title = fn ? (fn.route && fn.title === fn.qualname.split('.').pop() ? fn.route : fn.title) : slot.task.function
    const card: CardView = {
      id: slot.id,
      kind: 'task',
      parent: null,
      dispatcher: slot.dispatcher?.id ?? null,
      depth: slot.depth,
      fn: fn?.key ?? null,
      fnSid: fn?.sid ?? '',
      title,
      titleFull: fn && title === fn.title ? fullTitle(fn) : title,
      name: fn?.qualname ?? slot.task.function,
      fnKind: fn?.kind ?? null,
      route: fn?.route ?? null,
      retries: fn?.retries ?? false,
      loc: fn ? locOf(project, fn.file, fn.line, fn.line) : null,
      expanded,
      expandable,
      goto,
      gotoTarget: goto ? (ctx.firstCard.get(slot.task.function) ?? null) : null,
      missing: fn === undefined,
      summary: cardSummary(fn),
      anchor: null,
      chainIndex: 0,
    }
    out.cards.push(card)
    if (expanded && fn) {
      const children = new Map<string, string>()
      for (const c of slot.children) {
        const target = c.task.goto ? ctx.firstCard.get(c.task.function) : c.id
        if (target && !children.has(c.task.function)) children.set(c.task.function, target)
      }
      contents.set(card.id, buildContent(ctx, card, fn, children, new Set([fn.key])))
    }
    slot.children.forEach(visit)
  }
  visit(root)

  // 派发连线：每张非根任务卡片，从派发它的卡片里发出派发的元素连过来
  const linkSeen = new Set<string>()
  const walk = (slot: Slot) => {
    for (const child of slot.children) {
      const content = contents.get(slot.id)
      let sources = content ? dispatchSources(content, child.task.function) : []
      if (sources.length === 0) {
        // 卡片折叠着，或者派发发生在没画成节点的地方（引擎折叠掉的分支）：从卡片本身出发
        const res = guessDispatchResolution(project, slot.task.function, child.task.function)
        sources = [{ id: slot.id, direct: res.direct, resolution: res.resolution }]
      }
      for (const s of sources) {
        const id = `link:${s.id}=>${child.id}`
        if (linkSeen.has(id)) continue
        linkSeen.add(id)
        out.links.push({ id, src: s.id, dst: child.id, srcCard: slot.id, direct: s.direct, resolution: s.resolution })
      }
      walk(child)
    }
  }
  walk(root)
  return out
}

/** 卡片折叠时的派发连线：看函数里的节点有没有直接派发它，取最弱的解析等级；一个都找不到时是 unresolved。 */
function guessDispatchResolution(
  project: ProjectGraph,
  fnKey: string,
  task: string,
): { direct: boolean; resolution: Resolution } {
  const fn = project.functions[fnKey]
  let direct = false
  let resolution: Resolution | null = null
  for (const n of fn?.nodes ?? []) {
    for (const d of dispatchKeys(n)) {
      if (d.task !== task) continue
      direct ||= d.direct
      resolution = resolution === null ? d.resolution : weakest(resolution, d.resolution)
    }
  }
  // 函数不在图里，或者派发没画成节点：不知道是怎么派发的，不能说是确定的
  return { direct, resolution: resolution ?? 'unresolved' }
}

/** "全部展开"：所有任务卡片展开、所有折叠区域展开（含嵌套的），钻入状态照 base。 */
export function expandAllState(
  project: ProjectGraph,
  lineSid: string,
  base: ViewState = emptyState(),
  options: Partial<CollapseOptions> = {},
): ViewState {
  return expandAll(project, findLine(project, lineSid), base, options)
}

/** 一次"全部展开"最多钻入几个被调函数（调用很多的功能线不把画布撑到排不动） */
export const MAX_EXPAND_DRILLS = 60

/**
 * 界面上的"全部展开"：任务卡片、折叠区域都展开（同 expandAllState），再把能钻入的步骤都钻进去——一个步骤调用了几个
 * 函数时每个都钻（同一个步骤的几张钻入卡片排成钻入链），已经钻入了的不动；钻进去露出来的卡片里接着展开、接着钻，直到
 * 没有可钻的（一层层钻进来的路上已经有的函数不再提供钻入，不会绕圈），或者钻满 maxDrills 个（more 为真：还有没钻入的）。
 */
export function expandEverythingState(
  project: ProjectGraph,
  lineSid: string,
  base: ViewState = emptyState(),
  maxDrills = MAX_EXPAND_DRILLS,
): { state: ViewState; more: boolean } {
  const line = findLine(project, lineSid)
  let state = base
  let drilled = 0
  let more = false
  for (let round = 0; round < 200; round++) {
    state = expandAll(project, line, state, {})
    const view = buildView(project, line, state, {})
    let next = state
    more = false
    for (const n of drillSteps(view)) {
      for (const d of n.drills) {
        if (d.open) continue
        if (drilled >= maxDrills) {
          more = true
          continue
        }
        next = setOpen(next, 'drills', d.id, true)
        drilled++
      }
    }
    if (next === state) break
    state = next
  }
  return { state, more }
}

/**
 * 画布上能钻入的步骤：画成节点的，加上已经换成钻入链的（链上卡片的 anchor；它的 drills 里有没展开的就还能再钻）。
 * 按视图里的先后，每个步骤一次。
 */
export function drillSteps(view: LineView): NodeView[] {
  const out: NodeView[] = view.nodes.filter((n) => n.drills.length > 0)
  const seen = new Set<string>()
  for (const c of view.cards) {
    if (c.kind !== 'drill' || !c.anchor || seen.has(c.anchor.id)) continue
    seen.add(c.anchor.id)
    out.push(c.anchor)
  }
  return out
}

const chainCache = new WeakMap<LineView, Map<string, CardView[]>>()

/** 代替步骤 stepId（画布节点 id）的钻入卡片，按钻入链上的顺序；这个步骤没被钻入（或不在画布上）时为空。 */
export function drillChainOf(view: LineView, stepId: string): CardView[] {
  let m = chainCache.get(view)
  if (!m) {
    m = new Map()
    for (const c of view.cards) {
      if (c.kind !== 'drill' || !c.anchor) continue
      const list = m.get(c.anchor.id)
      if (list) list.push(c)
      else m.set(c.anchor.id, [c])
    }
    for (const list of m.values()) list.sort((a, b) => a.chainIndex - b.chainIndex)
    chainCache.set(view, m)
  }
  return m.get(stepId) ?? []
}

/** 单个函数视图（buildFunctionView）的"全部展开"。 */
export function expandAllFunctionState(
  project: ProjectGraph,
  fnKey: string,
  base: ViewState = emptyState(),
  options: Partial<CollapseOptions> = {},
): ViewState {
  const fn = project.functions[fnKey]
  if (!fn) throw new Error(`图里没有这个函数：${fnKey}`)
  return expandAll(project, functionLine(fn), base, options)
}

function expandAll(project: ProjectGraph, line: FlowLine, base: ViewState, options: Partial<CollapseOptions>): ViewState {
  let state: ViewState = { cards: { ...base.cards }, folds: { ...base.folds }, drills: { ...base.drills } }
  // 嵌套的折叠区域在外层展开后才看得见：反复展开到没有折叠着的为止
  for (let round = 0; round < 50; round++) {
    const view = buildView(project, line, state, options)
    let changed = false
    for (const c of view.cards) {
      if (c.kind === 'task' && c.expandable && !c.expanded) {
        state = setOpen(state, 'cards', c.id, true)
        changed = true
      }
    }
    for (const f of view.folds) {
      if (f.collapsed) {
        state = setOpen(state, 'folds', f.id, true)
        changed = true
      }
    }
    if (!changed) break
  }
  return state
}
