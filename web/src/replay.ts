// 回放（"运行路径"页签，运行条上的"回放"）：选中的测试在画布上一个节点一个节点地亮起来。纯函数，测试见 replay.test.ts；
// 动画（每步多久、暂停、停止）在 runstate.ts。
//
// 只用记下来的事实：每个函数播放的是它在这个测试里**第一次执行**时依次经过的节点（运行路径 JSON 的 first_path，见
// core/src/pystepmap/runpath.py；最多 500 个，超出时只记了前 499 个）。函数之间的先后不是记下来的，按画布排：
//   - 先播放根任务卡片；走到一个钻入了（画布上展开成卡片）的调用时，先在那张钻入卡片里播放被调函数，再接着往下；
//     一个步骤同时钻入了它调用的几个函数（钻入链）时，按链上的顺序一张一张播放（这个测试执行过的那几张；源码里出现的
//     先后，不一定是执行的先后）；
//   - 派发的任务卡片按任务树的顺序接在后面；
//   - 只播放画布上看得见的函数：没钻入的调用、不在这条线上的函数不播放；收起的卡片整张亮一下，折叠着的分支在占位上亮；
//   - 节点在运行之后改过、对不上的（runplace 配不上）跳过，前后两步之间也不亮连线。

import type { RunPathsJson } from './api'
import { drillChainOf, type CardView, type LineView, type ProjectGraph } from './graph'
import { locateTestRun } from './runplace'
import { emptyRunMarks, type RunMarks } from './runview'

/** 运行路径 JSON 里 first_path 被截断时的最后一项 */
const MORE = '…'

export interface ReplayStep {
  /** 在哪张卡片里（任务卡片或钻入卡片；收起的卡片是它自己） */
  card: string
  /** 这一步亮的画布元素：节点、收起它的折叠占位、收起的卡片、代替它的钻入卡片 */
  element: string
  /** 走进这一步经过的图里的连线（这张卡片的函数里的"起点 id|终点 id"）；连续几步落在同一个元素上时合成一步 */
  pairs: string[]
}

export interface ReplayPlan {
  steps: ReplayStep[]
  /** 有函数的 first_path 被截断了（只记了前 499 个节点） */
  truncated: boolean
}

/**
 * 回放的每一步（见模块说明）。view 是画布上这条线现在的样子（展开哪些卡片、分支）；paths 是这个测试的运行路径（整份或
 * ?test= 的切片）。测试不在里面、画布上的函数都没执行过时没有步骤。
 */
export function replayPlan(view: LineView, graph: ProjectGraph, paths: RunPathsJson, testId: string): ReplayPlan {
  const located = locateTestRun(graph, paths, testId)
  const steps: ReplayStep[] = []
  const out: ReplayPlan = { steps, truncated: false }
  if (!located.test) return out
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  const played = new Set<string>()

  const push = (card: string, element: string, pair: string | null) => {
    const last = steps[steps.length - 1]
    if (last && last.card === card && last.element === element) {
      if (pair) last.pairs.push(pair)
      return
    }
    steps.push({ card, element, pairs: pair ? [pair] : [] })
  }

  const play = (c: CardView) => {
    const p = c.fn ? located.placed.get(c.fn) : undefined
    if (!p || played.has(c.id)) return
    played.add(c.id)
    if (!c.expanded) {
      push(c.id, c.id, null)
      return
    }
    const sidOf = new Map(p.fn.nodes.map((n) => [n.id, n.sid || n.id]))
    let prev: string | null = null
    for (const runSid of p.run.first_path) {
      if (runSid === MORE) {
        out.truncated = true
        break
      }
      const id = p.mapping.get(runSid)
      if (id === undefined) {
        prev = null // 对不上的节点：跳过，它前后两步之间不亮连线
        continue
      }
      const pair = prev !== null ? `${prev}|${id}` : null
      prev = id
      const vid = `${c.id}/${sidOf.get(id)}`
      if (nodeIds.has(vid)) {
        push(c.id, vid, pair)
        continue
      }
      const fold = view.folds.find((f) => f.card === c.id && f.collapsed && f.keys.includes(id))
      if (fold) {
        push(c.id, fold.id, pair)
        continue
      }
      const chain = drillChainOf(view, vid)
      if (chain.length) {
        // 被调函数在调用它的这一步插进去播放（每张只播放一次）：钻入链上这个测试执行过的几张，按链上的顺序；一张都
        // 没执行过时亮第一张（代表这一步）
        const ran = chain.filter((d) => d.fn !== null && located.placed.has(d.fn))
        const shown = ran.length ? ran : chain.slice(0, 1)
        shown.forEach((d, i) => {
          push(c.id, d.id, i === 0 ? pair : null)
          play(d)
        })
      }
    }
  }

  const root = view.cards[0]
  if (root) play(root)
  for (const c of view.cards) if (c.kind === 'task' && !c.goto) play(c)
  return out
}

