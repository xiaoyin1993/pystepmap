<script setup lang="ts">
import BlockNode from './BlockNode.vue'
import type { LeafData } from './types'

defineOptions({ inheritAttrs: false })
defineProps<{ data: LeafData }>()
</script>

<template>
  <BlockNode :data="data" icon="loop" />
</template>
