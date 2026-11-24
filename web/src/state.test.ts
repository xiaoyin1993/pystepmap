// 界面状态（state.ts）和平台后端之间的来回：用一个假的后端（换掉 fetch）把真的 state.ts、api.ts、排版跑一遍。
// 每个测试重新导入 state.ts，状态互不影响。
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'
import type { OverrideMap, ProjectInfo } from './api'
import { K, mini, miniLine } from './graph/fixtures/mini'
import { drillChainOf, setOpen } from './graph'
import { CK, at, chainProject, into } from './graph/fixtures/chain'

type State = typeof import('./state')
type LoadProgress = import('./state').LoadProgress

const TRANSFER = readFileSync(new URL('./graph/fixtures/transfer.json', import.meta.url), 'utf8')
const MINI = JSON.stringify(mini)
/** mini 去掉第一条功能线（重新分析后它没了） */
const MINI_WITHOUT_FIRST = JSON.stringify({ ...mini, lines: mini.lines.slice(1) })

function deferred<T = void>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

const summary = (lines: number) => ({
  functions: 10,
  lines,
  common_tasks: 0,
  files: 2,
  unresolved_calls: 1,
  guessed_calls: 0,
  unreachable: 0,
  dead_ends: 0,
  skipped_files: [],
  source_roots: ['.'],
  rules: [],
  excluded: {},
  has_entries: true,
  seconds: 0.1,
})

interface FakeProject {
  info: ProjectInfo
  graph: string
  overrides: OverrideMap
  /** 分析配置 */
  config: Record<string, unknown>
  /** 下一次分析换上的图 */
  next?: string
  /** GET /status 的 stale（没有时是没过期） */
  stale?: unknown
  /** GET /diff 的 diff（has_diff 为真时才有意义） */
  diff?: unknown
  /** 收到 GET /diff 时先做的事（模拟别的进程恰好在这时分析完） */
  onDiff?: () => void
  /** GET /requirements 的回应；没有时是旧版后端（404"没有这个接口"） */
  requirements?: unknown
  /** 变更记录（新的在前）；没有时是旧版后端（404"没有这个接口"） */
  reviews?: FakeReview[]
}

/** 一条变更记录：概要、存下的差异、文字、当时的图（没有时是旧版本存的记录，取图 404 no_graph） */
interface FakeReview {
  info: { id: number; acked_at: string; head_id: number; head_at: string; base_id: number | null; base_at: string | null; summary: unknown; graph_size: number | null }
  diff: unknown
  text: string
  graph?: string
}

const FRESH = { checked: true, stale: false, changed: [], added: [], removed: [], unreadable: [], lines: [], total: { changed: 0, added: 0, removed: 0, unreadable: 0 }, note: '' }

/** 假的平台后端：按路径回应；hold() 把匹配的下一次请求挂起，直到手动放行 */
function fakeBackend() {
  let analysisSeq = 0
  const projects = new Map<string, FakeProject>()
  const gates: { match: (method: string, url: string) => boolean; d: ReturnType<typeof deferred<void>> }[] = []
  const log: string[] = []
  let down = false
  /** 新版后端（有 /status、/diff）；默认是旧版，这些接口 404 */
  let stage5 = false

  const add = (id: string, name: string, graph: string) => {
    const a = ++analysisSeq
    projects.set(id, {
      info: {
        id,
        name,
        root: `/code/${name}`,
        created_at: '2026-11-16T04:00:00+00:00',
        updated_at: '2026-11-16T04:00:00+00:00',
        analyzing: false,
        analysis: {
          id: a,
          created_at: '2026-11-16T04:00:00+00:00',
          engine_version: '0.1.0',
          graph_size: graph.length,
          summary: summary(JSON.parse(graph).lines.length),
          base_id: null,
          has_diff: false,
        },
        mcp_tests: false,
      },
      graph,
      overrides: {},
      config: {},
    })
  }

  const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
    new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })

  function route(method: string, url: string, init?: RequestInit): Response {
    const u = new URL(url, 'http://x')
    const parts = u.pathname.split('/').filter(Boolean).map(decodeURIComponent) // api, projects, id, …
    if (parts[1] !== 'projects') return json({ error: '没有这个接口' }, 404)
    if (parts.length === 2 && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { root: string; name?: string }
      if ([...projects.values()].some((p) => p.info.root === body.root)) {
        return json({ error: `这个目录已经添加过：${body.root}`, code: 'duplicate', project_id: 'pa' }, 409)
      }
      add('pnew', body.name ?? 'new', TRANSFER)
      const p = projects.get('pnew')!
      p.info = { ...p.info, root: body.root }
      return json(p.info, 201)
    }
    if (parts.length === 2) return json([...projects.values()].map((p) => p.info))
    const p = projects.get(parts[2])
    if (!p) return json({ error: `项目不存在（可能已经删除了）：${parts[2]}`, code: 'not_found' }, 404)
    const what = parts[3] ?? ''
    if (what === '' && method === 'DELETE') {
      projects.delete(parts[2])
      return new Response(null, { status: 204 })
    }
    if (what === 'graph') {
      return new Response(p.graph, {
        headers: { 'Content-Type': 'application/json', 'X-Pystepmap-Analysis': String(p.info.analysis!.id), 'X-Pystepmap-Graph-Size': String(new TextEncoder().encode(p.graph).length) },
      })
    }
    if (what === 'overrides' && method === 'GET') return json(p.overrides)
    if (what === 'overrides' && method === 'PUT') {
      const sid = u.searchParams.get('sid')!
      const body = JSON.parse(String(init?.body)) as { title: string | null; group: string | null }
      const entry: { title?: string; group?: string } = {}
      if (body.title) entry.title = body.title
      if (body.group) entry.group = body.group
      p.overrides = { ...p.overrides }
      if (entry.title || entry.group) p.overrides[sid] = entry
      else delete p.overrides[sid]
      return json(p.overrides)
    }
    if (what === 'config' && method === 'GET') return json(p.config)
    if (what === 'config' && method === 'PUT') {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>
      if ('bogus' in body) return json({ error: '分析配置不合法：不认识的配置项 bogus' }, 400)
      p.config = body
      return json(body)
    }
    if (what === 'status' && stage5 && method === 'GET') {
      const a = p.info.analysis
      return json({ analysis_id: a?.id ?? null, analyzing: p.info.analyzing, stale: p.stale ?? FRESH, base_id: a?.base_id ?? null, has_diff: a?.has_diff ?? false })
    }
    if (what === 'diff' && stage5 && parts[4] === 'ack' && method === 'POST') {
      if (Number(u.searchParams.get('analysis')) !== p.info.analysis!.id) return json({ error: '分析结果已经更新', code: 'moved' }, 409)
      p.info = { ...p.info, analysis: { ...p.info.analysis!, base_id: null, has_diff: false } }
      p.diff = undefined
      return json({ head: p.info.analysis, base: null, diff: null })
    }
    if (what === 'diff' && stage5 && parts.length === 4 && method === 'GET') {
      p.onDiff?.()
      const a = p.info.analysis!
      return json({ head: a, base: a.base_id ? { ...a, id: a.base_id, base_id: null, has_diff: false } : null, diff: a.has_diff ? (p.diff ?? null) : null })
    }
    if (what === 'requirements' && method === 'GET' && p.requirements !== undefined) return json(p.requirements)
    if (what === 'reviews' && method === 'GET' && p.reviews !== undefined) {
      if (parts.length === 4) return json({ reviews: p.reviews.map((r) => r.info) })
      const r = p.reviews.find((x) => x.info.id === Number(parts[4]))
      if (!r) return json({ error: `没有这条变更记录：${parts[4]}（只留最近 10 条，可能已经清掉了）` }, 404)
      if (parts[5] === 'graph') {
        if (!r.graph) return json({ error: '这条变更记录没有存当时的图（旧版本存的记录），只能看文字明细', code: 'no_graph' }, 404)
        return new Response(r.graph, { headers: { 'Content-Type': 'application/json', 'X-Pystepmap-Analysis': String(r.info.head_id) } })
      }
      return json({ ...r.info, diff: r.diff, text: r.text })
    }
    if (what === 'analyze') {
      if (p.next) p.graph = p.next
      const a = ++analysisSeq
      p.stale = undefined
      p.info = { ...p.info, analysis: { ...p.info.analysis!, id: a, summary: summary(JSON.parse(p.graph).lines.length), base_id: null, has_diff: false } }
      return json(p.info)
    }
    return json({ error: '没有这个接口' }, 404)
  }

  /** 请求到达时就处理（后端的状态当时就变），挂起的只是响应在路上 */
  async function fetchFn(input: string | URL | Request, init?: RequestInit): Promise<Response> {
    const url = String(input)
    const method = init?.method ?? 'GET'
    log.push(`${method} ${url}`)
    if (down) throw new TypeError('Failed to fetch')
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
    add,
    fetchFn,
    setDown: (b: boolean) => (down = b),
    /** 换成新版后端（有 /status、/diff、/diff/ack） */
    setStage5: (b: boolean) => (stage5 = b),
    /** 挂起下一次匹配的请求；返回放行函数 */
    hold(method: string, pattern: RegExp) {
      const d = deferred()
      gates.push({ match: (m, url) => m === method && pattern.test(url), d })
      return () => d.resolve()
    },
  }
}

let be: ReturnType<typeof fakeBackend>
let S: State

/** 等到 cond 成立（异步排版、读取都在微任务和定时器里） */
async function until(cond: () => boolean, what = '条件') {
  for (let i = 0; i < 400; i++) {
    if (cond()) return
    await new Promise((r) => setTimeout(r, 5))
  }
  throw new Error(`等不到：${what}`)
}

/** 读完、排完（loading 结束且画布上是这个项目的这条线） */
async function settled(id: string) {
  await until(() => !S.loading.value && S.projectId.value === id && (S.project.value === null || S.view.value?.line.sid === S.lineSid.value), `${id} 读完`)
}

