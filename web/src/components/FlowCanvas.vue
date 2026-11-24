<script setup lang="ts">
// 画布：Vue Flow 只当平移 / 缩放 / 小地图的底座，节点位置全部来自排版（layoutLineView），节点不能拖。
// 层次：任务卡片（容器）在最下，连线在中间，节点在最上；钻入卡片按嵌套层数往上叠。
// 缩小到 MAP_ZOOM 以下进入地图模式：节点只剩色块，卡片标题由 MapLabels 按屏幕字号画。
// 画布上方：过期条（StaleBar，代码改过时）、差异栏（DiffBar，这条线和对比基准比有变化时）。和对比基准比的变化
// 由各节点组件按 state.ts 的 marks 标出来（外圈）；小地图里有变化的节点、卡片描成变化的颜色，底色仍按类型。
// 两个页签：流程设计（上面这些）和运行路径（选中的测试走过的路径：RunBar 代替差异栏，节点、连线按 runstate.ts 的
// canvasRunMarks 标，小地图里走过的描蓝、停点描红）。换页签不重新排版，标注都挂在节点 / 连线的 id 上。
// 看需求时（左边列表切到"需求"）：画的是需求设计的流程（同一套节点组件），标题区写需求的标题、编号、状态和"功能线：…"
// （点了换到那条功能线），没有页签、差异栏；和实现对不上的节点由各节点组件按 state.ts 的 reqMarks 标，小地图里描红 / 琥珀。
// 看变更记录时（state.ts 的 review）：画的是那条记录当时的图，差异栏和标记是那次的变化（同一套组件），没有页签、过期条，
// 差异栏里没有"标为已看"。进来时记下视口，回到现在、还是原来那条线时放回去。
import { computed, markRaw, nextTick, shallowRef, watch } from 'vue'
import { VueFlow, useVueFlow, type Edge, type EdgeTypesObject, type Node, type NodeTypesObject, type ViewportTransform } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { MiniMap } from '@vue-flow/minimap'
import { Controls } from '@vue-flow/controls'
import { NButton, NTooltip } from 'naive-ui'
import { METRICS, nodeSize, type EdgeView, type FoldView, type LineView, type Positioned } from '../graph'
import {
  canvasCommand,
  canvasMoving,
  clearSelection,
  currentDisplay,
  currentReq,
  displayBySid,
  expandAll,
  fitCanvas,
  layout,
  layoutReason,
  layoutTick,
  marks,
  openReqLine,
  reqMarks,
  reqMode,
  resetDefault,
  review,
  tab,
  takeAnchor,
  view,
  type Anchor,
} from '../state'
import { canvasRunMarks, running } from '../runstate'
import { statusClass } from '../requirements'
import { C, DIFF_COLOR, END_COLOR, FN_KIND_NAME, MAP_ZOOM } from '../theme'
import { screenRect, viewportAfterChange } from '../viewport'
import CanvasStatus from './CanvasStatus.vue'
import DiffBar from './DiffBar.vue'
import RunBar from './RunBar.vue'
import StaleBar from './StaleBar.vue'
import Icon from './Icon.vue'
import MapLabels from './MapLabels.vue'
import PolyEdge from './edges/PolyEdge.vue'
import CatchNode from './nodes/CatchNode.vue'
import CondNode from './nodes/CondNode.vue'
import EndNode from './nodes/EndNode.vue'
import FoldNode from './nodes/FoldNode.vue'
import GotoNode from './nodes/GotoNode.vue'
import LoopNode from './nodes/LoopNode.vue'
import StartNode from './nodes/StartNode.vue'
import StepNode from './nodes/StepNode.vue'
import TaskCardNode from './nodes/TaskCardNode.vue'
import type { CardData, EdgeData, FoldData, LeafData } from './nodes/types'

const FLOW_ID = 'pystepmap-canvas'
const vf = useVueFlow(FLOW_ID)
// 开发时给截图脚本用：window.__pystepmap.vf.setViewport(...)
if (import.meta.env.DEV) (window as unknown as Record<string, unknown>).__pystepmap = { vf, view, layout }

