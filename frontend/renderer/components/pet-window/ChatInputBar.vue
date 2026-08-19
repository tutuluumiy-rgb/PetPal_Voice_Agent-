<script setup lang="ts">
/**
 * 输入区域：自适应文本输入框 + 固定底部按钮栏
 * --------------------------------------------------------------------------
 * - 文本输入框：初始高 120px，内容变多向上自动放大，最大 220px（超出内部滚动）；
 *   向上扩张时挤压消息区可用高度（由父级 flex-1 布局自动完成）
 * - 底部按钮栏：固定 30px、位置锁定贴面板底，不跟随输入滚动
 * - 图标统一 18px、内边距缩小，保证全部按钮横向排布不换行
 */
import { ref } from 'vue'

const MIN_H = 100
const MAX_H = 220

const message = ref('')
const textareaRef = ref<HTMLTextAreaElement | null>(null)

const emit = defineEmits<{
  (e: 'settings', ev: MouseEvent): void
  (e: 'file'): void
  (e: 'mode-card', ev: MouseEvent): void
  (e: 'auth-card', ev: MouseEvent): void
  (e: 'hide-pet'): void
}>()

/** textarea 自适应：高度 auto → 测量 scrollHeight → clamp 120~220 */
function autoResize(): void {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  const h = Math.min(Math.max(el.scrollHeight, MIN_H), MAX_H)
  el.style.height = `${h}px`
}

/** 发送：TODO 后续迭代实现 — 走主进程语音/LLM 链路 */
function send(): void {
  // TODO: 后续迭代实现 — 发送消息到主进程（WebSocket / LLM），追加到对话历史
  const text = message.value.trim()
  if (!text) return
  void text
}

/** 中止：模型运行中支持中止当前生成/TTS（图标为灰色小方块） */
function abort(): void {
  // TODO: 后续迭代实现 — 中止当前 LLM 请求 / TTS 播放
}

/**
 * 模型是否运行中（驱动右下角发送 icon 变为灰色小方块中止）。
 * TODO: 后续迭代实现 — 由主进程"模型开始/结束"事件驱动本状态
 */
const isRunning = ref(false)

/** 右下角按钮：运行中 → 中止；否则 → 发送（Enter 键发送） */
function onSendClick(): void {
  if (isRunning.value) {
    abort()
  } else {
    send()
  }
}

/** 添加文件：TODO 后续迭代实现 — 文件选择器 + 附件上传 */
function addFile(): void {
  // TODO: 后续迭代实现 — 附件选择与发送
  emit('file')
}
</script>

