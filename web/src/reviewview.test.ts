import { describe, expect, it } from 'vitest'
import { ApiError, type ReviewInfo } from './api'
import { mini, miniLine } from './graph/fixtures/mini'
import { applyOverrides } from './overrides'
import {
  REVIEW_REF,
  headKept,
  parseReviewRef,
  reviewBannerText,
  reviewBaseline,
  reviewFailText,
  reviewHash,
  reviewLine,
  reviewNeighbors,
} from './reviewview'

/** 和 state.ts 的 readHash 一样：按 / 分段，各段解码，项目后面的段再用 / 拼起来 */
function readTail(hash: string): { project: string; tail: string } {
  const parts = hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent)
  return { project: parts[0], tail: parts.slice(1).join('/') }
}

const rec = (id: number, head: number, base: number | null, has_graph: boolean | null = true): ReviewInfo => ({
  id,
  acked_at: `2026-11-1${id}T08:00:00+00:00`,
  head_id: head,
  head_at: `2026-11-1${id}T07:00:00+00:00`,
  base_id: base,
  base_at: base === null ? null : `2026-11-1${id - 1}T07:00:00+00:00`,
  summary: { lines: [], functions: 0, added: 0, removed: 0, changed: 0 },
  has_graph,
})

const when = (iso: string) => `「${iso.slice(5, 10)}」`

describe('地址栏里的变更记录', () => {
  it('review:<编号>/<功能线>：编号、功能线读出来；只有编号时功能线为空；不是记录时 null', () => {
    expect(parseReviewRef('review:3/line:api.py::entry')).toEqual({ id: 3, line: 'line:api.py::entry' })
    expect(parseReviewRef('review:12')).toEqual({ id: 12, line: '' })
    expect(parseReviewRef('line:api.py::entry')).toBeNull()
    expect(parseReviewRef('req:R001')).toBeNull()
    expect(parseReviewRef('')).toBeNull()
  })

  it('编号不是正整数：id 为 null（界面说一声，回到现在）', () => {
    expect(parseReviewRef('review:abc/x')).toEqual({ id: null, line: 'x' })
    expect(parseReviewRef('review:0')).toMatchObject({ id: null })
    expect(parseReviewRef('review:')).toMatchObject({ id: null })
    expect(parseReviewRef('review:-2')).toMatchObject({ id: null })
  })

  it('写出来再按地址栏的读法读回来：项目、编号、功能线 sid（里面有 /、:、#）都还原', () => {
    const sid = 'line:app/orders.py::Order#run'
    const h = reviewHash('p 1/x', 7, sid)
    expect(h.startsWith(`#/${encodeURIComponent('p 1/x')}/${REVIEW_REF}7/`)).toBe(true)
    const { project, tail } = readTail(h)
    expect(project).toBe('p 1/x')
    expect(parseReviewRef(tail)).toEqual({ id: 7, line: sid })
    expect(reviewHash('p', 7, '')).toBe('#/p/review:7')
  })
})

describe('打开一条记录时看哪条功能线', () => {
  const lines = applyOverrides(mini.lines, {})
  const [a, b, c] = lines.map((d) => d.sid)

  it('地址栏指定的（图里有）优先；图里没有时不算', () => {
    expect(reviewLine(lines, new Map([[b, 'changed']]), c, a)).toBe(c)
    expect(reviewLine(lines, new Map([[b, 'changed']]), 'line:gone', a)).toBe(b)
  })

  it('正在看的线这次有变化：留在它上面；没有变化：换到第一条有变化的', () => {
    expect(reviewLine(lines, new Map([[b, 'changed'], [c, 'added']]), '', c)).toBe(c)
    expect(reviewLine(lines, new Map([[b, 'changed'], [c, 'added']]), '', a)).toBe(b)
  })

  it('第一条有变化的按列表顺序（手工改过的分组排在前面的组先）', () => {
    // c 改到一个新的组：按分组第一次出现的顺序，"test"组（a、b）在前
    const regrouped = applyOverrides(mini.lines, { [c]: { group: '后来的组' } })
    expect(reviewLine(regrouped, new Map([[c, 'changed'], [b, 'changed']]), '', '')).toBe(b)
  })

  it('这次哪条线都没有变化（只删了功能线）：正在看的线（图里有时），再不然第一条', () => {
    expect(reviewLine(lines, new Map(), '', c)).toBe(c)
    expect(reviewLine(lines, new Map(), '', 'line:gone')).toBe(miniLine.sid)
    expect(reviewLine([], new Map(), '', a)).toBe('')
  })
})

