// "运行路径"页签的状态和动作：测试命令、运行列表、正在跑的那次（每隔一会儿查一次，跑完自动选中新的运行）、选中的
// 运行 / 测试、这个测试的运行路径（只取这一个测试：GET /runs/{id}/paths?test=，真实项目整份有几十 MB）、叠加层、没走到
// 的结局、确认运行的两步、取消。画布上要画的东西由 runview.ts 的纯函数算（canvasRunMarks），改了不重新排版。
// 运行详情按功能线取（?line=：每个测试带 on_line，走过这条线上几个函数，后端按最新一次分析的图算）。
// 换项目时作废还在路上的响应（gen）；同一类请求只认最后一次（发号器）。旧版后端（接口 404"没有这个接口"）：页签说
// 不支持，不提示、不轮询，只在重新进页签、窗口回到前台时各再探一次（用户可能重启了 pystepmap serve）。正在跑时查运行
// 列表出错不放弃：退避着重试（2、4、8 秒，之后每 15 秒），这期间说状态不明、不再自己走秒。
// 运行中（测试命令在跑，后端给了 progress）：每次查运行列表都带上画布上的功能线（?live_line=），换线时马上再查一次；
// 画布上是运行中的样子（runview 的 liveRunMarks：走到过的节点亮，别的暗），选了测试时照旧画那个测试；侧栏的功能线带
// 小标记（liveLines、liveFresh）。跑完（running 为 null）、取消、换项目时都没了，换成正常的结果。
// 两种运行：全部测试（顶栏的"运行全部测试"，startRun）和只跑一条功能线的测试（面板的"运行这条线的测试"，startLineRun：
// 名单取自最近一次全量运行，GET /line-tests 先问一遍有几个、挑不出来时的原因）。运行列表里每次运行带 scope（全量为 null）；
// 这条线的面板默认选最新的全量运行或只跑了这条线的运行，只跑了别的线的运行列出来但不给这条线的测试路径、没走到的结局
// （不取 /uncovered：后端会回 409 scoped）。正在跑的是哪种记在 runningScope 里：这个页面开始的自己记，别处开始的按后端 running.scope 说的（旧版后端不说），别处
// 开始的（另一个页面、AI 经 MCP）不知道，只说阶段。项目一打开就读测试命令和运行列表（顶栏的"运行全部测试"要用）。
// 回放（replay.ts）：选中的测试按记录一步步点亮，每步 replaySpeed.ms；换测试、换运行、换线、换页签、画布展开状态变了都停。

import { computed, ref, shallowRef, watch } from 'vue'
import {
  api,
  errorText,
  isApiError,
  isMissingEndpoint,
  type Interpreter,
  type LineTestsResult,
  type LiveProgress,
  type RunDetail,
  type RunFailure,
  type RunInfo,
  type RunningInfo,
  type RunPathsJson,
  type RunScope,
  type RunsResult,
  type TestCommand,
  type TestRow,
  type UncoveredJson,
  type UncoveredResult,
} from './api'
import type { LineView } from './graph'
import { createSequencer } from './latest'
import { REPLAY_REDUCED_TIP, replayMarks, replayPlan, type ReplayPlan } from './replay'
import { failureStop, type FailureStop } from './runfailure'
import {
  buildRunOverlay,
  defaultRunFor,
  emptyRunMarks,
  findOnGraph,
  isFailedOutcome,
  liveRunMarks,
  runFitsLine,
  runMarks,
  runResultText,
  staleRunNote,
  testsForLine,
  uncoveredMarks,
  type LaunchScope,
  type RingAt,
  type RunMarks,
  type RunOverlay,
} from './runview'
import {
  analyzingPoll,
  backend,
  currentProject,
  displayBySid,
  graphOf,
  lineSid,
  notify,
  patchProject,
  peekElement,
  project,
  projectId,
  refreshProjects,
  revealElement,
  selectLine,
  tab,
  view,
  revealWalked,
} from './state'

/** 全部测试 */
const FULL: LaunchScope = { line: null, title: '', tests: 0 }

