<script setup lang="ts">
// "运行路径"页签的画布上方一条（代替差异栏）：选中的测试是哪个、通过还是失败、用了多久；失败时说停在哪里——runfailure 的
// failureStop / failureBarText（裁决 S11e、S14）：异常从这条线抛出去了时是停点或者"异常是在它调用的 X 里抛出的"，能定位时
// 有"定位到停点"；以那种异常离开、没有记录到流程里的函数调用它的线上函数（画布上只经执行过的函数到得了时画红圈，否则只能
// "只定位到函数"）；异常是从别的线抛出去的（"切到那条线"）；都没有：流程正常走完，测试是在自己的断言里失败的。入口以这种
// 异常离开不止一次时，别的抛出处列成可以点的。"取消选择"；没选测试时提示去右边选一个；还在读、读出错、还没运行过各说各的。
// （运行是在更早的分析上记的、有对不上的地方的说明在右边的运行面板里。）
// 测试命令跑着、没选测试时（runstate 的 liveView）：说画布上是运行中的样子——亮的是到目前为止走到过的节点（所有已跑完的
// 测试合起来），跑完后可以按测试看路径和耗时；悬停说哪些节点运行中分不清；后面写这条线已到几个函数。
// 只跑一条线的测试时说清是哪条线的（runview 的 liveBarText）。
// 选了测试（路径读到了）时有"回放"：按记录一步步点亮这个测试走过的节点（replay.ts），放的时候可以暂停、继续、停止，写着
// 第几步；系统设置了"减少动态效果"时不放（悬停说明）。选中的运行只跑了别的线的测试时说这一点（不给这条线的路径）。
// 旧版后端（接口 404）：写"后端版本不支持，请重启 pystepmap serve"，不报错。状态和动作在 runstate.ts。
import { computed } from 'vue'
import { NButton } from 'naive-ui'
import {
  clearTest,
  failure,
  failureLine,
  failureReveal,
  gotoFailureLine,
  gotoRaise,
  liveLines,
  liveView,
  motion,
  overlay,
  pathsError,
  pathsLoading,
  pauseReplay,
  replay,
  resumeReplay,
  revealFailure,
  runDetail,
  runningScope,
  runSupport,
  runs,
  runsError,
  selectedElsewhere,
  selectedRun,
  selectedRunInfo,
  selectedTest,
  startReplay,
  stopReplay,
  testPaths,
} from '../runstate'
import { failureBarText, unknownIntro, type RaisePlace } from '../runfailure'
import { REPLAY_REDUCED_TIP, REPLAY_TIP } from '../replay'
import { LIVE_BAR_HINT, isFailedOutcome, liveBarText, secondsText, testName } from '../runview'
import { backend, displayBySid, lineSid, project, projectId } from '../state'
import Icon from './Icon.vue'

const row = computed(() => runDetail.value?.tests.find((t) => t.test === selectedTest.value) ?? null)
const outcome = computed(() => row.value?.outcome ?? testPaths.value?.paths.tests[0]?.outcome ?? '')
const failed = computed(() => isFailedOutcome(outcome.value))
const OUTCOME_TEXT: Record<string, string> = { passed: '通过', failed: '失败', error: '出错', skipped: '跳过' }
/** 结果的图标和颜色：通过绿、失败 / 出错红，跳过和没有结果记录（outcome 为空）中性灰 */
const tone = computed(() => (failed.value ? 'is-failed' : outcome.value === 'passed' ? 'is-passed' : 'is-other'))
const icon = computed(() => (failed.value ? 'failed' : outcome.value === 'passed' ? 'ok' : 'skipped'))
const name = computed(() => (selectedTest.value !== null ? testName(selectedTest.value) : null))
/** 这个测试执行到的图里的函数个数（不只这条线上的） */
const fnCount = computed(() => {
  const o = overlay.value
  return o ? o.functionsRun.size : 0
})
/** 失败的测试停在哪（另有的异常也写上；别的抛出处在后面列成可以点的） */
const stopText = computed(() =>
  failure.value && project.value ? failureBarText(failure.value, project.value, (sid) => displayBySid.value.get(sid)?.titleFull, true) : null,
)
/** 悬停时的全文（别的抛出处也写在句子里） */
const stopTip = computed(() =>
  failure.value && project.value ? failureBarText(failure.value, project.value, (sid) => displayBySid.value.get(sid)?.titleFull) : '',
)
/** 入口以这种异常离开不止一次时，红圈之外的抛出处（点了定位、不画红圈） */
const otherPlaces = computed<RaisePlace[]>(() => {
  const f = failure.value
  return f && (f.kind === 'stop' || f.kind === 'direct') ? (f.repeated?.elsewhere ?? []) : []
})
/** 运行中：画布上这条线已到几个函数 */
const liveLine = computed(() => liveLines.value[lineSid.value] ?? null)
/** 运行中的说明：只跑一条线的测试时说是哪条线的 */
const liveText = computed(() => liveBarText(runningScope.value, lineSid.value))
/** 选中的运行只跑了这条线的测试：异常是从别的线抛出去的时，换到那条线也看不到这个测试（那条线不给只跑了别的线的运行的路径） */
const scopedHere = computed(() => !!selectedRunInfo.value?.scope)
/** 这个测试的路径读到了，可以回放 */
const canReplay = computed(() => !!overlay.value && testPaths.value?.test === selectedTest.value)
const reduced = motion.reduced()
const replayHere = computed(() => {
  const r = replay.value
  return r && r.test === selectedTest.value && r.run === selectedRun.value ? r : null
})
/** 只跑了别的线的测试的那次运行：那条线的标题 */
const elsewhereTitle = computed(() => {
  const s = selectedElsewhere.value
  return s ? (displayBySid.value.get(s.line)?.titleFull ?? s.line_title) || s.line : ''
})
/** 列表里有运行，但都是只跑了别的线的测试 */
const noneForLine = computed(() => runs.value.length > 0 && selectedRun.value === null)

