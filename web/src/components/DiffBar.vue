<script setup lang="ts">
// 差异栏（画布上方，选中的功能线相对对比基准有变化时出现）："这条线相对上次标为已看时（<时间>）：新增 3、改动 1、
// 删除 2"，"上一处""下一处"依次定位到每一处（需要时展开卡片 / 分支、钻入，把节点移到画布中间并选中；删掉的定位到
// 它原来前面的节点），"删除了什么"展开列出删掉的东西（标题、原来的位置），"标为已看"清掉这个项目的全部变化标记。
// 每一处变化、定位、文字都在 diffview.ts（lineChangeList、revealStop、diffHeadline），状态在 state.ts。
// 看变更记录时是那次的变化（同一套代码），"相对什么"按记录说（reviewview.ts 的 reviewBaseline），没有"标为已看"。
import { computed, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { whenText } from '../format'
import { changeStops, diffResult, diffView, gotoStop, lineSid, nextStop, review, reviews, stopIndex } from '../state'
import { baselineRef, diffHeadline, engineNote } from '../diffview'
import { reviewBaseline } from '../reviewview'
import AckButton from './AckButton.vue'
import Icon from './Icon.vue'

const showRemoved = ref(false)
watch([lineSid, () => review.value?.id], () => (showRemoved.value = false))

const stops = changeStops
const baseline = computed(() =>
  review.value ? reviewBaseline(review.value.info, reviews.value, whenText) : baselineRef(diffResult.value, whenText),
)
const headline = computed(() => diffHeadline(stops.value, diffView.value.lineStatus.get(lineSid.value), baseline.value))
/** 引擎变了、这条线上有所在文件没改的函数时的一句提醒（看变更记录时不说：记录里没有存两次的引擎） */
const engine = computed(() =>
  review.value ? null : engineNote(diffResult.value, diffView.value.lines.get(lineSid.value)?.functions ?? []),
)
const current = computed(() => (stopIndex.value >= 0 ? (stops.value[stopIndex.value] ?? null) : null))
/** 删掉的那几处（连同它们在整个列表里的位置，点一下定位过去） */
const removed = computed(() => stops.value.map((s, i) => ({ s, i })).filter((x) => x.s.kind === 'removed'))
const KIND_NAME = { added: '新增', changed: '改动', removed: '删除' } as const
</script>

<template>
  <div v-if="stops.length" class="diff-bar" role="region" aria-label="这条线的流程变化">
    <div class="db-row">
      <span class="db-text">{{ headline }}</span>
      <span class="db-nav">
        <n-button size="tiny" secondary aria-label="上一处变化" @click="nextStop(-1)">
          <template #icon><Icon name="chevron-up" /></template>上一处
        </n-button>
        <n-button size="tiny" secondary aria-label="下一处变化" @click="nextStop(1)">
          <template #icon><Icon name="chevron-down" /></template>下一处
        </n-button>
      </span>
      <button v-if="removed.length" class="bar-link" :aria-expanded="showRemoved" @click="showRemoved = !showRemoved">
        删除了什么（{{ removed.length }}）
      </button>
      <span class="bar-gap" />
      <AckButton v-if="!review" />
    </div>
    <div v-if="engine" class="db-engine">{{ engine }}</div>
    <div v-if="current" class="db-current">
      <span class="db-pos">第 {{ stopIndex + 1 }} / {{ stops.length }} 处</span>
      <span class="db-kind" :class="`k-${current.kind}`">{{ KIND_NAME[current.kind] }}</span>
      <span class="db-stop">{{ current.text }}</span>
    </div>
    <ul v-if="showRemoved && removed.length" class="db-removed">
      <li v-for="r in removed" :key="r.i">
        <button class="db-removed-item" @click="gotoStop(r.i)">
          <span>{{ r.s.text }}</span>
          <span v-if="r.s.where" class="db-where">{{ r.s.where }}</span>
        </button>
      </li>
    </ul>
  </div>
</template>

<style>
.db-engine {
  margin-top: 4px;
  color: var(--decision);
}
.diff-bar {
  flex: none;
  padding: 6px 18px;
  font-size: 12.5px;
  color: var(--text-soft);
  background: var(--panel);
  border-bottom: 1px solid var(--panel-line);
}
.db-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
}
.db-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.db-nav {
  display: flex;
  flex: none;
  gap: 4px;
}
.db-current {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  min-width: 0;
}
.db-pos {
  flex: none;
  color: var(--muted);
  font-size: 12px;
}
.db-kind {
  flex: none;
  font-size: 10.5px;
  line-height: 15px;
  padding: 0 5px;
  border-radius: 3px;
  border: 1px solid;
}
.db-kind.k-added {
  color: var(--ok);
}
.db-kind.k-changed {
  color: var(--decision);
}
.db-kind.k-removed {
  color: var(--muted);
}
.db-stop {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.db-removed {
  margin: 6px 0 2px;
  padding: 0;
  list-style: none;
  max-height: 180px;
  overflow: auto;
}
.db-removed-item {
  all: unset;
  box-sizing: border-box;
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  width: 100%;
  padding: 3px 6px;
  border-radius: 5px;
  cursor: pointer;
}
.db-removed-item:hover {
  background: rgba(255, 255, 255, 0.05);
}
.db-removed-item:focus-visible {
  outline: 2px solid var(--task-soft);
  outline-offset: -2px;
}
.db-where {
  color: var(--muted);
}
</style>
