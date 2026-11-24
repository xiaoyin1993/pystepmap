// 鼠标悬停显示被截断的完整文字：显示什么、什么时候显示（纯函数，fulltext.test.ts）。界面是 FullTextTip.vue。
//
// 字被截断有两种：
// - 引擎截断：生成图时标题、副标题超过 60 个字（判断条件、case 模式超过 80）就截成"前面一段…"，画在画布上的就是这个
//   显示形式；完整的在视图模型的 titleFull / subtitleFull / labelFull 里（和显示形式不同就是被截断了）。显示形式在框里
//   放得下也要显示全文。
// - 界面截断：显示形式在框里放不下。先按排版的估算（graph/layout.ts 的 nodeClipped / cardClipped，和节点尺寸同一套
//   数字），再加上悬停时量到的 DOM（估算用的是全宽字符表，西文按单词换行时 DOM 可能比估算多截一点）。
// 浮层里总是写完整的文字。地图模式（缩到 50% 以下）节点只剩色块、字都看不见：总是显示。

import { cardClipped, cardSubtitle, nodeClipped, type CardView, type EdgeView, type NodeView } from './graph'

export interface FullText {
  /** 第一行：完整的标题 */
  title: string
  /** 第二行：函数名 / 条件原文 / 接口路径（等宽）；不用显示时为 null */
  sub: string | null
}

/** 悬停时量到的 DOM：标题、第二行有没有溢出（没量时为 undefined） */
export interface DomClip {
  title?: boolean
  sub?: boolean
}

/** 引擎截断过：完整形式和显示形式不同 */
export const engineCut = (shown: string, full: string): boolean => full !== shown

/**
 * 步骤、判断、循环、捕获、结局、开始节点。第一行是完整标题；第二行是完整副标题（步骤是调用的函数名，判断、循环是
 * 条件原文），它也被截断（或在地图模式）时才显示。开始节点写的是"开始"，只在地图模式里显示函数标题。
 */
export function nodeFullText(n: NodeView, width: number, map: boolean, dom: DomClip = {}): FullText | null {
  if (n.type === 'start') return map ? { title: n.titleFull, sub: null } : null
  const est = nodeClipped(n, width)
  const sub = n.subtitle !== '' && (map || est.sub || !!dom.sub || engineCut(n.subtitle, n.subtitleFull))
  if (!(map || est.title || dom.title || engineCut(n.title, n.titleFull) || sub)) return null
  return { title: n.titleFull, sub: sub ? n.subtitleFull : null }
}

/** 任务卡片、钻入卡片、goto 卡片的标题栏：完整标题一行，第二行函数名（接口加方法和路径）。 */
export function cardFullText(c: CardView, width: number, map: boolean, dom: DomClip = {}): FullText | null {
  const est = cardClipped(c, width)
  const sub = map || est.sub || !!dom.sub
  if (!(map || est.title || dom.title || engineCut(c.title, c.titleFull) || sub)) return null
  return { title: c.titleFull, sub: sub ? cardSubtitle(c) : null }
}

/** 地图模式里卡片的标题标签：标签上是短标题（或撞车时的函数名），和完整标题不一样、或被截断时显示全文和函数名。 */
export function mapLabelFullText(c: CardView, shown: string, domClipped: boolean): FullText | null {
  const plain = shown.replace(/^↩\s*/, '')
  if (plain === c.titleFull && !domClipped) return null
  return { title: c.titleFull, sub: cardSubtitle(c) }
}

/** 连线上的分支标签：case 模式被引擎截断时显示完整的模式（标签本身总是画全的） */
export function edgeFullText(e: EdgeView): FullText | null {
  return e.label && engineCut(e.label, e.labelFull) ? { title: 'match 分支的模式', sub: e.labelFull } : null
}

/**
 * 界面别处（左侧列表、标题区）：DOM 量到被截断时显示。cut：那里显示的是引擎截断过的形式（title 是完整的），放得下
 * 也显示。
 */
export function textFullText(title: string, sub: string | null, domClipped: boolean, cut = false): FullText | null {
  return (domClipped || cut) && title ? { title, sub: sub || null } : null
}
