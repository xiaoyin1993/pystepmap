<script setup lang="ts">
// 测试命令的两个对话框（顶栏的"测试命令"、运行面板的"修改"打开）：
// - "测试命令"：分开填——在哪个目录、哪个解释器、哪些环境变量、pytest 的参数（testcmd.ts 拼成后端存的那一条命令，下面
//   预览）；项目里找到的虚拟环境列出来，点一下填好目录和解释器。存着的命令不是按表单的样子写的、或者想自己写时切到
//   "直接写命令"（一行输入框）。保存到平台（PUT /test-command）。命令在项目目录里执行，平台自动挂上记录插件，不改项目
//   文件；被测项目的 Python 要 3.12 以上；要设 PYTHONPATH 时接在原来的后面（插件目录在里面）。
//   下面一个勾选框"允许 AI（经 MCP）运行单条功能线的测试"（项目的 mcp_tests，默认不允许）：一勾就存（PUT /mcp-tests），
//   不跟着"保存"；旧版后端没有这一项时不显示。
// - "确认运行"：一条命令第一次运行前（或者后端说命令已经变了）弹出，说清在哪个目录里执行什么、外部服务会被真的访问；
//   只跑一条线的测试时也说是哪条线、几个测试；点"运行"才带 confirm 和这条命令的原文去开始（runstate.ts 的 confirmRun）。
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NDynamicInput, NInput, NModal } from 'naive-ui'
import { errorText, type Interpreter } from '../api'
import {
  cancelConfirm,
  commandDialog,
  confirmAsk,
  confirmRun,
  loadInterpreters,
  mcpSaving,
  saveTestCommand,
  setMcpTests,
  testCommand,
} from '../runstate'
import { currentProject } from '../state'
import { composeCommand, DEFAULT_FORM, formProblem, parseCommand, type TestForm } from '../testcmd'

const EXAMPLE = '.venv/bin/python -m pytest tests -q'

/** form：分开填；raw：直接写整条命令 */
const mode = ref<'form' | 'raw'>('form')
const cwd = ref('')
const python = ref('')
const env = ref<{ key: string; value: string }[]>([])
const args = ref('')
const text = ref('')
/** 存着的命令不是表单的样子（打开时落在"直接写命令"上的原因） */
const foreign = ref(false)
const found = ref<Interpreter[]>([])
const saving = ref(false)
const failure = ref('')

const form = computed<TestForm>(() => ({
  cwd: cwd.value,
  python: python.value,
  env: env.value.map((e) => ({ name: e.key, value: e.value })),
  args: args.value,
}))
const problem = computed(() => (mode.value === 'form' ? formProblem(form.value) : null))
const command = computed(() => (mode.value === 'raw' ? text.value.trim() : problem.value === null ? composeCommand(form.value) : ''))

function fill(f: TestForm) {
  cwd.value = f.cwd
  python.value = f.python
  env.value = f.env.map((e) => ({ key: e.name, value: e.value }))
  args.value = f.args
}

function pick(i: Interpreter) {
  cwd.value = i.cwd
  python.value = i.python
}
const interpreterLabel = (i: Interpreter): string => (i.cwd ? `${i.cwd}/${i.python}` : i.python)
const picked = (i: Interpreter): boolean => cwd.value.trim() === i.cwd && python.value.trim() === i.python

let opened = 0
watch(commandDialog, async (open) => {
  if (!open) return
  const mine = ++opened
  failure.value = ''
  found.value = []
  const saved = testCommand.value?.command ?? ''
  const parsed = saved ? parseCommand(saved) : null
  foreign.value = saved !== '' && parsed === null
  mode.value = foreign.value ? 'raw' : 'form'
  text.value = saved
  fill(parsed ?? DEFAULT_FORM)
  const list = await loadInterpreters()
  if (mine !== opened || !commandDialog.value) return
  found.value = list
  // 还没填过命令：项目里只在子目录有虚拟环境（没有 ./.venv）时先填上找到的第一个
  if (!saved && list.length && !list.some((i) => i.cwd === '' && i.python === DEFAULT_FORM.python) && form.value.python === DEFAULT_FORM.python && !cwd.value) {
    pick(list[0])
  }
})

