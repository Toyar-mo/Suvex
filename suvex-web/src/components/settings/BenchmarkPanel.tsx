import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { Button } from '@/components/ui/Button'
import { TextInput } from './Field'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import {
  captureSnapshot,
  compareSnapshots,
  loadSnapshots,
  type BenchmarkCompare,
  type BenchmarkSnapshot,
  type QualityPayload,
} from '@/data/benchmark'
import { Select } from '@/components/ui/Select'

/** 对比表里展示的指标（key 与后端 delta 一致）。 */
const DELTAS = [
  {
    key: 'solveRate',
    scale: 100,
    unit: '%',
    read: (p: QualityPayload) => p.challenges.solveRate,
  },
  {
    key: 'falsePositiveRate',
    scale: 100,
    unit: '%',
    read: (p: QualityPayload) => p.findings.falsePositiveRate,
  },
  {
    key: 'verifiedShare',
    scale: 100,
    unit: '%',
    read: (p: QualityPayload) =>
      p.findings.total > 0 ? p.findings.verified / p.findings.total : null,
  },
  {
    key: 'totalTokens',
    scale: 1,
    unit: '',
    read: (p: QualityPayload) => p.cost?.totalTokens ?? null,
  },
  {
    key: 'coverageSurfaces',
    scale: 1,
    unit: '',
    unitKey: 'settings.benchmark.unitRecords',
    read: (p: QualityPayload) => p.coverage.surfaces,
  },
  {
    key: 'avgScanDurationSec',
    scale: 1,
    unit: 's',
    read: (p: QualityPayload) => p.scans.avgDurationSec,
  },
] as const

function formatValue(value: number | null, scale: number, unit: string): string {
  if (value === null) return '—'
  if (scale === 100) return `${(value * 100).toFixed(1)}%`
  return `${value}${unit}`
}

function formatDelta(value: number | null | undefined, scale: number, unit: string): string {
  if (value === null || value === undefined) return '—'
  const scaled = value * scale
  const sign = scaled > 0 ? '+' : ''
  return `${sign}${scaled.toFixed(scale === 100 ? 1 : 0)}${unit}`
}

function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(1)}%`
}

export function BenchmarkPanel() {
  const { t } = useI18n()
  const [snapshots, setSnapshots] = useState<BenchmarkSnapshot[]>([])
  const [loading, setLoading] = useState(true)
  const [label, setLabel] = useState('')
  const [saving, setSaving] = useState(false)
  const [beforeId, setBeforeId] = useState('')
  const [afterId, setAfterId] = useState('')
  const [compare, setCompare] = useState<BenchmarkCompare | null>(null)
  const [comparing, setComparing] = useState(false)

  const load = useCallback(async () => {
    try {
      setSnapshots(await loadSnapshots())
    } catch {
      // 读失败不拦住设置页；空列表就是"还没有快照"。
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 异步读取快照后写状态：这是数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
  }, [load])

  const capture = async () => {
    setSaving(true)
    try {
      await captureSnapshot(label)
      setLabel('')
      await load()
      showToast({ tone: 'ok', message: t('settings.benchmark.captured') })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.benchmark.captureFailed'),
      })
    } finally {
      setSaving(false)
    }
  }

  const runCompare = async () => {
    if (!beforeId || !afterId) return
    setComparing(true)
    try {
      setCompare(await compareSnapshots(beforeId, afterId))
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : t('settings.benchmark.compareFailed') })
    } finally {
      setComparing(false)
    }
  }

  return (
    <SettingsPanel
      id="benchmark"
      title={t('settings.benchmark.title')}
      description={t('settings.benchmark.description')}
    >
      <div className="flex flex-col gap-panel">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-[220px] flex-1 flex-col gap-1 text-sm">
            <span className="text-ink-muted">
              <T k="settings.benchmark.labelField" />
            </span>
            <TextInput
              id="benchmark-label"
              value={label}
              onChange={(value) => setLabel(value)}
              placeholder={t('settings.benchmark.labelPlaceholder')}
            />
          </label>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void capture()}
            disabled={saving}
          >
            {saving ? (
              <T k="settings.benchmark.capturing" />
            ) : (
              <T k="settings.benchmark.capture" />
            )}
          </Button>
        </div>

        {loading ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.benchmark.loading" />
          </p>
        ) : snapshots.length === 0 ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.benchmark.empty" />
          </p>
        ) : (
          <>
            <ul className="divide-y divide-line rounded-sm border border-line">
              {snapshots.map((snapshot) => (
                <li key={snapshot.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                  <span className="min-w-0 flex-1 truncate text-ink">
                    {snapshot.label || t('settings.benchmark.untitled')}
                  </span>
                  <span className="text-2xs text-ink-faint">
                    {new Date(snapshot.capturedAt).toLocaleString()}
                  </span>
                  <span className="text-2xs text-ink-muted">
                    <T
                      k="settings.benchmark.summary"
                      vars={{
                        solve: formatRate(snapshot.payload.challenges.solveRate),
                        fp: formatRate(snapshot.payload.findings.falsePositiveRate),
                        coverage: snapshot.payload.coverage.surfaces,
                      }}
                    />
                  </span>
                </li>
              ))}
            </ul>

            <div className="flex flex-wrap items-end gap-2">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">
                  <T k="settings.benchmark.baseline" />
                </span>
                <Select
                  value={beforeId}
                  options={[
                    { value: '', label: t('settings.benchmark.selectPlaceholder') },
                    ...snapshots.map((snapshot) => ({
                      value: snapshot.id,
                      label: snapshot.label || snapshot.id,
                    })),
                  ]}
                  onChange={setBeforeId}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">
                  <T k="settings.benchmark.compare" />
                </span>
                <Select
                  value={afterId}
                  options={[
                    { value: '', label: t('settings.benchmark.selectPlaceholder') },
                    ...snapshots.map((snapshot) => ({
                      value: snapshot.id,
                      label: snapshot.label || snapshot.id,
                    })),
                  ]}
                  onChange={setAfterId}
                />
              </label>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void runCompare()}
                disabled={!beforeId || !afterId || comparing}
              >
                {comparing ? (
                  <T k="settings.benchmark.comparing" />
                ) : (
                  <T k="settings.benchmark.compareAction" />
                )}
              </Button>
            </div>

            {compare && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-2xs text-ink-faint">
                    <th className="py-1 font-normal">
                      <T k="settings.benchmark.metric" />
                    </th>
                    <th className="py-1 font-normal">
                      <T k="settings.benchmark.baseline" />
                    </th>
                    <th className="py-1 font-normal">
                      <T k="settings.benchmark.compare" />
                    </th>
                    <th className="py-1 font-normal">
                      <T k="settings.benchmark.delta" />
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {DELTAS.map((item) => (
                    <tr key={item.key} className="border-t border-line">
                      <td className="py-1.5 text-ink-muted">
                        {t(`settings.benchmark.metric.${item.key}`)}
                      </td>
                      <td className="py-1.5 tabular-nums">
                        {formatValue(
                          item.read(compare.before.metrics),
                          item.scale,
                          'unitKey' in item ? t(item.unitKey) : item.unit,
                        )}
                      </td>
                      <td className="py-1.5 tabular-nums">
                        {formatValue(
                          item.read(compare.after.metrics),
                          item.scale,
                          'unitKey' in item ? t(item.unitKey) : item.unit,
                        )}
                      </td>
                      <td className="py-1.5 tabular-nums">
                        {formatDelta(
                          compare.delta[item.key],
                          item.scale,
                          'unitKey' in item ? t(item.unitKey) : item.unit,
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </SettingsPanel>
  )
}
