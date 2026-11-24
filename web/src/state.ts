// 界面状态和动作：平台后端里的项目、读哪个项目、看哪条功能线、折叠 / 展开 / 钻入的状态、选中了什么、手工改的功能线
// 标题和分组、添加 / 重新分析 / 删除项目。
// 每次状态变化都重新建视图、重新排版（几十毫秒）；"被操作的元素"记成锚点，画布排好后据此平移视口，让它在屏幕上不动。
// 快速连点时只认最后一次（latest.ts）：读项目（图、手工项一起算一次）、排版都按发号作废旧的；换线的起因留到这条线
// 真正画上去为止。重新分析后照原样保留展开状态（ViewState 里的 id 都由 sid 拼成，新图里没有的自然不起作用），功能线
// 按 sid 找回，找不到时回到列表第一条并提示。换项目时要看的功能线（地址栏里的、调用时指定的）在这个项目的图画上去
// 之前一直记着：中途点了重新分析、重试，或者读失败了，都不丢。
// 列表里有项目正在分析时（刷新页面前点的、别的页面点的），每 2 秒重新取一次项目列表，直到都分析完或者连不上后端；
// 当前项目有了更新的一次分析就换上新图，保留功能线和展开状态。
// 过期检查（GET /status）：打开 / 切换项目时、重新分析之后、窗口回到前台时（3 秒内不重复）各查一次，不定时轮询；
// 发现别的进程（pystepmap mcp、别的页面）做了新的分析就自动换上新图。流程差异（GET /diff，相对对比基准：上次标为已看时，没标过就是第一次分析）在这次分析有差异时取，
// 和图对不上（不是同一次分析）时两样都重取。旧版后端没有这两个接口（404）：当作没有信息，不提示、不重试。

// 需求（GET /requirements）：左边列表可以换成"需求"；选中一条时画布上画它设计的流程（后端给的设计图和分析结果同一个
// 格式，同一套视图、排版、节点组件），和实现对不上的节点由 reqMarks 标出来，右栏是需求的说明和对照结果
// （RequirementPanel.vue）。功能线的展开状态在看需求时记着，回到功能线时照旧。打开项目时、重新分析（或换上别处的新
// 分析）之后、点"刷新"时重新取；旧版后端没有这个接口（404"没有这个接口"）时整个功能不出现。地址栏里写
// #/<项目>/req:<编号>（requirements.ts）。

// 变更记录（GET /reviews：每次标为已看时存下的那次流程变化，连同当时的图）：openReview 把画布换成那条记录当时的图、
// 标出那次的变化（review；画布、功能线列表、差异栏、详情栏读的是 canvasGraph、diffView，和现在的差异同一套代码），
// 只读——标为已看、过期条、运行路径、需求、改标题都不出现。project、graphOf、diffResult 仍是项目现在的，照常更新
// （重新分析、别处的新分析在底下换好），"回到现在"（leaveReview）时画布换回来，功能线、展开状态、页签照看记录之前的
// 样子。地址栏里写 #/<项目>/review:<编号>/<功能线>（reviewview.ts）。

import { computed, ref, shallowRef, watch } from 'vue'
import {
  ApiError,
  api,
  errorText,
  isApiError,
  isMissingEndpoint,
  type ApiErrorKind,
  type DiffResult,
  type GraphDiffJson,
  type NewProject,
  type OverrideMap,
  type ProjectInfo,
  type ProjectStatus,
  type Requirement,
  type RequirementsResult,
  type ReviewDetail,
  type ReviewInfo,
} from './api'
import { buildDiffView, emptyDiffView, lineChangeList, revealStop, viewMarks, type ChangeStop, type FlowDiffView, type ViewMarks } from './diffview'
import {
  buildLineView,
  drillChainOf,
  emptyState,
  expandAllState,
  expandEverythingState,
  MAX_EXPAND_DRILLS,
  layoutLineView,
  setOpen,
  type Box,
  type CardView,
  type DrillOption,
  type FoldView,
  type LineTask,
  type LineView,
  type NodeView,
  type Positioned,
  type ProjectGraph,
  type ViewState,
} from './graph'
import { MAX_AUTO_DRILLS, revealWalkedState, type RunOverlay } from './runview'
import { createLatestLoader, createLayoutRunner, createSequencer } from './latest'
import { applyOverrides, firstLine, patchOverride, type DisplayLine } from './overrides'
import { slowAfter, watchSlow } from './slow'
import {
  REQ_REF,
  designLineSid,
  emptyReqMarks,
  findReq,
  firstReqKey,
  lineRootKey,
  nodeStop,
  parseReqRef,
  reqKeyOf,
  reqRef,
  reqViewMarks,
  type ReqViewMarks,
} from './requirements'
import { headKept, parseReviewRef, reviewFailText, reviewHash, reviewLine, reviewNeighbors, type ReviewRef } from './reviewview'
import { hasNewerAnalysis, staleView, type StaleView } from './stale'

export type SelectionKind = 'node' | 'fold' | 'card'
export interface Selection {
  kind: SelectionKind
  id: string
}

/**
 * 视口锚点：操作前那个元素的位置；排好后取 ids 里第一个还在画布上的元素，把它挪回原来的屏幕位置。
 * reveal：收起类的操作（收起一段、展开 / 收起卡片、收回钻入）——操作前那个元素不在屏幕上时（多半是在详情栏里点的），
 * 不死守看不见的位置，而是把排好后的元素拉进屏幕（viewport.ts）。
 */
export interface Anchor {
  oldBox: Box
  ids: string[]
  reveal: boolean
}

/** 画布要做的事：fit 适应屏幕；focus 对准一个元素；peek 元素不在屏幕上时平移过去（不改缩放，回放用） */
export type CanvasCommand = { seq: number; type: 'fit' } | { seq: number; type: 'focus' | 'peek'; id: string }

/** 平台后端：connecting 第一次连接中；down 连不上；error 连上了但列项目出错 */
export type BackendStatus = 'connecting' | 'ok' | 'down' | 'error'

/** 读项目失败：哪个项目、哪种错（api.ts 的 ApiError.kind，别的错误为 other）、状态码、中文说明 */
export interface LoadFailure {
  project: string
  kind: ApiErrorKind | 'other'
  status: number
  message: string
}

export interface LoadProgress {
  received: number
  total: number | null
}

export interface Notice {
  seq: number
  type: 'info' | 'success' | 'warning' | 'error'
  text: string
  /** 显示在右上角（"已载入新的分析结果"），别的在下方 */
  corner?: boolean
}

/** 正在编辑的功能线，和编辑框出现的位置（屏幕坐标） */
export interface EditTarget {
  sid: string
  x: number
  y: number
}

export const backend = ref<BackendStatus>('connecting')
export const backendError = ref('')
/** 平台里的项目（按添加顺序） */
export const projects = ref<ProjectInfo[]>([])
/** 选中的项目 */
export const projectId = ref('')
/** 选中项目的图；读的时候、读失败时为 null。不为 null 时一定是 projectId 的图 */
export const project = shallowRef<ProjectGraph | null>(null)
/** 画布上这张图是哪个项目、哪一次分析的 */
export const graphOf = shallowRef<{ project: string; analysis: number | null } | null>(null)
/** 选中项目的手工标题和分组 */
export const overrides = shallowRef<OverrideMap>({})
export const loading = ref(false)
/** 读图的进度（按已收到的字节）；不在读或用的是缓存时为 null */
export const progress = shallowRef<LoadProgress | null>(null)
export const loadFailure = shallowRef<LoadFailure | null>(null)
/** 排版出错 */
export const error = ref('')
/** 这个页面发起的、还没完成的重新分析（项目 id） */
export const reanalyzing = ref<string[]>([])
/** 其中已经等了很久（slow.ts 的 slowAfter，30 秒）的 */
export const slowReanalyzing = ref<string[]>([])
export const addDialogOpen = ref(false)
/** 添加项目的请求已经等了很久 */
export const addingSlow = ref(false)
/** 正在看哪个项目的分析配置（ConfigDialog.vue）；关着时为空 */
export const configFor = ref('')
export const editing = ref<EditTarget | null>(null)
/** 要弹出的提示（Notices.vue 显示后不删，按 seq 认新的） */
export const notices = shallowRef<Notice[]>([])

export const lineSid = ref('')
export const viewState = shallowRef<ViewState>(emptyState())
export const view = shallowRef<LineView | null>(null)
export const layout = shallowRef<Positioned | null>(null)
/** 每排好一次加一；画布据此更新 */
export const layoutTick = ref(0)
/**
 * 这次排版的起因：换了功能线（视口重新定位）、改了展开状态（视口按锚点保持），或者从变更记录回到现在、还是原来那条线
 * 和展开状态（back：视口照看记录之前的样子，FlowCanvas.vue 记着）
 */
export const layoutReason = ref<'line' | 'state' | 'back'>('line')
let pendingAnchor: Anchor | null = null
/** 下一次换线的排版是"回到现在"、功能线和展开状态都照旧：视口也照旧 */
let backPending = false
/** 看需求时，功能线的展开状态记在这里（回到功能线时照旧）；没有时回到功能线按默认 */
let savedLineState: ViewState | null = null
export const takeAnchor = (): Anchor | null => {
  const a = pendingAnchor
  pendingAnchor = null
  return a
}

