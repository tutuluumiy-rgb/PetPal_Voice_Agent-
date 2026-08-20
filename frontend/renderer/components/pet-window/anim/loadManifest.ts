/**
 * loadManifest — 按约定目录生成动画素材清单
 * --------------------------------------------------------------------------
 * 帧素材放在 <frontend>/renderer/public/pet-anim/{state}/frame‑NN.png
 * （public 目录在 dev 与 build 都被原样提供，URL 固定为 /pet-anim/{state}/frame‑NN.png）。
 *
 * 文件名使用「U+2011 非断行连字符」+ 1 基序号（frame‑01.png …），由素材生产工具产出。
 * 当前已就绪状态（均 12fps）：
 *   - speaking:      24 帧（= 待机状态，idle 复用 speaking）
 *   - working:       49 帧
 *   - trans_speak_work: 14 帧（单向过渡）
 *   - trans_work_speak: 12 帧（单向过渡）
 *
 * 若某套帧文件尚不存在，SpriteFrameProvider.load 会失败并把 ready 置 false，
 * PetAnimator 自动回落到 ProceduralFrameProvider 兜底。
 */
import type { FrameManifest, FrameStateConfig, PetAnimState } from './types'

/** 每套动画的帧数 / fps / 是否循环 */
const CONF: Partial<Record<PetAnimState, Omit<FrameStateConfig, 'frames' | 'ready'>>> = {
  speaking: { fps: 12, loop: true },
  working: { fps: 12, loop: true },
  trans_speak_work: { fps: 12, loop: false },
  trans_work_speak: { fps: 12, loop: false },
}

/** 各套动画的帧数量（1 基，frame‑01 … frame‑N） */
const FRAME_COUNT: Partial<Record<PetAnimState, number>> = {
  speaking: 24,
  working: 49,
  trans_speak_work: 14,
  trans_work_speak: 12,
}

/** 资源根路径 */
export const PET_ANIM_BASE = '/pet-anim/'

/** U+2011 非断行连字符（与素材文件名一致） */
const NB_HYPHEN = '\u2011'

function frameName(i: number): string {
  const n = String(i).padStart(2, '0')
  return `frame${NB_HYPHEN}${n}.png`
}

/** 生成完整 FrameManifest（帧 URL 全部指向 /pet-anim/...；ready 初始 false） */
export function buildManifest(): FrameManifest {
  const manifest: FrameManifest = {}
  for (const state of Object.keys(CONF) as PetAnimState[]) {
    const c = CONF[state]!
    const count = FRAME_COUNT[state] ?? 0
    const frames = Array.from({ length: count }, (_, i) => `${PET_ANIM_BASE}${state}/${frameName(i + 1)}`)
    manifest[state] = { frames, fps: c.fps, loop: c.loop, ready: false }
  }
  // 待机状态等同于语音状态：idle 直接复用 speaking 的帧配置（指向同一帧数组）
  if (manifest.speaking) {
    manifest.idle = { ...manifest.speaking }
  }
  return manifest
}
