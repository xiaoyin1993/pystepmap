// 排版：用 ELK 的 layered 算法自上而下排一条功能线的视图（卡片是容器，节点在卡片里，钻入卡片嵌在卡片里）。
// 节点尺寸由文字长度估算（中文按全宽），不依赖浏览器测量，所以能在 vitest 里跑；界面按同样的数字定 DOM 的尺寸
// （nodeSize / foldSize / cardSize 和 METRICS 里的字号、行高、内边距）。界面画的宽高取排版结果：出边标签放不下时
// 节点会被加宽（见 layoutLineView），比估算的宽。
// 分支的左右只由图定，展开、收起、钻入都不变：是在左、否在右（见 branchPorts、branchKeys）。
//
// 文字的约定（界面照做，估算才对得上）：标题允许任意位置换行（CSS overflow-wrap: anywhere），超出行数用省略号；
// 副标题、函数名一行，超出用省略号；字号、行高、内边距取 METRICS。

import ELK from 'elkjs/lib/elk.bundled.js'
import type { ElkExtendedEdge, ElkNode, ElkLabel } from 'elkjs/lib/elk-api'
import type { EndKind } from './types'
import type { CardView, EdgeView, FoldView, LineView, NodeView } from './viewmodel'

// ---------------------------------------------------------------------------------------------------------------
// 尺寸

export const METRICS = {
  node: {
    /** 标题字号 / 行高、最多几行 */
    titleFont: 13,
    titleLine: 18,
    titleLines: 2,
    /** 副标题（函数名、条件原文）：等宽字体 */
    subFont: 11,
    subLine: 15,
    padX: 12,
    padY: 8,
    /** 左侧类型图标占的宽度（含间距） */
    icon: 20,
    minWidth: 150,
    maxWidth: 260,
  },
  /** 开始 / 结局：一行的胶囊 */
  pill: { font: 13, height: 32, padX: 14, icon: 18, minWidth: 80, maxWidth: 260 },
  /** 节点上的小标记（派发、推测、目标不明），放在副标题那一行的右边 */
  chip: { font: 10, padX: 4, gap: 4 },
  /** 折叠占位：第一行摘要（不截断），第二行入口节点标题（maxWidth 只限这一行） */
  fold: { font: 12, line: 16, subFont: 11, subLine: 15, padX: 12, padY: 7, icon: 20, minWidth: 150, maxWidth: 260 },
  /** 任务卡片 / 钻入卡片的标题栏；折叠的卡片多一行摘要 */
  card: {
    titleFont: 15,
    titleLine: 20,
    subFont: 12,
    subLine: 16,
    padX: 14,
    padY: 10,
    minWidth: 220,
    maxWidth: 360,
    /** 展开的卡片里，内容和卡片边框的距离（标题栏之下） */
    inner: 16,
    /**
     * 钻入卡片标题那一行右边的"再展开 N 个"：字号；除了字以外占的宽度（按钮左右内边距和边框 16、按钮右边到卡片内边距
     * 6——按钮离卡片右边 20，让开收起的小箭头——、和标题之间 6）
     */
    moreFont: 11,
    morePad: 28,
  },
  /** 分支标签 */
  label: { font: 11, height: 16, padX: 4 },
}

const WIDE_RANGES: [number, number][] = [
  [0x1100, 0x115f],
  // 破折号、省略号、箭头：中文字体里画成全宽
  [0x2014, 0x2014],
  [0x2026, 0x2026],
  [0x2190, 0x21ff],
  [0x2e80, 0x303e],
  [0x3041, 0x33ff],
  [0x3400, 0x4dbf],
  [0x4e00, 0x9fff],
  [0xa000, 0xa4cf],
  [0xac00, 0xd7a3],
  [0xf900, 0xfaff],
  [0xfe30, 0xfe4f],
  [0xff00, 0xff60],
  [0xffe0, 0xffe6],
  [0x1f300, 0x1faff],
  [0x20000, 0x3fffd],
]

const isWide = (cp: number): boolean => WIDE_RANGES.some(([a, b]) => cp >= a && cp <= b)
const NARROW = new Set(" il.,:;'|!()[]{}`ijtf/\\")
const WIDE_LATIN = new Set('mwMW@%')

/** 一个字符的宽度（以字号为单位）。宁可估宽一点：估宽了只是留白，估窄了会被截断。 */
function charWidth(ch: string, mono: boolean): number {
  const cp = ch.codePointAt(0)!
  if (isWide(cp)) return 1
  if (mono) return 0.62
  if (NARROW.has(ch)) return 0.34
  if (WIDE_LATIN.has(ch)) return 0.88
  if (ch >= 'A' && ch <= 'Z') return 0.68
  if (ch >= '0' && ch <= '9') return 0.6
  return 0.58
}

/** 文字宽度估算（像素）：中日韩文字、全角符号按一个字号宽，西文按字符类别估。mono 为等宽字体。 */
export function textWidth(text: string, fontSize: number, mono = false): number {
  let w = 0
  for (const ch of text) w += charWidth(ch, mono)
  return Math.ceil(w * fontSize)
}

/** 按宽度折行（任意位置可断），返回需要的行数（不超过 maxLines）。 */
export function lineCount(text: string, fontSize: number, width: number, maxLines: number, mono = false): number {
  if (!text) return 1
  let lines = 1
  let cur = 0
  for (const ch of text) {
    const w = charWidth(ch, mono) * fontSize
    if (cur + w > width && cur > 0) {
      lines++
      cur = 0
      if (lines > maxLines) return maxLines
    }
    cur += w
  }
  return lines
}

export interface Size {
  width: number
  height: number
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Math.ceil(v)))

/** 节点上的小标记（界面照这个顺序画）：派发、推测、目标不明。 */
export function nodeChips(n: NodeView): string[] {
  const chips: string[] = []
  if (n.dispatches.length) chips.push('派发')
  if (n.guessed) chips.push('推测')
  if (n.unresolved) chips.push('目标不明')
  return chips
}

const chipsWidth = (chips: string[]): number =>
  chips.reduce((w, c) => w + textWidth(c, METRICS.chip.font) + 2 * METRICS.chip.padX + METRICS.chip.gap, 0)

