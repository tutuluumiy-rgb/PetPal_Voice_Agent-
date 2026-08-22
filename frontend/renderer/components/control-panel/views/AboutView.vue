<script setup lang="ts">
/**
 * 关于 / 版本信息页
 * 展示版本号（经 IPC 读取 package.json version）与项目描述
 */
import { onMounted, ref } from 'vue'
import PageCard from '../PageCard.vue'
import LogoImg from '../LogoImg.vue'

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
        <LogoImg :size="64" />
        <div class="flex flex-col gap-1.5">
          <h3 class="text-[15px] font-semibold tracking-ds-tight text-fg-primary">PetPal</h3>
          <p class="font-mono text-[11px] uppercase tracking-widest text-fg-muted">
            PetPal Voice Agent · v{{ version }}
          </p>
          <p class="max-w-96 text-[12px] leading-5 text-fg-secondary">
            悬浮桌面 AI 语音宠物：说「你好西西」唤醒进入闲聊 / 工作双模式对话；
            WebRTC 消回声 + Silero VAD 支持随时插话打断；四态帧动画（闲聊 / 工作 / 两种切换过渡）。
            控制面板可查看每次对话的 run 事件轨迹、真实编辑宠物人设、用户档案与语音参数，
            支持深色 / 浅色双皮肤。
          </p>
        </div>
      </div>
    </PageCard>
  </div>
</template>
