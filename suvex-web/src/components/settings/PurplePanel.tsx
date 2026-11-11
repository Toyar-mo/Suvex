import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { Button } from '@/components/ui/Button'
import { TextInput } from './Field'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import {
  createRule,
  deleteRule,
  evaluateRules,
  exportHits,
  loadRules,
  type EvaluateResult,
  type PurpleRule,
} from '@/data/purple'
import { Select } from '@/components/ui/Select'

const SEVERITIES = ['low', 'medium', 'high', 'critical']

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * 紫队：用检测规则评估现有发现，并导出 SIEM 格式。
 *
 * 这是"如果这条漏洞发生在你的环境，哪些检测会响"的显式回答。
 */
export function PurplePanel() {
  const { t } = useI18n()
  const [rules, setRules] = useState<PurpleRule[]>([])
  const [result, setResult] = useState<EvaluateResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [cwe, setCwe] = useState('')
  const [severity, setSeverity] = useState('high')

  const load = useCallback(async () => {
    try {
      setRules(await loadRules())
    } catch {
      // 读失败不拦住设置页；空列表就是"还没有规则"。
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // 异步读取后写状态：数据加载，不是从 props 派生的级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
  }, [load])

  const add = async () => {
    if (!name.trim()) return
    setBusy(true)
    try {
      await createRule({ name: name.trim(), cwe: cwe.trim() || undefined, severity })
      setName('')
      setCwe('')
      await load()
      showToast({ tone: 'ok', message: t('settings.purple.ruleAdded') })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.purple.addFailed'),
      })
    } finally {
      setBusy(false)
    }
  }

  const evaluate = async () => {
    setBusy(true)
    try {
      setResult(await evaluateRules())
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.purple.evaluateFailed'),
      })
    } finally {
      setBusy(false)
    }
  }

  const exportAs = async (format: 'jsonl' | 'cef' | 'attack' | 'misp' | 'sigma' | 'suricata') => {
    try {
      const out = await exportHits(format)
      const ext =
        format === 'attack' || format === 'misp'
          ? 'json'
          : format === 'sigma'
            ? 'yaml'
            : format === 'suricata'
              ? 'rules'
              : format
      download(out.blob, `purple-${format}.${ext}`)
    } catch {
      showToast({ tone: 'risk', message: t('settings.purple.exportFailed') })
    }
  }

  const remove = async (ruleId: string) => {
    try {
      await deleteRule(ruleId)
      await load()
    } catch {
      showToast({ tone: 'risk', message: t('settings.purple.deleteFailed') })
    }
  }

  return (
    <SettingsPanel
      id="purple"
      title={t('settings.purple.title')}
      description={t('settings.purple.description')}
    >
      <div className="flex flex-col gap-panel">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
          <label className="flex flex-col gap-1 text-sm lg:col-span-2">
            <span className="text-ink-muted">
              <T k="settings.purple.ruleName" />
            </span>
            <TextInput id="purple-name" value={name} onChange={setName} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">
              <T k="settings.purple.cwe" />
            </span>
            <TextInput id="purple-cwe" value={cwe} onChange={setCwe} placeholder="CWE-89" />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-ink-muted">
              <T k="settings.purple.severity" />
            </span>
            <Select
              value={severity}
              options={SEVERITIES.map((item) => ({
                value: item,
                label: t(`severity.${item}`),
              }))}
              onChange={setSeverity}
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" size="sm" onClick={() => void add()} disabled={busy || !name.trim()}>
            <T k="settings.purple.addRule" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void evaluate()} disabled={busy}>
            <T k="settings.purple.evaluate" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('jsonl')}>
            <T k="settings.purple.exportJsonl" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('cef')}>
            <T k="settings.purple.exportCef" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('attack')}>
            <T k="settings.purple.exportAttack" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('misp')}>
            MISP
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('sigma')}>
            <T k="settings.purple.exportSigma" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void exportAs('suricata')}>
            <T k="settings.purple.exportSuricata" />
          </Button>
        </div>

        {result && (
          <div className="rounded-sm border border-line px-3 py-2 text-sm">
            <p className="text-ink">
              <T
                k="settings.purple.resultSummary"
                vars={{ total: result.totalFindings, hits: result.hits.length }}
              />
            </p>
            {result.hits.length > 0 && (
              <ul className="mt-1 flex flex-col gap-0.5 text-2xs text-ink-muted">
                {result.hits.map((hit) => (
                  <li key={hit.rule.id}>
                    <T k="settings.purple.hitCount" vars={{ name: hit.rule.name, count: hit.count }} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {loading ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.purple.loading" />
          </p>
        ) : rules.length === 0 ? (
          <p className="text-sm text-ink-muted">
            <T k="settings.purple.empty" />
          </p>
        ) : (
          <ul className="divide-y divide-line rounded-sm border border-line">
            {rules.map((rule) => (
              <li key={rule.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 truncate text-ink">{rule.name}</span>
                <span className="text-2xs text-ink-faint">
                  {rule.cwe ?? '—'} · {rule.severity}
                </span>
                <button
                  type="button"
                  className="text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline"
                  onClick={() => void remove(rule.id)}
                >
                  <T k="settings.purple.delete" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </SettingsPanel>
  )
}
