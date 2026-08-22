# agent.md — PetPal Voice Agent 前端工程说明（给 Agent / 新成员）

> 本文档是**当前工程状态的权威速览**，供 AI Agent / 新成员接手时快速建立心智模型，
> 避免踩已知的坑。正式需求与边界以 `docs/前端开发提示词.md`（任务提示词）为准，
> 后端契约以 `docs/后端对接交底.md` 为准。**本文档应随工程演进同步更新。**

---

## 1. 项目是什么

**PetPal Voice Agent** — 桌面端 AI 语音虚拟形象客户端（悬浮宠物 + 控制面板 + 对话面板），
Electron-Vite + Vue3 + TypeScript + TailwindCSS。Windows 桌面应用，宠物为真实猫咪照片。

**三个独立 BrowserWindow（三个图层，彻底解耦）：**

| 窗口 | 尺寸 | 特性 | 说明 |
|---|---|---|---|
| 宠物窗口 | 220×280 | 透明、无边框、置顶、**尺寸恒定（永不缩放）** | 画布绘制猫咪动画；可左键拖拽；右键打开对话面板 |
| 对话面板 | 350×550 | 透明、无边框、置顶、尺寸恒定、非模态 | 独立窗口承载语音管线；由宠物位置定位（四象限对角）；打开/关闭只 show/hide，不影响宠物窗口 |
| 控制面板 | 800×620 | 可缩放、非模态 | 各设置页（语音参数/历史/人设/用户档案/动画生成/关于） |

> ⚠️ **尺寸恒定是硬约束**：宠物窗口与对话面板窗口**必须是 220×280 / 350×550 恒定**，
> 任何代码不得允许它们被系统/OS 放大（详见 §5 已知坑）。

---

## 2. 目录结构（frontend/ 内）

```
frontend/
├─ docs/                    # 工程文档（本文件、前端开发提示词、后端对接交底、会话记录）
├─ main/                    # Electron 主进程：入口 / 窗口 / IPC / 拖拽 / 状态 / KWS
│  ├─ index.ts              # 入口：媒体权限放行、单实例锁、注册 IPC、KWS、创建窗口
│  ├─ windows.ts            # 三窗口创建/定位/强制锁尺寸（enforceLockedSize）
│  ├─ ipc.ts                # 全部 IPC 通道注册 + 广播
│  ├─ drag.ts               # 宠物拖拽（帧合并 rAF 节流 setPosition）
│  ├─ state.ts              # 全局状态（mode / authPolicy / petVisible）
│  └─ kws.ts                # 唤醒词（sherpa-onnx-node，懒加载）
├─ preload/                 # IPC 桥接层（无业务逻辑）
│  ├─ types.ts              # ⭐ IPC 通道 + AppApi 类型【单一事实源】
│  ├─ index.ts              # contextBridge 暴露 window.api
│  └─ index.d.ts            # 全局类型注入
├─ renderer/
│  ├─ pet.html / chat.html / panel.html   # 三窗口 HTML 入口
│  ├─ main-pet.ts / main-chat.ts / main-panel.ts
│  ├─ app/voice/VoicePipeline.ts          # 语音管线（直连真实后端 8001 /ws/audio）
│  ├─ assets/               # pet-photo.png（真实猫咪照片）等
│  ├─ public/
│  │  ├─ pet-anim/{state}/  # ⭐ PNG 多帧动画素材（见 §4）
│  │  ├─ vad/               # VAD 资源（onnx/wasm，gitignore，勿手动入库）
│  │  └─ placeholders/      # 占位音频 wav
│  ├─ styles/               # design-tokens.css / index.css
│  └─ components/
│     ├─ pet-window/        # PetWindow.vue + pet-canvas.ts + hooks.ts + anim/
│     │  └─ anim/           # 动画引擎（见 §4.1）
│     ├─ chat-panel/        # ChatPanel.vue（独立对话面板，承载语音管线）
│     └─ control-panel/     # 控制面板（PanelShell + SidebarNav + views/*）
└─ package.json             # scripts: dev / build / start / typecheck
```