/**
 * 回放到第 index 步（从 0 起）时画布上的样子。final 是这个测试的完整标注（runview 的 runMarks）：连线、派发连线只亮
 * 它里面也亮的（并行的 raise 连线等的取舍照它）。亮的节点不带次数、耗时（那是整个测试的汇总，回放完再看）；还没走到的
 * 变暗；没有停点的红圈、没走到的结局。current 是这一步的元素。
 */
export function replayMarks(final: RunMarks, view: LineView, plan: ReplayPlan, index: number): RunMarks {
  const out = emptyRunMarks()
  out.active = true
  const upto = plan.steps.slice(0, Math.max(0, Math.min(index, plan.steps.length - 1)) + 1)
  if (!plan.steps.length) return out
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  const cards = new Map(view.cards.map((c) => [c.id, c]))
  const folds = new Set(view.folds.filter((f) => f.collapsed).map((f) => f.id))
  const walked = new Set<string>()
  for (const s of upto) {
    out.cardsRun.add(s.card)
    for (const p of s.pairs) walked.add(`${s.card}\u0000${p}`)
    if (nodeIds.has(s.element)) out.nodes.set(s.element, { visits: 0, ns: 0, share: 0, badge: '' })
    else if (folds.has(s.element)) out.inside.add(s.element)
    else if (cards.has(s.element)) {
      out.cardsRun.add(s.element)
      if (!cards.get(s.element)!.expanded) out.inside.add(s.element)
    }
  }
  const pairOf = (k: string) => k.split('|').slice(0, 2).join('|')
  for (const e of view.edges) {
    const n = final.edges.get(e.id)
    if (n && e.keys.some((k) => walked.has(`${e.card}\u0000${pairOf(k)}`))) out.edges.set(e.id, 1)
  }
  for (const l of view.links) if (final.links.has(l.id) && out.cardsRun.has(l.dst)) out.links.add(l.id)
  out.current = upto[upto.length - 1].element
  return out
}

/** 回放按钮的说明（悬停时）：说清播放的是什么（记下来的事实）、什么不是 */
export const REPLAY_TIP =
  '按记录一步步点亮这个测试走过的节点：每个函数播放的是它在这个测试里第一次执行时依次经过的节点。钻入展开的被调函数在调用它的那一步插进去播放，派发的任务按任务树的顺序接在后面——函数之间的先后是按画布排的，不是记录下来的时间。同一条语句里调用的几个函数都钻入了时，按源码里出现的先后一个个播放，不一定是执行的先后。只播放画布上看得见的函数（没钻入的调用不播放，收起的卡片、折叠的分支整体亮一下）。'

/** 系统设置了"减少动态效果"时回放按钮的说明 */
export const REPLAY_REDUCED_TIP = '系统设置了"减少动态效果"：不播放回放，画布上就是这个测试走完后的样子'
