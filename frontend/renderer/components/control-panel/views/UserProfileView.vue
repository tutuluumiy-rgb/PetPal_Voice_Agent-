<script setup lang="ts">
/**
 * 用户档案 — 文本域 + 修改/保存按钮
 * 关联 user.md 文件，文件读写逻辑 TODO
 */
import { ref } from 'vue'
import PageCard from '../PageCard.vue'

// TODO: 后续迭代实现 — 经主进程 IPC 读取 user.md 内容
const content = ref(`# 用户档案

- 称呼：主人
- 偏好：简洁回答，重要结论放前面
- 使用场景：日常陪伴 + 桌面办公辅助

（此文件由主进程读写，当前为占位内容）
`)

// TODO: 后续迭代实现 — 经主进程 IPC 写入 user.md
function save(): void {
  void content.value
  // TODO: 后续迭代实现 — IPC 写文件 + 保存反馈
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="用户档案" description="编辑 user.md — 记录你的偏好与使用场景">
      <textarea
        v-model="content"
        rows="14"
        spellcheck="false"
        class="w-full resize-y rounded-md border border-line-subtle bg-surface-1 px-3 py-2 font-mono text-[12px] leading-5 text-fg-primary outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60 focus:shadow-[0_0_0_2px_rgba(94,106,210,0.25),var(--ds-shadow-sm)]"
      />
      <div class="mt-3 flex items-center justify-between">
        <span class="font-mono text-[10px] uppercase tracking-widest text-fg-muted">user.md</span>
        <div class="flex gap-2">
          <button
            type="button"
            class="h-8 rounded-md border border-line-subtle bg-transparent px-3 text-[13px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active"
          >
            修改
          </button>
          <button
            type="button"
            class="h-8 rounded-md bg-accent px-3.5 text-[13px] font-medium text-fg-inverse transition-all duration-ds-sm ease-expo-out hover:bg-accent-hover hover:shadow-ds-hover active:shadow-ds-active"
            @click="save"
          >
            保存
          </button>
        </div>
      </div>
    </PageCard>
  </div>
</template>
