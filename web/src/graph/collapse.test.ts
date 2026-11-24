import { describe, expect, it } from 'vitest'
import { analyzeFunction, branchLabel, cardExpandedByDefault, edgeBranch } from './collapse'
import { fn, uncaught, type EdgeSpec, type NodeSpec } from './fixtures/builders'
import { finFn, sharedFn } from './fixtures/shapes'

// 守卫式写法：if bad: alert(); return 失败  → 主干在 no 一侧
const guardNodes: NodeSpec[] = [
  ['n1', 'start'],
  ['n2', 'cond', '不对？'],
  ['n3', 'step', '告警'],
  ['n4', 'end', '失败', { end: 'failed' }],
  ['n5', 'step', '干活'],
  ['n6', 'step', '再干活'],
  ['n7', 'end', '完成', { end: 'ok' }],
  uncaught('n8'),
]
const guardEdges: EdgeSpec[] = [
  ['n1', 'n2', 'next'],
  ['n2', 'n3', 'yes'],
  ['n3', 'n4', 'next'],
  ['n2', 'n5', 'no'],
  ['n5', 'n6', 'next'],
  ['n6', 'n7', 'next'],
]

describe('主干', () => {
  it('取从开始到正常结局的最长路径', () => {
    const a = analyzeFunction(fn('a.py::f@1', guardNodes, guardEdges))
    expect(a.trunk).toEqual(['n1', 'n2', 'n5', 'n6', 'n7'])
  })

  it('有多个正常结局时取节点最多的那条', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond'],
        ['n3', 'step'],
        ['n4', 'step'],
        ['n5', 'step'],
        ['n6', 'end', '长的', { end: 'ok' }],
        ['n7', 'end', '短的', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n3', 'n4', 'next'],
        ['n4', 'n5', 'next'],
        ['n5', 'n6', 'next'],
        ['n2', 'n7', 'no'],
      ],
    )
    expect(analyzeFunction(f).trunk).toEqual(['n1', 'n2', 'n3', 'n4', 'n5', 'n6'])
  })

  it('没有 ok 结局时退到 skipped，再退到 failed', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond'],
        ['n3', 'step'],
        ['n4', 'end', '失败', { end: 'failed' }],
        ['n5', 'end', '跳过', { end: 'skipped' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n3', 'n4', 'next'],
        ['n2', 'n5', 'no'],
      ],
    )
    expect(analyzeFunction(f).trunk).toEqual(['n1', 'n2', 'n5'])
  })

  it('不走异常连线、不走回边', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'loop', 'for x in xs'],
        ['n3', 'step', '处理'],
        ['n4', 'catch', '捕获 E'],
        ['n5', 'step', '补救'],
        ['n6', 'step', '补救 2'],
        ['n7', 'step', '收尾'],
        ['n8', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'loop_each'],
        ['n3', 'n2', 'loop_back'],
        ['n3', 'n4', 'raise'],
        ['n4', 'n5', 'next'],
        ['n5', 'n6', 'next'],
        ['n6', 'n2', 'loop_back'],
        ['n2', 'n7', 'loop_exit'],
        ['n7', 'n8', 'next'],
      ],
    )
    const a = analyzeFunction(f)
    expect(a.trunk).toEqual(['n1', 'n2', 'n7', 'n8'])
    expect([...a.back].map((e) => `${e.src}>${e.dst}`).sort()).toEqual(['n3>n2', 'n6>n2'])
  })

  it('没有节点的函数、只有开始节点的函数不报错', () => {
    expect(analyzeFunction(fn('a.py::f@1', [], [])).trunk).toEqual([])
    const a = analyzeFunction(fn('a.py::f@1', [['n1', 'start'], uncaught('n2')], []))
    expect(a.trunk).toEqual(['n1'])
    expect(a.folds).toEqual([])
  })
})

