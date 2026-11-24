import { beforeEach, describe, expect, it, vi } from 'vitest'
import { loadFolded, REQ_FOLDS, saveFolded, toggled } from './groupFold'

function fakeStorage() {
  const data = new Map<string, string>()
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    data,
  }
}

describe('groupFold', () => {
  let store: ReturnType<typeof fakeStorage>
  beforeEach(() => {
    store = fakeStorage()
    vi.stubGlobal('window', { localStorage: store })
  })

  it('按项目记下收起的分组，读回来一样；空了就去掉这个项目', () => {
    expect([...loadFolded('p1')]).toEqual([])
    saveFolded('p1', ['api', '后台任务', 'api'])
    saveFolded('p2', ['其他入口'])
    expect([...loadFolded('p1')].sort()).toEqual(['api', '后台任务'])
    expect([...loadFolded('p2')]).toEqual(['其他入口'])
    saveFolded('p1', [])
    expect(JSON.parse(store.data.get('pystepmap.groups')!)).toEqual({ p2: ['其他入口'] })
  })

  it('给了别的键时记在那个键下，和功能线列表的分开', () => {
    saveFolded('p1', ['api'])
    saveFolded('p1', ['date:2026-01-01'], REQ_FOLDS)
    expect([...loadFolded('p1')]).toEqual(['api'])
    expect([...loadFolded('p1', REQ_FOLDS)]).toEqual(['date:2026-01-01'])
    expect(JSON.parse(store.data.get(REQ_FOLDS)!)).toEqual({ p1: ['date:2026-01-01'] })
  })

  it('存着的东西坏了、拿不到 localStorage 时当作都展开，不抛错', () => {
    store.data.set('pystepmap.groups', '{坏的')
    expect([...loadFolded('p1')]).toEqual([])
    store.data.set('pystepmap.groups', JSON.stringify({ p1: 'x', p2: ['a', 3] }))
    expect([...loadFolded('p1')]).toEqual([])
    expect([...loadFolded('p2')]).toEqual(['a'])
    vi.stubGlobal('window', undefined)
    expect([...loadFolded('p1')]).toEqual([])
    expect(() => saveFolded('p1', ['a'])).not.toThrow()
  })

  it('只留最近的 50 个项目', () => {
    for (let i = 0; i < 55; i++) saveFolded(`p${i}`, ['g'])
    const saved = JSON.parse(store.data.get('pystepmap.groups')!)
    expect(Object.keys(saved)).toHaveLength(50)
    expect(saved.p0).toBeUndefined()
    expect(saved.p54).toEqual(['g'])
  })

  it('toggled：点一下收起，再点一下展开，不改原来的集合', () => {
    const a = new Set(['api'])
    expect([...toggled(a, 'api')]).toEqual([])
    expect([...toggled(a, '后台任务')].sort()).toEqual(['api', '后台任务'])
    expect([...a]).toEqual(['api'])
  })
})
