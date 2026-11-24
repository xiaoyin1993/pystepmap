import { describe, expect, it } from 'vitest'
import type { LiveProgress, RunInfo, RunPathsJson, TestRow, TestRunJson, UncoveredJson } from './api'
import { buildLineView, emptyState, setOpen } from './graph'
import { call, fn, line, project, task, uncaught } from './graph/fixtures/builders'
import { K, LINE, T_OK, all, check, fnRun, fsid, graph, mainLine, okTest, paths, post, run, send } from './graph/fixtures/runs'
import { CK, CL, at, bFn, cFn, chainFns, chainProject, into, mainFn } from './graph/fixtures/chain'
import { drillChainOf, type ViewState } from './graph'
import { functionShape } from './runplace'
import {
  LIVE_BAR_TEXT,
  buildRunOverlay,
  defaultRunFor,
  formatNs,
  liveBarText,
  lineFunctions,
  liveBar,
  liveCountParts,
  liveLineTitle,
  liveRunMarks,
  liveRunningText,
  locateOnLine,
  phaseText,
  pointAlong,
  runBadge,
  runFitsLine,
  runMarks,
  runNodeClasses,
  runResultText,
  runScopeView,
  runningText,
  staleRunNote,
  testName,
  testsForLine,
  uncoveredCountText,
  uncoveredMarks,
  verifyView,
  emptyOverlay,
  revealWalkedState,
  leadsToRun,
} from './runview'

describe('lineFunctions：照 core 的 line_functions', () => {
  it('派发树先序，每个任务之后紧跟它沿 call_targets 能到的函数', () => {
    expect(lineFunctions(graph, mainLine)).toEqual([K.run, K.check, K.big, K.post, K.send])
  })
})

describe('buildRunOverlay', () => {
  it('节点、连线按 sid 对上；次数、耗时', () => {
    const o = buildRunOverlay(graph, paths([okTest]), T_OK, LINE)
    expect(o.unmatched).toBe(0)
    expect(o.hasTime).toBe(true)
    expect([...o.functionsRun].sort()).toEqual([K.check, K.post, K.run, K.send].sort())
    const r = o.nodes.get(K.run)!
    expect(r.get('n5')).toMatchObject({ visits: 3, ns: 3_000 })
    expect(r.get('n6')).toMatchObject({ visits: 2, ns: 2_000_000 })
    expect(r.has('n4')).toBe(false)
    expect(o.edges.get(K.run)!.get('n5|n6')).toBe(2)
    expect(o.edges.get(K.run)!.get('n6|n5')).toBe(2)
    expect(o.edges.get(K.post)!.get('n1|n2')).toBe(2)
  })

  it('图上没有的连线（运行里的 start|end:n8）不画', () => {
    const o = buildRunOverlay(graph, paths([okTest]), T_OK, LINE)
    expect(o.edges.get(K.run)!.has('n1|n8')).toBe(false)
    expect(o.unmatched).toBe(0)
  })

  it('占比：这条线上任务的耗时为分母，每个都不超过 1，任务节点的加起来不超过 1', () => {
    const o = buildRunOverlay(graph, paths([okTest]), T_OK, LINE)
    const total = 4_000_000 + 1_000 + 3_000 + 2_000_000 + 990_000 + 6_000 + 500_000 // run + send
    expect(o.nodes.get(K.run)!.get('n2')!.share).toBeCloseTo(4_000_000 / total, 9)
    expect(o.nodes.get(K.post)!.get('n2')!.share).toBeCloseTo(1_800_000 / total, 9)
    let sum = 0
    for (const key of [K.run, K.send]) for (const v of o.nodes.get(key)!.values()) sum += v.share
    expect(sum).toBeLessThanOrEqual(1 + 1e-9)
    for (const m of o.nodes.values()) for (const v of m.values()) expect(v.share).toBeLessThanOrEqual(1)
  })

  it('这条线的任务都没跑（单测直接调用 check）：定不了分母，不给占比（不是 100%）', () => {
    const t: TestRunJson = { ...okTest, test: 'u', functions: { [fsid(check)]: okTest.functions[fsid(check)] } }
    const o = buildRunOverlay(graph, paths([t]), 'u', LINE)
    expect(o.nodes.get(K.check)!.get('n2')).toMatchObject({ ns: 1_000_000, share: 0 })
  })

  it('被调函数的耗时比这条线上调用它的步骤还长（测试在线外也直接调用了它）、或调用它的步骤没走到：不给占比', () => {
    const r = okTest.functions[fsid(run)]
    const outside: TestRunJson = {
      ...okTest,
      test: 'o',
      functions: { ...okTest.functions, [fsid(run)]: { ...r, nodes: { ...r.nodes, 'step:n2': { visits: 1, ns: 1_000 } } } },
    }
    const o = buildRunOverlay(graph, paths([outside]), 'o', LINE)
    expect(o.nodes.get(K.check)!.get('n2')!.share).toBe(0)
    expect(o.nodes.get(K.post)!.get('n2')!.share).toBeGreaterThan(0)
    const { 'step:n6': _gone, ...rest } = r.nodes
    void _gone
    const noCaller: TestRunJson = { ...okTest, test: 'c', functions: { ...okTest.functions, [fsid(run)]: { ...r, nodes: rest } } }
    expect(buildRunOverlay(graph, paths([noCaller]), 'c', LINE).nodes.get(K.post)!.get('n2')!.share).toBe(0)
  })

  it('没有选测试、测试不在里面：空的叠加层', () => {
    for (const o of [buildRunOverlay(graph, null, T_OK, LINE), buildRunOverlay(graph, paths([okTest]), 'nope', LINE)]) {
      expect(o.nodes.size).toBe(0)
      expect(o.functionsRun.size).toBe(0)
      expect(o.unmatched).toBe(0)
      expect(o.hasTime).toBe(false)
    }
  })

  it('sid 在图里找不到：记进 unmatched，不抛异常，不亮别的节点', () => {
    const f = okTest.functions[fsid(post)]
    const t: TestRunJson = {
      ...okTest,
      functions: {
        [fsid(post)]: { ...f, nodes: { ...f.nodes, 'step:gone': { visits: 1, ns: 5 } }, edges: { ...f.edges, 'step:gone|end:n3': 1 }, exits: { 'end:old|return|': 1 } },
        'wf.py::W.gone': fnRun(post, [['start', 1, 0]], {}, {}),
      },
    }
    t.functions['wf.py::W.gone'].function = 'wf.py::W.gone'
    // 形状不同（post 的形状和运行时不同）：只按相等配不重名的
    const o = buildRunOverlay(graph, paths([t], [run, check, send]), T_OK, LINE)
    // 放不上的函数 1 + 配不上的节点 sid 2（step:gone、end:old）
    expect(o.unmatched).toBe(3)
    expect([...o.nodes.get(K.post)!.keys()].sort()).toEqual(['n1', 'n2', 'n3'])
    expect(o.edges.get(K.post)!.has('n2|n3')).toBe(true)
    expect([...o.edges.get(K.post)!.keys()].every((k) => !k.includes('gone'))).toBe(true)
    expect(o.functionsRun.has('wf.py::W.gone')).toBe(false)
  })

  it('没有耗时的旧数据：hasTime 为假，占比都是 0', () => {
    const t: TestRunJson = { ...okTest, functions: { [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 0]], {}, {}, false) } }
    const o = buildRunOverlay(graph, paths([t]), T_OK, LINE)
    expect(o.hasTime).toBe(false)
    expect(o.nodes.get(K.post)!.get('n2')).toEqual({ visits: 1, ns: 0, share: 0 })
  })

  it('停点：return / raise、异常类型名、次数；截断的记录（停点为空）不算', () => {
    const t: TestRunJson = {
      ...okTest,
      outcome: 'failed',
      functions: {
        [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 10]], {}, { 'end:raise1|raise|ValueError': 2, '|raise|KeyError': 1, 'end:n4|return|': 1 }),
      },
    }
    const o = buildRunOverlay(graph, paths([t]), T_OK, LINE)
    expect(o.stops).toEqual(
      expect.arrayContaining([
        { functionKey: K.check, nodeId: 'n3', kind: 'raise', exc: 'ValueError', n: 2 },
        { functionKey: K.check, nodeId: 'n4', kind: 'return', exc: '', n: 1 },
      ]),
    )
    expect(o.stops).toHaveLength(2)
  })
})

