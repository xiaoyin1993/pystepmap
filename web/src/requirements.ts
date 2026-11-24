// 需求（GET /requirements，后端 requirements_view.py）给界面用的几样东西：左边列表上的一行、设计图上要标出来的节点
// （和实现对不上的）、右栏的结论 / 差异 / "设计的结局有没有测试走到"、说明文字的分段、地址栏里怎么写一条需求、怎么
// 定位到设计图或实现图上的一个节点。都是纯函数（requirements.test.ts），状态和动作在 state.ts。
// 需求只读：界面上不改需求文件。

import type { ReqComparison, ReqCoverage, ReqDifference, ReqDiffKind, Requirement } from './api'
import type { ChangeStop } from './diffview'
import type { LineView, ProjectGraph } from './graph'

// ---------------------------------------------------------------------------------------------------------------
// 状态

/** 需求的四种状态（docs/superpowers/specs/2026-11-18-requirements-design.md 第 4 节）；别的写法照原样显示成灰色 */
export const REQ_STATUSES = ['草稿', '已确认', '已实现', '已完成'] as const

const STATUS_CLASS: Record<string, string> = { 草稿: 'st-draft', 已确认: 'st-confirmed', 已实现: 'st-done', 已完成: 'st-finished' }

/** 状态小标签的样式类：草稿灰、已确认蓝、已实现绿框、已完成绿底；不认识的灰 */
export const statusClass = (status: string): string => STATUS_CLASS[status] ?? 'st-other'

/** 按现在的对照结果够得上的状态和写的不一样时的一句；一样时 null */
export function reachableNote(r: Requirement): string | null {
  if (!r.reachable_status || r.reachable_status === r.status) return null
  return `按现在的对照结果，够得上的状态是「${r.reachable_status}」`
}

// ---------------------------------------------------------------------------------------------------------------
// 列表

/** 列表上标题旁的小标记：读不懂（红）、找不到功能线 / 有几处不一致（琥珀）；对得上时没有 */
export interface ReqListMark {
  text: string
  tone: 'bad' | 'warn'
  tip: string
}

/**
 * 一条需求在列表、选中状态里的 key：需求文件；同一个文件里的第二个 flow 代码块起（OpenSpec 的 design.md 里可以有几块，
 * 编号是 变更名#2、#3……）后面接 #n，不然几块共用一个 key，点哪一块选中的都是第一块。
 */
export function reqKeyOf(r: Requirement): string {
  const n = r.id.includes('#') ? r.id.slice(r.id.lastIndexOf('#')) : ''
  return /^#\d+$/.test(n) ? r.file + n : r.file
}

export interface ReqItem {
  /** 选中、列表 key 用：reqKeyOf（一个项目里不会重复） */
  key: string
  /** OpenSpec 已归档的变更里的：列表里和正在做的分开放 */
  archived: boolean
  id: string
  /** 完整的标题（OpenSpec 的是"变更名：小节标题"）；提示里用 */
  title: string
  status: string
  statusClass: string
  /** 第二行：写的功能线 */
  second: string
  mark: ReqListMark | null
  /** 搜索用 */
  hay: string
  /** OpenSpec 的一个变更（规格）里有几块 flow 时：它们共同的编号（去掉 #n），列表里收成一组；不然空 */
  group: string
  /** 行上（或组的标题上）编号的位置写的：自己的需求是编号；OpenSpec 的是变更名（已归档的去掉 archive/ 和开头的日期） */
  name: string
  /** 行上的标题：OpenSpec 的去掉开头的"变更名："（组里、行上已经写了变更名） */
  short: string
  /** 已归档的变更的归档日期（目录名开头的 YYYY-MM-DD，OpenSpec 归档时加的）；没有时空 */
  date: string
  /** 在组里时的第二行：标题就是功能线时不再写一遍（空） */
  groupSecond: string
}