export const selection = ref<Selection | null>(null)
/** goto 跳转后短暂高亮的卡片 */
export const flash = ref<string | null>(null)
/** 鼠标停在"收起"按钮上时，把要收起的那段节点标出来 */
export const hoverFold = ref<string | null>(null)
/** 画布正在平移 / 缩放（拖动、滚轮、动画）：悬停提示这时不出来 */
export const canvasMoving = ref(false)
export const search = ref('')
export const canvasCommand = shallowRef<CanvasCommand | null>(null)
let commandSeq = 0

/** 当前项目的过期检查结果（GET /status）；还没查、查不了（旧版后端、出错）时为 null */
export const projectStatus = shallowRef<ProjectStatus | null>(null)
/** 当前项目的流程差异（GET /diff、标为已看的返回）；没有时为 null */
export const diffResult = shallowRef<(DiffResult & { project: string }) | null>(null)
/** 差异栏上定位到了第几处（-1 = 还没定位） */
export const stopIndex = ref(-1)
/**
 * 画布上方的页签：流程设计（图、流程差异）/ 运行路径（测试走过的路径，runstate.ts）。流程差异的标记和差异栏只在
 * 流程设计里，运行路径的标注只在运行路径里。
 */
export const tab = ref<'design' | 'run'>('design')
/** 正在标为已看的项目 */
const ackingFor = ref<string[]>([])

/** 左边列表看的是功能线还是需求；看需求时画布上是选中的需求设计的流程 */
export const listMode = ref<'lines' | 'requirements'>('lines')
/** 当前项目的需求（GET /requirements）；还没取到、取不了（旧版后端、还没有分析结果）时为 null */
export const reqData = shallowRef<(RequirementsResult & { project: string }) | null>(null)
/** 正在取需求 */
export const reqLoading = ref(false)
/** 上一次取需求出的错（旧版后端没有这个接口不算错）；成功后清空 */
export const reqError = ref('')
/** 选中的需求（需求文件，相对项目根）；回到功能线后还记着，再切回"需求"时还是它 */
export const reqKey = ref('')

/** 画布上正在回看的变更记录：存下的那次差异和当时的图（只读） */
export interface ReviewShown {
  project: string
  id: number
  info: ReviewInfo
  graph: ProjectGraph
  diff: GraphDiffJson | null
}
/** 正在回看的变更记录；不在看时为 null（画布上是项目现在的图） */
export const review = shallowRef<ReviewShown | null>(null)
/** 正在读的变更记录（横幅上说"正在读"）；没在读时为 null */
export const reviewLoading = shallowRef<{ project: string; id: number } | null>(null)
/** 读记录当时的图的进度（按已收到的字节） */
export const reviewProgress = shallowRef<LoadProgress | null>(null)
/** 当前项目的变更记录（新的在前，GET /reviews）；旧版后端、读不到时是空的 */
const reviewsOf = shallowRef<{ project: string; list: ReviewInfo[] } | null>(null)

// ---------------------------------------------------------------------------------------------------------------
// 查找

export const currentProject = computed<ProjectInfo | null>(() => projects.value.find((p) => p.id === projectId.value) ?? null)
/** 当前项目正在标为已看 */
export const acking = computed(() => ackingFor.value.includes(projectId.value))
/** 选中项目的名字（"复制给 AI"、标题栏用） */
export const projectName = computed(() => currentProject.value?.name ?? '')

/** 画布上的图：看变更记录时是那条记录当时的图，否则是项目最新一次分析的图（project） */
export const canvasGraph = computed<ProjectGraph | null>(() => review.value?.graph ?? project.value)
/** 画布上这张图是哪个项目的哪一次分析（源码片段按它判断文件改过没有）；看变更记录时是记录的那次 */
export const canvasOf = computed<{ project: string; analysis: number | null } | null>(() => {
  const r = review.value
  return r ? { project: r.project, analysis: r.info.head_id } : graphOf.value
})
/** 当前项目的变更记录（新的在前） */
export const reviews = computed<ReviewInfo[]>(() => (reviewsOf.value?.project === projectId.value ? reviewsOf.value.list : []))
/**
 * 正在看的变更记录那次分析确定还在后端（reviewview.ts 的 headKept）：源码接口按它判断文件改过没有；不确定时详情栏说
 * "这是现在的源码，行号是当时的"
 */
export const reviewHeadKept = computed(() => {
  const r = review.value
  if (!r) return false
  const a = currentProject.value?.analysis
  const s = projectStatus.value
  const d = diffResult.value?.project === r.project ? diffResult.value : null
  return headKept(r.info.head_id, [a?.id, a?.base_id, s?.analysis_id, s?.base_id, d?.head.id, d?.base?.id])
})

/** 应用了手工标题和分组的功能线（列表、标题区、搜索、悬停浮层、"复制给 AI"都用它）；看变更记录时是那时的图里的 */
export const displayLines = computed<DisplayLine[]>(() => (canvasGraph.value ? applyOverrides(canvasGraph.value.lines, overrides.value) : []))
export const displayBySid = computed(() => new Map(displayLines.value.map((d) => [d.sid, d])))

export const nodeById = computed(() => new Map((view.value?.nodes ?? []).map((n) => [n.id, n])))
export const foldById = computed(() => new Map((view.value?.folds ?? []).map((f) => [f.id, f])))
export const cardById = computed(() => new Map((view.value?.cards ?? []).map((c) => [c.id, c])))
export const edgeById = computed(() => new Map((view.value?.edges ?? []).map((e) => [e.id, e])))

/** 当前功能线（应用了手工项） */
export const currentDisplay = computed<DisplayLine | null>(() => displayBySid.value.get(lineSid.value) ?? null)

/** 过期条 */
export const staleInfo = computed<StaleView>(() => staleView(projectStatus.value))

const NO_DIFF = emptyDiffView()
/** 项目现在的流程差异（最新一次分析相对对比基准）：差异和图是同一次分析才用（对不上时 syncDiff 正在重取） */
export const presentDiffView = computed<FlowDiffView>(() => {
  const d = diffResult.value
  const shown = graphOf.value
  if (!d || !d.diff || !shown || d.project !== shown.project || d.head.id !== shown.analysis) return NO_DIFF
  return buildDiffView(project.value, d.diff)
})
/** 画布上这张图的流程差异：看变更记录时是那条记录存下的那次变化，否则是现在的 */
export const diffView = computed<FlowDiffView>(() => {
  const r = review.value
  if (r) return r.diff ? buildDiffView(r.graph, r.diff) : NO_DIFF
  return presentDiffView.value
})
/** 当前项目的需求（按文件名的顺序）；不是这个项目的、还没取到时为空 */
export const requirements = computed<Requirement[]>(() =>
  reqData.value && reqData.value.project === projectId.value ? reqData.value.requirements : [],
)
/** 画布上是需求的设计图（左边列表看的是需求） */
export const reqMode = computed(() => listMode.value === 'requirements')
/** 画布上的需求；不在看需求时为 null */
export const currentReq = computed<Requirement | null>(() =>
  reqMode.value ? (requirements.value.find((r) => reqKeyOf(r) === reqKey.value) ?? null) : null,
)
/** 需求设计流程的图（后端给的；读不了时为 null） */
export const reqGraph = computed<ProjectGraph | null>(() =>
  reqData.value && reqData.value.project === projectId.value ? reqData.value.graph : null,
)
const NO_REQ_MARKS = emptyReqMarks()
/** 设计图上和实现对不上的节点（还没有实现 / 分类不一样 / 走向不一致），折叠着的分支里有几个；不看需求时为空 */
export const reqMarks = computed<ReqViewMarks>(() => (reqMode.value ? reqViewMarks(view.value, currentReq.value) : NO_REQ_MARKS))

/** 当前功能线上的每一处变化（差异栏的"上一处 / 下一处"）；"运行路径"页签里、看需求时为空（差异栏不显示） */
export const changeStops = computed<ChangeStop[]>(() =>
  tab.value === 'design' && !reqMode.value && canvasGraph.value && lineSid.value
    ? lineChangeList(diffView.value, canvasGraph.value, lineSid.value)
    : [],
)
/**
 * 画布上要标出来的节点、连线，卡片 / 折叠 / 步骤里的变化数（按每一处落在哪里数；改了不重新排版）。只在"流程设计"
 * 页签里有；"运行路径"页签里是空的（那里的标注是 runstate.ts 的 canvasRunMarks），看需求时也是空的（那里是 reqMarks）。
 */
export const marks = computed<ViewMarks>(() =>
  viewMarks(tab.value === 'design' && !reqMode.value ? view.value : null, diffView.value, canvasGraph.value, changeStops.value),
)

export type Selected =
  | { kind: 'node'; node: NodeView }
  | { kind: 'fold'; fold: FoldView }
  | { kind: 'card'; card: CardView }
  | null

export const selected = computed<Selected>(() => {
  const s = selection.value
  if (!s) return null
  if (s.kind === 'node') {
    const node = nodeById.value.get(s.id)
    return node ? { kind: 'node', node } : null
  }
  if (s.kind === 'fold') {
    const fold = foldById.value.get(s.id)
    return fold && fold.collapsed ? { kind: 'fold', fold } : null
  }
  const card = cardById.value.get(s.id)
  return card ? { kind: 'card', card } : null
})

/** 功能线任务树里的任务数（不算 goto） */
export function taskCount(t: LineTask): number {
  return (t.goto ? 0 : 1) + t.children.reduce((n, c) => n + taskCount(c), 0)
}

// ---------------------------------------------------------------------------------------------------------------
// 提示

/** 这条提示由哪个消息位置显示：右上角的那个（corner）只显示标了 corner 的，下方的显示其余的（Notices.vue） */
export const noticeShownIn = (n: Notice, corner: boolean): boolean => !!n.corner === corner