describe('提前结束的短分支', () => {
  it('守卫分支折叠，摘要的成员正确', () => {
    const a = analyzeFunction(fn('a.py::f@1', guardNodes, guardEdges))
    expect(a.folds).toHaveLength(1)
    expect(a.folds[0]).toMatchObject({
      key: 'cond:n2:yes',
      kind: 'exit',
      anchor: 'n2',
      branch: 'yes',
      members: ['n3', 'n4'],
      defaultCollapsed: true,
      parent: null,
    })
  })

  it('只有一个结局节点的分支不折叠（占位节点不比它小）', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond'],
        ['n3', 'end', '直接返回', { end: 'skipped' }],
        ['n4', 'step'],
        ['n5', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n2', 'n4', 'no'],
        ['n4', 'n5', 'next'],
      ],
    )
    expect(analyzeFunction(f).folds).toEqual([])
    expect(analyzeFunction(f, { minFoldNodes: 1 }).folds.map((x) => x.members)).toEqual([['n3']])
  })

  it('超过 N 个节点不折叠；N 可配置', () => {
    const nodes: NodeSpec[] = [
      ['n1', 'start'],
      ['n2', 'cond'],
      ['n3', 'step'],
      ['n4', 'step'],
      ['n5', 'step'],
      ['n6', 'step'],
      ['n7', 'end', '失败', { end: 'failed' }],
      ['n8', 'step'],
      ['n9', 'step'],
      ['n10', 'step'],
      ['n11', 'step'],
      ['n12', 'step'],
      ['n13', 'end', '完成', { end: 'ok' }],
    ]
    const edges: EdgeSpec[] = [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'yes'],
      ['n3', 'n4', 'next'],
      ['n4', 'n5', 'next'],
      ['n5', 'n6', 'next'],
      ['n6', 'n7', 'next'],
      ['n2', 'n8', 'no'],
      ['n8', 'n9', 'next'],
      ['n9', 'n10', 'next'],
      ['n10', 'n11', 'next'],
      ['n11', 'n12', 'next'],
      ['n12', 'n13', 'next'],
    ]
    const f = fn('a.py::f@1', nodes, edges)
    expect(analyzeFunction(f).folds).toEqual([])
    expect(analyzeFunction(f, { shortBranchMax: 5 }).folds.map((x) => x.members)).toEqual([['n3', 'n4', 'n5', 'n6', 'n7']])
  })

  it('汇回主干的分支不算提前结束（只有两个节点，也不到长分支的门槛）', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond'],
        ['n3', 'step', '可选的一步'],
        ['n4', 'step', '可选的第二步'],
        ['n5', 'step', '汇合'],
        ['n6', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n3', 'n4', 'next'],
        ['n4', 'n5', 'next'],
        ['n2', 'n5', 'no'],
        ['n5', 'n6', 'next'],
      ],
    )
    const a = analyzeFunction(f)
    expect(a.trunk).toEqual(['n1', 'n2', 'n3', 'n4', 'n5', 'n6'])
    expect(a.folds).toEqual([])
  })

  it('分支里还有短分支：两层都折叠，里层的 parent 是外层', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond', '外层'],
        ['n3', 'cond', '里层'],
        ['n4', 'step'],
        ['n5', 'end', 'A', { end: 'failed' }],
        ['n6', 'end', 'B', { end: 'skipped' }],
        ['n7', 'step'],
        ['n8', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n3', 'n4', 'yes'],
        ['n4', 'n5', 'next'],
        ['n3', 'n6', 'no'],
        ['n2', 'n7', 'no'],
        ['n7', 'n8', 'next'],
      ],
    )
    const a = analyzeFunction(f)
    expect(a.folds.map((x) => [x.key, x.members, x.parent])).toEqual([
      ['cond:n2:yes', ['n3', 'n4', 'n5', 'n6'], null],
      ['cond:n3:yes', ['n4', 'n5'], 'cond:n2:yes'],
    ])
  })

  it('循环里的短分支（旧图：分支从 kind / label 推断）', () => {
    const nodes: NodeSpec[] = [
      ['n1', 'start'],
      ['n2', 'loop', 'for x in xs'],
      ['n3', 'cond', '坏的？'],
      ['n4', 'step', '告警'],
      ['n5', 'end', '失败', { end: 'failed' }],
      ['n6', 'end', '完成', { end: 'ok' }],
    ]
    const edges = (withBranch: boolean): EdgeSpec[] => [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'loop_each'],
      ['n3', 'n4', 'yes', '', withBranch ? 'yes' : null],
      ['n4', 'n5', 'next'],
      ['n3', 'n2', 'loop_back', '否', withBranch ? 'no' : null],
      ['n2', 'n6', 'loop_exit'],
    ]
    const newer = analyzeFunction(fn('a.py::f@1', nodes, edges(true)))
    const older = analyzeFunction(fn('a.py::f@1', nodes, edges(false)))
    expect(newer.folds.map((x) => x.key)).toEqual(['cond:n3:yes'])
    expect(older.folds).toEqual(newer.folds)
    expect(newer.trunk).toEqual(['n1', 'n2', 'n6'])
  })

  it('经过 finally 的提前返回也算以结局收尾', () => {
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'cond'],
        ['n3', 'step', '记一笔'],
        ['n4', 'end', '提前返回', { end: 'skipped' }],
        ['n5', 'step', '正常干活'],
        ['n6', 'step', 'finally 收尾'],
        ['n7', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'yes'],
        ['n3', 'n4', 'next'],
        ['n4', 'n6', 'finally'],
        ['n2', 'n5', 'no'],
        ['n5', 'n6', 'next'],
        ['n6', 'n7', 'next'],
      ],
    )
    const a = analyzeFunction(f)
    expect(a.trunk).toEqual(['n1', 'n2', 'n5', 'n6', 'n7'])
    expect(a.folds.map((x) => x.members)).toEqual([['n3', 'n4']])
  })
})

