<script setup lang="ts">
// 需求列表（RequirementList.vue）的一行。单独一条：编号（OpenSpec 的是变更名）和标题，第二行是标记和功能线，右边是状态
// （"已归档"那一组里不写：都是已归档）。变更组里的一行：只写去掉变更名的标题，右边是标记（状态写在组的标题上），
// 标题不是功能线时第二行写功能线。提示里是完整的编号、标题和功能线。
import type { ReqItem } from '../requirements'
import { selectRequirement } from '../state'

defineProps<{ item: ReqItem; on: boolean; inGroup?: boolean }>()
</script>

<template>
  <button
    class="li rq-li"
    :class="{ on, 'rq-in': inGroup }"
    :aria-current="on ? 'true' : undefined"
    data-tip="text"
    :data-tip-title="item.id ? `${item.id}  ${item.title}` : item.title"
    :data-tip-sub="item.second"
    @click="selectRequirement(item.key)"
  >
    <template v-if="inGroup">
      <span class="li-main">
        <span class="li-title" data-clip>{{ item.short }}</span>
      </span>
      <span v-if="item.groupSecond" class="li-second" data-clip>{{ item.groupSecond }}</span>
      <span v-if="item.mark" class="rq-mark rq-side" :class="`rq-${item.mark.tone}`" :title="item.mark.tip" data-tip-skip>{{
        item.mark.text
      }}</span>
    </template>
    <template v-else>
      <span class="li-main">
        <span v-if="item.name" class="rq-id">{{ item.name }}</span>
        <span class="li-title" data-clip>{{ item.short }}</span>
      </span>
      <span class="li-second" data-clip>
        <span v-if="item.mark" class="rq-mark" :class="`rq-${item.mark.tone}`" :title="item.mark.tip" data-tip-skip>{{
          item.mark.text
        }}</span>{{ item.second }}
      </span>
      <span v-if="!item.archived" class="rq-status" :class="item.statusClass">{{ item.status || '没有状态' }}</span>
    </template>
  </button>
</template>