describe('占比的计算是线性的（N4）', () => {
  // 一个任务调用 f1，f1 调用 f2……：每个函数一个调用处
  function chainGraph(n: number) {
    const keys = Array.from({ length: n }, (_, i) => `c.py::f${i}@${i * 10 + 1}`)
    const fns = keys.map((k, i) =>
      fn(k, [['n1', 'start', `f${i}`], ['n2', 'step', '下一个', { calls: i + 1 < n ? [call(keys[i + 1])] : [] }], ['n3', 'end', '返回', { end: 'ok' }]], [['n1', 'n2', 'next'], ['n2', 'n3', 'next']], i === 0 ? { kind: 'task', call_targets: [keys[1]] } : { call_targets: i + 1 < n ? [keys[i + 1]] : [] }),
    )
    const ln = line(task(keys[0]), fns)
    const g = project(fns, [ln])
    const functions = Object.fromEntries(fns.map((f, i) => [f.sid, fnRun(f, [['start', 1, 0], ['step:n2', 1, (n - i) * 10], ['end:n3', 1, 0]], {}, { 'end:n3|return|': 1 })]))
    const p: RunPathsJson = { version: 1, tests: [{ test: 't', outcome: 'passed', seconds: 0, cases: 1, functions }], shapes: Object.fromEntries(fns.map((f) => [f.sid, { shape: functionShape(f), dups: [] }])), function_dups: [] }
    return { g, p, ln }
  }

  it('看调用处的次数和函数个数成正比', () => {
    const steps = (n: number) => {
      const { g, p, ln } = chainGraph(n)
      const counters = { shareSteps: 0 }
      const o = buildRunOverlay(g, p, 't', ln.sid, counters)
      expect(o.nodes.get(`c.py::f${n - 1}@${(n - 1) * 10 + 1}`)!.get('n2')!.share).toBeGreaterThan(0)
      return counters.shareSteps
    }
    const a = steps(200)
    const b = steps(400)
    expect(a).toBeLessThanOrEqual(3 * 200)
    expect(b).toBeLessThanOrEqual(3 * 400)
  })
})

describe('formatNs、runBadge', () => {
  it('几档', () => {
    expect(formatNs(0)).toBe('0 ms')
    expect(formatNs(958)).toBe('1 µs')
    expect(formatNs(14_208)).toBe('14 µs')
    expect(formatNs(800_000)).toBe('0.8 ms')
    expect(formatNs(1_250_000)).toBe('1.3 ms')
    expect(formatNs(9_960_000)).toBe('10 ms')
    expect(formatNs(12_400_000)).toBe('12 ms')
    expect(formatNs(1_300_000_000)).toBe('1.3 s')
    expect(formatNs(75_000_000_000)).toBe('75 s')
  })

  it('角上的字：有耗时写耗时，多次写"×3 · 12 ms"，耗时为 0（或没有耗时数据）只写次数', () => {
    expect(runBadge(1, 12_000_000, true)).toBe('12 ms')
    expect(runBadge(3, 12_000_000, true)).toBe('×3 · 12 ms')
    expect(runBadge(1, 0, true)).toBe('×1')
    expect(runBadge(4, 0, true)).toBe('×4')
    expect(runBadge(2, 12_000_000, false)).toBe('×2')
  })
})

describe('uncoveredMarks', () => {
  it('函数 sid → 函数 key，节点 sid → 节点 id；找不到的忽略', () => {
    const outcomes: UncoveredJson[] = [
      { function: fsid(run), node: 'end:n4', title: '拒绝', end_kind: 'failed', file: 'wf.py', line: 5, uncertain: false },
      { function: fsid(check), node: 'end:raise1', title: '抛出超限', end_kind: 'failed', file: 'wf.py', line: 23, uncertain: false },
      { function: 'gone.py::x', node: 'end:n1', title: '?', end_kind: 'ok', file: 'gone.py', line: 1, uncertain: false },
      { function: fsid(run), node: 'end:gone', title: '?', end_kind: 'ok', file: 'wf.py', line: 1, uncertain: false },
    ]
    const m = uncoveredMarks(graph, outcomes)
    expect([...m.keys()].sort()).toEqual([K.check, K.run].sort())
    expect([...m.get(K.run)!]).toEqual(['n4'])
    expect([...m.get(K.check)!]).toEqual(['n3'])
  })
})

describe('testsForLine', () => {
  const row = (test: string, outcome: string, on_line: number | null): TestRow => ({ test, outcome, seconds: 0.1, cases: 1, functions: 3, on_line })
  const rows = [row('t/a.py::ok1', 'passed', 2), row('t/a.py::bad', 'failed', 1), row('t/a.py::other', 'passed', 0), row('t/a.py::err', 'error', 0), row('', '', 0), row('t/b.py::bad2', 'failed', 3)]

  it('只列走过这条线的测试，失败的在前（失败的之间、通过的之间照原来的顺序）', () => {
    expect(testsForLine(rows, true, true, '').map((r) => r.test)).toEqual(['t/a.py::bad', 't/b.py::bad2', 't/a.py::ok1'])
  })

  it('全部测试：出错的也算失败放在前面，测试之外的执行放最后', () => {
    expect(testsForLine(rows, true, false, '').map((r) => r.test)).toEqual(['t/a.py::bad', 't/a.py::err', 't/b.py::bad2', 't/a.py::ok1', 't/a.py::other', ''])
  })

  it('后端不知道哪些测试走过这条线（旧的运行）：列全部', () => {
    expect(testsForLine(rows, false, true, '')).toHaveLength(6)
  })

  it('搜索：按测试 id 里的字（不分大小写）', () => {
    expect(testsForLine(rows, true, false, 'B.PY').map((r) => r.test)).toEqual(['t/b.py::bad2'])
  })

  it('testName：文件和名字分开；测试之外的执行有自己的说法', () => {
    expect(testName('tests/test_t.py::TestX::test_y')).toEqual({ file: 'tests/test_t.py', name: 'TestX::test_y' })
    expect(testName('')).toEqual({ file: '', name: '（测试之外的执行：收集测试时导入模块等）' })
  })
})

