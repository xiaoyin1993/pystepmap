import { describe, expect, it } from 'vitest'
import type { FlowLine } from './graph'
import {
  NO_GROUP,
  applyOverrides,
  firstLine,
  groupLines,
  groupNames,
  overrideFromInput,
  patchOverride,
  restoreOne,
  saveNotice,
} from './overrides'

const L = (id: string, title: string, group: string, extra: Partial<FlowLine> = {}): FlowLine => ({
  id,
  title,
  group,
  kind: 'route',
  root: { function: id, goto: false, children: [] },
  sid: `line:${id}`,
  ...extra,
})

const lines = [
  L('a.py::create', 'POST /orders', '订单'),
  L('a.py::cancel', '取消订单：校验状态、退款…', '订单', { title_full: '取消订单：校验状态、退款、通知用户、记审计日志' }),
  L('b.py::sync', '同步库存', ''),
  L('c.py::report', '日报', '报表'),
]

describe('应用手工项', () => {
  it('没有手工项：显示图里的原值，完整标题取 title_full，没有分组的归到"其他"', () => {
    const d = applyOverrides(lines, {})
    expect(d.map((x) => [x.sid, x.title, x.titleFull, x.group, x.titleEdited, x.groupEdited])).toEqual([
      ['line:a.py::create', 'POST /orders', 'POST /orders', '订单', false, false],
      ['line:a.py::cancel', '取消订单：校验状态、退款…', '取消订单：校验状态、退款、通知用户、记审计日志', '订单', false, false],
      ['line:b.py::sync', '同步库存', '同步库存', NO_GROUP, false, false],
      ['line:c.py::report', '日报', '日报', '报表', false, false],
    ])
  })

  it('只改标题：显示和完整标题都换成手工的，分组不变，原值留着', () => {
    const [, cancel] = applyOverrides(lines, { 'line:a.py::cancel': { title: '取消订单' } })
    expect(cancel).toMatchObject({ title: '取消订单', titleFull: '取消订单', group: '订单', titleEdited: true, groupEdited: false })
    expect(cancel.original).toEqual({
      title: '取消订单：校验状态、退款…',
      titleFull: '取消订单：校验状态、退款、通知用户、记审计日志',
      group: '订单',
    })
  })

  it('只改分组：标题不变', () => {
    const [, , sync] = applyOverrides(lines, { 'line:b.py::sync': { group: '库存' } })
    expect(sync).toMatchObject({ title: '同步库存', group: '库存', titleEdited: false, groupEdited: true })
    expect(sync.original.group).toBe(NO_GROUP)
  })

  it('功能线不存在的孤儿项被忽略：不多出条目，也不报错', () => {
    const d = applyOverrides(lines, { 'line:gone.py::old': { title: '早就没了', group: '孤儿' }, 'line:c.py::report': { title: '每日报表' } })
    expect(d).toHaveLength(4)
    expect(d.map((x) => x.group)).not.toContain('孤儿')
    expect(d[3].title).toBe('每日报表')
  })

  it('空白的手工值当作没改；原型链上的名字（constructor 等）不会被当成手工项', () => {
    const d = applyOverrides([...lines, L('x.py::constructor', 'c', 'g', { sid: 'constructor' })], {
      'line:a.py::create': { title: '   ', group: '' },
    })
    expect(d[0]).toMatchObject({ title: 'POST /orders', titleEdited: false, groupEdited: false })
    expect(d[4]).toMatchObject({ title: 'c', titleEdited: false })
  })

  it('旧图（没有 sid）按 "line:" 加 id 对应', () => {
    const [x] = applyOverrides([L('z.py::f', 't', 'g', { sid: '' })], { 'line:z.py::f': { title: '新' } })
    expect(x).toMatchObject({ sid: 'line:z.py::f', title: '新' })
  })
})

