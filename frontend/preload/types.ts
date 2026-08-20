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

  // 独立对话面板窗口：渲染进程 → 主进程（send）
  // 面板独立成透明窗口（350×550），由宠物窗口位置定位；宠物窗口尺寸恒定、
  // canvas 无需补偿。打开/关闭不影响宠物窗口本身。
  chatPanelOpen: 'chat-panel:open',
  chatPanelClose: 'chat-panel:close',

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
  ttsEnd: 'tts-end',

  // 唤醒词（KWS）：渲染进程 → 主进程（喂 16k 浮点音频帧；主进程 sherpa-onnx-node 推理）
  kwsFeed: 'kws:feed',
  // 唤醒命中：主进程 → 渲染进程（广播 keyword，渲染进程据此进对话）
  kwsWake: 'kws:wake',

  // 语音播报预览（宠物窗口底部消息条）：
  // 对话面板(chat) → 主进程：推送一条实时语音播报文本（invoke，落地广播）
  voicePreviewPush: 'voice-preview:push',
  // 主进程 → 宠物窗口：广播最新语音播报文本（宠物底部消息条滚动显示）
  voicePreview: 'voice-preview',
  // 宠物窗口 → 主进程：读取最近一次语音播报文本（挂载回放用）
  voicePreviewGet: 'voice-preview:get',

  // 宠物动画状态联动：
  // 对话面板(chat) → 主进程：通知宠物进入 'speaking' / 'idle'（说话开始/结束）
  petAnim: 'pet-anim',
  // 主进程 → 宠物窗口：广播最新动画状态
  petAnimChanged: 'pet-anim:changed'
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
   * 打开独立对话面板窗口（350×550 透明窗口，由宠物窗口位置定位，
   * 位于宠物左侧，空间不足切右侧）。不影响宠物窗口尺寸/位置。
   */
  openChatPanel(): void
  /** 关闭（隐藏）独立对话面板窗口；不影响宠物窗口。 */
  closeChatPanel(): void

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

  /** 喂入一段 16k 浮点音频帧给主进程 KWS（待机时每帧调用） */
  kwsFeed(frame: Float32Array): void
  /** 订阅主进程唤醒命中广播（KWS 命中 keyword），返回取消订阅函数 */
  onKwsWake(callback: (keyword: string) => void): () => void

  /**
   * 推送一条实时语音播报文本（对话面板调用 → 主进程广播到宠物窗口底部消息条）。
   * @param text 一段语音播报文本（空串则清空当前预览）
   */
  pushVoicePreview(text: string): void
  /** 订阅语音播报文本广播（宠物窗口底部消息条接收），返回取消订阅函数 */
  onVoicePreview(callback: (text: string) => void): () => void
  /** 读取最近一次语音播报文本（宠物窗口底部消息条挂载回放） */
  getVoicePreview(): Promise<string>

  /**
   * 通知宠物进入动画状态（对话面板说话开始/结束调用）。
   * @param state 'speaking' | 'idle'
   */
  setPetAnim(state: 'speaking' | 'idle'): void
  /** 订阅宠物动画状态广播（宠物窗口接收），返回取消订阅函数 */
  onPetAnimChanged(callback: (state: 'speaking' | 'idle') => void): () => void
}