describe('上一次 / 下一次', () => {
  const list = [rec(5, 50, 40), rec(4, 40, 30, false), rec(3, 30, 20), rec(2, 20, null)] // 新的在前

  it('按编号找更早、更晚的，跳过没存图的；第几条按时间从早到晚数', () => {
    expect(reviewNeighbors(list, 3)).toMatchObject({ earlier: { id: 2 }, later: { id: 5 }, index: 2, total: 4 })
    expect(reviewNeighbors(list, 5)).toMatchObject({ earlier: { id: 3 }, later: null, index: 4 })
    expect(reviewNeighbors(list, 2)).toMatchObject({ earlier: null, later: { id: 3 }, index: 1 })
  })

  it('看着的那条已经不在列表里（被新的挤掉了）：前后照样找得到，第几条为 0', () => {
    expect(reviewNeighbors(list.slice(0, 3), 2)).toMatchObject({ earlier: null, later: { id: 3 }, index: 0, total: 3 })
  })

  it('旧版后端不说存没存图：不往那里走', () => {
    expect(reviewNeighbors([rec(2, 20, 10, null), rec(1, 10, null, null)], 2)).toMatchObject({ earlier: null, later: null })
  })
})

describe('横幅、差异栏的话', () => {
  it('横幅：哪次标为已看、分析 #head 相对 #base，提醒这是当时的图', () => {
    expect(reviewBannerText(rec(3, 30, 20), when)).toBe(
      '正在看变更记录：「11-13」 标为已看的那次变化（分析 #30 相对 #20）。这是当时的图，不是现在的代码',
    )
    expect(reviewBannerText(rec(2, 20, null), when)).toContain('（分析 #20 相对 更早的一次）')
  })

  it('差异栏的"相对什么"：基准是更早一条记录标为已看的那次时说"上次标为已看时"，别的说"对比基准"', () => {
    const list = [rec(3, 30, 20), rec(2, 20, 10)]
    expect(reviewBaseline(list[0], list, when)).toEqual({ kind: 'acknowledged', when: '「11-12」' })
    // 最早的一条：更早的记录不在了（或者没有过），不知道基准是不是标为已看的那次
    expect(reviewBaseline(list[1], list, when)).toEqual({ kind: 'base', when: '「11-11」' })
    expect(reviewBaseline(rec(1, 10, null), list, when)).toBeNull()
  })
})

describe('打开失败时说的话', () => {
  const http = (status: number, error: string, code: string | null = null) => new ApiError('http', error, { status, code, data: { error } })

  it('记录已经清掉了、没存图、旧版后端、旧版引擎的图、别的错', () => {
    expect(reviewFailText(3, http(404, '没有这条变更记录：3（只留最近 10 条，可能已经清掉了）'), false)).toBe(
      '变更记录 #3 已经不在了（每个项目只留最近 10 条），显示的是现在的图',
    )
    expect(reviewFailText(3, http(404, '这条变更记录没有存当时的图', 'no_graph'), true)).toBe(
      '变更记录 #3 没有存当时的图（旧版本存的记录），只能在"变更记录"里看文字，画布上还是原来那条',
    )
    expect(reviewFailText(3, http(404, '没有这个接口：GET /x'), false)).toContain('平台后端不支持在图上看变更记录')
    expect(reviewFailText(3, new ApiError('old-graph', '旧版引擎'), false)).toContain('旧版引擎生成的')
    expect(reviewFailText(3, new ApiError('unreachable', '连不上平台后端'), false)).toBe('没有打开变更记录 #3：连不上平台后端，显示的是现在的图')
  })
})

describe('记录那次分析还在不在后端', () => {
  it('是页面知道的最新一次或它的对比基准才算确定还在', () => {
    expect(headKept(30, [31, 30, null])).toBe(true)
    expect(headKept(30, [31, null, undefined])).toBe(false)
    expect(headKept(30, [])).toBe(false)
  })
})