/** 后端支不支持测试运行：unknown 还没问；ok；unsupported 旧版后端（接口 404）；error 问的时候出错了（可以重试） */
export const runSupport = ref<'unknown' | 'ok' | 'unsupported' | 'error'>('unknown')
export const runsError = ref('')
export const testCommand = shallowRef<TestCommand | null>(null)
/** 最近的几次运行，新的在前 */
export const runs = shallowRef<RunInfo[]>([])
/** 正在跑的那次；没有时 null */
export const running = shallowRef<RunningInfo | null>(null)
/** 收到 running 的时刻（本机毫秒）：界面在两次查询之间自己走秒 */
export const runningAt = ref(0)
/** 正在跑，但最近一次查运行列表出错了（正在退避重试）：不知道现在怎样，界面不走秒 */
export const runningUnknown = ref(false)
/** 202 之后、存下运行之前就失败了的原因（后端进程内存里的） */
export const lastFailure = shallowRef<RunFailure | null>(null)
export const starting = ref(false)
export const cancelling = ref(false)
export const selectedRun = ref<number | null>(null)
/** 选中的运行和它的每个测试（按画布上的功能线取，带 on_line） */
export const runDetail = shallowRef<RunDetail | null>(null)
export const detailError = ref('')
export const selectedTest = ref<string | null>(null)
/** 选中的测试的运行路径（只有这一个测试） */
export const testPaths = shallowRef<{ run: number; test: string; paths: RunPathsJson } | null>(null)
export const pathsLoading = ref(false)
export const pathsError = ref('')
/** 选中的运行在这条线上没走到的结局 */
export const uncovered = shallowRef<{ run: number; line: string; result: UncoveredResult } | null>(null)
/** 取没走到的结局出错了（面板上说一句，可以重试） */
export const uncoveredError = ref('')
/** "在图上标出"没走到的结局 */
export const showUncovered = ref(false)
/** 测试列表只列走过这条线的（关掉 = 全部测试） */
export const onlyLine = ref(true)
export const testQuery = ref('')
/** 要用户确认的命令（确认框开着时不为 null）；scope：确认后只跑这条线的测试（全部测试时没有这一项） */
export const confirmAsk = shallowRef<{ command: string; root: string; scope?: LaunchScope } | null>(null)
export const commandDialog = ref(false)
/** 正在开始的那次运行（POST 还没回）跑的是什么；没有时 null */
export const startingScope = shallowRef<LaunchScope | null>(null)
/** 还在跑的运行跑的是什么（这个页面开始的自己记，别处开始的按后端说的）；没在跑、旧版后端不说时 null（界面只说阶段） */
export const runningScope = shallowRef<LaunchScope | null>(null)
/** 画布上这条线的测试名单（只跑这条线的话会跑哪些；line 是问的时候的线） */
export const lineTests = shallowRef<{ line: string; result: LineTestsResult } | null>(null)
export const lineTestsError = ref('')
/** 后端支不支持只跑一条线的测试：unsupported 旧版后端（没有 line-tests 接口），不显示"运行这条线的测试" */
export const lineRunSupport = ref<'unknown' | 'ok' | 'unsupported'>('unknown')
/** 正在保存"允许 AI 经 MCP 运行单条功能线的测试" */
export const mcpSaving = ref(false)

/** 回放：哪次运行的哪个测试、每一步、放到第几步、在不在放、按哪个画布视图算的（视图换了就停） */
export interface ReplayState {
  run: number
  test: string
  plan: ReplayPlan
  index: number
  playing: boolean
  view: LineView
}
export const replay = shallowRef<ReplayState | null>(null)
/** 回放每一步多久（毫秒）；测试里改小 */
export const replaySpeed = { ms: 350 }
/** 系统设置了"减少动态效果"（测试里换掉） */
export const motion = {
  reduced: (): boolean => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches,
}
/** 最近一次运行的输出末尾（点"看测试输出"才取） */
export const output = shallowRef<string[] | null>(null)
export const outputError = ref('')
/** 运行中最近一次取到的、某条功能线上走到过的节点（函数 key → 节点 sid）；line 是请求时画布上的那条线 */
const liveNodes = shallowRef<{ line: string; nodes: Record<string, string[]> } | null>(null)
/** 运行中最近一次有变化的刷新里，执行过的函数变多了的功能线 */
export const liveFresh = shallowRef<Set<string>>(new Set())

// ---------------------------------------------------------------------------------------------------------------
// 算出来的

export const selectedRunInfo = computed<RunInfo | null>(() => runs.value.find((r) => r.id === selectedRun.value) ?? null)

/** 选中的运行只跑了别的功能线的测试：那条线（不给这条线的测试路径、没走到的结局）；全量、只跑了这条线的为 null */
export const selectedElsewhere = computed<RunScope | null>(() => {
  const r = selectedRunInfo.value
  return r && !runFitsLine(r, lineSid.value) ? r.scope : null
})

/** 画布上这条线的测试名单（还没取到、取的是别的线时 null） */
export const lineTestsNow = computed<LineTestsResult | null>(() => (lineTests.value?.line === lineSid.value ? lineTests.value.result : null))

/** 测试命令正在跑时后端给的进度（阶段 testing；旧版后端没有）；别的时候 null */
export const liveProgress = computed<LiveProgress | null>(() => (running.value?.phase === 'testing' ? running.value.progress : null))

/** 运行中每条功能线有几个函数执行过（只在"运行路径"页签里给侧栏用；没在跑时空） */
export const liveLines = computed<LiveProgress['lines']>(() => liveProgress.value?.lines ?? {})

/** 画布上是运行中的样子：在"运行路径"页签里、测试命令在跑、后端给了进度、没有选测试 */
export const liveView = computed(() => tab.value === 'run' && liveProgress.value !== null && selectedTest.value === null)

/** 测试列表（走过这条线的 / 全部，失败的在前，按搜索框筛） */
export const visibleTests = computed<TestRow[]>(() => {
  const d = runDetail.value
  return d ? testsForLine(d.tests, d.line_known, onlyLine.value, testQuery.value) : []
})

/** 选中的测试叠到画布上这张图上 */
export const overlay = computed<RunOverlay | null>(() => {
  const t = testPaths.value
  const p = project.value
  if (!t || !p || t.test !== selectedTest.value || t.run !== selectedRun.value || selectedElsewhere.value) return null
  return buildRunOverlay(p, t.paths, t.test, lineSid.value || null)
})

