<script setup lang="ts">
/**
 * 悬浮宠物主窗口（画布化，尺寸恒定 220×240）
 * --------------------------------------------------------------------------
 * - DOM 极简：canvas 固定 220×240（尺寸由 pet-canvas.ts 锁死），透明。
 *   另加一个覆盖在底部的「语音播报消息条」小浮层（不影响 canvas 拖拽/右键）。
 * - 交互：
 *   · 左键按住拖动 → 移动宠物窗口（rAF 帧合并，防闪烁）
 *   · 鼠标右键点击画布 → 打开/聚焦【独立对话面板窗口】
 *   · 点击底部消息条 → 展开查看本次/实时的语音播报内容（可滚动），再点收起
 * - 底部消息条：交付占位 + 预留 —— 未来用于滚动播报实时语音文本（经 IPC
 *   voice-preview 由主进程广播喂入），字体与对话面板回复区正文一致。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { startPetAnimation, PET_CANVAS_SIZE, BALL_BOTTOM_PADDING } from './pet-canvas'
import type { PetAnimator } from './anim/PetAnimator'
import petPhotoUrl from '../../assets/pet-photo.png'
import { onTtsEnd, onTtsStart } from './hooks'

const canvasRef = ref<HTMLCanvasElement | null>(null)

// 消息条作为【独立图层】：固定在宠物图片下方 30px（单一事实源，随画布尺寸自动同步）
const VOICE_BAR_GAP = 30
const voiceBarTop = PET_CANVAS_SIZE.height - BALL_BOTTOM_PADDING + VOICE_BAR_GAP

// ---------- 动画播放器实例（由 startPetAnimation 创建并启动） ----------
let animator: PetAnimator | null = null

// ---------- 语音播报消息条（底部独立图层，单行滚动播报最新语音） ----------
// 恒定显示最新一条播报文本，横向滚动播放；点击该条打开上下文对话面板。
const voiceText = ref('') // 最新一条播报文本

// ---------- TTS / 语音状态 / 模式 / 可见性 订阅 ----------
let unsubTtsStart: (() => void) | null = null
let unsubTtsEnd: (() => void) | null = null
let unsubPetAnim: (() => void) | null = null
let unsubMode: (() => void) | null = null
let onVisibilityChange: (() => void) | null = null

// ---------- 左键拖拽（rAF 帧合并，修复拖动闪烁） ----------
const DRAG_THRESHOLD_PX = 5

interface PressPoint {
  screenX: number
  screenY: number
}

let pressPoint: PressPoint | null = null
let dragged = false
let pendingTarget: { screenX: number; screenY: number } | null = null
let rafId: number | null = null

function onMouseDown(e: MouseEvent): void {
  if (e.button !== 0) return
  pressPoint = { screenX: e.screenX, screenY: e.screenY }
  dragged = false
  e.preventDefault()
}

function onMouseMove(e: MouseEvent): void {
  if (!pressPoint) return
  if (!dragged) {
    const dx = e.screenX - pressPoint.screenX
    const dy = e.screenY - pressPoint.screenY
    if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return
    dragged = true
    window.api.dragStart(pressPoint)
    rafLoop()
  }
  pendingTarget = { screenX: e.screenX, screenY: e.screenY }
}

/** rAF 帧合并循环：每帧至多发送一次 dragMove */
function rafLoop(): void {
  if (!dragged) return
  if (pendingTarget) {
    window.api.dragMove(pendingTarget)
    pendingTarget = null
  }
  rafId = requestAnimationFrame(rafLoop)
}

function onMouseUp(): void {
  if (dragged) {
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
    }
    rafId = null
    pendingTarget = null
    window.api.dragEnd()
  }
  pressPoint = null
  dragged = false
}

// ---------- 右键画布 → 打开/聚焦独立对话面板窗口 ----------
function onCanvasContextMenu(e: MouseEvent): void {
  e.preventDefault()
  window.api.openChatPanel()
}

/** 点击底部语音消息条 → 打开上下文对话面板 */
function openContextPanel(): void {
  window.api.openChatPanel()
}

// 全局阻止浏览器原生右键菜单（画布外的透明区域同样不弹菜单）
function onDocumentContextMenu(e: MouseEvent): void {
  e.preventDefault()
}

