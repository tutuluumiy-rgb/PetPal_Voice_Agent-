/**
 * 窗口管理：悬浮宠物窗口 + 独立对话面板窗口 + 独立控制面板窗口
 * --------------------------------------------------------------------------
 * 分层（两个图层，彻底解耦）：
 * - 宠物窗口：220×240，透明 / 无边框 / 置顶 / 不可缩放，可自由拖拽。
 *   窗口尺寸【恒定不变】，画布也从不需要 left/top 补偿（问题1/2的根源消除）。
 * - 对话面板窗口：350×550，透明 / 无边框 / 置顶 / 非模态，独立于宠物窗口，
 *   由宠物窗口位置定位（默认宠物左侧，空间不足切右侧），不影响宠物窗口本身。
 * - 控制面板：800×620，可缩放，非模态。
 */
import { BrowserWindow, screen, shell } from 'electron'
import { join } from 'path'

/** 悬浮宠物窗口尺寸（恒定） */
export const PET_WINDOW_SIZE = { width: 220, height: 280 } as const

/** 独立对话面板窗口尺寸 */
export const CHAT_PANEL_SIZE = { width: 350, height: 550 } as const

/** 控制面板窗口初始尺寸 */
export const PANEL_WINDOW_SIZE = { width: 800, height: 620 } as const

// 宠物在画布内的默认左上角偏移（canvas 220×280，宠物约 199×199 底部居中 → 左缘≈10、顶缘≈33）
const BALL_IN_CANVAS = { left: 10, top: 13 }
/** 宠物精灵默认尺寸（占位；与 pet-canvas.ts 的默认一致） */
const PET_SPRITE_DEFAULT = { width: 128, height: 128 }

let petWindow: BrowserWindow | null = null
let chatWindow: BrowserWindow | null = null
let panelWindow: BrowserWindow | null = null

/** 防止 enforceLockedSize 在 setSize 触发的 resize 事件里重入 */
let _forceResizing = false

/**
 * 强制把窗口尺寸扳回锁定值。
 * 背景：Windows 下透明(transparent)无边框窗口在 setPosition/移动时，Chromium 合成器
 * 会把窗口尺寸重算并逐次累积（491×620…持续膨胀，日志 `[pet:resize]` +1~+2）。
 * setMinimumSize/maximumSize 对透明窗口可能被绕过，故用代码在 resize 事件里强制扳回，
 * 用 setSize 把任何偏离拉回锁定尺寸（对抗系统漂移，根治累积放大）。
 */
function enforceLockedSize(win: BrowserWindow, targetW: number, targetH: number, tag: string): void {
  if (_forceResizing) return
  const [w, h] = win.getSize()
  if (w === targetW && h === targetH) return
  _forceResizing = true
  try {
    // 非 resizable 窗口在 Windows 上 setSize 可能被拒：临时允许再还原
    win.setResizable(true)
    win.setSize(targetW, targetH)
    win.setResizable(false)
    console.log(`[${tag}:force-resize] ${w}x${h} -> ${targetW}x${targetH}`)
  } catch (e) {
    console.log(`[${tag}:force-resize] failed`, e)
  } finally {
    _forceResizing = false
  }
}

/** 加载渲染页面：dev 走 Vite dev server，prod 走 out/renderer 静态文件 */
function loadRenderer(win: BrowserWindow, page: 'pet.html' | 'chat.html' | 'panel.html'): void {
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    win.loadURL(`${devUrl}/${page}`)
  } else {
    win.loadFile(join(__dirname, `../renderer/${page}`))
  }
}

// ---------- 宠物窗口（尺寸恒定，永不扩展，canvas 永不补偿） ----------