/**
 * 选中的测试失败了：停在这条线上哪里（runfailure 的 failureStop，裁决 S11e / S14，按记下的异常类型、调用和入口函数）——
 * 异常从这条线抛出去了时是红圈画在哪个节点上（抛出的函数钻入不到时是调用它的节点），没有入口把它带到测试里时是"测试是在
 * 自己的断言里失败的"等。通过的测试、没选测试时 null。
 */
export const failure = computed<FailureStop | null>(() => {
  const o = overlay.value
  const p = project.value
  const t = testPaths.value?.paths.tests.find((x) => x.test === o?.test)
  if (!o || !p || !t || !isFailedOutcome(t.outcome) || !lineSid.value) return null
  return failureStop(o, p, lineSid.value)
})

/** 没走到的结局已经按画布上的运行和功能线取到了（换线、换运行时，取到新的之前为假） */
export const uncoveredReady = computed(() => {
  const u = uncovered.value
  return !!u && u.run === selectedRun.value && u.line === lineSid.value
})

/** 这条线上没走到的结局：确定没走到的、对不上（代码改过，重新运行测试才知道）的 */
export const uncoveredLists = computed<{ certain: UncoveredJson[]; uncertain: UncoveredJson[] }>(() => {
  const u = uncovered.value
  if (!u || u.run !== selectedRun.value || u.line !== lineSid.value) return { certain: [], uncertain: [] }
  return { certain: u.result.outcomes.filter((o) => !o.uncertain), uncertain: u.result.outcomes.filter((o) => o.uncertain) }
})

const NO_MARKS = emptyRunMarks()

/** 失败的测试的红圈画在哪：停点（stop），或者没有记录到流程函数调用它的线上函数的停点（direct，只经执行过的函数到得了时） */
function ringOf(f: FailureStop | null): RingAt | null {
  if (f?.kind === 'stop') return f
  if (f?.kind === 'direct' && f.reveal) return f
  return null
}

/** 失败的测试能定位到哪（"定位到停点"、选中时自动定位）；没有时 null */
export const failureReveal = computed(() => {
  const f = failure.value
  return f?.kind === 'stop' ? f.reveal : f?.kind === 'direct' ? f.reveal : null
})
const NONE = new Map<string, Set<string>>()

/**
 * 选中的测试（或运行中、没走到的结局）的完整标注，不管回放：详情栏的"这个测试里：走过 3 次……"按它说。画布用
 * canvasRunMarks（回放时是回放到的那一步）。
 */
export const testRunMarks = computed<RunMarks>(() => {
  if (tab.value !== 'run') return NO_MARKS
  if (liveView.value) {
    // 运行中：只用画布上这条线的节点（刚换线、新线的还没取到时什么都不标）
    const live = liveNodes.value
    return live && live.line === lineSid.value ? liveRunMarks(view.value, project.value, live.nodes) : NO_MARKS
  }
  const p = project.value
  const show = showUncovered.value && selectedTest.value === null && !!p
  const lists = uncoveredLists.value
  return runMarks(
    view.value,
    p,
    overlay.value,
    ringOf(failure.value),
    show ? uncoveredMarks(p!, lists.certain) : NONE,
    show ? uncoveredMarks(p!, lists.uncertain) : NONE,
  )
})

/** 画布上的运行标注：只在"运行路径"页签里；选了测试画它走过的路，没选、打开"在图上标出"时画没走到的结局；回放时是回放到的那一步 */
export const canvasRunMarks = computed<RunMarks>(() => {
  const base = testRunMarks.value
  const r = replay.value
  if (!r || tab.value !== 'run' || r.view !== view.value || !base.active || base.live) return base
  return replayMarks(base, r.view, r.plan, r.index)
})

/** "这次运行之后代码改过（重新分析过）……"：运行用的分析不是画布上这次的，或者选中的测试有对不上的地方 */
export const runNote = computed<string | null>(() => {
  const r = selectedRunInfo.value
  if (!r) return null
  return staleRunNote(r.analysis_id, graphOf.value?.analysis ?? null, overlay.value ? overlay.value.unmatched : null)
})

// ---------------------------------------------------------------------------------------------------------------
// 读

/** 换项目时加一：还在路上的响应回来时发现不是这一代就丢掉 */
let gen = 0
const runsSeq = createSequencer()
const detailSeq = createSequencer()
const pathsSeq = createSequencer()
const uncoveredSeq = createSequencer()
const lineTestsSeq = createSequencer()
/** 已经为哪个（运行，功能线，图的分析）取过运行详情和没走到的结局 */
let loadedKey = ''
/** 已经为哪个（功能线，图的分析，运行列表）取过测试名单 */
let lineTestsKey = ''
let pollTimer: ReturnType<typeof setTimeout> | null = null
/** 正在跑时连续查错了几次（退避用） */
let pollFailures = 0
/** 退避的间隔：轮询间隔（2 秒）的 1、2、4 倍，之后每 7.5 倍（15 秒） */
const BACKOFF = [1, 2, 4, 7.5]

function stopPoll() {
  if (pollTimer !== null) clearTimeout(pollTimer)
  pollTimer = null
}

