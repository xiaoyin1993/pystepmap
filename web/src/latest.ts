// 快速连点时"只认最后一次"：换项目、重新排版都是异步的，先发的可能后到。这里的逻辑不依赖 Vue，单独测（latest.test.ts）。

/** 发号器：每次 begin() 发一个新号，isLatest(号) 判断它是不是最后发出的那个。 */
export function createSequencer() {
  let seq = 0
  return {
    begin: (): number => ++seq,
    isLatest: (n: number): boolean => n === seq,
  }
}

export type LayoutReason = 'line' | 'state'

export interface LayoutApplied<R, A> {
  /** 这次排版是哪条功能线的 */
  line: string
  result: R
  /** 换了功能线（视口重新定位）还是改了展开状态（视口按锚点保持） */
  reason: LayoutReason
  /** 改展开状态时的锚点；reason 为 line 时为 null */
  anchor: A | null
}

/**
 * 排版调度：每次 request 都重新算（compute 是异步的），只有最后一次请求的结果会被 apply，之前的作废。
 * 换功能线的起因要留到"这条线的排版真正画上去"为止：换线还没排完时又来了一次改状态（全部展开、点卡片），
 * 后一次的结果仍按换线处理（视口重新定位，不按旧线的锚点挪）；换线的那次排版失败了，下一次成功的也按换线处理。
 */
export function createLayoutRunner<R, A>(
  compute: (line: string) => Promise<R>,
  apply: (applied: LayoutApplied<R, A>) => void,
  fail: (error: unknown) => void,
) {
  const seq = createSequencer()
  /** 还没画上去的换线：目标功能线 */
  let pendingLine: string | null = null
  return {
    async request(line: string, reason: LayoutReason, anchor: A | null = null): Promise<void> {
      if (reason === 'line') pendingLine = line
      const n = seq.begin()
      let result: R
      try {
        result = await compute(line)
      } catch (e) {
        if (seq.isLatest(n)) fail(e)
        return
      }
      if (!seq.isLatest(n)) return // 已经有更新的一次
      const isSwitch = pendingLine === line
      if (isSwitch) pendingLine = null
      apply({ line, result, reason: isSwitch ? 'line' : 'state', anchor: isSwitch ? null : anchor })
    },
    /** 有没有还没画上去的换线（测试和调试用） */
    get pendingLine(): string | null {
      return pendingLine
    },
  }
}

/**
 * 读项目：每次 select 都开始读，只有最后一次选的会被采用（onLoaded），"正在读"也只由最后一次结束。
 * 先选 A 再快速选 B：A 后读完也不会盖掉 B；A 先读完也不会把"正在读"清掉。K 是一次选择的内容（项目、要看的功能线）。
 * 读一个项目有好几步（图、手工项……）：都放在一次 load 里，整个算一次，任何一步慢了都落不到后选的项目上。
 * load 的第二个参数 isCurrent()：这次读取是不是还是最后一次（报告进度时用，被作废的读取不再改进度）。
 */
export function createLatestLoader<K, P>(
  load: (key: K, isCurrent: () => boolean) => Promise<P>,
  hooks: {
    setLoading: (loading: boolean) => void
    onLoaded: (key: K, value: P) => void | Promise<void>
    onError: (key: K, error: unknown) => void
  },
) {
  const seq = createSequencer()
  return async function select(key: K): Promise<void> {
    const n = seq.begin()
    hooks.setLoading(true)
    try {
      const value = await load(key, () => seq.isLatest(n))
      if (!seq.isLatest(n)) return
      await hooks.onLoaded(key, value)
    } catch (e) {
      if (seq.isLatest(n)) hooks.onError(key, e)
    } finally {
      if (seq.isLatest(n)) hooks.setLoading(false)
    }
  }
}
