<script setup lang="ts">
// 左边列表切到"需求"时：项目里的需求（.pystepmap/requirements/*.md，后端每次现读）。每行第一行是编号和标题，右边是状态
// （草稿 / 已确认 / 已实现 / 已完成）；第二行是写的功能线，前面标出和实现对不上的地方（几处不一致、找不到功能线、读不懂）。
// OpenSpec 的一个变更里有几块流程时收成一个能收起的组（组的标题上是变更名和状态）；已归档的变更单独一组放在下面，按归档
// 日期再分，日期的组默认收起。收起 / 展开按项目记在浏览器里；搜索时都展开；从地址栏打开的需求在收起的组里时把组打开。
// 点一行在画布上看它设计的流程。上方"刷新"重新读需求文件（改了需求文件不用重新分析）。顶栏的搜索框在这里按编号、
// 标题、功能线、状态找。只读：改需求通过 MCP 的 save_requirement 或者直接改文件。
import { computed, nextTick, ref, watch } from 'vue'
import { NEmpty, NScrollbar } from 'naive-ui'
import { loadFolded, REQ_FOLDS, saveFolded, toggled } from '../groupFold'
import { FOLD_ARCHIVED, foldDefault, reqFoldsOf, reqItems, reqKeyOf, reqTree } from '../requirements'
import { currentReq, loadRequirements, projectId, reqError, reqLoading, requirements, search } from '../state'
import Icon from './Icon.vue'
import RequirementRow from './RequirementRow.vue'

const items = computed(() => reqItems(requirements.value, search.value))
const tree = computed(() => reqTree(items.value))
const on = computed(() => (currentReq.value ? reqKeyOf(currentReq.value) : ''))

// 记的是和默认不一样的组（foldDefault：归档日期的组默认收起，别的默认展开）
const flipped = ref<Set<string>>(new Set())
watch(
  () => projectId.value,
  (id) => {
    flipped.value = id ? loadFolded(id, REQ_FOLDS) : new Set()
  },
  { immediate: true },
)
const searching = computed(() => search.value.trim() !== '')
const isFolded = (fold: string): boolean => !searching.value && foldDefault(fold) !== flipped.value.has(fold)
function setFlipped(next: Set<string>) {
  flipped.value = next
  if (projectId.value) saveFolded(projectId.value, next, REQ_FOLDS)
}
const toggle = (fold: string) => setFlipped(toggled(flipped.value, fold))
/** 正在看的需求所在的组：收起时标题上有个小点 */
const onFolds = computed(() => new Set(reqFoldsOf(tree.value, on.value)))
const tipOf = (fold: string): string | undefined => (searching.value ? undefined : isFolded(fold) ? '展开' : '收起')

// 选中的需求（从地址栏打开时）在收起的组里就把组打开，再滚进可见范围；已经看得见就不动
watch(
  on,
  async (key) => {
    const shut = reqFoldsOf(tree.value, key).filter(isFolded)
    if (shut.length) setFlipped(shut.reduce((s, f) => toggled(s, f), flipped.value))
    await nextTick()
    document.querySelector('.linelist .li.on')?.scrollIntoView({ block: 'nearest' })
  },
  { immediate: true },
)
</script>

