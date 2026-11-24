<script setup lang="ts">
// 顶栏的项目菜单（代替阶段 3 的项目下拉框）：按钮上是当前项目；点开列出平台里的全部项目（名称、目录、上次分析
// 时间），点一个切过去。当前项目下面是分析摘要（函数数、功能线数、目标不明 / 推测调用数、跳过的文件）和"重新分析"、
// "分析配置"、"删除项目"（正在分析时不能删）。顶部有"添加项目"。按钮右边单独放一个"重新分析"：代码改了以后最常点
// 的就是它。列表里有项目正在分析时，state.ts 每 2 秒刷新一次项目列表，转圈会在分析完成后自己停。
// 当前项目的代码在分析之后改过（过期检查）时在摘要下面写一句；有未看的流程变化（上次标为已看以来，没标过就是第一次分析以来）时也写上（列表里的
// 每个项目同样标着）。看变更记录时（state.ts 的 review）这里说的仍是项目现在的变化，但不在这里标为已看（先回到现在）。
import { computed, ref } from 'vue'
import { NButton, NPopover, NScrollbar, NSpin, NTooltip, useDialog, useMessage } from 'naive-ui'
import { errorText } from '../api'
import { summaryRows, whenText } from '../format'
import { SLOW_TEXT } from '../slow'
import { projectDiffNote } from '../diffview'
import {
  addDialogOpen,
  backend,
  backendError,
  currentProject,
  diffResult,
  graphOf,
  openConfig,
  presentDiffView,
  projectId,
  projects,
  reanalyze,
  reanalyzing,
  refreshProjects,
  removeProject,
  review,
  selectProject,
  slowReanalyzing,
  staleInfo,
} from '../state'
import AckButton from './AckButton.vue'
import Icon from './Icon.vue'

const dialog = useDialog()
const message = useMessage()
const open = ref(false)
const now = ref(new Date())

const busy = (id: string) => reanalyzing.value.includes(id) || !!projects.value.find((p) => p.id === id)?.analyzing
const currentBusy = computed(() => !!projectId.value && busy(projectId.value))
/** 这个页面点的重新分析已经等了 30 秒以上 */
const currentSlow = computed(() => !!projectId.value && slowReanalyzing.value.includes(projectId.value))
const canReanalyze = computed(() => !!currentProject.value && !currentBusy.value && backend.value !== 'down')

const label = computed(() => {
  if (currentProject.value) return currentProject.value.name
  if (backend.value === 'connecting') return '正在连接…'
  if (backend.value !== 'ok') return '没有连上后端'
  return projects.value.length ? '选择项目' : '还没有项目'
})

/** 这次的流程变化怎么说；差异栏不会出现时在这里标为已看 */
const diffNote = computed(() => projectDiffNote(presentDiffView.value))
/** 画布上这次分析的差异已经取到了（取到之前 changedLines 还是 0，不能当作"没有功能线有变化"） */
const diffLoaded = computed(() => !!diffResult.value?.diff && diffResult.value.head.id === graphOf.value?.analysis)
const rows = computed(() => (currentProject.value?.analysis ? summaryRows(currentProject.value.analysis.summary) : []))
const skipped = computed(() => currentProject.value?.analysis?.summary.skipped_files ?? [])

function onShow(show: boolean) {
  open.value = show
  if (show) {
    now.value = new Date()
    // 打开时顺手刷新：别的页面添加、重新分析过的也看得到
    void refreshProjects()
  }
}

function choose(id: string) {
  open.value = false
  if (id !== projectId.value) void selectProject(id)
}

function add() {
  open.value = false
  addDialogOpen.value = true
}

