/**
 * 后端网关 — 纯协议客户端（无 Electron 依赖，可在普通 Node 下独立测试）
 * --------------------------------------------------------------------------
 * 对接 Mock 后端（backend/mock_server.py）或后续同一契约的真实后端：
 *   WebSocket: ws://127.0.0.1:9100/ws（Mock；真实后端 8001 /ws/mgmt 同契约）
 * 协议：backend/docs/MOCK_CONTRACT.md（方案 A）
 *   - 握手：连接建立后先发 { type:'auth', id, clientId, token } → auth:ok
 *   - 请求：{ type:'<op>', id, ...业务字段 } → 以 <op>:ok / <op>:error 关联 id 返回
 *   - 主动事件：chat:running / chat:send:start|delta|done / tts:start|end / mode:changed
 *
 * 职责：
 *   - 连接管理：断线指数退避重连、重连后重新 auth、心跳 ping
 *   - 请求关联：以 id 为键的 Promise 化 RPC（含超时）
 *   - 流式事件分发：chat:send 的 delta/done/running/tts 通过回调吐出
 *   - 对外事件：连接状态变化、mode:changed 广播
 *
 * 本类保持纯净：不 import electron，便于写独立 probe 脚本联调验证。
 */
import WebSocket from 'ws'
import net from 'net'
import type {
  HistoryPage,
  HistoryDetail,
  UserProfile,
  VoiceSettings,
  VoiceListResp,
  ModelConfig,
  ModelSavePayload,
  ModelCheckResult,
  ModelListResp
} from '../../preload/types'

/** 网关连接状态 */
export type GatewayStatus = 'connecting' | 'connected' | 'disconnected'

/** chat:send 流式事件的回调集合（由调用方订阅） */
export interface ChatStreamEvents {
  /** 服务端推送运行态（驱动前端"运行中/可中止"） */
  onRunning?: (running: boolean, sessionId?: string) => void
  /** 流式中间结果（delta 全文覆盖式，前端可实时追加） */
  onDelta?: (delta: { text: string; action: string | null; id?: string }) => void
  /** 生成完成（含最终回复文本与可选 TTS 音频 base64） */
  onDone?: (done: { id?: string; text: string; action: string | null; audio?: string }) => void
  /** TTS 播放开始/结束（驱动"说话动画"） */
  onTts?: (kind: 'start' | 'end') => void
}

/** 历史记录条目 */
// (类型定义统一在 preload/types.ts，此处只复用 HistoryPage / VoiceSettings)

/** 请求完成时的响应体（<op>:ok 的整包） */
export interface RpcResult {
  id?: string
  [key: string]: unknown
}

interface PendingRequest {
  op: string
  resolve: (value: RpcResult) => void
  reject: (err: Error) => void
  timer: NodeJS.Timeout
  /** chat:send 专有：流式事件回调 */
  events?: ChatStreamEvents
}

interface RequestOptions {
  /** 超时毫秒（默认 15000） */
  timeout?: number
  /** chat:send 专有：流式事件回调 */
  events?: ChatStreamEvents
}

/** 默认超时 */
const DEFAULT_TIMEOUT = 15_000

/** 各操作的成功事件类型（极少数与 <op>:ok 命名不一致的在此特判） */
function okTypeFor(op: string): string {
  if (op === 'chat:send') return 'chat:send:done' // 流式：以 done 收尾，无 chat:send:ok
  if (op === 'history:search') return 'history:list:ok' // Mock 对 search 也回 list:ok 结构
  return `${op}:ok`
}

/** 各操作的失败事件类型 */
function errorTypeFor(op: string): string {
  return `${op}:error`
}

export interface GatewayClientOptions {
  /** 主后端连续失败 N 次后自动切到的备用地址（如 Mock 9100） */
  fallbackUrl?: string
  /** 主后端连续失败多少次触发回退（默认 2：1s+2s 后即切备用，避免开发期等太久） */
  fallbackAfterAttempts?: number
}

export class GatewayClient {
  private ws: WebSocket | null = null
  private primaryUrl: string
  private url: string
  private fallbackUrl: string | null
  private fallbackAfterAttempts: number
  private clientId = 'desktop-1'
  private token = 'fake-token'

  private status: GatewayStatus = 'disconnected'
  private statusCbs = new Set<(status: GatewayStatus) => void>()
  private modeCbs = new Set<(mode: 'chat' | 'work') => void>()

