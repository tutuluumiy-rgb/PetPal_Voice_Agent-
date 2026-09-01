/**
 * VoicePipeline — Electron renderer 语音管线（后端职责提供）
 * --------------------------------------------------------------------------
 * 对接【真实后端 8001】（/ws/audio，测试看板那套协议）：
 *   前端→后端：二进制 PCM（Int16 16k）+ JSON 控制消息
 *              { type:'speech_start', preRollBase64, isPlaying } / { type:'speech_end' } / { type:'vad_cancel' }
 *   后端→前端：JSON（ready/asr_partial/asr_final/reply_start/reply/reply_append/reply_end/
 *              tts_start/tts_end/barge_confirm/barge_reject/stop_playback/mode_changed）+ 二进制 TTS PCM(24k)
 *            mode_changed：语音切模式 → onModeChanged 回调 → UI 同步主进程 → 面板/动画三端一致
 *
 * 基于测试看板（testboard）验证过的采集 + Silero VAD 逻辑迁移；renderer 直连音频 WS。
 * 依赖 renderer/assets/vad/ 的 UMD 库（onnxruntime-web + @ricky0123/vad-web）。
 */
// 预卷纯函数（改造清单#1）：锚点 = VAD 判定窗口起点（非判定完成时刻），回退 = 爬坡期+余量
import { computePreRollWindowMs, PRE_ROLL_MS as PRE_ROLL_DEFAULT_MS } from './preRoll'

declare global {
  interface Window {
    vad?: any
    ort?: any
  }
}

export interface VoicePipelineOptions {
  /** 后端语音 WebSocket 地址（真实后端: ws://127.0.0.1:8001/ws/audio） */
  wsUrl: string
  /** UMD 库基础路径（默认 assets/vad/） */
  vadAssetsBase?: string
  positiveThreshold?: number
  negativeThreshold?: number
}

export class VoicePipeline {
  private ws: WebSocket | null = null
  private audioCtx: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private processor: ScriptProcessorNode | null = null
  private vad: any = null
  private running = false
  private playbackCtx: AudioContext | null = null
  private gainNode: GainNode | null = null
  private _playbackState: { nextStartTime: number; active: AudioBufferSourceNode[] } | null = null

  // ── 打断状态（ducking 体感层 + 后端确认后丢弃音频）──
  // 对照 testboard/index.html 同名变量，行为一致；移过来补齐 Electron 端缺失的打断机制
  /** DUCKING_GAIN：ducking 时把播放音量降到这个比例（0.2 = 20%）。
   *  不是静音——继续播报，只是小声，等后端确认后再决定静音/恢复。 */
  private readonly DUCKING_GAIN = 0.2
  /** ducking 超时兜底（ms）：ducking 生效后既没 barge_confirm/reject 也没 onSpeechEnd/misfire
   *  触发（消息丢失/竞态），超时强制恢复音量——修复误报后音量卡死。 */
  private readonly DUCKING_TIMEOUT_MS = 2000
  /** barge_confirm 后等结果的兜底（ms）：3s 内既没 asr_final 也没 resume_playback，
   *  强制销毁播放器，防 pendingBargeResume 卡死后续对话。 */
  private readonly BARGE_RESUME_TIMEOUT_MS = 3000
  /** playback_done 兜底（ms）：tts_start 后 15s 还没自然播完，强制补发 client_playback_done，
   *  防后端永远收不到播放完成 → 打断窗口不闭合。 */
  private readonly PLAYBACK_DONE_TIMEOUT_MS = 15000

  private duckingTimer: ReturnType<typeof setTimeout> | null = null
  private pendingPlaybackDone = false // 已收 reply_end，等音频真正播完
  private bargeConfirmed = false       // 后端已发 barge_confirm（misfire 不再反向恢复）
  /** barge_confirm 后的「静音等待」状态：期间到达的 audio bytes 一律 drop（防止播旧内容），
   *  等 asr_final（有效→销毁）/ resume_playback（无内容→恢复播完）/ 3s 兜底 退出。 */
  private pendingBargeResume = false
  private bargeResumeTimer: ReturnType<typeof setTimeout> | null = null
  private playbackDoneTimer: ReturnType<typeof setTimeout> | null = null
  private speechStartTime: number | null = null  // 用户开口时刻（performance.now），算打断延迟
  /** 真实端到端测量：用户说话结束时刻（VAD onSpeechEnd）→ 第一帧音频出声（_onAudio 首块 start） */
  private userSpeechFinishTs: number | null = null
  private playFirstFrameTs: number | null = null

  // ── 环形缓存（pre-roll ring buffer）──
  // 持续保留最近 2 秒的 PCM 音频，打断时回退取「开口前」的预卷，补 VAD 触发延迟/后端
  // 保护期吞掉的首字（对照 testboard/index.html）。Electron 早期版本没实现 preRoll，
  // 打断后后端只能靠"含球球回声的 speaking_audio_cache"补首字 → 前半句丢字/错位。
  private readonly PRE_ROLL_BUFFER_SECONDS = 2   // 环形缓存总长（秒）
  private readonly PRE_ROLL_MS = 256              // 回退量（毫秒），从VAD触发点往前取这么多
  /** 端点判定喂给 smart-turn 的"该说话段"长度上限（改造清单#7）。
   *  与上游 pipecat max_duration_secs=8 对齐：喂【整段话】而非"最近 N ms"——
   *  实测标注数据（scripts/smart_turn_probe.py，20 条）整段话判别 90% vs 尾部 1.6s 仅 80%，
   *  且尾部切片会把"未完"误判成"说完"（韵律上下文被截断）。 */
  private readonly SMART_TURN_MAX_MS = 8000
  private preRollBuffer: { data: Uint8Array; ts: number }[] = []  // 存 PCM 块（含到达时刻 ts）
  /** speech 窗口内的整段话缓冲（onSpeechStart 起累计，≥SMART_TURN_MAX_MS 保尾截断） */
  private utteranceAudio: Uint8Array[] = []
  private speechActive = false