---

## 3. 进程分层与数据流（守则）

```
renderer（纯 UI + 交互）
   │  contextBridge → window.api（仅白名单能力）
preload（只做 IPC 桥接，无业务逻辑）
   │  ipcMain（main/ipc.ts）
main（全部业务逻辑 + 全局状态；持有网络能力）
```

- **渲染进程不可直接访问网络**（`contextIsolation: true`、`nodeIntegration: false`），
  唯一例外：语音音频 WS（`VoicePipeline`）。
- **`preload/types.ts` 是 IPC 契约唯一事实源**：通道名改这里，`main`/`preload`/`renderer` 三方共享。
- 新通道命名约定：全小写、冒号分隔（如 `voice-preview`）；方向明确：
  - R→M 用 `ipcMain.on`（send）或 `ipcMain.handle`（invoke）
  - M→R 用 `webContents.send` 广播（多窗口同步靠主进程遍历 `BrowserWindow.getAllWindows()`）

### 当前 IPC 通道速查

| 域 | 通道 | 方向 | 说明 |
|---|---|---|---|
| 模式 | `mode:get` / `mode-switch` / `mode:changed` | R→M / R→M / M→R | chat / work |
| 权限 | `auth-policy:get` / `auth-policy:set` | R→M | full / ask |
| 窗口 | `panel:open` | R→M | 打开控制面板 |
| 拖拽 | `pet:drag-start` / `pet:drag-move` / `pet:drag-end` | R→M | 主进程节流 setPosition |
| 对话面板 | `chat-panel:open` / `chat-panel:close` | R→M | show/hide 面板，不影响宠物 |
| 可见性 | `pet:set-visible` / `pet:get-visible` / `pet:visible-changed` | R→M / R→M / M→R | 隐藏后只能从控制面板重新开启 |
| 版本 | `app:version` | R→M | invoke |
| TTS（预留） | `tts-start` / `tts-end` | M→R | 说话动画联动 |
| KWS | `kws:feed`（16k Float32）/ `kws:wake` | R→M / M→R | 主进程 sherpa-onnx 推理 |
| 语音播报 | `voice-preview:push` / `voice-preview` / `voice-preview:get` | R→M / M→R / R→M | 对话面板→宠物底部消息条 |
| 动画联动 | `pet-anim` / `pet-anim:changed` | R→M / M→R | 'speaking' / 'idle' |
| 网关状态 | `backend:status` | M→R | connecting/connected/disconnected |
| 文本对话 | `chat:send` / `chat:abort` | R→M | 主进程网关 chat:send（流式）/ 中止 |
| 流式事件 | `chat:running` / `chat:delta` / `chat:done` / `tts:event` | M→R | 运行态 / 增量 / 完成(含 audio) / TTS 播放 |
| 历史 | `history:list` / `history:search` | R→M (invoke) | 分页查询 |
| 人设/用户 | `personality:get/set` / `user:get/set` | R→M (invoke) | markdown 读写 |
| 语音参数 | `voice:settings:get/set` | R→M (invoke) | volume/pitch/voice |
| 动画诊断 | `anim:debug` | R→M | 宠物窗口上报素材就绪/过渡路径 → 主进程终端 |

> 后端网关：`main/services/gateway.ts`（纯协议客户端）+ `main/services/backendGateway.ts`
> （Electron 单例）。协议见 `backend/docs/MOCK_CONTRACT.md`（Mock 9000，与未来真实后端同契约）。
> ⚠️ **主进程 console 日志一律用英文**：Windows 终端 GBK 编码会把 UTF-8 中文打成乱码
> （`鐘舵€佸彉鍖`）；已把 `main/services/*`、`main/kws.ts` 的日志改为英文，新增日志沿用此约定。

---

## 4. 宠物动画系统

### 4.1 文件结构（renderer/components/pet-window/anim/）

