import { describe, expect, it } from 'vitest'
import { reviewBlocks } from './reviewtext'

const TEXT = [
  '流程有变化：2 条功能线、1 个函数。',
  '新增、改动的节点给本次分析的行号，删除的给拿来比的那次（较早的）分析的行号；连线两端是本次分析的节点。',
  '',
  '## 功能线 [改动] 下单（订单）  line:a.py::f',
  '### 函数 [改动] 下单  a.py::f  a.py',
  '新增',
  '  - 步骤「查库存」（load）  a.py:5',
  '删除',
  '  - 结局「库存不够」  a.py:9（原来在「库存不够？」之后，是）',
  '改动',
  '  - 判断「不够？」 → 「够吗？」  a.py:7',
  '连线',
  '  - 连线新增：「」 —是 是→ 「」',
  '',
  '## 功能线 [删除] 退款（订单）  line:a.py::g',
  '…还有 3 条功能线未列出',
].join('\n')

describe('reviewBlocks', () => {
  it('把带记号的文字拆成块：说明、功能线、函数、分好组的条目，记号不留', () => {
    expect(reviewBlocks(TEXT)).toEqual([
      { kind: 'para', text: '流程有变化：2 条功能线、1 个函数。' },
      { kind: 'para', text: '新增、改动的节点给本次分析的行号，删除的给拿来比的那次（较早的）分析的行号；连线两端是本次分析的节点。' },
      { kind: 'line', status: '改动', title: '下单（订单）', where: ['line:a.py::f'] },
      { kind: 'fn', status: '改动', title: '下单', where: ['a.py::f', 'a.py'] },
      { kind: 'item', group: '新增', text: '步骤「查库存」（load）', where: ['a.py:5'] },
      { kind: 'item', group: '删除', text: '结局「库存不够」', where: ['a.py:9（原来在「库存不够？」之后，是）'] },
      { kind: 'item', group: '改动', text: '判断「不够？」 → 「够吗？」', where: ['a.py:7'] },
      { kind: 'item', group: '连线', text: '连线新增：「」 —是 是→ 「」', where: [] },
      { kind: 'line', status: '删除', title: '退款（订单）', where: ['line:a.py::g'] },
      { kind: 'para', text: '…还有 3 条功能线未列出' },
    ])
  })

  it('认不出的行照原样当一段话（去掉开头的 #），空的文字没有块', () => {
    expect(reviewBlocks('## 别的标题\n  - 没有分组的条目\n随便一行')).toEqual([
      { kind: 'para', text: '别的标题' },
      { kind: 'item', group: '', text: '没有分组的条目', where: [] },
      { kind: 'para', text: '随便一行' },
    ])
    expect(reviewBlocks('')).toEqual([])
  })
})
