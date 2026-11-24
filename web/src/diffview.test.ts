import { describe, expect, it } from 'vitest'
import type { EdgeChangeJson, FunctionChangeJson, GraphDiffJson, LineChangeJson, NodeChangeJson } from './api'
import {
  baselineRef,
  engineNote,
  buildDiffView,
  calleeNoteLines,
  cardChangeCount,
  changedNote,
  diffHeadline,
  lineChangeList,
  projectDiffNote,
  revealStop,
  viewMarks,
  type ChangeStop,
} from './diffview'
import { buildLineView, emptyState, setOpen } from './graph'
import { call, dispatch, fn, line, project, task, uncaught } from './graph/fixtures/builders'
import { CK, CL, at, bFn, chainProject, cFn, into, mainFn } from './graph/fixtures/chain'
import { drillChainOf } from './graph'

// 一个小项目（新的那次分析）：
//   下单 run（根任务）：查订单（调用 load）→ 库存够？─是→ 扣库存 → 派发发货（派发 ship）→ 完成
//                                              └否→ 记缺货 → 缺货（提前结束的短分支，默认折叠）
//   load ↔ deep 互相调用（环）；ship 是子任务；notify 是这次新增的函数（另一条线的根任务）。
const K = {
  run: 'flow.py::run@1',
  load: 'helpers.py::load@1',
  deep: 'helpers.py::deep@10',
  ship: 'tasks.py::ship@20',
  notify: 'tasks.py::notify@40',
}
const SID = {
  run: 'flow.py::run',
  load: 'helpers.py::load',
  deep: 'helpers.py::deep',
  ship: 'tasks.py::ship',
  notify: 'tasks.py::notify',
}

