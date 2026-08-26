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
  petAnimChanged: 'pet-anim:changed',

  // 语音界面状态（连接/待机/聆听/播报）：对话面板 → 主进程广播 → 宠物窗口消息条指示灯
  voiceState: 'voice-state',
  voiceStateChanged: 'voice-state:changed',

  // 新建会话：对话面板 → 主进程广播（仅用于日志/占位，实际动作在前端重连语音 WS）
  newSession: 'new-session',

  // 退出应用：渲染进程 → 主进程（send）
  appQuit: 'app-quit',

  // ── 后端网关（主进程 → Mock/真实后端 9000，协议见 backend/docs/MOCK_CONTRACT.md）──
  // 连接状态广播：主进程 → 渲染进程（connecting/connected/disconnected）
  backendStatus: 'backend:status',
  // 文本对话（流式）：渲染进程 → 主进程（send），附 { text, mode }
  chatSend: 'chat:send',
  // 中止当前生成：渲染进程 → 主进程（send）
  chatAbort: 'chat:abort',
  // 流式运行态：主进程 → 渲染进程（chat:running）
  chatRunning: 'chat:running',
  // 流式中间结果：主进程 → 渲染进程（chat:send:delta）
  chatDelta: 'chat:delta',
  // 生成完成：主进程 → 渲染进程（chat:send:done，含 reply/audio）
  chatDone: 'chat:done',
  // TTS 开始/结束：主进程 → 渲染进程（tts:start/tts:end）
  ttsEvent: 'tts:event',

  // 历史记录：渲染进程 → 主进程（invoke）
  historyList: 'history:list',
  historySearch: 'history:search',
  // 人设 / 用户档案（invoke）
  personalityGet: 'personality:get',
  personalitySet: 'personality:set',
  userGet: 'user:get',
  userSet: 'user:set',
  // 语音参数（invoke）
  voiceSettingsGet: 'voice:settings:get',
  voiceSettingsSet: 'voice:settings:set',
  // 音色列表（按当前 TTS 模型实时拉取，invoke）
  voiceVoices: 'voice:voices',
  // 模型配置（当前模型 + 所需 API 密钥）：查询 / 保存 / 检查 / 获取可用模型（invoke）
  modelGet: 'model:get',
  modelSet: 'model:set',
  modelCheck: 'model:check',
  modelList: 'model:list',

  // 历史审详情（run 事件轨迹）：渲染进程 → 主进程（invoke）
  historyDetail: 'history:detail',
  // 删除会话（invoke）
  historyDelete: 'history:delete',

  // 皮肤主题：渲染进程 → 主进程（invoke / send）
  skinGet: 'skin:get',
  skinSet: 'skin:set',
  // 皮肤变化广播：主进程 → 渲染进程
  skinChanged: 'skin:changed',

  // 宠物动画诊断：渲染进程 → 主进程（日志打到主进程终端，便于查看素材就绪状态）
  animDebug: 'anim:debug'
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

// ── 后端网关共享数据类型（协议 MOCK_CONTRACT 方案 A，主进程/渲染进程共用） ──

/** 网关连接状态（广播 backend:status 的 payload） */
export type BackendStatusState = 'connecting' | 'connected' | 'disconnected'

export interface BackendStatusPayload {
  state: BackendStatusState
}

/** 历史记录条目（session 粒度：一次对话=一个 session，内含多轮 run） */
export interface HistoryItem {
  sessionId?: string
  mode?: PetMode
  time: number
  preview: string
  msgCount?: number
  runCount?: number
}

/** 历史分页结果 */
export interface HistoryPage {
  items: HistoryItem[]
  total: number
  page: number
}

/** session 事件轨迹（history:detail 事件流；runId 变化可做按轮分组） */
export interface HistoryEvent {
  ts?: number
  runId?: string
  kind: 'user' | 'assistant' | 'tool' | 'tool_result' | 'system'
  text?: string
  name?: string
  args?: Record<string, unknown>
  subTurn?: number
}

export interface HistoryDetail {
  sessionId?: string
  title: string
  events: HistoryEvent[]
}

/** 用户档案（users/<ACTIVE_USER>/profile.json 结构化） */
export interface UserProfile {
  basic: { name: string; role: string }
  reply_style?: string
  likes?: string[]
  dislikes?: string[]
  daily?: { wake_time?: string; sleep_time?: string }
}

/** 皮肤主题（深色 / 浅色·白底黑字） */
export type Skin = 'dark' | 'light'

/** 语音界面状态（消息条指示灯：idle=待机橙 / listening·speaking=对话绿 / off=未启用灰） */
export type VoiceUiState = 'off' | 'idle' | 'listening' | 'speaking'

export interface VoiceStatePayload {
  state: VoiceUiState
}

/** 语音参数（volume/pitch 0-100；voice 为音色 id，见 VoiceInfo） */
export interface VoiceSettings {
  volume: number
  pitch: number
  voice: string
}

/** 音色项（voice:voices 返回） */
export interface VoiceInfo {
  id: string
  label: string
}

/** 音色列表响应（按当前 TTS 模型实时拉取） */
export interface VoiceListResp {
  model?: string
  current: string
  voices: VoiceInfo[]
}

/** 模型配置组（llm/asr/tts/vision/video 各一组；api_key_masked 掩码显示，绝不回传明文） */
export interface ModelSection {
  type: string
  label: string
  hint?: string
  sub?: string
  url: string
  model: string
  voice?: string
  api_key_set: boolean
  api_key_env: string
  api_key_masked?: string
}

/** 模型配置（model:get / model:set 返回，5 组） */
export interface ModelConfig {
  llm: ModelSection
  asr: ModelSection
  tts: ModelSection
  vision: ModelSection
  video: ModelSection
}