function reset() {
  gen++
  stopPoll()
  loadedKey = ''
  runSupport.value = 'unknown'
  runsError.value = ''
  testCommand.value = null
  runs.value = []
  running.value = null
  runningUnknown.value = false
  pollFailures = 0
  lastFailure.value = null
  starting.value = false
  cancelling.value = false
  selectedRun.value = null
  runDetail.value = null
  detailError.value = ''
  selectedTest.value = null
  testPaths.value = null
  pathsLoading.value = false
  pathsError.value = ''
  uncovered.value = null
  uncoveredError.value = ''
  confirmAsk.value = null
  commandDialog.value = false
  output.value = null
  outputError.value = ''
  testQuery.value = ''
  liveNodes.value = null
  liveFresh.value = new Set()
  startingScope.value = null
  runningScope.value = null
  lineTests.value = null
  lineTestsError.value = ''
  lineTestsKey = ''
  lineRunSupport.value = 'unknown'
  mcpSaving.value = false
  runTabLoading = false
  stopReplay()
}

/** 正在读测试命令和运行列表（loadRunTab）：进页签时不再重复读 */
let runTabLoading = false

/** 换了项目、切到"运行路径"页签时：读测试命令和运行列表 */
export async function loadRunTab(): Promise<void> {
  const id = projectId.value
  if (!id || backend.value === 'down') return
  const g = gen
  runTabLoading = true
  try {
    const [cmd, list] = await Promise.all([api.getTestCommand(id), api.listRuns(id)])
    if (g !== gen) return
    runSupport.value = 'ok'
    runsError.value = ''
    testCommand.value = cmd
    applyRuns(list)
  } catch (e) {
    if (g !== gen) return
    if (isMissingEndpoint(e)) runSupport.value = 'unsupported'
    else {
      runSupport.value = 'error'
      runsError.value = errorText(e)
    }
  } finally {
    if (g === gen) runTabLoading = false
  }
}

/** 重新查一次运行列表（正在跑时每隔 analyzingPoll.ms 一次；窗口回到前台时一次） */
export async function refreshRuns(): Promise<void> {
  const id = projectId.value
  if (!id || runSupport.value === 'unsupported' || backend.value === 'down') return
  const g = gen
  const n = runsSeq.begin()
  // 在跑：带上画布上的功能线，后端给这条线上走到过的节点
  const line = running.value ? lineSid.value || null : null
  try {
    const list = await api.listRuns(id, line)
    if (g !== gen || !runsSeq.isLatest(n)) return
    runSupport.value = 'ok'
    runsError.value = ''
    pollFailures = 0
    runningUnknown.value = false
    applyRuns(list, line)
  } catch (e) {
    if (g !== gen || !runsSeq.isLatest(n)) return
    stopPoll()
    if (isMissingEndpoint(e)) {
      runSupport.value = 'unsupported'
      return
    }
    runsError.value = errorText(e)
    // 正在跑：不放弃，退避着再查（这期间不知道现在怎样）
    if (running.value && runSupport.value === 'ok') {
      runningUnknown.value = true
      const delay = BACKOFF[Math.min(pollFailures, BACKOFF.length - 1)] * analyzingPoll.ms
      pollFailures += 1
      pollTimer = setTimeout(() => {
        pollTimer = null
        if (g === gen) void refreshRuns()
      }, delay)
    }
  }
}

function applyRuns(list: RunsResult, line: string | null = null) {
  const finished = running.value !== null && list.running === null
  const prevTop = runs.value[0]?.id ?? null
  applyLive(list.running?.phase === 'testing' ? list.running.progress : null, line)
  runs.value = list.runs
  running.value = list.running
  // 后端说了正在跑的是哪种（别处开始的运行也知道）：这个页面自己没记着时用它
  const told = list.running?.scope
  if (list.running && runningScope.value === null && told !== undefined) {
    runningScope.value = told === null ? { line: null, title: '', tests: 0 } : { line: told.line, title: told.line_title, tests: told.tests }
  }
  runningAt.value = Date.now()
  lastFailure.value = list.last_failure
  if (!list.running) cancelling.value = false
  const top = list.runs[0] ?? null
  const newTop = (top?.id ?? null) !== prevTop
  // 这个页面开始的全部测试跑完了、人不在"运行路径"页签里：说一声结果
  const ours = runningScope.value
  if (finished && newTop && top && ours && ours.line === null && tab.value !== 'run') {
    const type = top.status === 'ok' ? 'success' : top.status === 'tests_failed' ? 'warning' : 'info'
    notify(type, `全部测试跑完了：${runResultText(top)}。到"运行路径"页签里按功能线看每个测试走过的路`)
  }
  if (!list.running) runningScope.value = null
  const sel = selectedRun.value
  // 有了新的一次运行（刚跑完的），或者选中的那次已经不在了：选这条线默认的（最新的全量运行，或只跑了这条线的运行）
  if (newTop || sel === null || !list.runs.some((r) => r.id === sel)) {
    const want = defaultRunFor(list.runs, lineSid.value)
    if (want !== sel) selectRun(want)
  }
  // 运行前图过期、先重新分析过：刷新项目列表，画布换上新图（state.ts 的 followNewAnalysis）
  const shown = graphOf.value?.analysis ?? null
  if (finished && top && shown !== null && top.analysis_id > shown) void refreshProjects()
  schedulePoll()
}

