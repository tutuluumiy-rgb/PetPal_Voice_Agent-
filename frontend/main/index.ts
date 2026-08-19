/**
 * 主进程入口 — PetPal Voice Agent
 * --------------------------------------------------------------------------
 * 主进程持有 WebSocket / 语音服务 / 全局状态机（均为 TODO 预留位）；
 * 渲染进程只负责 UI 渲染，业务逻辑不上渲染进程。
 */
import { app, BrowserWindow } from 'electron'
import { registerIpcHandlers } from './ipc'
import { createPetWindow, destroyWindows } from './windows'

// ---------- 单实例锁：二次启动时聚焦已有宠物窗口 ----------
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    createPetWindow()
  })

  app.whenReady().then(() => {
    // 注册全部 IPC（窗口创建前完成，避免渲染进程早期调用落空）
    registerIpcHandlers()

    // 启动即创建悬浮宠物主窗口；控制面板由右键菜单「设置」按需打开
    createPetWindow()

    // TODO: 后续迭代实现 — 初始化 WebSocket 长连接、语音服务（ASR/TTS）、全局状态机
  })

  // Windows / Linux：全部窗口关闭即退出应用
  app.on('window-all-closed', () => {
    app.quit()
  })

  app.on('before-quit', () => {
    destroyWindows()
  })

  // 显式持有引用，避免 GC 回收（Electron 官方建议）
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createPetWindow()
    }
  })
}
