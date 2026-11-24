// 回放（replay.ts）：每一步按记下来的 first_path，钻入的被调函数插在调用它的那一步，派发的任务接在后面；画布上一步步亮。
import { describe, expect, it } from 'vitest'
import type { TestRunJson } from './api'
import { buildLineView, emptyState, setOpen, type LineView } from './graph'
import { K, LINE, T_OK, check, fnRun, fsid, graph, okTest, paths, run } from './graph/fixtures/runs'
import { CK, CL, ROOT, aFn, at, bFn, chainFns, chainProject, into, mainFn } from './graph/fixtures/chain'
import { REPLAY_TIP, replayMarks, replayPlan } from './replay'
import { buildRunOverlay, emptyRunMarks, runMarks, runNodeClasses } from './runview'

const view = (state = emptyState()) => buildLineView(graph, LINE, state)
const cardOf = (v: LineView, fnKey: string) => v.cards.find((c) => c.fn === fnKey && !c.goto)!

/** 通过的测试，run 的第一次执行按真实顺序记：循环两圈 */
const RUN_PATH = ['start', 'step:n2', 'cond:n3', 'loop:n5', 'step:n6', 'loop:n5', 'step:n6', 'loop:n5', 'step:n7', 'end:n8']
const loopTest: TestRunJson = {
  ...okTest,
  functions: { ...okTest.functions, [fsid(run)]: { ...okTest.functions[fsid(run)], first_path: RUN_PATH } },
}

describe('replayPlan', () => {
  it('先放根任务（按 first_path 的顺序，循环几圈就走几圈），收起的派发任务卡片接在后面整张亮一下；没钻入的调用不放', () => {
    const v = view()
    const root = cardOf(v, K.run).id
    const send = cardOf(v, K.send)
    expect(send.expanded).toBe(false)
    const plan = replayPlan(v, graph, paths([loopTest]), T_OK)
    expect(plan.truncated).toBe(false)
    expect(plan.steps.map((s) => s.element)).toEqual([...RUN_PATH.map((sid) => `${root}/${sid}`), send.id])
    expect(plan.steps.every((s, i) => s.card === (i < RUN_PATH.length ? root : send.id))).toBe(true)
    // 走进每一步经过的连线（图里的节点 id）
    expect(plan.steps.slice(0, 5).map((s) => s.pairs)).toEqual([[], ['n1|n2'], ['n2|n3'], ['n3|n5'], ['n5|n6']])
    expect(plan.steps.at(-1)!.pairs).toEqual([])
  })

  it('钻入了的调用：先在钻入卡片里放被调函数（它第一次执行的路），再接着往下；同一个调用再走到时只亮卡片', () => {
    const v0 = view()
    const root = cardOf(v0, K.run).id
    const drillId = `${root}/step:n2>${check.sid}`
    const v = view(setOpen(emptyState(), 'drills', drillId, true))
    expect(v.cards.some((c) => c.id === drillId && c.kind === 'drill')).toBe(true)
    const twice: TestRunJson = {
      ...loopTest,
      functions: { ...loopTest.functions, [fsid(run)]: { ...loopTest.functions[fsid(run)], first_path: ['start', 'step:n2', 'cond:n3', 'step:n2'] } },
    }
    const plan = replayPlan(v, graph, paths([twice]), T_OK)
    const short = (id: string) => (id === drillId ? 'DRILL' : id.replace(`${drillId}/`, 'D/').replace(`${root}/`, 'R/'))
    expect(plan.steps.map((s) => [s.card === root ? 'root' : s.card === drillId ? 'drill' : s.card, short(s.element)])).toEqual([
      ['root', 'R/start'],
      ['root', 'DRILL'],
      ['drill', 'D/start'],
      ['drill', 'D/cond:n2'],
      ['drill', 'D/end:n4'],
      ['root', 'R/cond:n3'],
      ['root', 'DRILL'],
      [cardOf(v, K.send).id, cardOf(v, K.send).id],
    ])
    // 钻入卡片里的连线是被调函数自己的
    expect(plan.steps[3].pairs).toEqual(['n1|n2'])
  })

  it('折叠着的分支：连着几步都在里面时合成一步，亮占位，连线都记上', () => {
    const v = view()
    const rejected: TestRunJson = {
      ...okTest,
      functions: { [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5], ['cond:n3', 1, 5], ['step:n10', 1, 5], ['end:n4', 1, 0]], { 'cond:n3|step:n10': 1, 'step:n10|end:n4': 1 }, { 'end:n4|return|': 1 }) },
    }
    const fold = v.folds.find((f) => f.keys.includes('n10') && f.collapsed)!
    const plan = replayPlan(v, graph, paths([rejected]), T_OK)
    expect(plan.steps.map((s) => s.element).slice(-2)).toEqual([`${cardOf(v, K.run).id}/cond:n3`, fold.id])
    expect(plan.steps.at(-1)!.pairs).toEqual(['n3|n10', 'n10|n4'])
  })

  it('对不上的节点跳过，它前后两步之间不算走过连线；截断的 first_path 记下 truncated', () => {
    const v = view()
    const odd: TestRunJson = {
      ...okTest,
      functions: {
        [fsid(run)]: { ...fnRun(run, [['start', 1, 0], ['step:gone', 1, 0], ['cond:n3', 1, 0]], {}, {}), first_path: ['start', 'step:gone', 'cond:n3', '…'] },
      },
    }
    const plan = replayPlan(v, graph, paths([odd]), T_OK)
    expect(plan.steps.map((s) => s.element)).toEqual([`${cardOf(v, K.run).id}/start`, `${cardOf(v, K.run).id}/cond:n3`])
    expect(plan.steps[1].pairs).toEqual([])
    expect(plan.truncated).toBe(true)
  })

  it('测试不在运行路径里、画布上的函数都没执行过：没有步骤', () => {
    const v = view()
    expect(replayPlan(v, graph, paths([okTest]), 'tests/nope.py::x').steps).toEqual([])
    const onlyCheck: TestRunJson = { ...okTest, functions: { [fsid(check)]: okTest.functions[fsid(check)] } }
    expect(replayPlan(v, graph, paths([onlyCheck]), T_OK).steps).toEqual([])
  })
})

