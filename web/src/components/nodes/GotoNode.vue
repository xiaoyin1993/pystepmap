<script setup lang="ts">
// "回到某某任务"：这个任务在本条线里已经出现过。点一下跳到它第一次出现的卡片，那张卡片短暂高亮。
import { computed } from 'vue'
import { cardSubtitle, cardSummaryText } from '../../graph'
import { gotoCard, selection } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses } from '../../runview'
import Icon from '../Icon.vue'
import type { CardData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: CardData }>()
const c = computed(() => props.data.card)
const runCls = computed(() => runNodeClasses(canvasRunMarks.value, c.value.id, 'card'))
function go() {
  if (c.value.gotoTarget) gotoCard(c.value.gotoTarget)
}
</script>

<template>
  <div
    class="fm-card is-closed k-goto"
    :class="[runCls, { selected: selection?.id === c.id }]"
    :style="{ width: `${data.width}px`, height: `${data.height}px` }"
  >
    <div
      class="card-head"
      role="button"
      :aria-label="`跳到前面的 ${c.name}`"
      :data-tip="`card:${c.id}`"
      tabindex="0"
      @click="go"
      @keydown.enter.self="go"
    >
      <div class="card-title" data-clip="title">{{ c.title }}</div>
      <div class="card-sub" data-clip="sub">{{ cardSubtitle(c) }}</div>
      <div class="card-sum">{{ cardSummaryText(c) }}</div>
      <span class="card-chev"><Icon name="goto" :size="12" :stroke="2.6" /></span>
    </div>
  </div>
</template>