/** 创建悬浮宠物主窗口 */
export function createPetWindow(): BrowserWindow {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.focus()
    return petWindow
  }

  petWindow = new BrowserWindow({
    ...PET_WINDOW_SIZE,
    transparent: true,
    frame: false,
    thickFrame: false,
    // ⚠️ 修复：禁止 useContentSize —— 透明无边框窗口在 Windows 上，
    // useContentSize 会在每次 setPosition(拖拽)/show 时反复做 outer↔content 换算
    // 并逐次累积，导致窗口物理尺寸越拖越大（X/Y 同步增长）。去掉后 width/height
    // 直接作为窗口总尺寸，setPosition 不再触发累积换算，尺寸恒为 220×240。
    alwaysOnTop: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    hasShadow: false,
    skipTaskbar: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  petWindow.setAlwaysOnTop(true, 'floating')
  // 硬锁尺寸：min=max=220×240，即使 OS/Electron 想改变也无法超越（治本于累积放大）
  petWindow.setMinimumSize(PET_WINDOW_SIZE.width, PET_WINDOW_SIZE.height)
  petWindow.setMaximumSize(PET_WINDOW_SIZE.width, PET_WINDOW_SIZE.height)

  // 诊断 + 强制扳回：监听宠物窗口 resize，一旦系统把尺寸改大就立刻 setSize 扳回锁定位
  petWindow.on('resize', () => {
    if (_forceResizing) return
    const b = petWindow!.getBounds()
    console.log(`[pet:resize] bounds=(${b.x},${b.y} ${b.width}x${b.height})`)
    enforceLockedSize(petWindow!, PET_WINDOW_SIZE.width, PET_WINDOW_SIZE.height, 'pet')
  })
  petWindow.on('move', () => {
    const b = petWindow!.getBounds()
    console.log(`[pet:move] bounds=(${b.x},${b.y} ${b.width}x${b.height})`)
  })
  // 修正初始尺寸：transparent 窗口启动即可能漂移到 491×620，创建后立即扳回 220×240
  enforceLockedSize(petWindow, PET_WINDOW_SIZE.width, PET_WINDOW_SIZE.height, 'pet-init')

  petWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  petWindow.on('closed', () => {
    petWindow = null
    // 宠物窗口关闭连带关闭对话面板
    closeChatPanel()
  })

  loadRenderer(petWindow, 'pet.html')
  return petWindow
}

/** 读取宠物窗口当前屏幕坐标（未创建返回 undefined） */
export function getPetWindow(): BrowserWindow | null {
  return petWindow && !petWindow.isDestroyed() ? petWindow : null
}

// ---------- 对话面板窗口（独立图层，由宠物窗口位置定位） ----------

/** 宠物精灵在屏幕上的矩形（宠物窗口位置 + 画布内偏移；canvas 恒在 0,0，无需补偿） */
function getPetScreenRect(): { left: number; top: number; width: number; height: number } | null {
  const win = getPetWindow()
  if (!win) return null
  const [px, py] = win.getPosition()
  return {
    left: px + BALL_IN_CANVAS.left,
    top: py + BALL_IN_CANVAS.top,
    width: PET_SPRITE_DEFAULT.width,
    height: PET_SPRITE_DEFAULT.height
  }
}

/** 创建（或聚焦）独立对话面板窗口 */
export function createChatWindow(): BrowserWindow {
  if (chatWindow && !chatWindow.isDestroyed()) {
    return chatWindow
  }
  chatWindow = new BrowserWindow({
    ...CHAT_PANEL_SIZE,
    transparent: true,
    frame: false,
    thickFrame: false,
    // ⚠️ 修复：禁止 useContentSize —— 同上，透明无边框固定的对话面板窗口，
    // useContentSize 会在每次 show/setPosition 时累积放大窗口物理尺寸。
    alwaysOnTop: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    hasShadow: false,
    skipTaskbar: false,
    show: false, // 打开时再按位置显示
    backgroundColor: '#00000000',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })
  chatWindow.setAlwaysOnTop(true, 'floating')
  // 硬锁尺寸：min=max=350×550，杜绝任何累积放大
  chatWindow.setMinimumSize(CHAT_PANEL_SIZE.width, CHAT_PANEL_SIZE.height)
  chatWindow.setMaximumSize(CHAT_PANEL_SIZE.width, CHAT_PANEL_SIZE.height)

  // 诊断 + 强制扳回：监听对话面板窗口 resize，一旦系统改大就 setSize 扳回锁定位
  chatWindow.on('resize', () => {
    if (_forceResizing) return
    const b = chatWindow!.getBounds()
    console.log(`[chat:resize] bounds=(${b.x},${b.y} ${b.width}x${b.height})`)
    enforceLockedSize(chatWindow!, CHAT_PANEL_SIZE.width, CHAT_PANEL_SIZE.height, 'chat')
  })
  chatWindow.on('move', () => {
    const b = chatWindow!.getBounds()
    console.log(`[chat:move] bounds=(${b.x},${b.y} ${b.width}x${b.height})`)
  })
  // 修正初始尺寸
  enforceLockedSize(chatWindow, CHAT_PANEL_SIZE.width, CHAT_PANEL_SIZE.height, 'chat-init')

  chatWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
  chatWindow.on('closed', () => {
    chatWindow = null
  })

  loadRenderer(chatWindow, 'chat.html')
  return chatWindow
}

/**
 * 对话面板定位：四象限对角规则。
 * 以宠物中心相对「工作区中心」所在象限决定面板方向，让面板始终出现在宠物对角侧、远离宠物：
 *   - 宠物在右上(1象限) → 面板放左下角
 *   - 宠物在左上(2象限) → 面板放右下角
 *   - 宠物在左下(3象限) → 面板放右上角
 *   - 宠物在右下(4象限) → 面板放左上角
 * 即：宠物在上半 → 面板放下方；在下半 → 面板放上方；在右半 → 面板放左侧；在左半 → 面板放右侧。
 * 全程只动对话窗口自身，宠物窗口不受影响。
 */
export function positionChatWindow(): void {
  const win = chatWindow
  const pet = getPetScreenRect()
  if (!win || win.isDestroyed() || !pet) return

  const wa = screen.getDisplayMatching(win.getBounds()).workArea
  const { width: cw, height: ch } = CHAT_PANEL_SIZE

  const petRight = pet.left + pet.width
  const petBottom = pet.top + pet.height
  // 宠物中心
  const cx = pet.left + pet.width / 2
  const cy = pet.top + pet.height / 2
  // 工作区中心
  const midX = wa.x + wa.width / 2
  const midY = wa.y + wa.height / 2

  const petAtTop = cy < midY // 宠物在上半（1、2 象限）
  const petAtRight = cx > midX // 宠物在右半（1、4 象限）

  // 复位距离微调：X 向离宠物距离增大 40px，Y 向减小 100px（要求 2）
  // 复位距离：X 向离宠物距离（当前基础上 -20），Y 向 -100（要求 2 微调）
  const GAP_X = 44
  const GAP_Y = -100

  // X 方向：宠物在右半 → 面板放左侧；左半 → 放右侧
  const left = petAtRight ? pet.left - cw - GAP_X : petRight + GAP_X
  // Y 方向：宠物在上半 → 面板放下方；下半 → 放上方（Y 距离减小 100）
  const top = petAtTop ? petBottom + GAP_Y : pet.top - ch - GAP_Y

  // 兜底 clamp 到工作区（保证面板完整显示）
  const cl = Math.max(wa.x, Math.min(Math.round(left), wa.x + wa.width - cw))
  const ct = Math.max(wa.y, Math.min(Math.round(top), wa.y + wa.height - ch))

  win.setPosition(cl, ct)
  logSizes('position')
}

/** 宠物拖拽时若对话面板开着，跟随重定位 */
export function repositionChatAfterPetDrag(): void {
  if (chatWindow && !chatWindow.isDestroyed() && chatWindow.isVisible()) {
    positionChatWindow()
  }
}

/** 对话面板关闭时由渲染进程自身触发（点 × / ESC / 外点） */
export function requestCloseChatPanel(): void {
  closeChatPanel()
}

// ---------- 诊断：打印各窗口真实物理尺寸（排查"拖拽/右键后窗口放大"） ----------
/** 打印当前窗口尺寸，用于 devtools 确认窗口物理尺寸是否在异常变化 */
function logSizes(tag: string): void {
  const parts: string[] = []
  const pet = getPetWindow()
  if (pet) {
    const [w, h] = pet.getSize()
    const [cw, ch] = pet.getContentSize()
    const [px, py] = pet.getPosition()
    const dpr = pet.webContents.getZoomFactor()
    const disp = screen.getDisplayMatching(pet.getBounds())
    parts.push(`pet(pos=${px},${py} w=${w}h=${h} content=${cw}x${ch} dpr=${dpr} dispScale=${disp.scaleFactor})`)
  }
  if (chatWindow && !chatWindow.isDestroyed()) {
    const vis = chatWindow.isVisible()
    const [w, h] = chatWindow.getSize()
    const [cw, ch] = chatWindow.getContentSize()
    const [px, py] = chatWindow.getPosition()
    const disp = screen.getDisplayMatching(chatWindow.getBounds())
    parts.push(`chat(vis=${vis} pos=${px},${py} w=${w}h=${h} content=${cw}x${ch} dispScale=${disp.scaleFactor})`)
  }
  console.log(`[win:${tag}] ${parts.join(' ')}`)
}

export function openChatPanel(): void {
  const win = createChatWindow()
  logSizes('open-pre')
  if (win.isVisible()) {
    positionChatWindow()
    win.focus()
    logSizes('open-already-visible')
    return
  }
  win.showInactive()
  positionChatWindow()
  logSizes('open-shown')
}

/** 关闭对话面板：隐藏（不销毁，便于快速重开）；宠物窗口不受影响 */
export function closeChatPanel(): void {
  if (chatWindow && !chatWindow.isDestroyed()) {
    logSizes('close-pre')
    chatWindow.hide()
    logSizes('close-after')
  }
}

// ---------- 控制面板窗口（现有，不变） ----------

/** 创建（或聚焦）独立控制面板窗口 */
export function createPanelWindow(): BrowserWindow {
  if (panelWindow && !panelWindow.isDestroyed()) {
    panelWindow.focus()
    return panelWindow
  }

  panelWindow = new BrowserWindow({
    ...PANEL_WINDOW_SIZE,
    minWidth: 720,
    minHeight: 540,
    show: false,
    backgroundColor: '#08090a',
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  panelWindow.once('ready-to-show', () => {
    const win = panelWindow
    if (win && !win.isDestroyed()) {
      win.show()
    }
  })
  setTimeout(() => {
    if (panelWindow && !panelWindow.isDestroyed() && !panelWindow.isVisible()) {
      panelWindow.show()
    }
  }, 3000)

  panelWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription) => {
    console.error(`[panel] did-fail-load ${errorCode} ${errorDescription}`)
  })

  panelWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  panelWindow.on('closed', () => {
    panelWindow = null
  })

  loadRenderer(panelWindow, 'panel.html')
  return panelWindow
}

/** 打开控制面板：已存在则聚焦，否则新建 */
export function openPanelWindow(): void {
  createPanelWindow()
}

/** 退出时清理窗口引用 */
export function destroyWindows(): void {
  petWindow?.destroy()
  chatWindow?.destroy()
  panelWindow?.destroy()
  petWindow = null
  chatWindow = null
  panelWindow = null
}
