import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import EmptyState from '@/components/ui/EmptyState'
import {
  cancelAgentTask,
  createAgentTask,
  loadAgentGraph,
  loadAgentTasks,
  type AgentGraph,
  type AgentTask,
} from '@/data/agent-tasks'
import { useI18n } from '@/i18n'
import { ListTree, Plus, X } from 'lucide-react'
import { useEffect, useState } from 'react'

const STATUS_TONE: Record<string, string> = {
  planning: 'text-ink-muted',
  running: 'text-accent-strong',
  completed: 'text-signal-ok',
  failed: 'text-signal-risk',
  cancelled: 'text-ink-faint',
}

export default function AgentTasksPage() {
  const { t } = useI18n()
  const [tasks, setTasks] = useState<AgentTask[]>([])
  const [selected, setSelected] = useState<AgentGraph | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ title: '', objective: '', scope: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    try {
      setTasks(await loadAgentTasks())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function open(task: AgentTask) {
    try {
      setSelected(await loadAgentGraph(task.id))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  async function submit() {
    if (!form.title.trim()) return
    setBusy(true)
    setError(null)
    try {
      const graph = await createAgentTask(form)
      setTasks((current) => [graph.task, ...current])
      setSelected(graph)
      setForm({ title: '', objective: '', scope: '' })
      setCreating(false)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function cancelCurrent() {
    if (!selected) return
    setBusy(true)
    try {
      const task = await cancelAgentTask(selected.task.id)
      setTasks((current) => current.map((row) => (row.id === task.id ? task : row)))
      setSelected({ ...selected, task })
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader navId="tasks" />
      <div className="grid min-h-0 flex-1 gap-panel overflow-auto p-gutter lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="flex min-h-0 flex-col gap-tight">
          <Button size="sm" variant="ghost" onClick={() => setCreating((value) => !value)}>
            {creating ? <X size={14} /> : <Plus size={14} />}
            {creating
              ? t('agentTasks.close', undefined, '收起')
              : t('agentTasks.new', undefined, '新建任务')}
          </Button>
          {creating && (
            <div className="flex flex-col gap-tight rounded-md border border-line bg-surface p-3">
              <TextInput
                id="agent-task-title"
                value={form.title}
                onChange={(value) => setForm({ ...form, title: value })}
                placeholder={t('agentTasks.titlePlaceholder', undefined, '任务标题')}
              />
              <TextInput
                id="agent-task-objective"
                value={form.objective}
                onChange={(value) => setForm({ ...form, objective: value })}
                placeholder={t('agentTasks.objective', undefined, '最终目标')}
              />
              <TextInput
                id="agent-task-scope"
                value={form.scope}
                onChange={(value) => setForm({ ...form, scope: value })}
                placeholder={t('agentTasks.scope', undefined, '授权范围 / 禁止项')}
              />
              <Button size="sm" disabled={busy || !form.title.trim()} onClick={() => void submit()}>
                {t('agentTasks.create', undefined, '创建')}
              </Button>
            </div>
          )}
          {error && <p className="text-xs text-signal-risk">{error}</p>}
          {tasks.length === 0 ? (
            <EmptyState
              icon={ListTree}
              title={t('agentTasks.empty.title', undefined, '还没有执行任务')}
              description={t(
                'agentTasks.empty.desc',
                undefined,
                '在对话里让 planner 创建任务，或在这里手动建一个。',
              )}
            />
          ) : (
            <ul className="flex flex-col gap-1">
              {tasks.map((task) => (
                <li key={task.id}>
                  <button
                    type="button"
                    onClick={() => void open(task)}
                    className={`w-full rounded-md border px-3 py-2 text-left transition-ui duration-fast ${
                      selected?.task.id === task.id
                        ? 'border-line-strong bg-surface-raised'
                        : 'border-line bg-surface hover:border-line-strong'
                    }`}
                  >
                    <span className="block truncate text-sm text-ink">{task.title}</span>
                    <span className="mt-0.5 flex items-center gap-2 text-2xs text-ink-faint">
                      <span className={STATUS_TONE[task.status] ?? 'text-ink-muted'}>
                        {t(`agentTasks.status.${task.status}`, undefined, task.status)}
                      </span>
                      <span>{task.id}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="min-w-0">
          {!selected ? (
            <EmptyState
              icon={ListTree}
              title={t('agentTasks.select.title', undefined, '选择一个任务')}
              description={t(
                'agentTasks.select.desc',
                undefined,
                '左侧列表会显示目标、意图与 worker 写回的结果。',
              )}
            />
          ) : (
            <div className="flex flex-col gap-panel">
              <div className="rounded-md border border-line bg-surface p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="truncate text-base font-medium text-ink">{selected.task.title}</h2>
                    <p className="mt-1 whitespace-pre-wrap text-xs text-ink-muted">
                      {selected.task.objective || '—'}
                    </p>
                    {selected.task.scope && (
                      <p className="mt-1 text-2xs text-ink-faint">
                        {t('agentTasks.scopeLabel', undefined, '范围')}：{selected.task.scope}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-2xs ${STATUS_TONE[selected.task.status] ?? ''}`}>
                      {t(`agentTasks.status.${selected.task.status}`, undefined, selected.task.status)}
                    </span>
                    {selected.task.status === 'running' || selected.task.status === 'planning' ? (
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => void cancelCurrent()}>
                        {t('agentTasks.cancel', undefined, '取消任务')}
                      </Button>
                    ) : null}
                  </div>
                </div>
              </div>

              <div className="rounded-md border border-line bg-surface p-4">
                <h3 className="text-sm font-medium text-ink">
                  {t('agentTasks.goals', undefined, '目标')}
                </h3>
                <ul className="mt-2 flex flex-col gap-2">
                  {selected.goals.map((goal) => (
                    <li key={goal.id} className="rounded-sm border border-line px-3 py-2">
                      <div className="flex items-center gap-2 text-xs">
                        <span className="tabular text-ink-faint">#{goal.seq}</span>
                        <span className="text-ink">{goal.text}</span>
                        <span className="ml-auto text-2xs text-ink-muted">{goal.status}</span>
                      </div>
                      {goal.evidence && (
                        <p className="mt-1 whitespace-pre-wrap text-2xs text-ink-faint">{goal.evidence}</p>
                      )}
                    </li>
                  ))}
                  {selected.goals.length === 0 && (
                    <p className="text-xs text-ink-faint">{t('agentTasks.noGoals', undefined, '还没有目标。')}</p>
                  )}
                </ul>
              </div>

              <div className="rounded-md border border-line bg-surface p-4">
                <h3 className="text-sm font-medium text-ink">
                  {t('agentTasks.intents', undefined, '意图')}
                </h3>
                <ul className="mt-2 flex flex-col gap-2">
                  {selected.intents.map((intent) => (
                    <li key={intent.id} className="rounded-sm border border-line px-3 py-2">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="tabular text-ink-faint">#{intent.seq}</span>
                        <span className="rounded-sm border border-line px-1 text-2xs text-ink-muted">
                          p{intent.priority}
                        </span>
                        <span className="text-ink">{intent.summary}</span>
                        <span className="ml-auto text-2xs text-ink-muted">{intent.status}</span>
                      </div>
                      {intent.result && (
                        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-2xs text-ink-faint">
                          {intent.result}
                        </pre>
                      )}
                    </li>
                  ))}
                  {selected.intents.length === 0 && (
                    <p className="text-xs text-ink-faint">{t('agentTasks.noIntents', undefined, '还没有意图。')}</p>
                  )}
                </ul>
              </div>

              {selected.runs.length > 0 && (
                <div className="rounded-md border border-line bg-surface p-4">
                  <h3 className="text-sm font-medium text-ink">
                    {t('agentTasks.runs', undefined, '执行记录')}
                  </h3>
                  <ul className="mt-2 flex flex-col gap-1 text-2xs text-ink-muted">
                    {selected.runs.map((run) => (
                      <li key={run.id} className="flex gap-2">
                        <span className="text-ink-faint">{run.role}</span>
                        <span>{run.status}</span>
                        {run.sessionId && <span className="truncate">{run.sessionId}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