/** 单组保存字段（只提交用户改动的） */
export interface ModelSectionSave {
  url?: string
  api_key?: string
  model?: string
  voice?: string
}

/** 模型保存 payload */
export interface ModelSavePayload {
  sections?: Record<string, ModelSectionSave>
}

/** 模型检查：单条密钥就绪状态 */
export interface ModelCheckItem {
  key: string
  label: string
  status: 'ok' | 'missing'
  detail: string
  model?: string
}

/** 模型检查：best-effort LLM 连通性 */
export interface ModelCheckLive {
  status: 'ok' | 'fail' | 'skipped'
  detail: string
  latency_ms?: number
}

/** 模型检查结果（model:check 返回） */
export interface ModelCheckResult {
  ok: boolean
  checks: ModelCheckItem[]
  live: ModelCheckLive
  required: string[]
}

/** 可用模型项（model:list 返回） */
export interface ModelListItem {
  id: string
  label: string
}

/** 可用模型列表（model:list 返回） */
export interface ModelListResp {
  category: string
  label: string
  models: ModelListItem[]
}

/** 流式对话运行态（chat:running payload） */
export interface ChatRunningPayload {
  running: boolean
  sessionId?: string
}

/** 流式中间结果（chat:send:delta payload） */
export interface ChatDeltaPayload {
  id?: string
  text: string
  action: string | null
}

/** 生成完成（chat:send:done payload） */
export interface ChatDonePayload {
  id?: string
  text: string
  action: string | null
  audio?: string
}

/** TTS 播放事件（tts:event payload） */
export interface TtsEventPayload {
  kind: 'start' | 'end'
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

  // ── 后端网关（主进程 WebSocket → 9000，协议见 backend/docs/MOCK_CONTRACT.md）──

  /** 订阅网关连接状态广播（主进程 → 渲染进程），返回取消订阅函数 */
  onBackendStatus(callback: (payload: BackendStatusPayload) => void): () => void

  /** 发送一条文本消息给后端（流式：走 chat:send，主进程广播 chat:running/delta/done） */
  chatSend(text: string, mode: PetMode): void
  /** 中止当前生成的 LLM 回复 / TTS */
  chatAbort(): void
  /** 订阅流式运行态（chat:running），返回取消订阅函数 */
  onChatRunning(callback: (payload: ChatRunningPayload) => void): () => void
  /** 订阅流式中间结果（chat:send:delta），返回取消订阅函数 */
  onChatDelta(callback: (payload: ChatDeltaPayload) => void): () => void
  /** 订阅生成完成（chat:send:done），返回取消订阅函数 */
  onChatDone(callback: (payload: ChatDonePayload) => void): () => void
  /** 订阅 TTS 播放事件（tts:event），返回取消订阅函数 */
  onTtsEvent(callback: (payload: TtsEventPayload) => void): () => void

  /** 查询历史记录（分页） */
  historyList(page: number, pageSize: number, mode?: PetMode): Promise<HistoryPage>
  /** 搜索历史记录（分页） */
  historySearch(keyword: string, page: number, pageSize: number): Promise<HistoryPage>
  /** 查询单个 session 的事件轨迹（抽屉展开；按 runId 分组） */
  historyDetail(sessionId: string): Promise<HistoryDetail>
  /** 删除一个历史会话 */
  historyDelete(sessionId: string): Promise<void>

  /** 读取人设 markdown */
  personalityGet(): Promise<{ content: string }>
  /** 保存人设 markdown */
  personalitySet(content: string): Promise<void>
  /** 读取用户档案（结构化 profile.json） */
  userGet(): Promise<UserProfile>
  /** 保存用户档案（结构化） */
  userSet(profile: UserProfile): Promise<void>

  /** 读取语音参数（音量/音调/音色） */
  voiceSettingsGet(): Promise<VoiceSettings>
  /** 保存语音参数 */
  voiceSettingsSet(settings: VoiceSettings): Promise<VoiceSettings>

  /** 拉取当前 TTS 模型可用音色列表（实时） */
  voiceVoices(): Promise<VoiceListResp>

  /** 读取当前模型配置（当前模型 + 所需 API 密钥状态） */
  modelGet(): Promise<ModelConfig>
  /** 保存模型配置（写回后端 .env） */
  modelSet(payload: ModelSavePayload): Promise<ModelConfig>
  /** 检查模型配置（各必需密钥就绪 + best-effort 连通性） */
  modelCheck(): Promise<ModelCheckResult>
  /** 获取某组（llm/asr/tts/vision/video）的可用模型 */
  modelList(type: string): Promise<ModelListResp>

  /** 读取皮肤主题 */
  getSkin(): Promise<Skin>
  /** 切换皮肤主题（主进程状态，广播到所有窗口） */
  setSkin(skin: Skin): void
  /** 订阅皮肤主题变化广播 */
  onSkinChanged(callback: (skin: Skin) => void): () => void

  /** 上报语音界面状态（对话面板 → 主进程广播给宠物窗口消息条指示灯） */
  voiceState(payload: VoiceStatePayload): void
  /** 订阅语音界面状态广播（宠物窗口接收） */
  onVoiceState(callback: (payload: VoiceStatePayload) => void): () => void

  /** 上报"新建会话"动作（对话面板发起；日志/广播用途） */
  notifyNewSession(): void
  /** 订阅新建会话广播（可选扩展点） */
  onNewSession(callback: () => void): () => void

  /** 退出应用（渲染进程 → 主进程，app.quit()） */
  quitApp(): void

  /** 上报宠物动画诊断信息（渲染进程 → 主进程，打印到主进程终端） */
  reportAnimDebug(message: string): void
}