| 文件 | 职责 |
|---|---|
| `types.ts` | `PetAnimState`（idle/speaking/listening/working/thinking/happy/sad/sleeping/surprised + trans_*）、`FrameStateConfig`、`FrameManifest` |
| `loadManifest.ts` | 按约定生成素材清单；idle 复用 speaking 帧配置（待机=语音，渲染层同帧循环） |
| `SpriteFrameProvider.ts` | PNG 多帧素材播放（懒加载，`ready` 标志；`draw` 居中绘制） |
| `ProceduralFrameProvider.ts` | 程序化兜底动画（基于真实照片的呼吸/摆动，按状态参数表） |
| `PetAnimator.ts` | rAF 播放器：状态机、`feedTts`、`setMode`、`playTransition`、prefetch |

**状态解析**：`resolveState` 把 `idle` 统一解析为 `speaking`（待机=配音：idle 也在播 speaking 帧循环，
帧共用不重复加载；帧不可用时程序化兜底）。

**四态动画模型（定稿）**：动画**只由模式状态决定**，说话/安静不参与选动画——
| 视觉态 | 何时显示 |
|---|---|
| 闲聊模式 | 处于闲聊模式（无论说不说话）→ `speaking` 循环 |
| 工作模式 | 处于工作模式（无论说不说话）→ `working` 循环 |
| 过渡·闲聊→工作 | 切模式瞬间播一次 → 落到 working |
| 过渡·工作→闲聊 | 切模式瞬间播一次 → 落到 speaking |

**过渡规则**（`transitionState`）：
- `speaking → working`（含 `idle→working`）→ `trans_speak_work`
- `working → speaking`（含 `working→idle`）→ `trans_work_speak`
- `idle ↔ speaking` → `trans_idle_speak / trans_speak_idle`（素材未制作 → 干净直接切换；**
  此映射不再被触发**，因为说话不再驱动过渡）
- 过渡素材未就绪 → **异步加载后补播**（`playTransition` 的 pending 机制），加载失败才直接切目标态

**渲染优先级**：PNG 帧素材就绪则画序列帧（idle 也走 speaking 帧）；不可用时程序化形变兜底。

> ✅ **素材已就绪（2025-06 确认）**：`frontend/pictures/`（源）与 `renderer/public/pet-anim/`（运行时副本）
> 四套完全同步——`speaking`24 / `working`49 / `trans_speak_work`14 / `trans_work_speak`12，全为 U+2011
> 连字符、12fps。`loadManifest` 帧数配置与之一致。切换动画依赖这四套真实素材：
> 闲聊循环 speaking、工作循环 working、闲聊→工作 trans_speak_work、工作→闲聊 trans_work_speak。

### 4.2 素材约定（⭐ 容易踩坑）

帧素材位于 `renderer/public/pet-anim/{state}/`，URL 固定为 `/pet-anim/{state}/frame‑NN.png`。

- **文件名使用 U+2011 非断行连字符（`‑`）**，不是普通 `-`！代码里是常量 `NB_HYPHEN = '\u2011'`。
- **帧号 1 基**（`frame‑01.png` 开始），补零 2 位。
- 当前已就绪套（全部 12fps）：

| 状态 | 帧数 | 循环 | 说明 |
|---|---|---|---|
| `speaking` | 24 | ✅ | 说话/工作播报状态（= 待机视觉，idle 复用） |
| `working` | 49 | ✅ | 工作状态 |
| `trans_speak_work` | 14 | ❌ | 单向过渡 |
| `trans_work_speak` | 12 | ❌ | 单向过渡 |

- 素材源目录：`pictures/`（用户制作区）。**更新帧时把最终帧放到 `renderer/public/pet-anim/{state}/` 即生效**。
- `trans_idle_speak` / `trans_speak_idle` 的素材尚未制作——**不要声称它们存在**，代码已做缺素材回退。

### 4.3 动画联动链（切换事件 = 何时发生切换）

