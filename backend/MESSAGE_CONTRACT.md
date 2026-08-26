# 前后端消息契约（MESSAGE_CONTRACT）

> **唯一真相源**：前端 Agent（Electron 桌面应用）与后端 Agent 都以此文档为基准开发，
> 不得自行增改消息字段。任何变更必须同步更新本文档与 `backend/` 代码。
>
> 版本：v1.0
> 传输层：WebSocket `ws://127.0.0.1:8001/ws/audio`

---

## 1. 概述与协作模型

前端（Electron 桌面应用）与后端（FastAPI，`backend/main.py`）是**两套独立 Agent**：
内存不共享，只能通过 JSON 网络消息通信。

- 传输：一条 WebSocket 连接 `/ws/audio`。
- **二进制帧** = PCM 音频（16kHz / 16bit / 单声道），前端 → 后端（麦克风录音流）。
- **文本帧** = JSON 控制消息（见下）。前端 → 后端用下发的 `type` 上报事件；
  后端 → 前端用下发的 `type` 推送状态/事件/回复。

### 1.1 协作原则（豆包建议采纳，双方一致遵守）

1. 后端维护独立状态机 `idle / listening / thinking / speaking / error`。
   状态切换触发源：**仅来自前端上报事件**（`speech_start`(vad_speech_start)、`speech_end`(vad_speech_end)、
   `client_playback_done`、`user_abort`、`set_mode`…）+ 后端内部结果（LLM/工具/TTS）。
2. 后端发送 `backend_state_change` 事件**仅作为通知**，不做强制命令，不假设前端一定收到并同步状态。
3. **speaking 状态重要规则**：后端发送完全部 TTS 数据，**不直接退出 speaking**；
   必须等待前端上报 `client_playback_done`，才将状态切回。
   只要处于 speaking 状态，收到 `speech_start`(vad_speech_start) 就进入打断确认逻辑。
4. 收到 `user_abort` 事件：后端立刻终止 LLM 推理、终止正在运行的工具调用、
   取消 TTS、清空 buffer、状态重置为 `idle`。
5. **超时保护**：
   - `listening` 收到 `speech_start`(vad_speech_start) 后，长时间没有 `speech_end` → 自动退出收音。
   - `speaking` 长时间收不到 `client_playback_done` → 安全超时兜底复位。
6. 业务逻辑判断**优先依赖事件**，不完全依赖内部 state 变量；state 主要用于日志、调试、对外通知。
7. 所有交互字段以下文枚举为准，前端 Agent 遵照同一份文档开发。

---

## 2. 后端状态机

对外统一五态（后端内部 `pending_play` 归一到 `speaking`，不外泄）：

| 状态 | 含义 |
|------|------|
| `idle` | 空闲/已中止（收到 `user_abort` 或刚复位） |
| `listening` | 待命收音（默认态，等用户开口） |
| `thinking` | LLM 生成回复中（用户的话已识别完毕） |
| `speaking` | 正在播报 TTS（等前端 `client_playback_done` 才回 `listening`） |
| `error` | 异常（LLM 失败等） |

### 2.1 迁移表

| 从 | 到 | 触发条件 | 前端需关注 |
|----|----|----------|-----------|
| `idle` | `listening` | 自动/收到新输入 | 收到 `backend_state_change: listening` |
| `listening` | `thinking` | 有效 ASR 识别完成，进入 LLM | `backend_state_change: thinking` |
| `thinking` | `speaking` | 开始下发 TTS 并播放 | `backend_state_change: speaking` |
| `speaking` | `listening` | 前端上报 `client_playback_done` | `backend_state_change: listening` |
| `speaking` | `listening` | 打断确认（`vad_speech_start` + 二次确认） | `backend_state_change: listening` |
| 任意 | `idle` | 前端上报 `user_abort` | `backend_state_change: idle` |
| 任意 | `error` | LLM 等异常 | `backend_state_change: error` |

**speaking 不因 TTS 发送完退出**：后端发完 TTS 只是开始播放，必须等
`client_playback_done` 才回 `listening`。

---

## 3. 前端 → 后端消息（前端上报事件）

所有字段仅列必有字段；`type` 为必填。除 `set_mode/get_mode` 外，其余都是**事件上报**（不请求返回）。

### 3.1 `set_mode`
模式切换（手动按钮）。
```json
{ "type": "set_mode", "mode": "chat" | "work" | "toggle" }
```
- `mode: "toggle"` = 切换；`"chat"/"work"` = 指定。
- 后端回复 `mode_changed`。

### 3.2 `get_mode`
查询当前模式。
```json
{ "type": "get_mode" }
```
- 后端回复 `mode_changed`。

