import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { ApiError, UNREACHABLE, createApi, isMissingEndpoint, parseGraph, type FetchFn } from './api'

const TRANSFER_TEXT = readFileSync(new URL('./graph/fixtures/transfer.json', import.meta.url), 'utf8')
const transferJson = () => JSON.parse(TRANSFER_TEXT) as Record<string, unknown>

interface Call {
  url: string
  init?: RequestInit
}

/** 假的 fetch：记下每次请求，按 handler 回应 */
function fakeFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = []
  const fetchFn: FetchFn = async (url, init) => {
    calls.push({ url, init })
    return handler(url, init)
  }
  return { api: createApi(fetchFn), calls }
}

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })

const PROJECT = {
  id: 'k3x9a2bq',
  name: 'transfer',
  root: '/repo/examples/transfer',
  created_at: '2026-11-16T04:33:06+00:00',
  updated_at: '2026-11-16T04:33:06+00:00',
  analyzing: false,
  analysis: {
    id: 3,
    created_at: '2026-11-16T04:33:06+00:00',
    engine_version: '0.1.0',
    graph_size: 75822,
    summary: {
      functions: 22,
      lines: 1,
      common_tasks: 0,
      files: 1,
      unresolved_calls: 13,
      guessed_calls: 0,
      unreachable: 0,
      dead_ends: 0,
      skipped_files: [{ file: 'bad.py', reason: '语法错误' }],
      source_roots: ['.'],
      rules: ['pystepkit'],
      excluded: { __pycache__: 1 },
      has_entries: true,
      seconds: 0.01,
    },
    base_id: null,
    has_diff: false,
  },
  mcp_tests: false,
}

async function caught(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError)
    return e as ApiError
  }
  throw new Error('应该抛错')
}

describe('项目', () => {
  it('列项目：GET /api/projects，原样规整成 ProjectInfo', async () => {
    const { api, calls } = fakeFetch(() => json([PROJECT]))
    expect(await api.listProjects()).toEqual([PROJECT])
    expect(calls).toEqual([{ url: '/api/projects', init: undefined }])
  })

  it('缺字段的项目信息补成空值，不让界面白屏；没有 id 的算格式不对', async () => {
    const { api } = fakeFetch(() => json([{ id: 'a', analysis: { id: 1, summary: {} } }]))
    const [p] = await api.listProjects()
    expect(p).toMatchObject({ id: 'a', name: 'a', root: '', analyzing: false })
    expect(p.analysis?.summary).toMatchObject({ functions: 0, lines: 0, skipped_files: [], rules: [], has_entries: true })
    // 旧版后端没有对比基准和差异：当作没有
    expect(p.analysis).toMatchObject({ base_id: null, has_diff: false })
    // 旧版后端没有"允许 AI 运行单条功能线的测试"：null（界面不显示这个开关）
    expect(p.mcp_tests).toBeNull()
    const bad = fakeFetch(() => json([{ name: 'x' }]))
    expect((await caught(bad.api.listProjects())).kind).toBe('format')
    const notList = fakeFetch(() => json({ projects: [] }))
    expect((await caught(notList.api.listProjects())).kind).toBe('format')
  })

  it('添加项目：POST 目录；名称、配置为空时不发', async () => {
    const { api, calls } = fakeFetch(() => json(PROJECT, 201))
    await api.addProject({ root: '/repo/x', name: '  ' })
    await api.addProject({ root: '~/x/', name: ' 我的 ', config: { exclude: ['gen'] } })
    expect(calls.map((c) => [c.url, c.init?.method, JSON.parse(String(c.init?.body))])).toEqual([
      ['/api/projects', 'POST', { root: '/repo/x' }],
      ['/api/projects', 'POST', { root: '~/x/', name: '我的', config: { exclude: ['gen'] } }],
    ])
    expect(new Headers(calls[0].init?.headers).get('Content-Type')).toBe('application/json')
  })

  it('添加失败：后端的中文说明、code、已有项目的 id 都带出来', async () => {
    const { api } = fakeFetch(() =>
      json({ error: '这个目录已经添加过（项目「transfer」）：/repo/x', code: 'duplicate', project_id: 'k3x9a2bq' }, 409),
    )
    const e = await caught(api.addProject({ root: '/repo/x' }))
    expect(e).toMatchObject({ kind: 'http', status: 409, code: 'duplicate', projectId: 'k3x9a2bq' })
    expect(e.message).toBe('这个目录已经添加过（项目「transfer」）：/repo/x')
  })

  it('删除：DELETE，204 没有返回体', async () => {
    const { api, calls } = fakeFetch(() => new Response(null, { status: 204 }))
    await expect(api.deleteProject('a/b')).resolves.toBeUndefined()
    expect(calls[0]).toMatchObject({ url: '/api/projects/a%2Fb', init: { method: 'DELETE' } })
  })

  it('重新分析：POST …/analyze；正在分析时 409，code 为 busy', async () => {
    const ok = fakeFetch(() => json(PROJECT))
    expect((await ok.api.analyzeProject('k3x9a2bq')).analysis?.id).toBe(3)
    expect(ok.calls[0]).toMatchObject({ url: '/api/projects/k3x9a2bq/analyze', init: { method: 'POST' } })
    const busy = fakeFetch(() => json({ error: '正在分析', code: 'busy' }, 409))
    expect(await caught(busy.api.analyzeProject('k3x9a2bq'))).toMatchObject({ code: 'busy', status: 409, message: '正在分析' })
  })

  it('健康检查、读项目、配置', async () => {
    const { api, calls } = fakeFetch((url, init) => {
      if (url === '/api/health') return json({ ok: true, version: '0.1.0', engine_version: '0.1.0' })
      if (url.endsWith('/config')) return json(init?.method === 'PUT' ? JSON.parse(String(init.body)) : { exclude: ['gen'] })
      return json(PROJECT)
    })
    expect(await api.health()).toEqual({ ok: true, version: '0.1.0', engine_version: '0.1.0' })
    expect((await api.getProject('k3x9a2bq')).name).toBe('transfer')
    expect(await api.getConfig('k3x9a2bq')).toEqual({ exclude: ['gen'] })
    expect(await api.putConfig('k3x9a2bq', { exclude: [] })).toEqual({ exclude: [] })
    expect(calls.map((c) => [c.url, c.init?.method ?? 'GET'])).toEqual([
      ['/api/health', 'GET'],
      ['/api/projects/k3x9a2bq', 'GET'],
      ['/api/projects/k3x9a2bq/config', 'GET'],
      ['/api/projects/k3x9a2bq/config', 'PUT'],
    ])
  })
})

