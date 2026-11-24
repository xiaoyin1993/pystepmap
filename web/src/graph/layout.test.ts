import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { K, mini, miniLine } from './fixtures/mini'
import { CK, CL, at, bFn, chainFns, chainLine, chainProject, into } from './fixtures/chain'
import { project } from './fixtures/builders'
import {
  RETRY,
  TRICKY,
  WHILE_RAISE,
  finProject,
  nestedProject,
  retryProject,
  sharedProject,
  trickyProject,
  whileRaiseProject,
} from './fixtures/shapes'
import {
  METRICS,
  cardClipped,
  cardHeaderWidth,
  cardMoreText,
  cardMoreWidth,
  cardSize,
  foldChips,
  foldSize,
  layoutLineView,
  lineCount,
  nodeClipped,
  nodeSize,
  textWidth,
  type Box,
  type Point,
  type Positioned,
} from './layout'
import { parseProject, type ProjectGraph } from './types'
import {
  buildFunctionView,
  buildLineView,
  drillChainOf,
  emptyState,
  expandAllFunctionState,
  expandAllState,
  setOpen,
  type EdgeView,
  type LineView,
  type ViewState,
} from './viewmodel'

// examples/transfer 的图（pystepmap analyze 的输出）是仓库里的测试夹具
const transfer = parseProject(JSON.parse(readFileSync(new URL('./fixtures/transfer.json', import.meta.url), 'utf8')))

const EPS = 0.5
const overlap = (a: Box, b: Box) =>
  a.x < b.x + b.width - EPS && b.x < a.x + a.width - EPS && a.y < b.y + b.height - EPS && b.y < a.y + a.height - EPS
const inside = (c: Box, p: Box) =>
  c.x >= p.x - EPS && c.y >= p.y - EPS && c.x + c.width <= p.x + p.width + EPS && c.y + c.height <= p.y + p.height + EPS
const onBorder = (pt: Point, b: Box) => {
  const inX = pt.x >= b.x - 1 && pt.x <= b.x + b.width + 1
  const inY = pt.y >= b.y - 1 && pt.y <= b.y + b.height + 1
  const nearV = Math.abs(pt.x - b.x) <= 1 || Math.abs(pt.x - b.x - b.width) <= 1
  const nearH = Math.abs(pt.y - b.y) <= 1 || Math.abs(pt.y - b.y - b.height) <= 1
  return inX && inY && (nearV || nearH)
}
/** 点到线段的距离 */
const segDist = (p: Point, a: Point, b: Point) => {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = dx * dx + dy * dy
  const t = len === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len))
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
}
const boxDistToSeg = (r: Box, a: Point, b: Point) =>
  Math.min(
    ...[
      { x: r.x, y: r.y },
      { x: r.x + r.width, y: r.y },
      { x: r.x, y: r.y + r.height },
      { x: r.x + r.width, y: r.y + r.height },
    ].map((c) => segDist(c, a, b)),
  )

/** 排版结果的结构检查：简报要求的几条，加上连线端点落在节点边上、分支标签贴着出边。 */
function checkLayout(view: LineView, p: Positioned) {
  const problems: string[] = []
  const leafIds = [
    ...view.nodes.map((n) => n.id),
    ...view.folds.filter((f) => f.collapsed).map((f) => f.id),
    ...view.cards.filter((c) => !c.expanded).map((c) => c.id),
  ]
  for (const id of [...leafIds, ...view.cards.map((c) => c.id)]) if (!p.boxes[id]) problems.push(`没有位置：${id}`)
  // 1. 叶子节点两两不重叠；同一容器里的卡片也不重叠
  for (let i = 0; i < leafIds.length; i++) {
    for (let j = i + 1; j < leafIds.length; j++) {
      if (overlap(p.boxes[leafIds[i]], p.boxes[leafIds[j]])) problems.push(`重叠：${leafIds[i]} / ${leafIds[j]}`)
    }
  }
  const cards = view.cards
  for (let i = 0; i < cards.length; i++) {
    for (let j = i + 1; j < cards.length; j++) {
      if (cards[i].parent !== cards[j].parent) continue
      if (overlap(p.boxes[cards[i].id], p.boxes[cards[j].id])) problems.push(`卡片重叠：${cards[i].id} / ${cards[j].id}`)
    }
  }
  // 2. 子元素都在所属卡片之内
  for (const b of Object.values(p.boxes)) {
    if (b.parent && !inside(b, p.boxes[b.parent])) problems.push(`出界：${b.id} 不在 ${b.parent} 里`)
  }
  // 3. 连线方向与上下顺序一致（回边、自环除外）；连线两端落在节点边上
  for (const e of view.edges) {
    const s = p.boxes[e.src]
    const d = p.boxes[e.dst]
    const pts = p.edges[e.id]?.points ?? []
    if (pts.length < 2) {
      problems.push(`连线没有折线：${e.id}`)
      continue
    }
    if (!onBorder(pts[0], s)) problems.push(`起点不在节点边上：${e.id}`)
    if (!onBorder(pts[pts.length - 1], d)) problems.push(`终点不在节点边上：${e.id}`)
    if (e.back || e.src === e.dst) continue
    if (s.y + s.height > d.y + EPS) problems.push(`方向不对：${e.id}（${s.y}+${s.height} > ${d.y}）`)
  }
  for (const l of view.links) {
    const s = p.boxes[l.src]
    const d = p.boxes[l.dst]
    if (s.y + s.height > d.y + EPS) problems.push(`派发连线方向不对：${l.id}`)
    const pts = p.edges[l.id]?.points ?? []
    if (pts.length < 2 || !onBorder(pts[pts.length - 1], d)) problems.push(`派发连线终点不在卡片边上：${l.id}`)
  }
  // 4. 分支标签贴着自己那条出边的第一段，不压在任何节点上
  const allLeaf = leafIds.map((id) => p.boxes[id])
  for (const e of view.edges) {
    const pe = p.edges[e.id]
    if (!e.label) continue
    if (!pe.label) {
      problems.push(`标签没有位置：${e.id}`)
      continue
    }
    if (boxDistToSeg(pe.label, pe.points[0], pe.points[1]) > 8) problems.push(`标签离出边太远：${e.id}`)
    if (allLeaf.some((b) => overlap(b, pe.label!))) problems.push(`标签压在节点上：${e.id}`)
  }
  // 5. 分支标签不被别的连线（函数内的连线、派发连线）穿过
  problems.push(...labelCrossings(view, p))
  // 6. 连线（函数内的连线、派发连线）的每一段都不穿过它两端以外的方框（节点、收起的折叠、收起的卡片；擦边不算）
  const ends: [string, string, string][] = [...view.edges.map((e): [string, string, string] => [e.id, e.src, e.dst]), ...view.links.map((l): [string, string, string] => [l.id, l.src, l.dst])]
  for (const [id, src, dst] of ends) {
    const pts = p.edges[id]?.points ?? []
    for (const box of leafIds) {
      if (box === src || box === dst) continue
      let len = 0
      for (let i = 1; i < pts.length; i++) len += lengthInside(pts[i - 1], pts[i], p.boxes[box])
      if (len > 0) problems.push(`连线穿过别的方框：${id} 穿过 ${box} ${len.toFixed(0)} 像素`)
    }
  }
  return problems
}

