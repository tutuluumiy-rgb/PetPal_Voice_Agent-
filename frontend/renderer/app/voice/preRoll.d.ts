/**
 * preRoll 纯函数类型声明（renderer 为 TS 环境，preRoll.js 纯 JS 供 Node 测试直接跑）
 */
export const VAD_FRAME_MS: number
export const VAD_WINDOW_FRAMES: number
export const VAD_WINDOW_MS: number
export const PRE_ROLL_MS: number
export const PRE_SPEECH_PAD_MS: number

export interface VadWindowOpts {
  vadWindowFrames?: number
  vadFrameMs?: number
}

export interface PreRollOpts extends VadWindowOpts {
  preSpeechPadMs?: number
  preRollMs?: number
}

export function computeVadWindowStartMs(triggerTs: number, opts?: VadWindowOpts): number

export function computePreRollWindowMs(
  triggerTs: number,
  opts?: PreRollOpts,
): { startMs: number; endMs: number; windowStartMs: number }