  private pending = new Map<string, PendingRequest>()
  /** 进行中的 chat:send 流式会话：id → 事件回调（从发送持续到 chat:running(false)） */
  private chatStreams = new Map<string, ChatStreamEvents>()
  /** 进行中的 chat:send 请求 id（用于把 running/tts 事件分发给流式会话） */
  private activeChats = new Set<string>()

  private seq = 0
  private closedByUser = false
  private readyPromise: Promise<void> | null = null
  private resolveReady: (() => void) | null = null
  private heartbeatTimer: NodeJS.Timeout | null = null
  private reconnectTimer: NodeJS.Timeout | null = null
  /** 处于 fallback(Mock) 时，定期探测主后端端口；恢复后自动切回主端 */
  private primaryProbeTimer: NodeJS.Timeout | null = null
  private reconnectAttempt = 0
  private lastStatusAt = 0

  constructor(url: string, opts: GatewayClientOptions = {}) {
    this.primaryUrl = url
    this.url = url
    this.fallbackUrl = opts.fallbackUrl ?? null
    this.fallbackAfterAttempts = opts.fallbackAfterAttempts ?? 2
  }

  // ---------- 查询 ----------

  get isReady(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN && this.resolveReady !== null
  }

  get currentStatus(): GatewayStatus {
    return this.status
  }

  // ---------- 生命周期 ----------

