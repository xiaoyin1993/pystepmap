// 平台后端的接口客户端（后端见 server/src/pystepmap_server/api.py，全部在 /api 下）。每个接口一个函数；出错一律抛
// ApiError（中文信息）。开发时 npm run dev 把 /api 转给后端（vite.config.ts），打包后的页面由后端自己提供，同源。
//
// 出错分几种（ApiError.kind）：
//   unreachable  连不上后端：fetch 本身失败（后端停了）、响应读到一半连接断了，或开发服务器转发失败（502 / 503 / 504
//                且不是后端的 JSON）
//   http         后端回了错误：{"error": "中文说明", "code"?, "project_id"?}
//   format       回来的不是想要的东西（不是 JSON、不是 pystepmap 的图、图的格式版本比页面新……）：重试
//   old-graph    图是旧版引擎生成的（schema_version 旧、没有 schema_version 的早期图、功能线没有 sid）：要重新分析
// 读图时按已收到的字节报告进度；总量取响应头 X-Pystepmap-Graph-Size（压缩前的字节数——浏览器的 fetch 会自动解压，
// 读到的就是压缩前的字节；Content-Length 是压缩后的，不能用）。

import { SCHEMA_VERSION, parseProject, type ProjectGraph } from './graph'

export type ApiErrorKind = 'unreachable' | 'http' | 'format' | 'old-graph'

export class ApiError extends Error {
  readonly kind: ApiErrorKind
  /** HTTP 状态码；连不上时为 0 */
  readonly status: number
  /** 后端流程失败时的 code（invalid、duplicate、busy……），没有时为 null */
  readonly code: string | null
  /** "目录已经添加过"时，已有的那个项目的 id */
  readonly projectId: string | null
  /** 后端错误响应的 JSON 体（其余字段，比如 409 confirm 的 command、root）；没有时为 {} */
  readonly data: Record<string, unknown>

  constructor(
    kind: ApiErrorKind,
    message: string,
    opts: { status?: number; code?: string | null; projectId?: string | null; data?: Record<string, unknown> } = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = opts.status ?? 0
    this.code = opts.code ?? null
    this.projectId = opts.projectId ?? null
    this.data = opts.data ?? {}
  }
}

export const isApiError = (e: unknown): e is ApiError => e instanceof ApiError

/**
 * 后端没有这个接口（旧版的 pystepmap serve：框架的 404"没有这个接口"，或者不是 pystepmap 格式的 404）。项目不在了、运行
 * 不在了这些 pystepmap 自己的 404 不算。
 */
export function isMissingEndpoint(e: unknown): boolean {
  if (!isApiError(e) || e.kind !== 'http' || e.status !== 404) return false
  const text = e.data.error
  return typeof text !== 'string' || text.startsWith('没有这个接口')
}

/** 给人看的错误信息（ApiError 的中文说明，别的错误的 message） */
export const errorText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

export const UNREACHABLE = '连不上平台后端'

// ---------------------------------------------------------------------------------------------------------------
// 返回的数据

export interface SkippedFile {
  file: string
  reason: string
}

/** 一次分析的摘要（后端 worker.summarize） */
export interface AnalysisSummary {
  functions: number
  lines: number
  common_tasks: number
  files: number
  unresolved_calls: number
  /** 其中名字在项目里有定义、可能真落在项目代码上的（旧的分析没有这一项） */
  unresolved_blind?: number
  guessed_calls: number
  unreachable: number
  dead_ends: number
  skipped_files: SkippedFile[]
  source_roots: string[]
  rules: string[]
  excluded: Record<string, number>
  has_entries: boolean
  /** 功能线一条都没有、或者全靠推断（没有人调用的顶层函数）时的说明；旧的分析没有这一项 */
  entry_notes?: string[]
  /** 标记里读不出来的参数（group=算出来的值 等），每处一句；旧的分析没有这一项 */
  mark_notes?: string[]
  seconds: number
}

export interface AnalysisInfo {
  id: number
  /** ISO 时间（UTC） */
  created_at: string
  engine_version: string
  /** 图 JSON 压缩前的字节数 */
  graph_size: number
  summary: AnalysisSummary
  /** 对比基准的分析 id：null = 没有可比的（第一次分析、已标为看过；旧版后端也没有这一项） */
  base_id: number | null
  /** 存着的流程差异不为空（旧版后端没有这一项，当作 false） */
  has_diff: boolean
}

/**
 * 代码和分析时比有没有变（GET /status 的 stale）：checked 为假时是没法检查（项目目录没了、读不了），带中文原因。
 * changed / added / removed 各按路径排序、最多 200 个，total 是真正的个数；lines 是涉及改了 / 删了的文件的功能线
 * sid（旧版本存的分析没有这项信息时为 null）。unreadable 是读不了、没法比的文件（不算过期），note 是后端给它们的
 * 说明（没有时为空串）。
 */
export type StaleCheck =
  | {
      checked: true
      stale: boolean
      changed: string[]
      added: string[]
      removed: string[]
      unreadable: string[]
      lines: string[] | null
      total: { changed: number; added: number; removed: number; unreadable: number }
      note: string
    }
  | { checked: false; reason: string }

/** GET /status：最新一次分析的 id（没有时 null）、有没有进程在分析它、代码过期了没有、对比基准和有没有流程差异 */
export interface ProjectStatus {
  analysis_id: number | null
  analyzing: boolean
  stale: StaleCheck
  base_id: number | null
  has_diff: boolean
}

// 流程差异（core/src/pystepmap/diff.py 的 diff_to_json，字段名原样）

/** 一个节点的变化；old_* / new_* 哪边没有就是空串 / 0。标题、副标题是完整形式。 */
export interface NodeChangeJson {
  status: 'added' | 'removed' | 'changed'
  type: string
  new_id: string
  new_sid: string
  new_title: string
  new_subtitle: string
  new_line: number
  old_sid: string
  old_title: string
  old_subtitle: string
  old_line: number
  /**
   * 只有 removed 有：它原来在控制流上前面那个仍然在的节点在新图里的 id（没有时为空）。旧版存下的差异是源码顺序上前面
   * 那个，可能是前一个分支里的结局节点——界面那时改按所在函数定位。
   */
  after: string
  /** 只有 removed 有：after 那个节点的标题（旧版存下的差异没有，为空串） */
  after_title: string
  /** 只有 removed 有：它挂在 after 的哪条出边上（"是""否"、case 模式、"每次""循环结束"），没有时为空串 */
  after_label: string
}

/**
 * 一条连线的变化。src / dst 是两端节点在新图里的 id：removed 的两端都是留下的节点；added 的一般也是，新增的判断 /
 * 循环 / 捕获节点的出边、新增节点的异常连线起点是新增节点，留下的判断进新增节点的分支终点是新增节点。
 */
export interface EdgeChangeJson {
  status: 'added' | 'removed'
  kind: string
  label: string
  src: string
  dst: string
  src_type: string
  src_title: string
  src_line: number
  dst_type: string
  dst_title: string
  dst_line: number
}

export interface FunctionChangeJson {
  status: 'added' | 'removed' | 'changed'
  sid: string
  title: string
  file: string
  new_key: string
  old_key: string
  /** 按源码顺序（删除的在它原来的位置）；删除的函数不列节点，新增的函数全列为 added */
  nodes: NodeChangeJson[]
  edges: EdgeChangeJson[]
  /** 只有 changed 的函数会是 true：它所在的文件两次分析时内容一样——流程变了不是因为它的代码改了（旧数据没有这一项） */
  file_same?: boolean
}