function toRaw() {
  if (problem.value === null) text.value = composeCommand(form.value)
  mode.value = 'raw'
}
function toForm() {
  const parsed = parseCommand(text.value.trim())
  if (parsed) fill(parsed)
  else if (!text.value.trim()) fill(DEFAULT_FORM)
  mode.value = 'form'
}
/** 直接写的命令拆不回表单：切到分开填会按表单里的内容重新拼 */
const rawUnparsed = computed(() => mode.value === 'raw' && text.value.trim() !== '' && parseCommand(text.value.trim()) === null)

const dirty = computed(() => command.value !== (testCommand.value?.command ?? ''))
const canSave = computed(() => dirty.value && problem.value === null)

async function save() {
  if (saving.value || !canSave.value) return
  saving.value = true
  failure.value = ''
  try {
    await saveTestCommand(command.value)
    commandDialog.value = false
  } catch (e) {
    failure.value = errorText(e)
  } finally {
    saving.value = false
  }
}

// 允许 AI 经 MCP 运行单条功能线的测试：一勾就存
const mcpError = ref('')
watch(commandDialog, (open) => {
  if (open) mcpError.value = ''
})
async function toggleMcp(allow: boolean) {
  mcpError.value = ''
  try {
    await setMcpTests(allow)
  } catch (e) {
    mcpError.value = errorText(e)
  }
}

const showConfirm = computed({
  get: () => confirmAsk.value !== null,
  set: (v: boolean) => {
    if (!v) cancelConfirm()
  },
})
</script>