/** 运行中的进度：这条线上走到过的节点、执行过的函数变多了的线（和上一次比；什么都没变的刷新不改） */
function applyLive(progress: LiveProgress | null, line: string | null) {
  if (!progress) {
    liveNodes.value = null
    if (liveFresh.value.size) liveFresh.value = new Set()
    return
  }
  if (line !== null && progress.nodes !== null) liveNodes.value = { line, nodes: progress.nodes }
  const before = liveProgress.value?.lines ?? {}
  const grew = Object.entries(progress.lines).filter(([sid, c]) => c.functions > (before[sid]?.functions ?? 0))
  if (grew.length) liveFresh.value = new Set(grew.map(([sid]) => sid))
}

function schedulePoll() {
  if (pollTimer !== null || !running.value || runSupport.value !== 'ok' || backend.value === 'down') return
  const g = gen
  pollTimer = setTimeout(() => {
    pollTimer = null
    if (g === gen) void refreshRuns()
  }, analyzingPoll.ms)
}

/** 选一次运行：清掉选中的测试，取它的详情和没走到的结局（在"运行路径"页签里时） */
export function selectRun(id: number | null) {
  selectedRun.value = id
  runDetail.value = null
  detailError.value = ''
  clearTest()
  uncovered.value = null
  loadedKey = ''
  syncRunData()
}

/**
 * 运行详情、没走到的结局按（运行，功能线，图的分析）取；同一组不重取。只跑了别的线的测试的运行不取（面板上只说它跑的
 * 是哪条线的测试）。
 */
function syncRunData() {
  if (tab.value !== 'run' || runSupport.value !== 'ok' || selectedRun.value === null || !projectId.value) return
  const key = `${selectedRun.value}\u0000${lineSid.value}\u0000${graphOf.value?.analysis ?? ''}`
  if (key === loadedKey) return
  loadedKey = key
  if (selectedElsewhere.value) {
    detailSeq.begin()
    uncoveredSeq.begin()
    runDetail.value = null
    detailError.value = ''
    uncovered.value = null
    uncoveredError.value = ''
    clearTest()
    return
  }
  void loadDetail()
  void loadUncovered()
}

/** 换了功能线：选中的运行只跑了别的线的测试（或者没选）时，换成这条线默认的那次 */
function followLine() {
  const r = selectedRunInfo.value
  if (r && runFitsLine(r, lineSid.value)) return
  const want = defaultRunFor(runs.value, lineSid.value)
  if (want !== selectedRun.value) selectRun(want)
}

/** 上一份运行详情是哪条线的（换线时按新线的 on_line 决定保不保留选中的测试） */
let detailLine = ''

async function loadDetail(): Promise<void> {
  const id = projectId.value
  const runId = selectedRun.value
  const line = lineSid.value
  if (!id || runId === null) return
  const g = gen
  const n = detailSeq.begin()
  try {
    const d = await api.getRun(id, runId, line || null)
    if (g !== gen || !detailSeq.isLatest(n) || selectedRun.value !== runId) return
    const lineChanged = runDetail.value !== null && detailLine !== line
    runDetail.value = d
    detailLine = line
    detailError.value = ''
    // 换了功能线：选中的测试没走过新线就清掉
    const t = selectedTest.value
    if (lineChanged && t !== null && d.line_known) {
      const row = d.tests.find((r) => r.test === t)
      if (!row || (row.on_line ?? 0) === 0) clearTest()
    }
  } catch (e) {
    if (g !== gen || !detailSeq.isLatest(n)) return
    detailError.value = errorText(e)
  }
}

async function loadUncovered(): Promise<void> {
  const id = projectId.value
  const runId = selectedRun.value
  const line = lineSid.value
  const run = runs.value.find((r) => r.id === runId)
  uncoveredError.value = ''
  // 只跑了别的线的测试的运行：这条线上"没走到"说明不了什么（后端也只回答它那条线）
  if (!id || runId === null || !line || !run || run.paths_size === null || !runFitsLine(run, line)) {
    uncoveredSeq.begin()
    uncovered.value = null
    return
  }
  const g = gen
  const n = uncoveredSeq.begin()
  try {
    const result = await api.getUncovered(id, runId, line)
    if (g !== gen || !uncoveredSeq.isLatest(n)) return
    uncovered.value = { run: runId, line, result }
  } catch (e) {
    // 最新的图里没有这条线、运行刚被清理掉……：面板上说一句，可以重试
    if (g !== gen || !uncoveredSeq.isLatest(n)) return
    uncovered.value = null
    uncoveredError.value = errorText(e)
  }
}

/** "没能取到未覆盖的结局"旁边的重试 */
export function retryUncovered(): Promise<void> {
  return loadUncovered()
}

/**
 * 这条线的测试名单（"运行这条线的测试（N 个）"）：在"运行路径"页签里，按（功能线，图的分析，运行列表）取——换线、
 * 重新分析、跑完一次运行后重取。旧版后端（没有这个接口）：lineRunSupport 为 unsupported，之后这个项目不再问。
 */