/** 以那种异常离开的线上函数画布上只经过没执行过的流程才到得了：不画红圈，只能定位到函数 */
const locateOnly = computed(() => {
  const f = failure.value
  return f?.kind === 'direct' && !f.reveal && f.locate ? f : null
})
</script>

<template>
  <div v-if="projectId" class="run-bar" role="region" aria-label="运行路径">
    <div class="rb-row">
      <template v-if="runSupport === 'unsupported'">
        <span class="rb-text rb-muted">后端版本不支持，请重启 pystepmap serve</span>
      </template>
      <template v-else-if="selectedTest !== null && name">
        <span class="rb-outcome" :class="tone">
          <Icon :name="icon" :size="13" :stroke="2.4" />{{ OUTCOME_TEXT[outcome] ?? (outcome || '没有结果记录') }}
        </span>
        <span class="rb-text" data-tip="text" :data-tip-title="selectedTest" data-tip-cut>{{ name.name }}</span>
        <span v-if="row" class="rb-meta">{{ secondsText(row.seconds) }}</span>
        <span v-if="pathsLoading" class="rb-meta">正在读这个测试的路径…</span>
        <span v-else-if="pathsError" class="rb-error">{{ pathsError }}</span>
        <span v-else-if="failed && failure?.kind === 'unknown'" class="rb-stop handled">
          {{ unknownIntro(failure) }}
          <button v-for="(r, i) in failure.raises" :key="i" class="bar-link" @click="gotoRaise(r.functionKey, r.nodeId)">「{{ r.title }}」（{{ r.exc }}）</button>
        </span>
        <span
          v-else-if="failed && stopText"
          class="rb-stop"
          :class="{ handled: failure?.kind !== 'stop' && !(failure?.kind === 'direct' && failure.reveal) }"
          data-tip="text"
          :data-tip-title="stopTip"
        >
          {{ stopText }}
        </span>
        <!-- 别的抛出处：放在会被截断的那句话外面，句子太长时也点得到 -->
        <span v-if="failed && !pathsLoading && !pathsError && otherPlaces.length" class="rb-places">
          <button v-for="(r, i) in otherPlaces" :key="i" class="bar-link" @click="gotoRaise(r.functionKey, r.nodeId)">{{ r.title }}</button>
        </span>
        <span v-else-if="overlay && lineSid" class="rb-meta">执行了图里 {{ fnCount }} 个函数</span>
        <span class="bar-gap" />
        <n-button v-if="failed && failureReveal" size="tiny" secondary @click="revealFailure">
          <template #icon><Icon name="failed" /></template>定位到停点
        </n-button>
        <n-button
          v-else-if="failed && locateOnly"
          size="tiny"
          secondary
          data-tip="text"
          data-tip-title="这个测试没有经过画布上通往它的调用，只是定位到函数，不画红圈"
          @click="gotoRaise(locateOnly.functionKey, locateOnly.nodeId)"
        >
          <template #icon><Icon name="goto" /></template>只定位到函数
        </n-button>
        <n-button v-else-if="failed && failureLine && !scopedHere" size="tiny" secondary @click="gotoFailureLine">
          <template #icon><Icon name="goto" /></template>切到那条线
        </n-button>
        <span
          v-else-if="failed && failureLine"
          class="rb-meta"
          data-tip="text"
          data-tip-title="这次运行只跑了这条线的测试：别的线上不给它的路径。要在那条线上看，运行一次那条线的测试或全部测试"
        >（只跑了这条线的测试）</span>
        <template v-if="replayHere">
          <span
            class="rb-meta rb-step"
            :title="replayHere.plan.truncated ? '有函数第一次执行经过的节点超过 500 个，记录里只留了前 499 个：回放到那里为止' : undefined"
            >回放 {{ replayHere.index + 1 }} / {{ replayHere.plan.steps.length }}{{ replayHere.plan.truncated ? '（有截断）' : '' }}</span
          >
          <n-button v-if="replayHere.playing" size="tiny" secondary @click="pauseReplay">
            <template #icon><Icon name="pause" /></template>暂停
          </n-button>
          <n-button v-else size="tiny" secondary @click="resumeReplay">
            <template #icon><Icon name="play" /></template>继续
          </n-button>
          <n-button size="tiny" secondary @click="stopReplay">
            <template #icon><Icon name="stop" /></template>停止
          </n-button>
        </template>
        <n-button
          v-else-if="canReplay"
          size="tiny"
          secondary
          data-tip="text"
          :data-tip-title="reduced ? REPLAY_REDUCED_TIP : REPLAY_TIP"
          @click="startReplay"
        >
          <template #icon><Icon name="replay" /></template>回放
        </n-button>
        <button class="bar-link" @click="clearTest">取消选择</button>
      </template>
      <template v-else-if="liveView">
        <span class="rb-live" aria-hidden="true" />
        <span class="rb-live-text" :title="LIVE_BAR_HINT">{{ liveText }}</span>
        <span v-if="liveLine" class="rb-meta">这条线已到 {{ liveLine.functions }} / {{ liveLine.total }} 个函数</span>
        <span v-else class="rb-meta">已跑完的测试还没有执行过这条线上的函数</span>
      </template>
      <template v-else-if="runSupport === 'unknown'">
        <span class="rb-text rb-muted">{{ backend === 'down' ? '连不上平台后端' : '正在读运行记录…' }}</span>
      </template>
      <template v-else-if="runSupport === 'error'">
        <span class="rb-error">读运行记录出错：{{ runsError }}</span>
      </template>
      <template v-else-if="selectedElsewhere">
        <span class="rb-text rb-muted">选中的运行 #{{ selectedRun }} 只跑了「{{ elsewhereTitle }}」的测试，不给这条线的路径：在右边选别的运行，或者运行这条线的测试</span>
      </template>
      <template v-else-if="noneForLine">
        <span class="rb-text rb-muted">最近的运行都只跑了别的线的测试：运行这条线的测试，或者在顶栏运行全部测试</span>
      </template>
      <template v-else-if="runs.length">
        <span class="rb-text rb-muted">在右边的测试列表里选一个测试，画布上标出它在这条线上走过的路径</span>
      </template>
      <template v-else>
        <span class="rb-text rb-muted">还没有运行过测试：先点顶栏的"运行全部测试"</span>
      </template>
    </div>
  </div>
</template>

<style>
.run-bar {
  flex: none;
  padding: 6px 18px;
  font-size: 12.5px;
  color: var(--text-soft);
  background: var(--panel);
  border-bottom: 1px solid var(--panel-line);
}
.rb-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
}
.rb-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.rb-muted {
  color: var(--muted);
}
.rb-meta {
  flex: none;
  color: var(--muted);
}
.rb-error {
  color: var(--failed);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rb-outcome {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
}
.rb-outcome.is-passed {
  color: var(--ok);
}
.rb-outcome.is-failed {
  color: var(--failed);
}
.rb-outcome.is-other {
  color: var(--skipped);
}
.rb-stop {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--failed);
}
.rb-stop.handled {
  color: var(--text-soft);
}
.rb-live {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--task);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--task) 25%, transparent);
}
.rb-live-text {
  min-width: 0;
  color: var(--text);
  line-height: 1.5;
  cursor: help;
}
.rb-places {
  flex: none;
  display: inline-flex;
  gap: 6px;
}
</style>
