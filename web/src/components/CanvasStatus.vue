<script setup lang="ts">
// 画布上的状态提示（盖在画布中间）：正在连接、正在读图（大图显示进度）、连不上后端（写明要运行的命令）、读项目
// 失败（重试 / 重新分析）、还没有项目（引导添加第一个）、项目没有功能线、排版出错。
// 看需求时画布上是需求设计的流程（不用读项目的图）：只说需求读不懂、画不出来，和排版出错。
// 看变更记录时画布上是那时的图（已经读好了）：现在的图在底下读、读失败都不盖在上面，只说排版出错。
import { computed, ref } from 'vue'
import { NButton, NProgress, NSpin, useMessage } from 'naive-ui'
import { progressPercent, progressText } from '../format'
import {
  addDialogOpen,
  backend,
  backendError,
  currentProject,
  currentReq,
  error,
  loadFailure,
  loading,
  progress,
  project,
  projects,
  reanalyze,
  reanalyzing,
  reqGraph,
  reqMode,
  retry,
  review,
} from '../state'
import { designLineSid } from '../requirements'

const message = useMessage()

/** 开发服务器把 /api 转到哪里（vite.config.ts）；打包后的页面由后端自己提供，没有这一层 */
const DEV = import.meta.env.DEV
const API_TARGET = typeof __PYSTEPMAP_API__ !== 'undefined' ? __PYSTEPMAP_API__ : 'http://127.0.0.1:5179'
const COMMAND = DEV ? 'uv run pystepmap serve --no-open' : 'uv run pystepmap serve'

type Status =
  | 'connecting'
  | 'loading'
  | 'down'
  | 'backend-error'
  | 'failed'
  | 'no-projects'
  | 'no-lines'
  | 'layout-error'
  | 'req-unreadable'
  | null

const status = computed<Status>(() => {
  if (review.value) return error.value ? 'layout-error' : null
  if (reqMode.value) {
    const r = currentReq.value
    if (r && !designLineSid(reqGraph.value, r)) return 'req-unreadable'
    return error.value ? 'layout-error' : null
  }
  if (loading.value) return 'loading'
  if (backend.value === 'connecting') return 'connecting'
  if (!project.value && (backend.value === 'down' || loadFailure.value?.kind === 'unreachable')) return 'down'
  if (!project.value && backend.value === 'error') return 'backend-error'
  if (loadFailure.value) return 'failed'
  if (backend.value === 'ok' && projects.value.length === 0) return 'no-projects'
  if (project.value && project.value.lines.length === 0) return 'no-lines'
  if (error.value) return 'layout-error'
  return null
})

const percent = computed(() => (progress.value ? progressPercent(progress.value.received, progress.value.total) : null))
const parsing = computed(() => !!progress.value?.total && progress.value.received >= progress.value.total)

/** 读失败时能不能靠重新分析解决：旧版的图、还没有分析结果 */
const offerReanalyze = computed(() => {
  const f = loadFailure.value
  return !!f && !!currentProject.value && (f.kind === 'old-graph' || (f.kind === 'http' && f.status === 404))
})
const busy = computed(() => !!currentProject.value && reanalyzing.value.includes(currentProject.value.id))

// 点"重试"：按钮转圈；还是不行时说一声（不然看起来像没反应）
const retrying = ref(false)
async function onRetry() {
  if (retrying.value) return
  retrying.value = true
  try {
    await retry()
  } finally {
    retrying.value = false
  }
  if (backend.value === 'down') message.warning(`还是连不上平台后端：确认 ${COMMAND} 正在运行`)
  else if (backend.value === 'error') message.warning(`后端还是出错：${backendError.value}`)
  else if (loadFailure.value) message.warning(`还是读不了：${loadFailure.value.message}`)
}

async function copyCommand() {
  try {
    await navigator.clipboard.writeText(COMMAND)
    message.success('已复制命令')
  } catch {
    message.error('浏览器不让写剪贴板，请手动选中复制')
  }
}
</script>