describe('运行的说明文字', () => {
  const info = (over: Partial<RunInfo> = {}, summary: Partial<RunInfo['summary']> = {}): RunInfo => ({
    id: 3,
    analysis_id: 7,
    started_at: '2026-11-17T04:17:02+00:00',
    finished_at: '2026-11-17T04:17:05+00:00',
    command: 'python -m pytest',
    status: 'tests_failed',
    exit_code: 1,
    paths_size: 100,
    summary: {
      tests: 13,
      passed: 12,
      failed: 1,
      skipped: 0,
      errors: 0,
      seconds: 3.21,
      verify: { ok: true, mismatches: { edge: 0, end: 0, call: 0 }, functions_seen: 1, distinct_paths: 1, steps_total: 1, plugin_errors: 0, plugin_error_types: {}, truncated_records: 0, stale: [] },
      coverage: null,
      mismatch_list: [],
      note: '',
      ...summary,
    },
    scope: null,
    ...over,
  })

  it('跑完的："12 通过、1 失败，用时 3.2 秒"；跳过、出错有才写', () => {
    expect(runResultText(info())).toBe('12 通过、1 失败，用时 3.2 秒')
    expect(runResultText(info({ status: 'ok' }, { passed: 9, failed: 0, skipped: 2, errors: 1, seconds: 0.169 }))).toBe('9 通过、2 跳过、1 出错，用时 0.2 秒')
    expect(runResultText(info({ status: 'ok' }, { tests: 0, passed: 0, failed: 0 }))).toBe('没有收集到测试，用时 3.2 秒')
  })

  it('没跑完、出错、不认识的状态', () => {
    expect(runResultText(info({ status: 'timeout' }))).toBe('超时，测试没有跑完')
    expect(runResultText(info({ status: 'cancelled' }))).toBe('已取消')
    expect(runResultText(info({ status: 'error' }))).toBe('没有跑成（看下面的说明和测试输出）')
    expect(runResultText(info({ status: 'weird' }))).toBe('weird')
  })

  it('阶段：准备中 / 正在先分析 / 正在跑测试 / 正在整理结果，不认识的照写', () => {
    expect(phaseText('preparing')).toBe('准备中')
    expect(phaseText('analyzing')).toBe('正在先分析')
    expect(phaseText('testing')).toBe('正在跑测试')
    expect(phaseText('processing')).toBe('正在整理结果')
    expect(phaseText('warming')).toBe('正在warming')
  })

  it('核对结果：一致（绿）/ 有 N 处实际执行不在图上（红，列出每条）/ 没有核对结果', () => {
    expect(verifyView(info(), graph)).toMatchObject({ kind: 'ok', text: '图和实际执行一致', items: [] })
    const bad = info({}, {
      verify: { ok: false, mismatches: { edge: 3, end: 1, call: 0 }, functions_seen: 1, distinct_paths: 1, steps_total: 1, plugin_errors: 0, plugin_error_types: {}, truncated_records: 0, stale: ['wf.py'] },
      mismatch_list: [
        { kind: 'edge', function: fsid(run), from_line: 3, to_line: 9, detail: '校验 → 完成', count: 3 },
        { kind: 'end', function: 'gone.py::x', from_line: 4, to_line: 0, detail: '', count: 1 },
      ],
    })
    const v = verifyView(bad, graph)
    expect(v.kind).toBe('bad')
    expect(v.text).toBe('有 4 次实际执行不在图上（2 处）——图不可信')
    expect(v.more).toBe(false)
    expect(v.items[0]).toMatchObject({ functionKey: K.run, function: '转账', where: '第 3 → 9 行', count: 3, kindText: '走了图上没有的连线', detail: '校验 → 完成' })
    expect(v.items[1]).toMatchObject({ functionKey: null, function: 'gone.py::x', where: '第 4 行' })
    expect(v.note).toContain('运行期间代码改过')
    expect(verifyView(info({ status: 'cancelled' }, { verify: null }), graph)).toMatchObject({ kind: 'none' })
    // 后端只列前 30 条：标题按 verify.mismatches 的计数，并说只列了前 30 条
    const many = info({}, {
      verify: { ...bad.summary.verify!, mismatches: { edge: 41, end: 0, call: 2 } },
      mismatch_list: Array.from({ length: 30 }, (_, i) => ({ kind: 'edge', function: fsid(run), from_line: i + 1, to_line: i + 2, detail: '', count: 1 })),
    })
    expect(verifyView(many, graph)).toMatchObject({ text: '有 43 次实际执行不在图上（至少 30 处）——图不可信', more: true })
    // 一条记录都没收到：ok 为 null，不说一致也不报警
    const nothing = info({ status: 'error' }, { verify: { ...info().summary.verify!, ok: null } })
    expect(verifyView(nothing, graph)).toMatchObject({ kind: 'none', text: '' })
  })

  it('重新分析之后：说运行之后代码改过；对不上的处数为 0 时只说前半句；画布上的图比运行用的旧时不这么说', () => {
    expect(staleRunNote(7, 7, 0)).toBeNull()
    expect(staleRunNote(9, 7, 0)).toBeNull()
    expect(staleRunNote(9, 7, 2)).toBe('有 2 处对不上现在的图')
    expect(staleRunNote(7, 9, 0)).toBe('这次运行之后代码改过（重新分析过），按节点标识对到现在的图上')
    expect(staleRunNote(7, 9, 3)).toBe('这次运行之后代码改过（重新分析过），按节点标识对到现在的图上，有 3 处对不上')
    expect(staleRunNote(7, 9, null)).toBe('这次运行之后代码改过（重新分析过），按节点标识对到现在的图上')
    expect(staleRunNote(7, 7, 2)).toBe('有 2 处对不上现在的图')
  })
})

