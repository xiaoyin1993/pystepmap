// 在画布上回看变更记录（只读）：每次点"标为已看"，平台把那次的流程变化连同当时的图存一条（每个项目留最近 10 条，
// 后端 GET …/reviews、…/reviews/{id}、…/reviews/{id}/graph）。打开一条时画布上是那时的图，标出的就是那次的变化——用的是
// 和现在的差异同一套视图、标记、"上一处 / 下一处"（diffview.ts）。这里是纯函数：地址栏里怎么写、进来时看哪条功能线、
// "上一次 / 下一次"是哪条、横幅上的话、差异栏说"相对什么"、那次分析还在不在后端（源码片段能不能按它判断改过没有）。
// 状态和动作在 state.ts（review、openReview、leaveReview），测试见 reviewview.test.ts。

import { errorText, isApiError, isMissingEndpoint, type ReviewInfo } from './api'
import type { BaselineRef } from './diffview'
import { groupLines, type DisplayLine } from './overrides'

/** 地址栏里项目后面那一段以它开头时是一条变更记录：#/<项目>/review:<编号>/<功能线 sid> */
export const REVIEW_REF = 'review:'

export interface ReviewRef {
  /** 记录的编号；写得不对（不是正整数）时为 null */
  id: number | null
  /** 要看的功能线 sid；没写时为空串 */
  line: string
}

/** 地址栏里项目后面那一段（已解码）是不是一条变更记录："review:3"、"review:3/<功能线 sid>"；不是时 null */
export function parseReviewRef(tail: string): ReviewRef | null {
  if (!tail.startsWith(REVIEW_REF)) return null
  const rest = tail.slice(REVIEW_REF.length)
  const slash = rest.indexOf('/')
  const num = slash < 0 ? rest : rest.slice(0, slash)
  return { id: /^[1-9]\d*$/.test(num) ? Number(num) : null, line: slash < 0 ? '' : rest.slice(slash + 1) }
}

/** 看变更记录时的地址栏：项目、功能线各自编码（功能线 sid 里有 /、:、#），中间的 review:<编号> 原样 */
export function reviewHash(project: string, id: number, line: string): string {
  return `#/${encodeURIComponent(project)}/${REVIEW_REF}${id}${line ? `/${encodeURIComponent(line)}` : ''}`
}

/**
 * 打开一条记录时看哪条功能线（lines 是那时的图里的功能线，应用了手工标题和分组；changed 是这次有变化的功能线）：
 * 地址栏指定的（图里有时）；正在看的线这次有变化就留在它上面；否则按列表顺序第一条有变化的；都没有变化时正在看的线
 * （图里有时），再不然列表第一条。
 */
export function reviewLine(lines: readonly DisplayLine[], changed: ReadonlyMap<string, unknown>, wanted: string, current: string): string {
  const has = (sid: string) => sid !== '' && lines.some((d) => d.sid === sid)
  if (has(wanted)) return wanted
  if (has(current) && changed.has(current)) return current
  const ordered = groupLines(lines).flatMap((g) => g.items)
  const first = ordered.find((d) => changed.has(d.sid))
  if (first) return first.sid
  if (has(current)) return current
  return ordered[0]?.sid ?? ''
}

export interface ReviewNeighbors {
  /** 更早的一条（"上一次"）、更晚的一条（"下一次"）：只算存着图的；没有时 null */
  earlier: ReviewInfo | null
  later: ReviewInfo | null
  /** 按时间从早到晚这是第几条（从 1 起）；看着的那条已经不在列表里（被新的挤掉了）时为 0 */
  index: number
  total: number
}

/** "上一次 / 下一次"：按编号（存的先后）找；看着的那条已经不在列表里了也找得到前后 */
export function reviewNeighbors(list: readonly ReviewInfo[], id: number): ReviewNeighbors {
  const sorted = [...list].sort((a, b) => a.id - b.id)
  const withGraph = sorted.filter((r) => r.has_graph === true)
  return {
    earlier: [...withGraph].reverse().find((r) => r.id < id) ?? null,
    later: withGraph.find((r) => r.id > id) ?? null,
    index: sorted.findIndex((r) => r.id === id) + 1,
    total: sorted.length,
  }
}

/** 横幅上的话：哪次标为已看、哪次分析相对哪次；提醒这是当时的图 */
export function reviewBannerText(info: ReviewInfo, when: (iso: string) => string): string {
  const base = info.base_id === null ? '更早的一次' : `#${info.base_id}`
  return `正在看变更记录：${when(info.acked_at)} 标为已看的那次变化（分析 #${info.head_id} 相对 ${base}）。这是当时的图，不是现在的代码`
}

/**
 * 差异栏说"相对什么"（diffview 的 diffHeadline）：这条记录的对比基准正是更早一条记录标为已看的那次分析时说"上次标为
 * 已看时"，别的（最早的一条、更早的记录已经清掉了）说"对比基准"；时间是基准那次分析的。没有基准时 null。
 */
export function reviewBaseline(info: ReviewInfo, list: readonly ReviewInfo[], when: (iso: string) => string): BaselineRef | null {
  if (info.base_id === null) return null
  const acked = list.some((r) => r.id < info.id && r.head_id === info.base_id)
  return { kind: acked ? 'acknowledged' : 'base', when: info.base_at ? when(info.base_at) : '' }
}

/**
 * 打开一条记录失败时说的话。staying：画布上还是原来那条记录（从横幅走到别的记录时）；否则是现在的图（地址栏里的记录
 * 不在了、从对话框打开失败）。
 */
export function reviewFailText(id: number, e: unknown, staying: boolean): string {
  const then = staying ? '，画布上还是原来那条' : '，显示的是现在的图'
  if (isApiError(e) && e.code === 'no_graph') return `变更记录 #${id} 没有存当时的图（旧版本存的记录），只能在"变更记录"里看文字${then}`
  if (isMissingEndpoint(e)) return `平台后端不支持在图上看变更记录（旧版本，升级后重启 pystepmap serve）${then}`
  if (isApiError(e) && e.kind === 'http' && e.status === 404) return `变更记录 #${id} 已经不在了（每个项目只留最近 10 条）${then}`
  if (isApiError(e) && e.kind === 'old-graph') return `变更记录 #${id} 当时的图是旧版引擎生成的，这个页面画不了，只能在"变更记录"里看文字${then}`
  return `没有打开变更记录 #${id}：${errorText(e)}${then}`
}

/**
 * 记录的那次分析（head）还在不在后端：源码接口按 analysis= 指定的那次判断文件改过没有，那次已经清掉了就悄悄按最新一次
 * 比，页面分不出来。后端每个项目只留最近 5 次分析，另外一定留着最新一次的对比基准——known 是页面知道的最新一次、它的
 * 对比基准（项目列表、过期检查、流程差异里的），head 是其中之一才算确定还在。
 */
export function headKept(head: number, known: readonly (number | null | undefined)[]): boolean {
  return known.some((k) => k === head)
}
