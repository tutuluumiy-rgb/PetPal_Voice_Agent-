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

  /** 语音：on=true 进入说话；off 回落基底 */
  feedTts(on: boolean): void {
    this.speakingFlag = on
    if (on) {
      // 进入说话：工作模式下播 trans_work_speak 过渡，否则直接 speaking
      this.playTransition(this.base, 'speaking')
    } else {
      // 结束说话：回落基底（工作→播 trans_speak_work；待机直接 idle）
      this.playTransition('speaking', this.base)
    }
  }

  /** 工作模式：basis 在 idle/working 间切换（不打断进行中的语音则待其结束） */
  setMode(mode: 'chat' | 'work'): void {
    this.base = mode === 'work' ? 'working' : 'idle'
    if (!this.speakingFlag) {
      this.setState(this.base)
    }
  }

  /** 播放一次过渡：过渡帧素材就绪则先播过渡态（时长=帧数/fps），结束后落到目标态 */
  playTransition(from: PetAnimState, to: PetAnimState): void {
    const ts = transitionState(from, to)
    if (ts && this.sprite.isReady(ts) && this.manifest[ts]) {
      this.setState(ts)
      const cfg = this.manifest[ts]!
      const ms = cfg.frames.length > 0 ? (cfg.frames.length / cfg.fps) * 1000 : 0
      window.setTimeout(() => this.setState(to), ms)
      return
    }
    // 过渡素材缺失 → 直接切目标态
    this.setState(to)
  }

  // ---------------- 生命周期 ----------------
  start(): void {
    if (this.rafId != null) return
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
    // 预载常驻动画（speaking/working + 两个过渡），失败自然跳过
    void this.prefetch('speaking')
    void this.prefetch('working')
    void this.prefetch('trans_speak_work')
    void this.prefetch('trans_work_speak')
    this.rafId = requestAnimationFrame(loop)
  }

  stop(): void {
    if (this.rafId != null) {
      cancelAnimationFrame(this.rafId)
      this.rafId = null
    }
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
  }

  private renderFrame(ts: number): void {
    if (!this.ctx) return
    const w = this.canvas.width
    const h = this.canvas.height
    // 待机=语音：把 idle 解析为 speaking（帧共用，避免重复加载；程序化也同参）
    const eff = this.resolveState(this.state)

    // 优先 PNG 素材；就绪则画序列帧
    if (!this.reducedMotion && this.sprite.isReady(eff)) {
      const ok = this.sprite.draw(this.ctx, w, h, eff, this.stateElapsed)
      if (ok) return
    }
    // 兜底：程序化形变（基于照片）
    this.procedural.setState(eff)
    this.procedural.draw(this.ctx, w, h, ts)
  }

  /** 状态解析：idle 复用 speaking（待机=语音） */
  private resolveState(s: PetAnimState): PetAnimState {
    return s === 'idle' ? 'speaking' : s
  }
}

/** 根据起止状态，返回对应过渡态名（无则 null） */
function transitionState(from: PetAnimState, to: PetAnimState): PetAnimState | null {
  const map: Record<string, PetAnimState> = {
    'speaking->working': 'trans_speak_work',
    'working->speaking': 'trans_work_speak',
    'idle->speaking': 'trans_idle_speak',
    'speaking->idle': 'trans_speak_idle',
  }
  return map[`${from}->${to}`] ?? null
}