beforeEach(async () => {
  be = fakeBackend()
  be.add('pa', 'alpha', MINI)
  be.add('pb', 'beta', TRANSFER)
  vi.stubGlobal('fetch', be.fetchFn)
  vi.resetModules()
  S = await import('./state')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('连接后端', () => {
  it('启动：列项目，选第一个，读它的图和手工项，画上第一条功能线', async () => {
    be.projects.get('pa')!.overrides = { [miniLine.sid]: { title: '受理请求', group: '入口' }, 'line:gone.py::x': { title: '孤儿' } }
    await S.init()
    await settled('pa')
    expect(S.backend.value).toBe('ok')
    expect(S.projects.value.map((p) => p.id)).toEqual(['pa', 'pb'])
    expect(S.lineSid.value).toBe(miniLine.sid)
    expect(S.currentDisplay.value).toMatchObject({ title: '受理请求', group: '入口', titleEdited: true })
    // 孤儿项不显示
    expect(S.displayLines.value.map((d) => d.title)).not.toContain('孤儿')
    expect(S.view.value?.line.sid).toBe(miniLine.sid)
    expect(S.graphOf.value).toEqual({ project: 'pa', analysis: 1 })
  })

  it('后端没启动：backend 为 down、没有画布；启动后点重试就连上', async () => {
    be.setDown(true)
    await S.init()
    expect(S.backend.value).toBe('down')
    expect(S.backendError.value).toBe('连不上平台后端')
    expect(S.project.value).toBeNull()
    be.setDown(false)
    await S.retry()
    await settled('pa')
    expect(S.backend.value).toBe('ok')
    expect(S.project.value).not.toBeNull()
  })

  it('一个项目都没有：没有选中的项目，也不报错', async () => {
    be.projects.clear()
    await S.init()
    expect(S.backend.value).toBe('ok')
    expect(S.projectId.value).toBe('')
    expect(S.loadFailure.value).toBeNull()
  })

  it('还没有项目时窗口回到前台：刷新项目列表，别处（MCP、另一个页面）添加的项目不用刷新页面就出现、被选上', async () => {
    be.projects.clear()
    await S.init()
    const lists = () => be.log.filter((l) => l === 'GET /api/projects').length
    expect(lists()).toBe(1)
    be.add('pm', 'mcp-added', MINI)
    S.onPageVisible()
    await settled('pm')
    expect(S.projects.value.map((p) => p.id)).toEqual(['pm'])
    expect(S.lineSid.value).toBe(miniLine.sid)
    expect(lists()).toBe(2)
    // 选上以后回到前台照旧只查过期，不再刷新列表
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(lists()).toBe(2)
  })

  it('还没有项目时：回到前台 3 秒内重复触发只刷新一次列表', async () => {
    be.projects.clear()
    await S.init()
    S.onPageVisible()
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(be.log.filter((l) => l === 'GET /api/projects')).toHaveLength(2)
    expect(S.projectId.value).toBe('')
  })

  it('读到旧版的图（功能线没有 sid）：读失败，提示重新分析；重新分析后正常显示', async () => {
    const old = JSON.parse(MINI) as { lines: { sid?: string }[] }
    old.lines.forEach((l) => delete l.sid)
    be.projects.get('pa')!.graph = JSON.stringify(old)
    be.projects.get('pa')!.next = MINI
    await S.init()
    await settled('pa')
    expect(S.loadFailure.value).toMatchObject({ project: 'pa', kind: 'old-graph' })
    expect(S.loadFailure.value?.message).toContain('重新分析')
    expect(S.project.value).toBeNull()
    await S.reanalyze()
    await settled('pa')
    expect(S.loadFailure.value).toBeNull()
    expect(S.lineSid.value).toBe(miniLine.sid)
  })
})

describe('换项目：慢的读取落不到后选的项目上', () => {
  it('先选 B 再选 A：B 的图后到，画布上仍是 A', async () => {
    await S.init()
    await settled('pa')
    const releaseB = be.hold('GET', /\/pb\/graph/)
    const toB = S.selectProject('pb')
    const toA = S.selectProject('pa')
    await toA
    await settled('pa')
    releaseB()
    await toB
    await new Promise((r) => setTimeout(r, 20))
    expect(S.projectId.value).toBe('pa')
    expect(S.graphOf.value?.project).toBe('pa')
    expect(S.project.value?.lines.length).toBe(mini.lines.length)
    expect(S.view.value?.line.sid).toBe(miniLine.sid)
    expect(S.loading.value).toBe(false)
  })

  it('B 的图到了、手工项慢：换到 A 之后 B 的手工项到了也不采用', async () => {
    be.projects.get('pb')!.overrides = { 'line:transfer_workflow.py::TransferWorkflow.run': { title: 'B 的手工标题' } }
    be.projects.get('pa')!.overrides = { [miniLine.sid]: { title: 'A 的手工标题' } }
    await S.init()
    await settled('pa')
    const releaseOverridesB = be.hold('GET', /\/pb\/overrides/)
    const toB = S.selectProject('pb')
    await until(() => be.log.some((l) => l.includes('/pb/graph')), '读 B 的图')
    const toA = S.selectProject('pa')
    await toA
    await settled('pa')
    releaseOverridesB()
    await toB
    await new Promise((r) => setTimeout(r, 20))
    expect(S.projectId.value).toBe('pa')
    expect(S.overrides.value).toEqual({ [miniLine.sid]: { title: 'A 的手工标题' } })
    expect(S.currentDisplay.value?.title).toBe('A 的手工标题')
  })

  it('同一个项目：先发的读取（旧的一次分析）后到，不盖掉重新分析之后读到的新图', async () => {
    be.projects.get('pa')!.next = MINI_WITHOUT_FIRST
    await S.init()
    await settled('pa')
    await S.selectProject('pb')
    await settled('pb')
    // 别的页面重新分析过 pa（图没变，分析 id 变了）：缓存对不上，换回 pa 时要重新下载
    const pa = be.projects.get('pa')!
    pa.info = { ...pa.info, analysis: { ...pa.info.analysis!, id: 40 } }
    await S.refreshProjects()
    const slowOld = be.hold('GET', /\/pa\/graph/)
    const select = S.selectProject('pa')
    await until(() => be.log.filter((l) => l.includes('/pa/graph')).length === 2, '读 pa 的旧图')
    // 马上点了重新分析：新图先到
    await S.reanalyze('pa')
    await settled('pa')
    expect(S.project.value?.lines).toHaveLength(mini.lines.length - 1)
    slowOld()
    await select
    await new Promise((r) => setTimeout(r, 20))
    expect(S.graphOf.value).toEqual({ project: 'pa', analysis: 3 })
    expect(S.project.value?.lines).toHaveLength(mini.lines.length - 1)
  })

  it('读图时按收到的字节报告进度（总量取 X-Pystepmap-Graph-Size），读完清掉', async () => {
    await S.init()
    await settled('pa')
    const seen: (LoadProgress | null)[] = []
    const stop = watch(S.progress, (v) => seen.push(v), { flush: 'sync' })
    await S.selectProject('pb')
    await settled('pb')
    stop()
    const size = new TextEncoder().encode(TRANSFER).length
    expect(seen.filter(Boolean)).toContainEqual({ received: size, total: size })
    expect(seen.filter(Boolean)[0]).toEqual({ received: 0, total: size })
    expect(S.progress.value).toBeNull()
    expect(S.project.value?.lines[0].sid).toBe('line:transfer_workflow.py::TransferWorkflow.run')
  })

  it('读过的图按分析 id 缓存：换回来不再下载；重新分析之后重新下载', async () => {
    await S.init()
    await settled('pa')
    await S.selectProject('pb')
    await settled('pb')
    await S.selectProject('pa')
    await settled('pa')
    expect(be.log.filter((l) => l === 'GET /api/projects/pa/graph')).toHaveLength(1)
    expect(be.log.filter((l) => l === 'GET /api/projects/pa/overrides')).toHaveLength(2)
    await S.reanalyze('pa')
    await settled('pa')
    expect(be.log.filter((l) => l === 'GET /api/projects/pa/graph')).toHaveLength(2)
  })
})

describe('重新分析', () => {
  it('完成后换上新图：功能线和展开状态照旧，选中的节点照旧；提示分析完成', async () => {
    await S.init()
    await settled('pa')
    const sid = S.lineSid.value
    const card = S.view.value!.cards.find((c) => c.expandable && !c.expanded)!
    S.toggleCard(card)
    await until(() => S.view.value?.cards.find((c) => c.id === card.id)?.expanded === true, '卡片展开')
    const node = S.view.value!.nodes[0]
    S.select('node', node.id)
    const state = S.viewState.value
    const busy = be.hold('POST', /\/pa\/analyze/)
    const done = S.reanalyze()
    expect(S.reanalyzing.value).toEqual(['pa'])
    busy()
    await done
    await settled('pa')
    expect(S.reanalyzing.value).toEqual([])
    expect(S.graphOf.value?.analysis).toBe(3)
    expect(S.lineSid.value).toBe(sid)
    expect(S.viewState.value).toBe(state)
    expect(S.view.value?.cards.find((c) => c.id === card.id)?.expanded).toBe(true)
    expect(S.selection.value).toEqual({ kind: 'node', id: node.id })
    expect(S.layoutReason.value).toBe('state')
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'success' })
    expect(S.notices.value.at(-1)?.text).toContain('分析完成')
  })

  it('原来看的功能线没了：回到列表第一条，并提示；手工项还在库里', async () => {
    be.projects.get('pa')!.next = MINI_WITHOUT_FIRST
    be.projects.get('pa')!.overrides = { [miniLine.sid]: { title: '受理请求' } }
    await S.init()
    await settled('pa')
    S.viewState.value = setOpen(S.viewState.value, 'cards', 'x', true)
    await S.reanalyze()
    await settled('pa')
    expect(S.lineSid.value).toBe(mini.lines[1].sid)
    expect(S.viewState.value).toEqual({ cards: {}, folds: {}, drills: {} })
    expect(S.layoutReason.value).toBe('line')
    const texts = S.notices.value.map((n) => n.text)
    expect(texts.some((t) => t.includes('「受理请求」在新的分析结果里没有了，已回到列表第一条'))).toBe(true)
    // 孤儿项不显示，也没有被删
    expect(S.displayLines.value.map((d) => d.title)).not.toContain('受理请求')
    expect(be.projects.get('pa')!.overrides).toEqual({ [miniLine.sid]: { title: '受理请求' } })
    expect(be.log.some((l) => l.startsWith('PUT'))).toBe(false)
  })

  it('分析完了、读新图失败：画布清空并说明原因；点重试读回来，功能线和展开状态照旧', async () => {
    await S.init()
    await settled('pa')
    const sid = S.lineSid.value
    const card = S.view.value!.cards.find((c) => c.expandable && !c.expanded)!
    S.toggleCard(card)
    await until(() => S.view.value?.cards.find((c) => c.id === card.id)?.expanded === true, '卡片展开')
    const state = S.viewState.value
    let failOnce = true
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (failOnce && url.endsWith('/pa/graph')) {
        failOnce = false
        return new Response(JSON.stringify({ error: '服务器内部错误：OSError: 磁盘读不了' }), { status: 500 })
      }
      return be.fetchFn(url, init)
    })
    await S.reanalyze()
    await settled('pa')
    expect(S.loadFailure.value).toMatchObject({ project: 'pa', kind: 'http', status: 500, message: '服务器内部错误：OSError: 磁盘读不了' })
    expect(S.project.value).toBeNull()
    expect(S.view.value).toBeNull()
    await S.retry()
    await settled('pa')
    expect(S.loadFailure.value).toBeNull()
    expect(S.lineSid.value).toBe(sid)
    expect(S.viewState.value).toBe(state)
    expect(S.view.value?.cards.find((c) => c.id === card.id)?.expanded).toBe(true)
    // 画布原来是空的：按换线重新定位视口
    expect(S.layoutReason.value).toBe('line')
  })

  it('分析中换到别的项目：分析完成后不把那个项目的图换上来', async () => {
    await S.init()
    await settled('pa')
    const busy = be.hold('POST', /\/pa\/analyze/)
    const done = S.reanalyze('pa')
    await S.selectProject('pb')
    await settled('pb')
    busy()
    await done
    await new Promise((r) => setTimeout(r, 20))
    expect(S.projectId.value).toBe('pb')
    expect(S.graphOf.value?.project).toBe('pb')
    // 列表里的分析信息更新了
    expect(S.projects.value.find((p) => p.id === 'pa')?.analysis?.id).toBe(3)
  })

  it('正在分析（409 busy）：提示等它完成，图不动', async () => {
    await S.init()
    await settled('pa')
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? new Response(JSON.stringify({ error: '正在分析', code: 'busy' }), { status: 409 })
        : be.fetchFn(url, init),
    )
    const graph = S.project.value
    await S.reanalyze()
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'warning' })
    expect(S.notices.value.at(-1)?.text).toContain('正在分析')
    expect(S.project.value).toBe(graph)
  })

  it('同一个项目连点两次：只发一次请求', async () => {
    await S.init()
    await settled('pa')
    const busy = be.hold('POST', /\/pa\/analyze/)
    const a = S.reanalyze()
    const b = S.reanalyze()
    busy()
    await Promise.all([a, b])
    expect(be.log.filter((l) => l.startsWith('POST'))).toHaveLength(1)
  })
})

