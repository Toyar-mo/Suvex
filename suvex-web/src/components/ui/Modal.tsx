import { cn } from '@/lib/cn'
import { X } from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/**
 * 居中弹窗。
 *
 * ## 为什么又加一个浮层组件（而不是把 Drawer 改个属性）
 *
 * 两者表达的**语义**不同，不是宽度不同：
 *
 * | | Drawer（右侧抽屉） | Modal（居中弹窗） |
 * |---|---|---|
 * | 场景 | 从列表里**看**一条的详情 | 专注**编辑**一份东西 |
 * | 与列表的关系 | 并存（列表还在左边） | 遮断（编完再回去） |
 * | 合适的内容 | 只读详情、分诊 | 多字段表单、文件树 |
 * | 宽高 | 竖长条 | 内容自定，可宽 |
 *
 * 技能编辑是一个**多字段 + 文件列表**的表单，塞进 34rem 的竖长抽屉里
 * 会让正文编辑区只剩几行 —— 那是"看得见但不好用"。而这个动作本身
 * 是"专心写完再保存"，遮断列表反而更对。
 *
 * ## ★ 焦点管理必须照抄 Drawer 的那条修法
 *
 * `onClose` 进 ref、**不进 effect 依赖**。理由与 Drawer 完全相同：
 * 调用方几乎总写内联箭头函数，若把它列进依赖，那么浮层里**每敲一个
 * 字符**（state 变 → 重渲染 → 新 onClose）都会让 effect 重跑一遍，
 * 清理函数把焦点还给触发元素、新 effect 再移进来 ——
 * 用户看到"敲一个字，光标跳走"。
 *
 * 这条 bug 在 Drawer 上真实发生过（用户报「打一个字就失焦」），
 * 所以新写的浮层从一开始就按修好后的形状写，不是等它再犯一次。
 *
 * 无障碍四件事（与 Drawer 一致，缺一不可）：
 *   1. Escape 关闭
 *   2. 打开时焦点移入，关闭时还给触发元素
 *   3. 打开时锁 body 滚动
 *   4. portal 到 body —— 躲开祖先 `transform` 成为包含块的那个坑
 */

export interface ModalProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  /** 标题右侧的附加内容（状态徽标等） */
  titleAside?: ReactNode
  children: ReactNode
  /** 固定在底部的操作区 */
  footer?: ReactNode
  /** 面板宽度类（默认 `max-w-2xl`）—— 内容窄的弹窗可以传更小的 */
  widthClass?: string
  /** 面板高度类（默认 `max-h-[85vh]`） */
  heightClass?: string
}

export function Modal({
  open,
  onClose,
  title,
  titleAside,
  children,
  footer,
  widthClass = 'max-w-2xl',
  heightClass = 'max-h-[85vh]',
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)

  // ★ 与 Drawer 同一条修法：onClose 只进 ref，不进 effect 依赖
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    if (!open) return

    previousFocusRef.current = document.activeElement as HTMLElement | null

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onCloseRef.current()
      }
    }
    document.addEventListener('keydown', onKeyDown)

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    panelRef.current?.focus({ preventScroll: true })

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      previousFocusRef.current?.focus({ preventScroll: true })
    }
  }, [open])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-gutter">
      {/* 遮罩：点击关闭，用 button 让读屏器有一个可聚焦的"关闭"目标 */}
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭"
        className="absolute inset-0 cursor-default bg-canvas/70 backdrop-blur-[2px]"
      />

      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        className={cn(
          'relative flex w-full flex-col overflow-hidden rounded-md border border-line bg-surface shadow-overlay',
          'animate-rise-in',
          widthClass,
          heightClass,
        )}
      >
        <div className="flex shrink-0 items-start gap-tight border-b border-line px-panel py-3">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-tight">{titleAside}</div>
            <h2 className="text-sm font-medium leading-snug text-ink">{title}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="shrink-0 rounded-sm p-1.5 text-ink-faint transition-ui duration-fast hover:bg-surface-raised hover:text-ink"
          >
            <X size={16} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-panel py-3">
          {children}
        </div>

        {footer && (
          <div className="shrink-0 border-t border-line bg-surface px-panel py-3">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  )
}

export default Modal
