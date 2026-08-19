/**
 * 窗口管理：悬浮宠物主窗口 + 独立控制面板窗口
 * --------------------------------------------------------------------------
 * - 宠物窗口：220×240，透明 / 无边框 / 置顶 / 不可缩放，可自由拖拽；
 * - 控制面板：800×620，可缩放，非模态，可与宠物窗口同时存在；
 * - 两个窗口使用独立 HTML 入口（renderer/pet.html / renderer/panel.html）。
 */
import { BrowserWindow, screen, shell } from 'electron'
import { join } from 'path'

/** 悬浮宠物窗口初始尺寸 */
export const PET_WINDOW_SIZE = { width: 220, height: 240 } as const

/** 上下文对话面板固定尺寸（宽 350 × 高 550） */
export const PANEL_SIZE = { width: 350, height: 550 } as const

/**
 * 面板打开时窗口扩展尺寸（宠物 canvas 220×240 + 8px 间距 + 面板 350×550）：
 * - 宽 = 220 + 8 + 350 = 578（面板固定宠物左侧并排）
 * - 高 = 面板 550 + 上下安全边距 8+8 = 566（面板垂直居中对齐宠物中心）
 * 宠物在窗口内的位置由渲染进程上报的「宠物在画布内偏移」决定（精灵尺寸
 * 变化时自适应），窗口位置随之定位，保证宠物屏幕位置恒定（宠物不动）
 */
const PET_PANEL_WINDOW = { width: 578, height: 566 } as const
/** 宠物在 canvas 内的位置兜底（canvas 220×240，宠物底部居中）：左缘 46、顶缘 104 */
const BALL_IN_CANVAS = { left: 46, top: 104 }

/** 控制面板窗口初始尺寸 */
export const PANEL_WINDOW_SIZE = { width: 800, height: 620 } as const

let petWindow: BrowserWindow | null = null
let panelWindow: BrowserWindow | null = null
/** 面板打开前的宠物窗口位置（关闭时恢复，保证球体屏幕位置不变） */
let petWindowPrevPos: { x: number; y: number } | null = null

/** 面板固定宠物左侧：窗口左移「面板宽 350 + 间距 8 = 358」露出面板空间 */
const PANEL_LEFT_SHIFT = 358

/**
 * 面板打开状态下拖拽移动窗口时，更新恢复锚点为"拖拽后宠物所对应的窗口位置"。
 * 面板打开时窗口比关闭时左移 358px（宠物在窗口右侧、面板在左）：
 * 关闭面板（canvas 归位后宠物回到窗口左侧 46）时若要宠物停在拖拽后的位置，
 * 恢复锚点 x 需加回这 358px 偏移（y 不变）。
 */
export function updatePetWindowPrevPos(x: number, y: number): void {
  if (petWindowPrevPos) {
    petWindowPrevPos = { x: x + PANEL_LEFT_SHIFT, y }
  }
}

/** 加载渲染页面：dev 走 Vite dev server，prod 走 out/renderer 静态文件 */
function loadRenderer(win: BrowserWindow, page: 'pet.html' | 'panel.html'): void {
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    win.loadURL(`${devUrl}/${page}`)
  } else {
    win.loadFile(join(__dirname, `../renderer/${page}`))
  }
}

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
    useContentSize: true,
    alwaysOnTop: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    hasShadow: false,
    skipTaskbar: false,
    backgroundColor: '#00000000',
    // 禁止窗口内容被系统拖拽选中（配合渲染层 user-select:none）
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  // 防止置顶窗口被任务栏遮挡
  petWindow.setAlwaysOnTop(true, 'floating')
  // TODO: 后续迭代实现 — 加载用户保存的宠物窗口位置（bounds 持久化）

  // 外链一律走系统浏览器，不在应用内打开
  petWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  petWindow.on('closed', () => {
    petWindow = null
    petWindowPrevPos = null
  })

  loadRenderer(petWindow, 'pet.html')
  return petWindow
}

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
    // 暗黑主题
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  // 就绪后再显示，避免白屏闪烁；ready-to-show 兜底 3s 强制显示，防黑屏
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

  // 加载失败打日志，避免静默黑屏
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

/**
 * 等待宠物窗口一次 resize / move 事件真正触发（窗口物理布局完成）后再返回。
 * - 必须在调用 setSize/setPosition 之前注册监听，避免漏掉同步/紧邻事件；
 * - 首次事件即视为到位（setSize/setPosition 为瞬时操作，无动画），随后
 *   用 getBounds() 读取真实最终 bounds；
 * - 兜底超时：极端情况下事件未触发时最迟 120ms 返回，避免 handler 挂死。
 */
function waitForWindowSettle(win: BrowserWindow): Promise<void> {
  return new Promise((resolve) => {
    let done = false
    const finish = (): void => {
      if (done) return
      done = true
      cleanup()
      resolve()
    }
    const cleanup = (): void => {
      win.removeListener('resize', finish)
      win.removeListener('move', finish)
      clearTimeout(timer)
    }
    win.on('resize', finish)
    win.on('move', finish)
    const timer = setTimeout(finish, 120)
  })
}

