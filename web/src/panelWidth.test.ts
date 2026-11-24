import { describe, expect, it } from 'vitest'
import {
  DEFAULT_WIDTHS,
  KEY_STEP,
  MIN_CANVAS,
  PANELS,
  STORAGE_KEY,
  clampBoth,
  clampWidth,
  dragged,
  keyed,
  loadWidths,
  maxWidth,
  parseWidths,
  saveWidths,
} from './panelWidth'

describe('宽度的范围', () => {
  it('最窄是以前固定的宽度；详情栏最宽 60% 窗口，列表 40%', () => {
    expect(PANELS.detail.min).toBe(320)
    expect(PANELS.list.min).toBe(264)
    expect(clampWidth('detail', 100, 2000, 264)).toBe(320)
    expect(maxWidth('detail', 2000, 264)).toBe(1200)
    expect(clampWidth('detail', 5000, 2000, 264)).toBe(1200)
    expect(maxWidth('list', 2000, 320)).toBe(800)
  })

  it('画布至少留 420 像素：窗口不宽时最宽受它限制', () => {
    // 1440 宽的窗口、列表 264：详情栏最宽 1440 - 264 - 420 = 756（比 60% 的 864 小）
    expect(maxWidth('detail', 1440, 264)).toBe(1440 - 264 - MIN_CANVAS)
    expect(clampWidth('detail', 900, 1440, 264)).toBe(756)
  })

  it('窗口窄到放不下：不小于最窄宽度', () => {
    expect(maxWidth('detail', 900, 264)).toBe(320)
    expect(clampWidth('detail', 600, 900, 264)).toBe(320)
  })

  it('两栏一起夹：加上画布不超过窗口；不是数时用默认', () => {
    const w = clampBoth({ list: 700, detail: 900 }, 1600)
    expect(w.list).toBe(640) // 40%
    expect(w.detail).toBe(1600 - 640 - MIN_CANVAS)
    expect(w.list + w.detail + MIN_CANVAS).toBeLessThanOrEqual(1600)
    expect(clampBoth({ list: Number.NaN, detail: Number.POSITIVE_INFINITY }, 1600)).toEqual(DEFAULT_WIDTHS)
  })
})

describe('拖动和方向键', () => {
  it('详情栏在右边：分界线往左拖变宽，往右拖变窄；列表反过来', () => {
    expect(dragged('detail', 320, 1000, 900)).toBe(420)
    expect(dragged('detail', 420, 1000, 1050)).toBe(370)
    expect(dragged('list', 264, 264, 364)).toBe(364)
    expect(dragged('list', 364, 364, 300)).toBe(300)
  })

  it('方向键每次 16 像素，方向和拖动一致；别的键不管', () => {
    expect(KEY_STEP).toBe(16)
    expect(keyed('detail', 320, 'ArrowLeft')).toBe(336)
    expect(keyed('detail', 336, 'ArrowRight')).toBe(320)
    expect(keyed('list', 264, 'ArrowRight')).toBe(280)
    expect(keyed('list', 280, 'ArrowLeft')).toBe(264)
    expect(keyed('detail', 320, 'Enter')).toBeNull()
  })
})

describe('记在浏览器里', () => {
  function memory() {
    const data = new Map<string, string>()
    return { data, getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) }
  }

  it('一个键存两栏的宽度，读回来一样', () => {
    const s = memory()
    saveWidths(s, { list: 300, detail: 512 })
    expect([...s.data.keys()]).toEqual([STORAGE_KEY])
    expect(loadWidths(s)).toEqual({ list: 300, detail: 512 })
  })

  it('没记过、不是 JSON、某一栏不是数：那一栏用默认', () => {
    expect(loadWidths(memory())).toEqual(DEFAULT_WIDTHS)
    expect(parseWidths('{bad')).toEqual(DEFAULT_WIDTHS)
    expect(parseWidths('[1,2]')).toEqual(DEFAULT_WIDTHS)
    expect(parseWidths('{"detail": 600, "list": "wide"}')).toEqual({ list: 264, detail: 600 })
  })

  it('存储不可用或读写抛错：不报错，读到默认', () => {
    expect(loadWidths(null)).toEqual(DEFAULT_WIDTHS)
    const broken = {
      getItem: () => {
        throw new Error('SecurityError')
      },
      setItem: () => {
        throw new Error('QuotaExceededError')
      },
    }
    expect(loadWidths(broken)).toEqual(DEFAULT_WIDTHS)
    expect(() => saveWidths(broken, DEFAULT_WIDTHS)).not.toThrow()
  })
})
