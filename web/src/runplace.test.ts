// runplace.ts：叠到图上（照 core 的 locate_function、match_function_run、locate_test_run）和入口函数（entry_functions）。
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { FunctionRunJson, RunPathsJson, TestRunJson } from './api'
import type { FunctionGraph, NodeType, ProjectGraph } from './graph'
import { fn, project, uncaught } from './graph/fixtures/builders'
import { K, fnRun, fsid, okTest, paths, post, run } from './graph/fixtures/runs'
import { TRIVIAL_SHAPES, baseSid, entryFunctions, functionShape, locateTestRun, sha1Hex } from './runplace'
import { buildRunOverlay } from './runview'

const sha1 = (s: string) => createHash('sha1').update(s, 'utf8').digest('hex')
const shapeOf = (f: FunctionGraph) => sha1(f.nodes.map((n) => n.sid || n.id).join('\n'))

describe('sha1Hex、函数形状', () => {
  it('和 node 的 sha1 一样（空串、ASCII、中文、跨好几块的长串）', () => {
    for (const s of ['', 'abc', 'start\nstep:abc#2\nend:uncaught', '转账：校验金额', 'x'.repeat(1000), '一'.repeat(300)]) {
      expect(sha1Hex(s)).toBe(sha1(s))
    }
  })

  it('functionShape 是节点 sid（没有时用节点 id）按图里的顺序用换行连起来的 sha1', () => {
    expect(functionShape(run)).toBe(shapeOf(run))
    const old = { ...post, nodes: post.nodes.map((n) => ({ ...n, sid: '' })) }
    expect(functionShape(old)).toBe(sha1('n1\nn2\nn3\nn4'))
  })

  it('TRIVIAL_SHAPES：只有隐含节点的四种形状', () => {
    const want = [['start'], ['start', 'end:implicit'], ['start', 'end:uncaught'], ['start', 'end:implicit', 'end:uncaught']].map((s) => sha1(s.join('\n')))
    expect([...TRIVIAL_SHAPES].sort()).toEqual(want.sort())
  })

  it('baseSid 去掉结尾的 #数字', () => {
    expect(baseSid('a.py::f#2')).toBe('a.py::f')
    expect(baseSid('end:abc#12')).toBe('end:abc')
    expect(baseSid('end:abc#')).toBe('end:abc#')
    expect(baseSid('a#b')).toBe('a#b')
    expect(baseSid('x')).toBe('x')
  })
})

describe('#n 的 sid', () => {
  // 两个同文本的结局：end:dup、end:dup#2
  const dupFn = fn(
    'd.py::f@1',
    [['n1', 'start', 'f'], ['n2', 'cond', '是不是？'], ['n3', 'end', '返回', { end: 'ok', sid: 'end:dup' }], ['n4', 'end', '返回', { end: 'ok', sid: 'end:dup#2' }], uncaught('n5')],
    [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'yes'],
      ['n2', 'n4', 'no'],
    ],
  )
  const t = (f: FunctionGraph): TestRunJson => ({
    test: 't',
    outcome: 'passed',
    seconds: 0,
    cases: 1,
    functions: { [f.sid]: fnRun(f, [['start', 1, 0], ['cond:n2', 1, 5], ['end:dup#2', 1, 0]], { 'start|cond:n2': 1, 'cond:n2|end:dup#2': 1 }, { 'end:dup#2|return|': 1 }) },
  })
  const stored = (f: FunctionGraph) => ({ [f.sid]: { shape: shapeOf(f), dups: ['end:dup'] } })

  it('形状没变：#2 按相等配上', () => {
    const g = project([dupFn])
    const o = buildRunOverlay(g, { version: 1, tests: [t(dupFn)], shapes: stored(dupFn), function_dups: [] }, 't', null)
    expect(o.unmatched).toBe(0)
    expect(o.nodes.get(dupFn.key)!.has('n4')).toBe(true)
    expect(o.edges.get(dupFn.key)!.get('n2|n4')).toBe(1)
  })

  it('形状变了（前面插了一个同文本的结局）：#2 配不上、不落到别的节点上，其余照配', () => {
    const now = fn(
      'd.py::f@1',
      [
        ['n1', 'start', 'f'],
        ['n2', 'cond', '是不是？'],
        ['n3', 'end', '返回', { end: 'ok', sid: 'end:dup' }],
        ['n4', 'end', '返回', { end: 'ok', sid: 'end:dup#2' }],
        ['n5', 'end', '返回', { end: 'ok', sid: 'end:dup#3' }],
        uncaught('n6'),
      ],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'yes'], ['n2', 'n4', 'no']],
    )
    const g = project([now])
    const o = buildRunOverlay(g, { version: 1, tests: [t(dupFn)], shapes: stored(dupFn), function_dups: [] }, 't', null)
    expect(o.unmatched).toBe(1)
    expect([...o.nodes.get(now.key)!.keys()].sort()).toEqual(['n1', 'n2'])
    expect(o.edges.get(now.key)!.size).toBe(1)
    expect(o.stops).toEqual([])
  })

  it('没有 shapes 的旧数据：不重名的照配，重名的配不上', () => {
    const g = project([dupFn])
    const o = buildRunOverlay(g, { version: 1, tests: [t(dupFn)], shapes: {}, function_dups: [] }, 't', null)
    expect(o.unmatched).toBe(1)
    expect([...o.nodes.get(dupFn.key)!.keys()].sort()).toEqual(['n1', 'n2'])
  })
})

