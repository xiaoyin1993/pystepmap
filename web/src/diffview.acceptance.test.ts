// 阶段 5 验收：真实后端存下来的图和流程差异，经前端自己的接口客户端读进来，
// 画布上标出来的正是 GET /diff 里的那些节点。
//
// 数据不进仓库：环境变量 PYSTEPMAP_TEST_DIFFS 指向一个目录，里面每个子目录是一次重新分析之后的
//   graph.json    GET /api/projects/<id>/graph 的响应体（graph.headers 里有 x-pystepmap-analysis 时一起用）
//   diff.json     GET /api/projects/<id>/diff 的响应体
//   expect.json   （可选）这一次人工核对过的预期：{line, function, stops?, added?, removed_at?, callee_marked?}
// 没设这个变量时整组跳过。生成方法见验收文档第 3 节。

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { createApi, type GraphDiffJson } from './api'
import { buildDiffView, lineChangeList, revealStop, viewMarks } from './diffview'
import { buildLineView, emptyState, type GraphNode, type ProjectGraph } from './graph'

interface Expect {
  line: string
  function: string
  stops?: number
  added?: { type: string; title: string }[]
  removed_at?: { type: string; title: string; label: string; count: number }
  callee_marked?: { type: string; title: string }[]
}

const DIR = process.env.PYSTEPMAP_TEST_DIFFS ?? ''
const CASES: string[] = DIR
  ? readdirSync(DIR)
      .filter((d) => statSync(join(DIR, d)).isDirectory() && existsSync(join(DIR, d, 'diff.json')))
      .sort()
  : []

/** 和界面一样经 createApi 读：假的 fetch 把存下来的响应体原样交回去 */
async function load(dir: string) {
  const graphText = readFileSync(join(dir, 'graph.json'))
  const diffText = readFileSync(join(dir, 'diff.json'), 'utf8')
  const headerFile = join(dir, 'graph.headers')
  const analysis = existsSync(headerFile) ? /x-pystepmap-analysis:\s*(\d+)/i.exec(readFileSync(headerFile, 'utf8'))?.[1] : undefined
  const api = createApi(async (input) => {
    if (input.endsWith('/graph')) {
      return new Response(graphText, {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'X-Pystepmap-Graph-Size': String(graphText.length), ...(analysis ? { 'X-Pystepmap-Analysis': analysis } : {}) },
      })
    }
    if (input.endsWith('/diff')) return new Response(diffText, { status: 200, headers: { 'Content-Type': 'application/json' } })
    return new Response('{"error":"没有这个接口"}', { status: 404 })
  })
  const { graph, analysisId } = await api.getGraph('p')
  const result = await api.getDiff('p')
  const expectFile = join(dir, 'expect.json')
  const want: Expect | null = existsSync(expectFile) ? JSON.parse(readFileSync(expectFile, 'utf8')) : null
  return { graph, analysisId, result, want }
}

const fnKeyOf = (graph: ProjectGraph, sid: string): string | undefined => Object.values(graph.functions).find((f) => f.sid === sid)?.key

const nodeTitled = (graph: ProjectGraph, fnKey: string, type: string, title: string): GraphNode | undefined =>
  graph.functions[fnKey]?.nodes.find((n) => n.type === type && (n.title_full || n.title) === title)

describe.skipIf(CASES.length === 0)('验收：真实后端的流程差异在画布上标在同一处（PYSTEPMAP_TEST_DIFFS）', () => {
  for (const name of CASES) {
    it(name, async () => {
      const { graph, analysisId, result, want } = await load(join(DIR, name))
      const diff = result.diff as GraphDiffJson
      expect(diff, '这一次分析存着流程差异').not.toBeNull()
      if (analysisId !== null) expect(result.head.id, '图和差异是同一次分析').toBe(analysisId)
      const dv = buildDiffView(graph, diff)

      // GET /diff 里每一个新增 / 改动的节点：buildDiffView 按同一个节点 id 标出同样的状态
      let checked = 0
      for (const [sid, fc] of Object.entries(diff.functions)) {
        if (fc.status === 'removed') continue
        const key = fnKeyOf(graph, sid)
        expect(key, `图里有函数 ${sid}`).toBeDefined()
        for (const c of fc.nodes) {
          if (c.status === 'removed') {
            const anchor = graph.functions[key!].nodes.find((n) => n.id === c.after)
            if (!anchor || anchor.type === 'end') continue
            const items = dv.removed.get(key!) ?? []
            expect(items.some((r) => r.what === 'node' && r.title === c.old_title && r.after === c.after && r.label === c.after_label)).toBe(true)
            checked++
            continue
          }
          expect(dv.nodeMarks.get(key!)?.get(c.new_id), `${sid} ${c.new_id}（${c.new_title}）`).toBe(c.status)
          checked++
        }
      }
      expect(checked).toBeGreaterThan(0)

      // 每条有变化的线：差异栏的每一处都能定位；新增 / 改动的那几处定位后，画布上那个元素带着同样的标记
      for (const lc of diff.lines) {
        if (lc.status === 'removed') continue
        expect(dv.lineStatus.get(lc.sid)).toBe(lc.status)
        const stops = lineChangeList(dv, graph, lc.sid)
        expect(stops.length).toBeGreaterThan(0)
        for (const stop of stops) {
          const r = revealStop(graph, lc.sid, emptyState(), stop)
          expect(r, stop.text).not.toBeNull()
          if (stop.kind === 'removed' || r!.kind !== 'node') continue
          const view = buildLineView(graph, lc.sid, r!.state)
          expect(viewMarks(view, dv, graph, stops).nodes.get(r!.target), stop.text).toBe(stop.kind)
        }
      }

      if (!want) return
      const key = fnKeyOf(graph, want.function)!
      expect(key).toBeDefined()
      const stops = lineChangeList(dv, graph, want.line)
      if (want.stops !== undefined) expect(stops.map((s) => s.text)).toHaveLength(want.stops)
      for (const a of want.added ?? []) {
        const c = diff.functions[want.function]?.nodes.find((x) => x.status === 'added' && x.type === a.type && x.new_title === a.title)
        expect(c, `GET /diff 里有新增的「${a.title}」`).toBeDefined()
        expect(nodeTitled(graph, key, a.type, a.title)?.id).toBe(c!.new_id)
        expect(dv.nodeMarks.get(key)?.get(c!.new_id)).toBe('added')
        expect(stops.some((s) => s.kind === 'added' && s.nodeId === c!.new_id)).toBe(true)
      }
      if (want.removed_at) {
        const w = want.removed_at
        const anchor = nodeTitled(graph, key, w.type, w.title)
        expect(anchor, `图里有「${w.title}」`).toBeDefined()
        const items = (dv.removed.get(key) ?? []).filter((r) => r.what === 'node')
        expect(items.filter((r) => r.after === anchor!.id && r.label === w.label)).toHaveLength(w.count)
        const removedStops = stops.filter((s) => s.kind === 'removed')
        expect(removedStops).toHaveLength(w.count)
        for (const s of removedStops) expect([s.nodeId, s.where]).toEqual([anchor!.id, `原来在「${w.title}」之后（${w.label}）`])
      }
      for (const c of want.callee_marked ?? []) {
        const n = nodeTitled(graph, key, c.type, c.title)
        expect(n, `图里有「${c.title}」`).toBeDefined()
        expect(dv.calleeMarked.get(key)?.has(n!.id)).toBe(true)
        expect(dv.nodeMarks.get(key)?.get(n!.id)).toBe('changed')
        expect(stops.some((s) => s.kind === 'changed' && s.nodeId === n!.id)).toBe(true)
      }
    })
  }
})
