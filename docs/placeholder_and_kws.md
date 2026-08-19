# 占位音频 + 唤醒词（KWS）方案

> 记录占位音频（Placeholder）设计与唤醒词（KWS）方案，供前端 Agent 开发 Electron 与后续扩展参考。
> 相关资源：`testboard/audio/placeholders/*.wav`（占位音频，测试看板下）；生成脚本 `backend/scripts/gen_placeholders.py`。

---

## 一、占位音频（Placeholder）

### 1.1 架构原则（前后端解耦）

- **占位音频放前端**：资源在 `testboard/audio/placeholders/`（测试看板，Electron 阶段可整体挪到 frontend/），由前端本地播放。
- **播放控制权在前端**：什么时候播、播哪条、重复几次，均由前端自判（前端自维护计时器与状态）。
- **后端只发「停止播报」事件**：后端一旦开始下发正式回复，发 `stop_placeholder` 事件；前端收到即切断占位并清计时器。
- **独立音频通道**：占位用独立 `Audio` 对象播放，与后端流式 PCM 播放器（`pcmPlayback`）**完全隔离**，不混音、不共享状态。

### 1.2 资源与文案表

| key | 文案（占位音频内容） | 场景 |
|-----|--------------------|------|
| `wait_processing` | 收到！我正在帮你查～ | 等待回复（第一个） |
| `wait_working` | 正在执行任务，请稍等～ | 等待回复（第二个） |
| `wait_almost` | 我还在处理，马上就好～ | 等待回复（第三个） |
| `tool_start` | 好的，这就帮你去办～ | 工具/任务早期反馈 |
| `task_failed` | 任务失败了，要不你再试试？ | 任务失败 |
| `task_retry` | 刚刚遇到点小问题，我们再试一次好不好？ | 任务失败/重试建议 |
| `conn_lost` | 连接好像断了，稍等让我重新连一下～ | 连接意外断开 |
| `conn_timeout` | 后台好像卡住了，我重新连一下～ | 后端超时/卡住 |
| `wake_here` | 我在呢～ | 唤醒响应（Electron 阶段） |
| `wake_yes` | 嗯？我在～ | 唤醒响应（Electron 阶段） |

> 文案可增删。增改后重跑 `backend/scripts/gen_placeholders.py` 重新生成 WAV 即可。

### 1.3 前端触发 / 停止逻辑（index.html 已实现）

**触发（前端自判）**：
- **等待回复超时**：`onSpeechEnd`（用户说完）→ `startWaitPlaceholder()`：
  - 6s 内未收到 `reply`/`reply_start`/`tts_start` 等 → 播 `wait_processing`
  - 之后每 9s 播 `wait_working`、`wait_almost`，共最多 3 次。
- **连接失败**：WS 意外 `onclose` → 播 `conn_lost`。

**停止（收到即切，`stopPlaceholder()`）**：
`reply_start` / `reply` / `reply_append` / `tts_start` / `barge_confirm` / `barge_reject` /
`stop_playback` / `resume_playback` / `stop_placeholder` / `asr_final` / `reply_end` → 立即切断占位并清计时器。

### 1.4 生成脚本

`backend/scripts/gen_placeholders.py`：用现有 TTS（`providers.get_tts`）把文案合成一次，
写成 24kHz mono 16bit WAV（纯 Python 加 RIFF 头，无第三方依赖）到 `testboard/audio/placeholders/`。

```powershell
cd backend
python scripts/gen_placeholders.py            # 全部
python scripts/gen_placeholders.py task_failed # 只合成某条
```

> 占位为**预生成低延迟资源**，运行时不再依赖云 TTS 实时合成，适合「立刻要播」的固定提示语。

---

## 二、唤醒词（KWS）—— Electron 阶段方案

### 2.1 唤醒词（已定 + 本次修正）

- **陷阱（重要）**：专用 KWS 模型**只能识别训练时预置的关键词**，不能随意换成任意词；
  原定「宠伴 / 球球 / 你好小伴」都不在预训练 KWS 词表里 → **改用模型现成词**。
