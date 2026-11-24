// Vue Flow 节点 / 连线的 data：画布（FlowCanvas）从视图和排版结果拼出来，节点组件只负责画。
import type { CardView, EdgeView, FoldView, LinkView, NodeView, PositionedEdge, Resolution } from '../../graph'

/** 开始、步骤、判断、循环、捕获、结局 */
export interface LeafData {
  node: NodeView
  width: number
  height: number
  /** 标题行数（nodeSize 估算的；DOM 按这个行数截断，保证不溢出） */
  titleLines: number
  /** 这个节点是某个已展开的折叠区域的入口：节点角上放"收起"按钮 */
  foldEntry: FoldView | null
}

export interface FoldData {
  fold: FoldView
  width: number
  height: number
}

export interface CardData {
  card: CardView
  width: number
  height: number
}

export interface EdgeData {
  pos: PositionedEdge
  /** 函数内的连线 */
  edge: EdgeView | null
  /** 卡片之间的派发连线 */
  link: LinkView | null
  /** 派发连线的解析等级（推测的用另一种颜色） */
  resolution: Resolution | null
  /** 分支标签的配色 */
  tone: 'yes' | 'no' | 'case' | 'else' | 'loop' | null
}