  private readonly wsUrl: string
  private readonly vadBase: string
  private readonly posTh: number
  private readonly negTh: number

  /** 后端 ASR 最终文本 */
  onUserText?: (text: string) => void
  /** 后端 reply/reply_append 文本 */
  onReply?: (text: string, append: boolean) => void
  onState?: (state: 'idle' | 'listening' | 'speaking') => void
  onTtsEvent?: (kind: 'start' | 'end') => void
  /** 命中退出语（拜拜/再见…）回到待机/聆听时的回调（供 UI 提示） */
  onExit?: () => void
  /** 后端语音切模式（mode_changed：说"打开工作模式"等）→ 交给 UI 同步主进程 */
  onModeChanged?: (mode: 'chat' | 'work') => void
  /** 口语命中「新建会话」→ UI 清空消息（重连由本类 newSession() 执行） */
  onNewSession?: () => void

  // ---------- 唤醒词（KWS）待机：渲染进程采集 → 主进程 sherpa-onnx-node 推理 ----------
  private kwsUnsub: (() => void) | null = null
  private wakeWordOn = false
  private conversationStarted = false // 已从唤醒进入对话
  /** 唤醒词命中回调（供 UI 提示，如显示「听到你了」） */
  onWake?: (keyword: string) => void

  // ---------- 方案 A：唤醒后连续对话，空闲超时回待机；口语退出规则 ----------
  /** 对话空闲超时（毫秒）：超过无语音/无回复进展 → 回待机重新等唤醒 */
  private readonly conversationIdleMs = 45000
  private idleTimer: ReturnType<typeof setTimeout> | null = null

  constructor(opts: VoicePipelineOptions) {
    this.wsUrl = opts.wsUrl
    this.vadBase = opts.vadAssetsBase ?? '/vad/'
    this.posTh = opts.positiveThreshold ?? 0.6
    this.negTh = opts.negativeThreshold ?? 0.4
  }

  /**
   * 退出对话的口语规则（命中即结束本轮对话回待机，不是退出前端）。
   * 全角/半角、首尾空格可容错；与用户说法的子串匹配。
   */
  private readonly EXIT_WORDS: readonly string[] = [
    '拜拜', '再见', '退出聊天', '退出对话', '结束对话', '不聊了', '先这样吧', '聊到这',
    '下次再聊', '回头聊', '晚安', '去忙了', '我要忙了', '挂了吧', '回见', '退下吧',
  ]

  /** 口语「新建会话」：命中即清空上下文开始全新对话（不回待机），子串匹配。
   *  注意：「换话题」类词（换个话题/换一个话题/开始新话题…）不算新建会话——
   *  只是换个话题聊，不清理历史上下文，故不在此表中。 */
  private readonly NEW_SESSION_WORDS: readonly string[] = [
    '创建新对话', '创建新会话', '新建对话', '新建会话', '新开一个对话',
    '重新开始对话', '重新开始', '清除记忆', '重置对话', '忘掉之前', '不记得之前',
  ]

  get isRunning(): boolean {
    return this.running
  }

