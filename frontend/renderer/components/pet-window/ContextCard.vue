<script setup lang="ts">
/**
 * 上下文对话面板（350×550 固定尺寸，球体左上角）
 * --------------------------------------------------------------------------
 * - 定位：整体固定在球体左上角——left = ball.left - 350 - 8、top = ball.top；
 *   边界：① 左侧超视口 → 切球体右侧；② 顶部越界 → 切球体下方
 * - 挂载 document.body 直接子节点（Teleport），position:absolute + z-index:9999，
 *   完全不受 canvas 画布约束；窗口扩展为 578×662 容纳「球体 + 面板」
 * - 布局（350×550）：
 *   头部 40px（标题 + 最右侧「关闭面板」按钮，仅关闭本面板不退出程序）
 *   消息区（滚动，与输入框共享 479px 总空间，13px）
 *   分割线 1px
 *   输入区（textarea 自适应 120→220 + 固定 30px 按钮栏）
 * - 底部按钮栏：设置（齿轮→控制面板）、文件、模式（弹出 250×100 小卡片）、
 *   权限（弹出 250×100 小卡片）、隐藏宠物（只隐藏球体，面板保持显示；
 *   只能从控制面板重新开启）
 * - 模式/权限弹出小卡片浮在面板上层（z-index 更高），不受面板裁剪，点击空白关闭
 * - 关闭面板仅 v-show 隐藏（不移动坐标、无过渡闪现）；打开时两阶段显示
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { AuthPolicy, PetMode } from '../../../preload/types'
import { getBallCenterInCanvas, getBallTopLeftInCanvas, getPetSpriteSize } from './pet-canvas'
import ChatInputBar from './ChatInputBar.vue'
import { VoicePipeline } from '../../app/voice/VoicePipeline'

const SAFE_MARGIN = 8
const BALL_GAP = 8
const PANEL_WIDTH = 350
const PANEL_HEIGHT = 550

const visible = ref(false)
const shown = ref(false)
const panelRef = ref<HTMLElement | null>(null)
const posX = ref(0)
const posY = ref(0)

const raf = (): Promise<void> => new Promise((resolve) => requestAnimationFrame(() => resolve()))

// ---------- 消息区（对话历史） ----------
interface ChatMessage {
  role: 'user' | 'pet'
  text: string
}

const messages = ref<ChatMessage[]>([
  { role: 'pet', text: '你好呀，我是球球！有什么想聊的？' }
])
// TODO: 后续迭代实现 — 接入语音/LLM 链路，将 ASR 文本与 LLM 回复追加到对话历史
function pushMessage(role: ChatMessage['role'], text: string): void {
  messages.value = [...messages.value, { role, text }]
}

// ---------- 语音接入（后端职责提供：连真实后端 8001 /ws/audio + 唤醒词待机） ----------
const wsUrl = 'ws://127.0.0.1:8001/ws/audio'
const vadBase = '/vad/'
// 唤醒词展示文本（实际可唤词由 KWS 模型词表决定，见 resources/kws/keywords.txt；
// 默认示例「你好西西」在词表内。可改成：小米小米 / 小爱同学 / 你好军哥 / 你好问问 / 小艺小艺 / 蛋哥蛋哥 / 林美丽）
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
          if (text) pushMessage('pet', text)
        }
        voice.onState = (s) => {
          // 唤醒模式：idle=待机、listening=正在对话、speaking=回复中
          stateLabel.value =
            s === 'idle' ? `待机中，说「${wakeKeyword}」唤醒` : s === 'speaking' ? '正在回复…' : '聆听中…'
          listening.value = s !== 'idle'
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
  // 手动点按钮 → 直接进入（或退出）语音对话
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
    // 主进程不可达时保持默认
  }
  // ASR 语音切换等主进程侧改动 → 广播回推 → UI 自动同步
  unsubModeChanged = window.api.onModeChanged((mode) => {
    currentMode.value = mode
  })
  try {
    authPolicy.value = await window.api.getAuthPolicy()
  } catch {
    // 保持默认
  }
  // 自动启动语音：启动即进入「待机听唤醒」；点头部 🎙 按钮才直接对话
  void setMicState(true, true)
})

onBeforeUnmount(() => {
  unsubModeChanged?.()
})

function selectMode(mode: PetMode): void {
  currentMode.value = mode
  window.api.switchMode(mode)
}

function selectAuth(policy: AuthPolicy): void {
  authPolicy.value = policy
  window.api.setAuthPolicy(policy)
}

// ---------- 打开 / 关闭 / 切换（宠物与面板两套独立体系：宠物绝对不动） ----------
async function open(): Promise<void> {
  visible.value = true
  shown.value = false // 先隐藏，定位完成后再可见（不闪旧坐标帧）
  await nextTick()
  await raf()
  // 宠物屏幕坐标必须在窗口扩展前采集（canvas 0,0 时宠物锚点是稳定值）
  const br = getBallRect()
  ballScreenRef = { x: window.screenX + br.left, y: window.screenY + br.top }
  await syncPanelSize()
  computePosition()
  shown.value = true
}

async function toggle(): Promise<void> {
  if (visible.value) {
    await close()
  } else {
    await open()
  }
}

// ---------- 宠物与面板两套体系：宠物坐标锚定（面板打开/关闭宠物绝对不动） ----------
let ballScreenRef = { x: 0, y: 0 } // 宠物屏幕坐标（锚定，面板开合不变）
let lastWinPos: { x: number; y: number } | null = null // 最近一次窗口位置
let restoreTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 应用 canvas 位置：窗口扩展/恢复时补偿，保证宠物屏幕坐标恒定
 * （宠物是独立体系：无论窗口如何移动，宠物钉在屏幕同一坐标）
 */