/** 节点是不是画成一行的胶囊（开始、结局）。 */
export const isPill = (n: NodeView): boolean => n.type === 'start' || n.type === 'end'

/** 胶囊上的字：开始节点写"开始"（它的标题就是函数标题，卡片标题栏已经有了），结局写标题。 */
export const pillText = (n: NodeView): string => (n.type === 'start' ? '开始' : n.title)

/** 节点的尺寸和标题行数。 */
export function nodeSize(n: NodeView): Size & { titleLines: number } {
  if (isPill(n)) {
    const p = METRICS.pill
    const w = textWidth(pillText(n), p.font) + 2 * p.padX + p.icon
    return { width: clamp(w, p.minWidth, p.maxWidth), height: p.height, titleLines: 1 }
  }
  const m = METRICS.node
  const chips = nodeChips(n)
  const titleW = textWidth(n.title, m.titleFont)
  const subW = (n.subtitle ? textWidth(n.subtitle, m.subFont, true) : 0) + chipsWidth(chips)
  const width = clamp(Math.max(titleW, subW) + 2 * m.padX + m.icon, m.minWidth, m.maxWidth)
  const titleLines = lineCount(n.title, m.titleFont, width - 2 * m.padX - m.icon, m.titleLines)
  const hasSub = n.subtitle !== '' || chips.length > 0
  const height = 2 * m.padY + titleLines * m.titleLine + (hasSub ? m.subLine + 2 : 0)
  return { width, height, titleLines }
}

/**
 * 节点上的字有没有被截断（标题超过两行被截、副标题一行放不下），按和 nodeSize 一样的估算。width 是排版给的宽度
 * （可能比估算的宽，见 layoutLineView）。估算宁宽勿窄，所以估算说放得下时界面上一定放得下。
 */
export function nodeClipped(n: NodeView, width: number): { title: boolean; sub: boolean } {
  if (isPill(n)) {
    const p = METRICS.pill
    return { title: textWidth(pillText(n), p.font) > width - 2 * p.padX - p.icon, sub: false }
  }
  const m = METRICS.node
  const inner = width - 2 * m.padX - m.icon
  return {
    title: lineCount(n.title, m.titleFont, inner, Infinity) > m.titleLines,
    sub: n.subtitle !== '' && textWidth(n.subtitle, m.subFont, true) + chipsWidth(nodeChips(n)) > inner,
  }
}

/** 卡片标题栏的字有没有被截断：标题一行、函数名（接口加方法和路径）一行。 */
export function cardClipped(c: CardView, width: number): { title: boolean; sub: boolean } {
  const m = METRICS.card
  const inner = width - 2 * m.padX
  return {
    title: textWidth(c.title, m.titleFont) > inner - cardMoreWidth(c),
    sub: textWidth(cardSubtitle(c), m.subFont, true) > inner,
  }
}

/** 结局种类的名字，界面各处（折叠占位、图例、详情栏）都用这一套 */
export const END_NAME: Record<EndKind, string> = { ok: '正常', skipped: '跳过', failed: '失败', exception: '未捕获异常' }

/** 折叠占位上的两行字（界面照这个显示）。 */
export function foldText(f: FoldView): { headline: string; detail: string } {
  const s = f.summary
  const parts: string[] = []
  if (f.kind === 'catch') parts.push('异常处理')
  parts.push(`${s.nodes} 个节点`)
  if (s.endKinds.length) parts.push(`以${s.endKinds.map((k) => END_NAME[k]).join(' / ')}收尾`)
  if (s.rejoins) parts.push('汇回')
  // 第二行：异常处理写捕获节点的标题（"捕获 某某异常"），分支写入口节点的标题
  return { headline: parts.join(' · '), detail: f.title }
}

/** 折叠占位上的小标记（第二行的右边，界面照这个顺序画）：里面有推测的调用、有目标不明的调用。 */
export function foldChips(f: FoldView): string[] {
  const chips: string[] = []
  if (f.summary.guessed) chips.push('推测')
  if (f.summary.unresolved) chips.push('目标不明')
  return chips
}

/**
 * 折叠占位的宽度：摘要行（几个节点、怎么收尾、是否汇回）总是放得下；第二行是入口标题加小标记，最宽 maxWidth，
 * 小标记总是放得下，标题超出用省略号。
 */
export function foldSize(f: FoldView): Size {
  const m = METRICS.fold
  const t = foldText(f)
  const pad = 2 * m.padX + m.icon
  const head = textWidth(t.headline, m.font) + pad
  const chips = chipsWidth(foldChips(f))
  const detail = Math.max(Math.min(textWidth(t.detail, m.subFont) + chips + pad, m.maxWidth), chips + pad)
  return { width: Math.ceil(Math.max(m.minWidth, head, detail)), height: 2 * m.padY + m.line + m.subLine }
}

/** 卡片标题栏的第二行：函数名，接口加上方法和路径。 */
export function cardSubtitle(c: CardView): string {
  if (c.kind === 'drill') return `钻入 ${c.name}`
  if (c.goto) return `回到前面的 ${c.name}`
  return c.route ? `${c.route} · ${c.name}` : c.name
}

/** 折叠的任务卡片的第三行摘要。 */
export function cardSummaryText(c: CardView): string {
  if (c.missing) return '图里没有这个函数'
  if (c.goto) return '已在前面出现，点击跳过去'
  const ends = (['ok', 'skipped', 'failed'] as const)
    .filter((k) => c.summary.ends[k])
    .map((k) => `${END_NAME[k]} ${c.summary.ends[k]}`)
  return `${c.summary.nodes} 个节点${ends.length ? ` · 结局 ${ends.join(' / ')}` : ''}`
}

/** 卡片标题栏的高度（展开的卡片：内容从它下面开始）。 */
export function cardHeaderHeight(): number {
  const m = METRICS.card
  return 2 * m.padY + m.titleLine + m.subLine
}

/**
 * 钻入卡片标题栏上"再展开 N 个"的字：同一个步骤还调用了没展开的函数时（N 是没展开的个数，钻入链上的每张卡片都有）；
 * 别的卡片为 null。
 */
export function cardMoreText(c: CardView): string | null {
  if (c.kind !== 'drill' || !c.anchor) return null
  const n = c.anchor.drills.filter((d) => !d.open).length
  return n > 0 ? `再展开 ${n} 个` : null
}

