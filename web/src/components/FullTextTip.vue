<script setup lang="ts">
// 鼠标停在被截断的字上（约 300 毫秒）时显示完整文字：整个界面共用这一个浮层（Naive UI 的 n-popover，手动定位）。
// 哪里会被截断，就在那个元素上写 data-tip：
//   node:<节点 id>   card:<卡片 id>（标题栏）   maplabel:<卡片 id>（地图模式的标题标签）
//   edge:<连线 id>（分支标签，只在 case 模式被引擎截断时写）
//   text（界面别处：完整文字写在 data-tip-title / data-tip-sub 上；那里显示的是引擎截断过的形式时另写 data-tip-cut）
// 元素里会被截断的那几行写 data-clip（节点、卡片分 "title" / "sub"），悬停时量它们有没有溢出。显示什么、什么时候显示
// 见 fulltext.ts（引擎截断过的，放得下也显示）。元素里自己有提示的按钮（钻入、收起）写 data-tip-skip，停在它们上面时
// 不显示。完整文字可能很长（结局"返回 …"的源码最长 2000 字）：浮层限高，里面可以滚动。
//
// 不碍事：只听冒泡上来的 pointerover / pointerdown，不拦点击；按下鼠标（拖动画布）、滚轮（平移 / 缩放）、画布在动、
// 重新排版时立刻收起，滚轮停下前不再弹出。浮层里的字可以选中复制（鼠标从元素挪到浮层上时不收起）。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NPopover } from 'naive-ui'
import { cardFullText, edgeFullText, mapLabelFullText, nodeFullText, textFullText, type DomClip, type FullText } from '../fulltext'
import { canvasMoving, cardById, edgeById, layout, layoutTick, nodeById } from '../state'

const SHOW_DELAY = 300
const HIDE_DELAY = 200
/** 滚轮之后这么久之内不弹出（画布还在滑） */
const QUIET_AFTER_WHEEL = 400

const show = ref(false)
const x = ref(0)
const y = ref(0)
const text = ref<FullText | null>(null)

let shownFor: HTMLElement | null = null
let pendingFor: HTMLElement | null = null
let showTimer: ReturnType<typeof setTimeout> | undefined
let hideTimer: ReturnType<typeof setTimeout> | undefined
let pressed = false
let quietUntil = 0

const overflows = (el: Element | null): boolean =>
  !!el && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)

function domClip(el: Element): DomClip {
  return { title: overflows(el.querySelector('[data-clip="title"]')), sub: overflows(el.querySelector('[data-clip="sub"]')) }
}

function resolve(el: HTMLElement): FullText | null {
  const key = el.dataset.tip ?? ''
  const i = key.indexOf(':')
  const kind = i < 0 ? key : key.slice(0, i)
  const id = i < 0 ? '' : key.slice(i + 1)
  // 地图模式：画布根元素上有 fm-map
  const map = el.closest('.fm-map') !== null
  if (kind === 'node') {
    const n = nodeById.value.get(id)
    const b = layout.value?.boxes[id]
    return n && b ? nodeFullText(n, b.width, map, domClip(el)) : null
  }
  if (kind === 'card') {
    const c = cardById.value.get(id)
    const b = layout.value?.boxes[id]
    return c && b ? cardFullText(c, b.width, map, domClip(el)) : null
  }
  if (kind === 'maplabel') {
    const c = cardById.value.get(id)
    return c ? mapLabelFullText(c, el.textContent?.trim() ?? '', overflows(el)) : null
  }
  if (kind === 'edge') {
    const e = edgeById.value.get(id)
    return e ? edgeFullText(e) : null
  }
  if (kind === 'text') {
    const clipped = overflows(el) || [...el.querySelectorAll('[data-clip]')].some(overflows)
    return textFullText(el.dataset.tipTitle ?? '', el.dataset.tipSub ?? null, clipped, el.dataset.tipCut !== undefined)
  }
  return null
}

function hideNow() {
  clearTimeout(showTimer)
  clearTimeout(hideTimer)
  hideTimer = undefined
  show.value = false
  shownFor = null
  pendingFor = null
}

const suppressed = () => pressed || canvasMoving.value || Date.now() < quietUntil

