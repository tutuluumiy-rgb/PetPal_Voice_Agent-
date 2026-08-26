/**
 * 主进程 IPC 注册
 * --------------------------------------------------------------------------
 * 骨架实现：只完成通道注册、参数校验与状态透传；
 * 所有业务逻辑统一以 // TODO: 后续迭代实现 占位。
 */
import { app, BrowserWindow, ipcMain } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import type { AuthPolicy, DragPoint, PetMode, Skin } from '../preload/types'
import { IPC_CH } from '../preload/types'
import { dragStart, dragMove, dragEnd } from './drag'
import {
  getSkin,
  isPetVisible,
  onModeChanged,
  onPetVisibleChanged,
  onSkinChanged,
  setPetVisible,
  setSkin
} from './state'
import { openPanelWindow, openChatPanel, closeChatPanel } from './windows'
import { backendGateway } from './services/backendGateway'

/** 皮肤偏好落盘路径（userData/skin.json） */
function skinPrefPath(): string {
  return path.join(app.getPath('userData'), 'skin.json')
}

/** 读取皮肤偏好（启动时调用） */
export function loadSkinPref(): void {
  try {
    const raw = fs.readFileSync(skinPrefPath(), 'utf-8')
    const v = JSON.parse(raw)?.skin
    if (v === 'dark' || v === 'light') setSkin(v)
  } catch {
    /* 无偏好，保持默认 */
  }
}

/** 皮肤变化 → 广播 + 落盘 */
function applySkin(s: Skin): void {
  setSkin(s)
  try {
    fs.writeFileSync(skinPrefPath(), JSON.stringify({ skin: s }), 'utf-8')
  } catch {
    /* ignore */
  }
}

