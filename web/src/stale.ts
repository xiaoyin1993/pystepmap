// 过期条（StaleBar.vue）要显示什么：代码在最新一次分析之后改过没有（后端 GET /status 的 stale）。纯函数，测试见
// stale.test.ts。什么时候去查、发现新的分析怎么办在 state.ts。

import type { ProjectStatus } from './api'

/** 展开的文件列表最多列几个，其余写"还有 N 个" */
export const STALE_FILES_SHOWN = 20

export type StaleFileKind = '改' | '增' | '删'

export interface StaleView {
  show: boolean
  /** stale：琥珀色的过期条；unchecked：灰色的"没法检查"；不显示时为 null */
  kind: 'stale' | 'unchecked' | null
  text: string
  /** 展开后列的文件（改的在前，再增、再删），最多 STALE_FILES_SHOWN 个 */
  files: { kind: StaleFileKind; file: string }[]
  /** 涉及改了 / 删了的文件的功能线 sid（列表上标小点）；后端没有这项信息时为 null */
  lineSids: Set<string> | null
  /** 没列出来的文件个数 */
  more: number
  /** 后端的说明（读不了、没法比的文件），放在文件列表那里；没有时为空串 */
  note: string
}

const HIDDEN: StaleView = { show: false, kind: null, text: '', files: [], lineSids: null, more: 0, note: '' }

export function staleView(status: ProjectStatus | null): StaleView {
  const s = status?.stale
  if (!s) return HIDDEN
  if (!s.checked) return { ...HIDDEN, show: true, kind: 'unchecked', text: `没法检查代码有没有变：${s.reason}` }
  if (!s.stale) return HIDDEN
  const parts: [StaleFileKind, number][] = [
    ['改', s.total.changed],
    ['增', s.total.added],
    ['删', s.total.removed],
  ]
  const counts = parts.filter(([, n]) => n > 0).map(([k, n]) => `${k} ${n}`)
  const all = [
    ...s.changed.map((file) => ({ kind: '改' as const, file })),
    ...s.added.map((file) => ({ kind: '增' as const, file })),
    ...s.removed.map((file) => ({ kind: '删' as const, file })),
  ]
  const files = all.slice(0, STALE_FILES_SHOWN)
  const total = Math.max(s.total.changed + s.total.added + s.total.removed, all.length)
  return {
    show: true,
    kind: 'stale',
    text: `代码已变更，图可能过期：${counts.join('、') || '有'} 个文件`,
    files,
    lineSids: s.lines ? new Set(s.lines) : null,
    more: total - files.length,
    note: s.note,
  }
}

/** 后端最新一次分析比已载入的新（别的进程——pystepmap mcp、别的页面——重新分析了）；还没载入时为假 */
export function hasNewerAnalysis(status: ProjectStatus | null, loadedAnalysisId: number | null): boolean {
  return status?.analysis_id != null && loadedAnalysisId !== null && status.analysis_id > loadedAnalysisId
}
