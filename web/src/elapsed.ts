// 正在跑的测试已用了多久（运行面板、顶栏的"运行全部测试"共用）：上次查到的秒数加上之后本机走过的，两次查询之间每秒
// 自己走一下；查不到（正在退避重试）时停在上次查到的。只能在组件的 setup 里用（卸载时停掉计时器）。

import { computed, onBeforeUnmount, ref, watch, type ComputedRef } from 'vue'
import { running, runningAt, runningUnknown } from './runstate'
import { secondsText } from './runview'

/** "12 秒"、"2 分 5 秒"；没在跑时空串 */
export function useElapsed(): ComputedRef<string> {
  const now = ref(Date.now())
  let ticker: ReturnType<typeof setInterval> | undefined
  watch(
    running,
    (r) => {
      clearInterval(ticker)
      if (r) ticker = setInterval(() => (now.value = Date.now()), 1000)
    },
    { immediate: true },
  )
  onBeforeUnmount(() => clearInterval(ticker))
  return computed(() => {
    const r = running.value
    if (!r) return ''
    const since = runningUnknown.value ? 0 : (now.value - runningAt.value) / 1000
    return secondsText(Math.max(0, r.seconds + since)).replace(/\.\d 秒$/, ' 秒')
  })
}
