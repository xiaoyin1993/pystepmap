// "运行路径"页签：一个测试的运行路径（后端 GET /runs/{id}/paths?test=，形状见 core/src/pystepmap/runpath.py）在画布上
// 的样子——哪些节点 / 连线走过、各几次多久、哪些结局没有测试走到——和运行面板上的说明文字。纯函数，测试见
// runview.test.ts。叠到图上（放置）在 runplace.ts，失败停在哪在 runfailure.ts。测试命令还在跑时的样子（liveRunMarks：
// 走到过的节点亮、别的暗）和进度的说明文字（liveCountParts 等）也在这里。
// 时间：一个节点的耗时含它调用的函数（return f() 的时间算在步骤上，结局没有）；耗时为 0 的节点只写次数。

import type { LiveProgress, RunInfo, RunPathsJson, TestRow, UncoveredJson } from './api'
import { drillTargets, type ChangeStop } from './diffview'
import { buildLineView, drillChainOf, drillSteps, setOpen } from './graph'
import type { CardView, EdgeView, FlowLine, FunctionGraph, LineTask, LineView, Point, ProjectGraph, ViewState } from './graph'
import { entryFunctions, locateTestRun } from './runplace'

const own = <T>(rec: Record<string, T>, key: string): T | undefined => (Object.hasOwn(rec, key) ? rec[key] : undefined)
const fnOf = (graph: ProjectGraph, key: string): FunctionGraph | undefined => own(graph.functions, key)
const fullTitle = (x: { title: string; title_full?: string }): string => x.title_full || x.title

// ---------------------------------------------------------------------------------------------------------------
// 功能线

export function findLine(graph: ProjectGraph, lineSid: string): FlowLine | null {
  return graph.lines.find((l) => l.sid === lineSid) ?? graph.lines.find((l) => l.id === lineSid) ?? null
}

/**
 * 这条线涉及的函数 key（同 core 的 query.line_functions）：派发树上的每个任务（先序），每个任务之后紧跟从它出发沿
 * call_targets 能到的所有函数（深度优先、按源码顺序）；不重复，不在图里的跳过。
 */
export function lineFunctions(graph: ProjectGraph, line: FlowLine): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  const tasks: LineTask[] = [line.root]
  while (tasks.length) {
    const t = tasks.pop()!
    const stack = [t.function]
    while (stack.length) {
      const key = stack.pop()!
      const f = fnOf(graph, key)
      if (seen.has(key) || !f) continue
      seen.add(key)
      out.push(key)
      stack.push(...[...f.call_targets].reverse())
    }
    tasks.push(...[...t.children].reverse())
  }
  return out
}