const nodeTypes = {
  'fm-start': markRaw(StartNode),
  'fm-step': markRaw(StepNode),
  'fm-cond': markRaw(CondNode),
  'fm-loop': markRaw(LoopNode),
  'fm-catch': markRaw(CatchNode),
  'fm-end': markRaw(EndNode),
  'fm-fold': markRaw(FoldNode),
  'fm-card': markRaw(TaskCardNode),
  'fm-goto': markRaw(GotoNode),
} as unknown as NodeTypesObject // 节点组件只声明自己用到的 data 属性
const edgeTypes = { fm: markRaw(PolyEdge) } as unknown as EdgeTypesObject

const nodes = shallowRef<Node[]>([])
const edges = shallowRef<Edge[]>([])
const reduceMotion = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
const dur = (ms: number) => (reduceMotion ? 0 : ms)

// 排版里的字号、行高、内边距 → CSS 变量（节点的 DOM 尺寸必须和估算一致）
const M = METRICS
const metricVars = {
  '--n-title': `${M.node.titleFont}px`,
  '--n-title-lh': `${M.node.titleLine}px`,
  '--n-sub': `${M.node.subFont}px`,
  '--n-sub-lh': `${M.node.subLine}px`,
  '--n-padx': `${M.node.padX}px`,
  '--n-pady': `${M.node.padY}px`,
  '--n-icon': `${M.node.icon}px`,
  '--p-font': `${M.pill.font}px`,
  '--p-padx': `${M.pill.padX}px`,
  '--p-icon': `${M.pill.icon}px`,
  '--chip-font': `${M.chip.font}px`,
  '--chip-padx': `${M.chip.padX}px`,
  '--chip-gap': `${M.chip.gap}px`,
  '--f-font': `${M.fold.font}px`,
  '--f-lh': `${M.fold.line}px`,
  '--f-sub': `${M.fold.subFont}px`,
  '--f-sub-lh': `${M.fold.subLine}px`,
  '--f-padx': `${M.fold.padX}px`,
  '--f-pady': `${M.fold.padY}px`,
  '--f-icon': `${M.fold.icon}px`,
  '--c-title': `${M.card.titleFont}px`,
  '--c-title-lh': `${M.card.titleLine}px`,
  '--c-sub': `${M.card.subFont}px`,
  '--c-sub-lh': `${M.card.subLine}px`,
  '--c-padx': `${M.card.padX}px`,
  '--c-pady': `${M.card.padY}px`,
  '--l-font': `${M.label.font}px`,
}

// ---------------------------------------------------------------------------------------------------------------
// 视图 + 排版 → Vue Flow 的节点和连线

function tone(e: EdgeView): EdgeData['tone'] {
  if (!e.label) return null
  if (e.branch === null) return 'loop'
  if (e.label === '是' && e.branch === 'yes') return 'yes'
  if (e.label === '否' && e.branch === 'no') return 'no'
  if (e.branch === 'else') return 'else'
  return 'case'
}

const Z_CARD = 0
const Z_EDGE = 50
const Z_TRUNK = 52
const Z_LINK = 48
const Z_LEAF = 100

