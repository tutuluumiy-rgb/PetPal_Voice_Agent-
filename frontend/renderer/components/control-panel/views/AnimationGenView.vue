<script setup lang="ts">
/**
 * 宠物动画生成 — 入口页面占位
 * TODO: 后续迭代实现 — 接入 petpack 生成链 / 素材上传 / 动画预览
 * 另含「重新显示宠物」入口（球体被隐藏后，只能在此处重新开启）
 */
import { ref } from 'vue'
import PageCard from '../PageCard.vue'

const showPetBtn = ref(true)

// TODO: 后续迭代实现 — 素材上传、生成任务、结果预览
function placeholder(): void {
  // TODO: 后续迭代实现 — 打开素材生成流程
}

/** 重新显示被隐藏的球体宠物 */
async function showPet(): Promise<void> {
  window.api.setPetVisible(true)
  try {
    showPetBtn.value = !(await window.api.getPetVisible())
  } catch {
    showPetBtn.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="宠物动画生成" description="由真实照片生成各状态动画素材（petpack 生成链）">
      <div
        class="flex min-h-40 flex-col items-center justify-center gap-3 rounded-md border border-dashed border-line-strong bg-surface-1/50"
      >
        <p class="text-[13px] text-fg-secondary">尚未接入素材生成流程</p>
        <p class="max-w-64 text-center text-[11px] leading-4 text-fg-muted">
          TODO：后续迭代实现 — 上传宠物照片，调用生成链产出 idle / walk / sit / sleep / reaction 动画帧
        </p>
        <button
          type="button"
          class="h-8 rounded-md bg-accent px-3.5 text-[13px] font-medium text-fg-inverse transition-all duration-ds-sm ease-expo-out hover:bg-accent-hover hover:shadow-ds-hover active:shadow-ds-active"
          @click="placeholder"
        >
          上传照片
        </button>
      </div>
    </PageCard>

    <PageCard title="宠物显示" description="球体被隐藏后，只能在此重新开启显示">
      <div class="flex items-center gap-3">
        <p class="text-[13px] text-fg-secondary">球体宠物当前状态：</p>
        <button
          v-if="showPetBtn"
          type="button"
          class="h-8 rounded-md bg-accent px-3.5 text-[13px] font-medium text-fg-inverse transition-all duration-ds-sm ease-expo-out hover:bg-accent-hover hover:shadow-ds-hover active:shadow-ds-active"
          @click="showPet"
        >
          重新显示宠物
        </button>
        <span v-else class="text-[13px] text-status-success">宠物已显示</span>
      </div>
    </PageCard>
  </div>
</template>