| # | 触发事件 | 何时发生 | 动画器动作 | 结果 |
|---|---|---|---|---|
| 1 | `pet-anim:changed → 'speaking'`（TTS 开始） | 西西开始播报 | `feedTts(true)` | **不切换动画**（四态模型：说话不驱动过渡）；仅记录标志位 |
| 2 | `pet-anim:changed → 'idle'`（TTS 结束，面板 1.2s 去抖） | 西西说完 | `feedTts(false)` | **不切换动画**；仅记录标志位 |
| 3 | `mode:changed`（模式卡片/语音切换/主进程侧切换） | 立即 | `setMode` → `playTransition(speaking\|working, newBase)` | 闲聊→工作：**trans_speak_work**；工作→闲聊：**trans_work_speak**；播完落对应循环 |
| 4 | `pet:visible-changed` false / `visibilitychange` | 隐藏/切走 | `pause()`；显示 → `resume()` | — |

链路：`ChatPanel.vue`（语音管线 TTS 事件）→ `window.api.setPetAnim('speaking'|'idle')` → 主进程广播 `pet-anim:changed` → `PetWindow.vue` 订阅 → `animator.feedTts(on/off)`；
`mode:changed` 广播 → `PetWindow` → `animator.setMode(mode)`（宠物窗口挂载时也会 `getMode()` 对齐一次初始模式）。
所有事件入口均带 `[pet-anim] event …` 日志（打到主进程终端），便于核对"切换是否到达/走了哪支"。

> **动画模型（定稿）**：说话态（含待机）= `speaking` 循环；工作模式 = `working` 独立循环；
> 两种模式之间的切换各用一套过渡（闲聊→工作 `trans_speak_work`、工作→闲聊 `trans_work_speak`）。
> 切换触发 = **语音切换**（后端 ASR 命中"切换到…"→ `mode:changed` 广播）或**按钮切换**（模式卡片）。
> 闲聊模式下的"说话起止"因 `trans_idle_speak/trans_speak_idle` 素材缺失而 direct（闲聊循环即待机视觉），
> 若需 morph 需补那两套帧（放入 `renderer/public/pet-anim/{state}/` 即自动生效）。

### 4.4 底部语音消息条（宠物窗口）

- 位置：`voiceBarTop = PET_CANVAS_SIZE.height - BALL_BOTTOM_PADDING + 30`（猫咪下方 30px）。
- 固定**单行 30px**；`measureMarquee()` 测量文本宽度：**仅当文本超出消息条宽度时**才启用
  `anim-marquee` 横向滚动（8s），未超宽则**静态显示**（`marqueeOn` 开关，文本变化时重测）。
- **点击消息条 → `openChatPanel()`**（打开上下文对话面板）。
- `BALL_BOTTOM_PADDING = 68`（`pet-canvas.ts`）：程序化兜底动画必须锚定在
  `h - BALL_BOTTOM_PADDING` 上方，**否则猫咪会被消息条遮挡**（已修，勿回退）。

---

## 5. 已知坑与已修复问题（改代码前必读）

### 5.1 ⚠️ Windows 透明无边框窗口尺寸累积放大（曾经的核心 Bug）

**症状**：宠物窗口拖拽几次后物理尺寸从 220×240 膨胀到 491×620、716×987……X/Y 同时增长。

**根因**：Windows 下 `transparent: true` 无边框窗口 + `BrowserWindow` 的
`useContentSize: true` 会在**每次 `setPosition`（拖拽）/`show` 时反复做 outer↔content
换算并逐次累积**，导致窗口越来越大。

**修复（三管齐下，缺一不可，勿回退）**：
1. **创建窗口时不设 `useContentSize`**（width/height 直接作为窗口总尺寸）。
2. **`enforceLockedSize(win, W, H, tag)`**：监听 `resize` 事件，发现尺寸偏离立即
   `setResizable(true) → setSize(W,H) → setResizable(false)` 扳回（`_forceResizing`
   防重入）。
3. **`setMinimumSize` + `setMaximumSize`** 等于目标尺寸（min=max），硬锁。

### 5.2 其他已解决事项（保持现状）

