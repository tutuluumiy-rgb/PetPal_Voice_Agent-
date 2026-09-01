/**
 * 后端网关 — Electron 主进程单例（把 GatewayClient 接到 IPC/窗口广播）
 * --------------------------------------------------------------------------
 * 承担主进程内的桥接职责：
 *   - 维护唯一 GatewayClient（连接 Mock 后端 9000 / 未来真实后端，同一契约）
 *   - 网关状态变化 → 广播 `backend:status` 到所有窗口
 *   - 网关 mode:changed → 同步本地 setMode（其订阅者再广播到所有窗口）
 *   - chat:send 流式事件 → 广播 chat:running / chat:delta / chat:done / tts:event
 *   - 供 main/ipc.ts 调用的各域 API（内部先 waitForReady）
 *
 * 渲染进程不直接碰网络（仅语音音频 WS 例外），全部走本网关经 IPC 暴露。
 */
import { BrowserWindow } from 'electron'
import { IPC_CH } from '../../preload/types'
import type { PetMode, AuthPolicy, HistoryPage, HistoryDetail, UserProfile, VoiceSettings, VoiceListResp, ModelConfig, ModelSavePayload, ModelCheckResult, ModelListResp } from '../../preload/types'
import { getAuthPolicy, getMode, setMode, setAuthPolicy } from '../state'
import { GatewayClient } from './gateway'
import type { ChatStreamEvents } from './gateway'

/** 管理端点：默认真实后端 /ws/mgmt（8001）；连不上自动回退 Mock 9100（环境变量可覆盖主地址） */
const BACKEND_WS_URL = process.env['PETPAL_MGMT_WS_URL'] ?? 'ws://127.0.0.1:8001/ws/mgmt'
const MOCK_WS_URL = process.env['PETPAL_MOCK_WS_URL'] ?? 'ws://127.0.0.1:9100/ws'

function broadcast(channel: string, payload?: unknown): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send(channel, payload)
    }
  }
}

class BackendGateway {
  private client = new GatewayClient(BACKEND_WS_URL, { fallbackUrl: MOCK_WS_URL })
  private started = false

  /** 主进程启动时调用一次：建立连接 + 订阅事件 */
  init(): void {
    if (this.started) return
    this.started = true

    // 连接状态 → 广播到全部窗口（UI 可展示"后端已连接/未连接"）
    this.client.onStatus((status) => {
      console.log(`[gw] status: ${status}`)
      broadcast(IPC_CH.backendStatus, { state: status })
    })

    // 服务端广播的 mode:changed → 同步本地全局状态（其订阅者会再广播到所有窗口）
    this.client.onModeChanged((mode) => {
      console.log('[gw] mode:changed received:', mode)
      setMode(mode)
    })

    this.client.connect()
  }

  /** 是否已握手完成（网关 ready） */
  get ready(): boolean {
    return this.client.isReady
  }

  // ---------- 对话（流式） ----------

  /**
   * chat:send — 发起一次文本对话。
   * 流式过程（running/delta/done/tts）通过 IPC 广播给所有窗口。
   * @returns done 时的响应体（含 reply/audio）
   */
  chatSend(mode: PetMode, text: string): Promise<unknown> {
    const events: ChatStreamEvents = {
      onRunning: (running, sessionId) => broadcast(IPC_CH.chatRunning, { running, sessionId }),
      onDelta: (delta) => broadcast(IPC_CH.chatDelta, delta),
      onDone: (done) => broadcast(IPC_CH.chatDone, done),
      onTts: (kind) => broadcast(IPC_CH.ttsEvent, { kind }),
    }
    return this.client.chatSend(mode, text, events)
  }

  /** chat:abort — 中止当前生成/TTS */
  chatAbort(): Promise<unknown> {
    return this.client.chatAbort()
  }

  // ---------- 历史 ----------

  historyList(page: number, pageSize: number, mode?: PetMode): Promise<HistoryPage> {
    return this.client.historyList(page, pageSize, mode)
  }

  historySearch(keyword: string, page: number, pageSize: number): Promise<HistoryPage> {
    return this.client.historySearch(keyword, page, pageSize)
  }

  /** session 事件轨迹（抽屉展开） */
  historyDetail(sessionId: string): Promise<HistoryDetail> {
    return this.client.historyDetail(sessionId)
  }

  /** 删除一个历史会话 */
  historyDelete(sessionId: string): Promise<unknown> {
    return this.client.historyDelete(sessionId)
  }

  // ---------- 人设 / 用户档案 ----------

  personalityGet(): Promise<{ content: string }> {
    return this.client.personalityGet()
  }

  personalitySet(content: string): Promise<unknown> {
    return this.client.personalitySet(content)
  }

  userGet(): Promise<UserProfile> {
    return this.client.userGet()
  }

  userSet(profile: UserProfile): Promise<unknown> {
    return this.client.userSet(profile)
  }

  // ---------- 语音参数 ----------

  voiceSettingsGet(): Promise<VoiceSettings> {
    return this.client.voiceSettingsGet()
  }

  voiceSettingsSet(settings: VoiceSettings): Promise<VoiceSettings> {
    return this.client.voiceSettingsSet(settings)
  }

  // ---------- 音色列表 / 模型配置 ----------

  voiceVoices(): Promise<VoiceListResp> {
    return this.client.voiceVoices()
  }

  modelGet(): Promise<ModelConfig> {
    return this.client.modelGet()
  }

  modelSet(payload: ModelSavePayload): Promise<ModelConfig> {
    return this.client.modelSet(payload)
  }

  modelCheck(): Promise<ModelCheckResult> {
    return this.client.modelCheck()
  }

  modelList(type: string): Promise<ModelListResp> {
    return this.client.modelList(type)
  }

  // ---------- 模式 / 权限（网关优先，未连接回退本地状态） ----------

  async modeGet(): Promise<PetMode> {
    if (this.client.isReady) {
      try {
        const r = await this.client.modeGet()
        if (r.mode === 'chat' || r.mode === 'work') {
          // 与本地状态对齐
          setMode(r.mode)
          return r.mode
        }
      } catch (e) {
        console.warn('[gw] mode:get failed, fallback local:', e)
      }
    }
    return getMode()
  }

  /** 切换模式：同步本地 + 通知网关（服务端会广播 mode:changed 回到本地，幂等） */
  modeSet(mode: PetMode): void {
    setMode(mode)
    if (this.client.isReady) {
      this.client.modeSet(mode).catch((e) => console.warn('[gw] mode:set failed:', e))
    }
  }

  async authPolicyGet(): Promise<AuthPolicy> {
    if (this.client.isReady) {
      try {
        const r = await this.client.authPolicyGet()
        if (r.policy === 'full' || r.policy === 'ask') {
          setAuthPolicy(r.policy)
          return r.policy
        }
      } catch (e) {
        console.warn('[gw] auth:policy:get failed, fallback local:', e)
      }
    }
    return getAuthPolicy()
  }

  authPolicySet(policy: AuthPolicy): void {
    setAuthPolicy(policy)
    if (this.client.isReady) {
      this.client.authPolicySet(policy).catch((e) => console.warn('[gw] auth:policy:set failed:', e))
    }
  }

  /** 退出时关闭网关 */
  dispose(): void {
    this.client.close()
  }
}

/** 主进程单例 */
export const backendGateway = new BackendGateway()