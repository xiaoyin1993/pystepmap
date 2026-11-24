<script setup lang="ts">
// 看变更记录时的横幅（顶栏下面，占满整个宽度）："正在看变更记录：<时间> 标为已看的那次变化（分析 #12 相对 #9）。这是
// 当时的图，不是现在的代码"，"上一次 / 下一次"走到更早 / 更晚的一条（不用再开对话框），"回到现在"。从现在打开一条
// 记录、还在读的时候只说"正在读"，可以取消。状态和动作在 state.ts（review、openReview、stepReview、leaveReview）。
import { computed } from 'vue'
import { NButton, NSpin } from 'naive-ui'
import { progressText, whenText } from '../format'
import { reviewBannerText, reviewNeighbors } from '../reviewview'
import { leaveReview, review, reviewLoading, reviewProgress, reviews, stepReview } from '../state'
import Icon from './Icon.vue'

const text = computed(() => (review.value ? reviewBannerText(review.value.info, whenText) : ''))
const nb = computed(() => (review.value ? reviewNeighbors(reviews.value, review.value.id) : null))
const loadingText = computed(() => {
  const l = reviewLoading.value
  if (!l) return ''
  const p = reviewProgress.value
  return `正在读变更记录 #${l.id} 当时的图…${p && p.received > 0 ? ` ${progressText(p.received, p.total)}` : ''}`
})
</script>

<template>
  <div v-if="review || reviewLoading" class="review-bar" role="region" aria-label="正在看变更记录">
    <span class="rb-mark" aria-hidden="true">记录</span>
    <span v-if="review" class="rb-text" :title="text">{{ text }}</span>
    <span v-else class="rb-text">{{ loadingText }}</span>
    <span v-if="review && reviewLoading" class="rb-loading"><n-spin :size="12" />正在读 #{{ reviewLoading.id }}…</span>
    <span v-else-if="reviewLoading" class="rb-loading"><n-spin :size="12" /></span>
    <span class="bar-gap" />
    <template v-if="review && nb">
      <span v-if="nb.index > 0" class="rb-pos" :title="`按时间从早到晚，第 ${nb.index} 条，共 ${nb.total} 条`">{{ nb.index }} / {{ nb.total }}</span>
      <n-button
        size="tiny"
        secondary
        :disabled="!nb.earlier || !!reviewLoading"
        :title="nb.earlier ? `看更早一次标为已看的变化（${whenText(nb.earlier.acked_at)}）` : '没有更早的、存着图的记录了'"
        @click="stepReview('earlier')"
      >
        <template #icon><Icon name="chevron-up" /></template>上一次
      </n-button>
      <n-button
        size="tiny"
        secondary
        :disabled="!nb.later || !!reviewLoading"
        :title="nb.later ? `看更晚一次标为已看的变化（${whenText(nb.later.acked_at)}）` : '没有更晚的记录了'"
        @click="stepReview('later')"
      >
        <template #icon><Icon name="chevron-down" /></template>下一次
      </n-button>
    </template>
    <n-button size="small" type="primary" class="rb-back" title="画布换回项目现在的图和变化，功能线、展开状态照看记录之前的样子" @click="leaveReview()">
      {{ review ? '回到现在' : '取消' }}
    </n-button>
  </div>
</template>

<style>
.review-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 7px 16px;
  font-size: 13px;
  color: var(--text);
  background: color-mix(in srgb, var(--finally) 16%, var(--panel));
  border-bottom: 1px solid color-mix(in srgb, var(--finally) 55%, transparent);
  box-shadow: inset 4px 0 0 var(--finally);
}
.rb-mark {
  flex: none;
  padding: 1px 6px;
  font-size: 11px;
  line-height: 16px;
  color: #0b0c0e;
  background: var(--finally);
  border-radius: 3px;
}
.rb-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rb-loading {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-soft);
}
.rb-pos {
  flex: none;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.rb-back {
  flex: none;
}
</style>
