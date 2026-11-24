<script setup lang="ts">
// "运行路径"页签的右侧栏（代替详情栏）：上方一块是点中的节点 / 卡片的详情（可收起，DetailPanel 的 embedded），下面是
// 运行面板——这条线的测试："运行这条线的测试（N 个）"（名单取自最近一次全量运行，GET /line-tests；挑不出来时说后端给的
// 原因、按钮灰着，旁边是"运行全部测试"），正在跑的是什么和已用时间、"取消"；测试命令一行小字（"修改"打开测试命令对话框，
// 全部测试在顶栏运行）；测试命令跑着时（后端给了进度）：跑完了几个、通过 / 失败几个（有总数时一根细进度条）、正在跑哪个、
// 最近跑完的几个（新的在前，失败的标红）；最近的运行（全量的、只跑一条线的各留 5 次，每次标着"全量"或「线的标题」）；
// 选中的那次的结果、核对结果（图和实际执行一致 / 有 N 处实际执行不在图上）、测试列表（默认只列走过这条线的，失败的在前，
// 可搜索）、这条线上没有测试走到的结局（可折叠，"在图上标出"）；选中的运行只跑了别的线的测试时只说这一点（不给这条线的
// 测试、没走到的结局）；排查用的"看测试输出"。旧版后端没有 line-tests 接口时不显示"运行这条线的测试"。
// 状态和动作都在 runstate.ts，文字在 runview.ts。
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NScrollbar, NSwitch } from 'naive-ui'
import { useElapsed } from '../elapsed'
import { whenText } from '../format'
import {
  cancelling,
  commandDialog,
  detailError,
  gotoFunction,
  gotoOutcome,
  lastFailure,
  lineRunSupport,
  lineTestsError,
  lineTestsNow,
  liveProgress,
  loadOutput,
  loadRunTab,
  onlyLine,
  output,
  outputError,
  retryLineTests,
  runDetail,
  runNote,
  running,
  runningScope,
  runs,
  runsError,
  runSupport,
  selectedElsewhere,
  selectedRun,
  selectedRunInfo,
  selectedTest,
  selectRun,
  selectTest,
  showUncovered,
  starting,
  startingScope,
  startLineRun,
  startRun,
  cancelRun,
  testCommand,
  testQuery,
  retryUncovered,
  runningUnknown,
  uncoveredError,
  uncoveredLists,
  uncoveredReady,
  visibleTests,
} from '../runstate'
import {
  RUN_STATUS_NAME,
  isFailedOutcome,
  liveBar,
  liveCountParts,
  liveRunningText,
  runFitsLine,
  runResultText,
  runScopeView,
  runningText,
  secondsText,
  testName,
  verifyView,
} from '../runview'
import { backend, clearSelection, displayBySid, lineSid, project, projectId, selected, selectLine } from '../state'
import { END_COLOR, END_NAME } from '../theme'
import DetailPanel from './DetailPanel.vue'
import Icon from './Icon.vue'

// 节点详情那一块：点了节点就展开（收起过的保持收起，直到换了选中的东西）
const detailOpen = ref(true)
watch(selected, (s, old) => {
  if (s && (!old || old.kind !== s.kind)) detailOpen.value = true
})

// 正在跑：两次查询之间自己走秒
const elapsed = useElapsed()
/** 正在跑的是什么（全部测试 / 这条线的 N 个测试 / 别的线的；别处开始的只说阶段） */
const runningWhat = computed(() => (running.value ? runningText(running.value.phase, runningScope.value, lineSid.value) : ''))

/** 功能线 sid → 界面上的标题（手工改过的用改过的） */
const titleOf = (sid: string) => displayBySid.value.get(sid)?.titleFull
const scopeOf = (r: (typeof runs.value)[number]) => runScopeView(r, lineSid.value, titleOf)