/** 线段 a-b 穿过矩形 r 的内部（四边各向里缩 1 像素，擦边不算）的长度。 */
function lengthInside(a: Point, b: Point, r: Box): number {
  const x0 = r.x + 1
  const x1 = r.x + r.width - 1
  const y0 = r.y + 1
  const y1 = r.y + r.height - 1
  const len = Math.hypot(b.x - a.x, b.y - a.y)
  let t0 = 0
  let t1 = 1
  for (const [p, q] of [
    [a.x - b.x, a.x - x0],
    [b.x - a.x, x1 - a.x],
    [a.y - b.y, a.y - y0],
    [b.y - a.y, y1 - a.y],
  ]) {
    if (p === 0) {
      if (q <= 0) return 0
    } else if (p < 0) t0 = Math.max(t0, q / p)
    else t1 = Math.min(t1, q / p)
  }
  return t1 > t0 ? (t1 - t0) * len : 0
}

/** 每个分支标签被别的连线穿过的地方："标签被穿过：<标签>（<连线>）被 <另一条> 穿过 N 像素"。 */
function labelCrossings(view: LineView, p: Positioned): string[] {
  const out: string[] = []
  const all = [...view.edges.map((e) => e.id), ...view.links.map((l) => l.id)]
  for (const e of view.edges) {
    const box = p.edges[e.id]?.label
    if (!e.label || !box) continue
    for (const other of all) {
      if (other === e.id) continue
      const pts = p.edges[other]?.points ?? []
      let len = 0
      for (let i = 1; i < pts.length; i++) len += lengthInside(pts[i - 1], pts[i], box)
      if (len > 0) out.push(`标签被穿过：${e.label}（${e.id}）被 ${other} 穿过 ${len.toFixed(0)} 像素`)
    }
  }
  return out
}

async function laidOut(view: LineView) {
  const p = await layoutLineView(view)
  expect(checkLayout(view, p)).toEqual([])
  return p
}

