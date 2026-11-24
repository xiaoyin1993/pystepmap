// "运行路径"页签的状态（runstate.ts）和平台后端之间的来回：一个假的后端（换掉 fetch）按脚本跑测试，把真的 state.ts、
// runstate.ts、api.ts、排版跑一遍。每个测试重新导入，状态互不影响。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectInfo, RunInfo, RunPathsJson, RunScope, TestRunJson, UncoveredJson } from './api'
import { line, project, task } from './graph/fixtures/builders'
import { K, LINE, all, big, check, fnRun, fsid, graph, okTest, paths, post, run } from './graph/fixtures/runs'

type State = typeof import('./state')
type Runs = typeof import('./runstate')

function deferred<T = void>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

/** 第二条功能线：子任务 send 自己成线 */
const sendLine = line(task(K.send), all)
const GRAPH = JSON.stringify(project(all, [graph.lines[0], sendLine]))

/** 被接住的异常之后测试自己的断言失败：check 抛出，run 走"拒绝"正常结束 */
const T_HANDLED = 'tests/test_wf.py::test_handled'
const handledTest: TestRunJson = {
  test: T_HANDLED,
  outcome: 'failed',
  seconds: 0.01,
  cases: 1,
  functions: {
    [fsid(run)]: { ...fnRun(run, [['start', 1, 0], ['step:n2', 1, 5], ['cond:n3', 1, 5], ['step:n10', 1, 5], ['end:n4', 1, 0]], {}, { 'end:n4|return|': 1 }), calls: { [fsid(check)]: 1 } },
    [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:raise1|raise|ValueError': 1 }),
  },
  exc: 'AssertionError',
  excs: ['AssertionError'],
}
/** 判断节点调用的判断函数 big 里抛出 */
const T_COND = 'tests/test_wf.py::test_condition_raises'
const condTest: TestRunJson = {
  test: T_COND,
  outcome: 'failed',
  seconds: 0.01,
  cases: 1,
  functions: {
    [fsid(run)]: { ...fnRun(run, [['start', 1, 0], ['step:n2', 1, 5], ['cond:n3', 1, 5]], { 'start|step:n2': 1, 'step:n2|cond:n3': 1 }, { 'end:uncaught|raise|RuntimeError': 1 }), calls: { [fsid(big)]: 1 } },
    [fsid(big)]: fnRun(big, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:uncaught|raise|RuntimeError': 1 }),
  },
  exc: 'RuntimeError',
  excs: ['RuntimeError'],
}
const SEND_LINE = sendLine.sid

const T_FAIL = 'tests/test_wf.py::test_limit'
/** 失败的测试：check 抛出超限（raise 语句），run 以未捕获异常离开 */
const failTest: TestRunJson = {
  test: T_FAIL,
  outcome: 'failed',
  seconds: 0.02,
  cases: 1,
  functions: {
    [fsid(run)]: { ...fnRun(run, [['start', 1, 0], ['step:n2', 1, 9_000]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }), calls: { [fsid(check)]: 1 } },
    [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5_000], ['end:raise1', 1, 0]], { 'start|cond:n2': 1, 'cond:n2|end:raise1': 1 }, { 'end:raise1|raise|ValueError': 1 }),
  },
  exc: 'ValueError',
  excs: ['ValueError'],
}

const summary = (passed: number, failed: number) => ({
  tests: passed + failed,
  passed,
  failed,
  skipped: 0,
  errors: 0,
  seconds: 0.5,
  verify: { ok: true, mismatches: { edge: 0, end: 0, call: 0 }, functions_seen: 4, distinct_paths: 3, steps_total: 20, plugin_errors: 0, plugin_error_types: {}, truncated_records: 0, stale: [] },
  coverage: null,
  mismatch_list: [],
  note: '',
})

interface FakeRun {
  info: RunInfo
  /** 每个测试：结果、走过哪些功能线 */
  tests: { test: string; outcome: string; walks: string[] }[]
  paths: RunPathsJson | null
  uncovered: UncoveredJson[]
}

interface FakeProject {
  info: ProjectInfo
  command: string | null
  confirmed: boolean
  runs: FakeRun[]
  running: { phase: string; polls: number; cancel: boolean; progress?: (liveLine: string | null) => unknown; scope?: RunScope } | null
  /** 下一次运行跑完时存下的 */
  next: FakeRun | null
}

/** 只跑这条线的话跑哪些测试（同后端的 pick_line_tests：名单取自最近一次全量运行） */
function pickLine(p: FakeProject, line: string): { ok: boolean; code: string; reason: string; tests: string[]; base_run: number | null } {
  const full = p.runs.find((r) => r.info.scope === null)
  if (!full) return { ok: false, code: 'no_full_run', reason: '还没有跑完的全量测试运行：先运行一次全部测试，平台才知道哪些测试走过这条功能线', tests: [], base_run: null }
  const tests = full.tests.filter((t) => t.walks.includes(line)).map((t) => t.test)
  if (!tests.length) return { ok: false, code: 'no_tests', reason: `全量运行 #${full.info.id} 里没有测试走过这条功能线：先给它写测试，再运行一次全部测试`, tests: [], base_run: null }
  return { ok: true, code: '', reason: '', tests, base_run: full.info.id }
}

let runSeq = 0
function fakeRun(analysis: number, tests: TestRunJson[], walks: Record<string, string[]>, status = 'tests_failed'): FakeRun {
  const id = ++runSeq
  return {
    info: {
      id,
      analysis_id: analysis,
      started_at: '2026-11-17T04:17:02+00:00',
      finished_at: '2026-11-17T04:17:05+00:00',
      command: 'pytest',
      status,
      exit_code: status === 'ok' ? 0 : 1,
      paths_size: 100,
      summary: summary(tests.filter((t) => t.outcome === 'passed').length, tests.filter((t) => t.outcome === 'failed').length),
      scope: null,
    },
    tests: tests.map((t) => ({ test: t.test, outcome: t.outcome, walks: walks[t.test] ?? [] })),
    paths: paths(tests),
    uncovered: [{ function: fsid(run), node: 'end:n4', title: '拒绝', end_kind: 'failed', file: 'wf.py', line: 5, uncertain: false }],
  }
}

