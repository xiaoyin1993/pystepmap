import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { K, emptyLine, mini, miniLine, missingLine } from './fixtures/mini'
import { fn, line, project, task, uncaught } from './fixtures/builders'
import {
  FIN,
  NESTED,
  PING,
  PONG,
  SHARED,
  TRICKY,
  finProject,
  nestedProject,
  pingPongProject,
  sharedProject,
  trickyProject,
} from './fixtures/shapes'
import { FK, FULL, cut, fullLineSid, fullTitles, oldTitles } from './fixtures/fulltitles'
import { CK, CL, ROOT, at, chainProject, into } from './fixtures/chain'
import { nodeSid } from './collapse'
import { cardSize, foldSize, foldText, labelSize, layoutLineView, nodeSize } from './layout'
import { parseProject, type ProjectGraph } from './types'
import {
  CHAIN_NOTE,
  buildFunctionView,
  buildLineView,
  drillChainOf,
  drillSteps,
  emptyState,
  expandAllFunctionState,
  expandAllState,
  expandEverythingState,
  setOpen,
  type LineView,
  type ViewState,
} from './viewmodel'

const L = miniLine.sid
const card = (v: LineView, fnKey: string, goto = false) => v.cards.find((c) => c.fn === fnKey && c.goto === goto)!
const transfer: ProjectGraph = parseProject(
  JSON.parse(readFileSync(new URL('./fixtures/transfer.json', import.meta.url), 'utf8')),
)

describe('卡片', () => {
  it('默认状态下根任务展开、其余折叠；卡片按任务树先序，id 由线 sid + 任务树路径组成', () => {
    const v = buildLineView(mini, L)
    expect(v.cards.map((c) => [c.name, c.expanded, c.goto, c.depth])).toEqual([
      ['entry', true, false, 0],
      ['task_alert', false, false, 1],
      ['task_a', false, false, 1],
      ['task_b', false, false, 2],
      ['task_c', false, false, 2],
      ['task_b', false, true, 3],
      ['task_b', false, true, 1],
    ])
    expect(v.cards[0].id).toBe('line:api.py::entry|api.py::entry')
    expect(v.cards[3].id).toBe('line:api.py::entry|api.py::entry>tasks.py::task_a>tasks.py::task_b')
    expect(new Set(v.cards.map((c) => c.id)).size).toBe(v.cards.length)
    // 只有展开的卡片有节点
    expect(new Set(v.nodes.map((n) => n.card))).toEqual(new Set([v.cards[0].id]))
  })

  it('goto 卡片指向这个任务第一次出现的卡片', () => {
    const v = buildLineView(mini, L)
    const first = card(v, K.b)
    const gotos = v.cards.filter((c) => c.goto)
    expect(gotos).toHaveLength(2)
    for (const g of gotos) {
      expect(g.gotoTarget).toBe(first.id)
      expect(g.expandable).toBe(false)
    }
    // 即使状态里要求展开，goto 卡片也不展开
    const v2 = buildLineView(mini, L, setOpen(emptyState(), 'cards', gotos[0].id, true))
    expect(v2.cards.find((c) => c.id === gotos[0].id)!.expanded).toBe(false)
  })

  it('折叠的卡片带摘要', () => {
    const v = buildLineView(mini, L)
    expect(card(v, K.a).summary).toEqual({ nodes: 5, ends: { ok: 1 } })
    expect(card(v, K.a).retries).toBe(true)
    expect(v.cards[0].route).toBe('POST /x')
    expect(v.cards[0].loc).toEqual({ file: 'api.py', line: 1, endLine: 1, path: '/repo/api.py' })
  })
})

describe('派发连线', () => {
  it('从发出派发的节点（被折叠时从占位节点）连到被派发任务的卡片；所在卡片折叠时从卡片出发', () => {
    const v = buildLineView(mini, L)
    const root = v.cards[0].id
    const name = (id: string) => {
      const c = v.cards.find((x) => x.id === id)
      return c ? `卡片 ${c.name}` : id.replace(root, 'ROOT')
    }
    expect(v.links.map((l) => [name(l.src), name(l.dst)])).toEqual([
      ['ROOT/fold:cond:n3:yes', '卡片 task_alert'], // n4 在默认折叠的短分支里
      ['ROOT/step:n6', '卡片 task_a'],
      ['卡片 task_a', '卡片 task_b'],
      ['卡片 task_a', '卡片 task_c'],
      ['卡片 task_c', '卡片 task_b'], // goto 卡片
      ['ROOT/step:n7', '卡片 task_b'], // 经 helper2 派发：连到 goto 卡片
    ])
    expect(v.links.find((l) => l.src === `${root}/step:n6`)).toMatchObject({ direct: true, resolution: 'exact' })
    expect(v.links.find((l) => l.src === `${root}/step:n7`)).toMatchObject({ direct: false })
    expect(v.links.find((l) => l.src === card(v, K.c).id)).toMatchObject({ direct: true, resolution: 'guessed' })
  })

  it('起点随卡片、折叠、钻入的状态改变', () => {
    const base = buildLineView(mini, L)
    const root = base.cards[0].id
    const a = card(base, K.a).id
    // 展开任务 A：派发 B、C 的连线从 A 里的节点出发
    let s = setOpen(emptyState(), 'cards', a, true)
    let v = buildLineView(mini, L, s)
    expect(v.links.filter((l) => l.srcCard === a).map((l) => l.src)).toEqual([`${a}/step:n2`, `${a}/step:n3`])
    // 展开短分支：派发告警的连线从节点 n4 出发
    s = setOpen(s, 'folds', `${root}/fold:cond:n3:yes`, true)
    v = buildLineView(mini, L, s)
    expect(v.links[0].src).toBe(`${root}/step:n4`)
    // 收起根卡片：所有从根出发的连线都从卡片本身出发
    v = buildLineView(mini, L, setOpen(s, 'cards', root, false))
    expect(v.links.filter((l) => l.srcCard === root).map((l) => l.src)).toEqual([root, root, root])
    // 钻入 helper2：派发 B 的连线从钻入卡片里的节点出发
    const drill = buildLineView(mini, L).nodes.find((n) => n.key === 'n7')!.drills[0]
    v = buildLineView(mini, L, setOpen(emptyState(), 'drills', drill.id, true))
    const gotoB = v.cards.find((c) => c.goto && c.dispatcher === root)!
    expect(v.links.find((l) => l.dst === gotoB.id)!.src).toBe(`${drill.id}/step:n2`)
  })

  it('公共任务不连线，但节点上标明', () => {
    const s = setOpen(emptyState(), 'cards', card(buildLineView(mini, L), K.a).id, true)
    const v = buildLineView(mini, L, s)
    const n3 = v.nodes.find((n) => n.card === card(v, K.a).id && n.key === 'n3')!
    expect(n3.dispatches.map((d) => [d.title, d.common, d.card !== null])).toEqual([
      ['任务 C', false, true],
      ['通知', true, false],
    ])
    expect(v.links.some((l) => l.dst.includes('notify'))).toBe(false)
  })
})