- **本次处理**：用 `download_kws.py` 下载模型并打印其关键词表，挑一个中文词作唤醒词
  （默认示例 **「你好小米」**）；展示文本在 `ContextCard.vue` 的 `wakeKeyword`，实际识别词由模型决定。
- **若未来坚持自定义词**：改用「流式中文 ASR（zipformer-zh）常驻转写 + 文本匹配」路线，
  任意词可用（较 KWS 略重）。

### 2.2 路线确认（最终落地）：Electron **主进程** + `sherpa-onnx-node`

- **WHY**：官方 JS/Electron 生态用 **Node 绑定 `sherpa-onnx-node`**（npm 确定存在，官方示例就是 `npm install sherpa-onnx-node`）；
  浏览器 wasm 版缺**轻量独立**运行时发布、`sherpa-onnx-wasm` 又非 npm 包，故弃 wasm、走主进程原生绑定。
- **架构**：
  ```
  渲染进程（采集，getUserMedia）─ IPC kws:feed(16k Float32) → 主进程
    主进程：sherpa-onnx-node（OnlineRecognizer + KWS 模型）流式推理
    主进程 ← 命中唤醒词 → IPC kws:wake 广播回渲染进程 → 进入对话（连后端 8001 + VAD）
  ```
- **落地文件**：
  - `frontend/scripts/download_kws.py`：下载 KWS 模型（官方 `kws-models` release URL，
    解压 encoder/decoder/joiner.onnx + tokens + keywords，打印关键词表）到 `frontend/resources/kws/` 或 `renderer/public/kws/`。
  - `frontend/main/kws.ts`：主进程 KWS（懒加载 `sherpa-onnx-node`，注册 `kws:feed`/`kws:wake` IPC）。
  - `frontend/main/index.ts`：app ready 时 `setupKws()`。
  - `frontend/preload/types.ts|index.ts`：新增 `kwsFeed` / `onKwsWake` 通道。
  - `frontend/renderer/app/voice/VoicePipeline.ts`：待机帧走 `window.api.kwsFeed`，监听 `onKwsWake` 进对话。

### 2.3 依赖与验证点（重要）

- **依赖**：`cd frontend && npm i sherpa-onnx-node`（官方 npm 包）。
  - Electron 报**原生模块 ABI 不匹配**时：`npx @electron/rebuild -f -w sherpa-onnx-node`。
- **模型**：`cd frontend && python scripts/download_kws.py`（需联网；官方 `kws-models` release
  `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`），并打印关键词表挑词。
- **验证点**（本 agent 沙箱**无法联网/无法跑 Electron**，需用户本机验证）：
  1. 下载脚本能拉到模型并打印关键词表；
  2. `npm i sherpa-onnx-node` 成功、主进程 `[kws]` 日志出现「唤醒词已就绪」；
  3. 喊唤醒词 → 进对话、说完回待机。起不来看主进程 `[kws]` 日志（缺库/缺模型/ABI 不匹配都有明确提示）。

### 2.4 交互（已定）

- **待机听唤醒，喊词才进对话，说完回待机**：
  - 启动即 `start({ wakeWord: true })` → `idle` 待机（麦克风帧 IPCI 喂主进程 KWS）。
  - 主进程命中 → `kws:wake` → `_enterConversation()` 连后端 8001 + VAD → `listening`。
  - 一轮 `reply_end` → `_backToWake()` 回待机。
- 头部「🎙 语音」按钮保留：点击直接进/退手动对话（`setMicState(true,false)`）。


---

## 三、与后端 `_TOOL_PROGRESS` 的职责划分

- 后端 `_TOOL_PROGRESS`（工具开始时云 TTS 播报）与前端占位**职责有重叠**。
- 本期约定：
  - **短工具 / 即时回复**：不播占位（`_TOOL_PROGRESS` 对快工具本就是空）。
  - **长等待**：交给前端占位（前端等待超时才播），避免重复播报。
- 若后续想让后端首发「收到」占位，可由后端 `_TOOL_PROGRESS` 或单独事件触发前端播 `tool_start`，保持前端播放权不变。