describe('异常处理', () => {
  // try: s() ... except E: 补救(); raise   /   except F: 记录()（汇回）  /   except G: return 默认值
  const f = fn(
    'a.py::f@1',
    [
      ['n1', 'start'],
      ['n2', 'step', '执行'],
      ['n3', 'catch', '捕获 E', { subtitle: 'E' }],
      ['n4', 'step', '补救'],
      ['n5', 'end', '抛出', { end: 'failed' }],
      ['n6', 'catch', '捕获 F', { subtitle: 'F' }],
      ['n7', 'step', '记录'],
      ['n8', 'catch', '捕获 G', { subtitle: 'G' }],
      ['n9', 'step', '算默认值'],
      ['n10', 'end', '返回默认值', { end: 'ok' }],
      ['n11', 'step', '后续'],
      ['n12', 'end', '完成', { end: 'ok' }],
    ],
    [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'raise'],
      ['n3', 'n4', 'next'],
      ['n4', 'n5', 'next'],
      ['n2', 'n6', 'raise'],
      ['n6', 'n7', 'next'],
      ['n7', 'n11', 'next'],
      ['n2', 'n8', 'raise'],
      ['n8', 'n9', 'next'],
      ['n9', 'n10', 'next'],
      ['n2', 'n11', 'next'],
      ['n11', 'n12', 'next'],
    ],
  )

  it('以失败收尾或汇回的处理体折叠（含捕获节点），以正常结局收尾的不折叠', () => {
    const a = analyzeFunction(f)
    expect(a.trunk).toEqual(['n1', 'n2', 'n11', 'n12'])
    expect(a.folds.map((x) => [x.key, x.kind, x.anchor, x.members])).toEqual([
      ['catch:n3', 'catch', 'n3', ['n3', 'n4', 'n5']],
      ['catch:n6', 'catch', 'n6', ['n6', 'n7']],
    ])
  })

  it('可以关掉', () => {
    expect(analyzeFunction(f, { foldCatches: false }).folds.every((x) => !x.defaultCollapsed)).toBe(true)
  })
})

