import { describe, expect, it } from 'vitest'
import { C, DIFF_COLOR, END_COLOR, END_NAME, foldTint, mostSevereEnd } from './theme'

describe('地图模式里折叠占位的颜色', () => {
  it('几种结局时取最严重的：未捕获异常 > 失败 > 跳过 > 正常', () => {
    expect(mostSevereEnd(['ok', 'failed'])).toBe('failed')
    expect(mostSevereEnd(['ok', 'skipped'])).toBe('skipped')
    expect(mostSevereEnd(['ok', 'skipped', 'failed', 'exception'])).toBe('exception')
    expect(mostSevereEnd(['failed', 'exception'])).toBe('exception')
    expect(mostSevereEnd(['ok'])).toBe('ok')
    expect(mostSevereEnd([])).toBeNull()
  })

  it('和顺序无关（以前只看第一种，正常 + 失败的折叠被涂成正常绿）', () => {
    expect(foldTint(['ok', 'failed'])).toBe(END_COLOR.failed)
    expect(foldTint(['failed', 'ok'])).toBe(END_COLOR.failed)
    expect(foldTint([])).toBe(C.faint)
  })
})

/** WCAG 的相对亮度对比 */
function contrast(a: string, b: string): number {
  const lum = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

describe('颜色', () => {
  it('捕获（玫红）和失败结局（红）分得开，在底色上看得见', () => {
    expect(contrast(C.catch, C.failed)).toBeGreaterThan(1.5)
    expect(contrast(C.catch, C.canvas)).toBeGreaterThan(3)
  })

  it('结局名各处一套：未捕获异常', () => {
    expect(END_NAME.exception).toBe('未捕获异常')
  })
})

describe('流程变化、过期提示的颜色', () => {
  it('用现有的语义色，不新造色值：新增是成功绿，改动是警告琥珀', () => {
    expect(DIFF_COLOR).toEqual({ added: C.ok, changed: C.decision })
  })
})
