import { useSyncExternalStore } from 'react'

import {
  BACKDROP_STORAGE_KEY,
  DEFAULT_BACKDROP,
  applyBackdrop,
  isBackdropId,
  readStoredBackdrop,
  type BackdropId,
} from '@/lib/backdrop'

/**
 * 氛围层偏好 —— 模块级外部 store，组件用 `useSyncExternalStore` 订阅。
 *
 * ## 为什么与 `theme-store` 一样是外部 store
 *
 * 同一条理由：它必须在**首次渲染之前**就写进 DOM（`initBackdrop()` 在
 * `main.tsx` 里、`createRoot` 之前跑），否则页面会先按默认氛围画一帧、
 * 再跳成用户选的那一档 —— 而背景是整屏的，闪一下比主题闪一下更明显。
 *
 * ## 为什么快照就是一个字符串
 *
 * 与主题不同，这里没有"偏好 vs 解析值"的两层：氛围没有跟随系统这一说。
 * 快照是字符串本身，引用天然稳定（`useSyncExternalStore` 要的就是这个）。
 */
let current: BackdropId = DEFAULT_BACKDROP

const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot(): BackdropId {
  return current
}

function commit(next: BackdropId): void {
  if (next === current) return
  current = next
  for (const listener of listeners) listener()
}

/** 启动时对齐一次：读存储 → 写 DOM → 对齐快照。幂等 */
export function initBackdrop(): void {
  current = readStoredBackdrop()
  applyBackdrop(current)
}

/** 用户在菜单里选了一档 */
export function setBackdrop(id: BackdropId): void {
  if (!isBackdropId(id)) return
  applyBackdrop(id)
  try {
    window.localStorage.setItem(BACKDROP_STORAGE_KEY, id)
  } catch {
    // 隐私模式下写不进去：本次会话内仍然生效，只是下次回来会退回默认
  }
  commit(id)
}

export function useBackdrop(): BackdropId {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** 非组件代码（事件处理器、测试）取当前值 */
export function currentBackdrop(): BackdropId {
  return current
}