describe('汇回的长分支（简报之外的补充规则）', () => {
  /** 开始 → pre 个步骤 → 判断 →（是：body 个步骤）→ 汇合 → 完成 */
  const optional = (pre: number, body: number) => {
    const nodes: NodeSpec[] = [['n1', 'start']]
    const edges: EdgeSpec[] = []
    let last = 'n1'
    let i = 2
    for (let k = 0; k < pre; k++, i++) {
      nodes.push([`n${i}`, 'step'])
      edges.push([last, `n${i}`, 'next'])
      last = `n${i}`
    }
    const cond = `n${i++}`
    nodes.push([cond, 'cond', '要不要推送？'])
    edges.push([last, cond, 'next'])
    last = cond
    const bodyIds: string[] = []
    for (let k = 0; k < body; k++, i++) {
      nodes.push([`n${i}`, 'step'])
      edges.push([last, `n${i}`, k === 0 ? 'yes' : 'next'])
      bodyIds.push(`n${i}`)
      last = `n${i}`
    }
    const merge = `n${i++}`
    nodes.push([merge, 'step', '汇合'])
    edges.push([last, merge, 'next'], [cond, merge, 'no'])
    nodes.push([`n${i}`, 'end', '完成', { end: 'ok' }], uncaught(`n${i + 1}`))
    edges.push([merge, `n${i}`, 'next'])
    return { f: fn('a.py::f@1', nodes, edges), cond, bodyIds }
  }

  it('主干长的函数里默认折叠，判断节点留着当标题', () => {
    const { f, cond, bodyIds } = optional(30, 3)
    const a = analyzeFunction(f)
    expect(a.trunk.length).toBeGreaterThan(30)
    expect(a.folds.map((x) => [x.kind, x.anchor, x.branch, x.members, x.defaultCollapsed])).toEqual([
      ['side', cond, 'yes', bodyIds, true],
    ])
  })

  it('主干短的函数里也能收起，但默认展开', () => {
    const { f } = optional(3, 3)
    expect(analyzeFunction(f).folds.map((x) => [x.kind, x.defaultCollapsed])).toEqual([['side', false]])
  })

  it('不到 sideBranchMin 个节点不提供；可以关掉；门槛可配置', () => {
    expect(analyzeFunction(optional(30, 2).f).folds).toEqual([])
    expect(analyzeFunction(optional(30, 3).f, { sideBranchMin: 0 }).folds).toEqual([])
    expect(analyzeFunction(optional(30, 3).f, { longTrunk: Number.POSITIVE_INFINITY }).folds[0].defaultCollapsed).toBe(false)
    expect(analyzeFunction(optional(3, 3).f, { longTrunk: 5 }).folds[0].defaultCollapsed).toBe(true)
  })

  it('经回边回到循环头的分支（循环体本身）不算', () => {
    // for x in xs: if skip: continue  /  否则 a(); b(); c()（走完回到循环头）
    const f = fn(
      'a.py::f@1',
      [
        ['n1', 'start'],
        ['n2', 'loop', 'for x in xs'],
        ['n3', 'cond', '跳过？'],
        ['n4', 'step'],
        ['n5', 'step'],
        ['n6', 'step'],
        ['n7', 'end', '完成', { end: 'ok' }],
      ],
      [
        ['n1', 'n2', 'next'],
        ['n2', 'n3', 'loop_each'],
        ['n3', 'n2', 'loop_back', '是'],
        ['n3', 'n4', 'no'],
        ['n4', 'n5', 'next'],
        ['n5', 'n6', 'next'],
        ['n6', 'n2', 'loop_back'],
        ['n2', 'n7', 'loop_exit'],
      ],
    )
    expect(analyzeFunction(f, { longTrunk: 0 }).folds).toEqual([])
  })

  it('长分支里的提前结束照样折叠，嵌在长分支里', () => {
    const { f, cond } = optional(30, 3)
    // 长分支的第二个节点改成判断：是 → 告警 → 失败
    const body2 = f.nodes.find((n) => n.id === 'n34')!
    body2.type = 'cond'
    body2.sid = 'cond:n34'
    f.nodes.push(
      { ...body2, id: 'n90', type: 'step', sid: 'step:n90', title: '告警' },
      { ...body2, id: 'n91', type: 'end', sid: 'end:n91', title: '失败', end_kind: 'failed' },
    )
    f.edges = f.edges.map((e) => (e.src === 'n34' && e.kind === 'next' ? { ...e, kind: 'no', branch: 'no' } : e))
    f.edges.push({ src: 'n34', dst: 'n90', kind: 'yes', label: '', branch: 'yes' }, { src: 'n90', dst: 'n91', kind: 'next', label: '' })
    const a = analyzeFunction(f)
    const side = a.folds.find((x) => x.kind === 'side')!
    const exit = a.folds.find((x) => x.kind === 'exit')!
    expect(side.anchor).toBe(cond)
    expect(side.members).toEqual(expect.arrayContaining(['n90', 'n91']))
    expect(exit).toMatchObject({ anchor: 'n34', members: ['n90', 'n91'], parent: side.key })
  })
})

