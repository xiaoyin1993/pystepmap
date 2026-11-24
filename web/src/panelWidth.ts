// 左右两栏（左边的功能线列表、右边的详情栏）的宽度：拖它们和画布之间的分界线改宽窄，方向键每次 16 像素，双击
// 恢复默认；记在浏览器里（localStorage 的一个键 pystepmap.panels，存两栏的宽度），打开页面和窗口大小变化时重新夹到
// 允许的范围里。纯函数，测试见 panelWidth.test.ts；拖动的接线在 components/ResizeHandle.vue，用法在 App.vue。

export type PanelName = 'list' | 'detail'

export interface PanelSpec {
  /** 最窄（也是默认宽度：以前固定的宽度，原来放得下的东西不会挤坏） */
  min: number
  default: number
  /** 最宽占窗口的比例 */
  maxFraction: number
  /** 这一栏在画布的哪一边：右边的详情栏往左拖变宽，左边的列表往右拖变宽 */
  side: 'left' | 'right'
}

export const PANELS: Record<PanelName, PanelSpec> = {
  list: { min: 264, default: 264, maxFraction: 0.4, side: 'left' },
  detail: { min: 320, default: 320, maxFraction: 0.6, side: 'right' },
}

/** 画布那一栏至少留这么宽 */
export const MIN_CANVAS = 420
/** 方向键每次挪多少像素 */
export const KEY_STEP = 16
export const STORAGE_KEY = 'pystepmap.panels'

export type Widths = Record<PanelName, number>
export const DEFAULT_WIDTHS: Widths = { list: PANELS.list.default, detail: PANELS.detail.default }

/** 一栏最宽多少：窗口宽度的 maxFraction，并且给画布留至少 MIN_CANVAS（other 是另一栏的宽度）；窗口再窄也不小于最窄宽度 */
export function maxWidth(name: PanelName, windowWidth: number, other: number): number {
  const spec = PANELS[name]
  return Math.max(spec.min, Math.floor(Math.min(spec.maxFraction * windowWidth, windowWidth - other - MIN_CANVAS)))
}

/** 夹到 [最窄, 最宽] 里，取整；不是有限的数时用默认宽度 */
export function clampWidth(name: PanelName, width: number, windowWidth: number, other: number): number {
  if (!Number.isFinite(width)) return PANELS[name].default
  return Math.round(Math.min(Math.max(width, PANELS[name].min), maxWidth(name, windowWidth, other)))
}

/** 两栏一起夹：列表先按详情栏最窄时算，详情栏再按夹好的列表算（两栏加画布不超过窗口，除非窗口窄到放不下最窄的两栏） */
export function clampBoth(w: Widths, windowWidth: number): Widths {
  const list = clampWidth('list', w.list, windowWidth, PANELS.detail.min)
  return { list, detail: clampWidth('detail', w.detail, windowWidth, list) }
}

/** 从 startX 拖到 x 时的宽度（还没夹） */
export function dragged(name: PanelName, startWidth: number, startX: number, x: number): number {
  return PANELS[name].side === 'right' ? startWidth + (startX - x) : startWidth + (x - startX)
}

/** 分界线有焦点时按方向键：分界线往左 / 往右挪 KEY_STEP 后的宽度（还没夹）；别的键为 null */
export function keyed(name: PanelName, width: number, key: string): number | null {
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null
  const borderMovesLeft = key === 'ArrowLeft'
  const grows = PANELS[name].side === 'right' ? borderMovesLeft : !borderMovesLeft
  return width + (grows ? KEY_STEP : -KEY_STEP)
}

/** 记下的宽度：读不到、不是 JSON、某一栏不是数时，那一栏用默认宽度 */
export function parseWidths(text: string | null): Widths {
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  const o = data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as Record<string, unknown>) : {}
  const pick = (name: PanelName) => (typeof o[name] === 'number' && Number.isFinite(o[name]) ? (o[name] as number) : PANELS[name].default)
  return { list: pick('list'), detail: pick('detail') }
}

type Store = Pick<Storage, 'getItem' | 'setItem'>

/** 从浏览器里读（存储不可用、读的时候出错都当作没记过） */
export function loadWidths(storage: Store | null): Widths {
  try {
    return parseWidths(storage?.getItem(STORAGE_KEY) ?? null)
  } catch {
    return { ...DEFAULT_WIDTHS }
  }
}

/** 记到浏览器里（存储不可用、写满了就算了，这次打开的页面照样用） */
export function saveWidths(storage: Store | null, w: Widths): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify({ list: w.list, detail: w.detail }))
  } catch {
    // 隐私模式、存储被禁用：不记
  }
}

/** window.localStorage；拿不到（被禁用、不在浏览器里）时为 null */
export function browserStorage(): Store | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}
