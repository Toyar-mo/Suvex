import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
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
import { Select } from '@/components/ui/Select'

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
  const { t } = useI18n()
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
      showToast({ tone: 'ok', message: t('settings.benchmarkCases.registered') })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.benchmarkCases.registerFailed'),
      })
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
        message: t('settings.benchmarkCases.collectDone', {
          passed: out.passed,
          failed: out.failed,
          pending: out.pending,
          error: out.error,
        }),
      })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.benchmarkCases.collectFailed'),
      })
    } finally {
      setBusy(false)
    }
  }

  const remove = async (caseId: string) => {
    try {
      await deleteCase(caseId)
      await load()
    } catch {
      showToast({ tone: 'risk', message: t('settings.benchmarkCases.deleteFailed') })
    }
  }

  return (
    <SettingsPanel
      id="benchmark-cases"
      title={t('settings.benchmarkCases.title')}
      description={t('settings.benchmarkCases.description')}
    >
      <div className="flex flex-col gap-panel">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-6">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">
              <T k="settings.benchmarkCases.suite" />
            </span>
            <TextInput id="bench-suite" value={suite} onChange={setSuite} />
          </label>
          <label className="flex flex-col gap-1 text-sm lg:col-span-2">
            <span className="text-ink-muted">
              <T k="settings.benchmarkCases.name" />
            </span>
            <TextInput id="bench-name" value={name} onChange={setName} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">
              <T k="settings.benchmarkCases.kind" />
            </span>
            <Select
              value={kind}
              options={[
                { value: 'scan', label: t('settings.benchmarkCases.kindScan') },
                { value: 'ctf', label: t('settings.benchmarkCases.kindCtf') },
              ]}
              onChange={(value) => setKind(value as 'ctf' | 'scan')}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm lg:col-span-2">
            <span className="text-ink-muted">
              {kind === 'ctf' ? (
                <T k="settings.benchmarkCases.challengeId" />
              ) : (
                <T k="settings.benchmarkCases.scanId" />
              )}
            </span>
            <TextInput id="bench-ref" value={refId} onChange={setRefId} />
          </label>
          {kind === 'scan' && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-ink-muted">
                <T k="settings.benchmarkCases.minFindings" />
              </span>
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
            <T k="settings.benchmarkCases.register" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void collect()} disabled={busy}>
            <T
              k="settings.benchmarkCases.collect"
              vars={{ suite: suite.trim() || 'default' }}
            />
          </Button>
        </div>

        {loading ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.benchmarkCases.loading" />
          </p>
        ) : cases.length === 0 ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.benchmarkCases.empty" />
          </p>
        ) : (
          <ul className="divide-y divide-line rounded-sm border border-line">
            {cases.map((item) => {
              const run = runs[item.id]
              return (
                <li key={item.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                  <span className="min-w-0 flex-1 truncate text-ink">{item.name}</span>
                  <span className="text-2xs text-ink-faint">
                    {item.suite} ·{' '}
                    {item.kind === 'ctf' ? (
                      <T k="settings.benchmarkCases.kindCtf" />
                    ) : (
                      <T k="settings.benchmarkCases.kindScan" />
                    )}{' '}
                    · {item.challengeId ?? item.scanId ?? '—'}
                  </span>
                  <span className={run ? STATUS_CLASS[run.status] : 'text-ink-faint'}>
                    {run ? (
                      t(`settings.benchmarkCases.status.${run.status}`)
                    ) : (
                      <T k="settings.benchmarkCases.notCollected" />
                    )}
                  </span>
                  <button
                    type="button"
                    className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                    onClick={() => void remove(item.id)}
                  >
                    <T k="settings.benchmarkCases.delete" />
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