describe('节点和连线', () => {
  it('节点带种类、标题、结局、派发、解析等级、源码位置；隐含节点没有位置', () => {
    const v = buildLineView(mini, L)
    const byKey = Object.fromEntries(v.nodes.map((n) => [n.key, n]))
    expect(byKey.n1).toMatchObject({ type: 'start', implicit: 'start', trunk: true })
    expect(byKey.n6.dispatches[0]).toMatchObject({ task: K.a, direct: true, common: false, card: card(v, K.a).id })
    expect(byKey.n8).toMatchObject({ type: 'end', endKind: 'ok', trunk: true })
    expect(byKey.n9).toMatchObject({ implicit: 'uncaught', loc: null, trunk: false })
    expect(byKey.n2.loc).toEqual({ file: 'api.py', line: 3, endLine: 3, path: '/repo/api.py' })
    expect(byKey.n2.calls[0]).toMatchObject({ name: 'helper', resolution: 'exact', targetTitle: '校验参数（helper）' })
    expect(byKey.n4).toBeUndefined() // 在折叠的短分支里
  })

  it('连线的端点换成占位节点，分支标签保留在判断节点的出边上', () => {
    const v = buildLineView(mini, L)
    const root = v.cards[0].id
    const toFold = v.edges.find((e) => e.dst === `${root}/fold:cond:n3:yes`)!
    expect(toFold).toMatchObject({ src: `${root}/cond:n3`, kind: 'yes', branch: 'yes', label: '是' })
    const no = v.edges.find((e) => e.dst === `${root}/step:n6`)!
    expect(no).toMatchObject({ kind: 'no', branch: 'no', label: '否' })
    // 折叠区域内部的连线不画
    expect(v.edges.some((e) => e.src.includes('step:n4'))).toBe(false)
    expect(new Set(v.edges.map((e) => e.id)).size).toBe(v.edges.length)
  })

  it('推测的派发、无法判断目标的调用有标记', () => {
    const s = setOpen(
      setOpen(emptyState(), 'cards', card(buildLineView(mini, L), K.c).id, true),
      'cards',
      card(buildLineView(mini, L), K.b).id,
      true,
    )
    const v = buildLineView(mini, L, s)
    const inC = v.nodes.find((n) => n.card === card(v, K.c).id && n.key === 'n2')!
    expect(inC.guessed).toBe(true)
    expect(inC.dispatches[0]).toMatchObject({ resolution: 'guessed', card: card(v, K.b).id })
    const inB = v.nodes.find((n) => n.card === card(v, K.b).id && n.key === 'n2')!
    expect(inB.unresolved).toBe(true)
  })
})

describe('折叠', () => {
  it('短分支被折叠，摘要写着里面几个节点、以什么结局收尾', () => {
    const v = buildLineView(mini, L)
    expect(v.folds).toHaveLength(1)
    expect(v.folds[0]).toMatchObject({
      kind: 'exit',
      branch: 'yes',
      label: '是',
      title: '发告警',
      collapsed: true,
      summary: {
        nodes: 2,
        ends: [{ kind: 'failed', title: '拒绝' }],
        endKinds: ['failed'],
        rejoins: false,
        first: '发告警',
        dispatches: ['告警任务'],
      },
    })
  })

  it('展开一个折叠后其余状态不变；再收起回到原样', () => {
    const t = transfer.lines[0].sid
    const before = buildLineView(transfer, t)
    expect(before.folds.map((f) => [f.kind, f.summary.nodes, f.collapsed])).toEqual([
      ['exit', 2, true],
      ['exit', 2, true],
      ['exit', 2, true],
      ['exit', 2, true],
      ['catch', 4, true],
    ])
    const target = before.folds[2]
    const opened = buildLineView(transfer, t, setOpen(emptyState(), 'folds', target.id, true))
    expect(opened.folds.map((f) => f.collapsed)).toEqual([true, true, false, true, true])
    expect(opened.cards.map((c) => c.expanded)).toEqual(before.cards.map((c) => c.expanded))
    // 展开后成员节点出现，并记着所在的折叠区域
    for (const m of target.members) expect(opened.nodes.find((n) => n.id === m)?.fold).toBe(target.id)
    const closed = buildLineView(transfer, t, setOpen(emptyState(), 'folds', target.id, false))
    expect(closed).toEqual(before)
  })

  it('嵌套折叠：外层折叠时看不见里层，外层展开后里层是占位', () => {
    const v = buildFunctionView(nestedProject, NESTED)
    expect(v.folds.map((f) => f.id.split('/fold:')[1])).toEqual(['cond:n2:yes'])
    const outer = v.folds[0].id
    const v2 = buildFunctionView(nestedProject, NESTED, setOpen(emptyState(), 'folds', outer, true))
    expect(v2.folds.map((f) => [f.id.split('/fold:')[1], f.collapsed, f.parent])).toEqual([
      ['cond:n2:yes', false, null],
      ['cond:n3:yes', true, outer],
    ])
    expect(v2.nodes.map((n) => n.key)).toEqual(['n1', 'n2', 'n3', 'n6', 'n7', 'n8', 'n9'])
  })

  it('全部展开：所有卡片和折叠（含嵌套）都展开', () => {
    const s = expandAllState(mini, L)
    const v = buildLineView(mini, L, s)
    expect(v.cards.filter((c) => c.expandable).every((c) => c.expanded)).toBe(true)
    expect(v.folds.every((f) => !f.collapsed)).toBe(true)
    const n = expandAllState(nestedProject, nestedProject.lines[0].sid)
    expect(buildLineView(nestedProject, nestedProject.lines[0].sid, n).nodes).toHaveLength(9)
  })
})

