# PetPal — 桌面端 AI 语音虚拟形象客户端

Electron-Vite + Vue3 + TypeScript + TailwindCSS 构建的可运行底座。
悬浮宠物主窗口 + 独立控制面板，Linear 风格暗黑设计系统，preload 脚本做 IPC 桥接。

> 当前为**可运行底座**：窗口、拖拽、上下文对话面板、控制面板、IPC 骨架已完成；
> 语音链路（ASR/LLM/TTS）、精灵动画、文件读写均为预留钩子（`// TODO: 后续迭代实现`）。
> 关于页展示 **PetPal Voice Agent**。

## 一、初始化与运行

```bash
cd desktop
npm install          # 依赖安装（npmmirror + electron 镜像已在 .npmrc 配置）

npm run dev          # 开发模式：Vite HMR + Electron 自动启动
npm run build        # 生产构建 → out/{main,preload,renderer}
npm run start        # 预览构建产物（需先 build）
npm run typecheck    # tsc(main/preload) + vue-tsc(renderer) 类型检查
```

环境要求：Node ≥ 22.12（开发机 v24.14.1），Windows 10+。

## 二、目录结构

```
desktop/
├─ main/                     # electron 主进程（业务状态与系统能力都在主进程）
│  ├─ index.ts               # 入口：单实例锁、app 生命周期、IPC 注册
│  ├─ windows.ts             # 宠物窗口(220×240 透明置顶) + 控制面板(800×620) + 卡片窗口联动
│  ├─ drag.ts                # Windows 无边框窗口拖拽节流（防漂移）
│  ├─ state.ts               # 全局状态 currentMode:'chat'|'work' + authPolicy + 模式订阅广播
│  └─ ipc.ts                 # 全部 ipcMain 注册 + TTS 事件发送 helper
├─ preload/                  # preload 脚本，IPC 类型定义
│  ├─ types.ts               # IPC 通道常量 + PetMode/AuthPolicy/DragPoint/AppApi 类型（三方共享）
│  ├─ index.ts               # contextBridge 暴露 window.api
│  └─ index.d.ts             # Window.api 全局类型声明
└─ renderer/                 # vue3 渲染进程
   ├─ pet.html / panel.html  # 双窗口各自 HTML 入口
   ├─ main-pet.ts / main-panel.ts
   ├─ components/
   │  ├─ pet-window/         # 悬浮宠物相关组件
   │  │  ├─ PetWindow.vue    # 仅 canvas 画布（透明、固定 220×240）、右键 toggle 面板、左键拖拽
   │  │  ├─ pet-canvas.ts    # 画布渲染 + PetFrameSource 素材替换接口（当前程序化球体）
   │  │  ├─ ContextCard.vue  # 上下文对话面板（350×550、球体左上角、弹出卡片、隐藏宠物）
   │  │  ├─ ChatInputBar.vue # 输入区域（textarea 自适应 120→220 + 固定 30px 按钮栏）
   │  │  └─ hooks.ts         # onTtsStart/onTtsEnd/parseActionTag 预留钩子
   │  └─ control-panel/      # 控制面板页面组件
   │     ├─ PanelShell.vue / SidebarNav.vue / PageCard.vue
   │     └─ views/           # History/VoiceSettings/AnimationGen/Personality/UserProfile/About
   ├─ styles/
   │  ├─ design-tokens.css   # 整套 Linear 暗黑设计系统 token（唯一事实源）
   │  └─ index.css           # tailwind 指令 + 全局基础层（body/canvas 透明）
   └─ assets/pet-placeholder.png  # 静态宠物占位图（canvas 程序化球体已替代显示，可删除）
```

## 三、设计系统

全部颜色/圆角/阴影/动效/字体均定义于 `renderer/styles/design-tokens.css`，
`tailwind.config.ts` 只做 token → 原子类 的桥接，**组件内禁止自定义样式**。

