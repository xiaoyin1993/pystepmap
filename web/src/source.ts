// 详情栏的源码片段：要哪几行、高亮哪几行、怎么拼进"复制给 AI"（纯函数，source.test.ts），以及向平台后端取片段
// （GET /api/projects/{id}/source，后端只读已添加项目根目录之内的 .py，一次最多 400 行）。

import { ref, watch, type Ref } from 'vue'
import { api, errorText, type SourceSnippet } from './api'
import type { SourceLoc } from './graph'

export type { SourceSnippet }

/** 节点前后各带几行上下文 */
export const CONTEXT = 5
/** 卡片：函数开头最多几行（签名和文档注释） */
export const HEAD_LINES = 30
/** 节点的语句很长时最多显示几行 */
export const MAX_SHOWN = 80

export interface SourceRequest {
  file: string
  /** 要取的行（从 1 起，含两端） */
  start: number
  end: number
  /** 高亮的行：节点自己的语句；卡片是 def 那一行 */
  focusStart: number
  focusEnd: number
}

/** 选中节点：它自己的语句行（高亮）前后各 CONTEXT 行；语句很长时最多 MAX_SHOWN 行。隐含节点（没有行号）没有。 */
export function nodeSourceRequest(loc: SourceLoc | null): SourceRequest | null {
  if (!loc || loc.line <= 0) return null
  const start = Math.max(1, loc.line - CONTEXT)
  const end = Math.min(Math.max(loc.endLine, loc.line) + CONTEXT, start + MAX_SHOWN - 1)
  return { file: loc.file, start, end, focusStart: loc.line, focusEnd: Math.max(loc.endLine, loc.line) }
}

/** 选中任务卡片 / 钻入卡片：函数的开头（签名和文档注释），最多 HEAD_LINES 行，不超过函数末尾。 */
export function functionHeadRequest(loc: SourceLoc | null, fnEndLine: number | null): SourceRequest | null {
  if (!loc || loc.line <= 0) return null
  const last = fnEndLine && fnEndLine >= loc.line ? fnEndLine : Infinity
  return { file: loc.file, start: loc.line, end: Math.min(loc.line + HEAD_LINES - 1, last), focusStart: loc.line, focusEnd: loc.line }
}

/** 图可能过期：文件指纹对不上，或者要高亮的行已经超出文件末尾 */
export function outOfDate(s: SourceSnippet, req: SourceRequest): boolean {
  return s.stale === true || req.focusEnd > s.total
}

const rangeText = (a: number, b: number) => (b > a ? `第 ${a}–${b} 行` : `第 ${a} 行`)

/**
 * "复制给 AI"里的源码：文件、行号范围、高亮的是哪几行，然后是代码块（围栏比代码里最长的反引号串长）。note：看变更记录时
 * 片段上的说明（reviewSourceNote；null = 确定是当时的代码，不写）；不给时按 stale 写"文件在生成图之后改过"。
 */
export function snippetForAi(s: SourceSnippet, req: SourceRequest, what: string, note?: string | null): string {
  if (s.lines.length === 0) return ''
  const longest = Math.max(0, ...s.lines.map((l) => Math.max(0, ...(l.match(/`+/g) ?? []).map((m) => m.length))))
  const fence = '`'.repeat(Math.max(3, longest + 1))
  const extra = note !== undefined ? (note ? `；${note}` : '') : s.stale ? '；文件在生成图之后改过，行号可能对不上' : ''
  const head = `源码（${s.file} ${rangeText(s.start, s.end)}，${what}在${rangeText(req.focusStart, req.focusEnd)}${extra}）：`
  return `${head}\n${fence}python\n${s.lines.join('\n')}\n${fence}`
}

/** 看变更记录时、不能确定片段就是当时的代码时说的话 */
export const REVIEW_SOURCE_NOTE = '这是现在的源码，行号是当时的，可能对不上'

/**
 * 看变更记录时源码片段上说什么。片段总是现在的文件；源码接口按记录那次分析（analysis=）判断文件改过没有——只有确定那次
 * 分析还在后端（headKept，reviewview.ts）、后端说没改过时，片段才确定是当时的代码（返回 null，不写说明）。那次分析可能
 * 已经清掉了（后端悄悄按最新一次比）、图里没有这个文件的指纹时，说"这是现在的源码……"；确定改过时说改过了。
 */
export function reviewSourceNote(s: SourceSnippet, req: SourceRequest, headKept: boolean): string | null {
  if (!headKept || s.stale === null) return REVIEW_SOURCE_NOTE
  if (outOfDate(s, req)) return `文件在那次分析之后改过：${REVIEW_SOURCE_NOTE}`
  return null
}

/** 缓存键：哪个项目的哪次分析（重新分析后文件指纹变了，stale 要重新判断）、哪个文件的哪几行 */
export function sourceKey(scope: SourceScope, req: SourceRequest): string {
  return `${scope.project}#${scope.analysis ?? ''}|${req.file}:${req.start}-${req.end}`
}

/** 源码从哪个项目取：项目 id 和画布上那张图的分析 id */
export interface SourceScope {
  project: string
  analysis: number | null
}

const cache = new Map<string, SourceSnippet>()
const CACHE_SIZE = 200

export type SourceStatus = 'none' | 'loading' | 'ok' | 'error'

/**
 * 跟着选中的东西取片段（后端的 /api/projects/{id}/source）：只在要取的范围变了时取（重新排版不重取），只认最后一次
 * （快速点别的节点时旧的作废），取过的记在缓存里、直接用。
 */
export function useSource(scope: Ref<SourceScope | null>, request: Ref<SourceRequest | null>) {
  const status = ref<SourceStatus>('none')
  const snippet = ref<SourceSnippet | null>(null)
  const error = ref('')
  let seq = 0
  watch(
    () => (request.value && scope.value ? { key: sourceKey(scope.value, request.value), scope: scope.value, req: request.value } : null),
    async (want, prev) => {
      if (want && prev && want.key === prev.key) return
      const n = ++seq
      snippet.value = null
      error.value = ''
      if (!want) return void (status.value = 'none')
      const hit = cache.get(want.key)
      if (hit) {
        snippet.value = hit
        status.value = 'ok'
        return
      }
      status.value = 'loading'
      try {
        // 带上画布上那张图的分析 id：后端按那一次判断文件改过没有
        const s = await api.getSource(want.scope.project, want.req.file, want.req.start, want.req.end, want.scope.analysis)
        cache.set(want.key, s)
        if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value!)
        if (n !== seq) return
        snippet.value = s
        status.value = 'ok'
      } catch (e) {
        if (n !== seq) return
        error.value = errorText(e)
        status.value = 'error'
      }
    },
    { immediate: true },
  )
  return { status, snippet, error }
}
