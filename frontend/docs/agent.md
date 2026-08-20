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

---

## 4. 宠物动画系统

### 4.1 文件结构（renderer/components/pet-window/anim/）

| 文件 | 职责 |
|---|---|
| `types.ts` | `PetAnimState`（idle/speaking/listening/working/thinking/happy/sad/sleeping/surprised + trans_*）、`FrameStateConfig`、`FrameManifest` |
| `loadManifest.ts` | 按约定生成素材清单；**idle 复用 speaking**（待机=语音） |
| `SpriteFrameProvider.ts` | PNG 多帧素材播放（懒加载，`ready` 标志；`draw` 居中绘制） |
| `ProceduralFrameProvider.ts` | 程序化兜底动画（基于真实照片的呼吸/摆动，按状态参数表） |
| `PetAnimator.ts` | rAF 播放器：状态机、`feedTts`、`setMode`、`playTransition`、prefetch |

**状态解析**：`resolveState` 把 `idle` 统一解析为 `speaking`（帧共用、不重复加载）。

**过渡规则**（`transitionState`）：
- `speaking → working` → `trans_speak_work`
- `working → speaking` → `trans_work_speak`
- `idle → speaking` → `trans_idle_speak` / `speaking → idle` → `trans_speak_idle`（等效 idle↔speaking，因 idle==speaking 不实际触发）
- 过渡素材未就绪 → 直接切目标态（不阻塞）

**渲染优先级**：PNG 素材就绪则画序列帧，否则回落程序化形变。

### 4.2 素材约定（⭐ 容易踩坑）

帧素材位于 `renderer/public/pet-anim/{state}/`，URL 固定为 `/pet-anim/{state}/frame‑NN.png`。

- **文件名使用 U+2011 非断行连字符（`‑`）**，不是普通 `-`！代码里是常量 `NB_HYPHEN = '\u2011'`。
- **帧号 1 基**（`frame‑01.png` 开始），补零 2 位。
- 当前已就绪套（全部 12fps）：

| 状态 | 帧数 | 循环 | 说明 |
|---|---|---|---|
| `speaking` | 24 | ✅ | **同时也是待机状态**（idle 复用） |
| `working` | 49 | ✅ | 工作状态 |
| `trans_speak_work` | 14 | ❌ | 单向过渡 |
| `trans_work_speak` | 12 | ❌ | 单向过渡 |

- 素材源目录：`pictures/`（用户制作区）。**更新帧时把最终帧放到 `renderer/public/pet-anim/{state}/` 即生效**。
- `trans_idle_speak` / `trans_speak_idle` 的素材尚未制作——**不要声称它们存在**，代码已做缺素材回退。

### 4.3 动画联动链

- `ChatPanel.vue`（语音管线）→ `window.api.setPetAnim('speaking'|'idle')`（`pet-anim`）→ 主进程广播 `pet-anim:changed` → `PetWindow.vue` 订阅 → `animator.feedTts(on/off)`。
- `tts-start` / `tts-end`（主进程预留广播）→ 同样驱动 `feedTts`。
- `mode:changed` → `animator.setMode(mode)`：工作模式基底切 `working`，聊天切 `idle`（=speaking）。
- 宠物隐藏（`pet:visible-changed` false）→ 动画 `pause()`；显示 → `resume()`；`visibilitychange` 同理。

### 4.4 底部语音消息条（宠物窗口）

- 位置：`voiceBarTop = PET_CANVAS_SIZE.height - BALL_BOTTOM_PADDING + 30`（猫咪下方 30px）。
- 固定**单行 30px**，`anim-marquee` 横向滚动最新语音文本（`voice-text`/`voice-preview` 广播喂入），空态显示「语音播报中…」。
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

### 5.3 素材命名陷阱

- 帧文件名的连字符是 **U+2011** 而非普通 `-`：复制素材、写脚本、做对比时不要用普通 `-` 去匹配（曾经因此加载失败）。
- 帧序号 **1 基 + 补零 2 位**（`frame‑01`）。

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

- **PowerShell 7（pwsh）优先**，不要用 Windows PowerShell 5.1。
- 前端只允许改 `frontend/` 目录；**禁止改动** `backend/`、`testboard/`、根目录 `docs/`；
  前后端协议/字段/端口不变更。
- 不提交模型文件（`.onnx`/`.wasm`）、`.env`、密钥（已 gitignore）。
- 诊断日志约定（调试时输出格式）：
  - `[win:*]`（logSizes）、`[pet:resize]`/`[pet:move]`、`[pet:force-resize]`、
    `[pet-canvas]`、`[pet]`、`[chat:resize]`/`[chat:move]`、`[chat:*]`。

---

## 8. 待办 / 未来工作（按前端职责）

- 控制面板各设置页 → 后端 Mock 9000（`chat:send` 流式、`history:list/search`、
  `personality:get/set`、`user:get/set`、`voice:settings:get/set`、`mode/auth:policy` 联动）
  —— 详见 `docs/前端开发提示词.md` P0 清单；接入走主进程网关（`main/services/*`），
  渲染进程不直接碰网络。
- `hooks.parseActionTag`：解析 LLM 回复中 `【action:xxx】` → 映射动画动作（wave/happy/sad/
  think/working/sleep/surprised/listen/idle），事件到达通道已就绪，动画映射完善在后续迭代。
- `trans_idle_speak` / `trans_speak_idle` 素材制作后，往 `renderer/public/pet-anim/` 放并同步 `loadManifest`。