function syncLineTests(force = false) {
  const id = projectId.value
  const line = lineSid.value
  if (tab.value !== 'run' || runSupport.value !== 'ok' || lineRunSupport.value === 'unsupported' || !id || !line) return
  const key = `${line}\u0000${graphOf.value?.analysis ?? ''}\u0000${runs.value.map((r) => r.id).join(',')}`
  if (!force && key === lineTestsKey) return
  lineTestsKey = key
  void loadLineTests(id, line)
}

async function loadLineTests(id: string, line: string): Promise<void> {
  const g = gen
  const n = lineTestsSeq.begin()
  try {
    const result = await api.getLineTests(id, line)
    if (g !== gen || !lineTestsSeq.isLatest(n)) return
    lineRunSupport.value = 'ok'
    lineTestsError.value = ''
    lineTests.value = { line, result }
  } catch (e) {
    if (g !== gen || !lineTestsSeq.isLatest(n)) return
    if (isMissingEndpoint(e)) {
      lineRunSupport.value = 'unsupported'
      return
    }
    lineTests.value = null
    lineTestsError.value = errorText(e)
  }
}

/** 取测试名单出错时的重试 */
export function retryLineTests() {
  syncLineTests(true)
}

// ---------------------------------------------------------------------------------------------------------------
// 回放

let replayTimer: ReturnType<typeof setTimeout> | undefined

/**
 * "回放"：按画布现在的样子算出每一步（replay.ts），从第一步开始每 replaySpeed.ms 走一步，走到的元素不在屏幕上时平移
 * 过去。系统设置了"减少动态效果"时不放（画布上就是走完后的样子），说一声。
 */
export function startReplay(): void {
  const t = testPaths.value
  const v = view.value
  const p = project.value
  if (!t || !v || !p || tab.value !== 'run' || t.test !== selectedTest.value || t.run !== selectedRun.value) return
  stopReplay()
  if (motion.reduced()) {
    notify('info', REPLAY_REDUCED_TIP)
    return
  }
  const plan = replayPlan(v, p, t.paths, t.test)
  if (!plan.steps.length) {
    notify('info', '画布上看得见的函数在这个测试里都没有执行过，没有可以回放的')
    return
  }
  replay.value = { run: t.run, test: t.test, plan, index: 0, playing: true, view: v }
  peekElement(plan.steps[0].element)
  scheduleReplay()
}

function scheduleReplay() {
  clearTimeout(replayTimer)
  replayTimer = setTimeout(stepReplay, replaySpeed.ms)
}

function stepReplay() {
  const r = replay.value
  if (!r || !r.playing) return
  if (r.index >= r.plan.steps.length - 1) {
    // 放完了：换回这个测试完整的样子（次数、耗时、停点）
    replay.value = null
    return
  }
  replay.value = { ...r, index: r.index + 1 }
  peekElement(r.plan.steps[r.index + 1].element)
  scheduleReplay()
}

export function pauseReplay() {
  const r = replay.value
  if (!r || !r.playing) return
  clearTimeout(replayTimer)
  replay.value = { ...r, playing: false }
}

export function resumeReplay() {
  const r = replay.value
  if (!r || r.playing) return
  replay.value = { ...r, playing: true }
  scheduleReplay()
}

/** 停止回放，画布换回这个测试完整的样子 */
export function stopReplay() {
  clearTimeout(replayTimer)
  replay.value = null
}

/** 选一个测试：只取它的运行路径；失败的测试排好后定位到停点 */
export async function selectTest(test: string | null): Promise<void> {
  if (test === null) return clearTest()
  const id = projectId.value
  const runId = selectedRun.value
  if (!id || runId === null) return
  selectedTest.value = test
  if (testPaths.value?.run === runId && testPaths.value.test === test) return
  testPaths.value = null
  pathsError.value = ''
  pathsLoading.value = true
  const g = gen
  const n = pathsSeq.begin()
  try {
    const r = await api.getTestPaths(id, runId, test)
    if (g !== gen || !pathsSeq.isLatest(n) || selectedTest.value !== test || selectedRun.value !== runId) return
    testPaths.value = { run: runId, test, paths: r.paths }
    pathsLoading.value = false
    // 只看走过这条线的测试时：点了一个测试，就把它在这条线上走过的地方都展开（不用一个个去点折叠的分支、钻入）
    if (onlyLine.value && overlay.value) {
      await revealWalked(overlay.value)
      if (g !== gen || selectedTest.value !== test || selectedRun.value !== runId) return
    }
    if (failureReveal.value) await revealFailure()
  } catch (e) {
    if (g !== gen || !pathsSeq.isLatest(n)) return
    pathsError.value = errorText(e)
  } finally {
    if (g === gen && pathsSeq.isLatest(n)) pathsLoading.value = false
  }
}

export function clearTest() {
  pathsSeq.begin()
  selectedTest.value = null
  testPaths.value = null
  pathsLoading.value = false
  pathsError.value = ''
}

// ---------------------------------------------------------------------------------------------------------------
// 定位

/** 定位到失败的停点（需要时展开卡片、分支、钻入）；没有能定位的停点时什么都不做 */
export async function revealFailure(): Promise<void> {
  const at = failureReveal.value
  if (at) await revealElement(at)
}

