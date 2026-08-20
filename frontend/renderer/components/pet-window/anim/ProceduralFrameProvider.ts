/**
 * ProceduralFrameProvider — 程序化兜底动画（基于真实照片）
 * --------------------------------------------------------------------------
 * 当某状态没有就绪的 PNG 帧素材时使用：以真实猫咪照片为底，用 canvas 变换
 * （平移/缩放）逐帧产生「呼吸 / 摆动 / 说话 / 快乐」等动态感，纯前端、无需素材。
 * 呼吸：以宠物身体中心为锚点做轻微纵向缩放（scaleY 0.98↔1.02）→ 像在呼吸而非拉伸。
 */
import type { FrameStateConfig, PetAnimState } from './types'
import { BALL_BOTTOM_PADDING } from '../pet-canvas'

export class ProceduralFrameProvider {
  private image: HTMLImageElement | null = null
  // 各状态用到的形变参数（幅度越大越明显）
  private params: Partial<Record<PetAnimState, { breathe: number; bob: number; sway: number; speed: number }>> = {
    idle: { breathe: 0.02, bob: 3, sway: 2, speed: 1 },
    speaking: { breathe: 0.025, bob: 7, sway: 4, speed: 2.2 },
    listening: { breathe: 0.018, bob: 4, sway: 2, speed: 1.3 },
    working: { breathe: 0.012, bob: 3, sway: 2.5, speed: 1.1 },
    thinking: { breathe: 0.012, bob: 2, sway: 2, speed: 0.8 },
    happy: { breathe: 0.03, bob: 9, sway: 6, speed: 2.6 },
    sad: { breathe: 0.01, bob: 1, sway: 1, speed: 0.6 },
    sleeping: { breathe: 0.02, bob: 1, sway: 0.5, speed: 0.35 },
    surprised: { breathe: 0.015, bob: 6, sway: 3, speed: 1.8 },
  }

  setImage(img: HTMLImageElement): void {
    this.image = img
  }

  hasImage(): boolean {
    return Boolean(this.image && this.image.complete && this.image.naturalWidth > 0)
  }

  /** 当前状态（由 PetAnimator 在渲染帧前调用 setState 记录） */
  private currentState: PetAnimState = 'idle'
  setState(state: PetAnimState): void {
    this.currentState = state
  }

  /** 便捷绘制入口（PetAnimator 调用）：透明清除 + 据当前状态程序化形变 */
  draw(ctx: CanvasRenderingContext2D, w: number, h: number, timeMs: number): void {
    this.drawCurrent(ctx, w, h, timeMs)
  }

  private drawCurrent(ctx: CanvasRenderingContext2D, w: number, h: number, timeMs: number): void {
    const img = this.image
    if (!img || !this.hasImage()) return
    ctx.clearRect(0, 0, w, h)
    const p = this.params[this.currentState] ?? this.params.idle!
    // 呼吸：以宠物中心为锚点的纵向缩放（正弦，慢速）
    const breathe = 1 + Math.sin((timeMs / 1000) * (Math.PI * 2 * 0.5 * p.speed)) * p.breathe
    const bob = Math.sin((timeMs / 1000) * (Math.PI * 2 * p.speed)) * p.bob
    const sway = Math.sin((timeMs / 1000) * (Math.PI * 2 * p.speed * 0.7)) * p.sway

    // 目标绘制尺寸（保留比例，底部居中；底部留出 BALL_BOTTOM_PADDING 给消息条）
    const availH = h - BALL_BOTTOM_PADDING - 4
    const scale = Math.min((w - 4) / img.naturalWidth, availH / img.naturalHeight, 1)
    const dw = img.naturalWidth * scale
    const dh = img.naturalHeight * scale
    const baseX = (w - dw) / 2
    const baseY = h - BALL_BOTTOM_PADDING - dh

    // 宠物中心锚点（用于呼吸缩放支点 ≈ 画布中心略微偏下）
    const anchorX = w / 2 + sway
    const anchorY = baseY + dh / 2 + bob

    ctx.save()
    ctx.translate(anchorX, anchorY)
    ctx.scale(1, breathe) // 只纵向缩放=呼吸
    ctx.translate(-anchorX, -anchorY)
    ctx.drawImage(img, baseX + sway, baseY + bob, dw, dh)
    ctx.restore()
  }

  // 构造兼容（manifest 未使用者直接 new，无需参数）
  constructor(_manifest?: Partial<Record<PetAnimState, FrameStateConfig>>) {
    // no-op
  }
}
