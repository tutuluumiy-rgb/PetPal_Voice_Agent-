/**
 * 宠物画布渲染模块（透明画布，行为类似 PNG 图片）
 * --------------------------------------------------------------------------
 * - 画布固定初始化宽高（220×240），任何点击 / 面板开合逻辑**禁止修改**画布尺寸
 * - 不填充底色（无 fillRect 黑色背景），仅用 clearRect（透明）清除，
 *   非图形区域像素保持 alpha=0（全透明）
 * - 素材替换接口：PetFrameSource.draw —— 当前为真实宠物照片（PNG，透明背景），
 *   绘制时保持透明；后续可替换为 PNG 序列帧 / 视频帧绘制（接口不变，
 *   画布透明属性与 DOM 盒子保持不变）
 * - 宠物精灵尺寸由图片实际尺寸动态计算（缩放适配画布，底部居中），
 *   面板定位（getBallRect）读取同一事实源，保证锚点与绘制一致
 */

/** 画布固定尺寸（与宠物窗口初始尺寸一致） */
export const PET_CANVAS_SIZE = { width: 220, height: 240 } as const

/** 球体底部留白（与绘制和面板定位共用，保持单一事实源） */
export const BALL_BOTTOM_PADDING = 8

/** 宠物精灵显示尺寸（图片加载后按比例缩放计算，默认占位 128×128） */
let petSpriteSize = { width: 128, height: 128 }

/** 读取宠物精灵当前显示尺寸（绘制与面板定位共用） */
export function getPetSpriteSize(): { width: number; height: number } {
  return { ...petSpriteSize }
}

/** 已加载的宠物图片（透明背景 PNG） */
let petImage: HTMLImageElement | null = null

/**
 * 素材帧源接口
 * 当前实现：真实宠物照片；后续替换：PNG 序列帧（按 timeMs 选帧）或视频帧
 */
export interface PetFrameSource {
  /**
   * 绘制一帧到画布
   * @param ctx    画布 2D 上下文（透明背景，禁止 fillRect 刷底色）
   * @param width  画布宽（固定 PET_CANVAS_SIZE.width）
   * @param height 画布高（固定 PET_CANVAS_SIZE.height）
   * @param timeMs 当前时间戳（帧动画 / 视频驱动用）
   */
  draw(ctx: CanvasRenderingContext2D, width: number, height: number, timeMs: number): void
}

/** 程序化球体帧源（占位兜底：图片加载失败时使用） */
export const ballFrameSource: PetFrameSource = {
  draw(ctx, width, height, _timeMs) {
    const cx = width / 2
    const cy = height - BALL_BOTTOM_PADDING - 64
    const grad = ctx.createRadialGradient(cx - 22, cy - 22, 8, cx, cy, 64)
    grad.addColorStop(0, '#7b86ea')
    grad.addColorStop(0.6, '#5e6ad2')
    grad.addColorStop(1, '#2e346e')
    ctx.fillStyle = grad
    ctx.beginPath()
    ctx.arc(cx, cy, 64, 0, Math.PI * 2)
    ctx.fill()
  }
}

/**
 * 真实宠物照片帧源：缩放适配画布（保留比例，宽 ≤ 画布宽-20、高 ≤ 画布高-16），
 * 底部居中绘制；图片本身透明背景（alpha 通道），非图形像素保持透明
 */
export const photoFrameSource: PetFrameSource = {
  draw(ctx, width, height, _timeMs) {
    if (!petImage) return
    const scale = Math.min(
      (width - 20) / petImage.width,
      (height - 16) / petImage.height,
      1
    )
    const dw = petImage.width * scale
    const dh = petImage.height * scale
    petSpriteSize = { width: Math.round(dw), height: Math.round(dh) }
    const dx = (width - dw) / 2
    const dy = height - BALL_BOTTOM_PADDING - dh
    ctx.drawImage(petImage, dx, dy, dw, dh)
  }
}

/**
 * 初始化宠物画布：固定尺寸 + 加载素材绘制
 * 画布尺寸一经设置不再修改（面板开合仅调整窗口尺寸，canvas 盒子固定）
 * @param imageUrl 宠物照片 URL（透明 PNG）；缺省时使用程序化球体兜底
 */
export function initPetCanvas(canvas: HTMLCanvasElement, imageUrl?: string): void {
  canvas.width = PET_CANVAS_SIZE.width
  canvas.height = PET_CANVAS_SIZE.height
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const paint = (): void => {
    // 透明清除（不清底色，非图形区域 alpha=0）
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    photoFrameSource.draw(ctx, canvas.width, canvas.height, 0)
  }

  if (imageUrl) {
    const img = new Image()
    img.onload = () => {
      petImage = img
      paint()
    }
    img.onerror = () => {
      // 图片加载失败 → 程序化球体兜底
      petImage = null
      petSpriteSize = { width: 128, height: 128 }
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      ballFrameSource.draw(ctx, canvas.width, canvas.height, 0)
    }
    img.src = imageUrl
  } else {
    petImage = null
    petSpriteSize = { width: 128, height: 128 }
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ballFrameSource.draw(ctx, canvas.width, canvas.height, 0)
  }

  // TODO: 后续迭代实现 — 素材帧动画 / 视频接入：
  // 以 requestAnimationFrame 驱动 frameSource.draw(ctx, w, h, performance.now())
  // 替换 photoFrameSource 为 PNG 序列帧 / 视频帧源即可，画布透明属性不变
}

/**
 * 宠物精灵在画布内的中心坐标（与 photoFrameSource 绘制位置保持一致，
 * 供悬浮面板定位读取球体屏幕坐标）
 */
export function getBallCenterInCanvas(): { x: number; y: number } {
  return {
    x: PET_CANVAS_SIZE.width / 2,
    y: PET_CANVAS_SIZE.height - BALL_BOTTOM_PADDING - petSpriteSize.height / 2
  }
}

/** 宠物精灵在画布内的左上角坐标（供窗口定位 / canvas 位置补偿使用） */
export function getBallTopLeftInCanvas(): { x: number; y: number } {
  const c = getBallCenterInCanvas()
  return {
    x: c.x - petSpriteSize.width / 2,
    y: c.y - petSpriteSize.height / 2
  }
}
