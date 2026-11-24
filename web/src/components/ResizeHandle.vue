<script setup lang="ts">
// 栏和画布之间的分界线：按住拖动改这一栏的宽度（指针捕获在分界线上，拖到画布或栏上面照样跟着走，在窗口外松开也会
// 结束），双击恢复默认宽度；有焦点时左右方向键每次挪 16 像素。拖动期间整个页面不选字（body 上的 fm-resizing），
// 按下的事件不往下传，画布不会开始平移。宽度的计算和夹取在 panelWidth.ts。
import { onBeforeUnmount, ref } from 'vue'
import { dragged, keyed, type PanelName } from '../panelWidth'

const props = defineProps<{ name: PanelName; width: number; min: number; max: number; label: string }>()
const emit = defineEmits<{
  /** 新宽度（还没夹） */
  resize: [width: number]
  /** 拖完、按完方向键：记下来 */
  commit: []
  reset: []
}>()

const dragging = ref(false)
let start: { x: number; width: number; pointer: number; el: HTMLElement } | null = null

function down(e: PointerEvent) {
  if (e.button !== 0) return
  e.preventDefault()
  e.stopPropagation()
  const el = e.currentTarget as HTMLElement
  el.setPointerCapture(e.pointerId)
  el.focus({ preventScroll: true })
  start = { x: e.clientX, width: props.width, pointer: e.pointerId, el }
  dragging.value = true
  document.body.classList.add('fm-resizing')
  window.addEventListener('blur', end)
}

function move(e: PointerEvent) {
  if (!start || e.pointerId !== start.pointer) return
  e.preventDefault()
  emit('resize', dragged(props.name, start.width, start.x, e.clientX))
}

function end() {
  if (!start) return
  const { el, pointer } = start
  start = null
  dragging.value = false
  document.body.classList.remove('fm-resizing')
  window.removeEventListener('blur', end)
  if (el.hasPointerCapture?.(pointer)) el.releasePointerCapture(pointer)
  emit('commit')
}

function key(e: KeyboardEvent) {
  const next = keyed(props.name, props.width, e.key)
  if (next === null) return
  e.preventDefault()
  emit('resize', next)
  emit('commit')
}

onBeforeUnmount(end)
</script>

<template>
  <div
    class="rh"
    :class="[`rh-${name}`, { dragging }]"
    role="separator"
    aria-orientation="vertical"
    tabindex="0"
    :aria-label="label"
    :aria-valuenow="width"
    :aria-valuemin="min"
    :aria-valuemax="max"
    :title="`${label}：拖动改宽窄，双击恢复默认`"
    @pointerdown="down"
    @pointermove="move"
    @pointerup="end"
    @pointercancel="end"
    @lostpointercapture="end"
    @dblclick="emit('reset')"
    @keydown="key"
  />
</template>

<style>
.rh {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 30;
  width: 6px;
  margin-left: -3px;
  cursor: col-resize;
  touch-action: none;
}
.rh::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 2px;
  background: transparent;
  transition: background 0.12s;
}
.rh:hover::after,
.rh.dragging::after,
.rh:focus-visible::after {
  background: var(--task-soft);
}
.rh:focus-visible {
  outline: none;
}
body.fm-resizing,
body.fm-resizing * {
  cursor: col-resize !important;
  user-select: none !important;
}
</style>