describe('手工标题和分组', () => {
  it('保存后立即生效（列表、分组、标题区都用它）；两项清空 = 恢复原样', async () => {
    await S.init()
    await settled('pa')
    await S.saveOverride(miniLine.sid, { title: '受理请求', group: '入口' })
    expect(S.currentDisplay.value).toMatchObject({ title: '受理请求', group: '入口', titleEdited: true, groupEdited: true })
    expect(be.log.at(-1)).toBe(`PUT /api/projects/pa/overrides?sid=${encodeURIComponent(miniLine.sid)}`)
    await S.saveOverride(miniLine.sid, { title: null, group: null })
    expect(S.currentDisplay.value).toMatchObject({ title: miniLine.title, titleEdited: false, groupEdited: false })
    expect(S.overrides.value).toEqual({})
  })

  it('保存的结果回来时已经换了项目：不动后一个项目的手工项', async () => {
    be.projects.get('pb')!.overrides = { 'line:transfer_workflow.py::TransferWorkflow.run': { title: 'B 的' } }
    await S.init()
    await settled('pa')
    const release = be.hold('PUT', /\/pa\/overrides/)
    const saving = S.saveOverride(miniLine.sid, { title: 'A 的新标题', group: null })
    await S.selectProject('pb')
    await settled('pb')
    release()
    await saving
    expect(S.overrides.value).toEqual({ 'line:transfer_workflow.py::TransferWorkflow.run': { title: 'B 的' } })
    // 存到的是 A
    expect(be.projects.get('pa')!.overrides).toEqual({ [miniLine.sid]: { title: 'A 的新标题' } })
  })

  it('同一条连着存两次、先发的后到：以后一次为准', async () => {
    await S.init()
    await settled('pa')
    const first = be.hold('PUT', /\/pa\/overrides/)
    const a = S.saveOverride(miniLine.sid, { title: '第一次', group: null })
    const b = S.saveOverride(miniLine.sid, { title: '第二次', group: null })
    await b
    first()
    await a
    expect(S.currentDisplay.value?.title).toBe('第二次')
  })

  it('保存失败：抛出后端的中文说明，本地不变', async () => {
    await S.init()
    await settled('pa')
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      init?.method === 'PUT'
        ? new Response(JSON.stringify({ error: '请求参数不对：title 太长了（最多 200 个字）' }), { status: 400 })
        : be.fetchFn(url, init),
    )
    await expect(S.saveOverride(miniLine.sid, { title: 'x'.repeat(300), group: null })).rejects.toThrow('最多 200 个字')
    expect(S.overrides.value).toEqual({})
  })
})

describe('添加、删除项目', () => {
  it('添加成功：列表里多一个，切到它；重复的目录抛出后端的说明和已有项目的 id', async () => {
    await S.init()
    await settled('pa')
    const info = await S.addProject({ root: '/code/new', name: '新项目' })
    expect(info.id).toBe('pnew')
    await settled('pnew')
    expect(S.projects.value.map((p) => p.id)).toEqual(['pa', 'pb', 'pnew'])
    expect(S.project.value?.lines[0].sid).toBe('line:transfer_workflow.py::TransferWorkflow.run')
    await expect(S.addProject({ root: '/code/alpha' })).rejects.toMatchObject({ code: 'duplicate', projectId: 'pa' })
    expect(S.projects.value).toHaveLength(3)
  })

  it('删除正在看的项目：切到剩下的第一个；删光了就没有选中的项目', async () => {
    await S.init()
    await settled('pa')
    await S.removeProject('pa')
    await settled('pb')
    expect(S.projects.value.map((p) => p.id)).toEqual(['pb'])
    expect(S.projectId.value).toBe('pb')
    await S.removeProject('pb')
    expect(S.projectId.value).toBe('')
    expect(S.project.value).toBeNull()
    expect(S.view.value).toBeNull()
  })

  it('读的时候把它删了：读完的图不会出现', async () => {
    await S.init()
    await settled('pa')
    const release = be.hold('GET', /\/pb\/graph/)
    const loading = S.selectProject('pb')
    await until(() => be.log.some((l) => l.includes('/pb/graph')), '读 B')
    await S.removeProject('pa')
    // pa 不是正在看的（正在看 pb）；再删 pb
    await S.removeProject('pb')
    release()
    await loading
    expect(S.projectId.value).toBe('')
    expect(S.project.value).toBeNull()
  })
})

/** 假的地址栏（window、location、history）。state.ts 导入时看有没有 window，所以装好后重新导入。 */
async function withAddressBar(hash: string) {
  const loc = { hash, pathname: '/', search: '' }
  vi.stubGlobal('location', loc)
  vi.stubGlobal('history', {
    replaceState: (_state: unknown, _title: string, url: string) => {
      loc.hash = url.startsWith('#') ? url : ''
    },
  })
  vi.stubGlobal('window', { addEventListener: () => undefined })
  vi.resetModules()
  S = await import('./state')
  return loc
}

describe('第一次读项目的途中：要看的功能线不丢', () => {
  /** 不是列表第一条的功能线 */
  const want = mini.lines[2].sid
  const wantHash = `#/pa/${encodeURIComponent(want)}`

  it('地址栏指定了功能线，读图途中点重新分析：停在那条线上，地址栏一直带着它', async () => {
    expect(want).not.toBe(miniLine.sid)
    const loc = await withAddressBar(wantHash)
    const slow = be.hold('GET', /\/pa\/graph/)
    const starting = S.init()
    await until(() => be.log.includes('GET /api/projects/pa/graph'), '开始读 pa 的图')
    expect(S.projectId.value).toBe('pa')
    expect(S.lineSid.value).toBe('')
    expect(loc.hash).toBe(wantHash)
    await S.reanalyze()
    await settled('pa')
    expect(S.lineSid.value).toBe(want)
    expect(S.view.value?.line.sid).toBe(want)
    expect(loc.hash).toBe(wantHash)
    // 先发的那次读取后到：作废，不改功能线
    slow()
    await starting
    await new Promise((r) => setTimeout(r, 20))
    expect(S.lineSid.value).toBe(want)
  })

  it('地址栏指定了功能线，第一次读失败：地址栏还带着它，点重试读回来就是那条', async () => {
    const loc = await withAddressBar(wantHash)
    let failOnce = true
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (failOnce && url.endsWith('/pa/graph')) {
        failOnce = false
        return new Response(JSON.stringify({ error: '服务器内部错误：OSError: 磁盘读不了' }), { status: 500 })
      }
      return be.fetchFn(url, init)
    })
    await S.init()
    await settled('pa')
    expect(S.loadFailure.value).toMatchObject({ project: 'pa', status: 500 })
    expect(loc.hash).toBe(wantHash)
    await S.retry()
    await settled('pa')
    expect(S.loadFailure.value).toBeNull()
    expect(S.lineSid.value).toBe(want)
    expect(loc.hash).toBe(wantHash)
  })

  it('换项目时指定了功能线（不经过地址栏），读的途中点重新分析：仍然是那条', async () => {
    await S.init()
    await settled('pa')
    await S.selectProject('pb')
    await settled('pb')
    const slow = be.hold('GET', /\/pa\/overrides/)
    const switching = S.selectProject('pa', want)
    await until(() => be.log.filter((l) => l === 'GET /api/projects/pa/overrides').length === 2, '开始读 pa')
    await S.reanalyze('pa')
    await settled('pa')
    expect(S.lineSid.value).toBe(want)
    slow()
    await switching
    await new Promise((r) => setTimeout(r, 20))
    expect(S.lineSid.value).toBe(want)
  })
})