function toFlow(v: LineView, p: Positioned): { nodes: Node[]; edges: Edge[] } {
  const out: Node[] = []
  const common = { draggable: false, selectable: false, connectable: false, focusable: false }
  // 已展开的折叠区域：入口节点上放"收起"（同一个入口有几个时取最小的那个）
  const entry = new Map<string, FoldView>()
  for (const f of v.folds) {
    if (f.collapsed) continue
    const prev = entry.get(f.members[0])
    if (!prev || prev.members.length > f.members.length) entry.set(f.members[0], f)
  }
  for (const c of v.cards) {
    const b = p.boxes[c.id]
    if (!b) continue
    const data: CardData = { card: c, width: b.width, height: b.height }
    out.push({
      ...common,
      id: c.id,
      type: c.goto ? 'fm-goto' : 'fm-card',
      position: { x: b.x, y: b.y },
      zIndex: c.kind === 'drill' ? Z_CARD + c.depth : Z_CARD,
      data,
    })
  }
  for (const n of v.nodes) {
    const b = p.boxes[n.id]
    if (!b) continue
    const data: LeafData = {
      node: n,
      width: b.width,
      height: b.height,
      titleLines: nodeSize(n).titleLines,
      foldEntry: entry.get(n.id) ?? null,
    }
    out.push({ ...common, id: n.id, type: `fm-${n.type}`, position: { x: b.x, y: b.y }, zIndex: Z_LEAF, data })
  }
  for (const f of v.folds) {
    if (!f.collapsed) continue
    const b = p.boxes[f.id]
    if (!b) continue
    const data: FoldData = { fold: f, width: b.width, height: b.height }
    out.push({ ...common, id: f.id, type: 'fm-fold', position: { x: b.x, y: b.y }, zIndex: Z_LEAF, data })
  }
  const es: Edge[] = []
  for (const e of v.edges) {
    const pos = p.edges[e.id]
    if (!pos) continue
    const data: EdgeData = { pos, edge: e, link: null, resolution: null, tone: tone(e) }
    es.push({ id: e.id, type: 'fm', source: e.src, target: e.dst, zIndex: e.trunk ? Z_TRUNK : Z_EDGE, data, selectable: false, focusable: false })
  }
  for (const l of v.links) {
    const pos = p.edges[l.id]
    if (!pos) continue
    const data: EdgeData = { pos, edge: null, link: l, resolution: l.resolution, tone: null }
    es.push({ id: l.id, type: 'fm', source: l.src, target: l.dst, zIndex: Z_LINK, data, selectable: false, focusable: false })
  }
  return { nodes: out, edges: es }
}

// ---------------------------------------------------------------------------------------------------------------
// 视口

function paneSize() {
  const d = vf.dimensions.value
  return { w: d.width || 800, h: d.height || 600 }
}

/**
 * 换了功能线：整条线放得下、字又不太小（缩放不低于 FIT_MIN）就整个放进来；放不下就按 0.85 从根卡片的顶上开始，
 * 要看全貌点"适应屏幕"或缩小。工具栏在标题区里，画布上方不用让位，四边留一样的边。
 */
const FIT_MIN = 0.66
const EDGE = 28
function initialViewport(p: Positioned, v: LineView) {
  const { w, h } = paneSize()
  const side = EDGE
  const top = EDGE
  const fit = Math.min((w - 2 * side) / Math.max(p.width, 1), (h - top - side) / Math.max(p.height, 1))
  if (fit >= FIT_MIN) {
    const zoom = Math.min(fit, 1)
    void vf.setViewport({ x: (w - p.width * zoom) / 2, y: top, zoom })
    return
  }
  const zoom = 0.85
  const root = v.cards[0] ? p.boxes[v.cards[0].id] : null
  const rx = root ? root.x : 0
  const rw = root ? root.width : p.width
  const x = rw * zoom <= w - 2 * side ? w / 2 - (rx + rw / 2) * zoom : side - rx * zoom
  void vf.setViewport({ x, y: top - (root ? root.y : 0) * zoom, zoom })
}

/**
 * 改了展开状态：被操作的元素在屏幕上时，把它挪回原来的屏幕位置（横向按中线、纵向按上边）；收起类的操作里它已经
 * 不在屏幕上时，把排好后代表它的元素拉到画布上方三分之一处（规则见 viewport.ts 的 viewportAfterChange）。
 */
function keepAnchor(a: Anchor, p: Positioned) {
  const id = a.ids.find((i) => p.boxes[i])
  if (!id) return
  const vp = vf.getViewport()
  const { w, h } = paneSize()
  const r = viewportAfterChange(screenRect(a.oldBox, vp), { width: w, height: h }, vp, a.oldBox, p.boxes[id], a.reveal)
  const t = r.viewport
  if (t.x === vp.x && t.y === vp.y) return
  void vf.setViewport(t, r.mode === 'reveal' ? { duration: dur(300) } : undefined)
}

function focusBox(id: string) {
  const b = layout.value?.boxes[id]
  if (!b) return
  const { h } = paneSize()
  const zoom = Math.max(vf.getViewport().zoom, 0.8)
  // 比屏幕高的卡片：对准它的上部（标题栏和开头几个节点）
  const cy = b.height * zoom > h * 0.8 ? b.y + (h * 0.42) / zoom : b.y + b.height / 2
  void vf.setCenter(b.x + Math.min(b.width, (paneSize().w * 0.9) / zoom) / 2, cy, { zoom, duration: dur(450) })
}

