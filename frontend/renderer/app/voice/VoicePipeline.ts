/**
 * VoicePipeline — Electron renderer 语音管线（后端职责提供）
 * --------------------------------------------------------------------------
 * 对接【真实后端 8001】（/ws/audio，测试看板那套协议）：
 *   前端→后端：二进制 PCM（Int16 16k）+ JSON 控制消息
 *              { type:'speech_start', preRollBase64 } / { type:'speech_end' } / { type:'vad_cancel' }
 *   后端→前端：JSON（ready/asr_partial/asr_final/reply_start/reply/reply_append/reply_end/
 *              tts_start/tts_end/barge_confirm/barge_reject/stop_playback）+ 二进制 TTS PCM(24k)
 *
 * 基于测试看板（testboard）验证过的采集 + Silero VAD 逻辑迁移；renderer 直连音频 WS。
 * 依赖 renderer/assets/vad/ 的 UMD 库（onnxruntime-web + @ricky0123/vad-web）。
 */
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
    this.posTh = opts.positiveThreshold ?? 0.4
    this.negTh = opts.negativeThreshold ?? 0.35
  }

  /**
   * 退出对话的口语规则（命中即结束本轮对话回待机，不是退出前端）。
   * 全角/半角、首尾空格可容错；与用户说法的子串匹配。
   */
  private readonly EXIT_WORDS: readonly string[] = [
    '拜拜', '再见', '退出聊天', '退出对话', '不聊了', '先这样吧', '聊到这',
    '下次再聊', '回头聊', '晚安', '去忙了', '我要忙了', '挂了吧', '回见',
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

  /** 统一音频帧分配：待机喂主进程 KWS（IPC），对话中喂后端 */
  private _onAudioFrame(input: Float32Array): void {
    if (this.wakeWordOn && !this.conversationStarted) {
      window.api.kwsFeed(input)
      return
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const pcm = new Int16Array(input.length)
    for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-1, Math.min(1, input[i])) * 32767
    this.ws.send(pcm.buffer) // 音频持续发送，不丢字
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
      onSpeechStart: () => this._control({ type: 'speech_start', preRollBase64: null }),
      onSpeechEnd: () => this._control({ type: 'speech_end' }),
      onVADMisfire: () => this._control({ type: 'vad_cancel' }),
      positiveSpeechThreshold: this.posTh,
      negativeSpeechThreshold: this.negTh,
      minSpeechFrames: 6,
      redemptionFrames: 10,
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
    switch (msg.type) {
      case 'asr_final': {
        const text = (msg.text ?? '').trim()
        if (text) {
          // 口语退出对话（不是退出前端）：命中即回待机
          if (this._isExitText(text)) {
            console.log('[voice] 收到退出语，回到待机:', text)
            if (this.wakeWordOn) this._backToWake()
            else this.onState?.('listening')
            break
          }
          this.onUserText?.(text)
          this._armIdleTimer() // 用户说完一句话 → 刷新对话空闲窗口
        }
        break
      }
      case 'reply':
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
        this.resetPlayback()
        this.onTtsEvent?.('start')
        this.onState?.('speaking')
        this._armIdleTimer()
        break
      case 'tts_end':
        this.onTtsEvent?.('end')
        break
      case 'reply_end':
        this.onTtsEvent?.('end')
        // 方案 A：一轮说完仍保持对话（可连续聊），由空闲超时/退出语回待机
        this.onState?.('listening')
        this._armIdleTimer()
        break
      case 'ready':
      case 'asr_partial':
      case 'barge_confirm':
      case 'barge_reject':
      default:
        break
    }
  }

  /** 后端 TTS PCM（24k mono 16bit）→ 时间线接续播放 */
  private _onAudio(arrayBuffer: ArrayBuffer): void {
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

  /** 停止 */
  async stop(): Promise<void> {
    this.running = false
    this.wakeWordOn = false
    this.conversationStarted = false
    this._clearIdleTimer()
    this.kwsUnsub?.()
    this.kwsUnsub = null
    if (this.vad) { try { this.vad.destroy() } catch { /* ignore */ } this.vad = null }
    if (this.processor) { try { this.processor.disconnect() } catch { /* ignore */ } }
    if (this.source) { try { this.source.disconnect() } catch { /* ignore */ } }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null }
    if (this.audioCtx) { try { this.audioCtx.close() } catch { /* ignore */ } this.audioCtx = null }
    this.resetPlayback()
    if (this.playbackCtx) { try { this.playbackCtx.close() } catch { /* ignore */ } this.playbackCtx = null; this.gainNode = null }
    if (this.ws) { try { this.ws.close() } catch { /* ignore */ } this.ws = null }
    this.onState?.('idle')
  }
}

export default VoicePipeline