describe('尺寸估算', () => {
  it('中文按全宽，西文按字符估', () => {
    expect(textWidth('处理邮件', 13)).toBe(52)
    expect(textWidth('abc', 10)).toBeLessThan(textWidth('处理邮', 10))
    expect(textWidth('abc', 10, true)).toBe(19)
  })

  it('箭头、省略号、破折号在中文字体里按全宽估', () => {
    expect(textWidth('→←⇒', 10)).toBe(30)
    expect(textWidth('…—', 10)).toBe(20)
  })

  it('折叠占位按摘要行定宽，不截断摘要；只有入口标题那一行有上限', () => {
    const v = buildLineView(transfer, transfer.lines[0].sid)
    const base = v.folds.find((f) => f.kind === 'catch')!
    const long = {
      ...base,
      title: '很长的入口标题'.repeat(10),
      summary: { ...base.summary, nodes: 12, endKinds: ['ok', 'skipped', 'failed', 'exception'] as const, rejoins: true },
    } as unknown as typeof base
    const headline = '异常处理 · 12 个节点 · 以正常 / 跳过 / 失败 / 未捕获异常收尾 · 汇回'
    const w = foldSize(long).width
    expect(w).toBeGreaterThanOrEqual(textWidth(headline, 12) + 2 * 12 + 20)
    // 入口标题再长也不撑宽
    expect(w).toBeLessThan(textWidth(headline, 12) + 2 * 12 + 20 + 1)
    expect(foldSize({ ...base, title: '很长的入口标题'.repeat(10) }).width).toBe(260)
  })

  it('折叠占位里有推测、目标不明的调用：第二行带上小标记，宽度把它们算进去', () => {
    const v = buildLineView(transfer, transfer.lines[0].sid)
    const base = v.folds.find((f) => f.kind === 'catch')!
    // 摘要行短（"异常处理 · 2 个节点"），宽度由第二行定
    const plain = {
      ...base,
      title: '短标题',
      summary: { ...base.summary, nodes: 2, endKinds: [], rejoins: false, guessed: false, unresolved: false },
    }
    const flagged = { ...plain, summary: { ...plain.summary, guessed: true, unresolved: true } }
    expect(foldChips(plain)).toEqual([])
    expect(foldChips(flagged)).toEqual(['推测', '目标不明'])
    expect(foldChips({ ...plain, summary: { ...plain.summary, unresolved: true } })).toEqual(['目标不明'])
    const m = METRICS.fold
    const pad = 2 * m.padX + m.icon
    const chipW = (c: string) => textWidth(c, METRICS.chip.font) + 2 * METRICS.chip.padX + METRICS.chip.gap
    const chips = chipW('推测') + chipW('目标不明')
    // 第二行：标题 + 两个小标记放得下
    const w = foldSize(flagged).width
    expect(w).toBeGreaterThanOrEqual(textWidth('短标题', m.subFont) + chips + pad)
    expect(w).toBeGreaterThan(foldSize(plain).width)
    // 标题很长：宽度到上限为止，标题截断，小标记仍然整个放得下
    const long = { ...flagged, title: '很长的入口标题'.repeat(10) }
    expect(foldSize(long).width).toBe(m.maxWidth)
    expect(foldSize(long).width - pad - chips).toBeGreaterThan(0)
    expect(foldSize(long).height).toBe(foldSize(plain).height)
  })

  it('哪些字被截断（悬停时显示全文用）：按同样的估算，排版加宽后放得下的不算', () => {
    const v = buildLineView(transfer, transfer.lines[0].sid)
    const step = v.nodes.find((n) => n.type === 'step')!
    const at = (n: typeof step) => nodeClipped(n, nodeSize(n).width)
    expect(at({ ...step, title: '短标题', subtitle: 'f' })).toEqual({ title: false, sub: false })
    const long = { ...step, title: '很长的标题'.repeat(12), subtitle: 'module.function_name_that_is_long'.repeat(3) }
    expect(nodeSize(long).width).toBe(METRICS.node.maxWidth)
    expect(at(long)).toEqual({ title: true, sub: true })
    // 两行刚好放下的不算截断
    const twoLines = { ...step, title: '字'.repeat(Math.floor((260 - 2 * 12 - 20) / 13) * 2), subtitle: '' }
    expect(nodeSize(twoLines).titleLines).toBe(2)
    expect(at(twoLines).title).toBe(false)
    // 循环节点被加宽到 600 像素：在 260 像素里要三行的标题，在 600 像素里放得下
    const three = { ...step, title: '字'.repeat(40), subtitle: '' }
    expect(at(three).title).toBe(true)
    expect(nodeClipped(three, 600).title).toBe(false)
    // 结局胶囊：一行，超出最宽就截断
    const end = v.nodes.find((n) => n.type === 'end' && !n.implicit)!
    expect(nodeClipped(end, nodeSize(end).width).title).toBe(false)
    const longEnd = { ...end, title: '返回一个很长很长的结局说明'.repeat(4) }
    expect(nodeClipped(longEnd, nodeSize(longEnd).width).title).toBe(true)
    // 卡片标题栏
    const card = v.cards[0]
    expect(cardClipped(card, cardSize(card).width)).toEqual({ title: false, sub: false })
    const longCard = { ...card, title: '处理 Gmail 推送的入站邮件：分析（LLM 判定 + 规则）'.repeat(2) }
    expect(cardClipped(longCard, cardSize(longCard).width).title).toBe(true)
  })

  it('长标题折两行、宽度有上限，短标题一行', () => {
    expect(lineCount('短', 13, 200, 2)).toBe(1)
    expect(lineCount('很'.repeat(40), 13, 200, 2)).toBe(2)
    const v = buildLineView(transfer, transfer.lines[0].sid)
    for (const n of v.nodes) {
      const s = nodeSize(n)
      expect(s.width).toBeLessThanOrEqual(260)
      expect(s.width).toBeGreaterThanOrEqual(80)
    }
    for (const f of v.folds) expect(foldSize(f).height).toBeGreaterThan(0)
    for (const c of v.cards) expect(cardSize(c).width).toBeGreaterThanOrEqual(220)
  })
})

