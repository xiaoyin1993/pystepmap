<script setup lang="ts">
// 任务卡片（功能线任务树里的一个任务）和钻入卡片（内嵌展开的被调用函数）。
// 标题栏：标题一行、函数名一行；折叠的卡片多一行摘要。点标题栏展开 / 折叠（钻入卡片是收回）。
// 展开的卡片是容器：里面的节点、连线由画布另外画在它上面。
// 和对比基准比：新增的函数在右上角标"新增"、绿色外圈；函数自己的节点改了时折叠着的卡片有琥珀色外圈；折叠着的
// 卡片里有这条线的几处变化时右上角写几处（只是调用的函数变了的不画外圈，只写数）（state.ts 的 marks）。
// 运行路径页签：选中的测试没执行过这个函数时整张卡片变暗；收起的卡片里执行过时左上角一个蓝点（canvasRunMarks）。
// 钻入卡片：一个步骤可以同时钻入它调用的几个函数（钻入链，一个函数一张）。这个步骤还调用了没展开的函数时，链上每张
// 卡片的标题栏右边有"再展开 N 个"：下拉列出没展开的，选一个就在链上加一张（两个以上时还可以"都展开"）。没展开的
// 函数里有流程变化、失败的停点、没走到的结局时标在这个按钮上（diffview 的 counts、runview 的 stops / uncoveredInside
// 按钻入选项的 id 记）。
import { computed } from 'vue'
import { NDropdown, type DropdownDividerOption, type DropdownOption } from 'naive-ui'
import { CHAIN_NOTE, cardHeaderHeight, cardMoreText, cardMoreWidth, cardSubtitle, cardSummaryText } from '../../graph'
import { drill, flash, marks, select, selection, toggleCard } from '../../state'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses, uncoveredCountText } from '../../runview'
import Icon from '../Icon.vue'
import type { CardData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: CardData }>()
const c = computed(() => props.data.card)
const head = cardHeaderHeight()
const actionLabel = computed(() => {
  if (c.value.kind === 'drill') {
    return c.value.anchor?.drills.some((d) => d.open && d.id !== c.value.id) ? '收起这张钻入卡片' : '收回到原来的步骤'
  }
  if (!c.value.expandable) return c.value.missing ? '图里没有这个函数' : '这个函数没有节点'
  return c.value.expanded ? '收起这个任务' : '展开这个任务'
})

const status = computed(() => marks.value.cards.get(c.value.id) ?? null)
/** 折叠着时里面几处变化；新增的函数只标"新增" */
const inside = computed(() => (status.value === 'added' ? 0 : (marks.value.counts.get(c.value.id) ?? 0)))

const runCls = computed(() => runNodeClasses(canvasRunMarks.value, c.value.id, 'card'))
const uncInside = computed(() => canvasRunMarks.value.uncoveredInside.get(c.value.id) ?? null)

/** 钻入卡片：原来那个步骤还调用了、还没展开的函数（"再展开 N 个"列出它们） */
const rest = computed(() => (c.value.kind === 'drill' && c.value.anchor ? c.value.anchor.drills.filter((d) => !d.open) : []))
const moreText = computed(() => cardMoreText(c.value))
/** 标题给按钮让出的宽度（和排版的估算一样） */
const moreWidth = computed(() => cardMoreWidth(c.value))
/** 没展开的函数里：流程变化几处、失败停在里面、没走到的结局 */
const restMarks = computed(() =>
  rest.value.map((d) => ({
    d,
    changes: marks.value.counts.get(d.id) ?? 0,
    stop: canvasRunMarks.value.stops.has(d.id),
    unc: canvasRunMarks.value.uncoveredInside.get(d.id) ?? null,
  })),
)
const restChanges = computed(() => restMarks.value.reduce((n, m) => n + m.changes, 0))
const restStop = computed(() => restMarks.value.some((m) => m.stop))
const restUnc = computed(() => {
  const all = restMarks.value.filter((m) => m.unc).map((m) => m.unc!)
  return all.length ? { certain: all.reduce((n, u) => n + u.certain, 0), uncertain: all.reduce((n, u) => n + u.uncertain, 0) } : null
})
const markWords = (m: (typeof restMarks.value)[number]): string[] => [
  ...(m.changes ? [`${m.changes} 处变化`] : []),
  ...(m.stop ? ['失败停在这里面'] : []),
  ...(m.unc ? [uncoveredCountText(m.unc, 'inside')] : []),
]
const ALL = '\u0000all'
const moreOptions = computed(() => {
  const opts: (DropdownOption | DropdownDividerOption)[] = restMarks.value.map((m) => ({
    key: m.d.id,
    label: [`${m.d.title}（${m.d.name}）`, ...(m.d.resolution === 'guessed' ? ['推测'] : []), ...markWords(m)].join(' · '),
  }))
  if (rest.value.length > 1) opts.push({ type: 'divider', key: 'divider' }, { key: ALL, label: `都展开（${rest.value.length} 个）` })
  return opts
})
const moreTip = computed(() => {
  const names = restMarks.value.map((m) => `${m.d.name}${markWords(m).length ? `（${markWords(m).join('，')}）` : ''}`)
  return `原来的步骤还调用了 ${names.join('、')}：点这里选一个，在这一串里加一张它的卡片（${CHAIN_NOTE}）`
})
function openMore(key: string) {
  const a = c.value.anchor
  if (!a) return
  const opts = key === ALL ? rest.value : rest.value.filter((d) => d.id === key)
  if (opts.length) drill(a.id, opts, c.value.id)
}