export interface LineChangeJson {
  status: 'added' | 'removed' | 'changed'
  sid: string
  title: string
  group: string
  new_id: string
  /** 这条线涉及的、有变化的函数 sid */
  functions: string[]
  /** 派发树上多出的 / 少掉的任务函数 sid（只有 changed 的线有） */
  tasks_added: string[]
  tasks_removed: string[]
  /** 根函数的 sid */
  root: string
}

export interface GraphDiffJson {
  version: number
  /** 函数 sid → 变化，只列有变化的 */
  functions: Record<string, FunctionChangeJson>
  lines: LineChangeJson[]
}

/**
 * 对比基准代表什么：acknowledged——上次标为已看时（at 是基准那次分析的时间）；first——没标为已看过，相对第一次分析
 * （at 是第一次分析的时间）。对比基准只在标为已看时移动，所以它可能比上一次分析早。
 */
export interface Baseline {
  kind: 'acknowledged' | 'first'
  at: string
}

/**
 * GET /diff、POST /diff/ack：head 是最新一次分析，base 是对比基准（没有时 null），diff 没有基准或差异为空时 null，
 * baseline 说对比基准是哪种（旧版后端没有，为 null）
 */
export interface DiffResult {
  head: AnalysisInfo
  base: AnalysisInfo | null
  diff: GraphDiffJson | null
  baseline: Baseline | null
}

// 测试运行（阶段 6；后端 api.py 的"测试命令和运行"，运行路径的 JSON 由 core/src/pystepmap/runpath.py 定）

/** GET / PUT /test-command：项目的测试命令（没填时 null）、这条命令确认过没有 */
export interface TestCommand {
  command: string | null
  confirmed: boolean
}

/** GET /interpreters 的一项：在项目目录下的哪个子目录（空 = 项目目录）、解释器相对它的路径 */
export interface Interpreter {
  cwd: string
  python: string
}

/** 核对结果（运行的 summary.verify） */
export interface RunVerify {
  /** 核对没有不符；null = 一条记录都没收到、什么都没核对（界面不说一致也不报警，看 summary.note） */
  ok: boolean | null
  /** 各类不符的次数 */
  mismatches: { edge: number; end: number; call: number }
  functions_seen: number
  distinct_paths: number
  steps_total: number
  plugin_errors: number
  plugin_error_types: Record<string, number>
  truncated_records: number
  /** 运行期间变了的文件（最多 20 个） */
  stale: string[]
}

/** 一处不符：kind edge / end / call，function 是函数 sid，detail 是引擎的结构性说明 */
export interface RunMismatch {
  kind: string
  function: string
  from_line: number
  to_line: number
  detail: string
  count: number
}

export interface RunSummary {
  /** 用例数（参数化的每个用例一个） */
  tests: number
  passed: number
  failed: number
  skipped: number
  errors: number
  /** 测试命令跑了几秒 */
  seconds: number
  /** 没跑完、记录读不了时 null */
  verify: RunVerify | null
  /** core 的 covered_summary；没有时 null */
  coverage: Record<string, number> | null
  mismatch_list: RunMismatch[]
  /** 中文说明（退出码是什么意思、运行期间代码改过、服务关闭时取消了……），没有时为空串；有就显示 */
  note: string
}

/**
 * 只跑了一条功能线的测试的运行（运行的 scope）：哪条线（sid）、当时的标题、跑了几个测试、名单取自哪次全量运行。
 */
export interface RunScope {
  line: string
  line_title: string
  tests: number
  base_run: number | null
}

/**
 * 一次运行。status：ok / tests_failed / error / timeout / cancelled（不认识的照原样显示）；exit_code 没跑完时 null；
 * paths_size 是运行路径 JSON 的字节数，没有路径（没跑完、没收到记录）时 null。scope：只跑了一条线的测试时是那条线，
 * 全量运行为 null（旧版后端没有这一项，也当作全量）。
 */
export interface RunInfo {
  id: number
  analysis_id: number
  started_at: string
  finished_at: string
  command: string
  status: string
  exit_code: number | null
  paths_size: number | null
  summary: RunSummary
  scope: RunScope | null
}

/**
 * GET /line-tests?line=：只跑这条线的话会跑哪些测试（名单取自最近一次全量运行 base_run）。ok 为假时 code 是
 * no_full_run（还没有全量运行）/ no_tests（没有测试走过这条线）/ no_index（那次运行没有记下每个测试走过的函数），
 * reason 是后端的中文说明。
 */
export interface LineTestsResult {
  ok: boolean
  code: string
  reason: string
  tests: string[]
  base_run: number | null
}

/** 正在跑：phase analyzing（先分析）/ testing（跑测试）/ processing（整理结果），不认识的照原样显示 */
export interface RunningInfo {
  started_at: string
  seconds: number
  phase: string
  /** 测试命令跑着时（phase testing）的进度；旧版后端、别的阶段没有时 null */
  progress: LiveProgress | null
  /**
   * 正在跑的是什么：null 全部测试；只跑一条功能线的测试时是那条线和名单里的个数；旧版后端不说（undefined）。
   * 别处（MCP、另一个标签页、刷新页面之前）开始的运行靠它知道跑的是哪种
   */
  scope?: { line: string; line_title: string; tests: number } | null
}

/** 运行中跑完的一个测试（参数化的每个用例一条） */
export interface LiveTest {
  test: string
  outcome: string
  seconds: number
}

/**
 * 测试命令跑着时的进度（GET …/runs 的 running.progress，后端 live.py 从记录插件正在写的文件里读）。计数按用例；
 * running 最多 3 个（先开始的在前），running_more 是另外还有几个；recent 是最近跑完的（新的在前）；lines 是这次运行用的
 * 分析里每条有函数执行过的功能线；nodes 只在请求带了 live_line、后端认得这条线时有：函数 key → 走到过的节点 sid。
 * 执行记录在每个测试结束时才写出，说的都是已经跑完的测试。
 */
export interface LiveProgress {
  /** 收集到的用例数；还不知道时 null */
  collected: number | null
  done: number
  passed: number
  failed: number
  skipped: number
  errors: number
  running: string[]
  running_more: number
  recent: LiveTest[]
  lines: Record<string, { functions: number; total: number }>
  nodes: Record<string, string[]> | null
}

/** 202 之后、存下运行之前就失败了（只在后端进程内存里） */
export interface RunFailure {
  title: string
  reason: string
  code: string
  at: string
}

export interface RunsResult {
  running: RunningInfo | null
  /** 新的在前，最多 5 次 */
  runs: RunInfo[]
  last_failure: RunFailure | null
}

/**
 * 运行里的一个测试（GET /runs/{id} 的 tests）。test 为 "" 是测试之外的执行（收集测试时导入模块等），outcome 为 ""
 * 是没有结果行。on_line：请求时带了 ?line=，这个测试执行了这条线上几个函数（按最新一次分析的图）；没带、或者这次运行
 * 没有索引时为 null。
 */
export interface TestRow {
  test: string
  outcome: string
  seconds: number
  cases: number
  /** 这个测试执行到的图里的函数个数 */
  functions: number
  on_line: number | null
}

export interface RunDetail extends RunInfo {
  tests: TestRow[]
  /** 后端算了每个测试走没走过 ?line= 那条线（旧的运行没有索引时为假：界面列全部测试） */
  line_known: boolean
}

