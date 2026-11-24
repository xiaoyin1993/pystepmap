<script setup lang="ts">
// 顶栏的"变更记录"：每次点"标为已看"，平台把那次的流程变化连同当时的图存一条（最近 10 条）；这里列出来，点一条看当时
// 改了什么（文字明细，和差异栏里说的是同一份内容）。存着图的记录可以"在图上看"：关掉对话框，画布换成那时的图、标出
// 那次的变化（state.ts 的 openReview，横幅是 ReviewBanner.vue）；没存图的（旧版本存的）只能看文字；旧版后端不说存没存
// 图时不给这个按钮。项目没有记录、旧版后端没有这个接口时整个入口不显示。列表由 state.ts 取（loadReviews）。只读。
import { computed, ref, watch } from 'vue'
import { NButton, NModal, NSpin } from 'naive-ui'
import { api, errorText, type ReviewDetail, type ReviewInfo } from '../api'
import { whenText } from '../format'
import { reviewBlocks } from '../reviewtext'
import { openReview, projectId, review, reviewLoading, reviews } from '../state'
import Icon from './Icon.vue'

const open = ref(false)
const picked = ref<number | null>(null)
const detail = ref<ReviewDetail | null>(null)
const failure = ref('')
const loading = ref(false)

watch(
  () => projectId.value,
  () => {
    open.value = false
    picked.value = null
    detail.value = null
  },
)

/** 一条记录的一句话：几条功能线（前几条的标题），节点新增 / 删除 / 改动各几个 */
function summaryText(r: ReviewInfo): string {
  const s = r.summary
  const titles = s.lines.slice(0, 3).map((l) => l.title).join('、') + (s.lines.length > 3 ? ' 等' : '')
  const parts = [s.added ? `新增 ${s.added}` : '', s.removed ? `删除 ${s.removed}` : '', s.changed ? `改动 ${s.changed}` : ''].filter(Boolean)
  return `${s.lines.length} 条功能线${titles ? `（${titles}）` : ''}${parts.length ? ` · 节点${parts.join('、')}` : ''}`
}

let detailTurn = 0
async function pick(id: number) {
  picked.value = id
  detail.value = null
  failure.value = ''
  const mine = ++detailTurn
  loading.value = true
  try {
    const got = await api.getReview(projectId.value, id)
    if (mine === detailTurn) detail.value = got
  } catch (e) {
    if (mine === detailTurn) failure.value = errorText(e)
  } finally {
    if (mine === detailTurn) loading.value = false
  }
}

/** 打开对话框：正在画布上看一条记录时先选它，否则选上次选的、没有时最新的一条 */
function show() {
  open.value = true
  const list = reviews.value
  if (!list.length) return
  const viewing = review.value?.id
  const want = viewing !== undefined && list.some((r) => r.id === viewing) ? viewing : picked.value
  void pick(want !== null && list.some((r) => r.id === want) ? want : list[0].id)
}

/** 明细按块排版（不把 ##、- 这些记号原样给人看） */
const blocks = computed(() => reviewBlocks(detail.value?.text ?? ''))
const pickedInfo = computed(() => reviews.value.find((r) => r.id === picked.value) ?? null)
/** 有没有一条存着图（底下的说明按它写） */
const anyGraph = computed(() => reviews.value.some((r) => r.has_graph === true))

function onCanvas(id: number) {
  open.value = false
  // 从现在进来：浏览器多一条历史，"后退"回到现在
  void openReview(id, '', review.value === null)
}
</script>

<template>
  <n-button
    v-if="reviews.length"
    size="small"
    secondary
    class="rv-button"
    :class="{ on: review !== null }"
    title="以前标为已看的流程变化：每次点“标为已看”存一条，留最近 10 条"
    @click="show"
  >
    变更记录<span class="rv-n">{{ reviews.length }}</span>
  </n-button>
  <n-modal v-model:show="open" preset="card" title="变更记录" class="pd" :style="{ width: 'min(1040px, 94vw)' }">
    <div class="rv-body">
      <ul class="rv-list" aria-label="变更记录">
        <li v-for="r in reviews" :key="r.id">
          <button class="rv-item" :class="{ on: r.id === picked }" :aria-current="r.id === picked ? 'true' : undefined" @click="pick(r.id)">
            <span class="rv-when">
              {{ whenText(r.acked_at) }} 标为已看
              <span v-if="review?.id === r.id" class="rv-tag">画布上正在看</span>
            </span>
            <span class="rv-sum">{{ summaryText(r) }}</span>
          </button>
        </li>
      </ul>
      <div class="rv-detail">
        <div v-if="pickedInfo" class="rv-top">
          <p class="rv-head">
            分析 #{{ pickedInfo.head_id }}（{{ whenText(pickedInfo.head_at) }}）相对
            {{ pickedInfo.base_id === null ? '更早的一次' : `分析 #${pickedInfo.base_id}` }}{{ pickedInfo.base_at ? `（${whenText(pickedInfo.base_at)}）` : '' }}
            的流程变化。文件和行号是当时的，现在的代码可能已经不同。
          </p>
          <n-button
            v-if="pickedInfo.has_graph === true"
            class="rv-canvas"
            size="small"
            type="primary"
            :loading="reviewLoading?.id === pickedInfo.id"
            title="关掉这个对话框，画布换成那时的图，标出这次的变化（只读）"
            @click="onCanvas(pickedInfo.id)"
          >
            <template #icon><Icon name="fit" /></template>在图上看
          </n-button>
          <span v-else-if="pickedInfo.has_graph === false" class="rv-nograph">这条记录没有存图，只能看文字</span>
        </div>
        <div v-if="loading" class="rv-loading"><n-spin :size="14" /> 正在读这条记录…</div>
        <p v-else-if="failure" class="rp-error">读这条记录出错：{{ failure }}</p>
        <div v-else-if="detail" class="rv-text">
          <template v-for="(b, i) in blocks" :key="i">
            <p v-if="b.kind === 'para'" class="rv-para">{{ b.text }}</p>
            <h3 v-else-if="b.kind === 'line'" class="rv-line">
              <span class="rv-chip" :class="`s-${b.status}`">{{ b.status }}</span>
              <span class="rv-line-title">{{ b.title }}</span>
              <code v-for="w in b.where" :key="w" class="rv-where">{{ w }}</code>
            </h3>
            <h4 v-else-if="b.kind === 'fn'" class="rv-fn">
              <span class="rv-chip small" :class="`s-${b.status}`">{{ b.status }}</span>
              <span>函数 {{ b.title }}</span>
              <code v-for="w in b.where" :key="w" class="rv-where">{{ w }}</code>
            </h4>
            <div v-else class="rv-item" :class="`g-${b.group}`">
              <span v-if="b.group" class="rv-tag">{{ b.group }}</span>
              <span class="rv-item-text">{{ b.text }}</span>
              <code v-for="w in b.where" :key="w" class="rv-where">{{ w }}</code>
            </div>
          </template>
        </div>
      </div>
    </div>
    <p class="d-hint rv-hint">
      每次点“标为已看”存一条，每个项目留最近 10 条。<template v-if="anyGraph"
        >点“在图上看”在画布上看那时的图和那次的变化（只读，看完点“回到现在”）。</template
      ><template v-else>只有文字明细，不能在画布上回看当时的图。</template>
    </p>
  </n-modal>
