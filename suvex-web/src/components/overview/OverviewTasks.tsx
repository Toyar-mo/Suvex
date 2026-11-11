import { scanStatusLabel } from '@/data/scan'
import { T } from '@/i18n/T'
import { memo, useMemo } from 'react'
import type { Scan } from '@/data/scan'
import { cn } from '@/lib/cn'

const STATUS_CLASS: Record<string, string> = {
  running: 'text-accent-strong',
  queued: 'text-ink-muted',
  completed: 'text-signal-ok',
  failed: 'text-signal-risk',
  aborted: 'text-signal-risk',
}

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return '—'
  return at.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 总览的任务表（学 ARTEX 总览的「任务」块）。
 *
 * 只列最近 5 条；「全部」跳扫描页。数据来自全局 scans-store，
 * 不额外发请求 —— 总览已经每 60 秒轮询一次 `/overview`，
 * 再单独拉一份任务列表会让两块数字在刷新窗口内互相打架。
 */
export const OverviewTasks = memo(function OverviewTasks({ scans }: { scans: Scan[] }) {
  const rows = useMemo(
    () => [...scans].sort((a, b) => (b.startedAt ?? '').localeCompare(a.startedAt ?? '')).slice(0, 5),
    [scans],
  )

  return (
    <section className="rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-ink">
          <span aria-hidden="true" className="text-ink-muted">
            ◷
          </span>
          任务
        </h3>
        <a href="/scans" className="text-2xs text-ink-muted underline-offset-2 hover:text-ink hover:underline">
          {scans.length} 个任务 · 全部 ↗
        </a>
      </div>

      {rows.length === 0 ? (
        <p className="px-4 py-8 text-center text-sm text-ink-muted"><T k="overview.tasks.none"/></p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-2xs text-ink-faint">
              <th className="px-4 py-2 font-normal"><T k="overview.tasks.task"/></th>
              <th className="px-3 py-2 font-normal"><T k="overview.tasks.status"/></th>
              <th className="px-3 py-2 font-normal"><T k="overview.tasks.progress"/></th>
              <th className="px-3 py-2 font-normal"><T k="overview.tasks.recent"/></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((scan) => (
              <tr key={scan.id} className="border-t border-line">
                <td className="max-w-[18rem] truncate px-4 py-2 text-ink">
                  <a href={`/scans/${scan.id}`} className="hover:underline">
                    {scan.name || scan.target}
                  </a>
                </td>
                <td className={cn('px-3 py-2 text-xs', STATUS_CLASS[scan.status] ?? 'text-ink-muted')}>
                  {scanStatusLabel(scan)}
                </td>
                <td className="px-3 py-2">
                  {scan.progress === null ? (
                    <span className="text-ink-faint">—</span>
                  ) : (
                    <div className="flex items-center gap-2">
                      <div className="h-1 w-24 overflow-hidden rounded-pill bg-surface-raised">
                        <div
                          className="h-full rounded-pill bg-accent"
                          style={{ width: `${Math.round(scan.progress * 100)}%` }}
                        />
                      </div>
                      <span className="tabular text-2xs text-ink-muted">
                        {Math.round(scan.progress * 100)}%
                      </span>
                    </div>
                  )}
                </td>
                <td className="px-3 py-2 text-2xs text-ink-faint">
                  {formatWhen(scan.finishedAt ?? scan.startedAt)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
})
