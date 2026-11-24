import { describe, expect, it } from 'vitest'
import { createLatestLoader, createLayoutRunner, type LayoutApplied } from './latest'

/** 手动控制什么时候完成的异步任务 */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** 假的排版：每次 compute 返回一个挂起的任务，测试里按任意顺序完成 */
function fakeLayout() {
  const calls: { line: string; d: ReturnType<typeof deferred<string>> }[] = []
  const applied: LayoutApplied<string, string>[] = []
  const errors: unknown[] = []
  const runner = createLayoutRunner<string, string>(
    (line) => {
      const d = deferred<string>()
      calls.push({ line, d })
      return d.promise
    },
    (a) => applied.push(a),
    (e) => errors.push(e),
  )
  return { runner, calls, applied, errors }
}

describe('排版调度', () => {
  it('只有最后一次请求的结果会画上去，先发后到的作废', async () => {
    const { runner, calls, applied } = fakeLayout()
    const p1 = runner.request('L1', 'state', 'a1')
    const p2 = runner.request('L1', 'state', 'a2')
    calls[1].d.resolve('second')
    calls[0].d.resolve('first')
    await Promise.all([p1, p2])
    expect(applied).toEqual([{ line: 'L1', result: 'second', reason: 'state', anchor: 'a2' }])
  })

  it('换线还没排完时来了一次改状态：后一次的结果仍按换线处理（视口重新定位，不用旧线的锚点）', async () => {
    const { runner, calls, applied } = fakeLayout()
    const sw = runner.request('L2', 'line')
    // 用户马上点了"全部展开"：锚点是旧线上的根卡片
    const st = runner.request('L2', 'state', 'old-root')
    calls[1].d.resolve('L2 全部展开')
    calls[0].d.resolve('L2 默认')
    await Promise.all([sw, st])
    expect(applied).toEqual([{ line: 'L2', result: 'L2 全部展开', reason: 'line', anchor: null }])
    expect(runner.pendingLine).toBeNull()
    // 之后的改状态照常按锚点
    const next = runner.request('L2', 'state', 'card')
    calls[2].d.resolve('L2 收起')
    await next
    expect(applied[1]).toMatchObject({ reason: 'state', anchor: 'card' })
  })

  it('换线的排版先完成、改状态的后完成：先按换线画，再按锚点改', async () => {
    const { runner, calls, applied } = fakeLayout()
    const sw = runner.request('L2', 'line')
    calls[0].d.resolve('L2 默认')
    await sw
    const st = runner.request('L2', 'state', 'card')
    calls[1].d.resolve('L2 展开')
    await st
    expect(applied.map((a) => [a.result, a.reason])).toEqual([
      ['L2 默认', 'line'],
      ['L2 展开', 'state'],
    ])
  })

  it('连换两条线：只画最后那条，而且按换线处理', async () => {
    const { runner, calls, applied } = fakeLayout()
    const a = runner.request('L2', 'line')
    const b = runner.request('L3', 'line')
    calls[1].d.resolve('L3')
    calls[0].d.resolve('L2')
    await Promise.all([a, b])
    expect(applied).toEqual([{ line: 'L3', result: 'L3', reason: 'line', anchor: null }])
  })

  it('换线的排版失败：报错；下一次成功的仍按换线处理', async () => {
    const { runner, calls, applied, errors } = fakeLayout()
    const sw = runner.request('L2', 'line')
    calls[0].d.reject(new Error('排不出来'))
    await sw
    expect(errors).toHaveLength(1)
    expect(applied).toEqual([])
    const st = runner.request('L2', 'state', 'x')
    calls[1].d.resolve('L2')
    await st
    expect(applied).toEqual([{ line: 'L2', result: 'L2', reason: 'line', anchor: null }])
  })

  it('作废的那次失败了不报错', async () => {
    const { runner, calls, applied, errors } = fakeLayout()
    const a = runner.request('L1', 'state', 'a')
    const b = runner.request('L1', 'state', 'b')
    calls[0].d.reject(new Error('旧的'))
    calls[1].d.resolve('新的')
    await Promise.all([a, b])
    expect(errors).toEqual([])
    expect(applied.map((x) => x.result)).toEqual(['新的'])
  })
})

describe('读项目', () => {
  function fakeLoader() {
    const pending = new Map<string, ReturnType<typeof deferred<string>>>()
    const loading: boolean[] = []
    const loaded: string[] = []
    const errors: string[] = []
    const select = createLatestLoader<string, string>(
      (name) => {
        const d = deferred<string>()
        pending.set(name, d)
        return d.promise
      },
      {
        setLoading: (b) => loading.push(b),
        onLoaded: (name, value) => void loaded.push(`${name}:${value}`),
        onError: (name) => void errors.push(name),
      },
    )
    return { select, pending, loading, loaded, errors }
  }

  it('先选 A 再选 B，A 后读完：结果是 B，A 被丢掉；"正在读"只在 B 完成时清掉', async () => {
    const { select, pending, loading, loaded } = fakeLoader()
    const a = select('A')
    const b = select('B')
    pending.get('B')!.resolve('b')
    await b
    expect(loaded).toEqual(['B:b'])
    expect(loading).toEqual([true, true, false])
    pending.get('A')!.resolve('a')
    await a
    expect(loaded).toEqual(['B:b'])
    expect(loading).toEqual([true, true, false])
  })

  it('先选 A 再选 B，A 先读完：A 不被采用，也不把"正在读"清掉', async () => {
    const { select, pending, loading, loaded } = fakeLoader()
    const a = select('A')
    const b = select('B')
    pending.get('A')!.resolve('a')
    await a
    expect(loaded).toEqual([])
    expect(loading).toEqual([true, true])
    pending.get('B')!.resolve('b')
    await b
    expect(loaded).toEqual(['B:b'])
    expect(loading.at(-1)).toBe(false)
  })

  it('isCurrent：被后一次选择作废的读取知道自己作废了（不再报告进度）', async () => {
    const checks: Record<string, () => boolean> = {}
    const pending = new Map<string, ReturnType<typeof deferred<string>>>()
    const select = createLatestLoader<string, string>(
      (name, isCurrent) => {
        checks[name] = isCurrent
        const d = deferred<string>()
        pending.set(name, d)
        return d.promise
      },
      { setLoading: () => {}, onLoaded: () => {}, onError: () => {} },
    )
    const a = select('A')
    expect(checks.A()).toBe(true)
    const b = select('B')
    expect(checks.A()).toBe(false)
    expect(checks.B()).toBe(true)
    pending.get('A')!.resolve('a')
    pending.get('B')!.resolve('b')
    await Promise.all([a, b])
  })

  it('读失败只报最后一次的错', async () => {
    const { select, pending, errors, loading } = fakeLoader()
    const a = select('A')
    const b = select('B')
    pending.get('A')!.reject(new Error('a'))
    pending.get('B')!.reject(new Error('b'))
    await Promise.all([a, b])
    expect(errors).toEqual(['B'])
    expect(loading.at(-1)).toBe(false)
  })
})