/** 运行路径 JSON（core 的 run_paths_to_json）：一个函数的汇总，节点、连线、停点都按 sid */
export interface FunctionRunJson {
  function: string
  executions: number
  /** 节点 sid → 次数、耗时（纳秒，含它调用的函数） */
  nodes: Record<string, { visits: number; ns: number }>
  /** "起点 sid|终点 sid" → 次数 */
  edges: Record<string, number>
  /** "停点 sid|return 或 raise|异常类型名或空" → 次数；截断的记录停点为空 */
  exits: Record<string, number>
  first_path: string[]
  off_graph: number
  has_time: boolean
  /**
   * 记录插件看到的真实调用（S12）：被调函数 sid（运行时的）→ 这个函数有几条执行记录调用过它（含动态调用、回调）；
   * 早于 S12 的数据没有（读成 {}）
   */
  calls?: Record<string, number>
  /**
   * 经项目以外的帧（库的包装函数、contextmanager、回调）的间接调用（S14），同 calls 汇总；早于 S14 的数据没有（读成 {}）
   */
  icalls?: Record<string, number>
}

export interface TestRunJson {
  test: string
  outcome: string
  seconds: number
  cases: number
  /** 运行时的函数 sid → 汇总 */
  functions: Record<string, FunctionRunJson>
  /** 让测试失败的异常类型名（第一个失败的用例）；通过、不知道、早于 S12 的数据为 "" */
  exc?: string
  /** 失败的用例的所有不同的异常类型名（参数化的几个用例），排序 */
  excs?: string[]
}

/** 运行时一个函数的样子：节点 sid 按图里顺序的 sha1、有不止一个节点的基础节点 sid */
export interface FunctionShapeJson {
  shape: string
  dups: string[]
}

export interface RunPathsJson {
  version: number
  tests: TestRunJson[]
  /** 运行时的函数 sid → 样子（旧数据为 {}） */
  shapes: Record<string, FunctionShapeJson>
  /** 运行时有不止一个定义的基础函数 sid（旧数据为 []） */
  function_dups: string[]
}

/** 没有测试走到的结局（GET /runs/{id}/uncovered），按最新一次分析的图 */
export interface UncoveredJson {
  /** 函数 sid */
  function: string
  /** 结局节点的 sid */
  node: string
  title: string
  end_kind: string
  file: string
  /** 执行到函数末尾为 0 */
  line: number
  /** 运行之后代码改过、对不上：既不算走到也不算没走到 */
  uncertain: boolean
}

export interface UncoveredResult {
  outcomes: UncoveredJson[]
  /** 最新一次分析不是这次运行用的那次 */
  stale: boolean
}

export interface ProjectInfo {
  id: string
  name: string
  /** 解析过的绝对路径 */
  root: string
  created_at: string
  updated_at: string
  /** 此刻有一次分析在进行（可能是别的页面点的） */
  analyzing: boolean
  /** 最新一次分析；正常情况下总有 */
  analysis: AnalysisInfo | null
  /** 允许 AI 经 MCP 运行单条功能线的测试；旧版后端没有这一项时 null（界面不显示这个开关） */
  mcp_tests: boolean | null
}

/** 一条功能线的手工标题和分组；没改的项不出现 */
export interface LineOverride {
  title?: string
  group?: string
}

/** 功能线 sid → 手工项 */
export type OverrideMap = Record<string, LineOverride>

export interface SourceSnippet {
  file: string
  /** 实际返回的行号范围（从 1 起，含两端）；start 超过文件末尾时 lines 为空、end = start - 1 */
  start: number
  end: number
  /** 文件一共几行 */
  total: number
  lines: string[]
  /** 文件和生成图时不一样了；图里没有这个文件的指纹时为 null */
  stale: boolean | null
}

export interface Health {
  ok: boolean
  version: string
  engine_version: string
}

export interface GraphResult {
  graph: ProjectGraph
  /** 响应头 X-Pystepmap-Analysis：这是哪一次分析的图 */
  analysisId: number | null
  /** 收到的字节数（压缩前） */
  bytes: number
}

/** 读图的进度：received 已收到的字节，total 总字节数（不知道时为 null） */
export type ProgressFn = (received: number, total: number | null) => void

// 需求（GET /requirements，server/src/pystepmap_server/requirements_view.py 的形状，字段名原样）

/** 对照出来的一处差异：少了 / 多了 / 结局分类不一样 / 走向不一致 / 功能线找不到；不认识的（新版后端）为 other */
export type ReqDiffKind = 'missing' | 'extra' | 'kind' | 'order' | 'no_line' | 'other'

export interface ReqDifference {
  kind: ReqDiffKind
  /** 给人看的一句 */
  text: string
  /** 节点类型 step / cond / loop / end（no_line 为空串） */
  type: string
  title: string
  /** extra、kind：实现里那个节点的 sid；别的为空串 */
  node: string
  /** 设计图里对应的节点 sid（extra 为空） */
  design_nodes: string[]
}

/** 设计的一个结局有没有测试走到：有 / 没有 / 不确定（运行之后代码改过）/ 那次运行里还没有（运行之后才写的）/ 实现里还没有 / 不知道（没有跑完的测试运行） */
export type ReqCoverage = 'covered' | 'uncovered' | 'uncertain' | 'added' | 'missing' | 'unknown'

export interface ReqOutcome {
  title: string
  /** 设计的分类 ok / skipped / failed */
  kind: string
  /** 实现里那个结局的 sid；实现里没有时为空串 */
  node: string
  covered: ReqCoverage
}

export interface ReqComparison {
  /** 对上的功能线 sid；没找到时为空串 */
  line_sid: string
  line_title: string
  matches: boolean
  differences: ReqDifference[]
  notes: string[]
  outcomes: ReqOutcome[]
}

export interface Requirement {
  id: string
  title: string
  status: string
  /** 需求里写的功能线 */
  line: string
  description: string
  /** 设计的流程（缩进文本原文） */
  flow: string
  /** 需求文件，相对项目根 */
  file: string
  /** 需求文件的原文（读不了的文件、旧版后端为空） */
  text: string
  /** 文件、流程哪里读不懂；不为空时没有对照、没有设计图 */
  errors: string[]
  /** OpenSpec 已归档的变更里的（当时的设计）；旧版后端没有这一项 */
  archived?: boolean
  /** 它在设计图里的功能线 id（= 函数 key）；读不懂时为 null */
  design_line: string | null
  comparison: ReqComparison | null
  /** 按现在的对照结果够得上的状态 */
  reachable_status: string
}

/** 一条变更记录的信息：标为已看时存下的那次流程变化 */
export interface ReviewInfo {
  id: number
  acked_at: string
  head_id: number
  head_at: string
  base_id: number | null
  base_at: string | null
  /** 有变化的功能线、有变化的函数个数、新增 / 删除 / 改动的节点个数 */
  summary: { lines: { sid: string; title: string; group: string; status: string }[]; functions: number; added: number; removed: number; changed: number }
  /**
   * 存没存当时的图（GET …/reviews/{id}/graph 能不能取）：true 存着（graph_size 是数）；false 没存（graph_size 为 null，
   * 旧版本存的记录，只有文字）；null 后端没说（旧版后端没有 graph_size 这一项，也没有取图的接口）
   */
  has_graph: boolean | null
}

