# PetPal Voice Agent — 前端对接契约（Mock 服务器）

> 本契约 = 前端 Electron 主进程与后端服务之间的 WebSocket 消息协议。
> **当前由 Mock 服务器（`backend/mock_server.py`）兑现**，不连真实 DB/LLM/TTS，返回假数据，
> 供前端开发/联调界面。后续接真实后端时**应遵守同一份契约**，仅替换数据来源。
>
> 依据：`frontend/docs/后端对接交底.md`（方案 A）已在本文件落为具体字段/格式/错误码。
> 传输：WebSocket 长连接 `ws://127.0.0.1:9000/ws`（默认端口 9000）。

---

## 1. 传输与通用帧

- **单条 WebSocket 连接**承载命令与事件。
- 文本帧为 UTF-8 JSON。
- **客户端 → 服务端（请求）**：
  ```json
  { "type": "<op>", "id": "c-123", ...业务字段 }
  ```
- **服务端 → 客户端（响应，带 id）**：
  ```json
  { "type": "<op>:ok",    "id": "c-123", ...结果 }
  { "type": "<op>:error", "id": "c-123", "code": "E_XXX", "message": "人类可读" }
  ```
- **服务端 → 客户端（主动事件，无 id）**：`{ "type": "<event>", ... }`。

命名：字段/事件 camelCase；时间戳 **Unix 毫秒**（`Date.now()`）；分页 `page`（1 起始）+ `pageSize`。

---

## 2. 连接生命周期

### 2.1 握手
客户端连上后先发：
```json
{ "type": "auth", "id": "h-1", "clientId": "desktop-1", "token": "fake-token" }
```
响应：
```json
{ "type": "auth:ok", "id": "h-1", "clientId": "mock-d2a117d7" }
```
失败（可选）：`{ "type": "auth:error", "id": "h-1", "code": "E_UNAUTHORIZED", "message": "..." }`，服务端关闭连接。

### 2.2 心跳
```json
{ "type": "ping", "id": "p-1" }   →   { "type": "pong", "id": "p-1" }
```

### 2.3 断线重连
客户端指数退避重连；重连后重新 `auth`。Mock 不持有跨连接状态（内存态，重连即复位）。

---

## 3. 功能域接口

### 3.1 对话 / LLM

**发送** `chat:send`：
```json
{ "type": "chat:send", "id": "c-1", "mode": "chat", "text": "你好", "sessionId": "mock-s1" }
```
响应为**流式**事件（服务端推送，共享 `id`）：
```
{ "type": "chat:running",        "sessionId": "mock-s1", "running": true }
{ "type": "chat:send:start",     "id": "c-1", "sessionId": "mock-s1" }
{ "type": "chat:send:delta",     "id": "c-1", "sessionId": "mock-s1", "text": "...", "action": null }
...（多个 delta）
{ "type": "chat:send:done",      "id": "c-1", "sessionId": "mock-s1",
  "reply": { "text": "...", "action": "wave" }, "audio": "<base64 wav>" }
{ "type": "tts:start", "sessionId": "mock-s1" }
{ "type": "tts:end",   "sessionId": "mock-s1" }
{ "type": "chat:running",        "sessionId": "mock-s1", "running": false }
```
- `chat:running` 驱动前端"运行中/可中断"状态。
- `reply.text` 可能含动作标签 `【action:xxx】`（前端 `parseActionTag` 解析并映射动画）。
- `reply.action` 为规范化动作名（`wave`/`blink`…）。
- `audio`：可选的 TTS 音频 base64（可空）。`tts:start`/`tts:end` 驱动前端"说话动画"。

**中止** `chat:abort`：
```json
{ "type": "chat:abort", "id": "c-9", "sessionId": "mock-s1" }
→ { "type": "chat:abort:ok", "id": "c-9", "aborted": true }
→ { "type": "chat:running", "sessionId": "mock-s1", "running": false }
```

### 3.2 历史

**列表** `history:list`：
```json
{ "type": "history:list", "id": "h-1", "page": 1, "pageSize": 20, "mode": "chat" }
→ { "type": "history:list:ok", "id": "h-1",
    "items": [ { "id": "h-1001", "mode": "chat", "time": 1787112257402, "preview": "你好西西" } ],
    "total": 4, "page": 1 }
```
**搜索** `history:search`：同结构，加 `keyword`：
```json
{ "type": "history:search", "id": "h-2", "keyword": "新闻", "page": 1, "pageSize": 20 }
```