/** 标题那一行右边给"再展开 N 个"留的宽度（标题在它左边截断）；没有这个按钮时 0。 */
export function cardMoreWidth(c: CardView): number {
  const t = cardMoreText(c)
  return t === null ? 0 : textWidth(t, METRICS.card.moreFont) + METRICS.card.morePad
}

/** 卡片标题栏需要的宽度。 */
export function cardHeaderWidth(c: CardView): number {
  const m = METRICS.card
  const w = Math.max(textWidth(c.title, m.titleFont) + cardMoreWidth(c), textWidth(cardSubtitle(c), m.subFont, true))
  return clamp(w + 2 * m.padX, m.minWidth, m.maxWidth)
}

/** 折叠的卡片（含 goto 卡片）的尺寸；展开的卡片由排版算出。 */
export function cardSize(c: CardView): Size {
  const m = METRICS.card
  const w = Math.max(cardHeaderWidth(c), textWidth(cardSummaryText(c), m.subFont) + 2 * m.padX)
  return { width: clamp(w, m.minWidth, m.maxWidth), height: cardHeaderHeight() + m.subLine }
}

export function labelSize(text: string): Size {
  const m = METRICS.label
  return { width: textWidth(text, m.font) + 2 * m.padX, height: m.height }
}

// ---------------------------------------------------------------------------------------------------------------
// 排版结果

export interface Point {
  x: number
  y: number
}

export interface Box {
  x: number
  y: number
  width: number
  height: number
}

export interface PositionedBox extends Box {
  id: string
  kind: 'card' | 'node' | 'fold'
  /** 所在的卡片；顶层的卡片为 null */
  parent: string | null
}

export interface PositionedEdge {
  id: string
  kind: 'edge' | 'link'
  /** 折线：起点、折点……、终点，绝对坐标；界面照画（起点处的小台阶已拉直，见 straighten） */
  points: Point[]
  /** 分支标签的位置（绝对坐标），贴着第一段、不被别的连线穿过（见 placeLabels）；没有标签时为 null */
  label: Box | null
}

export interface Positioned {
  /** 整张画布的尺寸：从 (0, 0) 到所有元素（含连线、标签）的右下角 */
  width: number
  height: number
  /** 卡片、节点、折叠占位，按 id */
  boxes: Record<string, PositionedBox>
  /** 卡片内的连线和卡片之间的派发连线，按 id */
  edges: Record<string, PositionedEdge>
}

// ---------------------------------------------------------------------------------------------------------------
// ELK

const SPACING = {
  betweenLayers: 26,
  nodeNode: 22,
  edgeNode: 12,
  edgeEdge: 8,
  components: 28,
}

const BASE_OPTIONS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.layered.spacing.nodeNodeBetweenLayers': String(SPACING.betweenLayers),
  'elk.spacing.nodeNode': String(SPACING.nodeNode),
  'elk.layered.spacing.edgeNodeBetweenLayers': String(SPACING.edgeNode),
  'elk.spacing.edgeNode': String(SPACING.edgeNode),
  'elk.spacing.edgeEdge': String(SPACING.edgeEdge),
  'elk.layered.spacing.edgeEdgeBetweenLayers': String(SPACING.edgeEdge),
  'elk.spacing.componentComponent': String(SPACING.components),
  'elk.spacing.edgeLabel': '2',
  // 回边：深度优先，从开始节点出发指回祖先的连线（和 collapse.ts 判定回边的方法一致）。
  // MODEL_ORDER / considerModelOrder 在 INCLUDE_CHILDREN 下会让 elkjs 0.12 崩溃，不能用。
  'elk.layered.cycleBreaking.strategy': 'DEPTH_FIRST',
  // 回边从源节点下方绕到目标节点上方，不和顺着往下的连线混在一起
  'elk.layered.feedbackEdges': 'true',
  // 长链保持竖直：BRANDES_KOEPF 会让几十层的主干一路往右漂，画布宽一倍；LINEAR_SEGMENTS 最窄
  'elk.layered.nodePlacement.strategy': 'LINEAR_SEGMENTS',
  'elk.layered.edgeLabels.sideSelection': 'ALWAYS_DOWN',
  'elk.edgeLabels.inline': 'false',
  // 分支终点在同一行里的左右按给定的先后（elk.position，见 toElkGraph），不交给交叉最少的启发式去碰运气；
  // 没给先后的节点照旧由 ELK 排
  'elk.layered.crossingMinimization.semiInteractive': 'true',
}

const ROOT_OPTIONS: Record<string, string> = {
  ...BASE_OPTIONS,
  'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
  'elk.json.shapeCoords': 'ROOT',
  'elk.json.edgeCoords': 'ROOT',
  'elk.padding': '[top=0,left=0,bottom=0,right=0]',
  'elk.layered.spacing.nodeNodeBetweenLayers': '48',
  'elk.spacing.nodeNode': '40',
}

function containerOptions(): Record<string, string> {
  const m = METRICS.card
  return {
    ...BASE_OPTIONS,
    'elk.padding': `[top=${cardHeaderHeight() + m.inner},left=${m.inner},bottom=${m.inner},right=${m.inner}]`,
  }
}

let elkInstance: InstanceType<typeof ELK> | null = null
const elk = () => (elkInstance ??= new ELK())

/**
 * 钻入卡片在所在卡片里的先后：被它代替的步骤的位置；同一个步骤的钻入链上的几张按链上的顺序（位置都是整数，链上的
 * 第 i 张加 i / 1000，排在下一个节点之前）。
 */
const drillOrder = (c: CardView): number => c.anchor!.order + c.chainIndex / 1000

interface Built {
  root: ElkNode
  kinds: Map<string, PositionedBox['kind']>
  parents: Map<string, string | null>
  edgeKinds: Map<string, PositionedEdge['kind']>
}

/**
 * 把视图变成 ELK 的图：任务卡片在顶层，节点、占位节点、钻入卡片在所在卡片里；连线放在两端共同的卡片里。
 * widen：要加宽的节点（节点 id → 最小宽度），见 layoutLineView。
 */
