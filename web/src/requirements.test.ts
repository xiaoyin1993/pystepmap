import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { parseRequirements, type Requirement } from './api'
import { buildLineView, emptyState, expandAllState, parseProject } from './graph'
import {
  descriptionParagraphs,
  designLineSid,
  designOutcomeSid,
  findReq,
  firstReqKey,
  FOLD_ARCHIVED,
  foldDefault,
  lineRootKey,
  listMark,
  nodeStop,
  outcomeRows,
  parseReqRef,
  reachableNote,
  reqDiffRows,
  reqFoldsOf,
  reqItems,
  reqKeyOf,
  reqRef,
  reqTree,
  reqVerdict,
  reqViewMarks,
  statusClass,
} from './requirements'

// examples/transfer 的 GET /requirements（requirements.json）：R001 对得上；R002 改出了少了（风控打分）、分类（已转人工
// 复核设计成失败）、多了（查询可用余额）、走向（记一笔账和生成记账分录换了位置）；R003 读不懂；R004 功能线还不存在。
const data = parseRequirements(JSON.parse(readFileSync(new URL('./graph/fixtures/requirements.json', import.meta.url), 'utf8')))
const [ok, diff, bad, noLine] = data.requirements
const graph = data.graph!
const transfer = parseProject(JSON.parse(readFileSync(new URL('./graph/fixtures/transfer.json', import.meta.url), 'utf8')))
const TRANSFER_LINE = 'line:transfer_workflow.py::TransferWorkflow.run'

/** 需求的设计图视图：默认（短的结局分支折叠着）或全部展开 */
function designView(r: Requirement, expanded = true) {
  const line = designLineSid(graph, r)!
  return buildLineView(graph, line, expanded ? expandAllState(graph, line) : emptyState())
}

const withComparison = (r: Requirement, over: Partial<NonNullable<Requirement['comparison']>>): Requirement => ({
  ...r,
  comparison: { ...r.comparison!, ...over },
})

