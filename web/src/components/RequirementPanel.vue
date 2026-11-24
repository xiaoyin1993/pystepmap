<script setup lang="ts">
// 右栏（看需求时，代替节点详情）：需求的编号、标题、状态，一句结论（设计和实现对得上 / 有几处不一致 / 功能线找不到 /
// 读不懂），说明，不一致的地方（"在图上看"定位到设计图上的节点；"实现里多了"的"到功能线上看"换到实现的功能线、选中那个
// 节点），备注，设计的结局有没有测试走到（点结局在设计图上定位），需求文件，以及按对照结果够得上的状态（和写的不一样时）。
// 点了设计图上的节点时，最上面先写这个节点：类型、标题、和实现对不上的原因（设计图没有源码，不取源码）。只读。
import { computed, ref, watch } from 'vue'
import { NModal, NScrollbar } from 'naive-ui'
import {
  descriptionParagraphs,
  designOutcomeSid,
  outcomeRows,
  reachableNote,
  reqDiffRows,
  reqKeyOf,
  reqVerdict,
  statusClass,
} from '../requirements'
import {
  currentReq,
  displayBySid,
  focusDesignNode,
  gotoImplNode,
  reqData,
  reqGraph,
  reqMarks,
  selected,
} from '../state'
import { renderMarkdown } from '../markdown'
import { END_COLOR, END_NAME, NODE_TYPE_NAME } from '../theme'
import CodeBlock from './CodeBlock.vue'
import type { EndKind } from '../graph'

const r = computed(() => currentReq.value)
const verdict = computed(() => (r.value ? reqVerdict(r.value) : null))
const paragraphs = computed(() => (r.value ? descriptionParagraphs(r.value.description) : []))
const diffs = computed(() => (r.value ? reqDiffRows(r.value) : []))
const outcomes = computed(() => (r.value ? outcomeRows(r.value) : { rows: [], hint: null }))
const reachable = computed(() => (r.value ? reachableNote(r.value) : null))
/** 实现的功能线：画布上有这条线时，"多了"的节点能点过去 */
const implLine = computed(() => {
  const sid = r.value?.comparison?.line_sid ?? ''
  return sid && displayBySid.value.has(sid) ? sid : ''
})
const run = computed(() => reqData.value?.run ?? null)

// 设计图上点中的节点
const node = computed(() => (selected.value?.kind === 'node' ? selected.value.node : null))
const nodeMark = computed(() => (node.value ? (reqMarks.value.nodes.get(node.value.id) ?? null) : null))
const nodeType = computed(() => {
  const n = node.value
  if (!n) return ''
  return n.type === 'end' && n.endKind ? `结局 · ${END_NAME[n.endKind]}` : NODE_TYPE_NAME[n.type]
})

const endColor = (kind: string): string => END_COLOR[kind as EndKind] ?? END_COLOR.ok

function focusOutcome(title: string) {
  const sid = designOutcomeSid(reqGraph.value, r.value, title)
  if (sid) void focusDesignNode(sid)
}

/** "查看文件内容"：需求文件渲染好的样子；"看原文"换成带行号的原文（读不懂的地方要对着行看时用） */
const showFile = ref(false)
const showRaw = ref(false)
const fileHtml = computed(() => renderMarkdown(r.value?.text ?? ''))
const fileLines = computed(() => (r.value?.text ?? '').replace(/\n$/, '').split('\n').map((text, i) => ({ no: i + 1, text })))
watch(
  () => (r.value ? reqKeyOf(r.value) : ''),
  () => {
    showFile.value = false
    showRaw.value = false
  },
)
</script>