let noticeSeq = 0
export function notify(type: Notice['type'], text: string, corner = false) {
  const n: Notice = { seq: ++noticeSeq, type, text }
  if (corner) n.corner = true
  notices.value = [...notices.value.slice(-9), n]
}

// ---------------------------------------------------------------------------------------------------------------
// 地址栏：#/<项目 id>/<功能线 sid>，刷新后回到原处（项目也认名字，阶段 3 的旧链接还能用）

const hasWindow = typeof window !== 'undefined' && typeof location !== 'undefined'

/**
 * 换项目时要看、还没画上去的功能线（地址栏里的，或者调用 selectProject 时指定的）。这个项目的一次读取把它用上
 * （画上了这条线或第一条）之前一直记着：读的时候点了重新分析、重试，读失败了，地址栏和之后的读取都还用它。
 */
let wanted: { project: string; line: string } | null = null
/**
 * 地址栏里的需求（req: 后面的部分），需求列表还没取到：取到后选上它（找不到时说一声）。和 wanted 一样，取到之前地址栏
 * 一直带着它。
 */
let wantedReq: { project: string; ref: string } | null = null

/** 这个项目要看的功能线：画布上的，没有时是还没画上去的那条 */
const wantedLine = (id: string): string => lineSid.value || (wanted?.project === id ? wanted.line : '')

/** 地址栏里项目后面那一段：看着的需求、还没选上的需求，否则是功能线 */
function hashTail(id: string): string {
  const r = currentReq.value
  if (r) return reqRef(requirements.value, r)
  if (wantedReq?.project === id) return REQ_REF + wantedReq.ref
  return wantedLine(id)
}

/**
 * 要打开的变更记录（还在读）：读好之前地址栏就写上它（刷新页面还是去它那里），读不了时地址栏回到原来的样子。line 是
 * 地址栏里要看的功能线（没有时为空）。
 */
let wantedReview: { project: string; id: number; line: string } | null = null

/** 地址栏上的变更记录：正在读的那条优先，否则画布上的那条；都没有时 null */
function hashReview(id: string): { id: number; line: string } | null {
  if (wantedReview?.project === id) return { id: wantedReview.id, line: wantedReview.line || lineSid.value }
  const r = review.value
  return r?.project === id ? { id: r.id, line: lineSid.value } : null
}

/** push：多一条浏览器历史（从对话框打开变更记录时，"后退"回到现在）；别的时候都是替换 */
function writeHash(push = false) {
  if (!hasWindow) return
  const id = projectId.value
  const rv = id ? hashReview(id) : null
  const h = !id ? '' : rv ? reviewHash(id, rv.id, rv.line) : `#/${encodeURIComponent(id)}/${encodeURIComponent(hashTail(id))}`
  if (location.hash === h) return
  const url = h || location.pathname + location.search
  if (push && typeof history.pushState === 'function') history.pushState(null, '', url)
  else history.replaceState(null, '', url)
}