| 分类 | Token | 说明 |
|---|---|---|
| 背景层级 | `--ds-bg-0/1/2/3` | 暗黑四层：8,9,10 → 23,24,26 |
| 文字 | `--ds-fg-primary/secondary/muted` | 244,245,246 / 148,153,161 / 110,115,124 |
| 强调色 | `--ds-accent(-hover)` | Linear 靛蓝 #5E6AD2 / #6F7BE0 |
| 状态色 | `--ds-success/danger/warning` | 语义色 |
| 描边 | `--ds-line-subtle/strong` | white 6% / 12% hairline |
| 圆角 | `--ds-radius-sm/md/lg/2xl` | 6 / 8 / 12 / 16px |
| 阴影 | `--ds-shadow-sm/md/lg/hover/active` | 多层阴影，禁止单层 |
| 动效 | `--ds-duration-sm/md/lg`、`--ds-ease-expo-out` | 200/250/300ms，cubic-bezier(0.16,1,0.3,1) |
| 纹理 | `--ds-noise`、`--ds-layered-bg` | SVG feTurbulence noise + 径向渐变分层背景 |

常用类：`bg-surface-3/60`、`text-fg-secondary`、`border-line-subtle`、`rounded-ds-2xl`、
`shadow-ds-lg`、`ease-expo-out`、`duration-ds-sm`、`animate-scale-in`、`ds-glass`。
`prefers-reduced-motion: reduce` 全局降级关闭动画。

## 四、IPC 通道契约

| 通道 | 方向 | Payload | 说明 |
|---|---|---|---|
| `mode:get` | R→M (invoke) | — → `PetMode` | 读取当前模式 |
| `mode-switch` | R→M (send) | `PetMode` | 切换闲聊/工作模式（主进程 `state.ts` 更新） |
| `mode:changed` | M→R (send) | `PetMode` | 模式变化广播（ASR 语音切换指令等 → UI 自动同步） |
| `auth-policy:get` | R→M (invoke) | — → `AuthPolicy` | 读取权限策略 |
| `auth-policy:set` | R→M (send) | `AuthPolicy` | 更新权限策略（完全批准/请求批准） |
| `panel:open` | R→M (send) | — | 打开/聚焦控制面板 |
| `pet:drag-start/move/end` | R→M (send) | `DragPoint` | 宠物窗口拖拽（渲染进程 rAF 帧合并） |
| `panel:height` | R→M (invoke) | `number` | 面板所需窗口尺寸（0=恢复 220×240） |
| `pet:set-visible` | R→M (send) | `boolean` | 隐藏/显示球体宠物 |
| `pet:get-visible` | R→M (invoke) | — → `boolean` | 读取宠物可见性（控制面板用） |
| `pet:visible-changed` | M→R (send) | `boolean` | 可见性广播（同步球体显示/隐藏） |
| `app:version` | R→M (invoke) | — → `string` | 应用版本号 |
| `tts-start` / `tts-end` | M→R (send) | — | TTS 播放开始/结束通知（预留） |

渲染进程统一通过 `window.api.*` 调用（preload 注入，类型完整）。

## 五、已实现行为

- **透明画布（行为类似 PNG）**：宠物窗口仅渲染宠物图形（真实照片），图形以外
  全部透明、无黑色背景（`body`/`canvas` 背景均为 transparent，非图形像素 alpha=0；
  控制面板暗色背景由 PanelShell 的 ds-layered-bg 提供，互不影响）
- **DOM 极简**：宠物窗口仅保留 canvas 画布元素（无外层包装 div）；
  canvas 固定初始化 220×240，任何点击 / 面板开合逻辑禁止修改其宽高
- **真实宠物照片**：`renderer/assets/pet-photo.png`（用户提供的透明背景 PNG，
  1254×1254）经 `photoFrameSource` 缩放适配画布（保留比例、底部居中）绘制；
  `PetFrameSource` 帧源接口预留——后续可替换为 PNG 序列帧 / 视频帧绘制，
  画布透明属性与 DOM 盒子保持不变；精灵尺寸动态计算，面板定位锚点自适应
