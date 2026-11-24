<script setup lang="ts">
// "添加项目"弹窗：目录（必填，可以用 ~ 开头）、名称（可选，默认目录名）、分析配置（可选，引擎的配置 JSON）。
// 提交后后端校验目录、查重、分析、入库，分析完才返回（大项目几秒），这期间显示"正在分析…"；失败显示后端的中文
// 说明（目录已经添加过时可以直接切到那个项目）。成功后关掉弹窗、切到新项目。
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NModal, NSpin, useMessage } from 'naive-ui'
import { errorText, isApiError } from '../api'
import { CONFIG_EXAMPLE, EXCLUDE_NOTE, parseConfigText } from '../config'
import { SLOW_TEXT } from '../slow'
import { addDialogOpen, addProject, addingSlow, projects, selectProject } from '../state'

const message = useMessage()
const root = ref('')
const name = ref('')
const config = ref('')
const showConfig = ref(false)
const submitting = ref(false)
const failure = ref<{ text: string; existing: string | null } | null>(null)

const existingName = computed(() => projects.value.find((p) => p.id === failure.value?.existing)?.name ?? '')

watch(addDialogOpen, (open) => {
  if (open) failure.value = null
})

function parseConfig(): Record<string, unknown> | null | undefined {
  const parsed = parseConfigText(config.value)
  if (parsed.ok) return parsed.config
  failure.value = { text: parsed.error, existing: null }
  return undefined
}

async function submit() {
  if (submitting.value) return
  failure.value = null
  if (!root.value.trim()) {
    failure.value = { text: '请填写项目目录', existing: null }
    return
  }
  const cfg = parseConfig()
  if (cfg === undefined) return
  submitting.value = true
  try {
    const info = await addProject({ root: root.value.trim(), name: name.value.trim() || null, config: cfg })
    const s = info.analysis?.summary
    message.success(`已添加「${info.name}」${s ? `：${s.lines} 条功能线，${s.functions} 个函数` : ''}`)
    addDialogOpen.value = false
    root.value = ''
    name.value = ''
    config.value = ''
    showConfig.value = false
  } catch (e) {
    failure.value = { text: errorText(e), existing: isApiError(e) && e.code === 'duplicate' ? e.projectId : null }
  } finally {
    submitting.value = false
  }
}

function goExisting() {
  const id = failure.value?.existing
  if (!id) return
  addDialogOpen.value = false
  void selectProject(id)
}

/** 回车提交；中文输入法拼字时的回车不算 */
function onEnter(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  void submit()
}
</script>

<template>
  <n-modal
    v-model:show="addDialogOpen"
    preset="card"
    title="添加项目"
    class="pd"
    :style="{ width: '540px' }"
    :mask-closable="!submitting"
    :closable="!submitting"
    :close-on-esc="!submitting"
  >
    <div class="pd-body">
      <label class="pd-field">
        <span class="pd-label">项目目录</span>
        <n-input
          v-model:value="root"
          placeholder="/Users/你/code/my-service 或 ~/code/my-service"
          :disabled="submitting"
          :input-props="{ spellcheck: false, autocomplete: 'off' }"
          @keydown.enter="onEnter"
        />
      </label>
      <p class="pd-hint">本机上 Python 项目的根目录（绝对路径，可以用 ~ 开头）。平台只读这个目录：不改、不导入、不执行里面的代码。</p>
      <label class="pd-field">
        <span class="pd-label">名称</span>
        <n-input v-model:value="name" placeholder="可选，默认用目录名" :maxlength="100" :disabled="submitting" @keydown.enter="onEnter" />
      </label>
      <button v-if="!showConfig" class="d-link pd-more" :disabled="submitting" @click="showConfig = true">
        填写分析配置（可选）
      </button>
      <template v-else>
        <label class="pd-field top">
          <span class="pd-label">分析配置</span>
          <n-input
            v-model:value="config"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 10 }"
            :placeholder="`可选。和 pystepmap analyze --config 的文件一样，例如 ${CONFIG_EXAMPLE}`"
            :disabled="submitting"
            :input-props="{ spellcheck: false }"
            class="pd-config"
          />
        </label>
        <p class="pd-hint">
          不填就用默认值。{{ EXCLUDE_NOTE }}以后改配置要重新分析才生效（在顶栏项目菜单里当前项目的"分析配置"改）。
        </p>
      </template>

      <div v-if="submitting" class="pd-busy"><n-spin :size="14" /> 正在分析…（大项目要几秒）</div>
      <p v-if="submitting && addingSlow" class="pd-slow" role="status">{{ SLOW_TEXT }}</p>
      <n-alert v-else-if="failure" type="error" :show-icon="false" class="pd-error">
        {{ failure.text }}
        <div v-if="failure.existing" class="pd-existing">
          <n-button size="tiny" secondary @click="goExisting">切换到{{ existingName ? `「${existingName}」` : '这个项目' }}</n-button>
        </div>
      </n-alert>
    </div>
    <template #footer>
      <div class="pd-actions">
        <n-button :disabled="submitting" @click="addDialogOpen = false">取消</n-button>
        <n-button type="primary" :loading="submitting" @click="submit">添加并分析</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style>
.pd-body {
  display: grid;
  gap: 6px;
}
.pd-field {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}
.pd-field.top {
  align-items: start;
}
.pd-field.top .pd-label {
  padding-top: 5px;
}
.pd-label {
  font-size: 13px;
  color: var(--muted);
}
.pd-hint {
  margin: 0 0 8px 74px;
  font-size: 12px;
  color: var(--faint);
}
.pd-more {
  justify-self: start;
  margin: 2px 0 6px 74px;
}
.pd-config textarea {
  font-family: var(--mono);
  font-size: 12px;
}
.pd-busy {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 13px;
  color: var(--text-soft);
}
.pd-slow {
  margin: 4px 0 0 22px;
  font-size: 12px;
  color: var(--faint);
}
.pd-error {
  margin-top: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
.pd-existing {
  margin-top: 8px;
}
.pd-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