### 3.3 `speech_start`（语义别称：`vad_speech_start`）
前端 Silero VAD 判定「人声开始」。
```json
{ "type": "speech_start", "preRollBase64": "<base64 PCM 或 null>", "isPlaying": true|false }
```
- `preRollBase64`：开口前约 256ms 的 PCM（补 VAD 触发延迟丢的首字），可为 `null`。
- `isPlaying`：前端此刻是否仍有球球语音在播。为 `true` 且后端处于 `listening` 时，说明
  `client_playback_done` 兜底/竞态提前关了打断窗口而前端还在播 → 后端**立即掐断前端**
  （发 `barge_confirm`，前端销毁播放器丢弃旧音频），再进入正常收话；为 `false` 走原收话流程。
- 后端基于当前状态做打断/收音决策。
- > wire type 为 `speech_start`（向后兼容测试看板）；语义上等价于 `vad_speech_start`。Electron 按 `speech_start` 发送即可。

### 3.4 `speech_end`（语义别称：`vad_speech_end`）
前端 VAD 判定「人声结束」。
```json
{ "type": "speech_end" }
```
- 后端据此触发最终识别 `asr_final`。
- > wire type 为 `speech_end`（向后兼容测试看板）；语义上等价于 `vad_speech_end`。

### 3.5 `vad_cancel`
前端判定上次 `vad_speech_start` 是误报（misfire）。
```json
{ "type": "vad_cancel" }
```
- 后端撤销 ASR 会话、复位说话状态，回复 `asr_cancel`。

### 3.6 `client_play_start`
喇叭真正开始发声（第一帧开始播放）。
```json
{ "type": "client_play_start" }
```
- 后端据此**确保** state = speaking、打开打断窗口。若 TTS 已下发但喇叭未响（排队），靠此消息对齐。

### 3.7 `client_playback_done`
前端所有音频真正播放完毕。
```json
{ "type": "client_playback_done" }
```
- 后端才从 `speaking` 切回 `listening` 并启动尾音保护期。**关键：这是「播放结束」与「TTS 发送完」分离的信号。**

### 3.8 `client_barge_in`
前端本地打断（检测到插话）。
```json
{ "type": "client_barge_in", "latency": 0.312, "preRollBase64": "<base64 PCM 或 null>" }
```
- `latency`：用户开口 → 西西闭嘴的真实打断响应延迟（秒），可 `null`。
- `preRollBase64`：打断时回退的预卷音频，可为 `null`。
- 后端取消 TTS/LLM、进入 listening、启动流式 ASR。

### 3.9 `barge_latency`
前端上报打断延迟统计（实测值）。
```json
{ "type": "barge_latency", "latency": 0.287 }
```
- 后端累计并回发 `barge_avg`。

### 3.10 `user_abort`（新增）
用户/系统主动中止本次对话（立即终止全链路）。
```json
{ "type": "user_abort" }
```
- 后端：终止 LLM 推理、终止工具调用、取消 TTS、清空 buffer、复位 `idle`。

### 3.11 `stop`（测试看板兼容，Electron 可选）
停止当前播报，复位 `listening`（不断开连接）。
```json
{ "type": "stop" }
```
- 兼容旧测试看板按钮；Electron 建议用 `user_abort` 表达更强语义。

---

## 4. 后端 → 前端消息（后端推送）

除 `ready` 外均为事件/数据推送。前端按 `type` 分派即可。

### 4.1 `ready`
连接建立后立即返回。
```json
{ "type": "ready", "session_id": "abc12345" }
```

### 4.2 `backend_state_change`（新增）
后端状态机变化**通知**（仅通知，不强令前端同步）。
```json
{
  "type": "backend_state_change",
  "state": "listening" | "thinking" | "speaking" | "idle" | "error",
  "reason": "playback_done",
  "ts": 1750000000.123
}
```

### 4.3 `mode_changed`
模式变更结果（响应 `set_mode/get_mode`）或语音指令切换通知。
```json
{ "type": "mode_changed", "mode": "chat" | "work" }
```

### 4.4 `asr_partial`
ASR 流式中间结果（全量修订，覆盖显示，非追加）。
```json
{ "type": "asr_partial", "text": "今天天气挺" }
```

### 4.5 `asr_final`
ASR 最终识别（用户一句话识别完毕）。
```json
{ "type": "asr_final", "text": "今天天气怎么样" }
```

### 4.6 `asr_cancel`
撤销误报识别（响应 `vad_cancel`），前端清理残留的流式识别行。
```json
{ "type": "asr_cancel" }
```

### 4.7 `reply_start`
整段回复开始（前端标记「西西开口」）。
```json
{ "type": "reply_start" }
```

