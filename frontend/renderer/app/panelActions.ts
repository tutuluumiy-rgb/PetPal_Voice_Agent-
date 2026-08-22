/**
 * 控制面板页面级动作（右下角操作栏）
 * --------------------------------------------------------------------------
 * 单窗口模块级单例：PanelShell 渲染 actions，各页面在挂载时 setPanelActions 注册
 * （保存/撤销/生成等），卸载时清空。按钮统一样式见 PanelShell 操作栏。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'

export interface PanelAction {
  key: string
  label: string
  /** true = 主按钮（填充强调色），false=次要（描边） */
  primary?: boolean
  /** 是否禁用（动态用函数） */
  disabled?: boolean | (() => boolean)
  onClick: () => void
}

/** 当前页注册的动作（PanelShell 右下角渲染） */
export const panelActions: Ref<PanelAction[]> = ref<PanelAction[]>([])

/** 页面挂载时注册动作 */
export function setPanelActions(actions: PanelAction[]): void {
  panelActions.value = actions
}

/** 页面卸载时清空 */
export function clearPanelActions(): void {
  panelActions.value = []
}

/** 是否禁用（支持函数） */
export function isActionDisabled(a: PanelAction): boolean {
  return typeof a.disabled === 'function' ? Boolean(a.disabled()) : Boolean(a.disabled)
}