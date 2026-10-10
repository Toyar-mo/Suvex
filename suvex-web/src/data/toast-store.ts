import { useSyncExternalStore } from 'react'

/**
 * toast 数据层 —— 「立即执行 + 可撤销」的前提。
 *
 * ## 为什么需要它（而不是用模态框确认）
 *
 * 归档这类操作**可撤销**，所以拦一道"你确定吗？"是多余的一次点击：
 * 它挡不住真正的误操作，只是让每一次正常操作都变慢。
 * 正确的做法是**先做，再给一个后悔的窗口** —— 而那需要一条
 * 能承载"撤销"按钮的浮层。
 *
 * ## 为什么是模块级 store 而不是 Context
 *
 * 与 `scans-store` / `settings-store` 同一套模式：调用方在**事件处理里**
 * 就要能发一条 toast，而它可能并不在某个 Provider 的子树里。
 * `showToast()` 是个普通函数，没有 hook 的调用位置限制 ——
 * 出错路径（`await` 之后的 `catch`）里也能用。
 *
 * ## 为什么数据层与组件分开成两个文件
 *
 * `react-refresh` 只在"一个文件只导出组件"时才可靠地热更新。
 * 同文件既导出组件又导出普通函数时，改一次会退化成整页刷新 ——
 * 而这一层恰好是会被频繁调整的。项目里 `data/` 与 `components/` 的分工
 * 本来也是这个意思。
 *
 * ⚠️ 快照必须**引用稳定**：`useSyncExternalStore` 每次渲染都比较它，
 * 每次返回新数组会无限重渲染。
 */

export interface ToastAction {
  label: string
  onAction: () => void
}

export interface Toast {
  id: string
  message: string
  /**
   * `risk` 用于失败/破坏性提示，`ok` 用于已完成的动作，
   * `info` 用于「动作成功了，但有一件你应该知道的事」——
   * 例如创建扫描时目标的可达性与预期不符（任务 11）。
   * 它既不是失败（别报红），也不该混进成功提示里被一眼扫过。
   */
  tone: 'ok' | 'risk' | 'info'
  action: ToastAction | null
  durationMs: number
}

/** 最多同时 3 条 —— 更多会盖住列表，而"撤销"恰恰需要可见。 */
const MAX_TOASTS = 3

/**
 * 有撤销动作时默认 **5 秒**（需求指定），没有时 3 秒。
 *
 * 为什么要区分：带撤销的提示需要用户**反应过来 + 移动鼠标 + 点中**，
 * 3 秒经常不够；而纯告知性提示停留太久反而碍事。
 */
export const UNDO_DURATION_MS = 5000
export const PLAIN_DURATION_MS = 3000

/**
 * `info` 停留 **8 秒**，比纯告知长得多。
 *
 * 理由：可达性提示是一整句人话（"目标只在 http 下有应答，将以
 * http://example.com 发起扫描"），读它需要时间；而它一旦消失就
 * **再也找不回来**（后端只在创建响应里带它）。3 秒对一句话太短。
 */
export const INFO_DURATION_MS = 8000

let toasts: Toast[] = []
let snapshot: Toast[] = toasts
const listeners = new Set<() => void>()

function commit() {
  snapshot = toasts
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot(): Toast[] {
  return snapshot
}

let seq = 0

export function showToast(input: {
  message: string
  tone?: Toast['tone']
  action?: ToastAction
  durationMs?: number
}): string {
  const id = `toast_${++seq}`
  const toast: Toast = {
    id,
    message: input.message,
    tone: input.tone ?? 'ok',
    action: input.action ?? null,
    durationMs:
      input.durationMs ??
      (input.action ? UNDO_DURATION_MS : input.tone === 'info' ? INFO_DURATION_MS : PLAIN_DURATION_MS),
  }
  toasts = [toast, ...toasts].slice(0, MAX_TOASTS)
  commit()
  return id
}

export function dismissToast(id: string): void {
  if (!toasts.some((t) => t.id === id)) return
  toasts = toasts.filter((t) => t.id !== id)
  commit()
}

/** 测试用：清空全部。 */
export function clearToasts(): void {
  toasts = []
  commit()
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
