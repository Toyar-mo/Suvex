import { cn } from '@/lib/cn'

/**
 * 骨架块 —— 加载态的统一积木。
 *
 * ## 两条从 `PageFallback` 继承来的规矩
 *
 * 1. **用 `animate-skeleton`，不用 Tailwind 默认的 `animate-pulse`。**
 *      默认 pulse 是 opacity 1→0.5，在近黑底上骨架块几乎消失，
 *      读起来像"页面在闪"而不是"内容正在来"；而且它的曲线与时长
 *      都不在设计系统里（见 `tailwind.config.js` 的 `skeleton` keyframes）。
 * 2. **骨架的尺寸节奏要贴近真实内容。**
 *      骨架比真实内容矮一截的话，数据到达的瞬间整页会往上跳一次 ——
 *      那一下比多等 200ms 更让人不适。所以下面这些预设的高度
 *      都是配着真实组件的行高定的，不是随手写的数字。
 *
 * ## 为什么是"静止的骨架"而不是转圈
 *
 * 本地接口通常几十毫秒就回，转圈会一闪而过，比不动更刺眼。
 * 骨架在中长延迟下才是"内容正在来"的正确信号。
 *
 * 可访问性：骨架整块 `aria-hidden`，由**外层容器**统一宣布"正在加载"
 * （见 `SkeletonRows` 的 `role="status"`）。逐个骨架块都报一次
 * 会让读屏器念出一串无意义的内容。
 */

export interface SkeletonProps {
  className?: string
}

export function Skeleton({ className }: SkeletonProps) {
  return <div aria-hidden="true" className={cn('animate-skeleton rounded-xs bg-surface-raised', className)} />
}

/**
 * 列表骨架 —— 最常用的一种，用于"一堆同构的行/卡片还在路上"。
 *
 * `rows` 默认 3：低于 3 看起来像渲染残缺，高于 5 会在短延迟下
 * 造成"内容很多"的错误预期。
 */
export interface SkeletonRowsProps {
  rows?: number
  /** 每行高度类名。默认对齐 `h-16` 的卡片；表格行可传 `h-11` */
  rowClassName?: string
  className?: string
}

export function SkeletonRows({ rows = 3, rowClassName = 'h-16', className }: SkeletonRowsProps) {
  return (
    <div
      className={cn('flex flex-col gap-tight', className)}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <span className="sr-only">正在加载</span>
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className={cn('rounded-md bg-surface', rowClassName)} />
      ))}
    </div>
  )
}

/**
 * 文本骨架 —— 用于"一段说明/描述还在路上"。
 *
 * 最后一行的宽度是**故意写死 60% 的**：等宽的几行看起来像排版错乱，
 * 而真实段落的末行本来就短。这是让骨架"像内容"而不是"像占位"的细节。
 */
export interface SkeletonTextProps {
  lines?: number
  className?: string
}

export function SkeletonText({ lines = 3, className }: SkeletonTextProps) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)} aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton
          key={index}
          className={cn('h-3.5', index === lines - 1 ? 'w-3/5' : 'w-full')}
        />
      ))}
    </div>
  )
}
