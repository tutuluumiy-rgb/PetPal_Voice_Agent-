<script setup lang="ts">
/**
 * 用户档案 — 真实读写 backend/users/<ACTIVE_USER>/profile.json（结构化表单）
 * 字段：basic(称呼/角色) + reply_style + likes/dislikes + daily(作息)
 * 按钮走控制面板右下角操作栏（保存=主、撤销=次）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import PageCard from '../PageCard.vue'
import type { UserProfile } from '../../../../preload/types'
import { setPanelActions, clearPanelActions } from '../../../app/panelActions'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const savedTip = ref('')

// 可编辑状态（与 profile.json 一一对应）
const name = ref('')
const role = ref('owner')
const replyStyle = ref('')
const likes = ref('')
const dislikes = ref('')
const wakeTime = ref('')
const sleepTime = ref('')

const ROLE_OPTIONS = [
  { value: 'owner', label: '主人' },
  { value: 'family', label: '家人' },
  { value: 'guest', label: '访客' }
]

function splitList(s: string): string[] {
  return s
    .split(/[,，、;\n]/)
    .map((x) => x.trim())
    .filter(Boolean)
}

function toProfile(): UserProfile {
  return {
    basic: { name: name.value.trim(), role: role.value },
    reply_style: replyStyle.value.trim(),
    likes: splitList(likes.value),
    dislikes: splitList(dislikes.value),
    daily: { wake_time: wakeTime.value, sleep_time: sleepTime.value }
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const p = await window.api.userGet()
    name.value = p.basic?.name ?? ''
    role.value = (p.basic?.role ?? 'owner') as 'owner' | 'family' | 'guest'
    replyStyle.value = p.reply_style ?? ''
    likes.value = (p.likes ?? []).join('、')
    dislikes.value = (p.dislikes ?? []).join('、')
    wakeTime.value = p.daily?.wake_time ?? ''
    sleepTime.value = p.daily?.sleep_time ?? ''
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
    await window.api.userSet(toProfile())
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
    <PageCard title="用户档案" description="记录你的称呼、偏好与作息，让西西更懂你、更贴心地陪你">
      <div v-if="loading" class="py-8 text-center text-[13px] text-fg-muted">加载中…</div>

      <template v-else>
        <div class="grid grid-cols-2 gap-x-6 gap-y-4">
          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">称呼</label>
            <input
              v-model="name"
              type="text"
              placeholder="如：主人"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2.5 text-[13px] text-fg-primary outline-none transition-all duration-ds-sm ease-expo-out placeholder:text-fg-muted focus:border-accent/60"
            />
          </div>
          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">角色</label>
            <select
              v-model="role"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2 text-[13px] text-fg-primary outline-none"
            >
              <option v-for="opt in ROLE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
          </div>

          <div class="col-span-2 flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">偏好回复风格</label>
            <textarea
              v-model="replyStyle"
              rows="2"
              class="w-full resize-y rounded-md border border-line-subtle bg-surface-1 px-2.5 py-1.5 text-[13px] leading-5 text-fg-primary outline-none focus:border-accent/60"
            />
          </div>

          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">喜好（顿号/逗号分隔）</label>
            <input
              v-model="likes"
              type="text"
              placeholder="被摸头、被夸、一起看视频"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2.5 text-[13px] text-fg-primary outline-none placeholder:text-fg-muted focus:border-accent/60"
            />
          </div>
          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">不喜欢（顿号/逗号分隔）</label>
            <input
              v-model="dislikes"
              type="text"
              placeholder="熬夜、被冷落"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2.5 text-[13px] text-fg-primary outline-none placeholder:text-fg-muted focus:border-accent/60"
            />
          </div>

          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">起床时间</label>
            <input
              v-model="wakeTime"
              type="time"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2 text-[13px] text-fg-primary outline-none focus:border-accent/60"
            />
          </div>
          <div class="flex flex-col gap-1.5">
            <label class="text-[13px] text-fg-secondary">睡觉时间</label>
            <input
              v-model="sleepTime"
              type="time"
              class="h-8 rounded-md border border-line-subtle bg-surface-1 px-2 text-[13px] text-fg-primary outline-none focus:border-accent/60"
            />
          </div>
        </div>

        <div class="mt-3 flex items-center justify-end">
          <div class="min-h-[16px]">
            <span v-if="savedTip" class="font-mono text-[11px] text-success">✓ {{ savedTip }}</span>
            <span v-else-if="error" class="font-mono text-[11px] text-danger">{{ error }}</span>
          </div>
        </div>
      </template>
    </PageCard>
  </div>
</template>