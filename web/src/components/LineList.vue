<script setup lang="ts">
// 左侧主列表：按分组列出功能线。接口显示方法和路径（标题不同于路径时第二行写标题），
// 后台任务等显示种类和标题（第二行写函数名）。右侧的数字是这条线跨几个任务。
// 标题和分组是应用了手工项之后的（overrides.ts）：分组按改过的归，改过的条目带一个"已改"小标记；鼠标停在条目上
// 右边出现笔形按钮，点开编辑框改标题、分组或恢复原样（LineEditor.vue）。
// 列表上是图里的标题（引擎截断过的显示形式）或手工标题；悬停浮层和搜索用完整标题。
// 和对比基准比（上次标为已看时，没标过就是第一次分析）有变化的功能线带"有变化"，新增的带"新增"，列表上方可以"只看有变化的"；被删掉的功能线列在最下面一个
// 折叠的小节里（只是文字）。代码在分析之后改过、涉及这条线的文件时，标题旁有一个琥珀色小点（过期检查，stale.ts）。
// 有变化、但不在任何有变化的功能线上的函数（画布上到不了）列在最下面另一个小节里（只是文字：标题、函数名、变了几处，悬停看文件）；
// 只有这种变化时"只看有变化的"照样出现，点了之后列表里只剩这个小节（展开着）。
// 流程差异的标记（"有变化""新增"、"只看有变化的"、已删除的功能线、不在功能线上的函数）只在"流程设计"页签里出现。
// "运行路径"页签里测试命令在跑时，有函数执行过的功能线标题旁有一个蓝色小点（悬停："已到 m / n 个函数"）；最近一次刷新时
// 执行过的函数变多了的线，小点外面多一圈（找得到刚走到的线）。列表的顺序不变。
// 项目里有需求（.pystepmap/requirements/，state.ts 的 requirements）时列表上方有"功能线 / 需求"两个小页签，切到需求时
// 下面换成需求列表（RequirementList.vue）；没有需求、旧版后端时不出现。
// 看变更记录时（state.ts 的 review）：列的是那条记录当时的图里的功能线，"有变化""新增""只看有变化的"、已删除的功能线
// 说的是那次的变化；不能改标题分组、没有"需求"、没有过期的小点。"只看有变化的"开没开，回到现在时照进来之前的。
import { computed, nextTick, ref, watch } from 'vue'
import { NEmpty, NScrollbar } from 'naive-ui'
import type { DisplayLine } from '../overrides'
import { loadFolded, saveFolded, toggled } from '../groupFold'
import { groupLines } from '../overrides'
import { liveFresh, liveLines } from '../runstate'
import { liveLineTitle } from '../runview'
import {
  canvasGraph,
  diffView,
  displayLines,
  editing,
  lineSid,
  openEditor,
  projectId,
  reqMode,
  requirements,
  review,
  search,
  selectLine,
  showLines,
  showRequirements,
  staleInfo,
  tab,
  taskCount,
} from '../state'
import { FN_KIND_NAME, METHOD_COLOR } from '../theme'
import Icon from './Icon.vue'
import RequirementList from './RequirementList.vue'

interface Item {
  sid: string
  kind: string
  method: string | null
  path: string | null
  title: string
  /** 完整标题（手工标题；没有时引擎截断了取图里的 title_full） */
  titleFull: string
  group: string
  second: string
  /** 第二行是函数名（等宽字体） */
  secondCode: boolean
  /** 悬停显示全文的第二行：接口的方法和路径、函数名 */
  tipSub: string
  tasks: number
  edited: boolean
  /** "已改"小标记的说明 */
  editedHint: string
  /** 和对比基准比：新增 / 有变化；没变时为 null */
  change: 'added' | 'changed' | null
  /** 涉及的代码在分析之后改过 */
  stale: boolean
  /** 运行中：有函数执行过时小点的说明（没有时 null）；fresh：最近一次刷新时变多了 */
  live: string | null
  fresh: boolean
  hay: string
}