describe('钻入', () => {
  it('步骤可以钻入被调用的函数，在原地换成一张内嵌卡片，可收回', () => {
    const v = buildLineView(mini, L)
    const n2 = v.nodes.find((n) => n.key === 'n2')!
    expect(n2.drills).toEqual([
      { id: `${n2.id}>helpers.py::helper`, fn: K.helper, title: '校验参数（helper）', name: 'helper', resolution: 'exact', open: false },
    ])
    const s = setOpen(emptyState(), 'drills', n2.drills[0].id, true)
    const d = buildLineView(mini, L, s)
    const drill = d.cards.find((c) => c.kind === 'drill')!
    expect(drill).toMatchObject({ id: n2.drills[0].id, parent: v.cards[0].id, expanded: true, depth: 1 })
    expect(drill.anchor?.id).toBe(n2.id)
    expect(d.nodes.some((n) => n.id === n2.id)).toBe(false)
    expect(d.nodes.filter((n) => n.card === drill.id).map((n) => n.key)).toEqual(['n1', 'n2', 'n3', 'n4'])
    // 进出步骤的连线改连钻入卡片
    expect(d.edges.find((e) => e.dst === drill.id)?.src).toBe(`${v.cards[0].id}/start`)
    expect(d.edges.find((e) => e.src === drill.id)?.dst).toBe(`${v.cards[0].id}/cond:n3`)
    // 收回
    expect(buildLineView(mini, L, setOpen(s, 'drills', drill.id, false))).toEqual(v)
  })

  it('钻入卡片里不再提供钻回已在链上的函数', () => {
    const v = buildFunctionView(pingPongProject, PING)
    const step = v.nodes.find((n) => n.key === 'n2')!
    expect(step.drills.map((d) => d.fn)).toEqual([PONG])
    const v2 = buildFunctionView(pingPongProject, PING, setOpen(emptyState(), 'drills', step.drills[0].id, true))
    const inner = v2.nodes.find((n) => n.card === step.drills[0].id && n.key === 'n2')!
    expect(inner.calls[0].target).toBe(PING)
    expect(inner.drills).toEqual([])
  })
})

describe('边界情况', () => {
  it('根任务不在图里、根任务没有节点：不报错', () => {
    const v = buildLineView(mini, missingLine.sid)
    expect(v.cards[0]).toMatchObject({ missing: true, expanded: false, expandable: false })
    expect(v.links).toHaveLength(1)
    const e = buildLineView(mini, emptyLine.sid)
    expect(e.cards).toHaveLength(1)
    expect(e.cards[0]).toMatchObject({ expanded: false, expandable: false, missing: false })
    expect(e.nodes).toEqual([])
  })

  it('没有这条功能线时抛错；没有功能线的项目照样能读', () => {
    expect(() => buildLineView(mini, 'line:nope')).toThrow(/没有这条功能线/)
    const p = parseProject({ ...JSON.parse(JSON.stringify(mini)), lines: [] })
    expect(p.lines).toEqual([])
  })

  it('旧图（没有 sid、没有 branch）也能建视图，id 退回图内编号', () => {
    const old = JSON.parse(JSON.stringify(transfer))
    for (const f of Object.values<any>(old.functions)) {
      delete f.sid
      for (const n of f.nodes) delete n.sid
      for (const e of f.edges) delete e.branch
    }
    for (const l of old.lines) delete l.sid
    delete old.sids
    const p = parseProject(old)
    const v = buildLineView(p, p.lines[0].id)
    expect(v.folds).toHaveLength(5)
    expect(v.edges.filter((e) => e.label).length).toBe(buildLineView(transfer, transfer.lines[0].sid).edges.filter((e) => e.label).length)
  })

  it('同样的输入两次结果相同（纯函数）', () => {
    expect(buildLineView(transfer, transfer.lines[0].sid)).toEqual(buildLineView(transfer, transfer.lines[0].sid))
  })
})