/**
 * 上下文对话面板显示时调整宠物窗口（canvas 220×240 固定不动）
 * - panelHeight > 0：窗口扩展为 578×566（面板固定宠物左侧并排，垂直居中宠物中心）
 * - panelHeight = 0：恢复初始 220×240 及面板打开前窗口位置
 * - 宠物屏幕位置不变：窗口位置 = 宠物屏幕坐标 - 宠物画布内偏移
 *   （宠物偏移由渲染进程上报，精灵尺寸变化时自适应；宠物始终钉在屏幕同一坐标，
 *   面板是独立体系，游离于宠物图片范围之外）
 * - 始终保持窗口完整位于屏幕工作区内
 * - async：setSize/setPosition 后 await 一次 resize/move 事件，等窗口物理布局
 *   真正完成，再把真实 getBounds() 返回给渲染进程（消除"窗口已移动、canvas 未
 *   补偿"的中间态跳动）。右键弹出 / 关闭上下文菜单与普通 panel:height 均走此路径。
 */
export async function resizePetWindowForPanel(
  panelHeight: number,
  ballScreen?: { x: number; y: number },
  ballInCanvas?: { x: number; y: number }
): Promise<{ x: number; y: number } | undefined> {
  if (!petWindow || petWindow.isDestroyed()) return undefined
  const win = petWindow
  const open = panelHeight > 0
  const targetW = open ? PET_PANEL_WINDOW.width : PET_WINDOW_SIZE.width
  const targetH = open ? PET_PANEL_WINDOW.height : PET_WINDOW_SIZE.height

  const [curX, curY] = win.getPosition()
  const [curW, curH] = win.getSize()
  if (targetH === curH && targetW === curW && !open) {
    return { x: curX, y: curY }
  }

  // 记录面板打开前的窗口位置（关闭时恢复，球体屏幕位置不变）
  if (open && !petWindowPrevPos) {
    petWindowPrevPos = { x: curX, y: curY }
  }

  // 球体在画布内的左上角坐标（精灵尺寸变化时由渲染进程上报，默认 46,104）
  const ballInX = ballInCanvas?.x ?? BALL_IN_CANVAS.left
  const ballInY = ballInCanvas?.y ?? BALL_IN_CANVAS.top

  // 球体屏幕坐标：优先用渲染进程上报（保持球体不动），否则用当前窗口推导
  const ballScreenX = ballScreen?.x ?? curX + ballInX
  const ballScreenY = ballScreen?.y ?? curY + ballInY

  // 面板固定宠物左侧：窗口左移「面板宽 350 + 间距 8」露出面板空间
  // （宠物在窗口内右移，屏幕位置不变；面板窗口内 46~396，宠物 404~568，不重叠）

  // 窗口位置：打开 = 球体屏幕 - 球体画布内偏移 - 面板左移量；关闭 = 恢复打开前位置
  let newX: number
  let newY: number
  if (open) {
    newX = ballScreenX - ballInX - PANEL_LEFT_SHIFT
    newY = ballScreenY - ballInY
  } else {
    newX = petWindowPrevPos?.x ?? curX
    newY = petWindowPrevPos?.y ?? curY
    petWindowPrevPos = null
  }

  const wa = screen.getDisplayMatching(win.getBounds()).workArea
  // 边缘兜底：窗口保持在屏幕工作区内
  if (newX < wa.x) {
    newX = wa.x
  }
  if (newX + targetW > wa.x + wa.width) {
    newX = wa.x + wa.width - targetW
  }
  if (newY < wa.y) {
    newY = wa.y
  }
  if (newY + targetH > wa.y + wa.height) {
    newY = wa.y + wa.height - targetH
  }

  // 日志：setBounds 调用时刻（目标值）
  const targetX = Math.round(newX)
  const targetY = Math.round(newY)
  console.log(
    `[petWin] setBounds call t=${Date.now()} open=${open} target=(${targetX},${targetY} ${targetW}x${targetH})`
  )

  // 预先注册 resize/move 监听（setSize/setPosition 前，避免漏事件）
  const settle = waitForWindowSettle(win)

  // 非 resizable 窗口在 Windows 上缩小尺寸可能被系统拒绝：临时允许缩放
  win.setResizable(true)
  win.setSize(targetW, targetH)
  win.setPosition(targetX, targetY)
  win.setResizable(false)

  // 等窗口物理布局完成（resize/move 事件触发）
  await settle

  // 读取真实最终 bounds 并返回
  const [rx, ry] = win.getPosition()
  console.log(`[petWin] settle t=${Date.now()} actual=(${rx},${ry}) ✔`)
  return { x: Math.round(rx), y: Math.round(ry) }
}

/** 退出时清理窗口引用 */
export function destroyWindows(): void {
  petWindow?.destroy()
  panelWindow?.destroy()
  petWindow = null
  panelWindow = null
}