function fakeBackend() {
  const projects = new Map<string, FakeProject>()
  const log: string[] = []
  const bodies: string[] = []
  const gates: { match: (method: string, url: string) => boolean; d: ReturnType<typeof deferred<void>> }[] = []
  /** 旧版后端：没有测试运行的接口 */
  let old = false
  /** 接下来几次 GET /runs 回 500（连接时好时坏） */
  let failRuns = 0
  /** 接下来几次 GET …/uncovered 回 500 */
  let failUncovered = 0
  /** 后端有测试运行的接口，但还没有 line-tests、mcp-tests（只跑一条线之前的版本） */
  let noLineTests = false

  const add = (id: string, name: string, analysis: number) => {
    projects.set(id, {
      info: {
        id,
        name,
        root: `/code/${name}`,
        created_at: '2026-11-16T04:00:00+00:00',
        updated_at: '2026-11-16T04:00:00+00:00',
        analyzing: false,
        analysis: { id: analysis, created_at: '2026-11-16T04:00:00+00:00', engine_version: '0.1.0', graph_size: GRAPH.length, summary: {} as never, base_id: null, has_diff: false },
        mcp_tests: false,
      },
      command: 'pytest',
      confirmed: true,
      runs: [],
      running: null,
      next: null,
    })
  }

  const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
  const missing = (method: string, path: string) => json({ error: `没有这个接口：${method} ${path}` }, 404)

  function route(method: string, url: string, init?: RequestInit): Response {
    const u = new URL(url, 'http://x')
    const parts = u.pathname.split('/').filter(Boolean).map(decodeURIComponent)
    if (parts.length === 2) return json([...projects.values()].map((p) => p.info))
    const p = projects.get(parts[2])
    if (!p) return json({ error: `项目不存在（可能已经删除了）：${parts[2]}` }, 404)
    const what = parts[3] ?? ''
    if (what === 'graph') return new Response(GRAPH, { headers: { 'Content-Type': 'application/json', 'X-Pystepmap-Analysis': String(p.info.analysis!.id) } })
    if (what === 'overrides') return json({})
    if (what === 'test-command' || what === 'runs') {
      if (old) return missing(method, u.pathname)
    }
    if (what === 'line-tests' || what === 'mcp-tests') {
      if (old || noLineTests) return missing(method, u.pathname)
      if (what === 'mcp-tests') {
        p.info = { ...p.info, mcp_tests: (JSON.parse(String(init?.body)) as { allow: boolean }).allow }
        return json({ allow: p.info.mcp_tests })
      }
      return json(pickLine(p, u.searchParams.get('line') ?? ''))
    }
    if (what === 'test-command' && method === 'GET') return json({ command: p.command, confirmed: p.confirmed })
    if (what === 'test-command' && method === 'PUT') {
      const body = JSON.parse(String(init?.body)) as { command: string | null }
      const c = body.command?.trim() || null
      if (c !== p.command) p.confirmed = false
      p.command = c
      return json({ command: p.command, confirmed: p.confirmed })
    }
    if (what !== 'runs') return missing(method, u.pathname)
    const sub = parts[4]
    if (sub === undefined && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}')) as { confirm?: boolean; command?: string; line?: string }
      if (!p.command) return json({ error: '还没有填测试命令', code: 'no_command' }, 400)
      if (!p.confirmed) {
        if (!body.confirm || body.command !== p.command) {
          return json({ error: '第一次运行这条测试命令之前要确认', code: 'confirm', command: p.command, root: p.info.root }, 409)
        }
        p.confirmed = true
      }
      let scope: RunScope | undefined
      if (body.line) {
        const picked = pickLine(p, body.line)
        if (!picked.ok) return json({ error: picked.reason, code: picked.code }, 409)
        scope = { line: body.line, line_title: '转账', tests: picked.tests.length, base_run: picked.base_run }
      }
      if (p.running) return json({ error: '正在运行测试', code: 'busy' }, 409)
      p.running = { phase: 'analyzing', polls: 2, cancel: false, scope }
      return json({ run: 'started' }, 202)
    }
    if (sub === undefined) {
      if (failRuns > 0) {
        failRuns -= 1
        return json({ error: '数据库正忙' }, 500)
      }
      const r = p.running
      if (r) {
        r.polls -= 1
        r.phase = 'testing'
        if (r.polls < 0) {
          p.running = null
          const done = p.next ?? fakeRun(p.info.analysis!.id, [okTest], {}, 'ok')
          if (r.cancel) done.info = { ...done.info, status: 'cancelled', paths_size: null }
          if (r.scope) done.info = { ...done.info, scope: r.scope }
          p.runs.unshift(done)
          p.next = null
        }
      }
      const live = p.running?.phase === 'testing' && p.running.progress ? { progress: p.running.progress(u.searchParams.get('live_line')) } : {}
      return json({
        running: p.running ? { started_at: '2026-11-17T04:17:02+00:00', seconds: 1, phase: p.running.phase, ...live } : null,
        runs: p.runs.map((x) => x.info),
        last_failure: null,
      })
    }
    if (sub === 'cancel') {
      if (!p.running) return json({ error: '这个项目现在没有在运行测试', code: 'not_running' }, 409)
      p.running.cancel = true
      return json({ cancelling: true })
    }
    if (sub === 'output') return json({ lines: ['collected 2 items', '1 failed'] })
    const fr = p.runs.find((x) => x.info.id === Number(sub))
    if (!fr) return json({ error: `没有这次运行（可能已经清理掉了）：#${sub}` }, 404)
    const lineSid = u.searchParams.get('line')
    if (parts[5] === undefined) {
      return json({
        ...fr.info,
        tests: fr.tests.map((t) => ({ test: t.test, outcome: t.outcome, seconds: 0.1, cases: 1, functions: 2, on_line: lineSid ? (t.walks.includes(lineSid) ? 1 : 0) : null })),
        line_known: !!lineSid,
      })
    }
    if (parts[5] === 'paths') {
      const test = u.searchParams.get('test')
      const t = fr.paths?.tests.find((x) => x.test === test)
      if (!fr.paths || test === null || !t) return json({ error: '运行里没有这个测试' }, 404)
      return json({ ...fr.paths, tests: [t] }, 200, { 'X-Pystepmap-Analysis': String(fr.info.analysis_id) })
    }
    if (parts[5] === 'uncovered') {
      if (failUncovered > 0) {
        failUncovered -= 1
        return json({ error: '分析结果或这次运行刚被清理掉了，请再取一次' }, 404)
      }
      return json({ outcomes: fr.uncovered, stale: false })
    }
    return missing(method, u.pathname)
  }

  async function fetchFn(input: string | URL | Request, init?: RequestInit): Promise<Response> {
    const url = String(input)
    const method = init?.method ?? 'GET'
    log.push(`${method} ${url}`)
    if (init?.body) bodies.push(`${method} ${url} ${String(init.body)}`)
    const resp = route(method, url, init)
    const gate = gates.find((g) => g.match(method, url))
    if (gate) {
      gates.splice(gates.indexOf(gate), 1)
      await gate.d.promise
    }
    return resp
  }

  return {
    projects,
    log,
    bodies,
    add,
    fetchFn,
    setOld: (b: boolean) => (old = b),
    setNoLineTests: (b: boolean) => (noLineTests = b),
    failRuns: (n: number) => (failRuns = n),
    failUncovered: (n: number) => (failUncovered = n),
    hold(method: string, pattern: RegExp) {
      const d = deferred()
      gates.push({ match: (m, url) => m === method && pattern.test(url), d })
      return () => d.resolve()
    },
  }
}

