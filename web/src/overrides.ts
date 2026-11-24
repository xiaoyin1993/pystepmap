// 手工改的功能线标题和分组：按功能线的 sid 存在后端（GET /overrides），图本身保持引擎的输出原样。
// 界面上凡是显示功能线标题、分组的地方（左侧列表和它的分组、标题区、搜索、悬停浮层、"复制给 AI"）都从这里的
// applyOverrides 取，原值留着给编辑框写"原标题：…"和"恢复原样"。纯函数，测试见 overrides.test.ts。
//
// 对应不上任何功能线的手工项（功能线在重新分析后没了）直接忽略：不显示、不报错，也不删——它留在库里，功能线回来时
// 仍然生效。

import type { LineOverride, OverrideMap } from './api'
import type { FlowLine } from './graph'

/** 没有分组的功能线放在这一组 */
export const NO_GROUP = '其他'

export interface DisplayLine {
  line: FlowLine
  /** 功能线的 sid（旧图没有 sid 时是 "line:" 加 id） */
  sid: string
  /** 显示用的标题：手工标题，没有时是图里的（引擎截断过的显示形式） */
  title: string
  /** 完整标题：手工标题，没有时是图里的完整标题（title_full，没被截断时同 title） */
  titleFull: string
  /** 显示用的分组：手工分组，没有时是图里的分组（空的归到"其他"） */
  group: string
  /** 图里的原值，给"原标题：…"和"恢复原样" */
  original: { title: string; titleFull: string; group: string }
  titleEdited: boolean
  groupEdited: boolean
}

export const lineSidOf = (l: FlowLine): string => l.sid || `line:${l.id}`

/** 手工项里有效的值：非空字符串（去掉首尾空白）才算改了 */
const value = (v: string | undefined): string | null => {
  const t = typeof v === 'string' ? v.trim() : ''
  return t || null
}

/** 把手工项应用到功能线列表上：顺序同图里的 lines，孤儿项忽略。 */
export function applyOverrides(lines: readonly FlowLine[], overrides: OverrideMap): DisplayLine[] {
  return lines.map((line) => {
    const sid = lineSidOf(line)
    const o = Object.hasOwn(overrides, sid) ? overrides[sid] : undefined
    const title = value(o?.title)
    const group = value(o?.group)
    const original = { title: line.title, titleFull: line.title_full || line.title, group: line.group || NO_GROUP }
    return {
      line,
      sid,
      title: title ?? original.title,
      titleFull: title ?? original.titleFull,
      group: group ?? original.group,
      original,
      titleEdited: title !== null,
      groupEdited: group !== null,
    }
  })
}

export interface LineGroup<T> {
  name: string
  items: T[]
}

/** 按（应用手工项之后的）分组归类：组的顺序是组里第一条功能线在列表里出现的顺序，组内保持原顺序。 */
export function groupLines<T extends { group: string }>(items: readonly T[]): LineGroup<T>[] {
  const out: LineGroup<T>[] = []
  const byName = new Map<string, LineGroup<T>>()
  for (const it of items) {
    let g = byName.get(it.group)
    if (!g) {
      g = { name: it.group, items: [] }
      byName.set(it.group, g)
      out.push(g)
    }
    g.items.push(it)
  }
  return out
}

/** 列表上第一条功能线（按分组排好之后的第一组第一条）；没有功能线时为 null */
export function firstLine(lines: readonly DisplayLine[]): DisplayLine | null {
  return groupLines(lines)[0]?.items[0] ?? null
}

/** 已有的分组名（编辑框里给建议），按列表里出现的顺序 */
export function groupNames(lines: readonly DisplayLine[]): string[] {
  return groupLines(lines).map((g) => g.name)
}

/**
 * 编辑框里填的值 → 要存的手工项（PUT 的请求体，整条改写）。和原值相同、或者空着的项存成 null（= 不改）；
 * 两项都是 null 就是删掉这条手工项。
 */
export function overrideFromInput(d: DisplayLine, title: string, group: string): { title: string | null; group: string | null } {
  const t = title.trim()
  const g = group.trim()
  return {
    title: t && t !== d.original.titleFull && t !== d.original.title ? t : null,
    group: g && g !== d.original.group ? g : null,
  }
}

/**
 * 保存之后的提示：这次只是把改过的项恢复成原值（把标题框清空或只留空格、改回原来的分组）而没有设新值时说
 * "已恢复原样"，否则"已保存"。next 是 overrideFromInput 的结果。
 */
export function saveNotice(d: DisplayLine, next: { title: string | null; group: string | null }): '已保存' | '已恢复原样' {
  const cur = { title: d.titleEdited ? d.titleFull : null, group: d.groupEdited ? d.group : null }
  const set = (next.title !== null && next.title !== cur.title) || (next.group !== null && next.group !== cur.group)
  const restored = (cur.title !== null && next.title === null) || (cur.group !== null && next.group === null)
  return restored && !set ? '已恢复原样' : '已保存'
}

/** 只恢复一项（标题或分组），另一项保持现在的手工值 */
export function restoreOne(d: DisplayLine, what: 'title' | 'group'): { title: string | null; group: string | null } {
  return {
    title: what === 'title' || !d.titleEdited ? null : d.titleFull,
    group: what === 'group' || !d.groupEdited ? null : d.group,
  }
}

/** 把 PUT 的结果里这一条的值合进本地的手工项（只动这一条，别的条目以本地为准；顺序不同的两次保存互不覆盖） */
export function patchOverride(local: OverrideMap, sid: string, server: OverrideMap): OverrideMap {
  const next: OverrideMap = { ...local }
  const entry: LineOverride | undefined = Object.hasOwn(server, sid) ? server[sid] : undefined
  if (entry && (entry.title || entry.group)) next[sid] = entry
  else delete next[sid]
  return next
}