describe('画布上的标注（runMarks）', () => {
  const view = (state = emptyState()) => buildLineView(graph, LINE, state)
  const overlay = buildRunOverlay(graph, paths([okTest]), T_OK, LINE)
  const card = (fnKey: string, v = view()) => v.cards.find((c) => c.fn === fnKey && !c.goto)!

  it('走过的节点带次数、耗时和角上的字；没走到的不在里面；看得见的走过的连线带次数', () => {
    const v = view(setOpen(emptyState(), 'folds', `${card(K.run).id}/fold:cond:n3:yes`, true))
    const m = runMarks(v, graph, overlay, null, new Map(), new Map())
    const id = (fnKey: string, sid: string) => `${card(fnKey, v).id}/${sid}`
    expect(m.active).toBe(true)
    expect(m.nodes.get(id(K.run, 'loop:n5'))).toMatchObject({ visits: 3, badge: '×3 · 3 µs' })
    expect(m.nodes.has(id(K.run, 'end:n4'))).toBe(false)
    const loopEach = v.edges.find((e) => e.src === id(K.run, 'loop:n5') && e.dst === id(K.run, 'step:n6'))!
    expect(m.edges.get(loopEach.id)).toBe(2)
    const yes = v.edges.find((e) => e.dst === id(K.run, 'end:n4'))!
    expect(m.edges.has(yes.id)).toBe(false)
    expect(runNodeClasses(m, id(K.run, 'loop:n5'))).toEqual(['run-walked'])
    expect(runNodeClasses(m, id(K.run, 'end:n4'))).toEqual(['run-dim'])
  })

  it('折叠着的分支 / 卡片里有走过的节点：蓝点；没执行过的任务卡片整体变暗', () => {
    const v = view()
    const notRun = buildRunOverlay(graph, paths([{ ...okTest, functions: { [fsid(run)]: okTest.functions[fsid(run)] } }]), T_OK, LINE)
    const m = runMarks(v, graph, notRun, null, new Map(), new Map())
    const sendCard = card(K.send, v)
    expect(m.cardsRun.has(card(K.run, v).id)).toBe(true)
    expect(m.cardsRun.has(sendCard.id)).toBe(false)
    expect(runNodeClasses(m, sendCard.id, 'card')).toEqual(['run-dim'])
    expect(runNodeClasses(m, card(K.run, v).id, 'card')).toEqual([])
    // 子任务卡片默认收起：跑过时有蓝点
    expect(sendCard.expanded).toBe(false)
    const m2 = runMarks(v, graph, overlay, null, new Map(), new Map())
    expect(m2.inside.has(sendCard.id)).toBe(true)
    expect(runNodeClasses(m2, sendCard.id, 'card')).toEqual(['run-inside'])
    // 折叠着的"拒绝"分支：没走到时变暗，走到了有蓝点；进它的连线（判断 → 占位）按图里的连线算次数
    const fold = v.folds.find((f) => f.keys.includes('n10') && f.collapsed)!
    expect(runNodeClasses(m2, fold.id, 'fold')).toEqual(['run-dim'])
    const rejected = buildRunOverlay(
      graph,
      paths([{ ...okTest, functions: { [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5], ['cond:n3', 1, 5], ['step:n10', 1, 5], ['end:n4', 1, 0]], { 'cond:n3|step:n10': 1, 'step:n10|end:n4': 1 }, { 'end:n4|return|': 1 }) } }]),
      T_OK,
      LINE,
    )
    const m3 = runMarks(v, graph, rejected, null, new Map(), new Map())
    expect(runNodeClasses(m3, fold.id, 'fold')).toEqual(['run-inside'])
    const into = v.edges.find((e) => e.dst === fold.id)!
    expect(m3.edges.get(into.id)).toBe(1)
  })

  it('失败的停点：红圈画在节点上；节点在折叠着的分支里时画在占位上', () => {
    const v = view()
    const stop = { functionKey: K.run, nodeId: 'n4' }
    const m = runMarks(v, graph, overlay, stop, new Map(), new Map())
    const fold = v.folds.find((f) => f.keys.includes('n4') && f.collapsed)!
    expect(m.stops).toEqual(new Set([fold.id]))
    expect(runNodeClasses(m, fold.id, 'fold')).toEqual(['run-dim', 'run-stop'])
    // 展开以后画在节点上
    const open = view(setOpen(emptyState(), 'folds', fold.id, true))
    const m2 = runMarks(open, graph, overlay, stop, new Map(), new Map())
    expect(m2.stops).toEqual(new Set([`${card(K.run, open).id}/end:n4`]))
  })

  it('没选测试时只有没走到的结局：红虚线框（说不准的灰虚线框），折叠着的分支照样标；不变暗', () => {
    const v = view()
    const unc = new Map([[K.run, new Set(['n4'])]])
    const unsure = new Map([[K.run, new Set(['n8'])]])
    const m = runMarks(v, graph, null, null, unc, unsure)
    expect(m.active).toBe(false)
    const target = v.folds.find((f) => f.keys.includes('n4') && f.collapsed)!.id
    expect(m.uncovered.get(target)).toBe('certain')
    expect(m.uncovered.get(`${card(K.run, v).id}/end:n8`)).toBe('uncertain')
    expect(runNodeClasses(m, target, 'fold')).toEqual(['run-uncovered'])
    expect(runNodeClasses(m, `${card(K.run, v).id}/end:n8`)).toEqual(['run-uncertain'])
    expect(runNodeClasses(m, `${card(K.run, v).id}/step:n2`)).toEqual([])
  })

  it('没走到的结局在没钻入 / 钻入不到的被调函数里：标在调用它的节点上，带个数', () => {
    const v = view()
    const runCard = card(K.run, v).id
    // check 能从"校验"钻入（没钻入）；big 是判断节点调用的判断函数（钻入不到）
    const unc = new Map([
      [K.check, new Set(['n3', 'n4'])],
      [K.big, new Set(['n3'])],
    ])
    const unsure = new Map([[K.big, new Set(['n4'])]])
    const m = runMarks(v, graph, null, null, unc, unsure)
    expect(m.uncovered.get(`${runCard}/step:n2`)).toBe('certain')
    expect(m.uncoveredInside.get(`${runCard}/step:n2`)).toEqual({ certain: 2, uncertain: 0 })
    expect(m.uncovered.get(`${runCard}/cond:n3`)).toBe('certain')
    expect(m.uncoveredInside.get(`${runCard}/cond:n3`)).toEqual({ certain: 1, uncertain: 1 })
    expect(uncoveredCountText(m.uncoveredInside.get(`${runCard}/cond:n3`)!, 'callee')).toBe('它调用的函数里有 1 个结局没有测试走到，1 个对不上（代码改过，重新运行测试才知道）')
    expect(uncoveredCountText({ certain: 2, uncertain: 0 }, 'inside')).toBe('里面有 2 个结局没有测试走到')
    expect(uncoveredCountText({ certain: 0, uncertain: 3 }, 'inside')).toBe('里面有 3 个结局对不上（代码改过，重新运行测试才知道）')
    // 只有对不上的：灰框
    const m2 = runMarks(v, graph, null, null, new Map(), unsure)
    expect(m2.uncovered.get(`${runCard}/cond:n3`)).toBe('uncertain')
    // 钻入以后标在钻入卡片里的结局上，不再标调用它的步骤
    const drilled = view(setOpen(emptyState(), 'drills', `${runCard}/step:n2>${check.sid}`, true))
    const m3 = runMarks(drilled, graph, null, null, new Map([[K.check, new Set(['n4'])]]), new Map())
    const drill = `${runCard}/step:n2>${check.sid}`
    expect(m3.uncovered.get(`${drill}/end:n4`)).toBe('certain')
    expect(m3.uncoveredInside.has(`${drill}/end:n4`)).toBe(false)
    expect(m3.uncovered.has(`${runCard}/step:n2`)).toBe(false)
  })

  it('折叠着的分支、收起的卡片里的没走到的结局：也带个数', () => {
    const v = view()
    const fold = v.folds.find((f) => f.keys.includes('n4') && f.collapsed)!
    const m = runMarks(v, graph, null, null, new Map([[K.run, new Set(['n4'])], [K.send, new Set(['n3'])]]), new Map())
    expect(m.uncoveredInside.get(fold.id)).toEqual({ certain: 1, uncertain: 0 })
    expect(m.uncoveredInside.get(card(K.send, v).id)).toEqual({ certain: 1, uncertain: 0 })
  })

  it('没有视图：空的', () => {
    const m = runMarks(null, graph, overlay, null, new Map(), new Map())
    expect(m.nodes.size).toBe(0)
    expect(m.active).toBe(false)
  })
})

