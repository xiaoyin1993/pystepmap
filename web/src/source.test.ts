import { describe, expect, it } from 'vitest'
import {
  CONTEXT,
  HEAD_LINES,
  MAX_SHOWN,
  functionHeadRequest,
  nodeSourceRequest,
  REVIEW_SOURCE_NOTE,
  outOfDate,
  reviewSourceNote,
  snippetForAi,
  sourceKey,
  type SourceSnippet,
} from './source'

const loc = (line: number, endLine = line) => ({ file: 'app/x.py', line, endLine, path: '/repo/app/x.py' })

describe('要取哪几行', () => {
  it('节点：自己的语句高亮，前后各 5 行；开头不早于第 1 行', () => {
    expect(nodeSourceRequest(loc(20, 22))).toEqual({ file: 'app/x.py', start: 15, end: 27, focusStart: 20, focusEnd: 22 })
    expect(nodeSourceRequest(loc(3))).toMatchObject({ start: 1, end: 3 + CONTEXT })
  })

  it('节点的语句很长：最多显示 MAX_SHOWN 行', () => {
    const r = nodeSourceRequest(loc(100, 400))!
    expect(r.end - r.start + 1).toBe(MAX_SHOWN)
    expect(r).toMatchObject({ focusStart: 100, focusEnd: 400 })
  })

  it('隐含节点（没有行号）不取', () => {
    expect(nodeSourceRequest(null)).toBeNull()
    expect(nodeSourceRequest(loc(0))).toBeNull()
  })

  it('卡片：函数开头最多 30 行，不超过函数末尾', () => {
    expect(functionHeadRequest(loc(50), 200)).toEqual({ file: 'app/x.py', start: 50, end: 50 + HEAD_LINES - 1, focusStart: 50, focusEnd: 50 })
    expect(functionHeadRequest(loc(50), 56)).toMatchObject({ start: 50, end: 56 })
    expect(functionHeadRequest(loc(50), null)).toMatchObject({ end: 79 })
    expect(functionHeadRequest(null, 10)).toBeNull()
  })

  it('缓存键带上项目、分析 id（重新分析后重新取，stale 重新判断）、文件、行号', () => {
    const req = nodeSourceRequest(loc(20))!
    expect(sourceKey({ project: 'k3x9', analysis: 3 }, req)).toBe('k3x9#3|app/x.py:15-25')
    expect(sourceKey({ project: 'k3x9', analysis: 4 }, req)).not.toBe(sourceKey({ project: 'k3x9', analysis: 3 }, req))
  })
})

const snip = (o: Partial<SourceSnippet>): SourceSnippet => ({
  file: 'app/x.py',
  start: 15,
  end: 17,
  total: 300,
  lines: ['a = 1', 'if a:', '    run()'],
  stale: false,
  ...o,
})

describe('图可能过期', () => {
  const req = nodeSourceRequest(loc(16))!
  it('指纹对不上、或要高亮的行超出文件末尾：算过期', () => {
    expect(outOfDate(snip({}), req)).toBe(false)
    expect(outOfDate(snip({ stale: null }), req)).toBe(false)
    expect(outOfDate(snip({ stale: true }), req)).toBe(true)
    expect(outOfDate(snip({ total: 12, lines: [] }), req)).toBe(true)
  })
})