// ---------- 宠物可见性（隐藏后只能从控制面板重新开启） ----------
const petVisible = ref(true)
let unsubPetVisible: (() => void) | null = null

// ---------- 语音播报文本接收 ----------
let unsubVoicePreview: (() => void) | null = null

onMounted(() => {
  if (canvasRef.value) {
    animator = startPetAnimation(canvasRef.value, petPhotoUrl)
  }

  // TTS（主进程预留通道）→ 动画说话态
  unsubTtsStart = window.api.onTtsStart(() => {
    animator?.feedTts(true)
    onTtsStart()
  })
  unsubTtsEnd = window.api.onTtsEnd(() => {
    animator?.feedTts(false)
    onTtsEnd()
  })
  unsubPetVisible = window.api.onPetVisibleChanged((visible) => {
    petVisible.value = visible
    // 隐藏时暂停动画（不空转），显示时恢复
    if (visible) animator?.resume()
    else animator?.pause()
  })

  // 三窗口语音状态（对话面板说/停）→ 动画
  unsubPetAnim = window.api.onPetAnimChanged((s) => {
    if (s === 'speaking') animator?.feedTts(true)
    else if (s === 'idle') animator?.feedTts(false)
  })

  // 模式（聊天/工作）→ 动画基底
  unsubMode = window.api.onModeChanged((mode) => {
    animator?.setMode(mode)
  })

  // 实时语音播报：主进程广播 → 消息条单行展示并滚动最新一条
  unsubVoicePreview = window.api.onVoicePreview((text) => {
    voiceText.value = text
  })

  // 挂载回放「当次」最近一条播报
  window.api.getVoicePreview().then((t) => {
    if (t) voiceText.value = t
  }).catch(() => undefined)

  // 窗口隐藏（最小化/切走）时暂停动画
  onVisibilityChange = (): void => {
    if (document.hidden) animator?.pause()
    else animator?.resume()
  }
  document.addEventListener('visibilitychange', onVisibilityChange)

  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
  window.addEventListener('blur', onMouseUp)
  document.addEventListener('contextmenu', onDocumentContextMenu)
})

onBeforeUnmount(() => {
  unsubTtsStart?.()
  unsubTtsEnd?.()
  unsubPetVisible?.()
  unsubVoicePreview?.()
  unsubPetAnim?.()
  unsubMode?.()
  if (onVisibilityChange) {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    onVisibilityChange = null
  }
  animator?.stop()
  animator = null
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  window.removeEventListener('blur', onMouseUp)
  document.removeEventListener('contextmenu', onDocumentContextMenu)
})
</script>

<template>
  <!-- canvas 画布（透明，恒定 220×280，无需补偿） -->
  <canvas
    v-show="petVisible"
    ref="canvasRef"
    class="fixed h-[280px] w-[220px] select-none"
    style="left: 0; top: 0"
    @mousedown="onMouseDown"
    @contextmenu="onCanvasContextMenu"
  />

  <!-- 底部语音播报消息条（独立图层，固定 30px，横向滚动播报最新语音）：点击打开上下文面板 -->
  <div
    v-show="petVisible"
    class="absolute left-1/2 w-[200px] -translate-x-1/2 cursor-pointer select-none rounded-[8px] bg-white shadow-[0_2px_8px_rgba(0,0,0,0.18)]"
    :style="{ top: `${voiceBarTop}px`, height: '30px' }"
    @mousedown.stop
    @contextmenu.prevent
    @click.stop="openContextPanel"
  >
    <div class="flex h-[30px] w-full items-center gap-1.5 overflow-hidden px-2.5">
      <span class="shrink-0 text-[11px]">🎙</span>
      <!-- 横向滚动播报最新语音文本 -->
      <div class="relative h-[30px] min-w-0 flex-1 overflow-hidden">
        <p class="anim-marquee absolute inset-y-0 left-0 whitespace-nowrap text-[13px] leading-[30px] text-[#333]">
          {{ voiceText || '语音播报中…' }}
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 语音播报消息条：横向滚动播放最新文本 */
@keyframes pet-marquee {
  0% {
    transform: translateX(0);
  }
  100% {
    transform: translateX(-100%);
  }
}
.anim-marquee {
  animation: pet-marquee 8s linear infinite;
}
</style>