- **面板闪烁**：`showInactive` 显示不抢焦点；关闭用 `hide()` 不 `destroy()`（快速重开）。
- **面板跟随拖拽**：已移除——对话面板是**独立图层**，宠物拖拽**不**跟随重定位
  （`repositionChatAfterPetDrag` 已从 drag.ts 移除调用）。
- **面板定位**：四象限对角规则（宠物在右上→面板左下，依次类推）；
  `GAP_X=44`、`GAP_Y=-100`（X 离宠物 +40 再 -20 微调、Y -100），clamp 到工作区。
- **消息条/宠物重叠**：兜底动画锚定 `h - BALL_BOTTOM_PADDING`（见 §4.4）。
- **唤醒词**：KWS 在主进程（`main/kws.ts`），帧素材与 VAD 资源已 gitignore，**勿手动入库**。
- **多句回复抢话（修复）**：后端逐句 `tts_start`（句1 → 句2 …），旧逻辑每个
  `tts_start` 都无条件 `resetPlayback()`，第二句的 `tts_start` 会把第一句仍在播放的
  音频掐掉造成"两句抢话"。修复：`VoicePipeline.ts` 的 `tts_start` 仅在时间线空闲
  （`_hasActivePlayback()` 为 false）时才重置，否则新句音频经 `nextStartTime` 自然接续；
  同时接通契约的 `stop_playback` / `barge_confirm` → 立即停播。动画侧 `ChatPanel.vue`
  对 `tts_end` 的 idle 切换加了 1.2s 延迟（句间不闪断，下一句 start 取消）。
- **语音口语退出（已接通 + 可见反馈）**：`VoicePipeline` 的 `EXIT_WORDS`
  （拜拜/再见/退出对话/不聊了/晚安…）命中 `asr_final` 即回到待机（唤醒模式）或聆听
  （直连模式）；`ChatPanel` 经 `onExit` 回显一句"先聊到这儿～说「唤醒词」随时再叫我"。
- **待机状态已取消**：不再把"待机"当独立状态——空闲/待唤醒即「闲聊模式」状态（= speaking
  帧循环，与闲聊模式一致的视觉）；唤醒词只是进入对话的开关。语音按钮在语音开启时空闲时
  显示「闲聊模式 · 说「你好西西」唤醒」，不再出现「待机中」字样。
- **语音回复整段一个气泡**：`ChatPanel` 的 `voice.onReply(reply/reply_append)` 改为
  聚合显示——整段回复开头新建气泡，后续句子追加进同一气泡，不再逐句成条。
- **模式/状态切换动画**：`PetAnimator.setMode` 空闲切换 chat↔work 时播放
  `trans_speak_work` / `trans_work_speak` 过渡（素材已就绪；idle 与 speaking 画面层等价，
  复用两套过渡），并加过渡定时器防竞态串台（`transitionTimer` 清理）。
- **LLM 失败可见化（后端联动）**：后端在 LLM 流式异常（网络/限流/额度耗尽 403）时，
  改为给前端发一条道歉 `reply` + `reply_end` 并把状态机复位 `listening`
  （`backend/main.py`，原本静默退出会卡 speaking）。前端按既有 `reply/reply_end`
  处理即可显示该气泡并回聆听态，无需额外适配。
- **模式/权限弹卡被窗口裁剪（修复）**：弹出卡原定位在按钮**下方**（`r.bottom+4`），而按钮栏
  贴面板底 → 卡片超出 350×550 窗口边界被裁。改为**按钮上方弹出**：`fixed + bottom` 锚定
  （`cardPosAbove`，距按钮上缘 6px，x 方向 clamp 到窗口内），并维持 `z-[10000]` 衬于面板内容之上。
- **语音切模式三端同步（修复）**：后端语音命中"打开工作模式"等会经 8001 发 `mode_changed`，
  此前 `VoicePipeline` 未处理该消息（掉 default 被丢）→ 面板选项、宠物动画都不变。现：
  `VoicePipeline` 新增 `onModeChanged` 处理 `mode_changed` → `ChatPanel` 调
  `window.api.switchMode(mode)` → 主进程 `setMode` 广播 `mode:changed` → 面板选项 + 宠物动画
  同步更新（模式唯一真源 = 主进程状态；三端：面板/动画/语音）。