describe('运行中的画布标注（liveRunMarks）', () => {
  const view = (state = emptyState()) => buildLineView(graph, LINE, state)
  const card = (fnKey: string, v = view()) => v.cards.find((c) => c.fn === fnKey && !c.goto)!

  it('走到过的节点亮（不带次数、耗时）、别的变暗；执行过的函数的卡片不暗，收起的卡片里有走到过的节点时有蓝点；连线不亮也不暗', () => {
    const v = view()
    const m = liveRunMarks(v, graph, { [K.run]: ['start', 'step:n2', 'cond:n3'], [K.send]: ['start'], 'nope.py::x@1': ['start'], [K.check]: ['nope'] })
    const id = (sid: string) => `${card(K.run, v).id}/${sid}`
    expect(m.active).toBe(true)
    expect(m.live).toBe(true)
    expect(m.nodes.get(id('step:n2'))).toMatchObject({ badge: '' })
    expect([...m.nodes.keys()].sort()).toEqual([id('cond:n3'), id('start'), id('step:n2')].sort())
    expect(runNodeClasses(m, id('cond:n3'))).toEqual(['run-walked'])
    expect(runNodeClasses(m, id('loop:n5'))).toEqual(['run-dim'])
    expect(m.edges.size + m.links.size + m.stops.size + m.uncovered.size).toBe(0)
    const sendCard = card(K.send, v)
    expect(runNodeClasses(m, card(K.run, v).id, 'card')).toEqual([])
    expect(runNodeClasses(m, sendCard.id, 'card')).toEqual(['run-inside'])
    const fold = v.folds.find((f) => f.keys.includes('n10') && f.collapsed)!
    expect(runNodeClasses(m, fold.id, 'fold')).toEqual(['run-dim'])
    const m2 = liveRunMarks(v, graph, { [K.run]: ['start', 'step:n10'] })
    expect(runNodeClasses(m2, fold.id, 'fold')).toEqual(['run-inside'])
    expect(runNodeClasses(m2, sendCard.id, 'card')).toEqual(['run-dim'])
  })

  it('还什么都没走到：全部变暗；没有视图：空的', () => {
    const v = view()
    const m = liveRunMarks(v, graph, {})
    expect(m.nodes.size).toBe(0)
    expect(runNodeClasses(m, card(K.run, v).id, 'card')).toEqual(['run-dim'])
    expect(liveRunMarks(null, graph, { [K.run]: ['start'] }).active).toBe(false)
  })
})

describe('运行中的说明文字', () => {
  const p = (x: Partial<LiveProgress>): LiveProgress => ({
    collected: 925,
    done: 37,
    passed: 36,
    failed: 1,
    skipped: 0,
    errors: 0,
    running: [],
    running_more: 0,
    recent: [],
    lines: {},
    nodes: null,
    ...x,
  })
  const text = (x: Partial<LiveProgress>) =>
    liveCountParts(p(x))
      .map((t) => t.text)
      .join(' · ')

  it('计数："已跑完 37 / 925 个测试 · 通过 36 · 失败 1"；不知道一共几个时只写跑完了几个；跳过、出错有才写；失败、出错标出来', () => {
    expect(text({})).toBe('已跑完 37 / 925 个测试 · 通过 36 · 失败 1')
    expect(text({ collected: null, passed: 37, failed: 0 })).toBe('已跑完 37 个测试 · 通过 37')
    expect(text({ done: 40, passed: 30, failed: 2, skipped: 5, errors: 3 })).toBe('已跑完 40 / 925 个测试 · 通过 30 · 失败 2 · 跳过 5 · 出错 3')
    expect(liveCountParts(p({ errors: 2, done: 39 })).filter((t) => t.bad).map((t) => t.text)).toEqual(['失败 1', '出错 2'])
  })

  it('进度条：跑完的占收集到的比例，失败、出错的一段单独；不知道一共几个时没有；超出时封顶', () => {
    expect(liveBar(p({}))).toEqual({ ok: 36 / 925, bad: 1 / 925 })
    expect(liveBar(p({ collected: null }))).toBeNull()
    expect(liveBar(p({ collected: 0 }))).toBeNull()
    expect(liveBar(p({ collected: 10, done: 12, passed: 11, failed: 1 }))).toEqual({ ok: 11 / 12, bad: 1 / 12 })
  })

  it('正在跑：短名字（去掉文件），另有几个；两个测试之间没有在跑的', () => {
    expect(liveRunningText(p({ running: ['tests/t.py::TestA::test_x'] }))).toBe('正在跑：TestA::test_x')
    expect(liveRunningText(p({ running: ['tests/t.py::a', 'tests/t.py::b', 'tests/t.py::c'], running_more: 2 }))).toBe('正在跑：a（另有 4 个）')
    expect(liveRunningText(p({ running: [] }))).toBeNull()
  })

  it('侧栏功能线上的小标记：已到 m / n 个函数；最近一次刷新时多了的另说一句', () => {
    expect(liveLineTitle({ functions: 3, total: 12 }, false)).toBe('运行中：已到 3 / 12 个函数（已跑完的测试执行过的）')
    expect(liveLineTitle({ functions: 4, total: 12 }, true)).toBe('运行中：已到 4 / 12 个函数（已跑完的测试执行过的），最近一次刷新时又多了')
  })
})

describe('locateOnLine：在功能线上找到一个函数（任务卡片、钻入的路）', () => {
  it('任务函数：没有钻入的路', () => {
    expect(locateOnLine(graph, LINE, K.send, 'n2')).toMatchObject({ task: K.send, path: [], functionKey: K.send, nodeId: 'n2' })
  })

  it('被调用的函数：经过调用它的步骤钻入', () => {
    expect(locateOnLine(graph, LINE, K.post, 'n3')).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n6' }], functionKey: K.post, nodeId: 'n3' })
  })

  it('不在这条线上：null', () => {
    const lone = fn('z.py::lone@1', [['n1', 'start', 'x']], [])
    const g = project([...all, lone], [mainLine])
    expect(locateOnLine(g, LINE, lone.key, null)).toBeNull()
  })
})

