import { describe, expect, it } from 'vitest'
import type { ProjectStatus, StaleCheck } from './api'
import { hasNewerAnalysis, staleView, STALE_FILES_SHOWN } from './stale'

const checked = (over: Partial<Extract<StaleCheck, { checked: true }>> = {}): StaleCheck => {
  const changed = over.changed ?? []
  const added = over.added ?? []
  const removed = over.removed ?? []
  const unreadable = over.unreadable ?? []
  return {
    checked: true,
    stale: changed.length + added.length + removed.length > 0,
    changed,
    added,
    removed,
    unreadable,
    lines: [],
    total: { changed: changed.length, added: added.length, removed: removed.length, unreadable: unreadable.length },
    note: '',
    ...over,
  }
}

const status = (stale: StaleCheck, analysis_id: number | null = 3): ProjectStatus => ({
  analysis_id,
  analyzing: false,
  stale,
  base_id: null,
  has_diff: false,
})

describe('staleView：过期条', () => {
  it('没有状态、没过期：不显示', () => {
    expect(staleView(null)).toMatchObject({ show: false, kind: null, files: [], lineSids: null, more: 0 })
    expect(staleView(status(checked()))).toMatchObject({ show: false, kind: null })
  })

  it('改 2 增 1：文字、文件列表（改的在前，再增、再删），功能线 sid', () => {
    const v = staleView(
      status(checked({ changed: ['a.py', 'b.py'], added: ['new.py'], lines: ['line:a.py::f', 'line:b.py::g'] })),
    )
    expect(v.show).toBe(true)
    expect(v.kind).toBe('stale')
    expect(v.text).toBe('代码已变更，图可能过期：改 2、增 1 个文件')
    expect(v.files).toEqual([
      { kind: '改', file: 'a.py' },
      { kind: '改', file: 'b.py' },
      { kind: '增', file: 'new.py' },
    ])
    expect(v.lineSids).toEqual(new Set(['line:a.py::f', 'line:b.py::g']))
    expect(v.more).toBe(0)
  })

  it('删掉的文件也写上；三种都有时都列', () => {
    const v = staleView(status(checked({ changed: ['a.py'], added: ['b.py'], removed: ['c.py', 'd.py'] })))
    expect(v.text).toBe('代码已变更，图可能过期：改 1、增 1、删 2 个文件')
    expect(v.files.map((f) => f.kind)).toEqual(['改', '增', '删', '删'])
  })

  it(`25 个文件：列 ${STALE_FILES_SHOWN} 个，其余记在 more 里`, () => {
    const files = Array.from({ length: 25 }, (_, i) => `f${String(i).padStart(2, '0')}.py`)
    const v = staleView(status(checked({ changed: files })))
    expect(STALE_FILES_SHOWN).toBe(20)
    expect(v.files).toHaveLength(20)
    expect(v.files[0]).toEqual({ kind: '改', file: 'f00.py' })
    expect(v.more).toBe(5)
    expect(v.text).toBe('代码已变更，图可能过期：改 25 个文件')
  })

  it('后端只列了前 200 个：个数按 total，more 也按 total 算', () => {
    const files = Array.from({ length: 200 }, (_, i) => `f${i}.py`)
    const v = staleView(status(checked({ changed: files, total: { changed: 230, added: 0, removed: 0, unreadable: 0 } })))
    expect(v.text).toBe('代码已变更，图可能过期：改 230 个文件')
    expect(v.files).toHaveLength(20)
    expect(v.more).toBe(210)
  })

  it('读不了的文件不算过期；过期时后端的说明（note）放进文件列表那里', () => {
    const quiet = staleView(status(checked({ stale: false, unreadable: ['locked.py'], note: '1 个文件读不了，没法比较' })))
    expect(quiet.show).toBe(false)
    const v = staleView(status(checked({ changed: ['a.py'], unreadable: ['locked.py'], note: '1 个文件读不了，没法比较' })))
    expect(v.text).toBe('代码已变更，图可能过期：改 1 个文件')
    expect(v.files).toEqual([{ kind: '改', file: 'a.py' }])
    expect(v.note).toBe('1 个文件读不了，没法比较')
    expect(staleView(status(checked({ changed: ['a.py'] }))).note).toBe('')
  })

  it('lines 为 null（旧版本存的分析没有这项信息）：lineSids 为 null', () => {
    const v = staleView(status(checked({ changed: ['a.py'], lines: null })))
    expect(v.show).toBe(true)
    expect(v.lineSids).toBeNull()
  })

  it('checked 为假：灰色的"没法检查"，写原因', () => {
    const v = staleView(status({ checked: false, reason: '项目目录不存在了：/code/x' }))
    expect(v).toMatchObject({ show: true, kind: 'unchecked', files: [], lineSids: null, more: 0 })
    expect(v.text).toBe('没法检查代码有没有变：项目目录不存在了：/code/x')
  })
})

describe('hasNewerAnalysis：别的进程做了新的分析', () => {
  it('相同 → 假；更大 → 真；更小 → 假', () => {
    expect(hasNewerAnalysis(status(checked(), 3), 3)).toBe(false)
    expect(hasNewerAnalysis(status(checked(), 4), 3)).toBe(true)
    expect(hasNewerAnalysis(status(checked(), 2), 3)).toBe(false)
  })

  it('还没载入、没有状态、后端说没有分析：假', () => {
    expect(hasNewerAnalysis(status(checked(), 4), null)).toBe(false)
    expect(hasNewerAnalysis(null, 3)).toBe(false)
    expect(hasNewerAnalysis(status(checked(), null), 3)).toBe(false)
  })
})
