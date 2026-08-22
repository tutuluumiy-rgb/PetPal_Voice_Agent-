/**
 * 主进程入口 — PetPal Voice Agent
 * --------------------------------------------------------------------------
 * 主进程持有 WebSocket / 语音服务 / 全局状态机（均为 TODO 预留位）；
 * 渲染进程只负责 UI 渲染，业务逻辑不上渲染进程。
 */
import { app, BrowserWindow, session } from 'electron'
import { registerIpcHandlers, loadSkinPref } from './ipc'
import { createPetWindow, destroyWindows, createChatWindow } from './windows'
import { setupKws } from './kws'
import { backendGateway } from './services/backendGateway'

// ---------- 媒体权限：允许 renderer 使用麦克风（语音采集 getUserMedia 必需） ----------
// Electron 默认对 getUserMedia 的 'media' 权限会弹系统询问；自动启动（无用户手势）时
// 必须有允许策略，否则 renderer 的 navigator.mediaDevices.getUserMedia 会被拒绝，
// 导致语音链路起不来。这里放行 media 请求（可后续按需要收紧）。
app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
    // Electron PermissionType 里麦克风归 'media'（无独立 'microphone' 类型）
    if (permission === 'media') {
      callback(true)
    } else {
      callback(false)
    }
  })
})

// ---------- 单实例锁：二次启动时聚焦已有宠物窗口 ----------
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    createPetWindow()
  })

  app.whenReady().then(() => {
    // 皮肤偏好（userData/skin.json）→ 主进程状态（窗口创建前）
    loadSkinPref()

    // 注册全部 IPC（窗口创建前完成，避免渲染进程早期调用落空）
    registerIpcHandlers()

    // 唤醒词（KWS）主进程：注册 IPC + 懒加载初始化（sherpa-onnx-node，缺库/模型会打印警告）
    setupKws()

    // 后端网关（Mock 9000 / 真实后端同契约）：建立连接、广播状态、供 IPC 调用
    backendGateway.init()

    // 启动即创建悬浮宠物主窗口；控制面板由右键菜单「设置」按需打开
    createPetWindow()

    // 预创建（隐藏）的独立对话面板窗口：其渲染进程承载语音管线，常驻后台，
    // 保证宠物即使不打开面板也能被唤醒（与之前宠物窗口常驻语音行为一致）。
    // 打开/关闭面板只 show/hide 该窗口，不影响宠物窗口尺寸与显示。
    createChatWindow()

    // TODO: 后续迭代实现 — 初始化 WebSocket 长连接、语音服务（ASR/TTS）、全局状态机
  })

  // Windows / Linux：全部窗口关闭即退出应用
  app.on('window-all-closed', () => {
    app.quit()
  })

  app.on('before-quit', () => {
    backendGateway.dispose()
    destroyWindows()
  })

  // 显式持有引用，避免 GC 回收（Electron 官方建议）
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createPetWindow()
    }
  })
}
