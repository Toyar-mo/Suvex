import { cn } from '@/lib/cn'
import type { SlaState } from '@/data/finding'
import { Clock } from 'lucide-react'

/**
 * 修复时限标签（任务 10 P3）。
 *
 * ## 为什么数字来自后端而不是前端算
 *
 * 组件拿到的是 `slaState` + `slaDaysLeft`，**不读 `dueAt` 自己比**。
 * 理由与"角标读 `/findings/stats` 而不是本地数一遍"完全一样：
 * 两处独立判断会取到不同时刻的快照，而"这条到底超期没有"在列表刷新
 * 前后给出不同的答案是很难向你解释的。而且前端算的话，"临期"的判定
 * 窗口（3 天）就要在前后端各写一份，改了规则还得两边一起改。
 *
 * ## 为什么不显示具体日期
 *
 * 列表里一屏几十行，`2026-10-02 14:33` 这种精确时刻**读不出"急不急"** ——
 * 而那是这一列唯一要回答的问题。所以显示成「超期 2 天」/「还剩 3 天」，
 * 精确时刻放 `title`，需要的时候悬停看。
 *
 * ## 配色只表达一件事：要不要现在管
 *
 * - `overdue` 红 —— 已经违约了
 * - `dueSoon` 黄 —— 快到
 * - `onTrack` 灰 —— 还早，别占注意力
 * - `none` **什么都不显示** —— 见下
 *
 * ## 为什么 `none` 不渲染
 *
 * "不计时"可能是"这个档位不设时限"（低危常常如此），也可能是
 * "这条已经关掉了"。两种情况都**不需要用户做任何事**，而在每一行
 * 都挂一个"无期限"的标签，会把真正需要看的那几个红黄标记淹掉。
 * 空白就是它应有的样子。
 */
export function SlaBadge({
  state,
  daysLeft,
  dueAt,
  className,
}: {
  state: SlaState
  daysLeft: number | null
  /** 精确到期时刻，只用于 title */
  dueAt?: string | null
  className?: string
}) {
  if (state === 'none' || daysLeft === null) return null

  const label =
    state === 'overdue'
      ? `超期 ${Math.abs(daysLeft)} 天`
      : daysLeft === 0
        ? '今天到期'
        : `还剩 ${daysLeft} 天`

  const tone =
    state === 'overdue'
      ? 'border-signal-risk/50 bg-signal-risk/[0.08] text-signal-risk'
      : state === 'dueSoon'
        ? 'border-signal-warn/50 bg-signal-warn/[0.08] text-signal-warn'
        : 'border-line text-ink-faint'

  // 精确时刻放 title —— 悬停能看到，但它不占列表的视觉重量
  const exact = dueAt ? new Date(dueAt) : null
  const title = exact
    ? `${state === 'overdue' ? '已于' : '到期于'} ${exact.toLocaleString('zh-CN')}`
    : undefined

  return (
    <span
      title={title}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-xs border px-1 text-2xs',
        tone,
        className,
      )}
    >
      <Clock size={10} strokeWidth={2} aria-hidden="true" />
      {label}
    </span>
  )
}

export default SlaBadge
