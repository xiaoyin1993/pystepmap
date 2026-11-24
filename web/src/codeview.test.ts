import { describe, expect, it } from 'vitest'
import { commonIndent, dedentLines, focusIndexes, scrollTopFor } from './codeview'

const L = (texts: string[], from = 10) => texts.map((text, i) => ({ no: from + i, text }))

describe('公共缩进', () => {
  it('只去掉非空行都有的那部分，不超过最浅的一行；空白行不算', () => {
    expect(commonIndent(['        await x()', '    return y', '', '      z'])).toBe('    ')
    expect(commonIndent(['@router.get("/me")', 'async def me():', '    return 1'])).toBe('')
    expect(commonIndent(['  ', '\t'])).toBe('')
    expect(commonIndent([])).toBe('')
    // Tab 和空格混用：只取逐字符相同的那段
    expect(commonIndent(['\t  a', '\t b'])).toBe('\t ')
    expect(commonIndent(['\ta', '    b'])).toBe('')
  })

  it('每一行去掉一样多；空白行最多去掉这么多；行号不变', () => {
    const { lines, removed } = dedentLines(L(['        await auth_service.logout(db)', '', '  ', '    return UserMeResponse.model_validate(user)']))
    expect(removed).toBe('    ')
    expect(lines).toEqual([
      { no: 10, text: '    await auth_service.logout(db)' },
      { no: 11, text: '' },
      { no: 12, text: '' },
      { no: 13, text: 'return UserMeResponse.model_validate(user)' },
    ])
  })

  it('有一行顶格（装饰器、def）时什么都不去掉：行首的字一个不少', () => {
    const src = ['@router.get("/me", response_model=UserMeResponse)', 'async def me(user: CurrentUser) -> UserMeResponse:', '    return UserMeResponse.model_validate(user)']
    expect(dedentLines(L(src)).lines.map((l) => l.text)).toEqual(src)
  })
})

describe('让高亮的行出现在框里', () => {
  // 行高 17，上下内边距 6，框可见 200 像素（大约 11 行），一共 40 行
  const at = (first: number, last: number, current = 0) => scrollTopFor(first, last, 40, 17, 6, 200, current)

  it('已经整行看得见：不动', () => {
    expect(at(3, 3)).toBe(0)
    expect(at(20, 20, 300)).toBe(300)
  })

  it('看不见：放在上方三分之一处；不超出能滚动的范围', () => {
    const top = 6 + 25 * 17
    expect(at(25, 25)).toBe(top - Math.floor((200 - 17) / 3))
    expect(at(39, 39)).toBe(2 * 6 + 40 * 17 - 200) // 最后一行：滚到底为止
    expect(at(0, 0, 300)).toBe(0)
  })

  it('高亮的一段比框还高：第一行贴着顶', () => {
    expect(at(10, 30)).toBe(6 + 10 * 17)
  })

  it('没有高亮的行：不动', () => {
    expect(at(-1, -1, 42)).toBe(42)
  })

  it('高亮的行号 → 下标', () => {
    const lines = L(['a', 'b', 'c', 'd'], 100)
    expect(focusIndexes(lines, 101, 102)).toEqual([1, 2])
    expect(focusIndexes(lines, 103, null)).toEqual([3, 3])
    expect(focusIndexes(lines, 99, 100)).toEqual([0, 0])
    expect(focusIndexes(lines, 200, 210)).toEqual([-1, -1])
    expect(focusIndexes(lines, null, null)).toEqual([-1, -1])
  })
})