describe('分组', () => {
  it('在应用手工项之后归组：组按第一条出现的顺序，组内保持原顺序', () => {
    const d = applyOverrides(lines, { 'line:c.py::report': { group: '订单' }, 'line:a.py::create': { group: '下单' } })
    expect(groupLines(d).map((g) => [g.name, g.items.map((x) => x.sid)])).toEqual([
      ['下单', ['line:a.py::create']],
      ['订单', ['line:a.py::cancel', 'line:c.py::report']],
      [NO_GROUP, ['line:b.py::sync']],
    ])
    expect(groupNames(d)).toEqual(['下单', '订单', NO_GROUP])
  })

  it('列表第一条：分组之后的第一组第一条（也就是图里的第一条）', () => {
    const d = applyOverrides(lines, { 'line:a.py::create': { group: 'Z' } })
    expect(firstLine(d)?.sid).toBe('line:a.py::create')
    expect(firstLine([])).toBeNull()
  })
})

describe('编辑框的值 → 要存的手工项', () => {
  const d = applyOverrides(lines, { 'line:a.py::cancel': { title: '取消订单', group: '售后' } })

  it('和原值相同、空着的项存成 null；都 null 就是删掉这条', () => {
    expect(overrideFromInput(d[1], ' 取消订单 ', '售后')).toEqual({ title: '取消订单', group: '售后' })
    expect(overrideFromInput(d[1], '取消订单：校验状态、退款、通知用户、记审计日志', '订单')).toEqual({ title: null, group: null })
    // 原来的截断形式也算原值
    expect(overrideFromInput(d[1], '取消订单：校验状态、退款…', '')).toEqual({ title: null, group: null })
    expect(overrideFromInput(d[2], '同步库存', NO_GROUP)).toEqual({ title: null, group: null })
    expect(overrideFromInput(d[2], '同步库存', '库存')).toEqual({ title: null, group: '库存' })
  })

  it('只恢复一项：另一项保持手工值', () => {
    expect(restoreOne(d[1], 'title')).toEqual({ title: null, group: '售后' })
    expect(restoreOne(d[1], 'group')).toEqual({ title: '取消订单', group: null })
    expect(restoreOne(d[0], 'title')).toEqual({ title: null, group: null })
  })
})

describe('保存之后的提示', () => {
  const edited = applyOverrides(lines, { 'line:a.py::cancel': { title: '退单', group: '售后' } })[1]
  const plain = applyOverrides(lines, {})[1]

  it('标题框只留空格（或清空）：恢复原标题，提示"已恢复原样"', () => {
    const next = overrideFromInput(edited, '   ', '售后')
    expect(next).toEqual({ title: null, group: '售后' })
    expect(saveNotice(edited, next)).toBe('已恢复原样')
    expect(saveNotice(edited, overrideFromInput(edited, '', '订单'))).toBe('已恢复原样')
  })

  it('设了新值：提示"已保存"（同时恢复了另一项也算保存）', () => {
    expect(saveNotice(plain, overrideFromInput(plain, '退单', '订单'))).toBe('已保存')
    expect(saveNotice(edited, overrideFromInput(edited, '   ', '新组'))).toBe('已保存')
    expect(saveNotice(edited, overrideFromInput(edited, '退单啦', '售后'))).toBe('已保存')
  })
})

describe('保存结果合进本地', () => {
  it('只动这一条：先发后到的另一条保存不会盖掉这条', () => {
    const local = { 'line:a': { title: 'A' }, 'line:b': { title: 'B 旧' } }
    // 服务器回来的全表里 line:a 是旧的（这次保存的是 line:b）
    const next = patchOverride(local, 'line:b', { 'line:a': { title: 'A 更旧' }, 'line:b': { title: 'B 新' } })
    expect(next).toEqual({ 'line:a': { title: 'A' }, 'line:b': { title: 'B 新' } })
    expect(local['line:b'].title).toBe('B 旧')
  })

  it('服务器上没有这条了（两项都清空）：本地也删掉', () => {
    expect(patchOverride({ 'line:a': { title: 'A' }, 'line:b': { group: 'g' } }, 'line:a', { 'line:b': { group: 'g' } })).toEqual({
      'line:b': { group: 'g' },
    })
  })
})
