<script setup lang="ts">
// 结局：一行的胶囊，按 ok / skipped / failed / exception 四种颜色。隐含的"执行到函数末尾""未捕获异常退出"用虚线边框。
// 和对比基准比新增 / 改动的结局：描边加"新增""改动"角标（同 BlockNode）。运行路径的标注、需求设计图上的标记同 BlockNode。
import { computed } from 'vue'
import { pillText } from '../../graph'
import { foldById, hoverFold, marks, reqMarks, select, selection } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses, uncoveredCountText } from '../../runview'
import Icon, { type IconName } from '../Icon.vue'
import type { LeafData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: LeafData }>()
const n = computed(() => props.data.node)
const mark = computed(() => marks.value.nodes.get(n.value.id) ?? null)
/** 只因为调用的函数（判断函数等）变了才标的"改动" */
const byCallee = computed(() => marks.value.callee.has(n.value.id))
const markTip = computed(() =>
  mark.value === 'added'
    ? '对比基准之后新增的'
    : byCallee.value
      ? '调用的函数变了（详情栏里写着变了什么）'
      : '和对比基准比改动过（详情栏里有原来的样子）',
)
const kind = computed(() => n.value.endKind ?? 'ok')
const runCls = computed(() => runNodeClasses(canvasRunMarks.value, n.value.id))
const run = computed(() => canvasRunMarks.value.nodes.get(n.value.id) ?? null)
/** 没钻入 / 钻入不到的被调函数里没走到的结局个数（标在调用它的这个节点上） */
const uncInside = computed(() => canvasRunMarks.value.uncoveredInside.get(n.value.id) ?? null)
/** 需求的设计图上：和实现对不上（没有实现、分类不一样、走向不一致） */
const req = computed(() => reqMarks.value.nodes.get(n.value.id) ?? null)
const ICON: Record<string, IconName> = { ok: 'ok', skipped: 'skipped', failed: 'failed', exception: 'exception' }
const inHoverFold = computed(
  () => hoverFold.value !== null && (foldById.value.get(hoverFold.value)?.members.includes(n.value.id) ?? false),
)
</script>

<template>
  <div
    class="fm-leaf fm-pill is-end"
    :class="[
      `end-${kind}`,
      mark ? `mark-${mark}` : '',
      req ? `req-${req.kinds[0]}` : '',
      runCls,
      { implicit: n.implicit !== null, selected: selection?.id === n.id, trunk: n.trunk, 'fold-hover': inHoverFold },
    ]"
    :style="{ width: `${data.width}px`, height: `${data.height}px` }"
    :data-req-label="req?.label"
    :data-tip="`node:${n.id}`"
    tabindex="0"
    @click="select('node', n.id)"
    @keydown.enter.self="select('node', n.id)"
  >
    <span class="pill-ico"><Icon :name="ICON[kind]" :size="13" :stroke="2.4" /></span>
    <span class="pill-text" data-clip="title">{{ pillText(n) }}</span>
    <span
      v-if="mark"
      class="fm-mark"
      :class="`fm-mark-${mark}`"
      data-tip-skip
      :title="markTip"
      >{{ mark === 'added' ? '新增' : '改动' }}</span
    >
    <span v-if="run?.badge" class="fm-run-badge" data-tip-skip :title="`这个测试里在这里结束 ${run.visits} 次`">{{ run.badge }}</span>
    <span
      v-if="uncInside"
      class="fm-run-unc"
      :class="{ unsure: !uncInside.certain }"
      data-tip-skip
      :title="uncoveredCountText(uncInside, 'callee')"
      >{{ uncInside.certain || uncInside.uncertain }}</span
    >
  </div>
</template>