function readHash(): { project: string; line: string } {
  if (!hasWindow) return { project: '', line: '' }
  const parts = location.hash.replace(/^#\/?/, '').split('/')
  const dec = (s: string) => {
    try {
      return decodeURIComponent(s)
    } catch {
      return ''
    }
  }
  return { project: dec(parts[0] ?? ''), line: dec(parts.slice(1).join('/')) }
}

// ---------------------------------------------------------------------------------------------------------------
// 排版

/**
 * 排版的调度键：项目 + 功能线（不同项目的功能线 sid 可能相同）；看需求时是项目 + 需求；看变更记录时带上记录编号（同一条
 * 线在记录的图和现在的图里是两张画）
 */
const lineKey = () => {
  if (reqMode.value) return `${projectId.value}\u0000req\u0000${reqKey.value}`
  const r = review.value
  return `${projectId.value}\u0000${r ? `review:${r.id}` : ''}\u0000${lineSid.value}`
}

/** 画布上画什么：看需求时是需求设计图里它的那条线，否则是画布上的图（canvasGraph）里选中的功能线；没有可画的时 null */
function canvasSource(): { graph: ProjectGraph; line: string } | null {
  if (reqMode.value) {
    const g = reqGraph.value
    const line = designLineSid(g, currentReq.value)
    return g && line ? { graph: g, line } : null
  }
  const p = canvasGraph.value
  return p && lineSid.value ? { graph: p, line: lineSid.value } : null
}

const layoutRunner = createLayoutRunner<{ view: LineView; layout: Positioned } | null, Anchor>(
  // 照请求那一刻的图、功能线、展开状态建视图（同步），再排版（异步）
  async () => {
    const src = canvasSource()
    if (!src) return null
    const v = buildLineView(src.graph, src.line, viewState.value)
    return { view: v, layout: await layoutLineView(v) }
  },
  ({ result, reason, anchor }) => {
    pendingAnchor = anchor
    view.value = result?.view ?? null
    layout.value = result?.layout ?? null
    layoutReason.value = reason === 'line' && backPending ? 'back' : reason
    if (reason === 'line') backPending = false
    error.value = ''
    layoutTick.value++
  },
  (e) => {
    error.value = `${reqMode.value ? '这条需求设计的流程' : '这条功能线'}排不出来：${errorText(e)}`
  },
)

const relayout = (reason: 'line' | 'state', anchor: Anchor | null = null) => layoutRunner.request(lineKey(), reason, anchor)

/**
 * 改展开状态；old 是被操作的元素（现在画布上的），next 是排好后代表它的元素（按顺序取第一个存在的）；
 * reveal 见 Anchor。
 */
function change(state: ViewState, old: string | null, next: string[], reveal = false) {
  const box = old ? layout.value?.boxes[old] : undefined
  viewState.value = state
  void relayout('state', box ? { oldBox: { x: box.x, y: box.y, width: box.width, height: box.height }, ids: next, reveal } : null)
}

// ---------------------------------------------------------------------------------------------------------------
// 项目列表

/** 列项目的发号：本地改了列表（添加、删除、重新分析回来的新信息）之后，还在路上的旧列表不再采用 */
const listSeq = createSequencer()

/** 从后端重新取项目列表；成功返回 true。连不上时 backend 变成 down。 */
export async function refreshProjects(): Promise<boolean> {
  const n = listSeq.begin()
  try {
    const list = await api.listProjects()
    // 先标成连上了，再换列表：列表变化时排下一次自动刷新（见下面），那时要看到 backend 已经是 ok
    backend.value = 'ok'
    backendError.value = ''
    if (listSeq.isLatest(n)) projects.value = list
    return true
  } catch (e) {
    if (listSeq.isLatest(n)) {
      backend.value = isApiError(e) && e.kind === 'unreachable' ? 'down' : 'error'
      backendError.value = errorText(e)
    }
    return false
  }
}

function upsertProject(info: ProjectInfo) {
  listSeq.begin()
  const i = projects.value.findIndex((p) => p.id === info.id)
  projects.value = i < 0 ? [...projects.value, info] : projects.value.map((p) => (p.id === info.id ? info : p))
}

/** 改列表里一个项目的几项（比如"允许 AI 运行单条功能线的测试"存好之后）；不在列表里时什么都不做 */
export function patchProject(id: string, patch: Partial<ProjectInfo>) {
  const p = projects.value.find((x) => x.id === id)
  if (p) upsertProject({ ...p, ...patch })
}

/** 只更新列表里已有的项目；不在列表里（已经删掉了）时不加回去，返回 false */
function replaceProject(info: ProjectInfo): boolean {
  if (!projects.value.some((p) => p.id === info.id)) return false
  upsertProject(info)
  return true
}

/** 项目已经不在了（删掉了）：从列表里去掉；正在看它时切到剩下的第一个 */
function forgetProject(id: string) {
  graphCache.delete(id)
  listSeq.begin()
  projects.value = projects.value.filter((p) => p.id !== id)
  if (projectId.value !== id) return
  const next = projects.value[0]
  if (next) void selectProject(next.id)
  else noProject()
}

// ---------------------------------------------------------------------------------------------------------------
// 读项目：图（大的有 10 MB，按字节报告进度）和手工项一起读，算一次

/** 最近读过的图（按项目 id；分析 id 和项目列表里最新的一次相同才用） */
const GRAPH_CACHE = 3
const graphCache = new Map<string, { graph: ProjectGraph; analysis: number | null }>()

function rememberGraph(id: string, graph: ProjectGraph, analysis: number | null) {
  graphCache.delete(id)
  graphCache.set(id, { graph, analysis })
  while (graphCache.size > GRAPH_CACHE) graphCache.delete(graphCache.keys().next().value!)
}

interface LoadKey {
  id: string
  /** 要看的功能线（画布上还没有功能线时用；找不到时看第一条） */
  line: string
  /** 重新分析、重试之后：保留现在画布上的功能线和展开状态 */
  keep: boolean
  /** 换上的是别处做的新分析（followNewAnalysis）：换上后提示一声 */
  follow?: boolean
}

interface Loaded {
  graph: ProjectGraph
  analysis: number | null
  overrides: OverrideMap
}

async function loadData(key: LoadKey, isCurrent: () => boolean): Promise<Loaded> {
  progress.value = null
  const wanted = projects.value.find((p) => p.id === key.id)?.analysis?.id ?? null
  const cached = graphCache.get(key.id)
  const graphPromise =
    cached && wanted !== null && cached.analysis === wanted
      ? Promise.resolve(cached)
      : api
          .getGraph(key.id, (received, total) => {
            if (isCurrent()) progress.value = { received, total }
          })
          .then((r) => ({ graph: r.graph, analysis: r.analysisId }))
  const [g, o] = await Promise.all([graphPromise, api.getOverrides(key.id)])
  return { graph: g.graph, analysis: g.analysis, overrides: o }
}

/** 先选 A 再快速选 B，最后停在 B（A 的图、手工项后到也不会盖掉它），"正在读"等 B 读完才消失 */
const loadLatest = createLatestLoader<LoadKey, Loaded>(loadData, {
  setLoading: (b) => (loading.value = b),
  onLoaded: async (key, data) => {
    // 读的时候项目被删掉了（没有发起新的读取）
    if (projectId.value !== key.id) return
    rememberGraph(key.id, data.graph, data.analysis)
    const before = currentDisplay.value
    project.value = data.graph
    graphOf.value = { project: key.id, analysis: data.analysis }
    overrides.value = data.overrides
    loadFailure.value = null
    progress.value = null
    const reviewing = review.value?.project === key.id
    if (key.follow) {
      const name = projects.value.find((p) => p.id === key.id)?.name ?? key.id
      notify(
        'info',
        reviewing
          ? `「${name}」有了新的分析结果（在别的页面或者通过 pystepmap mcp 做的），点"回到现在"看`
          : `已载入「${name}」新的分析结果（在别的页面或者通过 pystepmap mcp 做的），功能线和展开状态照旧`,
        true,
      )
      void checkStatus()
    }
    void syncDiff()
    // 重新分析、换上别处的新分析、重试之后：需求的对照是按最新一次分析算的，跟着重取（第一次读项目时 selectProject 已经在取）
    if (key.keep) void loadRequirements(key.id)
    // 画布上是变更记录当时的图：现在的图在底下换好了，看哪条线、展开状态到"回到现在"时再定（leaveReview）
    if (reviewing) return
    const lines = displayLines.value
    if (key.keep && lineSid.value) {
      if (displayBySid.value.has(lineSid.value)) {
        // 同一条线：展开状态、选中照旧（新图里没有的 id 自然不起作用），视口不动；画布原来是空的（读失败后重试）
        // 就按换线重新定位视口。看需求时画布上不是这条线，不动
        const hadCanvas = view.value !== null
        writeHash()
        if (!reqMode.value) await relayout(hadCanvas ? 'state' : 'line')
        followNewAnalysis()
        return
      }
      const name = before?.titleFull ?? lineSid.value
      if (!reqMode.value) notify('info', `原来看的功能线「${name}」在新的分析结果里没有了，已回到列表第一条`)
      await settleLine(firstLine(lines)?.sid ?? '')
      followNewAnalysis()
      return
    }
    // 画布上还没有功能线（第一次读这个项目，或者第一次读的途中点了重新分析、重试）：看要看的那条
    const want = key.line || (wanted?.project === key.id ? wanted.line : '')
    const target = (want ? lines.find((d) => d.sid === want) : undefined) ?? firstLine(lines)
    await settleLine(target?.sid ?? '')
    followNewAnalysis()
  },
  onError: (key, e) => {
    progress.value = null
    if (projectId.value !== key.id) return
    // 画布上不留旧图：读失败的项目没有可看的东西（功能线和展开状态留着，重试时照旧）
    project.value = null
    graphOf.value = null
    overrides.value = {}
    loadFailure.value = {
      project: key.id,
      kind: isApiError(e) ? e.kind : 'other',
      status: isApiError(e) ? e.status : 0,
      message: errorText(e),
    }
    if (isApiError(e) && e.kind === 'unreachable') backend.value = 'down'
    // 画布上是变更记录当时的图：不动它，"回到现在"时才看到读失败
    if (review.value?.project !== key.id) void relayout('line')
  },
})

/** 清掉画布上的项目（换项目时、项目删光了时） */
function clearCanvas() {
  editing.value = null
  project.value = null
  graphOf.value = null
  overrides.value = {}
  loadFailure.value = null
  progress.value = null
  lineSid.value = ''
  viewState.value = emptyState()
  selection.value = null
  hoverFold.value = null
  search.value = ''
  projectStatus.value = null
  diffResult.value = null
  stopIndex.value = -1
  diffMismatch = ''
  listMode.value = 'lines'
  reqData.value = null
  reqError.value = ''
  reqKey.value = ''
  savedLineState = null
  // 正在看、正在读的变更记录都作废；页签回到看记录之前的
  if (presentView) tab.value = presentView.tab
  presentView = null
  review.value = null
  reviewLoading.value = null
  reviewProgress.value = null
  wantedReview = null
  reviewSeq.begin()
  backPending = false
  // 作废还没排完的旧排版，画布清空
  void relayout('line')
}

/** 换项目；line 是要看的功能线，req 是要看的需求（地址栏里 req: 后面的部分，见 requirements.ts） */
export async function selectProject(id: string, line = '', req = '') {
  if (!projects.value.some((p) => p.id === id)) return
  projectId.value = id
  wanted = line ? { project: id, line } : null
  wantedReq = req ? { project: id, ref: req } : null
  clearCanvas()
  writeHash()
  void checkStatus()
  void loadRequirements(id)
  await loadLatest({ id, line, keep: false })
}

function noProject() {
  projectId.value = ''
  wanted = null
  wantedReq = null
  clearCanvas()
  writeHash()
}

/** 按地址栏选项目和功能线（或需求）；地址栏里的项目不在了就选第一个 */
async function followHash() {
  const h = readHash()
  const list = projects.value
  const byName = list.filter((p) => p.name === h.project)
  const target = list.find((p) => p.id === h.project) ?? (byName.length === 1 ? byName[0] : null) ?? list[0] ?? null
  if (!target) return noProject()
  const rev = parseReviewRef(h.line)
  if (rev) return followReviewHash(target.id, rev)
  // 地址栏不再是变更记录（浏览器的"后退"、手改）：先回到现在，再按地址栏换功能线 / 需求
  if (target.id === projectId.value && (review.value || reviewLoading.value)) await leaveReview()
  const req = parseReqRef(h.line)
  const line = req === null ? h.line : ''
  if (target.id !== projectId.value || (!project.value && !loading.value)) await selectProject(target.id, line, req ?? '')
  else if (req !== null) {
    if (reqData.value?.project !== target.id) {
      if (req) wantedReq = { project: target.id, ref: req } // 需求还在取：取到后看这条
      return
    }
    const r = findReq(requirements.value, req)
    if (r && r !== currentReq.value) await selectRequirement(reqKeyOf(r))
  } else if (line && (line !== lineSid.value || reqMode.value)) {
    if (displayBySid.value.has(line)) await selectLine(line)
    else if (!project.value) wanted = { project: target.id, line } // 图还在读：读完看这条
  }
}

/**
 * 地址栏是一条变更记录（#/<项目>/review:<编号>/<功能线>）：项目还没读（刷新页面、换了项目）时先读现在的图（和记录一起
 * 读），再打开记录；已经在看这条时只换功能线。编号不对、记录已经清掉了（只留最近 10 条）：说一声，回到现在。
 */
async function followReviewHash(id: string, rev: ReviewRef): Promise<void> {
  if (id !== projectId.value || (!project.value && !loading.value)) void selectProject(id, rev.line)
  if (rev.id === null) {
    notify('info', '地址栏里的变更记录编号不对，显示的是现在的图')
    if (review.value || reviewLoading.value) await leaveReview()
    else writeHash()
    return
  }
  const r = review.value
  if (r && r.project === id && r.id === rev.id) {
    if (rev.line && rev.line !== lineSid.value && displayBySid.value.has(rev.line)) await selectLine(rev.line)
    return
  }
  if (reviewLoading.value?.project === id && reviewLoading.value.id === rev.id) return
  await openReview(rev.id, rev.line)
}

let started = false
export async function init() {
  if (started) return
  started = true
  if (await refreshProjects()) await followHash()
  if (!hasWindow) return
  // 手改地址栏、前进后退：跟着换项目 / 功能线（界面自己改地址栏用 replaceState，不触发这里）
  window.addEventListener('hashchange', () => void followHash())
  // 后端没启动时：用户去终端启动后切回浏览器，自动再连一次
  window.addEventListener('focus', () => {
    if (backend.value === 'down' && !loading.value) void retry()
    else onPageVisible()
  })
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') onPageVisible()
    })
  }
}

/** 「重试」：连不上 / 列项目失败时重新连；读项目失败时重新读（保留功能线和展开状态） */
export async function retry() {
  const ok = await refreshProjects()
  if (!ok) return
  const id = projectId.value
  if (!id || !projects.value.some((p) => p.id === id)) return followHash()
  // 后端断了又连上：代码可能在这期间改过
  void checkStatus()
  if (project.value && !loadFailure.value) return
  await loadLatest({ id, line: wantedLine(id), keep: true })
}

// ---------------------------------------------------------------------------------------------------------------
// 添加、重新分析、删除

/** 添加项目（后端分析完才返回）；成功后切到它。失败抛 ApiError（中文信息）。 */
export async function addProject(p: NewProject): Promise<ProjectInfo> {
  const info = await watchSlow(api.addProject(p), slowAfter.ms, (b) => (addingSlow.value = b))
  upsertProject(info)
  backend.value = 'ok'
  void selectProject(info.id)
  return info
}

/**
 * 重新分析：完成后如果还在看这个项目，换上新图，保留功能线和展开状态。结果用提示告诉用户。
 * 分析期间项目被删掉了（别的页面删的；这个页面在分析时不让删）：说"项目已经被删除了"，从列表里去掉，不再加回来。
 */