/** 一条变更记录的明细：diff 是存下的流程差异（和 GET /diff 的 diff 同一个形状，读不出来时 null），text 是写成的文字 */
export interface ReviewDetail extends ReviewInfo {
  diff: GraphDiffJson | null
  text: string
}

export function parseReview(v: unknown): ReviewInfo {
  const o = obj(v)
  const s = obj(o.summary)
  return {
    id: count(o.id),
    acked_at: str(o.acked_at),
    head_id: count(o.head_id),
    head_at: str(o.head_at),
    base_id: numOrNull(o.base_id),
    base_at: typeof o.base_at === 'string' ? o.base_at : null,
    has_graph: !('graph_size' in o) ? null : typeof o.graph_size === 'number' && o.graph_size > 0,
    summary: {
      lines: arr(s.lines).map((x) => ({ sid: str(obj(x).sid), title: str(obj(x).title), group: str(obj(x).group), status: str(obj(x).status) })),
      functions: count(s.functions),
      added: count(s.added),
      removed: count(s.removed),
      changed: count(s.changed),
    },
  }
}

export interface RequirementsResult {
  requirements: Requirement[]
  /** 设计流程的图（和分析结果同一个格式；读不懂的需求不在里面）；读不了时为 null */
  graph: ProjectGraph | null
  /** 算测试覆盖用的那次运行；没有时为 null */
  run: number | null
}

export interface NewProject {
  root: string
  name?: string | null
  config?: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------------------------------------------
// 规整：后端回来的东西缺字段时补成空值，界面不因为少一个字段白屏

type Json = Record<string, unknown>
const isObject = (v: unknown): v is Json => v !== null && typeof v === 'object' && !Array.isArray(v)
const obj = (v: unknown): Json => (isObject(v) ? v : {})
const str = (v: unknown, d = ''): string => (typeof v === 'string' ? v : d)
const num = (v: unknown, d = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : d)
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function parseSummary(v: unknown): AnalysisSummary {
  const s = obj(v)
  return {
    functions: num(s.functions),
    lines: num(s.lines),
    common_tasks: num(s.common_tasks),
    files: num(s.files),
    unresolved_calls: num(s.unresolved_calls),
    ...(s.unresolved_blind === undefined ? {} : { unresolved_blind: num(s.unresolved_blind) }),
    guessed_calls: num(s.guessed_calls),
    unreachable: num(s.unreachable),
    dead_ends: num(s.dead_ends),
    skipped_files: arr(s.skipped_files).map((f) => ({ file: str(obj(f).file), reason: str(obj(f).reason) })),
    source_roots: arr(s.source_roots).map((r) => str(r)),
    rules: arr(s.rules).map((r) => str(r)),
    excluded: Object.fromEntries(Object.entries(obj(s.excluded)).map(([k, n]) => [k, num(n)])),
    has_entries: s.has_entries !== false,
    ...(s.entry_notes === undefined ? {} : { entry_notes: arr(s.entry_notes).map((n) => str(n)) }),
    ...(s.mark_notes === undefined ? {} : { mark_notes: arr(s.mark_notes).map((n) => str(n)) }),
    seconds: num(s.seconds),
  }
}

const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const strs = (v: unknown): string[] => arr(v).filter((x): x is string => typeof x === 'string')

function parseAnalysis(v: unknown): AnalysisInfo {
  const a = obj(v)
  return {
    id: num(a.id),
    created_at: str(a.created_at),
    engine_version: str(a.engine_version),
    graph_size: num(a.graph_size),
    summary: parseSummary(a.summary),
    base_id: numOrNull(a.base_id),
    has_diff: a.has_diff === true,
  }
}

export function parseProjectInfo(v: unknown): ProjectInfo {
  const p = obj(v)
  if (typeof p.id !== 'string' || !p.id) throw new ApiError('format', '后端返回的项目信息不对：缺少 id')
  return {
    id: p.id,
    name: str(p.name, p.id),
    root: str(p.root),
    created_at: str(p.created_at),
    updated_at: str(p.updated_at),
    analyzing: p.analyzing === true,
    analysis: p.analysis === null || p.analysis === undefined ? null : parseAnalysis(p.analysis),
    mcp_tests: typeof p.mcp_tests === 'boolean' ? p.mcp_tests : null,
  }
}

function parseStatus(v: unknown): ProjectStatus {
  const s = obj(v)
  const st = obj(s.stale)
  let stale: StaleCheck
  if (st.checked === false) {
    stale = { checked: false, reason: str(st.reason, '原因不明') }
  } else {
    const changed = strs(st.changed)
    const added = strs(st.added)
    const removed = strs(st.removed)
    const unreadable = strs(st.unreadable)
    const t = obj(st.total)
    stale = {
      checked: true,
      stale: st.stale === true,
      changed,
      added,
      removed,
      unreadable,
      lines: Array.isArray(st.lines) ? strs(st.lines) : null,
      total: {
        changed: num(t.changed, changed.length),
        added: num(t.added, added.length),
        removed: num(t.removed, removed.length),
        unreadable: num(t.unreadable, unreadable.length),
      },
      note: str(st.note),
    }
  }
  return {
    analysis_id: numOrNull(s.analysis_id),
    analyzing: s.analyzing === true,
    stale,
    base_id: numOrNull(s.base_id),
    has_diff: s.has_diff === true,
  }
}

const CHANGE_STATUS = ['added', 'removed', 'changed'] as const
const changeStatus = (v: unknown): 'added' | 'removed' | 'changed' =>
  CHANGE_STATUS.includes(v as never) ? (v as 'added' | 'removed' | 'changed') : 'changed'

function parseNodeChange(v: unknown): NodeChangeJson {
  const c = obj(v)
  return {
    status: changeStatus(c.status),
    type: str(c.type, 'step'),
    new_id: str(c.new_id),
    new_sid: str(c.new_sid),
    new_title: str(c.new_title),
    new_subtitle: str(c.new_subtitle),
    new_line: num(c.new_line),
    old_sid: str(c.old_sid),
    old_title: str(c.old_title),
    old_subtitle: str(c.old_subtitle),
    old_line: num(c.old_line),
    after: str(c.after),
    after_title: str(c.after_title),
    after_label: str(c.after_label),
  }
}

function parseEdgeChange(v: unknown): EdgeChangeJson {
  const c = obj(v)
  return {
    status: c.status === 'removed' ? 'removed' : 'added',
    kind: str(c.kind, 'next'),
    label: str(c.label),
    src: str(c.src),
    dst: str(c.dst),
    src_type: str(c.src_type),
    src_title: str(c.src_title),
    src_line: num(c.src_line),
    dst_type: str(c.dst_type),
    dst_title: str(c.dst_title),
    dst_line: num(c.dst_line),
  }
}

/** 流程差异 JSON（core 的 diff_to_json）→ GraphDiffJson；缺的字段补成空值。不是对象时为 null。 */
function parseGraphDiff(v: unknown): GraphDiffJson | null {
  if (!isObject(v)) return null
  const functions: Record<string, FunctionChangeJson> = {}
  for (const [sid, f] of Object.entries(obj(v.functions))) {
    const c = obj(f)
    functions[sid] = {
      status: changeStatus(c.status),
      sid: str(c.sid, sid),
      title: str(c.title),
      file: str(c.file),
      new_key: str(c.new_key),
      old_key: str(c.old_key),
      nodes: arr(c.nodes).map(parseNodeChange),
      edges: arr(c.edges).map(parseEdgeChange),
    }
  }
  const lines: LineChangeJson[] = arr(v.lines).map((l) => {
    const c = obj(l)
    return {
      status: changeStatus(c.status),
      sid: str(c.sid),
      title: str(c.title),
      group: str(c.group),
      new_id: str(c.new_id),
      functions: strs(c.functions),
      tasks_added: strs(c.tasks_added),
      tasks_removed: strs(c.tasks_removed),
      root: str(c.root),
    }
  })
  return { version: num(v.version, 1), functions, lines }
}

function parseDiffResult(v: unknown): DiffResult {
  const d = obj(v)
  if (!isObject(d.head)) throw new ApiError('format', '后端返回的流程差异不对：缺少 head')
  return {
    head: parseAnalysis(d.head),
    base: isObject(d.base) ? parseAnalysis(d.base) : null,
    diff: parseGraphDiff(d.diff),
    baseline: parseBaseline(d.baseline),
  }
}

function parseBaseline(v: unknown): Baseline | null {
  if (!isObject(v) || typeof v.at !== 'string') return null
  return v.kind === 'acknowledged' || v.kind === 'first' ? { kind: v.kind, at: v.at } : null
}

function parseOverrides(v: unknown): OverrideMap {
  const out: OverrideMap = {}
  for (const [sid, o] of Object.entries(obj(v))) {
    const item: LineOverride = {}
    const { title, group } = obj(o)
    if (typeof title === 'string' && title) item.title = title
    if (typeof group === 'string' && group) item.group = group
    out[sid] = item
  }
  return out
}

function parseSnippet(v: unknown): SourceSnippet {
  const s = obj(v)
  if (!Array.isArray(s.lines)) throw new ApiError('format', '后端返回的源码片段不对：缺少 lines')
  return {
    file: str(s.file),
    start: num(s.start, 1),
    end: num(s.end),
    total: num(s.total),
    lines: s.lines.map((l) => str(l)),
    stale: typeof s.stale === 'boolean' ? s.stale : null,
  }
}

// 测试运行

const count = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : 0)
const counts = (v: unknown): Record<string, number> => Object.fromEntries(Object.entries(obj(v)).map(([k, n]) => [k, count(n)]))

