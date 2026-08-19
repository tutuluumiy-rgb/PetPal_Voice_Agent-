<script setup lang="ts">
/**
 * 侧边栏导航
 * 6 个导航项，点击切换主内容区；active 高亮走设计 token
 */
export interface NavItem {
  key: string
  label: string
  icon: string
}

defineProps<{
  items: NavItem[]
  activeKey: string
}>()

const emit = defineEmits<{
  (e: 'select', key: string): void
}>()
</script>

<template>
  <nav class="flex w-52 shrink-0 flex-col gap-0.5 border-r border-line-subtle bg-surface-1/60 p-2">
    <div class="flex items-center gap-2 px-2 pb-3 pt-1">
      <div class="flex h-6 w-6 items-center justify-center rounded-md bg-accent/15 text-[13px] font-semibold text-accent">
        球
      </div>
      <span class="text-[13px] font-semibold tracking-ds-tight text-fg-primary">PetPal</span>
    </div>

    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      class="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-[13px] transition-all duration-ds-sm ease-expo-out"
      :class="
        activeKey === item.key
          ? 'bg-accent-soft font-medium text-fg-primary shadow-ds-sm'
          : 'text-fg-secondary hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active'
      "
      @click="emit('select', item.key)"
    >
      <span class="w-4 text-center font-mono text-[11px]">{{ item.icon }}</span>
      <span class="tracking-ds-normal">{{ item.label }}</span>
    </button>

    <div class="mt-auto px-2 pt-4">
      <div class="rounded-md border border-line-subtle bg-surface-2/50 px-2 py-1.5">
        <p class="font-mono text-[10px] uppercase tracking-widest text-fg-muted">v0.1.0</p>
        <p class="text-[10px] text-fg-muted">桌面端 AI 虚拟形象客户端</p>
      </div>
    </div>
  </nav>
</template>