  /** 建立连接（可重复调用；已连接则忽略） */
  connect(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return
    }
    this.closedByUser = false
    this._open()
  }

  /** 主动关闭（不做重连） */
  close(): void {
    this.closedByUser = true
    this._clearHeartbeat()
    this._clearPrimaryProbe()
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    try {
      this.ws?.removeAllListeners()
      this.ws?.close()
      this.ws?.terminate()
    } catch {
      /* ignore */
    }
    this.ws = null
    // 挂起的请求全部失败
    this._failAll(new Error('gateway closed'))
    this._setStatus('disconnected')
  }

  private _open(): void {
    this._setStatus('connecting')
    // 预创建 ready Promise：auth:ok 到达时若已有等待方（waitForReady）必然能被唤醒
    if (!this.readyPromise) {
      this.readyPromise = new Promise((resolve) => {
        this.resolveReady = resolve
      })
    }
    console.log(`[gw] connecting ${this.url}`)
    try {
      const ws = new WebSocket(this.url)
      this.ws = ws

      ws.on('open', () => {
        console.log('[gw] ws open, sending auth handshake')
        this.reconnectAttempt = 0
        this._sendRaw({ type: 'auth', id: `h-${this._nextId()}`, clientId: this.clientId, token: this.token })
      })

      ws.on('message', (data: Buffer | string) => this._onMessage(data))

      ws.on('error', (err) => {
        console.error('[gw] ws error:', err?.message ?? err)
      })

      ws.on('close', () => {
        console.log('[gw] ws closed, closedByUser=', this.closedByUser)
        this.ws = null
        this.resolveReady = null
        this.readyPromise = null
        this._clearHeartbeat()
        this._failAll(new Error('connection closed'))
        if (!this.closedByUser) this._scheduleReconnect()
        else this._setStatus('disconnected')
      })
    } catch (err) {
      console.error('[gw] connect threw:', err)
      this._scheduleReconnect()
    }
  }

  /** 指数退避重连：1s → 2s → 4s → … 最大 30s；主后端连续失败达阈值 → 回退备用地址 */
  private _scheduleReconnect(): void {
    if (this.closedByUser || this.reconnectTimer) return
    const wait = Math.min(30_000, 1000 * 2 ** this.reconnectAttempt)
    this.reconnectAttempt += 1
    this._setStatus('disconnected')
    // 主后端连续不可达 → 切备用（Mock 9100）；备用也持续不可达 → 回主后端重试。
    // 防止“粘死在备用地址”上：主后端恢复后能自动接回，无需重启 App。
    if (this.fallbackUrl && this.url !== this.fallbackUrl && this.reconnectAttempt >= this.fallbackAfterAttempts) {
      this.url = this.fallbackUrl
      this.reconnectAttempt = 0
      console.log(`[gw] primary backend unreachable, fallback to ${this.url}`)
    } else if (this.fallbackUrl && this.url === this.fallbackUrl && this.reconnectAttempt >= this.fallbackAfterAttempts) {
      this.url = this.primaryUrl
      this.reconnectAttempt = 0
      console.log(`[gw] fallback unreachable, back to primary ${this.url}`)
    } else {
      console.log(`[gw] reconnect in ${wait}ms (attempt ${this.reconnectAttempt})`)
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this._open()
    }, wait)
  }

  /** 等待连接就绪（已握手 auth:ok）。超时抛错（后端未连接时不阻塞 UI）。 */
  waitForReady(timeoutMs = 8_000): Promise<void> {
    if (this.isReady) return Promise.resolve()
    if (!this.readyPromise) {
      this.readyPromise = new Promise((resolve) => {
        this.resolveReady = resolve
      })
    }
    const base = this.readyPromise
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`backend not connected (timeout ${timeoutMs}ms)`))
      }, timeoutMs)
      base.then(
        () => {
          clearTimeout(timer)
          resolve()
        },
        () => {
          clearTimeout(timer)
          reject(new Error('connection closed'))
        }
      )
    })
  }

  // ---------- 对外事件订阅 ----------

  /** 订阅连接状态变化，返回取消订阅函数 */
  onStatus(cb: (status: GatewayStatus) => void): () => void {
    this.statusCbs.add(cb)
    return () => this.statusCbs.delete(cb)
  }

  /** 订阅 mode:changed 广播，返回取消订阅函数 */
  onModeChanged(cb: (mode: 'chat' | 'work') => void): () => void {
    this.modeCbs.add(cb)
    return () => this.modeCbs.delete(cb)
  }

  // ---------- 底层收发 ----------

  private _nextId(): number {
    this.seq += 1
    return this.seq
  }

  private _sendRaw(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj))
    }
  }

  /** 通用 RPC：等待就绪 → 发送 → 以 id 关联兑现，超时报错 */
  async request<T = RpcResult>(op: string, payload: Record<string, unknown> = {}, opts: RequestOptions = {}): Promise<T> {
    await this.waitForReady()
    const id = `r-${this._nextId()}`
    const timeout = opts.timeout ?? DEFAULT_TIMEOUT
    return new Promise<T>((resolve, reject) => {
      const p: PendingRequest = {
        op,
        resolve: (v) => resolve(v as T),
        reject,
        timer: setTimeout(() => {
          this.pending.delete(id)
          if (op === 'chat:send') {
            this.activeChats.delete(id)
            this.chatStreams.delete(id)
          }
          reject(new Error(`${op} 请求超时（${timeout}ms）`))
        }, timeout),
        events: opts.events,
      }
      this.pending.set(id, p)
      if (op === 'chat:send') {
        this.activeChats.add(id)
        this.chatStreams.set(id, opts.events ?? {})
      }
      this._sendRaw({ type: op, id, ...payload })
    })
  }

  /** 主动事件分发失败时清理 pending（连接断开/关闭） */
  private _failAll(err: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer)
      p.reject(err)
    }
    this.pending.clear()
    this.chatStreams.clear()
    this.activeChats.clear()
  }

  private _setStatus(s: GatewayStatus): void {
    if (this.status === s && Date.now() - this.lastStatusAt < 1000) return
    this.status = s
    this.lastStatusAt = Date.now()
    for (const cb of this.statusCbs) cb(s)
  }

  // ---------- 消息处理 ----------

  private _onMessage(data: Buffer | string): void {
    let msg: any
    try {
      msg = JSON.parse(data.toString())
    } catch {
      return
    }
    const t: string = msg?.type ?? ''
    if (!t) return

    // ── 握手 / 心跳 ──
    if (t === 'auth:ok') {
      console.log('[gw] handshake ok clientId=', msg.clientId)
      this._setStatus('connected')
      this.resolveReady?.()
      this._startHeartbeat()
      this._maybeStartPrimaryProbe()
      return
    }
    if (t === 'pong') {
      // 心跳回执（目前只发不验证，连接有收即有活）
      return
    }

    // ── 流式对话事件（无独立请求 id 的用 activeChats 分发）──
    if (t === 'chat:running') {
      const running = Boolean(msg.running)
      for (const id of this.activeChats) {
        this.chatStreams.get(id)?.onRunning?.(running, msg.sessionId)
      }
      // running=false 表示本轮流式会话结束：清理会话（含其后的 tts 已完成）
      if (!running) {
        for (const id of this.activeChats) this.chatStreams.delete(id)
        this.activeChats.clear()
      }
      return
    }
    if (t === 'chat:send:delta') {
      this.chatStreams.get(msg.id)?.onDelta?.({ text: String(msg.text ?? ''), action: msg.action ?? null, id: msg.id })
      return
    }
    if (t === 'chat:send:done') {
      const p = this.pending.get(msg.id)
      if (!p) return
      clearTimeout(p.timer)
      this.pending.delete(msg.id)
      // 注意：不清理 activeChats/chatStreams —— mock 在 done 之后还会发 tts:start/end
      const text = String(msg.reply?.text ?? '')
      this.chatStreams.get(msg.id)?.onDone?.({ id: msg.id, text, action: msg.reply?.action ?? null, audio: msg.audio ?? undefined })
      p.resolve(msg)
      return
    }
    if (t === 'tts:start' || t === 'tts:end') {
      const kind = t === 'tts:start' ? 'start' : 'end'
      for (const id of this.activeChats) {
        this.chatStreams.get(id)?.onTts?.(kind)
      }
      return
    }

    // ── 广播事件 ──
    if (t === 'mode:changed') {
      const m = msg.mode === 'work' ? 'work' : 'chat'
      for (const cb of this.modeCbs) cb(m)
      return
    }

    // ── 通用错误帧：后端用 _.error / _:error 返回（未知类型、校验失败等）──
    // 按 id 关联拒绝挂起的请求，避免「后端报错但前端一直等到超时」。
    if (t === '_.error' || t === '_:error') {
      const eid = msg.id
      // 握手被拒（auth token 与后端 MGMT_TOKEN 失配）：不静默挂死——
      // 断开触发退避重连；主端持续失配会自动回退 fallback（Mock），UI 不至于全挂。
      if (msg.code === 'E_UNAUTHORIZED' && typeof eid === 'string' && eid.startsWith('h-')) {
        console.error('[gw] auth rejected (MGMT_TOKEN mismatch?), closing and retrying...')
        try {
          this.ws?.close()
        } catch {
          /* ignore */
        }
        return
      }
      if (typeof eid === 'string') {
        const p = this.pending.get(eid)
        if (p) {
          clearTimeout(p.timer)
          this.pending.delete(eid)
          this.activeChats.delete(eid)
          this.chatStreams.delete(eid)
          p.reject(new Error(`${msg.code ?? 'E_UNKNOWN'}: ${msg.message ?? '请求失败'}`))
        }
      }
      return
    }

    // ── 通用请求关联 ──
    const id = msg.id
    if (id == null || typeof id !== 'string') return
    const p = this.pending.get(id)
    if (!p) return
    if (t === okTypeFor(p.op)) {
      clearTimeout(p.timer)
      this.pending.delete(id)
      this.activeChats.delete(id)
      p.resolve(msg)
    } else if (t === errorTypeFor(p.op)) {
      clearTimeout(p.timer)
      this.pending.delete(id)
      this.activeChats.delete(id)
      this.chatStreams.delete(id)
      p.reject(new Error(`${msg.code ?? 'E_UNKNOWN'}: ${msg.message ?? '请求失败'}`))
    }
  }

  // ---------- 心跳 ----------

  private _startHeartbeat(): void {
    this._clearHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      this._sendRaw({ type: 'ping', id: `p-${this._nextId()}` })
    }, 30_000)
  }

  private _clearHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  // ---------- 主后端探活（避免「粘死在 Mock 上」） ----------
  // 场景：主后端 8001 挂掉时网关会回退到 fallback(Mock 9100)，此时只要 Mock 一直
  // 可达，旧逻辑永远不会再尝试主后端——即使真实后端已恢复，面板仍显示 Mock 假数据。
  // 修复：处于 fallback 时，每 15s 探测一次主后端端口，一旦恢复立即切回主端。

  private _maybeStartPrimaryProbe(): void {
    this._clearPrimaryProbe()
    if (!this.fallbackUrl || this.url !== this.fallbackUrl) return // 已在主端，无需探活
    this.primaryProbeTimer = setInterval(() => this._probePrimaryNow(), 15_000)
    this._probePrimaryNow()
  }

  private _probePrimaryNow(): void {
    try {
      const u = new URL(this.primaryUrl)
      const port = Number(u.port || (u.protocol === 'wss:' ? 443 : 80))
      const sock = net.connect({ host: u.hostname, port }, () => {
        sock.destroy()
        if (this.url === this.primaryUrl) {
          this._clearPrimaryProbe() // 防御：已切回主端则停止探活
          return
        }
        console.log(`[gw] primary backend back online, switching back to ${this.primaryUrl}`)
        this._clearPrimaryProbe()
        this.close()
        this.url = this.primaryUrl
        this.connect()
      })
      sock.on('error', () => sock.destroy())
    } catch (err) {
      console.warn('[gw] primary probe failed:', err)
    }
  }

  private _clearPrimaryProbe(): void {
    if (this.primaryProbeTimer) {
      clearInterval(this.primaryProbeTimer)
      this.primaryProbeTimer = null
    }
  }

  // ---------- 业务 API（对应 MOCK_CONTRACT 各域） ----------

  /** chat:send — 流式对话。返回 done 时的响应；流式过程经 opts.events 回调。 */
  chatSend(mode: 'chat' | 'work', text: string, events: ChatStreamEvents, sessionId = 'mock-s1'): Promise<RpcResult> {
    return this.request('chat:send', { mode, text, sessionId }, { events, timeout: 60_000 })
  }

  /** chat:abort — 中止当前生成 */
  chatAbort(): Promise<RpcResult> {
    return this.request('chat:abort', { sessionId: 'mock-s1' })
  }

  /** history:list / history:search — 分页查询历史 */
  historyList(page = 1, pageSize = 20, mode?: 'chat' | 'work'): Promise<HistoryPage> {
    return this.request('history:list', { page, pageSize, mode: mode ?? 'chat' }) as Promise<HistoryPage>
  }

  historySearch(keyword: string, page = 1, pageSize = 20): Promise<HistoryPage> {
    return this.request('history:search', { keyword, page, pageSize }) as Promise<HistoryPage>
  }

  /** history:detail — 单个 session 的事件轨迹（抽屉展开，按 runId 分组） */
  historyDetail(sessionId: string): Promise<HistoryDetail> {
    return this.request('history:detail', { sessionId }) as Promise<HistoryDetail>
  }

  /** history:delete — 删除一个历史会话 */
  historyDelete(sessionId: string): Promise<RpcResult> {
    return this.request('history:delete', { sessionId })
  }

  personalityGet(): Promise<{ content: string }> {
    return this.request('personality:get')
  }

  personalitySet(content: string): Promise<RpcResult> {
    return this.request('personality:set', { content })
  }

  userGet(): Promise<UserProfile> {
    return this.request('user:get') as Promise<UserProfile>
  }

  userSet(profile: UserProfile): Promise<RpcResult> {
    return this.request('user:set', { profile })
  }

  voiceSettingsGet(): Promise<VoiceSettings> {
    return this.request('voice:settings:get') as Promise<VoiceSettings>
  }

  voiceSettingsSet(settings: VoiceSettings): Promise<VoiceSettings> {
    return this.request('voice:settings:set', {
      volume: settings.volume,
      pitch: settings.pitch,
      voice: settings.voice,
    }) as Promise<VoiceSettings>
  }

  /** voice:voices — 按当前 TTS 模型实时拉取音色列表 */
  voiceVoices(): Promise<VoiceListResp> {
    return this.request('voice:voices') as Promise<VoiceListResp>
  }

  /** model:get — 读取当前模型配置 + 所需 API 密钥状态 */
  modelGet(): Promise<ModelConfig> {
    return this.request('model:get') as Promise<ModelConfig>
  }

  /** model:set — 保存模型配置（写回后端 .env） */
  modelSet(payload: ModelSavePayload): Promise<ModelConfig> {
    return this.request('model:set', { ...payload }) as Promise<ModelConfig>
  }

  /** model:check — 检查模型配置（密钥就绪 + best-effort 连通性） */
  modelCheck(): Promise<ModelCheckResult> {
    return this.request('model:check') as Promise<ModelCheckResult>
  }

  /** model:list — 获取某组（llm/asr/tts/vision/video）的可用模型 */
  modelList(category: string): Promise<ModelListResp> {
    return this.request('model:list', { category }) as Promise<ModelListResp>
  }

  modeGet(): Promise<{ mode: 'chat' | 'work' }> {
    return this.request('mode:get')
  }

  modeSet(mode: 'chat' | 'work'): Promise<RpcResult> {
    return this.request('mode:set', { mode })
  }

  authPolicyGet(): Promise<{ policy: 'full' | 'ask' }> {
    return this.request('auth:policy:get')
  }

  authPolicySet(policy: 'full' | 'ask'): Promise<RpcResult> {
    return this.request('auth:policy:set', { policy })
  }
}

export default GatewayClient