// 功能线列表里收起了哪些分组：按项目记在浏览器里（localStorage 的一个键 pystepmap.groups：{项目 id: [分组名]}）。
// 需求列表的记在另一个键（REQ_FOLDS）下，写法一样。拿不到 localStorage（被禁用、不在浏览器里）时只在这次打开期间有效。

const KEY = 'pystepmap.groups'
/** 需求列表的分组（requirements.ts 的 reqTree）：记的是和默认不一样的那些（归档日期的组默认收起，别的默认展开） */
export const REQ_FOLDS = 'pystepmap.reqgroups'
const MAX_PROJECTS = 50 // 最多记这么多个项目，多了丢掉最早的

type Saved = Record<string, string[]>

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

function readAll(key: string): Saved {
  try {
    const v: unknown = JSON.parse(storage()?.getItem(key) ?? '{}')
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return {}
    const out: Saved = {}
    for (const [k, names] of Object.entries(v)) {
      if (Array.isArray(names)) out[k] = names.filter((n): n is string => typeof n === 'string')
    }
    return out
  } catch {
    return {}
  }
}

/** 这个项目里收起的分组名 */
export function loadFolded(projectId: string, key = KEY): Set<string> {
  return new Set(readAll(key)[projectId] ?? [])
}

/** 记下这个项目里收起的分组名（空 = 都展开，这个项目的记录去掉） */
export function saveFolded(projectId: string, folded: Iterable<string>, key = KEY): void {
  const all = readAll(key)
  delete all[projectId]
  const names = [...new Set(folded)].sort()
  if (names.length) all[projectId] = names
  const ids = Object.keys(all)
  for (const id of ids.slice(0, Math.max(0, ids.length - MAX_PROJECTS))) delete all[id]
  try {
    storage()?.setItem(key, JSON.stringify(all))
  } catch {
    // 写不进去（配额、隐私模式）：这次打开期间照常用
  }
}

/** 点一下分组标题之后收起的集合：原来收起的展开，原来展开的收起 */
export function toggled(folded: ReadonlySet<string>, name: string): Set<string> {
  const next = new Set(folded)
  if (!next.delete(name)) next.add(name)
  return next
}