<template>
  <div class="list-count">
    <span>{{ search.trim() ? `找到 ${items.length} 条需求` : `${items.length} 条需求` }}</span>
    <button
      class="lc-toggle rq-refresh"
      :disabled="reqLoading"
      title="重新读项目里的需求文件，按最新一次分析重新对照（改了需求文件不用重新分析）"
      @click="loadRequirements()"
    >
      <Icon name="refresh" :size="11" :class="{ 'rq-spin': reqLoading }" />{{ reqLoading ? '正在读' : '刷新' }}
    </button>
  </div>
  <p v-if="reqError" class="rq-list-error" role="status">{{ reqError }}</p>
  <n-scrollbar class="list-scroll">
    <div v-if="tree.active.length" class="grp rq-grp">
      <template v-for="e in tree.active" :key="e.kind === 'item' ? e.item.key : e.fold">
        <RequirementRow v-if="e.kind === 'item'" :item="e.item" :on="e.item.key === on" />
        <div v-else class="rq-change">
          <button
            class="rq-fold rq-change-head"
            :aria-expanded="!isFolded(e.fold)"
            :disabled="searching"
            :title="tipOf(e.fold) && `${tipOf(e.fold)}这个变更里的 ${e.items.length} 块流程`"
            @click="toggle(e.fold)"
          >
            <Icon :name="isFolded(e.fold) ? 'chevron-down' : 'chevron-up'" :size="12" class="grp-chev" />
            <span class="rq-id rq-fold-name">{{ e.name }}</span>
            <span v-if="isFolded(e.fold) && onFolds.has(e.fold)" class="grp-here" title="正在看的需求在这个组里" />
            <span class="grp-n">{{ e.items.length }}</span>
            <span class="rq-status" :class="e.statusClass">{{ e.status || '没有状态' }}</span>
          </button>
          <div v-if="!isFolded(e.fold)" class="rq-nest">
            <RequirementRow v-for="it in e.items" :key="it.key" :item="it" :on="it.key === on" in-group />
          </div>
        </div>
      </template>
    </div>
    <div v-if="tree.archived.length" class="grp rq-grp rq-archived">
      <button
        class="grp-name rq-archived-head"
        :aria-expanded="!isFolded(FOLD_ARCHIVED)"
        :disabled="searching"
        :title="
          'OpenSpec 已归档的变更（openspec/changes/archive/）里的流程：当时的设计，和现在的代码不一致不算问题' +
          (tipOf(FOLD_ARCHIVED) ? `。点一下${tipOf(FOLD_ARCHIVED)}` : '')
        "
        @click="toggle(FOLD_ARCHIVED)"
      >
        <span class="grp-title">
          <Icon :name="isFolded(FOLD_ARCHIVED) ? 'chevron-down' : 'chevron-up'" :size="12" class="grp-chev" />已归档
          <span v-if="isFolded(FOLD_ARCHIVED) && onFolds.has(FOLD_ARCHIVED)" class="grp-here" title="正在看的需求在这个组里" />
        </span>
        <span class="grp-n">{{ tree.archivedCount }}</span>
      </button>
      <template v-if="!isFolded(FOLD_ARCHIVED)">
        <div v-for="d in tree.archived" :key="d.fold" class="rq-day">
          <button
            class="rq-fold rq-day-head"
            :aria-expanded="!isFolded(d.fold)"
            :disabled="searching"
            :title="tipOf(d.fold) && (d.date ? `${tipOf(d.fold)} ${d.date} 归档的变更` : `${tipOf(d.fold)}目录名不带日期的变更`)"
            @click="toggle(d.fold)"
          >
            <Icon :name="isFolded(d.fold) ? 'chevron-down' : 'chevron-up'" :size="12" class="grp-chev" />
            <span class="rq-fold-name" :class="{ 'rq-day-date': d.date }">{{ d.label }}</span>
            <span v-if="isFolded(d.fold) && onFolds.has(d.fold)" class="grp-here" title="正在看的需求在这一天里" />
            <span class="grp-n">{{ d.count }}</span>
          </button>
          <div v-if="!isFolded(d.fold)" class="rq-nest">
            <template v-for="e in d.entries" :key="e.kind === 'item' ? e.item.key : e.fold">
              <RequirementRow v-if="e.kind === 'item'" :item="e.item" :on="e.item.key === on" />
              <template v-else>
                <div class="rq-sub">
                  <span class="rq-id">{{ e.name }}</span><span class="grp-n">{{ e.items.length }}</span>
                </div>
                <div class="rq-nest">
                  <RequirementRow v-for="it in e.items" :key="it.key" :item="it" :on="it.key === on" in-group />
                </div>
              </template>
            </template>
          </div>
        </div>
      </template>
    </div>
    <n-empty
      v-if="items.length === 0"
      class="list-empty"
      size="small"
      :description="search.trim() ? '没有匹配的需求，换个关键词试试' : '项目里没有需求'"
    />
  </n-scrollbar>
</template>
