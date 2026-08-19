# 占位音频 + 唤醒词（KWS）方案

> 记录占位音频（Placeholder）设计与唤醒词（KWS）方案，供前端 Agent 开发 Electron 与后续扩展参考。
> 相关资源：`frontend/audio/placeholders/*.wav`（占位音频）；生成脚本 `backend/scripts/gen_placeholders.py`。

---

## 一、占位音频（Placeholder）

### 1.1 架构原则（前后端解耦）

- **占位音频放前端**：资源在 `frontend/audio/placeholders/`，由前端本地播放。
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
写成 24kHz mono 16bit WAV（纯 Python 加 RIFF 头，无第三方依赖）到 `frontend/audio/placeholders/`。

```powershell
cd backend
python scripts/gen_placeholders.py            # 全部
python scripts/gen_placeholders.py task_failed # 只合成某条
```

> 占位为**预生成低延迟资源**，运行时不再依赖云 TTS 实时合成，适合「立刻要播」的固定提示语。

---

## 二、唤醒词（KWS）—— Electron 阶段方案

### 2.1 唤醒词（已定）

- **主唤醒词：「宠伴」**（对应 PetPal Voice Agent「宠伴」；2 字、辨识度高、KWS 模型友好、不易与环境音误触发）。
- **备选别名：「球球」**（人设曾用名；但"球球"与 ASR 拟声词接近、易误触发，故作文案/别名而非主唤醒）。

### 2.2 路线确认：Electron 常驻 CPU KWS

- **纠正认知**：KWS 用**轻量小模型跑 CPU** 即可（流式推理 <50ms、功耗低，适合 7×24 待机监听）；
  **GPU 应留给完整大 ASR / LLM，不是 KWS 的必需**。若坚持 GPU 跑 KWS，仅适合超大/多唤醒词或极低延迟极端场景，一般用不上。
- **选型推荐**：**sherpa-onnx 流式 KWS**
  （KWS + 整句 ASR 同引擎、MIT、可自定义中文词表/发音字典、CPU 推理快）；
  备选 Picovoice Porcupine（需付费授权）、Vosk（KWS demo，略旧）。

### 2.3 Electron 主进程架构

```
Electron 主进程
  ├─ 持续麦克风采集（CPU 低占用）
  ├─ KWS 待机：仅跑轻量唤醒模型（<50ms/次），不唤醒不启动大模型
  ├─ 命中「宠伴」→ 触发唤醒
  │     └─ 播「我在呢～」（占位 wake_here）→ 启动完整 ASR 链路（此时才可选 GPU 大 ASR）
  └─ 待机/唤醒两档资源策略：唤醒后提升采样率 / 模型档位
```

### 2.4 Web 阶段过渡

- 浏览器无法可靠 7×24 常驻麦克风（后台标签节流、麦克风占用、autoplay 限制），
  故**web 测试看板本期不做常驻 KWS**；用「点击 / 按住说话(PTT)」过渡。
- KWS 代码留待 Electron 阶段，按本文档 2.2/2.3 落地；唤醒响应音频已预生成（`wake_here`/`wake_yes`）。

---

## 三、与后端 `_TOOL_PROGRESS` 的职责划分

- 后端 `_TOOL_PROGRESS`（工具开始时云 TTS 播报）与前端占位**职责有重叠**。
- 本期约定：
  - **短工具 / 即时回复**：不播占位（`_TOOL_PROGRESS` 对快工具本就是空）。
  - **长等待**：交给前端占位（前端等待超时才播），避免重复播报。
- 若后续想让后端首发「收到」占位，可由后端 `_TOOL_PROGRESS` 或单独事件触发前端播 `tool_start`，保持前端播放权不变。
