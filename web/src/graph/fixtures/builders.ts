// 测试用的小图：手写函数图、功能线的简便写法。
import type {
  CallRef,
  EdgeKind,
  EndKind,
  FlowLine,
  FnKind,
  FunctionGraph,
  GraphEdge,
  GraphNode,
  LineTask,
  NodeType,
  ProjectGraph,
} from '../types'

export interface NodeOpts {
  sid?: string
  subtitle?: string
  line?: number
  calls?: CallRef[]
  end?: EndKind
}

export type NodeSpec = [id: string, type: NodeType, title?: string, opts?: NodeOpts]
/** [src, dst, kind, label?, branch?]；branch 写 null 表示旧图（不带 branch 字段） */
export type EdgeSpec = [src: string, dst: string, kind: EdgeKind, label?: string, branch?: string | null]

export function call(target: string | null, opts: Partial<CallRef> = {}): CallRef {
  return {
    name: opts.name ?? (target ? target.split('::')[1].split('@')[0] : 'unknown'),
    target,
    resolution: opts.resolution ?? (target ? 'exact' : 'unresolved'),
    is_dispatch: opts.is_dispatch ?? false,
    conditional: opts.conditional ?? false,
    dispatches: opts.dispatches ?? [],
  }
}

export function dispatch(task: string, opts: Partial<CallRef> = {}): CallRef {
  return call(task, { name: `派发 ${task.split('::')[1].split('@')[0]}`, ...opts, is_dispatch: true })
}

/** 函数图。key 形如 "a.py::f@3"；节点 sid 默认 "<type>:<id>"，开始 / 未捕获异常退出用固定名字。 */
export function fn(
  key: string,
  nodes: NodeSpec[],
  edges: EdgeSpec[],
  extra: Partial<FunctionGraph> = {},
): FunctionGraph {
  const [file, rest] = key.split('::')
  const [qualname, line] = rest.split('@')
  const graphNodes: GraphNode[] = nodes.map(([id, type, title, o = {}]) => ({
    id,
    type,
    title: title ?? id,
    sid: o.sid ?? (type === 'start' ? 'start' : `${type}:${id}`),
    subtitle: o.subtitle ?? '',
    line: o.line ?? (type === 'start' ? Number(line) : Number(id.slice(1)) + Number(line)),
    end_line: o.line ?? Number(id.slice(1)) + Number(line),
    calls: o.calls ?? [],
    end_kind: o.end ?? null,
    fields: {},
    passthrough: null,
  }))
  const graphEdges: GraphEdge[] = edges.map(([src, dst, kind, label = '', branch]) => {
    const e: GraphEdge = { src, dst, kind, label }
    if (branch === undefined) {
      // 新图：照引擎的规则补 branch
      if (kind === 'yes' || kind === 'no') e.branch = kind
      else if (label === '是') e.branch = 'yes'
      else if (label === '否') e.branch = 'no'
      else if (label === '都不匹配' && kind !== 'raise') e.branch = 'else'
      else if (label && kind !== 'raise') e.branch = label
    } else if (branch !== null) e.branch = branch
    return e
  })
  return {
    key,
    file,
    qualname,
    line: Number(line),
    end_line: Number(line) + 50,
    kind: 'function' as FnKind,
    title: qualname,
    is_async: false,
    sid: `${file}::${qualname}`,
    marked: false,
    group: null,
    route: null,
    retries: false,
    parent: null,
    nodes: graphNodes,
    edges: graphEdges,
    call_targets: [],
    unresolved: [],
    ...extra,
  }
}

/** 隐含的"未捕获异常退出"节点。 */
export const uncaught = (id: string, title = '未捕获异常退出'): NodeSpec => [
  id,
  'end',
  title,
  { sid: 'end:uncaught', line: 0, end: 'exception' },
]

export const task = (fnKey: string, children: LineTask[] = [], goto = false): LineTask => ({
  function: fnKey,
  goto,
  children,
})

export function project(fns: FunctionGraph[], lines: FlowLine[] = [], common: string[] = []): ProjectGraph {
  return {
    schema_version: 1,
    root: '/repo',
    functions: Object.fromEntries(fns.map((f) => [f.key, f])),
    lines,
    common_tasks: common,
    selfcheck: { unreachable: [], dead_ends: [], unresolved_calls: 0, guessed_calls: 0, skipped_files: [] },
    file_hashes: {},
    sids: Object.fromEntries(fns.map((f) => [f.sid, f.key])),
  }
}

export function line(rootTask: LineTask, functions: Record<string, FunctionGraph> | FunctionGraph[]): FlowLine {
  const map = Array.isArray(functions) ? Object.fromEntries(functions.map((f) => [f.key, f])) : functions
  const root = map[rootTask.function]
  return {
    id: rootTask.function,
    title: root?.title ?? rootTask.function,
    group: 'test',
    kind: root?.kind ?? 'function',
    root: rootTask,
    sid: `line:${root?.sid ?? rootTask.function}`,
  }
}