describe('replayMarks', () => {
  it('放到第几步就亮到第几步（不带次数、耗时），正走到的那个多一圈；只亮走过了的连线；没到的卡片暗；最后一步时派发连线亮', () => {
    const v = view()
    const root = cardOf(v, K.run).id
    const send = cardOf(v, K.send).id
    const final = runMarks(v, graph, buildRunOverlay(graph, paths([loopTest]), T_OK, LINE), null, new Map(), new Map())
    const plan = replayPlan(v, graph, paths([loopTest]), T_OK)
    const m = replayMarks(final, v, plan, 2)
    expect(m.active).toBe(true)
    expect([...m.nodes.keys()]).toEqual([`${root}/start`, `${root}/step:n2`, `${root}/cond:n3`])
    expect(m.nodes.get(`${root}/step:n2`)?.badge).toBe('')
    expect(m.current).toBe(`${root}/cond:n3`)
    expect(runNodeClasses(m, `${root}/cond:n3`)).toEqual(['run-walked', 'run-current'])
    expect(runNodeClasses(m, `${root}/loop:n5`)).toEqual(['run-dim'])
    const edge = (src: string, dst: string) => v.edges.find((e) => e.src === `${root}/${src}` && e.dst === `${root}/${dst}`)!.id
    expect(m.edges.has(edge('step:n2', 'cond:n3'))).toBe(true)
    expect(m.edges.has(edge('cond:n3', 'loop:n5'))).toBe(false)
    expect(runNodeClasses(m, send, 'card')).toEqual(['run-dim'])
    expect(m.links.size).toBe(0)
    expect(m.stops.size + m.uncovered.size).toBe(0)
    const last = replayMarks(final, v, plan, plan.steps.length - 1)
    expect(last.current).toBe(send)
    expect(runNodeClasses(last, send, 'card')).toEqual(['run-inside', 'run-current'])
    expect([...last.links]).toEqual([...final.links])
    expect(final.links.size).toBeGreaterThan(0)
    // 连线只亮完整标注里也亮的
    expect([...last.edges.keys()].every((id) => final.edges.has(id))).toBe(true)
    expect(last.edges.has(edge('cond:n3', 'loop:n5'))).toBe(true)
  })

  it('下标越界时夹到两头；没有步骤时只是变暗', () => {
    const v = view()
    const final = runMarks(v, graph, buildRunOverlay(graph, paths([loopTest]), T_OK, LINE), null, new Map(), new Map())
    const plan = replayPlan(v, graph, paths([loopTest]), T_OK)
    expect(replayMarks(final, v, plan, -3).current).toBe(plan.steps[0].element)
    expect(replayMarks(final, v, plan, 999).current).toBe(plan.steps.at(-1)!.element)
    const none = replayMarks(final, v, { steps: [], truncated: false }, 0)
    expect(none.active).toBe(true)
    expect(none.current).toBeNull()
    expect(none.nodes.size).toBe(0)
  })
})


