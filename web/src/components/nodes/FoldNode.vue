<script setup lang="ts">
// 折叠占位：第一行摘要（几个节点、以什么结局收尾、是否汇回），第二行入口节点的标题，里面有推测 / 目标不明的调用时
// 第二行右边带同样的小标记（尺寸照 foldSize）。点一下在原地展开。
// 左边的竖条按里面的结局种类分段上色；地图模式里整块按最严重的结局上色（foldTint）。鼠标停留时列出里面的结局和派发的任务。
// 里面有和对比基准比的变化时右上角写几处（state.ts 的 marks）；占位自己 overflow: hidden，角标放在外面一层。
// 运行路径页签：里面有选中的测试走过的节点时左上角一个蓝点，没有时变暗；停点、没走到的结局在里面时红圈 / 虚线框
// 画在占位上（runstate.ts 的 canvasRunMarks）。
// 需求的设计图：里面有和实现对不上的节点时右上角一个红色数字（state.ts 的 reqMarks），点开看。
import { computed } from 'vue'
import { NTooltip } from 'naive-ui'
import { foldChips, foldText } from '../../graph'
import { canvasMoving, foldById, hoverFold, marks, openFold, reqMarks, selection } from '../../state'
import { END_COLOR, END_NAME, foldTint } from '../../theme'
import { canvasRunMarks } from '../../runstate'
import { runNodeClasses, uncoveredCountText } from '../../runview'
import Icon from '../Icon.vue'
import type { FoldData } from './types'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ data: FoldData }>()
const f = computed(() => props.data.fold)
const t = computed(() => foldText(f.value))
const chips = computed(() => foldChips(f.value))
const CHIP_CLASS: Record<string, string> = { 推测: 'chip-guessed', 目标不明: 'chip-unresolved' }
const bar = computed(() => f.value.summary.endKinds.map((k) => END_COLOR[k]))
const tint = computed(() => foldTint(f.value.summary.endKinds))
// 外层"收起"按钮悬停时，里面已折叠的子区域也标出来
const inHoverFold = computed(
  () => hoverFold.value !== null && (foldById.value.get(hoverFold.value)?.members.includes(f.value.members[0]) ?? false),
)
const inside = computed(() => marks.value.counts.get(f.value.id) ?? 0)
const reqInside = computed(() => reqMarks.value.counts.get(f.value.id) ?? 0)
const runCls = computed(() => runNodeClasses(canvasRunMarks.value, f.value.id, 'fold'))
const uncInside = computed(() => canvasRunMarks.value.uncoveredInside.get(f.value.id) ?? null)
const kindName = computed(() =>
  f.value.kind === 'catch' ? '异常处理' : f.value.kind === 'exit' ? '提前结束的分支' : '汇回主线的分支',
)
</script>

<template>
  <div class="fm-fold-box" :class="[runCls, { 'has-change': inside > 0 }]" :style="{ width: `${data.width}px`, height: `${data.height}px` }">
    <!-- 折叠占位自己的说明（总是有）：第一行是第二行的完整文字（入口标题或捕获的异常，引擎截断过的也是全文），被截断时在这里看全 -->
    <n-tooltip :delay="300" placement="right" :disabled="canvasMoving" :style="{ maxWidth: '420px' }">
      <template #trigger>
        <div
          class="fm-leaf fm-fold"
          :class="[`fold-${f.kind}`, { trunk: f.trunk, selected: selection?.id === f.id, 'fold-hover': inHoverFold }]"
          :style="{ width: `${data.width}px`, height: `${data.height}px`, '--fold-tint': tint }"
          role="button"
          :aria-label="`展开：${t.headline}`"
          tabindex="0"
          @click="openFold(f)"
          @keydown.enter.self="openFold(f)"
        >
          <span class="ends-bar">
            <span v-for="(c, i) in bar" :key="i" :style="{ background: c }" />
          </span>
          <span class="ico"><Icon name="plus" :size="12" :stroke="2.6" /></span>
          <div class="txt">
            <div class="headline">{{ t.headline }}</div>
            <div class="detail">
              <span class="dtext">{{ t.detail }}</span>
              <span v-if="chips.length" class="chips">
                <span v-for="c in chips" :key="c" class="chip" :class="CHIP_CLASS[c]">{{ c }}</span>
              </span>
            </div>
          </div>
        </div>
      </template>
      <div class="fold-tip">
        <b>{{ kindName }}{{ f.kind !== 'catch' ? `「${f.labelFull}」` : `（${f.labelFull}）` }}：{{ f.titleFull }}</b>
        <div>{{ t.headline }}</div>
        <div v-if="f.summary.first !== f.titleFull">入口：{{ f.summary.first }}</div>
        <div v-if="f.summary.ends.length">
          结局：
          <span v-for="(e, i) in f.summary.ends" :key="i">
            <span :style="{ color: e.kind ? END_COLOR[e.kind] : undefined }">{{ e.title }}</span
            ><span v-if="e.kind">（{{ END_NAME[e.kind] }}）</span><span v-if="i < f.summary.ends.length - 1">、</span>
          </span>
        </div>
        <div v-if="f.summary.dispatches.length">派发：{{ f.summary.dispatches.join('、') }}</div>
        <div v-if="f.summary.rejoins">走完后回到主线</div>
        <div v-if="f.summary.guessed">里面有按方法名推测的调用，不保证对</div>
        <div v-if="f.summary.unresolved">里面有没法判断目标的调用</div>
        <div v-if="inside" class="tip-diff">里面有 {{ inside }} 处和对比基准不一样</div>
        <div v-if="reqInside" class="tip-stop">里面有 {{ reqInside }} 个节点和实现对不上</div>
        <div v-if="runCls.includes('run-inside')" class="tip-run">选中的测试走过里面的节点</div>
        <div v-if="runCls.includes('run-stop')" class="tip-stop">失败的测试停在里面</div>
        <div v-if="uncInside" :class="uncInside.certain ? 'tip-stop' : 'tip-run'">{{ uncoveredCountText(uncInside, 'inside') }}</div>
        <div class="tip-hint">点击在原地展开</div>
      </div>
    </n-tooltip>
    <span
      v-if="inside"
      class="fm-count at-corner"
      data-tip-skip
      :title="`里面有 ${inside} 处变化，点开看`"
      @click="openFold(f)"
      >{{ inside }}</span
    >
    <span
      v-if="reqInside"
      class="fm-count at-corner req-count"
      data-tip-skip
      :title="`里面有 ${reqInside} 个节点和实现对不上，点开看`"
      @click="openFold(f)"
      >{{ reqInside }}</span
    >
    <span
      v-if="uncInside"
      class="fm-run-unc"
      :class="{ unsure: !uncInside.certain }"
      data-tip-skip
      :title="uncoveredCountText(uncInside, 'inside')"
      @click="openFold(f)"
      >{{ uncInside.certain || uncInside.uncertain }}</span
    >
  </div>
</template>
