# PetPal Voice Agent（宠伴）🐱

> 一款可中途插话、能干活、高度拟人化的 AI 语音宠物。
> 定位：车载语音助理架构的可展示映射，服务于汽车行业 AI 语音岗位面试与自研展示。

**项目名**：PetPal Voice Agent（宠伴）
**曾用名**：球球 / 年年 / AI 语音宠物

---

## 这是什么

一个「网页语音 Agent Demo」：浏览器里对着一只 AI 宠物说话，它能听懂（ASR）、
思考并调用工具（LLM + 原生 function calling）、用带情绪的文字和声音回复（TTS），
还能在它说话时随时插话打断（barge-in）。

架构是 **级联模拟端到端**：
`耳朵（流式 ASR）→ 大脑（文字 LLM + 工具 + 记忆）→ 嗓子（情感 TTS）`

## 核心特性

- **双层打断（barge-in）**：前端 Silero VAD（体感层）快速 ducking + 后端二次确认（业务层）准确判定人声/噪声，AEC 由浏览器 WebRTC 消除回声。
- **双模式 Agent**：
  - 闲聊模式：`chat`（完整保留 20 轮、10 次内部调用上限、只给搜索/读取/计算三个工具）
  - 工作模式：`work`（完整保留 10 轮、30 次内部调用上限、全工具放开）
- **原生 function calling**：LLM 返回原生 `tool_calls` → 并发调度执行 → 按 `tool_call_id` 回填，工具结果 JSON 占位降维。
- **上下文压缩**：估算 token 达 `max_context × 0.7`（1M 上下文 → 700k）自动压缩旧轮为结构化检查点，会话完整 JSONL 可追溯。
- **规范化后端状态机**：`idle / listening / thinking / speaking / error`，事件驱动，`backend_state_change` 通知。
- **双 Agent 消息契约**：`backend/MESSAGE_CONTRACT.md` 是前端与后端两个独立 Agent 协作的唯一真相源。

## 目录结构

```
backend/            FastAPI 后端（WebSocket 音频流 + 编排）
  main.py           后端入口（python main.py [port]，默认 8001）
  agent_runtime.py  原生 function calling 多 sub_turn agent 环
  agent_state.py    规范化状态机（五态 + 超时兜底）
  compaction.py     上下文压缩检查点
  context_builder.py 按模式派生送模型的上下文视图
  session_store.py  会话层（全量 JSONL 持久化）
  tools/            工具集（搜索/读取/计算/天气/文件/提问）
  MESSAGE_CONTRACT.md  前后端消息契约（供前端 Agent 开发 Electron）
testboard/           测试看板（8001 后端 + 8080 静态服务）
  index.html        语音管道测试看板（AEC→VAD→ASR→LLM→TTS）
  vad/              Silero VAD / onnxruntime 本地资源
  audio/            预生成占位音频（placeholders/*.wav）
fix_port.py         一键清理占用 8001 的残留进程
```

> **目录命名约定**：`testboard/` 是**测试看板**（浏览器 demo，用于验证语音管道）；
> `frontend/` 目录**已预留给最终 Electron 桌面应用**（由另一套前端 Agent
> 依据 `backend/MESSAGE_CONTRACT.md` 开发，将占用 `frontend/` 命名）。
> 后端 VAD 模型路径已指向 `testboard/vad/` 下。

## 快速启动

```powershell
# ① 启动后端（保持窗口别关）
cd backend
python main.py            # 监听 0.0.0.0:8001

# ② 启动测试看板（可选，另一终端）
cd testboard
python -m http.server 8080

# 浏览器打开 http://127.0.0.1:8080/
```

若端口 8001 被残留进程占用（后端起不来 / 前端连不上），一键清理：
```powershell
python fix_port.py        # 扫 8001 监听进程并结束
```

## 环境变量

后端从 `backend/.env` 读取（`providers` 层按 `*_PROVIDER` 可插拔切换 ASR/LLM/TTS）。
`.env` / `*.env` 已在 `.gitignore`，**绝不提交**（含 API key）。

## 测试

```powershell
cd backend
python tests/test_state_machine.py   # 后端状态机单测（契约核心）
python tests/test_agent_core.py      # agent 底座单测（配置/会话/上下文/压缩）
```

## 协议 / 说明

本项目用于自研展示与学习，依赖第三方云接口（详见 `backend/.env` 与 `requirements.txt`）。
