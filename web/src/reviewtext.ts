// 变更记录的明细：后端给的是一段带记号的文字（core 的 diff_text：## 功能线 …、### 函数 …、新增 / 删除 / 改动 / 连线、
// 两个空格缩进的 "- 条目"，条目里用两个以上的空格隔开"说的是什么"和"在哪里"）。这里把它拆成一块块，界面按块排版，
// 不把 ##、- 这些记号原样给人看。拆不出来的行照原样当一段话：格式以后变了也不会丢内容。

export type ChangeGroup = '新增' | '删除' | '改动' | '连线'

export type ReviewBlock =
  /** 开头的说明、拆不出来的行 */
  | { kind: 'para'; text: string }
  /** ## 功能线 [改动] 标题（分组）  line:… */
  | { kind: 'line'; status: string; title: string; where: string[] }
  /** ### 函数 [改动] 标题  函数 sid  文件:行 */
  | { kind: 'fn'; status: string; title: string; where: string[] }
  /** 新增 / 删除 / 改动 / 连线 下面的一条；group 是它在哪一组下面（没有时空串） */
  | { kind: 'item'; group: ChangeGroup | ''; text: string; where: string[] }

const GROUPS = new Set<string>(['新增', '删除', '改动', '连线'])
const HEAD = /^(#{2,3}) (功能线|函数) \[([^\]]*)\] (.*)$/

/** "说的是什么  在哪里  还有什么" → [说的是什么, [在哪里, 还有什么]]（两个以上的空格隔开） */
function split(text: string): [string, string[]] {
  const [first, ...rest] = text.trim().split(/\s{2,}/)
  return [first ?? '', rest.filter((p) => p !== '')]
}

export function reviewBlocks(text: string): ReviewBlock[] {
  const out: ReviewBlock[] = []
  let group: ChangeGroup | '' = ''
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd()
    if (line.trim() === '') continue
    const head = HEAD.exec(line)
    if (head) {
      const [title, where] = split(head[4])
      out.push({ kind: head[2] === '功能线' ? 'line' : 'fn', status: head[3], title, where })
      group = ''
      continue
    }
    if (GROUPS.has(line.trim())) {
      group = line.trim() as ChangeGroup
      continue
    }
    const item = /^\s+- (.*)$/.exec(line)
    if (item) {
      const [main, where] = split(item[1])
      out.push({ kind: 'item', group, text: main, where })
      continue
    }
    out.push({ kind: 'para', text: line.replace(/^#+\s*/, '').trim() })
    if (/^#/.test(line)) group = ''
  }
  return out
}