describe('连不上、出错', () => {
  it('后端没启动（fetch 本身失败）：unreachable，中文', async () => {
    const { api } = fakeFetch(() => {
      throw new TypeError('Failed to fetch')
    })
    const e = await caught(api.listProjects())
    expect(e).toMatchObject({ kind: 'unreachable', status: 0, message: UNREACHABLE })
  })

  it('开发服务器转发失败（Vite 回 502、空的 text/plain）：也算连不上', async () => {
    for (const status of [502, 503, 504]) {
      const { api } = fakeFetch(() => new Response('', { status, headers: { 'Content-Type': 'text/plain' } }))
      expect((await caught(api.listProjects())).kind).toBe('unreachable')
    }
  })

  it('后端的 500 带 JSON：原样的中文说明，不算连不上', async () => {
    const { api } = fakeFetch(() => json({ error: '服务器内部错误：RuntimeError: 坏了' }, 500))
    expect(await caught(api.listProjects())).toMatchObject({ kind: 'http', status: 500, message: '服务器内部错误：RuntimeError: 坏了' })
  })

  it('不是 JSON 的错误响应：写状态码', async () => {
    const { api } = fakeFetch(() => new Response('Internal Server Error', { status: 500, statusText: 'Internal Server Error' }))
    expect((await caught(api.listProjects())).message).toBe('后端返回了 HTTP 500 Internal Server Error')
  })

  it('响应体读到一半连接断了：连不上（中文），不是原样的 TypeError', async () => {
    const broken = (status: number) =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(new TextEncoder().encode('[{"id": "a"'))
            c.error(new TypeError('network error'))
          },
        }),
        { status, headers: { 'Content-Type': 'application/json' } },
      )
    for (const status of [200, 500]) {
      const { api } = fakeFetch(() => broken(status))
      const e = await caught(api.listProjects())
      expect(e.kind).toBe('unreachable')
      expect(e.message).toContain(UNREACHABLE)
      expect(e.message).toContain('读到一半断了')
    }
    const del = fakeFetch(() => broken(500))
    expect((await caught(del.api.deleteProject('p'))).kind).toBe('unreachable')
    const graph = fakeFetch(() => broken(200))
    expect((await caught(graph.api.getGraph('p'))).kind).toBe('unreachable')
  })

  it('200 但不是 JSON（端口上跑的是别的东西）：format', async () => {
    const { api } = fakeFetch(() => new Response('<!doctype html><title>x</title>', { status: 200 }))
    const e = await caught(api.listProjects())
    expect(e.kind).toBe('format')
    expect(e.message).toContain('不是 JSON')
  })
})

/** 把一段文字切成几块，做成流式响应体 */
function chunked(text: string, parts: number, headers: Record<string, string>) {
  const bytes = new TextEncoder().encode(text)
  const size = Math.ceil(bytes.length / parts)
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      for (let i = 0; i < bytes.length; i += size) c.enqueue(bytes.slice(i, i + size))
      c.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/json', ...headers } })
}

describe('读图', () => {
  const size = new TextEncoder().encode(TRANSFER_TEXT).length

  it('按已收到的字节报告进度，总量取 X-Pystepmap-Graph-Size；分析 id 取 X-Pystepmap-Analysis', async () => {
    const { api, calls } = fakeFetch(() =>
      chunked(TRANSFER_TEXT, 4, {
        'X-Pystepmap-Graph-Size': String(size),
        'X-Pystepmap-Analysis': '7',
        // 压缩后的大小：不能当总量用
        'Content-Length': '1234',
        'Content-Encoding': 'gzip',
      }),
    )
    const seen: [number, number | null][] = []
    const r = await api.getGraph('k3x9a2bq', (got, total) => seen.push([got, total]))
    expect(calls[0].url).toBe('/api/projects/k3x9a2bq/graph')
    expect(r.analysisId).toBe(7)
    expect(r.bytes).toBe(size)
    expect(r.graph.lines[0].sid).toBe('line:transfer_workflow.py::TransferWorkflow.run')
    expect(seen[0]).toEqual([0, size])
    expect(seen.at(-1)).toEqual([size, size])
    expect(seen.length).toBeGreaterThanOrEqual(5)
    for (let i = 1; i < seen.length; i++) expect(seen[i][0]).toBeGreaterThanOrEqual(seen[i - 1][0])
  })

  it('没有 X-Pystepmap-Graph-Size：没压缩时用 Content-Length，压缩了就不知道总量', async () => {
    const plain = fakeFetch(() => chunked(TRANSFER_TEXT, 2, { 'Content-Length': String(size) }))
    const a: (number | null)[] = []
    await plain.api.getGraph('p', (_, total) => a.push(total))
    expect(a.at(-1)).toBe(size)
    const gz = fakeFetch(() => chunked(TRANSFER_TEXT, 2, { 'Content-Length': '99', 'Content-Encoding': 'gzip' }))
    const b: (number | null)[] = []
    await gz.api.getGraph('p', (_, total) => b.push(total))
    expect(b.at(-1)).toBeNull()
  })

  it('中文跨了分块的边界也解码正确', async () => {
    const g = transferJson()
    ;(g.lines as { title: string }[])[0].title = '账户转账：校验、扣款、入账'.repeat(3)
    const text = JSON.stringify(g)
    const { api } = fakeFetch(() => chunked(text, 97, {}))
    expect((await api.getGraph('p')).graph.lines[0].title).toBe('账户转账：校验、扣款、入账'.repeat(3))
  })

  it('项目还没有分析结果：后端的 404 说明原样带出', async () => {
    const { api } = fakeFetch(() => json({ error: '这个项目还没有分析结果' }, 404))
    expect(await caught(api.getGraph('p'))).toMatchObject({ kind: 'http', status: 404, message: '这个项目还没有分析结果' })
  })

  it('旧版的图（功能线没有 sid、schema_version 旧）：old-graph；比页面新：format；不是 JSON：format', async () => {
    const noSid = transferJson()
    ;(noSid.lines as { sid?: string }[]).forEach((l) => delete l.sid)
    const e1 = await caught(fakeFetch(() => json(noSid)).api.getGraph('p'))
    expect(e1.kind).toBe('old-graph')
    expect(e1.message).toContain('重新分析')
    const v0 = { ...transferJson(), schema_version: 0 }
    expect((await caught(fakeFetch(() => json(v0)).api.getGraph('p'))).kind).toBe('old-graph')
    const v2 = { ...transferJson(), schema_version: 2 }
    const e3 = await caught(fakeFetch(() => json(v2)).api.getGraph('p'))
    expect(e3.kind).toBe('format')
    expect(e3.message).toContain('npm run build')
    expect((await caught(fakeFetch(() => new Response('{"schema', { status: 200 })).api.getGraph('p'))).kind).toBe('format')
  })

  it('parseGraph 单独可用', () => {
    expect(parseGraph(transferJson()).lines).toHaveLength(1)
    expect(() => parseGraph([])).toThrow(ApiError)
  })

  it('不是 pystepmap 的图（别的 JSON）：format，提示重试，不说是旧图；真的旧图（早期没有版本号的、版本号小的）：old-graph', () => {
    const kind = (data: unknown) => {
      try {
        parseGraph(data)
      } catch (e) {
        return [(e as ApiError).kind, (e as ApiError).message] as const
      }
      return ['ok', ''] as const
    }
    for (const notGraph of [[], 'x', null, 42, { detail: 'Not Found' }, { error: '坏了' }, { functions: {} }, { lines: [] },
      { schema_version: '1', functions: {}, lines: [] }, { schema_version: null }]) {
      const [k, msg] = kind(notGraph)
      expect(k, JSON.stringify(notGraph)).toBe('format')
      expect(msg).toContain('不是 pystepmap 的分析结果')
      expect(msg).toContain('重试')
      expect(msg).not.toContain('重新分析')
    }
    const { schema_version: _, ...unversioned } = transferJson()
    expect(kind(unversioned)[0]).toBe('old-graph')
    expect(kind({ ...transferJson(), schema_version: 0 })[0]).toBe('old-graph')
    expect(kind({ ...transferJson(), schema_version: 0 })[1]).toContain('重新分析')
  })

  it('读图时别的 JSON（比如别的服务的 404 页面体）：format，不是 old-graph', async () => {
    const e = await caught(fakeFetch(() => json({ detail: 'Not Found' })).api.getGraph('p'))
    expect(e.kind).toBe('format')
  })
})

