import { cn } from '@/lib/cn'
import { dismissToast, useToasts, type Toast } from '@/data/toast-store'
import { AlertTriangle, Check, Info, Undo2, X } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * toast 的渲染层。数据与逻辑在 `@/data/toast-store`（见那里的说明）。
 */

const TONE_ICON = { ok: Check, risk: AlertTriangle, info: Info } as const

/** 每条 tone 的边框与图标色。默认（ok）走中性描边 —— 成功不必抢眼。 */
const TONE_STYLE = {
  ok: { border: 'border-line', icon: 'text-signal-ok' },
  risk: { border: 'border-signal-risk/40', icon: 'text-signal-risk' },
  info: { border: 'border-signal-warn/40', icon: 'text-signal-warn' },
} as const

function ToastRow({ toast }: { toast: Toast }) {
  const [paused, setPaused] = useState(false)
  const Icon = TONE_ICON[toast.tone]
  const style = TONE_STYLE[toast.tone]

  /*
    ⚠️ 计时器依赖 `paused`，而 `paused` 由鼠标进出切换。
    没有它会出现一个很恼人的失败：用户看到"已归档 3 项 · 撤销"，
    把鼠标移过去想点，**在移过去的路上它就消失了**。
    悬停暂停是这个组件最值钱的一行。
  */
  useEffect(() => {
    if (paused) return
    const timer = window.setTimeout(() => dismissToast(toast.id), toast.durationMs)
    return () => window.clearTimeout(timer)
  }, [toast.id, toast.durationMs, paused])

  return (
    <div
      role="status"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      className={cn(
        'pointer-events-auto flex items-center gap-2.5 rounded-md border px-3 py-2 shadow-overlay',
        'animate-stream-in bg-surface-overlay',
        style.border,
      )}
    >
      <Icon
        size={13}
        strokeWidth={2.25}
        aria-hidden="true"
        className={cn('shrink-0', style.icon)}
      />
      <span className="min-w-0 flex-1 text-xs text-ink">{toast.message}</span>

      {toast.action && (
        <button
          type="button"
          onClick={() => {
            // ★ 先关掉自己再执行：否则 `onAction` 抛错时这条 toast 会永远留着，
            //   而"撤销失败"这件事反而看不见了。
            dismissToast(toast.id)
            toast.action?.onAction()
          }}
          className={cn(
            'inline-flex h-6 shrink-0 items-center gap-1 rounded-sm px-2 text-2xs',
            'text-accent-strong transition-ui duration-fast hover:bg-accent/10',
            'focus:outline-none focus:ring-2 focus:ring-accent/40',
          )}
        >
          <Undo2 size={11} strokeWidth={2.25} aria-hidden="true" />
          {toast.action.label}
        </button>
      )}

      <button
        type="button"
        onClick={() => dismissToast(toast.id)}
        aria-label="关闭提示"
        className="shrink-0 rounded-sm p-0.5 text-ink-faint transition-ui duration-fast hover:text-ink"
      >
        <X size={12} strokeWidth={2} aria-hidden="true" />
      </button>
    </div>
  )
}

/**
 * 全局宿主。挂在 `App` 里（路由之外），所以每个页面都有 ——
 * 包括登录页与初始化页，那两处也需要报错提示。
 *
 * `pointer-events-none` 在外层、`pointer-events-auto` 在每条 toast 上：
 * 容器铺在右上角，不这样写它会挡住底下那一块的点击。
 */
export function ToastHost() {
  const items = useToasts()
  if (items.length === 0) return null

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed right-gutter top-bay z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
    >
      {items.map((toast) => (
        <ToastRow key={toast.id} toast={toast} />
      ))}
    </div>
  )
}

export default ToastHost
