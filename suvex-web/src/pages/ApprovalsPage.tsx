import { PageHeader } from '@/components/layout/AppShell'
import EmptyState from '@/components/ui/EmptyState'
import { ClipboardCheck } from 'lucide-react'
import { loadApprovals, type ChatApproval } from '@/data/approvals'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { useEffect, useState } from 'react'

const STATUS_KEYS: Record<string, string> = {
  pending: 'approvals.status.pending',
  approved: 'approvals.status.approved',
  rejected: 'approvals.status.rejected',
}

export function ApprovalsPage() {
  const { t, locale } = useI18n()
  const [items, setItems] = useState<ChatApproval[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    void loadApprovals()
      .then((rows) => {
        if (alive) setItems(rows)
      })
      .catch(() => {})
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  return (
    <>
      <PageHeader navId="approvals" />
      <div className="p-panel">
        <section className="rounded-md border border-line bg-surface">
          <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="text-sm font-medium text-ink"><T k="approvals.title" /></h2>
            <span className="text-2xs text-ink-faint">{t('approvals.count', { n: items.length })}</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted"><T k="approvals.loading" /></p>
          ) : items.length === 0 ? (
            <EmptyState icon={ClipboardCheck} title={t('approvals.empty.title')} description={t('approvals.empty.desc')} />
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal"><T k="approvals.col.session" /></th>
                  <th className="px-3 py-2 font-normal"><T k="approvals.col.tool" /></th>
                  <th className="px-3 py-2 font-normal"><T k="approvals.col.args" /></th>
                  <th className="px-3 py-2 font-normal"><T k="approvals.col.status" /></th>
                  <th className="px-3 py-2 font-normal"><T k="approvals.col.decidedBy" /></th>
                  <th className="px-3 py-2 font-normal"><T k="approvals.col.time" /></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-t border-line align-top transition-colors hover:bg-surface-raised/40">
                    <td className="max-w-[14rem] truncate px-4 py-2 text-ink">
                      <a href={`/chat/${item.sessionId}`} className="hover:underline">
                        {item.sessionTitle || item.sessionId}
                      </a>
                    </td>
                    <td className="px-3 py-2 font-mono text-2xs text-ink-muted">{item.toolName}</td>
                    <td className="max-w-[22rem] truncate px-3 py-2 font-mono text-2xs text-ink-faint">
                      {JSON.stringify(item.toolArgs).slice(0, 120)}
                    </td>
                    <td className="px-3 py-2 text-2xs text-ink">{STATUS_KEYS[item.status] ? t(STATUS_KEYS[item.status]) : item.status}</td>
                    <td className="px-3 py-2 text-2xs text-ink-muted">{item.decidedBy || '—'}</td>
                    <td className="px-3 py-2 text-2xs text-ink-faint">
                      {item.decidedAt
                        ? new Date(item.decidedAt).toLocaleString(locale, { hour12: false })
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  )
}

export default ApprovalsPage
