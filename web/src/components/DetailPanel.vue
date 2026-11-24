<script setup lang="ts">
// 右侧详情栏：选中的节点 / 折叠 / 卡片的说明。没有选中时显示图例（第一次看图时用得上）。
// 标题、条件原文、分支模式都写完整的（titleFull 等），不是画布上截断过的显示形式。
// 节点和卡片在"文件:行"下面带源码片段（平台后端的 /source 接口，见 src/source.ts），"复制给 AI"把片段一起带上，
// 还写上所在功能线的标题（手工改过的用改过的）。
// 和对比基准比：改动的节点多一段"原来是：<旧的标题 / 条件原文>（原第 N 行）"，新增的标"对比基准之后新增"，节点后面
// 删掉了东西时列出来；判断节点等调用的函数（判断函数）变了时列出那个函数里变了什么；卡片、折叠写里面有几处变化
// （diffview.ts）。
// 运行路径页签里（embedded）：右侧栏是运行面板，节点详情嵌在面板上方的一块里（RunPanel.vue），不画图例，不写流程差异
// 的东西，另加一行"这个测试里：走过 3 次，共 12 ms，占 40%"（runstate.ts 的 testRunMarks，回放时也是整个测试的）；测试命令还在跑、画布是运行中
// 的样子时写"运行中：已跑完的测试走到过这里"（只知道执行过哪些行，没有次数、耗时）。
// 看变更记录时（state.ts 的 review）：说的是那时的图和那次的变化；源码片段按记录那次分析取（analysis=），能确定是当时的
// 代码才不加说明，否则说"这是现在的源码，行号是当时的，可能对不上"（source.ts 的 reviewSourceNote）。
import { computed } from 'vue'
import { NButton, NScrollbar, useMessage } from 'naive-ui'
import { CHAIN_NOTE, type CardView, type FoldView, type NodeView } from '../graph'
import { cardAiText, editorUrl, foldAiText, locText, nodeAiText } from '../describe'
import { calleeNoteLines, changedNote } from '../diffview'
import {
  canvasGraph,
  canvasOf,
  cardById,
  closeFold,
  currentDisplay,
  diffView,
  drill,
  foldById,
  gotoCard,
  marks,
  openFold,
  projectName,
  review,
  reviewHeadKept,
  selected,
  tab,
  toggleCard,
  undrill,
} from '../state'
import { failure, testRunMarks } from '../runstate'
import { failureText } from '../runfailure'
import { formatNs, uncoveredCountText } from '../runview'
import { C, END_COLOR, END_NAME, FN_KIND_NAME, NODE_TYPE_NAME, RESOLUTION_HINT, RESOLUTION_NAME } from '../theme'
import { functionHeadRequest, nodeSourceRequest, reviewSourceNote, snippetForAi, useSource } from '../source'
import Icon from './Icon.vue'
import SourceView from './SourceView.vue'

const props = defineProps<{ embedded?: boolean }>()
const message = useMessage()
/** 流程设计页签：流程差异的东西只在这里写 */
const design = computed(() => tab.value === 'design')

const TYPE_COLOR: Record<string, string> = {
  start: C.textSoft,
  step: C.textSoft,
  cond: C.decision,
  loop: C.loop,
  catch: C.catch,
}

/** 包含这个节点的已展开折叠区域，从里到外 */
function foldsOf(n: NodeView): FoldView[] {
  const out: FoldView[] = []
  for (let id = n.fold; id; ) {
    const f = foldById.value.get(id)
    if (!f) break
    out.push(f)
    id = f.parent
  }
  return out
}

const node = computed(() => (selected.value?.kind === 'node' ? selected.value.node : null))
const fold = computed(() => (selected.value?.kind === 'fold' ? selected.value.fold : null))
const card = computed(() => (selected.value?.kind === 'card' ? selected.value.card : null))
const nodeCard = computed(() => (node.value ? cardById.value.get(node.value.card) : undefined))