describe('locateTestRun：同名函数（函数 sid 的 #n）', () => {
  // 两个定义：v（空形状的桩）和 v#2（真正的实现）
  const stub = fn('p.py::C.v@1', [['n1', 'start', 'v'], ['n2', 'end', '执行到函数末尾', { sid: 'end:implicit', line: 0, end: 'ok' }]], [['n1', 'n2', 'next']], { sid: 'p.py::C.v' })
  const impl = (extra: string, key = 'p.py::C.v@10', sid = 'p.py::C.v#2') =>
    fn(key, [['n1', 'start', 'v'], ['n2', 'step', `做事${extra}`, { sid: `step:do${extra}` }], ['n3', 'end', '返回', { end: 'ok' }]], [['n1', 'n2', 'next'], ['n2', 'n3', 'next']], { sid })
  const implRun = (f: FunctionGraph, sid: string): FunctionRunJson => ({ ...fnRun(f, [['start', 1, 0], [f.nodes[1].sid, 1, 9], ['end:n3', 1, 0]], { [`start|${f.nodes[1].sid}`]: 1 }, { 'end:n3|return|': 1 }), function: sid })
  const thenStub = stub
  const thenImpl = impl('')
  const shapes = { [thenStub.sid]: { shape: shapeOf(thenStub), dups: [] }, [thenImpl.sid]: { shape: shapeOf(thenImpl), dups: [] } }
  const runPaths = (functions: Record<string, FunctionRunJson>): RunPathsJson => ({
    version: 1,
    tests: [{ test: 't', outcome: 'passed', seconds: 0, cases: 1, functions }],
    shapes,
    function_dups: ['p.py::C.v'],
  })

  it('同一张图：这一组没变，按相等放上', () => {
    const g = project([thenStub, thenImpl])
    const r = locateTestRun(g, runPaths({ [thenImpl.sid]: implRun(thenImpl, thenImpl.sid) }), 't')
    expect(r.unmatched).toBe(0)
    expect([...r.placed.keys()]).toEqual([thenImpl.key])
  })

  it('前面又插了一个桩：实现变成 #3，按形状找回', () => {
    const stub2 = { ...stub, key: 'p.py::C.v@5', sid: 'p.py::C.v#2' }
    const nowImpl = impl('', 'p.py::C.v@20', 'p.py::C.v#3')
    const g = project([stub, stub2, nowImpl])
    const r = locateTestRun(g, runPaths({ [thenImpl.sid]: implRun(thenImpl, thenImpl.sid) }), 't')
    expect(r.unmatched).toBe(0)
    expect([...r.placed.keys()]).toEqual([nowImpl.key])
    expect(r.placed.get(nowImpl.key)!.mapping.get('step:do')).toBe('n2')
  })

  it('跑的是空形状的桩、这一组变了：放不上（不按形状找到别的空函数上）', () => {
    const stub2 = { ...stub, key: 'p.py::C.v@5', sid: 'p.py::C.v#2' }
    const nowImpl = impl('', 'p.py::C.v@20', 'p.py::C.v#3')
    const g = project([stub, stub2, nowImpl])
    const stubRun: FunctionRunJson = { ...fnRun(stub, [['start', 1, 0], ['end:implicit', 1, 0]], { 'start|end:implicit': 1 }, { 'end:implicit|return|': 1 }), function: stub.sid }
    const r = locateTestRun(g, runPaths({ [stub.sid]: stubRun }), 't')
    expect(r.placed.size).toBe(0)
    expect(r.unmatched).toBe(1)
  })

  it('运行时这一组里另有同形状的定义（双胞胎）：放不上', () => {
    const twin = impl('', 'p.py::C.v@1', 'p.py::C.v')
    const twin2 = impl('', 'p.py::C.v@10', 'p.py::C.v#2')
    const p: RunPathsJson = {
      version: 1,
      tests: [{ test: 't', outcome: 'passed', seconds: 0, cases: 1, functions: { [twin2.sid]: implRun(twin2, twin2.sid) } }],
      shapes: { [twin.sid]: { shape: shapeOf(twin), dups: [] }, [twin2.sid]: { shape: shapeOf(twin2), dups: [] } },
      function_dups: ['p.py::C.v'],
    }
    // 同一张图：按相等
    expect(locateTestRun(project([twin, twin2]), p, 't').unmatched).toBe(0)
    // 第一个改了：按形状能找到一个，但运行时另一个也是这个形状 → 放不上
    const changed = impl('改', 'p.py::C.v@1', 'p.py::C.v')
    const r = locateTestRun(project([changed, twin2]), p, 't')
    expect(r.placed.size).toBe(0)
    expect(r.unmatched).toBe(1)
  })

  it('运行时、现在都不重名：sid 相等才放上', () => {
    const g = project([post])
    const p = paths([{ test: 't', outcome: 'passed', seconds: 0, cases: 1, functions: { 'wf.py::W.post': okTest.functions[fsid(post)], 'wf.py::W.other': { ...okTest.functions[fsid(post)], function: 'wf.py::W.other' } } }], [post])
    const r = locateTestRun(g, p, 't')
    expect([...r.placed.keys()]).toEqual([K.post])
    expect(r.unmatched).toBe(1)
  })
})

