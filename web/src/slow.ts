// "等得有点久了"：一个请求超过一段时间还没结束时说一声（添加项目、重新分析都要等引擎分析完才返回，大项目要几秒，
// 再大就更久）。纯逻辑，测试见 slow.test.ts。

/** 还在分析时的提示：项目很大时要等；刷新页面不会中断后端里的分析 */
export const SLOW_TEXT = '还在分析…项目很大时要等一会儿；刷新页面不会中断分析'

/** 等多久算久（毫秒；测试里改小） */
export const slowAfter = { ms: 30_000 }

/**
 * 等 p：超过 ms 还没结束时调 onSlow(true)；结束（成功或失败）时清掉定时器，调过 onSlow(true) 的再调 onSlow(false)。
 * 返回 p 的结果，失败照样抛出。
 */
export async function watchSlow<T>(p: Promise<T>, ms: number, onSlow: (slow: boolean) => void): Promise<T> {
  let slow = false
  const timer = setTimeout(() => {
    slow = true
    onSlow(true)
  }, ms)
  try {
    return await p
  } finally {
    clearTimeout(timer)
    if (slow) onSlow(false)
  }
}