function parseVerify(v: unknown): RunVerify | null {
  if (!isObject(v)) return null
  const m = obj(v.mismatches)
  return {
    ok: typeof v.ok === 'boolean' ? v.ok : null,
    mismatches: { edge: count(m.edge), end: count(m.end), call: count(m.call) },
    functions_seen: count(v.functions_seen),
    distinct_paths: count(v.distinct_paths),
    steps_total: count(v.steps_total),
    plugin_errors: count(v.plugin_errors),
    plugin_error_types: counts(v.plugin_error_types),
    truncated_records: count(v.truncated_records),
    stale: strs(v.stale),
  }
}

function parseRunSummary(v: unknown): RunSummary {
  const s = obj(v)
  return {
    tests: count(s.tests),
    passed: count(s.passed),
    failed: count(s.failed),
    skipped: count(s.skipped),
    errors: count(s.errors),
    seconds: count(s.seconds),
    verify: parseVerify(s.verify),
    coverage: isObject(s.coverage) ? counts(s.coverage) : null,
    mismatch_list: arr(s.mismatch_list).map((x) => {
      const m = obj(x)
      return { kind: str(m.kind), function: str(m.function), from_line: num(m.from_line), to_line: num(m.to_line), detail: str(m.detail), count: count(m.count) }
    }),
    note: str(s.note),
  }
}

function parseRun(v: unknown): RunInfo {
  const r = obj(v)
  return {
    id: num(r.id),
    analysis_id: num(r.analysis_id),
    started_at: str(r.started_at),
    finished_at: str(r.finished_at),
    command: str(r.command),
    status: str(r.status, 'error'),
    exit_code: numOrNull(r.exit_code),
    paths_size: numOrNull(r.paths_size),
    summary: parseRunSummary(r.summary),
    scope: parseScope(r.scope),
  }
}

/** 运行的 scope：没有、不是对象、没有 line 时当作全量运行（null） */
function parseScope(v: unknown): RunScope | null {
  if (!isObject(v) || typeof v.line !== 'string' || !v.line) return null
  return { line: v.line, line_title: str(v.line_title), tests: count(v.tests), base_run: numOrNull(v.base_run) }
}

function parseLineTests(v: unknown): LineTestsResult {
  const d = obj(v)
  if (typeof d.ok !== 'boolean') throw new ApiError('format', '后端返回的测试名单不对：缺少 ok')
  return { ok: d.ok, code: str(d.code), reason: str(d.reason), tests: strs(d.tests), base_run: numOrNull(d.base_run) }
}

function parseProgress(v: unknown): LiveProgress | null {
  if (!isObject(v)) return null
  const lines: LiveProgress['lines'] = {}
  for (const [sid, c] of Object.entries(obj(v.lines))) if (isObject(c)) lines[sid] = { functions: count(c.functions), total: count(c.total) }
  let nodes: LiveProgress['nodes'] = null
  if (isObject(v.nodes)) {
    nodes = {}
    for (const [key, sids] of Object.entries(v.nodes)) if (Array.isArray(sids)) nodes[key] = strs(sids)
  }
  return {
    collected: typeof v.collected === 'number' && Number.isFinite(v.collected) && v.collected >= 0 ? v.collected : null,
    done: count(v.done),
    passed: count(v.passed),
    failed: count(v.failed),
    skipped: count(v.skipped),
    errors: count(v.errors),
    running: strs(v.running),
    running_more: count(v.running_more),
    recent: arr(v.recent)
      .filter(isObject)
      .map((t) => ({ test: str(t.test), outcome: str(t.outcome), seconds: count(t.seconds) })),
    lines,
    nodes,
  }
}

function parseRuns(v: unknown): RunsResult {
  const d = obj(v)
  const r = d.running
  const sc = isObject(r) ? r.scope : undefined
  const scope = isObject(sc) ? { line: str(sc.line), line_title: str(sc.line_title), tests: count(sc.tests) } : sc === null ? null : undefined
  const running = isObject(r)
    ? { started_at: str(r.started_at), seconds: count(r.seconds), phase: str(r.phase), progress: parseProgress(r.progress), ...(scope === undefined ? {} : { scope }) }
    : null
  const f = d.last_failure
  const lastFailure = isObject(f) ? { title: str(f.title), reason: str(f.reason), code: str(f.code), at: str(f.at) } : null
  return { running, runs: arr(d.runs).map(parseRun), last_failure: lastFailure }
}

function parseRunDetail(v: unknown): RunDetail {
  const d = obj(v)
  const tests = arr(d.tests).map((x): TestRow => {
    const t = obj(x)
    return { test: str(t.test), outcome: str(t.outcome), seconds: count(t.seconds), cases: count(t.cases), functions: count(t.functions), on_line: numOrNull(t.on_line) }
  })
  return { ...parseRun(d), tests, line_known: d.line_known === true }
}

function parseFunctionRun(fsid: string, v: unknown): FunctionRunJson {
  const f = obj(v)
  const nodes: FunctionRunJson['nodes'] = {}
  for (const [sid, st] of Object.entries(obj(f.nodes))) nodes[sid] = { visits: count(obj(st).visits), ns: count(obj(st).ns) }
  return {
    function: str(f.function, fsid),
    executions: count(f.executions),
    nodes,
    edges: counts(f.edges),
    exits: counts(f.exits),
    first_path: strs(f.first_path),
    off_graph: count(f.off_graph),
    has_time: f.has_time === true,
    calls: counts(f.calls),
    icalls: counts(f.icalls),
  }
}

