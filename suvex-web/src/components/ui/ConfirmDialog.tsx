import Button from '@/components/ui/Button'
import { useEffect, useRef } from 'react'

/**
 * 二次确认弹窗。**只用于删除**，不用于归档。
 *
 * ## 为什么归档不要它、删除要
 *
 * 两者的**可撤销性**不同，所以正确的交互也不同：
 *
 * | 操作 | 可撤销 | 交互 |
 * | --- | --- | --- |
 * | 归档 | 是（取消归档） | 立即执行 + toast 给撤销窗口 |
 * | 删除 | 是（回收站恢复） | 先确认，再执行 + toast 给撤销窗口 |
 *
 * 删除多一道确认，是因为它的**心理成本**更高：用户会以为"删了就是没了"，
 * 哪怕我们实际上只打了软删除标记。让他先看一眼"将删除 N 项"比事后解释
 * "其实还能恢复"更省事。归档不给人这种感觉，所以不必拦。
 *
 * ## 焦点管理不是可选项
 *
 * 弹窗打开时焦点必须移进去，否则键盘用户 Tab 着就跑到后面的页面上了 ——
 * 而**屏幕上看不出来的**。这里把初始焦点给取消按钮（不是危险按钮）：
 * 回车误触时应当什么都不发生。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  onConfirm,
  onCancel,
  danger = true,
  busy = false,
}: {
  open: boolean
  title: string
  description: string
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  danger?: boolean
  busy?: boolean
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    // 焦点给"取消"——误触回车时应当什么都不发生
    cancelRef.current?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onCancel])

  if (!open) return null

  return (
    <div
      // 遮罩层点击 = 取消。这是通用预期，比"必须点按钮"少一次瞄准。
      onClick={onCancel}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/70 p-gutter backdrop-blur-sm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        // 阻止冒泡：否则点弹窗内部也会触发遮罩的 onClick
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md animate-rise-in rounded-md border border-line bg-surface-overlay p-panel shadow-overlay"
      >
        <h2 id="confirm-title" className="text-sm font-medium text-ink">
          {title}
        </h2>
        <p className="mt-1.5 text-xs text-ink-muted">{description}</p>

        <div className="mt-panel flex justify-end gap-tight">
          {/*
            焦点环全站统一由 `src/index.css` 的 `:focus-visible` 给，这里不再单独画。
            原来那两个 `focus:ring-2 focus:ring-accent/40` 有两个问题：
              ① 用的是 `focus:` 而不是 `focus-visible:` —— 鼠标点一下也亮环，
                 和 index.css 里 `:focus:not(:focus-visible)` 的"鼠标不画环"是打架的；
              ② `accent/40` 是 40% 透明，对白底的实际对比度远低于 UI 组件的 3:1 下限。
          */}
          <Button ref={cancelRef} variant="secondary" size="sm" onClick={onCancel}>
            取消
          </Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            size="sm"
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}

export default ConfirmDialog
