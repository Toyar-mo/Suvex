import { cn } from '@/lib/cn'
import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

/**
 * 通用空状态。
 *
 * 与 `OverviewEmptyState`（Overview 页专属）的区别：那个换的是**整页主张**
 * （"你的态势" → "从这里开始"），这里的粒度是**一个区域**
 * （一张表、一个列表、一块面板）。
 *
 * ## 三条从 OverviewEmptyState 继承、这里也坚持的规矩
 *
 * 1. **不显示一堆 0 或一张灰色插画 + "暂无数据"。**
 *      空的表格不是"表格病了"，是"还没有东西进来"。要给出**下一步**。
 * 2. **`action` 是可选的，且只有一个。**
 *      两个并列的行动按钮等于没有推荐。
 * 3. **`description` 说清"为什么是空的"以及"怎么让它不空"。**
 *      "暂无数据"四个字没有回答其中任何一个。
 */

export interface EmptyStateProps {
  icon: LucideIcon
  title: string
  description?: ReactNode
  /** 主动作。不传就只显示说明 —— 有些空状态确实没有可做的事 */
  action?: ReactNode
  className?: string
  /** 紧凑模式：用于卡片/抽屉内部 */
  compact?: boolean
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
  compact = false,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-start rounded-md border border-dashed border-line-strong',
        compact ? 'p-panel' : 'p-bay',
        className,
      )}
    >
      <Icon
        size={compact ? 16 : 20}
        strokeWidth={1.5}
        className="shrink-0 text-ink-ghost"
        aria-hidden="true"
      />

      <h3 className={cn('mt-panel font-medium text-ink', compact ? 'text-xs' : 'text-base')}>
        {title}
      </h3>

      {description && (
        <p
          className={cn(
            'mt-tight max-w-[56ch] text-ink-muted',
            compact ? 'text-2xs' : 'text-sm',
          )}
        >
          {description}
        </p>
      )}

      {action && <div className="mt-bay flex flex-wrap items-center gap-panel">{action}</div>}
    </div>
  )
}

export default EmptyState
