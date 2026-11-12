import { PageHeader } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/Button'
import { TextInput } from '@/components/settings/Field'
import EmptyState from '@/components/ui/EmptyState'
import {
  cancelAgentTask,
  createAgentTask,
  loadAgentGraph,
  addAgentGoal,
  addAgentIntent,
  loadAgentTasks,
  loadTaskReport,
  proveAgentGoal,
  runAgentTask,
  type AgentGraph,
  type AgentTask,
} from '@/data/agent-tasks'
import { showToast } from '@/data/toast-store'
import { useI18n } from '@/i18n'
import { ListTree, Plus, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

const STATUS_TONE: Record<string, string> = {
  planning: 'text-ink-muted',
  running: 'text-accent-strong',
  completed: 'text-signal-ok',
  failed: 'text-signal-risk',
  cancelled: 'text-ink-faint',
}

export default function AgentTasksPage() {
  const { t } = useI18n()
  const [params] = useSearchParams()
  const [tasks, setTasks] = useState<AgentTask[]>([])
  const [selected, setSelected] = useState<AgentGraph | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ title: '', objective: '', scope: '' })
  const [busy, setBusy] = useState(false)
  const [goalDraft, setGoalDraft] = useState('')
  const [intentDraft, setIntentDraft] = useState('')
  const [proving, setProving] = useState<{ id: string; evidence: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    try {
      const rows = await loadAgentTasks()
      setTasks(rows)
      const wanted = params.get('task')
      if (wanted) {
        const graph = await loadAgentGraph(wanted)
        setSelected(graph)
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    // 数据加载，不是从 props 派生的级联渲染；与其它页面同一处理。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload()
  }, [])

  // 执行中的任务自动刷新：worker/planner 是后台会话，页面不轮询就会
  // 一直停在“运行中”的旧图上。
  useEffect(() => {
    if (!selected) return
    if (selected.task.status !== 'running' && selected.task.status !== 'planning') return
    const taskId = selected.task.id
    const timer = window.setInterval(() => {
      void loadAgentGraph(taskId)
        .then((graph) => setSelected(graph))
        .catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [selected?.task.id, selected?.task.status])

  async function open(task: AgentTask) {
    try {
      setSelected(await loadAgentGraph(task.id))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  async function addGoal() {
    if (!selected || !goalDraft.trim()) return
    setBusy(true)
    try {
      await addAgentGoal(selected.task.id, goalDraft.trim())
      setGoalDraft('')
      setSelected(await loadAgentGraph(selected.task.id))
      setTasks(await loadAgentTasks())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function addIntent() {
    if (!selected || !intentDraft.trim()) return
    setBusy(true)
    try {
      await addAgentIntent(selected.task.id, intentDraft.trim())
      setIntentDraft('')
      setSelected(await loadAgentGraph(selected.task.id))
      setTasks(await loadAgentTasks())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function confirmProve() {
    if (!selected || !proving || !proving.evidence.trim()) return
    setBusy(true)
    try {
      await proveAgentGoal(proving.id, selected.task.id, proving.evidence.trim())
      setProving(null)
      setSelected(await loadAgentGraph(selected.task.id))
      setTasks(await loadAgentTasks())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
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

  async function downloadReport() {
    if (!selected) return
    try {
      const { filename, text } = await loadTaskReport(selected.task.id)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = filename ?? `${selected.task.id}.md`
      link.click()
      URL.revokeObjectURL(url)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  async function runCurrent() {
    if (!selected) return
    setBusy(true)
    try {
      const queued = await runAgentTask(selected.task.id)
      showToast({
        message:
          queued > 0
            ? t('agentTasks.started', { n: queued }, `已开始执行 ${queued} 条意图`)
            : t('agentTasks.nothingToRun', undefined, '没有待执行的意图'),
        tone: 'ok',
      })
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
                    <Button size="sm" variant="ghost" onClick={() => void downloadReport()}>
                      {t('agentTasks.export', undefined, '导出报告')}
                    </Button>
                    {selected.intents.some((intent) => intent.status === 'open') ||
                    selected.goals.some((goal) => goal.status === 'open') ? (
                      <Button size="sm" disabled={busy} onClick={() => void runCurrent()}>
                        {t('agentTasks.run', undefined, '执行待办')}
                      </Button>
                    ) : null}
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
                        {goal.status === 'open' && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => setProving({ id: goal.id, evidence: '' })}
                          >
                            {t('agentTasks.prove', undefined, '标完成')}
                          </Button>
                        )}
                      </div>
                      {proving?.id === goal.id && (
                        <div className="mt-2 flex gap-2">
                          <TextInput
                            id={`prove-${goal.id}`}
                            value={proving.evidence}
                            onChange={(value) => setProving({ ...proving, evidence: value })}
                            placeholder={t('agentTasks.evidence', undefined, '证据')}
                          />
                          <Button size="sm" disabled={busy || !proving.evidence.trim()} onClick={() => void confirmProve()}>
                            {t('agentTasks.confirm', undefined, '确认')}
                          </Button>
                        </div>
                      )}
                      {goal.evidence && (
                        <p className="mt-1 whitespace-pre-wrap text-2xs text-ink-faint">{goal.evidence}</p>
                      )}
                    </li>
                  ))}
                  {selected.goals.length === 0 && (
                    <p className="text-xs text-ink-faint">{t('agentTasks.noGoals', undefined, '还没有目标。')}</p>
                  )}
                  <li className="flex gap-2">
                    <TextInput
                      id="agent-task-goal"
                      value={goalDraft}
                      onChange={setGoalDraft}
                      placeholder={t('agentTasks.goalPlaceholder', undefined, '新的可验证目标')}
                    />
                    <Button size="sm" disabled={busy || !goalDraft.trim()} onClick={() => void addGoal()}>
                      {t('agentTasks.addGoal', undefined, '加目标')}
                    </Button>
                  </li>
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
                  <li className="flex gap-2">
                    <TextInput
                      id="agent-task-intent"
                      value={intentDraft}
                      onChange={setIntentDraft}
                      placeholder={t('agentTasks.intentPlaceholder', undefined, '新的探索方向')}
                    />
                    <Button size="sm" disabled={busy || !intentDraft.trim()} onClick={() => void addIntent()}>
                      {t('agentTasks.addIntent', undefined, '加意图')}
                    </Button>
                  </li>
                </ul>
              </div>

              {selected.scans.length > 0 && (
                <div className="rounded-md border border-line bg-surface p-4">
                  <h3 className="text-sm font-medium text-ink">
                    {t('agentTasks.scans', undefined, '关联扫描')}
                  </h3>
                  <ul className="mt-2 flex flex-col gap-1 text-2xs">
                    {selected.scans.map((scan) => (
                      <li key={scan.id} className="flex items-center gap-2">
                        <Link
                          to={`/scans?scan=${encodeURIComponent(scan.id)}`}
                          className="mono-ui text-accent-strong hover:underline"
                        >
                          {scan.id}
                        </Link>
                        <span className="min-w-0 truncate text-ink-muted">{scan.target}</span>
                        <span className="ml-auto text-ink-faint">{scan.status}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {selected.runs.length > 0 && (
                <div className="rounded-md border border-line bg-surface p-4">
                  <h3 className="text-sm font-medium text-ink">
                    {t('agentTasks.runs', undefined, '执行记录')}
                  </h3>
                  <ul className="mt-2 flex flex-col gap-1 text-2xs text-ink-muted">
                    {selected.runs.map((run) => (
                      <li key={run.id} className="flex items-center gap-2">
                        <span className="w-16 shrink-0 text-ink-faint">{run.role}</span>
                        <span className="shrink-0">{run.status}</span>
                        {run.sessionId ? (
                          <Link
                            to={`/chat?session=${encodeURIComponent(run.sessionId)}`}
                            className="min-w-0 truncate text-accent-strong hover:underline"
                          >
                            {run.sessionId}
                          </Link>
                        ) : (
                          <span className="text-ink-faint">—</span>
                        )}
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