/** 失败的测试要去看的另一条线（异常是从那条线抛出去的，或者抛出它的函数在那条线上）；没有时 null */
export const failureLine = computed(() => {
  const f = failure.value
  return f?.kind === 'otherLine' ? f.line : f?.kind === 'elsewhere' ? (f.lines[0]?.sid ?? null) : null
})

/** "切到那条线"：换到 failureLine（选中的测试照旧），排好后能定位时定位到停点 */
export async function gotoFailureLine(): Promise<void> {
  const target = failureLine.value
  if (!target) return
  const test = selectedTest.value
  await selectLine(target)
  if (selectedTest.value === test && failureReveal.value) await revealFailure()
}

/** 点一个没走到的结局：在画布上露出来、选中 */
export async function gotoOutcome(o: UncoveredJson): Promise<void> {
  const p = project.value
  if (!p) return
  const hit = [...uncoveredMarks(p, [o])][0]
  if (!hit) {
    notify('info', `「${o.title}」在画布上的这张图里找不到了（代码可能又改过），重新分析后再看`)
    return
  }
  const [fnKey, ids] = hit
  await gotoNode(fnKey, [...ids][0])
}

/** 没有记下异常类型的运行里，点"抛出过异常的函数"中的一个：定位并选中那个停点（不画红圈） */
export function gotoRaise(functionKey: string, nodeId: string): Promise<void> {
  return gotoNode(functionKey, nodeId)
}

/** 点核对结果里的一条：跳到那个函数（不在这条线上时换到涉及它的线） */
export function gotoFunction(fnKey: string): Promise<void> {
  return gotoNode(fnKey, null)
}

async function gotoNode(fnKey: string, nodeId: string | null): Promise<void> {
  const p = project.value
  if (!p) return
  const found = findOnGraph(p, lineSid.value, fnKey, nodeId)
  if (!found) {
    const f = p.functions[fnKey]
    notify('info', `这个函数不在任何功能线上${f ? `（${f.file} 第 ${f.line} 行）` : ''}`)
    return
  }
  if (found.line !== lineSid.value) await selectLine(found.line)
  await revealElement(found.stop, true)
}

// ---------------------------------------------------------------------------------------------------------------
// 运行、取消、测试命令

/** "运行全部测试"（顶栏）：没填命令打开命令对话框；命令还没确认过先弹确认；否则开始 */
export function startRun(): Promise<void> {
  return begin(FULL)
}

/**
 * "运行这条线的测试"（运行面板）：只跑名单里的测试（走过画布上这条线的，名单取自最近一次全量运行）。名单还没取到、
 * 挑不出来时什么都不做（按钮是灰的）。命令的确认同"运行全部测试"。
 */
export function startLineRun(): Promise<void> {
  const line = lineSid.value
  const picked = lineTestsNow.value
  if (!line || !picked?.ok) return Promise.resolve()
  return begin({ line, title: displayBySid.value.get(line)?.titleFull ?? line, tests: picked.tests.length })
}

async function begin(scope: LaunchScope): Promise<void> {
  const id = projectId.value
  if (!id || runSupport.value !== 'ok' || starting.value || running.value) return
  const cmd = testCommand.value
  if (!cmd?.command) {
    commandDialog.value = true
    return
  }
  if (!cmd.confirmed) {
    confirmAsk.value = askFor(cmd.command, currentProject.value?.root ?? '', scope)
    return
  }
  await launch(id, null, scope)
}

/** 确认框的内容：只跑一条线时带上 scope */
const askFor = (command: string, root: string, scope: LaunchScope) => (scope.line === null ? { command, root } : { command, root, scope })

/** 确认框里点了"运行"：带上确认框里的命令原文 */
export async function confirmRun(): Promise<void> {
  const ask = confirmAsk.value
  const id = projectId.value
  if (!ask || !id) return
  confirmAsk.value = null
  await launch(id, ask.command, ask.scope ?? FULL)
}

export function cancelConfirm() {
  confirmAsk.value = null
}

/** 挑不出这条线的测试（后端在开始之前先问了一遍）的 code */
const CANNOT_PICK = new Set(['no_full_run', 'no_tests', 'no_index', 'no_line'])

async function launch(id: string, confirmed: string | null, scope: LaunchScope): Promise<void> {
  const g = gen
  starting.value = true
  startingScope.value = scope
  try {
    await api.startRun(id, confirmed, scope.line)
    if (g !== gen) return
    if (confirmed !== null) testCommand.value = { command: confirmed, confirmed: true }
    clearTest() // 画布上换成这次运行的进度（选中的是上一次运行的测试）
    running.value = { started_at: new Date().toISOString(), seconds: 0, phase: 'preparing', progress: null }
    runningScope.value = scope
    runningAt.value = Date.now()
    lastFailure.value = null
    output.value = null
    await refreshRuns()
  } catch (e) {
    if (g !== gen) return
    if (isApiError(e) && e.code === 'confirm') {
      // 第一次运行这条命令，或者确认框开着时命令被改了：按后端说的现在的命令再问一次
      const command = typeof e.data.command === 'string' ? e.data.command : (testCommand.value?.command ?? '')
      const root = typeof e.data.root === 'string' ? e.data.root : (currentProject.value?.root ?? '')
      testCommand.value = { command, confirmed: false }
      confirmAsk.value = askFor(command, root, scope)
    } else if (isApiError(e) && e.code === 'no_command') {
      testCommand.value = { command: null, confirmed: false }
      commandDialog.value = true
    } else if (isApiError(e) && e.code === 'busy') {
      notify('warning', e.message || '这个项目正在分析或已经在运行测试，等它完成后再试')
      void refreshRuns()
    } else if (scope.line !== null && isApiError(e) && e.code !== null && CANNOT_PICK.has(e.code)) {
      // 名单变了（别处刚跑过、重新分析过）：说后端的原因，重新取名单
      notify('warning', `没有开始运行：${e.message}`)
      syncLineTests(true)
    } else if (isMissingEndpoint(e)) runSupport.value = 'unsupported'
    else if (isApiError(e) && e.kind === 'unreachable') notify('error', '没有开始运行：连不上平台后端')
    else notify('error', `没有开始运行：${errorText(e)}`)
  } finally {
    if (g === gen) {
      starting.value = false
      startingScope.value = null
    }
  }
}

