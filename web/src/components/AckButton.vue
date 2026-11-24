<script setup lang="ts">
// "标为已看"：清掉的是这个项目全部未看的变化标记（之后从这次分析起比），先确认。差异栏（按钮）和项目菜单（链接）共用这一个，确认的
// 说法一样。用对话框而不是气泡确认：项目菜单本身在一个弹出层里，气泡套气泡点一下会把外层关掉。确认框打开时记下
// 看的是哪个项目的哪次分析（ackTarget），确认时已经换了就不标。
import { NButton, useDialog } from 'naive-ui'
import { ackChanges, ackTarget, acking } from '../state'
import Icon from './Icon.vue'

defineProps<{ link?: boolean }>()
const emit = defineEmits<{ confirming: [] }>()
const dialog = useDialog()

function confirm() {
  // 记下这会儿看的是哪次分析：确认框开着时页面换上了新的分析，就不标那次没看过的
  const target = ackTarget()
  emit('confirming')
  dialog.warning({
    title: '把这些流程变化都标为看过？',
    content: '这个项目所有功能线上的变化标记都会去掉；下次重新分析只标出那之后的变化。代码和分析结果都不动。',
    positiveText: '标为已看',
    negativeText: '取消',
    onPositiveClick: () => ackChanges(target),
  })
}
</script>

<template>
  <button v-if="link" class="d-link" :disabled="acking" @click="confirm">标为已看</button>
  <n-button v-else size="tiny" tertiary :loading="acking" @click="confirm">
    <template #icon><Icon name="ok" /></template>标为已看
  </n-button>
</template>
