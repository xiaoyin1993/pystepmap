import { describe, expect, it } from 'vitest'
import type { AnalysisSummary } from './api'
import { DEPENDENCY_WARNING, dependencyPaths, progressPercent, progressText, sizeText, summaryRows, whenText } from './format'

describe('时间', () => {
  // 本机时区里的 2026-11-16 15:30
  const now = new Date(2026, 10, 16, 15, 30, 0)
  const at = (...a: [number, number, number, number, number]) => new Date(...a).toISOString()

  it('刚刚、几分钟前、今天、昨天、今年、往年', () => {
    expect(whenText(at(2026, 10, 16, 15, 29), now)).toBe('1 分钟前')
    expect(whenText(new Date(now.getTime() - 20_000).toISOString(), now)).toBe('刚刚')
    expect(whenText(at(2026, 10, 16, 9, 5), now)).toBe('今天 09:05')
    expect(whenText(at(2026, 10, 15, 23, 59), now)).toBe('昨天 23:59')
    expect(whenText(at(2026, 0, 3, 8, 0), now)).toBe('1月3日 08:00')
    expect(whenText(at(2025, 10, 14, 8, 0), now)).toBe('2025年11月14日')
  })

  it('后端给的 UTC 时间按本机时区显示；认不出的写"时间不明"', () => {
    const iso = '2026-11-16T04:53:24+00:00'
    const local = new Date(iso)
    const hm = `${String(local.getHours()).padStart(2, '0')}:${String(local.getMinutes()).padStart(2, '0')}`
    expect(whenText(iso, new Date(local.getTime() + 3 * 3600_000))).toBe(
      local.getDate() === new Date(local.getTime() + 3 * 3600_000).getDate() ? `今天 ${hm}` : `昨天 ${hm}`,
    )
    expect(whenText('', now)).toBe('时间不明')
    expect(whenText('nope', now)).toBe('时间不明')
  })
})

describe('大小和进度', () => {
  it('B / KB / MB', () => {
    expect(sizeText(900)).toBe('900 B')
    expect(sizeText(75822)).toBe('74 KB')
    expect(sizeText(10515364)).toBe('10.0 MB')
  })

  it('知道总量时写"已收到 / 总量"和百分比；不知道时只写已收到的', () => {
    expect(progressText(5 * 1024 * 1024, 10 * 1024 * 1024)).toBe('5.0 MB / 10.0 MB')
    expect(progressPercent(5, 10)).toBe(50)
    expect(progressPercent(12, 10)).toBe(100)
    expect(progressText(2048, null)).toBe('2 KB')
    expect(progressPercent(2048, null)).toBeNull()
  })
})

describe('分析摘要', () => {
  const s: AnalysisSummary = {
    functions: 1152,
    lines: 119,
    common_tasks: 1,
    files: 257,
    unresolved_calls: 3874,
    unresolved_blind: 112,
    guessed_calls: 8,
    unreachable: 0,
    dead_ends: 0,
    skipped_files: [{ file: 'bad.py', reason: '语法错误' }],
    source_roots: ['.'],
    rules: ['fastapi', 'taskiq'],
    excluded: {},
    has_entries: true,
    seconds: 1.4,
  }

  it('旧的分析没有记盲点数时只写总数', () => {
    const { unresolved_blind: _, ...old } = s
    expect(summaryRows(old).map((r) => r.value)).toContain('目标不明 3874 · 推测 8')
  })

  it('函数数、功能线数、目标不明 / 推测调用数、跳过的文件都在', () => {
    const text = summaryRows(s).map((r) => `${r.label}：${r.value}`)
    expect(text).toEqual([
      '规模：1152 个函数 · 119 条功能线 · 257 个文件',
      '调用：目标不明 3874（可能是项目内的 112） · 推测 8',
      '公共任务：1 个（被很多功能线派发，单独成线）',
      '跳过的文件：1 个',
      '分析：规则 fastapi、taskiq · 用时 1.4 秒',
    ])
    expect(summaryRows(s).find((r) => r.label === '跳过的文件')?.warn).toBe(true)
  })

  it('分析范围里有虚拟环境或依赖目录：认得出（按整级目录名），摘要第一行提醒，鼠标停留时列出路径', () => {
    expect(dependencyPaths(s)).toEqual([])
    const venv = { ...s, source_roots: ['.', '.venv/lib/python3.12/site-packages', 'core/src', 'web/node_modules/x'] }
    expect(dependencyPaths(venv)).toEqual(['.venv/lib/python3.12/site-packages', 'web/node_modules/x'])
    expect(dependencyPaths({ ...s, source_roots: ['venv/lib/python3.11/site-packages'] })).toEqual(['venv/lib/python3.11/site-packages'])
    expect(dependencyPaths({ ...s, skipped_files: [{ file: 'node_modules/a/b.py', reason: '语法错误' }] })).toEqual(['node_modules/a/b.py'])
    // 名字里只是含有这几个字不算
    expect(dependencyPaths({ ...s, source_roots: ['my.venv.tools', 'node_modules_helper', 'src/site-packages-docs'] })).toEqual([])
    const rows = summaryRows(venv)
    expect(rows[0]).toEqual({
      label: '注意',
      value: DEPENDENCY_WARNING,
      warn: true,
      hint: '例如：.venv/lib/python3.12/site-packages、web/node_modules/x',
    })
    expect(DEPENDENCY_WARNING).toBe('分析范围里有虚拟环境或依赖目录，检查分析配置里的 exclude')
    expect(summaryRows(s)[0].label).toBe('规模')
  })

  it('没有入口时提示；没有跳过的文件写"没有"', () => {
    const rows = summaryRows({ ...s, has_entries: false, skipped_files: [], common_tasks: 0, rules: [] })
    expect(rows.map((r) => r.label)).toEqual(['规模', '调用', '跳过的文件', '入口', '分析'])
    expect(rows.find((r) => r.label === '跳过的文件')).toMatchObject({ value: '没有', warn: false })
    expect(rows.at(-1)?.value).toBe('用时 1.4 秒')
    expect(rows.find((r) => r.label === '入口')).toMatchObject({ value: '没有功能线', warn: true })
  })

  it('入口是推断出来的时候说明；后端给的整句放在提示里', () => {
    const note = '入口是推断的：没有认出路由、任务、工作流、命令或脚本入口，把没有人调用的 2 个顶层函数各当作一条功能线'
    const row = summaryRows({ ...s, entry_notes: [note] }).find((r) => r.label === '入口')
    expect(row).toMatchObject({ value: '入口是推断的（没有人调用的顶层函数）', warn: true, hint: note })
    expect(summaryRows(s).some((r) => r.label === '入口')).toBe(false)
  })

  it('标记里有读不出来的参数时多一行，原句放在提示里', () => {
    const note = 'keys.py:5 标记里的 group=GROUP.strip() 读不出来，当作没写'
    const row = summaryRows({ ...s, mark_notes: [note, note] }).find((r) => r.label === '标记')
    expect(row).toMatchObject({ value: '2 处参数读不出来（当作没写）', warn: true, hint: `${note}\n${note}` })
    expect(summaryRows(s).some((r) => r.label === '标记')).toBe(false)
  })
})
