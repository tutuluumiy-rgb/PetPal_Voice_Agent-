<script setup lang="ts">
/**
 * 控制面板外壳 — 侧边栏导航 + 主内容区
 * --------------------------------------------------------------------------
 * - 分层背景：径向渐变底层 + noise 纹理（ds-layered-bg / ds-noise-overlay）
 * - 页面入场：scale-in 动画（300ms，expo-out）
 * - 6 个页面按需切换（KeepAlive 保留状态）
 * - 右下角：版本/产品标识（小字）+ 页面级动作操作栏（50×27 圆角按钮，右 30 底 25）
 */
import { onMounted, ref } from 'vue'
import type { NavItem, LoginMenuKind } from './SidebarNav.vue'
import SidebarNav from './SidebarNav.vue'
import AboutView from './views/AboutView.vue'
import AnimationGenView from './views/AnimationGenView.vue'
import HistoryView from './views/HistoryView.vue'
import PersonalityView from './views/PersonalityView.vue'
import UserProfileView from './views/UserProfileView.vue'
import VoiceSettingsView from './views/VoiceSettingsView.vue'
import { panelActions, isActionDisabled, clearPanelActions } from '../../app/panelActions'

const NAV_ITEMS: NavItem[] = [
  { key: 'history', label: '历史记录', icon: '◷' },
  { key: 'voice', label: '语音参数设置', icon: '♪' },
  { key: 'animation', label: '宠物动画生成', icon: '✦' },
  { key: 'personality', label: '宠物人设配置', icon: '◈' },
  { key: 'profile', label: '用户档案', icon: '◉' }
  // 「关于」已移入左下登录菜单
]

const activeKey = ref('history')

// TODO: 后续迭代实现 — 从主进程读取已保存的默认页
function onSelect(key: string): void {
  activeKey.value = key
  // 切页时清空上页注册的操作栏动作（KeepAlive 不会触发上页 onBeforeUnmount）
  clearPanelActions()
}

// 登录菜单：登录账户/帮助 → 占位提示；退出应用 → 退出；关于 → 打开关于页
const notice = ref('')
let noticeTimer: ReturnType<typeof setTimeout> | null = null
function onMenu(kind: LoginMenuKind): void {
  if (kind === 'login') showNotice('登录功能即将上线')
  else if (kind === 'help') showNotice('帮助文档即将上线')
  else if (kind === 'quit') window.api.quitApp()
  else activeKey.value = 'about'
}
function showNotice(text: string): void {
  notice.value = text
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => {
    notice.value = ''
    noticeTimer = null
  }, 2200)
}

onMounted(() => {
  // 皮肤：控制面板跟随主进程主题（token 双主题）
  window.api.getSkin().then((s) => (document.documentElement.dataset.skin = s)).catch(() => undefined)
  window.api.onSkinChanged((s) => {
    document.documentElement.dataset.skin = s
  })
})
</script>

<template>
  <div class="ds-layered-bg relative flex h-screen w-screen overflow-hidden">
    <!-- noise 纹理层 -->
    <div class="ds-noise-overlay pointer-events-none absolute inset-0 opacity-[0.025]" />

    <!-- 侧边栏 -->
    <SidebarNav :items="NAV_ITEMS" :active-key="activeKey" @select="onSelect" @menu="onMenu" />

    <!-- 主内容区（底部留白，避免内容被右下角操作栏遮挡） -->
    <main class="relative min-w-0 flex-1">
      <div class="h-full animate-scale-in overflow-y-auto p-5 pb-24">
        <KeepAlive>
          <HistoryView v-if="activeKey === 'history'" />
          <VoiceSettingsView v-else-if="activeKey === 'voice'" />
          <AnimationGenView v-else-if="activeKey === 'animation'" />
          <PersonalityView v-else-if="activeKey === 'personality'" />
          <UserProfileView v-else-if="activeKey === 'profile'" />
          <AboutView v-else />
        </KeepAlive>
      </div>

      <!-- 占位提示（登录/帮助等） -->
      <transition name="fade">
        <div
          v-if="notice"
          class="pointer-events-none absolute rounded-md border border-line-subtle bg-surface-2 px-2.5 py-1.5 text-[12px] text-fg-secondary shadow-ds-md"
          style="left: 208px; bottom: 16px"
        >
          {{ notice }}
        </div>
      </transition>

      <!-- 页面动作操作栏（右下角：右 30 / 底 25，50×27 圆角，并排） -->
      <div v-if="panelActions.length" class="absolute flex items-center gap-2" style="right: 30px; bottom: 25px">
        <button
          v-for="act in panelActions"
          :key="act.key"
          type="button"
          :disabled="isActionDisabled(act)"
          class="flex h-[27px] w-[50px] items-center justify-center rounded-[6px] text-[12px] font-medium transition-all duration-ds-sm ease-expo-out disabled:opacity-40"
          :class="
            act.primary
              ? 'bg-accent text-fg-inverse hover:bg-accent-hover hover:shadow-ds-hover active:shadow-ds-active'
              : 'border border-line-subtle bg-surface-1 text-fg-secondary hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active'
          "
          @click="act.onClick"
        >
          {{ act.label }}
        </button>
      </div>
    </main>
  </div>
</template>