export async function reanalyze(id = projectId.value) {
  if (!id || reanalyzing.value.includes(id)) return
  const name = projects.value.find((p) => p.id === id)?.name ?? id
  reanalyzing.value = [...reanalyzing.value, id]
  try {
    const info = await watchSlow(api.analyzeProject(id), slowAfter.ms, (b) => {
      slowReanalyzing.value = b ? [...slowReanalyzing.value, id] : slowReanalyzing.value.filter((x) => x !== id)
    })
    if (!replaceProject(info)) return // 等结果的时候在这个页面里删掉了：不加回列表
    graphCache.delete(id)
    const s = info.analysis?.summary
    notify('success', `「${info.name}」分析完成${s ? `：${s.lines} 条功能线，用时 ${s.seconds} 秒` : ''}`)
    if (projectId.value === id) {
      await loadLatest({ id, line: wantedLine(id), keep: true })
      void checkStatus()
    }
  } catch (e) {
    if (isApiError(e) && e.code === 'busy') notify('warning', '这个项目正在分析（可能是在别的页面点的），等它完成后再试')
    else if (isApiError(e) && e.code === 'timeout') notify('error', `重新分析「${name}」没有完成：${e.message}。上一次的分析结果还在`)
    else if (isApiError(e) && (e.code === 'not_found' || e.status === 404)) {
      notify('warning', `「${name}」没有重新分析：项目已经被删除了`)
      forgetProject(id)
    } else if (isApiError(e) && e.kind === 'unreachable') notify('error', '重新分析失败：连不上平台后端')
    else notify('error', `重新分析失败：${errorText(e)}（上一次的分析结果还在）`)
  } finally {
    reanalyzing.value = reanalyzing.value.filter((x) => x !== id)
  }
}

/** 删除项目（只删平台里的记录）。后端说项目已经不在了也当作删掉了。失败抛 ApiError。 */
export async function removeProject(id: string) {
  try {
    await api.deleteProject(id)
  } catch (e) {
    if (!(isApiError(e) && e.status === 404)) throw e
  }
  forgetProject(id)
}

// ---------------------------------------------------------------------------------------------------------------
// 分析状态的自动刷新

/** 有项目正在分析时多久刷新一次项目列表（毫秒；测试里改小） */
export const analyzingPoll = { ms: 2000 }
let pollTimer: ReturnType<typeof setTimeout> | null = null
/** 上一次看到的列表里正在分析的项目 */
let lastAnalyzing = new Set<string>()

/**
 * 列表里有项目正在分析、后端连得上时，过一会儿再取一次列表；取到了列表会变，于是再排下一次，取不到就停。
 * 有项目刚分析完时再多取一次：后端存好新的分析、清理完才去掉"正在分析"，这一次只是保险——看到不在分析了、列表里却
 * 还是旧的那次（比如两次取之间别的进程又存了一次）时也不错过。
 */
function schedulePoll() {
  const now = new Set(projects.value.filter((p) => p.analyzing).map((p) => p.id))
  const justFinished = [...lastAnalyzing].some((id) => !now.has(id))
  lastAnalyzing = now
  if (pollTimer !== null || backend.value === 'down' || (now.size === 0 && !justFinished)) return
  pollTimer = setTimeout(() => {
    pollTimer = null
    void refreshProjects()
  }, analyzingPoll.ms)
}

/** 已经为哪个项目的哪一次分析换过图（同一次分析不重复换） */
let followed = ''

/**
 * 当前项目在列表里有了比画布上更新的一次分析（刷新页面前点的、别的页面点的重新分析完成了）：换上新图，保留功能线
 * 和展开状态。这个页面自己发起的重新分析由 reanalyze 换图，这里不管。
 */
function followNewAnalysis() {
  const p = currentProject.value
  const shown = graphOf.value
  if (!p?.analysis || !shown || shown.project !== p.id || shown.analysis === null) return
  if (p.analysis.id <= shown.analysis || reanalyzing.value.includes(p.id)) return
  const key = `${p.id}\u0000${p.analysis.id}`
  if (followed === key) return
  followed = key
  void loadLatest({ id: p.id, line: wantedLine(p.id), keep: true, follow: true })
}

watch(
  projects,
  () => {
    schedulePoll()
    followNewAnalysis()
  },
  { flush: 'sync' },
)

// ---------------------------------------------------------------------------------------------------------------
// 过期检查、流程差异

/** 窗口回到前台时，距上次检查不到这么久（毫秒）不再查；测试里改小 */
export const statusThrottle = { ms: 3000 }
let lastStatusCheck = -Infinity
const statusSeq = createSequencer()

/**
 * 查当前项目的代码有没有在最新一次分析之后改过（GET /status）。force 为假时（窗口回到前台）距上次不到
 * statusThrottle.ms 不查。查不了（旧版后端 404、出错）时当作没有信息，不提示。
 * 后端说的"正在分析"、最新一次分析和列表里的不一样（别的进程在分析或分析完了）：刷新项目列表——正在分析时列表每 2 秒
 * 刷新，分析完了 followNewAnalysis 换上新图；列表已经是新的、画布上还是旧图时直接换。
 */
async function checkStatus(force = true): Promise<void> {
  const id = projectId.value
  if (!id || backend.value === 'down') return
  const now = Date.now()
  if (!force && now - lastStatusCheck < statusThrottle.ms) return
  lastStatusCheck = now
  const n = statusSeq.begin()
  let s: ProjectStatus | null
  try {
    s = await api.getStatus(id)
  } catch {
    s = null
  }
  if (!statusSeq.isLatest(n) || projectId.value !== id) return
  projectStatus.value = s
  if (!s) return
  const p = projects.value.find((x) => x.id === id)
  const shown = graphOf.value
  if (p && (s.analyzing !== p.analyzing || (s.analysis_id ?? 0) > (p.analysis?.id ?? 0))) void refreshProjects()
  else if (shown?.project === id && hasNewerAnalysis(s, shown.analysis)) followNewAnalysis()
  void syncDiff()
}

/**
 * 浏览器窗口回到前台（visibilitychange 变为可见、窗口重新获得焦点）：查一次过期，3 秒内不重复。还没有选中项目时
 * （"添加第一个项目"的空白页）改为刷新项目列表：别处（pystepmap mcp、另一个页面）添加的项目不用刷新页面就出现、被选上。
 */
export function onPageVisible() {
  if (projectId.value) {
    void checkStatus(false)
    // 需求文件（.pystepmap/requirements、OpenSpec 的 flow 代码块）是在编辑器、AI 那边写的：回来时重取一次。只在打开项目、
    // 重新分析时取的话，页面开着之后才写的第一条需求不会出现——那时列表上还没有"需求"这个切换，也就没有"刷新"可点
    const now = Date.now()
    if (now - lastReqRefresh >= statusThrottle.ms && !review.value) {
      lastReqRefresh = now
      void loadRequirements()
    }
  } else void refreshWhenEmpty()
}
let lastReqRefresh = 0

async function refreshWhenEmpty(): Promise<void> {
  if (backend.value === 'down') return
  const now = Date.now()
  if (now - lastStatusCheck < statusThrottle.ms) return
  lastStatusCheck = now
  if ((await refreshProjects()) && !projectId.value && projects.value.length) await followHash()
}

const diffSeq = createSequencer()
/** 已经为哪一对（图的分析 id，差异的 head id）重取过：同一对只重取一次，不来回 */
let diffMismatch = ''
/** 正在取的差异（项目 + 图的分析 id）：换图后取一次、换图后的检查又想取一次时，不重复发 */
let diffInFlight = ''

/**
 * 画布上这张图的那次分析有流程差异时取 /diff（已经取到的不重取）。有没有差异看检查结果或项目列表里的 has_diff（取
 * 和画布上同一次分析的那个；两边都对不上时先不动）。取到的差异和图不是同一次分析（中间别的进程又分析了一次）：
 * 刷新列表，followNewAnalysis 按新的那次重取图，图换好后差异就对上了；差异反而旧的，再取一次差异。
 */
async function syncDiff(): Promise<void> {
  const shown = graphOf.value
  if (!shown || shown.analysis === null) return
  const id = shown.project
  const s = projectId.value === id ? projectStatus.value : null
  const info = projects.value.find((p) => p.id === id)?.analysis
  const has = s && s.analysis_id === shown.analysis ? s.has_diff : info && info.id === shown.analysis ? info.has_diff : null
  if (has === null) return
  const have = diffResult.value
  if (!has) {
    if (have?.project === id && have.diff) diffResult.value = null
    return
  }
  if (have && have.project === id && have.head.id === shown.analysis) return
  const flight = `${id}\u0000${shown.analysis}`
  if (diffInFlight === flight) return
  diffInFlight = flight
  const n = diffSeq.begin()
  let r: DiffResult
  try {
    r = await api.getDiff(id)
  } catch {
    // 旧版后端（404）或者出错：当作没有差异信息，不提示
    if (diffSeq.isLatest(n) && graphOf.value?.project === id) diffResult.value = null
    return
  } finally {
    if (diffInFlight === flight) diffInFlight = ''
  }
  const cur = graphOf.value
  if (!diffSeq.isLatest(n) || cur?.project !== id) return
  diffResult.value = { ...r, project: id }
  if (r.head.id === cur.analysis) return
  const pair = `${id}:${cur.analysis}:${r.head.id}`
  if (diffMismatch === pair) return
  diffMismatch = pair
  if (cur.analysis !== null && r.head.id > cur.analysis) {
    void refreshProjects()
    return
  }
  diffResult.value = null
  void syncDiff()
}

