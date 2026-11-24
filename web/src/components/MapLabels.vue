<script setup lang="ts">
// 地图模式（缩得很小时）的任务卡片标题：画在屏幕坐标里、字号固定，像地图上的地名——缩多小都看得清。
// 互相压住时按重要程度留一个（展开的卡片 > 浅层的卡片 > 深层的、goto 卡片），被挤掉的卡片只剩色块。
// 点标题跳到那张卡片；鼠标停在标题上显示完整标题和函数名（FullTextTip）。
import { computed } from 'vue'
import { textWidth, type CardView, type Positioned } from '../graph'
import { shortTitle } from '../describe'
import { focusElement } from '../state'

const props = defineProps<{
  cards: CardView[]
  layout: Positioned | null
  zoom: number
  x: number
  y: number
  active: boolean
}>()

interface Label {
  id: string
  text: string
  left: number
  top: number
  width: number
  font: number
  kind: string
}

const labels = computed<Label[]>(() => {
  const p = props.layout
  if (!props.active || !p) return []
  const z = props.zoom
  const rank = (c: CardView) =>
    (c.goto ? 300 : 0) + (c.expanded ? 0 : 100) + (c.kind === 'drill' ? 50 : 0) + c.depth
  const placed: { l: number; t: number; r: number; b: number }[] = []
  const out: Label[] = []
  // 短标题在这条线里撞车（不同函数的文档注释同一个开头，如"主工作流：……"）时改用函数名
  const fnsByShort = new Map<string, Set<string>>()
  for (const c of props.cards) {
    const k = shortTitle(c.title)
    if (!fnsByShort.has(k)) fnsByShort.set(k, new Set())
    fnsByShort.get(k)!.add(c.fn ?? c.id)
  }
  const labelOf = (c: CardView) => {
    const k = shortTitle(c.title)
    return fnsByShort.get(k)!.size > 1 ? (c.name.split('.').pop() ?? c.name) : k
  }
  const free = (r: { l: number; t: number; r: number; b: number }) =>
    !placed.some((q) => r.l < q.r && q.l < r.r && r.t < q.b && q.t < r.b)
  const area = (c: CardView) => (p.boxes[c.id] ? p.boxes[c.id].width * p.boxes[c.id].height : 0)
  // 同样重要时大的卡片优先
  for (const c of [...props.cards].sort((a, b) => rank(a) - rank(b) || area(b) - area(a))) {
    const box = p.boxes[c.id]
    if (!box) continue
    const font = c.expanded && c.kind === 'task' && c.depth === 0 ? 15 : 13
    const h = Math.ceil(font * 1.35 + 8)
    const text = (c.goto ? '↩ ' : '') + labelOf(c)
    const natural = textWidth(text, font) + 18
    const sx = box.x * z
    const sy = box.y * z
    const sw = box.width * z
    const sh = box.height * z
    // 候选位置：展开的卡片贴左上角（不超出卡片，至少 200 像素宽）；折叠的卡片压在中间，放不下就挪到上方、下方
    const candidates: { left: number; top: number; width: number }[] = []
    if (c.expanded) {
      // 左上角；被占了就右上角，再不行沿左边往下挪
      const width = Math.min(natural, Math.max(sw - 8, 200))
      candidates.push(
        { left: sx + 4, top: sy + 4, width },
        { left: sx + sw - 4 - width, top: sy + 4, width },
        { left: sx + 4, top: sy + 4 + h + 6, width },
        { left: sx + 4, top: sy + 4 + 2 * (h + 6), width },
      )
    } else {
      const width = Math.min(natural, Math.max(sw + 24, 120))
      const left = sx + sw / 2 - width / 2
      candidates.push({ left, top: sy + sh / 2 - h / 2, width }, { left, top: sy - h - 2, width }, { left, top: sy + sh + 2, width })
    }
    for (const cand of candidates) {
      const r = { l: cand.left - 2, t: cand.top - 1, r: cand.left + cand.width + 2, b: cand.top + h + 1 }
      if (!free(r)) continue
      placed.push(r)
      out.push({
        id: c.id,
        text,
        left: cand.left,
        top: cand.top,
        width: cand.width,
        font,
        kind: c.goto ? 'goto' : c.kind === 'drill' ? 'drill' : c.expanded ? 'open' : 'closed',
      })
      break
    }
  }
  return out
})
</script>

<template>
  <div v-if="active" class="fm-maplabels">
    <div class="fm-maplabels-pane" :style="{ transform: `translate(${x}px, ${y}px)` }">
      <button
        v-for="l in labels"
        :key="l.id"
        class="maplabel"
        :class="`ml-${l.kind}`"
        :style="{ left: `${l.left}px`, top: `${l.top}px`, maxWidth: `${l.width}px`, fontSize: `${l.font}px` }"
        :data-tip="`maplabel:${l.id}`"
        @click="focusElement(l.id)"
      >
        {{ l.text }}
      </button>
    </div>
  </div>
</template>