- 悬浮宠物窗口：220×240、透明、无边框、置顶（`floating` 层级防任务栏遮挡）、可自由拖拽
- **Windows 拖拽适配（rAF 帧合并，防闪烁）**：渲染进程 mousemove 全部接收但只保存
  目标坐标，`requestAnimationFrame` 每帧合并一次发送 dragMove（同一帧多次移动只渲染
  一次）；主进程直接 `setPosition(Math.round(...))` 整数坐标（无亚像素抖动、无
  setInterval 节流）；mouseup 丢失时以 blur/鼠标出屏兜底；**单击与拖拽区分**
  （位移超 5px 才进入拖拽）；**拖拽期间无其他逻辑争夺球体位置
- **拖拽限制在屏幕内**：拖拽目标位置 clamp 到屏幕工作区，宠物/窗口不会被拖出屏幕外
- **面板打开时拖拽不瞬移**：面板打开状态下拖拽窗口时，同时更新"恢复锚点"
  （含面板左侧 358px 偏移），关闭面板后宠物停留在拖拽后位置（不会瞬移回面板打开前）
- **球体屏幕位置固定**：面板打开/关闭时，主进程 async 等待窗口真正到位后返回实际
  位置，渲染进程按预测位置提前补偿 canvas（窗口到位即宠物位置正确）→ 打开/
  关闭宠物屏幕坐标恒定、无闪烁；关闭恢复窗口原位置
- **窗口尺寸稳定**：`thickFrame:false + useContentSize:true`，消除 Windows 无边框
  透明窗口的隐藏边框导致的初始尺寸漂移（实测连续启动均为精确 220×240）
- **上下文对话面板（350×550 固定，宠物与面板两套独立体系）**：
  - **鼠标右键点击画布**唤起（`event.preventDefault()` 阻止原生右键菜单）；
    打开状态下再次右键 = 关闭（toggle，**不重复叠加渲染**）；**左键点击不再唤起**（留给语音交互）
  - 面板**挂载为 document.body 直接子节点**（Teleport），`position:absolute + z-index:9999`，
    定位基准为浏览器视口，**完全不受 canvas 画布约束**
  - **宠物绝对不动（两套体系）**：面板打开 / 关闭 / 快速 toggle，宠物屏幕坐标
    恒定不变（窗口扩展时 canvas 补偿 + 关闭时恢复，宠物钉在屏幕同一坐标；
    拖拽后宠物保持在拖拽位置，不会"回弹"到任何中心点）
  - **面板固定宠物左侧**：`left = 宠物左缘 - 350 - 8`、`top = 垂直居中对齐宠物中心`；
    **游离于宠物图片范围之外**（面板右缘 ≤ 宠物左缘 - 间距，不重叠）；
    左侧屏幕空间不足时切换到宠物右侧（仍不重叠）
  - **布局（350×550 固定，子元素禁止超出面板边界）**：
    - 头部 40px：标题 + 最右侧【关闭面板】按钮（仅关闭本面板，不退出程序）
    - 消息区：对话历史（`你：xxx` / `球球：xxx`），滚动容器，与输入框共享剩余
      可用空间（输入框扩张时自动压缩并保持滚动），字体 13px
    - 输入区：textarea 初始 100px → 内容变多向上自动放大 → 最大 220px（超出内部
      滚动）；左右两侧留白 3px、用阴影做层级区分（整体卡片底色统一白色，
      **无分割线、无背景色块**）；**输入框右下角发送箭头 icon**（其中：
      Enter 键发送；模型运行中 icon 变为灰色小方块并支持中止生成/TTS）；
      底部按钮栏固定 30px 贴面板底，不跟随输入滚动
  - **底部按钮栏**（30px，图标统一 18px、内边距缩小不换行）：左侧依次为设置
    （**齿轮线条风格细线图标**→独立控制面板）、文件（+）、模式切换（弹出
    250×100 小卡片：聊天/工作）、权限（弹出 250×100 小卡片：完全批准/请求批准）；
    最右侧【隐藏宠物】（隐藏球体画布，上下文面板保持显示，只能从控制面板
    「重新显示宠物」开启）
  - 模式/权限弹出小卡片浮在面板上层（z-index 10000），不受面板裁剪限制，
    点击空白关闭，卡片内字体 13px
  - **窗口尺寸联动（防裁切）**：面板 350×550 需要大窗口，打开时经 IPC `panel:height`
    窗口扩展为 578×566（面板固定宠物左侧并排，垂直居中宠物中心；窗口左移
    358px 露出面板空间），关闭恢复 220×240 及原窗口位置
  - **防闪动（打开/关闭无闪烁，右键与普通 `panel:height` 同一条路径）**：
    主进程 `panel:height` handler 为 async，窗口 setSize/setPosition 前注册
    `resize`/`move` 一次性监听（`waitForWindowSettle`，120ms 兜底），等到窗口
    物理布局真正完成（事件驱动，非盲 sleep）后才读取 `getPosition()` 返回真实
    位置；渲染进程在发起窗口扩展**之前**先按预测位置（`win=球体屏幕-画布内偏移-358`
    ）补偿 canvas（不等待真实 bounds）→ 待主进程返回真实位置后再校正（clamp
    差异）。全程宠物屏幕坐标恒定、无中间态错位闪动；实测 predict→correct 的
    canvas 校正 ≤1px（采样噪声），打开/关闭宠物屏幕位置无 ≥1px 跳变。
    **日志**：主进程 `[petWin] setBounds call t=… target=…` / `settle t=… actual=… ✔`；
    渲染进程 `[pet] predict win=… canvas=…` / `correct win=… canvas=…`（用于观测跳变）
  - **关闭无闪现**：关闭面板仅 `v-show` 隐藏（display:none），**不修改 top/left、
    不移动 DOM、无中间过渡状态**；打开时先隐藏（visibility:hidden）→ 定位完成 →
    再可见（不渲染旧坐标帧）
  - 白底 #ffffff / 圆角 12px / 阴影 `0 4px 16px rgba(0,0,0,.15)`