describe('分析期间项目被删除', () => {
  it('分析的结果是"项目不存在"：提示项目已经被删除了（不说上一次的结果还在），从列表里去掉，切到剩下的项目', async () => {
    await S.init()
    await settled('pa')
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST' && url.endsWith('/pa/analyze')) {
        be.projects.delete('pa')
        return new Response(JSON.stringify({ error: '项目在分析期间被删除了：pa', code: 'not_found' }), { status: 404 })
      }
      return be.fetchFn(url, init)
    })
    await S.reanalyze()
    const last = S.notices.value.at(-1)!
    expect(last.text).toContain('项目已经被删除了')
    expect(last.text).not.toContain('上一次的分析结果还在')
    expect(S.projects.value.map((p) => p.id)).toEqual(['pb'])
    await settled('pb')
    expect(S.graphOf.value?.project).toBe('pb')
  })

  it('分析完成的回应到达之前项目已经删掉了：不把它加回列表，也不报分析完成', async () => {
    await S.init()
    await settled('pa')
    const answer = be.hold('POST', /\/pa\/analyze/)
    const analyzing = S.reanalyze('pa')
    await S.removeProject('pa')
    await settled('pb')
    answer()
    await analyzing
    await new Promise((r) => setTimeout(r, 20))
    expect(S.projects.value.map((p) => p.id)).toEqual(['pb'])
    expect(S.projectId.value).toBe('pb')
    expect(S.notices.value.some((n) => n.text.includes('分析完成'))).toBe(false)
  })
})

describe('分析状态的自动刷新', () => {
  it('打开页面时项目正在分析：每隔一会儿刷新列表；分析完就换上新图（功能线、展开状态照旧），然后停止刷新', async () => {
    const pa = be.projects.get('pa')!
    pa.info = { ...pa.info, analyzing: true }
    S.analyzingPoll.ms = 10
    await S.init()
    await settled('pa')
    const card = S.view.value!.cards.find((c) => c.expandable && !c.expanded)!
    S.toggleCard(card)
    await until(() => S.view.value?.cards.find((c) => c.id === card.id)?.expanded === true, '卡片展开')
    const state = S.viewState.value
    const lists = () => be.log.filter((l) => l === 'GET /api/projects').length
    await until(() => lists() >= 4, '自动刷新了几次')
    expect(S.graphOf.value?.analysis).toBe(1)
    // 分析完成（别的页面、刷新之前点的那次）
    pa.info = { ...pa.info, analyzing: false, analysis: { ...pa.info.analysis!, id: 77 } }
    await until(() => S.graphOf.value?.analysis === 77, '换上新图')
    await settled('pa')
    expect(S.lineSid.value).toBe(miniLine.sid)
    expect(S.viewState.value).toBe(state)
    expect(S.view.value?.cards.find((c) => c.id === card.id)?.expanded).toBe(true)
    expect(S.notices.value.at(-1)?.text).toContain('新的分析结果')
    const n = lists()
    await new Promise((r) => setTimeout(r, 60))
    expect(lists()).toBe(n)
  })

  it('刚分析完时多取一次列表：看到不在分析了、列表里却还是旧的那次分析时，不错过新的分析', async () => {
    const pa = be.projects.get('pa')!
    pa.info = { ...pa.info, analyzing: true }
    S.analyzingPoll.ms = 50
    await S.init()
    await settled('pa')
    // 取到列表的这一刻：已经不在分析了，列表里却还是旧的那次分析（比如两次取之间别的进程又存了一次）
    pa.info = { ...pa.info, analyzing: false }
    await until(() => S.projects.value.find((p) => p.id === 'pa')?.analyzing === false, '看到分析结束')
    pa.info = { ...pa.info, analysis: { ...pa.info.analysis!, id: 78 } }
    await until(() => S.graphOf.value?.analysis === 78, '换上新图')
    await settled('pa')
    const n = be.log.filter((l) => l === 'GET /api/projects').length
    await new Promise((r) => setTimeout(r, 150))
    expect(be.log.filter((l) => l === 'GET /api/projects')).toHaveLength(n)
  })

  it('连不上后端就停止刷新', async () => {
    const pa = be.projects.get('pa')!
    pa.info = { ...pa.info, analyzing: true }
    S.analyzingPoll.ms = 10
    await S.init()
    await settled('pa')
    be.setDown(true)
    await until(() => S.backend.value === 'down', '发现连不上')
    const n = be.log.length
    await new Promise((r) => setTimeout(r, 60))
    expect(be.log.length).toBe(n)
  })

  it('没有项目在分析时不刷新；这个页面自己点的重新分析只换一次图', async () => {
    S.analyzingPoll.ms = 10
    await S.init()
    await settled('pa')
    await new Promise((r) => setTimeout(r, 50))
    expect(be.log.filter((l) => l === 'GET /api/projects')).toHaveLength(1)
    await S.reanalyze()
    await settled('pa')
    await new Promise((r) => setTimeout(r, 30))
    expect(be.log.filter((l) => l === 'GET /api/projects/pa/graph')).toHaveLength(2)
  })
})

describe('分析配置', () => {
  it('读、改项目的分析配置；后端说不合法时抛出它的中文说明；改完不自动重新分析', async () => {
    await S.init()
    await settled('pa')
    expect(await S.readConfig('pa')).toEqual({})
    expect(await S.writeConfig('pa', { exclude: ['gen'] })).toEqual({ exclude: ['gen'] })
    expect(await S.readConfig('pa')).toEqual({ exclude: ['gen'] })
    await expect(S.writeConfig('pa', { bogus: 1 })).rejects.toMatchObject({ kind: 'http', status: 400, message: '分析配置不合法：不认识的配置项 bogus' })
    expect(await S.readConfig('pa')).toEqual({ exclude: ['gen'] })
    expect(be.log.some((l) => l.startsWith('POST'))).toBe(false)
    S.openConfig()
    expect(S.configFor.value).toBe('pa')
  })
})

describe('等得久了说一声', () => {
  it('重新分析超过时限还没完成：标成"等得久"，完成时取消；很快完成的不标', async () => {
    const slow = await import('./slow')
    slow.slowAfter.ms = 20
    await S.init()
    await settled('pa')
    const answer = be.hold('POST', /\/pa\/analyze/)
    const analyzing = S.reanalyze('pa')
    expect(S.slowReanalyzing.value).toEqual([])
    await until(() => S.slowReanalyzing.value.includes('pa'), '标成等得久')
    answer()
    await analyzing
    expect(S.slowReanalyzing.value).toEqual([])
    slow.slowAfter.ms = 10_000
    await S.reanalyze('pa')
    expect(S.slowReanalyzing.value).toEqual([])
  })

  it('添加项目超过时限：标成"等得久"，失败时也取消', async () => {
    const slow = await import('./slow')
    slow.slowAfter.ms = 20
    await S.init()
    await settled('pa')
    const answer = be.hold('POST', /\/api\/projects$/)
    const adding = S.addProject({ root: '/code/alpha' })
    await until(() => S.addingSlow.value, '标成等得久')
    answer()
    await expect(adding).rejects.toMatchObject({ code: 'duplicate' })
    expect(S.addingSlow.value).toBe(false)
  })
})

/** mini 的一份流程差异：接收请求的"校验参数"原来叫"检查参数" */
const MINI_DIFF = {
  version: 1,
  functions: {
    'api.py::entry': {
      status: 'changed',
      sid: 'api.py::entry',
      title: '接收请求',
      file: 'api.py',
      new_key: K.entry,
      old_key: K.entry,
      nodes: [
        {
          status: 'changed',
          type: 'step',
          new_id: 'n2',
          new_sid: 'step:n2',
          new_title: '校验参数',
          new_subtitle: 'helper',
          new_line: 3,
          old_sid: 'step:n2',
          old_title: '检查参数',
          old_subtitle: 'helper',
          old_line: 3,
          after: '',
        },
      ],
      edges: [],
    },
  },
  lines: [
    { status: 'changed', sid: miniLine.sid, title: '接收请求', group: 'test', new_id: K.entry, functions: ['api.py::entry'], tasks_added: [], tasks_removed: [], root: 'api.py::entry' },
  ],
}

/** pa 这次分析和对比基准比有 MINI_DIFF 这些变化 */
function withDiff(id = 'pa', analysis?: number) {
  const p = be.projects.get(id)!
  p.info = { ...p.info, analysis: { ...p.info.analysis!, id: analysis ?? p.info.analysis!.id, base_id: 99, has_diff: true } }
  p.diff = MINI_DIFF
}

const count = (line: string) => be.log.filter((l) => l === line).length

