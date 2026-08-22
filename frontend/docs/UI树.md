# PetPal Voice Agent — 桌面应用 UI 树

> 生成日期：2025-08（与当前代码同步）。三窗口架构：悬浮宠物窗口 + 独立对话面板 + 控制面板。
> 渲染层组件位置：`frontend/renderer/components/`；主进程/Preload 只做状态与桥接，不参与 UI 树。

## 1. 窗口级结构

```
Electron 应用（frontend/main/windows.ts）
├── 悬浮宠物窗口 PetWindow（220×280，透明·无边框·置顶·恒定尺寸）
│     └── 页面：pet.html → PetWindow.vue
├── 对话面板窗口 ChatPanel（350×550，透明·无边框·置顶·恒定尺寸）
│     └── 页面：chat.html → ChatPanel.vue
└── 控制面板窗口 PanelWindow（800×620，可缩放）
      └── 页面：panel.html → PanelShell.vue
```

## 2. 悬浮宠物窗口（PetWindow.vue）

```
PetWindow
├── canvas 宠物动画画布（fixed 220×280；点击拖拽 / 右键→打开对话面板）
│     ├── PetAnimator（rAF 播放器：四态动画 闲聊=speaking循环 / 工作=working循环 / 两个过渡）
│     ├── SpriteFrameProvider（/pet-anim/{state}/frame‑NN.png 懒加载，双连字符兼容）
│     └── ProceduralFrameProvider（素材缺失时的程序化呼吸/摆动兜底）
└── 底部语音播报消息条（独立图层，30px，点击 → 打开对话面板）
      ├── 语音状态指示灯（圆点：待机/超时断开=橙 · 聆听/回复=绿 · 未启用=灰）
      └── 播报文本（超宽才滚动，滚动时长=字数/4 秒 匹配播报速度）
```

## 3. 对话面板（ChatPanel.vue <-> ChatInputBar.vue）

```
ChatPanel（token 化双皮肤：data-skin='dark'|'light'）
├── 头部 header（可拖拽区，40px）
│     ├── 标题「西西对话」
│     ├── 后端连接状态点（9000 网关：绿=已连 / 黄=连接中 / 红=未连）
│     ├── 语音开关按钮（空闲=「闲聊模式 · 说「你好西西」唤醒」/ 聆听 / 回复中）
│     ├── ＋ 新建会话按钮（清空消息 + 重连语音后端）
│     └── × 关闭面板按钮
├── 消息区（滚动，13px）
│     ├── 你：xxx（用户消息）
│     └── 西西：xxx（回复气泡，整段聚合为一条）
├── 输入区 ChatInputBar
│     ├── textarea（100→220px 自适应，Enter 发送）
│     ├── 发送/中止按钮（运行中=灰方块中止）
│     └── 底部按钮栏（30px）：
│           [设置⚙  → 控制面板] [文件＋] [模式] [权限] [皮肤] [隐藏宠物] [退出应用]
│             └─ 模式/权限/皮肤 均为「图标按钮 + 上方弹出小卡片」：
│                  · 模式卡：聊天模式 / 工作模式
│                  · 权限卡：完全批准 / 请求批准
│                  · 皮肤卡：深色 / 浅色
└── （隐藏）浮动弹卡：mode/auth/skin 三张 fixed 卡片（z-10000，按钮上方定位防裁剪）
```

## 4. 控制面板（PanelShell.vue）

```
PanelShell
├── 侧边栏 SidebarNav（w-52）
│     ├── 头部：Logo（assets/logo.png）+ 「PetPal」
│     └── 导航项（5 个）：
│           ◷ 历史记录  /  ♪ 语音参数设置  /  ✦ 宠物动画生成  /  ◈ 宠物人设配置  /  ◉ 用户档案
│           （「关于」已移入登录菜单，不再出现在导航列表）
│     └── 底部「登录」按钮 → 展开菜单（点击外部关闭）：
│           登录账户 / 帮助 / 退出应用 / 关于
├── 主内容区（KeepAlive 缓存各视图，右下方固定操作栏）
│     └── 视图×6：
│           ◷ HistoryView        历史记录——搜索框 + session 抽屉列表 + 事件轨迹时间线
│                                （每会话=日期+首句摘要+轮数/条数；展开事件：标题行可折叠、
│                                  内容在下方、按「第N轮」分段；分页上/下一页）
│           ♪ VoiceSettingsView  语音参数设置——音量滑条 / 音调滑条 / 音色下拉（真实作用于 TTS）
│           ✦ AnimationGenView   宠物动画生成（占位）+ 宠物显示开关
│           ◈ PersonalityView    宠物人设配置——markdown 文本域（真实读写 personality.md）
│           ◉ UserProfileView    用户档案——结构化表单：称呼/角色/偏好回复风格/喜好/不喜欢/作息
│           ⓘ AboutView          关于——Logo + 版本号 + 实际功能描述（经登录菜单进入）
├── 右下角操作栏（仅编辑类页面出现）：
│     [撤销(次钮)] [保存(主钮)] —— 50×27、圆角6、右30/底25、并排
└── 左下占位提示 toast（登录/帮助「即将上线」）
```

## 5. 状态与联动速查（UI 相关）

| 状态 | 来源 | 广播到 |
|---|---|---|
| 模式 chat/work | 主进程 state.ts（按钮/语音切换均走它） | `mode:changed` → 面板单选卡 + 宠物动画基底 |
| 皮肤 dark/light | 主进程 state.ts（userData/skin.json 持久化） | `skin:changed` → 三窗口 html[data-skin] token |
| 语音界面状态 off/idle/listening/speaking | ChatPanel（VoicePipeline.onState） | `voice-state` → 宠物消息条指示灯圆点 |
| 宠物可见性 | 主进程 state.ts | `pet:visible-changed` → 画布显示/隐藏 + 按钮开关态 |
| 网关后端状态 connecting/connected/disconnected | backendGateway（默认 8001 /ws/mgmt，失败回退 9000） | `backend:status` → 面板头部状态点 |
| 动画说话态 speaking/idle | ChatPanel TTS 事件 | `pet-anim` → 主进程 → `pet-anim:changed` → PetAnimator |

## 6. 文件地图（UI 相关）

```
frontend/renderer/
├─ main-pet.ts / main-chat.ts / main-panel.ts     三窗口入口
├─ pet.html / chat.html / panel.html
├─ components/
│  ├─ pet-window/
│  │  ├─ PetWindow.vue · pet-canvas.ts · ChatInputBar.vue · hooks.ts
│  │  └─ anim/  PetAnimator.ts · SpriteFrameProvider.ts · ProceduralFrameProvider.ts · loadManifest.ts · types.ts
│  ├─ chat-panel/ChatPanel.vue
│  └─ control-panel/
│     ├─ PanelShell.vue · SidebarNav.vue · PageCard.vue · LogoImg.vue
│     └─ views/ HistoryView · VoiceSettingsView · AnimationGenView · PersonalityView · UserProfileView · AboutView
├─ app/
│  ├─ voice/VoicePipeline.ts
│  └─ panelActions.ts（右下角操作栏注册）
├─ styles/ design-tokens.css（双皮肤 token）· index.css
└─ public/ logo.png · pet-anim/{speaking,working,trans_speak_work,trans_work_speak}/ · vad/ · placeholders/
```

> 约定：界面文案与宠物名统一为「西西」；主进程 console 日志用英文；组件颜色一律走 token 类。