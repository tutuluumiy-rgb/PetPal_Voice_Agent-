/**
 * KWS 唤醒词 — Electron 主进程（sherpa-onnx-node → KeywordSpotter）
 * --------------------------------------------------------------------------
 * 用官方 Node 绑定 `sherpa-onnx-node` 的 **KeywordSpotter**（不是 OnlineRecognizer）
 * 在主进程做关键词检测：
 *   - 模型：`download_kws.py` 下载到 <frontend>/resources/kws/
 *     （encoder/decoder/joiner.onnx + tokens.txt + keywords.txt）
 *   - 麦克风：主进程无 getUserMedia，音频由渲染进程采集后经 IPC `kws:feed`
 *     一帧帧发来（16k Float32 PCM）。
 *   - 命中：主进程 `webContents.send(IPC_CH.kwsWake, keyword)` 广播回渲染进程，
 *     渲染进程收到后进入对话。
 *
 * 依赖（官方 npm，已装）：
 *   cd frontend && npm i sherpa-onnx-node
 *   若 Electron 报原生模块 ABI 不匹配：npx @electron/rebuild -f -w sherpa-onnx-node
 *
 * 准备（跑一次，需联网）：
 *   cd frontend && python scripts/download_kws.py   # 下模型到 resources/kws/
 */
import { BrowserWindow, ipcMain } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { IPC_CH } from '../preload/types'

/** 模型目录：优先 <cwd>/resources/kws，退回 renderer/public/kws */
function modelDir(): string {
  const candidates = [
    path.join(process.cwd(), 'resources', 'kws'),
    path.join(process.cwd(), 'renderer', 'public', 'kws'),
  ]
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, 'encoder.onnx'))) return c
  }
  return candidates[0]
}

interface KwsState {
  spotter: any
  stream: any
  ready: boolean
  onWake: (keyword: string) => void
}

let kws: KwsState | null = null

/** 初始化 KWS（懒加载 sherpa-onnx-node；缺库/缺模型时打印明确报错） */
export function initKws(): void {
  if (kws) return
  // 1) 本地静态模型
  const dir = modelDir()
  const encoder = path.join(dir, 'encoder.onnx')
  if (!fs.existsSync(encoder)) {
    console.warn('[kws] 未找到模型，跳过唤醒词。请先：python scripts/download_kws.py（模型应含 encoder.onnx）')
    return
  }
  // 2) require 原生绑定
  let sherpa: any
  try {
    sherpa = require('sherpa-onnx-node')
  } catch (e) {
    console.error('[kws] 加载 sherpa-onnx-node 失败，请先 cd frontend && npm i sherpa-onnx-node (ABI 不匹配: npx @electron/rebuild -f -w sherpa-onnx-node)', e)
    return
  }
  if (!sherpa?.KeywordSpotter) {
    console.error('[kws] sherpa-onnx-node 缺少 KeywordSpotter（版本过旧）')
    return
  }
  // 3) 关键词表（优先模型自带 keywords.txt）
  const keywordsFile = ['keywords.txt', 'keywords.bpe', 'keywords']
    .map((n) => path.join(dir, n))
    .find((p) => fs.existsSync(p))

  const config: Record<string, unknown> = {
    featConfig: { sampleRate: 16000, featureDim: 80 },
    modelConfig: {
      // KWS zipformer（transducer）模型三件套
      transducer: {
        encoder,
        decoder: path.join(dir, 'decoder.onnx'),
        joiner: path.join(dir, 'joiner.onnx'),
      },
      tokens: path.join(dir, 'tokens.txt'),
      numThreads: 1,
      provider: 'cpu',
      debug: false,
    },
    maxActivePaths: 4,
    numTrailingBlanks: 1,
    keywordsScore: 1.0,
    keywordsThreshold: 0.25,
  }
  if (keywordsFile) config.keywordsFile = keywordsFile

  // 4) 创建 KeywordSpotter + stream
  const spotter = new sherpa.KeywordSpotter(config)
  const stream = spotter.createStream()
  kws = {
    spotter,
    stream,
    ready: true,
    onWake: (keyword) => {
      for (const win of BrowserWindow.getAllWindows()) {
        if (!win.isDestroyed()) win.webContents.send(IPC_CH.kwsWake, keyword)
      }
    },
  }
  console.log('[kws] 唤醒词已就绪, 模型目录:', dir, ', 词表:', keywordsFile ? path.basename(keywordsFile) : '未指定')
}

/** 喂入一帧 16k 浮点音频（渲染进程经 IPC kws:feed 上报） */
function feedPcm(float32: Float32Array): void {
  if (!kws?.ready) return
  try {
    kws.stream.acceptWaveform({ samples: float32, sampleRate: 16000 })
    while (kws.spotter.isReady(kws.stream)) {
      kws.spotter.decode(kws.stream)
    }
    const result = kws.spotter.getResult(kws.stream)
    const keyword: string = result?.keyword
    if (keyword) {
      console.log('[kws] 命中唤醒词:', keyword)
      kws.onWake(keyword)
      try {
        kws.spotter.reset(kws.stream) // 命中后复位，准备下一次唤醒
      } catch { /* ignore */ }
    }
  } catch (e) {
    console.error('[kws] 推理出错', e)
  }
}

/** 注册 IPC：渲染进程喂入 16k 浮点音频帧 */
export function registerKwsIpc(): void {
  ipcMain.on(IPC_CH.kwsFeed, (_event, payload: unknown): void => {
    if (!kws?.ready) return
    let data: Float32Array
    if (payload instanceof Float32Array) {
      data = payload
    } else if (ArrayBuffer.isView(payload)) {
      data = new Float32Array((payload as ArrayBufferView).buffer)
    } else {
      return
    }
    feedPcm(data)
  })
}

/** 主进程启动时调用一次 */
export function setupKws(): void {
  registerKwsIpc()
  initKws()
}
