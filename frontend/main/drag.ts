/**
 * 窗口拖拽适配
 * --------------------------------------------------------------------------
 * 帧合并方案（修复拖动闪烁）：
 * - 渲染进程 mousemove 全部接收，但只保存目标坐标（不发 IPC）；
 * - 渲染进程 requestAnimationFrame 每帧合并一次目标坐标 → 发一次 dragMove；
 * - 主进程 dragMove 直接 setPosition（Math.round 整数坐标，无亚像素抖动）；
 * - 拖拽位置 clamp 到屏幕工作区内，宠物/窗口不会被拖到屏幕外（Sub-task 3）；
 * - 面板打开时拖拽：更新「恢复锚点」prevPos 为拖拽后位置，
 *   关闭面板时恢复到拖拽后位置而非面板打开前（Sub-task 2，避免瞬移）。
 */
import { BrowserWindow, screen } from 'electron'
import type { DragPoint } from '../preload/types'
import { updatePetWindowPrevPos } from './windows'

interface DragState {
  window: BrowserWindow
  offsetX: number
  offsetY: number
}

let dragState: DragState | null = null

/** 渲染进程 mousedown：记录鼠标与窗口位置偏移 */
export function dragStart(win: BrowserWindow, point: DragPoint): void {
  dragEnd()
  const [winX, winY] = win.getPosition()
  dragState = {
    window: win,
    offsetX: point.screenX - winX,
    offsetY: point.screenY - winY
  }
}

/** 渲染进程 rAF 帧合并后调用：设置窗口位置（整数坐标、clamp 屏幕内） */
export function dragMove(point: DragPoint): void {
  if (!dragState) return

  const { window: win, offsetX, offsetY } = dragState
  const [winW, winH] = win.getSize()
  const wa = screen.getDisplayMatching(win.getBounds()).workArea

  // 目标位置（整数，无亚像素抖动）
  let x = Math.round(point.screenX - offsetX)
  let y = Math.round(point.screenY - offsetY)

  // 约束：窗口完整保持在屏幕工作区内（宠物不被拖出屏幕外）
  x = Math.max(wa.x, Math.min(x, wa.x + wa.width - winW))
  y = Math.max(wa.y, Math.min(y, wa.y + wa.height - winH))

  win.setPosition(x, y)
  // 面板打开时更新恢复锚点 → 关闭面板恢复到拖拽后位置（不瞬移回面板打开前）
  updatePetWindowPrevPos(x, y)
}

/** 渲染进程 mouseup / window blur：结束拖拽 */
export function dragEnd(): void {
  dragState = null
}