// 这条线的测试名单："运行这条线的测试（N 个）"
const lineTestsCount = computed(() => (lineTestsNow.value?.ok ? lineTestsNow.value.tests.length : null))
const lineRunLabel = computed(() => (lineTestsCount.value !== null ? `运行这条线的测试（${lineTestsCount.value} 个）` : '运行这条线的测试'))
const lineRunBusy = computed(() => starting.value && startingScope.value?.line === lineSid.value)
const canLineRun = computed(
  () => runSupport.value === 'ok' && lineTestsCount.value !== null && !running.value && !starting.value && backend.value !== 'down',
)
const canFullRun = computed(() => runSupport.value === 'ok' && !running.value && !starting.value && backend.value !== 'down')
/** 上次给这条线的结果的运行（全量的或只跑了这条线的） */
const lastForLine = computed(() => runs.value.find((r) => runFitsLine(r, lineSid.value)) ?? null)
const elsewhereTitle = computed(() => {
  const s = selectedElsewhere.value
  return s ? titleOf(s.line) || s.line_title || s.line : ''
})
const lineKnown = computed(() => !!lineSid.value && displayBySid.value.has(lineSid.value))

// 测试命令跑着：进度（后端每秒最多读一次记录文件；这里跟着运行列表每隔一会儿查一次）
const live = computed(() => liveProgress.value)
const liveCounts = computed(() => (live.value ? liveCountParts(live.value) : []))
const bar = computed(() => (live.value ? liveBar(live.value) : null))
const runningNow = computed(() => (live.value ? liveRunningText(live.value) : null))
const pct = (x: number) => `${(Math.max(0, Math.min(1, x)) * 100).toFixed(2)}%`
const outcomeIcon = (o: string) => (isFailedOutcome(o) ? 'failed' : o === 'passed' ? 'ok' : 'skipped')
const outcomeTone = (o: string) => (isFailedOutcome(o) ? 'is-failed' : o === 'passed' ? 'is-passed' : 'is-other')

const run = computed(() => selectedRunInfo.value)
/** 选中的运行只跑了这条线的测试：几个、名单取自哪次全量运行 */
const runScopedHere = computed(() => (run.value?.scope && !selectedElsewhere.value ? run.value.scope : null))
const scopedNote = computed(() => {
  const s = runScopedHere.value
  const r = run.value
  if (!s || !r) return ''
  const parts = [r.summary.tests > 0 && r.summary.tests !== s.tests ? `共 ${r.summary.tests} 个用例` : '', s.base_run !== null ? `名单取自全量运行 #${s.base_run}` : '']
  const tail = parts.filter(Boolean).join('；')
  return `只跑了这条线的 ${s.tests} 个测试${tail ? `（${tail}）` : ''}`
})
/** 只跑一条线时：名单按测试数，进度按用例数（参数化的测试每组参数一个），两个数不一样时说一句 */
const caseNote = computed(() => {
  const sc = runningScope.value
  const p = live.value
  if (!sc || sc.line === null || !p || p.collected === null || p.collected === sc.tests) return ''
  return `${sc.tests} 个测试共 ${p.collected} 个用例：上面按用例数（参数化的测试每组参数算一个）`
})
const verify = computed(() => (run.value ? verifyView(run.value, project.value) : null))
const showMismatches = ref(false)
watch(selectedRun, () => (showMismatches.value = false))

const showUncoveredList = ref(true)
const uncoveredCount = computed(() => uncoveredLists.value.certain.length)
const uncertainCount = computed(() => uncoveredLists.value.uncertain.length)

const showOutput = ref(false)
watch(projectId, () => {
  showOutput.value = false
})
function toggleOutput() {
  showOutput.value = !showOutput.value
  if (showOutput.value) void loadOutput()
}

const STATUS_CLASS: Record<string, string> = { ok: 's-ok', tests_failed: 's-failed', error: 's-error', timeout: 's-error', cancelled: 's-muted' }
const statusName = (s: string) => RUN_STATUS_NAME[s] ?? s

function pickTest(test: string) {
  void selectTest(selectedTest.value === test ? null : test)
}

/** 运行中是否展开之前那次运行的结果（默认收起；每次开始运行重新收起） */
const showOldRun = ref(false)
watch(
  () => !!running.value,
  (now) => {
    if (now) showOldRun.value = false
  },
)
</script>

