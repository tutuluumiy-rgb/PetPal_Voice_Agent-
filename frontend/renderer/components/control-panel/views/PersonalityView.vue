<script setup lang="ts">
/**
 * 宠物人设配置 — 真实读写 backend/prompts/personality.md（保存后下轮 LLM 立即生效）
 * 按钮走控制面板右下角操作栏（保存=主、撤销=次）。
 */
import { onActivated, onBeforeUnmount, onMounted, ref } from 'vue'
import PageCard from '../PageCard.vue'
import { setPanelActions, clearPanelActions } from '../../../app/panelActions'

const content = ref('')
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const savedTip = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const res = await window.api.personalityGet()
    content.value = res.content || ''
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
    await window.api.personalitySet(content.value)
    savedTip.value = '已保存，下次对话即可生效'
    setTimeout(() => (savedTip.value = ''), 2500)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void load()
  registerActions()
})

// KeepAlive 激活（首次挂载与切回）时重新注册右下角操作按钮
onActivated(registerActions)

function registerActions(): void {
  setPanelActions([
    { key: 'revert', label: '撤销', onClick: () => void load() },
    { key: 'save', label: '保存', primary: true, disabled: () => saving.value, onClick: () => void save() },
  ])
}

onBeforeUnmount(() => {
  clearPanelActions()
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="宠物人设配置" description="定义西西的性格、说话风格与陪伴习惯，保存后下次对话立刻生效">
      <div v-if="loading" class="py-8 text-center text-[13px] text-fg-muted">加载中…</div>

      <template v-else>
        <textarea
          v-model="content"
          rows="14"
          spellcheck="false"
          class="w-full resize-y rounded-md border border-line-subtle bg-surface-1 px-3 py-2 font-mono text-[12px] leading-5 text-fg-primary outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60 focus:shadow-[0_0_0_2px_rgba(94,106,210,0.25),var(--ds-shadow-sm)]"
        />
        <div class="mt-3 flex items-center justify-between">
          <span class="text-[12px] text-fg-muted">这里描述西西的性格与说话方式</span>
          <div class="min-h-[16px]">
            <span v-if="savedTip" class="font-mono text-[11px] text-success">✓ {{ savedTip }}</span>
            <span v-else-if="error" class="font-mono text-[11px] text-danger">{{ error }}</span>
          </div>
        </div>
      </template>
    </PageCard>
  </div>
</template>