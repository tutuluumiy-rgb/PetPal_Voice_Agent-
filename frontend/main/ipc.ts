/**
 * 主进程 IPC 注册
 * --------------------------------------------------------------------------
 * 骨架实现：只完成通道注册、参数校验与状态透传；
 * 所有业务逻辑统一以 // TODO: 后续迭代实现 占位。
 */
import { BrowserWindow, ipcMain } from 'electron'
import type { AuthPolicy, DragPoint, PetMode } from '../preload/types'
import { IPC_CH } from '../preload/types'
import { dragStart, dragMove, dragEnd } from './drag'
import {
  getAuthPolicy,
  getMode,
  isPetVisible,
  onModeChanged,
  onPetVisibleChanged,
  setAuthPolicy,
  setMode,
  setPetVisible
} from './state'
import { openPanelWindow, openChatPanel, closeChatPanel } from './windows'

/** 注册全部 IPC 处理器（app ready 后调用一次） */
export function registerIpcHandlers(): void {
  // ---------- 模式 ----------
  ipcMain.handle(IPC_CH.modeGet, (): PetMode => {
    return getMode()
  })

  ipcMain.on(IPC_CH.modeSwitch, (_event, mode: unknown): void => {
    if (mode !== 'chat' && mode !== 'work') return
    setMode(mode)
    // TODO: 后续迭代实现 — 模式切换后的业务逻辑（会话上下文切换等）
  })

  // 模式变化 → 广播到全部窗口（同步手风琴单选选中态，ASR 语音切换同样走 setMode）
  onModeChanged((mode) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.modeChanged, mode)
      }
    }
  })

  // ---------- 权限策略 ----------
  ipcMain.handle(IPC_CH.authPolicyGet, (): AuthPolicy => {
    return getAuthPolicy()
  })

  ipcMain.on(IPC_CH.authPolicySet, (_event, policy: unknown): void => {
    if (policy !== 'full' && policy !== 'ask') return
    setAuthPolicy(policy)
    // TODO: 后续迭代实现 — 权限策略业务生效
  })

  // ---------- 窗口 ----------
  ipcMain.on(IPC_CH.panelOpen, (): void => {
    openPanelWindow()
  })

  // ---------- 悬浮宠物拖拽 ----------
  ipcMain.on(IPC_CH.dragStart, (event, point: DragPoint): void => {
    const win = BrowserWindow.fromWebContents(event.sender)
    if (win && typeof point?.screenX === 'number' && typeof point?.screenY === 'number') {
      dragStart(win, point)
    }
  })

  ipcMain.on(IPC_CH.dragMove, (_event, point: DragPoint): void => {
    if (typeof point?.screenX === 'number' && typeof point?.screenY === 'number') {
      dragMove(point)
    }
  })

  ipcMain.on(IPC_CH.dragEnd, (): void => {
    dragEnd()
  })

  // ---------- 独立对话面板窗口（宠物窗口尺寸恒定，面板独立成窗口） ----------
  // 打开：创建/显示独立对话面板窗口并按宠物位置定位；宠物窗口不受影响。
  ipcMain.on(IPC_CH.chatPanelOpen, (): void => {
    openChatPanel()
  })

  // 关闭：隐藏独立对话面板窗口；宠物窗口不受影响。
  ipcMain.on(IPC_CH.chatPanelClose, (): void => {
    closeChatPanel()
  })

  // ---------- 语音播报预览：对话面板推送 → 广播到宠物窗口底部消息条 ----------
  // 保留最近一次文本（供新开的宠物窗口/面板回放「当次」语音播报）
  let lastVoicePreview = ''
  ipcMain.on(IPC_CH.voicePreviewPush, (_event, text: unknown): void => {
    lastVoicePreview = typeof text === 'string' ? text : String(text ?? '')
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.voicePreview, lastVoicePreview)
      }
    }
  })

  // 宠物窗口挂载后可回放最近一次语音播报内容
  ipcMain.handle(IPC_CH.voicePreviewGet, (): string => lastVoicePreview)

  // ---------- 宠物动画状态：对话面板说开始/结束 → 广播到宠物窗口 ----------
  ipcMain.on(IPC_CH.petAnim, (_event, state: unknown): void => {
    const s = state === 'speaking' ? 'speaking' : 'idle'
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.petAnimChanged, s)
      }
    }
  })

  // ---------- 宠物可见性 ----------
  ipcMain.on(IPC_CH.petVisibleSet, (_event, visible: unknown): void => {
    if (typeof visible === 'boolean') {
      setPetVisible(visible)
    }
  })

  // 可见性变化 → 广播到全部窗口（同步球体显示/隐藏）
  onPetVisibleChanged((visible) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.petVisibleChanged, visible)
      }
    }
  })

  // 控制面板「重新显示宠物」需要读取当前状态
  ipcMain.handle(IPC_CH.petVisibleGet, (): boolean => {
    return isPetVisible()
  })

  // ---------- 应用信息 ----------
  ipcMain.handle(IPC_CH.appVersion, (): string => {
    return process.env['npm_package_version'] ?? '0.1.0'
  })
}

/** 通知渲染进程：TTS 音频开始播放（预留，由未来语音服务调用） */
export function sendTtsStart(win: BrowserWindow): void {
  // TODO: 后续迭代实现 — 语音服务在 TTS 播放开始时调用本函数
  win.webContents.send(IPC_CH.ttsStart)
}

/** 通知渲染进程：TTS 音频播放结束（预留，由未来语音服务调用） */
export function sendTtsEnd(win: BrowserWindow): void {
  // TODO: 后续迭代实现 — 语音服务在 TTS 播放结束时调用本函数
  win.webContents.send(IPC_CH.ttsEnd)
}
