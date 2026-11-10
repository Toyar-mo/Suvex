import type { Finding } from '@/data/finding'

const SEVERITY_CLASS: Record<string, string> = {
  critical: 'text-signal-risk',
  high: 'text-signal-warn',
  medium: 'text-signal-warn',
  low: 'text-ink-muted',
  info: 'text-ink-faint',
}

/**
 * 最近发现（学 ARTEX 总览的「发现」卡）。
 *
 * 只展示前 5 条；点击去 Findings 页。数据来自 findings-store，
 * 由总览页在挂载时静默加载一次。
 */
export function RecentFindingsCard({ findings }: { findings: Finding[] }) {
  const rows = [...findings]
    .sort((a, b) => (b.discoveredAt ?? '').localeCompare(a.discoveredAt ?? ''))
    .slice(0, 5)

  return (
    <section className="rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h3 className="text-sm font-medium text-ink">发现</h3>
        <a
          href="/findings"
          className="text-2xs text-ink-muted underline-offset-2 hover:text-ink hover:underline"
        >
          全部 ↗
        </a>
      </div>

      {rows.length === 0 ? (
        <p className="px-4 py-8 text-center text-sm text-ink-muted">暂无发现</p>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((finding) => (
            <li key={finding.id} className="flex flex-col gap-0.5 px-4 py-2">
              <div className="flex items-center justify-between gap-2">
                <a
                  href={`/findings/${finding.id}`}
                  className="min-w-0 flex-1 truncate text-sm text-ink hover:underline"
                >
                  {finding.title}
                </a>
                <span className={`shrink-0 text-2xs ${SEVERITY_CLASS[finding.severity] ?? 'text-ink-muted'}`}>
                  {finding.severity}
                </span>
              </div>
              <span className="truncate font-mono text-2xs text-ink-faint">{finding.target}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
