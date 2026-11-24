import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watchSlow } from './slow'

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((a, b) => ((resolve = a), (reject = b)))
  return { promise, resolve, reject }
}

describe('等得久了说一声', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('超过时限还没结束：标成"久"；结束时取消', async () => {
    const d = deferred<number>()
    const seen: boolean[] = []
    const done = watchSlow(d.promise, 30_000, (b) => seen.push(b))
    await vi.advanceTimersByTimeAsync(29_999)
    expect(seen).toEqual([])
    await vi.advanceTimersByTimeAsync(1)
    expect(seen).toEqual([true])
    d.resolve(7)
    expect(await done).toBe(7)
    expect(seen).toEqual([true, false])
  })

  it('在时限之前结束：什么都不说，定时器清掉（之后时间再走也不触发）', async () => {
    const d = deferred<string>()
    const seen: boolean[] = []
    const done = watchSlow(d.promise, 30_000, (b) => seen.push(b))
    d.resolve('ok')
    expect(await done).toBe('ok')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(seen).toEqual([])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('失败也照样取消，并把错误抛出去', async () => {
    const d = deferred<void>()
    const seen: boolean[] = []
    const done = watchSlow(d.promise, 10, (b) => seen.push(b))
    await vi.advanceTimersByTimeAsync(10)
    d.reject(new Error('坏了'))
    await expect(done).rejects.toThrow('坏了')
    expect(seen).toEqual([true, false])
    expect(vi.getTimerCount()).toBe(0)
  })
})
