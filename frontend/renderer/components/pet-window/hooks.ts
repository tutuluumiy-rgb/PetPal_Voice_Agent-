/**
 * 悬浮宠物 — 预留事件钩子（空实现）
 * --------------------------------------------------------------------------
 * 本文件仅定义钩子签名与调用时机，业务实现全部 TODO 占位。
 * 后续迭代接入：TTS 说话动画、动作映射表、状态机。
 */

/**
 * TTS 音频开始播放触发
 * 调用时机：window.api.onTtsStart 订阅回调
 * TODO: 后续迭代实现 — 触发「说话」动画（如嘴型开合、能量驱动帧切换）
 */
export function onTtsStart(): void {
  // TODO: 后续迭代实现 — 说话精灵动画播放入口
}

/**
 * TTS 音频播放结束触发，切回待机状态
 * 调用时机：window.api.onTtsEnd 订阅回调
 * TODO: 后续迭代实现 — 动画状态机切回 idle
 */
export function onTtsEnd(): void {
  // TODO: 后续迭代实现 — 切回待机状态
}

/**
 * 解析 LLM 输出中的动作标签，预留动作映射表
 * 例如：rawText = "【action:wave】你好呀" → 提取 wave 动作
 * TODO: 后续迭代实现 — 动作标签 → 动画动作映射表
 */
export function parseActionTag(rawText: string): string | null {
  // TODO: 后续迭代实现 — 正则解析【action:xxx】并映射动作
  void rawText
  return null
}
