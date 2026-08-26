<script setup lang="ts">
/**
 * 模型配置 — 参照「图像设置」版式：5 组配置（大语言模型 / ASR / TTS / 识图 / 视频）
 * 每组：API 地址 / API Key / 模型 ID / ↻ 获取可用模型（另 TTS 附「音色/说话人」）。
 * - 读取：model:get（密钥只回掩码，绝不回传明文）
 * - 保存：model:set（写回后端 .env，重启后端生效）—— 右下角操作栏 撤销/保存
 * - 检查：model:check（各密钥就绪 + best-effort 连通性）—— 页内「检查模型配置」按钮
 * - 获取可用模型：model:list（按组返回目录，点选填充模型 ID）
 */
import { onActivated, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import PageCard from '../PageCard.vue'
import type { ModelConfig, ModelCheckResult, ModelListItem, ModelSavePayload } from '../../../../preload/types'
import { setPanelActions, clearPanelActions } from '../../../app/panelActions'

const ORDER = ['llm', 'asr', 'tts', 'vision', 'video'] as const
type SectionType = (typeof ORDER)[number]

interface Section {
  label: string
  hint: string
  sub: string
  url: string
  apiKey: string
  model: string
  voice: string
  apiKeyEnv: string
  keyMasked: string
  keySet: boolean
}

const cfg = ref<ModelConfig | null>(null)
const loading = ref(true)
const saving = ref(false)
const checking = ref(false)
const error = ref('')
const savedTip = ref('')
const checkResult = ref<ModelCheckResult | null>(null)

const sections = reactive<Record<SectionType, Section>>({
  llm: { label: '大语言模型', hint: '', sub: '', url: '', apiKey: '', model: '', voice: '', apiKeyEnv: '', keyMasked: '', keySet: false },
  asr: { label: 'ASR', hint: '', sub: '', url: '', apiKey: '', model: '', voice: '', apiKeyEnv: '', keyMasked: '', keySet: false },
  tts: { label: 'TTS', hint: '', sub: '', url: '', apiKey: '', model: '', voice: '', apiKeyEnv: '', keyMasked: '', keySet: false },
  vision: { label: '识图模型', hint: '', sub: '', url: '', apiKey: '', model: '', voice: '', apiKeyEnv: '', keyMasked: '', keySet: false },
  video: { label: '视频模型', hint: '', sub: '', url: '', apiKey: '', model: '', voice: '', apiKeyEnv: '', keyMasked: '', keySet: false },
})

const choices = ref<Record<string, ModelListItem[]>>({})
const loadingChoices = ref<Record<string, boolean>>({})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const c = await window.api.modelGet()
    cfg.value = c
    for (const t of ORDER) {
      const s = c[t]
      if (!s) continue
      sections[t].label = s.label
      sections[t].hint = s.hint ?? ''
      sections[t].sub = s.sub ?? ''
      sections[t].url = s.url ?? ''
      sections[t].apiKey = ''
      sections[t].model = s.model ?? ''
      sections[t].voice = s.voice ?? ''
      sections[t].apiKeyEnv = s.api_key_env ?? ''
      sections[t].keyMasked = s.api_key_masked ?? ''
      sections[t].keySet = s.api_key_set
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function fetchModels(type: SectionType): Promise<void> {
  loadingChoices.value[type] = true
  try {
    const r = await window.api.modelList(type)
    choices.value = { ...choices.value, [type]: r.models ?? [] }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    choices.value = { ...choices.value, [type]: [] }
  } finally {
    loadingChoices.value[type] = false
  }
}

function pickModel(type: SectionType, id: string): void {
  sections[type].model = id
  choices.value = { ...choices.value, [type]: [] }
}

function buildPayload(): ModelSavePayload {
  const payload: ModelSavePayload = { sections: {} }
  for (const t of ORDER) {
    const s = sections[t]
    const sec: { url?: string; api_key?: string; model?: string; voice?: string } = {}
    if (s.url.trim()) sec.url = s.url.trim()
    if (s.apiKey.trim()) sec.api_key = s.apiKey.trim()
    if (s.model.trim()) sec.model = s.model.trim()
    if (s.voice.trim()) sec.voice = s.voice.trim()
    payload.sections![t] = sec
  }
  return payload
}

async function save(): Promise<void> {
  saving.value = true
  error.value = ''
  try {
    const c = await window.api.modelSet(buildPayload())
    cfg.value = c
    for (const t of ORDER) {
      const s = c[t]
      if (!s) continue
      sections[t].url = s.url ?? ''
      sections[t].model = s.model ?? ''
      sections[t].voice = s.voice ?? ''
      sections[t].apiKey = ''
      sections[t].keyMasked = s.api_key_masked ?? ''
      sections[t].keySet = s.api_key_set
    }
    savedTip.value = '已保存，重启后端后生效'
    setTimeout(() => (savedTip.value = ''), 3000)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

async function check(): Promise<void> {
  checking.value = true
  checkResult.value = null
  error.value = ''
  try {
    checkResult.value = await window.api.modelCheck()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  void load()
  registerActions()
})

onActivated(registerActions)

function registerActions(): void {
  setPanelActions([
    { key: 'revert', label: '撤销', onClick: () => void load() },
    { key: 'save', label: '保存', primary: true, disabled: () => saving.value, onClick: () => void save() },
  ])
}

onBeforeUnmount(() => clearPanelActions())

const fieldCls =
  'h-8 w-full rounded-md border border-line-subtle bg-surface-1 px-2 font-mono text-[12px] text-fg-primary outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60'
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="模型配置" description="大语言模型 / ASR / TTS / 识图 / 视频五组配置，检查所需 API 是否就绪">
      <div v-if="loading" class="py-8 text-center text-[13px] text-fg-muted">加载中…</div>
      <div v-else-if="!cfg" class="py-8 text-center text-[13px] text-danger">{{ error || '无法读取模型配置' }}</div>

      <div v-else class="flex flex-col gap-4">
        <!-- 各组配置卡 -->
        <section
          v-for="t in ORDER"
          :key="t"
          class="overflow-hidden rounded-lg border border-line-subtle bg-surface-3/60 shadow-ds-md"
        >
          <header class="border-b border-line-subtle px-4 py-2.5">
            <div class="flex items-center gap-2">
              <h3 class="text-[13px] font-semibold tracking-ds-tight text-fg-primary">{{ sections[t].label }}</h3>
              <span v-if="sections[t].sub" class="font-mono text-[10px] text-fg-muted">{{ sections[t].sub }}</span>
            </div>
            <p v-if="sections[t].hint" class="mt-0.5 text-[11px] leading-4 text-fg-muted">{{ sections[t].hint }}</p>
          </header>

          <div class="flex flex-col gap-3 p-4">
            <div class="flex flex-col gap-1">
              <label class="text-[12px] text-fg-secondary">API 地址</label>
              <input v-model="sections[t].url" type="text" spellcheck="false" :class="fieldCls" />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-[12px] text-fg-secondary">API Key</label>
              <input
                v-model="sections[t].apiKey"
                type="password"
                spellcheck="false"
                :placeholder="sections[t].keySet ? sections[t].keyMasked || '已配置' : 'sk-…'"
                :class="fieldCls"
              />
              <p class="font-mono text-[10px] text-fg-muted">
                {{ sections[t].apiKeyEnv }}{{ sections[t].keySet ? ' · 已配置' : ' · 未配置' }}
              </p>
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-[12px] text-fg-secondary">模型 ID</label>
              <input v-model="sections[t].model" type="text" spellcheck="false" :class="fieldCls" />
            </div>
            <div v-if="t === 'tts'" class="flex flex-col gap-1">
              <label class="text-[12px] text-fg-secondary">音色 / 说话人</label>
              <input v-model="sections[t].voice" type="text" spellcheck="false" :class="fieldCls" />
            </div>

            <!-- 获取可用模型 -->
            <div class="flex items-center gap-3">
              <button
                type="button"
                class="flex items-center gap-1.5 text-[13px] font-medium text-accent transition-opacity duration-ds-sm ease-expo-out hover:opacity-80 disabled:opacity-40"
                :disabled="loadingChoices[t]"
                @click="fetchModels(t)"
              >
                <!-- 统一 icon：圆环箭头刷新（各组一致） -->
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  aria-hidden="true"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  :class="loadingChoices[t] ? 'animate-spin' : ''"
                >
                  <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
                  <path d="M21 3v5h-5" />
                  <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
                  <path d="M8 16H3v5" />
                </svg>
                {{ loadingChoices[t] ? '获取中…' : '获取可用模型' }}
              </button>
            </div>

            <!-- 可用模型候选 -->
            <div v-if="choices[t]?.length" class="flex flex-wrap gap-1.5">
              <button
                v-for="m in choices[t]"
                :key="m.id"
                type="button"
                class="rounded-md border border-line-subtle bg-surface-1 px-2 py-1 font-mono text-[11px] text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:border-accent/50 hover:text-accent"
                @click="pickModel(t, m.id)"
              >
                {{ m.label }}
              </button>
            </div>
            <p v-else-if="t === 'video'" class="font-mono text-[10px] text-fg-muted">暂无可选视频模型</p>
          </div>
        </section>

        <!-- 检查模型配置 -->
        <div class="flex items-center gap-3">
          <button
            type="button"
            :disabled="checking"
            class="flex h-[27px] items-center justify-center rounded-[6px] border border-line-subtle bg-surface-1 px-3 text-[12px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active disabled:opacity-40"
            @click="check"
          >
            {{ checking ? '检查中…' : '检查模型配置' }}
          </button>
          <span v-if="checkResult" class="font-mono text-[11px]" :class="checkResult.ok ? 'text-success' : 'text-danger'">
            {{ checkResult.ok ? '✓ 配置就绪' : '✗ 存在未就绪项' }}
          </span>
        </div>

        <!-- 检查结果 -->
        <div v-if="checkResult" class="flex flex-col gap-1.5 rounded-md border border-line-subtle bg-surface-1 p-3">
          <div v-for="c in checkResult.checks" :key="c.key" class="flex items-center gap-2 text-[12px]">
            <span class="shrink-0 font-mono text-[11px]" :class="c.status === 'ok' ? 'text-success' : 'text-danger'">
              {{ c.status === 'ok' ? '✓' : '✗' }}
            </span>
            <span class="min-w-0 flex-1 truncate text-fg-secondary">{{ c.label }}</span>
            <span class="font-mono text-[11px] text-fg-muted">{{ c.detail }}</span>
          </div>
          <div class="mt-1 flex items-center gap-2 border-t border-line-subtle pt-1.5 text-[12px]">
            <span class="text-fg-secondary">连通性</span>
            <span
              class="font-mono text-[11px]"
              :class="checkResult.live.status === 'ok' ? 'text-success' : checkResult.live.status === 'fail' ? 'text-danger' : 'text-fg-muted'"
            >{{ checkResult.live.detail }}</span>
          </div>
        </div>

        <div class="flex items-center justify-end">
          <div class="min-h-[16px]">
            <span v-if="savedTip" class="font-mono text-[11px] text-success">✓ {{ savedTip }}</span>
            <span v-else-if="error" class="font-mono text-[11px] text-danger">{{ error }}</span>
          </div>
        </div>
      </div>
    </PageCard>
  </div>
</template>
