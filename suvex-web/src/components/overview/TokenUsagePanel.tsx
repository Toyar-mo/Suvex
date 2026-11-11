import { memo, useEffect, useMemo, useState } from 'react'
import { T } from '@/i18n/T'
import { Link } from 'react-router-dom'
import { loadTokenUsage, type TokenUsageSeries } from '@/data/tokenUsage'
import { cn } from '@/lib/cn'

const RANGES = [
  { days: 7, label: '7天' },
  { days: 30, label: '30天' },
  { days: 90, label: '3月' },
  { days: 180, label: '6月' },
  { days: 365, label: '一年' },
] as const

function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

/**
 * LLM Token 消耗面板（对齐 ARTEX 总览的「新版」）。
 *
 * 左侧是分项与命中率，右侧是按天折线。**没有缓存列**时明确标注
 * `cacheTracked=false`：缓存读恒 0，命中率显示 `—`，不估算。
 */
export const TokenUsagePanel = memo(function TokenUsagePanel() {
  const [days, setDays] = useState<number>(30)
  const [data, setData] = useState<TokenUsageSeries | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let alive = true
    void loadTokenUsage(days)
      .then((value) => {
        if (alive) {
          setData(value)
          setError(false)
        }
      })
      .catch(() => {
        if (alive) setError(true)
      })
    return () => {
      alive = false
    }
  }, [days])

  const chart = useMemo(() => {
    if (!data || data.totals.total === 0) return null
    const points = data.series
    const max = Math.max(1, ...points.map((point) => Math.max(point.prompt, point.completion)))
    const x = (index: number) => 12 + (index * 576) / Math.max(1, points.length - 1)
    const y = (value: number) => 188 - (value / max) * 168
    const line = (key: 'prompt' | 'completion') =>
      points.map((point, index) => `${x(index)},${y(point[key])}`).join(' ')
    return { line, count: points.length }
  }, [data])

  return (
    <section className="rounded-lg border border-line bg-surface p-panel">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-ink"><T k="overview.token.trend"/></h3>
        <span className="text-2xs text-ink-faint">activity</span>
        <Link
          to="/kernel"
          className="ml-auto rounded-sm bg-ink px-2.5 py-1 text-2xs text-canvas"
        >
          全部
        </Link>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-panel lg:grid-cols-[18rem_minmax(0,1fr)]">
          <div className="flex flex-col gap-2 text-sm">
            <span className="text-2xs text-ink-muted"><T k="overview.token.total"/></span>
            <span className="tabular text-3xl font-medium text-ink">
              {compact(data?.totals.total ?? 0)}
            </span>
            <span className="text-2xs text-ink-faint">
              {data?.totals.calls ?? 0} 个任务
              {data && data.totals.unreportedCalls > 0
                ? ` · ${data.totals.unreportedCalls} 次未上报`
                : ''}
            </span>

            {([
              ['输入（未命中）', data?.totals.prompt ?? 0, 'bg-accent'],
              [
                data?.cacheTracked ? '缓存命中' : '缓存命中（未记录）',
                data?.cacheRead ?? 0,
                'bg-signal-ok',
              ],
              ['输出', data?.totals.completion ?? 0, 'bg-signal-warn'],
            ] as const).map(([label, value, tone]) => {
              const total = data?.totals.total ?? 0
              const width = total > 0 ? Math.min(100, (value / total) * 100) : 0
              return (
                <div key={label} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-2xs text-ink-muted">{label}</span>
                    <span className="tabular text-2xs text-ink">{compact(value)}</span>
                  </div>
                  <div className="h-1 overflow-hidden rounded-pill bg-surface-raised">
                    <div className={cn('h-full rounded-pill', tone)} style={{ width: `${width}%` }} />
                  </div>
                </div>
              )
            })}

            <div className="mt-1 flex items-center justify-between rounded-sm border border-line px-2 py-1.5 text-2xs">
              <span className="text-ink-muted"><T k="overview.token.cache"/></span>
              <span className="tabular text-signal-warn">
                {data?.cacheRate === null || data?.cacheRate === undefined
                  ? '—'
                  : `${Math.round(data.cacheRate * 100)}%`}
              </span>
            </div>
          </div>

          <div className="relative min-h-[220px] rounded-sm border border-line">
            <div className="flex items-center gap-3 px-3 pt-2 text-2xs text-ink-muted">
              <span className="flex items-center gap-1">
                <span className="size-1.5 rounded-full bg-accent" /> 输入
              </span>
              <span className="flex items-center gap-1">
                <span className="size-1.5 rounded-full bg-signal-warn" /> 输出
              </span>
              <span className="flex items-center gap-1">
                <span className="size-1.5 rounded-full bg-signal-ok" /> 缓存读
              </span>
              <span className="ml-auto flex gap-1">
                {RANGES.map((range) => (
                  <button
                    key={range.days}
                    type="button"
                    onClick={() => setDays(range.days)}
                    className={cn(
                      'rounded-sm border px-1.5 py-0.5',
                      days === range.days
                        ? 'border-line-strong bg-surface-raised text-ink'
                        : 'border-line text-ink-faint hover:text-ink',
                    )}
                  >
                    {range.label}
                  </button>
                ))}
              </span>
            </div>

            {chart ? (
              <svg viewBox="0 0 600 200" className="h-[200px] w-full" role="img" aria-label="token 趋势">
                <polyline
                  points={chart.line('prompt')}
                  fill="none"
                  stroke="rgb(var(--suvex-accent))"
                  strokeWidth="2"
                />
                <polyline
                  points={chart.line('completion')}
                  fill="none"
                  stroke="rgb(var(--suvex-signal-warn))"
                  strokeWidth="2"
                />
              </svg>
            ) : (
              <p className="flex h-[200px] items-center justify-center text-sm text-ink-muted">
                {error ? '加载失败' : '暂无数据'}
              </p>
            )}
          </div>
        </div>
    </section>
  )
})