export interface AckTarget {
  project: string
  analysis: number
}

/** "标为已看"的确认框打开时，记下用户看的是哪个项目的哪次分析（画布上没有图时为 null） */
export function ackTarget(): AckTarget | null {
  const shown = graphOf.value
  return shown && shown.analysis !== null ? { project: shown.project, analysis: shown.analysis } : null
}

/**
 * "标为已看"：带上用户看的那次分析（确认框打开时用 ackTarget 记下的；不给时是画布上现在的）。确认框开着的时候页面
 * 换上了新的分析、或者换了项目：不标，说一声。之后又有了新的分析时后端回 409 moved：提示先看新的，重新载入。成功后
 * 差异清空、所有变化标记消失，列表里这次分析也记成没有差异（结果回来时已经换了项目，也照样更新那个项目的列表项）。
 */
export async function ackChanges(target: AckTarget | null = ackTarget()): Promise<void> {
  const shown = graphOf.value
  if (!target || !shown) return
  // 看变更记录时画布上不是现在的变化（界面上也没有"标为已看"）
  if (review.value) return notify('info', '正在看变更记录：先点"回到现在"，再标为已看')
  if (shown.project !== target.project) return notify('info', '已经换了项目，没有标为看过')
  if (shown.analysis !== target.analysis) return notify('warning', '分析结果已经更新，请先看新的变化')
  const id = target.project
  if (ackingFor.value.includes(id)) return
  ackingFor.value = [...ackingFor.value, id]
  try {
    const r = await api.ackDiff(id, target.analysis)
    const p = projects.value.find((x) => x.id === id)
    if (p?.analysis?.id === r.head.id) replaceProject({ ...p, analysis: r.head })
    if (graphOf.value?.project !== id) return
    diffResult.value = { ...r, project: id }
    stopIndex.value = -1
    const s = projectStatus.value
    if (s && s.analysis_id === r.head.id) projectStatus.value = { ...s, base_id: null, has_diff: false }
    notify('success', '已标为看过，变化标记都去掉了')
  } catch (e) {
    if (isApiError(e) && e.code === 'moved') {
      notify('warning', '分析结果已经更新，请先看新的变化')
      await refreshProjects()
      void checkStatus()
    } else if (isApiError(e) && e.code === 'busy') notify('warning', e.message || '正在分析，等分析完再标为看过')
    else notify('error', `没有标为看过：${errorText(e)}`)
  } finally {
    ackingFor.value = ackingFor.value.filter((x) => x !== id)
  }
}

// 画布上的差异或图换了（标为已看、换上新图、换项目、打开 / 离开变更记录）："第几处"回到没定位。看变更记录时现在的
// 差异、图在底下换了不算
watch([diffView, canvasGraph], () => (stopIndex.value = -1), { flush: 'sync' })

/** 定位到当前功能线的第 index 处变化（循环）：需要时展开卡片、分支、钻入，排好后选中它、移到画布中间 */
export async function gotoStop(index: number) {
  const stops = changeStops.value
  if (!canvasGraph.value || !lineSid.value || stops.length === 0) return
  const i = ((index % stops.length) + stops.length) % stops.length
  stopIndex.value = i
  await revealElement(stops[i], true)
}

/**
 * 在当前功能线上露出一个节点或卡片（stop 说它在哪个任务卡片里、经哪些步骤钻入，见 diffview 的 revealStop）：需要时
 * 展开卡片、分支、钻入，排好后移到画布中间；selectIt 时同时选中它（详情栏显示）。差异栏和运行路径页签共用。
 */
export async function revealElement(stop: ChangeStop, selectIt = false): Promise<void> {
  const src = canvasSource()
  if (!src) return
  const key = lineKey()
  const r = revealStop(src.graph, src.line, viewState.value, stop)
  if (!r) return
  if (r.state !== viewState.value) {
    viewState.value = r.state
    await relayout('state')
    if (lineKey() !== key || canvasSource()?.graph !== src.graph) return
  }
  if (selectIt) select(r.kind, r.target)
  focusElement(r.target)
}

/** "上一处"（-1）/"下一处"（1）；还没定位过时下一处是第一处、上一处是最后一处 */
export function nextStop(delta: 1 | -1): Promise<void> {
  const i = stopIndex.value
  return gotoStop(i < 0 ? (delta > 0 ? 0 : -1) : i + delta)
}

// ---------------------------------------------------------------------------------------------------------------
// 分析配置（ConfigDialog.vue）

export function openConfig(id = projectId.value) {
  if (id) configFor.value = id
}

/** 读项目的分析配置（{} = 全用默认值）。失败抛 ApiError。 */
export function readConfig(id: string): Promise<Record<string, unknown>> {
  return api.getConfig(id)
}

/** 改项目的分析配置：后端校验（不合法时抛 ApiError，400，中文说明），不自动重新分析。返回改后的配置。 */
export function writeConfig(id: string, config: Record<string, unknown>): Promise<Record<string, unknown>> {
  return api.putConfig(id, config)
}

// ---------------------------------------------------------------------------------------------------------------
// 手工标题和分组

export function openEditor(sid: string, x: number, y: number) {
  editing.value = { sid, x, y }
}

export function closeEditor() {
  editing.value = null
}

/** 每条手工项最后一次保存的号：同一条连着存两次，先发后到的那次不采用 */
const overrideSaves = new Map<string, number>()
let overrideSeq = 0

/**
 * 保存一条功能线的手工标题和分组（整条改写；两项都是 null = 恢复原样）。存到的是发起时画布上的那个项目；
 * 回来时已经换了项目就不动界面。失败抛 ApiError。
 */
export async function saveOverride(sid: string, value: { title: string | null; group: string | null }): Promise<void> {
  const id = graphOf.value?.project
  if (!id) throw new Error('项目还没读完')
  const key = `${id}\u0000${sid}`
  const n = ++overrideSeq
  overrideSaves.set(key, n)
  const server = await api.putOverride(id, sid, value)
  if (overrideSaves.get(key) !== n || graphOf.value?.project !== id) return
  overrides.value = patchOverride(overrides.value, sid, server)
}

// ---------------------------------------------------------------------------------------------------------------
// 需求（只读：界面上不改需求文件）

const reqSeq = createSequencer()

/**
 * 取项目的需求（后端每次现读需求文件，改了需求不用重新分析）。旧版后端没有这个接口：当作没有需求（列表上不出现
 * "需求"，看着需求时回到功能线），不提示。别的错（还没有分析结果、连不上）：记在 reqError，已经取到的照旧。
 */
export async function loadRequirements(id = projectId.value): Promise<void> {
  if (!id || backend.value === 'down') return
  const n = reqSeq.begin()
  reqLoading.value = true
  try {
    const r = await api.getRequirements(id)
    if (!reqSeq.isLatest(n) || projectId.value !== id) return
    const prev = currentReq.value
    reqData.value = { ...r, project: id }
    reqError.value = ''
    afterRequirements(prev)
  } catch (e) {
    if (!reqSeq.isLatest(n) || projectId.value !== id) return
    if (isMissingEndpoint(e)) {
      reqData.value = null
      reqError.value = ''
      if (wantedReq?.project === id) {
        wantedReq = null
        writeHash()
      }
      if (reqMode.value) void showLines()
    } else reqError.value = `需求没有刷新：${errorText(e)}`
  } finally {
    if (reqSeq.isLatest(n)) reqLoading.value = false
  }
}

/** 取到需求之后：选上地址栏里的那条；看着需求时，那条不在了换到第一条，需求都没了回到功能线；还在就按新的对照重画 */
function afterRequirements(prev: Requirement | null) {
  const list = requirements.value
  if (wantedReq && wantedReq.project === projectId.value) {
    const ref = wantedReq.ref
    wantedReq = null
    const r = findReq(list, ref)
    if (r) {
      void selectRequirement(reqKeyOf(r))
      return
    }
    notify('info', `地址栏里的需求「${ref}」在项目里找不到了`)
    writeHash()
  }
  if (!reqMode.value) return
  if (list.length === 0) {
    notify('info', '项目里已经没有需求了，回到功能线')
    void showLines()
    return
  }
  const cur = list.find((r) => reqKeyOf(r) === reqKey.value)
  if (!cur) {
    notify('info', `原来看的需求${prev ? `「${prev.title}」` : ''}在项目里找不到了，已回到列表第一条`)
    void selectRequirement(firstReqKey(list) ?? reqKeyOf(list[0]))
    return
  }
  writeHash()
  // 同一条需求：设计的流程没改时展开状态、视口照旧（差异标记跟着新的对照结果变）；改了按默认重新画
  if (prev && prev.flow === cur.flow && prev.design_line === cur.design_line) void relayout('state')
  else {
    selection.value = null
    viewState.value = designState()
    void relayout('line')
  }
}

/** 需求设计图的默认展开状态：全部展开 */
function designState(): ViewState {
  const src = reqMode.value ? canvasSource() : null
  if (!src) return emptyState()
  try {
    return expandAllState(src.graph, src.line)
  } catch {
    return emptyState()
  }
}

