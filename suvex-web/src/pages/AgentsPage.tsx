import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import { createAgent, deleteAgent, loadAgents, type AgentProfile } from '@/data/agents'
import { showToast } from '@/data/toast-store'
import { Bot, Plus } from 'lucide-react'
import { useEffect, useState } from 'react'

function splitList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

export function AgentsPage() {
  const [items, setItems] = useState<AgentProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({
    key: '',
    name: '',
    description: '',
    mcpServers: '',
    skills: '',
    tools: '',
  })

  async function reload() {
    try {
      setItems(await loadAgents())
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

  async function submit() {
    if (!form.key.trim() || !form.name.trim()) return
    setSaving(true)
    try {
      await createAgent({
        key: form.key.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        mcpServers: splitList(form.mcpServers),
        skills: splitList(form.skills),
        tools: splitList(form.tools),
      })
      setForm({ key: '', name: '', description: '', mcpServers: '', skills: '', tools: '' })
      setCreating(false)
      await reload()
      showToast({ tone: 'ok', message: 'Agent 已创建' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '创建失败' })
    } finally {
      setSaving(false)
    }
  }

  async function remove(item: AgentProfile) {
    try {
      await deleteAgent(item.id)
      await reload()
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '删除失败' })
    }
  }

  return (
    <>
      <PageHeader navId="agents" />
      <div className="flex flex-col gap-panel p-panel">
        <section className="rounded-md border border-line bg-surface p-panel">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-medium text-ink">Agent 清单</h2>
              <p className="mt-0.5 text-2xs text-ink-muted">
                共 {items.length} 个 · 内置只读，自定义可增删
              </p>
            </div>
            <Button variant="primary" size="sm" onClick={() => setCreating((v) => !v)}>
              <Plus className="size-3.5" strokeWidth={2} />
              新建 Agent
            </Button>
          </div>

          {creating && (
            <div className="mt-3 grid grid-cols-1 gap-3 border-t border-line pt-3 md:grid-cols-3">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">key</span>
                <TextInput id="agent-key" value={form.key} onChange={(v) => setForm({ ...form, key: v })} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">名称</span>
                <TextInput id="agent-name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">描述</span>
                <TextInput id="agent-desc" value={form.description} onChange={(v) => setForm({ ...form, description: v })} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">MCP（逗号分隔）</span>
                <TextInput id="agent-mcp" value={form.mcpServers} onChange={(v) => setForm({ ...form, mcpServers: v })} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">Skill（逗号分隔）</span>
                <TextInput id="agent-skills" value={form.skills} onChange={(v) => setForm({ ...form, skills: v })} />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">工具（逗号分隔）</span>
                <TextInput id="agent-tools" value={form.tools} onChange={(v) => setForm({ ...form, tools: v })} />
              </label>
              <div className="md:col-span-3">
                <Button variant="primary" size="sm" onClick={() => void submit()} disabled={saving}>
                  {saving ? '保存中…' : '保存'}
                </Button>
              </div>
            </div>
          )}
        </section>

        {loading ? (
          <p className="text-sm text-ink-muted">读取中…</p>
        ) : (
          <div className="grid grid-cols-1 gap-panel md:grid-cols-2 xl:grid-cols-3">
            {items.map((item) => (
              <section key={item.id} className="flex flex-col gap-2 rounded-md border border-line bg-surface p-panel">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <Bot className="size-4 shrink-0 text-ink-muted" strokeWidth={1.75} />
                    <span className="truncate text-sm text-ink">{item.name}</span>
                    <code className="truncate font-mono text-2xs text-ink-faint">{item.key}</code>
                  </div>
                  <span className="shrink-0 rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-muted">
                    {item.builtin ? '内置' : '自定义'}
                  </span>
                </div>
                <p className="min-h-[2.5rem] text-2xs leading-relaxed text-ink-muted">
                  {item.description || '（没有写描述）'}
                </p>
                <div className="mt-auto flex items-center gap-2 text-2xs text-ink-faint">
                  <span>MCP {item.mcpServers.length}</span>
                  <span>Skill {item.skills.length}</span>
                  <span>工具 {item.tools.length}</span>
                  {!item.builtin && (
                    <button
                      type="button"
                      onClick={() => void remove(item)}
                      className="ml-auto text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                    >
                      删除
                    </button>
                  )}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

export default AgentsPage