function toElkGraph(view: LineView, widen: Map<string, number>, via: Set<string> = new Set()): Built {
  const kinds = new Map<string, PositionedBox['kind']>()
  const parents = new Map<string, string | null>()
  const edgeKinds = new Map<string, PositionedEdge['kind']>()
  const elkNodes = new Map<string, ElkNode>()
  const root: ElkNode = { id: '__root__', layoutOptions: ROOT_OPTIONS, children: [], edges: [] }

  const childrenOf = (parent: string | null): ElkNode[] =>
    parent === null ? root.children! : (elkNodes.get(parent)!.children ??= [])

  for (const c of view.cards) {
    const n: ElkNode = { id: c.id }
    if (c.expanded) {
      n.layoutOptions = containerOptions()
      n.children = []
      n.edges = []
      // 卡片至少有标题栏那么宽：给一个最小宽度。elkjs 0.12 的 layered 自上而下排、含子图（INCLUDE_CHILDREN）时，
      // 容器的 nodeSize.minimum 是按转过来的坐标用的（写 (宽,高) 得到的是宽 ≥ 高、高 ≥ 宽），所以这里倒过来写成
      // (高,宽)；layout.test.ts 的"展开的卡片至少有标题栏那么宽"看着它
      n.layoutOptions['elk.nodeSize.constraints'] = 'MINIMUM_SIZE'
      n.layoutOptions['elk.nodeSize.minimum'] = `(${cardHeaderHeight() + 2 * METRICS.card.inner},${cardHeaderWidth(c)})`
    } else {
      Object.assign(n, cardSize(c))
    }
    elkNodes.set(c.id, n)
    kinds.set(c.id, 'card')
    parents.set(c.id, c.parent)
  }
  // 钻入卡片挂到所在卡片里；任务卡片在顶层（按视图里的顺序，先序）
  for (const c of view.cards) childrenOf(c.parent).push(elkNodes.get(c.id)!)

  const startOf = new Map(view.nodes.filter((n) => n.implicit === 'start').map((n) => [n.card, n.id]))
  const leafNodes = new Map<string, ElkNode>()
  for (const n of view.nodes) {
    const size = nodeSize(n)
    const elkNode: ElkNode = { id: n.id, width: Math.max(size.width, widen.get(n.id) ?? 0), height: size.height }
    leafNodes.set(n.id, elkNode)
    const start = startOf.get(n.card)
    if (n.implicit === 'uncaught' && start) {
      // "未捕获异常退出"没有入边（任何节点都可能从这里离开）：放在第一层，和开始节点并排在卡片的第一行。
      // （elkjs 0.12 不理会 inLayerSuccOf / inLayerPredOf，左右由 ELK 定，实际数据里都在开始节点左边。）
      elkNode.layoutOptions = { 'elk.layered.layering.layerConstraint': 'FIRST' }
    }
    childrenOf(n.card).push(elkNode)
    kinds.set(n.id, 'node')
    parents.set(n.id, n.card)
  }
  for (const f of view.folds) {
    if (!f.collapsed) continue
    const size = foldSize(f)
    childrenOf(f.card).push({ id: f.id, width: size.width, height: size.height })
    kinds.set(f.id, 'fold')
    parents.set(f.id, f.card)
  }
  // 卡片里的子元素按它们在函数里的顺序：DEPTH_FIRST 判回边时从先出现的节点开始走，结果才和 collapse.ts 的一致。
  // 同一个步骤的钻入链按链上的顺序（drillOrder）
  const order = new Map<string, number>()
  for (const n of view.nodes) order.set(n.id, n.order)
  for (const f of view.folds) order.set(f.id, f.order)
  for (const c of view.cards) if (c.anchor) order.set(c.id, drillOrder(c))
  for (const n of elkNodes.values()) n.children?.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))

  // 分支的左右（见 branchPorts、branchKeys）：
  // 1. 有两条以上往下的出边的节点，出边从下边上固定顺序的端口出发；
  // 2. 这些出边的终点（节点、折叠占位、钻入卡片）在同一行里的左右按 branchKeys 给的先后（elk.position，
  //    semiInteractive 只排给了先后的，别的照旧由 ELK 排）；
  // 3. 终点比同一节点别的分支终点靠下的长分支（via，第二遍排版时才有），经过一个排好先后的中转点。
  const ported = branchPorts(view)
  const portOf = new Map<string, string>()
  const portIndex = new Map<string, number>()
  const selfPorts = new Map<string, { out: string; in: string }>()
  for (const [src, ordered] of ported) {
    const elkNode = leafNodes.get(src)!
    const slots = portSlots(ordered)
    const last = slots.length - 1
    const port = (id: string, side: string, index: number) => ({
      id,
      width: 0,
      height: 0,
      layoutOptions: { 'elk.port.side': side, 'elk.port.index': String(index) },
    })
    // FIXED_ORDER 的端口序号按顺时针数（上边从左往右、右边从上往下、下边从右往左、左边从下往上）：
    // 下边上最左边的序号最大
    elkNode.ports = slots.map((e, i) => {
      const id = e ? `${e.id}#port` : `${src}#pad${i}`
      if (e) {
        portOf.set(e.id, id)
        portIndex.set(e.id, i)
      }
      return port(id, 'SOUTH', last - i)
    })
    // 自环（循环节点指回自己的 loop_back）：没有端口的节点上 ELK 把它画成贴着左边的小圈，有了端口就改成从下边
    // 绕到上边的大圈；给它在左边开一进一出两个端口（出在上、进在下），照旧画成左边的小圈
    let next = last + 1
    for (const e of view.edges) {
      if (e.src !== src || e.dst !== src) continue
      const p = { in: `${e.id}#in`, out: `${e.id}#out` }
      elkNode.ports.push(port(p.in, 'WEST', next++), port(p.out, 'WEST', next++))
      selfPorts.set(e.id, p)
    }
    elkNode.layoutOptions = { ...elkNode.layoutOptions, 'elk.portConstraints': 'FIXED_ORDER' }
  }
  const keys = branchKeys(view, ported)
  const position = (k: number) => ({ 'elk.position': `(${k},${k})` })
  const elements = new Map<string, ElkNode>(leafNodes)
  for (const c of view.cards) elements.set(c.id, elkNodes.get(c.id)!)
  for (const n of elkNodes.values()) for (const c of n.children ?? []) elements.set(c.id, c)
  for (const list of ported.values()) {
    for (const e of list) {
      const n = elements.get(e.dst)
      if (n) n.layoutOptions = { ...n.layoutOptions, ...position(keys.get(e.dst)!) }
    }
  }

  const addEdge = (container: string | null, e: ElkExtendedEdge) => {
    const list = container === null ? root.edges! : (elkNodes.get(container)!.edges ??= [])
    list.push(e)
  }
  for (const e of view.edges) {
    let target = e.dst
    if (via.has(e.id)) {
      // 中转点：没有大小，排在源节点下面一行（从同一个端口再连一条并行的看不见的连线，分层时两条入边比一条
      // 出边重，把它拉到紧挨着源节点的那一行），左右的先后跟终点一样（同一终点的几条按端口先后）
      const w = `${e.id}#via`
      const k = keys.get(e.dst)! + (portIndex.get(e.id)! + 1) / 1000
      childrenOf(e.card).push({ id: w, width: 0, height: 0, layoutOptions: position(k) })
      addEdge(e.card, { id: `${e.id}#pin`, sources: [portOf.get(e.id)!], targets: [w] })
      addEdge(e.card, { id: `${e.id}#rest`, sources: [w], targets: [e.dst] })
      target = w
    }
    const self = selfPorts.get(e.id)
    const edge: ElkExtendedEdge = self
      ? { id: e.id, sources: [self.out], targets: [self.in] }
      : { id: e.id, sources: [portOf.get(e.id) ?? e.src], targets: [target] }
    if (e.label) {
      const size = labelSize(e.label)
      const label: ElkLabel = {
        id: `${e.id}#label`,
        text: e.label,
        width: size.width,
        height: size.height,
        layoutOptions: { 'elk.edgeLabels.placement': 'TAIL' },
      }
      edge.labels = [label]
    }
    addEdge(e.card, edge)
    edgeKinds.set(e.id, 'edge')
  }
  for (const l of view.links) {
    addEdge(null, { id: l.id, sources: [l.src], targets: [l.dst] })
    edgeKinds.set(l.id, 'link')
  }
  return { root, kinds, parents, edgeKinds }
}