/** 运行路径 JSON（core 的 run_paths_to_json，整份或一个测试的切片）→ RunPathsJson；没有 tests 列表时 format */
export function parseRunPaths(v: unknown): RunPathsJson {
  if (!isObject(v) || !Array.isArray(v.tests)) throw new ApiError('format', '后端返回的运行路径不对：没有 tests 列表')
  const tests = v.tests.map((x): TestRunJson => {
    const t = obj(x)
    const functions: Record<string, FunctionRunJson> = {}
    for (const [fsid, f] of Object.entries(obj(t.functions))) functions[fsid] = parseFunctionRun(fsid, f)
    return { test: str(t.test), outcome: str(t.outcome), seconds: count(t.seconds), cases: count(t.cases), functions, exc: str(t.exc), excs: strs(t.excs) }
  })
  const shapes: Record<string, FunctionShapeJson> = {}
  for (const [fsid, sh] of Object.entries(obj(v.shapes))) {
    if (typeof obj(sh).shape === 'string') shapes[fsid] = { shape: str(obj(sh).shape), dups: strs(obj(sh).dups) }
  }
  return { version: num(v.version, 1), tests, shapes, function_dups: strs(v.function_dups) }
}

function parseUncovered(v: unknown): UncoveredResult {
  const d = obj(v)
  return {
    outcomes: arr(d.outcomes).map((x) => {
      const o = obj(x)
      return {
        function: str(o.function),
        node: str(o.node),
        title: str(o.title),
        end_kind: str(o.end_kind),
        file: str(o.file),
        line: num(o.line),
        uncertain: o.uncertain === true,
      }
    }),
    stale: d.stale === true,
  }
}

function parseTestCommand(v: unknown): TestCommand {
  const d = obj(v)
  return { command: typeof d.command === 'string' && d.command ? d.command : null, confirmed: d.confirmed === true }
}

// 需求

const REQ_DIFF_KINDS: readonly ReqDiffKind[] = ['missing', 'extra', 'kind', 'order', 'no_line']
const REQ_COVERAGE: readonly ReqCoverage[] = ['covered', 'uncovered', 'uncertain', 'added', 'missing', 'unknown']

function parseReqComparison(v: unknown): ReqComparison | null {
  if (!isObject(v)) return null
  return {
    line_sid: str(v.line_sid),
    line_title: str(v.line_title),
    matches: v.matches === true,
    differences: arr(v.differences).map((x): ReqDifference => {
      const d = obj(x)
      const kind = REQ_DIFF_KINDS.includes(d.kind as ReqDiffKind) ? (d.kind as ReqDiffKind) : 'other'
      return { kind, text: str(d.text), type: str(d.type), title: str(d.title), node: str(d.node), design_nodes: strs(d.design_nodes) }
    }),
    notes: strs(v.notes),
    outcomes: arr(v.outcomes).map((x): ReqOutcome => {
      const o = obj(x)
      const covered = REQ_COVERAGE.includes(o.covered as ReqCoverage) ? (o.covered as ReqCoverage) : 'unknown'
      return { title: str(o.title), kind: str(o.kind, 'ok'), node: str(o.node), covered }
    }),
  }
}

function parseRequirement(v: unknown): Requirement {
  const r = obj(v)
  const comparison = parseReqComparison(r.comparison)
  const status = str(r.status)
  return {
    id: str(r.id),
    title: str(r.title),
    status,
    line: str(r.line),
    description: str(r.description),
    flow: str(r.flow),
    file: str(r.file),
    text: str(r.text),
    errors: strs(r.errors),
    archived: r.archived === true,
    design_line: typeof r.design_line === 'string' && r.design_line ? r.design_line : null,
    comparison,
    reachable_status: str(r.reachable_status, status),
  }
}

/**
 * GET /requirements → RequirementsResult。requirements 不是数组时 format；设计图读不了（不是图、格式版本不对）时 graph
 * 为 null（列表照样能看，只是画不出设计的流程）。
 */
export function parseRequirements(v: unknown): RequirementsResult {
  const d = obj(v)
  if (!Array.isArray(d.requirements)) throw new ApiError('format', '后端返回的需求列表不对：没有 requirements')
  let graph: ProjectGraph | null = null
  if (isObject(d.graph)) {
    try {
      graph = parseProject(d.graph)
    } catch {
      graph = null
    }
  }
  return { requirements: d.requirements.map(parseRequirement), graph, run: numOrNull(d.run) }
}

const NOT_A_GRAPH = '后端返回的不是 pystepmap 的分析结果（JSON 里没有图的格式版本、函数和功能线）：点"重试"再读一次；还不行就重启 pystepmap serve'

/**
 * 图 JSON（已 JSON.parse）→ ProjectGraph。
 * - 不是 pystepmap 的图（不是对象；没有 schema_version，也没有 functions 和 lines；schema_version 不是数）：format；
 * - 格式版本比页面认识的新：format；
 * - 旧版引擎生成的（schema_version 比现在的小；没有 schema_version 但有 functions 和 lines 的早期图；功能线没有 sid）：
 *   old-graph，要重新分析。
 */
export function parseGraph(data: unknown): ProjectGraph {
  if (!isObject(data)) throw new ApiError('format', NOT_A_GRAPH)
  const version = data.schema_version
  const shaped = isObject(data.functions) && Array.isArray(data.lines)
  if (version === undefined ? !shaped : typeof version !== 'number') throw new ApiError('format', NOT_A_GRAPH)
  if (typeof version === 'number' && version > SCHEMA_VERSION) {
    throw new ApiError(
      'format',
      `图的格式版本（${version}）比这个页面认识的（${SCHEMA_VERSION}）新：刷新页面；还不行就在 web/ 下重新 npm run build`,
    )
  }
  if (version !== SCHEMA_VERSION) {
    throw new ApiError('old-graph', `这份分析结果是旧版引擎生成的（图的格式版本 ${String(version ?? '没有')}），点"重新分析"用现在的引擎重新生成`)
  }
  let graph: ProjectGraph
  try {
    graph = parseProject(data)
  } catch (e) {
    throw new ApiError('format', `图数据不对：${errorText(e)}`)
  }
  if (graph.lines.some((l) => !l.sid)) {
    throw new ApiError(
      'old-graph',
      '这份分析结果是旧版引擎生成的（功能线没有 sid，手工改的标题和分组对不上），点"重新分析"用现在的引擎重新生成',
    )
  }
  return graph
}

// ---------------------------------------------------------------------------------------------------------------
// 请求

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>

const enc = encodeURIComponent

/** 开发服务器转发失败时回的状态码（Vite 是 502，空的 text/plain） */
const GATEWAY = new Set([502, 503, 504])

/** 读完响应体；读到一半连接断了（后端停了、网络断了）算连不上 */
async function bodyText(resp: Response): Promise<string> {
  try {
    return await resp.text()
  } catch {
    throw new ApiError('unreachable', `${UNREACHABLE}（响应读到一半断了）`, { status: resp.status })
  }
}

