import { Link } from 'react-router-dom'

import { SEVERITY_META } from '@/data/findings'
import type { SeverityBreakdownEntry } from '@/data/overview'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/cn'

/**
 * 发现优先级（P0–P4，v1.90）。
 *
 * ## 为什么换掉原来的严重级别条
 *
 * 同一个数据，两种读法：
 *   - `critical/high/medium/low/info` 是**漏洞的属性**，读的人要自己换算成
 *     "我该多快处理"；
 *   - `P0–P4` 是**处置承诺**（立即 / 本周 / 排期 / 观察 / 记录），
 *     它与工单、值班表、复盘口径直接对齐。
 *
 * 移植自 AgenticPentest 的看板：每级一行，左边是承诺、右边是数量，
 * 行尾直接进过滤后的发现列表 —— 看板不该要求用户再想一次"点哪"。
 *
 * 映射刻意是**固定的**（critical→P0 … info→P4），不做可配置：
 * 允许改映射，就会出现"同样的漏洞在两个部署里优先级不同"，
 * 而基准与复盘都建立在同一个口径上。
 */

const PRIORITY_ROWS = [
  { severity: 'critical', level: 'P0', tone: 'text-signal-risk', dot: 'bg-signal-risk' },
  { severity: 'high', level: 'P1', tone: 'text-signal-high', dot: 'bg-signal-high-solid' },
  { severity: 'medium', level: 'P2', tone: 'text-signal-warn', dot: 'bg-signal-warn' },
  { severity: 'low', level: 'P3', tone: 'text-ink-muted', dot: 'bg-ink-faint' },
  { severity: 'info', level: 'P4', tone: 'text-ink-faint', dot: 'bg-line' },
] as const

export function PriorityBoard({
  data,
  className,
}: {
  data: SeverityBreakdownEntry[]
  className?: string
}) {
  const { t } = useI18n()
  const counts = new Map(data.map((entry) => [entry.severity, entry.count]))
  const total = data.reduce((sum, entry) => sum + entry.count, 0)

  return (
    <section
      aria-labelledby="priority-board-title"
      className={cn('rounded-md border border-line bg-surface p-panel', className)}
      data-testid="priority-board"
    >
      <div className="flex items-baseline justify-between gap-tight">
        <h2 id="priority-board-title" className="text-sm font-medium text-ink">
          {t('priority.title')}
        </h2>
        <span className="mono-ui tabular text-ink-faint" data-numeric>
          {t('priority.total', { n: total })}
        </span>
      </div>

      {total === 0 ? (
        <p className="mt-panel text-xs text-ink-faint">{t('priority.empty')}</p>
      ) : (
        <ul className="mt-panel flex flex-col">
          {PRIORITY_ROWS.map((row) => {
            const count = counts.get(row.severity) ?? 0
            const meta = SEVERITY_META[row.severity]
            return (
              <li
                key={row.level}
                className="flex items-center gap-tight border-b border-line/60 py-1.5 last:border-b-0"
                data-priority={row.level}
              >
                <span
                  aria-hidden="true"
                  className={cn('h-1.5 w-1.5 shrink-0 rounded-full', row.dot, count === 0 && 'opacity-40')}
                />
                <span className="w-8 shrink-0 text-xs font-medium tabular text-ink-muted">
                  {row.level}
                </span>
                <span className={cn('flex-1 truncate text-xs', count === 0 ? 'text-ink-faint' : row.tone)}>
                  {t(`priority.${row.level.toLowerCase()}`)}
                  <span className="ml-1.5 text-ink-faint">{meta.label}</span>
                </span>
                <span className="mono-ui tabular text-xs text-ink" data-numeric>
                  {count}
                </span>
                <Link
                  to={`/findings?severity=${row.severity}`}
                  className="shrink-0 rounded-xs px-1.5 py-0.5 text-2xs text-ink-faint transition-colors hover:bg-surface-sunken hover:text-ink"
                  aria-label={`${row.level} ${meta.label}`}
                >
                  {t('priority.open')}
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default PriorityBoard
