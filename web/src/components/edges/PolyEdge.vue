<script setup lang="ts">
// 连线：照排版给的折线画，不按端点重新连——ELK 的走线把回边、并行的异常连线分开了。起点处的小台阶已在排版里拉直，
// 标签的位置也由排版定（贴着这条出边的第一段、不被别的连线穿过），这里照画，画出来的就是排版检查过的。
// 拐角做成小圆角；终点画箭头。case 模式被引擎截断的标签可以悬停看完整模式（data-tip，只有它接收鼠标）。
// 线宽、虚线、箭头随缩放加粗（CSS 变量 --sz，画布在缩小时调大），缩到很小也看得见走向。
// 和对比基准比新增的连线画成绿色（state.ts 的 marks）。运行路径页签：选中的测试走过的连线变蓝加粗，走了不止一次的
// 在中点写次数；没走到的变暗（runstate.ts 的 canvasRunMarks）。运行中（测试命令还在跑）不知道走的是哪条连线：都画成
// 半亮（run-live），不亮也不暗。
// 钻入链的连接线（同一个步骤的相邻两张钻入卡片之间，不是图里的连线）：细虚线、没有箭头，不标新增、走过、变暗；悬停时
// 说几个函数是按源码里出现的先后排的，不一定是执行的先后。
import { computed } from 'vue'
import { CHAIN_NOTE, type Point } from '../../graph'
import { marks } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { pointAlong } from '../../runview'
import type { EdgeData } from '../nodes/types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ id: string; data: EdgeData }>()

const RADIUS = 7
const ARROW = 6

/** arrow：终点画箭头（线停在箭头底部）；钻入链的连接线没有箭头，画到终点 */
function geometry(pts: Point[], arrow = true) {
  if (pts.length < 2) return { d: '', tip: null as Point | null, angle: 0 }
  const last = pts[pts.length - 1]
  const prev = pts[pts.length - 2]
  const len = Math.hypot(last.x - prev.x, last.y - prev.y) || 1
  const cut = arrow ? Math.min(ARROW - 1, len / 2) : 0
  // 线停在箭头底部，不从箭头尖上穿出去
  const end = { x: last.x - ((last.x - prev.x) * cut) / len, y: last.y - ((last.y - prev.y) * cut) / len }
  const all = [...pts.slice(0, -1), end]
  let d = `M${all[0].x},${all[0].y}`
  for (let i = 1; i < all.length - 1; i++) {
    const p0 = all[i - 1]
    const p1 = all[i]
    const p2 = all[i + 1]
    const l1 = Math.hypot(p1.x - p0.x, p1.y - p0.y)
    const l2 = Math.hypot(p2.x - p1.x, p2.y - p1.y)
    const r = Math.min(RADIUS, l1 / 2, l2 / 2)
    if (r < 0.5) {
      d += `L${p1.x},${p1.y}`
      continue
    }
    const a = { x: p1.x + ((p0.x - p1.x) * r) / l1, y: p1.y + ((p0.y - p1.y) * r) / l1 }
    const b = { x: p1.x + ((p2.x - p1.x) * r) / l2, y: p1.y + ((p2.y - p1.y) * r) / l2 }
    d += `L${a.x},${a.y}Q${p1.x},${p1.y} ${b.x},${b.y}`
  }
  d += `L${end.x},${end.y}`
  const angle = (Math.atan2(last.y - prev.y, last.x - prev.x) * 180) / Math.PI
  return { d, tip: last, angle }
}

const e = computed(() => props.data.edge)
const chain = computed(() => e.value?.chain === true)
const geo = computed(() => geometry(props.data.pos.points, !chain.value))
const label = computed(() => {
  const v = e.value
  if (!v?.label || !props.data.pos.label) return null
  const cut = v.labelFull !== v.label
  return { text: v.label, box: props.data.pos.label, cut, tip: cut ? `edge:${v.id}` : undefined }
})
/** 运行路径：走过几次（没走过 0）、要不要变暗 */
const walked = computed(() => {
  const m = canvasRunMarks.value
  const l = props.data.link
  const count = l ? (m.links.has(l.id) ? 1 : 0) : (m.edges.get(props.id) ?? 0)
  return { count, dim: m.active && !m.live && count === 0, live: m.live }
})
/** 次数标签的宽度：按位数（10px 的粗体数字约 6px 一位，"×" 和两边留白约 12px） */
const countWidth = computed(() => 12 + 6 * (String(walked.value.count).length + 1))
const countAt = computed(() => (walked.value.count > 1 && !props.data.link ? pointAlong(props.data.pos.points, 0.5) : null))
const cls = computed(() => {
  const run = { 'run-walked': walked.value.count > 0, 'run-dim': walked.value.dim, 'run-live': walked.value.live }
  const l = props.data.link
  if (l) return ['is-link', `res-${l.resolution}`, { indirect: !l.direct, ...run }]
  const v = e.value!
  if (v.chain) return ['chain']
  return [`k-${v.kind}`, { trunk: v.trunk, back: v.back, 'diff-added': marks.value.edges.has(v.id), ...run }]
})
</script>

<template>
  <g class="fm-edge" :class="cls">
    <path class="line" :d="geo.d" />
    <path v-if="chain" class="hit" :d="geo.d" data-tip="text" :data-tip-title="CHAIN_NOTE" data-tip-cut />
    <g v-if="geo.tip && !chain" :transform="`translate(${geo.tip.x},${geo.tip.y}) rotate(${geo.angle})`">
      <path class="head" :d="`M0,0L${-ARROW - 1.5},${-ARROW / 2 - 0.5}L${-ARROW - 1.5},${ARROW / 2 + 0.5}Z`" />
    </g>
    <g
      v-if="label"
      class="lbl"
      :class="[`tone-${data.tone}`, { 'has-full': label.cut }]"
      :data-tip="label.tip"
    >
      <rect :x="label.box.x" :y="label.box.y" :width="label.box.width" :height="label.box.height" rx="3" />
      <text :x="label.box.x + label.box.width / 2" :y="label.box.y + label.box.height / 2 + 0.5">{{ label.text }}</text>
    </g>
    <g v-if="countAt" class="run-count" :transform="`translate(${countAt.x},${countAt.y})`">
      <rect :x="-countWidth / 2" y="-8" :width="countWidth" height="16" rx="8" />
      <text x="0" y="0.5">×{{ walked.count }}</text>
    </g>
  </g>
</template>
