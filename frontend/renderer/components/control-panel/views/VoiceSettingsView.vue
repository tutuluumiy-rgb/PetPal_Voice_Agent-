<script setup lang="ts">
/**
 * 语音参数设置 — UI 骨架占位
 * 音量滑块 / 音调滑块 / 音色选择下拉
 * TODO: 后续迭代实现 — 参数下发主进程 TTS 引擎
 */
import { ref } from 'vue'
import PageCard from '../PageCard.vue'

const volume = ref(80)
const pitch = ref(50)
const voice = ref('default')

const VOICE_OPTIONS = [
  { value: 'default', label: '默认音色' },
  { value: 'cute', label: '软萌音' },
  { value: 'calm', label: '沉稳音' },
  { value: 'bright', label: '明亮音' }
]

// TODO: 后续迭代实现 — 保存参数并经 IPC 下发 TTS 引擎
function saveSettings(): void {
  void { volume: volume.value, pitch: pitch.value, voice: voice.value }
  // TODO: 后续迭代实现 — IPC 持久化 + 生效
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="语音参数设置" description="调整球球的发音音量、音调与音色">
      <!-- 音量 -->
      <div class="flex flex-col gap-1.5">
        <div class="flex items-center justify-between">
          <label class="text-[13px] text-fg-secondary">音量</label>
          <span class="font-mono text-[11px] text-fg-muted">{{ volume }}%</span>
        </div>
        <input
          v-model.number="volume"
          type="range"
          min="0"
          max="100"
          class="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-accent"
        />
      </div>

      <!-- 音调 -->
      <div class="mt-4 flex flex-col gap-1.5">
        <div class="flex items-center justify-between">
          <label class="text-[13px] text-fg-secondary">音调</label>
          <span class="font-mono text-[11px] text-fg-muted">{{ pitch }}%</span>
        </div>
        <input
          v-model.number="pitch"
          type="range"
          min="0"
          max="100"
          class="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-accent"
        />
      </div>

      <!-- 音色 -->
      <div class="mt-4 flex flex-col gap-1.5">
        <label class="text-[13px] text-fg-secondary">音色</label>
        <select
          v-model="voice"
          class="h-8 w-full max-w-56 rounded-md border border-line-subtle bg-surface-1 px-2 text-[13px] text-fg-primary outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60"
        >
          <option v-for="opt in VOICE_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </div>

      <div class="mt-4 flex justify-end">
        <button
          type="button"
          class="h-8 rounded-md bg-accent px-3.5 text-[13px] font-medium text-fg-inverse transition-all duration-ds-sm ease-expo-out hover:bg-accent-hover hover:shadow-ds-hover active:shadow-ds-active"
          @click="saveSettings"
        >
          保存
        </button>
      </div>
    </PageCard>
  </div>
</template>