function confirmDelete() {
  const p = currentProject.value
  if (!p || busy(p.id)) return
  open.value = false
  dialog.warning({
    title: `删除项目「${p.name}」？`,
    content: `只删除平台里的记录：这个项目的分析结果，和手工改过的功能线标题、分组。不会改动或删除 ${p.root} 里的任何文件；以后想看可以重新添加。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await removeProject(p.id)
        message.success(`已删除「${p.name}」（代码没有动）`)
      } catch (e) {
        message.error(`没有删除：${errorText(e)}`)
        return false
      }
    },
  })
}

function analyzeCurrent() {
  open.value = false
  void reanalyze()
}

function editConfig() {
  open.value = false
  openConfig()
}
</script>

<template>
  <div class="pm-bar">
    <n-popover
      trigger="click"
      placement="bottom-start"
      :show="open"
      :show-arrow="false"
      raw
      @update:show="onShow"
    >
      <template #trigger>
        <button class="pm-trigger" :class="{ open }" aria-label="项目" aria-haspopup="dialog" :aria-expanded="open">
          <Icon name="folder" :size="14" />
          <span class="pm-trigger-name">{{ label }}</span>
          <n-spin v-if="currentBusy" :size="12" />
          <Icon :name="open ? 'chevron-up' : 'chevron-down'" :size="13" />
        </button>
      </template>

      <div class="pm" role="dialog" aria-label="项目">
        <div class="pm-head">
          <span>平台里的项目<span class="pm-n">{{ projects.length }}</span></span>
          <n-button size="tiny" type="primary" secondary @click="add">
            <template #icon><Icon name="plus" /></template>添加项目
          </n-button>
        </div>
        <div v-if="backend === 'down'" class="pm-warn">连不上平台后端：在 pystepmap 仓库根目录运行 <code>uv run pystepmap serve</code></div>
        <div v-else-if="backend === 'error'" class="pm-warn">{{ backendError }}</div>
        <n-scrollbar class="pm-scroll">
          <div v-for="p in projects" :key="p.id" class="pm-item" :class="{ on: p.id === projectId }">
            <button class="pm-row" @click="choose(p.id)">
              <span class="pm-name-row">
                <span class="pm-name">{{ p.name }}</span>
                <span v-if="p.id === projectId" class="pm-cur">当前</span>
              </span>
              <code class="pm-root" :title="p.root">{{ p.root }}</code>
              <span class="pm-meta">
                <template v-if="busy(p.id)"><n-spin :size="10" /> 正在分析…</template>
                <template v-else-if="p.analysis">
                  上次分析 {{ whenText(p.analysis.created_at, now) }} · {{ p.analysis.summary.lines }} 条功能线
                  <span v-if="p.analysis.has_diff" class="pm-has-diff">· 有流程变化</span>
                </template>
                <template v-else>还没有分析结果</template>
              </span>
            </button>
            <div v-if="p.id === projectId" class="pm-detail">
              <dl v-if="rows.length" class="pm-sum">
                <template v-for="r in rows" :key="r.label">
                  <dt>{{ r.label }}</dt>
                  <dd :class="{ warn: r.warn }" :title="r.hint">{{ r.value }}</dd>
                </template>
              </dl>
              <div v-if="staleInfo.show" class="pm-stale" :class="staleInfo.kind === 'stale' ? 'is-stale' : ''">{{ staleInfo.text }}</div>
              <div v-if="currentProject?.analysis?.has_diff" class="pm-diff">
                <template v-if="!diffLoaded">有未看的流程变化</template>
                <template v-else>
                  {{ diffNote.text }}
                  <!-- 标为已看清的是整个项目的变化：差异栏只在选中有变化的功能线时出现，这里总是给一个 -->
                  <AckButton v-if="!review" link @confirming="open = false" />
                </template>
              </div>
              <details v-if="skipped.length" class="pm-skipped">
                <summary>看跳过的文件</summary>
                <ul>
                  <li v-for="f in skipped" :key="f.file"><code>{{ f.file }}</code>：{{ f.reason }}</li>
                </ul>
              </details>
              <div class="pm-actions">
                <n-button size="small" secondary :disabled="!canReanalyze" :loading="currentBusy" @click="analyzeCurrent">
                  <template #icon><Icon name="refresh" /></template>重新分析
                </n-button>
                <n-button size="small" tertiary :disabled="backend === 'down'" @click="editConfig">
                  <template #icon><Icon name="settings" /></template>分析配置
                </n-button>
                <n-button
                  size="small"
                  tertiary
                  type="error"
                  :disabled="currentBusy"
                  :title="currentBusy ? '正在分析，完成后才能删除' : undefined"
                  @click="confirmDelete"
                >
                  <template #icon><Icon name="trash" /></template>删除项目
                </n-button>
              </div>
            </div>
          </div>
          <div v-if="!projects.length && backend === 'ok'" class="pm-empty">
            还没有项目。点右上角的"添加项目"，填一个本机 Python 项目的目录。
          </div>
        </n-scrollbar>
      </div>
    </n-popover>

    <n-tooltip :delay="400" placement="bottom">
      <template #trigger>
        <n-button
          class="pm-reanalyze"
          size="small"
          secondary
          :disabled="!canReanalyze"
          :loading="currentBusy"
          aria-label="重新分析当前项目"
          @click="reanalyze()"
        >
          <template #icon><Icon name="refresh" /></template>重新分析
        </n-button>
      </template>
      {{
        currentBusy
          ? '正在分析…'
          : `${staleInfo.kind === 'stale' ? `${staleInfo.text}。` : '代码改了以后点这里：'}重新分析当前项目，保留现在看的功能线和展开状态`
      }}
    </n-tooltip>
    <span v-if="currentSlow" class="pm-slow" role="status">{{ SLOW_TEXT }}</span>
  </div>
</template>

<style>
.pm-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.pm-slow {
  max-width: 260px;
  font-size: 11.5px;
  line-height: 1.35;
  color: var(--faint);
}
.pm-trigger {
  all: unset;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 7px;
  width: 200px;
  height: 28px;
  padding: 0 9px 0 10px;
  border-radius: 6px;
  border: 1px solid var(--panel-line);
  background: #1d1e23;
  color: var(--text-soft);
  font-size: 13px;
  cursor: pointer;
}
.pm-trigger:hover,
.pm-trigger.open {
  border-color: var(--task);
}
.pm-trigger:focus-visible {
  outline: 2px solid var(--task-soft);
  outline-offset: 1px;
}
.pm-trigger-name {
  flex: 1;
  min-width: 0;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pm {
  width: 440px;
  box-sizing: border-box;
  background: #1c1d22;
  border: 1px solid var(--surface-line);
  border-radius: 8px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  color: var(--text-soft);
  font-size: 12.5px;
  overflow: hidden;
}
.pm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px 8px 14px;
  font-size: 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--panel-line);
}
.pm-n {
  margin-left: 6px;
  color: var(--faint);
}
.pm-warn {
  padding: 8px 14px;
  font-size: 12px;
  color: var(--decision);
  background: rgba(232, 161, 58, 0.08);
  border-bottom: 1px solid var(--panel-line);
}
.pm-warn code {
  color: var(--text);
}
.pm-scroll {
  max-height: min(70vh, 640px);
}
.pm-item {
  border-bottom: 1px solid #23252b;
}
.pm-item:last-child {
  border-bottom: none;
}
.pm-item.on {
  background: #181d2c;
}
.pm-row {
  all: unset;
  box-sizing: border-box;
  display: grid;
  gap: 2px;
  width: 100%;
  padding: 9px 14px;
  cursor: pointer;
}
.pm-row:hover {
  background: rgba(255, 255, 255, 0.04);
}
.pm-row:focus-visible {
  outline: 2px solid var(--task-soft);
  outline-offset: -2px;
}
.pm-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.pm-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pm-cur {
  flex: none;
  font-size: 10.5px;
  line-height: 15px;
  padding: 0 5px;
  border-radius: 4px;
  color: var(--task-soft);
  border: 1px solid #2f4486;
}
.pm-root {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pm-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--faint);
}
.pm-detail {
  padding: 0 14px 12px;
}
.pm-sum {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 4px 10px;
  margin: 2px 0 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #15161a;
  border: 1px solid #25272d;
}
.pm-sum dt {
  color: var(--faint);
}
.pm-sum dd {
  margin: 0;
  color: var(--text-soft);
}
.pm-sum dd.warn {
  color: var(--decision);
}
.pm-sum dd[title] {
  cursor: help;
}
.pm-stale,
.pm-diff {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}
.pm-stale.is-stale {
  color: var(--decision);
}
.pm-has-diff {
  color: var(--decision);
}
.pm-skipped {
  margin: 0 0 8px;
  font-size: 12px;
}
.pm-skipped summary {
  cursor: pointer;
  color: var(--task-soft);
}
.pm-skipped ul {
  margin: 6px 0 0;
  padding-left: 18px;
  max-height: 140px;
  overflow: auto;
  color: var(--muted);
}
.pm-actions {
  display: flex;
  gap: 8px;
}
.pm-empty {
  padding: 18px 14px;
  color: var(--muted);
}
</style>
