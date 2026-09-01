/**
 * preRoll 纯函数（改造清单#1）
 * --------------------------------------------------------------------------
 * 修正 preRoll 的锚点与回退量：
 *   - 旧实现：从"VAD 判定完成时刻"（onSpeechStart 触发点）往前回退 PRE_ROLL_MS，
 *     实际覆盖的是省略判定窗口之外、摸不到开口最开头（判定窗口之误）。
 *   - 新实现：以【判定窗口起点】（第一帧被判为人声的帧 ≈ triggerTs − 判定窗口长）
 *     为锚，从起点再往前回退"爬坡期+余量（preSpeechPadMs）"，覆盖"开口最开头"。
 *
 * 纯 JS 模块，便于 Node 直接跑单元测试（frontend/tests/preRoll.test.mjs）。
 */

// 前端 Silero VAD 帧参数（512 帧，与后端 FRAME_SIZE=512 对齐；32ms/帧，可调 512/1024/1536）
export const VAD_FRAME_MS = 32
/** 稳闸判定帧数（= minSpeechFrames，VoicePipeline 配置） */
export const VAD_WINDOW_FRAMES = 6
/** 判定窗口总长：6 × 32 = 192ms */
export const VAD_WINDOW_MS = VAD_FRAME_MS * VAD_WINDOW_FRAMES
/** 预卷回退总量（ms），沿用原 PRE_ROLL_MS */
export const PRE_ROLL_MS = 256
/** 爬坡期+余量（ms）：开口弱音段概率爬升所需回退（❓ 可实测标定，默认 2 帧） */
export const PRE_SPEECH_PAD_MS = VAD_FRAME_MS * 2

/**
 * 计算 VAD 判定窗口起点（第一帧被判为人声的时刻，相对触发点 triggerTs 之前）。
 * @param {number} triggerTs onSpeechStart 触发时刻（performance.now()）
 * @param {{vadWindowFrames?: number, vadFrameMs?: number}} [opts]
 * @returns {number} 窗口起点时刻（ms）
 */
export function computeVadWindowStartMs(triggerTs, opts = {}) {
  const frames = opts.vadWindowFrames ?? VAD_WINDOW_FRAMES
  const frameMs = opts.vadFrameMs ?? VAD_FRAME_MS
  return triggerTs - frames * frameMs
}

/**
 * 计算 preRoll 应覆盖的时间窗口 [startMs, endMs)。
 * 锚点 = 判定窗口起点；回退 = 爬坡期+余量；总长 = preRollMs（默认 256ms）。
 * 即覆盖：判定窗口起点往前 padMs 处 起，向后 preRollMs 长（≈开口最开头 + 开口初期）。
 *
 * @param {number} triggerTs onSpeechStart 触发时刻
 * @param {{preSpeechPadMs?: number, preRollMs?: number, vadWindowFrames?: number}} [opts]
 * @returns {{startMs: number, endMs: number, windowStartMs: number}}
 */
export function computePreRollWindowMs(triggerTs, opts = {}) {
  const windowStartMs = computeVadWindowStartMs(triggerTs, opts)
  const padMs = opts.preSpeechPadMs ?? PRE_SPEECH_PAD_MS
  const lengthMs = opts.preRollMs ?? PRE_ROLL_MS
  const startMs = windowStartMs - padMs
  return { startMs, endMs: startMs + lengthMs, windowStartMs }
}