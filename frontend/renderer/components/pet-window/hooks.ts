/**
 * 悬浮宠物 — 事件钩子
 * --------------------------------------------------------------------------
 * onTtsStart/onTtsEnd：主进程 TTS 事件 → 已由 PetWindow 接到动画器 feedTts。
 * parseActionTag：解析 LLM 回复的动作标签【action:xxx】→ 动画状态（接入点）。
 */
import type { PetAnimState } from './anim/types'

/**
 * TTS 音频开始播放触发
 * 调用时机：window.api.onTtsStart 订阅回调 → 已交给 PetAnimator.feedTts(true)
 */
export function onTtsStart(): void {
  // 说话动画已由 PetWindow 的 onTtsStart → animator.feedTts(true) 驱动，此处无需额外实现
}

/**
 * TTS 音频播放结束触发，切回待机状态
 * 调用时机：window.api.onTtsEnd 订阅回调 → 已交给 PetAnimator.feedTts(false)
 */
export function onTtsEnd(): void {
  // 已由 PetWindow 的 onTtsEnd → animator.feedTts(false) 驱动
}

/** 动作标签 → 动画状态 映射表（LLM 输出的动作名） */
const ACTION_TO_STATE: Record<string, PetAnimState> = {
  wave: 'happy',
  happy: 'happy',
  sad: 'sad',
  think: 'thinking',
  thinking: 'thinking',
  work: 'working',
  working: 'working',
  sleep: 'sleeping',
  surprised: 'surprised',
  listen: 'listening',
  idle: 'idle',
}

/**
 * 解析 LLM 输出中的动作标签，映射到动画状态。
 * 例如：rawText = "【action:wave】你好呀" → 提取 wave → 返回 'happy'
 * @param rawText LLM 回复原文
 * @returns 匹配到的动画状态；无匹配返回 null
 */
export function parseActionTag(rawText: string): PetAnimState | null {
  const m = /【action:([\w-]+)】/.exec(rawText)
  if (!m) return null
  const action = m[1].toLowerCase()
  return ACTION_TO_STATE[action] ?? null
}