describe('手工标题和分组', () => {
  const SIDS = ['line:app/a.py::Flow.run', 'line:app/x y.py::f#2', 'line:中文/路径.py::类.方法#3', 'line:a?b=c&d.py::g']

  it('读：GET …/overrides，只留非空的字符串', async () => {
    const { api } = fakeFetch(() => json({ [SIDS[0]]: { title: '新标题' }, [SIDS[1]]: { group: '组', title: 3 } }))
    expect(await api.getOverrides('p')).toEqual({ [SIDS[0]]: { title: '新标题' }, [SIDS[1]]: { group: '组' } })
  })

  it('改：PUT …/overrides?sid=…（/、:、#、?、&、中文都编码进查询参数），整条改写；空白当作不改', async () => {
    const { api, calls } = fakeFetch((url) => json({ [new URL(url, 'http://x').searchParams.get('sid')!]: { title: 't' } }))
    for (const sid of SIDS) {
      const back = await api.putOverride('p', sid, { title: ' t ', group: '   ' })
      expect(back).toEqual({ [sid]: { title: 't' } })
    }
    for (const [i, c] of calls.entries()) {
      const u = new URL(c.url, 'http://x')
      expect(u.pathname).toBe('/api/projects/p/overrides')
      expect(u.searchParams.get('sid')).toBe(SIDS[i])
      expect(c.init?.method).toBe('PUT')
      expect(JSON.parse(String(c.init?.body))).toEqual({ title: 't', group: null })
    }
  })

  it('两项都不给 = 删除这条', async () => {
    const { api, calls } = fakeFetch(() => json({}))
    expect(await api.putOverride('p', SIDS[0], {})).toEqual({})
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ title: null, group: null })
  })

  it('标题太长：后端的 400 说明原样带出', async () => {
    const { api } = fakeFetch(() => json({ error: '请求参数不对：title 太长了（最多 200 个字）' }, 400))
    expect((await caught(api.putOverride('p', SIDS[0], { title: 'x'.repeat(201) }))).message).toContain('最多 200 个字')
  })
})

describe('源码', () => {
  it('GET …/source?file=&start=&end=', async () => {
    const snippet = { file: 'app/x.py', start: 15, end: 17, total: 300, lines: ['a', 'b', 'c'], stale: false }
    const { api, calls } = fakeFetch(() => json(snippet))
    expect(await api.getSource('p', 'app/x.py', 15, 17)).toEqual(snippet)
    expect(calls[0].url).toBe('/api/projects/p/source?file=app%2Fx.py&start=15&end=17')
  })

  it('stale 不是布尔值时为 null；路径越界等的 400 原样带出', async () => {
    const ok = fakeFetch(() => json({ file: 'a.py', start: 1, end: 1, total: 1, lines: ['x'], stale: null }))
    expect((await ok.api.getSource('p', 'a.py', 1, 1)).stale).toBeNull()
    const bad = fakeFetch(() => json({ error: '路径里不能有 ..' }, 400))
    expect((await caught(bad.api.getSource('p', '../a.py', 1, 1))).message).toBe('路径里不能有 ..')
    const shape = fakeFetch(() => json({ file: 'a.py' }))
    expect((await caught(shape.api.getSource('p', 'a.py', 1, 1))).kind).toBe('format')
  })
})

