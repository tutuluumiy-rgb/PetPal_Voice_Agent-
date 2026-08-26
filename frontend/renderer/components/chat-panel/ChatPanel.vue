<script setup lang="ts">
/**
 * 对话面板（独立窗口）— PetPal Chat Panel
 * --------------------------------------------------------------------------
 * 运行在独立的透明窗口（350×550）中，由主进程按宠物位置定位（宠物左侧，
 * 空间不足切右侧）。宠物窗口尺寸恒定、canvas 永不补偿 → 打开/关闭本面板
 * 完全不影响宠物状态（两个图层彻底解耦）。
 *
 * 职责：
 * - 消息区（对话历史）+ 输入区（ChatInputBar）+ 语音开关
 * - 模式 / 权限 / 皮肤 小卡片（按钮上方弹出）、设置、隐藏宠物开关
 * - 语音管线（VoicePipeline：8001 /ws/audio + KWS 唤醒待机）
 * - 双皮肤（深色默认 / 浅色白底黑字）：html[data-skin] token 切换
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { AuthPolicy, PetMode, Skin, VoiceUiState } from '../../../preload/types'
import ChatInputBar from '../pet-window/ChatInputBar.vue'
import { VoicePipeline } from '../../app/voice/VoicePipeline'

// ---------- 消息区（对话历史） ----------
interface ChatMessage {
  role: 'user' | 'pet'
  text: string
}

const messages = ref<ChatMessage[]>([])
// 消息区自动滚到底部（新增消息时）
const msgBox = ref<HTMLElement | null>(null)
watch(
  () => messages.value.length,
  async () => {
    await nextTick()
    if (msgBox.value) msgBox.value.scrollTop = msgBox.value.scrollHeight
  },
)

function pushMessage(role: ChatMessage['role'], text: string): void {
  messages.value = [...messages.value, { role, text }]
}

// ---------- 语音接入（8001 /ws/audio + 唤醒词待机） ----------
const wsUrl = 'ws://127.0.0.1:8001/ws/audio'
const vadBase = '/vad/'
// 实际可唤词由 KWS 模型词表决定（resources/kws/keywords.txt，默认示例「你好西西」）
const wakeKeyword = '你好西西'
let voice: VoicePipeline | null = null
const listening = ref(false) // 是否正在语音交互
// 状态提示：待机已取消——空闲即「闲聊模式」状态（唤醒词只是进入对话的开关）
const stateLabel = ref('')

/** 上报语音界面状态 → 主进程广播 → 宠物窗口消息条指示灯（idle=橙 / 对话=绿 / off=灰） */
function sendVoiceState(s: VoiceUiState): void {
  window.api.voiceState({ state: s })
}

/** 新建会话（UI 侧）：清空消息与文本对话草稿（保持窗口干净，不显示提示语） */
function uiNewSession(): void {
  messages.value = []
  window.api.pushVoicePreview('')
  draftIndex = -1
  running.value = false
  chatAudioEl?.pause()
}

/** 头部「新建会话」按钮：UI 清空 + 重连语音后端（后端按连接创建新会话） */
function newSessionClick(): void {
  window.api.notifyNewSession()
  uiNewSession()
  void voice?.newSession()
}

/** 退出应用（底部按钮栏最右侧） */
function quitApp(): void {
  window.api.quitApp()
}