<template>
  <aside class="detail run-side" aria-label="运行路径">
    <n-scrollbar>
      <div class="detail-inner">
        <!-- 点中的节点 / 卡片 -->
        <section v-if="selected" class="rp-detail">
          <div class="rp-detail-head">
            <button class="rp-fold" :aria-expanded="detailOpen" @click="detailOpen = !detailOpen">
              <Icon :name="detailOpen ? 'chevron-up' : 'chevron-down'" :size="12" />{{ detailOpen ? '收起详情' : '展开详情' }}
            </button>
            <span class="bar-gap" />
            <button class="d-link" @click="clearSelection">不看了</button>
          </div>
          <DetailPanel v-if="detailOpen" embedded />
        </section>

        <p v-if="!projectId" class="d-hint">先选一个项目。</p>
        <p v-else-if="runSupport === 'unsupported'" class="rp-unsupported">后端版本不支持，请重启 pystepmap serve</p>
        <p v-else-if="runSupport === 'unknown'" class="d-hint">{{ backend === 'down' ? '连不上平台后端' : '正在读运行记录…' }}</p>
        <template v-else>
          <!-- 这条线的测试：只跑走过这条线的测试 -->
          <section class="rp-top">
            <h3 class="rp-top-title">这条线的测试</h3>
            <template v-if="!lineKnown">
              <p class="d-hint">先在左边选一条功能线。</p>
            </template>
            <template v-else-if="lineRunSupport === 'unsupported'">
              <p class="rp-grey">
                这个版本的后端不能只跑一条线的测试（重启 pystepmap serve 后可用）。<button
                  v-if="!running"
                  class="d-link rp-inline"
                  :disabled="!canFullRun"
                  @click="startRun"
                >运行全部测试</button>
              </p>
            </template>
            <template v-else>
              <div class="rp-actions">
                <n-button
                  type="primary"
                  size="small"
                  :loading="lineRunBusy"
                  :disabled="!canLineRun"
                  data-tip="text"
                  data-tip-title="只跑走过这条线的测试（名单取自最近一次全量运行；之后新写的测试不在名单里，要先运行一次全部测试）"
                  @click="startLineRun"
                >
                  <template #icon><Icon name="play" /></template>{{ lineRunLabel }}
                </n-button>
              </div>
              <p v-if="lineTestsError" class="rp-error">
                没能取到这条线的测试：{{ lineTestsError }}
                <button class="d-link" @click="retryLineTests">重试</button>
              </p>
              <p v-else-if="lineTestsNow && !lineTestsNow.ok" class="rp-why">
                {{ lineTestsNow.reason }}<button v-if="!running" class="d-link rp-inline" :disabled="!canFullRun" @click="startRun">运行全部测试</button>
              </p>
              <p v-else-if="!lineTestsNow && runSupport === 'ok'" class="d-hint">正在看哪些测试走过这条线…</p>
            </template>
            <div v-if="running" class="rp-actions">
              <span v-if="runningUnknown" class="rp-running unknown">{{ runningWhat }}（{{ elapsed }} 时）——现在查不到状态，正在重试…</span>
              <span v-else class="rp-running">{{ runningWhat }}… 已用 {{ elapsed }}</span>
              <n-button size="small" secondary :loading="cancelling" :disabled="cancelling" @click="cancelRun">取消</n-button>
            </div>
            <p v-else-if="lastForLine" class="rp-last">
              上次：{{ whenText(lastForLine.finished_at) }}，{{ lastForLine.scope ? '只跑这条线' : '全量' }}，{{ runResultText(lastForLine) }}
            </p>
            <div class="rp-cmd">
              <span class="rp-cmd-label">测试命令</span>
              <code v-if="testCommand?.command" class="rp-cmd-text" data-tip="text" :data-tip-title="testCommand.command">{{ testCommand.command }}</code>
              <span v-else class="rp-cmd-none">还没有填</span>
              <button class="d-link" :disabled="runSupport !== 'ok'" @click="commandDialog = true">{{ testCommand?.command ? '修改' : '填写' }}</button>
            </div>
            <!-- 测试命令跑着：进度 -->
            <div v-if="live" class="rp-live" aria-live="polite">
              <p v-if="live.done === 0" class="rp-live-wait">
                测试已启动，等第一个测试跑完…<span v-if="live.collected !== null" class="d-dim">（收集到 {{ live.collected }} 个）</span>
              </p>
              <template v-else>
                <p class="rp-live-counts">
                  <template v-for="(part, i) in liveCounts" :key="i">
                    <span v-if="i" class="rp-live-sep"> · </span><span :class="{ bad: part.bad }">{{ part.text }}</span>
                  </template>
                </p>
                <div v-if="bar" class="rp-live-bar" role="progressbar" :aria-valuenow="live.done" aria-valuemin="0" :aria-valuemax="live.collected ?? undefined">
                  <span class="ok" :style="{ width: pct(bar.ok) }" /><span class="bad" :style="{ width: pct(bar.bad) }" />
                </div>
              </template>
              <p v-if="runningNow" class="rp-live-now" data-tip="text" :data-tip-title="live.running.join('、')" data-tip-cut>{{ runningNow }}</p>
              <p v-if="caseNote" class="rp-live-note">{{ caseNote }}</p>
              <template v-if="live.recent.length">
                <h4 class="rp-sub">最近跑完的</h4>
                <ul class="rp-tests rp-live-recent">
                  <li v-for="(t, i) in live.recent" :key="i">
                    <span class="rp-test rp-live-test" :class="{ failed: isFailedOutcome(t.outcome) }" data-tip="text" :data-tip-title="t.test" data-tip-cut>
                      <span class="rp-test-ico" :class="outcomeTone(t.outcome)"><Icon :name="outcomeIcon(t.outcome)" :size="12" :stroke="2.6" /></span>
                      <span class="rp-test-name">{{ testName(t.test).name }}</span>
                      <span class="rp-test-time">{{ secondsText(t.seconds) }}</span>
                    </span>
                  </li>
                </ul>
              </template>
            </div>
            <p v-if="lastFailure" class="rp-error">
              没有跑成（{{ whenText(lastFailure.at) }}）：{{ lastFailure.reason || lastFailure.title }}
            </p>
            <p v-if="runSupport === 'error' || runsError" class="rp-error">
              读运行记录出错：{{ runsError }}
              <button class="d-link" @click="loadRunTab">重试</button>
            </p>
          </section>

          <!-- 最近的运行（全量的、只跑一条线的） -->
          <section class="d-sec">
            <h3>最近的运行 <span class="d-n">{{ runs.length }}</span></h3>
            <ul v-if="runs.length" class="rp-runs">
              <li v-for="r in runs" :key="r.id">
                <button
                  class="rp-run"
                  :class="{ on: r.id === selectedRun, elsewhere: scopeOf(r).kind === 'other' }"
                  :aria-pressed="r.id === selectedRun"
                  @click="selectRun(r.id)"
                >
                  <span class="rp-status" :class="STATUS_CLASS[r.status] ?? 's-muted'">{{ statusName(r.status) }}</span>
                  <span
                    class="rp-scope"
                    :class="`k-${scopeOf(r).kind}`"
                    :title="r.scope ? `只跑了这条线的 ${r.scope.tests} 个测试` : '全部测试'"
                    >{{ scopeOf(r).label }}</span
                  >
                  <span class="rp-run-when">#{{ r.id }} · {{ whenText(r.finished_at) }}</span>
                  <span class="rp-run-result">{{ runResultText(r) }}</span>
                </button>
              </li>
            </ul>
            <p v-else class="d-hint">还没有运行过。先点顶栏的"运行全部测试"：跑过一次全部测试，平台才知道哪些测试走过这条线。</p>
          </section>

          <!-- 选中的运行只跑了别的线的测试：不给这条线的测试、没走到的结局 -->
          <section v-if="run && selectedElsewhere" class="d-sec">
            <h3>运行 #{{ run.id }} <span class="d-n">{{ whenText(run.finished_at) }}</span></h3>
            <p class="rp-result">{{ runResultText(run) }}</p>
            <p class="rp-elsewhere">
              这次运行只跑了「{{ elsewhereTitle }}」的测试（{{ selectedElsewhere.tests }} 个），不代表这条线：这里不列它的测试、也不算这条线上哪些结局没走到。
            </p>
            <div class="rp-actions">
              <n-button v-if="displayBySid.has(selectedElsewhere.line)" size="small" secondary @click="selectLine(selectedElsewhere.line)">
                <template #icon><Icon name="goto" /></template>切到「{{ elsewhereTitle }}」
              </n-button>
            </div>
          </section>

          <!-- 运行中：之前那次运行的结果、测试列表先收起（和正在跑的这次混在一起会看错），可以点开 -->
          <p v-if="run && !selectedElsewhere && running && !showOldRun" class="rp-old-folded">
            运行 #{{ run.id }} 的结果收起了（现在显示的是正在跑的这次）
            <button class="d-link" @click="showOldRun = true">展开</button>
          </p>
          <template v-if="run && !selectedElsewhere && (!running || showOldRun)">
            <!-- 选中的运行：结果、核对 -->
            <section class="d-sec">
              <h3>运行 #{{ run.id }} <span class="d-n">{{ whenText(run.finished_at) }}</span></h3>
              <p class="rp-result">{{ runResultText(run) }}</p>
              <p v-if="runScopedHere" class="rp-grey">{{ scopedNote }}</p>
              <p v-if="run.summary.note" class="rp-note">{{ run.summary.note }}</p>
              <div v-if="verify && verify.kind === 'ok'" class="rp-verify v-ok"><Icon name="ok" :size="13" :stroke="2.4" />{{ verify.text }}</div>
              <div v-else-if="verify && verify.kind === 'bad'" class="rp-verify v-bad">
                <button class="rp-verify-btn" :aria-expanded="showMismatches" @click="showMismatches = !showMismatches">
                  <Icon name="exception" :size="14" :stroke="2.4" /><b>{{ verify.text }}</b>
                  <Icon :name="showMismatches ? 'chevron-up' : 'chevron-down'" :size="12" />
                </button>
                <p v-if="showMismatches && verify.more" class="rp-more">下面只列出前 30 条</p>
                <ul v-if="showMismatches" class="rp-mismatches">
                  <li v-for="(m, i) in verify.items" :key="i">
                    <button class="rp-item" :disabled="!m.functionKey" @click="m.functionKey && gotoFunction(m.functionKey)">
                      <span class="rp-item-main">{{ m.kindText }}：{{ m.function }}</span>
                      <span class="rp-item-sub">{{ m.where }} · {{ m.count }} 次{{ m.detail ? ` · ${m.detail}` : '' }}</span>
                    </button>
                  </li>
                </ul>
              </div>
              <p v-if="verify?.note" class="rp-grey">{{ verify.note }}</p>
              <p v-if="runNote" class="rp-grey">{{ runNote }}</p>
            </section>

            <!-- 测试列表 -->
            <section class="d-sec">
              <div class="rp-sec-head">
                <h3>测试 <span class="d-n">{{ visibleTests.length }}</span></h3>
                <label
                  class="rp-switch"
                  title="走过这条线：这个测试执行过这条线的入口函数（接口函数、任务或工作流的函数，以及它派发出去的任务）。只碰到线上深处共用的函数（登录校验、数据库会话之类）的测试不算"
                >
                  <n-switch v-model:value="onlyLine" size="small" :disabled="runDetail !== null && !runDetail.line_known" />
                  只看走过这条线的
                </label>
              </div>
              <n-input v-model:value="testQuery" size="small" clearable placeholder="搜索测试" class="rp-search">
                <template #prefix><Icon name="search" :size="12" /></template>
              </n-input>
              <p v-if="detailError" class="rp-error">读测试列表出错：{{ detailError }}</p>
              <p v-else-if="!runDetail" class="d-hint">正在读测试列表…</p>
              <template v-else>
                <p v-if="!runDetail.line_known && runDetail.tests.length" class="rp-grey">这次运行不知道每个测试走过哪些功能线（旧的运行），列出全部测试</p>
                <ul v-if="visibleTests.length" class="rp-tests">
                  <li v-for="t in visibleTests" :key="t.test">
                    <button
                      class="rp-test"
                      :class="{ on: t.test === selectedTest }"
                      :aria-pressed="t.test === selectedTest"
                      data-tip="text"
                      :data-tip-title="t.test || testName(t.test).name"
                      data-tip-cut
                      @click="pickTest(t.test)"
                    >
                      <span
                        class="rp-test-ico"
                        :class="isFailedOutcome(t.outcome) ? 'is-failed' : t.outcome === 'passed' ? 'is-passed' : 'is-other'"
                        :title="t.outcome ? undefined : '没有结果记录'"
                      >
                        <Icon :name="isFailedOutcome(t.outcome) ? 'failed' : t.outcome === 'passed' ? 'ok' : 'skipped'" :size="12" :stroke="2.6" />
                      </span>
                      <span class="rp-test-name">{{ testName(t.test).name }}</span>
                      <span class="rp-test-time">{{ secondsText(t.seconds) }}</span>
                    </button>
                  </li>
                </ul>
                <p v-else-if="runDetail.tests.length === 0" class="d-hint">这次运行没有测试记录（没跑完，或者没收到记录）。</p>
                <p v-else-if="testQuery.trim()" class="d-hint">没有匹配的测试。</p>
                <p v-else class="d-hint">没有测试走过这条线。关掉"只看走过这条线的"可以看全部测试。</p>
              </template>
            </section>

            <!-- 没走到的结局 -->
            <section v-if="run.paths_size !== null" class="d-sec">
              <div class="rp-sec-head">
                <button class="rp-fold" :aria-expanded="showUncoveredList" @click="showUncoveredList = !showUncoveredList">
                  <Icon :name="showUncoveredList ? 'chevron-up' : 'chevron-down'" :size="12" />
                  <template v-if="!uncoveredReady">没走到的结局</template>
                  <template v-else-if="uncoveredCount">这条线有 {{ uncoveredCount }} 个结局没有测试走到</template>
                  <template v-else-if="uncertainCount">这条线有 {{ uncertainCount }} 个结局对不上（代码改过，重新运行测试才知道），其余都有测试走到</template>
                  <template v-else>这条线的结局都有测试走到</template>
                </button>
                <label class="rp-switch" title="没选测试时，在画布上用红色虚线框标出没走到的结局">
                  <n-switch v-model:value="showUncovered" size="small" />在图上标出
                </label>
              </div>
              <p v-if="uncoveredError" class="rp-error">
                没能取到未覆盖的结局：{{ uncoveredError }}
                <button class="d-link" @click="retryUncovered">重试</button>
              </p>
              <p v-else-if="showUncoveredList && !uncoveredReady" class="d-hint">正在取没走到的结局…</p>
              <template v-if="showUncoveredList && uncoveredReady">
                <p v-if="showUncovered && selectedTest !== null" class="rp-grey">选着测试时画布上标的是这个测试走过的路；取消选择后标出没走到的结局。</p>
                <ul v-if="uncoveredLists.certain.length" class="rp-outcomes">
                  <li v-for="(o, i) in uncoveredLists.certain" :key="`c${i}`">
                    <button class="rp-item" @click="gotoOutcome(o)">
                      <span class="rp-item-main">
                        <span class="dot" :style="{ background: END_COLOR[o.end_kind as keyof typeof END_COLOR] ?? END_COLOR.ok }" />{{ o.title }}
                        <span class="d-dim">{{ END_NAME[o.end_kind as keyof typeof END_NAME] ?? '' }}</span>
                      </span>
                      <span class="rp-item-sub">{{ o.file }}{{ o.line ? `:${o.line}` : '（执行到函数末尾）' }}</span>
                    </button>
                  </li>
                </ul>
                <template v-if="uncoveredLists.uncertain.length">
                  <h4 class="rp-sub">对不上（代码改过，重新运行测试才知道）<span class="d-n">{{ uncoveredLists.uncertain.length }}</span></h4>
                  <ul class="rp-outcomes uncertain">
                    <li v-for="(o, i) in uncoveredLists.uncertain" :key="`u${i}`">
                      <button class="rp-item" @click="gotoOutcome(o)">
                        <span class="rp-item-main">{{ o.title }}</span>
                        <span class="rp-item-sub">{{ o.file }}{{ o.line ? `:${o.line}` : '（执行到函数末尾）' }}</span>
                      </button>
                    </li>
                  </ul>
                </template>
              </template>
            </section>
          </template>

          <!-- 排查用 -->
          <section class="d-sec">
            <button class="rp-fold" :aria-expanded="showOutput" @click="toggleOutput">
              <Icon :name="showOutput ? 'chevron-up' : 'chevron-down'" :size="12" />看测试输出
            </button>
            <template v-if="showOutput">
              <p class="rp-grey">最近一次运行输出的末尾。只留在服务的内存里，重启 pystepmap serve 后就没有了。</p>
              <p v-if="outputError" class="rp-error">{{ outputError }}</p>
              <pre v-else-if="output && output.length" class="rp-output">{{ output.join('\n') }}</pre>
              <p v-else-if="output" class="d-hint">没有输出（这次服务启动以来还没有运行过）。</p>
              <button class="d-link" @click="loadOutput">刷新</button>
            </template>
          </section>
        </template>
      </div>
    </n-scrollbar>
  </aside>
