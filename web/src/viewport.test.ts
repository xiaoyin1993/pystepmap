import { describe, expect, it } from 'vitest'
import { REVEAL_SIDE, isOnScreen, screenRect, viewportAfterChange, type Viewport } from './viewport'

const pane = { width: 1000, height: 800 }

describe('改了展开状态之后的视口', () => {
  // 一段折叠区域的入口在画布坐标 (400, 1000)，收起后占位在 (380, 1000)
  const oldBox = { x: 400, y: 1000, width: 200, height: 60 }
  const placeholder = { x: 380, y: 1000, width: 240, height: 38 }

  it('入口在屏幕上：照旧让它不动（横向按中线、纵向按上边）', () => {
    const vp: Viewport = { x: 0, y: -700, zoom: 1 } // 入口在屏幕 y = 300
    const anchor = screenRect(oldBox, vp)
    expect(isOnScreen(anchor, pane)).toBe(true)
    const r = viewportAfterChange(anchor, pane, vp, oldBox, placeholder, true)
    expect(r.mode).toBe('keep')
    // 中线 500 → 500，上边 1000 → 1000：视口不变
    expect(r.viewport).toEqual(vp)
    // 排好后元素挪了位置：视口跟着挪，屏幕上的位置不变
    const moved = { ...placeholder, x: 480, y: 1100 }
    const r2 = viewportAfterChange(anchor, pane, { ...vp, zoom: 0.5 }, oldBox, moved, true)
    const before = screenRect(oldBox, { ...vp, zoom: 0.5 })
    const after = screenRect(moved, r2.viewport)
    expect(after.y).toBeCloseTo(before.y)
    expect(after.x + after.width / 2).toBeCloseTo(before.x + before.width / 2)
  })

  it('入口滚出了屏幕（在上面）：收起后的占位拉到画布上方三分之一处、横向居中，缩放不变', () => {
    const vp: Viewport = { x: 0, y: -2000, zoom: 0.8 } // 入口在屏幕 y = -1200
    const anchor = screenRect(oldBox, vp)
    expect(isOnScreen(anchor, pane)).toBe(false)
    const r = viewportAfterChange(anchor, pane, vp, oldBox, placeholder, true)
    expect(r.mode).toBe('reveal')
    expect(r.viewport.zoom).toBe(0.8)
    const s = screenRect(placeholder, r.viewport)
    expect(s.y).toBeCloseTo(pane.height / 3)
    expect(s.x + s.width / 2).toBeCloseTo(pane.width / 2)
  })

  it('入口在屏幕下面、左右两边外面也一样拉回来', () => {
    for (const vp of [
      { x: 0, y: 200, zoom: 1 }, // 屏幕 y = 1200，在下面
      { x: -900, y: -700, zoom: 1 }, // 屏幕 x = -500，在左边
      { x: 700, y: -700, zoom: 1 }, // 屏幕 x = 1100，在右边
    ]) {
      const r = viewportAfterChange(screenRect(oldBox, vp), pane, vp, oldBox, placeholder, true)
      expect(r.mode).toBe('reveal')
      expect(isOnScreen(screenRect(placeholder, r.viewport), pane)).toBe(true)
    }
  })

  it('只露出一条边（不到 8 像素）算不在屏幕上；露出够多算在', () => {
    expect(isOnScreen({ x: 100, y: -55, width: 200, height: 60 }, pane)).toBe(false)
    expect(isOnScreen({ x: 100, y: -50, width: 200, height: 60 }, pane)).toBe(true)
    expect(isOnScreen({ x: 995, y: 100, width: 200, height: 60 }, pane)).toBe(false)
  })

  it('比画布区域宽的卡片：标题栏拉到上方三分之一处，左对齐留边', () => {
    const card = { x: 0, y: 3000, width: 1600, height: 900 }
    const vp: Viewport = { x: 0, y: 0, zoom: 1 }
    const r = viewportAfterChange(screenRect(card, vp), pane, vp, card, card, true)
    expect(r.mode).toBe('reveal')
    const s = screenRect(card, r.viewport)
    expect(s.x).toBeCloseTo(REVEAL_SIDE)
    expect(s.y).toBeCloseTo(pane.height / 3)
  })

  it('不要求拉回（展开、全部展开等）：即使在屏幕外也照旧保持', () => {
    const vp: Viewport = { x: 0, y: -2000, zoom: 1 }
    const r = viewportAfterChange(screenRect(oldBox, vp), pane, vp, oldBox, placeholder, false)
    expect(r).toEqual({ viewport: vp, mode: 'keep' })
  })
})