describe('复制给 AI 的源码', () => {
  const req = nodeSourceRequest(loc(16, 17))!
  it('写文件、行号范围、节点在哪几行，然后是代码块', () => {
    expect(snippetForAi(snip({}), req, '节点')).toBe(
      '源码（app/x.py 第 15–17 行，节点在第 16–17 行）：\n```python\na = 1\nif a:\n    run()\n```',
    )
  })

  it('代码里有反引号：围栏更长；文件改过时注明；没有行时为空', () => {
    const s = snippetForAi(snip({ lines: ['x = "```"'], end: 15, stale: true }), req, '节点')
    expect(s).toContain('文件在生成图之后改过')
    expect(s).toContain('````python\nx = "```"\n````')
    expect(snippetForAi(snip({ lines: [] }), req, '节点')).toBe('')
  })

  it('看变更记录时：写给的说明（不按 stale）；说明为 null（确定是当时的代码）时什么都不加', () => {
    expect(snippetForAi(snip({ stale: true }), req, '节点', REVIEW_SOURCE_NOTE)).toContain(`节点在第 16–17 行；${REVIEW_SOURCE_NOTE}）`)
    expect(snippetForAi(snip({ stale: true }), req, '节点', null)).not.toContain('改过')
  })
})

describe('看变更记录时源码片段上说什么', () => {
  const req = nodeSourceRequest(loc(16))!
  it('记录那次分析确定还在、后端说没改过：确定是当时的代码，不写说明', () => {
    expect(reviewSourceNote(snip({ stale: false }), req, true)).toBeNull()
  })

  it('那次分析可能已经清掉了（后端会按最新一次比）、图里没有这个文件的指纹：一律说"这是现在的源码……"', () => {
    expect(reviewSourceNote(snip({ stale: false }), req, false)).toBe(REVIEW_SOURCE_NOTE)
    expect(reviewSourceNote(snip({ stale: true }), req, false)).toBe(REVIEW_SOURCE_NOTE)
    expect(reviewSourceNote(snip({ stale: null }), req, true)).toBe(REVIEW_SOURCE_NOTE)
  })

  it('确定改过（指纹对不上、行号超出文件末尾）：说改过了', () => {
    expect(reviewSourceNote(snip({ stale: true }), req, true)).toBe(`文件在那次分析之后改过：${REVIEW_SOURCE_NOTE}`)
    expect(reviewSourceNote(snip({ total: 12, lines: [] }), req, true)).toContain('改过')
  })
})

describe('从后端取片段（useSource）', () => {
  it('选中的东西变了才取；取过的直接用；重新分析（分析 id 变了）后重新取；出错写后端的说明', async () => {
    const { ref, nextTick } = await import('vue')
    const { vi } = await import('vitest')
    const { useSource } = await import('./source')
    const urls: string[] = []
    vi.stubGlobal('fetch', async (url: string) => {
      urls.push(url)
      if (url.includes('bad.py')) return new Response(JSON.stringify({ error: '文件不存在' }), { status: 404 })
      return new Response(JSON.stringify({ file: 'app/x.py', start: 15, end: 25, total: 300, lines: ['a'], stale: false }))
    })
    const settle = async () => {
      for (let i = 0; i < 5; i++) await nextTick()
      await new Promise((r) => setTimeout(r, 0))
    }
    try {
      const scope = ref<{ project: string; analysis: number | null } | null>({ project: 'p1', analysis: 1 })
      const req = ref(nodeSourceRequest(loc(20)))
      const s = useSource(scope, req)
      await settle()
      // 带上画布上那张图的分析 id（后端按那一次判断文件改过没有）
      expect(urls).toEqual(['/api/projects/p1/source?file=app%2Fx.py&start=15&end=25&analysis=1'])
      expect(s.status.value).toBe('ok')
      expect(s.snippet.value?.lines).toEqual(['a'])
      // 换个节点再换回来：第二次用缓存
      req.value = nodeSourceRequest(loc(40))
      await settle()
      req.value = nodeSourceRequest(loc(20))
      await settle()
      expect(urls).toHaveLength(2)
      scope.value = { project: 'p1', analysis: 2 }
      await settle()
      expect(urls).toHaveLength(3)
      req.value = nodeSourceRequest({ file: 'bad.py', line: 3, endLine: 3, path: '/r/bad.py' })
      await settle()
      expect(s.status.value).toBe('error')
      expect(s.error.value).toBe('文件不存在')
      req.value = null
      await settle()
      expect(s.status.value).toBe('none')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