</template>

<style>
.rv-button {
  flex: none;
}
.rv-button.on {
  box-shadow: inset 0 0 0 1px var(--finally);
}
.rv-n {
  margin-left: 6px;
  font-size: 11px;
  color: var(--faint);
}
.rv-body {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 14px;
  min-height: 300px;
}
.rv-list {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 64vh;
  overflow: auto;
}
.rv-item {
  all: unset;
  box-sizing: border-box;
  width: 100%;
  display: grid;
  gap: 3px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
}
.rv-item:hover,
.rv-item:focus-visible {
  background: rgba(255, 255, 255, 0.04);
}
.rv-item.on {
  border-color: var(--task);
  background: color-mix(in srgb, var(--task) 12%, transparent);
}
.rv-when {
  font-size: 13px;
  color: var(--text);
}
.rv-tag {
  margin-left: 6px;
  padding: 0 5px;
  font-size: 10.5px;
  line-height: 15px;
  color: var(--finally);
  border: 1px solid currentColor;
  border-radius: 3px;
}
.rv-sum {
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}
.rv-detail {
  min-width: 0;
}
.rv-top {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin: 0 0 8px;
}
.rv-head {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}
.rv-canvas {
  flex: none;
}
.rv-nograph {
  flex: none;
  font-size: 12px;
  line-height: 1.6;
  color: var(--faint);
}
.rv-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-soft);
}
.rv-text {
  max-height: 58vh;
  overflow: auto;
  padding: 12px 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-soft);
  background: #0f1013;
  border: 1px solid var(--panel-line);
  border-radius: 6px;
}
.rv-para {
  margin: 0 0 4px;
  font-size: 12px;
  color: var(--muted);
}
.rv-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  margin: 16px 0 6px;
  padding-top: 12px;
  border-top: 1px solid var(--panel-line);
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.rv-fn {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  margin: 10px 0 4px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}
.rv-chip {
  flex: none;
  padding: 0 7px;
  border-radius: 9px;
  font-size: 11px;
  font-weight: 500;
  line-height: 18px;
  color: var(--muted);
  border: 1px solid var(--panel-line);
}
.rv-chip.small {
  font-size: 10px;
  line-height: 16px;
}
.rv-chip.s-新增,
.rv-item.g-新增 .rv-tag {
  color: #6fd39a;
  border-color: color-mix(in srgb, #6fd39a 50%, transparent);
}
.rv-chip.s-删除,
.rv-item.g-删除 .rv-tag {
  color: var(--failed);
  border-color: color-mix(in srgb, var(--failed) 50%, transparent);
}
.rv-chip.s-改动,
.rv-item.g-改动 .rv-tag {
  color: var(--decision);
  border-color: color-mix(in srgb, var(--decision) 50%, transparent);
}
.rv-where {
  font: 11px/1.5 var(--mono);
  color: var(--faint);
  overflow-wrap: anywhere;
}
.rv-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 8px;
  margin: 3px 0 3px 4px;
  padding-left: 10px;
  border-left: 2px solid var(--panel-line);
}
.rv-item.g-新增 {
  border-left-color: color-mix(in srgb, #6fd39a 60%, transparent);
}
.rv-item.g-删除 {
  border-left-color: color-mix(in srgb, var(--failed) 60%, transparent);
}
.rv-item.g-改动 {
  border-left-color: color-mix(in srgb, var(--decision) 60%, transparent);
}
.rv-tag {
  flex: none;
  padding: 0 5px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 16px;
  color: var(--muted);
  border: 1px solid var(--panel-line);
}
.rv-item-text {
  color: var(--text);
  overflow-wrap: anywhere;
}
.rv-hint {
  margin: 12px 0 0;
}
@media (max-width: 820px) {
  .rv-body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
