import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { TextInput } from './Field'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import {
  createCase,
  deleteCase,
  loadCases,
  loadRuns,
  runSuite,
  type BenchmarkCase,
  type CaseRun,
} from '@/data/benchmark'

const STATUS_LABEL: Record<CaseRun['status'], string> = {
  passed: '通过',
  failed: '失败',
  pending: '待定',
  error: '错误',
}

const STATUS_CLASS: Record<CaseRun['status'], string> = {
  passed: 'text-signal-ok',
  failed: 'text-signal-risk',
  pending: 'text-ink-muted',
  error: 'text-signal-risk',
}

/**
 * 评测用例：登记 → 采集 → 看结果。
 *
 * 采集只读关联对象（已有扫描/题目），**不触发新的扫描或解题** ——
 * 评测不该偷偷花掉扫描额度。
 */
export function BenchmarkCasesPanel() {
  const [cases, setCases] = useState<BenchmarkCase[]>([])
  const [runs, setRuns] = useState<Record<string, CaseRun>>({})
  const [loading, setLoading] = useState(true)
  const [suite, setSuite] = useState('default')
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'ctf' | 'scan'>('scan')
  const [refId, setRefId] = useState('')
  const [minFindings, setMinFindings] = useState('0')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [items, latest] = await Promise.all([loadCases(), loadRuns()])
      setCases(items)
      setRuns(Object.fromEntries(latest.map((run) => [run.caseId, run])))
      // 首次加载时把采集目标对齐到已有用例的套件，避免对着空套件点采集。
      setSuite((prev) => (prev === 'default' && items.length > 0 ? items[0].suite : prev))
    } catch {
      // 读失败不拦住设置页；空列表就是"还没有用例"。
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 异步读取后写状态：数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
  }, [load])

  const register = async () => {
    if (!name.trim() || !refId.trim()) return
    setBusy(true)
    try {
      await createCase({
        suite: suite.trim() || 'default',
        name: name.trim(),
        kind,
        ...(kind === 'ctf' ? { challengeId: refId.trim() } : { scanId: refId.trim() }),
        expect: kind === 'scan' ? { minFindings: Number(minFindings) || 0 } : {},
      })
      setName('')
      setRefId('')
      await load()
      showToast({ tone: 'ok', message: '用例已登记' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '登记失败' })
    } finally {
      setBusy(false)
    }
  }

  const collect = async () => {
    setBusy(true)
    try {
      const out = await runSuite(suite.trim() || 'default')
      await load()
      showToast({
        tone: out.failed + out.error > 0 ? 'risk' : 'ok',
        message: `采集完成：通过 ${out.passed} · 失败 ${out.failed} · 待定 ${out.pending} · 错误 ${out.error}`,
      })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '采集失败' })
    } finally {
      setBusy(false)
    }
  }

  const remove = async (caseId: string) => {
    try {
      await deleteCase(caseId)
      await load()
    } catch {
      showToast({ tone: 'risk', message: '删除失败' })
    }
  }

  return (
    <SettingsPanel
      id="benchmark-cases"
      title="评测用例"
      description="登记固定用例（题目或扫描），一键采集当前判定。只读已有记录，不触发新扫描。"
    >
      <div className="flex flex-col gap-panel">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-6">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">套件</span>
            <TextInput id="bench-suite" value={suite} onChange={setSuite} />
          </label>
          <label className="flex flex-col gap-1 text-sm lg:col-span-2">
            <span className="text-ink-muted">名称</span>
            <TextInput id="bench-name" value={name} onChange={setName} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">类型</span>
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value as 'ctf' | 'scan')}
              className="h-9 rounded-sm border border-line bg-surface px-2 text-sm"
            >
              <option value="scan">扫描</option>
              <option value="ctf">CTF 题目</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm lg:col-span-2">
            <span className="text-ink-muted">{kind === 'ctf' ? '题目 id' : '扫描 id'}</span>
            <TextInput id="bench-ref" value={refId} onChange={setRefId} />
          </label>
          {kind === 'scan' && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">最少发现数</span>
              <TextInput id="bench-min" value={minFindings} onChange={setMinFindings} type="number" />
            </label>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={() => void register()}
            disabled={busy || !name.trim() || !refId.trim()}
          >
            登记用例
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void collect()} disabled={busy}>
            采集「{suite.trim() || 'default'}」结果
          </Button>
        </div>

        {loading ? (
          <p className="text-sm text-ink-muted">读取中…</p>
        ) : cases.length === 0 ? (
          <p className="text-sm text-ink-muted">还没有用例。先登记一条，再采集。</p>
        ) : (
          <ul className="divide-y divide-line rounded-sm border border-line">
            {cases.map((item) => {
              const run = runs[item.id]
              return (
                <li key={item.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                  <span className="min-w-0 flex-1 truncate text-ink">{item.name}</span>
                  <span className="text-2xs text-ink-faint">
                    {item.suite} · {item.kind === 'ctf' ? '题目' : '扫描'} ·{' '}
                    {item.challengeId ?? item.scanId ?? '—'}
                  </span>
                  <span className={run ? STATUS_CLASS[run.status] : 'text-ink-faint'}>
                    {run ? STATUS_LABEL[run.status] : '未采集'}
                  </span>
                  <button
                    type="button"
                    className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                    onClick={() => void remove(item.id)}
                  >
                    删除
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </SettingsPanel>
  )
}