// 和对比基准比
const nodeMark = computed(() => (node.value ? (marks.value.nodes.get(node.value.id) ?? null) : null))
const nodeFn = computed(() => nodeCard.value?.fn ?? null)
const was = computed(() => {
  if (!design.value) return null
  const n = node.value
  const from = n && nodeFn.value ? diffView.value.changedFrom.get(nodeFn.value)?.get(n.key) : undefined
  return n && from ? changedNote(from, n) : null
})
/** 只因为调用的函数变了才标的"改动" */
const byCallee = computed(() => !!node.value && marks.value.callee.has(node.value.id))
/** 它调用的、不能钻入的函数（判断函数等）里变了什么，一句一条 */
const calleeLines = computed(() => {
  if (!design.value) return []
  const n = node.value
  const notes = n && nodeFn.value ? (diffView.value.calleeNotes.get(nodeFn.value)?.get(n.key) ?? []) : []
  return notes.flatMap(calleeNoteLines)
})
/** 这一步后面删掉的节点、从它出发删掉的连线 */
const removedAfter = computed(() => {
  if (!design.value) return []
  const n = node.value
  return n && nodeFn.value ? (diffView.value.removed.get(nodeFn.value) ?? []).filter((r) => r.after === n.key) : []
})
const cardStatus = computed(() => (card.value ? (marks.value.cards.get(card.value.id) ?? null) : null))
/** 这条线有几处变化在这张卡片里（差异栏的处数按卡片分） */
const cardChanges = computed(() => (card.value ? (marks.value.cardStops.get(card.value.id) ?? 0) : 0))
const foldChanges = computed(() => (fold.value ? (marks.value.counts.get(fold.value.id) ?? 0) : 0))

// 运行路径：失败的测试的红圈就在这里时说是怎么回事（停在这里 / 异常是在它调用的函数里抛出的）
const stopLine = computed(() => {
  const f = failure.value
  const id = node.value?.id ?? fold.value?.id ?? card.value?.id
  if (!props.embedded || !f || (f.kind !== 'stop' && f.kind !== 'direct') || !id || !testRunMarks.value.stops.has(id)) return null
  return failureText(f)
})
// 没走到的结局：这个节点调用的函数里（或这个折叠、卡片里）有几个
const uncoveredLine = computed(() => {
  const id = node.value?.id ?? fold.value?.id ?? card.value?.id
  const c = props.embedded && id ? testRunMarks.value.uncoveredInside.get(id) : undefined
  return c ? uncoveredCountText(c, node.value ? 'callee' : 'inside') : null
})

// 运行路径：选中的测试在这里走过几次、多久、占这条线的多少
const runLine = computed(() => {
  const m = testRunMarks.value
  if (!props.embedded || !m.active) return null
  if (m.live) {
    // 运行中：只知道已跑完的测试执行过哪些行（没有次数、耗时）
    if (node.value) {
      const hit = m.nodes.has(node.value.id)
      return { walked: hit, text: hit ? '运行中：已跑完的测试走到过这里' : '运行中：已跑完的测试里还没看到走到这里' }
    }
    if (fold.value) {
      const inside = m.inside.has(fold.value.id)
      return { walked: inside, text: inside ? '运行中：已跑完的测试走到过里面的节点（展开看）' : '运行中：已跑完的测试里还没看到走到里面' }
    }
    if (card.value) {
      const ran = m.cardsRun.has(card.value.id)
      return { walked: ran, text: ran ? '运行中：已跑完的测试执行过这个函数' : '运行中：已跑完的测试里还没看到执行这个函数' }
    }
    return null
  }
  if (node.value) {
    const r = m.nodes.get(node.value.id)
    if (!r) return { walked: false, text: '这个测试里：没有走到这里' }
    const time = m.hasTime && r.ns > 0 ? `，共 ${formatNs(r.ns)}（含调用的函数）${r.share > 0 ? `，占 ${Math.round(r.share * 100)}%` : ''}` : ''
    return { walked: true, text: `这个测试里：走过 ${r.visits} 次${time}` }
  }
  if (fold.value) {
    const inside = m.inside.has(fold.value.id)
    return { walked: inside, text: inside ? '这个测试里：走过里面的节点（展开看）' : '这个测试里：没有走到里面' }
  }
  if (card.value) {
    const ran = m.cardsRun.has(card.value.id)
    return { walked: ran, text: ran ? '这个测试里：执行过这个函数' : '这个测试里：没有执行这个函数' }
  }
  return null
})