  /** 启动：连 WS、初始化 VAD、采集。wakeWord=true 时进入「待机听唤醒」模式。 */
  async start(opts?: { wakeWord?: boolean }): Promise<void> {
    if (this.running) return
    this.running = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: 16000,
        },
      })
      this.stream = stream

      const AC = window.AudioContext || (window as any).webkitAudioContext
      this.audioCtx = new AC({ sampleRate: 16000 })
      this.source = this.audioCtx.createMediaStreamSource(stream)
      this.processor = this.audioCtx.createScriptProcessor(1024, 1, 1)
      this.processor.onaudioprocess = (e) => {
        if (!this.running) return
        this._onAudioFrame(e.inputBuffer.getChannelData(0))
      }
      this.source.connect(this.processor)
      this.processor.connect(this.audioCtx.destination)
      // 兜底：新 AudioContext 默认可能 suspended，resume 保证采集循环真正跑起来
      // （否则 onaidioprocess 不触发，待机 KWS 与对话都无法持续喂帧）。
      if (this.audioCtx.state === 'suspended') {
        void this.audioCtx.resume().catch(() => { /* ignore */ })
      }

      // 是否以「待机听唤醒」启动：麦克风帧经 IPC 喂主进程 KWS，命中广播回来
      if (opts?.wakeWord) {
        this.wakeWordOn = true
        this.conversationStarted = false
        this.kwsUnsub = window.api.onKwsWake((kw) => this._onWakeHit(kw))
        this.onState?.('idle') // 待机：只监听唤醒
        return
      }

      // 直接进入对话（手动语音）
      await this._connectWs()
      await this._initVad()
      this.conversationStarted = true
      this.onState?.('listening')
    } catch (err) {
      this.running = false
      throw err
    }
  }

  /** 唤醒命中：进入对话（连 WS + VAD） */
  private _onWakeHit(keyword: string): void {
    console.log('[voice] 唤醒命中:', keyword)
    this.onWake?.(keyword)
    // 唤醒后立即让后端预热 TTS WS 长连接（fire-and-forget，不阻塞进对话）
    // 把首句合成的 5.5s 建连提前到唤醒时间窗内跑完 → 首包 2.7s
    // 仅 MiniMax WS transport 生效；HTTP/其他 provider 后端会 noop
    this._control({ type: 'tts_preheat' })
    void this._enterConversation()
  }

  /** 从待机进入对话 */
  private async _enterConversation(): Promise<void> {
    if (this.conversationStarted) return
    this.conversationStarted = true
    try {
      await this._connectWs()
      await this._initVad()
      this.onState?.('listening')
      this._armIdleTimer()
    } catch (err) {
      console.error('[voice] 进对话失败，回待机', err)
      this.conversationStarted = false
      this._clearIdleTimer()
      this.onState?.('idle')
    }
  }

  /** 回到待机（只监听唤醒，切断后端会话） */
  private _backToWake(): void {
    this._clearIdleTimer()
    if (this.vad) { try { this.vad.destroy() } catch { /* ignore */ } this.vad = null }
    this._closeWs()
    this.conversationStarted = false
    this.onState?.('idle')
  }

  private _closeWs(): void {
    if (this.ws) { try { this.ws.close() } catch { /* ignore */ } this.ws = null }
  }

  /** 对话空闲计时：有语音/回复进展就续期；超时回待机（仅对话态 & 唤醒模式）。 */
  private _armIdleTimer(): void {
    if (!this.wakeWordOn || !this.conversationStarted) return
    if (this.idleTimer) clearTimeout(this.idleTimer)
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null
      console.log('[voice] 对话空闲超时，回到待机')
      if (this.wakeWordOn && this.conversationStarted) this._backToWake()
    }, this.conversationIdleMs)
  }

  private _clearIdleTimer(): void {
    if (this.idleTimer) { clearTimeout(this.idleTimer); this.idleTimer = null }
  }

  /** 用户说法是否命中退出规则（口语退出对话，不退出前端）。 */
  private _isExitText(text: string): boolean {
    const t = (text || '').replace(/\s+/g, '')
    if (!t) return false
    return this.EXIT_WORDS.some((w) => t.includes(w.replace(/\s+/g, '')))
  }

  /** 用户说法是否命中「新建会话」规则（口语开启全新对话，仍然保持对话态）。 */
  private _isNewSessionText(text: string): boolean {
    const t = (text || '').replace(/\s+/g, '')
    if (!t) return false
    return this.NEW_SESSION_WORDS.some((w) => t.includes(w.replace(/\s+/g, '')))
  }

  /**
   * 主动/语音创建新会话：清掉当前上下文并重连语音 WS。
   * 后端按连接创建会话（main.py: 每次 /ws/audio 连接一个 ConversationSession），
   * 重连后即得到全新的会话（干净历史），前端继续留在对话态。
   */
  async newSession(): Promise<void> {
    console.log('[voice] 创建新会话：断开并重连语音后端')
    // 用 stopStreamPlayback 彻底销毁播放器（关 ctx + 清 gain），并清打断状态
    // 防止上一轮 pendingBargeResume / ducking 残留影响新会话
    if (this.bargeResumeTimer) { clearTimeout(this.bargeResumeTimer); this.bargeResumeTimer = null }
    if (this.playbackDoneTimer) { clearTimeout(this.playbackDoneTimer); this.playbackDoneTimer = null }
    if (this.duckingTimer) { clearTimeout(this.duckingTimer); this.duckingTimer = null }
    this.pendingBargeResume = false
    this.bargeConfirmed = false
    this.pendingPlaybackDone = false
    this.userSpeechFinishTs = null
    this.playFirstFrameTs = null
    this.preRollBuffer = []
    this.utteranceAudio = []
    this.speechActive = false
    this.stopStreamPlayback()
    this._closeWs()
    try {
      await this._connectWs()
      this._armIdleTimer()
      this.onState?.('listening')
    } catch (err) {
      console.error('[voice] 新建会话重连失败，回待机:', err)
      if (this.wakeWordOn) {
        this._backToWake()
      } else {
        this.conversationStarted = false
        this.onState?.('listening')
      }
    }
  }

  /** 统一音频帧分配：待机喂主进程 KWS（IPC），对话中喂后端 */
  private _onAudioFrame(input: Float32Array): void {
    if (this.wakeWordOn && !this.conversationStarted) {
      window.api.kwsFeed(input)
      return
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const pcm = new Int16Array(input.length)
    for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-1, Math.min(1, input[i])) * 32767
    // 写入环形缓存（持续保留最近 PRE_ROLL_BUFFER_SECONDS 秒，打断时切预卷补首字）
    this._writePreRoll(new Uint8Array(pcm.buffer))
    // speech 窗口内累计"整段话"（供 speech_end 喂 smart-turn 端点判定）
    if (this.speechActive) this._writeUtterance(new Uint8Array(pcm.buffer))
    this.ws.send(pcm.buffer) // 音频持续发送，不丢字
  }

  /** 写入话段缓冲：累计整段话，超出 SMART_TURN_MAX_MS 则丢弃最旧（保尾 ≤8s，
   *  与上游 max_duration_secs=8 / judge 内 8s 尾锚一致） */
  private _writeUtterance(bytes: Uint8Array): void {
    this.utteranceAudio.push(bytes)
    let total = 0
    for (const b of this.utteranceAudio) total += b.length
    const capBytes = (this.SMART_TURN_MAX_MS / 1000) * 16000 * 2
    while (total > capBytes && this.utteranceAudio.length > 0) {
      total -= this.utteranceAudio.shift()!.length
    }
  }

  /** 拼接整段话缓冲；为空返回 null */
  private _concatUtterance(): Uint8Array | null {
    if (this.utteranceAudio.length === 0) return null
    let total = 0
    for (const b of this.utteranceAudio) total += b.length
    const merged = new Uint8Array(total)
    let off = 0
    for (const c of this.utteranceAudio) {
      merged.set(c, off)
      off += c.length
    }
    return merged
  }

  /** 写入环形缓存：保留最近 PRE_ROLL_BUFFER_SECONDS 秒 PCM，超出则丢弃最旧块
   *  每块记录到达时刻 ts（供预卷按【VAD 判定窗口起点】时间窗切取，改造清单#1） */
  private _writePreRoll(bytes: Uint8Array): void {
    const ts = performance.now()
    this.preRollBuffer.push({ data: bytes, ts })
    // 累计总字节，超出 2 秒（16k*2*2=64000B）截掉最旧
    let total = 0
    for (const b of this.preRollBuffer) total += b.data.length
    while (total > this.PRE_ROLL_BUFFER_SECONDS * 16000 * 2 && this.preRollBuffer.length > 0) {
      total -= this.preRollBuffer.shift()!.data.length
    }
  }

  /** 从环形缓存切出预卷（改造清单#1）：
   *  锚点 = VAD 判定窗口起点（triggerTs 为 onSpeechStart 触发时刻），
   *  回退 = 爬坡期+余量（PRE_SPEECH_PAD_MS），总长 PRE_ROLL_MS；
   *  覆盖"[窗口起点 − pad, 窗口起点 − pad + 256ms]"≈ 开口最开头 + 开口初期。
   *  兜底：按时间窗没切到任何块时，回退取尾部 256ms（旧行为）。 */
  private _slicePreRoll(anchorTs: number | null = null): Uint8Array | null {
    if (this.preRollBuffer.length === 0) return null
    const refTs = anchorTs ?? this.preRollBuffer[this.preRollBuffer.length - 1].ts ?? performance.now()
    const { startMs, endMs } = computePreRollWindowMs(refTs)
    const chunks: Uint8Array[] = []
    let collected = 0
    for (const b of this.preRollBuffer) {
      if (b.ts == null) continue
      if (b.ts >= startMs && b.ts < endMs) {
        chunks.push(b.data)
        collected += b.data.length
      }
    }
    // 兜底：时间窗匹配为空 → 回退取尾部 PRE_ROLL_MS
    if (chunks.length === 0) {
      const targetBytes = Math.round((this.PRE_ROLL_MS || PRE_ROLL_DEFAULT_MS) / 1000 * 16000 * 2)
      for (let i = this.preRollBuffer.length - 1; i >= 0; i--) {
        chunks.unshift(this.preRollBuffer[i].data)
        collected += this.preRollBuffer[i].data.length
        if (collected >= targetBytes) break
      }
    }
    const merged = new Uint8Array(collected)
    let off = 0
    for (const c of chunks) {
      merged.set(c, off)
      off += c.length
    }
    return merged
  }

  /** 从环形缓存切出最近 ms 毫秒的音频段（供端点判定 smart-turn，改造清单#7） */
  private _sliceRecentMs(ms: number): Uint8Array | null {
    if (this.preRollBuffer.length === 0) return null
    const now = performance.now()
    const startTs = now - ms
    const chunks: Uint8Array[] = []
    let collected = 0
    for (const b of this.preRollBuffer) {
      if (b.ts == null) continue
      if (b.ts >= startTs) {
        chunks.push(b.data)
        collected += b.data.length
      }
    }
    if (chunks.length === 0) return null
    const merged = new Uint8Array(collected)
    let off = 0
    for (const c of chunks) {
      merged.set(c, off)
      off += c.length
    }
    return merged
  }

  private _arrayBufferToBase64(buffer: ArrayBuffer): string {
    let binary = ''
    const bytes = new Uint8Array(buffer)
    const chunkSize = 0x8000
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + chunkSize)))
    }
    return btoa(binary)
  }

  private async _initVad(): Promise<void> {
    const base = this.vadBase
    await this._loadScript(`${base}ort.min.js`)
    await this._loadScript(`${base}bundle.min.js`)
    // 配置 onnxruntime-web 的 WASM 路径
    if (window.ort?.env) window.ort.env.wasm.wasmPaths = base
    const vadLib = window.vad
    if (!vadLib?.MicVAD) throw new Error('VAD 库未加载（缺 /vad/bundle.min.js）')
    this.vad = await vadLib.MicVAD.new({
      modelURL: `${base}silero_vad.onnx`,
      workletURL: `${base}vad.worklet.bundle.min.js`,
      stream: this.stream!,
      // ── 前端 VAD 判定（体感层）：512 帧(32ms) → 连续 6 帧过阈值≈192ms 判定为人声 ──
      // 判定为人声 → 启动 ducking（音量降到 20% 继续播）+ 上报 speech_start 给后端
      // 后端做二次确认（~16ms）：barge_reject 恢复 / barge_confirm 进入静音等待
      frameSamples: 512,  // 对齐后端（512=32ms/帧）：触发更快（6帧≈192ms），帧级判定更细但噪声下更抖
      onSpeechStart: () => {
        console.log('[VAD] onSpeechStart（判定为人声 → ducking + 上报）')
        // 0. 记录用户开口时刻（用于算打断延迟：开口 → 停止播报）
        this.speechStartTime = performance.now()
        this.bargeConfirmed = false  // 新一轮判定开始，重置后端确认标记
        // 0.5 重置话段缓冲并以预卷种子覆盖开口初期（VAD 判定 6 帧≈192ms 期间已过的帧）
        this.utteranceAudio = []
        this.speechActive = true
        const seed = this._slicePreRoll(this.speechStartTime)
        if (seed && seed.byteLength > 0) this._writeUtterance(seed)
        // 1. 启动 ducking：gainNode.gain → 0.2，**不静音不销毁播放器**，只是小声继续播
        //    只有球球正在播放时才需要 ducking；球球没在播（如 listening 态）就跳过
        this.startDucking()
        // 2. 从环形缓存切预卷（改造清单#1：以判定窗口起点为锚，覆盖开口最开头）
        const preRoll = this._slicePreRoll(this.speechStartTime)
        const preRollBase64 = preRoll ? this._arrayBufferToBase64(preRoll.buffer as ArrayBuffer) : null
        // 3. 上报 speech_start 给后端（带预卷 + 前端是否仍在播）。
        //    isPlaying：若后端已 listening（playback_done 兜底/竞态提前关了打断窗口）
        //    但球球还有音频在播，后端据此立即掐断，而不是只启动 ASR 让旧音频继续播。
        this._control({
          type: 'speech_start',
          preRollBase64,
          isPlaying: this._hasActivePlayback(),
        })
      },
      // ── 用户说完（连续 20 帧静音 = 640ms 判定人声结束；frameSamples=512=32ms/帧）──
      onSpeechEnd: () => {
        console.log('[VAD] onSpeechEnd（上报 speech_end）')
        // 记录用户输入完成时刻（用于真实端到端延迟：说完 → 第一帧出声）。
        // 口径（用户确认）：每次 VAD 判定结束都刷新——补充说话/补充窗口场景下，
        // "最后一次说完"自然覆盖前面的碎片；首帧播放时才一次性换算（751 行）。
        this.userSpeechFinishTs = performance.now()
        // 语义 A 下：确认打断（barge_confirm）时已直接 stopStreamPlayback 销毁播放器，
        // 无需在这里做 confirmedDucking 兜底。
        // 重要：非打断场景（球球刚播完用户开口，后端走 listening 分支不发 barge_reject）
        // 也必须恢复音量，否则 ducking 会一直卡着球球声音变小
        this.stopDucking()
        // 上报人声结束（改造清单#7：携带"该说话段"音频，供后端 smart-turn 端点判定
        // "是否说完"——若判可能未完，后端开补充窗口等续说）
        // 窗口口径：整段话（onSpeechStart 起含预卷与尾静音，≤8s 保尾），与上游一致；
        // 实测（scripts/smart_turn_probe.py）整段话判别显著优于"最近 1.6s"切片
        this.speechActive = false
        const seg = this._concatUtterance()
        if (seg && seg.byteLength > 0) {
          this._control({ type: 'speech_end', audioB64: this._arrayBufferToBase64(seg.buffer as ArrayBuffer) })
        } else {
          // 兜底：缓冲异常为空（如种子/累计失败）→ 退回最近 8s 切片
          const fallback = this._sliceRecentMs(this.SMART_TURN_MAX_MS)
          if (fallback && fallback.byteLength > 0) {
            this._control({ type: 'speech_end', audioB64: this._arrayBufferToBase64(fallback.buffer as ArrayBuffer) })
          } else {
            this._control({ type: 'speech_end' })
          }
        }
      },
      // ── 误报（VAD 判定后又反悔，可能是噪声短暂过线）──
      onVADMisfire: () => {
        console.log('[VAD] onVADMisfire（误报，延迟恢复音量 + 撤销后端会话）')
        // 前后端判定一致性：若后端已确认打断（barge_confirm，球球已停），
        // 前端的 misfire 是"事后反悔"，无需再恢复音量（播放器已销毁）
        if (this.bargeConfirmed) {
          console.log('[VAD] 后端已确认打断，忽略 misfire 恢复')
          return
        }
        // 关键：通知后端撤销本次 speech_start 启动的 ASR 会话
        // 否则后端 is_user_speaking 一直卡 True，用户后续真实说话被防重入忽略
        this._control({ type: 'vad_cancel' })
        // 不立即恢复音量（避免 ducking 只持续 ~100ms 无感，且误伤快速真插话）
        // 延迟 400ms 恢复：给后端确认窗口——
        //   400ms 内 barge_confirm → 打断（ducking 持续到停播 ✓）
        //   400ms 内 barge_reject → 立即恢复（stopDucking 会清掉本定时器）
        //   都没有 → 400ms 后兜底恢复（音量不卡住）
        if (this.duckingTimer) clearTimeout(this.duckingTimer)
        this.duckingTimer = setTimeout(() => {
          if (this.bargeConfirmed) return
          console.log('[ducking] misfire 延迟恢复音量')
          this.stopDucking()
        }, 400)
      },
      positiveSpeechThreshold: this.posTh,
      negativeSpeechThreshold: this.negTh,
      minSpeechFrames: 6,
      redemptionFrames: 20,         /* 静音尾长一帧32ms */
      preSpeechPadFrames: 1,
    })
    this.vad.start()
  }

  private _loadScript(src: string): Promise<void> {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) {
        resolve()
        return
      }
      const s = document.createElement('script')
      s.src = src
      s.onload = () => resolve()
      s.onerror = () => reject(new Error(`加载脚本失败: ${src}`))
      document.head.appendChild(s)
    })
  }

  private _control(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj))
  }

  private _connectWs(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.wsUrl)
      ws.binaryType = 'arraybuffer'
      ws.onopen = () => { this.ws = ws; resolve() }
      ws.onerror = () => reject(new Error(`无法连接语音后端: ${this.wsUrl}`))
      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const msg = JSON.parse(ev.data)
            this._onJson(msg)
          } catch { /* ignore */ }
        } else {
          this._onAudio(ev.data as ArrayBuffer) // 后端 TTS PCM
        }
      }
      ws.onclose = () => { if (this.running) this.ws = null }
    })
  }

  /** 后端 JSON 控制消息（真实后端 8001 协议） */
  private _onJson(msg: any): void {
    // 一旦后端开始下发新音频/确认/打断类消息，立即切断占位音频 + 清等待计时（testboard 同款契约）
    if (['reply_start', 'reply', 'reply_append', 'tts_start', 'barge_confirm',
         'barge_reject', 'stop_playback', 'resume_playback', 'asr_final', 'reply_end'].includes(msg.type)) {
      // VoicePipeline 不维护占位音频（renderer 有独立的 PlaceholderPlayer 模块），
      // 这里只对 barge 状态做迁移，stop_playback/tts_start 等见下文 case
    }
    switch (msg.type) {
      case 'asr_final': {
        const text = (msg.text ?? '').trim()
        if (text) {
          // 口语「新建会话」（创建新对话/重新开始…）：清空上下文重新对话，维持对话态
          if (this._isNewSessionText(text)) {
            console.log('[voice] 收到新建会话指令:', text)
            this.onNewSession?.()
            void this.newSession()
            break
          }
          // 口语退出对话（不是退出前端）：命中即回待机
          if (this._isExitText(text)) {
            console.log('[voice] 收到退出语，回到待机:', text)
            this.onExit?.()
            if (this.wakeWordOn) this._backToWake()
            else this.onState?.('listening')
            break
          }
          this.onUserText?.(text)
          this._armIdleTimer() // 用户说完一句话 → 刷新对话空闲窗口
        }
        break
      }
      case 'resume_playback': {
        // 后端判定打断无有效输入（语气词/噪声被过滤）→ 恢复音量到 1.0
        // 语义 A 下 barge_confirm 已销毁播放器，这里只负责把 ducking 状态清干净，
        // 避免下一轮开始时 gain 还是 0.2 / duckingActive 残留。
        console.log('[barge] resume_playback（无有效输入，恢复正常音量）')
        this.stopDucking()
        break
      }
      case 'reply':
        // 新一轮回复开始：解除打断后的「丢弃旧音频」标记，重置播放时间线
        // 防止打断后残留的旧 audio bytes 在 barge_confirm 之后重建播放器播旧内容
        if (this.pendingBargeResume) {
          console.log('[barge] 新一轮 reply：解除丢弃标记，开始新内容播放')
          this.pendingBargeResume = false
          if (this.bargeResumeTimer) { clearTimeout(this.bargeResumeTimer); this.bargeResumeTimer = null }
        }
        this.resetPlayback() // 新一段回复开始，重置播放时间线
        this.onReply?.(msg.text ?? '', false)
        this.onState?.('speaking')
        this._armIdleTimer()
        break
      case 'reply_append':
        this.onReply?.(msg.text ?? '', true)
        this._armIdleTimer()
        break
      case 'tts_start':
        // ⚠️ 修复「两句抢话」：多句回复的每一句都会触发 tts_start，若上一句还在播放
        // 就无条件 resetPlayback()，会把上一句剩余音频掐掉、第二句立刻响起（重叠/抢话）。
        // 正确行为：仅当时间线空闲（无排队/无播放）时才重置（兜底漏 reply 的情况）；
        // 上一句仍在播时让新句音频经 _onAudio 的 nextStartTime 自然接续，无缝连贯。
        if (!this._hasActivePlayback()) this.resetPlayback()
        this.onTtsEvent?.('start')
        this.onState?.('speaking')
        this._armIdleTimer()
        // 上报「喇叭真正开始发声」—— 后端据此打开打断窗口
        // 解决排队模式：TTS已下发但喇叭未响时，后端以为是listening
        this._control({ type: 'client_play_start' })
        // 兜底：playback_done 丢包防护
        // 如果 15 秒内没正常发 client_playback_done（onended 丢失），强制补发
        // 否则后端永远收不到播放完成，打断窗口不闭合
        if (this.playbackDoneTimer) clearTimeout(this.playbackDoneTimer)
        this.playbackDoneTimer = setTimeout(() => {
          if (this.pendingPlaybackDone) {
            console.log('[播放兜底] 超时未收到播放完成，强制补发 client_playback_done')
            this.pendingPlaybackDone = false
            this._control({ type: 'client_playback_done' })
          }
        }, this.PLAYBACK_DONE_TIMEOUT_MS)
        break
      case 'tts_end':
        this.onTtsEvent?.('end')
        break
      case 'reply_end':
        // 后端整段回复发完，等所有音频块真正播完再发 playback_done
        this.pendingPlaybackDone = true
        this.onTtsEvent?.('end')
        this._armIdleTimer()
        break
      case 'stop_playback':
        // 契约 §4.13：停止当前（进度）音频播放，直接转最终回复
        this.stopStreamPlayback()
        break
      case 'barge_confirm': {
        // 后端确认「真打断」：**直接彻底静音 + 丢弃旧音频 + 销毁播放器**（语义 A）
        // 不再"保持 ducking 等 asr_final"——二级确认（能量跃升+RMS+人声占比）已足够准，
        // 确认打断即丢弃旧内容，避免"打断后球球恢复播放/先读旧音频"。
        // 误打断靠后端二次确认拦截（barge_reject 走恢复分支），这里是确定性打断。
        // ducking（20% 音量）只是打断判定前的**临时体感**，判定确认后彻底静音。
        console.log('[barge] barge_confirm：确认打断，直接静音+丢弃旧音频+销毁播放器')
        this.stopStreamPlayback()  // gain→0 + 停所有 source + 关 ctx + 清状态
        // 标记：丢弃「打断前已推送但还没消费」的旧 audio bytes，直到新一轮 reply
        // 否则这些旧 PCM 到达时会重建播放器把未读完的旧内容播出来（问题3）
        this.pendingBargeResume = true
        // 兜底：3s 内若没有新一轮 reply（打断后用户没说话/ASR 卡住），解除标记，
        // 避免永久丢弃后续正常音频
        if (this.bargeResumeTimer) clearTimeout(this.bargeResumeTimer)
        this.bargeResumeTimer = setTimeout(() => {
          if (this.pendingBargeResume) {
            console.log('[barge] 3s 无新一轮 reply，解除丢弃标记')
            this.pendingBargeResume = false
          }
        }, this.BARGE_RESUME_TIMEOUT_MS)
        this.bargeConfirmed = true  // 标记：后端已确认打断（misfire 不再反向恢复）
        // 改造清单#3：上报打断延迟必须先于清空 speechStartTime（fix 死代码）。
        // 口径注意：起点为 VAD 确认人声时机（onSpeechStart 触发时刻），
        // 不含前端 VAD 判定窗口（6 帧 ≈ 576ms）。
        if (this.speechStartTime !== null) {
          const totalMs = performance.now() - this.speechStartTime
          this._control({ type: 'barge_latency', latency: parseFloat((totalMs / 1000).toFixed(3)) })
        }
        this.speechStartTime = null
        // 打断后转回 listening（等下一轮 reply/tts_start 再切 speaking）
        this.onState?.('listening')
        break
      }
      case 'barge_reject':
        // 后端判定「误报」：恢复音量
        console.log('[barge] 误报（后端判噪声，恢复音量）')
        this.stopDucking()
        break
      case 'mode_changed': {
        // 契约 §4.3：语音切模式（如"打开工作模式"）→ 通知 UI 同步主进程，
        // 由主进程广播 mode:changed 回面板选项 + 宠物动画（三端同步）
        const m = msg.mode === 'work' ? 'work' : 'chat'
        console.log('[voice] mode_changed:', m, msg.notice ?? '')
        this.onModeChanged?.(m)
        break
      }
      case 'ready':
        // 连接就绪：向后端同步一次当前模式（get_mode → 后端回 mode_changed → onModeChanged → 三端一致）
        this._control({ type: 'get_mode' })
        break
      case 'asr_partial':
      default:
        break
    }
  }

  /** 手动（按钮）切换模式 → 同步到 8001 后端（契约 §3.1 set_mode），保后端/面板/动画一致 */
  setBackendMode(mode: 'chat' | 'work'): void {
    this._control({ type: 'set_mode', mode })
  }

  /** 后端 TTS PCM（24k mono 16bit）→ 时间线接续播放 */
  private _onAudio(arrayBuffer: ArrayBuffer): void {
    // 打断已确认（barge_confirm）→ 丢弃「打断前已推送但还没消费完」的旧 audio bytes，
    // 直到新一轮 reply（reply case 清标记）。否则这些旧 PCM 到达时会重建播放器，
    // 把"打断后本来该丢弃的旧内容"播出来（问题3：下一次回复先读之前音频）。
    if (this.pendingBargeResume) {
      console.log('[barge] 丢弃打断前的旧 audio bytes（等到下一轮 reply）')
      return
    }
    // 播放器已销毁（如已 stopStreamPlayback）：新 audio 来时重建。
    try {
      const p = this._ensurePlayback()
      const ctx = p.ctx
      const samples = new Int16Array(arrayBuffer)
      if (!samples.length) return
      const float32 = new Float32Array(samples.length)
      for (let i = 0; i < samples.length; i++) float32[i] = samples[i] / 32768
      // 后端 24k：buffer 明确 24k，AudioContext 默认采样率自动重采样（音调/时长正确）
      const buf = ctx.createBuffer(1, float32.length, 24000)
      buf.getChannelData(0).set(float32)
      const src = ctx.createBufferSource()
      src.buffer = buf
      src.connect(p.gain)
      // 记录第一帧开始播放的时刻，计算真实端到端延迟（用户说完 → 喇叭第一帧出声）
      // 这是用户感知的「我说话结束 → 球球开口」延迟，与服务端首响（e2e）语义不同。
      // 只在 userSpeechFinishTs 有效且还没测过时测一次（同一轮只测一次；下一轮 onSpeechEnd 会更新）。
      if (this.userSpeechFinishTs !== null && this.playFirstFrameTs === null) {
        this.playFirstFrameTs = performance.now()
        const realE2EMs = this.playFirstFrameTs - this.userSpeechFinishTs
        console.log(`[真实E2E] 用户说完→第一帧出声 = ${realE2EMs.toFixed(0)}ms`)
        // 上报后端供 [指标] 打印（语义：说话结束 → 首帧播报）
        this._control({ type: 'client_real_e2e', ms: Math.round(realE2EMs) })
        // 清空，避免下一轮误用
        this.userSpeechFinishTs = null
        this.playFirstFrameTs = null
      }
      // 时间线接续：上一块播完的位置作为下一块开始（p.state 为同一引用，确保累加保留）
      const startAt = Math.max(p.state.nextStartTime, ctx.currentTime + 0.02)
      const duration = float32.length / 24000
      p.state.nextStartTime = startAt + duration
      // ── 诊断（定位加速/重叠）：打印实际采样率 + 每块时长/起始 ──
      console.log(`[play]ctx.sampleRate=${ctx.sampleRate} len=${float32.length} dur=${duration.toFixed(3)}s start=${startAt.toFixed(3)} next=${p.state.nextStartTime.toFixed(3)} buf_sr=${buf.sampleRate}`)
      src.start(startAt)
      p.state.active.push(src)
      src.onended = () => {
        const i = p.state.active.indexOf(src)
        if (i >= 0) p.state.active.splice(i, 1)
        // 所有音频块播完 + 已收到 reply_end → 前端真正播放结束
        // 此时发 client_playback_done 给后端，后端才进入 listening + 保护期
        if (p.state.active.length === 0 && this.pendingPlaybackDone) {
          this.pendingPlaybackDone = false
          if (this.playbackDoneTimer) { clearTimeout(this.playbackDoneTimer); this.playbackDoneTimer = null }
          this._control({ type: 'client_playback_done' })
        }
      }
    } catch (e) {
      console.error('[VoicePipeline] TTS 播放失败', e)
    }
  }

  private _ensurePlayback(): { ctx: AudioContext; gain: GainNode; state: { nextStartTime: number; active: AudioBufferSourceNode[] } } {
    if (!this.playbackCtx) {
      // 不要显式指定 sampleRate（Electron 常忽略它，会造成 24k/44.1k 错乱加速）；
      // 让 AudioContext 用系统默认采样率，内部正确对 24k buffer 重采样。
      this.playbackCtx = new AudioContext()
      this.gainNode = this.playbackCtx.createGain()
      this.gainNode.gain.value = 1
      this.gainNode.connect(this.playbackCtx.destination)
      this._playbackState = { nextStartTime: 0, active: [] }
    }
    return {
      ctx: this.playbackCtx!,
      gain: this.gainNode!,
      state: this._playbackState!,
    }
  }

  /** 是否有尚未播完/已排队待播的音频（决定 tts_start 是否该重置时间线） */
  private _hasActivePlayback(): boolean {
    const p = this._playbackState
    if (!this.playbackCtx || !p) return false
    if (p.active.length > 0) return true
    // 音频可能已全部播完但下一句音频块还在路上：以时间线是否仍在未来判断
    return p.nextStartTime > this.playbackCtx.currentTime + 0.1
  }

  /** 新一段回复/打断时：停止当前播放、重置时间线（不留上一段残留音频） */
  private resetPlayback(): void {
    if (!this.playbackCtx || !this._playbackState) return
    const p = this._playbackState
    for (const src of p.active) {
      try {
        src.stop()
      } catch { /* 已播完 */ }
    }
    p.active = []
    p.nextStartTime = 0
  }

  // ── Ducking：体感层快速压低音量（不立即静音）──────────
  // 对照 testboard/index.html:startDucking — 完全移植
  private startDucking(): boolean {
    // 只有球球正在播放时才需要 ducking
    if (!this.playbackCtx || !this.gainNode) {
      console.log('[ducking] 球球没在播放，跳过 ducking')
      return false
    }
    console.log('[ducking] 执行 ducking, 当前gain=', this.gainNode.gain.value)
    try {
      const ctx = this.playbackCtx
      const gain = this.gainNode.gain
      gain.cancelScheduledValues(ctx.currentTime)
      gain.setTargetAtTime(this.DUCKING_GAIN, ctx.currentTime, 0.03)
      console.log('[ducking] 已设置目标 gain=', this.DUCKING_GAIN)
    } catch (e) {
      console.log('[ducking] 异常:', e)
    }
    // 超时兜底：若 barge_confirm/barge_reject/onSpeechEnd/onVADMisfire 都没触发
    // （消息丢失/竞态），超时后强制恢复音量——修复「误报后音量未真正恢复」
    if (this.duckingTimer) clearTimeout(this.duckingTimer)
    this.duckingTimer = setTimeout(() => {
      console.log('[ducking] 超时无确认，强制恢复音量')
      this.stopDucking()
    }, this.DUCKING_TIMEOUT_MS)
    return true
  }

  private stopDucking(): void {
    // 无条件恢复音量（不依赖任何打断状态标记，误报恢复时直接恢复）
    if (this.duckingTimer) { clearTimeout(this.duckingTimer); this.duckingTimer = null }
    if (this.playbackCtx && this.gainNode) {
      try {
        const ctx = this.playbackCtx
        const gain = this.gainNode.gain
        gain.cancelScheduledValues(ctx.currentTime)
        gain.setTargetAtTime(1.0, ctx.currentTime, 0.05)
      } catch { /* ignore */ }
    }
  }

  /** 打断/退出时彻底销毁播放器：gain 瞬间归零 + 停所有 source + 关 ctx + 清状态
   *  对照 testboard/index.html:stopStreamPlayback — 完全移植 */
  private stopStreamPlayback(): void {
    // 清播放完成兜底定时器（播放已停止，不需要兜底）
    if (this.playbackDoneTimer) { clearTimeout(this.playbackDoneTimer); this.playbackDoneTimer = null }
    // 清「静音等待恢复」状态（播放器已销毁，无需恢复）
    if (this.bargeResumeTimer) { clearTimeout(this.bargeResumeTimer); this.bargeResumeTimer = null }
    // 清 ducking 兜底定时器（播放器已销毁，2s 保险丝不再需要；避免残留空转）
    if (this.duckingTimer) { clearTimeout(this.duckingTimer); this.duckingTimer = null }
    this.pendingBargeResume = false
    if (this.playbackCtx) {
      // 1. 立即静音（gain 瞬间归零，消除残音）
      try {
        if (this.gainNode) this.gainNode.gain.setValueAtTime(0, this.playbackCtx.currentTime)
      } catch { /* ignore */ }
      // 2. 停止所有已排队的 source
      if (this._playbackState) {
        for (const s of this._playbackState.active) {
          try { s.stop() } catch { /* ignore */ }
        }
        this._playbackState.active = []
      }
      // 3. 关闭音频上下文
      try { this.playbackCtx.close() } catch { /* ignore */ }
      this.playbackCtx = null
      this.gainNode = null
      this._playbackState = null
    }
  }

  /** 停止 */
  async stop(): Promise<void> {
    this.running = false
    this.wakeWordOn = false
    this.conversationStarted = false
    this._clearIdleTimer()
    // 清理打断相关定时器（防止 stop 后定时器还在跑触发状态错乱）
    if (this.duckingTimer) { clearTimeout(this.duckingTimer); this.duckingTimer = null }
    if (this.bargeResumeTimer) { clearTimeout(this.bargeResumeTimer); this.bargeResumeTimer = null }
    if (this.playbackDoneTimer) { clearTimeout(this.playbackDoneTimer); this.playbackDoneTimer = null }
    this.pendingBargeResume = false
    this.bargeConfirmed = false
    this.pendingPlaybackDone = false
    this.speechStartTime = null
    this.userSpeechFinishTs = null
    this.playFirstFrameTs = null
    this.preRollBuffer = []
    this.utteranceAudio = []
    this.speechActive = false
    this.kwsUnsub?.()
    this.kwsUnsub = null
    if (this.vad) { try { this.vad.destroy() } catch { /* ignore */ } this.vad = null }
    if (this.processor) { try { this.processor.disconnect() } catch { /* ignore */ } }
    if (this.source) { try { this.source.disconnect() } catch { /* ignore */ } }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null }
    if (this.audioCtx) { try { this.audioCtx.close() } catch { /* ignore */ } this.audioCtx = null }
    this.stopStreamPlayback()  // 用 stopStreamPlayback 而非 resetPlayback：彻底销毁（关 ctx + 清 gain）
    if (this.ws) { try { this.ws.close() } catch { /* ignore */ } this.ws = null }
    this.onState?.('idle')
  }
}

export default VoicePipeline