/** 选一条需求（key 是 reqKeyOf：需求文件，同一个文件里的第二块起带 #n）：左边列表换成需求，画布上画它设计的流程，功能线的展开状态记下来 */
export async function selectRequirement(key: string): Promise<void> {
  if (!requirements.value.some((r) => reqKeyOf(r) === key)) return
  if (!reqMode.value) {
    savedLineState = view.value ? viewState.value : null
    listMode.value = 'requirements'
    search.value = '' // 搜的是功能线，换到需求列表时不带过去
  }
  wantedReq = null
  // 需求的设计图只在"流程设计"里看（运行路径的标注、流程差异都不是这张图的）
  tab.value = 'design'
  reqKey.value = key
  selection.value = null
  hoverFold.value = null
  editing.value = null
  viewState.value = designState()
  writeHash()
  await relayout('line')
}

/** 列表上方切到"需求"：回到上次看的那条，没有时第一条 */
export function showRequirements(): Promise<void> {
  const list = requirements.value
  const key = list.some((x) => reqKeyOf(x) === reqKey.value) ? reqKey.value : firstReqKey(list)
  return key ? selectRequirement(key) : Promise.resolve()
}

/** 列表上方切到"功能线"：画布回到原来的功能线，展开状态照旧 */
export async function showLines(): Promise<void> {
  if (!reqMode.value) return
  listMode.value = 'lines'
  search.value = ''
  viewState.value = savedLineState ?? emptyState()
  savedLineState = null
  selection.value = null
  hoverFold.value = null
  writeHash()
  await relayout('line')
}

/**
 * 读完图后定下要看的功能线。看着需求时画布上是设计图：只记下这条线（展开状态按默认），回到功能线时再画。
 */
async function settleLine(sid: string): Promise<void> {
  if (!reqMode.value) return showLine(sid, true)
  if (lineSid.value !== sid) savedLineState = null
  lineSid.value = sid
  wanted = null
  writeHash()
}

/** 需求对上的功能线（画布上方"功能线：…"）：换到它 */
export async function openReqLine(): Promise<void> {
  const sid = currentReq.value?.comparison?.line_sid
  if (sid && displayBySid.value.has(sid)) await selectLine(sid)
}

/** 差异里"实现里多了"的节点：换到实现的功能线，露出这个节点（需要时展开分支）、选中、移到画布中间 */
export async function gotoImplNode(line: string, sid: string): Promise<void> {
  if (!displayBySid.value.has(line)) return
  await selectLine(line)
  const key = lineRootKey(project.value, line)
  const stop = key ? nodeStop(project.value, key, sid) : null
  if (stop) await revealElement(stop, true)
}

/** 在设计图上露出一个节点（设计图里的 sid）、选中、移到画布中间 */
export async function focusDesignNode(sid: string): Promise<void> {
  const r = currentReq.value
  if (!r?.design_line) return
  const stop = nodeStop(reqGraph.value, r.design_line, sid)
  if (stop) await revealElement(stop, true)
}

// ---------------------------------------------------------------------------------------------------------------
// 变更记录（只读回看，reviewview.ts）

const reviewsSeq = createSequencer()

/**
 * 取项目的变更记录列表（顶栏的"变更记录"、横幅的"上一次 / 下一次"）。旧版后端没有这个接口时是空的（入口不出现）；
 * 别的错：换了项目时是空的，同一个项目照旧用已经取到的。
 */
export async function loadReviews(id = projectId.value): Promise<void> {
  const n = reviewsSeq.begin()
  if (!id) {
    reviewsOf.value = null
    return
  }
  try {
    const list = await api.listReviews(id)
    if (reviewsSeq.isLatest(n) && projectId.value === id) reviewsOf.value = { project: id, list }
  } catch {
    if (reviewsSeq.isLatest(n) && projectId.value === id && reviewsOf.value?.project !== id) reviewsOf.value = { project: id, list: [] }
  }
}

// 打开项目、标为已看之后（差异的结果换了）、换上新的分析之后：重新取记录列表
watch([projectId, diffResult, () => graphOf.value?.analysis], () => void loadReviews())

/** 看变更记录之前画布上的样子："回到现在"时照原样放回 */
interface PresentView {
  lineSid: string
  viewState: ViewState
  selection: Selection | null
  tab: 'design' | 'run'
  listMode: 'lines' | 'requirements'
  /** 那时画布上画着东西（回到现在、还是原来那条线时视口也照旧，FlowCanvas.vue 记着） */
  drawn: boolean
  /** 差异栏定位到了第几处；那时的图和差异（回到现在时都没换才照旧定位在那一处） */
  stopIndex: number
  graph: ProjectGraph | null
  diff: DiffResult | null
}
let presentView: PresentView | null = null
const reviewSeq = createSequencer()

/**
 * 打开记录之前先对一下记录列表（还没取到、列表里没有它时现取一次，可能是别处刚存的；取到的顺便换上）：已经清掉了的、
 * 没存图的不去取明细和图（不白发请求，浏览器也不报 404），直接抛出和后端回答一样的错。列表是空的（旧版后端、项目还
 * 没有记录）、取不到时不知道，照常去取，由后端回答。
 */
async function checkListed(id: string, rid: number, isCurrent: () => boolean): Promise<void> {
  let list = reviewsOf.value?.project === id ? reviewsOf.value.list : null
  if (!list || !list.some((r) => r.id === rid)) {
    try {
      list = await api.listReviews(id)
    } catch {
      return
    }
    if (!isCurrent()) return
    reviewsSeq.begin() // 还在路上的旧列表不再采用
    reviewsOf.value = { project: id, list }
  }
  if (!list.length) return
  const found = list.find((r) => r.id === rid)
  const gone = (error: string, code: string | null = null) => new ApiError('http', error, { status: 404, code, data: { error } })
  if (!found) throw gone(`没有这条变更记录：${rid}（只留最近 10 条，可能已经清掉了）`)
  if (found.has_graph === false) throw gone('这条变更记录没有存当时的图（旧版本存的记录），只能看文字明细', 'no_graph')
}

/**
 * 在画布上打开一条变更记录：存下的那次差异和当时的图一起读，读好后画布换成那时的图、标出那次的变化（和现在的差异
 * 同一套标记、"上一处 / 下一处"、"删除了什么"）。从现在进来时记下画布上的样子（功能线、展开状态、选中、页签、看的是
 * 功能线还是需求），"回到现在"时放回；页签换到"流程设计"，左边列表换到功能线。line 是要看的功能线（地址栏里的；
 * 没有时按 reviewLine 挑：正在看的线这次有变化就留在它上面，否则第一条有变化的）。push：地址栏多一条历史（从对话框
 * 打开时，浏览器的"后退"回到现在）。读不了（记录已经清掉了、没有存图、旧版后端、旧版引擎的图）时说一声，停在原来
 * 的样子（地址栏也回去）。快速连点时只认最后一次。
 */
export async function openReview(rid: number, line = '', push = false): Promise<void> {
  const id = projectId.value
  if (!id) return
  const n = reviewSeq.begin()
  wantedReview = { project: id, id: rid, line }
  reviewLoading.value = { project: id, id: rid }
  reviewProgress.value = null
  writeHash(push)
  const isCurrent = () => reviewSeq.isLatest(n) && projectId.value === id
  let detail: ReviewDetail
  let graph: ProjectGraph
  try {
    await checkListed(id, rid, isCurrent)
    if (!isCurrent()) return
    const [d, g] = await Promise.all([
      api.getReview(id, rid),
      api.getReviewGraph(id, rid, (received, total) => {
        if (isCurrent()) reviewProgress.value = { received, total }
      }),
    ])
    detail = d
    graph = g.graph
  } catch (e) {
    if (!isCurrent()) return
    wantedReview = null
    reviewLoading.value = null
    reviewProgress.value = null
    notify(review.value ? 'warning' : 'info', reviewFailText(rid, e, review.value !== null))
    writeHash()
    return
  }
  if (!isCurrent()) return
  wantedReview = null
  reviewLoading.value = null
  reviewProgress.value = null
  if (!review.value) {
    presentView = {
      lineSid: lineSid.value,
      viewState: viewState.value,
      selection: selection.value,
      tab: tab.value,
      listMode: listMode.value,
      drawn: view.value !== null,
      stopIndex: stopIndex.value,
      graph: project.value,
      diff: diffResult.value,
    }
  }
  const current = lineSid.value
  editing.value = null
  hoverFold.value = null
  selection.value = null
  tab.value = 'design'
  // 搜的是需求，换到功能线时不带过去（和 showLines 一样）
  if (listMode.value === 'requirements') search.value = ''
  listMode.value = 'lines'
  review.value = { project: id, id: rid, info: detail, graph, diff: detail.diff }
  lineSid.value = reviewLine(displayLines.value, diffView.value.lineStatus, line, current)
  wanted = null
  viewState.value = emptyState()
  stopIndex.value = -1
  writeHash()
  await relayout('line')
}

/** 横幅上的"上一次"（更早标为已看的那条）/"下一次"（更晚的）；只在存着图的记录之间走 */
export function stepReview(dir: 'earlier' | 'later'): Promise<void> {
  const r = review.value
  if (!r || reviewLoading.value) return Promise.resolve()
  const nb = reviewNeighbors(reviews.value, r.id)
  const to = dir === 'earlier' ? nb.earlier : nb.later
  return to ? openReview(to.id) : Promise.resolve()
}

/**
 * "回到现在"：画布换回项目现在的图和差异（看记录期间重新分析过的，就是新的那次），功能线、展开状态、选中、页签、看的
 * 是功能线还是需求都照看记录之前的样子；那条线在现在的图里没有了时回到列表第一条。还在读的记录作废。现在的图还没
 * 读好（地址栏直接打开的记录）时，读好后按记录里看着的那条线画。
 */