<template>
  <!-- 输入区域：左右两侧留白 3px，用阴影做层级区分（整体卡片底色统一白色，无背景色块/分割线） -->
  <div class="w-full shrink-0 px-[3px]">
    <div class="flex w-full flex-col rounded-lg shadow-[0_-1px_6px_rgba(0,0,0,0.06)]">
      <!-- 文本输入框：初始 100px，向上自动放大，最大 220px 内部滚动（底色统一白色） -->
      <div class="relative">
        <textarea
          ref="textareaRef"
          v-model="message"
          rows="4"
          placeholder="输入消息…"
          class="w-full resize-none overflow-y-auto rounded-lg border border-black/[0.06] bg-white px-3 pb-8 pt-2 pr-10 text-[13px] leading-5 text-[#1a1a1a] placeholder:text-black/35 outline-none transition-colors duration-200 ease-expo-out focus:border-accent/70"
          :style="{ height: `${MIN_H}px`, maxHeight: `${MAX_H}px` }"
          @input="autoResize"
          @keydown.enter.exact.prevent="onSendClick"
        />

        <!-- 右下角发送 / 中止按钮：运行中 → 灰色小方块（中止）；否则 → 发送箭头（Enter 发送） -->
        <button
          type="button"
          class="absolute bottom-2 right-2 flex h-7 w-7 items-center justify-center rounded-full transition-all duration-200 ease-expo-out"
          :class="
            isRunning
              ? 'bg-black/[0.12] text-[#9a9a9a] hover:bg-black/[0.2]'
              : 'bg-accent text-white hover:bg-accent-hover active:brightness-95 disabled:opacity-40'
          "
          :title="isRunning ? '中止' : '发送（Enter）'"
          :disabled="!isRunning && !message.trim()"
          @click="onSendClick"
        >
          <!-- 运行中：灰色小方块（中止） -->
          <svg v-if="isRunning" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <rect x="2" y="2" width="8" height="8" rx="1" fill="currentColor" />
          </svg>
          <!-- 发送：向上箭头 -->
          <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      </div>

      <!-- 底部按钮栏：固定 30px，贴面板底不随输入滚动 -->
      <div class="flex h-[30px] w-full shrink-0 items-center justify-between px-1.5">
        <!-- 左侧按钮组 -->
        <div class="flex items-center gap-0.5">
          <!-- 设置（齿轮，线条风格细线图标）→ 打开独立控制面板 -->
          <button
            type="button"
            class="flex h-6 w-6 items-center justify-center rounded-md text-[#6b6b6b] transition-colors duration-200 ease-expo-out hover:bg-black/[0.05] hover:text-[#1a1a1a] active:bg-black/[0.08]"
            title="设置"
            @click="emit('settings', $event)"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z"
                stroke="currentColor"
                stroke-width="1.5"
              />
              <path
                d="M19.4 13.5a7.7 7.7 0 0 0 .1-1.5 7.7 7.7 0 0 0-.1-1.5l2-1.6-2-3.3-2.4 1a7.7 7.7 0 0 0-2.6-1.5L14 2.8h-4l-.4 2.3a7.7 7.7 0 0 0-2.6 1.5l-2.4-1-2 3.3 2 1.6a7.7 7.7 0 0 0 0 3L2.6 15l2 3.3 2.4-1a7.7 7.7 0 0 0 2.6 1.5l.4 2.4h4l.4-2.4a7.7 7.7 0 0 0 2.6-1.5l2.4 1 2-3.3-2-1.6Z"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
            </svg>
          </button>

        <!-- 文件（+） -->
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-md text-[#6b6b6b] transition-colors duration-200 ease-expo-out hover:bg-black/[0.05] hover:text-[#1a1a1a] active:bg-black/[0.08]"
          title="添加文件"
          @click="addFile"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </button>

        <!-- 模式切换（弹出小卡片） -->
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-md text-[#6b6b6b] transition-colors duration-200 ease-expo-out hover:bg-black/[0.05] hover:text-[#1a1a1a] active:bg-black/[0.08]"
          title="模式切换"
          @click="emit('mode-card', $event)"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8" />
            <path d="M12 4v16M4 12h16" stroke="currentColor" stroke-width="1.8" />
          </svg>
        </button>

        <!-- 权限（弹出小卡片） -->
        <button
          type="button"
          class="flex h-6 w-6 items-center justify-center rounded-md text-[#6b6b6b] transition-colors duration-200 ease-expo-out hover:bg-black/[0.05] hover:text-[#1a1a1a] active:bg-black/[0.08]"
          title="权限"
          @click="emit('auth-card', $event)"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <rect x="5" y="10" width="14" height="10" rx="2" stroke="currentColor" stroke-width="1.8" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" stroke="currentColor" stroke-width="1.8" />
          </svg>
        </button>
      </div>

      <!-- 最右侧：隐藏宠物 -->
      <button
        type="button"
        class="flex h-6 w-6 items-center justify-center rounded-md text-[#6b6b6b] transition-colors duration-200 ease-expo-out hover:bg-black/[0.05] hover:text-[#1a1a1a] active:bg-black/[0.08]"
        title="隐藏宠物（可在控制面板重新开启）"
        @click="emit('hide-pet')"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"
            stroke="currentColor"
            stroke-width="1.8"
          />
          <circle cx="12" cy="12" r="2.5" stroke="currentColor" stroke-width="1.8" />
          <path d="M3 3l18 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
      </button>
      </div>
    </div>
  </div>
</template>
