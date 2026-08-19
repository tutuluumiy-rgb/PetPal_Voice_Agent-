<script setup lang="ts">
/**
 * 关于 / 版本信息页
 * 展示版本号（经 IPC 读取 package.json version）与项目描述
 */
import { onMounted, ref } from 'vue'
import PageCard from '../PageCard.vue'

const version = ref('0.1.0')

onMounted(async () => {
  try {
    version.value = await window.api.getAppVersion()
  } catch {
    // 保持默认
  }
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="关于" description="PetPal Voice Agent — 桌面端 AI 语音虚拟形象客户端">
      <div class="flex items-start gap-4">
        <div
          class="flex h-16 w-16 shrink-0 items-center justify-center rounded-lg border border-line-subtle bg-accent/15 text-2xl font-semibold text-accent shadow-ds-md"
        >
          球
        </div>
        <div class="flex flex-col gap-1.5">
          <h3 class="text-[15px] font-semibold tracking-ds-tight text-fg-primary">PetPal</h3>
          <p class="font-mono text-[11px] uppercase tracking-widest text-fg-muted">
            PetPal Voice Agent · v{{ version }}
          </p>
          <p class="max-w-80 text-[12px] leading-5 text-fg-secondary">
            悬浮桌面宠物 + 语音交互入口。当前为可运行底座：窗口、拖拽、上下文对话面板、控制面板与
            IPC 桥接已完成，语音链路与动画引擎将在后续迭代接入。
          </p>
        </div>
      </div>

      <div class="mt-4 flex flex-col gap-1 border-t border-line-subtle pt-3">
        <p class="font-mono text-[10px] uppercase tracking-widest text-fg-muted">技术栈</p>
        <p class="text-[12px] leading-5 text-fg-secondary">
          Electron-Vite · Vue 3 · TypeScript · TailwindCSS · Linear 风格暗黑设计系统
        </p>
      </div>
    </PageCard>
  </div>
</template>
