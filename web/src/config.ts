// 分析配置的文字和对象之间的转换。"添加项目"和"分析配置"两个弹窗里，用户填的是引擎的配置 JSON（和
// pystepmap analyze --config 的文件一样，例如 {"noise_receivers": ["logger", "log"]}）。纯函数，测试见 config.test.ts。
// 示例不用 exclude：给了 exclude 就取代默认的排除目录（.venv、node_modules、tests……），照抄示例会把虚拟环境分析进来。

/** 两个弹窗里配置的示例 */
export const CONFIG_EXAMPLE = '{"noise_receivers": ["logger", "log"]}'

/** 两个弹窗里关于排除目录的提醒 */
export const EXCLUDE_NOTE =
  'exclude 会取代默认的排除目录（.venv、node_modules、tests…），要保留就一起写上；只想多排除一个目录用 extra_exclude，例如 {"extra_exclude": ["migrations"]}。'

export const BAD_CONFIG = `分析配置不是合法的 JSON 对象（形如 ${CONFIG_EXAMPLE}）`

/** 填的文字 → 配置对象。空着（只有空白）是 null：全用默认值。不是 JSON 对象时返回中文说明。 */
export function parseConfigText(text: string): { ok: true; config: Record<string, unknown> | null } | { ok: false; error: string } {
  const t = text.trim()
  if (!t) return { ok: true, config: null }
  let v: unknown
  try {
    v = JSON.parse(t)
  } catch {
    return { ok: false, error: BAD_CONFIG }
  }
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return { ok: false, error: BAD_CONFIG }
  return { ok: true, config: v as Record<string, unknown> }
}

/** 配置对象 → 编辑框里的文字：缩进两格；全用默认值时写 {} */
export function configText(config: Record<string, unknown>): string {
  return Object.keys(config).length ? JSON.stringify(config, null, 2) : '{}'
}