let be: ReturnType<typeof fakeBackend>
let S: State
let R: Runs

async function until(cond: () => boolean, what = '条件') {
  for (let i = 0; i < 400; i++) {
    if (cond()) return
    await new Promise((r) => setTimeout(r, 5))
  }
  throw new Error(`等不到：${what}`)
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** 读完、排完这个项目，切到"运行路径"页签，列出运行 */
async function openRunTab(id: string) {
  await until(() => !S.loading.value && S.projectId.value === id && S.view.value?.line.sid === S.lineSid.value && !!S.project.value, `${id} 读完`)
  S.tab.value = 'run'
  await until(() => R.runSupport.value !== 'unknown', '运行列表')
}

beforeEach(async () => {
  runSeq = 0
  be = fakeBackend()
  be.add('pa', 'alpha', 1)
  be.add('pb', 'beta', 2)
  vi.stubGlobal('fetch', be.fetchFn)
  vi.resetModules()
  S = await import('./state')
  R = await import('./runstate')
  S.analyzingPoll.ms = 10
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('旧版后端', () => {
  it('没有测试运行的接口：页签说不支持，不提示、不自己重试；打开项目时探一次，进页签、窗口回到前台时各再探一次', async () => {
    be.setOld(true)
    await S.init()
    const probes = () => be.log.filter((l) => l === 'GET /api/projects/pa/runs').length
    // 打开项目就探一次（顶栏的"运行全部测试"在哪个页签都要知道）
    await until(() => R.runSupport.value === 'unsupported', '打开项目时探一次')
    expect(probes()).toBe(1)
    await openRunTab('pa')
    await until(() => probes() === 2, '进页签再探一次')
    expect(R.runSupport.value).toBe('unsupported')
    await R.startRun()
    await sleep(40)
    expect(probes()).toBe(2)
    expect(S.notices.value).toEqual([])
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
    R.onRunPageVisible()
    await until(() => probes() === 3, '回到前台再探一次')
    S.tab.value = 'design'
    await sleep(5)
    S.tab.value = 'run'
    await until(() => probes() === 4, '重新进页签再探一次')
    await sleep(40)
    expect(probes()).toBe(4)
    expect(S.notices.value).toEqual([])
    // 后端换成新版（用户重启了 pystepmap serve）：再探就好了
    be.setOld(false)
    R.onRunPageVisible()
    await until(() => R.runSupport.value === 'ok', '新版后端')
  })
})

describe('运行测试', () => {
  it('启动 → 每隔一会儿查一次 → 跑完后自动选中新的运行', async () => {
    await S.init()
    await openRunTab('pa')
    expect(R.runs.value).toEqual([])
    await R.startRun()
    expect(be.bodies).toContain('POST /api/projects/pa/runs {}')
    expect(R.running.value).not.toBeNull()
    await until(() => R.running.value === null && R.runs.value.length === 1, '跑完')
    expect(be.log.filter((l) => /^GET \/api\/projects\/pa\/runs(\?|$)/.test(l)).length).toBeGreaterThanOrEqual(3)
    await until(() => R.selectedRun.value === R.runs.value[0].id, '选中新的运行')
    await until(() => R.runDetail.value?.id === R.runs.value[0].id, '运行详情')
    expect(be.log).toContain(`GET /api/projects/pa/runs/1?line=${encodeURIComponent(LINE)}`)
    // 跑完以后不再查
    const n = be.log.length
    await sleep(40)
    expect(be.log.slice(n).filter((l) => l === 'GET /api/projects/pa/runs')).toEqual([])
  })

  it('第一次运行这条命令：先弹确认；确认后带 confirm 和命令原文；确认框开着时命令被改了，后端再要确认就再弹一次', async () => {
    be.projects.get('pa')!.confirmed = false
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
    expect(R.confirmAsk.value).toEqual({ command: 'pytest', root: '/code/alpha' })
    // 确认框开着的时候，别的页面把命令改了
    be.projects.get('pa')!.command = 'pytest -x'
    await R.confirmRun()
    expect(be.bodies.at(-1)).toBe('POST /api/projects/pa/runs {"confirm":true,"command":"pytest"}')
    expect(R.confirmAsk.value).toEqual({ command: 'pytest -x', root: '/code/alpha' })
    expect(R.running.value).toBeNull()
    await R.confirmRun()
    expect(be.bodies.at(-1)).toBe('POST /api/projects/pa/runs {"confirm":true,"command":"pytest -x"}')
    expect(R.confirmAsk.value).toBeNull()
    expect(R.testCommand.value).toEqual({ command: 'pytest -x', confirmed: true })
    await until(() => R.running.value === null && R.runs.value.length === 1, '跑完')
  })

  it('不确认：不运行', async () => {
    be.projects.get('pa')!.confirmed = false
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    R.cancelConfirm()
    expect(R.confirmAsk.value).toBeNull()
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
  })

  it('还没填命令：打开命令对话框；保存后命令变了要重新确认', async () => {
    be.projects.get('pa')!.command = null
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    expect(R.commandDialog.value).toBe(true)
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
    await R.saveTestCommand('  .venv/bin/python -m pytest tests -q  ')
    expect(R.testCommand.value).toEqual({ command: '.venv/bin/python -m pytest tests -q', confirmed: false })
  })

  it('正在跑时查运行列表出错：不放弃，退避着重试（每次间隔变长），这期间说状态不明；恢复后照常', async () => {
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    be.projects.get('pa')!.running!.polls = 1
    be.failRuns(4)
    await until(() => R.runningUnknown.value, '状态不明')
    const t0 = Date.now()
    await until(() => R.running.value === null && R.runs.value.length === 1, '恢复后跑完')
    // 10 + 20 + 40 + 75 毫秒的退避（测试里的轮询间隔是 10 毫秒）
    expect(Date.now() - t0).toBeGreaterThanOrEqual(100)
    expect(R.runningUnknown.value).toBe(false)
    expect(S.notices.value.filter((n) => n.type === 'error')).toEqual([])
  })

  it('取消：发取消，跑完后选中那次取消了的运行，"正在取消"复位', async () => {
    await S.init()
    await openRunTab('pa')
    be.projects.get('pa')!.running = null
    await R.startRun()
    be.projects.get('pa')!.running!.polls = 5
    await R.cancelRun()
    expect(be.log).toContain('POST /api/projects/pa/runs/cancel')
    expect(R.cancelling.value).toBe(true)
    await until(() => R.running.value === null && R.runs.value.length === 1, '结束')
    expect(R.runs.value[0].status).toBe('cancelled')
    expect(R.cancelling.value).toBe(false)
    await until(() => R.selectedRun.value === R.runs.value[0].id, '选中')
  })

  it('取消时已经没有在跑（409 not_running）：重新查一次，不报错', async () => {
    await S.init()
    await openRunTab('pa')
    be.projects.get('pa')!.running = { phase: 'testing', polls: 100, cancel: false }
    await R.refreshRuns()
    be.projects.get('pa')!.running = null
    await R.cancelRun()
    expect(S.notices.value.filter((n) => n.type === 'error')).toEqual([])
    expect(R.running.value).toBeNull()
    expect(R.cancelling.value).toBe(false)
  })
})

describe('换项目、换功能线', () => {
  it('切项目时丢弃迟到的响应', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], {})]
    be.projects.get('pb')!.runs = [fakeRun(2, [failTest], {}), fakeRun(2, [okTest], {})]
    await S.init()
    await openRunTab('pa')
    const release = be.hold('GET', /\/pa\/runs$/)
    const late = R.refreshRuns()
    await S.selectProject('pb')
    await until(() => R.runs.value.length === 2, 'pb 的运行')
    release()
    await late
    await sleep(20)
    expect(S.projectId.value).toBe('pb')
    expect(R.runs.value.map((r) => r.id)).toEqual(be.projects.get('pb')!.runs.map((r) => r.info.id))
  })

  it('切功能线：选中的测试也走过新线时保留，否则清空；测试列表按新线的 on_line', async () => {
    const fr = fakeRun(1, [okTest, failTest], { [okTest.test]: [LINE, SEND_LINE], [T_FAIL]: [LINE] })
    be.projects.get('pa')!.runs = [fr]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    expect(R.visibleTests.value.map((t) => t.test)).toEqual([T_FAIL, okTest.test])
    await R.selectTest(okTest.test)
    await S.selectLine(SEND_LINE)
    await until(() => R.runDetail.value?.tests.some((t) => t.on_line === 0) === true, '新线的详情')
    expect(R.selectedTest.value).toBe(okTest.test)
    expect(R.visibleTests.value.map((t) => t.test)).toEqual([okTest.test])
    await R.selectTest(T_FAIL)
    await S.selectLine(LINE)
    await until(() => R.runDetail.value?.tests.every((t) => t.on_line === 1) === true, '回到第一条线')
    expect(R.selectedTest.value).toBe(T_FAIL)
    await S.selectLine(SEND_LINE)
    await until(() => R.selectedTest.value === null, '清空')
    expect(R.testPaths.value).toBeNull()
  })
})