async function bodyJson(resp: Response): Promise<unknown> {
  const text = await bodyText(resp)
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

/** 非 2xx 的响应 → ApiError：后端的 {"error"} 原样；转发失败算连不上；别的写状态码 */
async function failure(resp: Response): Promise<ApiError> {
  const body = obj(await bodyJson(resp))
  if (typeof body.error === 'string' && body.error) {
    return new ApiError('http', body.error, {
      status: resp.status,
      code: typeof body.code === 'string' ? body.code : null,
      projectId: typeof body.project_id === 'string' ? body.project_id : null,
      data: body,
    })
  }
  if (GATEWAY.has(resp.status)) return new ApiError('unreachable', UNREACHABLE, { status: resp.status })
  return new ApiError('http', `后端返回了 HTTP ${resp.status}${resp.statusText ? ` ${resp.statusText}` : ''}`, { status: resp.status, data: body })
}

/**
 * 接口客户端。fetchFn 默认是浏览器的 fetch（每次调用时取 globalThis.fetch，测试可以换掉）；base 是接口前缀。
 */
export function createApi(fetchFn: FetchFn = (input, init) => globalThis.fetch(input, init), base = '/api') {
  async function send(path: string, init?: RequestInit): Promise<Response> {
    let resp: Response
    try {
      resp = await fetchFn(base + path, init)
    } catch {
      throw new ApiError('unreachable', UNREACHABLE)
    }
    if (!resp.ok) throw await failure(resp)
    return resp
  }

  async function json(path: string, init?: RequestInit): Promise<unknown> {
    const resp = await send(path, init)
    const text = await bodyText(resp)
    try {
      return JSON.parse(text)
    } catch {
      throw new ApiError('format', `后端返回的不是 JSON（${base + path}）：端口上跑的可能不是 pystepmap serve`, { status: resp.status })
    }
  }

  const withBody = (method: string, body: unknown): RequestInit => ({
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  const project = (id: string) => `/projects/${enc(id)}`

  /** 读一张图（gzip 传输，按已收到的字节报告进度）：最新一次分析的，或者变更记录当时的 */
  async function readGraph(path: string, onProgress?: ProgressFn): Promise<GraphResult> {
    const resp = await send(path)
    const size = Number(resp.headers.get('X-Pystepmap-Graph-Size'))
    const length = Number(resp.headers.get('Content-Length'))
    const total = size > 0 ? size : !resp.headers.get('Content-Encoding') && length > 0 ? length : null
    let bytes: Uint8Array
    try {
      bytes = await readAll(resp, total, onProgress)
    } catch {
      throw new ApiError('unreachable', `${UNREACHABLE}（读图读到一半断了）`)
    }
    let data: unknown
    try {
      data = JSON.parse(new TextDecoder().decode(bytes))
    } catch {
      throw new ApiError('format', '后端返回的图不是合法的 JSON')
    }
    const analysis = Number(resp.headers.get('X-Pystepmap-Analysis'))
    return { graph: parseGraph(data), analysisId: analysis > 0 ? analysis : null, bytes: bytes.length }
  }

  return {
    async health(): Promise<Health> {
      const h = obj(await json('/health'))
      return { ok: h.ok === true, version: str(h.version), engine_version: str(h.engine_version) }
    },

    async listProjects(): Promise<ProjectInfo[]> {
      const data = await json('/projects')
      if (!Array.isArray(data)) throw new ApiError('format', '后端返回的项目列表不对：不是数组')
      return data.map(parseProjectInfo)
    },

    async getProject(id: string): Promise<ProjectInfo> {
      return parseProjectInfo(await json(project(id)))
    },

    /** 添加项目：后端校验目录、查重、分析、入库，分析完才返回（大项目几秒）。名称、配置为空时不发。 */
    async addProject(p: NewProject): Promise<ProjectInfo> {
      const body: Record<string, unknown> = { root: p.root }
      if (p.name && p.name.trim()) body.name = p.name.trim()
      if (p.config) body.config = p.config
      return parseProjectInfo(await json('/projects', withBody('POST', body)))
    },

    async deleteProject(id: string): Promise<void> {
      await send(project(id), { method: 'DELETE' })
    },

    /** 重新分析：分析完才返回（带新的一次分析的项目信息）；正在分析时 409，code 为 busy。 */
    async analyzeProject(id: string): Promise<ProjectInfo> {
      return parseProjectInfo(await json(`${project(id)}/analyze`, { method: 'POST' }))
    },

    /** 最新一次分析的图；onProgress 按已收到的字节报告进度。 */
    getGraph(id: string, onProgress?: ProgressFn): Promise<GraphResult> {
      return readGraph(`${project(id)}/graph`, onProgress)
    },

    /**
     * 一条变更记录当时的图（标为已看的那次分析的图，和 getGraph 同一个格式）：记录里差异的节点 new_id 就是这张图里的。
     * 没有这条记录 404；记录没有存图 404、code 为 no_graph。
     */
    getReviewGraph(id: string, reviewId: number, onProgress?: ProgressFn): Promise<GraphResult> {
      return readGraph(`${project(id)}/reviews/${reviewId}/graph`, onProgress)
    },

    async getOverrides(id: string): Promise<OverrideMap> {
      return parseOverrides(await json(`${project(id)}/overrides`))
    },

    /**
     * 改一条功能线的手工标题和分组：整条改写（没给的项 = 不改），两项都空 = 删除这条。sid 里有 /、:、#，放在查询参数里。
     * 返回改后这个项目的全部手工项。
     */
    async putOverride(id: string, sid: string, value: { title?: string | null; group?: string | null }): Promise<OverrideMap> {
      const body = { title: value.title?.trim() || null, group: value.group?.trim() || null }
      return parseOverrides(await json(`${project(id)}/overrides?sid=${enc(sid)}`, withBody('PUT', body)))
    },

    /**
     * 源码片段：file 是相对项目根的路径，行号从 1 起、含两端（后端一次最多 400 行）。analysis 是画布上那张图的分析
     * id：后端按那一次判断文件改过没有（那次已被清理时按最新一次）；不知道时不发。
     */
    async getSource(id: string, file: string, start: number, end: number, analysis: number | null = null): Promise<SourceSnippet> {
      const q = new URLSearchParams({ file, start: String(start), end: String(end) })
      if (analysis !== null) q.set('analysis', String(analysis))
      return parseSnippet(await json(`${project(id)}/source?${q}`))
    },

    /** 代码在最新一次分析之后改过没有、最新一次分析是哪次、有没有流程差异。旧版后端没有这个接口（404）。 */
    async getStatus(id: string): Promise<ProjectStatus> {
      return parseStatus(await json(`${project(id)}/status`))
    },

    /** 最新一次分析和对比基准之间的流程差异。旧版后端没有这个接口（404）。 */
    async getDiff(id: string): Promise<DiffResult> {
      return parseDiffResult(await json(`${project(id)}/diff`))
    },

    /**
     * 把流程变化标为看过（清掉对比基准）。analysis 是界面上看的那次分析：之后又有了新的分析时后端回 409、code 为
     * moved（新的变化还没看）；正在分析时 409 busy。返回同 getDiff 的形状（base、diff 为 null）。
     */
    async ackDiff(id: string, analysis: number): Promise<DiffResult> {
      return parseDiffResult(await json(`${project(id)}/diff/ack?analysis=${analysis}`, { method: 'POST' }))
    },

    async getConfig(id: string): Promise<Record<string, unknown>> {
      return obj(await json(`${project(id)}/config`))
    },

    /** 改分析配置（不自动重新分析）。 */
    async putConfig(id: string, config: Record<string, unknown>): Promise<Record<string, unknown>> {
      return obj(await json(`${project(id)}/config`, withBody('PUT', config)))
    },

    // 测试运行（阶段 6）。旧版后端没有这些接口（404，isMissingEndpoint）。

    async getTestCommand(id: string): Promise<TestCommand> {
      return parseTestCommand(await json(`${project(id)}/test-command`))
    },

    /** 项目目录和它的直接子目录里找到的虚拟环境解释器（给测试命令的表单用）；旧版后端没有这个接口时空 */
    async getInterpreters(id: string): Promise<Interpreter[]> {
      try {
        const d = obj(await json(`${project(id)}/interpreters`))
        return arr(d.interpreters).map((x) => ({ cwd: str(obj(x).cwd), python: str(obj(x).python) })).filter((x) => x.python)
      } catch (e) {
        if (isMissingEndpoint(e)) return []
        throw e
      }
    },

    /** 改测试命令（null 或空 = 清掉）；命令变了后端把"确认过"清掉。 */
    async putTestCommand(id: string, command: string | null): Promise<TestCommand> {
      return parseTestCommand(await json(`${project(id)}/test-command`, withBody('PUT', { command })))
    },

    /**
     * 开始运行测试（后端在后台跑，202 就回）。命令还没确认过时 409、code confirm（error.data 里有 command、root）：
     * 用户同意后再发一次，带上 confirm 和确认框里给用户看的那条命令原文（confirmed）；项目的命令已经不是它时后端又回
     * 409 confirm（带现在的命令）。没填命令 400 no_command；正在分析或已经在跑 409 busy。line 是功能线 sid：只跑走过
     * 这条线的测试（名单同 getLineTests；挑不出来 409 no_full_run / no_tests / no_index，图里没有这条线 404）；不给是全部测试。
     */
    async startRun(id: string, confirmed: string | null = null, line: string | null = null): Promise<void> {
      const body: Record<string, unknown> = confirmed !== null ? { confirm: true, command: confirmed } : {}
      if (line) body.line = line
      await send(`${project(id)}/runs`, withBody('POST', body))
    },

    /**
     * 只跑这条功能线的话会跑哪些测试（名单取自最近一次全量运行）；挑不出来时 ok 为假、带原因（200）。图里没有这条线
     * 404；旧版后端没有这个接口（404，isMissingEndpoint）。
     */
    async getLineTests(id: string, line: string): Promise<LineTestsResult> {
      return parseLineTests(await json(`${project(id)}/line-tests?line=${enc(line)}`))
    },

    /** 允许 / 不允许 AI 经 MCP 运行单条功能线的测试；返回改后的值。旧版后端没有这个接口（404，isMissingEndpoint）。 */
    async putMcpTests(id: string, allow: boolean): Promise<boolean> {
      return obj(await json(`${project(id)}/mcp-tests`, withBody('PUT', { allow }))).allow === true
    },

    /**
     * 正在跑的（没有时 null）、最近的几次运行（新的在前）、202 之后就失败了的原因。liveLine 是画布上的功能线 sid：测试
     * 命令跑着时进度里带上这条线上走到过的节点（running.progress.nodes）。
     */
    async listRuns(id: string, liveLine: string | null = null): Promise<RunsResult> {
      const q = liveLine ? `?live_line=${enc(liveLine)}` : ''
      return parseRuns(await json(`${project(id)}/runs${q}`))
    },

    /**
     * 取消正在跑的测试；返回后端的说明（比如在运行前的重新分析期间取消；没有时为空串）。没有在跑的 409 not_running；
     * 测试已经跑完、正在整理结果时 409 too_late。
     */
    async cancelRun(id: string): Promise<string> {
      return str(obj(await json(`${project(id)}/runs/cancel`, { method: 'POST' })).note)
    },

    /** 最近一次运行的输出末尾（只在后端进程内存里，重启后没有） */
    async runOutput(id: string): Promise<string[]> {
      return strs(obj(await json(`${project(id)}/runs/output`)).lines)
    },

    /** 一次运行和它的每个测试；line 是功能线 sid：每个测试带 on_line（走过这条线上几个函数） */
    async getRun(id: string, runId: number, line: string | null = null): Promise<RunDetail> {
      const q = line ? `?line=${enc(line)}` : ''
      return parseRunDetail(await json(`${project(id)}/runs/${runId}${q}`))
    },

    /** 一个测试的运行路径（不下载整份）：和整份同形状，tests 里只有它；analysisId 是这次运行用的分析 */
    async getTestPaths(id: string, runId: number, test: string): Promise<{ paths: RunPathsJson; analysisId: number | null }> {
      const resp = await send(`${project(id)}/runs/${runId}/paths?test=${enc(test)}`)
      const text = await bodyText(resp)
      let data: unknown
      try {
        data = JSON.parse(text)
      } catch {
        throw new ApiError('format', '后端返回的运行路径不是合法的 JSON')
      }
      const analysis = Number(resp.headers.get('X-Pystepmap-Analysis'))
      return { paths: parseRunPaths(data), analysisId: analysis > 0 ? analysis : null }
    },

    /** 没有测试走到的结局（按最新一次分析的图）；line 是功能线 sid，不给时是全部功能线 */
    async getUncovered(id: string, runId: number, line: string | null = null): Promise<UncoveredResult> {
      const q = line ? `?line=${enc(line)}` : ''
      return parseUncovered(await json(`${project(id)}/runs/${runId}/uncovered${q}`))
    },

    /**
     * 项目里的需求（后端现读 .pystepmap/requirements/）、各自和最新一次分析的对照、设计流程的图。还没有分析结果时
     * 404；旧版后端没有这个接口（404，isMissingEndpoint）。
     */
    async getRequirements(id: string): Promise<RequirementsResult> {
      return parseRequirements(await json(`${project(id)}/requirements`))
    },

    /** 变更记录（新的在前）；旧版后端没有这个接口时是空的 */
    async listReviews(id: string): Promise<ReviewInfo[]> {
      try {
        return arr(obj(await json(`${project(id)}/reviews`)).reviews).map(parseReview)
      } catch (e) {
        if (isMissingEndpoint(e)) return []
        throw e
      }
    },

    /** 一条变更记录的明细（带存下的流程差异和文字）；没有这条记录（只留最近 10 条）404 */
    async getReview(id: string, reviewId: number): Promise<ReviewDetail> {
      const d = await json(`${project(id)}/reviews/${reviewId}`)
      return { ...parseReview(d), diff: parseGraphDiff(obj(d).diff), text: str(obj(d).text) }
    },
  }
}

export type Api = ReturnType<typeof createApi>

/** 读完整个响应体，边读边报告进度 */
async function readAll(resp: Response, total: number | null, onProgress?: ProgressFn): Promise<Uint8Array> {
  if (!resp.body) {
    const buf = new Uint8Array(await resp.arrayBuffer())
    onProgress?.(buf.length, total)
    return buf
  }
  const reader = resp.body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  onProgress?.(0, total)
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    received += value.length
    onProgress?.(received, total)
  }
  const out = new Uint8Array(received)
  let at = 0
  for (const c of chunks) {
    out.set(c, at)
    at += c.length
  }
  return out
}

/** 界面用的那一个客户端 */
export const api = createApi()
