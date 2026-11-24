<script setup lang="ts">
// 过期条（画布上方一条）：代码在最新一次分析之后改过时，琥珀色写"代码已变更，图可能过期：改 2、增 1 个文件"，
// "看是哪些文件"展开列表（最多 20 个，其余"还有 N 个"；读不了的文件的说明也在这里），右边"重新分析"。没法检查时
// 是一条灰色的"没法检查代码有没有变：<原因>"。显示什么由 stale.ts 的 staleView 定（state.ts 的 staleInfo）。
import { computed, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { backend, currentProject, projectId, reanalyze, reanalyzing, staleInfo } from '../state'
import Icon from './Icon.vue'

const open = ref(false)
watch(projectId, () => (open.value = false))
const busy = computed(() => {
  const p = currentProject.value
  return !!p && (reanalyzing.value.includes(p.id) || p.analyzing)
})
const KIND_CLASS = { 改: 'k-changed', 增: 'k-added', 删: 'k-removed' } as const
</script>

<template>
  <div v-if="staleInfo.show" class="stale-bar" :class="staleInfo.kind === 'stale' ? 'sb-stale' : 'sb-unchecked'" role="status">
    <div class="sb-row">
      <span class="sb-dot" aria-hidden="true" />
      <span class="sb-text">{{ staleInfo.text }}</span>
      <template v-if="staleInfo.kind === 'stale'">
        <button class="bar-link" :aria-expanded="open" @click="open = !open">{{ open ? '收起文件列表' : '看是哪些文件' }}</button>
        <span class="bar-gap" />
        <n-button size="tiny" secondary :loading="busy" :disabled="busy || backend === 'down'" @click="reanalyze()">
          <template #icon><Icon name="refresh" /></template>重新分析
        </n-button>
      </template>
    </div>
    <ul v-if="open && staleInfo.kind === 'stale'" class="sb-files">
      <li v-for="f in staleInfo.files" :key="`${f.kind}:${f.file}`">
        <span class="sb-kind" :class="KIND_CLASS[f.kind]">{{ f.kind }}</span><code>{{ f.file }}</code>
      </li>
      <li v-if="staleInfo.more > 0" class="sb-more">还有 {{ staleInfo.more }} 个</li>
      <li v-if="staleInfo.note" class="sb-note">{{ staleInfo.note }}</li>
    </ul>
  </div>
</template>

<style>
.stale-bar {
  flex: none;
  padding: 6px 18px;
  font-size: 12.5px;
  border-bottom: 1px solid var(--panel-line);
}
.stale-bar.sb-stale {
  color: var(--decision);
  background: rgba(232, 161, 58, 0.08);
  border-bottom-color: rgba(232, 161, 58, 0.35);
}
.stale-bar.sb-unchecked {
  color: var(--muted);
  background: var(--panel);
}
.sb-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
}
.sb-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.sb-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-files {
  margin: 6px 0 2px 17px;
  padding: 0;
  list-style: none;
  max-height: 180px;
  overflow: auto;
  color: var(--text-soft);
}
.sb-files li {
  display: flex;
  align-items: center;
  gap: 8px;
  line-height: 20px;
}
.sb-files code {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-kind {
  flex: none;
  font-size: 10.5px;
  line-height: 15px;
  padding: 0 4px;
  border-radius: 3px;
  border: 1px solid;
}
.sb-kind.k-changed {
  color: var(--decision);
}
.sb-kind.k-added {
  color: var(--ok);
}
.sb-kind.k-removed {
  color: var(--failed);
}
.sb-more,
.sb-note {
  color: var(--muted);
}
</style>