async function setMicState(on: boolean, wake = false): Promise<void> {
  try {
    if (on) {
      if (!voice) {
        voice = new VoicePipeline({ wsUrl, vadAssetsBase: vadBase })
        voice.onUserText = (text) => {
          // 新一轮用户发言 → 清空消息条累计
          barAllText = ''
          barFirstRound = ''
          barRoundText = ''
          barRoundCount = 0
          if (text) pushMessage('user', text)
        }
        // 口语「新建会话」→ UI 清空（重连由 VoicePipeline.newSession() 执行）
        voice.onNewSession = () => {
          uiNewSession()
        }
        // 语音回复聚合：整段回复（reply + 多个 reply_append）只显示为一个气泡
        let voiceDraft = -1
        // 消息条文本累计：聊天=整轮之和；工作=第一轮 + 最新一轮
        let barAllText = ''
        let barFirstRound = ''
        let barRoundText = ''
        let barRoundCount = 0
        voice.onReply = (text, append) => {
          if (!text) return
          const hasDraft = voiceDraft >= 0 && voiceDraft < messages.value.length
          if (append && hasDraft) {
            const cur = messages.value[voiceDraft]
            messages.value[voiceDraft] = { ...cur, text: cur.text + text }
          } else {
            pushMessage('pet', text)
            voiceDraft = messages.value.length - 1
          }
          if (!append) {
            barRoundCount += 1
            if (barRoundCount === 1) barFirstRound = text
            barRoundText = text
          } else {
            barRoundText += text
          }
          barAllText += text
          const barText =
            currentMode.value === 'work' && barRoundCount > 1
              ? `${barFirstRound}…${barRoundText}`
              : barAllText
          window.api.pushVoicePreview(barText)
        }
        voice.onExit = () => {
          const kw = wakeKeyword || '唤醒词'
          pushMessage('pet', `好呀，先聊到这儿～ 说「${kw}」随时再叫我`)
          window.api.setPetAnim('idle')
        }
        voice.onModeChanged = (mode) => {
          window.api.switchMode(mode)
        }
        voice.onState = (s) => {
          stateLabel.value =
            s === 'idle'
              ? `闲聊模式 · 说「${wakeKeyword}」唤醒`
              : s === 'speaking'
                ? '正在回复…'
                : '聆听中…'
          listening.value = s !== 'idle'
          if (s === 'idle') sendVoiceState('idle')
          else if (s === 'speaking') sendVoiceState('speaking')
          else sendVoiceState('listening')
        }
        voice.onTtsEvent = (kind) => {
          window.api.setPetAnim(kind === 'start' ? 'speaking' : 'idle')
        }
        voice.onWake = (kw) => {
          pushMessage('pet', `(唤醒成功：${kw})`)
          sendVoiceState('listening')
        }
      }
      await voice.start(wake ? { wakeWord: true } : undefined)
      stateLabel.value = wake ? `闲聊模式 · 说「${wakeKeyword}」唤醒` : '聆听中…'
    } else {
      await voice?.stop()
      listening.value = false
      stateLabel.value = ''
      sendVoiceState('off')
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

// ---------- 文本对话（主进程网关 chat:send 流式） ----------
const running = ref(false)
const backendState = ref<'connecting' | 'connected' | 'disconnected'>('connecting')
let draftIndex = -1
let chatAudioEl: HTMLAudioElement | null = null
const animIdleTimer = ref<ReturnType<typeof setTimeout> | null>(null)

function onTextSend(text: string): void {
  pushMessage('user', text)
  running.value = true
  draftIndex = -1
  window.api.chatSend(text, currentMode.value)
}

function onAbort(): void {
  window.api.chatAbort()
}

function playChatAudio(base64Wav: string): void {
  try {
    chatAudioEl?.pause()
    chatAudioEl = new Audio(`data:audio/wav;base64,${base64Wav}`)
    void chatAudioEl.play().catch(() => {
      /* ignore */
    })
  } catch {
    /* ignore */
  }
}

const unsubs: Array<() => void> = []
unsubs.push(
  window.api.onBackendStatus((p) => {
    backendState.value = p.state
  }),
  window.api.onChatRunning((p) => {
    running.value = p.running
  }),
  window.api.onChatDelta((d) => {
    if (draftIndex < 0) {
      pushMessage('pet', '')
      draftIndex = messages.value.length - 1
    }
    const cur = messages.value[draftIndex]
    messages.value[draftIndex] = { ...cur, text: cur.text + (d.text ?? '') }
  }),
  window.api.onChatDone((d) => {
    if (draftIndex >= 0 && d.text) {
      const clean = d.text.replace(/【action:[^】]*】/g, '').trim()
      messages.value[draftIndex] = { ...messages.value[draftIndex], text: clean }
    }
    draftIndex = -1
    running.value = false
    if (d.audio) playChatAudio(d.audio)
  }),
  window.api.onTtsEvent((p) => {
    if (p.kind === 'start') {
      if (animIdleTimer.value) {
        clearTimeout(animIdleTimer.value)
        animIdleTimer.value = null
      }
      window.api.setPetAnim('speaking')
    } else {
      animIdleTimer.value = setTimeout(() => {
        animIdleTimer.value = null
        window.api.setPetAnim('idle')
      }, 1200)
    }
  })
)

// ---------- 全局状态（模式 / 权限 / 皮肤 / 宠物可见性，与主进程同步） ----------
const currentMode = ref<PetMode>('chat')
const authPolicy = ref<AuthPolicy>('ask')
const skin = ref<Skin>('dark')
const petVisible = ref(true)

let unsubModeChanged: (() => void) | null = null
let unsubSkinChanged: (() => void) | null = null
let unsubPetVisible: (() => void) | null = null

/** 皮肤应用到当前窗口（html[data-skin] token 切换，白底黑字=light） */
function applySkin(s: Skin): void {
  document.documentElement.dataset.skin = s
}

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
  try {
    skin.value = await window.api.getSkin()
  } catch {
    /* 保持默认 */
  }
  applySkin(skin.value)
  unsubSkinChanged = window.api.onSkinChanged((s) => {
    skin.value = s
    applySkin(s)
  })
  try {
    petVisible.value = await window.api.getPetVisible()
  } catch {
    /* 保持默认 */
  }
  unsubPetVisible = window.api.onPetVisibleChanged((v) => {
    petVisible.value = v
  })
  // 自动启动语音：进入即「待机听唤醒」；点 🎙 才直接对话
  void setMicState(true, true)
})

onBeforeUnmount(() => {
  unsubModeChanged?.()
  unsubSkinChanged?.()
  unsubPetVisible?.()
  for (const u of unsubs) u()
  if (animIdleTimer.value) {
    clearTimeout(animIdleTimer.value)
    animIdleTimer.value = null
  }
  void voice?.stop()
  voice = null
})

function selectMode(mode: PetMode): void {
  currentMode.value = mode
  window.api.switchMode(mode)
  // 按钮切换同步到 8001 语音后端（否则后端 mode_state 停在旧模式）
  voice?.setBackendMode(mode)
}

function selectAuth(policy: AuthPolicy): void {
  authPolicy.value = policy
  window.api.setAuthPolicy(policy)
}

function selectSkin(s: Skin): void {
  skin.value = s
  applySkin(s)
  window.api.setSkin(s)
  skinCardOpen.value = false
}

// ---------- 面板动作：关闭 / 设置 / 隐藏宠物（开关） ----------
function closePanel(): void {
  window.api.closeChatPanel()
}

function openSettings(): void {
  window.api.openPanel()
}

/** 隐藏宠物 → 开关：隐藏后可再次点击恢复显示 */
function togglePet(): void {
  window.api.setPetVisible(!petVisible.value)
}

// ---------- 模式 / 权限 / 皮肤 弹出小卡片（按钮上方弹出，衬于面板上层） ----------
const CARD_W = 250
const CARD_GAP = 6
const modeCardOpen = ref(false)
const authCardOpen = ref(false)
const skinCardOpen = ref(false)
const modeCardPos = ref({ x: 0, bottom: 0 })
const authCardPos = ref({ x: 0, bottom: 0 })
const skinCardPos = ref({ x: 0, bottom: 0 })

function cardPosAbove(btnRect: DOMRect): { x: number; bottom: number } {
  const maxX = Math.max(0, Math.min(btnRect.left, window.innerWidth - CARD_W - 8))
  const bottom = Math.max(8, window.innerHeight - btnRect.top + CARD_GAP)
  return { x: maxX, bottom }
}

/** 打开一个卡片，关闭其余 */
function openCard(kind: 'mode' | 'auth' | 'skin', ev: MouseEvent): void {
  const btn = ev.currentTarget as HTMLElement | null
  if (!btn) return
  const pos = cardPosAbove(btn.getBoundingClientRect())
  const want = kind === 'mode' ? !modeCardOpen.value : kind === 'auth' ? !authCardOpen.value : !skinCardOpen.value
  modeCardOpen.value = kind === 'mode' ? want : false
  authCardOpen.value = kind === 'auth' ? want : false
  skinCardOpen.value = kind === 'skin' ? want : false
  if (kind === 'mode' && want) modeCardPos.value = pos
  if (kind === 'auth' && want) authCardPos.value = pos
  if (kind === 'skin' && want) skinCardPos.value = pos
}

// ---------- 全局事件：ESC 关闭 ----------
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
  <!-- 面板卡片：token 化（深色=暗底浅字 / 浅色=白底黑字） -->
  <div class="absolute inset-[16px] flex flex-col overflow-hidden rounded-xl bg-surface-1 shadow-ds-lg transition-colors duration-ds-md ease-expo-out">
    <!-- 头部：40px，标题 + 状态 + 语音开关 + 关闭；header 为可拖拽区 -->
    <header
      class="flex h-10 shrink-0 items-center justify-between border-b border-line-subtle px-3"
      style="-webkit-app-region: drag"
    >
      <span class="text-[13px] font-semibold text-fg-primary">西西对话</span>
      <div class="flex items-center gap-1.5" style="-webkit-app-region: no-drag">
        <!-- 后端连接状态 -->
        <span
          class="flex h-2 w-2 rounded-full"
          :class="
            backendState === 'connected'
              ? 'bg-[#22c55e]'
              : backendState === 'connecting'
                ? 'bg-amber-400 animate-pulse'
                : 'bg-[#ef4444]'
          "
          :title="
            backendState === 'connected'
              ? '后端已连接（ws://127.0.0.1:9000）'
              : backendState === 'connecting'
                ? '正在连接后端…'
                : '后端未连接（Mock 9000 未启动）'
          "
        />
        <!-- 语音开关 -->
        <button
          type="button"
          class="flex h-6 items-center gap-1 rounded-full px-2 text-[11px] font-medium transition-colors duration-200 ease-expo-out"
          :class="listening ? 'bg-accent text-fg-inverse' : 'bg-surface-2 text-fg-secondary hover:bg-surface-3 hover:text-fg-primary'"
          :title="listening ? '停止语音' : '开始语音'"
          @click="toggleMic"
        >
          <span :class="listening ? 'animate-pulse' : ''">{{ (listening || voice?.isRunning) ? (stateLabel || '● 对话中…') : '🎙 语音' }}</span>
        </button>
        <!-- 新建会话（开启全新对话：清空上下文 + 重连语音后端） -->
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-full bg-surface-2 text-fg-secondary transition-colors duration-200 ease-expo-out hover:bg-surface-3 hover:text-fg-primary active:bg-surface-3"
          title="新建会话"
          @click="newSessionClick"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <!-- 笔记本：圆角封面 + 左书脊 -->
            <rect x="3.4" y="4.4" width="12.4" height="15.2" rx="2.4" />
            <path d="M7.2 7.2v9.6" />
            <!-- 铅笔：斜搭在本子上，笔尖朝下偏左 -->
            <path d="M18.9 4.6l1.6 1.6-6.9 6.9-2.2.6.6-2.2z" />
          </svg>
        </button>
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-full bg-surface-2 text-fg-secondary transition-colors duration-200 ease-expo-out hover:bg-surface-3 hover:text-fg-primary active:bg-surface-3"
          title="关闭面板"
          @click="closePanel"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </div>
    </header>

    <!-- 消息区 + 输入区 -->
    <div class="flex min-h-0 flex-1 flex-col">
      <div ref="msgBox" class="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        <div v-for="(m, i) in messages" :key="i" class="mb-1.5 text-[13px] leading-5">
          <template v-if="m.role === 'user'">
            <span class="font-medium text-fg-primary">你：</span><span class="text-fg-secondary">{{ m.text }}</span>
          </template>
          <template v-else>
            <span class="font-medium text-accent">西西：</span><span class="text-fg-secondary">{{ m.text }}</span>
          </template>
        </div>
      </div>

      <ChatInputBar
        :running="running"
        :pet-visible="petVisible"
        @settings="openSettings"
        @file="() => undefined"
        @mode-card="(e: MouseEvent) => openCard('mode', e)"
        @auth-card="(e: MouseEvent) => openCard('auth', e)"
        @skin-card="(e: MouseEvent) => openCard('skin', e)"
        @hide-pet="togglePet"
        @quit-app="quitApp"
        @send="onTextSend"
        @abort="onAbort"
      />
    </div>

    <!-- 模式弹出小卡片 -->
    <div
      v-show="modeCardOpen"
      data-mode-card
      class="fixed z-[10000] w-[250px] rounded-xl border border-line-subtle bg-surface-1 p-1.5 shadow-ds-lg"
      :style="{ left: `${modeCardPos.x}px`, bottom: `${modeCardPos.bottom}px` }"
    >
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="currentMode === 'chat' ? 'bg-accent/10 font-medium' : ''"
        @click="selectMode('chat')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="currentMode === 'chat' ? 'border-accent' : 'border-line-strong'"
        >
          <span v-if="currentMode === 'chat'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        聊天模式
      </button>
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="currentMode === 'work' ? 'bg-accent/10 font-medium' : ''"
        @click="selectMode('work')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="currentMode === 'work' ? 'border-accent' : 'border-line-strong'"
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
      class="fixed z-[10000] w-[250px] rounded-xl border border-line-subtle bg-surface-1 p-1.5 shadow-ds-lg"
      :style="{ left: `${authCardPos.x}px`, bottom: `${authCardPos.bottom}px` }"
    >
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="authPolicy === 'full' ? 'bg-accent/10 font-medium' : ''"
        @click="selectAuth('full')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="authPolicy === 'full' ? 'border-accent' : 'border-line-strong'"
        >
          <span v-if="authPolicy === 'full'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        完全批准
      </button>
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="authPolicy === 'ask' ? 'bg-accent/10 font-medium' : ''"
        @click="selectAuth('ask')"
      >
        <span
          class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
          :class="authPolicy === 'ask' ? 'border-accent' : 'border-line-strong'"
        >
          <span v-if="authPolicy === 'ask'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        请求批准
      </button>
    </div>

    <!-- 皮肤弹出小卡片（与模式/权限同样式） -->
    <div
      v-show="skinCardOpen"
      data-skin-card
      class="fixed z-[10000] w-[250px] rounded-xl border border-line-subtle bg-surface-1 p-1.5 shadow-ds-lg"
      :style="{ left: `${skinCardPos.x}px`, bottom: `${skinCardPos.bottom}px` }"
    >
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="skin === 'dark' ? 'bg-accent/10 font-medium' : ''"
        @click="selectSkin('dark')"
      >
        <span class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
              :class="skin === 'dark' ? 'border-accent' : 'border-line-strong'">
          <span v-if="skin === 'dark'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        深色
      </button>
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-fg-primary transition-colors duration-200 ease-expo-out hover:bg-surface-2 active:bg-surface-3"
        :class="skin === 'light' ? 'bg-accent/10 font-medium' : ''"
        @click="selectSkin('light')"
      >
        <span class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full border"
              :class="skin === 'light' ? 'border-accent' : 'border-line-strong'">
          <span v-if="skin === 'light'" class="h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        浅色
      </button>
    </div>
  </div>
</template>