import { PageHeader } from '@/components/layout/AppShell'
import { loadApprovals, type ChatApproval } from '@/data/approvals'
import { useEffect, useState } from 'react'

const STATUS_LABEL: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
}

export function ApprovalsPage() {
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
            <h2 className="text-sm font-medium text-ink">审批记录</h2>
            <span className="text-2xs text-ink-faint">{items.length} 条</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted">读取中…</p>
          ) : items.length === 0 ? (
            <p className="px-4 py-6 text-sm text-ink-muted">还没有审批记录。</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal">会话</th>
                  <th className="px-3 py-2 font-normal">工具</th>
                  <th className="px-3 py-2 font-normal">参数</th>
                  <th className="px-3 py-2 font-normal">状态</th>
                  <th className="px-3 py-2 font-normal">决定人</th>
                  <th className="px-3 py-2 font-normal">时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-t border-line align-top">
                    <td className="max-w-[14rem] truncate px-4 py-2 text-ink">
                      <a href={`/chat/${item.sessionId}`} className="hover:underline">
                        {item.sessionTitle || item.sessionId}
                      </a>
                    </td>
                    <td className="px-3 py-2 font-mono text-2xs text-ink-muted">{item.toolName}</td>
                    <td className="max-w-[22rem] truncate px-3 py-2 font-mono text-2xs text-ink-faint">
                      {JSON.stringify(item.toolArgs)}
                    </td>
                    <td className="px-3 py-2 text-2xs text-ink">{STATUS_LABEL[item.status] ?? item.status}</td>
                    <td className="px-3 py-2 text-2xs text-ink-muted">{item.decidedBy || '—'}</td>
                    <td className="px-3 py-2 text-2xs text-ink-faint">
                      {item.decidedAt
                        ? new Date(item.decidedAt).toLocaleString('zh-CN', { hour12: false })
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
