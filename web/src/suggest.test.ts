import { describe, expect, it } from 'vitest'
import { CLOSED, listShown, suggestKey, typed, type SuggestState } from './suggest'

/** 从 s 开始依次按 keys，返回最后的状态和每一步的动作 */
function press(s: SuggestState, keys: string[], count: number) {
  const actions: string[] = []
  for (const k of keys) {
    const r = suggestKey(s, k, count)
    s = r.state
    actions.push(r.action.type === 'pick' ? `pick:${r.action.index}` : r.action.type)
  }
  return { s, actions }
}

describe('分组建议列表的键盘规则', () => {
  it('打字后列表出现但不选中任何一条：回车是保存框里打的字', () => {
    const s = typed(3)
    expect(s).toEqual({ open: true, active: -1 })
    expect(listShown(s, 3)).toBe(true)
    expect(suggestKey(s, 'Enter', 3)).toEqual({ state: CLOSED, action: { type: 'save' }, handled: true })
  })

  it('没有建议时列表不出现；箭头不被用掉', () => {
    expect(listShown(typed(0), 0)).toBe(false)
    expect(suggestKey(typed(0), 'ArrowDown', 0).handled).toBe(false)
    expect(suggestKey(typed(0), 'Enter', 0).action).toEqual({ type: 'save' })
  })

  it('用箭头移到某一条再回车：选它（不保存）；下箭头走过最后一条回到"不选"', () => {
    expect(press(typed(3), ['ArrowDown', 'Enter'], 3).actions).toEqual(['none', 'pick:0'])
    expect(press(typed(3), ['ArrowDown', 'ArrowDown', 'Enter'], 3).actions.at(-1)).toBe('pick:1')
    expect(press(typed(2), ['ArrowDown', 'ArrowDown', 'ArrowDown'], 2).s).toEqual({ open: true, active: -1 })
    expect(press(typed(2), ['ArrowDown', 'ArrowDown', 'ArrowDown', 'Enter'], 2).actions.at(-1)).toBe('save')
  })

  it('上箭头从"不选"到最后一条，从第一条回到"不选"', () => {
    expect(press(typed(3), ['ArrowUp'], 3).s.active).toBe(2)
    expect(press(typed(3), ['ArrowDown', 'ArrowUp'], 3).s.active).toBe(-1)
  })

  it('列表开着时 Esc 只关列表；再按 Esc 才是放弃编辑', () => {
    const r = press(typed(3), ['ArrowDown', 'Escape', 'Escape'], 3)
    expect(r.actions).toEqual(['none', 'close-list', 'cancel'])
    // 关了列表之后回车：保存框里的字，不选建议
    expect(press(typed(3), ['ArrowDown', 'Escape', 'Enter'], 3).actions.at(-1)).toBe('save')
  })

  it('列表关着时按下箭头：打开并选第一条', () => {
    expect(press(CLOSED, ['ArrowDown'], 2).s).toEqual({ open: true, active: 0 })
  })

  it('建议变少了（选中的下标超出）：当作没有选中', () => {
    expect(suggestKey({ open: true, active: 4 }, 'Enter', 2).action).toEqual({ type: 'save' })
  })

  it('别的键不管', () => {
    expect(suggestKey(typed(2), 'a', 2)).toEqual({ state: typed(2), action: { type: 'none' }, handled: false })
  })
})