describe('排版', () => {
  it('transfer：默认视图和全部展开都满足结构检查；未捕获异常退出和开始节点在第一行', async () => {
    const t = transfer.lines[0].sid
    const v = buildLineView(transfer, t)
    const p = await laidOut(v)
    const start = v.nodes.find((n) => n.implicit === 'start')!
    const unc = v.nodes.find((n) => n.implicit === 'uncaught')!
    expect(p.boxes[unc.id].y).toBe(p.boxes[start.id].y)
    await laidOut(buildLineView(transfer, t, expandAllState(transfer, t)))
  })

  it('小项目的功能线：折叠的卡片、展开的卡片、钻入卡片、派发连线', async () => {
    const L = miniLine.sid
    await laidOut(buildLineView(mini, L))
    const all = expandAllState(mini, L)
    const v = buildLineView(mini, L, all)
    const n7 = v.nodes.find((n) => n.key === 'n7' && n.card === v.cards[0].id)!
    const drilled = buildLineView(mini, L, setOpen(all, 'drills', n7.drills[0].id, true))
    const p = await laidOut(drilled)
    const drill = drilled.cards.find((c) => c.kind === 'drill')!
    expect(p.boxes[drill.id]).toMatchObject({ kind: 'card', parent: drilled.cards[0].id })
    // 任务卡片按任务树往下排：被派发的卡片在派发它的卡片下面
    const byFn = (k: string) => p.boxes[drilled.cards.find((c) => c.fn === k && !c.goto)!.id]
    expect(byFn(K.a).y).toBeGreaterThan(byFn(K.entry).y + byFn(K.entry).height)
    expect(byFn(K.b).y).toBeGreaterThan(byFn(K.a).y + byFn(K.a).height)
  })

  it('回边、raise 与普通连线并行、经过 finally 的跳转：方向正确、并行的两条不重合', async () => {
    const v = buildFunctionView(trickyProject, TRICKY, expandAllFunctionState(trickyProject, TRICKY))
    expect(v.folds.every((f) => !f.collapsed)).toBe(true)
    const p = await laidOut(v)
    const back = v.edges.filter((e) => e.back)
    expect(back.map((e) => e.kind)).toEqual(['loop_back'])
    const parallel = v.edges.filter((e) => e.src.endsWith('cond:n6') && e.dst.endsWith('step:n8'))
    expect(parallel.map((e) => e.kind).sort()).toEqual(['no', 'raise'])
    expect(p.edges[parallel[0].id].points).not.toEqual(p.edges[parallel[1].id].points)
  })

  it('循环节点的出边挤在一起：标签都不被别的连线穿过，仍贴着自己那条出边的第一段', async () => {
    // while True：raise 从"条件成立"和"循环结束"之间出来，"条件成立"的标签换到另一侧
    const w = buildFunctionView(whileRaiseProject, WHILE_RAISE)
    const pw = await laidOut(w)
    expect(w.edges.filter((e) => e.label).map((e) => e.label).sort()).toEqual(['条件成立', '循环结束'].sort())
    const loopW = w.nodes.find((n) => n.type === 'loop')!
    expect(pw.boxes[loopW.id].width).toBe(nodeSize(loopW).width) // 换一侧就够，循环节点不用加宽
    // for：每一项、循环结束、raise 三条出边间距不到 40 像素，中间的标签哪侧都放不下 → 循环节点加宽后重排
    const r = buildFunctionView(retryProject, RETRY)
    const pr = await laidOut(r)
    const loopR = r.nodes.find((n) => n.type === 'loop')!
    const exits = r.edges.filter((e) => e.src === loopR.id)
    expect(exits.map((e) => e.kind).sort()).toEqual(['loop_each', 'loop_exit', 'raise'])
    expect(pr.boxes[loopR.id].width).toBeGreaterThan(nodeSize(loopR).width)
    // 只有它加宽
    for (const n of r.nodes) if (n.id !== loopR.id) expect(pr.boxes[n.id].width).toBe(nodeSize(n).width)
  })

  it('同一个视图排两次结果相同', async () => {
    const v = buildLineView(transfer, transfer.lines[0].sid)
    expect(await layoutLineView(v)).toEqual(await layoutLineView(v))
  })

  it('钻入链：同一个步骤的几张钻入卡片在所在卡片里按链上的顺序从上往下排，连接线从上一张连到下一张；排两次一样', async () => {
    const n3 = at('step:n3')
    // 开的先后（状态里的顺序）不影响
    const ids = [into(n3, CK.c), into(n3, CK.a), into(n3, CK.b)]
    const s = ids.reduce((acc, id) => setOpen(acc, 'drills', id, true), expandAllState(chainProject, CL))
    const v = buildLineView(chainProject, CL, s)
    const p = await laidOut(v)
    const chain = drillChainOf(v, n3)
    expect(chain.map((c) => c.fn)).toEqual([CK.a, CK.b, CK.c])
    const boxes = chain.map((c) => p.boxes[c.id])
    for (let i = 1; i < boxes.length; i++) expect(boxes[i].y).toBeGreaterThan(boxes[i - 1].y + boxes[i - 1].height)
    // 在步骤原来的位置：上一个节点之下、下一个节点之上
    expect(boxes[0].y).toBeGreaterThan(p.boxes[at('step:n2')].y)
    expect(p.boxes[at('cond:n4')].y).toBeGreaterThan(boxes[2].y + boxes[2].height)
    for (const e of v.edges.filter((x) => x.chain)) {
      const pts = p.edges[e.id].points
      expect(onBorder(pts[0], p.boxes[e.src])).toBe(true)
      expect(onBorder(pts[pts.length - 1], p.boxes[e.dst])).toBe(true)
      expect(p.edges[e.id].label).toBeNull()
    }
    expect(await layoutLineView(v)).toEqual(p)
    // 状态里换个先后，排出来一样
    const s2 = [...ids].reverse().reduce((acc, id) => setOpen(acc, 'drills', id, true), expandAllState(chainProject, CL))
    expect(await layoutLineView(buildLineView(chainProject, CL, s2))).toEqual(p)
  })

  it('展开的卡片至少有标题栏那么宽（标题、"再展开 N 个"放得下），不因为标题栏宽而变高（elkjs 的最小尺寸是倒过来写的）', async () => {
    const longB = { ...bFn, title: '一个标题相当长的被调函数，写得很长很长很长' }
    const g = project([...chainFns.filter((f) => f.key !== CK.b), longB], [chainLine])
    const n2 = at('step:n2')
    const v = buildLineView(g, CL, setOpen(emptyState(), 'drills', into(n2, CK.b), true))
    const p = await laidOut(v)
    const b = drillChainOf(v, n2)[0]
    expect(cardMoreText(b)).toBe('再展开 1 个')
    const want = cardHeaderWidth(b)
    expect(want).toBe(METRICS.card.maxWidth)
    expect(p.boxes[b.id].width).toBeGreaterThanOrEqual(want - EPS)
    expect(p.boxes[b.id].height).toBeLessThan(want)
    for (const c of v.cards) if (c.expanded) expect(p.boxes[c.id].width).toBeGreaterThanOrEqual(cardHeaderWidth(c) - EPS)
    const t = buildLineView(transfer, transfer.lines[0].sid, expandAllState(transfer, transfer.lines[0].sid))
    const pt = await laidOut(t)
    for (const c of t.cards) if (c.expanded) expect(pt.boxes[c.id].width).toBeGreaterThanOrEqual(cardHeaderWidth(c) - EPS)
  })

  it('钻入卡片的"再展开 N 个"：同一个步骤还有没展开的被调函数时才有；标题栏的宽度把它算进去，标题在它左边截断', () => {
    const n3 = at('step:n3')
    const v = buildLineView(chainProject, CL, setOpen(emptyState(), 'drills', into(n3, CK.b), true))
    const b = drillChainOf(v, n3)[0]
    expect(cardMoreText(b)).toBe('再展开 2 个')
    expect(cardMoreWidth(b)).toBe(textWidth('再展开 2 个', METRICS.card.moreFont) + METRICS.card.morePad)
    const plain = { ...b, anchor: null }
    expect(cardMoreText(plain)).toBeNull()
    expect(cardMoreWidth(plain)).toBe(0)
    expect(cardMoreText(v.cards[0])).toBeNull()
    // 标题加上按钮比函数名那一行宽时，标题栏按标题 + 按钮算
    const long = { ...b, title: '一个相当长的被调函数标题' }
    expect(cardHeaderWidth(long)).toBe(
      Math.min(METRICS.card.maxWidth, Math.max(METRICS.card.minWidth, textWidth(long.title, METRICS.card.titleFont) + cardMoreWidth(b) + 2 * METRICS.card.padX)),
    )
    // 估算的截断也扣掉按钮
    const w = textWidth(long.title, METRICS.card.titleFont) + 2 * METRICS.card.padX
    expect(cardClipped({ ...long, anchor: null }, w).title).toBe(false)
    expect(cardClipped(long, w).title).toBe(true)
    // 都开了：没有按钮
    const all = buildLineView(chainProject, CL, [CK.a, CK.b, CK.c].reduce((acc, f) => setOpen(acc, 'drills', into(n3, f), true), emptyState()))
    expect(drillChainOf(all, n3).map(cardMoreText)).toEqual([null, null, null])
  })

  it('空的卡片（函数没有节点）也能排', async () => {
    const v = buildLineView(mini, mini.lines[2].sid, emptyState())
    const p = await laidOut(v)
    expect(p.boxes[v.cards[0].id].width).toBeGreaterThan(0)
  })
})

