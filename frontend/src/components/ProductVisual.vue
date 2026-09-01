<script setup lang="ts">
import { computed } from 'vue'
import type { VisualProfile } from '@/types'

const props = withDefaults(
  defineProps<{
    visual: VisualProfile
    size?: number
    showLabel?: boolean
  }>(),
  { size: 44, showLabel: true },
)

const height = computed(() => Math.round(props.size * 1.4))

// 深色底用白字，浅色底用深字，保证小尺寸下的可读性
const labelColor = computed(() => {
  const hex = props.visual.color.replace('#', '')
  const r = parseInt(hex.slice(0, 2), 16)
  const g = parseInt(hex.slice(2, 4), 16)
  const b = parseInt(hex.slice(4, 6), 16)
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return luminance > 0.6 ? '#1f2937' : '#ffffff'
})

const shortLabel = computed(() => props.visual.label.slice(0, 3))
</script>

<template>
  <svg
    :width="size"
    :height="height"
    viewBox="0 0 40 56"
    role="img"
    :aria-label="visual.label"
  >
    <!-- 罐装：可乐、百事 -->
    <g v-if="visual.shape === 'can'">
      <rect x="9" y="8" width="22" height="42" rx="4" :fill="visual.color" />
      <ellipse cx="20" cy="9" rx="11" ry="3" :fill="visual.color2" opacity="0.9" />
      <rect x="9" y="22" width="22" height="9" :fill="visual.color2" opacity="0.55" />
    </g>

    <!-- 瓶装：水、气泡水、茶、洗衣液 -->
    <g v-else-if="visual.shape === 'bottle'">
      <rect x="16" y="4" width="8" height="5" rx="1.5" :fill="visual.color2" />
      <path d="M15 9h10l3 7v32a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4V16z" :fill="visual.color" />
      <rect x="12" y="22" width="16" height="11" :fill="visual.color2" opacity="0.65" />
    </g>

    <!-- 盒装：牛奶、派、坚果、口罩、香皂 -->
    <g v-else-if="visual.shape === 'box'">
      <rect x="8" y="12" width="24" height="36" rx="2" :fill="visual.color" />
      <rect x="8" y="12" width="24" height="10" rx="2" :fill="visual.color2" opacity="0.75" />
      <rect x="8" y="28" width="24" height="8" :fill="visual.color2" opacity="0.45" />
    </g>

    <!-- 袋装：薯片、辣条、沙琪玛 -->
    <g v-else-if="visual.shape === 'bag'">
      <path d="M7 20c3-3 6-2 26-2 1 0 1 22-3 26-6 5-18 5-22 0-3-4-2-21-1-24z" :fill="visual.color" />
      <rect x="8" y="15" width="24" height="6" rx="2" :fill="visual.color2" opacity="0.8" />
      <rect x="9" y="32" width="22" height="8" :fill="visual.color2" opacity="0.4" />
    </g>

    <!-- 管状：牙膏 -->
    <g v-else-if="visual.shape === 'tube'">
      <rect x="13" y="4" width="14" height="6" rx="2" :fill="visual.color2" />
      <path d="M12 10h16v34a6 6 0 0 1-6 6h-4a6 6 0 0 1-6-6z" :fill="visual.color" />
      <rect x="12" y="26" width="16" height="9" :fill="visual.color2" opacity="0.45" />
    </g>

    <!-- 条状 / 卷装：饼干、巧克力、雪糕、抽纸 -->
    <g v-else-if="visual.shape === 'pack'">
      <rect x="4" y="17" width="32" height="22" rx="5" :fill="visual.color" />
      <rect x="4" y="25" width="32" height="7" :fill="visual.color2" opacity="0.6" />
    </g>

    <!-- 杯装：咖啡 -->
    <g v-else>
      <path d="M11 17h18l-2 27a5 5 0 0 1-5 4h-4a5 5 0 0 1-5-4z" :fill="visual.color" />
      <ellipse cx="20" cy="16" rx="9" ry="3.5" :fill="visual.color2" />
      <rect x="12" y="26" width="16" height="8" :fill="visual.color2" opacity="0.45" />
    </g>

    <text
      v-if="showLabel && size >= 34"
      x="20"
      y="52"
      text-anchor="middle"
      :fill="labelColor"
      font-size="7"
      font-weight="600"
      style="paint-order: stroke; stroke: rgba(255,255,255,0.55); stroke-width: 2px"
    >
      {{ shortLabel }}
    </text>
  </svg>
</template>