describe('过期检查、流程差异', () => {
  const STALE = {
    checked: true,
    stale: true,
    changed: ['a.py'],
    added: [],
    removed: ['gone.py'],
    unreadable: ['locked.py'],
    lines: ['line:a.py::f'],
    total: { changed: 1, added: 0, removed: 1, unreadable: 1 },
    note: '1 个文件读不了，没法比较',
  }
  const STATUS = { analysis_id: 3, analyzing: false, stale: STALE, base_id: 2, has_diff: true }
  const DIFF = {
    version: 1,
    functions: {
      'a.py::f': {
        status: 'changed',
        sid: 'a.py::f',
        title: 'f',
        file: 'a.py',
        new_key: 'a.py::f@1',
        old_key: 'a.py::f@1',
        nodes: [
          {
            status: 'added',
            type: 'step',
            new_id: 'n4',
            new_sid: 'step:abc',
            new_title: '发通知',
            new_subtitle: 'self.notify',
            new_line: 9,
            old_sid: '',
            old_title: '',
            old_subtitle: '',
            old_line: 0,
            after: '',
            after_title: '',
            after_label: '',
          },
        ],
        edges: [
          {
            status: 'removed',
            kind: 'no',
            label: '',
            src: 'n2',
            dst: 'n5',
            src_type: 'cond',
            src_title: '够吗？',
            src_line: 4,
            dst_type: 'end',
            dst_title: '完成',
            dst_line: 8,
          },
        ],
      },
    },
    lines: [
      { status: 'changed', sid: 'line:a.py::f', title: 'f', group: 'g', new_id: 'a.py::f@1', functions: ['a.py::f'], tasks_added: [], tasks_removed: [], root: 'a.py::f' },
    ],
  }
  const HEAD = { ...PROJECT.analysis, base_id: 2, has_diff: true }
  const BASE = { ...PROJECT.analysis, id: 2 }

  it('GET …/status：原样规整；读不了的文件和说明带出来', async () => {
    const { api, calls } = fakeFetch(() => json(STATUS))
    expect(await api.getStatus('a/b')).toEqual(STATUS)
    expect(calls[0].url).toBe('/api/projects/a%2Fb/status')
  })

  it('status：没法检查时带原因；缺字段补成空值（旧一点的后端没有 unreadable、note）', async () => {
    const off = fakeFetch(() => json({ analysis_id: 3, analyzing: true, stale: { checked: false, reason: '项目目录不存在了' }, base_id: null, has_diff: false }))
    expect(await off.api.getStatus('p')).toEqual({
      analysis_id: 3,
      analyzing: true,
      stale: { checked: false, reason: '项目目录不存在了' },
      base_id: null,
      has_diff: false,
    })
    const bare = fakeFetch(() => json({ analysis_id: null, stale: { checked: true, stale: true, changed: ['a.py'], lines: null } }))
    expect(await bare.api.getStatus('p')).toEqual({
      analysis_id: null,
      analyzing: false,
      stale: {
        checked: true,
        stale: true,
        changed: ['a.py'],
        added: [],
        removed: [],
        unreadable: [],
        lines: null,
        total: { changed: 1, added: 0, removed: 0, unreadable: 0 },
        note: '',
      },
      base_id: null,
      has_diff: false,
    })
  })

  it('旧版后端没有这个接口：404 原样抛出（界面当作没有信息）', async () => {
    const { api } = fakeFetch(() => json({ error: '没有这个接口：GET /api/projects/p/status' }, 404))
    expect(await caught(api.getStatus('p'))).toMatchObject({ kind: 'http', status: 404 })
  })

  it('GET …/diff：head、base、diff 原样（字段名照 core 的 dataclass），baseline 说对比基准是哪种', async () => {
    const baseline = { kind: 'acknowledged', at: '2026-11-16T04:30:00+00:00' }
    const { api, calls } = fakeFetch(() => json({ head: HEAD, base: BASE, diff: DIFF, baseline }))
    expect(await api.getDiff('p')).toEqual({ head: HEAD, base: BASE, diff: DIFF, baseline })
    expect(calls[0]).toEqual({ url: '/api/projects/p/diff', init: undefined })
    const first = fakeFetch(() => json({ head: HEAD, base: BASE, diff: DIFF, baseline: { kind: 'first', at: 'x' } }))
    expect((await first.api.getDiff('p')).baseline).toEqual({ kind: 'first', at: 'x' })
  })

  it('diff：没有 baseline（旧版后端）或者看不懂时为 null', async () => {
    for (const baseline of [undefined, null, { kind: 'later', at: 'x' }, { kind: 'first' }, 'first']) {
      const { api } = fakeFetch(() => json({ head: HEAD, base: BASE, diff: DIFF, baseline }))
      expect((await api.getDiff('p')).baseline).toBeNull()
    }
  })

  it('diff：没有基准时 diff 为 null；缺字段补成空值；没有 head 算格式不对', async () => {
    const none = fakeFetch(() => json({ head: HEAD, base: null, diff: null }))
    expect(await none.api.getDiff('p')).toEqual({ head: HEAD, base: null, diff: null, baseline: null })
    const sparse = fakeFetch(() =>
      json({ head: HEAD, base: BASE, diff: { functions: { 'a.py::f': { status: 'added', sid: 'a.py::f', nodes: [{ status: 'added', new_id: 'n1' }] } } } }),
    )
    const d = (await sparse.api.getDiff('p')).diff!
    expect(d.version).toBe(1)
    expect(d.lines).toEqual([])
    expect(d.functions['a.py::f']).toMatchObject({ status: 'added', title: '', new_key: '', edges: [] })
    expect(d.functions['a.py::f'].nodes[0]).toMatchObject({ status: 'added', type: 'step', new_id: 'n1', new_title: '', old_line: 0, after: '', after_title: '', after_label: '' })
    const bad = fakeFetch(() => json({ diff: null }))
    expect((await caught(bad.api.getDiff('p'))).kind).toBe('format')
  })

  it('POST …/diff/ack?analysis=<看的那次分析>：返回同样的形状（diff 为 null）；有了更新的分析时 409 moved', async () => {
    const ok = fakeFetch(() => json({ head: { ...HEAD, base_id: null, has_diff: false }, base: null, diff: null }))
    expect(await ok.api.ackDiff('p', 3)).toEqual({ head: { ...HEAD, base_id: null, has_diff: false }, base: null, diff: null, baseline: null })
    expect(ok.calls[0]).toMatchObject({ url: '/api/projects/p/diff/ack?analysis=3', init: { method: 'POST' } })
    const moved = fakeFetch(() => json({ error: '分析结果已经更新', code: 'moved' }, 409))
    expect(await caught(moved.api.ackDiff('p', 3))).toMatchObject({ kind: 'http', status: 409, code: 'moved' })
  })

  it('源码请求带上画布上那张图的分析 id', async () => {
    const snippet = { file: 'a.py', start: 1, end: 2, total: 9, lines: ['x', 'y'], stale: true }
    const { api, calls } = fakeFetch(() => json(snippet))
    await api.getSource('p', 'a.py', 1, 2, 7)
    await api.getSource('p', 'a.py', 1, 2, null)
    expect(calls.map((c) => c.url)).toEqual([
      '/api/projects/p/source?file=a.py&start=1&end=2&analysis=7',
      '/api/projects/p/source?file=a.py&start=1&end=2',
    ])
  })

  it('分析超时：后端的 504 带 JSON，是 http 错误（code timeout、服务端的原文），不算连不上', async () => {
    const text = '分析超过 120 秒，已中止：/code/x（项目太大，或者配置里的正则太慢；可以用环境变量 PYSTEPMAP_ANALYZE_TIMEOUT 调整秒数）'
    const { api } = fakeFetch(() => json({ error: text, code: 'timeout' }, 504))
    expect(await caught(api.analyzeProject('p'))).toMatchObject({ kind: 'http', status: 504, code: 'timeout', message: text })
  })
})