- **切换动画重复播放（修复）**：切到工作模式时过渡播两次——切模式 `setMode → trans_speak_work`
  后，紧随的 TTS 事件（语音切换后的"已切换到…"通知回复、或上一轮回复的延迟 idle 事件）又触发
  `feedTts` 把同一段 `speaking→working` 过渡再播一遍。修复：`PetAnimator.feedTts` 加**状态守卫**
  ——已在说话（或正在切向说话的过渡中）则 `on=true` 跳过；已离开说话态（如已落在 working）则
  `on=false` 跳过，不再重复播放过渡。
  **语音触发补充**：语音切模式时模式过渡 A 放一半，通知回复的 `tts_start` 会发起 `trans_work_speak`
  （B）并把 A 掐掉，随后 `tts_end` 又回落（C）→ 视觉上"放两次"。`feedTts(true)` 增加
  **过渡在播检测**（`transInFlight`：`transitionTimer` 有效且处于过渡态）：过渡在播时不打断、
  不新起过渡，等 A 自然播完 → `displayState` 按 `speakingFlag` 直接进说话；结束时因已落在
  working，回落守卫也跳过 C → **语音/按钮切换都只播一次 morph**。
- **模式三端（面板/动画/后端）彻底同步（修复）**：此前按钮切模式只更新前端+9000 网关，
  **8001 后端 `mode_state` 未同步** → 问"是什么模式"时 LLM 按后端旧模式作答（面板显示聊天、
  回复却说工作）。修复：
  - `ChatPanel.selectMode` 按钮切换 → `voice.setBackendMode(mode)` 发 `set_mode` 给 8001；
  - `VoicePipeline` 新增 `setBackendMode()`；连接 `ready` 后自动发 `get_mode` → 后端回
    `mode_changed` → `onModeChanged` → 主进程广播 → 面板+动画同步（覆盖重连/历史不一致）。
  现在按钮/语音/后端/动画四处模式单一真源一致。
- **LLM 偶尔无回复（后端加固，联动）**：根因① LLM 调用无超时（provider 挂起→整轮卡 speaking）；
  ② 空响应（整轮零句子→前端静默）。修复：
  - `providers/llm.py` 单次超时 `LLM_TIMEOUT_S`（默认 45s）、`agent_loop.py` 透传 timeout；
  - `main.py` 整轮零句子补发兜底话术；
  - **单流式重构（agent_runtime.py，主链路）**：旧的"决策非流式 + 回复流式"两次请求会丢弃
    第一轮正文、且第二次流式偶发空 → 空回复。现改为**每轮一个带工具的流式请求**：正文随流
    逐句 cut 出播报（第一轮=工具执行前的前言、最后一轮=最终答复，**都是流式播报**，首包快、
    可打断）；同时累积工具调用 JSON 片段，流结束时按 `tool_calls` 决定执行工具进下一轮或收尾。
    `main.py` 的罐头进度 TTS 增加去重（前言已播则不再播，`full_reply` 为空才播）。
  `timeout` 透传主链路（`main.py` 传 `timeout=llm.timeout`）。
- **工作模式仍播闲聊动画（修复）**：动画器内部 `state` 可能停在 `speaking/idle` 与基底 `base`
  不同步（切模式/打断时序残留），旧渲染只认 `state`。新增 `displayState()` 推导展示态——
  过渡在播→过渡态；说话中→speaking；否则→基底。渲染 `renderFrame` 与 `feedTts` 守卫均改用它，
  **空闲时必然渲染基底对应动画**（工作模式=working 循环，不再出现"切了工作还播闲聊"）。
- **消息条显示完整回复（修复）**：旧逻辑每句都独立 `pushVoicePreview(句)` → 只显最后一句。
  现按轮累计：**聊天=整轮所有句子之和**；**工作=第一轮 + 最新一轮**（`第一轮…最新轮`，单轮时
  即整段）；新一轮用户发言时清零重来。

