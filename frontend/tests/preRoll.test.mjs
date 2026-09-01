/**
 * 前端改造（清单#1 / #3）单元测试 —— Node 直接运行（无需构建）
 *   cd frontend && node tests/preRoll.test.mjs
 * 覆盖：
 *  1. preRoll 锚点：窗口起点 = 触发时刻 − 判定窗口(576ms)；回退 = 爬坡+余量(192ms)
 *  2. preRoll 覆盖区间计算
 *  3. 静态契约：VoicePipeline.ts 中 barge_latency 计算先于 speechStartTime 清空（清单#3 死代码修复）
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  VAD_FRAME_MS,
  VAD_WINDOW_FRAMES,
  VAD_WINDOW_MS,
  PRE_ROLL_MS,
  PRE_SPEECH_PAD_MS,
  computeVadWindowStartMs,
  computePreRollWindowMs,
} from '../renderer/app/voice/preRoll.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
let failures = 0
function check(name, cond, detail = '') {
  if (cond) {
    console.log(`[PASS] ${name}`)
  } else {
    failures += 1
    console.error(`[FAIL] ${name} ${detail}`)
  }
}

// ── 1. 窗口起点锚定（512 帧 = 32ms/帧，与后端对齐）──
const triggerTs = 10_000_000
check('VAD_WINDOW_MS = 6×32 = 192', VAD_WINDOW_MS === 192, `got ${VAD_WINDOW_MS}`)
check('VAD_FRAME_MS = 32', VAD_FRAME_MS === 32)
check('PRE_ROLL_MS = 256', PRE_ROLL_MS === 256)
check('PRE_SPEECH_PAD_MS = 64（爬坡+余量，2帧）', PRE_SPEECH_PAD_MS === 64, `got ${PRE_SPEECH_PAD_MS}`)

const windowStart = computeVadWindowStartMs(triggerTs)
check('窗口起点 = triggerTs − 192ms', windowStart === triggerTs - 192, `got ${windowStart}`)

// ── 2. preRoll 覆盖区间（锚点=窗口起点，往前 pad 64ms，总长 256ms）──
const win = computePreRollWindowMs(triggerTs)
check('preRoll.startMs = 窗口起点 − 64ms', win.startMs === triggerTs - 192 - 64, `got ${win.startMs}`)
check('preRoll.endMs = start + 256ms', win.endMs === win.startMs + 256, `got ${win.endMs}`)
check(
  'preRoll 覆盖“开口最开头”（窗口起点附近），而非旧的“判定完成点前256ms”',
  win.endMs <= triggerTs,  // 新行为 end = triggerTs−192−64+256 = triggerTs−0 = triggerTs（≤触发点）
  `endMs=${win.endMs} triggerTs=${triggerTs}`,
)
check('configurable: preSpeechPadMs/preRollMs 可覆盖', computePreRollWindowMs(triggerTs, { preSpeechPadMs: 32, preRollMs: 128 }).startMs === triggerTs - 192 - 32)

// ── 3. 静态契约：barge_latency 先于 speechStartTime 清空（清单#3）──
const vp = readFileSync(join(__dirname, '../renderer/app/voice/VoicePipeline.ts'), 'utf8')
const nullIdx = vp.indexOf("this.speechStartTime = null")
const latIdx = vp.indexOf("type: 'barge_latency'")
check('VoicePipeline.ts 存在 barge_latency 上报', latIdx >= 0)
check(
  'barge_latency 计算出现在 speechStartTime=null 之前（死代码已修复）',
  latIdx !== -1 && nullIdx !== -1 && latIdx < nullIdx,
  `latIdx=${latIdx} nullIdx=${nullIdx}`,
)
// 额外静态契约：_slicePreRoll 以判定窗口起点为锚（调用时传 speechStartTime）
check(
  'preRoll 切取使用锚点参数（_slicePreRoll(this.speechStartTime)）',
  /_slicePreRoll\(this\.speechStartTime\)/.test(vp),
)

if (failures > 0) {
  console.error(`\n${failures} 项失败`)
  process.exit(1)
}
console.log('\n前端逻辑测试全部通过（preRoll 锚点修正 + barge_latency 死代码修复 静态验证）')