/** 派发树上的任务函数 key（先序，不重复，不在图里的跳过） */
export function lineTasks(graph: ProjectGraph, line: FlowLine): string[] {
  const out: string[] = []
  const visit = (t: LineTask) => {
    if (fnOf(graph, t.function) && !out.includes(t.function)) out.push(t.function)
    t.children.forEach(visit)
  }
  visit(line.root)
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 叠加层

export interface NodeRun {
  visits: number
  /** 耗时（纳秒，含它调用的函数） */
  ns: number
  /**
   * 占这条线在这个测试里总耗时的比例（0–1）：分母是这条线上跑过的任务函数的耗时之和。定不了时为 0（不显示）：这条线的
   * 任务都没跑；或者被调函数的耗时装不进这条线上调用它的、走过的步骤里（调用它的步骤没走到 / 对不上，或者测试在线外
   * 也直接调用了它）。
   */
  share: number
}

export interface RunStop {
  functionKey: string
  nodeId: string
  kind: 'return' | 'raise'
  /** 异常类型名；return 为空串 */
  exc: string
  n: number
}

export interface RunOverlay {
  /** 叠的是哪个测试；没有选测试时为 null */
  test: string | null
  /** 函数 key → 节点 id → 次数、耗时、占比 */
  nodes: Map<string, Map<string, NodeRun>>
  /** 函数 key → "起点 id|终点 id" → 次数（只有图上有的连线） */
  edges: Map<string, Map<string, number>>
  stops: RunStop[]
  /** 这个测试执行过、放得到这张图上的函数 key */
  functionsRun: Set<string>
  /** 函数 key → 这个测试里执行了几次 */
  executions: Map<string, number>
  /**
   * 函数 key → 节点 id → 在这个节点结束的执行次数：经过次数减去运行记录里从它出发的连线次数（记录里的原样，含图上没有
   * 的；以"未捕获异常退出"离开时不记连线）。异常从哪个调用处抛出来，那里就多一次结束。
   */
  ended: Map<string, Map<string, number>>
  /** 让测试失败的异常类型名（第一个失败的用例）和所有失败的用例的类型名；早于 S12 的数据为 "" / [] */
  testExc: string
  testExcs: string[]
  /** 记录插件看到的真实调用（S12）：函数 key → 被调函数 key → 有几条执行记录调用过它（含动态调用、回调） */
  calls: Map<string, Map<string, number>>
  /** 同上，经项目以外的帧（库的包装函数、contextmanager、回调）的间接调用（S14） */
  icalls: Map<string, Map<string, number>>
  /** 这条测试里有没有任何记下的调用（早于 S12 的数据没有） */
  hasCalls: boolean
  /** 入口函数（core 的 entry_functions，放到这张图上的）：执行过、没有别的执行过的函数直接或间接调用过它 */
  entries: Set<string>
  /** 放不上的函数 + 配不上的节点 sid 个数 */
  unmatched: number
  hasTime: boolean
}

export const emptyOverlay = (): RunOverlay => ({
  test: null,
  nodes: new Map(),
  edges: new Map(),
  stops: [],
  functionsRun: new Set(),
  executions: new Map(),
  ended: new Map(),
  testExc: '',
  testExcs: [],
  calls: new Map(),
  icalls: new Map(),
  hasCalls: false,
  entries: new Set(),
  unmatched: 0,
  hasTime: false,
})


/**
 * 一个测试的叠加层。paths 是运行路径（整份或 ?test= 的切片，切片带着重名组的全部 shapes）；lineId 是画布上的功能线
 * （只用来算占比：分母是这条线上跑过的任务函数的耗时之和；这条线的任务都没跑时取这条线上跑过的函数里耗时最长的）。
 */
export function buildRunOverlay(
  graph: ProjectGraph,
  paths: RunPathsJson | null,
  testId: string,
  lineId: string | null,
  counters?: { shareSteps: number },
): RunOverlay {
  const out = emptyOverlay()
  if (!paths) return out
  const located = locateTestRun(graph, paths, testId)
  if (!located.test) return out
  out.test = testId
  out.unmatched = located.unmatched
  out.testExc = located.test.exc ?? ''
  out.testExcs = located.test.excs ?? []
  // 运行时的函数 sid → 图里的 key（放得上的）；记下的调用、入口函数都按它换到这张图上
  const keyOf = new Map([...located.placed].map(([key, p]) => [p.runSid, key]))
  for (const [runSid, f] of Object.entries(located.test.functions)) {
    const key = keyOf.get(runSid)
    for (const [field, target] of [
      [f.calls, out.calls],
      [f.icalls, out.icalls],
    ] as const) {
      const recorded = Object.entries(field ?? {})
      if (recorded.length) out.hasCalls = true
      if (key === undefined) continue
      const m = new Map<string, number>()
      for (const [callee, n] of recorded) {
        const ck = keyOf.get(callee)
        if (ck !== undefined) m.set(ck, (m.get(ck) ?? 0) + n)
      }
      target.set(key, m)
    }
  }
  for (const runSid of entryFunctions(located.test)) {
    const key = keyOf.get(runSid)
    if (key !== undefined) out.entries.add(key)
  }
  for (const [key, p] of located.placed) {
    out.functionsRun.add(key)
    out.executions.set(key, p.run.executions)
    if (p.run.has_time) out.hasTime = true
    const nodes = new Map<string, NodeRun>()
    for (const [sid, stat] of Object.entries(p.run.nodes)) {
      const id = p.mapping.get(sid)
      if (id === undefined) continue
      const prev = nodes.get(id)
      nodes.set(id, { visits: (prev?.visits ?? 0) + stat.visits, ns: (prev?.ns ?? 0) + stat.ns, share: 0 })
    }
    out.nodes.set(key, nodes)
    const outgoing = new Map<string, number>()
    for (const [k, n] of Object.entries(p.run.edges)) {
      const from = k.split('|')[0]
      outgoing.set(from, (outgoing.get(from) ?? 0) + n)
    }
    const ended = new Map<string, number>()
    for (const [sid, stat] of Object.entries(p.run.nodes)) {
      const id = p.mapping.get(sid)
      if (id !== undefined) ended.set(id, (ended.get(id) ?? 0) + stat.visits - (outgoing.get(sid) ?? 0))
    }
    out.ended.set(key, ended)
    const real = new Set(p.fn.edges.map((e) => `${e.src}|${e.dst}`))
    const edges = new Map<string, number>()
    for (const [k, n] of Object.entries(p.run.edges)) {
      const parts = k.split('|')
      if (parts.length !== 2) continue
      const a = p.mapping.get(parts[0])
      const b = p.mapping.get(parts[1])
      if (a === undefined || b === undefined || !real.has(`${a}|${b}`)) continue
      edges.set(`${a}|${b}`, (edges.get(`${a}|${b}`) ?? 0) + n)
    }
    out.edges.set(key, edges)
    for (const [k, n] of Object.entries(p.run.exits)) {
      const [sid, kind, ...exc] = k.split('|')
      const id = sid ? p.mapping.get(sid) : undefined
      if (id === undefined || (kind !== 'return' && kind !== 'raise')) continue
      out.stops.push({ functionKey: key, nodeId: id, kind, exc: exc.join('|'), n })
    }
  }
  // 占比：分母是这条线上跑过的任务函数的耗时之和（节点的耗时含它调用的函数，被调函数的时间已经在调用它的步骤里）
  const line = lineId ? findLine(graph, lineId) : null
  const tasks = line ? lineTasks(graph, line) : []
  const total = (key: string) => [...(out.nodes.get(key)?.values() ?? [])].reduce((s, v) => s + v.ns, 0)
  const denom = tasks.reduce((s, k) => s + total(k), 0)
  if (denom > 0) {
    const ok = shareable(graph, out, new Set(tasks), total, counters)
    for (const [key, m] of out.nodes) if (ok(key)) for (const v of m.values()) v.share = Math.min(1, v.ns / denom)
  }
  return out
}

/**
 * 一个函数的节点能不能按这条线的总耗时算占比：任务函数可以；被调函数要它的总耗时装得进调用它的、走过的、本身也能算
 * 占比的那些节点的耗时之和里（调用它的步骤没走到 / 对不上、测试在线外也直接调用了它时装不进，不给占比）。
 * counters：测试用来数看过几个调用处（确认是线性的），界面不传。
 */
function shareable(
  graph: ProjectGraph,
  o: RunOverlay,
  tasks: Set<string>,
  total: (key: string) => number,
  counters?: { shareSteps: number },
): (key: string) => boolean {
  const callers = callersOf(graph)
  const memo = new Map<string, boolean>()
  const ok = (key: string): boolean => {
    const known = memo.get(key)
    if (known !== undefined) return known
    memo.set(key, false) // 环：先当作不行
    let result = tasks.has(key)
    if (!result) {
      let callerNs = 0
      for (const [caller, site] of callers.get(key) ?? []) {
        if (counters) counters.shareSteps += 1
        const stat = o.nodes.get(caller)?.get(site)
        if (stat && ok(caller)) callerNs += stat.ns
      }
      result = callerNs > 0 && total(key) <= callerNs
    }
    memo.set(key, result)
    return result
  }
  return ok
}


/** 没有测试走到的结局（后端按最新一次分析的图算的）→ 函数 key → 节点 id；这张图里找不到的忽略 */
export function uncoveredMarks(graph: ProjectGraph, outcomes: UncoveredJson[]): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  for (const o of outcomes) {
    const key = own(graph.sids, o.function) ?? (fnOf(graph, o.function) ? o.function : undefined)
    const f = key ? fnOf(graph, key) : undefined
    const n = f?.nodes.find((x) => (x.sid || x.id) === o.node)
    if (!f || !n) continue
    const set = out.get(f.key)
    if (set) set.add(n.id)
    else out.set(f.key, new Set([n.id]))
  }
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 文字

/** 耗时："14 µs"、"0.8 ms"、"12 ms"、"1.3 s" */
export function formatNs(ns: number): string {
  if (ns <= 0) return '0 ms'
  if (ns < 100_000) return `${Math.max(1, Math.round(ns / 1_000))} µs`
  const ms = ns / 1_000_000
  if (ms < 9.95) return `${ms.toFixed(1)} ms`
  if (ms < 1_000) return `${Math.round(ms)} ms`
  const s = ns / 1_000_000_000
  return s < 9.95 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`
}

/** 节点角上的字：有耗时写耗时（多次时"×3 · 12 ms"），耗时为 0 或没有耗时数据时只写次数 */
export function runBadge(visits: number, ns: number, hasTime: boolean): string {
  if (!hasTime || ns <= 0) return `×${visits}`
  return visits > 1 ? `×${visits} · ${formatNs(ns)}` : formatNs(ns)
}

/** 秒数："0.2 秒"、"3.2 秒"、"2 分 5 秒" */
export function secondsText(s: number): string {
  if (s < 60) return `${s.toFixed(1)} 秒`
  return `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`
}

const OUTCOME_FAILED = new Set(['failed', 'error'])

/**
 * 测试列表：onlyLine 时只列走过这条线的（后端给的 on_line > 0：执行过这条线的入口函数才不为 0；lineKnown 为假——旧的运行没有索引——时列全部）；
 * query 按测试 id 里的字筛（不分大小写）。失败、出错的在前，测试之外的执行放最后，其余照原来的顺序。
 */
export function testsForLine(rows: TestRow[], lineKnown: boolean, onlyLine: boolean, query: string): TestRow[] {
  const q = query.trim().toLowerCase()
  const rank = (r: TestRow) => (r.test === '' ? 2 : OUTCOME_FAILED.has(r.outcome) ? 0 : 1)
  return rows
    .filter((r) => (!onlyLine || !lineKnown || (r.on_line ?? 0) > 0) && (!q || r.test.toLowerCase().includes(q)))
    .map((r, i) => ({ r, i }))
    .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
    .map((x) => x.r)
}

/** 测试 id 拆成文件和名字（列表上写名字，悬停显示完整 id） */
export function testName(id: string): { file: string; name: string } {
  if (!id) return { file: '', name: '（测试之外的执行：收集测试时导入模块等）' }
  const i = id.indexOf('::')
  return i < 0 ? { file: '', name: id } : { file: id.slice(0, i), name: id.slice(i + 2) }
}

export const isFailedOutcome = (outcome: string): boolean => OUTCOME_FAILED.has(outcome)

export const RUN_STATUS_NAME: Record<string, string> = { ok: '全部通过', tests_failed: '有失败', error: '没跑成', timeout: '超时', cancelled: '已取消' }

/** 一次运行的结果："12 通过、1 失败，用时 3.2 秒"；没跑完、出错时一句话；不认识的状态照原样 */
export function runResultText(run: RunInfo): string {
  const s = run.summary
  switch (run.status) {
    case 'ok':
    case 'tests_failed': {
      if (s.tests === 0) return `没有收集到测试，用时 ${secondsText(s.seconds)}`
      const parts = [`${s.passed} 通过`]
      if (s.failed) parts.push(`${s.failed} 失败`)
      if (s.skipped) parts.push(`${s.skipped} 跳过`)
      if (s.errors) parts.push(`${s.errors} 出错`)
      return `${parts.join('、')}，用时 ${secondsText(s.seconds)}`
    }
    case 'timeout':
      return '超时，测试没有跑完'
    case 'cancelled':
      return '已取消'
    case 'error':
      return '没有跑成（看下面的说明和测试输出）'
    default:
      return run.status
  }
}

const PHASE_TEXT: Record<string, string> = { preparing: '准备中', analyzing: '正在先分析', testing: '正在跑测试', processing: '正在整理结果' }

/** 正在跑的阶段：准备中 / 正在先分析（只在图过期、运行前重新分析时）/ 正在跑测试 / 正在整理结果；不认识的写"正在 X" */
export const phaseText = (phase: string): string => own(PHASE_TEXT, phase) ?? `正在${phase}`

// ---------------------------------------------------------------------------------------------------------------
// 运行的范围：全量 / 只跑一条功能线的测试

/**
 * 这个页面开始的一次运行跑的是什么：line 为 null 是全部测试，否则是这条线（title 是开始时界面上的标题，tests 是名单
 * 里的测试个数）。后端的 running 不说正在跑的是哪种：别处（另一个页面、AI 经 MCP）开始的运行界面不知道，为 null。
 */
export interface LaunchScope {
  line: string | null
  title: string
  tests: number
}

/** 一次运行对画布上这条线来说是什么：全量（full）、只跑了这条线的测试（here）、只跑了别的线的测试（other） */
export interface RunScopeView {
  kind: 'full' | 'here' | 'other'
  /** 列表上的标签："全量" / "「标题」" */
  label: string
  /** 只跑了一条线时那条线的标题（界面上手工改过的用改过的），全量时空串 */
  title: string
}

/** 运行列表上一次运行的范围。titleOf：功能线 sid → 界面上的标题（找不到时用运行记下的标题） */
export function runScopeView(run: RunInfo, line: string, titleOf: (sid: string) => string | undefined = () => undefined): RunScopeView {
  const s = run.scope
  if (!s) return { kind: 'full', label: '全量', title: '' }
  const title = titleOf(s.line) || s.line_title || s.line
  return { kind: s.line === line ? 'here' : 'other', label: `「${title}」`, title }
}

/** 这次运行能代表这条线的情况吗：全量运行，或者只跑了这条线的测试 */
export const runFitsLine = (run: RunInfo, line: string): boolean => run.scope === null || run.scope.line === line

/** 这条线的面板默认选哪次运行：最新的一次全量运行、或只跑了这条线的运行（runs 新的在前）；都没有时 null */
export function defaultRunFor(runs: RunInfo[], line: string): number | null {
  return runs.find((r) => runFitsLine(r, line))?.id ?? null
}

/**
 * 正在跑的是什么（界面在后面接"… 已用 N 秒"）。scope 为 null（不知道，别处开始的）时只说阶段；跑测试的阶段说"正在跑
 * 全部测试" / "正在跑这条线的 6 个测试" / "正在跑「X」的 6 个测试"，别的阶段写成"准备中（这条线的 6 个测试）"。
 */
export function runningText(phase: string, scope: LaunchScope | null, line: string): string {
  if (!scope) return phaseText(phase)
  const what = scope.line === null ? '全部测试' : scope.line === line ? `这条线的 ${scope.tests} 个测试` : `「${scope.title}」的 ${scope.tests} 个测试`
  return phase === 'testing' ? `正在跑${what}` : `${phaseText(phase)}（${what}）`
}

/** 运行条上运行中的说明：只跑一条线时说清是哪条线的测试；全量、不知道时同 LIVE_BAR_TEXT */
export function liveBarText(scope: LaunchScope | null, line: string): string {
  if (!scope || scope.line === null) return LIVE_BAR_TEXT
  if (scope.line === line) return `正在跑这条线的 ${scope.tests} 个测试…亮的是到目前为止走到过的节点（已跑完的测试合起来）；跑完后可以按测试看路径和耗时`
  return `正在跑「${scope.title}」的 ${scope.tests} 个测试…亮的是它们到目前为止在这条线上走到过的节点；跑完后到那条线上按测试看`
}

export interface VerifyItem {
  kindText: string
  /** 现在的图里的函数 key（按 sid 找；找不到时 null，不能跳过去） */
  functionKey: string | null
  /** 函数的标题；找不到时是函数 sid */
  function: string
  where: string
  detail: string
  count: number
}

export interface VerifyView {
  /** ok：图和实际执行一致；bad：有不在图上的执行；none：没有核对结果（没跑完、没收到记录） */
  kind: 'ok' | 'bad' | 'none'
  text: string
  items: VerifyItem[]
  /** 后端只列了一部分（最多 30 条）：界面说"下面只列出前 30 条" */
  more: boolean
  /** 运行期间代码改过时的说明（灰色），没有时为空串 */
  note: string
}

const MISMATCH_KIND: Record<string, string> = { edge: '走了图上没有的连线', end: '结局对不上', call: '调用对不上' }

/** 核对结果那一行和展开的列表 */
export function verifyView(run: RunInfo, graph: ProjectGraph | null): VerifyView {
  const v = run.summary.verify
  // 没有核对结果（没跑完），或者一条记录都没收到、什么都没核对（ok 为 null）：不说一致也不报警，看运行的说明
  if (!v || v.ok === null) return { kind: 'none', text: '', items: [], note: '', more: false }
  const more = v.stale.length > 3 ? ` 等 ${v.stale.length} 个文件` : ''
  const note = v.stale.length ? `运行期间代码改过（${v.stale.slice(0, 3).join('、')}${more}），核对里的不符不一定是图的问题` : ''
  if (v.ok) return { kind: 'ok', text: '图和实际执行一致', items: [], note, more: false }
  const items = run.summary.mismatch_list.map((m): VerifyItem => {
    const key = graph ? (own(graph.sids, m.function) ?? (fnOf(graph, m.function) ? m.function : null)) : null
    const f = graph && key ? fnOf(graph, key) : undefined
    const where = m.to_line > 0 && m.to_line !== m.from_line ? `第 ${m.from_line} → ${m.to_line} 行` : `第 ${m.from_line} 行`
    return { kindText: own(MISMATCH_KIND, m.kind) ?? m.kind, functionKey: f ? f.key : null, function: f ? fullTitle(f) : m.function, where, detail: m.detail, count: m.count }
  })
  // 处数按核对的计数（列表最多 30 条，不全）
  const total = v.mismatches.edge + v.mismatches.end + v.mismatches.call
  const listed = items.reduce((s, m) => s + m.count, 0)
  const capped = listed < total
  const places = items.length ? `（${capped ? '至少 ' : ''}${items.length} 处）` : ''
  return { kind: 'bad', text: `有 ${Math.max(total, listed)} 次实际执行不在图上${places}——图不可信`, items, note, more: capped }
}

/**
 * 画布上的图比运行用的那次分析新（运行之后重新分析过）：说一句"这次运行之后代码改过（重新分析过），按节点标识对到现在的
 * 图上"，选了测试且有对不上的地方时加"有 N 处对不上"。别的情况只在有对不上的地方时说。unmatched 不知道（没选测试）时
 * 为 null。
 */
export function staleRunNote(runAnalysis: number, shownAnalysis: number | null, unmatched: number | null): string | null {
  if (shownAnalysis !== null && shownAnalysis > runAnalysis) {
    const tail = unmatched ? `，有 ${unmatched} 处对不上` : ''
    return `这次运行之后代码改过（重新分析过），按节点标识对到现在的图上${tail}`
  }
  return unmatched ? `有 ${unmatched} 处对不上现在的图` : null
}

// ---------------------------------------------------------------------------------------------------------------
// 画布上的标注

export interface NodeRunMark extends NodeRun {
  /** 角上的字 */
  badge: string
}

export interface UncoveredCount {
  certain: number
  uncertain: number
}

/** 调用处（callee）、折叠占位和卡片（inside）上没走到的结局的说明，两种数分开写 */
export function uncoveredCountText(c: UncoveredCount, where: 'callee' | 'inside'): string {
  const head = where === 'callee' ? '它调用的函数里有' : '里面有'
  const parts: string[] = []
  if (c.certain) parts.push(`${c.certain} 个结局没有测试走到`)
  if (c.uncertain) parts.push(`${c.uncertain} 个${c.certain ? '' : '结局'}对不上（代码改过，重新运行测试才知道）`)
  return `${head} ${parts.join('，')}`
}

export interface RunMarks {
  /** 选了测试（或运行中）：走过的正常亮度、没走到的变暗 */
  active: boolean
  /** 运行中的样子（liveRunMarks）：只有走到过的节点，没有次数、耗时，连线不亮也不暗 */
  live: boolean
  hasTime: boolean
  /** 画布节点 id → 走过几次、多久 */
  nodes: Map<string, NodeRunMark>
  /** 走过的连线（画布连线 id）→ 次数 */
  edges: Map<string, number>
  /** 走过的派发连线 */
  links: Set<string>
  /** 失败的停点：画布节点 id；节点被折叠着时是折叠占位、卡片收起时是卡片 */
  stops: Set<string>
  /**
   * 没有测试走到的结局：画布节点 id（或者把它收起来的折叠占位、卡片；在没钻入、钻入不到的被调函数里时是调用它的节点）
   * → 确定没走到 / 对不上（两种都有时算确定没走到）
   */
  uncovered: Map<string, 'certain' | 'uncertain'>
  /** 折叠占位、收起的卡片、调用处里面有几个确定没走到、几个对不上的结局 */
  uncoveredInside: Map<string, UncoveredCount>
  /** 收起的卡片、折叠占位里有走过的节点（蓝点） */
  inside: Set<string>
  /** 函数执行过的卡片（任务、钻入、回到前面的任务）；选了测试时其余的卡片变暗 */
  cardsRun: Set<string>
  /** 回放时正走到的那个元素（节点、折叠占位、卡片），外面一圈亮蓝；不在回放时 null */
  current: string | null
}

export const emptyRunMarks = (): RunMarks => ({
  current: null,
  active: false,
  live: false,
  hasTime: false,
  nodes: new Map(),
  edges: new Map(),
  links: new Set(),
  stops: new Set(),
  uncovered: new Map(),
  uncoveredInside: new Map(),
  inside: new Set(),
  cardsRun: new Set(),
})

const nodeIdsCache = new WeakMap<LineView, Set<string>>()

/**
 * 一个图内节点在画布上由谁代表：节点自己（self）、把它收起来的折叠占位、收起的卡片、代替它的钻入卡片。这个函数在画布上
 * 没有卡片时为空。
 * 步骤换成了钻入链（同时钻入了它调用的几个函数）时按 via 挑链上的哪一张（chainElement）：via 是要标的东西其实在这个
 * 步骤调用的哪个函数里——往上找调用处时是被调函数，失败的停点画在调用处时是抛出异常的函数。
 */
function elementsFor(
  view: LineView,
  fnKey: string,
  nodeId: string,
  graph: ProjectGraph,
  via: string | null = null,
): { id: string; self: boolean }[] {
  const n = fnOf(graph, fnKey)?.nodes.find((x) => x.id === nodeId)
  if (!n) return []
  const out: { id: string; self: boolean }[] = []
  let nodeIds = nodeIdsCache.get(view)
  if (!nodeIds) nodeIdsCache.set(view, (nodeIds = new Set(view.nodes.map((x) => x.id))))
  for (const c of view.cards) {
    if (c.fn !== fnKey || c.goto) continue
    if (!c.expanded) {
      out.push({ id: c.id, self: false })
      continue
    }
    const vid = `${c.id}/${n.sid || n.id}`
    if (nodeIds.has(vid)) {
      out.push({ id: vid, self: true })
      continue
    }
    const fold = view.folds.find((f) => f.card === c.id && f.collapsed && f.keys.includes(nodeId))
    if (fold) {
      out.push({ id: fold.id, self: false })
      continue
    }
    const chain = drillChainOf(view, vid)
    if (chain.length) out.push({ id: chainElement(graph, chain, via), self: false })
  }
  return out
}

const reachCache = new WeakMap<ProjectGraph, Map<string, Set<string>>>()

/**
 * 换成了钻入链的步骤上，要标的东西（在 via 这个函数里）标在哪：链上就是 via 的那张卡片；via 是这个步骤还没展开的
 * 被调函数时是那个钻入选项的 id（同展开后那张钻入卡片的 id，画布上没有这个元素：链上卡片的"再展开 N 个"按它标）；
 * 链上的卡片里能调用到 via 的那张；都不是（不知道在哪个函数里、或者 via 不经这一步的能钻入的调用）时是链的第一张。
 * 不会标到别的被调函数的卡片上去。
 */
function chainElement(graph: ProjectGraph, chain: CardView[], via: string | null): string {
  if (via !== null) {
    const card = chain.find((c) => c.fn === via)
    if (card) return card.id
    const option = chain[0].anchor?.drills.find((d) => d.fn === via && !d.open)
    if (option) return option.id
    let byTarget = reachCache.get(graph)
    if (!byTarget) reachCache.set(graph, (byTarget = new Map()))
    let from = byTarget.get(via)
    if (!from) byTarget.set(via, (from = leadsToRun(graph, new Set([via]))))
    const reach = chain.find((c) => c.fn !== null && from.has(c.fn))
    if (reach) return reach.id
  }
  return chain[0].id
}

const callersCache = new WeakMap<ProjectGraph, Map<string, [string, string][]>>()

/** 被调函数 key → [调用它的函数 key, 调用处的节点 id]（不算派发：派发的任务有自己的卡片） */
function callersOf(graph: ProjectGraph): Map<string, [string, string][]> {
  let m = callersCache.get(graph)
  if (!m) {
    m = new Map()
    for (const f of Object.values(graph.functions)) {
      for (const n of f.nodes) {
        for (const c of n.calls) {
          if (c.is_dispatch || !c.target || c.target === f.key) continue
          const list = m.get(c.target) ?? []
          if (!list.some(([k, id]) => k === f.key && id === n.id)) list.push([f.key, n.id])
          m.set(c.target, list)
        }
      }
    }
    callersCache.set(graph, m)
  }
  return m
}

/**
 * 一个结局节点在画布上标在哪里：它自己（或收起它的折叠、卡片）；它所在的函数在画布上没有卡片（没钻入、钻入不到）时，
 * 一层层往上找调用它的函数，标在最近的、画布上有的调用处（self 为假）。
 */
function outcomeElements(view: LineView, graph: ProjectGraph, fnKey: string, nodeId: string): { id: string; self: boolean }[] {
  const direct = elementsFor(view, fnKey, nodeId, graph)
  if (direct.length) return direct
  const callers = callersOf(graph)
  const out = new Map<string, { id: string; self: boolean }>()
  const seen = new Set([fnKey])
  let frontier = [fnKey]
  while (frontier.length && out.size === 0) {
    const next: string[] = []
    for (const g of frontier) {
      for (const [f, site] of callers.get(g) ?? []) {
        const els = elementsFor(view, f, site, graph, g)
        if (els.length) for (const e of els) out.set(e.id, { id: e.id, self: false })
        else if (!seen.has(f)) {
          seen.add(f)
          next.push(f)
        }
      }
    }
    frontier = next
  }
  return [...out.values()]
}

/**
 * 同一对图内节点之间并行的几条连线（比如普通连线和 raise 连线到同一个节点）：运行记录只记"相邻两个节点"，分不出走的是
 * 哪一条。分得出时只亮那一条：进捕获节点的是 raise 连线；这个函数在这个测试里没有异常（没以 raise 离开、没进过捕获
 * 节点）时只亮普通的。分不出时（有异常）几条都亮——宁多勿漏，是近似。
 */
function edgeSupported(e: EdgeView, siblings: EdgeView[], dstType: string | undefined, hadException: boolean): boolean {
  const isRaise = (x: EdgeView) => x.kind === 'raise'
  if (!siblings.some((x) => isRaise(x) !== isRaise(e))) return true
  if (dstType === 'catch') return isRaise(e)
  return hadException || !isRaise(e)
}

/** 失败的停点（红圈）画在哪：图里的函数、节点；画在调用处时异常是在哪个函数里抛出的（raisedIn） */
export interface RingAt {
  functionKey: string
  nodeId: string
  raisedIn?: { functionKey: string } | null
}

/**
 * 一条线的视图上，运行路径要标出来的东西（节点组件、连线按它画；改了不重新排版）。overlay 是选中的测试（没选时 null）；
 * stop 是失败的停点（failureStop）；uncovered / uncertain 是要标出的没走到的结局（uncoveredMarks；不标时传空表）。
 */
export function runMarks(
  view: LineView | null,
  graph: ProjectGraph | null,
  overlay: RunOverlay | null,
  stop: RingAt | null,
  uncovered: Map<string, Set<string>>,
  uncertain: Map<string, Set<string>>,
): RunMarks {
  const out = emptyRunMarks()
  if (!view || !graph) return out
  const o = overlay && overlay.test !== null ? overlay : null
  if (o) {
    out.active = true
    out.hasTime = o.hasTime
    const cardFn = new Map(view.cards.map((c) => [c.id, c.fn]))
    for (const c of view.cards) {
      if (!c.fn || !o.functionsRun.has(c.fn)) continue
      out.cardsRun.add(c.id)
      if (!c.expanded && !c.goto) out.inside.add(c.id)
    }
    for (const n of view.nodes) {
      const fn = cardFn.get(n.card)
      const stat = fn ? o.nodes.get(fn)?.get(n.key) : undefined
      if (stat) out.nodes.set(n.id, { ...stat, badge: runBadge(stat.visits, stat.ns, o.hasTime) })
    }
    for (const f of view.folds) {
      const fn = cardFn.get(f.card)
      const walked = fn ? o.nodes.get(fn) : undefined
      if (f.collapsed && walked && f.keys.some((k) => walked.has(k))) out.inside.add(f.id)
    }
    // 同一张卡片里同一对图内节点的几条画布连线
    const pairOf = (k: string) => k.split('|').slice(0, 2).join('|')
    const byPair = new Map<string, EdgeView[]>()
    for (const e of view.edges) {
      for (const p of new Set(e.keys.map(pairOf))) {
        const list = byPair.get(`${e.card}\u0000${p}`) ?? []
        list.push(e)
        byPair.set(`${e.card}\u0000${p}`, list)
      }
    }
    const hadException = new Map<string, boolean>()
    const exceptionIn = (fn: string): boolean => {
      let v = hadException.get(fn)
      if (v === undefined) {
        const f = fnOf(graph, fn)
        const walked = o.nodes.get(fn)
        v = o.stops.some((s) => s.functionKey === fn && s.kind === 'raise') || (f?.nodes.some((n) => n.type === 'catch' && walked?.has(n.id)) ?? false)
        hadException.set(fn, v)
      }
      return v
    }
    for (const e of view.edges) {
      const fn = cardFn.get(e.card)
      const walked = fn ? o.edges.get(fn) : undefined
      if (!fn || !walked) continue
      let count = 0
      for (const p of new Set(e.keys.map(pairOf))) {
        const c = walked.get(p) ?? 0
        if (c === 0) continue
        const dst = fnOf(graph, fn)?.nodes.find((n) => n.id === p.split('|')[1])
        if (edgeSupported(e, byPair.get(`${e.card}\u0000${p}`) ?? [e], dst?.type, exceptionIn(fn))) count += c
      }
      if (count > 0) out.edges.set(e.id, count)
    }
    for (const l of view.links) {
      if (!out.cardsRun.has(l.dst)) continue
      if (out.nodes.has(l.src) || out.inside.has(l.src) || out.cardsRun.has(l.src)) out.links.add(l.id)
    }
    if (stop) for (const { id } of elementsFor(view, stop.functionKey, stop.nodeId, graph, stop.raisedIn?.functionKey ?? null)) out.stops.add(id)
  }
  for (const [kind, marks] of [
    ['uncertain', uncertain],
    ['certain', uncovered],
  ] as const) {
    for (const [fnKey, ids] of marks) {
      for (const nodeId of ids) {
        for (const { id, self } of outcomeElements(view, graph, fnKey, nodeId)) {
          out.uncovered.set(id, kind)
          if (!self) {
            const c = out.uncoveredInside.get(id) ?? { certain: 0, uncertain: 0 }
            c[kind] += 1
            out.uncoveredInside.set(id, c)
          }
        }
      }
    }
  }
  return out
}

/**
 * 运行中（测试命令还在跑）的画布标注。reached 是后端给的这条线上每个执行过的函数里走到过的节点（函数 key → 节点 sid，
 * GET …/runs?live_line= 的 progress.nodes：节点语句的第一行在已经跑完的测试里执行过）。走到过的节点亮（不带次数、耗时），
 * 别的变暗；执行过的函数的卡片不暗，收起的卡片、折叠着的分支里有走到过的节点时有蓝点。只记了执行过哪些行、不知道走的
 * 是哪条连线：连线不亮也不暗。没有失败的红圈、没走到的结局——那些等跑完、整理好结果后按测试看。
 */
export function liveRunMarks(view: LineView | null, graph: ProjectGraph | null, reached: Record<string, string[]>): RunMarks {
  const out = emptyRunMarks()
  if (!view || !graph) return out
  out.active = true
  out.live = true
  const ids = new Map<string, Set<string>>()
  for (const [key, sids] of Object.entries(reached)) {
    const f = fnOf(graph, key)
    if (!f) continue
    const bySid = new Map(f.nodes.map((n) => [n.sid || n.id, n.id]))
    ids.set(key, new Set(sids.flatMap((sid) => bySid.get(sid) ?? [])))
  }
  const cardFn = new Map(view.cards.map((c) => [c.id, c.fn]))
  for (const c of view.cards) {
    if (!c.fn || !ids.has(c.fn)) continue
    out.cardsRun.add(c.id)
    if (!c.expanded && !c.goto && ids.get(c.fn)!.size) out.inside.add(c.id)
  }
  for (const n of view.nodes) {
    const fn = cardFn.get(n.card)
    if (fn && ids.get(fn)?.has(n.key)) out.nodes.set(n.id, { visits: 0, ns: 0, share: 0, badge: '' })
  }
  for (const f of view.folds) {
    const fn = cardFn.get(f.card)
    const walked = fn ? ids.get(fn) : undefined
    if (f.collapsed && walked && f.keys.some((k) => walked.has(k))) out.inside.add(f.id)
  }
  return out
}

/** 运行中的计数，一段一段（界面用 " · " 连起来，bad 的标红）："已跑完 37 / 925 个测试"、"通过 36"、"失败 1"；跳过、出错有才写 */
export function liveCountParts(p: LiveProgress): { text: string; bad: boolean }[] {
  const out = [{ text: p.collected !== null ? `已跑完 ${p.done} / ${p.collected} 个测试` : `已跑完 ${p.done} 个测试`, bad: false }]
  out.push({ text: `通过 ${p.passed}`, bad: false })
  if (p.failed) out.push({ text: `失败 ${p.failed}`, bad: true })
  if (p.skipped) out.push({ text: `跳过 ${p.skipped}`, bad: false })
  if (p.errors) out.push({ text: `出错 ${p.errors}`, bad: true })
  return out
}

/** 进度条：跑完的（不算失败、出错）和失败、出错的各占收集到的多少（0–1）；不知道一共几个时 null。跑完的比收集到的多时按跑完的算 */
export function liveBar(p: LiveProgress): { ok: number; bad: number } | null {
  if (p.collected === null || p.collected <= 0) return null
  const total = Math.max(p.collected, p.done)
  const bad = Math.min(p.done, p.failed + p.errors)
  return { ok: (p.done - bad) / total, bad: bad / total }
}

/** "正在跑：test_x（另有 2 个）"：名字去掉文件；两个测试之间（没有在跑的）时 null */
export function liveRunningText(p: LiveProgress): string | null {
  if (!p.running.length) return null
  const more = p.running.length - 1 + p.running_more
  return `正在跑：${testName(p.running[0]).name}${more > 0 ? `（另有 ${more} 个）` : ''}`
}

/** 侧栏功能线上运行中的小标记的说明 */
export function liveLineTitle(c: { functions: number; total: number }, fresh: boolean): string {
  return `运行中：已到 ${c.functions} / ${c.total} 个函数（已跑完的测试执行过的）${fresh ? '，最近一次刷新时又多了' : ''}`
}

/** 运行条上运行中的说明（必须是记下的事实：亮 = 已跑完的测试执行过这个节点的第一行） */
export const LIVE_BAR_TEXT = '运行中：亮的是到目前为止走到过的节点（所有已跑完的测试合起来）；跑完后可以按测试看路径和耗时'
/** 悬停时的补充：哪些节点运行中分不清 */
export const LIVE_BAR_HINT =
  '节点语句的第一行在已跑完的测试里执行过，就算走到过。捕获（except）节点、和调用写在同一行的结局、执行到函数末尾和未捕获异常的结局，光看执行过的行分不清，运行中不亮；连线也不标。'

/** 折线上按长度比例 t（0–1）取的点：连线上"走了几次"的数字标在中点。没有点时 null */
export function pointAlong(points: Point[], t: number): Point | null {
  if (points.length === 0) return null
  const lens = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y))
  let left = lens.reduce((a, b) => a + b, 0) * Math.min(1, Math.max(0, t))
  for (let i = 0; i < lens.length; i++) {
    if (left <= lens[i] && lens[i] > 0) {
      const k = left / lens[i]
      return { x: points[i].x + (points[i + 1].x - points[i].x) * k, y: points[i].y + (points[i + 1].y - points[i].y) * k }
    }
    left -= lens[i]
  }
  return { ...points[points.length - 1] }
}

/** 节点组件的 class：run-walked / run-dim / run-inside / run-stop / run-uncovered / run-uncertain */
export function runNodeClasses(m: RunMarks, id: string, kind: 'node' | 'fold' | 'card' = 'node'): string[] {
  const out: string[] = []
  if (kind === 'node') {
    if (m.nodes.has(id)) out.push('run-walked')
    else if (m.active) out.push('run-dim')
  } else if (kind === 'fold') {
    if (m.inside.has(id)) out.push('run-inside')
    else if (m.active) out.push('run-dim')
  } else if (m.active && !m.cardsRun.has(id)) out.push('run-dim')
  else if (m.inside.has(id)) out.push('run-inside')
  if (m.stops.has(id)) out.push('run-stop')
  const u = m.uncovered.get(id)
  if (u) out.push(u === 'certain' ? 'run-uncovered' : 'run-uncertain')
  if (m.current === id) out.push('run-current')
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 定位

/**
 * 在一条功能线上找到一个函数（以及其中的节点）：先找派发树上的任务卡片，再沿能钻入的调用找（最短的钻入路）。给
 * diffview 的 revealStop 用（它据此展开卡片、钻入、展开分支）。不在这条线上时 null。
 */
export function locateOnLine(graph: ProjectGraph, lineSid: string, fnKey: string, nodeId: string | null): ChangeStop | null {
  const line = findLine(graph, lineSid)
  if (!line || !fnOf(graph, fnKey)) return null
  for (const t of lineTasks(graph, line)) {
    const path = drillPath(graph, t, fnKey)
    if (path) return { functionKey: fnKey, nodeId, kind: 'changed', text: '', where: '', task: t, path }
  }
  return null
}

function drillPath(graph: ProjectGraph, from: string, target: string): ChangeStop['path'] | null {
  if (from === target) return []
  const seen = new Set([from])
  const queue: { key: string; path: ChangeStop['path'] }[] = [{ key: from, path: [] }]
  while (queue.length) {
    const { key, path } = queue.shift()!
    for (const n of fnOf(graph, key)?.nodes ?? []) {
      for (const callee of drillTargets(graph, n)) {
        if (seen.has(callee)) continue
        seen.add(callee)
        const next = [...path, { fn: key, node: n.id }]
        if (callee === target) return next
        queue.push({ key: callee, path: next })
      }
    }
  }
  return null
}

/**
 * 在图上找一个函数去哪条线上看：优先 preferred 这条线，再按列表顺序找别的线；画布上钻入不到、但这条线涉及它（判断函数
 * 等）时定位到这条线的根任务卡片。哪条线都不涉及时 null。
 */
export function findOnGraph(graph: ProjectGraph, preferred: string, fnKey: string, nodeId: string | null): { line: string; stop: ChangeStop } | null {
  const lines = [...graph.lines].sort((a, b) => Number(b.sid === preferred) - Number(a.sid === preferred))
  for (const l of lines) {
    const stop = locateOnLine(graph, l.sid, fnKey, nodeId)
    if (stop) return { line: l.sid, stop }
  }
  for (const l of lines) {
    if (!lineFunctions(graph, l).includes(fnKey)) continue
    const root = l.root.function
    return { line: l.sid, stop: { functionKey: root, nodeId: null, kind: 'changed', text: '', where: '', task: root, path: [] } }
  }
  return null
}

// ---------------------------------------------------------------------------------------------------------------
// 选了测试：把它走过的地方都露出来

/** 一次最多自动钻入几个被调函数（调用链很深、很宽的测试不把画布撑到排不动） */
export const MAX_AUTO_DRILLS = 40

/**
 * 从哪些函数出发、顺着图上的调用能走到这个测试执行过的函数（含执行过的函数自己）。画布上要露出执行过的函数，得先
 * 钻进这些函数：调用链上面几层没被这个测试执行（测试直接调了深处的函数、共用的辅助函数）时也一样。
 */
export function leadsToRun(graph: ProjectGraph, functionsRun: ReadonlySet<string>): Set<string> {
  // 反过来走：从执行过的函数往调用方找
  const callers = new Map<string, string[]>()
  for (const [key, f] of Object.entries(graph.functions)) {
    for (const t of f.call_targets) {
      const list = callers.get(t)
      if (list) list.push(key)
      else callers.set(t, [key])
    }
  }
  const out = new Set<string>()
  const stack = [...functionsRun].filter((k) => fnOf(graph, k))
  while (stack.length) {
    const key = stack.pop()!
    if (out.has(key)) continue
    out.add(key)
    for (const c of callers.get(key) ?? []) if (!out.has(c)) stack.push(c)
  }
  return out
}

/**
 * 把一个测试走过的地方都展开：执行过的任务卡片展开，里面有走过节点的折叠分支展开，步骤钻入它调用的函数里这个测试执行
 * 过的、和顺着调用能走到执行过的函数的（它自己没被执行，画出来是暗的，里面再往下才是走过的）——一个步骤调用了几个
 * 这样的函数时都钻入（排成钻入链），已经钻入了别的也照样补上。折叠的分支里有这样的步骤时也展开。展开之后露出来的新
 * 卡片、新分支接着看，直到没有可展开的，或者钻入满 MAX_AUTO_DRILLS 个（more 为真：还有没钻入的）。已经展开的不动，
 * 不收起任何东西。返回新的展开状态和展开了几处（0 = 不用动）。
 */
export function revealWalkedState(
  graph: ProjectGraph,
  lineSid: string,
  overlay: RunOverlay,
  base: ViewState,
): { state: ViewState; opened: number; more: boolean } {
  const leads = leadsToRun(graph, overlay.functionsRun)
  /** 函数 fn 里的节点 key 调用的函数里，有没有通向执行过的函数的 */
  const callsInto = (fn: string, key: string): boolean =>
    !!fnOf(graph, fn)
      ?.nodes.find((n) => n.id === key)
      ?.calls.some((c) => c.target !== null && leads.has(c.target))
  let state = base
  let opened = 0
  let drilled = 0
  let more = false
  for (let round = 0; round < 60; round++) {
    const view = buildLineView(graph, lineSid, state)
    const cardFn = new Map(view.cards.map((c) => [c.id, c.fn]))
    let next = state
    for (const c of view.cards) {
      if (c.kind === 'task' && c.fn && leads.has(c.fn) && c.expandable && !c.expanded && !c.goto) {
        next = setOpen(next, 'cards', c.id, true)
        opened++
      }
    }
    for (const f of view.folds) {
      const fn = cardFn.get(f.card)
      if (!f.collapsed || !fn) continue
      const walked = overlay.nodes.get(fn)
      if (f.keys.some((k) => walked?.has(k) || callsInto(fn, k))) {
        next = setOpen(next, 'folds', f.id, true)
        opened++
      }
    }
    more = false
    for (const n of drillSteps(view)) {
      // leads 里含执行过的函数自己
      for (const d of n.drills) {
        if (d.open || !leads.has(d.fn)) continue
        if (drilled >= MAX_AUTO_DRILLS) {
          more = true
          continue
        }
        next = setOpen(next, 'drills', d.id, true)
        drilled++
        opened++
      }
    }
    if (next === state) break
    state = next
  }
  return { state, opened, more }
}
