import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import { createCredential, loadCredentials, loadReusePlan, revealCredential } from '@/data/credentials'
import type { Credential, ReusePlan } from '@/data/credentials'
import { showToast } from '@/data/toast-store'
import { useEffect, useState } from 'react'

const KINDS = ['password', 'hash', 'ticket', 'key'] as const

export function CredentialsPage() {
  const [items, setItems] = useState<Credential[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState({ kind: 'password', username: '', secret: '', target: '', source: '' })
  const [saving, setSaving] = useState(false)
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [plans, setPlans] = useState<Record<string, ReusePlan>>({})

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
      await createCredential(form)
      setForm({ kind: 'password', username: '', secret: '', target: '', source: '' })
      await reload()
      showToast({ tone: 'ok', message: '凭据已保存（加密）' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '保存失败' })
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
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '读取失败' })
    }
  }

  async function plan(id: string) {
    try {
      const out = await loadReusePlan(id)
      setPlans((prev) => ({ ...prev, [id]: out }))
    } catch {
      showToast({ tone: 'risk', message: '评分失败' })
    }
  }

  return (
    <>
      <PageHeader navId="credentials" />
      <div className="flex flex-col gap-panel p-panel">
        <section className="rounded-md border border-line bg-surface p-panel">
          <h2 className="text-sm font-medium text-ink">登记凭据</h2>
          <p className="mt-1 text-2xs text-ink-muted">
            明文只用于加密存储；列表不回显，读取由管理员触发并写审计。
          </p>
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-6">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">类型</span>
              <select
                value={form.kind}
                onChange={(event) => setForm({ ...form, kind: event.target.value })}
                className="h-9 rounded-sm border border-line bg-surface px-2 text-sm"
              >
                {KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">用户名</span>
              <TextInput id="cred-user" value={form.username} onChange={(v) => setForm({ ...form, username: v })} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">目标</span>
              <TextInput id="cred-target" value={form.target} onChange={(v) => setForm({ ...form, target: v })} />
            </label>
            <label className="flex flex-col gap-1 text-sm md:col-span-2">
              <span className="text-ink-muted">凭据</span>
              <TextInput
                id="cred-secret"
                type="password"
                value={form.secret}
                onChange={(v) => setForm({ ...form, secret: v })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">来源</span>
              <TextInput id="cred-source" value={form.source} onChange={(v) => setForm({ ...form, source: v })} />
            </label>
          </div>
          <div className="mt-3">
            <Button variant="primary" size="sm" onClick={() => void submit()} disabled={saving}>
              {saving ? '保存中…' : '保存凭据'}
            </Button>
          </div>
        </section>

        <section className="rounded-md border border-line bg-surface">
          <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="text-sm font-medium text-ink">凭据台账</h2>
            <span className="text-2xs text-ink-faint">{items.length} 条</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted">读取中…</p>
          ) : items.length === 0 ? (
            <p className="px-4 py-6 text-sm text-ink-muted">还没有凭据。</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal">目标</th>
                  <th className="px-3 py-2 font-normal">类型</th>
                  <th className="px-3 py-2 font-normal">用户名</th>
                  <th className="px-3 py-2 font-normal">来源</th>
                  <th className="px-3 py-2 font-normal">复用</th>
                  <th className="px-3 py-2 font-normal">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const itemPlan = plans[item.id]
                  return (
                    <tr key={item.id} className="border-t border-line align-top">
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
                            评分
                          </button>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        {revealed[item.id] ? (
                          <code className="font-mono text-2xs text-signal-warn">{revealed[item.id]}</code>
                        ) : (
                          <button
                            type="button"
                            className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                            onClick={() => void reveal(item.id)}
                          >
                            显示明文
                          </button>
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
