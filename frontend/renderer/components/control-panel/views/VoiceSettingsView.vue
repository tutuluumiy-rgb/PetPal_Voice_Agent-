<script setup lang="ts">
/**
 * 语音参数设置 — 真实读写（voice:settings：音量/音调/音色）
 * 真实后端落盘 backend/data/voice_settings.json，且音色/音量/音调实际作用于 TTS。
 * 按钮走控制面板右下角操作栏（保存=主、撤销=次）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import PageCard from '../PageCard.vue'
import type { VoiceSettings } from '../../../../preload/types'
import { setPanelActions, clearPanelActions } from '../../../app/panelActions'

const volume = ref(80)
const pitch = ref(50)
const voice = ref<VoiceSettings['voice']>('default')

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const savedTip = ref('')

const VOICE_OPTIONS: Array<{ value: VoiceSettings['voice']; label: string }> = [
  { value: 'default', label: '默认音色' },
  { value: 'cute', label: '软萌音' },
  { value: 'calm', label: '沉稳音' },
  { value: 'bright', label: '明亮音' }
]

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const s = await window.api.voiceSettingsGet()
    volume.value = s.volume ?? 80
    pitch.value = s.pitch ?? 50
    voice.value = s.voice ?? 'default'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  error.value = ''
  try {
    const s = await window.api.voiceSettingsSet({ volume: volume.value, pitch: pitch.value, voice: voice.value })
    volume.value = s.volume ?? volume.value
    pitch.value = s.pitch ?? pitch.value
    voice.value = s.voice ?? voice.value
    savedTip.value = '已保存，下次说话即生效'
    setTimeout(() => (savedTip.value = ''), 2500)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void load()
  setPanelActions([
    { key: 'revert', label: '撤销', onClick: () => void load() },
    { key: 'save', label: '保存', primary: true, disabled: () => saving.value, onClick: () => void save() },
  ])
})

onBeforeUnmount(() => {
  clearPanelActions()
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="语音参数设置" description="调整西西的音量、音调和音色，让它说起话来更合你的心意">
      <div v-if="loading" class="py-8 text-center text-[13px] text-fg-muted">加载中…</div>

      <template v-else>
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

        <div class="mt-4 flex items-center justify-end">
          <div class="min-h-[16px]">
            <span v-if="savedTip" class="font-mono text-[11px] text-success">✓ {{ savedTip }}</span>
            <span v-else-if="error" class="font-mono text-[11px] text-danger">{{ error }}</span>
          </div>
        </div>
      </template>
    </PageCard>
  </div>
</template>