### 4.8 `reply` / `reply_append`
回复正文（感情色彩标签剥离后）。
```json
{ "type": "reply", "text": "今天天气很好呢~", "emotion": "开心" }
{ "type": "reply_append", "text": "适合出去走走。" }
```
- `reply`：首句；`reply_append`：后续句追加。

### 4.9 `reply_end`
整段回复发送完毕（TTS 可能仍在播放）。
```json
{ "type": "reply_end" }
```

### 4.10 `tts_start` / `tts_end`
单句 TTS 合成开始/结束。
```json
{ "type": "tts_start", "session_id": "abc12345", "text": "今天天气很好呢~" }
{ "type": "tts_end", "session_id": "abc12345" }
```
- 音频通过二进制帧下发（PCM 24000Hz，见 §5）。

### 4.11 `barge_confirm` / `barge_reject`
后端打断二次确认结果：
- `barge_confirm`：确认真打断（含后端确认耗时 `backend_ms`）。
- `barge_reject`：判定噪声/误报，恢复音量。
```json
{ "type": "barge_confirm", "backend_ms": 32.5 }
{ "type": "barge_reject" }
```

### 4.12 `resume_playback`
打断被判定无有效输入 → 恢复之前被打断的播报音量。
```json
{ "type": "resume_playback" }
```

### 4.13 `stop_playback`
停止当前（进度）音频播放，直接转最终回复。
```json
{ "type": "stop_playback" }
```

### 4.14 `timing`
每轮耗时统计（current + avg）。
```json
{
  "type": "timing",
  "current": { "asr": 0.3, "llm_first_token": 0.1, "llm_first_sentence": 0.9, "tts_first_packet": 0.9, "e2e": 1.5, "total": 2.1, "interrupted": false },
  "avg": { "asr": 0.25, "llm_first_token": 0.12, "llm_first_sentence": 0.85, "tts_first_packet": 0.9, "e2e": 1.4, "total": 2.0 },
  "count": 3
}
```
- 字段单位均为秒；`interrupted: true` 表示该轮被打断（部分数据）。

### 4.15 `barge_avg`
打断延迟平均。
```json
{ "type": "barge_avg", "avg": 0.31, "count": 5 }
```

### 4.16 `event`
事件流看板条目（测试/调试用，Electron 可选展示）。
```json
{ "type": "event", "round": 1, "stage": "LLM", "detail": "…", "ts": 1.2, "duration": 0.9 }
```

### 4.17 `barge_in`（遗留兼容，Electron 可不处理）
旧能量检测打断通知（旧路径遗留；当前主路径走 `barge_confirm/reject`）。
```json
{ "type": "barge_in" }
```

---

## 5. 音频流约定

- **前端 → 后端**：PCM Int16 单声道 16000Hz，每帧约 64ms（1024 采样点），二进制帧。
- **后端 → 前端**：PCM Int16 单声道 24000Hz，二进制帧（TTS 输出）。
- 前端用 Web Audio API 播放；`client_play_start` / `client_playback_done` 必须以**真实播放进度**为准。

---

## 6. 前后端协作时序（典型一轮）

```
前端                            后端
 │── speech_start ───────────────→   (listening 防重入/打断分支)
 │←──────────────── backend_state_change(listening) [若切态]
 │── speech_end ─────────────────→   触发 finalize
 │←──────────────── asr_final(text)
 │←──────────────── backend_state_change(thinking)
 │←──────────────── reply_start
 │←──────────────── reply(text)/reply_append(text)
 │←──────────────── tts_start / (音频帧...) / tts_end
 │── client_play_start ──────────→   确保 speaking（打断窗口开）
 │←──────────────── backend_state_change(speaking)
 │←──────────────── reply_end / timing
 │── client_playback_done ───────→   speaking → listening
 │←──────────────── backend_state_change(listening)
```

打断场景（speaking 中插话）：
```
前端                            后端
 │── speech_start ───────────────→   二次确认
 │←──────────────── barge_confirm / barge_reject
 │（确认后）── client_playback_done → 复位 listening
```

---

## 7. 字段命名与编码约定

- JSON 编码统一 UTF-8。
- 时间字段 `ts` 为 Unix 秒（float）；耗时字段（`timing`/`latency`）为秒（float）。
- 音频 base64 字段统一命名 `preRollBase64`（驼峰，勿改）。
- 模式枚举统一小写：`"chat"` / `"work"`。
- 状态枚举统一小写：`"idle" / "listening" / "thinking" / "speaking" / "error"`。

## 8. 变更流程

1. 任何一方需要新增/修改消息 → 先改本文档（回退原则：文档先行）。
2. 同步修改后端 `backend/main.py` / `backend/agent_state.py` 对应处理。
3. 前端 Agent 严格按本文档布局 Electron 前端。
4. 提交前跑 `backend/tests/test_state_machine.py` 确认状态机未破坏。