describe('分支的识别和标签', () => {
  const match = fn(
    'a.py::f@1',
    [
      ['n1', 'start'],
      ['n2', 'cond', '看类型', { subtitle: 'match kind' }],
      ['n3', 'step'],
      ['n4', 'step'],
      ['n5', 'end', '完成', { end: 'ok' }],
    ],
    [
      ['n1', 'n2', 'next'],
      ['n2', 'n3', 'case', '"a"'],
      ['n2', 'n4', 'case', 'yes'],
      ['n2', 'n5', 'case', '都不匹配'],
      ['n3', 'n5', 'next'],
      ['n4', 'n5', 'next'],
    ],
  )

  it('match 的模式、都不匹配；模式恰好写成 yes 时不显示成"是"', () => {
    const a = analyzeFunction(match)
    expect(a.matchNodes.has('n2')).toBe(true)
    const labels = match.edges
      .filter((e) => e.src === 'n2')
      .map((e) => branchLabel(edgeBranch(a.nodes.get('n2'), e, true), true))
    expect(labels).toEqual(['"a"', 'yes', '都不匹配'])
  })

  it('旧图的 match 分支从 label 推断', () => {
    const old = { ...match, edges: match.edges.map(({ branch: _b, ...e }) => e) }
    const a = analyzeFunction(old)
    const branches = old.edges.filter((e) => e.src === 'n2').map((e) => edgeBranch(a.nodes.get('n2'), e, true))
    expect(branches).toEqual(['"a"', 'yes', 'else'])
  })

  it('raise 出边不是分支（即使 label 写着 是 / 否）', () => {
    const n = { ...match.nodes[1], subtitle: 'x > 1' }
    expect(edgeBranch(n, { src: 'n2', dst: 'n3', kind: 'raise', label: '是' }, false)).toBeNull()
    expect(edgeBranch(n, { src: 'n2', dst: 'n3', kind: 'loop_back', label: '否' }, false)).toBe('no')
    expect(branchLabel('no', false)).toBe('否')
  })
})

describe('任务卡片的默认状态', () => {
  it('根展开，其余折叠，goto 总是折叠', () => {
    expect(cardExpandedByDefault({ isRoot: true, goto: false })).toBe(true)
    expect(cardExpandedByDefault({ isRoot: false, goto: false })).toBe(false)
    expect(cardExpandedByDefault({ isRoot: false, goto: true })).toBe(false)
  })
})

describe('评审后的修订', () => {
  it('分支里的 continue 穿过 finally 是接着往下走：不算以结局收尾，算汇回', () => {
    const a = analyzeFunction(finFn)
    // "有问题"那条分支：记日志 → 严重？→（是：抛出 E / 否：经 finally 继续循环）。以前被当成提前结束的短分支
    expect(a.folds.map((f) => [f.kind, f.anchor, f.branch, f.members])).toEqual([['side', 'n3', 'yes', ['n4', 'n5', 'n6']]])
  })

  it('结局节点经 finally 离开函数仍然不算汇回', () => {
    // 只把 n5 的"否"改成直接结束：分支里只剩结局（都经 finally 离开），是提前结束的短分支
    const nodes = [...finFn.nodes]
    const f = { ...finFn, edges: finFn.edges.filter((e) => !(e.src === 'n5' && e.dst === 'n8')) }
    f.nodes = nodes
    expect(analyzeFunction(f).folds.map((x) => [x.kind, x.members])).toEqual([['exit', ['n4', 'n5', 'n6']]])
  })

  it('提前结束的分支以共用的结局收尾', () => {
    const a = analyzeFunction(sharedFn)
    expect(a.folds.map((f) => [f.kind, f.anchor, f.members])).toEqual([['exit', 'n2', ['n3', 'n4']]])
  })
})
