<script setup lang="ts">
/**
 * 对话面板（独立窗口）— PetPal Chat Panel
 * --------------------------------------------------------------------------
 * 运行在独立的透明窗口（350×550）中，由主进程按宠物位置定位（宠物左侧，
 * 空间不足切右侧）。宠物窗口尺寸恒定、canvas 永不补偿 → 打开/关闭本面板
 * 完全不影响宠物状态（两个图层彻底解耦，解决问题1/2）。
 *
 * 本组件只负责：
 * - 消息区（对话历史）
 * - 输入区（ChatInputBar）+ 语音开关
 * - 模式 / 权限小卡片、设置、隐藏宠物
 * - 语音管线（VoicePipeline：后端 8001 /ws/audio + KWS 唤醒待机）
 *
 * 窗口的显示 / 隐藏 / 定位全部由主进程管理；本组件只发 open/close 通知。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { AuthPolicy, PetMode } from '../../../preload/types'
import ChatInputBar from '../pet-window/ChatInputBar.vue'
import { VoicePipeline } from '../../app/voice/VoicePipeline'

// ---------- 消息区（对话历史） ----------
interface ChatMessage {
  role: 'user' | 'pet'
  text: string
}

const messages = ref<ChatMessage[]>([
  { role: 'pet', text: '你好呀，我是球球！有什么想聊的？' }
])

function pushMessage(role: ChatMessage['role'], text: string): void {
  messages.value = [...messages.value, { role, text }]
}

// ---------- 语音接入（后端职责提供：连真实后端 8001 /ws/audio + 唤醒词待机） ----------
const wsUrl = 'ws://127.0.0.1:8001/ws/audio'
const vadBase = '/vad/'
// 实际可唤词由 KWS 模型词表决定（resources/kws/keywords.txt，默认示例「你好西西」）
const wakeKeyword = '你好西西'
let voice: VoicePipeline | null = null
const listening = ref(false)   // 是否正在语音交互
const stateLabel = ref('')     // 待机/聆听状态提示

async function setMicState(on: boolean, wake = false): Promise<void> {
  try {
    if (on) {
      if (!voice) {
        voice = new VoicePipeline({ wsUrl, vadAssetsBase: vadBase })
        voice.onUserText = (text) => {
          if (text) pushMessage('user', text)
        }
        voice.onReply = (text) => {
          if (text) {
            pushMessage('pet', text)
            // 把宠物本次播报的文本同步推给宠物窗口底部消息条（实时语音播报）
            window.api.pushVoicePreview(text)
          }
        }
        voice.onState = (s) => {
          stateLabel.value =
            s === 'idle' ? `待机中，说「${wakeKeyword}」唤醒` : s === 'speaking' ? '正在回复…' : '聆听中…'
          listening.value = s !== 'idle'
        }
        // TTS 开始/结束 → 通知宠物窗口切换动画说话态
        voice.onTtsEvent = (kind) => {
          window.api.setPetAnim(kind === 'start' ? 'speaking' : 'idle')
        }
        voice.onWake = (kw) => {
          pushMessage('pet', `(唤醒成功：${kw})`)
        }
      }
      await voice.start(wake ? { wakeWord: true } : undefined)
      stateLabel.value = wake ? `待机中，说「${wakeKeyword}」唤醒` : '聆听中…'
    } else {
      await voice?.stop()
      listening.value = false
      stateLabel.value = ''
    }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    console.error('[语音] 启动失败:', reason, err)
    listening.value = false
    stateLabel.value = ''
    pushMessage('pet', '语音连接失败：' + reason)
    if (voice) {
      try {
        await voice.stop()
      } catch {
        /* ignore */
      }
      voice = null
    }
  }
}

function toggleMic(): void {
  if (voice?.isRunning) {
    void setMicState(false)
  } else {
    void setMicState(true, false)
  }
}

// ---------- 全局状态（单选选中态，与主进程同步） ----------
const currentMode = ref<PetMode>('chat')
const authPolicy = ref<AuthPolicy>('ask')

let unsubModeChanged: (() => void) | null = null

onMounted(async () => {
  try {
    currentMode.value = await window.api.getMode()
  } catch {
    /* 保持默认 */
  }
  unsubModeChanged = window.api.onModeChanged((mode) => {
    currentMode.value = mode
  })
  try {
    authPolicy.value = await window.api.getAuthPolicy()
  } catch {
    /* 保持默认 */
  }
  // 自动启动语音：进入即「待机听唤醒」；点 🎙 才直接对话
  void setMicState(true, true)
})

onBeforeUnmount(() => {
  unsubModeChanged?.()
  void voice?.stop()
  voice = null
})

function selectMode(mode: PetMode): void {
  currentMode.value = mode
  window.api.switchMode(mode)
}

function selectAuth(policy: AuthPolicy): void {
  authPolicy.value = policy
  window.api.setAuthPolicy(policy)
}

// ---------- 面板动作：关闭 / 设置 / 隐藏宠物 ----------
function closePanel(): void {
  window.api.closeChatPanel()
}

function openSettings(): void {
  window.api.openPanel()
}

function hidePet(): void {
  window.api.setPetVisible(false)
}

// ---------- 模式 / 权限弹出小卡片（250×100，悬浮面板上层，点空白关闭） ----------
const modeCardOpen = ref(false)
const authCardOpen = ref(false)
const modeCardPos = ref({ x: 0, y: 0 })
const authCardPos = ref({ x: 0, y: 0 })

function toggleModeCard(ev: MouseEvent): void {
  const btn = ev.currentTarget as HTMLElement
  const r = btn.getBoundingClientRect()
  modeCardPos.value = { x: r.left, y: r.bottom + 4 }
  modeCardOpen.value = !modeCardOpen.value
  authCardOpen.value = false
}