/** 打开变更记录之前的视口：回到现在、还是原来那条线和展开状态时放回去（layoutReason 为 back） */
let presentViewport: ViewportTransform | null = null
watch(review, (now, before) => {
  if (now && !before) presentViewport = { ...vf.getViewport() }
})

watch(layoutTick, async () => {
  const v = view.value
  const p = layout.value
  const anchor = takeAnchor()
  const reason = layoutReason.value
  if (!v || !p) {
    nodes.value = []
    edges.value = []
    return
  }
  const flow = toFlow(v, p)
  nodes.value = flow.nodes
  edges.value = flow.edges
  await nextTick()
  if (reason === 'back' && presentViewport) void vf.setViewport(presentViewport)
  else if (reason !== 'state') initialViewport(p, v)
  else if (anchor) keepAnchor(anchor, p)
  if (reason !== 'state') presentViewport = review.value ? presentViewport : null
})

/** 回放走到的元素不在屏幕上（留 40 像素边）时平移过去，不改缩放；比屏幕大的卡片对准它的左上部分 */
function peekBox(id: string) {
  const b = layout.value?.boxes[id]
  if (!b) return
  const vp = vf.getViewport()
  const { w, h } = paneSize()
  const m = 40
  const bw = Math.min(b.width * vp.zoom, w - 2 * m)
  const bh = Math.min(b.height * vp.zoom, h - 2 * m)
  const x0 = b.x * vp.zoom + vp.x
  const y0 = b.y * vp.zoom + vp.y
  if (x0 >= m && y0 >= m && x0 + bw <= w - m && y0 + bh <= h - m) return
  void vf.setCenter(b.x + bw / vp.zoom / 2, b.y + bh / vp.zoom / 2, { zoom: vp.zoom, duration: dur(250) })
}

watch(canvasCommand, (cmd) => {
  if (!cmd) return
  if (cmd.type === 'fit') void vf.fitView({ padding: '24px', duration: dur(300), minZoom: 0.02 })
  else if (cmd.type === 'peek') peekBox(cmd.id)
  else focusBox(cmd.id)
})

// ---------------------------------------------------------------------------------------------------------------
// 缩放：线宽随缩放加粗（--sz），低于 MAP_ZOOM 进入地图模式

const zoom = computed(() => vf.viewport.value.zoom)
const mapMode = computed(() => zoom.value < MAP_ZOOM)
const strokeScale = computed(() => Math.max(1, 0.8 / zoom.value).toFixed(3))
const zoomPct = computed(() => Math.round(zoom.value * 100))

// ---------------------------------------------------------------------------------------------------------------
// 标题区

const header = computed(() => {
  const v = view.value
  const d = currentDisplay.value
  if (!v || !d) return null
  const line = d.line
  const root = v.cards[0]
  const tasks = v.cards.filter((c) => c.kind === 'task' && !c.goto)
  const gotos = v.cards.filter((c) => c.goto).length
  return {
    // 完整标题（手工改过的用改过的，否则引擎截断了时取 title_full）：标题区一行放不下由 CSS 加省略号，悬停看全文
    title: d.titleFull,
    kind: FN_KIND_NAME[line.kind],
    route: root?.route ?? null,
    name: root?.name ?? '',
    tasks: tasks.length,
    gotos,
  }
})

/** 看需求时的标题区：需求标题、编号、状态，对上的功能线（画布上有这条线时可以点过去） */
const reqHeader = computed(() => {
  const r = currentReq.value
  if (!r) return null
  const c = r.comparison
  const line = c?.line_title || r.line
  const sid = c?.line_sid ?? ''
  return {
    title: r.title || r.file,
    id: r.id,
    status: r.status || '没有状态',
    statusClass: statusClass(r.status),
    line,
    /** link：能点过去；missing：对照过，图上没有这条线；unknown：读不懂、没对照 */
    lineState: sid && displayBySid.value.has(sid) ? 'link' : c ? 'missing' : 'unknown',
  }
})

// Vue Flow 只在有人监听节点事件时才让节点接收鼠标（否则 pointer-events: none）。点击由各节点组件自己处理，
// 这里的监听器只为打开鼠标事件。
function onNodeClick() {}
const onMoveStart = () => (canvasMoving.value = true)
const onMoveEnd = () => (canvasMoving.value = false)