<template>
  <aside class="detail rq-panel" aria-label="需求">
    <n-scrollbar>
      <div v-if="r" class="detail-inner">
        <!-- 设计图上点中的节点 -->
        <div v-if="node" class="rq-node">
          <div class="d-tags">
            <span class="d-type">设计里的{{ nodeType }}</span>
            <span v-if="!nodeMark && r.comparison?.matches" class="d-flag d-added">和实现对得上</span>
          </div>
          <h2 class="d-title">{{ node.type === 'start' ? `开始：${node.titleFull}` : node.titleFull }}</h2>
          <ul v-if="nodeMark" class="d-list rq-node-why">
            <li v-for="(t, i) in nodeMark.texts" :key="i">{{ t }}</li>
          </ul>
          <p class="d-hint">设计图里的节点由需求文件写成，没有对应的源码。</p>
        </div>

        <div class="d-tags">
          <span class="d-type task">需求</span>
          <code v-if="r.id" class="rq-panel-id">{{ r.id }}</code>
          <span class="rq-status" :class="statusClass(r.status)">{{ r.status || '没有状态' }}</span>
        </div>
        <h2 class="d-title">{{ r.title || r.file }}</h2>
        <p v-if="verdict" class="rq-verdict" :class="`rq-v-${verdict.tone}`" role="status">{{ verdict.text }}</p>

        <section v-if="r.errors.length" class="d-sec">
          <h3>读不懂的地方 <span class="d-n">{{ r.errors.length }}</span></h3>
          <ul class="d-list rq-errors">
            <li v-for="(e, i) in r.errors" :key="i">{{ e }}</li>
          </ul>
        </section>

        <section v-if="paragraphs.length" class="d-sec">
          <h3>说明</h3>
          <p v-for="(p, i) in paragraphs" :key="i" class="rq-desc">{{ p }}</p>
        </section>

        <section v-if="diffs.length" class="d-sec">
          <h3>不一致的地方 <span class="d-n">{{ diffs.length }}</span></h3>
          <ul class="d-list rq-diffs">
            <li v-for="(d, i) in diffs" :key="i">
              <span class="rq-kind" :class="`k-${d.kind}`">{{ d.name }}</span>
              <span class="rq-diff-text">
                {{ d.text }}
                <button v-if="d.design.length" class="d-link" @click="focusDesignNode(d.design[0])">在图上看</button>
                <button v-if="d.impl && implLine" class="d-link" @click="gotoImplNode(implLine, d.impl)">到功能线上看</button>
              </span>
            </li>
          </ul>
        </section>

        <section v-if="r.comparison?.notes.length" class="d-sec">
          <h3>备注</h3>
          <ul class="d-list">
            <li v-for="(t, i) in r.comparison.notes" :key="i" class="d-dim">{{ t }}</li>
          </ul>
        </section>

        <section v-if="outcomes.rows.length" class="d-sec">
          <h3>设计的结局有没有测试走到 <span class="d-n">{{ outcomes.rows.length }}</span></h3>
          <p v-if="outcomes.hint" class="d-hint">{{ outcomes.hint }}</p>
          <ul class="d-list rq-outcomes">
            <li v-for="(o, i) in outcomes.rows" :key="i">
              <button class="rq-outcome" title="在设计图上看这个结局" @click="focusOutcome(o.title)">
                <span class="dot" :style="{ background: endColor(o.kind) }" />{{ o.title }}
              </button>
              <span v-if="o.text" class="rq-cov" :class="`rq-cov-${o.tone}`">{{ o.text }}</span>
            </li>
          </ul>
          <p v-if="run !== null" class="d-dim rq-run">按第 {{ run }} 次测试运行算</p>
        </section>

        <section class="d-sec">
          <h3>需求文件</h3>
          <code class="d-code">{{ r.file }}</code>
          <button v-if="r.text" class="d-link rq-open" @click="showFile = true">查看文件内容</button>
          <p v-if="reachable" class="rq-reach">{{ reachable }}</p>
          <p class="d-hint">只读：改需求用 MCP 的 save_requirement，或者直接改这个文件，改完点左边的"刷新"。</p>
        </section>
      </div>
    </n-scrollbar>
  </aside>
  <n-modal
    v-if="r"
    v-model:show="showFile"
    preset="card"
    :title="r.file"
    class="pd"
    :style="{ width: 'min(860px, 92vw)' }"
  >
    <CodeBlock v-if="showRaw" :lines="fileLines" max-height="70vh" label="需求文件原文" />
    <!-- 内容来自被分析的项目：renderMarkdown 不认文件里的 HTML，链接协议也把过关 -->
    <div v-else class="md" tabindex="0" aria-label="需求文件" v-html="fileHtml" />
    <p class="d-hint" style="margin: 10px 0 0">
      这是项目里的文件，只读。改它用 MCP 的 save_requirement，或者直接在编辑器里改，改完点左边的"刷新"。
      <button class="d-link" @click="showRaw = !showRaw">{{ showRaw ? '看渲染好的' : '看原文' }}</button>
    </p>
  </n-modal>
</template>
