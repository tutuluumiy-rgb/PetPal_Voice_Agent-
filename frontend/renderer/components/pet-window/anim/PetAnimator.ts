/**
 * PetAnimator — rAF 动画播放器
 * --------------------------------------------------------------------------
 * 驱动宠物画布：优先播放「PNG 多帧素材」（懒加载），未就绪则回落「程序化
 * 形变动画」（基于真实照片的呼吸/摆动/说话）。对外提供状态切换与 TTS/模式联动。
 */
import { SpriteFrameProvider } from './SpriteFrameProvider'
import { ProceduralFrameProvider } from './ProceduralFrameProvider'
import { buildManifest } from './loadManifest'
import type { FrameManifest, PetAnimState } from './types'

export class PetAnimator {
  private canvas: HTMLCanvasElement
  private ctx: CanvasRenderingContext2D | null
  private sprite: SpriteFrameProvider
  private procedural: ProceduralFrameProvider
  private manifest: FrameManifest
  private rafId: number | null = null
  private lastTs = 0
  private paused = false
  private reducedMotion = false

  // 当前展示状态 与 该状态累计播放时长
  private state: PetAnimState = 'idle'
  private stateElapsed = 0
  // 基底（回落到 idle / working）
  private base: PetAnimState = 'idle'
  // 语音开关（说话时优先 speaking）
  private speakingFlag = false
  // 过渡定时器（播放完过渡后落到目标态）；新过渡开始时需清掉旧的，防竞态串台
  private transitionTimer: number | null = null
  // 过渡动画的「最新意向」：素材未就绪时记录，后台加载完成后若仍是最新意向则补播
  private pendingTransition: { ts: PetAnimState; to: PetAnimState } | null = null
  /** 诊断回调（PetWindow 接到后经 IPC 打到主进程终端） */
  onDebug?: (message: string) => void

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas
    this.ctx = canvas.getContext('2d')
    this.manifest = buildManifest()
    this.sprite = new SpriteFrameProvider(this.manifest)
    this.procedural = new ProceduralFrameProvider()
    try {
      this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    } catch {
      this.reducedMotion = false
    }
    this.debug(`init reducedMotion=${this.reducedMotion} states=${Object.keys(this.manifest).join(',')}`)
  }

  private debug(msg: string): void {
    try {
      this.onDebug?.(msg)
    } catch {
      /* ignore */
    }
  }

  /** 设置基底照片（程序化兜底用） */
  setBaseImage(img: HTMLImageElement | null): void {
    if (img) this.procedural.setImage(img)
  }

  /** 待机等需要的基底帧素材是否就绪 */
  isReady(s: PetAnimState): boolean {
    return this.sprite.isReady(s)
  }

  /** 切换展示状态（立即生效；过渡态由调用方显式传入） */
  setState(s: PetAnimState): void {
    if (s !== this.state) {
      this.state = s
      this.stateElapsed = 0
    }
    // 懒加载：进入该状态时后台加载其帧；同时预取常用套（idle/speaking/working）
    this.prefetch(s)
  }

  /**
   * 语音事件（说/停）：**四态模型下不再触发任何切换动画**。
   * 动画只由「模式状态」决定（闲聊=speaking 循环 / 工作=working 循环 / 切模式播过渡），
   * 说话不改变视觉状态——闲聊说话与待机同为 speaking 循环；工作模式说话保持 working 循环。
   * 此处仅记录标志位（供将来可选的语气联动用），不调用 playTransition。
   */
  feedTts(on: boolean): void {
    this.speakingFlag = on
  }

  /**
   * 模式切换：动画的唯一驱动。
   * 闲聊→工作 播 trans_speak_work；工作→闲聊 播 trans_work_speak；播完落到目标基底循环。
   * （不再受说话状态影响：说话中切模式也立即播过渡。）
   */
  setMode(mode: 'chat' | 'work'): void {
    const newBase: PetAnimState = mode === 'work' ? 'working' : 'idle'
    if (this.base === newBase) return
    const from: PetAnimState = mode === 'work' ? 'speaking' : 'working'
    this.base = newBase
    this.debug(`setMode -> ${newBase} (speakingFlag=${this.speakingFlag}, state=${this.state})`)
    this.playTransition(from, newBase)
  }

  /**
   * 播放一次过渡：过渡帧就绪则播过渡态（时长=帧数/fps），结束后落到目标态；
   * 素材尚未就绪时先异步加载，完成后若仍是最新意向则补播（避免直接硬切）。
   */
  playTransition(from: PetAnimState, to: PetAnimState): void {
    const ts = transitionState(from, to)
    if (!ts || !this.manifest[ts] || this.reducedMotion) {
      // 无过渡素材（如 trans_idle_speak/trans_speak_idle 未制作）→ 直接切目标态
      this.debug(`playTransition ${from}->${to}: direct (ts=${ts ?? 'none'}, reducedMotion=${this.reducedMotion})`)
      this.setState(to)
      return
    }
    if (this.sprite.isReady(ts)) {
      this.debug(`playTransition ${from}->${to}: transition ${ts}`)
      this._runTransition(ts, to)
      return
    }
    // 素材未就绪：记录最新意向并后台加载；完成后若仍是最新意向则补播过渡
    this.debug(`playTransition ${from}->${to}: async load ${ts} (pending)`)
    this.pendingTransition = { ts, to }
    void this.sprite
      .load(ts)
      .then(() => {
        if (this.pendingTransition && this.pendingTransition.to === to && this.sprite.isReady(ts)) {
          // 仍是同一意向 → 补播过渡
          this.pendingTransition = null
          this.debug(`playTransition ${from}->${to}: loaded, replay ${ts}`)
          this._runTransition(ts, to)
        }
        // 已被更新的意向取代（通常已有更新的过渡在播），无需再动状态
      })
      .catch(() => {
        if (this.pendingTransition?.to === to) this.pendingTransition = null
        this.debug(`playTransition ${from}->${to}: load failed, direct to ${to}`)
        this.setState(to)
      })
  }

  /** 实际播放过渡：清旧定时器 → 播过渡态 → 到期落到目标态 */
  private _runTransition(ts: PetAnimState, to: PetAnimState): void {
    // 清掉旧的过渡定时器：连续切换（如说话中改模式）时防止旧的回调把状态"串台"
    if (this.transitionTimer !== null) {
      clearTimeout(this.transitionTimer)
      this.transitionTimer = null
    }
    this.setState(ts)
    const cfg = this.manifest[ts]
    if (!cfg) {
      this.setState(to)
      return
    }
    const ms = cfg.frames.length > 0 ? (cfg.frames.length / cfg.fps) * 1000 : 0
    this.transitionTimer = window.setTimeout(() => {
      this.transitionTimer = null
      this.setState(to)
    }, ms)
  }

  // ---------------- 生命周期 ----------------
  start(): void {
    if (this.rafId != null) return
    this.debug(`init reducedMotion=${this.reducedMotion} states=${Object.keys(this.manifest).join(',')}`)
    this.lastTs = performance.now()
    const loop = (ts: number): void => {
      const dt = ts - this.lastTs
      this.lastTs = ts
      if (!this.paused) {
        this.stateElapsed += dt
        this.renderFrame(ts)
      }
      this.rafId = requestAnimationFrame(loop)
    }
    // 预载常驻动画（speaking/working + 两个过渡），失败自然跳过；每套加载完即上报
    const states: PetAnimState[] = ['speaking', 'working', 'trans_speak_work', 'trans_work_speak']
    void Promise.allSettled(states.map(async (s) => this.prefetch(s))).then(() => {
      const ready = states
        .map((s) => `${s}=${this.sprite.isReady(s) ? 1 : 0}`)
        .join(' ')
      this.debug(`prefetch done: ${ready}`)
    })
    this.rafId = requestAnimationFrame(loop)
  }

  stop(): void {
    if (this.rafId != null) {
      cancelAnimationFrame(this.rafId)
      this.rafId = null
    }
    if (this.transitionTimer !== null) {
      clearTimeout(this.transitionTimer)
      this.transitionTimer = null
    }
    this.pendingTransition = null
  }

  pause(): void {
    this.paused = true
  }

  resume(): void {
    this.paused = false
    this.lastTs = performance.now()
  }

  // ---------------- 内部 ----------------
  private async prefetch(s: PetAnimState): Promise<void> {
    if (this.reducedMotion) return
    await this.sprite.load(s)
    this.debug(`load ${s} ready=${this.sprite.isReady(s) ? 1 : 0}`)
  }

  /**
   * 渲染一帧。
   * 展示态由「过渡是否在播 → 是否说话 → 基底」推导（displayState），
   * 即使内部 state 与基底不同步（切模式/打断各种时序残留），
   * 空闲时也必然渲染基底对应动画（工作模式=working 循环，不再出现"切了工作还播闲聊"）。
   */
  private renderFrame(ts: number): void {
    if (!this.ctx) return
    const w = this.canvas.width
    const h = this.canvas.height
    const eff = this.resolveState(this.displayState())

    let drew = false
    // 优先 PNG 帧素材（idle 也复用 speaking 帧，保持「待机=语音」的原有视觉）
    if (!this.reducedMotion && this.sprite.isReady(eff)) {
      drew = this.sprite.draw(this.ctx, w, h, eff, this.stateElapsed)
    }
    // 兜底：程序化形变（基于照片的呼吸/摆动）
    if (!drew) {
      this.procedural.setState(eff)
      this.procedural.draw(this.ctx, w, h, ts)
    }
  }

  /**
   * 展示态推导（四态模型，纯模式驱动）：
   * 过渡正在播 → 过渡态；否则 → 基底（闲聊=idle→speaking 循环；工作=working 循环）。
   * 说话不参与选动画；内部 state/base 不同步时以基底自愈。
   */
  private displayState(): PetAnimState {
    const s = this.state
    if (this.transitionTimer !== null && this.isTransState(s)) return s
    return this.base
  }

  /** 是否为过渡态 */
  private isTransState(s: PetAnimState): boolean {
    return (
      s === 'trans_speak_work' || s === 'trans_work_speak' || s === 'trans_idle_speak' || s === 'trans_speak_idle'
    )
  }

  /** 状态→帧素材别名：idle 复用 speaking（待机=语音，帧共用不重复加载） */
  private resolveState(s: PetAnimState): PetAnimState {
    return s === 'idle' ? 'speaking' : s
  }
}

/**
 * 根据起止状态，返回对应过渡态名（无则 null）。
 * 说明：idle 与 speaking 在画面层等价（resolveState 统一解析为 speaking 帧），
 * 因此 idle↔working 的模式切换复用 说话↔工作 两套过渡素材。
 * trans_idle_speak / trans_speak_idle 素材未制作，保持缺省（回退直接切换）。
 */
function transitionState(from: PetAnimState, to: PetAnimState): PetAnimState | null {
  const map: Record<string, PetAnimState> = {
    'speaking->working': 'trans_speak_work',
    'working->speaking': 'trans_work_speak',
    'idle->working': 'trans_speak_work',
    'working->idle': 'trans_work_speak',
    'idle->speaking': 'trans_idle_speak',
    'speaking->idle': 'trans_speak_idle',
  }
  return map[`${from}->${to}`] ?? null
}