function miniColor(n: Node): string {
  const t = n.type ?? ''
  if (t === 'fm-card') return 'rgba(76,125,255,0.10)'
  if (t === 'fm-goto') return 'transparent'
  if (t === 'fm-cond') return C.decision
  if (t === 'fm-loop') return C.loop
  if (t === 'fm-catch') return C.catch
  if (t === 'fm-fold') return '#4a4d58'
  if (t === 'fm-end') return END_COLOR[(n.data as LeafData).node.endKind ?? 'ok']
  if (t === 'fm-start') return '#f2f3f5'
  return (n.data as LeafData)?.node?.trunk ? '#b9bdc8' : '#6d717e'
}
/** 小地图的描边：有变化的节点、折叠、卡片描成变化的颜色（读 marks，变化标记改了小地图跟着重画），别的照旧 */
function miniStroke(n: Node): string {
  if (reqMode.value) return miniReqStroke(n)
  if (tab.value === 'run') return miniRunStroke(n)
  const m = marks.value
  if (n.type === 'fm-card') {
    // 只按函数自己：新增绿、自己的节点改了琥珀；只是调用的函数变了的不描（标着圈的是调用它的那个节点）
    const status = m.cards.get(n.id)
    return status === 'added' ? DIFF_COLOR.added : status === 'changed' ? DIFF_COLOR.changed : C.task
  }
  if (n.type === 'fm-goto') return C.task
  if (n.type === 'fm-fold') return m.counts.get(n.id) ? DIFF_COLOR.changed : 'transparent'
  const mark = m.nodes.get(n.id)
  return mark ? DIFF_COLOR[mark] : 'transparent'
}
/** 运行路径页签的小地图描边：停点红；走过的节点、里面走过的折叠蓝；卡片照旧描任务蓝 */
function miniRunStroke(n: Node): string {
  const r = canvasRunMarks.value
  if (r.stops.has(n.id)) return C.failed
  if (n.type === 'fm-card' || n.type === 'fm-goto') return C.task
  if (n.type === 'fm-fold') return r.inside.has(n.id) ? C.task : 'transparent'
  return r.nodes.has(n.id) ? C.task : 'transparent'
}
/** 需求设计图的小地图描边：还没有实现的红，分类 / 走向不一致的琥珀，折叠着、里面有的红 */
function miniReqStroke(n: Node): string {
  const r = reqMarks.value
  if (n.type === 'fm-card' || n.type === 'fm-goto') return C.task
  if (n.type === 'fm-fold') return r.counts.get(n.id) ? C.failed : 'transparent'
  const m = r.nodes.get(n.id)
  return m ? (m.kinds[0] === 'missing' ? C.failed : C.decision) : 'transparent'
}
const setTab = (t: 'design' | 'run') => (tab.value = t)
</script>