describe('列表', () => {
  it('同一个文件里的几块 flow（OpenSpec 的 design.md）各有各的 key，按 key、按编号都找得到各自那一条；已归档的标出来', () => {
    const file = 'openspec/changes/add-x/design.md'
    const blocks: Requirement[] = [
      { ...ok, id: 'add-x', file, title: 'add-x：查看' },
      { ...ok, id: 'add-x#2', file, title: 'add-x：登记' },
      { ...ok, id: 'archive/2026-01-01-old', file: 'openspec/changes/archive/2026-01-01-old/design.md', status: '已归档', archived: true },
    ]
    expect(blocks.map(reqKeyOf)).toEqual([file, `${file}#2`, 'openspec/changes/archive/2026-01-01-old/design.md'])
    expect(reqKeyOf(ok)).toBe(ok.file)
    const rows = reqItems(blocks)
    expect(new Set(rows.map((r) => r.key)).size).toBe(3)
    expect(rows.map((r) => r.archived)).toEqual([false, false, true])
    expect(rows[2].statusClass).toBe('st-other')
    expect(findReq(blocks, `${file}#2`)).toBe(blocks[1])
    expect(findReq(blocks, reqRef(blocks, blocks[1]))).toBe(blocks[1])
    expect(findReq(blocks, file)).toBe(blocks[0])
  })

  it('每行：编号、标题、状态和样式、写的功能线；对不上时的标记（几处不一致 / 找不到功能线 / 读不懂），对得上没有', () => {
    const rows = reqItems(data.requirements)
    expect(rows.map((r) => [r.id, r.title, r.status, r.statusClass, r.second])).toEqual([
      ['R001', '账户转账', '已确认', 'st-confirmed', '功能线：账户转账'],
      ['R002', '转账加风控', '已实现', 'st-done', '功能线：账户转账'],
      ['R003', '坏的需求', '草稿', 'st-draft', '功能线：账户转账'],
      ['R004', '退款', '草稿', 'st-draft', '功能线：退款'],
    ])
    expect(rows.map((r) => r.key)).toEqual(data.requirements.map((r) => r.file))
    expect(rows.map((r) => r.mark && [r.mark.text, r.mark.tone])).toEqual([
      null,
      ['4 处不一致', 'warn'],
      ['读不懂', 'bad'],
      ['找不到功能线', 'warn'],
    ])
    expect(listMark(bad)?.tip).toContain('第 2 行')
    expect(listMark(noLine)?.tip).toContain('图上找不到功能线「退款」')
  })

  it('搜索：编号、标题、功能线、状态，不分大小写；没写功能线、没有标题时的写法', () => {
    expect(reqItems(data.requirements, 'r00').map((r) => r.id)).toEqual(['R001', 'R002', 'R003', 'R004'])
    expect(reqItems(data.requirements, '  退款 ').map((r) => r.id)).toEqual(['R004'])
    expect(reqItems(data.requirements, '已实现').map((r) => r.id)).toEqual(['R002'])
    expect(reqItems(data.requirements, '风控').map((r) => r.id)).toEqual(['R002'])
    const [row] = reqItems([{ ...bad, title: '', line: '', status: '进行中' }])
    expect(row).toMatchObject({ title: 'R003-坏的需求.md', second: '没有写功能线', statusClass: 'st-other' })
  })

  it('读后端的结果时留着"已归档"；旧版后端没有这一项、写的不是 true 时不算已归档', () => {
    const raw = JSON.parse(readFileSync(new URL('./graph/fixtures/requirements.json', import.meta.url), 'utf8'))
    const [a, b, c] = raw.requirements
    const parsed = parseRequirements({ ...raw, requirements: [{ ...a, archived: true }, { ...b, archived: 'yes' }, c] })
    expect(parsed.requirements.map((r) => r.archived)).toEqual([true, false, false])
  })

  describe('分组', () => {
    // OpenSpec：进行中的变更 add-x 有两块、solo 只有一块，规格 specs/pay 有两块；已归档的两天各一个变更，另有一个目录名不带日期的
    const os = (id: string, dir: string, title: string, line: string, extra: Partial<Requirement> = {}): Requirement => ({
      ...ok,
      id,
      title,
      line,
      file: `openspec/${dir}/design.md`,
      comparison: { ...ok.comparison!, line_title: line },
      ...extra,
    })
    const old = { status: '已归档', archived: true }
    const reqs: Requirement[] = [
      ok,
      os('add-x', 'changes/add-x', 'add-x：查看资源', '查看资源'),
      os('add-x#2', 'changes/add-x', 'add-x：登记的小节', '登记资源'),
      os('solo', 'changes/solo', 'solo：看', '看'),
      os('specs/pay', 'specs/pay', 'specs/pay：付款', '付款', { status: '已实现' }),
      os('specs/pay#2', 'specs/pay', 'specs/pay：退款', '退款', { status: '已实现' }),
      os('archive/2026-01-01-old', 'changes/archive/2026-01-01-old', '2026-01-01-old：甲', '甲', old),
      os('archive/2026-03-05-new', 'changes/archive/2026-03-05-new', '2026-03-05-new：乙', '乙', old),
      os('archive/2026-03-05-new#2', 'changes/archive/2026-03-05-new', '2026-03-05-new：丙', '丙', old),
      os('archive/legacy', 'changes/archive/legacy', 'legacy：丁', '丁', old),
    ]

    it('每行：属于哪个变更（有几块时才分组）、行上写的名字（已归档的去掉 archive/ 和日期）、去掉变更名的标题、归档日期', () => {
      const rows = reqItems(reqs)
      expect(rows.map((r) => [r.group, r.name, r.short, r.date])).toEqual([
        ['', 'R001', '账户转账', ''],
        ['add-x', 'add-x', '查看资源', ''],
        ['add-x', 'add-x', '登记的小节', ''],
        ['', 'solo', '看', ''],
        ['specs/pay', 'specs/pay', '付款', ''],
        ['specs/pay', 'specs/pay', '退款', ''],
        ['', 'old', '甲', '2026-01-01'],
        ['archive/2026-03-05-new', 'new', '乙', '2026-03-05'],
        ['archive/2026-03-05-new', 'new', '丙', '2026-03-05'],
        ['', 'legacy', '丁', ''],
      ])
      // 组里的行：标题就是功能线时第二行不再写一遍
      expect(rows.slice(1, 3).map((r) => r.groupSecond)).toEqual(['', '功能线：登记资源'])
      // 搜索只剩一块时还算在那个变更的组里（看得出它是哪个变更的）
      expect(reqItems(reqs, '登记').map((r) => r.group)).toEqual(['add-x'])
    })

    it('正在做的按后端的顺序，几块的变更收成一组；已归档的按日期分组，新的在前，没有日期的放最后', () => {
      const tree = reqTree(reqItems(reqs))
      expect(
        tree.active.map((e) => (e.kind === 'item' ? e.item.key : [e.fold, e.name, e.status, e.statusClass, e.items.length])),
      ).toEqual([
        ok.file,
        ['change:add-x', 'add-x', '已确认', 'st-confirmed', 2],
        'openspec/changes/solo/design.md',
        ['change:specs/pay', 'specs/pay', '已实现', 'st-done', 2],
      ])
      expect(tree.archivedCount).toBe(4)
      expect(
        tree.archived.map((d) => [d.fold, d.label, d.count, d.entries.map((e) => (e.kind === 'item' ? e.item.short : `${e.name}×${e.items.length}`))]),
      ).toEqual([
        ['date:2026-03-05', '2026-03-05', 2, ['new×2']],
        ['date:2026-01-01', '2026-01-01', 1, ['甲']],
        ['date:', '没有日期', 1, ['丁']],
      ])
      const none = reqTree(reqItems(reqs, '没有这个'))
      expect([none.active, none.archived, none.archivedCount]).toEqual([[], [], 0])
    })

    it('收起：归档日期的组默认收起，别的默认展开；一条需求在哪几个能收起的组里', () => {
      expect(['change:add-x', FOLD_ARCHIVED, 'date:2026-03-05', 'date:'].map(foldDefault)).toEqual([false, false, true, true])
      const tree = reqTree(reqItems(reqs))
      const key = (id: string) => reqKeyOf(reqs.find((r) => r.id === id)!)
      expect(reqFoldsOf(tree, key('add-x#2'))).toEqual(['change:add-x'])
      expect(reqFoldsOf(tree, key('solo'))).toEqual([])
      // 已归档的变更在日期组里的小标题不能收：只有"已归档"和日期两层
      expect(reqFoldsOf(tree, key('archive/2026-03-05-new#2'))).toEqual([FOLD_ARCHIVED, 'date:2026-03-05'])
      expect(reqFoldsOf(tree, key('archive/legacy'))).toEqual([FOLD_ARCHIVED, 'date:'])
      expect(reqFoldsOf(tree, '')).toEqual([])
    })

    it('列表上的第一条：正在做的第一条；都归档了时最新那天的第一条（不是后端顺序里最早的）；没有需求时 null', () => {
      expect(firstReqKey(reqs)).toBe(ok.file)
      const archivedOnly = reqs.filter((r) => r.archived)
      expect(firstReqKey(archivedOnly)).toBe(reqKeyOf(reqs.find((r) => r.id === 'archive/2026-03-05-new')!))
      expect(firstReqKey([])).toBeNull()
    })
  })

  it('状态的样式；够得上的状态和写的不一样时说一句', () => {
    expect(['草稿', '已确认', '已实现', '已完成', ''].map(statusClass)).toEqual(['st-draft', 'st-confirmed', 'st-done', 'st-finished', 'st-other'])
    expect(reachableNote(ok)).toBe('按现在的对照结果，够得上的状态是「已实现」')
    expect(reachableNote(diff)).toBe('按现在的对照结果，够得上的状态是「已确认」')
    expect(reachableNote(bad)).toBeNull()
  })
})

