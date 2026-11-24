import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { cardFullText, edgeFullText, engineCut, mapLabelFullText, nodeFullText, textFullText } from './fulltext'
import { buildLineView, cardSize, cardSubtitle, expandAllState, nodeSize, parseProject, type CardView, type NodeView } from './graph'

const transfer = parseProject(JSON.parse(readFileSync(new URL('./graph/fixtures/transfer.json', import.meta.url), 'utf8')))
const v = buildLineView(transfer, transfer.lines[0].sid)
const step = v.nodes.find((n) => n.type === 'step')!
const at = (n: NodeView, map = false, dom = {}) => nodeFullText(n, nodeSize(n).width, map, dom)
/** 节点上的字：显示形式（画在节点上的）和完整形式；完整形式省略时同显示形式（引擎没截断） */
const node = (title: string, subtitle: string, full: { title?: string; subtitle?: string } = {}): NodeView => ({
  ...step,
  title,
  subtitle,
  titleFull: full.title ?? title,
  subtitleFull: full.subtitle ?? subtitle,
})
const withTitle = (c: CardView, title: string, titleFull = title): CardView => ({ ...c, title, titleFull })

describe('悬停显示完整文字', () => {
  it('没被截断的不显示', () => {
    expect(at(node('短标题', 'f'))).toBeNull()
  })

  it('标题被截断：显示完整标题；副标题没被截断就不重复', () => {
    const n = node('很长的标题'.repeat(12), 'f')
    expect(at(n)).toEqual({ title: n.title, sub: null })
  })

  it('副标题（函数名、条件原文）也被截断：第二行显示它', () => {
    const n = node('很长的标题'.repeat(12), 'module.function_name_that_is_long'.repeat(3))
    expect(at(n)).toEqual({ title: n.title, sub: n.subtitle })
    // 只有副标题被截断：仍然第一行标题、第二行副标题
    expect(at(node('短', n.subtitle))).toEqual({ title: '短', sub: n.subtitle })
  })

  it('估算说放得下、DOM 量到被截断（西文按单词换行）：也显示', () => {
    expect(at(node('短标题', 'f'), false, { title: true })).toEqual({ title: '短标题', sub: null })
  })

  it('引擎截断过（有完整形式）：显示形式在框里放得下也显示，浮层里是完整的', () => {
    const full = '先看这封邮件是不是已经处理过：同一个 message_id 可能被推送好几次，重复处理会让下游的统计多出一份'
    const n = node('先看这封邮件是不是已经处理过…', 'f', { title: full })
    expect(engineCut(n.title, n.titleFull)).toBe(true)
    expect(at({ ...n, titleFull: n.title })).toBeNull() // 同样的显示形式，没有完整形式：放得下就不显示
    expect(at(n)).toEqual({ title: full, sub: null })
    // 副标题（条件原文）被引擎截断：第二行写完整的
    const cond = 'message.attachments and message.sender_is_trusted and message.size < 10_000_000'
    expect(at(node('有附件？', 'message.attachments…', { subtitle: cond }))).toEqual({ title: '有附件？', sub: cond })
    // 结局胶囊（"返回 …"）一样
    const end = { ...v.nodes.find((x) => x.type === 'end' && !x.implicit)!, title: '返回 x…', titleFull: '返回 x + y' }
    expect(at(end)).toEqual({ title: '返回 x + y', sub: null })
  })

  it('地图模式：节点只剩色块，总是显示标题和副标题；开始节点显示函数标题', () => {
    expect(at(node('短标题', 'f'), true)).toEqual({ title: '短标题', sub: 'f' })
    expect(at(node('短…', 'f…', { title: '短而完整', subtitle: 'f(x)' }), true)).toEqual({ title: '短而完整', sub: 'f(x)' })
    const start = v.nodes.find((n) => n.type === 'start')!
    expect(at(start)).toBeNull()
    expect(at(start, true)).toEqual({ title: start.title, sub: null })
    expect(at({ ...start, titleFull: '完整的函数标题' }, true)).toEqual({ title: '完整的函数标题', sub: null })
  })

  it('卡片标题栏：标题、函数名被截断时显示；引擎截断过的放得下也显示；地图模式总是显示', () => {
    const c = v.cards[0]
    const w = (x: CardView) => cardSize(x).width
    expect(cardFullText(c, w(c), false)).toBeNull()
    expect(cardFullText(c, w(c), true)).toEqual({ title: c.title, sub: cardSubtitle(c) })
    const long = withTitle(c, '处理 Gmail 推送的入站邮件：分析（LLM 判定 + 规则）'.repeat(2))
    expect(cardFullText(long, w(long), false)).toEqual({ title: long.title, sub: null })
    const cut = withTitle(c, '账户转账…', '账户转账：查重、校验、冻结、执行、记账、通知')
    expect(cardFullText(cut, w(cut), false)).toEqual({ title: cut.titleFull, sub: null })
  })

  it('地图上的卡片标签：标签上是短标题时显示全文和函数名；就是全文时不显示', () => {
    const c = withTitle(v.cards[0], '处理 Gmail 推送的入站邮件：分析')
    expect(mapLabelFullText(c, '处理 Gmail 推送的入站邮件', false)).toEqual({ title: c.title, sub: cardSubtitle(c) })
    expect(mapLabelFullText(c, c.title, false)).toBeNull()
    expect(mapLabelFullText(c, `↩ ${c.title}`, false)).toBeNull()
    expect(mapLabelFullText(c, c.title, true)).not.toBeNull()
    // 标签上恰好是整个显示形式，但它是引擎截断过的：显示完整标题
    const cut = withTitle(v.cards[0], '账户转账…', '账户转账：查重、校验、冻结、执行、记账、通知')
    expect(mapLabelFullText(cut, cut.title, false)).toEqual({ title: cut.titleFull, sub: cardSubtitle(cut) })
  })

  it('分支标签：只有被引擎截断的 case 模式显示完整模式', () => {
    const all = buildLineView(transfer, transfer.lines[0].sid, expandAllState(transfer, transfer.lines[0].sid))
    const labelled = all.edges.find((e) => e.label !== '')!
    expect(edgeFullText(labelled)).toBeNull()
    const full = '{"kind": "invoice", "currency": currency, "amount": amount, **rest} if currency in allowed_currencies'
    const cut = { ...labelled, label: `${full.slice(0, 79)}…`, labelFull: full }
    expect(edgeFullText(cut)).toEqual({ title: 'match 分支的模式', sub: full })
  })

  it('左侧列表、标题区：DOM 量到被截断才显示；显示的是引擎截断过的形式时也显示', () => {
    expect(textFullText('标题', 'POST /pubsub', false)).toBeNull()
    expect(textFullText('标题', 'POST /pubsub', true)).toEqual({ title: '标题', sub: 'POST /pubsub' })
    expect(textFullText('标题', '', true)).toEqual({ title: '标题', sub: null })
    expect(textFullText('完整的标题', 'POST /pubsub', false, true)).toEqual({ title: '完整的标题', sub: 'POST /pubsub' })
  })
})