// 真实项目的图（别的项目的内容，不进仓库）：环境变量 PYSTEPMAP_TEST_GRAPHS 指向一个放了图 JSON 的目录时才跑，目录里的
// 每个 *.json 都测。生成：uv run pystepmap analyze <项目目录> [--config <配置>] -o <目录>/<名字>.json
// 没设这个变量时只跑 transfer。
const REAL_DIR = process.env.PYSTEPMAP_TEST_GRAPHS ?? ''
const REAL: (readonly [string, ProjectGraph])[] = REAL_DIR
  ? readdirSync(REAL_DIR)
      .filter((f) => f.endsWith('.json'))
      .sort()
      .map((f) => [f, parseProject(JSON.parse(readFileSync(join(REAL_DIR, f), 'utf8')))] as const)
  : []

// 每条功能线、默认和全部展开两种状态：结构检查（含"标签不被别的连线穿过"）。transfer 总在；真实图见上面。
describe('每条功能线（transfer 和 PYSTEPMAP_TEST_GRAPHS 里的真实图）', () => {
  for (const [file, project] of [['transfer.json（夹具）', transfer] as const, ...REAL]) {
    it(`${file}：默认视图和全部展开都满足结构检查，分支标签不被别的连线穿过`, async () => {
      const problems: string[] = []
      for (const l of project!.lines) {
        const sid = l.sid || `line:${l.id}`
        for (const [name, state] of [
          ['默认', emptyState()],
          ['全部展开', expandAllState(project!, sid)],
        ] as const) {
          const view = buildLineView(project!, sid, state)
          for (const msg of checkLayout(view, await layoutLineView(view))) problems.push(`${sid} ${name}：${msg}`)
        }
      }
      expect(problems).toEqual([])
    }, 120_000)
  }
})

