# 打断机制整体架构

## 双层分工
- **前端 Silero VAD**（renderer / testboard 浏览器）：体感层，检测到人声立即降低播放音量（ducking），并发 `speech_start` 给后端
- **后端 VAD + ASR**（main.py `_confirm_real_speech`）：业务层，二次确认是真人声还是回声/噪声，决定是否真打断

```mermaid
flowchart LR
    subgraph "前端（renderer / testboard）"
        Mic[麦克风 16k PCM]
        Silero[Silero VAD<br/>6帧过线=576ms 判定人声]
        Ducking[ducking: gain → 0.2<br/>DUCKING_TIMEOUT_MS=2000 兜底]
        WsSend[speech_start<br/>+ preRoll 256ms]
        Play[pcmPlayback<br/>gainNode → destination]
        WsRecv[barge_confirm / barge_reject<br/>resume_playback / asr_final]
    end

    subgraph "后端（main.py 8001）"
        VAD2[二次确认<br/>preRoll + speaking_audio_cache<br/>256ms RMS + 5段连续]
        ASR[ASR 流式会话]
        LLM[LLM agent 流水线]
        TTSTask[tts.speak_and_send<br/>逐句合成]
        Cancel[tts.cancel<br/>+ task.cancel<br/>+ abort_speaking=True]
    end

    Mic --> Silero
    Silero -->|判定人声| Ducking
    Silero --> WsSend
    Ducking --> Play
    WsRecv -->|barge_confirm| Cancel

    WsSend -->|WS 8001| VAD2
    VAD2 -->|2204/2205 噪声| WsRecv
    VAD2 -->|确认人声| ASR
    VAD2 --> Cancel
    Cancel -->|取消| TTSTask
    Cancel -->|取消| LLM
    TTSTask -.音频流.-> Play
    LLM -.reply / reply_append.-> WsRecv
```

## 当前 Electron vs testboard 的关键差异

| 模块 | testboard | Electron VoicePipeline.ts |
|---|---|---|
| **ducking** | ✓ `startDucking()` 立即压音量 0.2 | ✗ 无（直接 gain=1）|
| **barge_confirm 处理** | 保持 ducking 静音**等结果**，再决定销毁/恢复 | `resetPlayback()` 立即销毁 |
| **新播报冲突防护** | `pendingBargeResume` 标记 + 3s 兜底 | 靠 `reply` / `tts_start` 的 resetPlayback |
| **old-audio dropping** | `pendingBargeResume` 期间忽略 `_onAudio` 数据 | 无（直接进时间线）|