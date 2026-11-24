<script setup lang="ts">
// 步骤、判断、循环、捕获共用的节点：左边类型图标，标题（最多两行），副标题（函数名 / 条件原文）和小标记。
// 尺寸照 nodeSize：宽高由画布给，字号、行高、内边距取 METRICS（画布根元素上的 CSS 变量）。
// 和对比基准比新增 / 改动的节点：描边加右上角的"新增""改动"角标；能钻入的被调函数里有变化时，右下角写几处
// （state.ts 的 marks，改了不重新排版）。
// "运行路径"页签：选中的测试走过它时左边一条蓝色竖条、角上写耗时（"×3 · 12 ms"），没走到时变暗；失败的停点红圈，
// 没走到的结局虚线框（runstate.ts 的 canvasRunMarks，同样不重新排版）。
// 需求的设计图：和实现对不上的节点虚线框，上方写"还没有实现""分类不一样""走向不一致"（state.ts 的 reqMarks）。
import { computed } from 'vue'
import { NDropdown, NTooltip, type DropdownDividerOption, type DropdownOption } from 'naive-ui'
import { nodeChips } from '../../graph'
import { closeFold, drill, foldById, hoverFold, marks, reqMarks, select, selection } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses, uncoveredCountText } from '../../runview'
import Icon, { type IconName } from '../Icon.vue'
import type { LeafData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: LeafData; icon: IconName }>()

const n = computed(() => props.data.node)
const chips = computed(() => nodeChips(n.value))
const hasSub = computed(() => n.value.subtitle !== '' || chips.value.length > 0)
const isSelected = computed(() => selection.value?.id === n.value.id)
const inHoverFold = computed(
  () => hoverFold.value !== null && (foldById.value.get(hoverFold.value)?.members.includes(n.value.id) ?? false),
)
/** 调用了几个能钻入的函数：下拉选一个钻入，或者都钻入（排成钻入链，同一条语句里的调用按源码里出现的先后） */
const ALL = '\u0000all'
const drillOptions = computed(() => {
  const opts: (DropdownOption | DropdownDividerOption)[] = n.value.drills.map((d) => ({
    key: d.id,
    label: `${d.title}（${d.name}）${d.resolution === 'guessed' ? ' · 推测' : ''}`,
  }))
  opts.push({ type: 'divider', key: 'divider' }, { key: ALL, label: `都钻入（${n.value.drills.length} 个）` })
  return opts
})
const CHIP_CLASS: Record<string, string> = { 派发: 'chip-dispatch', 推测: 'chip-guessed', 目标不明: 'chip-unresolved' }

function onDrill(key: string) {
  const opts = key === ALL ? n.value.drills : n.value.drills.filter((d) => d.id === key)
  if (opts.length) drill(n.value.id, opts)
}
const fe = computed(() => props.data.foldEntry)
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
const inside = computed(() => marks.value.counts.get(n.value.id) ?? 0)
const setHover = (id: string | null) => (hoverFold.value = id)
const runCls = computed(() => runNodeClasses(canvasRunMarks.value, n.value.id))
const run = computed(() => canvasRunMarks.value.nodes.get(n.value.id) ?? null)
/** 没钻入 / 钻入不到的被调函数里没走到的结局个数（标在调用它的这个节点上） */
const uncInside = computed(() => canvasRunMarks.value.uncoveredInside.get(n.value.id) ?? null)
/** 需求的设计图上：和实现对不上 */
const req = computed(() => reqMarks.value.nodes.get(n.value.id) ?? null)
</script>

<template>
  <div
    class="fm-leaf fm-block"
    :class="[`is-${n.type}`, mark ? `mark-${mark}` : '', req ? `req-${req.kinds[0]}` : '', runCls, { trunk: n.trunk, selected: isSelected, 'fold-hover': inHoverFold }]"
    :style="{ width: `${data.width}px`, height: `${data.height}px` }"
    :data-req-label="req?.label"
    :data-tip="`node:${n.id}`"
    tabindex="0"
    @click="select('node', n.id)"
    @keydown.enter.self="select('node', n.id)"
  >
    <span class="ico" :data-tip-skip="n.drills.length ? '' : undefined">
      <template v-if="n.drills.length === 1">
        <n-tooltip :delay="300">
          <template #trigger>
            <button class="ico-btn" :aria-label="`钻入 ${n.drills[0].name}`" @click.stop="onDrill(n.drills[0].id)">
              <Icon name="drill" :size="14" />
            </button>
          </template>
          钻入 {{ n.drills[0].name }}：在这里展开它的流程
        </n-tooltip>
      </template>
      <n-dropdown
        v-else-if="n.drills.length > 1"
        trigger="click"
        :options="drillOptions"
        size="small"
        @select="onDrill"
      >
        <button class="ico-btn" aria-label="钻入被调用的函数" @click.stop>
          <Icon name="drill" :size="14" />
        </button>
      </n-dropdown>
      <Icon v-else :name="icon" :size="14" />
    </span>
    <div class="txt">
      <div class="title" data-clip="title" :style="{ WebkitLineClamp: data.titleLines }">{{ n.title }}</div>
      <div v-if="hasSub" class="sub">
        <span class="code" data-clip="sub">{{ n.subtitle }}</span>
        <span v-if="chips.length" class="chips">
          <span v-for="c in chips" :key="c" class="chip" :class="CHIP_CLASS[c]">{{ c }}</span>
        </span>
      </div>
    </div>
    <n-tooltip v-if="fe" :delay="200">
      <template #trigger>
        <button
          class="collapse-btn"
          data-tip-skip
          aria-label="收起这段"
          @click.stop="closeFold(fe)"
          @mouseenter="setHover(fe.id)"
          @mouseleave="setHover(null)"
        >
          <Icon name="minus" :size="10" :stroke="3" />
        </button>
      </template>
      收起这段{{ fe.kind === 'catch' ? '异常处理' : `「${fe.label}」分支` }}（{{ fe.summary.nodes }} 个节点）
    </n-tooltip>
    <span
      v-if="mark"
      class="fm-mark"
      :class="`fm-mark-${mark}`"
      data-tip-skip
      :title="markTip"
      >{{ mark === 'added' ? '新增' : '改动' }}</span
    >
    <span v-if="inside" class="fm-count at-bottom" data-tip-skip :title="`钻入调用的函数，里面有 ${inside} 处变化`">{{ inside }}</span>
    <span v-if="run?.badge" class="fm-run-badge" data-tip-skip :title="`这个测试里走过 ${run.visits} 次（耗时含它调用的函数）`">{{ run.badge }}</span>
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
