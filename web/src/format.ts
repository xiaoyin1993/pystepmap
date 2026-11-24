// 项目菜单、读图进度里给人看的数字和时间（纯函数，format.test.ts）。

import type { AnalysisSummary } from './api'

const pad = (n: number) => String(n).padStart(2, '0')
const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

/** 分析时间：刚刚 / N 分钟前 / 今天 14:05 / 昨天 14:05 / 11月14日 14:05 / 2025年11月14日（按本机时区） */
export function whenText(iso: string, now: Date = new Date()): string {
  const t = new Date(iso)
  if (!iso || Number.isNaN(t.getTime())) return '时间不明'
  const minutes = Math.floor((now.getTime() - t.getTime()) / 60_000)
  if (minutes >= 0 && minutes < 1) return '刚刚'
  if (minutes >= 1 && minutes < 60) return `${minutes} 分钟前`
  const hm = `${pad(t.getHours())}:${pad(t.getMinutes())}`
  if (sameDay(t, now)) return `今天 ${hm}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(t, yesterday)) return `昨天 ${hm}`
  if (t.getFullYear() === now.getFullYear()) return `${t.getMonth() + 1}月${t.getDate()}日 ${hm}`
  return `${t.getFullYear()}年${t.getMonth() + 1}月${t.getDate()}日`
}

/** 字节数：12 KB / 3.4 MB */
export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 读图进度：已收到 / 总量（不知道总量时只写已收到的） */
export function progressText(received: number, total: number | null): string {
  if (total && total > 0) return `${sizeText(Math.min(received, total))} / ${sizeText(total)}`
  return sizeText(received)
}

/** 进度百分比（0–100）；不知道总量时为 null */
export function progressPercent(received: number, total: number | null): number | null {
  if (!total || total <= 0) return null
  return Math.min(100, Math.floor((received / total) * 100))
}

/** 依赖目录：分析范围里出现它们，多半是 exclude 只写了几项、取代了默认的排除目录 */
const DEPENDENCY_DIRS = new Set(['site-packages', '.venv', 'node_modules'])

/**
 * 摘要表明分析了虚拟环境或依赖目录：源码根、跳过的文件的路径里有一级目录叫 site-packages、.venv 或 node_modules。
 * 返回这样的路径（源码根在前，去重）；没有时是空列表。
 */
export function dependencyPaths(s: AnalysisSummary): string[] {
  const hit = (path: string) => path.split(/[\\/]+/).some((part) => DEPENDENCY_DIRS.has(part))
  return [...new Set([...s.source_roots.filter(hit), ...s.skipped_files.map((f) => f.file).filter(hit)])]
}

export const DEPENDENCY_WARNING = '分析范围里有虚拟环境或依赖目录，检查分析配置里的 exclude'

export interface SummaryRow {
  label: string
  value: string
  /** 值得留意（有跳过的文件、没有入口、分析范围里有依赖目录） */
  warn?: boolean
  /** 鼠标停留时的说明 */
  hint?: string
}

/** 项目菜单里的分析摘要：一行一项。分析了虚拟环境或依赖目录时第一行是提醒。 */
export function summaryRows(s: AnalysisSummary): SummaryRow[] {
  const deps = dependencyPaths(s)
  const blind = s.unresolved_blind  // 旧的分析没有记这一项
  const rows: SummaryRow[] = []
  if (deps.length) {
    const shown = deps.slice(0, 3).join('、') + (deps.length > 3 ? ` 等 ${deps.length} 处` : '')
    rows.push({ label: '注意', value: DEPENDENCY_WARNING, warn: true, hint: `例如：${shown}` })
  }
  rows.push(
    { label: '规模', value: `${s.functions} 个函数 · ${s.lines} 条功能线 · ${s.files} 个文件` },
    {
      label: '调用',
      // 目标不明的调用大半是第三方库、标准库和日志，不是盲点：括号里才是可能真落在项目代码上的那部分
      value: `目标不明 ${s.unresolved_calls}${blind === undefined ? '' : `（可能是项目内的 ${blind}）`} · 推测 ${s.guessed_calls}`,
      hint:
        '目标不明：没法判断调用的是哪个函数，大半是第三方库、标准库和日志；' +
        '括号里是名字在项目里有定义、可能真落在项目代码上的那部分。推测：按方法名猜的，不保证对',
    },
  )
  if (s.common_tasks > 0) rows.push({ label: '公共任务', value: `${s.common_tasks} 个（被很多功能线派发，单独成线）` })
  rows.push({
    label: '跳过的文件',
    value: s.skipped_files.length ? `${s.skipped_files.length} 个` : '没有',
    warn: s.skipped_files.length > 0,
  })
  if (!s.has_entries) {
    rows.push({ label: '入口', value: '没有功能线', warn: true, hint: s.entry_notes?.[0] ?? '功能线从入口函数开始：框架的路由、任务、命令、脚本的 main，或 pystepkit 标记' })
  } else if (s.entry_notes?.length) {
    rows.push({ label: '入口', value: '入口是推断的（没有人调用的顶层函数）', warn: true, hint: s.entry_notes[0] })
  }
  if (s.mark_notes?.length) {
    rows.push({ label: '标记', value: `${s.mark_notes.length} 处参数读不出来（当作没写）`, warn: true, hint: s.mark_notes.join('\n') })
  }
  const extra = [s.rules.length ? `规则 ${s.rules.join('、')}` : '', `用时 ${s.seconds} 秒`].filter(Boolean)
  rows.push({ label: '分析', value: extra.join(' · ') })
  return rows
}
