import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import {
  createAgent,
  deleteAgent,
  loadAgentTemplates,
  loadAgents,
  updateAgent,
  type AgentProfile,
  type AgentTemplate,
} from '@/data/agents'
import { showToast } from '@/data/toast-store'
import { Bot, Plus, X } from 'lucide-react'
import EmptyState from '@/components/ui/EmptyState'
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
  const [templates, setTemplates] = useState<AgentTemplate[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [editing, setEditing] = useState<AgentProfile | null>(null)
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
      const [profiles, roles] = await Promise.all([loadAgents(), loadAgentTemplates()])
      setItems(profiles)
      setTemplates(roles)
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
      if (editing) {
        await updateAgent(editing.id, {
          name: form.name.trim(),
          description: form.description.trim(),
          mcpServers: splitList(form.mcpServers),
          skills: splitList(form.skills),
          tools: splitList(form.tools),
        })
        setEditing(null)
      } else {
      await createAgent({
        key: form.key.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        mcpServers: splitList(form.mcpServers),
        skills: splitList(form.skills),
        tools: splitList(form.tools),
      })
      }
      setForm({ key: '', name: '', description: '', mcpServers: '', skills: '', tools: '' })
      setCreating(false)
      await reload()
      showToast({ tone: 'ok', message: editing ? 'Agent 已更新' : 'Agent 已创建' })
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
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>key</span>
                <TextInput id="agent-key" value={form.key} onChange={(v) => setForm({ ...form, key: v })} />
              </label>
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>名称</span>
                <TextInput id="agent-name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
              </label>
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>描述</span>
                <TextInput id="agent-desc" value={form.description} onChange={(v) => setForm({ ...form, description: v })} />
              </label>
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>MCP（逗号分隔）</span>
                <TextInput id="agent-mcp" value={form.mcpServers} onChange={(v) => setForm({ ...form, mcpServers: v })} />
              </label>
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>Skill（逗号分隔）</span>
                <TextInput id="agent-skills" value={form.skills} onChange={(v) => setForm({ ...form, skills: v })} />
              </label>
              <label className="flex flex-col gap-1.5 text-xs font-medium text-ink">
                <span>工具（逗号分隔）</span>
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
        ) : items.length === 0 ? (
          <EmptyState icon={Bot} title="没有 Agent" description="从右上角「新建 Agent」开始。" />
        ) : (
          <div className="grid grid-cols-1 gap-panel md:grid-cols-2 xl:grid-cols-3">
            {items.map((item) => (
              <section key={item.id} className="flex flex-col gap-2 rounded-md border border-line bg-surface p-panel transition-ui hover:border-line-strong">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <Bot className="size-4 shrink-0 text-ink-muted" strokeWidth={1.75} />
                    <span className="truncate text-sm text-ink">{item.name}</span>
                    <code className="truncate font-mono text-2xs text-ink-faint">{item.key}</code>
                  </div>
                  <span className="shrink-0 rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-muted">
                    {item.builtin ? '内置' : '自定义'}
                  </span>
                  <span className="shrink-0 rounded-xs border border-line px-1.5 py-0.5 text-2xs text-ink-faint">
                    {item.kind === 'persona' ? '聊天角色' : '流程角色'}
                  </span>
                </div>
                <p className="min-h-[2.5rem] text-2xs leading-relaxed text-ink-muted">
                  {item.description || '（没有写描述）'}
                </p>
                <div className="mt-auto flex items-center gap-2 text-2xs text-ink-faint tabular-nums">
                  <span>MCP {item.mcpServers.length}</span>
                  <span>Skill {item.skills.length}</span>
                  <span>工具 {item.tools.length}</span>
                  {!item.builtin && (
                    <span className="ml-auto flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          setEditing(item)
                          setForm({
                            key: item.key,
                            name: item.name,
                            description: item.description,
                            mcpServers: item.mcpServers.join(', '),
                            skills: item.skills.join(', '),
                            tools: item.tools.join(', '),
                          })
                          setCreating(true)
                        }}
                        className="text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        onClick={() => void remove(item)}
                        className="text-ink-faint underline-offset-2 hover:text-signal-risk hover:underline"
                      >
                        删除
                      </button>
                    </span>
                  )}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>

      {pickerOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="选一个角色"
        >
          <div className="w-full max-w-3xl rounded-lg border border-line bg-surface p-panel shadow-xl">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="text-sm font-medium text-ink">选一个角色</h3>
                <p className="mt-1 text-2xs text-ink-muted">
                  角色只改「关注什么、怎么说」，不改变权限 —— 能调哪些工具仍由执行器决定。
                </p>
              </div>
              <button
                type="button"
                onClick={() => setPickerOpen(false)}
                className="rounded-sm p-1 text-ink-faint hover:text-ink"
                aria-label="关闭"
              >
                <X className="size-4" strokeWidth={1.75} />
              </button>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
              {templates.map((template) => (
                <button
                  key={template.key}
                  type="button"
                  onClick={() => {
                    setForm({
                      key: `role-${template.key}`,
                      name: template.name,
                      description: template.description,
                      mcpServers: '',
                      skills: '',
                      tools: '',
                    })
                    setPickerOpen(false)
                    setCreating(true)
                  }}
                  className="rounded-sm border border-line px-3 py-2 text-left transition-ui hover:border-line-strong hover:bg-surface-raised"
                >
                  <div className="text-sm text-ink">{template.name}</div>
                  <div className="mt-0.5 text-2xs leading-relaxed text-ink-muted">
                    {template.description}
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default AgentsPage
