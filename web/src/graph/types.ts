// 图 JSON 的类型：照 docs/graph-schema.md 和 core/src/pystepmap/model.py（字段名保持 JSON 里的蛇形写法）。
// 读图时忽略不认识的字段；旧图缺的字段（sid、sids、branch……）由 parseProject 补成空值。
// title、subtitle、case 连线的 label 是引擎截断过的显示形式（60 / 80 字）；被截断时完整的文字在同名加 _full 的字段里，
// 只在被截断时有（旧图没有），所以完整文字是"x_full 不为空就用它，否则 x"。

export const SCHEMA_VERSION = 1

export type NodeType = 'start' | 'step' | 'cond' | 'loop' | 'catch' | 'end'
export type EdgeKind =
  | 'next'
  | 'yes'
  | 'no'
  | 'case'
  | 'loop_each'
  | 'loop_back'
  | 'loop_exit'
  | 'break'
  | 'raise'
  | 'finally'
export type EndKind = 'ok' | 'skipped' | 'failed' | 'exception'
export type FnKind = 'route' | 'task' | 'workflow' | 'step' | 'condition' | 'function'
export type Resolution = 'exact' | 'guessed' | 'unresolved' | 'external'

export interface CallRef {
  name: string
  /** 被调用函数的 key（派发时是被派发任务的 key）；没有时为 null */
  target: string | null
  resolution: Resolution
  is_dispatch: boolean
  conditional: boolean
  /** 非派发调用：target 内部任意深度会派发的任务 key */
  dispatches: string[]
}

export interface GraphNode {
  /** 函数图内的位置编号 n1、n2……（一次分析之内的身份） */
  id: string
  type: NodeType
  title: string
  /** 跨两次分析的稳定标识，函数内唯一；旧图为空串 */
  sid: string
  subtitle: string
  /** title / subtitle 被引擎截断时的完整文字；没被截断（或旧图）时没有 */
  title_full?: string
  subtitle_full?: string
  /** 语句头部所在行；"执行到函数末尾"、"未捕获异常退出"为 0 */
  line: number
  end_line: number
  calls: CallRef[]
  end_kind: EndKind | null
  fields: Record<string, string>
  passthrough: string | null
}

export interface GraphEdge {
  src: string
  dst: string
  kind: EdgeKind
  label: string
  /**
   * 判断节点的分支出边标明是哪个分支：if / elif 为 "yes" / "no"，match 为模式原文，没有兜底时越过 match 的为 "else"。
   * 只在非空时出现在 JSON 里；早于这个字段的旧图没有，从 kind / label 推断（见 viewmodel 的 edgeBranch）。
   */
  branch?: string
  /** case 连线的 label（模式原文）被引擎截断时的完整模式；branch 仍是截断后的 label。只在被截断时有 */
  label_full?: string
}

export interface UnresolvedCall {
  line: number
  name: string
}

export interface FunctionGraph {
  key: string
  file: string
  qualname: string
  line: number
  end_line: number
  kind: FnKind
  title: string
  is_async: boolean
  sid: string
  marked: boolean
  group: string | null
  route: string | null
  retries: boolean
  parent: string | null
  nodes: GraphNode[]
  edges: GraphEdge[]
  call_targets: string[]
  unresolved: UnresolvedCall[]
  rule?: string
  name?: string
  version?: string
  /** title 被引擎截断时的完整标题；只在被截断时有 */
  title_full?: string
}

export interface LineTask {
  /** 任务函数的 key */
  function: string
  /** true：本条功能线里已经出现过，不再展开 */
  goto: boolean
  children: LineTask[]
}

export interface FlowLine {
  /** 根任务函数的 key */
  id: string
  title: string
  group: string
  kind: FnKind
  root: LineTask
  /** "line:" 加根任务函数的 sid；旧图为空串 */
  sid: string
  /** 标题取自被截断的函数标题时，函数的 title_full；只在被截断时有 */
  title_full?: string
}

export interface SelfCheck {
  unreachable: [string, string][]
  dead_ends: [string, string][]
  unresolved_calls: number
  guessed_calls: number
  skipped_files: [string, string][]
}

export interface ProjectGraph {
  schema_version: number
  /** 分析的目录，绝对路径 */
  root: string
  functions: Record<string, FunctionGraph>
  lines: FlowLine[]
  common_tasks: string[]
  selfcheck: SelfCheck
  file_hashes: Record<string, string>
  /** 函数 sid → 函数 key；旧图为空表 */
  sids: Record<string, string>
}

type Json = Record<string, unknown>

const str = (v: unknown, dflt = ''): string => (typeof v === 'string' ? v : dflt)
const num = (v: unknown, dflt = 0): number => (typeof v === 'number' ? v : dflt)
const bool = (v: unknown): boolean => v === true
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])
const obj = (v: unknown): Json => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : {})
const strOrNull = (v: unknown): string | null => (typeof v === 'string' ? v : null)