const run = fn(
  K.run,
  [
    ['n1', 'start', '下单'],
    ['n2', 'step', '查订单', { subtitle: 'load', calls: [call(K.load)] }],
    ['n3', 'cond', '库存够？', { subtitle: 'enough(stock)' }],
    ['n4', 'step', '扣库存', { subtitle: 'deduct' }],
    ['n5', 'step', '派发发货', { subtitle: '派发 ship', calls: [dispatch(K.ship)] }],
    ['n6', 'end', '完成', { end: 'ok' }],
    ['n7', 'step', '记缺货', { subtitle: 'log_shortage' }],
    ['n8', 'end', '缺货', { end: 'failed' }],
    uncaught('n9'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'yes'],
    ['n4', 'n5', 'next'],
    ['n5', 'n6', 'next'],
    ['n3', 'n7', 'no'],
    ['n7', 'n8', 'next'],
  ],
  { kind: 'route', title: '下单', route: 'POST /order' },
)
const load = fn(
  K.load,
  [['n1', 'start', 'load'], ['n2', 'step', '查库', { calls: [call(K.deep)] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '查订单（load）' },
)
const deep = fn(
  K.deep,
  [['n1', 'start', 'deep'], ['n2', 'step', '读缓存', { calls: [call(K.load)] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '深处（deep）' },
)
const ship = fn(
  K.ship,
  [['n1', 'start', '发货'], ['n2', 'step', '叫快递'], ['n3', 'end', '完成', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { kind: 'task', title: '发货' },
)
const notify = fn(
  K.notify,
  [['n1', 'start', '通知'], ['n2', 'cond', '要短信？'], ['n3', 'step', '发短信'], ['n4', 'end', '结束', { end: 'ok' }], uncaught('n5')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n2', 'n4', 'no'],
    ['n3', 'n4', 'next'],
  ],
  { kind: 'task', title: '通知' },
)
const all = [run, load, deep, ship, notify]
const L = line(task(K.run, [task(K.ship)]), all)
const L2 = line(task(K.notify), all)
const graph = project(all, [L, L2])

// ---------- 差异 JSON（字段照 core/src/pystepmap/diff.py 的 dataclass） ----------

function node(
  status: NodeChangeJson['status'],
  type: string,
  now: [id: string, sid: string, title: string, subtitle: string, line: number] | null,
  old: [sid: string, title: string, subtitle: string, line: number] | null,
  after = '',
  afterLabel = '',
): NodeChangeJson {
  return {
    status,
    type,
    new_id: now?.[0] ?? '',
    new_sid: now?.[1] ?? '',
    new_title: now?.[2] ?? '',
    new_subtitle: now?.[3] ?? '',
    new_line: now?.[4] ?? 0,
    old_sid: old?.[0] ?? '',
    old_title: old?.[1] ?? '',
    old_subtitle: old?.[2] ?? '',
    old_line: old?.[3] ?? 0,
    after,
    after_title: '',
    after_label: afterLabel,
  }
}

function edge(status: EdgeChangeJson['status'], src: string, dst: string, kind: string, srcTitle: string, dstTitle: string, label = ''): EdgeChangeJson {
  return { status, kind, label, src, dst, src_type: '', src_title: srcTitle, src_line: 0, dst_type: '', dst_title: dstTitle, dst_line: 0 }
}

function fnChange(status: FunctionChangeJson['status'], sid: string, title: string, newKey: string, nodes: NodeChangeJson[] = [], edges: EdgeChangeJson[] = []): FunctionChangeJson {
  return { status, sid, title, file: sid.split('::')[0], new_key: newKey, old_key: newKey || `${sid}@1`, nodes, edges }
}

function lineChange(status: LineChangeJson['status'], sid: string, title: string, over: Partial<LineChangeJson> = {}): LineChangeJson {
  return { status, sid, title, group: 'test', new_id: '', functions: [], tasks_added: [], tasks_removed: [], root: '', ...over }
}

const diff: GraphDiffJson = {
  version: 1,
  functions: {
    [SID.run]: fnChange('changed', SID.run, '下单', K.run, [
      node('changed', 'step', ['n2', 'step:n2', '查订单', 'load', 3], ['step:n2', '查询订单', 'load', 3]),
      node('added', 'step', ['n4', 'step:n4', '扣库存', 'deduct', 5], null),
      node('removed', 'step', null, ['step:x9', '记日志', 'log', 7], 'n5'),
      node('changed', 'step', ['n7', 'step:n7', '记缺货', 'log_shortage', 8], ['step:n7', '记录缺货', 'log_shortage', 9]),
      // 图里没有的节点（差异和图不是同一次分析）：忽略
      node('changed', 'step', ['n99', 'step:n99', '幽灵', '', 99], ['step:n99', '鬼', '', 99]),
    ], [
      // 留下的判断进新增节点的分支：画成绿色，但不单算一处变化（节点的新增已经说明了）
      edge('added', 'n3', 'n4', 'yes', '库存够？', '扣库存'),
      edge('added', 'n3', 'n7', 'no', '库存够？', '记缺货'),
      edge('removed', 'n3', 'n6', 'no', '库存够？', '完成'),
    ]),
    [SID.ship]: fnChange('changed', SID.ship, '发货', K.ship, [
      node('changed', 'step', ['n2', 'step:n2', '叫快递', '', 22], ['step:n2', '叫快递', '', 21]),
    ]),
    [SID.deep]: fnChange('changed', SID.deep, '深处（deep）', K.deep, [
      node('changed', 'step', ['n2', 'step:n2', '读缓存', '', 12], ['step:n2', '读数据库', '', 12]),
    ]),
    [SID.notify]: fnChange(
      'added',
      SID.notify,
      '通知',
      K.notify,
      notify.nodes.map((n) => node('added', n.type, [n.id, n.sid, n.title, n.subtitle, n.line], null)),
      [edge('added', 'n2', 'n3', 'yes', '要短信？', '发短信'), edge('added', 'n2', 'n4', 'no', '要短信？', '结束')],
    ),
    'gone.py::old': fnChange('removed', 'gone.py::old', '旧任务', ''),
    // 图里没有的函数：忽略
    'mystery.py::x': fnChange('changed', 'mystery.py::x', '神秘', 'mystery.py::x@1', [
      node('added', 'step', ['n2', 'step:n2', '?', '', 2], null),
    ]),
  },
  lines: [
    lineChange('changed', L.sid, '下单', {
      new_id: K.run,
      functions: [SID.run, SID.ship, SID.deep, 'gone.py::old'],
      tasks_removed: ['gone.py::old'],
      root: SID.run,
    }),
    lineChange('added', L2.sid, '通知', { new_id: K.notify, functions: [SID.notify], root: SID.notify }),
    lineChange('removed', 'line:gone.py::old', '旧功能', { group: '其他', root: 'gone.py::old' }),
    // 图里没有的线：忽略
    lineChange('changed', 'line:mystery.py::x', '神秘', { functions: ['mystery.py::x'] }),
  ],
}

const dv = buildDiffView(graph, diff)
const rootCard = `${L.sid}|${SID.run}`
const shipCard = `${L.sid}|${SID.run}>${SID.ship}`

describe('buildDiffView', () => {
  it('差异为 null：全空', () => {
    const v = buildDiffView(graph, null)
    expect(v.lineStatus.size + v.removedLines.length + v.nodeMarks.size + v.edgeMarks.size + v.removed.size).toBe(0)
    expect(v.changedFrom.size + v.functionStatus.size).toBe(0)
  })

  it('新增、改动的节点进 nodeMarks（按函数 key、图内节点 id）；改动的节点原来的样子进 changedFrom', () => {
    expect(dv.nodeMarks.get(K.run)).toEqual(
      new Map([
        ['n2', 'changed'],
        ['n4', 'added'],
        ['n7', 'changed'],
      ]),
    )
    expect(dv.changedFrom.get(K.run)?.get('n2')).toEqual({ title: '查询订单', subtitle: 'load', line: 3 })
    expect(dv.changedFrom.get(K.run)?.get('n7')).toEqual({ title: '记录缺货', subtitle: 'log_shortage', line: 9 })
    expect(dv.nodeMarks.get(K.ship)).toEqual(new Map([['n2', 'changed']]))
  })

  it('删掉的节点、连线进 removed：标题、原行号、原来在哪个节点之后（连线用两端的标题）', () => {
    expect(dv.removed.get(K.run)).toEqual([
      { what: 'node', type: 'step', title: '记日志', subtitle: 'log', line: 7, after: 'n5', label: '' },
      { what: 'edge', type: 'no', title: '库存够？ → 完成', subtitle: '否', line: 0, after: 'n3', label: '' },
    ])
  })

  it('新增的连线：键是"起点|终点|kind|label"（图内 id）', () => {
    expect(dv.edgeMarks.get(K.run)).toEqual(new Set(['n3|n4|yes|', 'n3|n7|no|']))
  })

  it('新增的函数：全部节点标 added，函数状态 added', () => {
    expect([...(dv.nodeMarks.get(K.notify)?.values() ?? [])]).toEqual(['added', 'added', 'added', 'added', 'added'])
    expect(dv.nodeMarks.get(K.notify)?.size).toBe(notify.nodes.length)
    expect(dv.functionStatus.get(K.notify)).toBe('added')
    expect(dv.functionStatus.get(K.run)).toBe('changed')
    expect(dv.edgeMarks.get(K.notify)).toEqual(new Set(['n2|n3|yes|', 'n2|n4|no|']))
  })

  it('功能线状态；被删的功能线（只是文字）', () => {
    expect(dv.lineStatus).toEqual(
      new Map([
        [L.sid, 'changed'],
        [L2.sid, 'added'],
      ]),
    )
    expect(dv.removedLines).toEqual([{ sid: 'line:gone.py::old', title: '旧功能', group: '其他' }])
  })

  it('差异里有图里没有的函数、节点、功能线（差异和图不是同一次分析）：忽略，不抛异常', () => {
    expect(dv.functionStatus.has('mystery.py::x@1')).toBe(false)
    expect(dv.nodeMarks.get(K.run)?.has('n99')).toBe(false)
    expect(dv.lineStatus.has('line:mystery.py::x')).toBe(false)
    // 节点 id 对得上、sid 对不上：按 sid 找；都找不到：忽略
    const shifted: GraphDiffJson = {
      version: 1,
      functions: {
        [SID.ship]: fnChange('changed', SID.ship, '发货', K.ship, [
          node('changed', 'step', ['n3', 'step:n2', '叫快递', '', 22], ['step:n2', '叫快递', '', 21]),
          node('added', 'step', ['n2', 'step:zzz', '?', '', 22], null),
          node('removed', 'step', null, ['step:q', '旧', '', 5], 'n77'),
        ], [edge('added', 'n1', 'n77', 'next', '发货', '?'), edge('removed', 'n77', 'n2', 'next', '?', '叫快递')]),
      },
      lines: [],
    }
    const v = buildDiffView(graph, shifted)
    expect(v.nodeMarks.get(K.ship)).toEqual(new Map([['n2', 'changed']]))
    expect(v.edgeMarks.get(K.ship) ?? new Set()).toEqual(new Set())
    expect(v.removed.get(K.ship)).toEqual([{ what: 'node', type: 'step', title: '旧', subtitle: '', line: 5, after: '', label: '' }])
    expect(() => buildDiffView(graph, { version: 1, functions: { x: { ...fnChange('changed', 'x', 'x', 'nope') } }, lines: [] })).not.toThrow()
  })

  it('有差异、但没有功能线涉及（diff.lines 为空）：没有线被标记，什么都不坏', () => {
    const v = buildDiffView(graph, { ...diff, lines: [] })
    expect(v.lineStatus.size).toBe(0)
    expect(v.removedLines).toEqual([])
    expect(v.nodeMarks.get(K.run)?.size).toBe(3)
    expect(lineChangeList(v, graph, L.sid)).toEqual([])
  })
})

const brief = (s: ChangeStop) => [s.functionKey, s.nodeId, s.kind]

describe('lineChangeList：这条线上的每一处变化，按画布上从上到下', () => {
  it('先根任务后子任务；同一函数里按行号；被调函数里的变化排在调用它的步骤之后', () => {
    const stops = lineChangeList(dv, graph, L.sid)
    expect(stops.map(brief)).toEqual([
      [K.run, 'n2', 'changed'],
      [K.deep, 'n2', 'changed'],
      [K.run, 'n3', 'added'],
      [K.run, 'n3', 'removed'],
      [K.run, 'n4', 'added'],
      [K.run, 'n5', 'removed'],
      [K.run, 'n7', 'changed'],
      [K.run, null, 'removed'],
      [K.ship, 'n2', 'changed'],
    ])
    // 被调函数里的那处：从哪个任务、经哪几步钻进去
    expect(stops[1]).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n2' }, { fn: K.load, node: 'n2' }] })
    expect(stops[8]).toMatchObject({ task: K.ship, path: [] })
  })

  it('文字：改动写原来和现在；标题没变写"语句有改动"；删除写原行号；连线写两端；派发树上少掉的任务', () => {
    const t = lineChangeList(dv, graph, L.sid).map((s) => s.text)
    expect(t[0]).toBe('改动步骤：查询订单 → 查订单')
    expect(t[2]).toBe('新增连线：库存够？ → 记缺货（否）')
    expect(t[3]).toBe('删除连线：库存够？ → 完成（否）')
    expect(t[4]).toBe('新增步骤：扣库存')
    expect(t[5]).toBe('删除步骤：记日志（原第 7 行）')
    expect(t[7]).toBe('这条线不再派发任务：旧任务')
    expect(t[8]).toBe('步骤「叫快递」语句有改动（原第 21 行）')
  })

  it('删除的那些带上原来的位置', () => {
    const removed = lineChangeList(dv, graph, L.sid).filter((s) => s.kind === 'removed')
    expect(removed.map((s) => s.where)).toEqual(['原来从「库存够？」出发', '原来在「派发发货」之后', ''])
  })

  it('新增的功能线：新增的函数整个算一处，定位到卡片', () => {
    expect(lineChangeList(dv, graph, L2.sid).map(brief)).toEqual([[K.notify, null, 'added']])
    expect(lineChangeList(dv, graph, L2.sid)[0].text).toBe('新增函数：通知')
  })

  it('没有变化的线、图里没有的线：空', () => {
    expect(lineChangeList(buildDiffView(graph, null), graph, L.sid)).toEqual([])
    expect(lineChangeList(dv, graph, 'line:nope')).toEqual([])
  })
})

describe('cardChangeCount：函数自己加上能钻入到的函数一共几处变化', () => {
  it('含被调函数里的变化（经 load 到 deep）；派发的子任务不算', () => {
    // run 自己：n2 改动、n3 新增连线、n3 删除连线、n4 新增、n5 之后删掉的节点、n7 改动 = 6；deep 1 处
    expect(cardChangeCount(dv, graph, K.run)).toBe(7)
    expect(cardChangeCount(dv, graph, K.ship)).toBe(1)
    expect(cardChangeCount(dv, graph, K.notify)).toBe(1)
  })

  it('有环（load ↔ deep）不死循环，每个函数只算一次', () => {
    expect(cardChangeCount(dv, graph, K.load)).toBe(1)
    expect(cardChangeCount(dv, graph, K.deep)).toBe(1)
  })

  it('图里没有的函数、没有差异：0', () => {
    expect(cardChangeCount(dv, graph, 'nope')).toBe(0)
    expect(cardChangeCount(buildDiffView(graph, null), graph, K.run)).toBe(0)
  })
})

describe('changedNote：详情栏里"原来是"', () => {
  it('标题变了：原来的标题和行号；条件 / 调用原文也变了时带上原来的', () => {
    expect(changedNote({ title: '查询订单', subtitle: 'load', line: 3 }, { titleFull: '查订单', subtitleFull: 'load' })).toEqual({
      text: '原来是：查询订单（原第 3 行）',
      code: null,
    })
    expect(changedNote({ title: '有货？', subtitle: 'a > 0', line: 4 }, { titleFull: '库存够？', subtitleFull: 'a >= 1' })).toEqual({
      text: '原来是：有货？（原第 4 行）',
      code: 'a > 0',
    })
  })

  it('标题相同、原文变了：写原来的原文；都一样：语句有改动', () => {
    expect(changedNote({ title: '库存够？', subtitle: 'a > 0', line: 4 }, { titleFull: '库存够？', subtitleFull: 'a >= 1' })).toEqual({
      text: '原来是（原第 4 行）：',
      code: 'a > 0',
    })
    expect(changedNote({ title: '叫快递', subtitle: '', line: 21 }, { titleFull: '叫快递', subtitleFull: '' })).toEqual({
      text: '语句有改动（原第 21 行）',
      code: null,
    })
  })
})

describe('viewMarks：画布上的标注', () => {
  it('节点、新增的连线、折叠着的卡片和短分支里的变化数、步骤调用的函数里的变化数', () => {
    const v = buildLineView(graph, L.sid)
    const m = viewMarks(v, dv, graph, lineChangeList(dv, graph, L.sid))
    expect(m.nodes.get(`${rootCard}/step:n2`)).toBe('changed')
    expect(m.nodes.get(`${rootCard}/step:n4`)).toBe('added')
    expect(m.nodes.has(`${rootCard}/step:n5`)).toBe(false)
    const yes = v.edges.find((e) => e.src === `${rootCard}/cond:n3` && e.dst === `${rootCard}/step:n4`)!
    expect(m.edges.has(yes.id)).toBe(true)
    // 否分支（记缺货 → 缺货）默认折叠：进折叠占位的那条连线是新增的；占位上 1 处变化
    const fold = v.folds.find((f) => f.collapsed)!
    expect(fold.keys).toEqual(['n7', 'n8'])
    const intoFold = v.edges.find((e) => e.dst === fold.id)!
    expect(m.edges.has(intoFold.id)).toBe(true)
    expect(m.counts.get(fold.id)).toBe(1)
    // 子任务卡片默认折叠：1 处
    expect(m.counts.get(shipCard)).toBe(1)
    // 查订单调用的 load → deep 里有 1 处
    expect(m.counts.get(`${rootCard}/step:n2`)).toBe(1)
    expect(m.cards.get(rootCard)).toBe('changed')
    expect(m.cards.get(shipCard)).toBe('changed')
    // 展开的根卡片不写数
    expect(m.counts.has(rootCard)).toBe(false)
  })

  it('新增的函数里的折叠不写数（卡片上已经标着新增）', () => {
    const added = buildDiffView(g2, {
      version: 1,
      functions: { 'svc.py::flow': fnChange('added', 'svc.py::flow', '受理转账', C.flow, flowFn.nodes.map((n) => node('added', n.type, [n.id, n.sid, n.title, '', n.line], null))) },
      lines: [],
    })
    const nv = buildLineView(g2, L3.sid)
    const m = viewMarks(nv, added, g2, lineChangeList(added, g2, L3.sid))
    expect(nv.folds.filter((f) => f.collapsed)).toHaveLength(1)
    expect(m.counts.size).toBe(0)
    expect(m.cards.get(flowCard)).toBe('added')
    expect(m.nodes.get(`${flowCard}/cond:n2`)).toBe('added')
  })

  it('没有视图、没有差异：全空', () => {
    const m = viewMarks(null, dv, graph, [])
    expect(m.nodes.size + m.edges.size + m.counts.size + m.cards.size).toBe(0)
    const n = viewMarks(buildLineView(graph, L.sid), buildDiffView(graph, null), graph, [])
    expect(n.nodes.size + n.edges.size + n.counts.size + n.cards.size).toBe(0)
  })
})

describe('revealStop：定位到一处变化（需要时展开卡片、分支，钻入被调函数）', () => {
  const stops = lineChangeList(dv, graph, L.sid)
  const visible = (state: ReturnType<typeof emptyState>, id: string) => {
    const v = buildLineView(graph, L.sid, state)
    return v.nodes.some((n) => n.id === id) || v.cards.some((c) => c.id === id) || v.folds.some((f) => f.id === id)
  }

  it('被调函数里的：钻入调用它的步骤（一层层），目标节点在画布上', () => {
    const r = revealStop(graph, L.sid, emptyState(), stops[1])!
    expect(r.target).toBe(`${rootCard}/step:n2>${SID.load}/step:n2>${SID.deep}/step:n2`)
    expect(r.kind).toBe('node')
    expect(visible(r.state, r.target)).toBe(true)
  })

  it('折叠着的子任务卡片里的：展开卡片', () => {
    const r = revealStop(graph, L.sid, emptyState(), stops[8])!
    expect(r.state.cards[shipCard]).toBe(true)
    expect(r.target).toBe(`${shipCard}/step:n2`)
    expect(visible(r.state, r.target)).toBe(true)
  })

  it('折叠着的短分支里的：展开分支', () => {
    const r = revealStop(graph, L.sid, emptyState(), stops[6])!
    expect(r.target).toBe(`${rootCard}/step:n7`)
    expect(Object.values(r.state.folds)).toContain(true)
    expect(visible(r.state, r.target)).toBe(true)
  })

  it('删掉的：定位到它原来前面的节点；整个函数 / 派发树上的：定位到卡片，状态不变', () => {
    expect(revealStop(graph, L.sid, emptyState(), stops[5])!.target).toBe(`${rootCard}/step:n5`)
    const r = revealStop(graph, L.sid, emptyState(), stops[7])!
    expect(r).toMatchObject({ target: rootCard, kind: 'card' })
    expect(r.state).toEqual(emptyState())
  })

  it('已经看得见：状态原样返回（同一个对象）', () => {
    const s = setOpen(emptyState(), 'cards', shipCard, true)
    expect(revealStop(graph, L.sid, s, stops[8])!.state).toBe(s)
  })

  it('任务不在这条线上：null', () => {
    expect(revealStop(graph, L.sid, emptyState(), { ...stops[0], task: 'nope' })).toBeNull()
  })
})

describe('diffHeadline：差异栏的第一句（对比基准只在标为已看时移动，所以不说"相对上一次分析"）', () => {
  const stops = lineChangeList(dv, graph, L.sid)
  const acked = { kind: 'acknowledged', when: '今天 14:05' } as const
  const first = { kind: 'first', when: '昨天 09:00' } as const

  it('按种类数：新增、改动、删除（没有的不写）；说相对上次标为已看时还是第一次分析，带时间', () => {
    expect(diffHeadline(stops, 'changed', acked)).toBe('这条线相对上次标为已看时（今天 14:05）：新增 2、改动 4、删除 3')
    expect(diffHeadline(stops.filter((s) => s.kind === 'changed'), 'changed', first)).toBe('这条线相对第一次分析（昨天 09:00）：改动 4')
    expect(diffHeadline(stops, 'changed', { kind: 'base', when: '今天 10:00' })).toBe('这条线相对对比基准（今天 10:00）：新增 2、改动 4、删除 3')
    expect(diffHeadline(stops.filter((s) => s.kind === 'changed'), 'changed', null)).toBe('这条线相对对比基准：改动 4')
  })

  it('新增的线：说是在哪之后新增的', () => {
    expect(diffHeadline(lineChangeList(dv, graph, L2.sid), 'added', acked)).toBe('这条线是上次标为已看（今天 14:05）之后新增的')
    expect(diffHeadline([], 'added', first)).toBe('这条线是第一次分析（昨天 09:00）之后新增的')
    expect(diffHeadline([], 'added', null)).toBe('这条线是对比基准之后新增的')
  })
})

describe('engineNote：引擎变了时差异栏多说的一句', () => {
  const head = { id: 5, created_at: '2026-11-16T05:00:00+00:00', engine_version: '0.1.0+bbbb', summary: {} as never, graph_size: 1, base_id: 3, has_diff: true }
  const base = { ...head, id: 3, engine_version: '0.1.0', base_id: null, has_diff: false }
  const fn = (sid: string, file_same?: boolean) => ({ status: 'changed' as const, sid, title: sid, file: 'a.py', new_key: '', old_key: '', nodes: [], edges: [], file_same })
  const diff = { version: 1, functions: { a: fn('a', true), b: fn('b', false), c: fn('c') }, lines: [] }

  it('这条线上所在文件没改的函数算引擎引起的；引擎没变、没有这样的函数时不说', () => {
    expect(engineNote({ head, base, diff, baseline: null }, ['a', 'b'])).toBe(
      '引擎也变了（0.1.0 → 0.1.0+bbbb）：这条线上流程有改动的 2 个函数里 1 个所在的文件没改，是引擎的画法变了引起的，不是代码改动',
    )
    expect(engineNote({ head, base, diff, baseline: null }, ['a'])).toContain('1 个函数所在的文件都没改')
    expect(engineNote({ head, base, diff, baseline: null }, ['b', 'c'])).toBeNull()
    expect(engineNote({ head, base: { ...base, engine_version: head.engine_version }, diff, baseline: null }, ['a'])).toBeNull()
    expect(engineNote({ head, base: null, diff: null, baseline: null }, ['a'])).toBeNull()
  })
})

describe('baselineRef：/diff 说对比基准是哪种', () => {
  const head = { id: 5, created_at: '2026-11-16T05:00:00+00:00', engine_version: '0.1.0', summary: {} as never, graph_size: 1, base_id: 3, has_diff: true }
  const base = { ...head, id: 3, created_at: '2026-11-16T04:00:00+00:00', base_id: null, has_diff: false }
  const when = (iso: string) => `「${iso.slice(11, 16)}」`

  it('上次标为已看时 / 第一次分析：用后端给的时间', () => {
    expect(baselineRef({ head, base, diff: null, baseline: { kind: 'acknowledged', at: '2026-11-16T04:30:00+00:00' } }, when)).toEqual({ kind: 'acknowledged', when: '「04:30」' })
    expect(baselineRef({ head, base, diff: null, baseline: { kind: 'first', at: '2026-11-16T01:00:00+00:00' } }, when)).toEqual({ kind: 'first', when: '「01:00」' })
  })

  it('后端没说（旧版）：按基准那次分析的时间说"对比基准"；没有基准：null', () => {
    expect(baselineRef({ head, base, diff: null, baseline: null }, when)).toEqual({ kind: 'base', when: '「04:00」' })
    expect(baselineRef({ head, base: null, diff: null, baseline: null }, when)).toBeNull()
    expect(baselineRef(null, when)).toBeNull()
  })
})

describe('删掉的节点放在哪里（after 是控制流上前面那个还在的节点，after_label 是挂在它哪条出边上）', () => {
  const runSid = SID.run
  const guard = (after: string, label = ''): GraphDiffJson => ({
    version: 1,
    functions: {
      [runSid]: fnChange('changed', runSid, '下单', K.run, [
        node('removed', 'cond', null, ['cond:g1', '超过限额？', 'over_limit(req)', 6], after, label),
      ]),
    },
    lines: [lineChange('changed', L.sid, '下单', { functions: [runSid], root: runSid })],
  })
  const fold = () => buildLineView(graph, L.sid).folds.find((f) => f.collapsed)!

  it('删掉一个守卫判断：定位到前面的判断节点，写清楚在它哪条分支上；前一个守卫的折叠分支不算它', () => {
    const v = buildDiffView(graph, guard('n3', '是'))
    const [stop] = lineChangeList(v, graph, L.sid)
    expect(stop).toMatchObject({ functionKey: K.run, nodeId: 'n3', kind: 'removed', text: '删除判断：超过限额？（原第 6 行）' })
    expect(stop.where).toBe('原来在「库存够？」之后（是）')
    expect(v.removed.get(K.run)).toEqual([{ what: 'node', type: 'cond', title: '超过限额？', subtitle: 'over_limit(req)', line: 6, after: 'n3', label: '是' }])
    const m = viewMarks(buildLineView(graph, L.sid), v, graph, lineChangeList(v, graph, L.sid))
    expect(m.counts.has(fold().id)).toBe(false)
    expect(revealStop(graph, L.sid, emptyState(), stop)).toMatchObject({ target: `${rootCard}/cond:n3`, kind: 'node' })
  })

  it('删掉的节点在折叠分支里的节点之后：只有锚点在这个折叠里时才算进它的数', () => {
    const v = buildDiffView(graph, guard('n7', ''))
    expect(viewMarks(buildLineView(graph, L.sid), v, graph, lineChangeList(v, graph, L.sid)).counts.get(fold().id)).toBe(1)
  })

  it('旧的差异（after 是结局节点）或者没有 after：定位到所在函数的卡片，写"原来在函数里（第 N 行）"，不算进任何折叠', () => {
    for (const after of ['n8', '', 'n404']) {
      const v = buildDiffView(graph, guard(after))
      const [stop] = lineChangeList(v, graph, L.sid)
      expect(stop).toMatchObject({ nodeId: null, kind: 'removed', where: '原来在函数「下单」里（第 6 行）' })
      expect(v.removed.get(K.run)?.[0].after).toBe('')
      expect(viewMarks(buildLineView(graph, L.sid), v, graph, lineChangeList(v, graph, L.sid)).counts.size).toBe(0)
      expect(revealStop(graph, L.sid, emptyState(), stop)).toMatchObject({ target: rootCard, kind: 'card' })
    }
  })

  it('落在函数卡片上的删除按原来的行号排在函数里的变化之间', () => {
    const d = guard('')
    d.functions[runSid].nodes.push(node('changed', 'step', ['n2', 'step:n2', '查订单', 'load', 3], ['step:n2', '查询订单', 'load', 3]))
    d.functions[runSid].nodes.push(node('changed', 'step', ['n7', 'step:n7', '记缺货', '', 8], ['step:n7', '记录缺货', '', 9]))
    expect(lineChangeList(buildDiffView(graph, d), graph, L.sid).map((s) => s.nodeId)).toEqual(['n2', null, 'n7'])
  })
})

// 判断函数（@condition）、或者别的只从不能钻入的节点调用的函数改了：变化落在调用它的那个节点上
const C = {
  flow: 'svc.py::flow@1',
  review: 'svc.py::needs_review@40',
  vip: 'svc.py::is_vip@50',
  audit: 'svc.py::audit@60',
  rule: 'rules.py::rule@1',
}
const flowFn = fn(
  C.flow,
  [
    ['n1', 'start', '受理'],
    ['n2', 'cond', '要复核？', { subtitle: 'self.needs_review(req)', calls: [call(C.review)] }],
    ['n3', 'step', '提交复核'],
    ['n4', 'end', '已转复核', { end: 'skipped', calls: [call(C.audit)] }],
    ['n5', 'cond', 'VIP？', { subtitle: 'self.is_vip(req)', calls: [call(C.vip)] }],
    ['n6', 'step', '转账'],
    ['n7', 'end', '完成', { end: 'ok' }],
    uncaught('n8'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n3', 'n4', 'next'],
    ['n2', 'n5', 'no'],
    ['n5', 'n6', 'yes'],
    ['n5', 'n6', 'no'],
    ['n6', 'n7', 'next'],
  ],
  { kind: 'task', title: '受理转账', call_targets: [C.review, C.audit, C.vip, C.rule] },
)
const condFn = (key: string, title: string, ret: string) =>
  fn(key, [['n1', 'start', title], ['n2', 'end', ret, { end: 'ok' }], uncaught('n3')], [['n1', 'n2', 'next']], { kind: 'condition', title })
const reviewFn = condFn(C.review, '需要复核？', '返回 a >= b')
const vipFn = condFn(C.vip, 'VIP 客户？', '返回 vip')
const auditFn = fn(C.audit, [['n1', 'start', '审计'], ['n2', 'step', '记审计'], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')], [
  ['n1', 'n2', 'next'],
  ['n2', 'n3', 'next'],
], { title: '审计' })
const ruleFn = fn(C.rule, [['n1', 'start', '规则'], ['n2', 'step', '算规则'], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')], [
  ['n1', 'n2', 'next'],
  ['n2', 'n3', 'next'],
], { title: '规则' })
const g2fns = [flowFn, reviewFn, vipFn, auditFn, ruleFn]
const L3 = line(task(C.flow), g2fns)
const g2 = project(g2fns, [L3])
const flowCard = `${L3.sid}|svc.py::flow`
const sidOf = (k: string) => k.split('@')[0]

function g2diff(over: (d: GraphDiffJson) => void = () => {}): GraphDiffJson {
  const d: GraphDiffJson = {
    version: 1,
    functions: {
      [sidOf(C.flow)]: fnChange('changed', sidOf(C.flow), '受理转账', C.flow, [node('added', 'cond', ['n5', 'cond:n5', 'VIP？', 'self.is_vip(req)', 6], null)]),
      [sidOf(C.review)]: fnChange('changed', sidOf(C.review), '需要复核？', C.review, [
        node('changed', 'end', ['n2', 'end:n2', '返回 a >= b', '', 42], ['end:n2', '返回 a > b', '', 42]),
      ]),
      [sidOf(C.vip)]: fnChange('added', sidOf(C.vip), 'VIP 客户？', C.vip, vipFn.nodes.map((n) => node('added', n.type, [n.id, n.sid, n.title, '', n.line], null))),
      [sidOf(C.audit)]: fnChange('changed', sidOf(C.audit), '审计', C.audit, [
        node('changed', 'step', ['n2', 'step:n2', '记审计', '', 62], ['step:n2', '写审计', '', 62]),
      ]),
      [sidOf(C.rule)]: fnChange('changed', sidOf(C.rule), '规则', C.rule, [node('changed', 'step', ['n2', 'step:n2', '算规则', '', 3], ['step:n2', '算老规则', '', 3])]),
    },
    lines: [
      lineChange('changed', L3.sid, '受理转账', {
        functions: [C.flow, C.review, C.vip, C.audit, C.rule].map(sidOf),
        root: sidOf(C.flow),
      }),
    ],
  }
  over(d)
  return d
}

describe('只从不能钻入的节点调用的函数（判断函数等）改了：标在调用它的节点上', () => {
  const v = buildDiffView(g2, g2diff())

  it('调用它的判断节点、结局节点标"改动"（原因是调用的函数变了）；自己新增的节点照旧是新增', () => {
    expect(v.nodeMarks.get(C.flow)).toEqual(
      new Map([
        ['n5', 'added'],
        ['n2', 'changed'],
        ['n4', 'changed'],
      ]),
    )
    expect(v.calleeMarked.get(C.flow)).toEqual(new Set(['n2', 'n4']))
    expect(v.functionStatus.get(C.flow)).toBe('changed')
  })

  it('详情栏写被调函数里变了什么', () => {
    const [note] = v.calleeNotes.get(C.flow)!.get('n2')!
    expect(note).toMatchObject({ fn: C.review, title: '需要复核？', condition: true, status: 'changed' })
    expect(calleeNoteLines(note)).toEqual(['判断函数里：「返回 a > b」改成「返回 a >= b」'])
    expect(calleeNoteLines(v.calleeNotes.get(C.flow)!.get('n4')![0])).toEqual(['函数「审计」里：「写审计」改成「记审计」'])
  })

  it('每一处：落在调用它的节点上；新增的节点调用新增的函数不另算；没有调用它的节点的才落在根卡片上', () => {
    const stops = lineChangeList(v, g2, L3.sid)
    expect(stops.map(brief)).toEqual([
      [C.flow, 'n2', 'changed'],
      [C.flow, 'n4', 'changed'],
      [C.flow, 'n5', 'added'],
      [C.rule, null, 'changed'],
    ])
    expect(stops[0].text).toBe('判断「要复核？」调用的判断函数「需要复核？」改了')
    expect(stops[2].text).toBe('新增判断：VIP？')
    expect(stops[3].text).toContain('画布上钻入不到')
  })

  it('折叠分支里调用它的节点算进折叠的数；卡片的数也算它', () => {
    const view = buildLineView(g2, L3.sid)
    const m = viewMarks(view, v, g2, lineChangeList(v, g2, L3.sid))
    const fold = view.folds.find((f) => f.collapsed)!
    expect(fold.keys).toEqual(['n3', 'n4'])
    expect(m.counts.get(fold.id)).toBe(1)
    expect(m.nodes.get(`${flowCard}/cond:n2`)).toBe('changed')
    expect(m.callee.has(`${flowCard}/cond:n2`)).toBe(true)
    expect(m.callee.has(`${flowCard}/cond:n5`)).toBe(false)
    expect(cardChangeCount(v, g2, C.flow)).toBe(3)
  })

  it('判断节点的标题就是判断函数的标题（flow-style 的常态）：不重复写', () => {
    const same = fn(C.flow, [['n1', 'start', '受理'], ['n2', 'cond', '需要复核？', { calls: [call(C.review)] }], ['n3', 'end', '完成', { end: 'ok' }]], [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'yes'],
      ['n2', 'n3', 'no'],
    ], { kind: 'task', title: '受理转账' })
    const g = project([same, reviewFn], [line(task(C.flow), [same, reviewFn])])
    const w = buildDiffView(g, { ...g2diff(), functions: { [sidOf(C.review)]: g2diff().functions[sidOf(C.review)] }, lines: [] })
    expect(w.changes.get(C.flow)?.map((c) => c.text)).toEqual(['判断「需要复核？」的判断函数改了'])
  })

  it('调用它的节点自己也改了：只算一处（节点自己的），被调函数里的变化仍在详情栏', () => {
    const w = buildDiffView(
      g2,
      g2diff((d) =>
        d.functions[sidOf(C.flow)].nodes.push(
          node('changed', 'cond', ['n2', 'cond:n2', '要复核？', 'self.needs_review(req)', 3], ['cond:n2', '需要审核？', 'self.needs_review(req)', 3]),
        ),
      ),
    )
    expect(lineChangeList(w, g2, L3.sid).filter((s) => s.nodeId === 'n2').map((s) => s.text)).toEqual(['改动判断：需要审核？ → 要复核？'])
    expect(w.calleeMarked.get(C.flow)?.has('n2')).toBe(false)
    expect(w.calleeNotes.get(C.flow)?.get('n2')?.[0].fn).toBe(C.review)
  })

  it('被调函数改了、但没有任何节点调用它（只在 call_targets 里）：照旧落在根卡片上', () => {
    const w = buildDiffView(g2, g2diff((d) => delete d.functions[sidOf(C.review)]))
    expect(lineChangeList(w, g2, L3.sid).map(brief)).toEqual([
      [C.flow, 'n4', 'changed'],
      [C.flow, 'n5', 'added'],
      [C.rule, null, 'changed'],
    ])
  })
})

describe('projectDiffNote：项目菜单里怎么说这次的流程变化', () => {
  it('有功能线变了：说几条、去列表里看（删掉的线也带上），不在菜单里标为已看', () => {
    expect(projectDiffNote(dv)).toEqual({ text: '有未看的流程变化：2 条功能线有变化、删掉了 1 条，左侧列表里标着"有变化""新增"，选中后画布上方可以逐处查看', ackHere: false })
  })

  it('只删掉了功能线：说删掉了几条，在菜单里标为已看（不会出现差异栏）', () => {
    const v = buildDiffView(graph, { ...diff, lines: diff.lines.filter((l) => l.status === 'removed') })
    expect(projectDiffNote(v)).toEqual({ text: '有未看的流程变化：删掉了 1 条功能线（列在左侧列表最下面）', ackHere: true })
  })

  it('变化都在不属于任何功能线的函数里：这样说（几个、列在哪里），在菜单里标为已看', () => {
    const v = buildDiffView(graph, { ...diff, lines: [] })
    expect(projectDiffNote(v)).toEqual({
      text: `有未看的流程变化，但都在不属于任何功能线的函数里（${v.offLine.length} 个，列在左侧列表最下面）`,
      ackHere: true,
    })
  })
})

describe('offLine：有变化、但不在任何有变化的功能线上的函数', () => {
  it('有变化的功能线涉及的函数不列；差异里没有功能线时每个有变化的函数都列（标题、文件、函数名、状态、几处）', () => {
    const onLines = new Set(diff.lines.flatMap((l) => l.functions))
    expect(dv.offLine.map((f) => f.sid)).toEqual(Object.keys(diff.functions).filter((sid) => !onLines.has(sid)))
    const all = buildDiffView(graph, { ...diff, lines: [] }).offLine
    expect(all.map((f) => f.sid)).toEqual(Object.keys(diff.functions))
    expect(all.length).toBeGreaterThan(0)
    for (const f of all) {
      const fc = diff.functions[f.sid]
      const name = (fc.new_key || fc.old_key).split('::')[1] ?? ''
      expect(f).toEqual({
        sid: f.sid,
        title: fc.title || name || f.sid,
        file: fc.file,
        name,
        status: fc.status,
        count: fc.status === 'changed' ? fc.nodes.length + fc.edges.length : 0,
      })
    }
  })

  it('没有差异时为空', () => {
    expect(buildDiffView(graph, null).offLine).toEqual([])
  })
})

// 同一个帮手函数既被步骤调用（能钻入）、又被判断调用的判断函数调用（不能钻入）：画布上看得到的每一处都是一处，数字等于处数
const R3 = {
  entry: 'api.py::refund_entry@1',
  refund: 'refund.py::refund@1',
  tooHigh: 'refund.py::fee_too_high@30',
  fee: 'money.py::fee_of@1',
}
const entryFn = fn(R3.entry, [['n1', 'start', '受理退款'], ['n2', 'step', '派发退款', { calls: [dispatch(R3.refund)] }], ['n3', 'end', '已受理', { end: 'ok' }]], [
  ['n1', 'n2', 'next'],
  ['n2', 'n3', 'next'],
], { kind: 'route', title: '受理退款' })
const refundFn = fn(
  R3.refund,
  [
    ['n1', 'start', '退款'],
    ['n2', 'step', '算手续费', { calls: [call(R3.fee)] }],
    ['n3', 'cond', '手续费太高？', { calls: [call(R3.tooHigh)] }],
    ['n4', 'end', '手续费太高', { end: 'failed' }],
    ['n5', 'end', '退款完成', { end: 'ok' }],
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'yes'],
    ['n3', 'n5', 'no'],
  ],
  { kind: 'task', title: '退款', call_targets: [R3.fee, R3.tooHigh] },
)
const tooHighFn = fn(R3.tooHigh, [['n1', 'start', '手续费太高？'], ['n2', 'end', '返回 fee_of(fee) > 10', { end: 'ok', calls: [call(R3.fee)] }]], [['n1', 'n2', 'next']], {
  kind: 'condition',
  title: '手续费太高？',
  call_targets: [R3.fee],
})
const feeFn = fn(R3.fee, [['n1', 'start', '手续费'], ['n2', 'step', '算千分之一'], ['n3', 'end', '返回', { end: 'ok' }]], [
  ['n1', 'n2', 'next'],
  ['n2', 'n3', 'next'],
], { title: '手续费' })
const r3fns = [entryFn, refundFn, tooHighFn, feeFn]
const L4 = line(task(R3.entry, [task(R3.refund)]), r3fns)
const g3 = project(r3fns, [L4])
const refundCard = `${L4.sid}|api.py::refund_entry>refund.py::refund`
const g3diff: GraphDiffJson = {
  version: 1,
  functions: {
    'money.py::fee_of': fnChange('changed', 'money.py::fee_of', '手续费', R3.fee, [
      node('changed', 'step', ['n2', 'step:n2', '算千分之一', '', 3], ['step:n2', '算百分之一', '', 3]),
    ]),
  },
  lines: [lineChange('changed', L4.sid, '受理退款', { functions: ['money.py::fee_of'], root: 'api.py::refund_entry' })],
}

describe('数字和每一处对得上（帮手函数既能钻入、又经判断函数调用到）', () => {
  const v = buildDiffView(g3, g3diff)
  const stops = lineChangeList(v, g3, L4.sid)

  it('钻入看到的那处和判断节点那处都是一处（不因为别处列过就丢掉）', () => {
    expect(stops.map(brief)).toEqual([
      [R3.fee, 'n2', 'changed'],
      [R3.refund, 'n3', 'changed'],
    ])
    expect(stops[0].path).toEqual([{ fn: R3.refund, node: 'n2' }])
  })

  it('折叠着的卡片上的数 = 这张卡片上的处数；整条线的处数 = 各处数之和', () => {
    const view = buildLineView(g3, L4.sid)
    const m = viewMarks(view, v, g3, stops)
    expect(m.counts.get(refundCard)).toBe(stops.filter((s) => s.task === R3.refund).length)
    expect([...m.counts.values()].reduce((a, b) => a + b, 0)).toBe(stops.length)
    expect(m.cardStops.get(refundCard)).toBe(2)
  })

  it('卡片展开后：步骤上的数（钻入后那处）+ 看得见的标了圈的节点 = 处数', () => {
    const view = buildLineView(g3, L4.sid, setOpen(emptyState(), 'cards', refundCard, true))
    const m = viewMarks(view, v, g3, stops)
    expect(m.counts.get(`${refundCard}/step:n2`)).toBe(1)
    expect(m.nodes.get(`${refundCard}/cond:n3`)).toBe('changed')
    expect(m.counts.has(`${refundCard}/cond:n3`)).toBe(false)
    const ringedStops = stops.filter((s) => s.path.length === 0 && s.nodeId !== null).length
    expect([...m.counts.values()].reduce((a, b) => a + b, 0) + ringedStops).toBe(stops.length)
    // 每一处都定位得到
    for (const s of stops) expect(revealStop(g3, L4.sid, emptyState(), s)).not.toBeNull()
  })

  it('自己的节点没变、只是调用的函数变了：不算"有改动"，卡片单独标成 callee（小地图不描琥珀色）', () => {
    expect(v.functionStatus.has(R3.refund)).toBe(false)
    expect(v.calleeOnly.has(R3.refund)).toBe(true)
    const m = viewMarks(buildLineView(g3, L4.sid), v, g3, stops)
    expect(m.cards.get(refundCard)).toBe('callee')
  })
})


// 钻入链（一个步骤同时钻入它调用的几个函数）：变化在第二个被调函数 b 里，c 里也有一处
describe('钻入链：变化落在链上对的那张卡片（或者没展开的那个被调函数的钻入选项）上', () => {
  const chainDiff: GraphDiffJson = {
    version: 1,
    functions: {
      [bFn.sid]: fnChange('changed', bFn.sid, '函数 b', CK.b, [node('changed', 'cond', ['n2', 'cond:n2', '要跳过？', '', 42], ['cond:n2', '要不要跳过？', '', 42])]),
      [cFn.sid]: fnChange('changed', cFn.sid, '函数 c', CK.c, [node('changed', 'step', ['n2', 'step:n2', '派发 t', '', 62], ['step:n2', '派发任务 t', '', 62])]),
    },
    lines: [lineChange('changed', CL, '主流程', { new_id: CK.main, functions: [mainFn.sid, bFn.sid, cFn.sid], root: mainFn.sid })],
  }
  const cdv = buildDiffView(chainProject, chainDiff)
  const stops = lineChangeList(cdv, chainProject, CL)
  const n2 = at('step:n2')
  const open = (...ids: string[]) => ids.reduce((acc, id) => setOpen(acc, 'drills', id, true), emptyState())
  const view = (st: ReturnType<typeof emptyState>) => buildLineView(chainProject, CL, st)
  const sum = (m: ReturnType<typeof viewMarks>) => [...m.counts.values()].reduce((a, b) => a + b, 0)

  it('每一处：c 的变化经 n2 的 a 钻入（在 a 里调用 c），b 的变化经 n2 钻入；同一个步骤的被调函数按源码先后', () => {
    expect(stops.map((x) => [x.functionKey, x.path.map((p) => p.fn)])).toEqual([
      [CK.c, [CK.main, CK.a]],
      [CK.b, [CK.main]],
    ])
    expect(stops.every((x) => x.path[0].node === 'n2')).toBe(true)
  })

  it('只开了第一个（a）：b 里的那处数在 b 的钻入选项上（链上卡片的"再展开"标它），不算到 a 的卡片上', () => {
    const v = view(open(into(n2, CK.a)))
    const m = viewMarks(v, cdv, chainProject, stops)
    expect(m.counts.get(into(n2, CK.b))).toBe(1)
    expect(m.counts.has(into(n2, CK.a))).toBe(false)
    // a 的卡片里"叫 c 和 d"没钻：c 那处在这个步骤上
    expect(m.counts.get(at('step:n2', into(n2, CK.a)))).toBe(1)
    expect(sum(m)).toBe(stops.length)
    expect(m.cardStops.get(into(n2, CK.a))).toBe(1)
  })

  it('两个都开：b 的变化标在 b 的卡片里的节点上，没有数', () => {
    const v = view(open(into(n2, CK.a), into(n2, CK.b)))
    const m = viewMarks(v, cdv, chainProject, stops)
    expect(m.nodes.get(at('cond:n2', into(n2, CK.b)))).toBe('changed')
    expect(m.counts.has(into(n2, CK.b))).toBe(false)
    expect(m.cardStops.get(into(n2, CK.b))).toBe(1)
    expect(m.cards.get(into(n2, CK.b))).toBe('changed')
    expect(m.cards.has(into(n2, CK.a))).toBe(false)
  })

  it('定位到 b 里的那处：钻入 b，已经开着的 a 照旧开着（排成链），目标是 b 的卡片里的节点', () => {
    const before = open(into(n2, CK.a))
    const r = revealStop(chainProject, CL, before, stops[1])!
    expect(r.state.drills[into(n2, CK.a)]).toBe(true)
    expect(r.state.drills[into(n2, CK.b)]).toBe(true)
    expect(r).toMatchObject({ target: at('cond:n2', into(n2, CK.b)), kind: 'node' })
    const v = view(r.state)
    expect(drillChainOf(v, n2).map((c) => c.fn)).toEqual([CK.a, CK.b])
    expect(v.nodes.some((n) => n.id === r.target)).toBe(true)
    // 原来没开：只开 b
    const fresh = revealStop(chainProject, CL, emptyState(), stops[1])!
    expect(Object.keys(fresh.state.drills)).toEqual([into(n2, CK.b)])
    // 已经看得见：状态原样
    expect(revealStop(chainProject, CL, r.state, stops[1])!.state).toBe(r.state)
  })
})