describe('地址栏', () => {
  it('写编号；编号重复或者没有时写需求文件；读回来先按编号再按文件', () => {
    expect(reqRef(data.requirements, diff)).toBe('req:R002')
    const twins = [ok, { ...diff, id: 'R001' }]
    expect(reqRef(twins, twins[1])).toBe(`req:${diff.file}`)
    expect(reqRef([{ ...bad, id: '' }], { ...bad, id: '' })).toBe(`req:${bad.file}`)
    expect(findReq(data.requirements, 'req:R004')).toBe(noLine)
    expect(findReq(data.requirements, 'R002')).toBe(diff)
    expect(findReq(twins, `req:${diff.file}`)).toBe(twins[1])
    expect(findReq(twins, 'req:R001')).toBe(twins[0]) // 重复的编号：取第一条
    expect(findReq(data.requirements, 'req:R404')).toBeNull()
    expect(findReq(data.requirements, 'req:')).toBeNull()
  })

  it('功能线的 sid 不是需求', () => {
    expect(parseReqRef(TRANSFER_LINE)).toBeNull()
    expect(parseReqRef('req:R001')).toBe('R001')
    expect(parseReqRef('req:')).toBe('')
  })
})

describe('设计图', () => {
  it('需求在设计图里的功能线；读不懂的没有', () => {
    expect(designLineSid(graph, ok)).toBe(`line:${ok.design_line}`)
    expect(designLineSid(graph, bad)).toBeNull()
    expect(designLineSid(null, ok)).toBeNull()
  })

  it('按 sid 定位节点（设计图的节点、实现里"多了"的节点）；结局按标题找到设计图的节点', () => {
    expect(nodeStop(graph, diff.design_line!, 'step:L20')).toMatchObject({
      functionKey: diff.design_line,
      task: diff.design_line,
      path: [],
      nodeId: graph.functions[diff.design_line!].nodes.find((n) => n.sid === 'step:L20')!.id,
    })
    expect(nodeStop(graph, diff.design_line!, 'step:nope')).toBeNull()
    const root = lineRootKey(transfer, TRANSFER_LINE)!
    expect(root).toBe('transfer_workflow.py::TransferWorkflow.run@85')
    expect(nodeStop(transfer, root, 'step:be6595ab')?.nodeId).toBe(transfer.functions[root].nodes.find((n) => n.title === '查询可用余额')!.id)
    expect(lineRootKey(transfer, 'line:nope')).toBeNull()
    expect(designOutcomeSid(graph, diff, '已转人工复核')).toBe('end:L22')
    expect(designOutcomeSid(graph, diff, '没有这个结局')).toBeNull()
    expect(designOutcomeSid(graph, bad, '已转人工复核')).toBeNull()
  })
})

