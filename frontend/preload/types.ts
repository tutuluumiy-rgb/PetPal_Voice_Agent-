/**
 * IPC 通道与类型定义（主进程 / preload / 渲染进程三方共享）
 * --------------------------------------------------------------------------
 * 只定义类型与通道契约，不实现业务逻辑。
 * 业务实现见 main/ipc.ts，均为骨架 + TODO 注释。
 */

/** 全部 IPC 通道名（单一事实源） */
export const IPC_CH = {
  // 模式切换：渲染进程 → 主进程（main 维护 currentMode 全局状态）
  modeGet: 'mode:get',
  modeSwitch: 'mode-switch',
  // 模式变化广播：主进程 → 渲染进程（ASR 语音切换等主进程侧改动时推送）
  modeChanged: 'mode:changed',

  // 权限策略：渲染进程 → 主进程（main 维护 authPolicy 全局状态）
  authPolicyGet: 'auth-policy:get',
  authPolicySet: 'auth-policy:set',

  // 窗口控制：渲染进程 → 主进程
  panelOpen: 'panel:open',

  // 悬浮宠物拖拽：渲染进程 → 主进程（主进程节流 setPosition）
  dragStart: 'pet:drag-start',
  dragMove: 'pet:drag-move',
  dragEnd: 'pet:drag-end',

  // 悬浮设置面板窗口尺寸：渲染进程 → 主进程（invoke）
  // 面板 350×550 挂 document.body，220×240 窗口容纳不下：
  // 打开面板时窗口扩展至容纳「球体 + 右侧面板」，关闭时恢复
  panelHeight: 'panel:height',

  // 宠物可见性：渲染进程 → 主进程（隐藏/显示球体）
  petVisibleSet: 'pet:set-visible',
  // 宠物可见性查询：渲染进程 → 主进程（invoke）
  petVisibleGet: 'pet:get-visible',
  // 宠物可见性广播：主进程 → 渲染进程
  petVisibleChanged: 'pet:visible-changed',

  // 应用信息
  appVersion: 'app:version',

  // TTS 事件：主进程 → 渲染进程（预留，TODO 后续由语音服务触发）
  ttsStart: 'tts-start',
  ttsEnd: 'tts-end'
} as const

/** 全局工作模式 */
export type PetMode = 'chat' | 'work'

/** 权限策略（完全批准 / 请求批准） */
export type AuthPolicy = 'full' | 'ask'

/** 拖拽采样点（screenX/screenY 为物理像素，与 setPosition 坐标系一致） */
export interface DragPoint {
  screenX: number
  screenY: number
}

/**
 * preload 暴露给渲染进程的完整 API 面。
 * 渲染进程通过 window.api 调用，类型经 index.d.ts 全局注入。
 */
export interface AppApi {
  /** 读取当前模式（chat 闲聊 / work 工作） */
  getMode(): Promise<PetMode>
  /** 通知主进程切换模式（IPC: mode-switch） */
  switchMode(mode: PetMode): void
  /** 订阅主进程模式变化广播（ASR 语音切换等），返回取消订阅函数 */
  onModeChanged(callback: (mode: PetMode) => void): () => void

  /** 读取当前权限策略（full 完全批准 / ask 请求批准） */
  getAuthPolicy(): Promise<AuthPolicy>
  /** 通知主进程更新权限策略（IPC: auth-policy:set） */
  setAuthPolicy(policy: AuthPolicy): void

  /** 打开（或聚焦）独立控制面板窗口 */
  openPanel(): void

  /** 拖拽开始：记录鼠标与窗口位置偏移 */
  dragStart(point: DragPoint): void
  /** 拖拽移动：主进程节流执行 setPosition */
  dragMove(point: DragPoint): void
  /** 拖拽结束：清理节流定时器 */
  dragEnd(): void

  /**
   * 调整上下文对话面板所需窗口尺寸（面板 350×550 挂 document.body，
   * 窗口 578×798 扩展容纳「球体 + 面板」；传 0 恢复 220×240）
   * @param panelHeight    面板高度（0 = 关闭）
   * @param ballScreen     球体屏幕坐标（保持球体屏幕位置不变）
   * @param ballInCanvas   球体在画布内的左上角坐标（精灵尺寸变化时锚点自适应）
   * @returns 实际窗口位置（渲染进程据此补偿 canvas 位置，球体屏幕位置恒定）
   */
  setPanelHeight(
    panelHeight: number,
    ballScreen?: { x: number; y: number },
    ballInCanvas?: { x: number; y: number }
  ): Promise<{ x: number; y: number } | undefined>

  /** 设置宠物（球体）可见性（false = 隐藏，只能从控制面板重新开启） */
  setPetVisible(visible: boolean): void
  /** 读取宠物可见性（控制面板「重新显示宠物」用） */
  getPetVisible(): Promise<boolean>
  /** 订阅宠物可见性广播（主进程 → 渲染进程），返回取消订阅函数 */
  onPetVisibleChanged(callback: (visible: boolean) => void): () => void

  /** 获取应用版本号（package.json version） */
  getAppVersion(): Promise<string>

  /** 订阅 TTS 开始事件（主进程 → 渲染进程），返回取消订阅函数 */
  onTtsStart(callback: () => void): () => void
  /** 订阅 TTS 结束事件，返回取消订阅函数 */
  onTtsEnd(callback: () => void): () => void
}
