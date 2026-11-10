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
import { useEffect, useState } from 'react'

const KINDS = ['webshell', 'reverse', 'ssh'] as const
const STATUSES = ['alive', 'dead', 'unknown'] as const
const STATUS_LABEL = { alive: '存活', dead: '失效', unknown: '未知' }

export function IntranetPage() {
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
      showToast({ tone: 'ok', message: '会话已登记（凭据加密）' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '登记失败' })
    } finally {
      setSaving(false)
    }
  }

  async function setStatus(item: IntranetSession, status: string) {
    try {
      await patchIntranetSession(item.id, { status })
      await reload()
    } catch {
      showToast({ tone: 'risk', message: '更新失败' })
    }
  }

  async function remove(id: string) {
    try {
      await deleteIntranetSession(id)
      await reload()
    } catch {
      showToast({ tone: 'risk', message: '删除失败' })
    }
  }

  return (
    <>
      <PageHeader navId="intranet" />
      <div className="flex flex-col gap-panel p-panel">
        <section className="rounded-md border border-line bg-surface p-panel">
          <h2 className="text-sm font-medium text-ink">登记立足点</h2>
          <p className="mt-1 text-2xs text-ink-muted">
            记录 webshell / 反弹 / ssh 会话。平台只做台账，不自动连接或执行命令。
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
            <label className="flex flex-col gap-1 text-sm md:col-span-2">
              <span className="text-ink-muted">地址</span>
              <TextInput id="isess-host" value={form.host} onChange={(v) => setForm({ ...form, host: v })} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">用户名</span>
              <TextInput id="isess-user" value={form.username} onChange={(v) => setForm({ ...form, username: v })} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">凭据</span>
              <TextInput
                id="isess-secret"
                type="password"
                value={form.secret}
                onChange={(v) => setForm({ ...form, secret: v })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">状态</span>
              <select
                value={form.status}
                onChange={(event) => setForm({ ...form, status: event.target.value })}
                className="h-9 rounded-sm border border-line bg-surface px-2 text-sm"
              >
                {STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {STATUS_LABEL[status]}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm md:col-span-6">
              <span className="text-ink-muted">备注</span>
              <TextInput id="isess-notes" value={form.notes} onChange={(v) => setForm({ ...form, notes: v })} />
            </label>
          </div>
          <div className="mt-3">
            <Button variant="primary" size="sm" onClick={() => void create()} disabled={saving}>
              {saving ? '保存中…' : '登记会话'}
            </Button>
          </div>
        </section>

        <section className="rounded-md border border-line bg-surface">
          <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 className="text-sm font-medium text-ink">会话台账</h2>
            <span className="text-2xs text-ink-faint">{items.length} 条</span>
          </header>
          {loading ? (
            <p className="px-4 py-6 text-sm text-ink-muted">读取中…</p>
          ) : items.length === 0 ? (
            <p className="px-4 py-6 text-sm text-ink-muted">还没有登记会话。</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-2xs text-ink-faint">
                  <th className="px-4 py-2 font-normal">类型</th>
                  <th className="px-3 py-2 font-normal">地址</th>
                  <th className="px-3 py-2 font-normal">用户名</th>
                  <th className="px-3 py-2 font-normal">状态</th>
                  <th className="px-3 py-2 font-normal">备注</th>
                  <th className="px-3 py-2 font-normal">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-t border-line align-top">
                    <td className="px-4 py-2 text-2xs text-ink-muted">{item.kind}</td>
                    <td className="max-w-[18rem] truncate px-3 py-2 font-mono text-xs text-ink">{item.host}</td>
                    <td className="px-3 py-2 text-2xs text-ink-muted">{item.username || '—'}</td>
                    <td className="px-3 py-2 text-2xs">
                      <select
                        value={item.status}
                        onChange={(event) => void setStatus(item, event.target.value)}
                        className="bg-transparent text-2xs text-ink"
                      >
                        {STATUSES.map((status) => (
                          <option key={status} value={status}>
                            {STATUS_LABEL[status]}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="max-w-[18rem] truncate px-3 py-2 text-2xs text-ink-faint">{item.notes || '—'}</td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        className="text-2xs text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                        onClick={() => void remove(item.id)}
                      >
                        删除
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