describe('过期提示和流程差异', () => {
  it('旧版后端（/status、/diff 都 404）：打开、切项目各查一次，当作没有信息，不提示、不重试', async () => {
    await S.init()
    await settled('pa')
    await new Promise((r) => setTimeout(r, 20))
    expect(count('GET /api/projects/pa/status')).toBe(1)
    expect(S.projectStatus.value).toBeNull()
    expect(S.staleInfo.value.show).toBe(false)
    expect(S.notices.value).toEqual([])
    expect(S.loadFailure.value).toBeNull()
    expect(be.log.some((l) => l.includes('/diff'))).toBe(false)
    await S.selectProject('pb')
    await settled('pb')
    await new Promise((r) => setTimeout(r, 20))
    expect(count('GET /api/projects/pb/status')).toBe(1)
    expect(S.notices.value).toEqual([])
  })

  it('窗口回到前台：3 秒内重复触发只查一次', async () => {
    be.setStage5(true)
    await S.init()
    await settled('pa')
    await until(() => S.projectStatus.value !== null, '查过一次')
    // 刚打开时查过：马上回到前台不再查
    S.onPageVisible()
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(count('GET /api/projects/pa/status')).toBe(1)
    S.statusThrottle.ms = 30
    await new Promise((r) => setTimeout(r, 40))
    S.onPageVisible()
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(count('GET /api/projects/pa/status')).toBe(2)
  })

  it('代码改过：过期条显示改了几个文件、涉及的功能线；重新分析之后再查一次，不再过期', async () => {
    be.setStage5(true)
    be.projects.get('pa')!.stale = { ...FRESH, stale: true, changed: ['api.py'], lines: [miniLine.sid], total: { ...FRESH.total, changed: 1 } }
    await S.init()
    await settled('pa')
    await until(() => S.staleInfo.value.show, '过期条')
    expect(S.staleInfo.value.text).toBe('代码已变更，图可能过期：改 1 个文件')
    expect(S.staleInfo.value.lineSids).toEqual(new Set([miniLine.sid]))
    await S.reanalyze()
    await until(() => !S.staleInfo.value.show, '过期条消失')
    expect(count('GET /api/projects/pa/status')).toBe(2)
  })

  it('别的进程做了新的分析（analysis_id 变大）：自动载入新的图和差异，保持选中的线，提示已载入', async () => {
    be.setStage5(true)
    S.statusThrottle.ms = 0
    await S.init()
    await settled('pa')
    const want = mini.lines[2].sid
    await S.selectLine(want)
    await settled('pa')
    // 比如 Claude Code 通过 pystepmap mcp 重新分析了
    withDiff('pa', 9)
    S.onPageVisible()
    await until(() => S.graphOf.value?.analysis === 9, '换上新图')
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    expect(S.lineSid.value).toBe(want)
    expect(S.diffView.value.lineStatus.get(miniLine.sid)).toBe('changed')
    // 换图后取差异、换图后的检查又想取一次：同一次分析只取一次
    await new Promise((r) => setTimeout(r, 30))
    expect(count('GET /api/projects/pa/diff')).toBe(1)
    const n = S.notices.value.find((x) => x.text.includes('已载入'))
    expect(n).toMatchObject({ type: 'info', corner: true })
    expect(n?.text).toContain('新的分析结果')
  })

  it('流程差异的标记只在"流程设计"页签：切到"运行路径"时标记和差异栏的处数都没有，切回来又有', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    const root = S.view.value!.cards[0].id
    expect(S.marks.value.nodes.get(`${root}/step:n2`)).toBe('changed')
    S.tab.value = 'run'
    expect(S.marks.value.nodes.size).toBe(0)
    expect(S.marks.value.cards.size).toBe(0)
    expect(S.changeStops.value).toEqual([])
    S.tab.value = 'design'
    expect(S.marks.value.nodes.get(`${root}/step:n2`)).toBe('changed')
    expect(S.changeStops.value).toHaveLength(1)
  })

  it('有差异：取一次 /diff，画布上标出改动的节点；标为已看（带上看的那次分析）之后 diffView 为空、不再取', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    const root = S.view.value!.cards[0].id
    expect(S.marks.value.nodes.get(`${root}/step:n2`)).toBe('changed')
    expect(S.changeStops.value.map((s) => s.text)).toEqual(['改动步骤：检查参数 → 校验参数'])
    await S.ackChanges()
    expect(be.log).toContain('POST /api/projects/pa/diff/ack?analysis=1')
    expect(S.diffView.value.lineStatus.size).toBe(0)
    expect(S.diffView.value.nodeMarks.size).toBe(0)
    expect(S.marks.value.nodes.size).toBe(0)
    expect(S.changeStops.value).toEqual([])
    expect(S.projects.value.find((p) => p.id === 'pa')?.analysis?.has_diff).toBe(false)
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'success' })
    S.statusThrottle.ms = 0
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(count('GET /api/projects/pa/diff')).toBe(1)
  })

  it('标为已看时已经有了更新的分析（409 moved）：提示先看新的变化，重新载入', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    withDiff('pa', 12)
    await S.ackChanges()
    expect(S.notices.value.some((n) => n.type === 'warning' && n.text === '分析结果已经更新，请先看新的变化')).toBe(true)
    await until(() => S.graphOf.value?.analysis === 12, '换上新图')
    await until(() => S.diffView.value.lineStatus.size === 1, '新的差异')
    expect(be.projects.get('pa')!.info.analysis?.has_diff).toBe(true)
  })

  it('差异和图不是同一次分析（取差异时别的进程刚分析完）：图和差异都按新的那次重取', async () => {
    be.setStage5(true)
    withDiff()
    const pa = be.projects.get('pa')!
    let once = true
    pa.onDiff = () => {
      if (!once) return
      once = false
      withDiff('pa', 5)
    }
    await S.init()
    await settled('pa')
    await until(() => S.graphOf.value?.analysis === 5, '按新的那次重取图')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    expect(count('GET /api/projects/pa/graph')).toBe(2)
    expect(S.diffResult.value?.head.id).toBe(5)
  })

  it('重新分析超时（504，code timeout）：提示用服务端的原文', async () => {
    await S.init()
    await settled('pa')
    const text = '分析超过 120 秒，已中止：/code/alpha（项目太大，或者配置里的正则太慢；可以用环境变量 PYSTEPMAP_ANALYZE_TIMEOUT 调整秒数）'
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      init?.method === 'POST' ? new Response(JSON.stringify({ error: text, code: 'timeout' }), { status: 504 }) : be.fetchFn(url, init),
    )
    await S.reanalyze()
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'error' })
    expect(S.notices.value.at(-1)?.text).toContain(text)
    expect(S.notices.value.at(-1)?.text).not.toContain('连不上')
  })

  it('标为已看时正在分析（409 busy）：提示用服务端的原文，差异照旧', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      init?.method === 'POST' ? new Response(JSON.stringify({ error: '正在分析，等分析完再标为看过', code: 'busy' }), { status: 409 }) : be.fetchFn(url, init),
    )
    await S.ackChanges()
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'warning', text: '正在分析，等分析完再标为看过' })
    expect(S.diffView.value.lineStatus.size).toBe(1)
    expect(S.acking.value).toBe(false)
  })

  it('提示分两个位置：右上角只显示"已载入新的分析结果"，其余在下方', () => {
    S.notify('info', '已载入', true)
    S.notify('success', '分析完成')
    const [corner, normal] = S.notices.value
    expect([S.noticeShownIn(corner, true), S.noticeShownIn(corner, false)]).toEqual([true, false])
    expect([S.noticeShownIn(normal, true), S.noticeShownIn(normal, false)]).toEqual([false, true])
  })

  it('换了项目之后才回来的 /status、/diff：不采用', async () => {
    be.setStage5(true)
    withDiff()
    be.projects.get('pa')!.stale = { ...FRESH, stale: true, changed: ['api.py'], lines: [miniLine.sid], total: { ...FRESH.total, changed: 1 } }
    const lateStatus = be.hold('GET', /\/pa\/status/)
    const lateDiff = be.hold('GET', /\/pa\/diff/)
    const starting = S.init()
    await until(() => be.log.includes('GET /api/projects/pa/diff'), '开始取 pa 的差异')
    await S.selectProject('pb')
    await settled('pb')
    lateStatus()
    lateDiff()
    await starting
    await new Promise((r) => setTimeout(r, 30))
    expect(S.projectId.value).toBe('pb')
    expect(S.staleInfo.value.show).toBe(false)
    expect(S.diffResult.value).toBeNull()
    expect(S.diffView.value.lineStatus.size).toBe(0)
  })

  it('/status 出错（500）：不提示，当作没有信息', async () => {
    be.setStage5(true)
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      url.endsWith('/status') ? new Response(JSON.stringify({ error: '服务器内部错误：OSError' }), { status: 500 }) : be.fetchFn(url, init),
    )
    await S.init()
    await settled('pa')
    await new Promise((r) => setTimeout(r, 20))
    expect(S.projectStatus.value).toBeNull()
    expect(S.notices.value).toEqual([])
    expect(S.loadFailure.value).toBeNull()
  })

  it('后端断了又连上、点重试：也查一次过期', async () => {
    be.setStage5(true)
    be.setDown(true)
    await S.init()
    expect(S.backend.value).toBe('down')
    be.setDown(false)
    await S.retry()
    await settled('pa')
    await until(() => count('GET /api/projects/pa/status') >= 1, '查过期')
    // 已经有画布时点重试（例如连不上之后又连上）也查
    const n = count('GET /api/projects/pa/status')
    await S.retry()
    await until(() => count('GET /api/projects/pa/status') === n + 1, '重试时再查')
  })

  it('换线、差异变了（标为已看）、换上新图时，"第几处"回到没定位', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.changeStops.value.length === 1, '差异')
    await S.nextStop(1)
    expect(S.stopIndex.value).toBe(0)
    await S.ackChanges()
    expect(S.stopIndex.value).toBe(-1)
    withDiff('pa', 30)
    S.statusThrottle.ms = 0
    S.onPageVisible()
    await until(() => S.changeStops.value.length === 1, '新的差异')
    await S.nextStop(1)
    expect(S.stopIndex.value).toBe(0)
    withDiff('pa', 31)
    S.onPageVisible()
    await until(() => S.graphOf.value?.analysis === 31, '换上新图')
    expect(S.stopIndex.value).toBe(-1)
  })

  it('标为已看按项目记：一个项目还在标的时候，换到另一个项目照样能标', async () => {
    be.setStage5(true)
    withDiff('pa')
    withDiff('pb')
    be.projects.get('pb')!.diff = { ...MINI_DIFF, functions: {}, lines: [] }
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    const slow = be.hold('POST', /\/pa\/diff\/ack/)
    const ackA = S.ackChanges()
    expect(S.acking.value).toBe(true)
    await S.selectProject('pb')
    await settled('pb')
    await until(() => S.diffResult.value?.project === 'pb', 'pb 的差异')
    expect(S.acking.value).toBe(false)
    await S.ackChanges()
    expect(be.log).toContain('POST /api/projects/pb/diff/ack?analysis=2')
    slow()
    await ackA
    // pa 的结果回来时已经换了项目：不动 pb 的差异，但 pa 在项目列表里也记成没有差异了（"· 有流程变化"去掉）
    expect(S.diffResult.value?.project).toBe('pb')
    expect(S.projects.value.find((p) => p.id === 'pa')?.analysis?.has_diff).toBe(false)
  })

  it('确认框打开时记下看的是哪次分析：确认前换上了新的分析就不标，提示先看新的变化', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.diffView.value.lineStatus.size === 1, '差异')
    const target = S.ackTarget()
    expect(target).toEqual({ project: 'pa', analysis: 1 })
    // 确认框开着的时候，别处又分析了一次，页面换上了新的
    withDiff('pa', 40)
    S.statusThrottle.ms = 0
    S.onPageVisible()
    await until(() => S.graphOf.value?.analysis === 40, '换上新图')
    await S.ackChanges(target)
    expect(be.log.some((l) => l.includes('/diff/ack'))).toBe(false)
    expect(S.notices.value.at(-1)).toMatchObject({ type: 'warning', text: '分析结果已经更新，请先看新的变化' })
    await until(() => S.diffView.value.lineStatus.size === 1, '新的差异还在')
  })

  it('确认框打开后换了项目：不标别的项目', async () => {
    be.setStage5(true)
    withDiff('pa')
    await S.init()
    await settled('pa')
    const target = S.ackTarget()
    await S.selectProject('pb')
    await settled('pb')
    await S.ackChanges(target)
    expect(be.log.some((l) => l.includes('/diff/ack'))).toBe(false)
    expect(S.notices.value.at(-1)?.text).toContain('换了项目')
  })

  it('上一处 / 下一处：定位到那一处（需要时展开），选中它', async () => {
    be.setStage5(true)
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.changeStops.value.length === 1, '差异')
    await S.nextStop(1)
    const root = S.view.value!.cards[0].id
    expect(S.stopIndex.value).toBe(0)
    expect(S.selection.value).toEqual({ kind: 'node', id: `${root}/step:n2` })
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: `${root}/step:n2` })
    await S.nextStop(-1)
    expect(S.stopIndex.value).toBe(0)
  })
})

