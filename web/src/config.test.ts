import { describe, expect, it } from 'vitest'
import { BAD_CONFIG, configText, parseConfigText } from './config'

describe('分析配置的文字', () => {
  it('空着是 null（全用默认值）；JSON 对象原样取出', () => {
    expect(parseConfigText('  \n ')).toEqual({ ok: true, config: null })
    expect(parseConfigText(' {"exclude": ["gen"], "source_roots": ["src"]} ')).toEqual({
      ok: true,
      config: { exclude: ['gen'], source_roots: ['src'] },
    })
  })

  it('不是合法 JSON、不是对象：中文说明', () => {
    for (const bad of ['{exclude: 1}', '[]', '"x"', 'null', '3']) expect(parseConfigText(bad)).toEqual({ ok: false, error: BAD_CONFIG })
  })

  it('对象写回文字：缩进两格；空对象写 {}，再读回来一样', () => {
    expect(configText({})).toBe('{}')
    const cfg = { exclude: ['gen'], rules: { fastapi: true } }
    expect(configText(cfg)).toBe('{\n  "exclude": [\n    "gen"\n  ],\n  "rules": {\n    "fastapi": true\n  }\n}')
    expect(parseConfigText(configText(cfg))).toEqual({ ok: true, config: cfg })
  })
})
