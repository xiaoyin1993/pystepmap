// 给人看的文字：地图模式的短标题、"在编辑器中打开"的链接、"复制给 AI"的说明（标题、条件、分支模式都用完整的；
// 功能线的标题用应用了手工标题之后的，见 overrides.ts）。
import type { CardView, FoldView, NodeView, SourceLoc } from './graph'
import { END_NAME, NODE_TYPE_NAME, RESOLUTION_NAME } from './theme'

/** 地图上用短标题：取到第一个句读为止（文档注释第一行常常很长），最多 22 个字 */
export function shortTitle(t: string): string {
  const cut = t.split(/[：:，,。；;]/)[0].trim() || t
  return cut.length > 22 ? `${cut.slice(0, 21)}…` : cut
}

/** vscode://file/<绝对路径>:<行> */
export function editorUrl(loc: SourceLoc): string {
  const p = loc.path.split('/').map(encodeURIComponent).join('/')
  return `vscode://file${p.startsWith('/') ? p : `/${p}`}:${loc.line}`
}

export function locText(loc: SourceLoc): string {
  return loc.endLine > loc.line ? `${loc.file}:${loc.line}–${loc.endLine}` : `${loc.file}:${loc.line}`
}

const lines = (rows: (string | false | null | undefined)[]) => rows.filter(Boolean).join('\n')

function where(loc: SourceLoc | null): string {
  if (!loc) return '位置：没有对应的源码行（隐含节点）'
  const range = loc.endLine > loc.line ? `第 ${loc.line}–${loc.endLine} 行` : `第 ${loc.line} 行`
  return `文件：${loc.path} ${range}`
}

/** "复制给 AI"的上下文：项目名、所在功能线的标题（手工改过的用改过的） */
export interface AiContext {
  project: string
  line: string
}

const scope = (ctx: AiContext) => `项目 ${ctx.project}${ctx.line ? `，功能线「${ctx.line}」` : ''}`

/** source：源码片段（src/source.ts 的 snippetForAi），取不到时为空串 */
export function nodeAiText(n: NodeView, card: CardView | undefined, ctx: AiContext, source = ''): string {
  const calls = n.calls.map(
    (c) =>
      `- ${c.name}${c.targetTitle ? ` → ${c.targetTitle}` : ''}（${RESOLUTION_NAME[c.resolution]}${c.isDispatch ? '，派发任务' : ''}${c.conditional ? '，不一定执行' : ''}）`,
  )
  const fields = Object.entries(n.fields).map(([k, v]) => `- ${k} = ${v}`)
  return lines([
    `pystepmap 流程图里的一个节点（${scope(ctx)}）：`,
    where(n.loc),
    card && `所在函数：${card.name}（${card.titleFull}）`,
    `节点：${NODE_TYPE_NAME[n.type]}「${n.titleFull}」${n.endKind ? `，结局种类：${END_NAME[n.endKind]}` : ''}`,
    // 步骤的副标题就是调用的显示名，下面的调用清单已经有了
    n.subtitle && n.type !== 'step' && `源码：${n.subtitleFull}`,
    calls.length > 0 && `调用：\n${calls.join('\n')}`,
    n.dispatches.length > 0 && `派发的任务：${n.dispatches.map((d) => d.title).join('、')}`,
    fields.length > 0 && `结局返回的字段：\n${fields.join('\n')}`,
    source,
  ])
}

export function foldAiText(f: FoldView, card: CardView | undefined, ctx: AiContext): string {
  const s = f.summary
  return lines([
    `pystepmap 流程图里一段折叠起来的分支（${scope(ctx)}）：`,
    card?.loc && `文件：${card.loc.path}`,
    card && `所在函数：${card.name}（${card.titleFull}）`,
    `${f.kind === 'catch' ? `异常处理（${f.labelFull}）` : `分支「${f.labelFull}」`}：${s.nodes} 个节点，入口「${s.first}」`,
    s.ends.length > 0 && `结局：${s.ends.map((e) => `${e.title}${e.kind ? `（${END_NAME[e.kind]}）` : ''}`).join('、')}`,
    s.dispatches.length > 0 && `派发的任务：${s.dispatches.join('、')}`,
  ])
}

export function cardAiText(c: CardView, ctx: AiContext, source = ''): string {
  return lines([
    `pystepmap 流程图里的一个${c.kind === 'drill' ? '被调用函数' : '任务'}（${scope(ctx)}）：`,
    where(c.loc),
    `函数：${c.name}（${c.titleFull}）`,
    c.route && `接口：${c.route}`,
    `${c.summary.nodes} 个节点`,
    c.anchor && `调用它的步骤：${c.anchor.titleFull}（${c.anchor.subtitleFull}）`,
    c.anchor &&
      c.anchor.drills.length > 1 &&
      `这一步还调用了：${c.anchor.drills
        .filter((d) => d.id !== c.id)
        .map((d) => d.name)
        .join('、')}（同一条语句里的调用，按源码里出现的先后，不一定是执行的先后）`,
    source,
  ])
}