describe.skipIf(REAL.length === 0)('规模（真实图）', () => {
  for (const [file, project] of REAL) {
    it(`${file}：节点最多的 5 个函数，单次排版不超过 2 秒，最大的那个默认折叠后不超过 4000 像素高`, async () => {
      const top = Object.values(project!.functions)
        .sort((a, b) => b.nodes.length - a.nodes.length || (a.key < b.key ? -1 : 1))
        .slice(0, 5)
      const rows: string[] = []
      for (const [i, fn] of top.entries()) {
        const view = buildFunctionView(project!, fn.key)
        const t0 = performance.now()
        const p = await layoutLineView(view)
        const ms = performance.now() - t0
        expect(checkLayout(view, p)).toEqual([])
        expect(ms).toBeLessThan(2000)
        const shown = view.nodes.length
        const folds = view.folds.filter((f) => f.collapsed).length
        rows.push(`${fn.qualname}（${fn.nodes.length} 个节点）：显示 ${shown} 个节点 + ${folds} 个折叠，${p.width}×${p.height}，${ms.toFixed(0)} ms`)
        if (i === 0) expect(p.height).toBeLessThanOrEqual(4000)
        // 全部展开也要排得出来、满足结构检查
        const all = buildFunctionView(project!, fn.key, expandAllFunctionState(project!, fn.key))
        const t1 = performance.now()
        const pa = await layoutLineView(all)
        expect(checkLayout(all, pa)).toEqual([])
        expect(performance.now() - t1).toBeLessThan(2000)
        rows.push(`    全部展开：${all.nodes.length} 个节点，${pa.width}×${pa.height}，${(performance.now() - t1).toFixed(0)} ms`)
      }
      console.log(`${file}\n${rows.join('\n')}`)
    })
  }
})

// ---------------------------------------------------------------------------------------------------------------
// 分支的左右（layout.ts 的 branchPorts、branchKeys）：展开、收起、钻入都不让分支左右翻转；规则是 是 / 模式 / 每一项
// 在左，否 / 都不匹配 / 循环结束 在右，raise 在最右（和某条分支落到同一个节点时夹在两者中间）

/** 一条往下的分支出边在哪一侧（见 branchSides） */
interface Side {
  /** 分支连线穿过"离源节点最近的那一行分支终点"的顶边时的横坐标 */
  x: number
  /** 终点：节点、折叠占位或钻入卡片的 id */
  box: string
  /** 终点就在最近的那一行（和那一行的分支终点上下重叠） */
  near: boolean
  edge: EdgeView
}

/** 折线第一次穿过水平线 y 时的横坐标；穿不过时为 NaN */
function crossingX(pts: Point[], y: number): number {
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1]
    const b = pts[i]
    if (a.y !== b.y && (a.y - y) * (b.y - y) <= 0) return a.x + ((b.x - a.x) * (y - a.y)) / (b.y - a.y)
  }
  return NaN
}

/**
 * 判断节点、循环节点每条往下的分支出边（回边、自环除外）在哪一侧：出边穿过离它最近的那一行分支终点的顶边时的
 * 横坐标。终点在同一行时这就是终点方框里的一点，左右和两个方框的左右一致；终点不在同一行时（长分支：没有 else 的
 * if，"否"直接落到"是"那一串下面的汇合处；循环结束绕过整个循环体），汇合处常常就在"是"那一串的正下方，比较两个
 * 方框的横坐标说明不了分支往哪边走，看得见的是连线经过那一行时在哪一边。
 * 节点 id → 图里的连线（EdgeView.keys 的一项，跨状态不变；一条视图连线代表几条时每条都算）→ Side。
 */
function branchSides(view: LineView, p: Positioned): Map<string, Map<string, Side>> {
  const types = new Map(view.nodes.map((n) => [n.id, n.type]))
  const outs = new Map<string, EdgeView[]>()
  for (const e of view.edges) {
    const t = types.get(e.src)
    if ((t !== 'cond' && t !== 'loop') || e.back || e.src === e.dst) continue
    outs.set(e.src, [...(outs.get(e.src) ?? []), e])
  }
  const res = new Map<string, Map<string, Side>>()
  for (const [src, list] of outs) {
    if (list.length < 2) continue
    const nearest = list.map((e) => p.boxes[e.dst]).reduce((a, b) => (b.y < a.y ? b : a))
    const m = new Map<string, Side>()
    for (const e of list) {
      const b = p.boxes[e.dst]
      const side = { x: crossingX(p.edges[e.id].points, nearest.y - 0.5), box: e.dst, near: b.y < nearest.y + nearest.height, edge: e }
      for (const k of e.keys) m.set(k, side)
    }
    res.set(src, m)
  }
  return res
}

