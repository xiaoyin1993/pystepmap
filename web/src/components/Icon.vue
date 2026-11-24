<script setup lang="ts">
// 线条图标（24 格，描边 2）。只收界面用到的几个。
export type IconName =
  | 'play'
  | 'pause'
  | 'stop'
  | 'replay'
  | 'step'
  | 'drill'
  | 'cond'
  | 'loop'
  | 'catch'
  | 'ok'
  | 'skipped'
  | 'failed'
  | 'exception'
  | 'plus'
  | 'minus'
  | 'goto'
  | 'chevron-down'
  | 'chevron-up'
  | 'search'
  | 'editor'
  | 'copy'
  | 'expand'
  | 'reset'
  | 'fit'
  | 'edit'
  | 'refresh'
  | 'trash'
  | 'folder'
  | 'settings'

const props = withDefaults(defineProps<{ name: IconName; size?: number; stroke?: number }>(), { size: 14, stroke: 2 })

const PATHS: Record<IconName, string[]> = {
  play: ['M8 5.5v13l11-6.5z'],
  pause: ['M8.5 5.5v13', 'M15.5 5.5v13'],
  stop: ['M6.5 6.5h11v11h-11z'],
  // 回放：绕回来的箭头加一个小三角
  replay: ['M4.5 12a7.5 7.5 0 1 0 2.2-5.3', 'M4.5 4.5v4h4', 'M10.5 9v6l4.5-3z'],
  step: ['M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10z'],
  drill: ['M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z', 'M9 8v4.5h6', 'M12.5 10l2.5 2.5-2.5 2.5'],
  cond: ['M12 3.5l8.5 8.5-8.5 8.5L3.5 12z'],
  loop: ['M4.5 12a7.5 7.5 0 0 1 13.2-4.9', 'M19.5 12a7.5 7.5 0 0 1-13.2 4.9', 'M18.5 3.5v4h-4', 'M5.5 20.5v-4h4'],
  catch: ['M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z', 'M12 8v4.5', 'M12 16h.01'],
  ok: ['M5 12.5l4.5 4.5L19 7.5'],
  skipped: ['M5 7l6 5-6 5', 'M13 7l6 5-6 5'],
  failed: ['M6.5 6.5l11 11', 'M17.5 6.5l-11 11'],
  exception: ['M13.5 3L5.5 13.5h6l-1 7.5 8-10.5h-6z'],
  plus: ['M12 6v12', 'M6 12h12'],
  minus: ['M6 12h12'],
  goto: ['M9.5 14.5L4.5 9.5l5-5', 'M4.5 9.5H15a5 5 0 0 1 0 10h-3'],
  'chevron-down': ['M6 9l6 6 6-6'],
  'chevron-up': ['M6 15l6-6 6 6'],
  search: ['M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13z', 'M15.5 15.5L20 20'],
  editor: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4'],
  copy: ['M9 9h10v11H9z', 'M5 15V4h10'],
  expand: ['M4 9V4h5', 'M20 9V4h-5', 'M4 15v5h5', 'M20 15v5h-5'],
  reset: ['M4.5 12a7.5 7.5 0 1 0 2.2-5.3', 'M4.5 4.5v4h4'],
  fit: ['M8 4H4v4', 'M16 4h4v4', 'M8 20H4v-4', 'M16 20h4v-4', 'M9 9h6v6H9z'],
  edit: ['M4.5 19.5h4l10-10a2.1 2.1 0 0 0-4-4l-10 10z', 'M13 7l4 4'],
  refresh: ['M19.5 12a7.5 7.5 0 1 1-2.2-5.3', 'M19.5 4.5v4h-4'],
  trash: ['M4.5 7h15', 'M9.5 7V4.5h5V7', 'M6.5 7l1 12.5h9l1-12.5'],
  folder: ['M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4.5l2 2.5H19A1.5 1.5 0 0 1 20.5 9.5v8A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z'],
  // 两根滑杆：分析配置
  settings: ['M4 8h9', 'M17 8h3', 'M4 16h3', 'M11 16h9', 'M15 5.5v5', 'M9 13.5v5'],
}
const FILLED = new Set<IconName>(['play', 'stop'])
</script>

<template>
  <svg
    class="fm-icon"
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    :fill="FILLED.has(props.name) ? 'currentColor' : 'none'"
    stroke="currentColor"
    :stroke-width="props.stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path v-for="(d, i) in PATHS[props.name]" :key="i" :d="d" />
  </svg>
</template>

<style>
.fm-icon {
  display: block;
  flex: none;
}
</style>
