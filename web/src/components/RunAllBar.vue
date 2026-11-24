<script setup lang="ts">
// 顶栏"重新分析"右边：项目级的"运行全部测试"和"测试命令"（打开测试命令对话框）。跑着时（不管是全部还是一条线的，
// 也不管是不是这个页面开始的）这里写正在跑什么、已用多久，有"取消"——在哪个页签都看得到。只跑一条线的测试在"运行路径"
// 页签右边的面板里。旧版后端（没有测试运行的接口）不显示。状态和动作在 runstate.ts。
import { computed } from 'vue'
import { NButton, NTooltip } from 'naive-ui'
import { useElapsed } from '../elapsed'
import { whenText } from '../format'
import {
  cancelling,
  cancelRun,
  commandDialog,
  running,
  runningScope,
  runningUnknown,
  runs,
  runSupport,
  starting,
  startingScope,
  startRun,
  testCommand,
} from '../runstate'
import { runResultText, runningText } from '../runview'
import { backend, currentProject, projectId, reanalyzing } from '../state'
import Icon from './Icon.vue'

const elapsed = useElapsed()
const shown = computed(() => !!projectId.value && runSupport.value !== 'unsupported')
const analyzing = computed(() => reanalyzing.value.includes(projectId.value) || !!currentProject.value?.analyzing)
const canRun = computed(
  () => runSupport.value === 'ok' && !running.value && !starting.value && !analyzing.value && backend.value !== 'down',
)
/** 正在跑的是什么（顶栏不对应哪条线：只跑一条线时写「线的标题」） */
const what = computed(() => (running.value ? runningText(running.value.phase, runningScope.value, '') : ''))
const lastFull = computed(() => runs.value.find((r) => r.scope === null) ?? null)
const tip = computed(() => {
  if (runSupport.value === 'error') return '读运行记录出错了，到"运行路径"页签里重试'
  if (analyzing.value) return '正在分析，分析完才能运行测试'
  const head = testCommand.value?.command
    ? '用测试命令跑项目的全部测试，记下每个测试走过的节点；跑完后在"运行路径"页签里按功能线看。只跑一条线的测试在那个页签右边（要先有一次全量运行，平台才知道哪些测试走过它）'
    : '还没有填测试命令：点了先填测试命令'
  const last = lastFull.value
  return last ? `${head}。上次全量运行：${whenText(last.finished_at)}，${runResultText(last)}` : head
})
</script>

<template>
  <div v-if="shown" class="ra-bar">
    <n-tooltip :delay="400" placement="bottom" :style="{ maxWidth: '420px' }">
      <template #trigger>
        <n-button
          size="small"
          secondary
          :disabled="!canRun"
          :loading="starting && startingScope?.line === null"
          aria-label="运行全部测试"
          @click="startRun"
        >
          <template #icon><Icon name="play" /></template>运行全部测试
        </n-button>
      </template>
      {{ tip }}
    </n-tooltip>
    <template v-if="running">
      <span class="ra-running" :class="{ unknown: runningUnknown }" role="status" :title="`${what}… 已用 ${elapsed}`">
        <span class="ra-dot" aria-hidden="true" /><span class="ra-running-text">{{ what }}… {{ elapsed }}</span>
      </span>
      <n-button size="small" tertiary :loading="cancelling" :disabled="cancelling" @click="cancelRun">取消</n-button>
    </template>
    <n-button
      size="small"
      tertiary
      :disabled="runSupport !== 'ok'"
      :title="testCommand?.command ? `测试命令：${testCommand.command}` : '还没有填测试命令'"
      @click="commandDialog = true"
    >
      <template #icon><Icon name="settings" /></template>测试命令
    </n-button>
  </div>
</template>

<style>
.ra-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
/* 正在跑什么：窗口窄时先截短这一段（悬停看全文），再窄只留一个点；顶栏放不下时收窄的是搜索框 */
.ra-running {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 280px;
  overflow: hidden;
  font-size: 12px;
  color: var(--task-soft);
  white-space: nowrap;
}
.ra-running-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (max-width: 1440px) {
  .ra-running {
    max-width: 200px;
  }
}
@media (max-width: 1320px) {
  .ra-running {
    max-width: 130px;
  }
}
@media (max-width: 1220px) {
  .ra-running-text {
    display: none;
  }
}
.ra-running.unknown {
  color: var(--muted);
}
.ra-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--task);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--task) 25%, transparent);
}
</style>