describe('主干和循环出边（界面用）', () => {
  const short = (id: string) => id.split('/').pop()
  const pair = (e: { src: string; dst: string }) => `${short(e.src)}->${short(e.dst)}`

  it('循环节点的出边标"每一项" / "循环结束"，不是分支；主干上相邻两个节点之间的连线标 trunk', () => {
    const v = buildFunctionView(trickyProject, TRICKY, expandAllFunctionState(trickyProject, TRICKY))
    expect(v.edges.filter((e) => e.kind === 'loop_each').map((e) => [e.label, e.branch])).toEqual([['每一项', null]])
    expect(v.edges.filter((e) => e.kind === 'loop_exit').map((e) => [e.label, e.branch])).toEqual([['循环结束', null]])
    // 主干：开始 → 取一批数据 → 循环 → 汇总 → 完成（循环体只能经回边回来，不在主干上）
    expect(v.edges.filter((e) => e.trunk).map(pair)).toEqual([
      'start->step:n2',
      'step:n2->loop:n3',
      'loop:n3->step:n9',
      'step:n9->end:n10',
    ])
    // 回边、异常连线、不在主干上的都不是
    expect(v.edges.filter((e) => e.back || e.kind === 'raise').every((e) => !e.trunk)).toBe(true)
  })

  it('while 循环：条件成立 / 循环结束', () => {
    const W = 'a.py::poll@1'
    const p = project([
      fn(
        W,
        [
          ['n1', 'start'],
          ['n2', 'loop', 'while not done', { subtitle: 'while not done' }],
          ['n3', 'step', '再查一次', { subtitle: 'check' }],
          ['n4', 'end', '完成', { end: 'ok' }],
          uncaught('n5'),
        ],
        [
          ['n1', 'n2', 'next'],
          ['n2', 'n3', 'loop_each'],
          ['n3', 'n2', 'loop_back'],
          ['n2', 'n4', 'loop_exit'],
        ],
      ),
    ])
    const v = buildFunctionView(p, W)
    expect(v.edges.filter((e) => e.label).map((e) => [e.kind, e.label])).toEqual([
      ['loop_each', '条件成立'],
      ['loop_exit', '循环结束'],
    ])
  })

  it('主干穿过折叠起来的汇回长分支：占位节点和进出它的连线都标 trunk', () => {
    // 开始 → 判断 →（是：三步）→ 汇合 → 完成；否：判断 → 汇合。主干走"是"那边（更长）
    const F = 'a.py::f@1'
    const p = project([
      fn(
        F,
        [
          ['n1', 'start'],
          ['n2', 'cond', '要不要推送？'],
          ['n3', 'step'],
          ['n4', 'step'],
          ['n5', 'step'],
          ['n6', 'step', '汇合'],
          ['n7', 'end', '完成', { end: 'ok' }],
          uncaught('n8'),
        ],
        [
          ['n1', 'n2', 'next'],
          ['n2', 'n3', 'yes'],
          ['n3', 'n4', 'next'],
          ['n4', 'n5', 'next'],
          ['n5', 'n6', 'next'],
          ['n2', 'n6', 'no'],
          ['n6', 'n7', 'next'],
        ],
      ),
    ])
    const v = buildFunctionView(p, F, emptyState(), { longTrunk: 2 })
    expect(v.folds.map((f) => [f.kind, f.collapsed, f.trunk])).toEqual([['side', true, true]])
    expect(v.edges.map((e) => [pair(e), e.trunk])).toEqual([
      ['start->cond:n2', true],
      ['cond:n2->fold:cond:n2:yes', true],
      ['fold:cond:n2:yes->step:n6', true],
      ['cond:n2->step:n6', false],
      ['step:n6->end:n7', true],
    ])
    // 不在主干上的折叠（提前结束的短分支）
    expect(buildLineView(mini, L).folds.map((f) => f.trunk)).toEqual([false])
  })
})