describe('需求', () => {
  // examples/transfer 的 GET /requirements（graph/fixtures/requirements.json，和 transfer.json 是同一个项目）：R001 对得上；
  // R002 改出了少了、分类、多了、走向；R003 读不懂；R004 功能线还不存在
  type ReqJson = { requirements: { id: string; file: string; design_line: string | null; title: string; flow: string }[] }
  const REQS_TEXT = readFileSync(new URL('./graph/fixtures/requirements.json', import.meta.url), 'utf8')
  const reqs = () => JSON.parse(REQS_TEXT) as ReqJson
  const fileOf = (id: string) => reqs().requirements.find((r) => r.id === id)!.file
  const designSid = (id: string) => `line:${reqs().requirements.find((r) => r.id === id)!.design_line}`
  const TRANSFER_LINE = 'line:transfer_workflow.py::TransferWorkflow.run'
  const reqGets = (id: string) => be.log.filter((l) => l === `GET /api/projects/${id}/requirements`).length

  /** 这个项目的需求取到了 */
  const reqsLoaded = (id: string) => until(() => S.reqData.value?.project === id && !S.reqLoading.value, `${id} 的需求`)
  /** 画布上是这条需求的设计图 */
  const designShown = (id: string) => until(() => S.view.value?.line.sid === designSid(id), `${id} 的设计图`)

  async function openTransfer() {
    be.projects.get('pb')!.requirements = reqs()
    await S.init()
    await settled('pa')
    await S.selectProject('pb')
    await settled('pb')
    await reqsLoaded('pb')
  }

  it('旧版后端没有这个接口：没有需求（列表上不出现"需求"），不报错、不提示', async () => {
    await S.init()
    await settled('pa')
    await until(() => reqGets('pa') === 1 && !S.reqLoading.value, '取过一次需求')
    expect(S.requirements.value).toEqual([])
    expect(S.reqData.value).toBeNull()
    expect(S.reqError.value).toBe('')
    expect(S.notices.value).toEqual([])
    expect(S.reqMode.value).toBe(false)
  })

  it('页面开着之后才写的第一条需求：窗口回到前台时重取，列表上出现；3 秒内不重复取', async () => {
    be.projects.get('pb')!.requirements = { requirements: [] }
    await S.init()
    await settled('pa')
    await S.selectProject('pb')
    await settled('pb')
    await reqsLoaded('pb')
    expect(S.requirements.value).toEqual([])
    const before = reqGets('pb')
    be.projects.get('pb')!.requirements = reqs() // 在编辑器 / AI 那边写了需求文件
    S.onPageVisible()
    await until(() => S.requirements.value.length === 4, '回到前台后取到需求')
    S.onPageVisible()
    await new Promise((r) => setTimeout(r, 20))
    expect(reqGets('pb')).toBe(before + 1)
  })

  it('打开项目时取需求；选一条：画布上是它设计的流程（全部展开），只在"流程设计"里看，和实现对不上的节点标出来', async () => {
    await openTransfer()
    expect(S.requirements.value.map((r) => r.id)).toEqual(['R001', 'R002', 'R003', 'R004'])
    S.tab.value = 'run'
    await S.selectRequirement(fileOf('R002'))
    expect(S.reqMode.value).toBe(true)
    expect(S.tab.value).toBe('design')
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.view.value?.line.sid).toBe(designSid('R002'))
    expect(S.view.value!.folds.every((f) => !f.collapsed)).toBe(true)
    expect([...S.reqMarks.value.nodes.values()].map((m) => m.label).sort()).toEqual(['分类不一样', '走向不一致', '还没有实现'].sort())
    // 流程差异的标记、差异栏都不是这张图的
    expect(S.marks.value.nodes.size).toBe(0)
    expect(S.changeStops.value).toEqual([])
    // 设计图的节点没有源码位置（详情不取源码）
    expect(S.view.value!.nodes.every((n) => n.loc === null)).toBe(true)
    // 对得上的：什么都不标
    await S.selectRequirement(fileOf('R001'))
    expect(S.view.value?.line.sid).toBe(designSid('R001'))
    expect(S.reqMarks.value.nodes.size).toBe(0)
  })

  it('回到功能线：画布回到原来的功能线，展开状态照旧；再切回需求还是上次那条', async () => {
    await openTransfer()
    const fold = S.view.value!.folds.find((f) => f.collapsed)!
    S.openFold(fold)
    await until(() => S.view.value!.folds.some((f) => f.id === fold.id && !f.collapsed), '展开')
    const lineState = S.viewState.value
    await S.selectRequirement(fileOf('R002'))
    await S.showLines()
    expect(S.reqMode.value).toBe(false)
    expect(S.view.value?.line.sid).toBe(TRANSFER_LINE)
    expect(S.viewState.value).toBe(lineState)
    expect(S.reqMarks.value.nodes.size).toBe(0)
    await S.showRequirements()
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.view.value?.line.sid).toBe(designSid('R002'))
  })

  it('读不懂的需求：画布上没有图', async () => {
    await openTransfer()
    await S.selectRequirement(fileOf('R003'))
    expect(S.currentReq.value?.errors.length).toBe(2)
    expect(S.view.value).toBeNull()
    expect(S.layout.value).toBeNull()
  })

  it('在设计图上定位一个节点：展开把它收起来的分支，选中、移到画布中间', async () => {
    await openTransfer()
    await S.selectRequirement(fileOf('R002'))
    const root = S.view.value!.cards[0].id
    S.resetDefault() // 恢复默认：设计图默认就是全部展开
    await until(() => S.view.value!.folds.every((f) => !f.collapsed), '全部展开')
    const fold = S.view.value!.folds.find((f) => f.members.includes(`${root}/step:L20`))!
    S.closeFold(fold)
    await until(() => S.view.value!.folds.some((f) => f.id === fold.id && f.collapsed), '收起')
    expect(S.reqMarks.value.counts.get(fold.id)).toBe(2)
    await S.focusDesignNode('step:L20')
    expect(S.view.value!.nodes.some((n) => n.id === `${root}/step:L20`)).toBe(true)
    expect(S.selection.value).toEqual({ kind: 'node', id: `${root}/step:L20` })
    expect(S.canvasCommand.value).toMatchObject({ type: 'focus', id: `${root}/step:L20` })
  })

  it('"功能线：…"换到实现的功能线（看需求之前就在这条线上时展开状态照旧）；"实现里多了"的节点：换到功能线，选中它', async () => {
    await openTransfer()
    const fold = S.view.value!.folds.find((f) => f.collapsed)!
    S.openFold(fold)
    await until(() => S.view.value!.folds.some((f) => f.id === fold.id && !f.collapsed), '展开')
    const lineState = S.viewState.value
    await S.selectRequirement(fileOf('R002'))
    await S.openReqLine()
    expect(S.reqMode.value).toBe(false)
    expect(S.lineSid.value).toBe(TRANSFER_LINE)
    expect(S.view.value?.line.sid).toBe(TRANSFER_LINE)
    expect(S.viewState.value).toBe(lineState)
    await S.selectRequirement(fileOf('R002'))
    await S.gotoImplNode(TRANSFER_LINE, 'step:be6595ab')
    expect(S.reqMode.value).toBe(false)
    const root = S.view.value!.cards[0].id
    expect(S.selection.value).toEqual({ kind: 'node', id: `${root}/step:be6595ab` })
    expect(S.nodeById.value.get(`${root}/step:be6595ab`)?.title).toBe('查询可用余额')
  })

  it('刷新：需求文件改了不用重新分析，对照跟着变；看着的那条没了换到第一条；都没了回到功能线', async () => {
    await openTransfer()
    await S.selectRequirement(fileOf('R002'))
    const before = reqGets('pb')
    // R002 的差异都改好了（只剩一处）
    const fixed = reqs()
    const r2 = (fixed.requirements as unknown as { comparison: { differences: unknown[] } }[])[1]
    r2.comparison.differences = r2.comparison.differences.slice(0, 1)
    be.projects.get('pb')!.requirements = fixed
    await S.loadRequirements()
    expect(reqGets('pb')).toBe(before + 1)
    expect(S.currentReq.value?.id).toBe('R002')
    expect([...S.reqMarks.value.nodes.values()].map((m) => m.label)).toEqual(['还没有实现'])
    // R002 删掉了
    const gone = reqs()
    gone.requirements.splice(1, 1)
    be.projects.get('pb')!.requirements = gone
    await S.loadRequirements()
    expect(S.currentReq.value?.id).toBe('R001')
    await designShown('R001')
    expect(S.notices.value.at(-1)?.text).toContain('找不到了')
    // 都删了
    be.projects.get('pb')!.requirements = { requirements: [], graph: null, run: null }
    await S.loadRequirements()
    await until(() => !S.reqMode.value, '回到功能线')
    await until(() => S.view.value?.line.sid === TRANSFER_LINE, '画上功能线')
    expect(S.requirements.value).toEqual([])
  })

  it('刷新出错：已经取到的照旧，说一声为什么没刷新', async () => {
    await openTransfer()
    await S.selectRequirement(fileOf('R002'))
    be.projects.get('pb')!.requirements = undefined // 换成"没有这个接口"以外的错：挂掉
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) =>
      url.endsWith('/requirements') ? new Response(JSON.stringify({ error: '服务器内部错误：读不了需求目录' }), { status: 500 }) : be.fetchFn(url, init),
    )
    await S.loadRequirements()
    expect(S.reqError.value).toBe('需求没有刷新：服务器内部错误：读不了需求目录')
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.reqMode.value).toBe(true)
  })

  it('重新分析之后重取需求（对照按最新一次分析算），看着的需求照旧', async () => {
    await openTransfer()
    await S.selectRequirement(fileOf('R002'))
    const before = reqGets('pb')
    await S.reanalyze()
    await until(() => reqGets('pb') === before + 1 && !S.reqLoading.value, '重取需求')
    expect(S.reqMode.value).toBe(true)
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.view.value?.line.sid).toBe(designSid('R002'))
  })

  it('地址栏：#/<项目>/req:<编号> 打开就是这条需求（功能线也读好了）；选需求、回到功能线时地址栏跟着改', async () => {
    be.projects.get('pb')!.requirements = reqs()
    const loc = await withAddressBar(`#/pb/${encodeURIComponent('req:R002')}`)
    await S.init()
    await designShown('R002')
    await until(() => !S.loading.value && S.lineSid.value === TRANSFER_LINE, '功能线读好')
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.view.value?.line.sid).toBe(designSid('R002'))
    expect(loc.hash).toBe(`#/pb/${encodeURIComponent('req:R002')}`)
    await S.selectRequirement(fileOf('R004'))
    expect(loc.hash).toBe(`#/pb/${encodeURIComponent('req:R004')}`)
    await S.showLines()
    expect(loc.hash).toBe(`#/pb/${encodeURIComponent(TRANSFER_LINE)}`)
  })

  it('地址栏：图先读完、需求后到也停在需求上；手改地址栏换需求、换回功能线', async () => {
    be.projects.get('pb')!.requirements = reqs()
    const loc = await withAddressBar(`#/pb/${encodeURIComponent('req:R004')}`)
    const handlers: Record<string, () => void> = {}
    vi.stubGlobal('window', { addEventListener: (type: string, f: () => void) => (handlers[type] = f) })
    const slow = be.hold('GET', /\/pb\/requirements/)
    await S.init()
    await until(() => !S.loading.value && S.view.value?.line.sid === TRANSFER_LINE, '先画上功能线')
    expect(loc.hash).toBe(`#/pb/${encodeURIComponent('req:R004')}`)
    slow()
    await designShown('R004')
    expect(S.currentReq.value?.id).toBe('R004')
    loc.hash = `#/pb/${encodeURIComponent('req:R001')}`
    handlers.hashchange()
    await designShown('R001')
    loc.hash = `#/pb/${encodeURIComponent(TRANSFER_LINE)}`
    handlers.hashchange()
    await until(() => !S.reqMode.value && S.view.value?.line.sid === TRANSFER_LINE, '回到功能线')
  })

  it('看着需求时打开变更记录：画布换成记录的图、列表换到功能线；回到现在时还是那条需求的设计图', async () => {
    await openTransfer()
    be.setStage5(true)
    be.projects.get('pb')!.reviews = [
      {
        info: { id: 4, acked_at: '2026-11-16T08:00:00+00:00', head_id: 2, head_at: '2026-11-16T08:00:00+00:00', base_id: null, base_at: null, summary: {}, graph_size: TRANSFER.length },
        diff: null,
        text: '',
        graph: TRANSFER,
      },
    ]
    await S.selectRequirement(fileOf('R002'))
    await designShown('R002')
    const designState = S.viewState.value
    S.search.value = 'R00'
    await S.openReview(4)
    await until(() => S.review.value?.id === 4 && S.view.value?.line.sid === TRANSFER_LINE, '记录的图')
    expect(S.reqMode.value).toBe(false)
    expect(S.search.value).toBe('')
    await S.leaveReview()
    await designShown('R002')
    expect(S.reqMode.value).toBe(true)
    expect(S.currentReq.value?.id).toBe('R002')
    expect(S.viewState.value).toBe(designState)
    // 回到功能线：展开状态是看需求之前的
    await S.showLines()
    expect(S.view.value?.line.sid).toBe(TRANSFER_LINE)
  })

  it('地址栏里的需求找不到了（或者后端没有需求的接口）：说一声，停在功能线上，地址栏改成功能线', async () => {
    be.projects.get('pb')!.requirements = reqs()
    const loc = await withAddressBar(`#/pb/${encodeURIComponent('req:R404')}`)
    await S.init()
    await until(() => !S.loading.value && S.view.value?.line.sid === TRANSFER_LINE && !S.reqLoading.value, '读完')
    await until(() => loc.hash === `#/pb/${encodeURIComponent(TRANSFER_LINE)}`, '地址栏改成功能线')
    expect(S.reqMode.value).toBe(false)
    expect(S.notices.value.at(-1)?.text).toContain('「R404」')

    be.projects.get('pb')!.requirements = undefined
    const loc2 = await withAddressBar(`#/pb/${encodeURIComponent('req:R002')}`)
    await S.init()
    await until(() => !S.loading.value && S.view.value?.line.sid === TRANSFER_LINE && reqGets('pb') >= 2 && !S.reqLoading.value, '读完')
    await until(() => loc2.hash === `#/pb/${encodeURIComponent(TRANSFER_LINE)}`, '地址栏改成功能线')
    expect(S.requirements.value).toEqual([])
  })
})

