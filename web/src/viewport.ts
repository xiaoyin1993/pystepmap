// 改了展开状态之后视口怎么动（纯函数，viewport.test.ts）。画布（FlowCanvas）排好后调用它。

import type { Box } from './graph'

/** Vue Flow 的视口：画布坐标 (x, y) 显示在屏幕的 (x·zoom + vp.x, y·zoom + vp.y) */
export interface Viewport {
  x: number
  y: number
  zoom: number
}

export interface PaneSize {
  width: number
  height: number
}

/** 画布坐标的框在画布区域（屏幕）上的矩形 */
export function screenRect(b: Box, vp: Viewport): Box {
  return { x: b.x * vp.zoom + vp.x, y: b.y * vp.zoom + vp.y, width: b.width * vp.zoom, height: b.height * vp.zoom }
}

/** 至少露出这么多像素（横竖都要）才算"在屏幕上" */
export const MIN_VISIBLE = 8
/** 拉回屏幕时左右留的边 */
export const REVEAL_SIDE = 24

/** 屏幕上的矩形和画布区域有没有重叠到 MIN_VISIBLE 以上 */
export function isOnScreen(r: Box, pane: PaneSize): boolean {
  const w = Math.min(r.x + r.width, pane.width) - Math.max(r.x, 0)
  const h = Math.min(r.y + r.height, pane.height) - Math.max(r.y, 0)
  return w >= MIN_VISIBLE && h >= MIN_VISIBLE
}

export interface ViewportChange {
  viewport: Viewport
  /** keep：被操作的元素在屏幕上没动；reveal：把排好后代表它的元素拉进了屏幕 */
  mode: 'keep' | 'reveal'
}

/**
 * 改了展开状态之后的视口（缩放不变）。
 * anchorScreen：操作前被操作的元素在屏幕上的矩形；oldBox / next：它在旧排版里的框、排好后代表它的元素在新排版里的框。
 * - 它在屏幕上（或者 reveal 为 false）：照旧让它在屏幕上不动——横向按中线、纵向按上边对齐；
 * - 它不在屏幕上、reveal 为 true（在详情栏里收起一段或一张卡片，入口早就滚出屏幕）：死守那个看不见的位置会把
 *   收起后的占位（或卡片的标题栏）留在屏幕外，于是把 next 的上边放到画布区域上方三分之一处；横向放得下就居中，
 *   比画布区域宽就左对齐、留 REVEAL_SIDE 的边。
 */
export function viewportAfterChange(
  anchorScreen: Box,
  pane: PaneSize,
  vp: Viewport,
  oldBox: Box,
  next: Box,
  reveal: boolean,
): ViewportChange {
  if (!reveal || isOnScreen(anchorScreen, pane)) {
    const dx = (oldBox.x + oldBox.width / 2 - (next.x + next.width / 2)) * vp.zoom
    const dy = (oldBox.y - next.y) * vp.zoom
    return { viewport: { x: vp.x + dx, y: vp.y + dy, zoom: vp.zoom }, mode: 'keep' }
  }
  const w = next.width * vp.zoom
  const x = w <= pane.width - 2 * REVEAL_SIDE ? pane.width / 2 - (next.x + next.width / 2) * vp.zoom : REVEAL_SIDE - next.x * vp.zoom
  const y = pane.height / 3 - next.y * vp.zoom
  return { viewport: { x, y, zoom: vp.zoom }, mode: 'reveal' }
}