describe('评审后的修订', () => {
  it('分支里的 continue 经 finally 继续循环：摘要写汇回', () => {
    const v = buildFunctionView(finProject, FIN, expandAllFunctionState(finProject, FIN))
    expect(v.folds.map((f) => [f.kind, f.summary.endKinds, f.summary.rejoins])).toEqual([['side', ['failed'], true]])
    const closed = buildFunctionView(finProject, FIN, setOpen(emptyState(), 'folds', v.folds[0].id, false))
    expect(foldText(closed.folds[0]).headline).toBe('3 个节点 · 以失败收尾 · 汇回')
  })

  it('提前结束的分支以共用的结局收尾：摘要里有这个结局', () => {
    const v = buildFunctionView(sharedProject, SHARED)
    expect(v.folds).toHaveLength(1)
    expect(v.folds[0].summary).toMatchObject({
      nodes: 2,
      ends: [{ kind: 'failed', title: '拒绝' }],
      endKinds: ['failed'],
      rejoins: false,
    })
    expect(foldText(v.folds[0]).headline).toBe('2 个节点 · 以失败收尾')
  })

  it('派发连线找不到发出派发的节点（函数不在图里）时，不说是确定的', () => {
    const v = buildLineView(mini, missingLine.sid)
    expect(v.links).toHaveLength(1)
    expect(v.links[0].resolution).toBe('unresolved')
  })

  it('派发没画成节点（函数在图里、卡片展开，但没有哪个节点派发这个任务）：从卡片本身连出，目标不明', () => {
    const ROOT = 'api.py::push@1'
    const CHILD = 'tasks.py::job@1'
    // 功能线说 push 派发 job，但 push 的节点里没有这次派发（引擎没画成节点的地方）
    const root = fn(ROOT, [['n1', 'start'], ['n2', 'step', '收下请求'], ['n3', 'end', '完成', { end: 'ok' }], uncaught('n4')], [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'next'],
    ])
    const child = fn(CHILD, [['n1', 'start'], ['n2', 'end', '完成', { end: 'ok' }]], [['n1', 'n2', 'next']], { kind: 'task' })
    const p = project([root, child], [line(task(ROOT, [task(CHILD)]), [root, child])])
    const v = buildLineView(p, p.lines[0].sid)
    expect(v.cards[0]).toMatchObject({ fn: ROOT, expanded: true })
    expect(v.links).toEqual([
      expect.objectContaining({ src: v.cards[0].id, dst: v.cards[1].id, srcCard: v.cards[0].id, direct: false, resolution: 'unresolved' }),
    ])
  })

  it('没写标题的入口（标题就是函数名）挂在路由上时，卡片标题写"方法 路径"：接口、工作流、任务都一样；写了标题的不换', () => {
    const body: [Parameters<typeof fn>[1], Parameters<typeof fn>[2]] = [
      [['n1', 'start'], ['n2', 'end', '完成', { end: 'ok' }]],
      [['n1', 'n2', 'next']],
    ]
    const titleOf = (extra: Parameters<typeof fn>[3]) => {
      const f = fn('api.py::Api.handle@1', body[0], body[1], extra)
      const p = project([f], [line(task(f.key), [f])])
      return buildLineView(p, p.lines[0].sid).cards[0].title
    }
    expect(titleOf({ kind: 'route', route: 'POST /x', title: 'handle' })).toBe('POST /x')
    expect(titleOf({ kind: 'workflow', route: 'POST /x', title: 'handle' })).toBe('POST /x')
    expect(titleOf({ kind: 'task', route: 'POST /x', title: 'handle' })).toBe('POST /x')
    expect(titleOf({ kind: 'workflow', route: 'POST /x', title: '停用用户' })).toBe('停用用户')
    expect(titleOf({ kind: 'workflow', route: null, title: 'handle' })).toBe('handle')
  })

  /** 展开的卡片里：每个图节点要么看得见，要么在恰好一个折叠着的占位里；每条图连线要么画出来，要么在折叠内部，要么改连到占位上。 */
  function checkCoverage(project: ProjectGraph, v: LineView): string[] {
    const problems: string[] = []
    const visible = new Set(v.nodes.map((n) => n.id))
    const collapsed = v.folds.filter((f) => f.collapsed)
    const drawn = new Set(v.edges.map((e) => `${e.src}|${e.dst}|${e.kind}`))
    for (const c of v.cards.filter((x) => x.expanded && x.kind === 'task')) {
      const fn = project.functions[c.fn!]
      const rep = new Map<string, string>()
      for (const n of fn.nodes) {
        const id = `${c.id}/${nodeSid(n)}`
        const holders = collapsed.filter((f) => f.members.includes(id))
        if (visible.has(id)) {
          if (holders.length) problems.push(`看得见却也在折叠里：${id}`)
          rep.set(n.id, id)
        } else if (holders.length === 1) rep.set(n.id, holders[0].id)
        else problems.push(`节点 ${id} 在 ${holders.length} 个折叠着的占位里`)
      }
      for (const e of fn.edges) {
        const s = rep.get(e.src)
        const d = rep.get(e.dst)
        if (s === undefined || d === undefined) continue
        if (s === d && s.includes('/fold:')) continue
        if (!drawn.has(`${s}|${d}|${e.kind}`)) problems.push(`没画：${e.src}->${e.dst} ${e.kind}（${s} -> ${d}）`)
      }
    }
    return problems
  }

  it('transfer：默认、全部展开、逐个展开每个折叠，节点和连线都有着落', () => {
    const t = transfer.lines[0].sid
    const base = buildLineView(transfer, t)
    expect(checkCoverage(transfer, base)).toEqual([])
    expect(checkCoverage(transfer, buildLineView(transfer, t, expandAllState(transfer, t)))).toEqual([])
    for (const f of base.folds) {
      const v = buildLineView(transfer, t, setOpen(emptyState(), 'folds', f.id, true))
      expect(checkCoverage(transfer, v)).toEqual([])
    }
    const nested = nestedProject.lines[0].sid
    const nv = buildLineView(nestedProject, nested)
    expect(checkCoverage(nestedProject, nv)).toEqual([])
    expect(checkCoverage(nestedProject, buildLineView(nestedProject, nested, setOpen(emptyState(), 'folds', nv.folds[0].id, true)))).toEqual([])
  })
})

