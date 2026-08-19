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
import { openPanelWindow, resizePetWindowForPanel } from './windows'

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

  // ---------- 悬浮设置面板窗口尺寸 ----------
  // 右键弹出/关闭上下文菜单 与 普通 panel:height 走同一条路径。
  // async：resizePetWindowForPanel 内部会 await 窗口 resize/move 事件，
  // 等窗口物理布局真正完成后再返回真实 getBounds()（无盲 sleep）。
  ipcMain.handle(IPC_CH.panelHeight, async (_event, payload: unknown): Promise<{ x: number; y: number } | undefined> => {
    const p = payload as { height?: unknown; ballScreen?: unknown; ballInCanvas?: unknown } | null
    const height = p?.height
    if (typeof height === 'number' && Number.isFinite(height) && height >= 0) {
      const ball = p?.ballScreen as { x?: unknown; y?: unknown } | null
      const bInC = p?.ballInCanvas as { x?: unknown; y?: unknown } | null
      return resizePetWindowForPanel(
        height,
        typeof ball?.x === 'number' && typeof ball?.y === 'number' ? { x: ball.x, y: ball.y } : undefined,
        typeof bInC?.x === 'number' && typeof bInC?.y === 'number' ? { x: bInC.x, y: bInC.y } : undefined
      )
    }
    return undefined
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
