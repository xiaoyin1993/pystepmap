<script setup lang="ts">
// "分析配置"弹窗：项目菜单里当前项目的"分析配置"打开。显示平台存着的这个项目的分析配置（引擎的配置 JSON，和
// pystepmap analyze --config 的文件一样；{} 是全用默认值），可以改。保存时后端校验，不合法时它的中文说明显示在框下面，
// 弹窗不关。改了不会自动重新分析：保存后问"现在重新分析"还是"以后再说"。
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NModal, NSpin, useMessage } from 'naive-ui'
import { errorText } from '../api'
import { CONFIG_EXAMPLE, EXCLUDE_NOTE, configText, parseConfigText } from '../config'
import { backend, configFor, projects, readConfig, reanalyze, reanalyzing, writeConfig } from '../state'

const message = useMessage()
const text = ref('')
/** 读到的（或刚保存的）配置的文字：没改时"保存"是灰的 */
const stored = ref('')
const reading = ref(false)
const readError = ref('')
const saving = ref(false)
/** 保存失败的说明（后端校验不通过、连不上……） */
const failure = ref('')
/** 保存成功：显示"现在重新分析 / 以后再说" */
const saved = ref(false)

const show = computed({
  get: () => !!configFor.value,
  set: (v: boolean) => {
    if (!v && !saving.value) configFor.value = ''
  },
})
const target = computed(() => projects.value.find((p) => p.id === configFor.value) ?? null)
const busy = computed(() => !!target.value && (reanalyzing.value.includes(target.value.id) || target.value.analyzing))
const dirty = computed(() => text.value.trim() !== stored.value.trim())

let readSeq = 0
async function read(id: string) {
  const n = ++readSeq
  reading.value = true
  readError.value = ''
  failure.value = ''
  saved.value = false
  text.value = ''
  stored.value = ''
  try {
    const cfg = await readConfig(id)
    if (n !== readSeq) return
    text.value = stored.value = configText(cfg)
  } catch (e) {
    if (n === readSeq) readError.value = errorText(e)
  } finally {
    if (n === readSeq) reading.value = false
  }
}

watch(configFor, (id) => {
  if (id) void read(id)
})

// 项目不在了（删掉了）：关掉
watch(target, (t) => {
  if (configFor.value && !t) configFor.value = ''
})

async function save() {
  const id = configFor.value
  if (!id || saving.value) return
  failure.value = ''
  const parsed = parseConfigText(text.value)
  if (!parsed.ok) {
    failure.value = parsed.error
    return
  }
  saving.value = true
  try {
    const cfg = await writeConfig(id, parsed.config ?? {})
    if (configFor.value !== id) return
    text.value = stored.value = configText(cfg)
    saved.value = true
  } catch (e) {
    failure.value = errorText(e)
  } finally {
    saving.value = false
  }
}

function analyzeNow() {
  const id = configFor.value
  configFor.value = ''
  if (id) void reanalyze(id)
}

function later() {
  configFor.value = ''
  message.info('配置已保存，下次点"重新分析"时生效')
}
</script>

<template>
  <n-modal
    v-model:show="show"
    preset="card"
    :title="target ? `分析配置：「${target.name}」` : '分析配置'"
    class="pd"
    :style="{ width: '560px' }"
    :mask-closable="!saving"
    :closable="!saving"
    :close-on-esc="!saving"
  >
    <div class="pd-body">
      <p class="cd-hint">
        引擎的分析配置（JSON，和 <code>pystepmap analyze --config</code> 的文件一样，例如
        <code>{{ CONFIG_EXAMPLE }}</code>）；<code>{}</code> 表示全用默认值。{{ EXCLUDE_NOTE }}改了要重新分析才生效。
      </p>
      <div v-if="reading" class="pd-busy"><n-spin :size="14" /> 正在读配置…</div>
      <n-alert v-else-if="readError" type="error" :show-icon="false" class="pd-error">
        读不了这个项目的配置：{{ readError }}
        <div class="pd-existing">
          <n-button size="tiny" secondary @click="read(configFor)">重试</n-button>
        </div>
      </n-alert>
      <template v-else>
        <n-input
          v-model:value="text"
          type="textarea"
          :autosize="{ minRows: 4, maxRows: 16 }"
          :disabled="saving || saved"
          :input-props="{ spellcheck: false, 'aria-label': '分析配置（JSON）' }"
          class="pd-config"
        />
        <n-alert v-if="failure" type="error" :show-icon="false" class="pd-error">没有保存：{{ failure }}</n-alert>
        <div v-if="saved" class="cd-saved" role="status">
          <span>已保存。新配置要重新分析才生效：</span>
          <div class="cd-saved-actions">
            <n-button size="small" type="primary" :disabled="busy || backend === 'down'" @click="analyzeNow">
              {{ busy ? '正在分析…' : '现在重新分析' }}
            </n-button>
            <n-button size="small" @click="later">以后再说</n-button>
          </div>
        </div>
      </template>
    </div>
    <template v-if="!saved" #footer>
      <div class="pd-actions">
        <n-button :disabled="saving" @click="show = false">取消</n-button>
        <n-button type="primary" :loading="saving" :disabled="reading || !!readError || !dirty" @click="save">保存</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style>
.cd-hint {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--faint);
}
.cd-hint code {
  color: var(--text-soft);
}
.cd-saved {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(76, 125, 255, 0.08);
  border: 1px solid #2f4486;
  font-size: 13px;
  color: var(--text-soft);
}
.cd-saved-actions {
  display: flex;
  gap: 8px;
}
</style>
