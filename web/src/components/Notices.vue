<script setup lang="ts">
// 把状态层（state.ts 的 notify）发出的提示用 Naive UI 的消息条显示出来：重新分析完成 / 失败、原来的功能线没了。
// 状态层不依赖界面，消息条只能在 NMessageProvider 里面用，所以由这个不画东西的组件转一下。
// 用了两个：corner 的那个放在右上角的 NMessageProvider 里，只显示标了 corner 的提示（"已载入新的分析结果"）；
// 另一个显示其余的。
import { watch } from 'vue'
import { useMessage } from 'naive-ui'
import { noticeShownIn, notices } from '../state'

const props = defineProps<{ corner?: boolean }>()
const message = useMessage()
let shown = notices.value.at(-1)?.seq ?? 0

watch(notices, (list) => {
  for (const n of list) {
    if (n.seq <= shown) continue
    shown = n.seq
    if (!noticeShownIn(n, !!props.corner)) continue
    message[n.type](n.text, { duration: n.type === 'error' || n.type === 'warning' ? 6000 : 3500 })
  }
})
</script>

<template>
  <span hidden />
</template>