describe('pointAlong：连线上次数标在哪', () => {
  it('按长度取折线上的点（中点落在第二段上）', () => {
    const pts = [{ x: 0, y: 0 }, { x: 0, y: 10 }, { x: 30, y: 10 }]
    expect(pointAlong(pts, 0.5)).toEqual({ x: 10, y: 10 })
    expect(pointAlong(pts, 0)).toEqual({ x: 0, y: 0 })
    expect(pointAlong(pts, 1)).toEqual({ x: 30, y: 10 })
  })

  it('只有一个点、长度为 0：就是那个点；没有点：null', () => {
    expect(pointAlong([{ x: 3, y: 4 }], 0.5)).toEqual({ x: 3, y: 4 })
    expect(pointAlong([{ x: 3, y: 4 }, { x: 3, y: 4 }], 0.5)).toEqual({ x: 3, y: 4 })
    expect(pointAlong([], 0.5)).toBeNull()
  })
})

describe('同一对节点之间并行的几条连线', () => {
  // 开始 → 做事 →（next）收尾，同时 做事 →（raise）收尾；做事 →（raise）捕获；捕获 → 失败
  const P = 'par.py::p@1'
  const par = fn(
    P,
    [['n1', 'start', 'p'], ['n2', 'step', '做事'], ['n3', 'step', '收尾'], ['n4', 'catch', '捕获', { subtitle: 'except E' }], ['n5', 'end', '完成', { end: 'ok' }], ['n6', 'end', '失败', { end: 'failed' }], uncaught('n7')],
    [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'next'],
      ['n2', 'n3', 'raise'],
      ['n2', 'n4', 'raise'],
      ['n3', 'n5', 'next'],
      ['n4', 'n6', 'next'],
    ],
  )
  const parLine = line(task(P), [par])
  const g = project([par], [parLine])
  const v = () => {
    let st = emptyState()
    for (let i = 0; i < 5; i++) {
      const lv = buildLineView(g, parLine.sid, st)
      const f = lv.folds.find((x) => x.collapsed)
      if (!f) return lv
      st = setOpen(st, 'folds', f.id, true)
    }
    return buildLineView(g, parLine.sid, st)
  }
  const overlayOf = (exits: Record<string, number>, nodes: [string, number, number][], edges: Record<string, number>) =>
    buildRunOverlay(g, { version: 1, tests: [{ test: 't', outcome: 'passed', seconds: 0, cases: 1, functions: { [par.sid]: fnRun(par, nodes, edges, exits) } }], shapes: { [par.sid]: { shape: functionShape(par), dups: [] } }, function_dups: [] }, 't', parLine.sid)
  const edge = (lv: ReturnType<typeof v>, dst: string, kind: string) => lv.edges.find((e) => e.src.endsWith('/step:n2') && e.dst.endsWith(dst) && e.kind === kind)!

  it('这个函数里没有异常（没有 raise 离开、没进捕获）：只亮普通的那条', () => {
    const lv = v()
    const o = overlayOf({ 'end:n5|return|': 1 }, [['start', 1, 0], ['step:n2', 1, 5], ['step:n3', 1, 5], ['end:n5', 1, 0]], { 'start|step:n2': 1, 'step:n2|step:n3': 1, 'step:n3|end:n5': 1 })
    const m = runMarks(lv, g, o, null, new Map(), new Map())
    expect(m.edges.get(edge(lv, '/step:n3', 'next').id)).toBe(1)
    expect(m.edges.has(edge(lv, '/step:n3', 'raise').id)).toBe(false)
  })

  it('进了捕获节点：进捕获的是 raise 连线', () => {
    const lv = v()
    const o = overlayOf({ 'end:n6|return|': 1 }, [['start', 1, 0], ['step:n2', 1, 5], ['catch:n4', 1, 5], ['end:n6', 1, 0]], { 'start|step:n2': 1, 'step:n2|catch:n4': 1, 'catch:n4|end:n6': 1 })
    const m = runMarks(lv, g, o, null, new Map(), new Map())
    expect(m.edges.get(edge(lv, '/catch:n4', 'raise').id)).toBe(1)
    // 有异常的执行：做事 → 收尾 分不清走的是哪条，两条都亮（宁多勿漏）
    const o2 = overlayOf({ 'end:n6|return|': 1 }, [['start', 1, 0], ['step:n2', 2, 5], ['step:n3', 1, 5], ['catch:n4', 1, 5], ['end:n5', 1, 0], ['end:n6', 1, 0]], { 'step:n2|step:n3': 1, 'step:n2|catch:n4': 1 })
    const m2 = runMarks(lv, g, o2, null, new Map(), new Map())
    expect(m2.edges.get(edge(lv, '/step:n3', 'next').id)).toBe(1)
    expect(m2.edges.get(edge(lv, '/step:n3', 'raise').id)).toBe(1)
  })
})

