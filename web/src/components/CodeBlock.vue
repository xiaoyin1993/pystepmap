<script setup lang="ts">
// 详情栏里所有代码框共用的显示（节点的源码片段、被调用的函数、整个函数）：行号一栏在左、代码一栏在右，两栏分开——
// 横向滚动只滚代码那一栏，行号永远不会盖住代码。去掉公共缩进（codeview.ts，绝不多于最浅的那一行），高亮的行
// 带底色。内容换了（或第一次显示）时横向滚回最左，竖向让高亮的行出现在框里。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { dedentLines, focusIndexes, scrollTopFor, type CodeLine } from '../codeview'

const props = withDefaults(
  defineProps<{
    lines: CodeLine[]
    focusStart?: number | null
    focusEnd?: number | null
    /** 框的最大高度（CSS 长度）；null 时不限高，跟着内容长 */
    maxHeight?: string | null
    label?: string
  }>(),
  { focusStart: null, focusEnd: null, maxHeight: null, label: '源码' },
)

/** 和样式里的 .cb-line 行高、.cb-scroll 上下内边距一致 */
const LINE = 17
const PAD = 6

const scroller = ref<HTMLElement | null>(null)
const codeCol = ref<HTMLElement | null>(null)
const shown = computed(() => dedentLines(props.lines).lines)
const focus = computed(() => focusIndexes(shown.value, props.focusStart, props.focusEnd))
const hit = (i: number) => i >= focus.value[0] && i <= focus.value[1] && focus.value[0] >= 0

async function place() {
  await nextTick()
  if (codeCol.value) codeCol.value.scrollLeft = 0
  const s = scroller.value
  if (!s) return
  const [first, last] = focus.value
  s.scrollTop = scrollTopFor(first, last, shown.value.length, LINE, PAD, s.clientHeight, first >= 0 ? s.scrollTop : 0)
}

watch(() => [props.lines, props.focusStart, props.focusEnd], () => void place())
onMounted(() => void place())
</script>

<template>
  <div
    ref="scroller"
    class="cb-scroll"
    :style="{ maxHeight: maxHeight ?? undefined, '--cb-lh': `${LINE}px`, '--cb-pad': `${PAD}px` }"
    tabindex="0"
    :aria-label="label"
  >
    <div class="cb-rows">
      <div class="cb-gutter" aria-hidden="true">
        <div v-for="(r, i) in shown" :key="r.no" class="cb-no" :class="{ hit: hit(i) }">{{ r.no }}</div>
      </div>
      <div ref="codeCol" class="cb-code">
        <div class="cb-code-inner">
          <div v-for="(r, i) in shown" :key="r.no" class="cb-line" :class="{ hit: hit(i) }">{{ r.text || ' ' }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
.cb-scroll {
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: var(--cb-pad) 0;
  box-sizing: border-box;
}
.cb-scroll:focus-visible {
  outline: 2px solid var(--task-soft);
  outline-offset: -2px;
}
.cb-rows {
  display: flex;
  align-items: flex-start;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: var(--cb-lh);
}
.cb-gutter {
  flex: none;
  min-width: 44px;
  box-sizing: border-box;
  border-right: 1px solid #23252b;
  user-select: none;
}
.cb-no {
  padding: 0 10px 0 8px;
  text-align: right;
  color: #5d606b;
}
.cb-no.hit {
  color: var(--task-soft);
  background: #1a2238;
  box-shadow: inset 2px 0 0 var(--task);
}
.cb-code {
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
}
.cb-code-inner {
  width: max-content;
  min-width: 100%;
}
.cb-line {
  padding: 0 12px 0 10px;
  white-space: pre;
  tab-size: 4;
  color: #b8bcc6;
}
.cb-line.hit {
  background: rgba(76, 125, 255, 0.16);
  color: #eef1ff;
}
</style>