describe('选测试、叠加层', () => {
  it('只取这个测试的路径（?test=），画布上标出走过的节点；失败的测试自动定位到停点（钻入调用的函数）', async () => {
    const fr = fakeRun(1, [okTest, failTest], { [okTest.test]: [LINE], [T_FAIL]: [LINE] })
    be.projects.get('pa')!.runs = [fr]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    await R.selectTest(okTest.test)
    expect(be.log).toContain(`GET /api/projects/pa/runs/${fr.info.id}/paths?test=${encodeURIComponent(okTest.test)}`)
    expect(be.log.some((l) => /\/paths$/.test(l))).toBe(false)
    const card = S.view.value!.cards[0].id
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.overlay.value?.unmatched).toBe(0)
    expect(R.canvasRunMarks.value.nodes.get(`${card}/loop:n5`)?.badge).toBe('×3 · 3 µs')
    expect(R.failure.value).toBeNull() // 通过的测试不算

    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_FAIL)
    await until(() => (S.canvasCommand.value?.seq ?? 0) > before, '定位')
    const drill = `${card}/step:n2>${check.sid}`
    expect(S.viewState.value.drills[drill]).toBe(true)
    expect(R.failure.value).toMatchObject({ kind: 'stop', functionKey: K.check, nodeId: 'n3', raisedIn: null })
    await until(() => R.canvasRunMarks.value.stops.has(`${drill}/end:raise1`), '红圈')
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: `${drill}/end:raise1` })
  })

  it('被接住的异常、测试自己的断言失败：没有红圈、不定位，说流程正常走完', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [handledTest], { [T_HANDLED]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_HANDLED)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.failure.value).toMatchObject({ kind: 'handled', ends: ['拒绝'] })
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
    await sleep(20)
    expect(S.canvasCommand.value?.seq ?? 0).toBe(before)
  })

  it('异常在判断节点调用的判断函数里抛出：红圈画在那个判断节点上并定位过去', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [condTest], { [T_COND]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_COND)
    const target = `${S.view.value!.cards[0].id}/cond:n3`
    await until(() => (S.canvasCommand.value?.seq ?? 0) > before, '定位')
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: target })
    expect(R.failure.value).toMatchObject({ kind: 'stop', functionKey: K.run, nodeId: 'n3', raisedIn: { functionKey: K.big, exc: 'RuntimeError' } })
    expect(R.canvasRunMarks.value.stops).toEqual(new Set([target]))
  })

  it('线上的函数以那种异常离开、没有记录到流程函数调用它，流程在这个测试里没跑过：不画红圈、不自动定位，只能"只定位到函数"', async () => {
    const T_DIRECT = 'tests/test_wf.py::test_post_direct'
    const directTest: TestRunJson = {
      test: T_DIRECT,
      outcome: 'failed',
      seconds: 0.01,
      cases: 1,
      functions: { [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }) },
      exc: 'KeyError',
      excs: ['KeyError'],
    }
    be.projects.get('pa')!.runs = [fakeRun(1, [directTest], { [T_DIRECT]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_DIRECT)
    expect(R.failure.value).toMatchObject({ kind: 'direct', functionKey: K.post, nodeId: 'n4', reveal: null })
    expect(R.failureReveal.value).toBeNull()
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
    expect(S.canvasCommand.value?.seq ?? 0).toBe(before)
    // "只定位到函数"（运行条上的按钮）：钻入定位、选中，但不画红圈
    await R.gotoRaise(K.post, 'n4')
    const target = `${S.view.value!.cards[0].id}/step:n6>${post.sid}/end:uncaught`
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: target })
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
  })

  it('同上，但流程在这个测试里跑过（调用经线程、并发任务，没记下）：红圈画在那个函数的停点上（只经执行过的函数钻入定位）', async () => {
    const T_DIRECT = 'tests/test_wf.py::test_post_thread'
    const threadTest: TestRunJson = {
      test: T_DIRECT,
      outcome: 'failed',
      seconds: 0.01,
      cases: 1,
      functions: {
        [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:n8|return|': 1 }),
        [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }),
      },
      exc: 'KeyError',
      excs: ['KeyError'],
    }
    be.projects.get('pa')!.runs = [fakeRun(1, [threadTest], { [T_DIRECT]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_DIRECT)
    expect(R.failure.value).toMatchObject({ kind: 'direct', functionKey: K.post, nodeId: 'n4', locate: null })
    const target = `${S.view.value!.cards[0].id}/step:n6>${post.sid}/end:uncaught`
    await until(() => (S.canvasCommand.value?.seq ?? 0) > before, '定位')
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: target })
    expect(R.canvasRunMarks.value.stops).toEqual(new Set([target]))
  })

  it('异常是从别的线抛出去的：这条线上不画红圈；"切到那条线"保留选中的测试并定位到停点', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [failTest], { [T_FAIL]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    await S.selectLine(SEND_LINE)
    await until(() => R.runDetail.value?.tests.every((t) => t.on_line === 0) === true, '子任务那条线的详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_FAIL)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.failure.value).toMatchObject({ kind: 'otherLine', line: LINE, exc: 'ValueError' })
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
    expect(R.failureReveal.value).toBeNull()
    await sleep(20)
    expect(S.canvasCommand.value?.seq ?? 0).toBe(before)
    await R.gotoFailureLine()
    expect(S.lineSid.value).toBe(LINE)
    expect(R.selectedTest.value).toBe(T_FAIL)
    const drill = `${S.view.value!.cards[0].id}/step:n2>${check.sid}`
    await until(() => S.canvasCommand.value?.type === 'focus' && S.canvasCommand.value.id === `${drill}/end:raise1`, '定位到停点')
    await until(() => R.runDetail.value?.tests.every((t) => t.on_line === 1) === true, '这条线的详情')
    expect(R.selectedTest.value).toBe(T_FAIL)
  })

  it('旧版本记录的运行（失败的测试没有异常类型）：不画红圈、不自动定位；列出的抛出处点了能定位并选中', async () => {
    const { exc: _e, excs: _es, ...oldFail } = failTest
    void _e
    void _es
    const old: TestRunJson = { ...oldFail, functions: Object.fromEntries(Object.entries(failTest.functions).map(([k, f]) => [k, { ...f, calls: undefined }])) }
    be.projects.get('pa')!.runs = [fakeRun(1, [old], { [T_FAIL]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    const before = S.canvasCommand.value?.seq ?? 0
    await R.selectTest(T_FAIL)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.failure.value).toMatchObject({ kind: 'unknown', old: true })
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
    expect(R.failureReveal.value).toBeNull()
    await sleep(20)
    expect(S.canvasCommand.value?.seq ?? 0).toBe(before)
    const f = R.failure.value as Extract<NonNullable<typeof R.failure.value>, { kind: 'unknown' }>
    const raise = f.raises.find((r) => r.functionKey === K.check)!
    await R.gotoRaise(raise.functionKey, raise.nodeId)
    const drill = `${S.view.value!.cards[0].id}/step:n2>${check.sid}`
    await until(() => S.selection.value?.id === `${drill}/end:raise1`, '选中抛出处')
    expect(R.canvasRunMarks.value.stops.size).toBe(0)
  })

  it('取没走到的结局出错：面板上说没取到，可以重试', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    be.failUncovered(1)
    await S.init()
    await openRunTab('pa')
    await until(() => R.uncoveredError.value !== '', '出错')
    expect(R.uncoveredError.value).toContain('刚被清理掉了')
    expect(R.uncovered.value).toBeNull()
    await R.retryUncovered()
    expect(R.uncoveredError.value).toBe('')
    expect(R.uncoveredLists.value.certain).toHaveLength(1)
  })

  it('没走到的结局：取这条线的；打开"在图上标出"、没选测试时画布上标出（折叠着的分支标在占位上）', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.uncovered.value !== null, '没走到的结局')
    expect(be.log).toContain(`GET /api/projects/pa/runs/1/uncovered?line=${encodeURIComponent(LINE)}`)
    expect(R.uncoveredLists.value.certain.map((o) => o.title)).toEqual(['拒绝'])
    expect(R.canvasRunMarks.value.uncovered.size).toBe(0)
    R.showUncovered.value = true
    const fold = S.view.value!.folds.find((f) => f.keys.includes('n4'))!
    expect(R.canvasRunMarks.value.uncovered.get(fold.id)).toBe('certain')
    // 选了测试时不标没走到的结局
    await R.selectTest(okTest.test)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.canvasRunMarks.value.uncovered.size).toBe(0)
    // 点一个结局：展开分支、选中并定位
    R.clearTest()
    await R.gotoOutcome(R.uncoveredLists.value.certain[0])
    await until(() => S.selection.value?.id === `${S.view.value!.cards[0].id}/end:n4`, '选中结局')
  })

  it('"流程设计"页签里看不到任何运行标注', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    await R.selectTest(okTest.test)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    S.tab.value = 'design'
    expect(R.canvasRunMarks.value.active).toBe(false)
    expect(R.canvasRunMarks.value.nodes.size).toBe(0)
    S.tab.value = 'run'
    expect(R.canvasRunMarks.value.active).toBe(true)
  })

  it('重新分析之后（运行用的是旧的分析）：面板说运行之后代码改过', async () => {
    be.projects.get('pa')!.runs = [fakeRun(0, [okTest], { [okTest.test]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    expect(R.runNote.value).toBe('这次运行之后代码改过（重新分析过），按节点标识对到现在的图上')
    await R.selectTest(okTest.test)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    expect(R.runNote.value).toBe('这次运行之后代码改过（重新分析过），按节点标识对到现在的图上')
  })

  it('看测试输出：取一次', async () => {
    await S.init()
    await openRunTab('pa')
    await R.loadOutput()
    expect(R.output.value).toEqual(['collected 2 items', '1 failed'])
  })
})

describe('运行中（测试命令在跑）', () => {
  /** 后端的进度：done 个跑完；live_line 是 LINE / SEND_LINE 时给这条线上走到过的节点 */
  function progressOf(done: number, lines: Record<string, { functions: number; total: number }>) {
    return (liveLine: string | null) => ({
      collected: 10,
      done,
      passed: done,
      failed: 0,
      skipped: 0,
      errors: 0,
      running: ['tests/test_wf.py::test_slow'],
      running_more: 0,
      recent: [{ test: 'tests/test_wf.py::test_ok', outcome: 'passed', seconds: 0.5 }],
      lines,
      ...(liveLine === LINE ? { nodes: { [K.run]: ['start', 'step:n2'] } } : liveLine === SEND_LINE ? { nodes: { [K.send]: ['start', 'step:n2'] } } : {}),
    })
  }

  async function startLive(progress: (liveLine: string | null) => unknown) {
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    const r = be.projects.get('pa')!.running!
    r.polls = 1_000_000
    r.progress = progress
    await until(() => R.liveProgress.value !== null, '运行中的进度')
  }

  it('每次查运行列表都带上画布上的功能线；面板有进度；画布上走到过的节点亮、别的暗；跑完后换成正常的结果', async () => {
    await startLive(progressOf(3, { [LINE]: { functions: 1, total: 5 } }))
    expect(be.log).toContain(`GET /api/projects/pa/runs?live_line=${encodeURIComponent(LINE)}`)
    expect(R.liveProgress.value).toMatchObject({ collected: 10, done: 3, running: ['tests/test_wf.py::test_slow'] })
    expect(R.liveView.value).toBe(true)
    const card = S.view.value!.cards[0].id
    await until(() => R.canvasRunMarks.value.live, '运行中的画布')
    const m = R.canvasRunMarks.value
    expect(m.active).toBe(true)
    expect([...m.nodes.keys()].sort()).toEqual([`${card}/start`, `${card}/step:n2`])
    expect(R.liveLines.value).toEqual({ [LINE]: { functions: 1, total: 5 } })
    // 设计页签里画布没有运行的标注
    S.tab.value = 'design'
    expect(R.canvasRunMarks.value.active).toBe(false)
    S.tab.value = 'run'
    // 跑完：进度没了，画布换成正常的样子（没选测试：不变暗）
    be.projects.get('pa')!.running!.polls = 0
    await until(() => R.running.value === null && R.runs.value.length === 1, '跑完')
    expect(R.liveProgress.value).toBeNull()
    expect(R.canvasRunMarks.value.live).toBe(false)
    expect(R.canvasRunMarks.value.active).toBe(false)
    expect(R.liveLines.value).toEqual({})
  })

  it('换功能线：马上再查一次、带上新的线；画布只用这条线的节点（新线的还没取到时什么都不标）', async () => {
    await startLive(progressOf(3, { [LINE]: { functions: 1, total: 5 }, [SEND_LINE]: { functions: 1, total: 1 } }))
    await until(() => R.canvasRunMarks.value.live, '运行中的画布')
    S.analyzingPoll.ms = 60_000 // 不再按时查：换线后的那一次只能是换线引起的
    await sleep(30)
    const release = be.hold('GET', new RegExp(`live_line=${encodeURIComponent(SEND_LINE)}`))
    await S.selectLine(SEND_LINE)
    await until(() => be.log.includes(`GET /api/projects/pa/runs?live_line=${encodeURIComponent(SEND_LINE)}`), '换线后马上查')
    expect(R.canvasRunMarks.value.nodes.size).toBe(0)
    expect(R.canvasRunMarks.value.active).toBe(false)
    release()
    await until(() => R.canvasRunMarks.value.live, '新线的节点')
    const card = S.view.value!.cards.find((c) => c.fn === K.send)!.id
    expect([...R.canvasRunMarks.value.nodes.keys()].sort()).toEqual([`${card}/start`, `${card}/step:n2`])
  })

  it('选了测试（上一次运行的）：画布照旧画那个测试的路径；开始新的运行时清掉选中的测试', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    await R.selectTest(okTest.test)
    await R.startRun()
    expect(R.selectedTest.value).toBeNull()
    const r = be.projects.get('pa')!.running!
    r.polls = 1_000_000
    r.progress = progressOf(1, {})
    await until(() => R.canvasRunMarks.value.live, '运行中的画布')
    await R.selectTest(okTest.test)
    await until(() => R.canvasRunMarks.value.active && !R.canvasRunMarks.value.live, '选中的测试')
    expect(R.liveView.value).toBe(false)
    expect(R.canvasRunMarks.value.nodes.get(`${S.view.value!.cards[0].id}/loop:n5`)?.badge).toBe('×3 · 3 µs')
    R.clearTest()
    expect(R.canvasRunMarks.value.live).toBe(true)
  })

  it('功能线上的小标记：最近一次刷新时多了函数的线另外标出；没变化的刷新不清掉', async () => {
    let lines: Record<string, { functions: number; total: number }> = { [LINE]: { functions: 1, total: 5 } }
    await startLive((l) => progressOf(3, lines)(l))
    expect(R.liveFresh.value).toEqual(new Set([LINE]))
    lines = { [LINE]: { functions: 1, total: 5 }, [SEND_LINE]: { functions: 1, total: 1 } }
    await until(() => R.liveFresh.value.has(SEND_LINE), '多了一条线')
    expect(R.liveFresh.value).toEqual(new Set([SEND_LINE]))
    const n = be.log.length
    await until(() => be.log.length > n + 2, '再查两次')
    expect(R.liveFresh.value).toEqual(new Set([SEND_LINE]))
  })

  it('旧版后端（没有 progress）：面板照旧只说阶段和已用时间，画布不标', async () => {
    await S.init()
    await openRunTab('pa')
    await R.startRun()
    be.projects.get('pa')!.running!.polls = 1_000_000
    await until(() => R.running.value?.phase === 'testing', '在跑测试')
    expect(R.running.value?.progress).toBeNull()
    expect(R.liveProgress.value).toBeNull()
    expect(R.liveView.value).toBe(false)
    expect(R.canvasRunMarks.value.active).toBe(false)
  })
})