/** 运行中这条线的小点的说明：只在"运行路径"页签里、有函数执行过时 */
function liveOf(sid: string): string | null {
  const c = tab.value === 'run' ? liveLines.value[sid] : undefined
  return c && c.functions > 0 ? liveLineTitle(c, liveFresh.value.has(sid)) : null
}

const items = computed(() => {
  const p = canvasGraph.value
  if (!p) return []
  return displayLines.value.map((d: DisplayLine): Item => {
    const l = d.line
    const fn = p.functions[l.id]
    const route = fn?.route ?? null
    const [method, ...rest] = route ? route.split(' ') : []
    const name = fn?.qualname ?? l.id
    const title = d.title
    const edits = [
      d.titleEdited ? `原标题：${d.original.titleFull}` : '',
      d.groupEdited ? `原分组：${d.original.group}` : '',
    ].filter(Boolean)
    return {
      sid: d.sid,
      kind: FN_KIND_NAME[l.kind] ?? l.kind,
      method: route ? method : null,
      path: route ? rest.join(' ') : null,
      title,
      titleFull: d.titleFull,
      group: d.group,
      second: route ? (title !== route ? title : name) : name,
      secondCode: !route || title === route,
      tipSub: [route, name].filter(Boolean).join('  '),
      tasks: taskCount(l.root),
      edited: edits.length > 0,
      editedHint: `手工改过。${edits.join('；')}`,
      change: tab.value === 'design' ? (diffView.value.lineStatus.get(d.sid) ?? null) : null,
      // 过期的小点说的是现在的代码：看变更记录时不标
      stale: !review.value && (staleInfo.value.lineSids?.has(d.sid) ?? false),
      live: liveOf(d.sid),
      fresh: tab.value === 'run' && liveFresh.value.has(d.sid),
      hay: `${d.titleFull} ${route ?? ''} ${name} ${d.group}`.toLowerCase(),
    }
  })
})

/** 有变化（含新增）的功能线条数 */
const changedCount = computed(() => items.value.filter((it) => it.change).length)
const removedLines = computed(() => (tab.value === 'design' ? diffView.value.removedLines : []))
/** 有变化、但不在任何有变化的功能线上的函数 */
const offLine = computed(() => (tab.value === 'design' ? diffView.value.offLine : []))
const OFF_WORD = { added: '新增', removed: '已删除', changed: '有变化' } as const
/** 能"只看"的有几样：有变化的功能线加不在功能线上的函数；都没有时不出现"只看有变化的" */
const filterable = computed(() => changedCount.value + offLine.value.length)
const onlyChanged = ref(false)
watch(filterable, (n) => {
  // 切到运行路径页签时变化都不显示（n 为 0），不算"变化都没了"，回来时照旧
  if (n === 0 && tab.value === 'design') onlyChanged.value = false
})
// 看变更记录：进来时记下"只看有变化的"开没开，回到现在时照旧
let onlyChangedBefore = false
watch(
  () => review.value !== null,
  (on) => {
    if (on) onlyChangedBefore = onlyChanged.value
    else onlyChanged.value = onlyChangedBefore && filterable.value > 0
  },
)

// 分组在应用手工项之后归：改了分组的条目出现在新的组里
const groups = computed(() => {
  const q = search.value.trim().toLowerCase()
  const only = onlyChanged.value && tab.value === 'design'
  return groupLines(items.value.filter((it) => (!q || it.hay.includes(q)) && (!only || it.change)))
})

const total = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0))

// 分组可以收起：点分组标题。按项目记在浏览器里；搜索时都展开（要找的那条可能在收起的组里）
const folded = ref<Set<string>>(new Set())
watch(
  () => projectId.value,
  (id) => {
    folded.value = id ? loadFolded(id) : new Set()
  },
  { immediate: true },
)
const searching = computed(() => search.value.trim() !== '')
// 搜索、只看有变化的时候分组都展开、不能收：要找的那几条可能在收起的组里（收起的组里藏着唯一一条有变化的线时，
// 列表上就什么都看不到，也就找不到差异栏里的"标为已看"）。收起的记录不动，退出之后照旧
const narrowed = computed(() => searching.value || (onlyChanged.value && tab.value === 'design'))
const isFolded = (name: string): boolean => !narrowed.value && folded.value.has(name)
function setFolded(next: Set<string>) {
  folded.value = next
  if (projectId.value) saveFolded(projectId.value, next)
}
const toggleGroup = (name: string) => setFolded(toggled(folded.value, name))
const allFolded = computed(() => groups.value.length > 0 && groups.value.every((g) => folded.value.has(g.name)))
const toggleAll = () => setFolded(allFolded.value ? new Set() : new Set(groups.value.map((g) => g.name)))