describe('设计图上的标记', () => {
  it('对得上、读不懂、没有视图：什么都不标', () => {
    expect(reqViewMarks(designView(ok), ok).nodes.size).toBe(0)
    expect(reqViewMarks(designView(ok), bad).nodes.size).toBe(0)
    expect(reqViewMarks(null, diff).nodes.size).toBe(0)
  })

  it('全部展开：少了的步骤红（还没有实现），分类、走向标在那条路的结局上；多了的不在设计图上', () => {
    const v = designView(diff)
    const root = v.cards[0].id
    const m = reqViewMarks(v, diff)
    expect([...m.nodes.keys()].sort()).toEqual([`${root}/end:L22`, `${root}/end:L32`, `${root}/step:L20`].sort())
    expect(m.nodes.get(`${root}/step:L20`)).toEqual({ kinds: ['missing'], label: '还没有实现', texts: ['设计里的步骤「风控打分」在实现里没有'] })
    expect(m.nodes.get(`${root}/end:L22`)).toMatchObject({ kinds: ['kind'], label: '分类不一样' })
    expect(m.nodes.get(`${root}/end:L32`)).toMatchObject({ kinds: ['order'], label: '走向不一致' })
    expect(m.nodes.get(`${root}/end:L32`)!.texts[0]).toMatch(/^走向不一致/)
    expect(m.counts.size).toBe(0)
    // 标出来的都是画布上真有的节点
    const ids = new Set(v.nodes.map((n) => n.id))
    for (const id of m.nodes.keys()) expect(ids.has(id)).toBe(true)
  })

  it('一个节点上几种都有：按 少了、分类、走向 排，用"·"连起来', () => {
    const r = withComparison(diff, {
      differences: [
        { kind: 'order', text: '走向不一致：…', type: 'end', title: '已转人工复核', node: '', design_nodes: ['end:L22'] },
        ...diff.comparison!.differences,
      ],
    })
    const v = designView(r)
    const m = reqViewMarks(v, r).nodes.get(`${v.cards[0].id}/end:L22`)
    expect(m).toMatchObject({ kinds: ['kind', 'order'], label: '分类不一样 · 走向不一致' })
    expect(m!.texts).toHaveLength(2)
  })

  it('默认状态（短的结局分支折叠着）：折叠里的数到占位上', () => {
    const v = designView(diff, false)
    const m = reqViewMarks(v, diff)
    const folded = v.folds.filter((f) => f.collapsed)
    expect(folded.length).toBeGreaterThan(0)
    // 人工复核那个分支（风控打分、已转人工复核）折叠着：2 个
    const review = folded.find((f) => f.members.some((id) => id.endsWith('/step:L20')))!
    expect(m.counts.get(review.id)).toBe(2)
    expect(m.nodes.has(`${v.cards[0].id}/step:L20`)).toBe(false)
    expect(m.nodes.has(`${v.cards[0].id}/end:L32`)).toBe(true)
  })

  it('功能线还不存在：设计里的每个节点都标"还没有实现"', () => {
    const v = designView(noLine)
    const m = reqViewMarks(v, noLine)
    const designed = v.nodes.filter((n) => n.type !== 'start')
    expect(designed.length).toBe(5)
    for (const n of designed) expect(m.nodes.get(n.id)?.label).toBe('还没有实现')
  })
})