export async function leaveReview(): Promise<void> {
  reviewSeq.begin()
  const wasLoading = reviewLoading.value !== null || wantedReview !== null
  wantedReview = null
  reviewLoading.value = null
  reviewProgress.value = null
  const r = review.value
  if (!r) {
    if (wasLoading) writeHash()
    return
  }
  const saved = presentView
  presentView = null
  const shownLine = lineSid.value
  review.value = null
  editing.value = null
  hoverFold.value = null
  stopIndex.value = -1
  tab.value = saved?.tab ?? 'design'
  const want = saved?.lineSid || shownLine
  if (!project.value) {
    // 现在的图还没读好、或者读失败了：读好后看这条（读失败时画布上是失败的说明）
    listMode.value = 'lines'
    lineSid.value = want
    wanted = want ? { project: r.project, line: want } : null
    viewState.value = saved?.lineSid && saved.listMode === 'lines' ? saved.viewState : emptyState()
    selection.value = null
    writeHash()
    await relayout('line')
    return
  }
  if (saved?.listMode === 'requirements' && requirements.value.some((q) => reqKeyOf(q) === reqKey.value)) {
    // 看记录之前在看需求：设计图、展开状态照旧（功能线的展开状态还在 savedLineState 里）
    listMode.value = 'requirements'
    if (displayBySid.value.has(saved.lineSid)) lineSid.value = saved.lineSid
    else {
      // 那条功能线在现在的图里没有了（看记录期间重新分析过）：回到功能线时看第一条
      lineSid.value = firstLine(displayLines.value)?.sid ?? ''
      savedLineState = null
    }
    viewState.value = saved.viewState
    selection.value = saved.selection
    backPending = saved.drawn
  } else {
    listMode.value = 'lines'
    const lineState = saved?.listMode === 'requirements' ? savedLineState : (saved?.viewState ?? null)
    savedLineState = null
    if (want && displayBySid.value.has(want)) {
      const same = !!saved && saved.lineSid === want
      lineSid.value = want
      viewState.value = (same && lineState) || emptyState()
      selection.value = same && saved.listMode === 'lines' ? saved.selection : null
      backPending = same && saved.drawn && saved.listMode === 'lines'
      // 现在的图和差异都没换（看记录期间没有重新分析、没有标为已看）：差异栏照旧定位在那一处
      if (same && saved.listMode === 'lines' && saved.graph === project.value && saved.diff === diffResult.value) stopIndex.value = saved.stopIndex
    } else {
      if (saved?.lineSid) notify('info', '原来看的功能线在现在的分析结果里没有了，已回到列表第一条')
      lineSid.value = firstLine(displayLines.value)?.sid ?? ''
      viewState.value = emptyState()
      selection.value = null
    }
  }
  wanted = null
  writeHash()
  await relayout('line')
  followNewAnalysis()
  void checkStatus(false)
}

// ---------------------------------------------------------------------------------------------------------------
// 画布上的动作

/** 选功能线（列表、地址栏、需求里的"功能线：…"链接）；看着需求时回到功能线列表 */
export function selectLine(sid: string): Promise<void> {
  return showLine(sid, false)
}

/** keepWantedReq：读完图自动选的线（settleLine），地址栏里还没取到的需求照旧等着 */
async function showLine(sid: string, keepWantedReq: boolean) {
  // 从需求回到看需求之前的那条线（"功能线：…"链接）：展开状态照旧
  const restore = reqMode.value && sid === lineSid.value ? savedLineState : null
  if (reqMode.value) {
    listMode.value = 'lines'
    savedLineState = null
    search.value = ''
  }
  lineSid.value = sid
  wanted = null
  if (!keepWantedReq) wantedReq = null
  stopIndex.value = -1
  viewState.value = restore ?? emptyState()
  selection.value = null
  hoverFold.value = null
  writeHash()
  await relayout('line')
}

export function select(kind: SelectionKind, id: string) {
  selection.value = { kind, id }
}

export function clearSelection() {
  selection.value = null
}

export function toggleCard(card: CardView) {
  if (card.kind === 'drill') return undrill(card)
  if (!card.expandable) return
  change(setOpen(viewState.value, 'cards', card.id, !card.expanded), card.id, [card.id], true)
}

export function openFold(fold: FoldView) {
  hoverFold.value = null
  change(setOpen(viewState.value, 'folds', fold.id, true), fold.id, [fold.members[0], fold.anchor])
}

export function closeFold(fold: FoldView) {
  hoverFold.value = null
  const old = [fold.members[0], fold.anchor].find((id) => layout.value?.boxes[id]) ?? null
  if (selection.value && fold.members.includes(selection.value.id)) selection.value = { kind: 'fold', id: fold.id }
  change(setOpen(viewState.value, 'folds', fold.id, false), old, [fold.id], true)
}

/** 代替步骤 nodeId 的钻入链（按链上的顺序）；没钻入时为空 */
const chainOfStep = (nodeId: string): CardView[] => (view.value ? drillChainOf(view.value, nodeId) : [])

/**
 * 钻入步骤 nodeId 调用的一个（或几个）函数：这个步骤已经钻入着的别的函数照旧开着，同一个步骤的几张钻入卡片排成钻入链。
 * 选中的是这个步骤（或它的钻入链上的卡片）时改选新开的第一张。from：在哪张卡片上点的（钻入链上卡片的"再展开"），
 * 新开的卡片排好后放在它原来在屏幕上的位置；没有时按步骤（已经换成钻入链时按链的第一张）。
 */
export function drill(nodeId: string, option: DrillOption | DrillOption[], from: string | null = null) {
  const opts = Array.isArray(option) ? option : [option]
  if (!opts.length) return
  const chain = chainOfStep(nodeId)
  const sel = selection.value?.id
  if (sel === nodeId || chain.some((c) => c.id === sel)) selection.value = { kind: 'card', id: opts[0].id }
  let s = viewState.value
  for (const o of opts) s = setOpen(s, 'drills', o.id, true)
  const old = from ?? (layout.value?.boxes[nodeId] ? nodeId : (chain[0]?.id ?? nodeId))
  change(s, old, [opts[0].id])
}

/**
 * 收回一张钻入卡片：只收这一张，同一个步骤钻入链上的别的照旧；最后一张收回后变回原来的步骤。选中的是它（或它里面的
 * 东西）时改选链上紧挨着的一张（后面的，没有就前面的），链上没别的了改选那个步骤。
 */
export function undrill(card: CardView) {
  const step = card.anchor?.id ?? null
  const chain = step ? chainOfStep(step) : []
  const i = chain.findIndex((c) => c.id === card.id)
  const near = i < 0 ? null : (chain[i + 1] ?? chain[i - 1] ?? null)
  const back = near?.id ?? step
  const sel = selection.value?.id
  if (back && sel && (sel === card.id || sel.startsWith(`${card.id}/`))) {
    selection.value = near ? { kind: 'card', id: near.id } : { kind: 'node', id: back }
  }
  change(setOpen(viewState.value, 'drills', card.id, false), card.id, back ? [back] : [], true)
}

function rootCardId(): string | null {
  return view.value?.cards[0]?.id ?? null
}

/**
 * 选了一个测试之后：把它走过的地方都展开（执行过的卡片、有走过节点的折叠分支、调用了执行过的函数的步骤钻进去），
 * 不收起任何东西。需求的设计图、变更记录的图上不做。钻入太多没钻完时说一声。
 */
export async function revealWalked(overlay: RunOverlay): Promise<void> {
  const src = canvasSource()
  if (!src || reqMode.value) return
  const r = revealWalkedState(src.graph, src.line, overlay, viewState.value)
  if (!r.opened) return
  viewState.value = r.state
  await relayout('state') // 等排好：紧接着的定位（失败的停点）要按展开之后的图来
  if (r.more) notify('info', `这个测试走过的函数很多，自动钻入了前 ${MAX_AUTO_DRILLS} 个；其余的点步骤左边的图标、钻入卡片上的"再展开"钻入`)
}

export function expandAll() {
  const src = canvasSource()
  if (!src) return
  const root = rootCardId()
  // 卡片、折叠的分支都展开，能钻入的步骤也都钻进去（一个步骤调用了几个函数时都钻，排成钻入链）
  const r = expandEverythingState(src.graph, src.line, viewState.value)
  change(r.state, root, root ? [root] : [])
  if (r.more) notify('info', `能钻入的函数很多，这次钻入了前 ${MAX_EXPAND_DRILLS} 个；其余的点步骤左边的图标、钻入卡片上的"再展开"钻入`)
}

/** 恢复默认：功能线按折叠规则；需求的设计图默认全部展开（设计的每条路、每个结局都要看得见） */
export function resetDefault() {
  const root = rootCardId()
  change(reqMode.value ? designState() : emptyState(), root, root ? [root] : [])
}

let flashTimer: ReturnType<typeof setTimeout> | undefined
export function gotoCard(target: string) {
  select('card', target)
  canvasCommand.value = { seq: ++commandSeq, type: 'focus', id: target }
  flash.value = target
  clearTimeout(flashTimer)
  flashTimer = setTimeout(() => (flash.value = null), 2000)
}

export function focusElement(id: string) {
  canvasCommand.value = { seq: ++commandSeq, type: 'focus', id }
}

/** 元素不在屏幕上时把画布平移过去（不改缩放）；已经看得见时不动 */
export function peekElement(id: string) {
  canvasCommand.value = { seq: ++commandSeq, type: 'peek', id }
}

export function fitCanvas() {
  canvasCommand.value = { seq: ++commandSeq, type: 'fit' }
}
