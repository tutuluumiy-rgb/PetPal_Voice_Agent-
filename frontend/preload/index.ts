/**
 * preload 脚本 — IPC 桥接层
 * --------------------------------------------------------------------------
 * 职责：通过 contextBridge 将受控的 IPC 能力暴露给渲染进程 window.api。
 * 所有通道名与 payload 类型在 types.ts 中统一定义，主进程与渲染进程共用。
 * 业务逻辑一律不进 preload。
 */
import { contextBridge, ipcRenderer } from 'electron'
import { IPC_CH } from './types'
import type { AppApi, AuthPolicy, DragPoint, PetMode } from './types'

const api: AppApi = {
  // ---------- 模式 ----------
  getMode: (): Promise<PetMode> => ipcRenderer.invoke(IPC_CH.modeGet),
  switchMode: (mode: PetMode): void => {
    ipcRenderer.send(IPC_CH.modeSwitch, mode)
  },
  onModeChanged: (callback: (mode: PetMode) => void): (() => void) => {
    const listener = (_event: unknown, mode: PetMode): void => callback(mode)
    ipcRenderer.on(IPC_CH.modeChanged, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.modeChanged, listener)
    }
  },

  // ---------- 权限策略 ----------
  getAuthPolicy: (): Promise<AuthPolicy> => ipcRenderer.invoke(IPC_CH.authPolicyGet),
  setAuthPolicy: (policy: AuthPolicy): void => {
    ipcRenderer.send(IPC_CH.authPolicySet, policy)
  },

  // ---------- 窗口 ----------
  openPanel: (): void => {
    ipcRenderer.send(IPC_CH.panelOpen)
  },

  // ---------- 悬浮宠物拖拽（主进程节流处理 Windows 漂移） ----------
  dragStart: (point: DragPoint): void => {
    ipcRenderer.send(IPC_CH.dragStart, point)
  },
  dragMove: (point: DragPoint): void => {
    ipcRenderer.send(IPC_CH.dragMove, point)
  },
  dragEnd: (): void => {
    ipcRenderer.send(IPC_CH.dragEnd)
  },

  // ---------- 独立对话面板窗口（打开/关闭；宠物窗口不受影响） ----------
  openChatPanel: (): void => {
    ipcRenderer.send(IPC_CH.chatPanelOpen)
  },
  closeChatPanel: (): void => {
    ipcRenderer.send(IPC_CH.chatPanelClose)
  },

  // ---------- 宠物可见性 ----------
  setPetVisible: (visible: boolean): void => {
    ipcRenderer.send(IPC_CH.petVisibleSet, Boolean(visible))
  },
  getPetVisible: (): Promise<boolean> => ipcRenderer.invoke(IPC_CH.petVisibleGet),
  onPetVisibleChanged: (callback: (visible: boolean) => void): (() => void) => {
    const listener = (_event: unknown, visible: boolean): void => callback(Boolean(visible))
    ipcRenderer.on(IPC_CH.petVisibleChanged, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.petVisibleChanged, listener)
    }
  },

  // ---------- 应用信息 ----------
  getAppVersion: (): Promise<string> => ipcRenderer.invoke(IPC_CH.appVersion),

  // ---------- TTS 事件订阅（主进程 → 渲染进程） ----------
  onTtsStart: (callback: () => void): (() => void) => {
    const listener = (): void => callback()
    ipcRenderer.on(IPC_CH.ttsStart, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.ttsStart, listener)
    }
  },
  onTtsEnd: (callback: () => void): (() => void) => {
    const listener = (): void => callback()
    ipcRenderer.on(IPC_CH.ttsEnd, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.ttsEnd, listener)
    }
  },

  // ---------- 唤醒词（KWS）：喂帧 → 主进程推理；命中广播回渲染进程 ----------
  kwsFeed: (frame: Float32Array): void => {
    if (frame && frame.length) ipcRenderer.send(IPC_CH.kwsFeed, frame)
  },
  onKwsWake: (callback: (keyword: string) => void): (() => void) => {
    const listener = (_event: unknown, keyword: string): void => callback(keyword)
    ipcRenderer.on(IPC_CH.kwsWake, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.kwsWake, listener)
    }
  },

  // ---------- 语音播报预览（宠物窗口底部消息条） ----------
  pushVoicePreview: (text: string): void => {
    ipcRenderer.send(IPC_CH.voicePreviewPush, String(text ?? ''))
  },
  onVoicePreview: (callback: (text: string) => void): (() => void) => {
    const listener = (_event: unknown, text: string): void => callback(String(text ?? ''))
    ipcRenderer.on(IPC_CH.voicePreview, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.voicePreview, listener)
    }
  },
  getVoicePreview: (): Promise<string> => ipcRenderer.invoke(IPC_CH.voicePreviewGet),

  // ---------- 宠物动画状态（chat → 主进程 → pet） ----------
  setPetAnim: (state: 'speaking' | 'idle'): void => {
    ipcRenderer.send(IPC_CH.petAnim, state === 'speaking' ? 'speaking' : 'idle')
  },
  onPetAnimChanged: (callback: (state: 'speaking' | 'idle') => void): (() => void) => {
    const listener = (_event: unknown, state: string): void =>
      callback(state === 'speaking' ? 'speaking' : 'idle')
    ipcRenderer.on(IPC_CH.petAnimChanged, listener)
    return () => {
      ipcRenderer.removeListener(IPC_CH.petAnimChanged, listener)
    }
  }
}

contextBridge.exposeInMainWorld('api', api)