describe('运行的范围：全量 / 只跑一条线的测试', () => {
  const base: RunInfo = {
    id: 1,
    analysis_id: 1,
    started_at: '',
    finished_at: '',
    command: 'pytest',
    status: 'ok',
    exit_code: 0,
    paths_size: 1,
    summary: { tests: 1, passed: 1, failed: 0, skipped: 0, errors: 0, seconds: 1, verify: null, coverage: null, mismatch_list: [], note: '' },
    scope: null,
  }
  const scoped = (id: number, line: string, title = '转账'): RunInfo => ({ ...base, id, scope: { line, line_title: title, tests: 6, base_run: 1 } })

  it('列表上的标签：全量；这条线的、别的线的写「标题」（界面上改过的标题优先，没有标题时写 sid）', () => {
    expect(runScopeView(base, LINE)).toEqual({ kind: 'full', label: '全量', title: '' })
    expect(runScopeView(scoped(2, LINE), LINE)).toEqual({ kind: 'here', label: '「转账」', title: '转账' })
    expect(runScopeView(scoped(3, 'line:other'), LINE, (sid) => (sid === 'line:other' ? '发通知（改过）' : undefined))).toEqual({
      kind: 'other',
      label: '「发通知（改过）」',
      title: '发通知（改过）',
    })
    expect(runScopeView(scoped(4, 'line:x', ''), LINE).label).toBe('「line:x」')
  })

  it('这条线默认选的运行：最新的全量运行或只跑了这条线的；只跑了别的线的不选（不能当作这条线没有覆盖）', () => {
    const runs = [scoped(9, 'line:other'), scoped(8, LINE), { ...base, id: 7 }]
    expect(defaultRunFor(runs, LINE)).toBe(8)
    expect(defaultRunFor(runs, 'line:third')).toBe(7)
    expect(defaultRunFor([scoped(9, 'line:other')], LINE)).toBeNull()
    expect(defaultRunFor([], LINE)).toBeNull()
    expect(runFitsLine(base, LINE)).toBe(true)
    expect(runFitsLine(scoped(2, LINE), LINE)).toBe(true)
    expect(runFitsLine(scoped(2, 'line:other'), LINE)).toBe(false)
  })

  it('正在跑的是什么：全部 / 这条线的 N 个 / 别的线的；不知道（别处开始的）只说阶段；别的阶段放在括号里', () => {
    const here = { line: LINE, title: '转账', tests: 6 }
    expect(runningText('testing', { line: null, title: '', tests: 0 }, LINE)).toBe('正在跑全部测试')
    expect(runningText('testing', here, LINE)).toBe('正在跑这条线的 6 个测试')
    expect(runningText('testing', here, 'line:other')).toBe('正在跑「转账」的 6 个测试')
    expect(runningText('preparing', here, LINE)).toBe('准备中（这条线的 6 个测试）')
    expect(runningText('analyzing', { line: null, title: '', tests: 0 }, LINE)).toBe('正在先分析（全部测试）')
    expect(runningText('testing', null, LINE)).toBe('正在跑测试')
    expect(runningText('processing', null, LINE)).toBe('正在整理结果')
  })

  it('运行条上运行中的说明：只跑这条线时说"正在跑这条线的 N 个测试"，别的线时说是哪条线的；全量、不知道时照旧', () => {
    expect(liveBarText({ line: LINE, title: '转账', tests: 6 }, LINE)).toMatch(/^正在跑这条线的 6 个测试…亮的是到目前为止走到过的节点/)
    expect(liveBarText({ line: LINE, title: '转账', tests: 6 }, 'line:other')).toMatch(/^正在跑「转账」的 6 个测试…/)
    expect(liveBarText({ line: null, title: '', tests: 0 }, LINE)).toBe(LIVE_BAR_TEXT)
    expect(liveBarText(null, LINE)).toBe(LIVE_BAR_TEXT)
  })

  it('回放正走到的元素：多一个 run-current', () => {
    const m = { ...liveRunMarks(null, null, {}), active: true, current: 'a' }
    expect(runNodeClasses(m, 'a')).toEqual(['run-dim', 'run-current'])
    expect(runNodeClasses(m, 'b')).toEqual(['run-dim'])
  })
})

describe('revealWalkedState：把一个测试走过的地方都展开', () => {
  const walked = buildRunOverlay(graph, paths([okTest]), T_OK, LINE)

  it('执行过的卡片展开、有走过节点的折叠分支展开、调用了执行过的函数的步骤钻进去；展开到没有可展开的为止', () => {
    const before = buildLineView(graph, LINE, emptyState())
    const r = revealWalkedState(graph, LINE, walked, emptyState())
    expect(r.opened).toBeGreaterThan(0)
    expect(r.more).toBe(false)
    const after = buildLineView(graph, LINE, r.state)
    // 展开之后没有哪张卡片、哪个折叠占位里还藏着走过的节点
    expect([...runMarks(after, graph, walked, null, new Map(), new Map()).inside]).toEqual([])
    // 执行过的被调函数都钻进去了：每个有钻入选项、选项里有执行过的函数的步骤都开着一个
    for (const n of after.nodes) {
      if (n.drills.some((d) => walked.functionsRun.has(d.fn))) expect(n.drills.some((d) => d.open)).toBe(true)
    }
    expect(after.nodes.length).toBeGreaterThan(before.nodes.length)
    // 再来一次：已经都展开了，不用动，状态还是同一个
    const again = revealWalkedState(graph, LINE, walked, r.state)
    expect(again.opened).toBe(0)
    expect(again.state).toBe(r.state)
  })

  it('测试只执行了调用链深处的函数时：顺着没执行的调用方一路钻进去，把执行过的那个露出来', () => {
    // a（入口，没执行）→ b（没执行）→ c（执行过）；d 和这个测试无关
    const simple = (key: string, targets: string[]) =>
      fn(
        key,
        [['n1', 'start'], ...targets.map((t, i): [string, 'step', string, { calls: ReturnType<typeof call>[] }] => [`n${i + 2}`, 'step', `调用 ${t}`, { calls: [call(t)] }]), [`n${targets.length + 2}`, 'end', '好了', { end: 'ok' }]],
        Array.from({ length: targets.length + 1 }, (_, i): [string, string, 'next'] => [`n${i + 1}`, `n${i + 2}`, 'next']),
        { call_targets: targets },
      )
    const c = simple('m.py::c@30', [])
    const d = simple('m.py::d@40', [])
    const b = simple('m.py::b@20', [c.key, d.key])
    const a = simple('m.py::a@1', [b.key])
    const ln = line(task(a.key), [a, b, c, d])
    const g = project([a, b, c, d], [ln])
    const deep = { ...emptyOverlay(), test: 't', functionsRun: new Set([c.key]), nodes: new Map([[c.key, new Map()]]) }
    expect([...leadsToRun(g, deep.functionsRun)].sort()).toEqual([a.key, b.key, c.key].sort())
    const r = revealWalkedState(g, ln.sid, deep, emptyState())
    const fns = new Set(buildLineView(g, ln.sid, r.state).cards.map((card) => card.fn))
    expect(fns.has(b.key) && fns.has(c.key)).toBe(true) // 钻进了 b，又从 b 钻进了 c
    expect(fns.has(d.key)).toBe(false) // 不通向执行过的函数的不钻
    expect(r.opened).toBe(2)
    // 什么都没执行过：一个都不钻
    expect(revealWalkedState(g, ln.sid, { ...deep, functionsRun: new Set<string>() }, emptyState()).opened).toBe(0)
  })

  it('已经展开、已经钻入的照旧开着（不收起任何东西）；什么都没走过时不展开', () => {
    const nothing = revealWalkedState(graph, LINE, emptyOverlay(), emptyState())
    expect(nothing).toEqual({ state: emptyState(), opened: 0, more: false })
    const view = buildLineView(graph, LINE, emptyState())
    const step = view.nodes.find((n) => n.drills.length > 0)
    if (step) {
      const mine = setOpen(emptyState(), 'drills', step.drills[step.drills.length - 1].id, true)
      const r = revealWalkedState(graph, LINE, walked, mine)
      expect(r.state.drills[step.drills[step.drills.length - 1].id]).toBe(true)
      expect(Object.entries(r.state.drills).filter(([id, open]) => open && id.startsWith(`${step.id}>`))).toHaveLength(1)
    }
  })
})


