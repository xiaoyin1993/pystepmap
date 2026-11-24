<script setup lang="ts">
// 改一条功能线的标题和分组：左侧列表条目上点笔形按钮，在条目右边弹出这个小框。
// 回车或"保存"立即存到平台（按功能线的 sid，重新分析后仍然生效）；Esc、"取消"、点框外面放弃。
// 每项下面写着原值（"原标题：…"），改过的项旁边"恢复"一点就恢复并保存；"全部恢复原样"两项一起恢复。
// 和原值相同、或者空着的项存成"不改"（overrides.ts 的 overrideFromInput）。
// 分组框下面的建议列表不预先选中任何一条：回车保存框里打的字，用上下箭头移到某一条（或点它）才是选它；列表开着时
// Esc 只关列表，再按一次才放弃（suggest.ts）。在框里关掉（保存、取消、Esc）之后焦点回到这条的笔形按钮；点框外面
// 关掉时焦点留在点的地方。粘贴的文字超过长度上限时，框下面说一声"多出的字没有填进来"。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NInput, NPopover, useMessage } from 'naive-ui'
import { errorText } from '../api'
import { groupNames, overrideFromInput, restoreOne, saveNotice } from '../overrides'
import { closeEditor, displayBySid, displayLines, editing, saveOverride } from '../state'
import { CLOSED, listShown, suggestKey, typed, type SuggestState } from '../suggest'

// 和后端的上限一致（PUT …/overrides：title 最多 200 个字，group 最多 100 个字）
const MAX_TITLE = 200
const MAX_GROUP = 100

const message = useMessage()
const line = computed(() => (editing.value ? (displayBySid.value.get(editing.value.sid) ?? null) : null))
const title = ref('')
const group = ref('')
const saving = ref(false)
const titleInput = ref<InstanceType<typeof NInput> | null>(null)
const suggest = ref<SuggestState>(CLOSED)

// 打开（或换了一条）时填上现在显示的值
watch(
  () => editing.value?.sid,
  async (sid) => {
    const d = sid ? displayBySid.value.get(sid) : null
    title.value = d?.titleFull ?? ''
    group.value = d?.group ?? ''
    suggest.value = CLOSED
    saving.value = false
    if (d) {
      await nextTick()
      titleInput.value?.focus()
      titleInput.value?.select()
    }
  },
)

// 这条功能线不在了（换了项目、重新分析后没了）：关掉
watch(line, (d) => {
  if (editing.value && !d) closeEditor()
})

// 分组的建议：输入和现在的分组不同时，列出名字里含有输入的已有分组
const groupOptions = computed(() => {
  const q = group.value.trim()
  if (!q || q === line.value?.group) return []
  return groupNames(displayLines.value).filter((g) => g !== q && g.includes(q))
})
const showList = computed(() => listShown(suggest.value, groupOptions.value.length))
const activeOption = computed(() => (showList.value && suggest.value.active >= 0 ? `le-opt-${suggest.value.active}` : undefined))

// 到了长度上限：多出的字（多半是粘贴的）被输入框截掉了，说一声
const titleFull = computed(() => title.value.length >= MAX_TITLE)
const groupFull = computed(() => group.value.length >= MAX_GROUP)

const changed = computed(() => {
  const d = line.value
  if (!d) return false
  const next = overrideFromInput(d, title.value, group.value)
  return next.title !== (d.titleEdited ? d.titleFull : null) || next.group !== (d.groupEdited ? d.group : null)
})

/** 在框里关掉（保存、取消、Esc）：关上之后焦点回到这条功能线的笔形按钮 */
function finish() {
  const sid = editing.value?.sid
  closeEditor()
  if (!sid) return
  void nextTick(() => {
    const pencil = Array.from(document.querySelectorAll<HTMLElement>('.li-edit')).find((b) => b.dataset.sid === sid)
    pencil?.focus()
  })
}

async function store(value: { title: string | null; group: string | null }, done: string) {
  const d = line.value
  if (!d || saving.value) return
  saving.value = true
  try {
    await saveOverride(d.sid, value)
    message.success(done)
    finish()
  } catch (e) {
    message.error(`没有保存：${errorText(e)}`)
  } finally {
    saving.value = false
  }
}

function save() {
  const d = line.value
  if (!d) return
  if (!changed.value) return finish()
  const next = overrideFromInput(d, title.value, group.value)
  void store(next, saveNotice(d, next))
}

function restore(what: 'title' | 'group' | 'all') {
  const d = line.value
  if (!d) return
  void store(what === 'all' ? { title: null, group: null } : restoreOne(d, what), '已恢复原样')
}

/** 中文输入法正在拼字时的按键（回车是上屏，箭头是选字）不归这里管 */
const composing = (e: KeyboardEvent) => e.isComposing || e.keyCode === 229

/** 标题框：回车保存 */
function onTitleEnter(e: KeyboardEvent) {
  if (composing(e)) return
  e.preventDefault()
  save()
}

function onGroupInput(v: string) {
  group.value = v
  suggest.value = typed(groupOptions.value.length)
}

function pick(i: number) {
  const g = groupOptions.value[i]
  if (g !== undefined) group.value = g
  suggest.value = CLOSED
}

/** 分组框：箭头在建议之间移动，回车选中的建议或者保存，Esc 先关列表再放弃 */
function onGroupKey(e: KeyboardEvent) {
  if (composing(e)) return
  const r = suggestKey(suggest.value, e.key, groupOptions.value.length)
  if (!r.handled) return
  e.preventDefault()
  e.stopPropagation()
  suggest.value = r.state
  if (r.action.type === 'pick') pick(r.action.index)
  else if (r.action.type === 'save') save()
  else if (r.action.type === 'cancel') finish()
}

