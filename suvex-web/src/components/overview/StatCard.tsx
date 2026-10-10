import CountUp from '@/components/effects/CountUp'
import { cn } from '@/lib/cn'
import { usePrefersReducedMotion } from '@/hooks/usePrefersReducedMotion'
import { ArrowDownRight, ArrowUpRight, Minus, type LucideIcon } from 'lucide-react'

/**
 * 关键指标卡。
 *
 * 可复用契约：只吃「一个标签 + 一个数值 + 可选的变化/趋势」，不认识任何业务。
 * 数字可以是 number（走滚动动效）或已经格式化好的 string（如 "6m 44s"，
 * 这种带单位的量没有"滚动"语义，直接显示）。
 *
 * 可访问性（这是这个卡片最容易被做错的地方）：
 *   CountUp 每帧都在改 textContent，读屏器若跟随会念出几十个中间值，
 *   所以动画数字标了 aria-hidden，另给一份静态文本给读屏器。
 *   视觉用户看到滚动，读屏用户听到一个确定的数。
 *
 * 趋势箭头只在**有可比数据**时出现。delta 为 null 表示"没有上周数据"，
 * 这时显示"暂无对比"而不是装作 0 变化 —— 0 变化和无法比较是两回事。
 */

export interface StatCardProps {
  label: string
  /** 数值。number 走滚动动画；string 视为已格式化，不做动画 */
  value: number | string
  /** 单位后缀，如 "秒"、"条"。紧贴数值，用弱化色 */
  unit?: string
  icon: LucideIcon
  /** 变化量。null = 无同比数据 */
  delta?: number | null
  /** delta 的方向语义：negative-when-up 时涨是坏事（漏洞） */
  deltaIntent?: 'neutral' | 'negative-when-up'
  /** delta 旁边的说明，如 "较上周" */
  deltaLabel?: string
  /** 补充说明一行 */
  footnote?: string
  /** 数值格式化函数 —— number 类型时生效 */
  format?: (value: number) => string
  className?: string
}

export function StatCard({
  label,
  value,
  unit,
  icon: Icon,
  delta,
  deltaIntent = 'neutral',
  deltaLabel,
  footnote,
  format,
  className,
}: StatCardProps) {
  const prefersReducedMotion = usePrefersReducedMotion()

  const isNumeric = typeof value === 'number'
  const displayValue = isNumeric && format ? format(value) : value

  // 数值为 0 且没有脚注时不显示"0"以外的装饰；空值统一走 "—"
  const isEmpty = value === null || value === undefined

  return (
    <div
      className={cn(
        'flex flex-col justify-between rounded-md border border-line bg-surface p-panel',
        'transition-ui duration-fast hover:border-line-strong',
        className,
      )}
    >
      <div className="flex items-start justify-between gap-tight">
        <span className="text-xs text-ink-muted">{label}</span>
        <Icon size={16} strokeWidth={1.75} className="shrink-0 text-ink-ghost" aria-hidden="true" />
      </div>

      <div className="mt-panel flex items-baseline gap-1.5">
        {isEmpty ? (
          <span className="tabular text-3xl font-medium text-ink-faint" data-numeric>
            —
          </span>
        ) : isNumeric && !prefersReducedMotion ? (
          <>
            {/* 视觉：滚动数字。读屏器不看它（中间值会刷屏） */}
            <CountUp
              to={value as number}
              duration={0.9}
              separator={typeof displayValue === 'string' && displayValue.includes(',') ? ',' : ''}
              className="tabular text-3xl font-medium text-ink"
            />
            {/* 读屏器：一个确定的终值 */}
            <span className="sr-only" data-numeric>
              {displayValue}
            </span>
          </>
        ) : (
          // 减少动效偏好，或值本身就是字符串（"6m 44s"）
          <span className="tabular text-3xl font-medium text-ink" data-numeric>
            {displayValue}
          </span>
        )}

        {unit && <span className="text-sm text-ink-muted">{unit}</span>}
      </div>

      {/* 底部一行：趋势 或 脚注。两者不同时出现，避免这一行变拥挤 */}
      <div className="mt-tight flex min-h-[1.25rem] items-center gap-tight text-xs">
        {delta === undefined ? null : delta === null ? (
          // 没有同比数据时**不渲染 deltaLabel**。
          // 之前这里会拼出 "No prior week to comparevs last week" —— 两个 span
          // 之间没有分隔符，少一个空格。语义上也不该拼：没有可比数据时，
          // "vs last week" 是句废话。
          <span className="text-ink-faint">暂无对比数据</span>
        ) : (
          <>
            <DeltaBadge delta={delta} intent={deltaIntent} />
            {deltaLabel && <span className="text-ink-faint">{deltaLabel}</span>}
          </>
        )}
        {footnote && <span className="text-ink-faint">{footnote}</span>}
      </div>
    </div>
  )
}

/**
 * 变化量徽标。
 *
 * 颜色只在 deltaIntent === 'negative-when-up' 时才用 signal 色 ——
 * 对"平均耗时"这类指标，涨跌本身没有好坏，上色会替用户下结论。
 */
function DeltaBadge({ delta, intent }: { delta: number; intent: 'neutral' | 'negative-when-up' }) {
  const up = delta > 0
  const flat = delta === 0
  const Icon = flat ? Minus : up ? ArrowUpRight : ArrowDownRight

  const tone =
    intent === 'negative-when-up' && !flat
      ? up
        ? 'text-signal-risk'
        : 'text-signal-ok'
      : 'text-ink-muted'

  return (
    <span className={cn('tabular inline-flex items-center gap-1', tone)} data-numeric>
      <Icon size={13} strokeWidth={2} aria-hidden="true" />
      {flat ? '持平' : `${up ? '+' : ''}${delta}`}
    </span>
  )
}

export default StatCard