// 钻入链（一个步骤同时钻入它调用的几个函数，见 graph/fixtures/chain.ts）：运行路径的标注落在链上对的那张卡片上
describe('钻入链上的运行标注', () => {
  const n2 = at('step:n2')
  const open = (...ids: string[]): ViewState => ids.reduce((acc, id) => setOpen(acc, 'drills', id, true), emptyState())
  const view = (st: ViewState) => buildLineView(chainProject, CL, st)
  const T = 'tests/test_m.py::test_x'
  /** 只执行了 main、b、c（a、d 没执行；a 调用了 c） */
  const bcTest: TestRunJson = {
    test: T,
    outcome: 'passed',
    seconds: 0.01,
    cases: 1,
    functions: {
      [fsid(mainFn)]: fnRun(mainFn, [['start', 1, 0], ['step:n2', 1, 10], ['step:n3', 1, 10], ['cond:n4', 1, 1], ['end:n7', 1, 0]], { 'start|step:n2': 1, 'step:n2|step:n3': 1, 'step:n3|cond:n4': 1, 'cond:n4|end:n7': 1 }, { 'end:n7|return|': 1 }),
      [fsid(bFn)]: fnRun(bFn, [['start', 1, 0], ['cond:n2', 1, 1], ['end:n4', 1, 0]], { 'start|cond:n2': 1, 'cond:n2|end:n4': 1 }, { 'end:n4|return|': 1 }),
      [fsid(cFn)]: fnRun(cFn, [['start', 1, 0], ['step:n2', 1, 1], ['end:n3', 1, 0]], { 'start|step:n2': 1, 'step:n2|end:n3': 1 }, { 'end:n3|return|': 1 }),
    },
  }
  const overlay = buildRunOverlay(chainProject, paths([bcTest], chainFns), T, CL)

  it('走过的：每张卡片按它自己的函数亮（a 没执行就暗着），连接线不算走过', () => {
    const v = view(open(into(n2, CK.a), into(n2, CK.b)))
    const m = runMarks(v, chainProject, overlay, null, new Map(), new Map())
    expect(m.cardsRun.has(into(n2, CK.b))).toBe(true)
    expect(m.cardsRun.has(into(n2, CK.a))).toBe(false)
    expect(runNodeClasses(m, into(n2, CK.a), 'card')).toEqual(['run-dim'])
    expect(m.nodes.has(at('cond:n2', into(n2, CK.b)))).toBe(true)
    const connector = v.edges.find((e) => e.chain)!
    expect(m.edges.has(connector.id)).toBe(false)
    // 进链的连线（start → n2）、出链的（n2 → n3）照旧按图里的那条算
    expect(m.edges.get(v.edges.find((e) => e.dst === into(n2, CK.a))!.id)).toBe(1)
    expect(m.edges.get(v.edges.find((e) => e.src === into(n2, CK.b) && !e.chain)!.id)).toBe(1)
  })

  it('失败的停点在第二个被调函数里：红圈在第二张卡片里的那个节点上，不在第一张上', () => {
    const v = view(open(into(n2, CK.a), into(n2, CK.b)))
    const m = runMarks(v, chainProject, overlay, { functionKey: CK.b, nodeId: 'n4' }, new Map(), new Map())
    expect([...m.stops]).toEqual([at('end:n4', into(n2, CK.b))])
  })

  it('红圈画在换成了钻入链的步骤上：按抛出异常的函数挑——链上它的那张；没展开时是它的钻入选项；链上能调用到它的那张；都不是时第一张', () => {
    const ring = (st: ViewState, raisedIn: string | null) =>
      [...runMarks(view(st), chainProject, overlay, { functionKey: CK.main, nodeId: 'n2', raisedIn: raisedIn ? { functionKey: raisedIn } : null }, new Map(), new Map()).stops]
    expect(ring(open(into(n2, CK.a), into(n2, CK.b)), CK.b)).toEqual([into(n2, CK.b)])
    expect(ring(open(into(n2, CK.a)), CK.b)).toEqual([into(n2, CK.b)]) // 钻入选项（画在"再展开"上），不在 a 的卡片上
    expect(ring(open(into(n2, CK.b), into(n2, CK.a)), CK.d)).toEqual([into(n2, CK.a)]) // a 调用 d
    expect(ring(open(into(n2, CK.b)), 'm.py::elsewhere@1')).toEqual([into(n2, CK.b)])
    expect(ring(open(into(n2, CK.b)), null)).toEqual([into(n2, CK.b)])
    expect(ring(emptyState(), CK.b)).toEqual([n2])
  })

  it('没走到的结局在没展开的被调函数里：标在它的钻入选项上（带个数），不标到链上别的卡片；展开后标在它自己的卡片里', () => {
    const unc = new Map([[CK.b, new Set(['n3'])]])
    const partial = runMarks(view(open(into(n2, CK.a))), chainProject, null, null, unc, new Map())
    expect(partial.uncovered.get(into(n2, CK.b))).toBe('certain')
    expect(partial.uncoveredInside.get(into(n2, CK.b))).toEqual({ certain: 1, uncertain: 0 })
    expect(partial.uncovered.has(into(n2, CK.a))).toBe(false)
    expect(partial.uncoveredInside.has(into(n2, CK.a))).toBe(false)
    // 别的调用 b 的步骤（n3 没钻入）照旧标在步骤上
    expect(partial.uncoveredInside.get(at('step:n3'))).toEqual({ certain: 1, uncertain: 0 })
    const both = runMarks(view(open(into(n2, CK.a), into(n2, CK.b))), chainProject, null, null, unc, new Map())
    expect(both.uncovered.get(at('end:n3', into(n2, CK.b)))).toBe('certain')
    expect(both.uncoveredInside.has(at('end:n3', into(n2, CK.b)))).toBe(false)
    expect(both.uncovered.has(into(n2, CK.b))).toBe(false)
  })

  it('选了测试自动展开：一个步骤调用的执行过的、和通向执行过的函数的都钻入（排成链），通不到的不钻', () => {
    const r = revealWalkedState(chainProject, CL, overlay, emptyState())
    expect(r.more).toBe(false)
    const v = view(r.state)
    const fns = (step: string) => drillChainOf(v, step).map((c) => c.fn)
    expect(fns(n2)).toEqual([CK.a, CK.b]) // a 没执行，但调用了执行过的 c
    expect(fns(at('step:n3'))).toEqual([CK.a, CK.b, CK.c])
    // a 的卡片里：c 执行过，d 没执行、也不通向执行过的
    expect(fns(at('step:n2', into(n2, CK.a)))).toEqual([CK.c])
    expect(drillChainOf(v, at('step:n2', into(n2, CK.a)))[0].anchor!.drills.find((d) => d.fn === CK.d)!.open).toBe(false)
    // 已经钻入了一个（用户开的 b）：照旧开着，通向执行过的函数的 a 补上
    const mine = open(into(n2, CK.b))
    const r2 = revealWalkedState(chainProject, CL, overlay, mine)
    expect(r2.state.drills[into(n2, CK.b)]).toBe(true)
    expect(drillChainOf(view(r2.state), n2).map((c) => c.fn)).toEqual([CK.a, CK.b])
    // 再来一次：不用动
    expect(revealWalkedState(chainProject, CL, overlay, r.state)).toMatchObject({ opened: 0, state: r.state })
  })
})