/**
 * 一条出边在源节点下边上从左往右排第几档（小的在左）：if 的 是、match 的各个模式、循环的 每一项（条件成立）在左；
 * 否、都不匹配、循环结束 在右；别的（next、break、finally）在中间；raise 在最右，但和某条分支落到同一个节点时
 * 夹在左右两档之间（挨着那条分支、不把它的标签挤在中间）。sameTarget：这条 raise 落到的节点也是别的出边的终点。
 */
function branchRank(e: EdgeView, sameTarget: boolean): number {
  if (e.kind === 'raise') return sameTarget ? 1.5 : 3
  if (e.branch !== null) return e.branch === 'no' || e.branch === 'else' ? 2 : 0
  if (e.kind === 'loop_each') return 0
  if (e.kind === 'loop_exit') return 2
  return 1
}

/** 连线在图里的终点（EdgeView.keys 的一项是"起点 id|终点 id|kind|label"，id 是 n1、n2……） */
const graphDst = (key: string): string => key.split('|')[1]

/**
 * 分支的左右只由图定。ELK 自己定一个节点几条出边的左右（交叉最少的那种），视图一变（展开一个折叠、钻入）就可能
 * 整个左右翻过来。有两条以上往下的出边的节点，出边从下边上固定顺序的端口出发：同一档（branchRank）的按图里连线的
 * 顺序。顺序只看连线本身（种类、分支、图里的终点），和分支那头画成节点、折叠占位还是钻入卡片无关，展开收起都不变。
 * 回边不在这里（照旧由 ELK 从源节点下方绕回去）；自环也不在这里，有端口的节点上它走左边的一对端口，画法照旧
 * （见 toElkGraph）。只有节点有端口，折叠占位、钻入卡片发出的连线照旧。
 * 返回 节点 id → 按从左到右排好的出边。
 */
function branchPorts(view: LineView): Map<string, EdgeView[]> {
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  const outs = new Map<string, { e: EdgeView; i: number }[]>()
  view.edges.forEach((e, i) => {
    if (e.back || e.src === e.dst || !nodeIds.has(e.src)) return
    let list = outs.get(e.src)
    if (!list) outs.set(e.src, (list = []))
    list.push({ e, i })
  })
  const result = new Map<string, EdgeView[]>()
  for (const [src, list] of outs) {
    if (list.length < 2) continue
    const others = new Set(list.filter((x) => x.e.kind !== 'raise').flatMap((x) => x.e.keys.map(graphDst)))
    const rank = new Map(list.map((x) => [x.e, branchRank(x.e, x.e.keys.some((k) => others.has(graphDst(k))))]))
    list.sort((a, b) => rank.get(a.e)! - rank.get(b.e)! || a.i - b.i)
    result.set(
      src,
      list.map((x) => x.e),
    )
  }
  return result
}

/**
 * 分支终点在同一行里的先后：从每张卡片的开始节点深度优先走（不走回边），分支节点的出边按端口的左右走，先走到的
 * 排在左边；raise 的终点（异常处理）等别的都走完了再走，排在右边。一个分支里的东西都在它右边的分支之前走到，所以
 * 同一行里左边分支的终点在右边分支的终点左边——除非右边分支的终点在这之前已经从别处（更早的分支）走到过，那时
 * 两个判断的要求互相矛盾，先走到的那个说了算。
 * 只由视图的连线定，和 ELK 无关。展开一个折叠区域时，区域只能从它的入口进入，展开前后都在同一时刻走到它，区域外
 * 元素的先后跟着不变（区域有两个以上出口时，出口那头的先后按图里连线的顺序或按深度优先的顺序，可能不同；测试里
 * 的真实图没有这种情况，见 layout.test.ts 的"分支的左右"）。
 * 返回 元素 id（节点、折叠占位、钻入卡片）→ 先后。
 */