**详情（run 事件轨迹）`history:detail`**（新增 v1.1）：按 run 拉取有序事件流（控制面板"抽屉展开"用）：
```json
{ "type": "history:detail", "id": "h-3", "sessionId": "01ccb25ade8c", "runId": "a1b2c3d4" }
→ { "type": "history:detail:ok", "id": "h-3", "runId": "a1b2c3d4",
    "title": "2025-06-18 21:33 在干嘛…",
    "events": [
      { "ts": 1787112257.4, "kind": "user", "text": "在干嘛？", "subTurn": 1 },
      { "ts": 1787112258.1, "kind": "assistant", "text": "[开心]在陪你呀~", "subTurn": 1 },
      { "ts": 1787112259.0, "kind": "tool", "name": "search", "args": {"query": "天气"}, "subTurn": 2 },
      { "ts": 1787112260.5, "kind": "tool_result", "name": "search", "text": "…结果摘要…", "subTurn": 2 },
      { "ts": 1787112261.2, "kind": "assistant", "text": "[平静]查好啦…", "subTurn": 3 }
    ] }
```
- `kind` 枚举：`user | assistant | tool | tool_result | system`；`events` 按 `ts` 升序（时间线）。
- 真实后端（`/ws/mgmt`）：`history:list` 按 `run_id` 聚合 `backend/sessions/*.jsonl`，
  `preview` 取该 run **首个 user 内容前 20 字 + …**；`history:detail` 返回该 run 的全部事件。

### 3.3 人设 / 用户档案（markdown）

```
personality:get → { "type":"personality:get:ok", "id", "content": "<markdown>" }
personality:set → { "type":"personality:set", "id", "content": "<markdown>" } → ok
user:get        → { "type":"user:get:ok", "id", "content": "<markdown>" }
user:set        → { "type":"user:set", "id", "content": "<markdown>" } → ok
```

### 3.4 语音参数

```
voice:settings:get → { "type":"voice:settings:get:ok", "id",
                       "volume": 80, "pitch": 50, "voice": "default" }
voice:settings:set → { "type":"voice:settings:set", "id",
                       "volume": 70, "pitch": 55, "voice": "cute" } → ok（回读同结构）
```
- `volume`/`pitch`：0–100（滑块默认 80/50）。
- `voice`：`default | cute | calm | bright`（默认 `default`）。

### 3.5 模式

```
mode:get → { "type":"mode:get:ok", "id", "mode": "chat" }
mode:set → { "type":"mode:set", "id", "mode": "work" }
        → { "type":"mode:set:ok", "id", "mode": "work" }
        → 服务端广播 { "type":"mode:changed", "mode": "work" } 到所有连接
```
- `mode`：`chat | work`。前端单选态以 `mode:changed` 广播为准（可被主进程侧 ASR 切换）。

### 3.6 权限策略

```
auth:policy:get → { "type":"auth:policy:get:ok", "id", "policy": "ask" }
auth:policy:set → { "type":"auth:policy:set", "id", "policy": "full" } → ok
```
- `policy`：`full`（完全批准）| `ask`（请求批准）。

---

## 4. 错误码

统一错误帧：`{ "type": "<op>:error", "id": "<关联id>", "code": "E_XXX", "message": "..." }`

| code | 含义 |
|------|------|
| `E_VALIDATION` | 参数校验失败（如 mode 不是 chat/work） |
| `E_NOT_FOUND` | 未知消息 type / 资源不存在 |
| `E_UNAUTHORIZED` | 鉴权失败 |
| `E_TIMEOUT` | 超时 |
| `E_LLM_*` / `E_TTS_*` / `E_ASR_*` | 对应服务错误（按域扩展） |
| `E_INTERNAL` | 内部错误 |

---

## 5. Mock 服务器使用方法

```powershell
cd backend
python mock_server.py          # 监听 0.0.0.0:9000，endpoint /ws
```

访问地址：**`ws://127.0.0.1:9000/ws`**（局域网使 IP 用 `0.0.0.0` 绑定的本机 IP）。
不连真数据库/LLM/TTS，全部假数据，前端可随时重启、无副作用。

## 6. 真实后端管理端点（v1.1）

真实后端（`python main.py`，8001）提供**同一份契约**的管理端点：

```
ws://127.0.0.1:8001/ws/mgmt
```

- 与语音端点 `/ws/audio` 并存（同一个 FastAPI 应用、独立路径），**不互扰**。
- 已实现的域：`auth / ping / mode:get|set / auth:policy:get|set / personality:get|set /
  user:get|set / voice:settings:get|set / history:list|search|detail`。
- **真实数据源**：
  - `personality:*` → `backend/prompts/personality.md`
  - `user:*` → `backend/users/<ACTIVE_USER>/profile.json`（返回/接收结构化的 `basic/reply_style/likes/dislikes/daily`，不再是 markdown）
  - `voice:settings:*` → `backend/data/voice_settings.json`（持久化；`voice` 映射进 TTS 语气指令）
  - `auth:policy:*` → `approval_policy`；`mode:*` → `mode_state`
  - `history:*` → `backend/sessions/*.jsonl`（按 `run_id` 聚合）
- **前端网关地址**：环境变量 `PETPAL_MGMT_WS_URL` 覆盖（默认 `ws://127.0.0.1:9000/ws` = Mock；跑真实后端时设 `ws://127.0.0.1:8001/ws/mgmt`）。

联调顺序建议（与交底文档一致）：`auth` → `mode:get` / `chat:send` → `tts:start/end` → `history` → `personality/user` → `voice:settings` → `auth:policy` → `chat:abort`。
