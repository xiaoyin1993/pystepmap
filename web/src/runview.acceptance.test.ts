// 阶段 6 验收：一个真实运行着的 pystepmap serve（临时数据目录、示例项目或验收项目），经前端自己的接口客户端读运行和
// 每个测试的运行路径，用 runview 叠到最新的图上，和后端（core）算的对一遍：
// - 运行用的就是最新一次分析时，每个测试的路径全部对上（unmatched 为 0）；
// - 每个测试走过这条线上几个函数（runview 放上去的函数 ∩ 这条线的函数）和后端的 on_line 一样；
// - 所有测试走到的结局节点（runview：走过的节点和停点）之外的，正是后端 /uncovered 里确定没走到的那些；
// - 失败的测试：异常从这条线抛出去了时，停点（或钻入不到时调用它的节点）能定位过去，红圈落在定位到的元素上；没抛出去
//   时说测试是在自己的断言里失败的。
// 每个测试的运行路径只取一次（GET …/paths?test=），再在每条线上用：真实项目有几百个测试、几十条线，每条线都重取一遍
// 就是几十万个请求。
//
// 只在设了 PYSTEPMAP_TEST_API（例如 http://127.0.0.1:5395/api）时跑；PYSTEPMAP_TEST_PROJECT 指定项目 id（不设时取第一个），
// PYSTEPMAP_TEST_RUN 指定运行 id（不设时取最新一次有路径的）。不改后端的任何东西（只发 GET）。

import { describe, expect, it } from 'vitest'
import { createApi, type RunPathsJson } from './api'
import { revealStop } from './diffview'
import { buildLineView, emptyState } from './graph'
import { failureBarText, failureStop } from './runfailure'
import { buildRunOverlay, isFailedOutcome, lineFunctions, runMarks, uncoveredMarks } from './runview'

const BASE = process.env.PYSTEPMAP_TEST_API ?? ''

describe.skipIf(!BASE)('运行路径叠到真实后端的图上', () => {
  it('每个测试全部对上；on_line、没走到的结局和后端一致', async () => {
    const api = createApi((input, init) => globalThis.fetch(input, init), BASE)
    const projects = await api.listProjects()
    const pid = process.env.PYSTEPMAP_TEST_PROJECT || projects[0]?.id
    expect(pid, '后端里没有项目').toBeTruthy()
    const { graph, analysisId } = await api.getGraph(pid!)
    const { runs } = await api.listRuns(pid!)
    const want = Number(process.env.PYSTEPMAP_TEST_RUN || 0)
    const run = want ? runs.find((r) => r.id === want) : runs.find((r) => r.paths_size !== null)
    expect(run, '没有带路径的运行').toBeTruthy()
    const sameAnalysis = run!.analysis_id === analysisId
    const report: string[] = [`项目 ${pid}，运行 #${run!.id}（分析 #${run!.analysis_id}，图是分析 #${analysisId}），${graph.lines.length} 条功能线`]
    // 每个测试的运行路径取一次
    const slices = new Map<string, RunPathsJson>()
    for (const row of (await api.getRun(pid!, run!.id)).tests) slices.set(row.test, (await api.getTestPaths(pid!, run!.id, row.test)).paths)
    for (const line of graph.lines) {
      const detail = await api.getRun(pid!, run!.id, line.sid)
      expect(detail.line_known).toBe(true)
      const fns = new Set(lineFunctions(graph, line))
      const covered = new Map<string, Set<string>>()
      let unmatched = 0
      let mismatchOnLine = 0
      const stops: string[] = []
      for (const row of detail.tests) {
        const paths = slices.get(row.test)
        expect(paths, `${row.test} 的运行路径`).toBeTruthy()
        const o = buildRunOverlay(graph, paths!, row.test, line.sid)
        unmatched += o.unmatched
        if (sameAnalysis) expect(o.unmatched, `${row.test}：对不上的处数`).toBe(0)
        const onLine = [...o.functionsRun].filter((k) => fns.has(k)).length
        if (onLine !== row.on_line) mismatchOnLine++
        expect(onLine, `${row.test} 在 ${line.sid} 上走过的函数个数`).toBe(row.on_line)
        if (isFailedOutcome(row.outcome)) {
          const f = failureStop(o, graph, line.sid)
          const reveal = f?.kind === 'stop' ? f.reveal : f?.kind === 'direct' ? f.reveal : null
          if (f && reveal && (f.kind === 'stop' || f.kind === 'direct')) {
            // 画布能定位过去：先按 revealStop 展开 / 钻入，红圈落在那时看得见的元素上
            const r = revealStop(graph, line.sid, emptyState(), reveal)
            expect(r, `${row.test} 的停点能定位`).not.toBeNull()
            const marks = runMarks(buildLineView(graph, line.sid, r!.state), graph, o, f, new Map(), new Map())
            expect(marks.stops.has(r!.target), `${row.test} 的红圈在定位到的元素上`).toBe(true)
            const path = reveal.path.map((p) => {
              const n = graph.functions[p.fn].nodes.find((x) => x.id === p.node)!
              return `${graph.functions[p.fn].qualname}「${n.title}」第 ${n.line} 行`
            })
            stops.push(`${row.test}：${failureBarText(f, graph)}（${f.kind}，钻入 ${path.join(' → ') || '无'}）`)
          } else stops.push(`${row.test}：${f ? failureBarText(f, graph) : '没有停点'}（${f?.kind ?? '-'}，不画红圈）`)
        }
        for (const [key, nodes] of o.nodes) {
          const set = covered.get(key) ?? new Set<string>()
          for (const id of nodes.keys()) set.add(id)
          covered.set(key, set)
        }
        for (const s of o.stops) {
          const set = covered.get(s.functionKey) ?? new Set<string>()
          set.add(s.nodeId)
          covered.set(s.functionKey, set)
        }
      }
      // 没走到的结局：这条线涉及的函数里，不是"未捕获异常退出"的结局节点、没有任何测试走到的
      const mine = new Map<string, Set<string>>()
      for (const key of fns) {
        for (const n of graph.functions[key].nodes) {
          if (n.type !== 'end' || n.end_kind === 'exception' || covered.get(key)?.has(n.id)) continue
          const set = mine.get(key) ?? new Set<string>()
          set.add(n.id)
          mine.set(key, set)
        }
      }
      const theirs = await api.getUncovered(pid!, run!.id, line.sid)
      const certain = uncoveredMarks(graph, theirs.outcomes.filter((x) => !x.uncertain))
      const flat = (m: Map<string, Set<string>>) => [...m].flatMap(([k, ids]) => [...ids].map((id) => `${k}#${id}`)).sort()
      if (sameAnalysis) {
        expect(theirs.outcomes.filter((x) => x.uncertain)).toEqual([])
        expect(flat(mine)).toEqual(flat(certain))
      }
      report.push(
        `  ${line.sid}：${detail.tests.length} 个测试（走过这条线的 ${detail.tests.filter((t) => (t.on_line ?? 0) > 0).length}），` +
          `对不上 ${unmatched} 处，on_line 不一致 ${mismatchOnLine} 个，没走到的结局 ${theirs.outcomes.length} 个（runview 算出 ${flat(mine).length} 个）`,
        ...stops.map((x) => `    失败：${x}`),
      )
    }
    console.log(report.join('\n'))
  }, 600_000)
})
