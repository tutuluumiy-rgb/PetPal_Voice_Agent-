/**
 * 网关联调探针 — 直接驱动 GatewayClient 走完整 Mock 契约（无需 Electron）
 * --------------------------------------------------------------------------
 * 用法（需先启动 mock_server.py）：
 *   cd frontend
 *   node --experimental-strip-types scripts/probe-gateway.ts
 *
 * 覆盖：auth 握手 / mode get+set（含广播）/ auth:policy / chat:send 流式(含 running/delta/done/tts)
 *       / chat:abort / history:list+search / personality / user / voice:settings
 * 全部 PASS 退出码 0；任一失败退出码 1。
 */
import { GatewayClient } from '../main/services/gateway.ts'

const URL = process.env.MOCK_WS_URL ?? 'ws://127.0.0.1:9100/ws'
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

let failures = 0
function check(name: string, ok: boolean, extra = ''): void {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  (' + extra + ')' : ''}`)
  if (!ok) failures += 1
}

async function main(): Promise<void> {
  const gw = new GatewayClient(URL)

  // 连接状态变化
  const seen: string[] = []
  gw.onStatus((s) => {
    seen.push(s)
  })

  gw.connect()
  await gw.waitForReady(10_000).catch((e) => {
    console.error('连接失败:', e.message)
    process.exit(1)
  })
  check('连接 + auth 握手', gw.isReady, `状态序列=${seen.join(',')}`)

  // ── 模式 ──
  const mg = await gw.modeGet()
  check('mode:get', mg.mode === 'chat', `mode=${mg.mode}`)

  let modeChanged = false
  const unsubMode = gw.onModeChanged((m) => {
    if (m === 'work') modeChanged = true
  })
  await gw.modeSet('work')
  await sleep(300)
  check('mode:set + mode:changed 广播', modeChanged)
  unsubMode()
  await gw.modeSet('chat') // 复位

  // ── 权限 ──
  const ag = await gw.authPolicyGet()
  check('auth:policy:get', (ag.policy as string) === 'ask', `policy=${ag.policy}`)
  await gw.authPolicySet('full')
  const ag2 = await gw.authPolicyGet()
  check('auth:policy:set → get 回读', (ag2.policy as string) === 'full', `policy=${ag2.policy}`)
  await gw.authPolicySet('ask')

  // ── 对话（流式） ──
  const stream: string[] = []
  let runningSeen = false
  let doneSeen = false
  let ttsSeen = false
  const done = await gw.chatSend('chat', '你好', {
    onRunning: (r) => {
      runningSeen = runningSeen || r
    },
    onDelta: (d) => stream.push(d.text),
    onDone: (d) => {
      doneSeen = true
      if (d.action) check('chat 动作标签', true, `action=${d.action}`)
    },
    onTts: () => {
      ttsSeen = true
    },
  })
  const replyText = (done.reply as { text: string })?.text ?? ''
  check('chat:send 返回 done', doneSeen && replyText.length > 0, `reply=${replyText.slice(0, 30)}…`)
  check('chat:send 流式 delta 收到', stream.length >= 1, `deltas=${stream.length}`)
  check('chat:send running 事件', runningSeen)
  // done 之后 mock 还会发 tts:start/end 与 chat:running(false)，稍等再断言
  await sleep(400)
  check('chat:send tts 事件', ttsSeen)
  await gw.chatAbort()

  // ── 历史 ──
  const h1 = await gw.historyList(1, 20)
  check('history:list', Array.isArray(h1.items) && h1.items.length > 0 && h1.total > 0, `total=${h1.total}, items=${h1.items.length}`)
  const h2 = await gw.historySearch('天气', 1, 20)
  check('history:search', Array.isArray(h2.items), `total=${h2.total}`)
  const firstRun = h1.items[0]
  if (firstRun && firstRun.sessionId) {
    const det = await gw.historyDetail(firstRun.sessionId)
    check('history:detail 事件轨迹', Array.isArray(det.events) && det.events.length > 0 && typeof det.title === 'string',
      `events=${det.events?.length}, title=${(det.title ?? '').slice(0, 20)}`)
  } else {
    check('history:detail 事件轨迹', false, '列表缺 sessionId')
  }

  // ── 人设 / 用户 ──
  const p = await gw.personalityGet()
  check('personality:get', typeof p.content === 'string' && p.content.length > 0, `len=${p.content.length}`)
  await gw.personalitySet('# 测试人设\n- 来自探针写入')
  const p2 = await gw.personalityGet()
  check('personality:set → get 回读', p2.content.includes('测试人设'), `len=${p2.content.length}`)

  const u = await gw.userGet()
  check('user:get（结构化）', typeof u?.basic?.name === 'string', `name=${u?.basic?.name}`)
  await gw.userSet({
    basic: { name: '探针用户', role: 'owner' },
    reply_style: '简洁',
    likes: ['测试'],
    dislikes: [],
    daily: { wake_time: '07:00', sleep_time: '23:00' },
  })
  const u2 = await gw.userGet()
  check('user:set → get 回读', u2?.basic?.name === '探针用户', `name=${u2?.basic?.name}`)

  // ── 语音参数 ──
  const v = await gw.voiceSettingsGet()
  check('voice:settings:get', typeof v.volume === 'number' && typeof v.voice === 'string', `vol=${v.volume}, pitch=${v.pitch}, voice=${v.voice}`)
  const v2 = await gw.voiceSettingsSet({ volume: 66, pitch: 44, voice: 'cute' })
  check('voice:settings:set → 回读', v2.volume === 66 && v2.pitch === 44 && v2.voice === 'cute', `vol=${v2.volume}, pitch=${v2.pitch}, voice=${v2.voice}`)

  gw.close()
  check('关闭连接', true)

  console.log(failures === 0 ? '\n== 全部通过 ==' : `\n== ${failures} 项失败 ==`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((e) => {
  console.error('探针异常:', e)
  process.exit(1)
})