/** 取消正在跑的测试；后端有说明（比如正在运行前的重新分析）时显示出来 */
export async function cancelRun(): Promise<void> {
  const id = projectId.value
  if (!id || !running.value || cancelling.value) return
  const g = gen
  cancelling.value = true
  try {
    const note = await api.cancelRun(id)
    if (g === gen && note) notify('info', note)
  } catch (e) {
    if (g !== gen) return
    cancelling.value = false
    if (isApiError(e) && e.code === 'not_running') await refreshRuns()
    else if (isApiError(e) && e.code === 'too_late') notify('info', e.message)
    else notify('error', `没有取消：${errorText(e)}`)
  }
}

/** 保存测试命令（空 = 清掉）；失败抛 ApiError（对话框显示后端的说明） */
export async function saveTestCommand(text: string): Promise<void> {
  const id = projectId.value
  if (!id) return
  const g = gen
  const r = await api.putTestCommand(id, text.trim() || null)
  if (g === gen) testCommand.value = r
}

/**
 * "允许 AI（经 MCP）运行单条功能线的测试"：马上存（PUT /mcp-tests），存好后改项目列表里的这一项。失败抛 ApiError
 * （对话框显示后端的说明）。
 */
export async function setMcpTests(allow: boolean): Promise<void> {
  const id = projectId.value
  if (!id || mcpSaving.value) return
  const g = gen
  mcpSaving.value = true
  try {
    const saved = await api.putMcpTests(id, allow)
    if (g === gen) patchProject(id, { mcp_tests: saved })
  } finally {
    if (g === gen) mcpSaving.value = false
  }
}

/** 项目里找到的虚拟环境解释器（测试命令的表单用）；读不到时空——只是少了可以点的候选 */
export async function loadInterpreters(): Promise<Interpreter[]> {
  const id = projectId.value
  if (!id) return []
  try {
    return await api.getInterpreters(id)
  } catch {
    return []
  }
}

/** "看测试输出"：最近一次运行输出的末尾（只在后端进程内存里） */
export async function loadOutput(): Promise<void> {
  const id = projectId.value
  if (!id) return
  const g = gen
  outputError.value = ''
  try {
    const lines = await api.runOutput(id)
    if (g === gen) output.value = lines
  } catch (e) {
    if (g === gen) outputError.value = errorText(e)
  }
}

// ---------------------------------------------------------------------------------------------------------------
// 跟着项目、页签、功能线、图

// 换项目：马上读测试命令和运行列表（顶栏的"运行全部测试"在哪个页签都要用）
watch(
  projectId,
  () => {
    reset()
    if (projectId.value) void loadRunTab()
  },
  { flush: 'sync' },
)

watch(tab, (t) => {
  if (t !== 'run' || !projectId.value) return
  // 旧版后端、出过错也再探一次：用户可能已经重启了 pystepmap serve（还在读的时候不重复）
  if (runSupport.value !== 'ok') {
    if (!runTabLoading) void loadRunTab()
  } else {
    void refreshRuns()
    syncRunData()
  }
})

watch([lineSid, () => graphOf.value?.analysis], ([line], [oldLine]) => {
  if (line !== oldLine) followLine()
  syncRunData()
})

// 测试名单：换线、重新分析、跑完一次（运行列表变了）、进页签时
watch([tab, lineSid, () => graphOf.value?.analysis, runs, runSupport], () => syncLineTests())

// 回放：换了测试、运行、线、页签，画布展开状态变了（每一步是按原来的画布算的）都停
watch([selectedTest, selectedRun, lineSid, tab, view], () => {
  if (replay.value) stopReplay()
})

// 运行中换了功能线：马上再查一次，取新线上走到过的节点
watch(lineSid, () => {
  if (tab.value === 'run' && liveProgress.value) void refreshRuns()
})

/** 窗口回到前台：在"运行路径"页签里时查一次运行列表（旧版后端、出过错时重新探一次） */
export function onRunPageVisible() {
  if (tab.value !== 'run' || !projectId.value) return
  if (runSupport.value === 'ok') void refreshRuns()
  else void loadRunTab()
}

let started = false
/** 注册"窗口回到前台"的监听（App 挂载时调用一次） */
export function initRuns() {
  if (started || typeof document === 'undefined') return
  started = true
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onRunPageVisible()
  })
}