describe('只跑一条功能线的测试', () => {
  it('还没有全量运行：说后端给的原因，不能跑；跑完全部测试后名单有 N 个；只跑这条线：POST 带 line，跑完后列表里标着范围、选中它', async () => {
    be.projects.get('pa')!.next = fakeRun(1, [okTest, failTest], { [okTest.test]: [LINE], [T_FAIL]: [LINE, SEND_LINE] })
    await S.init()
    await openRunTab('pa')
    await until(() => R.lineTestsNow.value !== null, '名单')
    expect(be.log).toContain(`GET /api/projects/pa/line-tests?line=${encodeURIComponent(LINE)}`)
    expect(R.lineTestsNow.value).toMatchObject({ ok: false, code: 'no_full_run' })
    expect(R.lineTestsNow.value!.reason).toContain('先运行一次全部测试')
    await R.startLineRun()
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
    // 运行全部测试（顶栏）
    await R.startRun()
    expect(be.bodies.at(-1)).toBe('POST /api/projects/pa/runs {}')
    expect(R.runningScope.value).toEqual({ line: null, title: '', tests: 0 })
    await until(() => R.running.value === null && R.runs.value.length === 1, '全量跑完')
    expect(R.runningScope.value).toBeNull()
    expect(R.runs.value[0].scope).toBeNull()
    // 跑完后重取名单
    await until(() => R.lineTestsNow.value?.ok === true, '有名单了')
    expect(R.lineTestsNow.value!.tests).toEqual([okTest.test, T_FAIL])
    // 只跑这条线
    be.projects.get('pa')!.next = fakeRun(1, [okTest, failTest], { [okTest.test]: [LINE], [T_FAIL]: [LINE, SEND_LINE] })
    await R.startLineRun()
    expect(be.bodies.at(-1)).toBe(`POST /api/projects/pa/runs ${JSON.stringify({ line: LINE })}`)
    expect(R.runningScope.value).toEqual({ line: LINE, title: S.displayBySid.value.get(LINE)!.titleFull, tests: 2 })
    await until(() => R.running.value === null && R.runs.value.length === 2, '这条线跑完')
    expect(R.runs.value[0].scope).toMatchObject({ line: LINE, tests: 2 })
    await until(() => R.selectedRun.value === R.runs.value[0].id, '选中只跑了这条线的那次')
    await until(() => R.uncovered.value !== null, '这条线没走到的结局照常取')
    expect(be.log).toContain(`GET /api/projects/pa/runs/${R.runs.value[0].id}/uncovered?line=${encodeURIComponent(LINE)}`)
    expect(R.selectedElsewhere.value).toBeNull()
  })

  it('只跑了别的线的运行：列出来但不默认选；选了也不取这条线的测试、没走到的结局，画布不标；换到那条线时照常，再换回来选回全量的', async () => {
    const full = fakeRun(1, [okTest], { [okTest.test]: [LINE, SEND_LINE] })
    const other = fakeRun(1, [okTest], { [okTest.test]: [LINE, SEND_LINE] })
    other.info = { ...other.info, scope: { line: SEND_LINE, line_title: '通知', tests: 1, base_run: full.info.id } }
    be.projects.get('pa')!.runs = [other, full]
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value?.id === full.info.id, '默认选全量的')
    expect(R.selectedRun.value).toBe(full.info.id)
    R.selectRun(other.info.id)
    expect(R.selectedElsewhere.value).toMatchObject({ line: SEND_LINE, line_title: '通知' })
    await sleep(30)
    expect(be.log.some((l) => l.includes(`/runs/${other.info.id}`))).toBe(false)
    expect(R.runDetail.value).toBeNull()
    expect(R.uncovered.value).toBeNull()
    expect(R.uncoveredError.value).toBe('')
    R.showUncovered.value = true
    expect(R.canvasRunMarks.value.active).toBe(false)
    expect(R.canvasRunMarks.value.uncovered.size).toBe(0)
    await R.retryUncovered()
    expect(be.log.some((l) => l.includes(`/runs/${other.info.id}/uncovered`))).toBe(false)
    // 换到那条线：这次运行就是那条线的，照常取
    await S.selectLine(SEND_LINE)
    expect(R.selectedRun.value).toBe(other.info.id)
    await until(() => R.uncovered.value !== null, '那条线没走到的结局')
    expect(be.log).toContain(`GET /api/projects/pa/runs/${other.info.id}/uncovered?line=${encodeURIComponent(SEND_LINE)}`)
    // 换回来：选中的只跑了别的线，换成这条线默认的（全量）
    await S.selectLine(LINE)
    await until(() => R.selectedRun.value === full.info.id, '选回全量的')
    expect(R.selectedElsewhere.value).toBeNull()
  })

  it('最近的运行都只跑了别的线的测试：什么都不选（不当作这条线没有覆盖）', async () => {
    const other = fakeRun(1, [okTest], { [okTest.test]: [SEND_LINE] })
    other.info = { ...other.info, scope: { line: SEND_LINE, line_title: '通知', tests: 1, base_run: null } }
    be.projects.get('pa')!.runs = [other]
    await S.init()
    await openRunTab('pa')
    await sleep(30)
    expect(R.runs.value).toHaveLength(1)
    expect(R.selectedRun.value).toBeNull()
    expect(be.log.some((l) => l.includes(`/runs/${other.info.id}`))).toBe(false)
  })

  it('命令还没确认过：确认框说只跑这条线的几个测试；确认后带 confirm、命令原文和 line', async () => {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    be.projects.get('pa')!.confirmed = false
    await S.init()
    await openRunTab('pa')
    await until(() => R.lineTestsNow.value?.ok === true, '名单')
    await R.startLineRun()
    const title = S.displayBySid.value.get(LINE)!.titleFull
    expect(R.confirmAsk.value).toEqual({ command: 'pytest', root: '/code/alpha', scope: { line: LINE, title, tests: 1 } })
    await R.confirmRun()
    expect(be.bodies.at(-1)).toBe(`POST /api/projects/pa/runs ${JSON.stringify({ confirm: true, command: 'pytest', line: LINE })}`)
    expect(R.runningScope.value).toMatchObject({ line: LINE, tests: 1 })
  })

  it('名单在开始前变了（后端 409 no_tests）：说后端的原因，重新取名单', async () => {
    const full = fakeRun(1, [okTest], { [okTest.test]: [LINE] })
    be.projects.get('pa')!.runs = [full]
    await S.init()
    await openRunTab('pa')
    await until(() => R.lineTestsNow.value?.ok === true, '名单')
    full.tests = full.tests.map((t) => ({ ...t, walks: [] }))
    await R.startLineRun()
    expect(R.running.value).toBeNull()
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'warning' })
    expect(S.notices.value.at(-1)!.text).toContain('没有测试走过这条功能线')
    await until(() => R.lineTestsNow.value?.ok === false, '重新取名单')
    expect(R.lineTestsNow.value!.code).toBe('no_tests')
  })

  it('后端还没有 line-tests 接口：不显示"运行这条线的测试"（unsupported），这个项目不再问；运行全部测试照常', async () => {
    be.setNoLineTests(true)
    await S.init()
    await openRunTab('pa')
    await until(() => R.lineRunSupport.value === 'unsupported', '不支持')
    await S.selectLine(SEND_LINE)
    await sleep(30)
    expect(be.log.filter((l) => l.includes('/line-tests')).length).toBe(1)
    expect(S.notices.value).toEqual([])
    await R.startRun()
    expect(be.bodies.at(-1)).toBe('POST /api/projects/pa/runs {}')
  })

  it('全部测试跑完时人在"流程设计"页签：说一声结果', async () => {
    await S.init()
    await until(() => R.runSupport.value === 'ok', '打开项目时就读了运行列表')
    expect(S.tab.value).toBe('design')
    await R.startRun()
    await until(() => R.running.value === null && R.runs.value.length === 1, '跑完')
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'success' })
    expect(S.notices.value.at(-1)!.text).toContain('全部测试跑完了：1 通过')
  })
})

