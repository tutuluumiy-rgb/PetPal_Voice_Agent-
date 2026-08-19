<script setup lang="ts">
/**
 * 控制面板通用页面卡片容器
 * 复用设计 token：surface-3 分层背景 + hairline 边框 + 多层阴影 + 圆角
 */
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <section
    class="rounded-lg border border-line-subtle bg-surface-3/60 shadow-ds-md transition-shadow duration-ds-sm ease-expo-out"
  >
    <header class="flex flex-col gap-0.5 border-b border-line-subtle px-4 py-3">
      <h2 class="text-[14px] font-semibold tracking-ds-tight text-fg-primary">{{ title }}</h2>
      <p v-if="description" class="text-[12px] leading-4 text-fg-muted">{{ description }}</p>
    </header>
    <div class="p-4">
      <slot />
    </div>
  </section>
</template>