describe('右栏', () => {
  it('结论：对得上（绿）、几处不一致、功能线找不到（琥珀）、读不懂（红）', () => {
    expect(reqVerdict(ok)).toEqual({ tone: 'ok', text: '设计和实现对得上' })
    expect(reqVerdict(diff)).toEqual({ tone: 'warn', text: '和实现有 4 处不一致' })
    expect(reqVerdict(noLine)).toEqual({ tone: 'warn', text: '图上找不到功能线「退款」（还没有实现，或者名字对不上、对上不止一条）' })
    expect(reqVerdict(bad)).toMatchObject({ tone: 'bad' })
  })

  it('差异列表：功能线找不到的那句是结论、不再列；"多了"带实现里的节点，别的带设计图的节点', () => {
    expect(reqDiffRows(diff).map((d) => [d.kind, d.name])).toEqual([
      ['missing', '少了'],
      ['kind', '分类'],
      ['extra', '多了'],
      ['order', '走向'],
    ])
    expect(reqDiffRows(diff)[2]).toMatchObject({ impl: 'step:be6595ab', design: [] })
    expect(reqDiffRows(diff)[1]).toMatchObject({ impl: '', design: ['end:L22'] })
    expect(reqDiffRows(noLine).map((d) => d.kind)).toEqual(['missing', 'missing', 'missing', 'missing', 'missing'])
    expect(reqDiffRows(bad)).toEqual([])
  })

  it('结局有没有测试走到：全都不知道时上面说一句、每行不重复；有运行时每行写清楚', () => {
    const none = outcomeRows(ok)
    expect(none.hint).toContain('还没有跑完的测试运行')
    expect(none.rows).toHaveLength(7)
    expect(none.rows.every((r) => r.text === '')).toBe(true)
    expect(none.rows[0]).toMatchObject({ title: '重复请求，不再处理', kind: 'skipped' })
    const states = ['covered', 'uncovered', 'uncertain', 'added', 'missing', 'unknown'] as const
    const mixed = withComparison(ok, {
      outcomes: states.map((covered, i) => ({ title: `结局${i}`, kind: 'ok', node: '', covered })),
    })
    const r = outcomeRows(mixed)
    expect(r.hint).toBeNull()
    expect(r.rows.map((x) => [x.text, x.tone])).toEqual([
      ['✓ 有测试走到', 'ok'],
      ['没有测试走到', 'bad'],
      ['不确定（运行之后代码改过）', 'unsure'],
      ['那次运行里还没有（重新运行测试才知道）', 'unsure'],
      ['实现里还没有', 'dim'],
      ['还没有跑完的测试运行', 'dim'],
    ])
    expect(outcomeRows(bad)).toEqual({ rows: [], hint: null })
  })

  it('说明分段：空行分段；段里的换行中文直接接上、英文补空格；列表项另起一行', () => {
    expect(descriptionParagraphs(ok.description)).toEqual([
      '用户发起一笔转账：重复提交的请求不再处理；参数不合法、超过单笔限额、余额不足时拒绝并说明原因；大额转账先交人工复核；其余的冻结资金后执行转账，转账出错时解冻资金并通知用户，成功后逐条记账并通知用户。',
    ])
    expect(descriptionParagraphs('第一段\n接着写。\n\n  \nSecond\nparagraph 在这里\r\n\r\n要点：\n- 甲\n- 乙\n1. 丙')).toEqual([
      '第一段接着写。',
      'Second paragraph 在这里',
      '要点：\n- 甲\n- 乙\n1. 丙',
    ])
    expect(descriptionParagraphs('')).toEqual([])
  })
})