describe('变更记录：在画布上回看（只读）', () => {
  /** 后面那条功能线（mini.lines[2]，没有节点的任务）、中间那条 */
  const EMPTY_LINE = mini.lines[2].sid
  /** #1 那时的图里只有后两条线，那次新增了 EMPTY_LINE */
  const ADDED_DIFF = {
    version: 1,
    functions: {},
    lines: [{ status: 'added', sid: EMPTY_LINE, title: '空任务', group: 'test', new_id: K.empty, functions: [], tasks_added: [], tasks_removed: [], root: '' }],
  }
  const summaryOf = { lines: [], functions: 1, added: 0, removed: 0, changed: 1 }
  const at = (d: number) => `2026-11-1${d}T08:00:00+00:00`
  const info = (id: number, head: number, base: number | null, graph: string | undefined) => ({
    id,
    acked_at: at(id),
    head_id: head,
    head_at: at(id),
    base_id: base,
    base_at: base === null ? null : at(id - 1),
    summary: summaryOf,
    graph_size: graph ? graph.length : null,
  })
  /** pa 的三条记录（新的在前）：#3 没存图（旧版本存的），#2 改了"校验参数"，#1 新增了一条线 */
  function withReviews() {
    be.setStage5(true)
    be.projects.get('pa')!.reviews = [
      { info: info(3, 7, 5, undefined), diff: MINI_DIFF, text: '#3' },
      { info: info(2, 5, 3, MINI), diff: MINI_DIFF, text: '改动步骤：检查参数 → 校验参数', graph: MINI },
      { info: info(1, 3, null, MINI_WITHOUT_FIRST), diff: ADDED_DIFF, text: '新增功能线：空任务', graph: MINI_WITHOUT_FIRST },
    ]
  }
  const reviewed = (id: number, line = '') =>
    until(() => S.review.value?.id === id && S.reviewLoading.value === null && S.view.value?.line.sid === (line || S.lineSid.value), `打开 #${id}`)

  it('列出记录；"在图上看"：画布换成那时的图、标出那次的变化（同一套标记、处数）；只读；回到现在时功能线、展开状态、页签照旧', async () => {
    withReviews()
    await S.init()
    await settled('pa')
    await until(() => S.reviews.value.length === 3, '记录列表')
    expect(S.reviews.value.map((r) => r.has_graph)).toEqual([false, true, true])
    // 现在：看后面那条线、全部展开、在"运行路径"页签
    await S.selectLine(mini.lines[1].sid)
    await settled('pa')
    S.expandAll()
    await until(() => Object.keys(S.viewState.value.cards).length > 0 || Object.keys(S.viewState.value.folds).length > 0, '展开')
    const before = S.viewState.value
    S.tab.value = 'run'
    const present = S.project.value

    await S.openReview(2)
    await reviewed(2, miniLine.sid)
    // 正在看的线这次没有变化：换到第一条有变化的
    expect(S.lineSid.value).toBe(miniLine.sid)
    expect(S.tab.value).toBe('design')
    expect(S.canvasGraph.value).not.toBe(present)
    expect(S.project.value).toBe(present)
    expect(S.canvasOf.value).toEqual({ project: 'pa', analysis: 5 })
    expect(S.diffView.value.lineStatus.get(miniLine.sid)).toBe('changed')
    const root = S.view.value!.cards[0].id
    expect(S.marks.value.nodes.get(`${root}/step:n2`)).toBe('changed')
    expect(S.changeStops.value.map((s) => s.text)).toEqual(['改动步骤：检查参数 → 校验参数'])
    // 上一处 / 下一处照样走
    await S.nextStop(1)
    expect(S.stopIndex.value).toBe(0)
    expect(S.selection.value).toEqual({ kind: 'node', id: `${root}/step:n2` })
    // 现在没有差异：项目菜单说的现在的差异是空的
    expect(S.presentDiffView.value.lineStatus.size).toBe(0)
    // 记录那次（#5）不是页面知道的最新一次、也不是它的基准：源码片段不能当作当时的代码
    expect(S.reviewHeadKept.value).toBe(false)
    // 不能标为已看
    await S.ackChanges({ project: 'pa', analysis: 1 })
    expect(be.log.some((l) => l.includes('/diff/ack'))).toBe(false)
    expect(S.notices.value.at(-1)?.text).toContain('正在看变更记录')

    await S.leaveReview()
    await settled('pa')
    expect(S.review.value).toBeNull()
    expect(S.canvasGraph.value).toBe(present)
    expect(S.lineSid.value).toBe(mini.lines[1].sid)
    expect(S.viewState.value).toBe(before)
    expect(S.tab.value).toBe('run')
    expect(S.layoutReason.value).toBe('back')
    expect(S.diffView.value.lineStatus.size).toBe(0)
  })

  it('回到现在：现在的差异没换时，差异栏照旧定位在原来那一处；看记录时现在的差异照常在底下', async () => {
    withReviews()
    withDiff()
    await S.init()
    await settled('pa')
    await until(() => S.changeStops.value.length === 1, '现在的差异')
    await S.nextStop(1)
    expect(S.stopIndex.value).toBe(0)
    const sel = S.selection.value
    await S.openReview(1)
    await reviewed(1, EMPTY_LINE)
    expect(S.stopIndex.value).toBe(-1)
    // 项目菜单说的是现在的差异
    expect(S.presentDiffView.value.lineStatus.get(miniLine.sid)).toBe('changed')
    await S.leaveReview()
    await settled('pa')
    expect(S.stopIndex.value).toBe(0)
    expect(S.selection.value).toEqual(sel)
    expect(S.changeStops.value.map((s) => s.text)).toEqual(['改动步骤：检查参数 → 校验参数'])
  })

  it('上一次 / 下一次：在存着图的记录之间走，看那次有变化的线；没存图的、旧版后端说不清的跳过', async () => {
    withReviews()
    await S.init()
    await settled('pa')
    await until(() => S.reviews.value.length === 3, '记录列表')
    await S.openReview(2)
    await reviewed(2)
    await S.stepReview('earlier')
    await reviewed(1, EMPTY_LINE)
    // #1 那时的图里只有两条线，那次新增了 EMPTY_LINE
    expect(S.displayLines.value).toHaveLength(2)
    expect(S.diffView.value.lineStatus.get(EMPTY_LINE)).toBe('added')
    await S.stepReview('earlier') // 没有更早的
    expect(S.review.value?.id).toBe(1)
    await S.stepReview('later')
    await reviewed(2, miniLine.sid)
    await S.stepReview('later') // #3 没存图：不往那里走
    expect(S.review.value?.id).toBe(2)
    expect(be.log.some((l) => l.includes('/reviews/3/graph'))).toBe(false)
  })

  it('从对话框打开没存图的记录（旧版本存的）：说一声，停在现在', async () => {
    withReviews()
    await S.init()
    await settled('pa')
    await S.openReview(3)
    expect(S.review.value).toBeNull()
    expect(S.reviewLoading.value).toBeNull()
    expect(S.notices.value.at(-1)?.text).toContain('#3 没有存当时的图')
  })

  it('看记录时重新分析完了：画布留在记录上（现在的图在底下换好）；回到现在时是新的图，原来那条线没了就回到第一条', async () => {
    withReviews()
    await S.init()
    await settled('pa')
    await S.openReview(2)
    await reviewed(2)
    be.projects.get('pa')!.next = MINI_WITHOUT_FIRST
    await S.reanalyze()
    await until(() => S.project.value?.lines.length === 2, '现在的图换好了')
    expect(S.review.value?.id).toBe(2)
    expect(S.canvasGraph.value?.lines).toHaveLength(3)
    expect(S.view.value?.line.sid).toBe(miniLine.sid)
    await S.leaveReview()
    await settled('pa')
    // 看记录之前看的是第一条（miniLine），新的图里没有了
    expect(S.lineSid.value).toBe(mini.lines[1].sid)
    expect(S.notices.value.some((n) => n.text.includes('原来看的功能线'))).toBe(true)
    expect(S.graphOf.value?.analysis).toBe(S.projects.value[0].analysis?.id)
  })

  it('换项目：看着的记录作废，页签回到看记录之前的', async () => {
    withReviews()
    await S.init()
    await settled('pa')
    S.tab.value = 'run'
    await S.openReview(2)
    await reviewed(2)
    await S.selectProject('pb')
    await settled('pb')
    expect(S.review.value).toBeNull()
    expect(S.tab.value).toBe('run')
    expect(S.canvasGraph.value).toBe(S.project.value)
  })

  it('地址栏：#/pa/review:2/<线> 刷新后直接是那条记录的那条线；"后退"到功能线时回到现在', async () => {
    withReviews()
    const loc = await withAddressBar(`#/pa/review:2/${encodeURIComponent(EMPTY_LINE)}`)
    const handlers: Record<string, () => void> = {}
    vi.stubGlobal('window', { addEventListener: (type: string, f: () => void) => (handlers[type] = f) })
    await S.init()
    await reviewed(2, EMPTY_LINE)
    expect(loc.hash).toBe(`#/pa/review:2/${encodeURIComponent(EMPTY_LINE)}`)
    await S.selectLine(miniLine.sid)
    expect(loc.hash).toBe(`#/pa/review:2/${encodeURIComponent(miniLine.sid)}`)
    // 浏览器的"后退"、手改地址栏
    loc.hash = `#/pa/review:1`
    handlers.hashchange()
    await reviewed(1, EMPTY_LINE)
    loc.hash = `#/pa/${encodeURIComponent(EMPTY_LINE)}`
    handlers.hashchange()
    await until(() => S.review.value === null && S.view.value?.line.sid === EMPTY_LINE, '回到现在')
    expect(S.canvasGraph.value).toBe(S.project.value)
    expect(loc.hash).toBe(`#/pa/${encodeURIComponent(EMPTY_LINE)}`)
  })

  it('地址栏里的记录已经不在了（只留最近 10 条）、编号不对：说一声，显示现在的图，地址栏改成功能线', async () => {
    withReviews()
    const loc = await withAddressBar('#/pa/review:9')
    await S.init()
    await until(() => S.notices.value.some((n) => n.text.includes('#9 已经不在了')), '提示')
    await settled('pa')
    // 记录列表里没有它：不去取明细和图（浏览器不报 404）
    expect(be.log.some((l) => l.includes('/reviews/9'))).toBe(false)
    expect(S.review.value).toBeNull()
    expect(S.reviewLoading.value).toBeNull()
    await until(() => loc.hash === `#/pa/${encodeURIComponent(miniLine.sid)}`, '地址栏改成功能线')

    const loc2 = await withAddressBar('#/pa/review:x')
    await S.init()
    await settled('pa')
    expect(S.notices.value.at(-1)?.text).toContain('编号不对')
    await until(() => loc2.hash === `#/pa/${encodeURIComponent(miniLine.sid)}`, '地址栏改成功能线')
  })

  it('旧版后端（没有 /reviews）：列表是空的，地址栏里的记录说不支持', async () => {
    be.setStage5(true)
    await withAddressBar('#/pa/review:2')
    await S.init()
    await settled('pa')
    await until(() => S.notices.value.some((n) => n.text.includes('不支持')), '提示')
    expect(S.reviews.value).toEqual([])
    expect(S.review.value).toBeNull()
  })
})