describe('完整形式（引擎截断过的文字，图里的 *_full）', () => {
  const S = fullLineSid
  const expanded = (p: ProjectGraph) => buildLineView(p, S, expandAllState(p, S))
  const byKey = (v: LineView, key: string) => v.nodes.find((n) => n.key === key)!

  it('读图：*_full 只在有值时出现，旧图没有', () => {
    const n2 = fullTitles.functions[FK.inbound].nodes[1]
    expect([n2.title, n2.title_full, 'subtitle_full' in n2]).toEqual([cut(FULL.comment, 60), FULL.comment, false])
    expect(fullTitles.functions[FK.inbound].title_full).toBe(FULL.fn)
    expect(fullTitles.lines[0].title_full).toBe(FULL.fn)
    expect(fullTitles.functions[FK.inbound].edges.find((e) => e.kind === 'case' && e.branch !== 'else')!.label_full).toBe(FULL.pattern)
    const text = JSON.stringify(oldTitles)
    expect(text).not.toContain('_full')
  })

  it('节点、卡片、功能线：画在画布上的是显示形式，另带完整形式', () => {
    const v = expanded(fullTitles)
    expect([v.line.title, v.line.titleFull]).toEqual([cut(FULL.fn, 60), FULL.fn])
    const root = v.cards[0]
    expect([root.title, root.titleFull]).toEqual([cut(FULL.fn, 60), FULL.fn])
    const n2 = byKey(v, 'n2')
    expect([n2.title, n2.titleFull, n2.subtitle, n2.subtitleFull]).toEqual([cut(FULL.comment, 60), FULL.comment, 'seen_before', 'seen_before'])
    const n3 = byKey(v, 'n3')
    expect([n3.title, n3.titleFull, n3.subtitle, n3.subtitleFull]).toEqual([cut(FULL.cond, 60), FULL.cond, cut(FULL.cond, 80), FULL.cond])
    const n10 = byKey(v, 'n10')
    expect([n10.subtitle, n10.subtitleFull]).toEqual([cut(FULL.exc, 80), FULL.exc])
    expect([byKey(v, 'n13').title, byKey(v, 'n13').titleFull]).toEqual([cut(FULL.ret, 60), FULL.ret])
    // 没被截断的：完整形式就是显示形式
    expect([byKey(v, 'n9').title, byKey(v, 'n9').titleFull]).toEqual(['通知账户所有者', '通知账户所有者'])
    // 只在详情栏列出的：调用目标是完整标题
    expect(byKey(v, 'n4').calls[0].targetTitle).toBe(FULL.archive)
    // 钻入卡片也带完整标题
    const drilled = buildLineView(fullTitles, S, setOpen(expandAllState(fullTitles, S), 'drills', byKey(v, 'n4').drills[0].id, true))
    const drill = drilled.cards.find((c) => c.kind === 'drill')!
    expect([drill.title, drill.titleFull, drill.anchor!.titleFull]).toEqual([cut(FULL.archive, 60), FULL.archive, FULL.archive])
  })

  it('连线标签：case 模式带完整的，分支（branch）仍是截断后的标签', () => {
    const v = expanded(fullTitles)
    const c = v.edges.find((e) => e.kind === 'case' && e.branch !== 'else')!
    expect([c.label, c.labelFull, c.branch]).toEqual([cut(FULL.pattern, 80), FULL.pattern, cut(FULL.pattern, 80)])
    const yes = v.edges.find((e) => e.branch === 'yes')!
    expect([yes.label, yes.labelFull]).toEqual(['是', '是'])
    const noMatch = v.edges.find((e) => e.branch === 'else')!
    expect([noMatch.label, noMatch.labelFull]).toEqual(['都不匹配', '都不匹配'])
  })

  it('折叠占位：入口标题、分支模式、捕获的异常类型带完整形式；摘要里的入口和结局是完整的', () => {
    const v = buildLineView(fullTitles, S)
    const exit = v.folds.find((f) => f.branch === 'yes')!
    expect([exit.title, exit.titleFull, exit.summary.first]).toEqual([cut(FULL.archive, 60), FULL.archive, FULL.archive])
    expect(exit.summary.ends).toEqual([{ kind: 'skipped', title: FULL.ret }])
    expect(foldText(exit).detail).toBe(cut(FULL.archive, 60)) // 占位上画的仍是显示形式
    const caseFold = v.folds.find((f) => f.branch === cut(FULL.pattern, 80))!
    expect([caseFold.label, caseFold.labelFull]).toEqual([cut(FULL.pattern, 80), FULL.pattern])
    const catchFold = v.folds.find((f) => f.kind === 'catch')!
    expect([catchFold.label, catchFold.labelFull]).toEqual([cut(FULL.exc, 80), FULL.exc])
    expect([catchFold.title, catchFold.titleFull]).toEqual([cut(`捕获 ${FULL.exc}`, 60), `捕获 ${FULL.exc}`])
  })

  it('旧图（没有 *_full）照常显示：完整形式等于显示形式，显示形式和新图相同', () => {
    for (const v of [buildLineView(oldTitles, S), expanded(oldTitles)]) {
      expect(v.line.titleFull).toBe(v.line.title)
      expect(v.cards.every((c) => c.titleFull === c.title)).toBe(true)
      expect(v.nodes.every((n) => n.titleFull === n.title && n.subtitleFull === n.subtitle)).toBe(true)
      expect(v.folds.every((f) => f.titleFull === f.title && f.labelFull === f.label)).toBe(true)
      expect(v.edges.every((e) => e.labelFull === e.label)).toBe(true)
    }
    const shown = (v: LineView) => [v.line.title, v.cards.map((c) => c.title), v.nodes.map((n) => [n.title, n.subtitle]),
      v.folds.map((f) => [f.title, f.label]), v.edges.map((e) => e.label)]
    expect(shown(expanded(oldTitles))).toEqual(shown(expanded(fullTitles)))
  })

  it('尺寸只按显示形式算：和旧图的一样', async () => {
    for (const [nv, ov] of [
      [buildLineView(fullTitles, S), buildLineView(oldTitles, S)],
      [expanded(fullTitles), expanded(oldTitles)],
    ]) {
      expect(nv.nodes.map(nodeSize)).toEqual(ov.nodes.map(nodeSize))
      expect(nv.cards.map(cardSize)).toEqual(ov.cards.map(cardSize))
      expect(nv.folds.map(foldSize)).toEqual(ov.folds.map(foldSize))
      expect(nv.edges.map((e) => labelSize(e.label))).toEqual(ov.edges.map((e) => labelSize(e.label)))
      expect(await layoutLineView(nv)).toEqual(await layoutLineView(ov))
    }
  })
})


describe('expandEverythingState：界面上的"全部展开"连能钻入的函数一起展开', () => {
  it('卡片、折叠都展开，能钻入的步骤都钻进去（钻进去之后露出来的接着钻），再来一次不用动；到上限时说还有', () => {
    const sid = transfer.lines[0].sid
    const plain = buildLineView(transfer, sid, expandAllState(transfer, sid))
    const canDrill = plain.nodes.filter((n) => n.drills.length > 0).length
    expect(canDrill).toBeGreaterThan(0)
    const r = expandEverythingState(transfer, sid)
    expect(r.more).toBe(false)
    const v = buildLineView(transfer, sid, r.state)
    expect(v.nodes.filter((n) => n.drills.length > 0 && !n.drills.some((d) => d.open))).toEqual([]) // 没有还能钻、没钻的步骤
    expect(v.folds.filter((f) => f.collapsed)).toEqual([])
    expect(v.cards.filter((c) => c.kind === 'drill').length).toBeGreaterThanOrEqual(canDrill)
    expect(expandEverythingState(transfer, sid, r.state).state).toEqual(r.state)
    const capped = expandEverythingState(transfer, sid, emptyState(), 2)
    expect(capped.more).toBe(true)
    expect(Object.values(capped.state.drills).filter(Boolean)).toHaveLength(2)
  })
})