function toggleAuthCard(ev: MouseEvent): void {
  const btn = ev.currentTarget as HTMLElement
  const r = btn.getBoundingClientRect()
  authCardPos.value = { x: r.left, y: r.bottom + 4 }
  authCardOpen.value = !authCardOpen.value
  modeCardOpen.value = false
}

// ---------- 全局事件：ESC 关闭；点击面板外部由主进程处理 ----------
function onKeyDown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    closePanel()
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeyDown)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeyDown)
})
</script>

<template>
  <!-- 面板卡片：inset 留边，让圆角阴影在透明窗口内完整渲染；阴影扩散与消息条一致(2px/8px) -->
  <div class="absolute inset-[16px] flex flex-col overflow-hidden rounded-xl bg-white shadow-[0_2px_8px_rgba(0,0,0,0.18)]">
    <!-- 头部：40px，标题 + 最右侧关闭面板按钮；header 为可拖拽区（面板可自由移动），按钮需 no-drag -->
    <header
      class="flex h-10 shrink-0 items-center justify-between border-b border-black/[0.06] px-3"
      style="-webkit-app-region: drag"
    >
      <span class="text-[13px] font-semibold text-[#1a1a1a]">球球对话</span>
      <div class="flex items-center gap-1.5" style="-webkit-app-region: no-drag">
        <!-- 语音开关：开始/停止语音对话 -->
        <button
          type="button"
          class="flex h-6 items-center gap-1 rounded-full px-2 text-[11px] font-medium transition-colors duration-200 ease-expo-out"
          :class="listening ? 'bg-accent text-white' : 'bg-black/[0.06] text-[#8a8a8a] hover:bg-black/[0.12] hover:text-[#1a1a1a]'"
          :title="listening ? '停止语音' : '开始语音'"
          @click="toggleMic"
        >
          <span :class="listening ? 'animate-pulse' : ''">{{ listening ? `${stateLabel || '● 对话中…'}` : '🎙 语音' }}</span>
        </button>
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-full bg-black/[0.06] text-[#8a8a8a] transition-colors duration-200 ease-expo-out hover:bg-black/[0.12] hover:text-[#1a1a1a] active:bg-black/[0.16]"
          title="关闭面板"
          @click="closePanel"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </div>
    </header>

    <!-- 消息区 + 输入区（flex 自适应共享剩余空间） -->
    <div class="flex min-h-0 flex-1 flex-col">
      <div class="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        <div v-for="(m, i) in messages" :key="i" class="mb-1.5 text-[13px] leading-5">
          <template v-if="m.role === 'user'">
            <span class="font-medium text-[#1a1a1a]">你：</span><span class="text-[#333]">{{ m.text }}</span>
          </template>
          <template v-else>
            <span class="font-medium text-[#5e6ad2]">球球：</span><span class="text-[#333]">{{ m.text }}</span>
          </template>
        </div>
      </div>

      <ChatInputBar
        @settings="openSettings"
        @file="() => undefined"
        @mode-card="toggleModeCard"
        @auth-card="toggleAuthCard"
        @hide-pet="hidePet"
      />
    </div>

    <!-- 模式弹出小卡片 -->
    <div
      v-show="modeCardOpen"
      data-mode-card
      class="fixed z-[10000] w-[250px] rounded-xl border border-black/[0.06] bg-white p-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.18)]"
      :style="{ left: `${modeCardPos.x}px`, top: `${modeCardPos.y}px` }"
    >
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-[#1a1a1a] transition-colors duration-200 ease-expo-out hover:bg-black/[0.04] active:bg-black/[0.06]"
        :class="currentMode === 'chat' ? 'bg-accent/10 font-medium' : ''"
        @click="selectMode('chat')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="currentMode === 'chat' ? 'border-accent' : 'border-black/20'"
        >
          <span v-if="currentMode === 'chat'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        聊天模式
      </button>
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-[#1a1a1a] transition-colors duration-200 ease-expo-out hover:bg-black/[0.04] active:bg-black/[0.06]"
        :class="currentMode === 'work' ? 'bg-accent/10 font-medium' : ''"
        @click="selectMode('work')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="currentMode === 'work' ? 'border-accent' : 'border-black/20'"
        >
          <span v-if="currentMode === 'work'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        工作模式
      </button>
    </div>

    <!-- 权限弹出小卡片 -->
    <div
      v-show="authCardOpen"
      data-auth-card
      class="fixed z-[10000] w-[250px] rounded-xl border border-black/[0.06] bg-white p-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.18)]"
      :style="{ left: `${authCardPos.x}px`, top: `${authCardPos.y}px` }"
    >
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-[#1a1a1a] transition-colors duration-200 ease-expo-out hover:bg-black/[0.04] active:bg-black/[0.06]"
        :class="authPolicy === 'full' ? 'bg-accent/10 font-medium' : ''"
        @click="selectAuth('full')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="authPolicy === 'full' ? 'border-accent' : 'border-black/20'"
        >
          <span v-if="authPolicy === 'full'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        完全批准
      </button>
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-[#1a1a1a] transition-colors duration-200 ease-expo-out hover:bg-black/[0.04] active:bg-black/[0.06]"
        :class="authPolicy === 'ask' ? 'bg-accent/10 font-medium' : ''"
        @click="selectAuth('ask')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="authPolicy === 'ask' ? 'border-accent' : 'border-black/20'"
        >
          <span v-if="authPolicy === 'ask'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        请求批准
      </button>
    </div>
  </div>
</template>