function branchKeys(view: LineView, ported: Map<string, EdgeView[]>): Map<string, number> {
  const outs = new Map<string, EdgeView[]>()
  for (const e of view.edges) {
    if (e.back || e.src === e.dst) continue
    let list = outs.get(e.src)
    if (!list) outs.set(e.src, (list = []))
    list.push(e)
  }
  for (const [src, list] of ported) outs.set(src, list)
  const key = new Map<string, number>()
  const later: string[] = []
  const visit = (root: string) => {
    if (key.has(root)) return
    key.set(root, key.size)
    const stack = [{ id: root, i: 0 }]
    while (stack.length) {
      const top = stack[stack.length - 1]
      const list = outs.get(top.id) ?? []
      if (top.i >= list.length) {
        stack.pop()
        continue
      }
      const e = list[top.i++]
      if (e.kind === 'raise') later.push(e.dst)
      else if (!key.has(e.dst)) {
        key.set(e.dst, key.size)
        stack.push({ id: e.dst, i: 0 })
      }
    }
  }
  const drain = () => {
    while (later.length) visit(later.shift()!)
  }
  for (const n of view.nodes) if (n.implicit === 'start') visit(n.id)
  drain()
  // 从开始节点走不到的（没有入边的节点等）按在函数里的顺序
  const rest = [
    ...view.nodes.map((n) => [n.id, n.order] as const),
    ...view.folds.filter((f) => f.collapsed).map((f) => [f.id, f.order] as const),
    ...view.cards.filter((c) => c.anchor).map((c) => [c.id, drillOrder(c)] as const),
  ].sort((a, b) => a[1] - b[1])
  for (const [id] of rest) {
    visit(id)
    drain()
  }
  return key
}

/**
 * 节点下边上的端口，从左到右（ELK 把它们均匀排开）。主干那条出边的端口放在正中：两侧端口少的那边补上没有连线的
 * 空端口，补到两边一样多。不这样的话主干每经过一个"是 → 提前结束"的判断就往右挪一截。没有主干出边的节点不补。
 * null 是空端口。
 */
function portSlots(ordered: EdgeView[]): (EdgeView | null)[] {
  const t = ordered.findIndex((e) => e.trunk)
  if (t < 0) return ordered
  const left = t
  const right = ordered.length - 1 - t
  const pad = (k: number) => Array.from({ length: Math.max(0, k) }, () => null)
  return [...pad(right - left), ...ordered, ...pad(left - right)]
}

/** 加宽重排最多几轮 */
const WIDEN_ROUNDS = 2

/**
 * 排版：返回每个卡片、节点、折叠占位的位置和尺寸，以及连线的折点和标签位置。
 *
 * ELK 把一个节点下边上的几条出边均匀排开（间距 = 节点宽 / (端口数 + 1)，端口数含让主干居中的空端口，见
 * portSlots）。循环节点有三条出边（每一项、循环结束、raise）时间距只有 40 像素左右，中间那条的标签哪一侧都放不下、
 * 必然压在邻边上。placeLabels 找不到位置的标签，把它的源节点加宽到每个间距放得下最宽的标签，再排一次（只有这种
 * 节点变宽，别的不动）。
 */
export async function layoutLineView(view: LineView): Promise<Positioned> {
  const widen = new Map<string, number>()
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  const ported = branchPorts(view)
  for (let round = 0; ; round++) {
    const { positioned, stuck } = await layoutOnce(view, widen)
    let changed = false
    if (round < WIDEN_ROUNDS) {
      for (const id of stuck) {
        const e = view.edges.find((x) => x.id === id)!
        if (!nodeIds.has(e.src)) continue
        const out = view.edges.filter((x) => x.src === e.src && x.dst !== e.src)
        const widest = Math.max(...out.map((x) => positioned.edges[x.id]?.label?.width ?? 0))
        // 下边上的出发点：每条出边一个，加上让主干居中补的空端口（见 portSlots）
        const list = ported.get(e.src)
        const pads = list ? portSlots(list).length - list.length : 0
        const need = Math.ceil((out.length + pads + 1) * (widest + LABEL_SLOT))
        if (need > (positioned.boxes[e.src]?.width ?? 0) && need > (widen.get(e.src) ?? 0)) {
          widen.set(e.src, need)
          changed = true
        }
      }
    }
    if (!changed) return positioned
  }
}

async function layoutOnce(view: LineView, widen: Map<string, number>): Promise<{ positioned: Positioned; stuck: string[] }> {
  let built = toElkGraph(view, widen)
  let result = await elk().layout(built.root)
  const via = longBranches(view, extract(built, result).boxes)
  if (via.size) {
    built = toElkGraph(view, widen, via)
    result = await elk().layout(built.root)
  }
  const { boxes, edges } = extract(built, result)
  return finish(view, boxes, edges)
}

/**
 * 分支节点下面一行里有它的分支终点时，别的（终点更靠下的）分支连线：它们在下面一行里的位置是 ELK 加的虚拟点，
 * 不受先后的约束，展开收起时会跑到另一侧。返回这些连线的 id，第二遍排版让它们经过一个排好先后的中转点。
 */
function longBranches(view: LineView, boxes: Record<string, PositionedBox>): Set<string> {
  const out = new Set<string>()
  const byParent = new Map<string | null, PositionedBox[]>()
  for (const b of Object.values(boxes)) {
    let list = byParent.get(b.parent)
    if (!list) byParent.set(b.parent, (list = []))
    list.push(b)
  }
  for (const [src, list] of branchPorts(view)) {
    const s = boxes[src]
    if (!s) continue
    const siblings = byParent.get(s.parent) ?? []
    const bottom = s.y + s.height
    // 终点在下面一行：源节点和它之间没有别的方框
    const nextRow = (t: PositionedBox) => !siblings.some((x) => x.y >= bottom - EPS && x.y + x.height <= t.y + EPS && x.id !== t.id && x.id !== src)
    const near = list.filter((e) => boxes[e.dst] && nextRow(boxes[e.dst]))
    if (near.length === 0 || near.length === list.length) continue
    for (const e of list) if (!near.includes(e)) out.add(e.id)
  }
  return out
}

const EPS = 0.5