// 点框外面放弃（自己判断：点别的条目的笔形按钮是换一条来改）；焦点留在点的地方
function onPointerDown(e: PointerEvent) {
  if (!editing.value) return
  const t = e.target as Element | null
  if (t?.closest('.le, .li-edit')) return
  closeEditor()
}
onMounted(() => document.addEventListener('pointerdown', onPointerDown, true))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown, true))

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && !e.defaultPrevented && !composing(e)) {
    e.stopPropagation()
    finish()
  }
}
</script>

<template>
  <!-- x / y 一直给数：没在改时也给（0），否则 naive 的 popover 当它是普通的、去找 trigger 插槽，控制台报错 -->
  <n-popover
    :show="!!line"
    :x="editing?.x ?? 0"
    :y="editing?.y ?? 0"
    trigger="manual"
    placement="right-start"
    :show-arrow="false"
    raw
  >
    <div v-if="line" class="le" role="dialog" aria-label="改功能线的标题和分组" @keydown="onKey">
      <div class="le-head">改功能线</div>
      <label class="le-field">
        <span class="le-label">标题</span>
        <n-input
          ref="titleInput"
          v-model:value="title"
          size="small"
          :maxlength="MAX_TITLE"
          :placeholder="line.original.titleFull"
          :disabled="saving"
          @keydown.enter="onTitleEnter"
        />
      </label>
      <p v-if="titleFull" class="le-limit" role="status">标题最多 {{ MAX_TITLE }} 个字，多出的字没有填进来</p>
      <div class="le-orig">
        <span class="le-orig-text" :title="line.original.titleFull">原标题：{{ line.original.titleFull }}</span>
        <button v-if="line.titleEdited" class="d-link" :disabled="saving" @click="restore('title')">恢复</button>
      </div>
      <div class="le-field">
        <label class="le-label" for="le-group">分组</label>
        <div class="le-combo">
          <n-input
            :value="group"
            size="small"
            :maxlength="MAX_GROUP"
            :placeholder="line.original.group"
            :disabled="saving"
            :input-props="{
              id: 'le-group',
              autocomplete: 'off',
              role: 'combobox',
              'aria-autocomplete': 'list',
              'aria-controls': 'le-suggest',
              'aria-expanded': showList ? 'true' : 'false',
              'aria-activedescendant': activeOption,
            }"
            @update:value="onGroupInput"
            @keydown="onGroupKey"
            @blur="suggest = CLOSED"
          />
          <ul v-if="showList" id="le-suggest" class="le-suggest" role="listbox" aria-label="已有的分组">
            <li
              v-for="(g, i) in groupOptions"
              :id="`le-opt-${i}`"
              :key="g"
              role="option"
              :aria-selected="i === suggest.active"
              :class="{ on: i === suggest.active }"
              @mousedown.prevent
              @click="pick(i)"
            >
              {{ g }}
            </li>
          </ul>
        </div>
      </div>
      <p v-if="groupFull" class="le-limit" role="status">分组最多 {{ MAX_GROUP }} 个字，多出的字没有填进来</p>
      <div class="le-orig">
        <span class="le-orig-text">原分组：{{ line.original.group }}</span>
        <button v-if="line.groupEdited" class="d-link" :disabled="saving" @click="restore('group')">恢复</button>
      </div>
      <p class="le-hint">按功能线存，重新分析后仍然生效；只影响平台里的显示，不改代码。</p>
      <div class="le-actions">
        <button v-if="line.titleEdited || line.groupEdited" class="d-link le-reset" :disabled="saving" @click="restore('all')">
          全部恢复原样
        </button>
        <n-button size="small" :disabled="saving" @click="finish">取消</n-button>
        <n-button size="small" type="primary" :loading="saving" @click="save">保存</n-button>
      </div>
    </div>
  </n-popover>
</template>

<style>
.le {
  width: 340px;
  box-sizing: border-box;
  padding: 12px 14px 12px;
  background: #1e2025;
  border: 1px solid var(--surface-line);
  border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  font-size: 12.5px;
  color: var(--text-soft);
}
.le-head {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 10px;
}
.le-field {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
}
.le-label {
  color: var(--muted);
}
.le-orig {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 4px 0 10px 44px;
  font-size: 11.5px;
  color: var(--faint);
  min-width: 0;
}
.le-orig-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.le-orig .d-link {
  flex: none;
}
.le-hint {
  margin: 2px 0 10px;
  font-size: 11.5px;
  color: var(--faint);
}
.le-combo {
  position: relative;
  min-width: 0;
}
.le-suggest {
  position: absolute;
  z-index: 2;
  top: calc(100% + 3px);
  left: 0;
  right: 0;
  max-height: 168px;
  overflow: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  background: #26282e;
  border: 1px solid var(--surface-line);
  border-radius: 6px;
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.45);
}
.le-suggest li {
  padding: 4px 8px;
  border-radius: 4px;
  color: var(--text-soft);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.le-suggest li:hover {
  background: rgba(255, 255, 255, 0.05);
}
.le-suggest li.on {
  background: #22305a;
  color: var(--text);
}
.le-limit {
  margin: 4px 0 0 44px;
  font-size: 11.5px;
  color: var(--decision);
}
.le-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.le-reset {
  margin-right: auto;
}
</style>