function applyCanvasPosition(): void {
  const canvas = document.querySelector('canvas')
  if (!canvas || !lastWinPos) return
  const tl = getBallTopLeftInCanvas()
  // 整数坐标消除亚像素抖动（宠物屏幕位置精确恒定）
  canvas.style.left = `${Math.round(ballScreenRef.x - lastWinPos.x - tl.x)}px`
  canvas.style.top = `${Math.round(ballScreenRef.y - lastWinPos.y - tl.y)}px`
}

/**
 * 宠物精灵屏幕坐标（视口坐标系）：canvas 220×240 位置由窗口补偿决定，
 * 精灵在画布内固定（底部居中），尺寸/锚点读取 pet-canvas 单一事实源
 */
function getBallRect(): DOMRect {
  const canvas = document.querySelector('canvas')
  const cr = canvas?.getBoundingClientRect()
  const c = getBallCenterInCanvas()
  const s = getPetSpriteSize()
  return new DOMRect(
    (cr?.left ?? 0) + c.x - s.width / 2,
    (cr?.top ?? 0) + c.y - s.height / 2,
    s.width,
    s.height
  )
}

/**
 * 同步窗口尺寸（面板 350×550 固定宠物左侧 → 窗口 578×566）
 * - 打开面板：先按预测窗口位置设置 canvas 补偿（与主进程公式一致），
 *   再发起窗口扩展（主进程 async await 窗口 resize/move 事件、返回真实
 *   bounds）→ 用真实位置校正（clamp 差异）。全程无盲 sleep，等窗口真正到位。
 * - 宠物屏幕坐标恒定（两套独立体系，宠物不动）
 */
async function syncPanelSize(): Promise<void> {
  try {
    const tl = getBallTopLeftInCanvas()
    // 预测窗口位置（无 clamp 公式，与主进程一致：面板固定宠物左侧，x 左移 358）
    const predPos = { x: ballScreenRef.x - tl.x - 358, y: ballScreenRef.y - tl.y }
    // 提前补偿 canvas（窗口到位时宠物屏幕位置即正确）
    lastWinPos = predPos
    applyCanvasPosition()
    const predCanvas = canvasPosOf(predPos, tl)
    console.log(`[pet] predict win=(${predPos.x},${predPos.y}) canvas=(${predCanvas.x},${predCanvas.y})`)
    // 主进程 async 等到窗口 resize/move 事件后返回真实 bounds
    const winPos = await window.api.setPanelHeight(PANEL_HEIGHT, ballScreenRef, tl)
    lastWinPos = winPos ?? predPos
    applyCanvasPosition()
    const corrCanvas = canvasPosOf(lastWinPos, tl)
    console.log(`[pet] correct win=(${lastWinPos.x},${lastWinPos.y}) canvas=(${corrCanvas.x},${corrCanvas.y})`)
  } catch {
    // 主进程不可达：窗口未扩展，面板可能被窗口裁切，仍尝试定位
  }
}

/** 计算某窗口位置下 canvas 的补偿坐标（仅 for 日志） */
function canvasPosOf(winPos: { x: number; y: number }, tl: { x: number; y: number }): { x: number; y: number } {
  return {
    x: Math.round(ballScreenRef.x - winPos.x - tl.x),
    y: Math.round(ballScreenRef.y - winPos.y - tl.y)
  }
}

/**
 * 关闭面板：只做隐藏 + 恢复窗口
 * - 先隐藏面板（v-show display:none），不修改 top/left、无中间过渡
 * - 异步恢复窗口（主进程等到位后返回）→ 再归位 canvas ：
 *   窗口已恢复为 220×240 时归位 canvas（0,0），宠物回到窗口左侧初始位置，
 *   屏幕坐标全程不变
 */
async function close(): Promise<void> {
  if (!visible.value) return
  visible.value = false
  shown.value = false
  if (restoreTimer) {
    clearTimeout(restoreTimer)
    restoreTimer = null
  }
  // 恢复窗口（主进程 async 等到位）
  await window.api.setPanelHeight(0).catch(() => undefined)
  // 窗口已恢复 → 归位 canvas；宠物屏幕位置 = 恢复窗口 + 窗口内偏移 = 打开前位置
  const canvas = document.querySelector('canvas')
  if (canvas) {
    canvas.style.left = '0px'
    canvas.style.top = '0px'
  }
  ballScreenRef = { x: 0, y: 0 }
  lastWinPos = null
}

