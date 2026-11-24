<script setup lang="ts">
// 开始节点：一行的胶囊，写"开始"（函数标题已经在卡片标题栏上）。新增的函数里它也标"新增"。
import { computed } from 'vue'
import { pillText } from '../../graph'
import { marks, select, selection } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses } from '../../runview'
import Icon from '../Icon.vue'
import type { LeafData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: LeafData }>()
const n = computed(() => props.data.node)
const mark = computed(() => marks.value.nodes.get(n.value.id) ?? null)
const runCls = computed(() => runNodeClasses(canvasRunMarks.value, n.value.id))
const run = computed(() => canvasRunMarks.value.nodes.get(n.value.id) ?? null)
</script>

<template>
  <div
    class="fm-leaf fm-pill is-start"
    :class="[mark ? `mark-${mark}` : '', runCls, { selected: selection?.id === n.id }]"
    :style="{ width: `${data.width}px`, height: `${data.height}px` }"
    :data-tip="`node:${n.id}`"
    tabindex="0"
    @click="select('node', n.id)"
    @keydown.enter.self="select('node', n.id)"
  >
    <span class="pill-ico"><Icon name="play" :size="12" /></span>
    <span class="pill-text">{{ pillText(n) }}</span>
    <span
      v-if="mark"
      class="fm-mark"
      :class="`fm-mark-${mark}`"
      data-tip-skip
      :title="mark === 'added' ? '对比基准之后新增的' : '和对比基准比改动过（详情栏里有原来的样子）'"
      >{{ mark === 'added' ? '新增' : '改动' }}</span
    >
    <span v-if="run?.badge" class="fm-run-badge" data-tip-skip :title="`这个测试里执行了 ${run.visits} 次`">{{ run.badge }}</span>
  </div>
</template>
