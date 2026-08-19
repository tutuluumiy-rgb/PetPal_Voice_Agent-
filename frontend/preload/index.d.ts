/**
 * 渲染进程全局类型声明：window.api
 * 由 preload/index.ts 的 contextBridge 注入，类型来自 preload/types.ts
 */
import type { AppApi } from './types'

declare global {
  interface Window {
    api: AppApi
  }
}

export {}
