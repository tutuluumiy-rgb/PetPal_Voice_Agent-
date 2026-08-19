<script setup lang="ts">
/**
 * 控制面板外壳 — 侧边栏导航 + 主内容区
 * --------------------------------------------------------------------------
 * - 分层背景：径向渐变底层 + noise 纹理（ds-layered-bg / ds-noise-overlay）
 * - 页面入场：scale-in 动画（300ms，expo-out）
 * - 6 个占位页面按需切换（KeepAlive 保留状态）
 */
import { ref } from 'vue'
import type { NavItem } from './SidebarNav.vue'
import SidebarNav from './SidebarNav.vue'
import AboutView from './views/AboutView.vue'
import AnimationGenView from './views/AnimationGenView.vue'
import HistoryView from './views/HistoryView.vue'
import PersonalityView from './views/PersonalityView.vue'
import UserProfileView from './views/UserProfileView.vue'
import VoiceSettingsView from './views/VoiceSettingsView.vue'

const NAV_ITEMS: NavItem[] = [
  { key: 'history', label: '历史记录', icon: '◷' },
  { key: 'voice', label: '语音参数设置', icon: '♪' },
  { key: 'animation', label: '宠物动画生成', icon: '✦' },
  { key: 'personality', label: '宠物人设配置', icon: '◈' },
  { key: 'profile', label: '用户档案', icon: '◉' },
  { key: 'about', label: '关于', icon: 'ⓘ' }
]

const activeKey = ref('history')

// TODO: 后续迭代实现 — 从主进程读取已保存的默认页
function onSelect(key: string): void {
  activeKey.value = key
}
</script>

<template>
  <div class="ds-layered-bg relative flex h-screen w-screen overflow-hidden">
    <!-- noise 纹理层 -->
    <div class="ds-noise-overlay pointer-events-none absolute inset-0 opacity-[0.025]" />

    <!-- 侧边栏 -->
    <SidebarNav :items="NAV_ITEMS" :active-key="activeKey" @select="onSelect" />

    <!-- 主内容区 -->
    <main class="relative min-w-0 flex-1 overflow-y-auto">
      <div class="h-full animate-scale-in p-5">
        <KeepAlive>
          <HistoryView v-if="activeKey === 'history'" />
          <VoiceSettingsView v-else-if="activeKey === 'voice'" />
          <AnimationGenView v-else-if="activeKey === 'animation'" />
          <PersonalityView v-else-if="activeKey === 'personality'" />
          <UserProfileView v-else-if="activeKey === 'profile'" />
          <AboutView v-else />
        </KeepAlive>
      </div>
    </main>
  </div>
</template>