</template>

<style>
.rp-detail {
  margin: -6px 0 14px;
  padding: 6px 10px 4px;
  border: 1px solid var(--panel-line);
  border-radius: 8px;
  background: #18191d;
}
.rp-detail-head {
  display: flex;
  align-items: center;
}
.rp-fold {
  all: unset;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-soft);
  cursor: pointer;
}
.rp-fold:focus-visible,
.rp-run:focus-visible,
.rp-test:focus-visible,
.rp-item:focus-visible,
.rp-verify-btn:focus-visible {
  outline: 2px solid var(--task-soft);
  outline-offset: 1px;
}
.rp-unsupported {
  margin: 8px 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: #1a1b20;
  color: var(--muted);
  font-size: 13px;
}
.rp-top {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rp-top-title {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}
/* 原因后面另起一行的"运行全部测试" */
.rp-inline {
  display: block;
  width: fit-content;
  margin: 3px 0 0;
}
.rp-inline:disabled {
  color: var(--faint);
  cursor: default;
  text-decoration: none;
}
.rp-live-note {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--faint);
}
.rp-why {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-soft);
}
.rp-cmd {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
}
.rp-cmd-label {
  flex: none;
  color: var(--faint);
}
.rp-cmd-text {
  min-width: 0;
  flex: 1;
  font: 11.5px/1.6 var(--mono);
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rp-cmd-none {
  flex: 1;
  color: var(--faint);
}
/* 运行的范围：全量（灰）/ 只跑了这条线（蓝）/ 只跑了别的线（灰，整行淡一点） */
.rp-scope {
  flex: none;
  max-width: 9em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  color: var(--muted);
}
.rp-scope.k-here {
  color: var(--task-soft);
}
.rp-run.elsewhere:not(.on) .rp-run-result,
.rp-run.elsewhere:not(.on) .rp-run-when {
  color: var(--faint);
}
.rp-elsewhere {
  margin: 4px 0 8px;
  padding: 7px 9px;
  border-radius: 6px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-soft);
  background: #1a1b20;
  border: 1px solid var(--panel-line);
}
.rp-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.rp-running {
  font-size: 12.5px;
  color: var(--task-soft);
}
.rp-running.unknown {
  color: var(--muted);
}
.rp-live {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--task) 35%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--task) 6%, transparent);
}
.rp-live-wait,
.rp-live-counts,
.rp-live-now {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-soft);
}
.rp-live-counts > span {
  white-space: nowrap;
}
.rp-live-counts .bad {
  color: var(--failed);
  font-weight: 600;
}
.rp-live-sep {
  color: var(--faint);
}
.rp-live-bar {
  display: flex;
  height: 4px;
  margin: 2px 0;
  border-radius: 2px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.08);
}
.rp-live-bar .ok {
  background: var(--task);
}
.rp-live-bar .bad {
  background: var(--failed);
}
.rp-live-now {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--task-soft);
}
.rp-live .rp-sub {
  margin: 6px 0 2px;
}
.rp-live .rp-live-test {
  cursor: default;
}
.rp-live .rp-live-test:hover {
  background: none;
}
.rp-live-test.failed .rp-test-name {
  color: var(--failed);
}
.rp-more {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--muted);
}
.rp-last {
  font-size: 12px;
  color: var(--muted);
}
.rp-error {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--failed);
}
.rp-grey {
  margin: 6px 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--muted);
}
.rp-note {
  margin: 4px 0 8px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-soft);
}
.rp-result {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--text);
}
.rp-runs,
.rp-tests,
.rp-outcomes,
.rp-mismatches {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rp-run,
.rp-test,
.rp-item {
  all: unset;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 4px 6px;
  border-radius: 5px;
  font-size: 12.5px;
  color: var(--text-soft);
  cursor: pointer;
}
.rp-run:hover,
.rp-test:hover,
.rp-item:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.05);
}
.rp-run.on,
.rp-test.on {
  background: rgba(76, 125, 255, 0.16);
  color: var(--text);
}
.rp-item:disabled {
  cursor: default;
}
.rp-status {
  flex: none;
  font-size: 10.5px;
  line-height: 16px;
  padding: 0 5px;
  border-radius: 3px;
  border: 1px solid;
}
.rp-status.s-ok {
  color: var(--ok);
}
.rp-status.s-failed,
.rp-status.s-error {
  color: var(--failed);
}
.rp-status.s-muted {
  color: var(--muted);
}
.rp-run-when {
  flex: none;
  color: var(--muted);
  font-size: 12px;
}
.rp-run-result {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rp-verify {
  margin: 6px 0 4px;
  font-size: 13px;
}
.rp-verify.v-ok {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--ok);
}
.rp-verify.v-bad {
  padding: 6px 8px;
  border-radius: 6px;
  color: var(--failed);
  background: color-mix(in srgb, var(--failed) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--failed) 45%, transparent);
}
.rp-verify-btn {
  all: unset;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.rp-mismatches {
  margin-top: 6px;
}
.rp-item {
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
}
.rp-item-main {
  color: var(--text-soft);
  overflow-wrap: anywhere;
}
.rp-item-sub {
  font: 11.5px/1.5 var(--mono);
  color: var(--muted);
  overflow-wrap: anywhere;
}
.rp-sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.rp-sec-head h3 {
  margin: 0;
}
.rp-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  white-space: nowrap;
}
.rp-search {
  margin-bottom: 8px;
}
.rp-test-ico {
  flex: none;
  display: flex;
}
.rp-test-ico.is-passed {
  color: var(--ok);
}
.rp-test-ico.is-failed {
  color: var(--failed);
}
.rp-test-ico.is-other {
  color: var(--skipped);
}
.rp-test-name {
  min-width: 0;
  flex: 1;
  font: 12px/1.5 var(--mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rp-test-time {
  flex: none;
  font-size: 11.5px;
  color: var(--muted);
}
.rp-sub {
  margin: 12px 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}
.rp-outcomes.uncertain .rp-item-main {
  color: var(--muted);
}
.rp-output {
  max-height: 260px;
  overflow: auto;
  margin: 6px 0;
  padding: 7px 9px;
  font: 11.5px/1.5 var(--mono);
  color: #c9ccd6;
  background: #0f1013;
  border: 1px solid var(--panel-line);
  border-radius: 6px;
  white-space: pre;
}
.rp-old-folded {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--faint);
}
</style>
