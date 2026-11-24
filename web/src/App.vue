<script setup lang="ts">
// 版式：顶栏（项目菜单、重新分析、运行全部测试和测试命令、搜索），左边功能线主列表，中间画布（上方两个
// 页签），右边详情栏（"运行路径"页签里换成运行面板，节点详情嵌在它上方）。项目、图、手工标题和分组都来自平台后端（api.ts）。
// 左边列表切到"需求"时，画布上是需求设计的流程，右边换成需求的说明和对照结果（RequirementPanel.vue）。
// 在画布上看变更记录时，顶栏下面多一行横幅（ReviewBanner.vue），整个工作区看的是那条记录当时的图。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NConfigProvider, NDialogProvider, NInput, NMessageProvider, darkTheme } from 'naive-ui'
import DetailPanel from './components/DetailPanel.vue'
import FlowCanvas from './components/FlowCanvas.vue'
import FullTextTip from './components/FullTextTip.vue'
import ConfigDialog from './components/ConfigDialog.vue'
import Icon from './components/Icon.vue'
import LineEditor from './components/LineEditor.vue'
import LineList from './components/LineList.vue'
import Notices from './components/Notices.vue'
import ProjectDialog from './components/ProjectDialog.vue'
import ProjectMenu from './components/ProjectMenu.vue'
import RequirementPanel from './components/RequirementPanel.vue'
import ResizeHandle from './components/ResizeHandle.vue'
import ReviewBanner from './components/ReviewBanner.vue'
import ReviewsButton from './components/ReviewsButton.vue'
import RunAllBar from './components/RunAllBar.vue'
import RunPanel from './components/RunPanel.vue'
import TestCommandDialog from './components/TestCommandDialog.vue'
import { whenText } from './format'
import { initRuns } from './runstate'
import { canvasGraph, currentProject, graphOf, init, reqMode, review, reviewLoading, search, tab } from './state'
import {
  DEFAULT_WIDTHS,
  PANELS,
  browserStorage,
  clampBoth,
  loadWidths,
  maxWidth,
  saveWidths,
  type PanelName,
  type Widths,
} from './panelWidth'
import { naiveOverrides } from './theme'

// 两栏的宽度（拖分界线改，记在浏览器里）：wanted 是想要的宽度，shown 是按现在的窗口夹过的（窗口变窄时自动收窄，
// 再变宽时回到想要的宽度）。画布那一栏宽度变了不重新排版：Vue Flow 自己观察容器大小，视口原点跟着画布左上角走，
// 原来在可见区域左边缘的那一点仍在左边缘。
const storage = browserStorage()
const wanted = ref<Widths>(loadWidths(storage))
const windowWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1600)
const shown = computed(() => clampBoth(wanted.value, windowWidth.value))
const other = (name: PanelName): PanelName => (name === 'list' ? 'detail' : 'list')
const maxOf = (name: PanelName) => maxWidth(name, windowWidth.value, shown.value[other(name)])
const onWindowResize = () => (windowWidth.value = window.innerWidth)

function resizePanel(name: PanelName, width: number) {
  wanted.value = clampBoth({ ...shown.value, [name]: width }, windowWidth.value)
}
function resetPanel(name: PanelName) {
  wanted.value = { ...shown.value, [name]: DEFAULT_WIDTHS[name] }
  saveWidths(storage, wanted.value)
}
const commitWidths = () => saveWidths(storage, wanted.value)

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
  void init()
  initRuns()
})
onBeforeUnmount(() => window.removeEventListener('resize', onWindowResize))

const stats = computed(() => {
  const p = canvasGraph.value
  if (!p) return ''
  const r = review.value
  const a = currentProject.value?.analysis
  const when = r
    ? ` · 分析 #${r.info.head_id}（${whenText(r.info.head_at)}）当时的图`
    : a && a.id === graphOf.value?.analysis
      ? ` · 分析于 ${whenText(a.created_at)}`
      : ''
  return `${Object.keys(p.functions).length} 个函数，${p.lines.length} 条功能线${when}`
})
/** 顶栏下面的横幅：看变更记录时、正在读一条记录时 */
const banner = computed(() => review.value !== null || reviewLoading.value !== null)
const setSearch = (v: string | null) => (search.value = v ?? '')
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="naiveOverrides" abstract>
    <n-message-provider placement="bottom">
      <n-dialog-provider>
        <div class="app" :class="{ 'with-banner': banner }">
          <header class="topbar">
            <div class="brand">
              <span class="brand-mark" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
                  <rect x="3" y="3" width="7" height="7" rx="1.5" />
                  <rect x="14" y="14" width="7" height="7" rx="1.5" />
                  <path d="M6.5 10v4.5a3 3 0 0 0 3 3H14" />
                </svg>
              </span>
              <span class="brand-name">pystepmap</span>
              <span class="brand-sub">只看图读懂流程</span>
            </div>
            <ProjectMenu />
            <RunAllBar />
            <ReviewsButton />
            <n-input
              class="search"
              size="small"
              clearable
              :value="search"
              :disabled="!canvasGraph"
              :placeholder="reqMode ? '搜索需求：编号、标题、功能线、状态' : '搜索功能线：标题、路径、函数名'"
              :aria-label="reqMode ? '搜索需求' : '搜索功能线'"
              @update:value="setSearch"
            >
              <template #prefix><Icon name="search" :size="13" /></template>
            </n-input>
            <div class="top-stats">{{ stats }}</div>
          </header>
          <ReviewBanner />
          <div class="workspace" :style="{ '--list-w': `${shown.list}px`, '--detail-w': `${shown.detail}px` }">
            <LineList />
            <FlowCanvas />
            <RequirementPanel v-if="reqMode" />
            <DetailPanel v-else-if="tab === 'design'" />
            <RunPanel v-else />
            <ResizeHandle
              name="list"
              class="rh-at-list"
              :width="shown.list"
              :min="PANELS.list.min"
              :max="maxOf('list')"
              :label="reqMode ? '调整需求列表宽度' : '调整功能线列表宽度'"
              @resize="resizePanel('list', $event)"
              @commit="commitWidths"
              @reset="resetPanel('list')"
            />
            <ResizeHandle
              name="detail"
              class="rh-at-detail"
              :width="shown.detail"
              :min="PANELS.detail.min"
              :max="maxOf('detail')"
              label="调整详情栏宽度"
              @resize="resizePanel('detail', $event)"
              @commit="commitWidths"
              @reset="resetPanel('detail')"
            />
          </div>
        </div>
        <!-- 鼠标停在被截断的字上时显示全文（整个界面共用一个） -->
        <FullTextTip />
        <LineEditor />
        <ProjectDialog />
        <ConfigDialog />
        <TestCommandDialog />
        <Notices />
        <!-- 右上角：别处做了新的分析、已经换上（state.ts 里标了 corner 的提示） -->
        <n-message-provider placement="top-right">
          <Notices corner />
        </n-message-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