- **单选状态双向同步**：UI 修改 → IPC 写回主进程全局变量（模式 ↔ `currentMode`、
  权限 ↔ `authPolicy`）；主进程侧模式变化（ASR 语音命中"切换到工作模式"等，
  调 `setMode`）→ `mode:changed` 广播 → 模式弹出卡片选中态自动刷新
- **宠物可见性**：隐藏宠物（`pet:set-visible`）→ 主进程记录 + 广播 → 球体画布
  隐藏；控制面板「宠物显示」页可重新开启（`pet:get-visible` / `pet:set-visible`）
- 控制面板：800×620 可缩放、非模态、与宠物窗口共存；侧边栏 6 页导航 +
  KeepAlive 主内容区；分层背景（径向渐变 + noise）；页面入场 scale-in 动画；
  **黑屏加固**：ready-to-show 显示 + 3s 兜底强制显示 + did-fail-load 日志
- 单实例锁：二次启动聚焦已有宠物窗口

## 六、扩展点位（后续迭代接入说明）

### 1. 语音链路（ASR → LLM → TTS）
- 主进程 `main/ipc.ts` 已提供 `sendTtsStart(win)` / `sendTtsEnd(win)` helper：
  WebSocket / 语音服务就绪后，在 TTS 开始/结束时调用即可推送到渲染进程
- 渲染进程 `PetWindow.vue` 已订阅 `window.api.onTtsStart/onTtsEnd`，
  回调指向 `components/pet-window/hooks.ts` 的预留钩子 `onTtsStart()/onTtsEnd()`
- 待办：主进程新建语音服务模块（`// TODO: 后续迭代实现` 标注处）、
  音频能量 `AnalyserNode` → 未来 `pet.setEnergy` 驱动嘴型