function edit(sid: string, ev: MouseEvent) {
  const row = (ev.currentTarget as HTMLElement).closest('.li-wrap') as HTMLElement | null
  const r = (row ?? (ev.currentTarget as HTMLElement)).getBoundingClientRect()
  openEditor(sid, r.right + 6, r.top)
}

// 选中的功能线（从地址栏打开、换项目时）滚进可见范围；已经看得见就不动（不在鼠标底下挪列表）
watch(
  lineSid,
  async () => {
    await nextTick()
    document.querySelector('.linelist .li.on')?.scrollIntoView({ block: 'nearest' })
  },
  { immediate: true },
)
</script>

<template>
  <nav class="linelist" :aria-label="reqMode ? '需求' : '功能线'">
    <div v-if="requirements.length && !review" class="list-switch" role="tablist" aria-label="左边列表看什么">
      <button class="ls-tab" :class="{ on: !reqMode }" role="tab" :aria-selected="!reqMode" @click="showLines">
        功能线<span class="ls-n">{{ displayLines.length }}</span>
      </button>
      <button
        class="ls-tab"
        :class="{ on: reqMode }"
        role="tab"
        :aria-selected="reqMode"
        title="项目里写下的业务需求（.pystepmap/requirements/）：设计的流程和实现对照"
        @click="showRequirements"
      >
        需求<span class="ls-n">{{ requirements.length }}</span>
      </button>
    </div>
    <RequirementList v-if="reqMode" />
    <template v-else>
      <div class="list-count">
        <span>
          <template v-if="search.trim()">找到 {{ total }} 条功能线</template>
          <template v-else-if="canvasGraph">{{ total }} 条功能线</template>
        </span>
        <button v-if="!narrowed && groups.length > 1" class="lc-toggle" @click="toggleAll">
          {{ allFolded ? '展开全部分组' : '收起全部分组' }}
        </button>
        <button
          v-if="filterable"
          class="lc-toggle"
          :class="{ on: onlyChanged }"
          :aria-pressed="onlyChanged"