/** ELK 的结果 → 方框和连线（经过中转点的连线接回一条）。 */
function extract(built: Built, result: ElkNode): { boxes: Record<string, PositionedBox>; edges: Record<string, PositionedEdge> } {
  const boxes: Record<string, PositionedBox> = {}
  const edges: Record<string, PositionedEdge> = {}
  const rest = new Map<string, Point[]>()
  const visit = (n: ElkNode) => {
    for (const c of n.children ?? []) {
      const kind = built.kinds.get(c.id)
      if (kind) {
        boxes[c.id] = {
          id: c.id,
          kind,
          x: c.x ?? 0,
          y: c.y ?? 0,
          width: c.width ?? 0,
          height: c.height ?? 0,
          parent: built.parents.get(c.id) ?? null,
        }
      }
      visit(c)
    }
    for (const e of (n.edges ?? []) as ElkExtendedEdge[]) {
      const points: Point[] = []
      for (const s of e.sections ?? []) {
        if (points.length === 0) points.push({ x: s.startPoint.x, y: s.startPoint.y })
        for (const b of s.bendPoints ?? []) points.push({ x: b.x, y: b.y })
        points.push({ x: s.endPoint.x, y: s.endPoint.y })
      }
      if (e.id.endsWith('#rest')) rest.set(e.id.slice(0, -'#rest'.length), points)
      const kind = built.edgeKinds.get(e.id)
      if (!kind) continue
      const l = e.labels?.[0]
      const label = l ? { x: l.x ?? 0, y: l.y ?? 0, width: l.width ?? 0, height: l.height ?? 0 } : null
      edges[e.id] = { id: e.id, kind, points, label }
    }
  }
  visit(result)
  for (const [id, pts] of rest) {
    const e = edges[id]
    if (e) e.points = joinAt(e.points, pts)
  }
  return { boxes, edges }
}

/**
 * 两段折线在中转点接起来。中转点没有大小，前一段的终点就是后一段的起点：去掉重合的那个点；接点前后在同一条
 * 竖线（或横线）上时接点也去掉，接成一段。
 */
function joinAt(a: Point[], b: Point[]): Point[] {
  if (a.length === 0 || b.length === 0) return [...a, ...b]
  const same = (p: Point, q: Point) => Math.abs(p.x - q.x) < 0.01 && Math.abs(p.y - q.y) < 0.01
  const head = same(a[a.length - 1], b[0]) ? a.slice(0, -1) : a
  const pts = [...head, ...b]
  const i = head.length // 接点（后一段的起点）
  const [r, q, p] = [pts[i - 1], pts[i], pts[i + 1]]
  if (r && p && ((Math.abs(r.x - q.x) < 0.01 && Math.abs(q.x - p.x) < 0.01) || (Math.abs(r.y - q.y) < 0.01 && Math.abs(q.y - p.y) < 0.01))) {
    pts.splice(i, 1)
  }
  return pts
}

function finish(
  view: LineView,
  boxes: Record<string, PositionedBox>,
  edges: Record<string, PositionedEdge>,
): { positioned: Positioned; stuck: string[] } {
  // 修整：拉直起点处的小台阶（带标签的出边不拉直：标签贴着第一段），再给被别的连线穿过的标签换位置
  const sources = new Map<string, string>([...view.edges.map((e) => [e.id, e.src] as const), ...view.links.map((l) => [l.id, l.src] as const)])
  for (const e of Object.values(edges)) {
    if (!e.label) e.points = straighten(e.points, boxes[sources.get(e.id) ?? ''])
  }
  const stuck = placeLabels(view, boxes, edges)

  let maxX = 0
  let maxY = 0
  for (const b of Object.values(boxes)) {
    maxX = Math.max(maxX, b.x + b.width)
    maxY = Math.max(maxY, b.y + b.height)
  }
  for (const e of Object.values(edges)) {
    for (const pt of e.points) {
      maxX = Math.max(maxX, pt.x)
      maxY = Math.max(maxY, pt.y)
    }
    if (e.label) {
      maxX = Math.max(maxX, e.label.x + e.label.width)
      maxY = Math.max(maxY, e.label.y + e.label.height)
    }
  }
  return { positioned: { width: Math.ceil(maxX), height: Math.ceil(maxY), boxes, edges }, stuck }
}

// ---------------------------------------------------------------------------------------------------------------
// 排好之后的修整

/**
 * 起点处的小台阶：ELK 让出边从源节点中线偏几像素的地方出发，再横挪几像素对准目标，画出来是一个难看的 S 形小弯。
 * 竖 - 横（不到 JOG 像素）- 竖 的开头，把起点横挪过去拉直；挪过去的起点必须还在源节点的下边上，否则不动。
 */
const JOG = 10
function straighten(pts: Point[], src: Box | undefined): Point[] {
  if (pts.length < 4 || !src) return pts
  const [a, b, c, d] = pts
  const vertical1 = Math.abs(a.x - b.x) < 0.5
  const horizontal = Math.abs(b.y - c.y) < 0.5 && Math.abs(b.x - c.x) < JOG
  const vertical2 = Math.abs(c.x - d.x) < 0.5
  if (!(vertical1 && horizontal && vertical2)) return pts
  if (c.x < src.x + 1 || c.x > src.x + src.width - 1) return pts
  return [{ x: c.x, y: a.y }, ...pts.slice(3)]
}

/** 线段 a-b 是否进入矩形 r 的内部；pad 为正时矩形四边先向外扩，为负时向里缩（Liang–Barsky 裁剪）。 */
function segmentHitsBox(a: Point, b: Point, r: Box, pad: number): boolean {
  const x0 = r.x - pad
  const x1 = r.x + r.width + pad
  const y0 = r.y - pad
  const y1 = r.y + r.height + pad
  if (x1 <= x0 || y1 <= y0) return false
  const dx = b.x - a.x
  const dy = b.y - a.y
  let t0 = 0
  let t1 = 1
  // 每条边界：p·t < q 的那部分在里面
  for (const [p, q] of [
    [-dx, a.x - x0],
    [dx, x1 - a.x],
    [-dy, a.y - y0],
    [dy, y1 - a.y],
  ]) {
    if (p === 0) {
      if (q <= 0) return false
      continue
    }
    const t = q / p
    if (p < 0) t0 = Math.max(t0, t)
    else t1 = Math.min(t1, t)
    if (t0 >= t1) return false
  }
  return true
}