/** 只在非空时出现的字符串字段（branch、*_full）：有值才写进 target */
function setOptional<T extends object>(target: T, key: keyof T & string, v: unknown): T {
  if (typeof v === 'string' && v !== '') (target as Record<string, unknown>)[key] = v
  return target
}

function parseCall(v: unknown): CallRef {
  const c = obj(v)
  return {
    name: str(c.name),
    target: strOrNull(c.target),
    resolution: str(c.resolution, 'unresolved') as Resolution,
    is_dispatch: bool(c.is_dispatch),
    conditional: bool(c.conditional),
    dispatches: arr(c.dispatches).map((d) => str(d)),
  }
}

function parseNode(v: unknown): GraphNode {
  const n = obj(v)
  const fields = obj(n.fields)
  const node: GraphNode = {
    id: str(n.id),
    type: str(n.type, 'step') as NodeType,
    title: str(n.title),
    sid: str(n.sid),
    subtitle: str(n.subtitle),
    line: num(n.line),
    end_line: num(n.end_line),
    calls: arr(n.calls).map(parseCall),
    end_kind: (strOrNull(n.end_kind) as EndKind | null) ?? null,
    fields: Object.fromEntries(Object.entries(fields).map(([k, x]) => [k, String(x)])),
    passthrough: strOrNull(n.passthrough),
  }
  setOptional(node, 'title_full', n.title_full)
  return setOptional(node, 'subtitle_full', n.subtitle_full)
}

function parseEdge(v: unknown): GraphEdge {
  const e = obj(v)
  const edge: GraphEdge = { src: str(e.src), dst: str(e.dst), kind: str(e.kind, 'next') as EdgeKind, label: str(e.label) }
  setOptional(edge, 'branch', e.branch)
  return setOptional(edge, 'label_full', e.label_full)
}

function parseFunction(v: unknown): FunctionGraph {
  const f = obj(v)
  const fn: FunctionGraph = {
    key: str(f.key),
    file: str(f.file),
    qualname: str(f.qualname),
    line: num(f.line),
    end_line: num(f.end_line),
    kind: str(f.kind, 'function') as FnKind,
    title: str(f.title),
    is_async: bool(f.is_async),
    sid: str(f.sid),
    marked: bool(f.marked),
    group: strOrNull(f.group),
    route: strOrNull(f.route),
    retries: bool(f.retries),
    parent: strOrNull(f.parent),
    nodes: arr(f.nodes).map(parseNode),
    edges: arr(f.edges).map(parseEdge),
    call_targets: arr(f.call_targets).map((k) => str(k)),
    unresolved: arr(f.unresolved).map((u) => ({ line: num(obj(u).line), name: str(obj(u).name) })),
  }
  for (const name of ['rule', 'name', 'version'] as const) {
    if (typeof f[name] === 'string') fn[name] = f[name] as string
  }
  return setOptional(fn, 'title_full', f.title_full)
}

function parseTask(v: unknown): LineTask {
  const t = obj(v)
  return { function: str(t.function), goto: bool(t.goto), children: arr(t.children).map(parseTask) }
}

const pairs = (v: unknown): [string, string][] => arr(v).map((p) => [str(arr(p)[0]), str(arr(p)[1])])

/** 把 pystepmap analyze 输出的 JSON（已 JSON.parse）规整成 ProjectGraph。顶层不是对象、版本不对时抛错。 */
export function parseProject(data: unknown): ProjectGraph {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('图文件的顶层必须是 JSON 对象')
  }
  const d = data as Json
  if (d.schema_version !== SCHEMA_VERSION) {
    throw new Error(`不支持的图 schema_version：${String(d.schema_version)}（需要 ${SCHEMA_VERSION}）`)
  }
  const sc = obj(d.selfcheck)
  const functions: Record<string, FunctionGraph> = {}
  for (const [k, f] of Object.entries(obj(d.functions))) functions[k] = parseFunction(f)
  return {
    schema_version: SCHEMA_VERSION,
    root: str(d.root),
    functions,
    lines: arr(d.lines).map((l) => {
      const ln = obj(l)
      const line: FlowLine = {
        id: str(ln.id),
        title: str(ln.title),
        group: str(ln.group),
        kind: str(ln.kind, 'function') as FnKind,
        root: parseTask(ln.root),
        sid: str(ln.sid),
      }
      return setOptional(line, 'title_full', ln.title_full)
    }),
    common_tasks: arr(d.common_tasks).map((k) => str(k)),
    selfcheck: {
      unreachable: pairs(sc.unreachable),
      dead_ends: pairs(sc.dead_ends),
      unresolved_calls: num(sc.unresolved_calls),
      guessed_calls: num(sc.guessed_calls),
      skipped_files: pairs(sc.skipped_files),
    },
    file_hashes: Object.fromEntries(Object.entries(obj(d.file_hashes)).map(([k, h]) => [k, str(h)])),
    sids: Object.fromEntries(Object.entries(obj(d.sids)).map(([k, h]) => [k, str(h)])),
  }
}
