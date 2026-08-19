<script setup lang="ts">
/**
 * 悬浮宠物主窗口（画布化）
 * --------------------------------------------------------------------------
 * - DOM 极简：仅保留 canvas 画布元素（无外层包装 div）；
 *   canvas 固定 220×240，任何点击/面板开合逻辑禁止修改其宽高
 * - 透明画布：只绘制球体图形，非图形区域像素全透明（行为类似 PNG，
 *   无黑色背景）；后续可直接替换为 PNG 序列帧 / 视频帧（见 pet-canvas.ts）
 * - 交互：
 *   · 鼠标右键点击画布 → toggle 悬浮设置面板（阻止原生右键菜单）
 *   · 左键按住拖动 → 窗口移动（左键单击不再唤起面板，留给语音交互）
 * - 悬浮设置面板挂载 document.body（Teleport），不嵌套在画布内，
 *   右键时从球体右上角弹出，完全不受画布约束
 * - 订阅 TTS 事件 → 调用 hooks 预留钩子
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import ContextCard from './ContextCard.vue'
import { initPetCanvas } from './pet-canvas'
import petPhotoUrl from '../../assets/pet-photo.png'
import { onTtsEnd, onTtsStart } from './hooks'

const canvasRef = ref<HTMLCanvasElement | null>(null)
const contextCardRef = ref<InstanceType<typeof ContextCard> | null>(null)

// ---------- 动画状态预留（不实现播放逻辑） ----------
// TODO: 后续迭代实现 — 素材帧动画驱动（见 pet-canvas.ts 帧源接口）
type PetAnimState = 'idle' | 'speaking' | 'happy' | 'sleeping'
const animState = ref<PetAnimState>('idle')
// TODO: 后续迭代实现 — 帧序列切换 / 视频帧绘制 / 状态机驱动 / 语音能量 setEnergy

// ---------- TTS 钩子订阅 ----------
let unsubTtsStart: (() => void) | null = null
let unsubTtsEnd: (() => void) | null = null

// ---------- 左键拖拽（rAF 帧合并，修复拖动闪烁） ----------
const DRAG_THRESHOLD_PX = 5

interface PressPoint {
  screenX: number
  screenY: number
}

let pressPoint: PressPoint | null = null
let dragged = false
// 帧合并：mousemove 全部接收但只保存目标坐标，rAF 每帧合并发送一次 dragMove
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
  // 位移超过阈值才判定为拖拽（区分单击与拖动）
  if (!dragged) {
    const dx = e.screenX - pressPoint.screenX
    const dy = e.screenY - pressPoint.screenY
    if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return
    dragged = true
    window.api.dragStart(pressPoint)
    rafLoop()
  }
  // 仅保存目标坐标，不直接发送；同一帧多次移动由 rAF 合并为一次渲染
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

// ---------- 右键画布 → toggle 悬浮设置面板 ----------
function onCanvasContextMenu(e: MouseEvent): void {
  e.preventDefault()
  void contextCardRef.value?.toggle()
}

// 全局阻止浏览器原生右键菜单（画布外的透明区域同样不弹菜单）
function onDocumentContextMenu(e: MouseEvent): void {
  e.preventDefault()
}

// ---------- 宠物可见性（隐藏后只能从控制面板重新开启） ----------
const petVisible = ref(true)
let unsubPetVisible: (() => void) | null = null

onMounted(() => {
  // 初始化透明画布（固定尺寸 + 加载宠物照片绘制）
  if (canvasRef.value) {
    initPetCanvas(canvasRef.value, petPhotoUrl)
  }

  unsubTtsStart = window.api.onTtsStart(() => {
    animState.value = 'speaking'
    onTtsStart()
  })
  unsubTtsEnd = window.api.onTtsEnd(() => {
    animState.value = 'idle'
    onTtsEnd()
  })
  // 主进程可见性广播 → 显示/隐藏球体画布（上下文面板不受影响）
  unsubPetVisible = window.api.onPetVisibleChanged((visible) => {
    petVisible.value = visible
  })

  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
  window.addEventListener('blur', onMouseUp)
  document.addEventListener('contextmenu', onDocumentContextMenu)
})

onBeforeUnmount(() => {
  unsubTtsStart?.()
  unsubTtsEnd?.()
  unsubPetVisible?.()
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  window.removeEventListener('blur', onMouseUp)
  document.removeEventListener('contextmenu', onDocumentContextMenu)
})
</script>

<template>
  <!-- 仅保留 canvas 画布（透明，行为类似 PNG；无外层包装 div）。
       CSS 尺寸固定（220×240）；位置由 ContextCard 按面板方向动态控制
       （默认 0,0，面板在球体左/上侧时移到窗口右/下侧，球体屏幕位置不变） -->
  <canvas
    v-show="petVisible"
    ref="canvasRef"
    class="fixed h-[240px] w-[220px] select-none"
    style="left: 0; top: 0"
    @mousedown="onMouseDown"
    @contextmenu="onCanvasContextMenu"
  />

  <!-- 上下文对话面板：挂载 document.body（Teleport），不在画布内 -->
  <ContextCard ref="contextCardRef" />
</template>