### 5.3 素材命名陷阱

- 帧文件名的连字符是 **U+2011** 而非普通 `-`：复制素材、写脚本、做对比时不要用普通 `-` 去匹配（曾经因此加载失败）。
  → 加固：`SpriteFrameProvider` 加载帧时**两种连字符都试**（U+2011 / U+002D），任一命中即成功，
  避免素材被工具规范化后整套帧 404；加载失败会 `console.warn("[anim] <state> 素材加载失败…")` 便于排查。
- 帧序号 **1 基 + 补零 2 位**（`frame‑01`）。
- 切换过渡动画：`PetAnimator.playTransition` 在过渡素材未就绪时会**先异步加载、就绪后补播**，
  不再直接硬切（前提：`speaking`/`working` 循环素材本身能加载，说明 URL 路径正常）。

---

## 6. 常用命令

```bash
npm run dev          # 开发运行（源码模式；用户日常即此命令）
npm run build        # electron-vite 构建（类型检查 + 打包产物 out/）
npm run typecheck    # tsc(node) + vue-tsc(web) 双重类型检查
npm start            # electron-vite preview（预览构建产物）
```

改动后必做：`npm run typecheck` 通过 +（必要时）`npm run build`；主进程改动需重启 dev 进程生效。

---

## 7. 环境与工作约定

- **统一使用 PowerShell 7（pwsh）**：当前环境就是 PowerShell 7，以后**一切命令**（启动/构建/脚本/诊断）一律用
  `pwsh`（PowerShell 7）执行，**禁止使用其他版本**：不用 Windows PowerShell 5.1
  （`powershell.exe`）、不用便携版 pwsh、不用 `cmd.exe`。若环境提示找不到 `pwsh`，
  先安装/切换到 PowerShell 7 再执行，不要私自换成别的 shell。
- 前端只允许改 `frontend/` 目录；**禁止改动** `backend/`、`testboard/`、根目录 `docs/`；
  前后端协议/字段/端口不变更。
- 不提交模型文件（`.onnx`/`.wasm`）、`.env`、密钥（已 gitignore）。
- 诊断日志约定（调试时输出格式）：
  - `[win:*]`（logSizes）、`[pet:resize]`/`[pet:move]`、`[pet:force-resize]`、
    `[pet-canvas]`、`[pet]`、`[chat:resize]`/`[chat:move]`、`[chat:*]`。

---

## 8. 待办 / 未来工作（按前端职责）

### ✅ 已完成（P0：控制面板 → 后端网关 → Mock 9000）

- **主进程网关**：`main/services/gateway.ts`（WS 连接/auth 握手/心跳/请求关联/指数退避重连/
  流式事件）+ `main/services/backendGateway.ts`（单例，接窗口广播 + 供 IPC 调用）。
  协议严格遵循 `backend/docs/MOCK_CONTRACT.md`。
- **文本对话**：`ChatInputBar` 发送/中止 + `ChatPanel` 消息区流式累积（running/delta/done/
  tts 事件），Mock 返回的 wav 会播放（说话动画联动的 P1 完善见下）。
- **历史记录**：`HistoryView` 分页查询/搜索（loading/空态/错误态/上一页下一页）。
- **人设 / 用户档案**：`PersonalityView` / `UserProfileView` 经网关读写，带保存反馈。
- **语音参数**：`VoiceSettingsView` 经网关读写（音量/音调/音色）。
- **模式 / 权限**：`chat:send` 流式与 `mode/auth:policy` 均经网关与 Mock 同步，
  `mode:changed` 广播回本地状态二次同步。
- **验证**：`scripts/probe-gateway.ts` 驱动网关走完整契约（18 项全 PASS）；
  `npm run typecheck` / `npm run build` 通过；dev 启动后 `[gw] 握手成功 → connected`。
- 依赖新增：`ws`（主进程 WebSocket 客户端，Electron 主进程 Node 20 无内置 WS）+ `@types/ws`。