<template>
  <section class="canvas-col">
    <header class="canvas-head">
      <div class="head-row">
        <div
          v-if="reqMode"
          class="head-title"
          data-tip="text"
          :data-tip-title="reqHeader?.title ?? ''"
          :data-tip-sub="reqHeader ? [reqHeader.id, reqHeader.line ? `功能线：${reqHeader.line}` : ''].filter(Boolean).join('  ') : ''"
        >
          <h1 data-clip>{{ reqHeader?.title ?? '需求' }}</h1>
          <div v-if="reqHeader" class="head-meta">
            <span class="meta-kind">需求</span>
            <code v-if="reqHeader.id" class="meta-fn">{{ reqHeader.id }}</code>
            <span class="rq-status" :class="reqHeader.statusClass">{{ reqHeader.status }}</span>
            <span class="meta-sep" />
            <button
              v-if="reqHeader.lineState === 'link'"
              class="head-link"
              data-tip-skip
              title="换到这条功能线，看实现的流程"
              @click="openReqLine"
            >
              功能线：{{ reqHeader.line }}
            </button>
            <span v-else-if="reqHeader.line" data-clip>
              功能线：{{ reqHeader.line }}<template v-if="reqHeader.lineState === 'missing'">（图上还没有）</template>
            </span>
            <span v-else>没有写功能线</span>
          </div>
        </div>
        <div
          v-else
          class="head-title"
          data-tip="text"
          :data-tip-title="header?.title ?? ''"
          :data-tip-sub="header ? [header.route, header.name].filter(Boolean).join('  ') : ''"
        >
          <h1 data-clip>{{ header?.title ?? '流程画布' }}</h1>
          <div v-if="header" class="head-meta" data-clip>
            <span class="meta-kind">{{ header.kind }}</span>
            <code v-if="header.route" class="meta-route">{{ header.route }}</code>
            <code class="meta-fn">{{ header.name }}</code>
            <span class="meta-sep" />
            <span>{{ header.tasks }} 个任务{{ header.gotos ? `，${header.gotos} 处回到前面的任务` : '' }}</span>
          </div>
        </div>
        <div class="head-side">
          <div v-if="!reqMode && !review" class="tabs" role="tablist">
            <button class="tab" :class="tab === 'design' ? 'on' : 'off-idle'" role="tab" :aria-selected="tab === 'design'" @click="setTab('design')">
              流程设计
            </button>
            <n-tooltip :delay="400">
              <template #trigger>
                <button class="tab" :class="tab === 'run' ? 'on' : 'off-idle'" role="tab" :aria-selected="tab === 'run'" @click="setTab('run')">
                  运行路径<span v-if="running" class="tab-running" role="img" aria-label="正在运行测试" />
                </button>
              </template>
              跑项目的测试，看每个测试实际走过的节点、每步耗时和没有测试走到的结局
            </n-tooltip>
          </div>
          <!-- 工具栏放在标题区里，不盖画布 -->
          <div class="toolbar" role="toolbar" aria-label="画布">
            <n-button size="small" secondary :disabled="!view" @click="expandAll">
              <template #icon><Icon name="expand" /></template>全部展开
            </n-button>
            <n-button size="small" secondary :disabled="!view" @click="resetDefault">
              <template #icon><Icon name="reset" /></template>恢复默认
            </n-button>
            <n-button size="small" secondary :disabled="!view" @click="fitCanvas">
              <template #icon><Icon name="fit" /></template>适应屏幕
            </n-button>
            <span class="zoom-read" :class="{ map: mapMode }">
              {{ zoomPct }}%<template v-if="mapMode"> · 只看结构</template>
            </span>
          </div>
        </div>
      </div>
    </header>
    <!-- 过期条说的是现在的代码和最新一次分析：看变更记录时不出现 -->
    <StaleBar v-if="!review" />
    <template v-if="!reqMode">
      <DiffBar v-if="tab === 'design'" />
      <RunBar v-else />
    </template>

    <div
      class="canvas-wrap"
      :class="{ 'fm-map': mapMode }"
      :style="{ ...metricVars, '--sz': strokeScale }"
    >
      <VueFlow
        :id="FLOW_ID"
        :nodes="nodes"
        :edges="edges"
        :node-types="nodeTypes"
        :edge-types="edgeTypes"
        :min-zoom="0.03"
        :max-zoom="2"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :elements-selectable="false"
        :elevate-nodes-on-select="false"
        :zoom-on-double-click="false"
        :pan-on-scroll="true"
        :zoom-on-pinch="true"
        :delete-key-code="null"
        :selection-key-code="null"
        :multi-selection-key-code="null"
        @pane-click="clearSelection"
        @node-click="onNodeClick"
        @move-start="onMoveStart"
        @move-end="onMoveEnd"
      >
        <Background :gap="22" :size="1.2" :pattern-color="C.dot" :bg-color="C.canvas" />
        <MiniMap
          pannable
          zoomable
          :width="180"
          :height="132"
          :node-color="miniColor"
          :node-stroke-color="miniStroke"
          :node-stroke-width="6"
          :node-border-radius="6"
          mask-color="rgba(10,11,13,0.62)"
          mask-stroke-color="#8eabff"
          :mask-stroke-width="2"
        />
        <Controls :show-interactive="false" :show-fit-view="false" position="bottom-left" />
        <MapLabels
          :cards="view?.cards ?? []"
          :layout="layout"
          :zoom="vf.viewport.value.zoom"
          :x="vf.viewport.value.x"
          :y="vf.viewport.value.y"
          :active="mapMode"
        />
      </VueFlow>
      <CanvasStatus />
    </div>
  </section>
</template>