describe('测试运行', () => {
  const RUN = {
    id: 3,
    analysis_id: 7,
    started_at: '2026-11-17T04:17:02+00:00',
    finished_at: '2026-11-17T04:17:02+00:00',
    command: '/x/.venv/bin/python -m pytest tests -q',
    status: 'ok',
    exit_code: 0,
    paths_size: 46828,
    summary: {
      tests: 9,
      passed: 9,
      failed: 0,
      skipped: 0,
      errors: 0,
      seconds: 0.169,
      verify: {
        ok: true,
        mismatches: { edge: 0, end: 0, call: 0 },
        functions_seen: 21,
        distinct_paths: 38,
        steps_total: 168,
        plugin_errors: 0,
        plugin_error_types: {},
        truncated_records: 0,
        stale: [],
      },
      coverage: { functions_run: 21, functions_on_lines: 19, outcomes: 30, outcomes_covered: 29, tests: 6, failed: 0, outcomes_uncertain: 0 },
      mismatch_list: [],
      note: '',
    },
    scope: null,
  }

  it('变更记录：列出每一条的概要，取一条的文字明细；旧版后端没有这个接口时是空的', async () => {
    const rec = { id: 3, acked_at: 't3', head_id: 9, head_at: 't2', base_id: 7, base_at: 't1', summary: { lines: [{ sid: 'line:a.py::f', title: '下单', group: '订单', status: 'changed' }], functions: 1, added: 1, removed: 2, changed: 0 } }
    const { api, calls } = fakeFetch((url) => json(url.endsWith('/reviews') ? { reviews: [rec] } : { ...rec, text: '删除步骤：查询库存', diff: {} }))
    const [one] = await api.listReviews('p 1')
    // 没有 graph_size（结构版本 5 时的后端）：不知道存没存图
    expect(one).toEqual({ ...rec, has_graph: null })
    expect((await api.getReview('p 1', 3)).text).toBe('删除步骤：查询库存')
    expect(calls.map((c) => c.url)).toEqual(['/api/projects/p%201/reviews', '/api/projects/p%201/reviews/3'])
    const old = fakeFetch(() => json({ error: '没有这个接口：GET /x' }, 404))
    expect(await old.api.listReviews('p')).toEqual([])
  })

  it('变更记录存没存当时的图（graph_size）；明细里的流程差异读出来；当时的图从 /reviews/{id}/graph 取，和最新的图同一个读法', async () => {
    const base = { id: 3, acked_at: 't3', head_id: 9, head_at: 't2', base_id: 7, base_at: 't1', summary: {} }
    const diff = { version: 1, functions: {}, lines: [{ status: 'changed', sid: 'line:a.py::f', title: '下单', group: '订单', new_id: 'a.py::f@1' }] }
    const list = { reviews: [{ ...base, graph_size: 1200 }, { ...base, id: 2, graph_size: null }] }
    const { api, calls } = fakeFetch((url) => {
      if (url.endsWith('/reviews')) return json(list)
      if (url.endsWith('/graph')) return json(transferJson(), 200, { 'X-Pystepmap-Analysis': '9' })
      return json({ ...base, graph_size: 1200, text: '…', diff })
    })
    expect((await api.listReviews('p')).map((r) => r.has_graph)).toEqual([true, false])
    const d = await api.getReview('p', 3)
    expect(d.has_graph).toBe(true)
    expect(d.diff?.lines[0]).toMatchObject({ status: 'changed', sid: 'line:a.py::f', new_id: 'a.py::f@1', functions: [] })
    const g = await api.getReviewGraph('p', 3)
    expect(calls.at(-1)?.url).toBe('/api/projects/p/reviews/3/graph')
    expect(g.analysisId).toBe(9)
    expect(g.graph.lines.length).toBeGreaterThan(0)
    const noGraph = fakeFetch(() => json({ error: '这条变更记录没有存当时的图（旧版本存的记录），只能看文字明细', code: 'no_graph' }, 404))
    expect(await caught(noGraph.api.getReviewGraph('p', 2))).toMatchObject({ kind: 'http', status: 404, code: 'no_graph' })
  })

  it('正在跑的运行带着范围：一条线、全部测试（null）、旧版后端不说（没有这一项）', async () => {
    const run = { started_at: 't', seconds: 1, phase: 'testing' }
    const answers = [
      { running: { ...run, scope: { line: 'line:a.py::f', line_title: '下单', tests: 3 } }, runs: [], last_failure: null },
      { running: { ...run, scope: null }, runs: [], last_failure: null },
      { running: run, runs: [], last_failure: null },
    ]
    const { api } = fakeFetch(() => json(answers.shift()))
    expect((await api.listRuns('p')).running?.scope).toEqual({ line: 'line:a.py::f', line_title: '下单', tests: 3 })
    expect((await api.listRuns('p')).running?.scope).toBeNull()
    expect('scope' in (await api.listRuns('p')).running!).toBe(false)
  })

  it('找到的解释器：读出目录和解释器；没有解释器路径的不要', async () => {
    const { api, calls } = fakeFetch(() => json({ interpreters: [{ cwd: 'backend', python: '.venv/bin/python' }, { cwd: '', python: 'venv/bin/python' }, { cwd: 'x' }, 3] }))
    expect(await api.getInterpreters('p 1')).toEqual([
      { cwd: 'backend', python: '.venv/bin/python' },
      { cwd: '', python: 'venv/bin/python' },
    ])
    expect(calls.map((c) => c.url)).toEqual(['/api/projects/p%201/interpreters'])
  })

  it('测试命令：GET 读；PUT 整条改写（null = 清掉），返回改后的', async () => {
    const { api, calls } = fakeFetch((_url, init) => json(init?.method === 'PUT' ? { command: 'pytest', confirmed: false } : { command: null, confirmed: false }))
    expect(await api.getTestCommand('p 1')).toEqual({ command: null, confirmed: false })
    expect(await api.putTestCommand('p 1', 'pytest')).toEqual({ command: 'pytest', confirmed: false })
    await api.putTestCommand('p 1', null)
    expect(calls.map((c) => [c.url, c.init?.method ?? 'GET', c.init?.body])).toEqual([
      ['/api/projects/p%201/test-command', 'GET', undefined],
      ['/api/projects/p%201/test-command', 'PUT', JSON.stringify({ command: 'pytest' })],
      ['/api/projects/p%201/test-command', 'PUT', JSON.stringify({ command: null })],
    ])
  })

  it('开始运行：POST …/runs（确认时带 confirm: true 和确认框里那条命令的原文），202 就算开始了', async () => {
    const { api, calls } = fakeFetch(() => json({ run: 'started' }, 202))
    await api.startRun('p')
    await api.startRun('p', '.venv/bin/python -m pytest tests -q')
    expect(calls.map((c) => [c.url, c.init?.method, c.init?.body])).toEqual([
      ['/api/projects/p/runs', 'POST', JSON.stringify({})],
      ['/api/projects/p/runs', 'POST', JSON.stringify({ confirm: true, command: '.venv/bin/python -m pytest tests -q' })],
    ])
  })

  it('只跑一条线的测试：POST …/runs 带上 line（确认时也带）；挑不出来时 409 带 code', async () => {
    const { api, calls } = fakeFetch(() => json({ run: 'started' }, 202))
    await api.startRun('p', null, 'line:wf.py::W.run#2')
    await api.startRun('p', 'pytest -q', 'line:wf.py::W.run#2')
    await api.startRun('p', null, null)
    expect(calls.map((c) => c.init?.body)).toEqual([
      JSON.stringify({ line: 'line:wf.py::W.run#2' }),
      JSON.stringify({ confirm: true, command: 'pytest -q', line: 'line:wf.py::W.run#2' }),
      JSON.stringify({}),
    ])
    const e = await caught(fakeFetch(() => json({ error: '还没有跑完的全量测试运行：先运行一次全部测试', code: 'no_full_run' }, 409)).api.startRun('p', null, 'line:x'))
    expect(e).toMatchObject({ status: 409, code: 'no_full_run' })
  })

  it('这条线的测试名单：GET …/line-tests?line=<编码过的 sid>；挑不出来时 ok 为假带原因；旧版后端 404"没有这个接口"', async () => {
    const body = { ok: true, code: '', reason: '', tests: ['tests/t.py::a', 3, 'tests/t.py::b'], base_run: 4 }
    const { api, calls } = fakeFetch(() => json(body))
    expect(await api.getLineTests('p 1', 'line:a.py::f#2')).toEqual({ ok: true, code: '', reason: '', tests: ['tests/t.py::a', 'tests/t.py::b'], base_run: 4 })
    expect(calls[0].url).toBe('/api/projects/p%201/line-tests?line=line%3Aa.py%3A%3Af%232')
    const no = { ok: false, code: 'no_full_run', reason: '还没有跑完的全量测试运行', tests: [], base_run: null }
    expect(await fakeFetch(() => json(no)).api.getLineTests('p', 'line:x')).toEqual(no)
    expect((await caught(fakeFetch(() => json({ tests: [] })).api.getLineTests('p', 'line:x'))).kind).toBe('format')
    const old = await caught(fakeFetch(() => json({ error: '没有这个接口：GET /api/projects/p/line-tests' }, 404)).api.getLineTests('p', 'line:x'))
    expect(isMissingEndpoint(old)).toBe(true)
    const gone = await caught(fakeFetch(() => json({ error: '最新的图里没有这条功能线：line:x', code: 'no_line' }, 404)).api.getLineTests('p', 'line:x'))
    expect(isMissingEndpoint(gone)).toBe(false)
    expect(gone.code).toBe('no_line')
  })

  it('允许 AI 经 MCP 运行单条功能线的测试：PUT …/mcp-tests {allow}，返回改后的', async () => {
    const { api, calls } = fakeFetch((_url, init) => json({ allow: JSON.parse(String(init?.body)).allow }))
    expect(await api.putMcpTests('p', true)).toBe(true)
    expect(await api.putMcpTests('p', false)).toBe(false)
    expect(calls.map((c) => [c.url, c.init?.method, c.init?.body])).toEqual([
      ['/api/projects/p/mcp-tests', 'PUT', JSON.stringify({ allow: true })],
      ['/api/projects/p/mcp-tests', 'PUT', JSON.stringify({ allow: false })],
    ])
  })

  it('运行的 scope：只跑一条线时读出来；没有（旧版后端、全量运行）、不像样时当作全量（null）', async () => {
    const scope = { line: 'line:wf.py::W.run', line_title: '转账', tests: 6, base_run: 2 }
    const r = await fakeFetch(() => json({ running: null, runs: [{ ...RUN, scope }, { ...RUN, id: 2, scope: null }, { ...RUN, id: 1, scope: { line: '' } }, { id: 0 }] })).api.listRuns('p')
    expect(r.runs.map((x) => x.scope)).toEqual([scope, null, null, null])
    const partial = await fakeFetch(() => json({ running: null, runs: [{ ...RUN, scope: { line: 'line:x', tests: 'many' } }] })).api.listRuns('p')
    expect(partial.runs[0].scope).toEqual({ line: 'line:x', line_title: '', tests: 0, base_run: null })
  })

  it('开始运行：409 confirm 带上命令和目录（界面弹确认）；400 no_command；409 busy', async () => {
    const confirm = { error: '第一次运行这条测试命令之前要确认：……', code: 'confirm', command: 'pytest -q', root: '/code/x' }
    const e = await caught(fakeFetch(() => json(confirm, 409)).api.startRun('p'))
    expect(e).toMatchObject({ kind: 'http', status: 409, code: 'confirm' })
    expect(e.data).toMatchObject({ command: 'pytest -q', root: '/code/x' })
    expect(await caught(fakeFetch(() => json({ error: '还没有填测试命令', code: 'no_command' }, 400)).api.startRun('p'))).toMatchObject({ code: 'no_command' })
    expect(await caught(fakeFetch(() => json({ error: '正在分析', code: 'busy' }, 409)).api.startRun('p'))).toMatchObject({ code: 'busy' })
  })

  it('列运行：running、runs、last_failure 原样；缺字段补成空值，不认识的状态、阶段照原样', async () => {
    const running = { started_at: '2026-11-17T04:17:02+00:00', seconds: 1.5, phase: 'warming' }
    const failure = { title: '运行测试', reason: '分析出错', code: 'analysis', at: '2026-11-17T04:17:03+00:00' }
    const { api, calls } = fakeFetch(() => json({ running, runs: [RUN, { id: 2, status: 'weird', summary: {} }], last_failure: failure }))
    const r = await api.listRuns('p')
    expect(calls[0].url).toBe('/api/projects/p/runs')
    expect(r.running).toEqual({ ...running, progress: null })
    expect(r.last_failure).toEqual(failure)
    expect(r.runs[0]).toEqual(RUN)
    expect(r.runs[1]).toMatchObject({ id: 2, status: 'weird', exit_code: null, paths_size: null, command: '' })
    expect(r.runs[1].summary).toMatchObject({ tests: 0, verify: null, coverage: null, mismatch_list: [], note: '' })
    const empty = await fakeFetch(() => json({ running: null, runs: [] })).api.listRuns('p')
    expect(empty).toEqual({ running: null, runs: [], last_failure: null })
  })

  it('运行中的进度：GET …/runs?live_line=<功能线 sid> 时带上这条线；progress 按字段读，缺的补空值；旧版后端没有 progress 时为 null', async () => {
    const progress = {
      collected: 925,
      done: 37,
      passed: 36,
      failed: 1,
      skipped: 0,
      errors: 0,
      running: ['tests/t.py::test_a', 7],
      running_more: 2,
      recent: [{ test: 'tests/t.py::test_b', outcome: 'failed', seconds: 1.5 }, { test: 'tests/t.py::test_c' }, 'x'],
      lines: { 'line:a.py::f': { functions: 3, total: 12 }, bad: 'x' },
      nodes: { 'a.py::f@1': ['start', 'step:1', 5], 'b.py::g@2': 'x' },
    }
    const running = { started_at: '2026-11-17T04:17:02+00:00', seconds: 30, phase: 'testing', progress }
    const { api, calls } = fakeFetch(() => json({ running, runs: [] }))
    const r = await api.listRuns('p', 'line:a.py::f #2')
    expect(calls[0].url).toBe(`/api/projects/p/runs?live_line=${encodeURIComponent('line:a.py::f #2')}`)
    expect(r.running?.progress).toEqual({
      collected: 925,
      done: 37,
      passed: 36,
      failed: 1,
      skipped: 0,
      errors: 0,
      running: ['tests/t.py::test_a'],
      running_more: 2,
      recent: [
        { test: 'tests/t.py::test_b', outcome: 'failed', seconds: 1.5 },
        { test: 'tests/t.py::test_c', outcome: '', seconds: 0 },
      ],
      lines: { 'line:a.py::f': { functions: 3, total: 12 } },
      nodes: { 'a.py::f@1': ['start', 'step:1'] },
    })
    const bare = await fakeFetch(() => json({ running: { ...running, progress: { done: 2 } }, runs: [] })).api.listRuns('p')
    expect(bare.running?.progress).toMatchObject({ collected: null, done: 2, running: [], recent: [], lines: {}, nodes: null })
    const old = await fakeFetch(() => json({ running: { started_at: 'x', seconds: 1, phase: 'testing' }, runs: [] })).api.listRuns('p')
    expect(old.running?.progress).toBeNull()
  })

  it('取消：POST …/runs/cancel，返回后端的说明（没有时空串）；没有在跑的 409 not_running，已经在整理结果的 409 too_late', async () => {
    const { api, calls } = fakeFetch(() => json({ cancelling: true }))
    expect(await api.cancelRun('p')).toBe('')
    expect(calls[0]).toMatchObject({ url: '/api/projects/p/runs/cancel', init: { method: 'POST' } })
    expect(await fakeFetch(() => json({ cancelling: true, note: '正在运行前的重新分析，分析完就停' })).api.cancelRun('p')).toBe('正在运行前的重新分析，分析完就停')
    expect(await caught(fakeFetch(() => json({ error: '没有在跑', code: 'not_running' }, 409)).api.cancelRun('p'))).toMatchObject({ code: 'not_running' })
    expect(await caught(fakeFetch(() => json({ error: '测试已经跑完，正在整理结果', code: 'too_late' }, 409)).api.cancelRun('p'))).toMatchObject({ code: 'too_late' })
  })

  it('核对结果 verify.ok 为 null（一条记录都没收到）：照原样 null，不当作一致', async () => {
    const run = { ...RUN, summary: { ...RUN.summary, verify: { ...RUN.summary.verify, ok: null }, note: '命令以退出码 4 结束（pytest：命令行用法错误）' } }
    const r = await fakeFetch(() => json({ running: null, runs: [run] })).api.listRuns('p')
    expect(r.runs[0].summary.verify?.ok).toBeNull()
    expect(r.runs[0].summary.note).toContain('退出码 4')
  })

  it('输出：GET …/runs/output 的 lines（只留字符串）', async () => {
    const { api } = fakeFetch(() => json({ lines: ['a', 3, '[pystepmap] 超时'] }))
    expect(await api.runOutput('p')).toEqual(['a', '[pystepmap] 超时'])
  })

  it('运行详情：GET …/runs/{id}?line=<功能线 sid>，每个测试带 on_line、整体带 line_known；不带 line 时 on_line 为 null', async () => {
    const tests = [
      { test: 'tests/t.py::a', outcome: 'passed', seconds: 0.1, cases: 2, functions: 5, on_line: 3 },
      { test: '', outcome: '', seconds: 0, cases: 0, functions: 1 },
    ]
    const { api, calls } = fakeFetch(() => json({ ...RUN, tests, line_known: true }))
    const d = await api.getRun('p', 3, 'line:a.py::f#2')
    expect(calls[0].url).toBe('/api/projects/p/runs/3?line=line%3Aa.py%3A%3Af%232')
    expect(d).toMatchObject({ id: 3, status: 'ok', line_known: true })
    expect(d.tests).toEqual([
      { test: 'tests/t.py::a', outcome: 'passed', seconds: 0.1, cases: 2, functions: 5, on_line: 3 },
      { test: '', outcome: '', seconds: 0, cases: 0, functions: 1, on_line: null },
    ])
    const old = await fakeFetch(() => json({ ...RUN, tests })).api.getRun('p', 3, 'line:x')
    expect(old.line_known).toBe(false)
    await api.getRun('p', 3)
    expect(calls[1].url).toBe('/api/projects/p/runs/3')
  })

  it('一个测试的运行路径：GET …/runs/{id}/paths?test=<编码过的 node id>；旧数据没有 shapes、function_dups 时补空', async () => {
    const t = { test: 'tests/t.py::a[x::y]', outcome: 'failed', seconds: 0.1, cases: 1, functions: { 'a.py::f': { function: 'a.py::f', executions: 1, nodes: { start: { visits: 1, ns: 5 } }, edges: {}, exits: { 'end:x|raise|ValueError': 1 }, first_path: ['start'], off_graph: 0, has_time: true } } }
    const body = { version: 1, tests: [t], shapes: { 'a.py::f': { shape: 'ab', dups: ['end:x'] } }, function_dups: ['a.py::g'] }
    const { api, calls } = fakeFetch(() => json(body, 200, { 'X-Pystepmap-Analysis': '7', 'X-Pystepmap-Run': '3' }))
    const r = await api.getTestPaths('p', 3, t.test)
    expect(calls[0].url).toBe('/api/projects/p/runs/3/paths?test=tests%2Ft.py%3A%3Aa%5Bx%3A%3Ay%5D')
    expect(r.analysisId).toBe(7)
    expect(r.paths).toMatchObject(body)
    // 失败的类型名、真实的调用（S12）：原样；没有时补空
    const withFacts = { version: 1, tests: [{ ...t, exc: 'ValueError', excs: ['AssertionError', 'ValueError'], functions: { 'a.py::f': { ...t.functions['a.py::f'], calls: { 'a.py::g': 2 }, icalls: { 'a.py::h': 1 } } } }] }
    const facts = (await fakeFetch(() => json(withFacts)).api.getTestPaths('p', 3, t.test)).paths.tests[0]
    expect(facts).toMatchObject({ exc: 'ValueError', excs: ['AssertionError', 'ValueError'] })
    expect(facts.functions['a.py::f'].calls).toEqual({ 'a.py::g': 2 })
    expect(facts.functions['a.py::f'].icalls).toEqual({ 'a.py::h': 1 }) // 经项目以外的帧的间接调用（S14）
    const old = await fakeFetch(() => json({ version: 1, tests: [t] })).api.getTestPaths('p', 3, '')
    expect(old.paths.tests[0]).toMatchObject({ exc: '', excs: [] })
    expect(old.paths.tests[0].functions['a.py::f'].calls).toEqual({})
    expect(old.paths.tests[0].functions['a.py::f'].icalls).toEqual({})
    expect(old.paths.shapes).toEqual({})
    expect(old.paths.function_dups).toEqual([])
    expect(old.analysisId).toBeNull()
    expect(await caught(fakeFetch(() => json({ version: 1 })).api.getTestPaths('p', 3, 'x'))).toMatchObject({ kind: 'format' })
    expect(await caught(fakeFetch(() => json({ error: '运行 #3 里没有这个测试：x' }, 404)).api.getTestPaths('p', 3, 'x'))).toMatchObject({ status: 404 })
  })

  it('没走到的结局：GET …/runs/{id}/uncovered?line=…', async () => {
    const o = { function: 'a.py::f', node: 'end:x', title: '超限', end_kind: 'failed', file: 'a.py', line: 9, uncertain: true }
    const { api, calls } = fakeFetch(() => json({ outcomes: [o, { function: 'a.py::f' }], stale: true }))
    const r = await api.getUncovered('p', 3, 'line:a.py::f')
    expect(calls[0].url).toBe('/api/projects/p/runs/3/uncovered?line=line%3Aa.py%3A%3Af')
    expect(r.stale).toBe(true)
    expect(r.outcomes[0]).toEqual(o)
    expect(r.outcomes[1]).toMatchObject({ node: '', uncertain: false, line: 0 })
  })

  it('isMissingEndpoint：旧版后端没有这个接口（404"没有这个接口"、不是 pystepmap 的 404）；项目不在了、运行不在了不算', async () => {
    const e = (body: unknown, status = 404) => caught(fakeFetch(() => json(body, status)).api.listRuns('p'))
    expect(isMissingEndpoint(await e({ error: '没有这个接口：GET /api/projects/p/runs' }))).toBe(true)
    expect(isMissingEndpoint(await e({ detail: 'Not Found' }))).toBe(true)
    expect(isMissingEndpoint(await e({ error: '项目不存在（可能已经删除了）：p' }))).toBe(false)
    expect(isMissingEndpoint(await e({ error: '项目不存在', code: 'not_found' }))).toBe(false)
    expect(isMissingEndpoint(await e({ error: '没有这个接口：x' }, 500))).toBe(false)
    expect(isMissingEndpoint(new Error('x'))).toBe(false)
  })
})