/**
 * 面板定位：固定在宠物左侧（游离于宠物图片范围之外，不与宠物重叠）
 * - left = 宠物左缘 - 面板宽 - 间距
 * - top = 垂直居中对齐宠物中心
 * - 若屏幕左侧空间不足则面板右移到宠物右侧（仍不重叠）
 */
function computePosition(): void {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const br = getBallRect()

  let left = br.left - PANEL_WIDTH - BALL_GAP
  let top = br.top + br.height / 2 - PANEL_HEIGHT / 2
  // 左侧空间不足 → 面板切换到宠物右侧弹出（游离于宠物图片之外）
  if (left < SAFE_MARGIN) {
    left = br.right + BALL_GAP
  }
  // 兜底 clamp 到安全边距（保证面板完整显示在视口内，不与宠物重叠）
  left = Math.max(SAFE_MARGIN, Math.min(left, vw - PANEL_WIDTH - SAFE_MARGIN))
  top = Math.max(SAFE_MARGIN, Math.min(top, vh - PANEL_HEIGHT - SAFE_MARGIN))

  posX.value = Math.round(left)
  posY.value = Math.round(top)
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

// ---------- 设置 / 隐藏宠物 ----------
function openSettings(): void {
  window.api.openPanel()
}

function hidePet(): void {
  // 隐藏球体（上下文面板保持显示）；只能从控制面板重新开启
  window.api.setPetVisible(false)
}

// ---------- 全局事件：document 点击外部关闭 / ESC 关闭 ----------
function onPointerDown(e: PointerEvent): void {
  if (e.button === 2) return // 右键由 PetWindow 的 contextmenu toggle 处理
  const target = e.target as Element | null
  // e.target 可能是 document（非 Element，无 closest 方法）
  if (!target || typeof target.closest !== 'function') return

  // 模式 / 权限弹出卡片：点击卡片外部关闭
  if (modeCardOpen.value && !target.closest('[data-mode-card]')) {
    modeCardOpen.value = false
  }
  if (authCardOpen.value && !target.closest('[data-auth-card]')) {
    authCardOpen.value = false
  }

  // 面板：点击外部关闭
  if (visible.value && panelRef.value && !panelRef.value.contains(target)) {
    close()
  }
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && visible.value) {
    close()
  }
}

onMounted(() => {
  document.addEventListener('pointerdown', onPointerDown)
  document.addEventListener('keydown', onKeyDown)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onPointerDown)
  document.removeEventListener('keydown', onKeyDown)
  void voice?.stop()
  voice = null
  if (restoreTimer) {
    clearTimeout(restoreTimer)
    restoreTimer = null
  }
})

defineExpose({ open, close, toggle, pushMessage })
</script>

<template>
  <Teleport to="body">
    <!-- 上下文对话面板：350×550 固定，球体左上角 -->
    <div
      v-show="visible"
      ref="panelRef"
      data-context-card
      class="absolute z-[9999] flex w-[350px] flex-col overflow-hidden rounded-xl bg-white shadow-[0_4px_16px_rgba(0,0,0,0.15)]"
      :style="{
        left: `${posX}px`,
        top: `${posY}px`,
        width: `${PANEL_WIDTH}px`,
        height: `${PANEL_HEIGHT}px`,
        visibility: shown ? 'visible' : 'hidden'
      }"
    >
      <!-- 头部：40px，标题 + 最右侧关闭面板按钮 -->
      <header class="flex h-10 shrink-0 items-center justify-between border-b border-black/[0.06] px-3">
        <span class="text-[13px] font-semibold text-[#1a1a1a]">球球对话</span>
        <div class="flex items-center gap-1.5">
          <!-- 语音开关：开始/停止语音对话（后端职责提供） -->
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
            @click="close"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </div>
      </header>

      <!-- 消息区 + 输入区（flex 自适应共享剩余空间） -->
      <div class="flex min-h-0 flex-1 flex-col">
        <!-- ① 消息区：对话历史滚动容器（flex-1，被输入框扩张自动压缩） -->
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

        <!-- ② 输入区域：左右留白 3px + 阴影层级区分（整体卡片底色统一白色，无分割线） -->
        <ChatInputBar
          @settings="openSettings"
          @file="() => undefined"
          @mode-card="toggleModeCard"
          @auth-card="toggleAuthCard"
          @hide-pet="hidePet"
        />
      </div>
    </div>

    <!-- 模式弹出小卡片（250×100，浮面板上层，不受面板裁剪） -->
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

    <!-- 权限弹出小卡片（250×100） -->
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
  </Teleport>
</template>
