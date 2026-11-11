import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import {
  createCredential,
  deleteCredential,
  loadCredentials,
  loadReusePlan,
  revealCredential,
  updateCredential,
} from '@/data/credentials'
import type { Credential, ReusePlan } from '@/data/credentials'
import { showToast } from '@/data/toast-store'
import { KeyRound } from 'lucide-react'
import EmptyState from '@/components/ui/EmptyState'
import { Select } from '@/components/ui/Select'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { useEffect, useState } from 'react'

const KINDS = ['password', 'hash', 'ticket', 'key'] as const

export function CredentialsPage() {
  const { t } = useI18n()
  const [items, setItems] = useState<Credential[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState({ kind: 'password', username: '', secret: '', target: '', source: '' })
  const [saving, setSaving] = useState(false)
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [plans, setPlans] = useState<Record<string, ReusePlan>>({})
  const [editingId, setEditingId] = useState<string | null>(null)

  async function reload() {
    try {
      setItems(await loadCredentials())
    } catch {
      // 增值数据：失败静默，列表保持空
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // 异步读取后写状态：数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [])

  async function submit() {
    if (!form.target.trim() || !form.secret.trim()) return
    setSaving(true)
    try {
      if (editingId) {
        await updateCredential(editingId, {
          kind: form.kind,
          username: form.username,
          target: form.target,
          source: form.source,
          ...(form.secret ? { secret: form.secret } : {}),
        })
        setEditingId(null)
      } else {
        await createCredential(form)
      }
      setForm({ kind: 'password', username: '', secret: '', target: '', source: '' })
      await reload()
      showToast({ tone: 'ok', message: editingId ? t('credentials.toast.updated') : t('credentials.toast.created') })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : t('credentials.toast.saveFailed') })
    } finally {
      setSaving(false)
    }
  }

  async function reveal(id: string) {
    try {
      const out = await revealCredential(id)
      setRevealed((prev) => ({ ...prev, [id]: out.secret }))
      window.setTimeout(
        () => setRevealed((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        }),
        20_000,
      )
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : t('credentials.toast.readFailed') })
    }
  }

  async function remove(id: string) {
    try {
      await deleteCredential(id)
      await reload()
      showToast({ tone: 'ok', message: t('credentials.toast.deleted') })
    } catch {
      showToast({ tone: 'risk', message: t('credentials.toast.deleteFailed') })
    }
  }

  async function plan(id: string) {
    try {
      const out = await loadReusePlan(id)
      setPlans((prev) => ({ ...prev, [id]: out }))
    } catch {
      showToast({ tone: 'risk', message: t('credentials.toast.planFailed') })
    }
  }

  return (
    <>
      <PageHeader navId="credentials" />
      <div className="flex flex-col gap-panel p-panel">
        <section className="rounded-md border border-line bg-surface p-panel">
          <h2 className="text-sm font-medium text-ink"><T k="credentials.form.title" /></h2>
          <p className="mt-1 text-2xs text-ink-muted">
            <T k="credentials.form.desc" />
          </p>
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-6">
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="credentials.field.kind" /></span>
              <Select
                value={form.kind}
                options={KINDS.map((kind) => ({ value: kind, label: kind }))}
                onChange={(kind) => setForm({ ...form, kind })}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="credentials.field.username" /></span>
              <TextInput id="cred-user" value={form.username} onChange={(v) => setForm({ ...form, username: v })} />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="credentials.field.target" /></span>
              <TextInput id="cred-target" value={form.target} onChange={(v) => setForm({ ...form, target: v })} />
            </label>
            <label className="flex flex-col gap-1 text-sm md:col-span-2">
              <span><T k="credentials.field.secret" /></span>
              <TextInput
                id="cred-secret"
                type="password"
                value={form.secret}
                onChange={(v) => setForm({ ...form, secret: v })}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
              <span><T k="credentials.field.source" /></span>
              <TextInput id="cred-source" value={form.source} onChange={(v) => setForm({ ...form, source: v })} />
            </label>
          </div>
          <div className="mt-3">
            <Button variant="primary" size="sm" onClick={() => void submit()} disabled={saving}>
              {saving ? t('credentials.saving') : editingId ? t('credentials.saveChanges') : t('credentials.save')}
            </Button>
            {editingId && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setEditingId(null)
                  setForm({ kind: 'password', username: '', secret: '', target: '', source: '' })
                }}
              >
                <T k="credentials.cancel" />
              </Button>
            )}
          </div>
        </section>

        <section className="rounded-md border border-line bg-surface">
          <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="text-sm font-medium text-ink"><T k="credentials.table.title" /></h2>
            <span className="text-2xs text-ink-faint">{t('credentials.table.count', { n: items.length })}</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted"><T k="credentials.loading" /></p>
          ) : items.length === 0 ? (
            <EmptyState icon={KeyRound} title={t('credentials.empty.title')} description={t('credentials.empty.desc')} />
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal"><T k="credentials.field.target" /></th>
                  <th className="px-3 py-2 font-normal"><T k="credentials.field.kind" /></th>
                  <th className="px-3 py-2 font-normal"><T k="credentials.field.username" /></th>
                  <th className="px-3 py-2 font-normal"><T k="credentials.field.source" /></th>
                  <th className="px-3 py-2 font-normal"><T k="credentials.table.reuse" /></th>
                  <th className="px-3 py-2 font-normal"><T k="credentials.actions" /></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const itemPlan = plans[item.id]
                  return (
                    <tr key={item.id} className="border-t border-line align-top transition-colors hover:bg-surface-raised/40">
                      <td className="max-w-[18rem] truncate px-4 py-2 font-mono text-xs text-ink">
                        {item.target}
                      </td>
                      <td className="px-3 py-2 text-2xs text-ink-muted">{item.kind}</td>
                      <td className="px-3 py-2 text-2xs text-ink-muted">{item.username || '—'}</td>
                      <td className="px-3 py-2 text-2xs text-ink-faint">{item.source || '—'}</td>
                      <td className="px-3 py-2 text-2xs">
                        {itemPlan ? (
                          <span title={itemPlan.basis.join('；')} className="text-ink">
                            {Math.round(itemPlan.score * 100)}%
                          </span>
                        ) : (
                          <button
                            type="button"
                            className="text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                            onClick={() => void plan(item.id)}
                          >
                            <T k="credentials.score" />
                          </button>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        {revealed[item.id] ? (
                          <code className="font-mono text-2xs text-signal-warn">{revealed[item.id]}</code>
                        ) : (
                          <span className="flex items-center gap-2">
                            <button
                              type="button"
                              className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                              onClick={() => void reveal(item.id)}
                            >
                              <T k="credentials.reveal" />
                            </button>
                            <button
                              type="button"
                              className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                              onClick={() => {
                                setEditingId(item.id)
                                setForm({
                                  kind: item.kind,
                                  username: item.username,
                                  secret: '',
                                  target: item.target,
                                  source: item.source,
                                })
                              }}
                            >
                              <T k="credentials.edit" />
                            </button>
                            <button
                              type="button"
                              className="text-2xs text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                              onClick={() => void remove(item.id)}
                            >
                              <T k="credentials.delete" />
                            </button>
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  )
}

export default CredentialsPage
