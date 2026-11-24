<script setup lang="ts">
// 详情栏里节点的源码片段：行号在左边一栏，节点自己的语句高亮；等宽字体、不换行（横向滚动），太长时竖向滚动。代码
// 的显示交给 CodeBlock.vue（和被调用的函数、整个函数共用：代码从行首显示，行号不会盖住代码）。
// 框的高度在取到之前就按要取的行数定好，读取中、出错、图过期的提示都在框里，不让详情栏下面的内容跳动。
// 源码由平台后端提供（src/source.ts）。看变更记录时说明由详情栏给（note：null 不写；source.ts 的 reviewSourceNote）。
import { computed } from 'vue'
import { outOfDate, type SourceRequest, type SourceSnippet, type SourceStatus } from '../source'
import CodeBlock from './CodeBlock.vue'

const props = defineProps<{
  status: SourceStatus
  snippet: SourceSnippet | null
  error: string
  request: SourceRequest | null
  /** 看变更记录时片段上的说明（null = 不写）；不给时按图过期没有说 */
  note?: string | null
}>()

const LINE = 17
const PAD = 6
const MAX_HEIGHT = 320
const height = computed(() =>
  props.request ? Math.min(MAX_HEIGHT, (props.request.end - props.request.start + 1) * LINE + 2 * PAD) : 0,
)
const rows = computed(() => (props.snippet?.lines ?? []).map((text, i) => ({ no: props.snippet!.start + i, text })))
const stale = computed(() => (props.snippet && props.request ? outOfDate(props.snippet, props.request) : false))
const notice = computed(() =>
  props.note !== undefined ? props.note : stale.value ? '文件在分析之后改过，行号可能对不上（点顶栏的"重新分析"更新）' : null,
)
</script>

<template>
  <div v-if="status !== 'none' && request" class="src" :style="{ height: `${height}px` }">
    <div v-if="status === 'loading'" class="src-msg">正在读源码…</div>
    <div v-else-if="status === 'error'" class="src-msg err">读不到源码：{{ error }}</div>
    <template v-else>
      <div v-if="notice" class="src-stale">{{ notice }}</div>
      <div v-if="rows.length === 0" class="src-msg">这几行已经不在文件里了</div>
      <CodeBlock v-else class="src-code-block" :lines="rows" :focus-start="request.focusStart" :focus-end="request.focusEnd" label="源码片段" />
    </template>
  </div>
</template>

<style>
.src {
  position: relative;
  display: flex;
  flex-direction: column;
  margin-top: 10px;
  box-sizing: border-box;
  border: 1px solid #2a2c33;
  border-radius: 6px;
  background: #121317;
  overflow: hidden;
}
.src-code-block {
  flex: 1;
}
.src-msg {
  margin: auto;
  padding: 0 12px;
  font-size: 12px;
  color: var(--faint);
  text-align: center;
}
.src-msg.err {
  color: #c98a8a;
}
.src-stale {
  flex: none;
  padding: 3px 10px;
  font-size: 11.5px;
  color: var(--muted);
  background: #1b1c21;
  border-bottom: 1px solid #2a2c33;
}
</style>