function onHead() {
  select('card', c.value.id)
  toggleCard(c.value)
}
</script>

<template>
  <div
    class="fm-card"
    :class="[
      c.expanded ? 'is-open' : 'is-closed',
      `k-${c.kind}`,
      runCls,
      {
        root: c.kind === 'task' && c.depth === 0,
        missing: c.missing,
        flat: !c.expandable && c.kind === 'task',
        selected: selection?.id === c.id,
        flash: flash === c.id,
        'has-change': status === 'changed',
        'has-added': status === 'added',
      },
    ]"
    :style="{ width: `${data.width}px`, height: `${data.height}px` }"
  >
    <div
      class="card-head"
      :style="c.expanded ? { height: `${head}px` } : undefined"
      role="button"
      :aria-label="actionLabel"
      :data-tip="`card:${c.id}`"
      tabindex="0"
      @click="onHead"
      @keydown.enter.self="onHead"
    >
      <div class="card-title" data-clip="title" :style="moreWidth ? { paddingRight: `${moreWidth}px` } : undefined">{{ c.title }}</div>
      <div class="card-sub" data-clip="sub">{{ cardSubtitle(c) }}</div>
      <div v-if="!c.expanded" class="card-sum">{{ cardSummaryText(c) }}</div>
      <n-dropdown v-if="moreText" trigger="click" :options="moreOptions" size="small" @select="openMore">
        <button
          class="card-more"
          :class="{ 'has-change': restChanges > 0, 'run-stop': restStop, 'run-unc': restUnc, unsure: restUnc && !restUnc.certain }"
          data-tip-skip
          :aria-label="`原来的步骤还调用了 ${rest.length} 个函数：再展开`"
          :title="moreTip"
          @click.stop
          @keydown.enter.stop
        >
          {{ moreText }}<span v-if="restChanges" class="more-n">{{ restChanges }}</span
          ><span v-if="restUnc" class="more-n unc">{{ restUnc.certain || restUnc.uncertain }}</span>
        </button>
      </n-dropdown>
      <span v-if="c.expandable || c.kind === 'drill'" class="card-chev">
        <Icon :name="c.expanded ? 'chevron-up' : 'chevron-down'" :size="12" :stroke="2.6" />
      </span>
    </div>
    <span v-if="status === 'added'" class="fm-mark fm-mark-added" data-tip-skip title="对比基准之后新增的函数">新增</span>
    <span
      v-if="inside"
      class="fm-count at-corner"
      data-tip-skip
      :title="`里面有 ${inside} 处和对比基准不一样（含能钻入的函数），展开看`"
      @click="onHead"
      >{{ inside }}</span
    >
    <span
      v-if="uncInside"
      class="fm-run-unc"
      :class="{ unsure: !uncInside.certain }"
      data-tip-skip
      :title="`${uncoveredCountText(uncInside, 'inside')}，展开看`"
      @click="onHead"
      >{{ uncInside.certain || uncInside.uncertain }}</span
    >
  </div>
</template>