/** 注册全部 IPC 处理器（app ready 后调用一次） */
export function registerIpcHandlers(): void {
  // ---------- 模式 ----------
  // 读取：网关优先（未连接回退本地），保证 UI 与后端一致
  ipcMain.handle(IPC_CH.modeGet, async (): Promise<PetMode> => {
    return backendGateway.modeGet()
  })

  ipcMain.on(IPC_CH.modeSwitch, (_event, mode: unknown): void => {
    if (mode !== 'chat' && mode !== 'work') return
    // 同步本地（立即广播 UI 单选态）+ 通知网关（服务端广播 mode:changed 再回本地，幂等）
    backendGateway.modeSet(mode)
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
  ipcMain.handle(IPC_CH.authPolicyGet, async (): Promise<AuthPolicy> => {
    return backendGateway.authPolicyGet()
  })

  ipcMain.on(IPC_CH.authPolicySet, (_event, policy: unknown): void => {
    if (policy !== 'full' && policy !== 'ask') return
    backendGateway.authPolicySet(policy)
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

  // ---------- 后端网关（文本对话 / 历史 / 人设 / 用户 / 语音参数，走 9000 契约） ----------
  // 连接状态广播由 backendGateway.init() 内部订阅（backend:status）

  // 文本对话：发送（流式事件由主进程广播 chat:running/delta/done/tts:event）
  ipcMain.on(IPC_CH.chatSend, (_event, payload: unknown): void => {
    const text = typeof (payload as any)?.text === 'string' ? (payload as any).text : ''
    const mode: PetMode = (payload as any)?.mode === 'work' ? 'work' : 'chat'
    if (!text.trim()) return
    backendGateway
      .chatSend(mode, text)
      .catch((err) => console.error('[gw] chat:send 失败:', err))
  })

  ipcMain.on(IPC_CH.chatAbort, (): void => {
    backendGateway.chatAbort().catch((err) => console.error('[gw] chat:abort 失败:', err))
  })

  // 历史记录
  ipcMain.handle(IPC_CH.historyList, async (_event, payload: unknown): Promise<unknown> => {
    const page = Math.max(1, Number((payload as any)?.page) || 1)
    const pageSize = Math.max(1, Number((payload as any)?.pageSize) || 20)
    const mode = (payload as any)?.mode === 'work' ? 'work' : 'chat'
    return backendGateway.historyList(page, pageSize, mode)
  })

  ipcMain.handle(IPC_CH.historySearch, async (_event, payload: unknown): Promise<unknown> => {
    const keyword = String((payload as any)?.keyword ?? '')
    const page = Math.max(1, Number((payload as any)?.page) || 1)
    const pageSize = Math.max(1, Number((payload as any)?.pageSize) || 20)
    return backendGateway.historySearch(keyword, page, pageSize)
  })

  // 历史 session 详情（抽屉展开事件轨迹）
  ipcMain.handle(IPC_CH.historyDetail, async (_event, payload: unknown): Promise<unknown> => {
    const sessionId = String((payload as any)?.sessionId ?? '')
    return backendGateway.historyDetail(sessionId)
  })

  // 删除历史会话
  ipcMain.handle(IPC_CH.historyDelete, async (_event, payload: unknown): Promise<unknown> => {
    const sessionId = String((payload as any)?.sessionId ?? '')
    return backendGateway.historyDelete(sessionId)
  })

  // 人设 / 用户档案
  ipcMain.handle(IPC_CH.personalityGet, async (): Promise<unknown> => backendGateway.personalityGet())
  ipcMain.handle(IPC_CH.personalitySet, async (_event, content: unknown): Promise<void> => {
    await backendGateway.personalitySet(String(content ?? ''))
  })
  ipcMain.handle(IPC_CH.userGet, async (): Promise<unknown> => backendGateway.userGet())
  ipcMain.handle(IPC_CH.userSet, async (_event, profile: unknown): Promise<void> => {
    await backendGateway.userSet((profile ?? {}) as any)
  })

  // 语音参数
  ipcMain.handle(IPC_CH.voiceSettingsGet, async (): Promise<unknown> => backendGateway.voiceSettingsGet())
  ipcMain.handle(IPC_CH.voiceSettingsSet, async (_event, payload: unknown): Promise<unknown> => {
    const s = (payload ?? {}) as Record<string, unknown>
    return backendGateway.voiceSettingsSet({
      volume: Math.max(0, Math.min(100, Number(s.volume) || 0)),
      pitch: Math.max(0, Math.min(100, Number(s.pitch) || 0)),
      voice: String(s.voice ?? 'default'),
    })
  })

  // 音色列表 / 模型配置
  ipcMain.handle(IPC_CH.voiceVoices, async (): Promise<unknown> => backendGateway.voiceVoices())
  ipcMain.handle(IPC_CH.modelGet, async (): Promise<unknown> => backendGateway.modelGet())
  ipcMain.handle(IPC_CH.modelSet, async (_event, payload: unknown): Promise<unknown> => {
    const p = (payload ?? {}) as { sections?: Record<string, Record<string, unknown>> }
    const sections: Record<string, { url?: string; api_key?: string; model?: string; voice?: string }> = {}
    for (const [typ, sec] of Object.entries(p.sections ?? {})) {
      if (!sec || typeof sec !== 'object') continue
      const s: { url?: string; api_key?: string; model?: string; voice?: string } = {}
      if (typeof sec.url === 'string') s.url = sec.url
      if (typeof sec.api_key === 'string') s.api_key = sec.api_key
      if (typeof sec.model === 'string') s.model = sec.model
      if (typeof sec.voice === 'string') s.voice = sec.voice
      sections[typ] = s
    }
    return backendGateway.modelSet({ sections })
  })
  ipcMain.handle(IPC_CH.modelCheck, async (): Promise<unknown> => backendGateway.modelCheck())
  ipcMain.handle(IPC_CH.modelList, async (_event, type: unknown): Promise<unknown> =>
    backendGateway.modelList(String(type ?? '')))

  // ---------- 宠物动画诊断（渲染进程上报 → 主进程终端日志，排查素材加载/过渡播放） ----------
  ipcMain.on(IPC_CH.animDebug, (_event, message: unknown): void => {
    console.log(`[pet-anim] ${String(message ?? '')}`)
  })

  // ---------- 皮肤主题（主进程状态 + 广播 + 落盘） ----------
  ipcMain.handle(IPC_CH.skinGet, (): Skin => getSkin())
  ipcMain.on(IPC_CH.skinSet, (_event, s: unknown): void => {
    if (s === 'dark' || s === 'light') applySkin(s)
  })
  onSkinChanged((s) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.skinChanged, s)
      }
    }
  })

  // ---------- 语音界面状态（对话面板 → 广播 → 宠物窗口指示灯） ----------
  ipcMain.on(IPC_CH.voiceState, (_event, payload: unknown): void => {
    const p = { state: ['off', 'idle', 'listening', 'speaking'].includes((payload as any)?.state)
      ? (payload as any).state : 'off' }
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.voiceStateChanged, p)
      }
    }
  })

  // ---------- 新建会话（对话面板发起 → 广播，日志/占位） ----------
  ipcMain.on(IPC_CH.newSession, (): void => {
    console.log('[voice] new session requested')
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send(IPC_CH.newSession)
      }
    }
  })

  // ---------- 退出应用 ----------
  ipcMain.on(IPC_CH.appQuit, (): void => {
    console.log('[app] quit requested')
    app.quit()
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