describe('entryFunctions：照 core 的 entry_functions', () => {
  it('执行过、却没有别的执行过的函数记录过调用它的（自己调用自己不算），按 sid 排序', () => {
    const t: TestRunJson = {
      test: 't',
      outcome: 'failed',
      seconds: 0,
      cases: 1,
      functions: {
        'b.py::flow': { ...fnRun(run, [], {}, {}), function: 'b.py::flow', calls: { 'b.py::step': 2, 'b.py::helper': 1 } },
        'b.py::step': { ...fnRun(run, [], {}, {}), function: 'b.py::step', calls: { 'b.py::helper': 1 } },
        'b.py::helper': { ...fnRun(run, [], {}, {}), function: 'b.py::helper' },
        'b.py::countdown': { ...fnRun(run, [], {}, {}), function: 'b.py::countdown', calls: { 'b.py::countdown': 3 } },
        'a.py::fixture': { ...fnRun(run, [], {}, {}), function: 'a.py::fixture' },
      },
    }
    expect(entryFunctions(t)).toEqual(['a.py::fixture', 'b.py::countdown', 'b.py::flow'])
  })
})

// ---------------------------------------------------------------------------------------------------------------
// 和 core 共用的用例：core/tests/fixtures/placement/*.json（重名组的 8 个用例）、core/tests/fixtures/entries.json
// （入口函数）；core 的 test_runpath.py 读同一批文件

const FIXTURES = fileURLToPath(new URL('../../core/tests/fixtures/', import.meta.url))

interface CorpusFunction {
  sid: string
  file: string
  qualname: string
  line: number
  nodes: [string, string][]
}

const nodeType = (sid: string): NodeType => {
  const head = sid.split(':')[0].split('#')[0]
  return (['start', 'end', 'step', 'cond', 'loop', 'catch'].includes(head) ? head : 'step') as NodeType
}

/** 用例里压缩过的图（函数 key → sid、文件、限定名、行、节点 [id, sid]）→ 只够放置用的图 */
function corpusGraph(functions: Record<string, CorpusFunction>): ProjectGraph {
  return project(Object.entries(functions).map(([key, f]) => fn(key, f.nodes.map(([id, sid]) => [id, nodeType(sid), id, { sid }]), [], { sid: f.sid })))
}

describe('和 core 共用的用例', () => {
  const files = readdirSync(join(FIXTURES, 'placement')).filter((f) => f.endsWith('.json')).sort()

  it('重名组的用例都在（8 个）', () => {
    expect(files.length).toBe(8)
  })

  for (const file of files) {
    it(`放置：${file}（整份运行和按测试切开的都和用例里写的一样）`, () => {
      const doc = JSON.parse(readFileSync(join(FIXTURES, 'placement', file), 'utf8'))
      const run = doc.run as RunPathsJson
      const dups = new Set(run.function_dups)
      for (const [g, functions] of Object.entries(doc.graphs as Record<string, Record<string, CorpusFunction>>)) {
        const graph = corpusGraph(functions)
        for (const t of run.tests) {
          const sliced: RunPathsJson = { ...run, tests: [t], shapes: Object.fromEntries(Object.entries(run.shapes).filter(([s]) => s in t.functions || dups.has(baseSid(s)))) }
          for (const p of [run, sliced]) {
            const located = locateTestRun(graph, p, t.test)
            const placed = Object.fromEntries(
              [...located.placed].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([key, pf]) => [key, Object.fromEntries([...pf.mapping].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))]),
            )
            expect({ unmatched: located.unmatched, placed }, `${doc.name} ${g}`).toEqual(doc.expect[`${g}|${t.test}`])
          }
        }
      }
    })
  }

  it('入口函数：entries.json 的每个用例（含 icalls）', () => {
    const doc = JSON.parse(readFileSync(join(FIXTURES, 'entries.json'), 'utf8'))
    expect(doc.cases.length).toBeGreaterThanOrEqual(5)
    for (const c of doc.cases) expect(entryFunctions(c.test as TestRunJson), c.name).toEqual(c.entries)
  })
})
