<script setup lang="ts">
/**
 * 侧边栏导航
 * 头部 = 品牌 logo（assets/logo.png）；底部 = 登录入口（点击展开菜单）：
 * 登录账户 / 帮助 / 模型 / 退出应用 / 关于（关于、模型页已从导航列表移入此处）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import LogoImg from './LogoImg.vue'
export interface NavItem {
  key: string
  label: string
  icon: string
}

export type LoginMenuKind = 'login' | 'help' | 'model' | 'quit' | 'about'

defineProps<{
  items: NavItem[]
  activeKey: string
}>()

const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'menu', kind: LoginMenuKind): void
}>()

// 登录菜单（点击外部关闭）
const menuOpen = ref(false)

// 宠物显示切换（圆点：绿=已显示，橙=已隐藏；点击切换）
const petVisible = ref(true)
let unsubPet: (() => void) | null = null
function togglePet(): void {
  window.api.setPetVisible(!petVisible.value)
}
onMounted(() => {
  window.api.getPetVisible().then((v) => (petVisible.value = v)).catch(() => undefined)
  unsubPet = window.api.onPetVisibleChanged((v) => (petVisible.value = v))
})
onBeforeUnmount(() => {
  unsubPet?.()
})

const MENU_ITEMS = [
  { kind: 'login' as const, label: '登录账户' },
  { kind: 'help' as const, label: '帮助' },
  { kind: 'model' as const, label: '模型' },
  { kind: 'quit' as const, label: '退出应用' },
  { kind: 'about' as const, label: '关于' }
]

function toggleMenu(): void {
  menuOpen.value = !menuOpen.value
}

function pick(kind: LoginMenuKind): void {
  menuOpen.value = false
  emit('menu', kind)
}

function onDocClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (target && target.closest('[data-login-menu]')) return
  menuOpen.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <nav class="flex w-52 shrink-0 flex-col gap-0.5 border-r border-line-subtle bg-surface-1/60 p-2">
    <div class="flex items-center gap-2 px-2 pb-3 pt-1">
      <LogoImg :size="24" />
      <span class="text-[13px] font-semibold tracking-ds-tight text-fg-primary">PetPal</span>
      <!-- 宠物显示切换：绿=已显示，橙=已隐藏（紧邻「PetPal」8px） -->
      <button
        type="button"
        title="宠物显示"
        @click="togglePet"
        class="flex h-5 w-5 items-center justify-center rounded-full transition-colors duration-ds-sm ease-expo-out hover:bg-surface-2"
      >
        <span class="h-2.5 w-2.5 rounded-full shadow-ds-sm" :class="petVisible ? 'bg-[#22c55e]' : 'bg-[#f97316]'" />
      </button>
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

    <!-- 底部：登录入口（点击展开菜单） -->
    <div class="mt-auto px-2 pt-4">
      <div class="relative" data-login-menu>
        <button
          type="button"
          class="flex w-full items-center gap-2 rounded-md border border-line-subtle bg-surface-2/50 px-2 py-1.5 text-left transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:shadow-ds-hover active:shadow-ds-active"
          title="账户与更多"
          @click.stop="toggleMenu"
        >
          <LogoImg :size="20" />
          <span class="flex-1 text-[12px] font-medium text-fg-secondary">登录</span>
          <span class="text-[10px] text-fg-muted transition-transform duration-ds-sm ease-expo-out"
                :class="menuOpen ? 'rotate-180' : ''">▾</span>
        </button>

        <!-- 登录菜单：登录账户 / 帮助 / 模型 / 退出应用 / 关于 -->
        <div
          v-show="menuOpen"
          class="absolute bottom-[calc(100%+6px)] left-0 z-[10000] w-full overflow-hidden rounded-md border border-line-subtle bg-surface-1 shadow-ds-lg"
        >
          <button
            v-for="opt in MENU_ITEMS"
            :key="opt.kind"
            type="button"
            class="flex w-full items-center gap-2 px-2.5 py-2 text-left text-[12px] text-fg-secondary transition-colors duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary active:bg-surface-3"
            @click.stop="pick(opt.kind)"
          >
            <span>{{ opt.label }}</span>
          </button>
        </div>
      </div>
    </div>
  </nav>
</template>