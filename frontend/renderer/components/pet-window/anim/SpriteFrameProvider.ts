/**
 * SpriteFrameProvider — PNG 多帧序列播放（懒加载）
 * --------------------------------------------------------------------------
 * 按状态播放一组 PNG 帧（透明），帧率/循环由 manifest 配置。
 * 帧号换算：自该状态开始累积时间 × fps → 取整；loop 取模，非 loop 停在末帧。
 */
import type { FrameStateConfig, PetAnimState } from './types'
import { BALL_BOTTOM_PADDING } from '../pet-canvas'

export class SpriteFrameProvider {
  private images = new Map<string, HTMLImageElement>()
  private manifest: Partial<Record<PetAnimState, FrameStateConfig>>

  constructor(manifest: Partial<Record<PetAnimState, FrameStateConfig>>) {
    this.manifest = manifest
  }

  /** 该状态是否有已就绪的帧素材 */
  isReady(state: PetAnimState): boolean {
    const cfg = this.manifest[state]
    return Boolean(cfg && cfg.ready && cfg.frames.length > 0)
  }

  /** 懒加载某状态的全部帧；就绪后置 ready=true。失败则保持未就绪（走程序化兜底）。 */
  async load(state: PetAnimState, baseUrl = ''): Promise<void> {
    const cfg = this.manifest[state]
    if (!cfg || cfg.ready) return
    const url = (f: string): string => (baseUrl ? `${baseUrl}/${f}` : f)
    try {
      const imgs = await Promise.all(cfg.frames.map((f) => loadImage(url(f))))
      imgs.forEach((img, i) => this.images.set(key(state, i), img))
      cfg.ready = true
    } catch {
      cfg.ready = false
    }
  }

  /** 取当前帧 Image；未就绪返回 null */
  getFrame(state: PetAnimState, elapsedMs: number): HTMLImageElement | null {
    const cfg = this.manifest[state]
    if (!cfg || !cfg.ready || cfg.frames.length === 0) return null
    const idx = Math.floor((elapsedMs / 1000) * cfg.fps)
    const i = cfg.loop ? idx % cfg.frames.length : Math.min(idx, cfg.frames.length - 1)
    return this.images.get(key(state, i)) ?? null
  }

  /** 画当前帧到画布（透明 clearRect + 底部居中适配，底部预留消息条空间）；无帧返回 false */
  draw(ctx: CanvasRenderingContext2D, w: number, h: number, state: PetAnimState, elapsedMs: number): boolean {
    const img = this.getFrame(state, elapsedMs)
    if (!img || !img.complete || img.naturalWidth === 0) return false
    ctx.clearRect(0, 0, w, h)
    // 与 photoFrameSource / ProceduralFrameProvider 对齐：底部留出 BALL_BOTTOM_PADDING
    // 给语音播报消息条，避免动画帧与消息条重叠（此前画到 h-4 会压住消息条）。
    const availH = h - BALL_BOTTOM_PADDING - 4
    const scale = Math.min((w - 4) / img.naturalWidth, availH / img.naturalHeight, 1)
    const dw = img.naturalWidth * scale
    const dh = img.naturalHeight * scale
    const dx = (w - dw) / 2
    const dy = h - BALL_BOTTOM_PADDING - dh
    ctx.drawImage(img, dx, dy, dw, dh)
    return true
  }
}

function key(state: PetAnimState, i: number): string {
  return `${state}|${i}`
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error(`sprite load fail: ${src}`))
    img.src = src
  })
}