const boxesOverlap = (a: Box, b: Box, pad: number): boolean =>
  a.x - pad < b.x + b.width && b.x < a.x + a.width + pad && a.y - pad < b.y + b.height && b.y < a.y + a.height + pad

/** 标签换位置时：沿第一段每次挪几像素；新位置离别的连线至少几像素（比离自己那条近不了） */
const LABEL_STEP = 4
const LABEL_CLEAR = 3
/** 加宽节点时，相邻两条出边之间除了标签还要留的宽度：标签离自己那条 3 像素，离邻边 LABEL_CLEAR 以上 */
const LABEL_SLOT = 3 + LABEL_CLEAR + 2

const isVertical = (a: Point, b: Point) => Math.abs(a.x - b.x) < 0.5
const isHorizontal = (a: Point, b: Point) => Math.abs(a.y - b.y) < 0.5
const flip = (p: Point): Point => ({ x: p.y, y: p.x })
const flipBox = (r: Box): Box => ({ x: r.y, y: r.x, width: r.height, height: r.width })

/**
 * 标签的候选位置，按离原位置由近到远：
 * 1. 换到第一段的另一侧（同一高度）；
 * 2. 沿第一段往前挪，两侧轮流（标签整个贴在第一段旁边，和它的距离不变）；
 * 3. 第一段太短、两旁都被别的出边占着时（循环节点三条出边挤在一起，中间那条的标签放不下）：贴在第一个拐角上，
 *    先放拐角外侧、沿第二段，再放拐角内侧——仍挨着第一段的末端。
 * 第一段竖着时直接算；横着时把 x、y 对调了算，再对调回来。第一段斜着时没有候选。
 */
function* labelCandidates(pts: Point[], label: Box): Generator<Box> {
  if (isVertical(pts[0], pts[1])) yield* verticalCandidates(pts, label)
  else if (isHorizontal(pts[0], pts[1])) for (const r of verticalCandidates(pts.map(flip), flipBox(label))) yield flipBox(r)
}

function* verticalCandidates(pts: Point[], label: Box): Generator<Box> {
  const [a, b, c] = pts
  const { width: w, height: h } = label
  const sx = a.x
  const right = label.x + w / 2 >= sx
  const gap = Math.max(0, right ? label.x - sx : sx - (label.x + w))
  const sides = right ? [sx + gap, sx - gap - w] : [sx - gap - w, sx + gap]
  const dir = b.y >= a.y ? 1 : -1
  const lo = Math.min(a.y, b.y)
  const hi = Math.max(a.y, b.y)
  for (let k = 0; ; k++) {
    const y = label.y + dir * k * LABEL_STEP
    if (k > 0 && (y < lo || y + h > hi)) break
    for (const [i, x] of sides.entries()) if (k > 0 || i > 0) yield { x, y, width: w, height: h }
  }
  if (!c || !isHorizontal(b, c)) return
  const turn = c.x >= b.x ? 1 : -1
  // 外侧：第二段的另一边（第一段往下走时在第二段下面），从拐角起沿第二段
  yield { x: turn > 0 ? b.x : b.x - w, y: dir > 0 ? b.y + gap : b.y - gap - h, width: w, height: h }
  // 内侧：第一段和第二段夹着的角里
  yield { x: turn > 0 ? b.x + gap : b.x - gap - w, y: dir > 0 ? b.y - gap - h : b.y + gap, width: w, height: h }
}

/**
 * 标签避让。ELK 把出边的标签放在出边第一段的旁边（TAIL），但不管同一个节点的别的出边会不会从标签上穿过：
 * 循环节点的"每一项""循环结束"两条出边离得近时，一条的标签常压在另一条上；从循环节点出来的 raise 也会。
 * 排好后逐个检查：被别的连线穿过或离别的连线不到 LABEL_CLEAR（贴着时分不清标签属于哪条）、被自己那条的后面几段
 * 穿过的标签，按 labelCandidates 的顺序换位置，取第一个离别的连线至少 LABEL_CLEAR、不压节点和别的标签、仍在所在
 * 卡片内容区里的位置；都不行就留在原处，返回这些连线的 id（layoutLineView 加宽源节点重排）。
 */
function placeLabels(view: LineView, boxes: Record<string, PositionedBox>, edges: Record<string, PositionedEdge>): string[] {
  const stuck: string[] = []
  const all = Object.values(edges)
  const header = cardHeaderHeight()
  const labelled = view.edges.filter((e) => edges[e.id]?.label && edges[e.id].points.length >= 2)
  for (const e of labelled) {
    const pe = edges[e.id]
    const own = pe.points
    const card = boxes[e.card]
    const containers = new Set<string>()
    for (let id: string | null = e.card; id; id = boxes[id]?.parent ?? null) containers.add(id)
    const obstacles = Object.values(boxes).filter((b) => !containers.has(b.id))
    const crossed = (r: Box, pad: number, ownPad: number): boolean =>
      all.some((o) => {
        const pts = o.points
        const self = o.id === e.id
        for (let i = self ? 2 : 1; i < pts.length; i++) {
          if (segmentHitsBox(pts[i - 1], pts[i], r, self ? ownPad : pad)) return true
        }
        return false
      })
    // ELK 的位置：别的连线离它不到 LABEL_CLEAR（穿过、或贴着让人分不清标签属于哪条）、自己那条后面几段穿过它，才换
    if (!crossed(pe.label!, LABEL_CLEAR, -1)) continue
    const fits = (r: Box): boolean =>
      (!card ||
        (r.x >= card.x + 1 &&
          r.x + r.width <= card.x + card.width - 1 &&
          r.y >= card.y + header + 1 &&
          r.y + r.height <= card.y + card.height - 1)) &&
      !obstacles.some((b) => boxesOverlap(r, b, 1)) &&
      !all.some((o) => o.id !== e.id && o.label !== null && boxesOverlap(r, o.label, 1)) &&
      !crossed(r, LABEL_CLEAR, 1)
    let placed = false
    for (const cand of labelCandidates(own, pe.label!)) {
      if (fits(cand)) {
        pe.label = cand
        placed = true
        break
      }
    }
    if (!placed) stuck.push(e.id)
  }
  return stuck
}