// 源码片段：节点取它自己的语句和前后几行，卡片取函数开头（签名和文档注释）
const sourceRequest = computed(() => {
  if (node.value) return nodeSourceRequest(node.value.loc)
  const c = card.value
  if (!c) return null
  const fn = c.fn ? canvasGraph.value?.functions[c.fn] : undefined
  return functionHeadRequest(c.loc, fn?.end_line ?? null)
})
// 看变更记录时按记录的那次分析判断文件改过没有
const source = useSource(canvasOf, sourceRequest)
/** 看变更记录时片段上的说明（null：确定是当时的代码）；不在看记录时 undefined（照常按 stale 说） */
const sourceNote = computed<string | null | undefined>(() => {
  const s = source.snippet.value
  const req = sourceRequest.value
  if (!review.value) return undefined
  return s && req ? reviewSourceNote(s, req, reviewHeadKept.value) : null
})
/** "复制给 AI"的上下文：项目名、功能线标题（手工改过的用改过的） */
const ai = computed(() => ({ project: projectName.value, line: currentDisplay.value?.titleFull ?? '' }))
/** 复制给 AI 时附上的源码（取到了才有） */
function aiSource(what: string): string {
  const s = source.snippet.value
  return s && sourceRequest.value ? snippetForAi(s, sourceRequest.value, what, sourceNote.value) : ''
}

function typeColor(n: NodeView) {
  return n.type === 'end' ? END_COLOR[n.endKind ?? 'ok'] : TYPE_COLOR[n.type]
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制，可以直接粘贴给 AI')
  } catch {
    message.error('浏览器不让写剪贴板：请在 localhost 打开页面后再试')
  }
}

function cardEnds(c: CardView) {
  return (['ok', 'skipped', 'failed'] as const)
    .filter((k) => c.summary.ends[k])
    .map((k) => ({ k, n: c.summary.ends[k]!, color: END_COLOR[k], name: END_NAME[k] }))
}

/** 钻入卡片：同一个步骤还展开着别的被调函数（收起这张不会变回步骤） */
const chainOthers = (c: CardView): boolean => !!c.anchor?.drills.some((d) => d.open && d.id !== c.id)
</script>