/** 规则里的左右：小的在左。sameTarget：这条 raise 落到的节点也是同一节点别的出边的终点（图里的终点）。 */
function ruleRank(e: EdgeView, sameTarget: boolean): number {
  if (e.kind === 'raise') return sameTarget ? 1.5 : 3
  if (e.branch === 'no' || e.branch === 'else' || (e.branch === null && e.kind === 'loop_exit')) return 2
  if (e.branch !== null || e.kind === 'loop_each') return 0
  return 1
}

/**
 * 每条功能线要比较的状态：默认、根卡片的折叠逐个展开、卡片全展开、卡片全展开时每张卡片的折叠逐个展开（每张卡片
 * 最多 FOLDS_PER_CARD 个）、全部展开、根卡片里的步骤逐个钻入（每条功能线最多 DRILLS_PER_LINE 个）。
 * 两个上限只是为了真实图上的耗时（大项目约 20 秒），没试的个数打印出来。
 */
const FOLDS_PER_CARD = 8
const DRILLS_PER_LINE = 8

function branchStates(
  project: ProjectGraph,
  sid: string,
): { states: [string, ViewState][]; folds: number; drills: number; skipped: number } {
  const states: [string, ViewState][] = [['默认', emptyState()]]
  const base = buildLineView(project, sid)
  let cards = emptyState()
  for (const c of base.cards) if (c.kind === 'task' && c.expandable && !c.expanded) cards = setOpen(cards, 'cards', c.id, true)
  let folds = 0
  let skipped = 0
  // 默认状态下看得见的（根卡片的）折叠，逐个展开
  for (const f of base.folds) {
    if (!f.collapsed) continue
    folds++
    states.push([`展开 ${f.id}`, setOpen(emptyState(), 'folds', f.id, true)])
  }
  states.push(['卡片全展开', cards])
  const perCard = new Map<string, number>()
  for (const f of buildLineView(project, sid, cards).folds) {
    if (!f.collapsed) continue
    const n = perCard.get(f.card) ?? 0
    if (n >= FOLDS_PER_CARD) {
      skipped++
      continue
    }
    perCard.set(f.card, n + 1)
    folds++
    states.push([`卡片全展开 + 展开 ${f.id}`, setOpen(cards, 'folds', f.id, true)])
  }
  states.push(['全部展开', expandAllState(project, sid)])
  // 钻入也是原地展开：根卡片里能钻入的步骤逐个钻入
  let drills = 0
  for (const o of base.nodes.flatMap((n) => n.drills)) {
    if (drills >= DRILLS_PER_LINE) {
      skipped++
      continue
    }
    drills++
    states.push([`钻入 ${o.id}`, setOpen(emptyState(), 'drills', o.id, true)])
  }
  return { states, folds, drills, skipped }
}

interface BranchReport {
  /** 同一对分支在不同状态下左右不同 */
  flips: string[]
  /** 两个终点在同一行、却不合规则（不算例外） */
  broken: string[]
  /** 结构检查（checkLayout）的问题 */
  problems: string[]
  /** 统计 */
  lines: number
  states: number
  folds: number
  drills: number
  skipped: number
  pairs: number
  longPairs: number
  sameRow: number
  exceptions: string[]
}

/**
 * 一个项目每条功能线在 branchStates 的各个状态下排版，对每个判断节点、循环节点的每一对分支（终点不是同一个方框的）：
 * 1. 左右在所有看得见它们的状态里都一样（不管终点在不在同一行）；
 * 2. 终点在同一行时合规则（ruleRank）。例外：两个终点之一还有一条从源节点上方或旁边（同一行）别的节点来的入边
 *    ——它是外面另一个判断的分支的汇合点，那条连线从外层分支那一侧进来，两个判断的要求在平面上不能同时满足
 *    （比如真实项目里见过的：外层判断的"是"和里层判断的"否"落到同一个节点，里层的"是"就只能在
 *    "否"的右边），按外层的排。例外逐个打印出来；
 *    终点不在同一行的（长分支）只查第 1 条：那里左右由连线绕过别的分支的方式定，规则说不上；
 * 3. 每个状态都满足结构检查。
 */
