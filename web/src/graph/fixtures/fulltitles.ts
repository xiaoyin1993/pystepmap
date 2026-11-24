// 引擎截断过文字的小图（照 core/tests/fixtures/long_texts）：节点的标题和副标题、case 模式、函数标题、功能线标题被截成
// 60 / 80 字的显示形式，完整的在 *_full 里（只在被截断时有）。
//
//   开始 → 查重（注释标题）→ 判断（长条件）─是→ 归档（被调用函数的文档注释）→ 返回 …（跳过）
//                                        └否→ match ─长模式→ 归档 → 已归档
//                                                  └都不匹配→ 通知 ─→ 记审计日志 → 返回 …（主干）
//                                                               └raise→ 捕获（长异常类型）→ 抛出
//
// raw 是引擎写出的 JSON 的样子；old 是同一张图去掉所有 *_full 字段（加这几个字段之前的旧图）。两者都经 parseProject 读。
import { parseProject, type ProjectGraph } from '../types'
import { call, fn, line, project, task, uncaught } from './builders'

export const cut = (s: string, limit: number): string => `${s.slice(0, limit - 1)}…`

export const FULL = {
  fn: '处理 Gmail 推送过来的一封入站邮件：读邮件、检查附件和发件人、按内容类型分别归档，失败时通知账户所有者、记审计日志并交给重试任务',
  archive: '把这封邮件连同它的全部附件一起归档到冷存储里，并在归档索引里记下原因、时间和处理它的工作进程编号，方便以后审计时按原因查找',
  comment: '先看这封邮件是不是已经处理过：同一个 message_id 可能被 Gmail 推送好几次，重复处理会让下游的统计和通知都多出一份',
  cond: 'message.attachments and message.sender_is_trusted and message.size_in_bytes < 10_000_000 and not message.is_spam',
  pattern: '{"kind": "invoice", "currency": currency, "amount": amount, **rest} if currency in message.currencies',
  exc: '(MailboxQuotaExceededError, AttachmentTooLargeForTheConfiguredLimitError, UpstreamAnalysisServiceTemporarilyUnavailableError)',
  ret: '返回 {"message_id": message_id, "attachments": len(message.attachments), "sent": len(sent), "trusted": True}',
}

export const FK = {
  inbound: 'api.py::inbound@10',
  archive: 'api.py::archive@3',
  seen: 'api.py::seen_before@1',
  notify: 'api.py::notify@5',
  audit: 'api.py::audit@7',
}

/** 节点 id → 被截断的文字的完整形式（标题、副标题） */
const NODE_FULL: Record<string, { title?: string; subtitle?: string }> = {
  n1: { title: FULL.fn },
  n2: { title: FULL.comment },
  n3: { title: FULL.cond, subtitle: FULL.cond },
  n4: { title: FULL.archive },
  n5: { title: FULL.ret },
  n7: { title: FULL.archive },
  n10: { title: `捕获 ${FULL.exc}`, subtitle: FULL.exc },
  n13: { title: FULL.ret },
}

const inbound = fn(
  FK.inbound,
  [
    ['n1', 'start', cut(FULL.fn, 60)],
    ['n2', 'step', cut(FULL.comment, 60), { subtitle: 'seen_before', calls: [call(FK.seen)] }],
    ['n3', 'cond', cut(FULL.cond, 60), { subtitle: cut(FULL.cond, 80) }],
    ['n4', 'step', cut(FULL.archive, 60), { subtitle: 'archive', calls: [call(FK.archive)] }],
    ['n5', 'end', cut(FULL.ret, 60), { end: 'skipped' }],
    ['n6', 'cond', 'match message.kind', { subtitle: 'match message.kind' }],
    ['n7', 'step', cut(FULL.archive, 60), { subtitle: 'archive', calls: [call(FK.archive)] }],
    ['n8', 'end', '已归档', { end: 'ok' }],
    ['n9', 'step', '通知账户所有者', { subtitle: 'notify', calls: [call(FK.notify)] }],
    ['n10', 'catch', cut(`捕获 ${FULL.exc}`, 60), { subtitle: cut(FULL.exc, 80) }],
    ['n11', 'end', '抛出 UpstreamError()', { end: 'failed' }],
    ['n12', 'step', '记审计日志', { subtitle: 'audit', calls: [call(FK.audit)] }],
    ['n13', 'end', cut(FULL.ret, 60), { end: 'ok' }],
    uncaught('n14'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'yes'],
    ['n4', 'n5', 'next'],
    ['n3', 'n6', 'no'],
    ['n6', 'n7', 'case', cut(FULL.pattern, 80)],
    ['n7', 'n8', 'next'],
    ['n6', 'n9', 'case', '都不匹配'],
    ['n9', 'n10', 'raise'],
    ['n10', 'n11', 'next'],
    ['n9', 'n12', 'next'],
    ['n12', 'n13', 'next'],
  ],
  { kind: 'route', route: 'POST /inbound', title: cut(FULL.fn, 60), title_full: FULL.fn },
)
for (const n of inbound.nodes) {
  const full = NODE_FULL[n.id]
  if (full?.title) n.title_full = full.title
  if (full?.subtitle) n.subtitle_full = full.subtitle
}
for (const e of inbound.edges) if (e.kind === 'case' && e.label !== '都不匹配') e.label_full = FULL.pattern

const simple = (key: string, title: string, titleFull?: string) =>
  fn(key, [['n1', 'start', title], ['n2', 'end', '结束', { end: 'ok' }], uncaught('n3')], [['n1', 'n2', 'next']], {
    title,
    ...(titleFull ? { title_full: titleFull } : {}),
  })

const all = [
  inbound,
  simple(FK.archive, cut(FULL.archive, 60), FULL.archive),
  simple(FK.seen, 'seen_before'),
  simple(FK.notify, '通知'),
  simple(FK.audit, '审计'),
]
const inboundLine = { ...line(task(FK.inbound), all), title_full: FULL.fn }

/** 引擎写出的 JSON（已 JSON.parse）的样子 */
const raw = JSON.parse(JSON.stringify(project(all, [inboundLine]))) as Record<string, unknown>

function stripFull(data: unknown): unknown {
  if (Array.isArray(data)) return data.map(stripFull)
  if (data === null || typeof data !== 'object') return data
  return Object.fromEntries(
    Object.entries(data as Record<string, unknown>)
      .filter(([k]) => !k.endsWith('_full'))
      .map(([k, v]) => [k, stripFull(v)]),
  )
}

export const fullLineSid = inboundLine.sid
export const fullTitles: ProjectGraph = parseProject(raw)
export const oldTitles: ProjectGraph = parseProject(stripFull(raw))