<template>
  <component :is="embedded ? 'div' : 'aside'" :class="embedded ? 'detail-embedded' : 'detail'" aria-label="详情">
    <component :is="embedded ? 'div' : NScrollbar">
      <div class="detail-inner">
        <p v-if="stopLine" class="d-run d-run-stop">{{ stopLine }}</p>
        <p v-if="runLine" class="d-run" :class="{ walked: runLine.walked }">{{ runLine.text }}</p>
        <p v-if="uncoveredLine" class="d-run d-run-stop">{{ uncoveredLine }}</p>
        <!-- 节点 -->
        <template v-if="node">
          <div class="d-tags">
            <span class="d-type" :style="{ color: typeColor(node), borderColor: typeColor(node) }">
              {{ node.type === 'end' && node.endKind ? `结局 · ${END_NAME[node.endKind]}` : NODE_TYPE_NAME[node.type] }}
            </span>
            <span v-if="node.trunk" class="d-flag">在主线上</span>
            <span v-if="node.implicit === 'uncaught'" class="d-flag">隐含节点：任何一步抛出没被接住的异常都从这里离开</span>
            <span v-else-if="node.implicit === 'end'" class="d-flag">隐含节点：执行到函数末尾</span>
            <span v-if="nodeMark === 'added'" class="d-flag d-added">对比基准之后新增</span>
            <span v-else-if="nodeMark === 'changed' && byCallee" class="d-flag d-changed">调用的函数变了</span>
            <span v-else-if="nodeMark === 'changed'" class="d-flag d-changed">和对比基准比改动过</span>
          </div>
          <h2 class="d-title">{{ node.type === 'start' ? `开始：${node.titleFull}` : node.titleFull }}</h2>
          <code v-if="node.subtitle" class="d-code">{{ node.subtitleFull }}</code>
          <div v-if="was" class="d-was">
            <span>{{ was.text }}</span>
            <code v-if="was.code" class="d-code">{{ was.code }}</code>
          </div>
          <div v-if="nodeCard" class="d-fn">
            所在函数 <code>{{ nodeCard.name }}</code>
          </div>
          <div v-if="node.loc" class="d-loc">
            <code>{{ locText(node.loc) }}</code>
          </div>
          <SourceView
            :status="source.status.value"
            :snippet="source.snippet.value"
            :error="source.error.value"
            :request="sourceRequest"
            :note="sourceNote"
          />
          <div class="d-actions">
            <n-button v-if="node.loc" tag="a" :href="editorUrl(node.loc)" type="primary" size="small">
              <template #icon><Icon name="editor" /></template>在编辑器中打开
            </n-button>
            <n-button size="small" secondary @click="copy(nodeAiText(node, nodeCard, ai, aiSource('节点')))">
              <template #icon><Icon name="copy" /></template>复制给 AI
            </n-button>
          </div>

          <section v-if="calleeLines.length" class="d-sec">
            <h3>调用的函数里变了什么</h3>
            <ul class="d-list">
              <li v-for="(l, i) in calleeLines" :key="i">{{ l }}</li>
            </ul>
          </section>

          <section v-if="removedAfter.length" class="d-sec">
            <h3>这一步后面删掉了 <span class="d-n">{{ removedAfter.length }}</span></h3>
            <ul class="d-list d-removed">
              <li v-for="(r, i) in removedAfter" :key="i">
                <template v-if="r.what === 'node'">
                  <span v-if="r.label" class="d-dim">「{{ r.label }}」那边：</span>
                  <span>{{ NODE_TYPE_NAME[r.type as keyof typeof NODE_TYPE_NAME] ?? '节点' }}：{{ r.title }}</span>
                  <span v-if="r.line" class="d-dim">（原第 {{ r.line }} 行）</span>
                </template>
                <template v-else>
                  <span>连线：{{ r.title }}</span><span v-if="r.subtitle" class="d-dim">（{{ r.subtitle }}）</span>
                </template>
              </li>
            </ul>
          </section>

          <section v-if="node.calls.length" class="d-sec">
            <h3>调用 <span class="d-n">{{ node.calls.length }}</span></h3>
            <ul class="d-calls">
              <li v-for="(c, i) in node.calls" :key="i">
                <div class="call-head">
                  <code class="call-name">{{ c.name }}</code>
                  <span class="res" :class="`res-${c.resolution}`" :title="RESOLUTION_HINT[c.resolution]">
                    {{ RESOLUTION_NAME[c.resolution] }}
                  </span>
                  <span v-if="c.isDispatch" class="res res-dispatch">派发</span>
                  <span v-if="c.conditional" class="res res-cond" title="在三元表达式、and / or 短路或 case 守卫里">不一定执行</span>
                </div>
                <div v-if="c.targetTitle" class="call-target">{{ c.targetTitle }}</div>
                <div v-else-if="c.resolution === 'unresolved'" class="call-target dim">没法判断调用的是哪个函数</div>
              </li>
            </ul>
          </section>

          <section v-if="node.drills.length" class="d-sec">
            <h3>钻入</h3>
            <p class="d-hint">
              在原地把被调用的函数展开成一张内嵌卡片，看它里面的流程。<template v-if="node.drills.length > 1"
                >可以钻入几个，几张卡片排成一串（{{ CHAIN_NOTE }}）。</template
              >
            </p>
            <div class="d-btns">
              <n-button v-for="d in node.drills" :key="d.id" size="small" secondary @click="drill(node.id, d)">
                <template #icon><Icon name="drill" /></template>{{ d.name }}{{ d.resolution === 'guessed' ? '（推测）' : '' }}
              </n-button>
              <n-button v-if="node.drills.length > 1" size="small" secondary @click="drill(node.id, node.drills)">
                都钻入（{{ node.drills.length }} 个）
              </n-button>
            </div>
          </section>

          <section v-if="node.dispatches.length" class="d-sec">
            <h3>派发的任务</h3>
            <ul class="d-list">
              <li v-for="d in node.dispatches" :key="d.task">
                <span>{{ d.title }}</span>
                <span v-if="!d.direct" class="d-dim">（经调用的函数派发）</span>
                <span v-if="d.resolution === 'guessed'" class="res res-guessed">推测</span>
                <button v-if="d.card" class="d-link" @click="gotoCard(d.card)">跳到卡片</button>
                <span v-else-if="d.common" class="d-dim">公共任务，单独成线</span>
              </li>
            </ul>
          </section>

          <section v-if="Object.keys(node.fields).length" class="d-sec">
            <h3>结局携带的字段</h3>
            <dl class="d-fields">
              <template v-for="(v, k) in node.fields" :key="k">
                <dt>{{ k }}</dt>
                <dd><code>{{ v }}</code></dd>
              </template>
            </dl>
          </section>

          <section v-if="node.passthrough" class="d-sec">
            <h3>转交返回</h3>
            <p class="d-hint">直接返回被调用函数的结果：<code>{{ node.passthrough.split('::').pop()?.split('@')[0] }}</code></p>
          </section>

          <section v-if="foldsOf(node).length" class="d-sec">
            <h3>所在的分支</h3>
            <ul class="d-list">
              <li v-for="f in foldsOf(node)" :key="f.id">
                <span>{{ f.kind === 'catch' ? `异常处理（${f.labelFull}）` : `「${f.labelFull}」分支` }}，{{ f.summary.nodes }} 个节点</span>
                <button class="d-link" @click="closeFold(f)">收起</button>
              </li>
            </ul>
          </section>
        </template>

        <!-- 折叠占位 -->
        <template v-else-if="fold">
          <div class="d-tags"><span class="d-type">折叠的{{ fold.kind === 'catch' ? '异常处理' : '分支' }}</span></div>
          <h2 class="d-title">{{ fold.kind === 'catch' ? fold.titleFull : `「${fold.labelFull}」：${fold.titleFull}` }}</h2>
          <p class="d-hint">{{ fold.summary.nodes }} 个节点{{ fold.summary.rejoins ? '，走完后回到主线' : '' }}</p>
          <p v-if="foldChanges" class="d-hint d-changed-text">里面有 {{ foldChanges }} 处和对比基准不一样，展开看</p>
          <div class="d-actions">
            <n-button type="primary" size="small" @click="openFold(fold)">
              <template #icon><Icon name="plus" /></template>在原地展开
            </n-button>
            <n-button size="small" secondary @click="copy(foldAiText(fold, cardById.get(fold.card), ai))">
              <template #icon><Icon name="copy" /></template>复制给 AI
            </n-button>
          </div>
          <section v-if="fold.summary.ends.length" class="d-sec">
            <h3>里面的结局</h3>
            <ul class="d-list">
              <li v-for="(e, i) in fold.summary.ends" :key="i">
                <span class="dot" :style="{ background: e.kind ? END_COLOR[e.kind] : C.faint }" />{{ e.title }}
              </li>
            </ul>
          </section>
          <section v-if="fold.summary.dispatches.length" class="d-sec">
            <h3>里面派发的任务</h3>
            <ul class="d-list">
              <li v-for="t in fold.summary.dispatches" :key="t">{{ t }}</li>
            </ul>
          </section>
        </template>

        <!-- 卡片 -->
        <template v-else-if="card">
          <div class="d-tags">
            <span class="d-type task">{{ card.kind === 'drill' ? '钻入的函数' : card.fnKind ? FN_KIND_NAME[card.fnKind] : '任务' }}</span>
            <span v-if="card.retries" class="d-flag">失败会重试</span>
            <span v-if="card.goto" class="d-flag">回到前面出现过的任务</span>
            <span v-if="cardStatus === 'added'" class="d-flag d-added">对比基准之后新增的函数</span>
            <span v-else-if="cardStatus === 'changed'" class="d-flag d-changed">和对比基准比有改动</span>
            <span v-else-if="cardStatus === 'callee'" class="d-flag d-changed">调用的函数有改动</span>
          </div>
          <h2 class="d-title">{{ card.titleFull }}</h2>
          <code class="d-code">{{ card.route ? `${card.route}  ${card.name}` : card.name }}</code>
          <div v-if="card.loc" class="d-loc"><code>{{ locText(card.loc) }}</code></div>
          <SourceView
            :status="source.status.value"
            :snippet="source.snippet.value"
            :error="source.error.value"
            :request="sourceRequest"
            :note="sourceNote"
          />
          <div class="d-actions">
            <n-button v-if="card.loc" tag="a" :href="editorUrl(card.loc)" type="primary" size="small">
              <template #icon><Icon name="editor" /></template>在编辑器中打开
            </n-button>
            <n-button size="small" secondary @click="copy(cardAiText(card, ai, aiSource('函数定义')))">
              <template #icon><Icon name="copy" /></template>复制给 AI
            </n-button>
          </div>
          <section class="d-sec">
            <h3>概况</h3>
            <p class="d-hint">
              {{ card.missing ? '图里没有这个函数（可能在分析范围之外）' : `${card.summary.nodes} 个节点` }}
              <template v-if="cardChanges && cardStatus !== 'added'">，这条线有 {{ cardChanges }} 处变化在这里（含钻入后的）</template>
              <template v-for="e in cardEnds(card)" :key="e.k">
                <span class="d-end"><span class="dot" :style="{ background: e.color }" />{{ e.name }} {{ e.n }}</span>
              </template>
            </p>
            <div class="d-btns">
              <n-button v-if="card.goto && card.gotoTarget" size="small" secondary @click="gotoCard(card.gotoTarget)">
                <template #icon><Icon name="goto" /></template>跳到第一次出现的地方
              </n-button>
              <n-button v-else-if="card.kind === 'drill'" size="small" secondary @click="undrill(card)">
                {{ chainOthers(card) ? '收起这张（同一步展开着的别的照旧）' : '收回成原来的步骤' }}
              </n-button>
              <n-button v-else-if="card.expandable" size="small" secondary @click="toggleCard(card)">
                {{ card.expanded ? '收起这个任务' : '展开这个任务' }}
              </n-button>
            </div>
          </section>
          <section v-if="card.kind === 'drill' && card.anchor" class="d-sec">
            <h3>原来的步骤</h3>
            <p class="d-hint">{{ card.anchor.titleFull }}<br /><code>{{ card.anchor.subtitleFull }}</code></p>
            <template v-if="card.anchor.drills.length > 1">
              <p class="d-hint">
                这一步调用了 {{ card.anchor.drills.length }} 个能钻入的函数，展开着的排成一串、代替这一步：{{ CHAIN_NOTE }}。
              </p>
              <ul class="d-list">
                <li v-for="(d, i) in card.anchor.drills" :key="d.id">
                  <span>{{ i + 1 }}. {{ d.title }}</span>
                  <span class="d-dim">（{{ d.name }}，{{ d.id === card.id ? '就是这张' : d.open ? '展开着' : '没展开' }}）</span>
                  <button v-if="d.id !== card.id && d.open" class="d-link" @click="gotoCard(d.id)">跳到卡片</button>
                  <button v-else-if="!d.open" class="d-link" @click="drill(card.anchor.id, d, card.id)">展开</button>
                </li>
              </ul>
            </template>
          </section>
        </template>

        <!-- 没有选中：图例（嵌在运行面板里时不画） -->
        <template v-else-if="!embedded">
          <h2 class="d-title quiet">点画布上的节点看详情</h2>
          <p class="d-hint">
            点任务卡片的标题栏展开或收起；点虚线框展开折叠起来的分支；点步骤左边的方框图标钻入被调用的函数。
            触控板双指拖动画布，捏合缩放。
          </p>
          <section class="d-sec legend">
            <h3>节点</h3>
            <ul>
              <li><span class="lg-node" /><span>步骤：中文标题，下面是调用的函数</span></li>
              <li><span class="lg-node cond" /><span>判断：分支标签贴在各自的出边上</span></li>
              <li><span class="lg-node loop" /><span>循环</span></li>
              <li><span class="lg-node catch" /><span>捕获异常</span></li>
              <li><span class="lg-node fold" /><span>折叠起来的分支，点开在原地展开</span></li>
            </ul>
            <h3>结局</h3>
            <ul class="lg-ends">
              <li v-for="k in (['ok', 'skipped', 'failed', 'exception'] as const)" :key="k">
                <span class="dot" :style="{ background: END_COLOR[k] }" />{{ END_NAME[k] }}
              </li>
            </ul>
            <h3>连线</h3>
            <ul>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.trunk" stroke-width="2.4" /></svg><span>主线：函数从开始到正常结束最长的那条路</span></li>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.edge" stroke-width="1.4" /></svg><span>其他走向</span></li>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.loop" stroke-width="1.6" /></svg><span>回到循环开头</span></li>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.catch" stroke-width="1.6" stroke-dasharray="5 4" /></svg><span>抛出异常后落到哪里</span></li>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.finally" stroke-width="1.6" stroke-dasharray="2 3.5" /></svg><span>进入 finally 收尾</span></li>
              <li><svg class="lg-line" viewBox="0 0 40 8"><path d="M2 4h36" :stroke="C.task" stroke-width="2.2" stroke-dasharray="8 5" /></svg><span>派发后台任务</span></li>
            </ul>
            <h3>标记</h3>
            <ul>
              <li><span class="chip chip-dispatch">派发</span><span>这一步会派发后台任务</span></li>
              <li><span class="chip chip-guessed">推测</span><span>调用目标是按方法名推测的，不保证对</span></li>
              <li><span class="chip chip-unresolved">目标不明</span><span>没法判断调用的是哪个函数</span></li>
              <li><span class="lg-mark added">新增</span><span>对比基准之后新增的节点（绿色外圈；绿色的连线是新增的连线）。对比基准是上次标为已看时，没标过就是第一次分析；差异栏第一句写着是哪一次</span></li>
              <li><span class="lg-mark changed">改动</span><span>和对比基准比改动过的节点（琥珀色外圈），或者它调用的判断函数等变了；点开看原来的样子</span></li>
              <li><span class="lg-mark changed">3</span><span>折叠着的卡片、分支或钻入后的函数里有几处变化</span></li>
            </ul>
          </section>
        </template>
      </div>
    </component>
  </component>
</template>
