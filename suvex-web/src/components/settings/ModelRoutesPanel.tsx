import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import { loadModelRoutes, saveModelRoutes } from '@/data/modelRoutes'
import type { ProviderAccount } from '@/data/settings'

/** 三个成功判据。顺序与扫描创建向导一致。 */
const GOALS = [
  { key: 'findVulns', label: '渗透评估（findVulns）' },
  { key: 'captureFlag', label: 'CTF 夺旗（captureFlag）' },
  { key: 'verifyFix', label: '复测已修（verifyFix）' },
] as const

/**
 * 模型路由：不同成功判据用不同 provider。
 *
 * 未配置的判据回退平台默认。保存是覆盖式：空值会被后端丢弃。
 */
export function ModelRoutesPanel({ accounts }: { accounts: ProviderAccount[] }) {
  const [routes, setRoutes] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    void loadModelRoutes()
      .then((snapshot) => {
        if (!alive) return
        setRoutes(snapshot.routes ?? {})
        setSaved(snapshot.routes ?? {})
      })
      .catch(() => {
        // 读失败不拦住设置页：显示空路由（= 全部走默认）。
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const dirty = JSON.stringify(routes) !== JSON.stringify(saved)

  const onSave = async () => {
    setSaving(true)
    try {
      const snapshot = await saveModelRoutes(routes)
      setRoutes(snapshot.routes ?? {})
      setSaved(snapshot.routes ?? {})
      showToast({ tone: 'ok', message: '模型路由已保存' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : '保存失败' })
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsPanel
      id="model-routes"
      title="模型路由"
      description="不同成功判据可以用不同模型：未配置的判据回退平台默认 provider。显式选择模型的扫描仍然优先。"
    >
      {loading ? (
        <p className="text-sm text-ink-muted">读取中…</p>
      ) : (
        <div className="flex flex-col gap-panel">
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {GOALS.map((goal) => (
              <label key={goal.key} className="flex flex-col gap-1.5 text-sm">
                <span className="text-ink-muted">{goal.label}</span>
                <select
                  value={routes[goal.key] ?? ''}
                  onChange={(event) => {
                    const value = event.target.value
                    setRoutes((prev) => {
                      const next = { ...prev }
                      if (value) next[goal.key] = value
                      else delete next[goal.key]
                      return next
                    })
                  }}
                  className="h-9 w-full rounded-sm border border-line bg-surface px-2 text-sm text-ink"
                >
                  <option value="">平台默认</option>
                  {accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.label} · {account.model}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={() => void onSave()}
              disabled={!dirty || saving}
            >
              {saving ? '保存中…' : '保存路由'}
            </Button>
            {!dirty && <span className="text-2xs text-ink-faint">没有未保存的修改</span>}
          </div>
        </div>
      )}
    </SettingsPanel>
  )
}