<template>
  <n-modal
    v-model:show="commandDialog"
    preset="card"
    :title="currentProject ? `测试命令：「${currentProject.name}」` : '测试命令'"
    class="pd"
    :style="{ width: '580px' }"
    :mask-closable="!saving"
    :closable="!saving"
    :close-on-esc="!saving"
  >
    <div class="pd-body">
      <p class="cd-hint">
        测试在项目目录（<code>{{ currentProject?.root }}</code>）里执行；平台会自动挂上记录插件，不改你的项目文件；被测项目的
        Python 要 3.12 以上。
      </p>
      <template v-if="mode === 'form'">
        <label class="pd-field">
          <span class="pd-label">解释器</span>
          <n-input
            v-model:value="python"
            :placeholder="DEFAULT_FORM.python"
            :disabled="saving"
            :input-props="{ spellcheck: false, 'aria-label': '解释器' }"
            class="tc-input"
          />
        </label>
        <p v-if="found.length" class="pd-hint tc-found">
          项目里找到的：
          <button v-for="i in found" :key="interpreterLabel(i)" class="d-link tc-pick" :class="{ on: picked(i) }" :disabled="saving" @click="pick(i)">
            {{ interpreterLabel(i) }}
          </button>
        </p>
        <p v-else class="pd-hint">项目虚拟环境里的 python，例如 <code>.venv/bin/python</code>；也可以写绝对路径。</p>
        <label class="pd-field">
          <span class="pd-label">所在目录</span>
          <n-input
            v-model:value="cwd"
            placeholder="可选。测试在项目的子目录里时填，例如 backend"
            :disabled="saving"
            :input-props="{ spellcheck: false, 'aria-label': '所在目录' }"
            class="tc-input"
          />
        </label>
        <p class="pd-hint">先进到这个子目录再执行；解释器和测试的路径都相对它写。不填就是项目目录。</p>
        <div class="pd-field top">
          <span class="pd-label">环境变量</span>
          <n-dynamic-input
            v-model:value="env"
            preset="pair"
            key-placeholder="名字"
            value-placeholder="值"
            :disabled="saving"
            class="tc-env"
          >
            <template #create-button-default>加一个环境变量</template>
          </n-dynamic-input>
        </div>
        <p class="pd-hint">
          可选。记录插件经 <code>PYTHONPATH</code> 挂上：要设它时接在原来的后面，值写成 <code>src:$PYTHONPATH</code>。
        </p>
        <label class="pd-field">
          <span class="pd-label">测试</span>
          <n-input
            v-model:value="args"
            :placeholder="DEFAULT_FORM.args"
            :disabled="saving"
            :input-props="{ spellcheck: false, 'aria-label': 'pytest 的参数' }"
            class="tc-input"
            @keydown.enter="save()"
          />
        </label>
        <p class="pd-hint">交给 pytest 的：测试目录或文件，加上选项。例如 <code>tests -q</code>、<code>tests/test_orders.py -q</code>。</p>
        <p class="tc-preview-title">会执行的命令</p>
        <code class="d-code tc-command">{{ problem ?? command }}</code>
        <button class="d-link tc-switch" :disabled="saving" @click="toRaw">直接写命令</button>
      </template>
      <template v-else>
        <p v-if="foreign" class="cd-hint">存着的这条命令不是按"分开填"的样子写的，所以在这里直接改。</p>
        <n-input
          v-model:value="text"
          :placeholder="EXAMPLE"
          :disabled="saving"
          :input-props="{ spellcheck: false, 'aria-label': '测试命令' }"
          class="tc-input"
          @keydown.enter="save()"
        />
        <p class="cd-hint">
          只写 <code>python</code> 或 <code>pytest</code> 时用的是 PATH 里找到的那个，不一定是项目的。要设
          <code>PYTHONPATH</code> 时接在原来的后面，写成 <code>PYTHONPATH=src:$PYTHONPATH</code>。
        </p>
        <button class="d-link tc-switch" :disabled="saving" @click="toForm">
          分开填{{ rawUnparsed ? '（这条命令拆不开，会按表单里的内容重新拼）' : '' }}
        </button>
      </template>
      <n-alert v-if="failure" type="error" :show-icon="false" class="pd-error">没有保存：{{ failure }}</n-alert>
      <div v-if="currentProject && currentProject.mcp_tests !== null" class="tc-mcp">
        <n-checkbox :checked="currentProject.mcp_tests" :disabled="mcpSaving || saving" @update:checked="toggleMcp">
          允许 AI（经 MCP）运行单条功能线的测试
        </n-checkbox>
        <p class="pd-hint tc-mcp-hint">
          一次只能跑一条线的测试，只用上面已经确认过的命令，不能跑全部测试、也不能确认命令。勾上马上生效，不用点保存。
        </p>
        <p v-if="mcpError" class="tc-mcp-error">没有改成：{{ mcpError }}</p>
      </div>
    </div>
    <template #footer>
      <div class="pd-actions">
        <n-button :disabled="saving" @click="commandDialog = false">取消</n-button>
        <n-button type="primary" :loading="saving" :disabled="!canSave" @click="save">保存</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showConfirm" preset="card" title="运行这条测试命令？" class="pd" :style="{ width: '580px' }">
    <div v-if="confirmAsk" class="pd-body">
      <p class="tc-confirm">
        这条命令会在 <code>{{ confirmAsk.root }}</code> 里执行：
      </p>
      <code class="d-code tc-command">{{ confirmAsk.command }}</code>
      <p v-if="confirmAsk.scope" class="tc-confirm">
        这次只跑走过「{{ confirmAsk.scope.title }}」的 {{ confirmAsk.scope.tests }} 个测试。
      </p>
      <p class="tc-confirm">如果测试会连开发数据库等外部服务，它们会被真的访问。</p>
      <p class="cd-hint">
        这条命令只在第一次运行（或者改过之后）问一次。运行中可以取消，取消会结束这条命令和它启动的进程；测试自己另起的后台进程（独立会话）不会被取消一并结束。
      </p>
    </div>
    <template #footer>
      <div class="pd-actions">
        <n-button @click="cancelConfirm">取消</n-button>
        <n-button type="primary" @click="confirmRun">运行</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style>
.tc-input,
.tc-env input {
  font-family: var(--mono);
}
.tc-found {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
}
.tc-pick {
  font-family: var(--mono);
}
.tc-pick.on {
  color: var(--text);
  text-decoration: none;
  cursor: default;
}
.tc-preview-title {
  margin: 6px 0 4px;
  font-size: 12px;
  color: var(--muted);
}
.tc-switch {
  justify-self: start;
  margin-top: 2px;
}
.tc-confirm {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-soft);
}
.tc-confirm code {
  color: var(--text);
  overflow-wrap: anywhere;
}
.tc-command {
  margin-bottom: 10px;
}
.tc-mcp {
  margin-top: 6px;
  padding-top: 12px;
  border-top: 1px solid var(--panel-line);
}
.tc-mcp-hint {
  margin: 4px 0 0 24px;
}
.tc-mcp-error {
  margin: 4px 0 0 24px;
  font-size: 12px;
  color: var(--failed);
}
</style>