const ARCHIVE_PREFIX = 'archive/'
const DATED = /^(\d{4}-\d{2}-\d{2})-(.+)$/
const isOpenSpec = (r: Requirement): boolean => r.file.startsWith('openspec/')
/** OpenSpec 的一块 flow 属于哪个变更（规格）：编号去掉 #n */
const changeOf = (r: Requirement): string => r.id.replace(/#\d+$/, '')

const noLineOf = (c: ReqComparison | null): ReqDifference | undefined => c?.differences.find((d) => d.kind === 'no_line')

export function listMark(r: Requirement): ReqListMark | null {
  if (r.errors.length || !r.comparison) {
    return { text: '读不懂', tone: 'bad', tip: r.errors.length ? `需求文件读不懂：${r.errors.join('；')}` : '需求文件读不懂，没有和实现对照' }
  }
  const c = r.comparison
  const noLine = noLineOf(c)
  if (noLine) return { text: '找不到功能线', tone: 'warn', tip: noLine.text }
  if (c.differences.length) return { text: `${c.differences.length} 处不一致`, tone: 'warn', tip: `设计的流程和实现有 ${c.differences.length} 处不一致` }
  return null
}

/**
 * 列表的行（按后端的顺序，也就是文件名的顺序）；query 按编号、标题、功能线、状态找（不分大小写）。属于哪个变更按全部需求算
 * （搜索只剩一块时还在那个变更的组里）。
 */
export function reqItems(reqs: Requirement[], query = ''): ReqItem[] {
  const q = query.trim().toLowerCase()
  const blocks = new Map<string, number>()
  for (const r of reqs) if (isOpenSpec(r)) blocks.set(changeOf(r), (blocks.get(changeOf(r)) ?? 0) + 1)
  return reqs
    .map((r): ReqItem => {
      const line = r.comparison?.line_title || r.line
      const title = r.title || r.file.split('/').pop() || r.file
      const second = line ? `功能线：${line}` : '没有写功能线'
      let name = r.id
      let short = title
      let date = ''
      let group = ''
      if (isOpenSpec(r)) {
        const change = changeOf(r)
        // 后端的标题是"<变更名>：<小节标题或功能线>"，已归档的变更名是目录名（带日期）
        const dir = r.archived && change.startsWith(ARCHIVE_PREFIX) ? change.slice(ARCHIVE_PREFIX.length) : change
        const m = r.archived ? DATED.exec(dir) : null
        name = m ? m[2] : dir
        date = m ? m[1] : ''
        short = title.startsWith(`${dir}：`) ? title.slice(dir.length + 1) : title === dir ? line || title : title
        group = (blocks.get(change) ?? 0) > 1 ? change : ''
      }
      return {
        key: reqKeyOf(r),
        archived: r.archived === true,
        id: r.id,
        title,
        status: r.status,
        statusClass: statusClass(r.status),
        second,
        mark: listMark(r),
        hay: `${r.id} ${r.title} ${r.line} ${line} ${r.status}`.toLowerCase(),
        group,
        name,
        short,
        date,
        groupSecond: line && line === short ? '' : second,
      }
    })
    .filter((it) => !q || it.hay.includes(q))
}

// 列表的分组：正在做的（自己的需求、OpenSpec 进行中的变更和规格）在上面，一个变更里有几块时收成一个能收起的组；已归档的
// 变更在下面"已归档"一组里，按归档日期再分（日期的组默认收起），同一天里有几块的变更只加一个小标题、不能单独收。
// 能收起的组用 fold 区分（记在浏览器里，groupFold.ts 的 REQ_FOLDS）。

/** 列表里的一项：一条单独的需求，或者同一个变更里的几块 */
export type ReqEntry =
  | { kind: 'item'; item: ReqItem }
  | { kind: 'change'; fold: string; name: string; status: string; statusClass: string; items: ReqItem[] }

export interface ReqDateGroup {
  fold: string
  /** YYYY-MM-DD；目录名不带日期的为空 */
  date: string
  /** 组的标题：日期，或者"没有日期" */
  label: string
  count: number
  entries: ReqEntry[]
}

export interface ReqTree {
  active: ReqEntry[]
  /** 新的日期在前，没有日期的放最后 */
  archived: ReqDateGroup[]
  archivedCount: number
}

/** "已归档"整组的 fold */
export const FOLD_ARCHIVED = 'archived'
const FOLD_CHANGE = 'change:'
const FOLD_DATE = 'date:'

/** 一个组默认收不收：归档日期的组默认收起（已归档的只看得到一串日期），别的默认展开 */
export const foldDefault = (fold: string): boolean => fold.startsWith(FOLD_DATE)

function entriesOf(items: ReqItem[]): ReqEntry[] {
  const out: ReqEntry[] = []
  const changes = new Map<string, Extract<ReqEntry, { kind: 'change' }>>()
  for (const it of items) {
    if (!it.group) {
      out.push({ kind: 'item', item: it })
      continue
    }
    let e = changes.get(it.group)
    if (!e) {
      e = { kind: 'change', fold: FOLD_CHANGE + it.group, name: it.name, status: it.status, statusClass: it.statusClass, items: [] }
      changes.set(it.group, e)
      out.push(e)
    }
    e.items.push(it)
  }
  return out
}

export function reqTree(items: ReqItem[]): ReqTree {
  const dates = new Map<string, ReqItem[]>()
  for (const it of items) if (it.archived) dates.set(it.date, [...(dates.get(it.date) ?? []), it])
  const archived = [...dates]
    .sort(([a], [b]) => (a === '' ? 1 : b === '' ? -1 : b.localeCompare(a)))
    .map(([date, list]): ReqDateGroup => ({ fold: FOLD_DATE + date, date, label: date || '没有日期', count: list.length, entries: entriesOf(list) }))
  return {
    active: entriesOf(items.filter((it) => !it.archived)),
    archived,
    archivedCount: archived.reduce((n, d) => n + d.count, 0),
  }
}

/** 列表上显示的第一条（切到需求、原来看的那条没了时选它）：正在做的第一条，都归档了时最新那天的第一条；没有需求时 null */
export function firstReqKey(reqs: Requirement[]): string | null {
  const tree = reqTree(reqItems(reqs))
  const e = tree.active[0] ?? tree.archived[0]?.entries[0]
  return !e ? null : e.kind === 'item' ? e.item.key : e.items[0].key
}

/** 一条需求（reqKeyOf）在哪几个能收起的组里，从外到里；不在列表里、不在组里时空 */
export function reqFoldsOf(tree: ReqTree, key: string): string[] {
  const has = (e: ReqEntry) => (e.kind === 'item' ? e.item.key === key : e.items.some((it) => it.key === key))
  const change = tree.active.find((e) => e.kind === 'change' && has(e))
  if (change?.kind === 'change') return [change.fold]
  const day = tree.archived.find((d) => d.entries.some(has))
  return day ? [FOLD_ARCHIVED, day.fold] : []
}

// ---------------------------------------------------------------------------------------------------------------
// 地址栏：#/<项目>/req:<编号>（编号在项目里重复或者没有时写需求文件），和功能线的 sid 分得开（那些以 line: 开头）

export const REQ_REF = 'req:'

/** 地址栏里那一段是不是一条需求；是时返回 req: 后面的部分，不是时 null */
export function parseReqRef(s: string): string | null {
  return s.startsWith(REQ_REF) ? s.slice(REQ_REF.length) : null
}

export function reqRef(reqs: Requirement[], r: Requirement): string {
  const unique = r.id !== '' && reqs.filter((x) => x.id === r.id).length === 1
  return REQ_REF + (unique ? r.id : reqKeyOf(r))
}

/** 按地址栏里的写法（带不带 req: 都行）找需求：先按编号（只有一条时），再按需求文件 */
export function findReq(reqs: Requirement[], ref: string): Requirement | null {
  const v = parseReqRef(ref) ?? ref
  if (!v) return null
  const byId = reqs.filter((r) => r.id === v)
  if (byId.length === 1) return byId[0]
  return reqs.find((r) => reqKeyOf(r) === v) ?? reqs.find((r) => r.file === v) ?? byId[0] ?? null
}

// ---------------------------------------------------------------------------------------------------------------
// 设计图

/** 需求在设计图里的功能线 sid；读不懂、图里没有时 null */
export function designLineSid(graph: ProjectGraph | null, r: Requirement | null): string | null {
  if (!graph || !r?.design_line) return null
  return graph.lines.find((l) => l.id === r.design_line)?.sid ?? null
}

/**
 * 在一张图的某个任务函数里按 sid 找节点，给 diffview 的 revealStop 定位用（展开把它收起来的分支、移到画布中间）。
 * 设计图：函数是需求的 design_line；实现图："多了"的节点只在功能线的根函数里（后端只在根函数里报），函数是功能线的 id。
 */
export function nodeStop(graph: ProjectGraph | null, fnKey: string, sid: string): ChangeStop | null {
  const n = graph?.functions[fnKey]?.nodes.find((x) => x.sid === sid)
  return n ? { functionKey: fnKey, nodeId: n.id, kind: 'changed', text: '', where: '', task: fnKey, path: [] } : null
}

/** 实现的功能线（sid）的根函数 key；图里没有这条线时 null */
export function lineRootKey(graph: ProjectGraph | null, lineSid: string): string | null {
  return graph?.lines.find((l) => l.sid === lineSid)?.id ?? null
}

/** 设计的一个结局在设计图里的节点 sid（按标题找；同名的取第一个）；找不到时 null */
export function designOutcomeSid(graph: ProjectGraph | null, r: Requirement | null, title: string): string | null {
  if (!graph || !r?.design_line) return null
  const n = graph.functions[r.design_line]?.nodes.find((x) => x.type === 'end' && (x.title_full || x.title) === title)
  return n?.sid ?? null
}

// ---------------------------------------------------------------------------------------------------------------
// 设计图上的标记

/** 画在设计图节点上的三种：少了（红）、结局分类不一样、走向不一致（琥珀） */
export type ReqMarkKind = 'missing' | 'kind' | 'order'
const MARK_ORDER: ReqMarkKind[] = ['missing', 'kind', 'order']
export const REQ_MARK_LABEL: Record<ReqMarkKind, string> = { missing: '还没有实现', kind: '分类不一样', order: '走向不一致' }

export interface ReqNodeMark {
  /** 按 少了、分类、走向 排好；第一个决定颜色 */
  kinds: ReqMarkKind[]
  /** 节点上方的一句（几种都有时用"·"连起来） */
  label: string
  /** 落在这个节点上的差异原文 */
  texts: string[]
}

export interface ReqViewMarks {
  /** 画布上的节点 id → 标记 */
  nodes: Map<string, ReqNodeMark>
  /** 折叠着的分支（占位 id）→ 里面有几个标了的节点 */
  counts: Map<string, number>
}

export const emptyReqMarks = (): ReqViewMarks => ({ nodes: new Map(), counts: new Map() })

const isMarkKind = (k: ReqDiffKind): k is ReqMarkKind => k === 'missing' || k === 'kind' || k === 'order'

/**
 * 设计图的视图上要标出来的节点：每处 少了 / 分类 / 走向 的差异按 design_nodes 落到设计图的节点上（走向、分类落在那
 * 条路的结局上）。节点在折叠着的分支里时数到占位上。对得上、读不懂时什么都不标。设计图只有一个函数（根卡片），
 * 节点在画布上的 id 是"<根卡片 id>/<节点 sid>"（viewmodel 的拼法）。
 */
export function reqViewMarks(view: LineView | null, r: Requirement | null): ReqViewMarks {
  const out = emptyReqMarks()
  const cmp = r?.comparison
  const root = view?.cards[0]
  if (!view || !root || !cmp || cmp.matches) return out
  const bySid = new Map<string, ReqNodeMark>()
  for (const d of cmp.differences) {
    if (!isMarkKind(d.kind)) continue
    for (const sid of d.design_nodes) {
      let m = bySid.get(sid)
      if (!m) bySid.set(sid, (m = { kinds: [], label: '', texts: [] }))
      if (!m.kinds.includes(d.kind)) m.kinds.push(d.kind)
      if (!m.texts.includes(d.text)) m.texts.push(d.text)
    }
  }
  const nodeIds = new Set(view.nodes.map((n) => n.id))
  const foldOf = new Map<string, string>()
  for (const f of view.folds) if (f.collapsed) for (const m of f.members) foldOf.set(m, f.id)
  for (const [sid, m] of bySid) {
    m.kinds.sort((a, b) => MARK_ORDER.indexOf(a) - MARK_ORDER.indexOf(b))
    m.label = m.kinds.map((k) => REQ_MARK_LABEL[k]).join(' · ')
    const id = `${root.id}/${sid}`
    if (nodeIds.has(id)) out.nodes.set(id, m)
    else {
      const fold = foldOf.get(id)
      if (fold) out.counts.set(fold, (out.counts.get(fold) ?? 0) + 1)
    }
  }
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 右栏

export interface ReqVerdict {
  tone: 'ok' | 'warn' | 'bad'
  text: string
}

/** 一句结论：对得上（绿）、有几处不一致 / 功能线找不到（琥珀）、读不懂（红） */
export function reqVerdict(r: Requirement): ReqVerdict {
  const c = r.comparison
  if (r.errors.length || !c) return { tone: 'bad', text: '需求文件读不懂，没有和实现对照' }
  if (c.matches) return { tone: 'ok', text: '设计和实现对得上' }
  const noLine = noLineOf(c)
  if (noLine) return { tone: 'warn', text: noLine.text }
  return { tone: 'warn', text: `和实现有 ${c.differences.length} 处不一致` }
}

export const REQ_DIFF_NAME: Record<ReqDiffKind, string> = {
  missing: '少了',
  extra: '多了',
  kind: '分类',
  order: '走向',
  no_line: '功能线',
  other: '其他',
}

export interface ReqDiffRow {
  kind: ReqDiffKind
  /** 小标签上的字 */
  name: string
  text: string
  /** 设计图上对应的节点 sid（点了在设计图上看）；没有时为空 */
  design: string[]
  /** extra：实现里那个节点的 sid（点了到功能线上看）；别的为空串 */
  impl: string
}

/** 差异列表（功能线找不到的那一句已经是结论，不再列） */
export function reqDiffRows(r: Requirement): ReqDiffRow[] {
  return (r.comparison?.differences ?? [])
    .filter((d) => d.kind !== 'no_line')
    .map((d) => ({
      kind: d.kind,
      name: REQ_DIFF_NAME[d.kind],
      text: d.text,
      design: d.design_nodes,
      impl: d.kind === 'extra' ? d.node : '',
    }))
}

const COVERAGE_TEXT: Record<ReqCoverage, { text: string; tone: OutcomeRow['tone'] }> = {
  covered: { text: '✓ 有测试走到', tone: 'ok' },
  uncovered: { text: '没有测试走到', tone: 'bad' },
  uncertain: { text: '不确定（运行之后代码改过）', tone: 'unsure' },
  added: { text: '那次运行里还没有（重新运行测试才知道）', tone: 'unsure' },
  missing: { text: '实现里还没有', tone: 'dim' },
  unknown: { text: '还没有跑完的测试运行', tone: 'dim' },
}

export interface OutcomeRow {
  title: string
  /** 设计的分类 ok / skipped / failed */
  kind: string
  covered: ReqCoverage
  /** 右边的一句；hint 已经说了"还没有跑完的测试运行"时，不知道的那几行为空 */
  text: string
  tone: 'ok' | 'bad' | 'unsure' | 'dim'
}

/**
 * "设计的结局有没有测试走到"：每个结局一行。全都不知道（没有跑完的测试运行）时不在每一行重复，改成上面一句 hint。
 */
export function outcomeRows(r: Requirement): { rows: OutcomeRow[]; hint: string | null } {
  const outcomes = r.comparison?.outcomes ?? []
  const allUnknown = outcomes.length > 0 && outcomes.every((o) => o.covered === 'unknown')
  const rows = outcomes.map((o): OutcomeRow => {
    const t = COVERAGE_TEXT[o.covered]
    return { title: o.title, kind: o.kind, covered: o.covered, text: allUnknown ? '' : t.text, tone: t.tone }
  })
  return { rows, hint: allUnknown ? '还没有跑完的测试运行：在「运行路径」页签里跑一次项目的测试，再点"刷新"' : null }
}

const CJK = /[⺀-鿿　-〿＀-￯]/
const LIST_ITEM = /^([-*+]|\d+[.、)）])\s*/

/**
 * 需求的说明（Markdown 正文原文）分成段落：空行分段；段落里的换行是 Markdown 的软换行——中文接着写（不加空格），
 * 英文之间补一个空格；列表项（- 、1. ）另起一行。每段里的 \n 由 CSS 的 pre-line 显示成换行。
 */
export function descriptionParagraphs(text: string): string[] {
  const out: string[] = []
  for (const block of text.replace(/\r\n?/g, '\n').split(/\n[ \t]*\n/)) {
    const lines = block
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
    if (!lines.length) continue
    let para = lines[0]
    for (const l of lines.slice(1)) {
      if (LIST_ITEM.test(l)) para += `\n${l}`
      else para += CJK.test(para.at(-1) ?? '') || CJK.test(l[0]) ? l : ` ${l}`
    }
    out.push(para)
  }
  return out
}