:title="
            review
              ? '这次变化里有变化（含新增）的功能线，和不在功能线上的有变化的函数'
              : '有未看的流程变化（含新增）的功能线，和不在功能线上的有变化的函数：和上次标为已看时比，没标过就是和第一次分析比'
          "
          @click="onlyChanged = !onlyChanged"
        >
          只看有变化的（{{ filterable }}）
        </button>
      </div>
      <n-scrollbar class="list-scroll">
        <div v-for="g in groups" :key="g.name" class="grp">
          <button
            class="grp-name"
            :class="{ folded: isFolded(g.name) }"
            :aria-expanded="!isFolded(g.name)"
            :disabled="narrowed"
            :title="narrowed ? undefined : isFolded(g.name) ? '展开这个分组' : '收起这个分组'"
            @click="toggleGroup(g.name)"
          >
            <span class="grp-title">
              <Icon :name="isFolded(g.name) ? 'chevron-down' : 'chevron-up'" :size="12" class="grp-chev" />{{ g.name }}
              <span v-if="isFolded(g.name) && g.items.some((it) => it.sid === lineSid)" class="grp-here" title="正在看的功能线在这个分组里" />
            </span>
            <span class="grp-n">{{ g.items.length }}</span>
          </button>
          <div
            v-for="it in isFolded(g.name) ? [] : g.items"
            :key="it.sid"
            class="li-wrap"
            :class="{ editing: editing?.sid === it.sid }"
          >
            <button
              class="li"
              :class="{ on: it.sid === lineSid }"
              :aria-current="it.sid === lineSid ? 'true' : undefined"
              data-tip="text"
              :data-tip-title="it.titleFull"
              :data-tip-sub="it.tipSub"
              :data-tip-cut="it.titleFull !== it.title ? '' : undefined"
              @click="selectLine(it.sid)"
            >
              <span class="li-main">
                <template v-if="it.method">
                  <span class="li-method" :style="{ color: METHOD_COLOR[it.method] ?? '#c5c8d0' }">{{ it.method }}</span>
                  <span class="li-path" data-clip>{{ it.path }}</span>
                </template>
                <template v-else>
                  <span class="li-kind">{{ it.kind }}</span>
                  <span class="li-title" data-clip>{{ it.title }}</span>
                </template>
                <span
                  v-if="it.stale"
                  class="li-stale"
                  role="img"
                  aria-label="这条线涉及的代码改过了"
                  title="这条线涉及的代码改过了"
                  data-tip-skip
                />
                <span
                  v-if="it.live"
                  class="li-live"
                  :class="{ fresh: it.fresh }"
                  role="img"
                  :aria-label="it.live"
                  :title="it.live"
                  data-tip-skip
                />
                <span
                  v-if="it.change"
                  class="li-change"
                  :class="`ch-${it.change}`"
                  :title="it.change === 'added' ? '对比基准之后新增的功能线' : '和对比基准比有变化'"
                  data-tip-skip
                  >{{ it.change === 'added' ? '新增' : '有变化' }}</span
                >
                <span v-if="it.edited" class="li-edited" :title="it.editedHint" data-tip-skip>已改</span>
              </span>
              <span class="li-second" :class="{ code: it.secondCode }" data-clip>{{ it.second }}</span>
              <span v-if="it.tasks > 1" class="li-tasks" :title="`跨 ${it.tasks} 个任务`">{{ it.tasks }}</span>
            </button>
            <button
              v-if="!review"
              class="li-edit"
              :data-sid="it.sid"
              :aria-label="`改这条功能线的标题和分组：${it.titleFull}`"
              title="改标题、分组"
              @click.stop="edit(it.sid, $event)"
            >
              <Icon name="edit" :size="13" />
            </button>
          </div>
        </div>
        <n-empty
          v-if="canvasGraph && total === 0"
          class="list-empty"
          size="small"
          :description="
            search.trim()
              ? '没有匹配的功能线，换个关键词试试'
              : !onlyChanged
                ? '这个项目没有识别出功能线'
                : offLine.length
                  ? '没有功能线有变化：变化都在下面这些不在功能线上的函数里'
                  : '没有有变化的功能线'
          "
        />
        <details v-if="offLine.length" class="grp-removed grp-offline" :open="onlyChanged">
          <summary title="这些函数的流程变了，但没有一条有变化的功能线走到它们，画布上看不到">
            不在功能线上的函数有变化（{{ offLine.length }}）
          </summary>
          <ul>
            <li v-for="f in offLine" :key="f.sid" :class="`off-${f.status}`" :title="[f.title, f.name, f.file].filter(Boolean).join('\n')">
              <span class="lr-title">{{ f.title }}</span>
              <span v-if="f.name && f.name !== f.title" class="lr-file">{{ f.name }}</span>
              <span class="lr-group">{{ OFF_WORD[f.status] }}{{ f.count ? ` ${f.count} 处` : '' }}</span>
            </li>
          </ul>
        </details>
        <details v-if="removedLines.length" class="grp-removed">
          <summary>{{ review ? '这次删除的功能线' : '已删除的功能线' }}（{{ removedLines.length }}）</summary>
          <ul>
            <li v-for="l in removedLines" :key="l.sid">
              <span class="lr-title">{{ l.title }}</span><span class="lr-group">{{ l.group }}</span>
            </li>
          </ul>
        </details>
      </n-scrollbar>
    </template>
  </nav>
</template>
