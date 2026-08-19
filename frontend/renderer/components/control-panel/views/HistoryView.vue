<script setup lang="ts">
/**
 * 历史记录 — UI 骨架占位
 * TODO: 后续迭代实现 — 接入主进程历史会话查询（WebSocket/本地存储）
 */
import PageCard from '../PageCard.vue'

// TODO: 后续迭代实现 — 历史会话数据源与分页查询
const placeholderRows = [
  { time: '--:--', mode: '闲聊', preview: '（暂无历史记录）' },
  { time: '--:--', mode: '工作', preview: '（暂无历史记录）' },
  { time: '--:--', mode: '闲聊', preview: '（暂无历史记录）' }
]
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="历史记录" description="查询与球球的过往对话">
      <div class="flex items-center gap-2">
        <input
          type="text"
          placeholder="搜索历史消息…"
          class="h-8 min-w-0 flex-1 rounded-md border border-line-subtle bg-surface-1 px-2.5 text-[13px] text-fg-primary placeholder:text-fg-muted outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60 focus:shadow-[0_0_0_2px_rgba(94,106,210,0.25),var(--ds-shadow-sm)]"
        />
        <button
          type="button"
          class="h-8 shrink-0 rounded-md border border-line-subtle bg-transparent px-3 text-[13px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active"
        >
          查询
        </button>
      </div>

      <div class="mt-3 flex flex-col gap-1.5">
        <div
          v-for="row in placeholderRows"
          :key="row.time + row.preview"
          class="flex items-center gap-3 rounded-md border border-line-subtle bg-surface-1/60 px-3 py-2"
        >
          <span class="font-mono text-[11px] text-fg-muted">{{ row.time }}</span>
          <span class="rounded-sm bg-accent/15 px-1.5 py-0.5 font-mono text-[10px] text-accent">
            {{ row.mode }}
          </span>
          <span class="flex-1 truncate text-[13px] text-fg-secondary">{{ row.preview }}</span>
        </div>
      </div>
    </PageCard>
  </div>
</template>
