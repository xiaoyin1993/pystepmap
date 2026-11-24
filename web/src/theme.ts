// 视觉方向：主干像一条地铁线——每个函数的主干画成一条粗亮的线，主干之外的连线和节点退后一层。
// 颜色只用来表达含义，一种颜色一个意思：钴蓝 = 任务卡片 / 派发，琥珀 = 判断，青 = 循环，玫红 = 异常处理，
// 结局四色（正常绿、跳过灰、失败红、未捕获异常紫）。底色是参考图那样的深石墨灰。
// 玫红（捕获、raise）和失败红挨得近：玫红偏品红、暗一档（色相差 35°，亮度对比 1.55:1），地图模式里捕获节点的色块
// 和以失败收尾的折叠、失败结局分得开。
// 字体：界面文字一律 PingFang SC（中文为主）；只有真正的代码标识（函数名、条件原文）用等宽字体。
// 尺寸（字号、行高、内边距）不在这里：它们由 src/graph/layout.ts 的 METRICS 决定，排版按那些数字估算。

import type { GlobalThemeOverrides } from 'naive-ui'
import { END_NAME, type EndKind, type FnKind, type NodeType, type Resolution } from './graph'

export const C = {
  /** 顶栏 */
  ink: '#0b0c0e',
  /** 左右栏 */
  panel: '#15161a',
  panelLine: '#25272d',
  /** 画布 */
  canvas: '#1a1b1f',
  dot: '#2c2e35',
  /** 节点底色、边框 */
  surface: '#25262c',
  surfaceLine: '#3a3c45',
  text: '#eceef2',
  textSoft: '#c5c8d0',
  muted: '#979aa6',
  faint: '#6b6e7a',

  task: '#4c7dff',
  taskSoft: '#8eabff',
  decision: '#e8a13a',
  loop: '#2fb8a8',
  catch: '#c8418f',
  finally: '#a194f2',

  ok: '#3fcf7f',
  skipped: '#9aa1b2',
  failed: '#ff6161',
  exception: '#cf7dff',

  trunk: '#d5d8e0',
  edge: '#646775',
}

export const END_COLOR: Record<EndKind, string> = {
  ok: C.ok,
  skipped: C.skipped,
  failed: C.failed,
  exception: C.exception,
}

/**
 * 流程变化（过期提示同改动）：不另造颜色，用现有的语义色——新增 = 成功绿（同正常结局），改动 = 警告琥珀（同判断、
 * 地图模式的读数）。变化一律画成外圈（中间隔一道画布色），节点本身的底色仍按类型，地图模式、小地图里也这样，
 * 不和判断节点、正常结局混。
 */
export const DIFF_COLOR = { added: C.ok, changed: C.decision } as const

/** 结局名：和折叠占位的摘要（graph/layout.ts 的 foldText）同一套 */
export { END_NAME }

/** 结局的严重程度，从重到轻：未捕获异常 > 失败 > 跳过 > 正常 */
export const END_SEVERITY: readonly EndKind[] = ['exception', 'failed', 'skipped', 'ok']

/** 几种结局里最严重的那个；没有结局时为 null */
export function mostSevereEnd(kinds: readonly EndKind[]): EndKind | null {
  return END_SEVERITY.find((k) => kinds.includes(k)) ?? null
}

/** 地图模式里折叠占位的底色：按里面最严重的结局上色，没有结局时中性灰 */
export function foldTint(kinds: readonly EndKind[]): string {
  const k = mostSevereEnd(kinds)
  return k ? END_COLOR[k] : C.faint
}

export const NODE_TYPE_NAME: Record<NodeType, string> = {
  start: '开始',
  step: '步骤',
  cond: '判断',
  loop: '循环',
  catch: '捕获异常',
  end: '结局',
}

export const FN_KIND_NAME: Record<FnKind, string> = {
  route: '接口',
  task: '后台任务',
  workflow: '工作流',
  step: '工作流步骤',
  condition: '条件',
  function: '函数',
}

export const RESOLUTION_NAME: Record<Resolution, string> = {
  exact: '确定',
  guessed: '推测',
  unresolved: '目标不明',
  external: '项目外',
}

export const RESOLUTION_HINT: Record<Resolution, string> = {
  exact: '按静态规则确定的项目内函数',
  guessed: '接收者类型未知，按方法名推测，不保证对',
  unresolved: '无法判断调用的是什么，可能进了项目代码',
  external: '标准库、第三方或字面量上的方法，项目里没有它的图',
}

/** HTTP 方法的颜色（主列表里的接口） */
export const METHOD_COLOR: Record<string, string> = {
  GET: C.ok,
  POST: C.taskSoft,
  PUT: C.decision,
  PATCH: C.finally,
  DELETE: C.failed,
}

/** 低于这个缩放比例进入"地图模式"：节点只剩色块，任务卡片的标题移到屏幕上按固定字号显示 */
export const MAP_ZOOM = 0.5

export const FONT_SANS = `'PingFang SC', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif`
export const FONT_MONO = `'SF Mono', ui-monospace, Menlo, 'JetBrains Mono', Consolas, monospace`

export const naiveOverrides: GlobalThemeOverrides = {
  common: {
    fontFamily: FONT_SANS,
    fontFamilyMono: FONT_MONO,
    primaryColor: C.task,
    primaryColorHover: '#6a93ff',
    primaryColorPressed: '#3b68e0',
    primaryColorSuppl: C.task,
    bodyColor: C.canvas,
    cardColor: C.panel,
    modalColor: C.panel,
    popoverColor: '#202227',
    tableColor: C.panel,
    borderRadius: '6px',
    borderRadiusSmall: '4px',
    textColor1: C.text,
    textColor2: C.textSoft,
    textColor3: C.muted,
    dividerColor: C.panelLine,
    borderColor: C.surfaceLine,
    inputColor: '#1d1e23',
    hoverColor: 'rgba(255,255,255,0.06)',
  },
  Input: { color: '#1d1e23', colorFocus: '#1d1e23', border: `1px solid ${C.panelLine}` },
  Tooltip: { color: '#24262c', textColor: C.text },
}