### 待后续迭代

- `hooks.parseActionTag`：目前 ChatPanel 已剥离 `【action:xxx】` 标签并用于触发说话动画；
  更丰富的动作→动画状态映射（happy/sad/thinking/working…）需扩展 `pet-anim` 通道状态枚举。
- `trans_idle_speak` / `trans_speak_idle` 素材制作后，往 `renderer/public/pet-anim/` 放并同步 `loadManifest`。
- 控制面板其它页（AnimationGen/About）接入真实能力；文件上传（+）按钮。

## 9. 控制面板对接与 UI 改造（已完成一批）

- **右下角操作栏**：`renderer/app/panelActions.ts` 单例注册页面动作，`PanelShell` 右下渲染
  （右 30/底 25、50×27、圆角 6、并排；撤销=次钮、保存=主钮）；人设/用户档案/语音参数页
  已注册，页内保存按钮移除。
- **历史按 session 抽屉（事件可折叠）**：`history:list` 一次性对话=一个条目（session 粒度，标题=日期+首输入前 20 字+…，
  显示消息数/轮数），展开调 `history:detail` 渲染该会话的事件轨迹时间线
  （user/assistant/tool/tool_result，按 ts 升序；runId 变化时按「轮」分段）。
  每条事件为「标题行（图标+时间+类型，点击折叠）+ 内容在标题下方」的纵向布局，类型标签不带「· 轮N」。
- **语音状态指示灯**：宠物消息条左侧原 🎙 改为**圆点**——待机/超时断开=橙、唤醒后聆听/回复=绿、
  未启用=灰（`voice-state` 广播：ChatPanel → 主进程 → 宠物窗口）。
- **新建会话**：①对话面板头部「语音开关右侧、关闭左侧」新增「＋」按钮（清空消息+重连语音后端，
  后端按连接建新会话）；②口语「创建新对话/新建会话/换个话题/重新开始对话…」命中同样触发
  （`VoicePipeline.newSession()`，`NEW_SESSION_WORDS` 词表可扩充）。
- **消息条滚动按播报速度**：`PetWindow.marqueeDuration = 字数 / 4 字每秒`（clamp 4~30s），
  滚动节奏与语音播报一致；文本变化重挂载动画（`:key="voiceText"`）。
- **页面介绍为功能描述**：人设/用户档案/语音参数/历史/动画生成页的副标题与保存提示均改为
  功能化描述，不再出现文件路径/技术名词。
- **真实后端管理端点 `/ws/mgmt`（8001）**：personality→`prompts/personality.md`、user→
  `users/<ACTIVE_USER>/profile.json`（结构化）、voice→`data/voice_settings.json`（voice 音色
  真拼入 TTS instruct、volume/pitch 为真实数值参数）、auth:policy→`data/auth_policy.json`、
  mode→`mode_state`、history→`sessions/*.jsonl` 按 run 聚合。
  **网关默认连真实后端 `ws://127.0.0.1:8001/ws/mgmt`，连续 2 次失败自动回退 Mock 9000**
  （环境变量 `PETPAL_MGMT_WS_URL` 可覆盖主地址），跑 `python main.py` 即真实接入。
- **控制面板**：窗口标题「PetPal 控制面板」+ 标题栏图标用 `logo.png`（public 副本）；
  底部版本/产品标识块先不展示（已删）。
- **双皮肤**：`html[data-skin='light']` token 覆盖（白底黑字），ChatPanel/ChatInputBar
  全面 token 化（深色下输入框不再白底）；ChatInputBar 权限后新增「皮肤」按钮 + 展开卡片
  （选项文案：深色 / 浅色，不备注配色）；主进程 `skin` 状态+广播+`userData/skin.json` 持久化。
- **logo 统一**：`LogoImg.vue` 使用 `assets/logo.png`（SidebarNav 头部/关于/登录占位）。
- **隐藏宠物开关**：ChatPanel 维护 `petVisible`（get/set + 订阅广播），按钮可隐藏又可恢复。