### 2. LLM 动作标签
- `hooks.ts` 的 `parseActionTag(rawText)` 空函数：解析 `【action:xxx】` 并映射到动作，
  待接入动作映射表

### 3. 精灵动画引擎
- `PetWindow.vue` 已预留 `animState` 状态变量与 `// TODO` 注释位
- 接入路径：`electron.vite.config.ts` 中 `@pet-avatar` alias 已注释预留，
  指向根目录 `../src/index.ts`（pet-avatar 形象驱动库）；petpack→PetAssets 导入器
  可复用根目录调研结论（desktop-pet-maker 生成链 → 多帧状态动画）
- 占位图 `renderer/assets/pet-placeholder.png` 由真实素材替换

### 4. 文件读写（personality.md / user.md）
- `PersonalityView.vue` / `UserProfileView.vue` 的保存按钮为 TODO；
  建议新增 IPC（如 `profile:read` / `profile:write`），文件读写放主进程 fs 模块

### 5. 权限系统
- 悬浮卡片「权限」抽屉的选中态已同步全局 `authPolicy`（主进程 `state.ts`），
  业务落点 TODO（策略落盘 / 实际生效逻辑）

### 6. 窗口增强
- 宠物窗口位置持久化：`windows.ts` 中 bounds 持久化 TODO 位
- 透明区域点击穿透：未来可用 `win.setIgnoreMouseEvents(true, { forward: true })`

## 七、手动验收清单

- [ ] **画布透明**：窗口只显示宠物照片图形，无黑色背景；非图形区域完全透明
- [ ] **照片显示**：canvas 绘制真实宠物照片（缩放适配、底部居中、透明背景）
- [ ] **画布固定**：canvas 恒为 220×240，面板开合 / 拖拽不改变其尺寸
- [ ] 拖拽宠物窗口跟手无闪烁（rAF 帧合并）；单击与拖拽不互相误触发
- [ ] **宠物绝对不动（两套体系）**：面板打开 / 关闭 / 快速 toggle，宠物屏幕坐标不变；
  拖拽后宠物保持在拖拽位置，不会回弹到任何中心点
- [ ] **面板固定宠物左侧**：面板在宠物左边（游离于宠物图片之外、不重叠）；
  左侧空间不足时切右侧
- [ ] **左键点击画布不弹面板**（留给语音交互）；**右键画布弹面板**，无原生右键菜单
- [ ] 右键 toggle：面板开着时再右键 → 关闭；再右键 → 打开，面板始终唯一不叠加
- [ ] 面板**固定 350×550**、白底圆角阴影
- [ ] 头部 40px：标题 + 最右侧【关闭面板】按钮（只关面板不退程序）
- [ ] 消息区：`你：xxx / 球球：xxx` 对话历史滚动显示，13px
- [ ] **无分割线**；输入区左右留白 3px、阴影层级区分（卡片底色统一白色）
- [ ] textarea 初始 100px，输入变多向上放大至 220px（超出内部滚动），消息区自动缩小
- [ ] 底部按钮栏固定 30px 贴面板底不随输入滚动，图标 18px 不换行（齿轮为线条风格）
- [ ] 设置（齿轮）→ 控制面板；模式/权限按钮 → 250×100 弹出小卡片（点空白关闭）
- [ ] **隐藏宠物**：球体消失、面板保持显示；控制面板「宠物显示」可重新开启
- [ ] 面板打开时窗口扩展 578×566（canvas 不动），关闭恢复 220×240、宠物位置不变
- [ ] **关闭无闪现**：× / 外点 / ESC 关闭时面板立即隐藏（无旧坐标帧闪烁）
- [ ] 切换模式/权限后主进程 `currentMode`/`authPolicy` 同步；主进程侧改模式（模拟
  ASR）→ 模式卡片选中态自动刷新
- [ ] 控制面板 6 页切换正常，滑块/下拉/文本域占位可交互
- [ ] 系统开启「减少动态效果」时动画降级关闭
