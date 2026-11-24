// 详情栏里所有代码框（节点的源码片段、被调用的函数、整个函数）共用的显示规则（纯函数，测试见 codeview.test.ts）：
// - 去掉公共缩进：只去掉所有非空行开头空白的公共部分，所以绝不会比最浅的那一行去得多；每一行去掉的一样多；
// - 打开或换了内容时，横向滚到最左，竖向让高亮的行出现在框里（已经整行看得见就不动）。

export interface CodeLine {
  /** 文件里的行号（从 1 起） */
  no: number
  text: string
}

const blank = (s: string) => s.trim() === ''

/** 非空行开头空白（空格、Tab）的公共前缀；没有非空行时为空串 */
export function commonIndent(lines: readonly string[]): string {
  let common: string | null = null
  for (const line of lines) {
    if (blank(line)) continue
    const lead = /^[ \t]*/.exec(line)![0]
    if (common === null) {
      common = lead
      continue
    }
    let i = 0
    while (i < common.length && i < lead.length && common[i] === lead[i]) i++
    common = common.slice(0, i)
    if (!common) break
  }
  return common ?? ''
}

/** 每一行去掉同样的公共缩进（空白行去掉开头的空白，最多去掉这么多） */
export function dedentLines(lines: readonly CodeLine[]): { lines: CodeLine[]; removed: string } {
  const removed = commonIndent(lines.map((l) => l.text))
  if (!removed) return { lines: lines.map((l) => ({ ...l })), removed }
  return {
    removed,
    lines: lines.map((l) => ({
      no: l.no,
      text: l.text.startsWith(removed) ? l.text.slice(removed.length) : l.text.replace(/^[ \t]+/, (w) => w.slice(removed.length)),
    })),
  }
}

/**
 * 竖向滚动到哪里，才能看到第 first–last 个（从 0 起）高亮的行：已经整段看得见时不动；否则把它放在框的上方三分之一处
 * （比框还高时让第一行贴着顶），不超出能滚动的范围。lineHeight 每行的高度，pad 上下内边距，viewport 框的可见高度。
 */
export function scrollTopFor(
  first: number,
  last: number,
  count: number,
  lineHeight: number,
  pad: number,
  viewport: number,
  current: number,
): number {
  if (first < 0 || count === 0) return current
  const top = pad + first * lineHeight
  const bottom = pad + (Math.min(last, count - 1) + 1) * lineHeight
  if (top >= current && bottom <= current + viewport) return current
  const maxScroll = Math.max(0, 2 * pad + count * lineHeight - viewport)
  const span = bottom - top
  const target = span >= viewport ? top : top - Math.floor((viewport - span) / 3)
  return Math.max(0, Math.min(maxScroll, target))
}

/** 高亮的行在 lines 里的下标范围；没有高亮的行时为 [-1, -1] */
export function focusIndexes(lines: readonly CodeLine[], focusStart: number | null, focusEnd: number | null): [number, number] {
  if (focusStart === null) return [-1, -1]
  const end = focusEnd ?? focusStart
  const first = lines.findIndex((l) => l.no >= focusStart && l.no <= end)
  if (first < 0) return [-1, -1]
  let last = first
  while (last + 1 < lines.length && lines[last + 1].no <= end) last++
  return [first, last]
}