// 钻入链（一个步骤同时钻入它调用的几个函数）：按链上的顺序一张一张播放
describe('replayPlan：钻入链', () => {
  const n2 = at('step:n2')
  const T = 'tests/test_m.py::test_x'
  const fns = (withA: boolean, withB: boolean): TestRunJson => ({
    test: T,
    outcome: 'passed',
    seconds: 0.01,
    cases: 1,
    functions: {
      [fsid(mainFn)]: fnRun(mainFn, [['start', 1, 0], ['step:n2', 1, 10], ['step:n3', 1, 10], ['cond:n4', 1, 1], ['end:n7', 1, 0]], {}, {}),
      ...(withA ? { [fsid(aFn)]: fnRun(aFn, [['start', 1, 0], ['step:n2', 1, 1], ['end:n3', 1, 0]], {}, {}) } : {}),
      ...(withB ? { [fsid(bFn)]: fnRun(bFn, [['start', 1, 0], ['cond:n2', 1, 1], ['end:n4', 1, 0]], {}, {}) } : {}),
    },
  })
  const v = buildLineView(chainProject, CL, setOpen(setOpen(emptyState(), 'drills', into(n2, CK.b), true), 'drills', into(n2, CK.a), true))
  const short = (id: string) =>
    id
      .replace(`${into(n2, CK.a)}/`, 'A/')
      .replace(`${into(n2, CK.b)}/`, 'B/')
      .replace(into(n2, CK.a), 'A')
      .replace(into(n2, CK.b), 'B')
      .replace(`${ROOT}/`, 'R/')
      .replace(ROOT, 'R')
  const plan = (t: TestRunJson) => replayPlan(v, chainProject, paths([t], chainFns), T).steps.map((st) => [short(st.card), short(st.element), st.pairs])

  it('走到这一步：先放第一张（a），再放第二张（b），都放完再接着往下；走进这一步的连线算在第一张上', () => {
    expect(plan(fns(true, true))).toEqual([
      ['R', 'R/start', []],
      ['R', 'A', ['n1|n2']],
      ['A', 'A/start', []],
      ['A', 'A/step:n2', ['n1|n2']],
      ['A', 'A/end:n3', ['n2|n3']],
      ['R', 'B', []],
      ['B', 'B/start', []],
      ['B', 'B/cond:n2', ['n1|n2']],
      ['B', 'B/end:n4', ['n2|n4']],
      ['R', 'R/step:n3', ['n2|n3']],
      ['R', 'R/cond:n4', ['n3|n4']],
      ['R', 'R/end:n7', ['n4|n7']],
    ])
    expect(REPLAY_TIP).toContain('按源码里出现的先后')
    // 回放到第二张卡片时：第一张、第二张都亮，连接线不亮
    const p = replayPlan(v, chainProject, paths([fns(true, true)], chainFns), T)
    const m = replayMarks(emptyRunMarks(), v, p, 5)
    expect(m.current).toBe(into(n2, CK.b))
    expect(m.cardsRun.has(into(n2, CK.a)) && m.cardsRun.has(into(n2, CK.b))).toBe(true)
    expect(m.edges.has(v.edges.find((e) => e.chain)!.id)).toBe(false)
  })

  it('只执行了第二个：只放 b 的那张（走进这一步的连线算在它上面）；都没执行：亮第一张', () => {
    expect(plan(fns(false, true)).slice(0, 4)).toEqual([
      ['R', 'R/start', []],
      ['R', 'B', ['n1|n2']],
      ['B', 'B/start', []],
      ['B', 'B/cond:n2', ['n1|n2']],
    ])
    expect(plan(fns(false, false)).slice(0, 3)).toEqual([
      ['R', 'R/start', []],
      ['R', 'A', ['n1|n2']],
      ['R', 'R/step:n3', ['n2|n3']],
    ])
  })
})
