import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import {
  createIntranetSession,
  deleteIntranetSession,
  loadIntranetSessions,
  patchIntranetSession,
  type IntranetSession,
} from '@/data/intranet'
import { showToast } from '@/data/toast-store'
import { Network } from 'lucide-react'
import EmptyState from '@/components/ui/EmptyState'
import { Select } from '@/components/ui/Select'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { useEffect, useState } from 'react'

const KINDS = ['webshell', 'reverse', 'ssh'] as const
const STATUSES = ['alive', 'dead', 'unknown'] as const

export function IntranetPage() {
  const { t } = useI18n()
  const statusLabel = (status: string) => t(`intranet.status.${status}`)
  const [items, setItems] = useState<IntranetSession[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState({
    kind: 'ssh',
    host: '',
    username: '',
    secret: '',
    notes: '',
    status: 'unknown',
  })
  const [saving, setSaving] = useState(false)

  async function reload() {
    try {
      setItems(await loadIntranetSessions())
    } catch {
      // 增值数据：失败静默
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // 异步读取后写状态：数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [])

  async function create() {
    if (!form.host.trim() || !form.secret.trim()) return
    setSaving(true)
    try {
      await createIntranetSession(form)
      setForm({ kind: 'ssh', host: '', username: '', secret: '', notes: '', status: 'unknown' })
      await reload()
      showToast({ tone: 'ok', message: t('intranet.toast.created') })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : t('intranet.toast.createFailed') })
    } finally {
      setSaving(false)
    }
  }

  async function setStatus(item: IntranetSession, status: string) {
    try {
      await patchIntranetSession(item.id, { status })
      await reload()
    } catch {
      showToast({ tone: 'risk', message: t('intranet.toast.updateFailed') })
    }
  }

  async function remove(id: string) {
    try {
      await deleteIntranetSession(id)
      await reload()
    } catch {
      showToast({ tone: 'risk', message: t('intranet.toast.deleteFailed') })
    }
  }

  return (
    <>
      <PageHeader navId="intranet" />
      <div className="flex flex-col gap-panel p-panel">
        <section className="rounded-md border border-line bg-surface p-panel">
          <h2 className="text-sm font-medium text-ink"><T k="intranet.form.title" /></h2>
          <p className="mt-1 text-2xs text-ink-muted">
            <T k="intranet.form.desc" />
          </p>
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-6">
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="intranet.field.kind" /></span>
              <Select
                value={form.kind}
                options={KINDS.map((kind) => ({ value: kind, label: kind }))}
                onChange={(kind) => setForm({ ...form, kind })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm md:col-span-2">
              <span><T k="intranet.field.host" /></span>
              <TextInput id="isess-host" value={form.host} onChange={(v) => setForm({ ...form, host: v })} />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="intranet.field.username" /></span>
              <TextInput id="isess-user" value={form.username} onChange={(v) => setForm({ ...form, username: v })} />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="intranet.field.secret" /></span>
              <TextInput
                id="isess-secret"
                type="password"
                value={form.secret}
                onChange={(v) => setForm({ ...form, secret: v })}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="intranet.field.status" /></span>
              <Select
                value={form.status}
                options={STATUSES.map((status) => ({ value: status, label: statusLabel(status) }))}
                onChange={(status) => setForm({ ...form, status })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm md:col-span-6">
              <span><T k="intranet.field.notes" /></span>
              <TextInput id="isess-notes" value={form.notes} onChange={(v) => setForm({ ...form, notes: v })} />
            </label>
          </div>
          <div className="mt-3">
            <Button variant="primary" size="sm" onClick={() => void create()} disabled={saving}>
              {saving ? t('intranet.saving') : t('intranet.create')}
            </Button>
          </div>
        </section>

        <section className="rounded-md border border-line bg-surface">
          <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="text-sm font-medium text-ink"><T k="intranet.table.title" /></h2>
            <span className="text-2xs text-ink-faint">{t('intranet.table.count', { n: items.length })}</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted"><T k="intranet.loading" /></p>
          ) : items.length === 0 ? (
            <EmptyState icon={Network} title={t('intranet.empty.title')} description={t('intranet.empty.desc')} />
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal"><T k="intranet.field.kind" /></th>
                  <th className="px-3 py-2 font-normal"><T k="intranet.field.host" /></th>
                  <th className="px-3 py-2 font-normal"><T k="intranet.field.username" /></th>
                  <th className="px-3 py-2 font-normal"><T k="intranet.field.status" /></th>
                  <th className="px-3 py-2 font-normal"><T k="intranet.field.notes" /></th>
                  <th className="px-3 py-2 font-normal"><T k="intranet.actions" /></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-t border-line align-top transition-colors hover:bg-surface-raised/40">
                    <td className="px-4 py-2 text-2xs text-ink-muted">{item.kind}</td>
                    <td className="max-w-[18rem] truncate px-3 py-2 font-mono text-xs text-ink">{item.host}</td>
                    <td className="px-3 py-2 text-2xs text-ink-muted">{item.username || '—'}</td>
                    <td className="px-3 py-2 text-2xs">
                      <Select
                        size="sm"
                        value={item.status}
                        options={STATUSES.map((status) => ({ value: status, label: statusLabel(status) }))}
                        onChange={(status) => void setStatus(item, status)}
                        className="h-7 w-24 border-0 bg-transparent px-1 text-2xs hover:bg-surface-sunken"
                      />
                    </td>
                    <td className="max-w-[18rem] truncate px-3 py-2 text-2xs text-ink-faint">{item.notes || '—'}</td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        className="text-2xs text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                        onClick={() => void remove(item.id)}
                      >
                        <T k="intranet.delete" />
                      </button>
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

export default IntranetPage