describe('允许 AI 经 MCP 运行单条功能线的测试', () => {
  it('一勾就存（PUT /mcp-tests），项目列表里跟着改', async () => {
    await S.init()
    await openRunTab('pa')
    expect(S.currentProject.value?.mcp_tests).toBe(false)
    await R.setMcpTests(true)
    expect(be.bodies).toContain('PUT /api/projects/pa/mcp-tests {"allow":true}')
    expect(S.currentProject.value?.mcp_tests).toBe(true)
    expect(R.mcpSaving.value).toBe(false)
    await R.setMcpTests(false)
    expect(S.currentProject.value?.mcp_tests).toBe(false)
  })

  it('存不了：抛出后端的说明，列表不改', async () => {
    be.setNoLineTests(true)
    await S.init()
    await openRunTab('pa')
    await expect(R.setMcpTests(true)).rejects.toMatchObject({ status: 404 })
    expect(S.currentProject.value?.mcp_tests).toBe(false)
    expect(R.mcpSaving.value).toBe(false)
  })
})

describe('回放', () => {
  async function selected() {
    be.projects.get('pa')!.runs = [fakeRun(1, [okTest], { [okTest.test]: [LINE] })]
    R.replaySpeed.ms = 5
    R.motion.reduced = () => false
    await S.init()
    await openRunTab('pa')
    await until(() => R.runDetail.value !== null, '运行详情')
    await R.selectTest(okTest.test)
    await until(() => R.canvasRunMarks.value.active, '叠加层')
    return S.view.value!.cards[0].id
  }

  it('一步步点亮（走到的元素不在屏幕上时平移过去），可以暂停、继续；放完换回这个测试完整的样子', async () => {
    const card = await selected()
    R.startReplay()
    expect(R.replay.value?.index).toBe(0)
    expect(R.canvasRunMarks.value.current).toBe(`${card}/start`)
    expect([...R.canvasRunMarks.value.nodes.keys()]).toEqual([`${card}/start`])
    expect(S.canvasCommand.value).toMatchObject({ type: 'peek', id: `${card}/start` })
    // 详情栏照旧按整个测试说
    expect(R.testRunMarks.value.nodes.get(`${card}/loop:n5`)?.badge).toBe('×3 · 3 µs')
    await until(() => (R.replay.value?.index ?? 0) >= 2, '往下走')
    R.pauseReplay()
    const at = R.replay.value!.index
    await sleep(30)
    expect(R.replay.value!.index).toBe(at)
    expect(R.replay.value!.playing).toBe(false)
    R.resumeReplay()
    await until(() => R.replay.value === null, '放完')
    expect(R.canvasRunMarks.value.current).toBeNull()
    expect(R.canvasRunMarks.value.nodes.get(`${card}/loop:n5`)?.badge).toBe('×3 · 3 µs')
  })

  it('换了测试、换了线、点了停止：停下，画布换回原来的样子', async () => {
    await selected()
    R.replaySpeed.ms = 60_000
    R.startReplay()
    expect(R.replay.value).not.toBeNull()
    R.stopReplay()
    expect(R.replay.value).toBeNull()
    R.startReplay()
    R.clearTest()
    await sleep(5)
    expect(R.replay.value).toBeNull()
    await R.selectTest(okTest.test)
    R.startReplay()
    expect(R.replay.value).not.toBeNull()
    S.expandAll()
    await until(() => R.replay.value === null, '画布展开状态变了就停')
  })

  it('系统设置了"减少动态效果"：不放，说一声（画布上就是走完后的样子）', async () => {
    await selected()
    R.motion.reduced = () => true
    R.startReplay()
    expect(R.replay.value).toBeNull()
    expect(S.notices.value.at(-1)!.text).toContain('减少动态效果')
    expect(R.canvasRunMarks.value.nodes.get(`${S.view.value!.cards[0].id}/loop:n5`)?.badge).toBe('×3 · 3 µs')
  })
})
