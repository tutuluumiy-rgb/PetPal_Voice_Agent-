/**
 * 主进程全局状态
 * --------------------------------------------------------------------------
 * 主进程持有全部业务状态；渲染进程只负责 UI 渲染。
 * - currentMode：全局工作模式（闲聊/工作），模式切换逻辑仅占位；
 *   模式变化通过订阅器广播到渲染进程（ASR 语音切换指令等场景同步 UI）。
 * - authPolicy：权限策略（完全批准/请求批准）。
 * - petVisible：宠物（球体）可见性（隐藏后只能从控制面板重新开启）。
 */
import type { AuthPolicy, PetMode } from '../preload/types'

/** 当前全局模式（默认闲聊） */
let currentMode: PetMode = 'chat'

/** 当前权限策略（默认请求批准） */
let authPolicy: AuthPolicy = 'ask'

/** 宠物（球体）可见性（默认可见） */
let petVisible = true

/** 模式变化订阅器（主进程侧改动 → 广播到渲染进程） */
const modeListeners = new Set<(mode: PetMode) => void>()

/** 宠物可见性订阅器 */
const visibleListeners = new Set<(visible: boolean) => void>()

export function getMode(): PetMode {
  return currentMode
}

export function setMode(mode: PetMode): void {
  currentMode = mode
  // TODO: 后续迭代实现 — 模式切换副作用（如切换提示词上下文、重置会话等）
  // 无论来源（UI / ASR 语音指令），变化一律广播，保证手风琴单选态同步
  for (const cb of modeListeners) {
    cb(mode)
  }
}

/** 订阅模式变化，返回取消订阅函数（供主进程广播模块使用） */
export function onModeChanged(cb: (mode: PetMode) => void): () => void {
  modeListeners.add(cb)
  return () => {
    modeListeners.delete(cb)
  }
}

export function getAuthPolicy(): AuthPolicy {
  return authPolicy
}

export function setAuthPolicy(policy: AuthPolicy): void {
  authPolicy = policy
  // TODO: 后续迭代实现 — 权限策略落盘 / 业务生效
}

export function isPetVisible(): boolean {
  return petVisible
}

export function setPetVisible(visible: boolean): void {
  petVisible = visible
  // TODO: 后续迭代实现 — 持久化可见性偏好
  for (const cb of visibleListeners) {
    cb(visible)
  }
}

/** 订阅宠物可见性变化，返回取消订阅函数 */
export function onPetVisibleChanged(cb: (visible: boolean) => void): () => void {
  visibleListeners.add(cb)
  return () => {
    visibleListeners.delete(cb)
  }
}