describe('钻入链：一个步骤同时钻入它调用的几个函数', () => {
  const open = (...ids: string[]): ViewState => ids.reduce((s, id) => setOpen(s, 'drills', id, true), emptyState())
  const n2 = at('step:n2')
  const n3 = at('step:n3')
  const view = (s: ViewState) => buildLineView(chainProject, CL, s)
  /** 钻入链：卡片的 id 换成被调函数 key，好读 */
  const chainFns = (v: LineView, step: string) => drillChainOf(v, step).map((c) => c.fn)

  it('步骤的钻入选项按 calls 的顺序（源码里出现的先后，外层的调用在前）', () => {
    const v = view(emptyState())
    expect(v.nodes.find((n) => n.id === n2)!.drills.map((d) => [d.id, d.fn, d.open])).toEqual([
      [into(n2, CK.a), CK.a, false],
      [into(n2, CK.b), CK.b, false],
    ])
    expect(v.nodes.find((n) => n.id === n3)!.drills.map((d) => d.fn)).toEqual([CK.a, CK.b, CK.c])
  })

  it('两个都开：步骤换成两张钻入卡片，进步骤的连线接第一张、出步骤的从第二张出发，中间一条钻入链的连接线', () => {
    // 开的先后和链上的先后无关：先开 b 再开 a，链上还是 a 在前
    const v = view(open(into(n2, CK.b), into(n2, CK.a)))
    expect(v.nodes.some((n) => n.id === n2)).toBe(false)
    const chain = drillChainOf(v, n2)
    expect(chain.map((c) => [c.id, c.fn, c.chainIndex, c.parent, c.depth])).toEqual([
      [into(n2, CK.a), CK.a, 0, ROOT, 1],
      [into(n2, CK.b), CK.b, 1, ROOT, 1],
    ])
    // 链上的卡片是同一个步骤代替的：anchor 是同一个，它的钻入选项都开着
    expect(chain[0].anchor).toBe(chain[1].anchor)
    expect(chain[0].anchor!.drills.map((d) => d.open)).toEqual([true, true])
    // 各自的内容在各自的卡片里
    expect(v.nodes.filter((n) => n.card === chain[0].id).map((n) => n.key)).toEqual(['n1', 'n2', 'n3', 'n4'])
    expect(v.nodes.filter((n) => n.card === chain[1].id).map((n) => n.key)).toEqual(['n1', 'n2', 'n3', 'n4', 'n5'])
    // 进、出、中间
    const into2 = v.edges.filter((e) => e.dst === chain[0].id)
    const out2 = v.edges.filter((e) => e.src === chain[1].id)
    expect(into2.map((e) => [e.src, e.keys, e.chain, e.trunk])).toEqual([[at('start'), ['n1|n2|next|'], false, true]])
    expect(out2.map((e) => [e.dst, e.keys, e.chain, e.trunk])).toEqual([[n3, ['n2|n3|next|'], false, true]])
    expect(v.edges.filter((e) => e.src === chain[0].id || e.dst === chain[1].id)).toEqual([
      {
        id: `chain:${chain[0].id}->${chain[1].id}`,
        card: ROOT,
        src: chain[0].id,
        dst: chain[1].id,
        kind: 'next',
        branch: null,
        label: '',
        labelFull: '',
        back: false,
        trunk: false,
        keys: [],
        chain: true,
      },
    ])
    // 图里的连线都不是钻入链的连接线
    expect(v.edges.filter((e) => e.chain)).toHaveLength(1)
    expect(v.edges.filter((e) => !e.chain).every((e) => e.keys.length > 0)).toBe(true)
    expect(CHAIN_NOTE).toContain('不一定是执行的先后')
  })

  it('三个都开：三张、两条连接线，按 a、b、c；只开一个就是一张（和以前一样）', () => {
    const v = view(open(into(n3, CK.c), into(n3, CK.a), into(n3, CK.b)))
    expect(chainFns(v, n3)).toEqual([CK.a, CK.b, CK.c])
    const ids = drillChainOf(v, n3).map((c) => c.id)
    expect(v.edges.filter((e) => e.chain).map((e) => [e.src, e.dst])).toEqual([
      [ids[0], ids[1]],
      [ids[1], ids[2]],
    ])
    expect(v.edges.find((e) => e.dst === ids[0] && !e.chain)!.src).toBe(n2)
    expect(v.edges.find((e) => e.src === ids[2] && !e.chain)!.dst).toBe(at('cond:n4'))
    // 只开一个
    const one = view(open(into(n3, CK.b)))
    expect(chainFns(one, n3)).toEqual([CK.b])
    expect(one.edges.filter((e) => e.chain)).toEqual([])
    expect(one.edges.find((e) => e.dst === into(n3, CK.b))!.src).toBe(n2)
    expect(one.edges.find((e) => e.src === into(n3, CK.b))!.dst).toBe(at('cond:n4'))
  })

  it('只开一部分：没开的还在钻入选项里（open 为假）；再开一个按选项的顺序插进链里', () => {
    const v = view(open(into(n3, CK.b), into(n3, CK.c)))
    expect(chainFns(v, n3)).toEqual([CK.b, CK.c])
    expect(drillChainOf(v, n3)[0].anchor!.drills.map((d) => [d.fn, d.open])).toEqual([
      [CK.a, false],
      [CK.b, true],
      [CK.c, true],
    ])
    const more = view(open(into(n3, CK.b), into(n3, CK.c), into(n3, CK.a)))
    expect(chainFns(more, n3)).toEqual([CK.a, CK.b, CK.c])
    expect(drillChainOf(more, n3).map((c) => c.chainIndex)).toEqual([0, 1, 2])
  })

  it('收回中间那张：只收它，前后两张直接连上；都收回后和没钻入一样', () => {
    const s = open(into(n3, CK.a), into(n3, CK.b), into(n3, CK.c))
    const v = view(setOpen(s, 'drills', into(n3, CK.b), false))
    expect(chainFns(v, n3)).toEqual([CK.a, CK.c])
    expect(v.cards.some((c) => c.fn === CK.b)).toBe(false)
    expect(v.edges.filter((e) => e.chain).map((e) => e.id)).toEqual([`chain:${into(n3, CK.a)}->${into(n3, CK.c)}`])
    expect(drillChainOf(v, n3).map((c) => c.chainIndex)).toEqual([0, 1])
    let back = s
    for (const f of [CK.a, CK.b, CK.c]) back = setOpen(back, 'drills', into(n3, f), false)
    expect(view(back)).toEqual(view(emptyState()))
  })

  it('钻入链里的卡片还可以再钻：a 的卡片里"叫 c 和 d"两个都开，嵌套的链在 a 的卡片里', () => {
    const a = into(n2, CK.a)
    const inner = at('step:n2', a)
    const v = view(open(a, into(n2, CK.b), into(inner, CK.c), into(inner, CK.d)))
    expect(chainFns(v, n2)).toEqual([CK.a, CK.b])
    expect(drillChainOf(v, inner).map((c) => [c.fn, c.parent, c.depth])).toEqual([
      [CK.c, a, 2],
      [CK.d, a, 2],
    ])
    expect(v.edges.filter((e) => e.chain).map((e) => [e.card, e.src, e.dst])).toEqual([
      [a, into(inner, CK.c), into(inner, CK.d)],
      [ROOT, a, into(n2, CK.b)],
    ])
    // a 的卡片里：开始 → c 的卡片 ··· d 的卡片 → 返回
    expect(v.edges.find((e) => e.dst === into(inner, CK.c))!.src).toBe(at('start', a))
    expect(v.edges.find((e) => e.src === into(inner, CK.d) && !e.chain)!.dst).toBe(at('end:n3', a))
    // 卡片按先序：a、a 里的 c、d，再 b
    expect(v.cards.filter((c) => c.kind === 'drill').map((c) => c.fn)).toEqual([CK.a, CK.c, CK.d, CK.b])
    expect(drillSteps(v).map((n) => n.id)).toEqual(expect.arrayContaining([n2, inner, n3]))
  })

  it('折叠着的分支里的步骤：钻入状态留着但看不见（连线照旧到占位）；展开分支后是钻入链，分支连线接第一张', () => {
    const n5 = at('step:n5')
    const s = open(into(n5, CK.a), into(n5, CK.b))
    const folded = view(s)
    const fold = folded.folds.find((f) => f.members.includes(n5))!
    expect(fold.collapsed).toBe(true)
    expect(folded.cards.some((c) => c.anchor?.id === n5)).toBe(false)
    expect(folded.edges.find((e) => e.dst === fold.id)!.src).toBe(at('cond:n4'))
    const v = view(setOpen(s, 'folds', fold.id, true))
    expect(chainFns(v, n5)).toEqual([CK.a, CK.b])
    const yes = v.edges.find((e) => e.dst === into(n5, CK.a))!
    expect([yes.src, yes.branch, yes.label]).toEqual([at('cond:n4'), 'yes', '是'])
    expect(v.edges.find((e) => e.src === into(n5, CK.b) && !e.chain)!.dst).toBe(at('end:n6'))
  })

  it('派发连线：钻进去时从卡片里派发的节点出发；派发不在开着的卡片里画成的节点上时，从带出派发的那张（或链的最后一张）出发', () => {
    // 默认折叠着的"记下问题"分支里也经 a 派发：只看 n2、n3
    const src = (s: ViewState) => view(s).links.map((l) => l.src).filter((id) => !id.includes('/fold:'))
    expect(src(emptyState())).toEqual([n2, n3])
    // n2 钻入 a、b：a 的卡片里"叫 c 和 d"经 c 派发
    expect(src(open(into(n2, CK.a), into(n2, CK.b)))).toEqual([at('step:n2', into(n2, CK.a)), n3])
    // n2 只钻入 b：派发经没展开的 a，从链的最后一张（b）出发
    expect(src(open(into(n2, CK.b)))).toEqual([into(n2, CK.b), n3])
    // n3 钻入 c、b（没开 a）：经 c 派发，c 的卡片里的"派发 t"
    expect(src(open(into(n3, CK.c), into(n3, CK.b)))).toEqual([n2, at('step:n2', into(n3, CK.c))])
  })

  it('drillSteps：画成节点的能钻入的步骤，加上已经换成钻入链的（每个一次）', () => {
    const v = view(open(into(n2, CK.a), into(n2, CK.b), into(n3, CK.c)))
    const steps = drillSteps(v)
    expect(steps.map((n) => n.id).filter((id) => id === n2 || id === n3)).toEqual([n2, n3])
    expect(new Set(steps.map((n) => n.id)).size).toBe(steps.length)
    expect(drillChainOf(v, at('cond:n4'))).toEqual([])
  })

  it('全部展开：每个步骤调用的能钻入的函数都钻（钻进去露出来的接着钻）；已经开着的不动；到上限时说还有', () => {
    const r = expandEverythingState(chainProject, CL)
    expect(r.more).toBe(false)
    const v = view(r.state)
    expect(chainFns(v, n2)).toEqual([CK.a, CK.b])
    expect(chainFns(v, n3)).toEqual([CK.a, CK.b, CK.c])
    expect(chainFns(v, at('step:n5'))).toEqual([CK.a, CK.b])
    // a 的卡片里的"叫 c 和 d"也都钻了（三处 a 各一次）
    for (const step of [n2, n3, at('step:n5')]) expect(chainFns(v, at('step:n2', into(step, CK.a)))).toEqual([CK.c, CK.d])
    // 没有还能钻、没钻的
    expect(drillSteps(v).filter((n) => n.drills.some((d) => !d.open))).toEqual([])
    expect(expandEverythingState(chainProject, CL, r.state).state).toEqual(r.state)
    // 上限：钻满就停，说还有
    const capped = expandEverythingState(chainProject, CL, emptyState(), 3)
    expect(capped.more).toBe(true)
    expect(Object.values(capped.state.drills).filter(Boolean)).toHaveLength(3)
    // 旧的展开状态（一个步骤只开着一个）照样用：已经开着的留着，别的补上
    const old = open(into(n3, CK.b))
    const r2 = expandEverythingState(chainProject, CL, old)
    expect(chainFns(view(r2.state), n3)).toEqual([CK.a, CK.b, CK.c])
  })
})