describe('需求', () => {
  // examples/transfer 的 GET /requirements：R001 对得上；R002 改出了少了 / 分类 / 多了 / 走向；R003 读不懂；R004 功能线还不存在
  const REQS = readFileSync(new URL('./graph/fixtures/requirements.json', import.meta.url), 'utf8')

  it('GET …/requirements：需求、对照、设计图（和分析结果同一个格式）、测试运行', async () => {
    const { api, calls } = fakeFetch(() => new Response(REQS, { headers: { 'Content-Type': 'application/json' } }))
    const r = await api.getRequirements('p')
    expect(calls[0].url).toBe('/api/projects/p/requirements')
    expect(r.requirements.map((x) => x.id)).toEqual(['R001', 'R002', 'R003', 'R004'])
    const [ok, diff, bad, noLine] = r.requirements
    expect(ok).toMatchObject({ title: '账户转账', status: '已确认', reachable_status: '已实现', errors: [], line: '账户转账' })
    expect(ok.comparison).toMatchObject({ matches: true, differences: [], line_sid: 'line:transfer_workflow.py::TransferWorkflow.run' })
    expect(ok.comparison!.outcomes).toHaveLength(7)
    expect(ok.comparison!.outcomes[0]).toEqual({ title: '重复请求，不再处理', kind: 'skipped', node: 'end:17497ec9', covered: 'unknown' })
    expect(diff.comparison!.differences.map((d) => d.kind)).toEqual(['missing', 'kind', 'extra', 'order'])
    expect(diff.comparison!.differences[0]).toMatchObject({ type: 'step', title: '风控打分', node: '', design_nodes: ['step:L20'] })
    expect(diff.comparison!.differences[2]).toMatchObject({ node: 'step:be6595ab', design_nodes: [] })
    expect(bad).toMatchObject({ comparison: null, design_line: null })
    expect(bad.errors).toHaveLength(2)
    expect(noLine.comparison!.differences[0]).toMatchObject({ kind: 'no_line', design_nodes: [] })
    expect(r.run).toBeNull()
    expect(r.graph?.lines.map((l) => l.id)).toEqual([ok.design_line, diff.design_line, noLine.design_line])
    expect(r.graph?.lines[0].group).toBe('需求')
    const fn = r.graph!.functions[ok.design_line!]
    expect(fn.nodes[0]).toMatchObject({ type: 'start', line: 0 })
    expect(fn.nodes[1]).toMatchObject({ sid: 'step:L1', title: '查询请求状态', line: 0 })
  })

  it('缺字段补成空值；不认识的差异、覆盖记成 other / unknown；设计图读不了时 graph 为 null；没有 requirements 是 format', async () => {
    const body = {
      requirements: [
        { id: 'R9', status: '已确认', comparison: { differences: [{ kind: 'renamed', text: '改了名' }], outcomes: [{ title: 'x', covered: 'maybe' }] } },
        {},
      ],
      graph: { schema_version: 99 },
      run: 4,
    }
    const r = await fakeFetch(() => json(body)).api.getRequirements('p')
    expect(r.requirements[0]).toMatchObject({ id: 'R9', title: '', errors: [], design_line: null, reachable_status: '已确认' })
    expect(r.requirements[0].comparison).toMatchObject({ line_sid: '', matches: false, notes: [] })
    expect(r.requirements[0].comparison!.differences[0]).toEqual({ kind: 'other', text: '改了名', type: '', title: '', node: '', design_nodes: [] })
    expect(r.requirements[0].comparison!.outcomes[0]).toEqual({ title: 'x', kind: 'ok', node: '', covered: 'unknown' })
    expect(r.requirements[1]).toMatchObject({ id: '', file: '', comparison: null, status: '', reachable_status: '' })
    expect(r.graph).toBeNull()
    expect(r.run).toBe(4)
    expect(await caught(fakeFetch(() => json({ graph: {} })).api.getRequirements('p'))).toMatchObject({ kind: 'format' })
  })

  it('旧版后端没有这个接口：404"没有这个接口"（isMissingEndpoint）；还没有分析结果的 404 不算', async () => {
    const old = await caught(fakeFetch(() => json({ error: '没有这个接口：GET /api/projects/p/requirements' }, 404)).api.getRequirements('p'))
    expect(isMissingEndpoint(old)).toBe(true)
    const none = await caught(fakeFetch(() => json({ error: '这个项目还没有分析结果' }, 404)).api.getRequirements('p'))
    expect(isMissingEndpoint(none)).toBe(false)
  })
})
