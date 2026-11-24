// 改功能线的小框里，分组框下面的建议列表的键盘规则（LineEditor.vue 用；纯函数，测试见 suggest.test.ts）：
// - 列表出现时不预先选中任何一条：回车保存框里打的字；
// - 上下箭头在建议之间移动（走过最后一条、或者从第一条往上，回到"不选"，也就是框里打的字）；移到了某一条再回车，
//   才是选它；点一条也是选它；
// - 列表开着时按 Esc 只关列表；列表关着时再按 Esc 才是放弃这次编辑；
// - 再打字时列表重新出现，仍然不选中。

export interface SuggestState {
  /** 列表开着（有建议时才真的显示） */
  open: boolean
  /** 选中的建议的下标；-1 是没有选中 */
  active: number
}

/** pick：选 index 那条建议；save：保存；close-list：只关列表；cancel：放弃这次编辑 */
export type SuggestAction = { type: 'none' } | { type: 'pick'; index: number } | { type: 'save' } | { type: 'close-list' } | { type: 'cancel' }

export interface SuggestKeyResult {
  state: SuggestState
  action: SuggestAction
  /** 这个键被列表用掉了（调用方要 preventDefault，Esc 还要 stopPropagation） */
  handled: boolean
}

export const CLOSED: SuggestState = { open: false, active: -1 }

/** 打了字：有建议就打开列表，不选中 */
export function typed(count: number): SuggestState {
  return { open: count > 0, active: -1 }
}

/** 列表现在显示着没有 */
export const listShown = (s: SuggestState, count: number): boolean => s.open && count > 0

/** 分组框里按了 key（count 是现在的建议条数） */
export function suggestKey(s: SuggestState, key: string, count: number): SuggestKeyResult {
  const shown = listShown(s, count)
  const active = shown && s.active < count ? s.active : -1
  switch (key) {
    case 'ArrowDown':
      if (count === 0) return { state: s, action: { type: 'none' }, handled: false }
      return { state: { open: true, active: !shown ? 0 : active + 1 < count ? active + 1 : -1 }, action: { type: 'none' }, handled: true }
    case 'ArrowUp':
      if (count === 0) return { state: s, action: { type: 'none' }, handled: false }
      return { state: { open: true, active: !shown || active < 0 ? count - 1 : active - 1 }, action: { type: 'none' }, handled: true }
    case 'Enter':
      if (active >= 0) return { state: CLOSED, action: { type: 'pick', index: active }, handled: true }
      return { state: CLOSED, action: { type: 'save' }, handled: true }
    case 'Escape':
      if (shown) return { state: CLOSED, action: { type: 'close-list' }, handled: true }
      return { state: CLOSED, action: { type: 'cancel' }, handled: true }
    default:
      return { state: s, action: { type: 'none' }, handled: false }
  }
}