async function branchReport(project: ProjectGraph): Promise<BranchReport> {
  const r: BranchReport = {
    flips: [],
    broken: [],
    problems: [],
    lines: 0,
    states: 0,
    folds: 0,
    drills: 0,
    skipped: 0,
    pairs: 0,
    longPairs: 0,
    sameRow: 0,
    exceptions: [],
  }
  for (const l of project.lines) {
    const sid = l.sid || `line:${l.id}`
    const { states, folds, drills, skipped } = branchStates(project, sid)
    r.lines++
    r.states += states.length
    r.folds += folds
    r.drills += drills
    r.skipped += skipped
    // 节点 id + 一对连线 → 状态 → 左右（-1：第一条在左）
    const seen = new Map<string, { src: string; pair: string; sides: Map<string, number> }>()
    const titles = new Map<string, string>()
    const long = new Set<string>()
    for (const [name, state] of states) {
      const view = buildLineView(project, sid, state)
      const p = await layoutLineView(view)
      for (const msg of checkLayout(view, p)) r.problems.push(`${sid} ${name}：${msg}`)
      for (const n of view.nodes) titles.set(n.id, n.title)
      for (const [src, sides] of branchSides(view, p)) {
        const keys = [...sides.keys()].sort()
        const graphDst = (k: string) => k.split('|')[1]
        for (let i = 0; i < keys.length; i++) {
          for (let j = i + 1; j < keys.length; j++) {
            const a = sides.get(keys[i])!
            const b = sides.get(keys[j])!
            if (a.box === b.box) continue
            const pair = `${src} ${keys[i]} / ${keys[j]}`
            let m = seen.get(pair)
            if (!m) seen.set(pair, (m = { src, pair: `${keys[i]} / ${keys[j]}`, sides: new Map() }))
            m.sides.set(name, Math.sign(a.x - b.x))
            if (!(a.near && b.near)) {
              long.add(pair)
              continue
            }
            const others = (s: Side) =>
              s.edge.keys.some((k) => [...sides.values()].some((o) => o.edge.kind !== 'raise' && o.edge.keys.some((x) => graphDst(x) === graphDst(k))))
            const ra = ruleRank(a.edge, a.edge.kind === 'raise' && others(a))
            const rb = ruleRank(b.edge, b.edge.kind === 'raise' && others(b))
            if (ra === rb) continue
            r.sameRow++
            const [left, right] = ra < rb ? [a, b] : [b, a]
            if (p.boxes[left.box].x + p.boxes[left.box].width <= p.boxes[right.box].x + EPS && left.x < right.x) continue
            // 例外：终点还有一条从源节点上方或旁边（同一行）别的节点来的入边
            const srcBottom = p.boxes[src].y + p.boxes[src].height
            const joins = (s: Side) =>
              view.edges.filter((x) => x.dst === s.box && x.src !== src && !x.back && p.boxes[x.src] && p.boxes[x.src].y < srcBottom - EPS)
            const what = `${sid} ${name}：「${titles.get(src)}」的 ${left.edge.label || left.edge.kind} 不在 ${right.edge.label || right.edge.kind} 左边`
            const via = [...joins(right), ...joins(left)].map((x) => `「${titles.get(x.src) ?? x.src}」的 ${x.label || x.kind}`)
            if (via.length) r.exceptions.push(`${what}（终点也是 ${via.join('、')} 的终点）`)
            else r.broken.push(what)
          }
        }
      }
    }
    for (const [key, m] of seen) {
      r.pairs++
      if (long.has(key)) r.longPairs++
      if (new Set(m.sides.values()).size > 1) {
        const where = [...m.sides].map(([s, v]) => `${s} ${v < 0 ? '前一条在左' : v > 0 ? '前一条在右' : '重合'}`)
        r.flips.push(`${sid}：「${titles.get(m.src)}」${m.pair}：${where.join('；')}`)
      }
    }
  }
  return r
}

describe('分支的左右', () => {
  it('账户转账：原地展开"参数不合法？"的折叠，各个判断的分支左右都不变（所有者报告的整体左右翻转）', async () => {
    const sid = transfer.lines[0].sid
    const v0 = buildLineView(transfer, sid)
    const cond = v0.nodes.find((n) => n.title === '参数不合法？')!
    const fold = v0.folds.find((f) => f.anchor === cond.id && f.collapsed)!
    expect(fold.label).toBe('是')
    const v1 = buildLineView(transfer, sid, setOpen(emptyState(), 'folds', fold.id, true))
    const [p0, p1] = [await laidOut(v0), await laidOut(v1)]
    // 每个判断：是 那条落到的第一个方框（节点、折叠占位）在 否 那条的左边还是右边
    const order = (v: LineView, p: Positioned) =>
      v.nodes
        .filter((n) => n.type === 'cond')
        .map((n) => {
          const center = (label: string) => {
            const b = p.boxes[v.edges.find((e) => e.src === n.id && e.label === label)!.dst]
            return b.x + b.width / 2
          }
          return `${n.title} 是在${center('是') < center('否') ? '左' : '右'}`
        })
    expect(order(v0, p0)).toContain('参数不合法？ 是在左')
    expect(order(v1, p1)).toEqual(order(v0, p0))
  })

  const fixtures: (readonly [string, ProjectGraph])[] = [
    ['transfer.json（夹具）', transfer],
    ['mini（夹具）', mini],
    ['shapes（夹具）：nested', nestedProject],
    ['shapes（夹具）：tricky', trickyProject],
    ['shapes（夹具）：fin', finProject],
    ['shapes（夹具）：shared', sharedProject],
    ['shapes（夹具）：whileRaise', whileRaiseProject],
    ['shapes（夹具）：retry', retryProject],
    ...REAL,
  ]
  for (const [file, project] of fixtures) {
    it(`${file}：每个判断、循环的分支左右在默认、逐个展开折叠、逐个钻入、全部展开时都一样，同一行的合规则`, async () => {
      const r = await branchReport(project)
      console.log(
        `${file}：${r.lines} 条功能线、${r.states} 个状态（逐个展开的折叠 ${r.folds} 个、逐个钻入 ${r.drills} 个，超过上限没试的 ${r.skipped} 个）；` +
          `${r.pairs} 对分支（终点不在同一行的 ${r.longPairs} 对），同一行按规则比较 ${r.sameRow} 次，例外 ${r.exceptions.length} 次` +
          (r.exceptions.length ? `\n  ${[...new Set(r.exceptions)].join('\n  ')}` : ''),
      )
      expect(r.flips).toEqual([])
      expect(r.broken).toEqual([])
      expect(r.problems).toEqual([])
    }, 300_000)
  }
})
