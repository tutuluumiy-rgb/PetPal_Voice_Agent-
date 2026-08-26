/** 新增契约探针：voice:voices / model:get|set|check|list / history:delete（连 Mock 9000） */
import { GatewayClient } from '../main/services/gateway.ts'

const URL = process.env.MOCK_WS_URL ?? 'ws://127.0.0.1:9000/ws'
let failures = 0
function check(name: string, ok: boolean, extra = ''): void {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  (' + extra + ')' : ''}`)
  if (!ok) failures += 1
}

async function main(): Promise<void> {
  const gw = new GatewayClient(URL)
  gw.connect()
  await gw.waitForReady(10_000).catch((e) => {
    console.error('连接失败:', e.message)
    process.exit(1)
  })
  check('连接 + auth 握手', gw.isReady)

  // voice:voices
  const vl = await gw.voiceVoices()
  check('voice:voices 返回列表', Array.isArray(vl.voices) && vl.voices.length > 0, `count=${vl.voices.length}`)
  check('voice:voices 含 current', typeof vl.current === 'string' && vl.current.length > 0, `current=${vl.current}`)

  // model:get（5 组）
  const cfg = await gw.modelGet()
  check('model:get 5 组齐全', [cfg?.llm, cfg?.asr, cfg?.tts, cfg?.vision, cfg?.video].every((s) => s && typeof s.url === 'string'),
    `keys=${Object.keys(cfg ?? {}).join(',')}`)
  check('model:get llm 有掩码', typeof cfg?.llm?.api_key_masked === 'string', `masked=${cfg?.llm?.api_key_masked}`)

  // model:set（sections）
  const m2 = await gw.modelSet({ sections: { llm: { model: 'qwen-plus' }, tts: { voice: 'Cherry' } } })
  check('model:set → llm.model', m2?.llm?.model === 'qwen-plus', `model=${m2?.llm?.model}`)
  check('model:set → tts.voice', m2?.tts?.voice === 'Cherry', `voice=${m2?.tts?.voice}`)

  // model:list
  const ml = await gw.modelList('llm')
  check('model:list(llm) 返回列表', Array.isArray(ml?.models) && ml?.models?.length > 0, `count=${ml?.models?.length}`)

  // model:check
  const chk = await gw.modelCheck()
  check('model:check 返回 checks', Array.isArray(chk?.checks) && chk.checks.length === 5, `checks=${chk?.checks?.length}`)
  check('model:check 返回 ok/live', typeof chk?.ok === 'boolean' && !!chk?.live, `ok=${chk?.ok}, live=${chk?.live?.status}`)

  // history:delete（先造一条再删）
  const h1 = await gw.historyList(1, 50)
  const first = h1.items?.[0]
  if (first?.sessionId) {
    await gw.historyDelete(first.sessionId)
    const h2 = await gw.historyList(1, 50)
    check('history:delete → 列表减少', (h2.total ?? 0) < (h1.total ?? 0), `total ${h1.total}→${h2.total}`)
  } else {
    check('history:delete → 列表减少', false, '无 sessionId 可删')
  }

  gw.close()
  console.log(failures === 0 ? '\n== 新增 ops 全部通过 ==' : `\n== ${failures} 项失败 ==`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((e) => {
  console.error('探针异常:', e)
  process.exit(1)
})
