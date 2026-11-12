import { useSyncExternalStore } from 'react'

/**
 * 工具卡全局展开状态（v1.92，Ctrl+O）。
 *
 * ## 为什么是外部 store，而不是从 ChatPage 往下传 prop
 *
 * 工具卡在 `AssistantTurn → ToolCard` 两层之下，而键盘快捷键属于**页面**。
 * 一层层透传 `forceOpen` 会让每个中间组件都多一个与自己无关的参数；
 * 卡片也可能同时被抽屉/预览复用。小的外部 store 只有三个状态，
 * 读取方是真正需要它的那个叶子节点。
 *
 * `auto` = 每张卡按自己的默认行为（默认展开、可单独收起）。
 */
export type ToolExpansion = 'auto' | 'expanded' | 'collapsed'

let mode: ToolExpansion = 'auto'
const listeners = new Set<() => void>()

export function setToolExpansion(next: ToolExpansion): void {
  if (mode === next) return
  mode = next
  listeners.forEach((listener) => listener())
}

export function getToolExpansion(): ToolExpansion {
  return mode
}

export function subscribeToolExpansion(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useToolExpansion(): ToolExpansion {
  return useSyncExternalStore(subscribeToolExpansion, getToolExpansion, getToolExpansion)
}

/** 重置——测试与会话切换时用，避免一个页面的快捷键影响另一个页面。 */
export function resetToolExpansion(): void {
  mode = 'auto'
  listeners.forEach((listener) => listener())
}