describe('钻入链：一个步骤同时钻入它调用的几个函数', () => {
  it('drill 不收起同一个步骤已经开着的；undrill 只收那一张，选中挪到链上紧挨着的一张；最后一张收回后变回步骤、选中步骤', async () => {
    be.add('pc', 'chain', JSON.stringify(chainProject))
    await S.init()
    await settled('pa')
    await S.selectProject('pc')
    await settled('pc')
    const step = at('step:n3')
    const chain = () => drillChainOf(S.view.value!, step).map((c) => c.fn)
    const node = S.view.value!.nodes.find((n) => n.id === step)!
    const [a, b, c] = node.drills
    S.select('node', step)
    S.drill(step, b)
    await until(() => chain().length === 1, '钻入 b')
    expect(S.selection.value).toEqual({ kind: 'card', id: b.id })
    // 从 b 的卡片上"再展开"a：b 照旧开着，a 排在前面
    S.drill(step, a, b.id)
    await until(() => chain().length === 2, '再展开 a')
    expect(chain()).toEqual([CK.a, CK.b])
    expect(S.viewState.value.drills[b.id]).toBe(true)
    expect(S.selection.value).toEqual({ kind: 'card', id: a.id })
    // 再展开剩下的（都展开）
    S.drill(step, [c], a.id)
    await until(() => chain().length === 3, '再展开 c')
    expect(chain()).toEqual([CK.a, CK.b, CK.c])
    // 收中间那张（b）：选中挪到后面的 c
    S.select('card', b.id)
    S.undrill(S.cardById.value.get(b.id)!)
    await until(() => chain().length === 2, '收回 b')
    expect(chain()).toEqual([CK.a, CK.c])
    expect(S.selection.value).toEqual({ kind: 'card', id: c.id })
    // 收最后那张（c）：后面没有了，挪到前面的 a
    S.undrill(S.cardById.value.get(c.id)!)
    await until(() => chain().length === 1, '收回 c')
    expect(S.selection.value).toEqual({ kind: 'card', id: a.id })
    // 选中卡片里的节点时收回：选中挪到步骤
    S.select('node', at('start', a.id))
    S.undrill(S.cardById.value.get(a.id)!)
    await until(() => chain().length === 0 && S.view.value!.nodes.some((n) => n.id === step), '变回步骤')
    expect(S.selection.value).toEqual({ kind: 'node', id: step })
    expect(Object.values(S.viewState.value.drills).filter(Boolean)).toEqual([])
    // 都钻入（步骤上的"都钻入"）
    S.drill(step, node.drills)
    await until(() => chain().length === 3, '都钻入')
    expect(chain()).toEqual([CK.a, CK.b, CK.c])
    expect(into(step, CK.a)).toBe(a.id)
  })
})