<template>
  <div v-if="status === 'loading'" class="canvas-overlay">
    <div class="cs-loading">
      <div class="cs-loading-row">
        <n-spin :size="14" />
        <span>{{ parsing ? '正在解析图…' : '正在读图…' }}</span>
        <span v-if="progress && progress.received > 0" class="cs-dim">{{ progressText(progress.received, progress.total) }}</span>
      </div>
      <n-progress
        v-if="percent !== null && !parsing"
        type="line"
        :percentage="percent"
        :show-indicator="false"
        :height="4"
        color="#4c7dff"
        rail-color="#2a2c33"
        class="cs-bar"
      />
    </div>
  </div>

  <div v-else-if="status === 'connecting'" class="canvas-overlay"><n-spin :size="14" /> 正在连接平台后端…</div>

  <div v-else-if="status === 'down'" class="canvas-overlay">
    <div class="cs-card" role="alert">
      <h3>连不上平台后端</h3>
      <p>页面的项目和图都从平台后端读。在 pystepmap 仓库的根目录运行：</p>
      <div class="cs-cmd">
        <code>{{ COMMAND }}</code>
        <button class="d-link" @click="copyCommand">复制</button>
      </div>
      <p v-if="DEV" class="cs-dim">
        开发服务器（npm run dev）把 /api 转到 <code>{{ API_TARGET }}</code>，后端默认就在这个端口。后端用了别的端口时，
        启动 npm run dev 前设 <code>PYSTEPMAP_API=http://127.0.0.1:端口</code>。
      </p>
      <p class="cs-dim">启动后点"重试"；从终端切回这个页面时也会自动再连一次。</p>
      <div class="cs-actions">
        <n-button type="primary" size="small" :loading="retrying" @click="onRetry">重试</n-button>
      </div>
    </div>
  </div>

  <div v-else-if="status === 'backend-error'" class="canvas-overlay">
    <div class="cs-card" role="alert">
      <h3>平台后端出错了</h3>
      <p class="cs-err">{{ backendError }}</p>
      <div class="cs-actions">
        <n-button type="primary" size="small" :loading="retrying" @click="onRetry">重试</n-button>
      </div>
    </div>
  </div>

  <div v-else-if="status === 'failed' && loadFailure" class="canvas-overlay">
    <div class="cs-card" role="alert">
      <h3>读不了{{ currentProject ? `「${currentProject.name}」` : '这个项目' }}的分析结果</h3>
      <p class="cs-err">{{ loadFailure.message }}</p>
      <div class="cs-actions">
        <n-button v-if="offerReanalyze" type="primary" size="small" :loading="busy" @click="reanalyze()">重新分析</n-button>
        <n-button :type="offerReanalyze ? 'default' : 'primary'" size="small" :secondary="offerReanalyze" :loading="retrying" @click="onRetry">
          重试
        </n-button>
      </div>
    </div>
  </div>

  <div v-else-if="status === 'no-projects'" class="canvas-overlay">
    <div class="cs-card">
      <h3>还没有项目</h3>
      <p>添加一个本机上的 Python 项目目录：平台会分析它、把结果存下来，然后在这里画出它的功能线。分析只读代码，不改、不执行。</p>
      <div class="cs-actions">
        <n-button type="primary" size="small" @click="addDialogOpen = true">添加第一个项目</n-button>
      </div>
      <p class="cs-dim">想先试试：用 pystepmap 仓库自带的样例目录 <code>examples/transfer</code>（填它的绝对路径）。</p>
    </div>
  </div>

  <div v-else-if="status === 'no-lines'" class="canvas-overlay">
    <div class="cs-card">
      <h3>这个项目没有识别出功能线</h3>
      <p>功能线从入口函数开始：框架的接口、后台任务、工作流，或者用 pystepkit 标记的流程。项目菜单里的分析摘要能看到识别了多少个函数、跳过了哪些文件。</p>
    </div>
  </div>

  <div v-else-if="status === 'req-unreadable' && currentReq" class="canvas-overlay">
    <div class="cs-card" role="alert">
      <h3>这条需求读不懂，画不出设计的流程</h3>
      <p v-for="(e, i) in currentReq.errors" :key="i" class="cs-err">{{ e }}</p>
      <p v-if="!currentReq.errors.length" class="cs-err">后端给的设计图里没有这条需求</p>
      <p class="cs-dim">改好需求文件 <code>{{ currentReq.file }}</code> 后，点左边列表上方的"刷新"。</p>
    </div>
  </div>

  <div v-else-if="status === 'layout-error'" class="canvas-overlay err">{{ error }}</div>
</template>

<style>
.cs-card {
  pointer-events: auto;
  max-width: 520px;
  margin: 0 24px;
  padding: 18px 20px 16px;
  border-radius: 10px;
  background: #1e2025;
  border: 1px solid var(--surface-line);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
  color: var(--text-soft);
  font-size: 13px;
  line-height: 1.65;
  user-select: text;
}
.cs-card h3 {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.cs-card p {
  margin: 0 0 8px;
}
.cs-card code {
  font-size: 12px;
  color: var(--text);
}
.cs-cmd {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 2px 0 10px;
  padding: 8px 12px;
  border-radius: 6px;
  background: #121317;
  border: 1px solid #2a2c33;
}
.cs-cmd code {
  flex: 1;
  font-size: 13px;
  color: #eef1ff;
}
.cs-dim {
  color: var(--faint);
  font-size: 12px;
}
.cs-err {
  color: #e59a9a;
  white-space: pre-wrap;
  word-break: break-word;
}
.cs-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.cs-loading {
  pointer-events: none;
  display: grid;
  gap: 8px;
  min-width: 240px;
  padding: 12px 16px;
  border-radius: 8px;
  background: rgba(22, 23, 27, 0.92);
  border: 1px solid #2a2c33;
}
.cs-loading-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-soft);
}
.cs-loading-row .cs-dim {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}
</style>