function fire(trigger: HTMLElement) {
  pendingFor = null
  if (!trigger.isConnected || suppressed()) return
  const ft = resolve(trigger)
  if (!ft) return
  const r = trigger.getBoundingClientRect()
  x.value = r.left + r.width / 2
  y.value = r.top
  text.value = ft
  shownFor = trigger
  show.value = true
}

function onOver(e: PointerEvent) {
  const target = e.target
  if (!(target instanceof Element)) return
  if (target.closest('.fm-fulltip')) {
    clearTimeout(hideTimer)
    hideTimer = undefined
    return
  }
  const hit = target.closest('[data-tip-skip], [data-tip]') as HTMLElement | null
  const trigger = hit && hit.hasAttribute('data-tip') ? hit : null
  if (trigger && trigger === shownFor) {
    clearTimeout(hideTimer)
    hideTimer = undefined
    return
  }
  if (trigger && trigger === pendingFor) return
  clearTimeout(showTimer)
  pendingFor = null
  if (shownFor) {
    if (trigger) hideNow()
    else if (hideTimer === undefined) hideTimer = setTimeout(hideNow, HIDE_DELAY)
  }
  if (trigger && !suppressed()) {
    pendingFor = trigger
    showTimer = setTimeout(() => fire(trigger), SHOW_DELAY)
  }
}

function onOut(e: PointerEvent) {
  // 鼠标离开窗口
  if (e.relatedTarget === null && (shownFor || pendingFor)) {
    clearTimeout(showTimer)
    pendingFor = null
    if (shownFor && hideTimer === undefined) hideTimer = setTimeout(hideNow, HIDE_DELAY)
  }
}

function onDown(e: PointerEvent) {
  if (e.target instanceof Element && e.target.closest('.fm-fulltip')) return // 在浮层里选字
  pressed = true
  hideNow()
}
const onUp = () => (pressed = false)
function onWheel(e: WheelEvent) {
  if (e.target instanceof Element && e.target.closest('.fm-fulltip')) return // 在浮层里滚动长文字
  quietUntil = Date.now() + QUIET_AFTER_WHEEL
  hideNow()
}
function onScroll(e: Event) {
  if (e.target instanceof Element && e.target.closest('.fm-fulltip')) return
  quietUntil = Date.now() + QUIET_AFTER_WHEEL
  hideNow()
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') hideNow()
}
function onBlur() {
  pressed = false
  hideNow()
}

watch(canvasMoving, (m) => m && hideNow())
watch(layoutTick, hideNow)

onMounted(() => {
  document.addEventListener('pointerover', onOver)
  document.addEventListener('pointerout', onOut)
  document.addEventListener('pointerdown', onDown, true)
  document.addEventListener('pointerup', onUp, true)
  document.addEventListener('pointercancel', onUp, true)
  document.addEventListener('wheel', onWheel, { capture: true, passive: true })
  document.addEventListener('scroll', onScroll, true)
  document.addEventListener('keydown', onKey)
  window.addEventListener('blur', onBlur)
})
onBeforeUnmount(() => {
  hideNow()
  document.removeEventListener('pointerover', onOver)
  document.removeEventListener('pointerout', onOut)
  document.removeEventListener('pointerdown', onDown, true)
  document.removeEventListener('pointerup', onUp, true)
  document.removeEventListener('pointercancel', onUp, true)
  document.removeEventListener('wheel', onWheel, { capture: true })
  document.removeEventListener('scroll', onScroll, true)
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('blur', onBlur)
})
</script>

<template>
  <n-popover :show="show" :x="x" :y="y" trigger="manual" placement="top" class="fm-fulltip" :style="{ maxWidth: '420px' }">
    <div v-if="text" class="fulltip-body">
      <div class="fulltip-title">{{ text.title }}</div>
      <code v-if="text.sub" class="fulltip-sub">{{ text.sub }}</code>
    </div>
  </n-popover>
</template>

<style>
.fm-fulltip {
  user-select: text;
}
.fulltip-body {
  max-height: min(60vh, 520px);
  overflow-y: auto;
  overscroll-behavior: contain;
}
.fulltip-title {
  font-size: 13px;
  line-height: 1.55;
  color: var(--text);
  white-space: normal;
  overflow-wrap: anywhere;
}
.fulltip-sub {
  display: block;
  margin-top: 4px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-soft);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
