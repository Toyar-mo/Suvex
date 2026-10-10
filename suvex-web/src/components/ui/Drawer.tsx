import { cn } from '@/lib/cn'
import { X } from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/**
 * 侧滑抽屉。
 *
 * 为什么用抽屉而不是跳详情页（brief 让我定，这里说明依据）：
 *   任务 3 已经建立了「列表在左、内容在右」的主从习惯。
 *   Findings 的列表是**表格**，需要横向宽度 —— 常驻侧栏会把它压到不可读。
 *   抽屉从右侧滑出，关闭后表格恢复全宽，既延续了"内容在右"的习惯，
 *   又不牺牲表格宽度。而且分诊（triage）时用户会连续看很多条，
 *   每次跳页再返回会丢掉列表的滚动位置和筛选状态。
 *
 * 无障碍三件事，缺一不可：
 *   1. Escape 关闭
 *   2. 打开时把焦点移进抽屉，关闭时还给触发它的元素
 *      —— 否则键盘用户的焦点会掉到页面顶部，得重新 Tab 一路回来
 *   3. 打开时锁住 body 滚动。不锁的话背景会跟着滚，
 *      在移动端尤其明显（抽屉和背景一起动，像是坏了）
 */

export interface DrawerProps {
  open: boolean
  onClose: () => void
  /** 无障碍标题（读屏器念的） */
  labelledBy?: string
  title: ReactNode
  /** 标题右侧的附加内容（状态徽标等） */
  titleAside?: ReactNode
  children: ReactNode
  /** 固定在底部的操作区 */
  footer?: ReactNode
  className?: string
}

export function Drawer({
  open,
  onClose,
  labelledBy,
  title,
  titleAside,
  children,
  footer,
  className,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  // 记录打开前的焦点元素，关闭时还回去
  const previousFocusRef = useRef<HTMLElement | null>(null)

  // Escape 关闭 + body 滚动锁
  useEffect(() => {
    if (!open) return

    previousFocusRef.current = document.activeElement as HTMLElement | null

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }

    document.addEventListener('keydown', onKeyDown)

    // 锁滚动：记下原来的值，恢复时还回去（可能有别的组件也设过）
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    // 焦点移进面板。用 preventScroll 避免页面跳动
    panelRef.current?.focus({ preventScroll: true })

    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      // 焦点还给触发元素 —— 键盘用户不会掉到页面顶部
      previousFocusRef.current?.focus({ preventScroll: true })
    }
  }, [open, onClose])

  if (!open) return null

  /*
    ★ 必须用 portal 挂到 body，不能就地渲染。

    踩过的坑：旧版 PageTransition 的包裹层（ReactBits 的 AnimatedContent）
    动画结束后会留下 `transform: matrix(1, 0, 0, 1, 0, 0)`。这是个**恒等矩阵，
    视觉上完全没变化**，但只要 transform 不是 `none`，它就会成为
    `position: fixed` 后代的**包含块** —— 于是 `inset-0` 的遮罩不再是整屏，
    `h-full` 的抽屉变成了页面内容的完整高度（实测 1217px，而视口只有 900px）。

    后果比看起来严重：抽屉比屏幕高，底部操作区在屏幕外，遮罩也盖不住整页。
    截图上看不出问题（顶部内容是对的），但交互是坏的。

    任务 8 已把 PageTransition 换成纯 CSS 透明度动画 —— **它不产生 transform**，
    所以那个具体陷阱没有了。但 portal **不打算改回去**，两个理由：

      1. 浮层本来就不该受内容层任何变换的影响。今天没有 transform，不代表
         明天加一个入场/视差不会引入 —— 而这类 bug 在截图上看不出来，
         只在真实交互里坏掉，是最难被发现的一种。
      2. 项目里其它地方仍可能有 transform 祖先（任何 scale/translate 的
         hover 效果、未来引入的动画库）。portal 让整个类问题消失。

    即 taste 复查里的那条：**不要因为当前恰好不出错，就把结构性防护删掉。**
  */
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end" role="presentation">
      {/*
        遮罩。点击关闭是通用预期。
        用 button 而不是 div：读屏器需要一个可聚焦的"关闭"目标，
        div 加 onClick 对键盘用户等于不存在。
      */}
      <button
        type="button"
        onClick={onClose}
        aria-label="关闭详情"
        className="absolute inset-0 cursor-default bg-canvas/70 backdrop-blur-[2px]"
      />

      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className={cn(
          'relative flex h-full w-full flex-col border-l border-line bg-surface shadow-overlay',
          // 窄屏占满，宽屏固定宽度 —— 表格仍能露出一部分，用户知道自己在哪
          'sm:w-[min(34rem,92vw)]',
          // 入场动画：只做位移 + 淡入，200ms。抽屉是"滑入"语义，
          // 这一处值得一点点 directional motion（它表达了从哪来）
          'animate-drawer-in',
          className,
        )}
      >
        {/* 头部 */}
        <div className="flex shrink-0 items-start gap-tight border-b border-line px-panel py-3">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-tight">{titleAside}</div>
            <h2 id={labelledBy} className="text-sm font-medium leading-snug text-ink">
              {title}
            </h2>
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

        {/* 内容区：独立滚动 */}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">{children}</div>

        {/* 底部操作区：常驻可见，不用滚到底才找到操作 */}
        {footer && (
          <div className="shrink-0 border-t border-line bg-surface px-panel py-3">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  )
}

export default Drawer
