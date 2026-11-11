import { useEffect, useState } from 'react'
import { useI18n } from '@/i18n'
import { T } from '@/i18n/T'
import { Button } from '@/components/ui/Button'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import { loadModelRoutes, saveModelRoutes } from '@/data/modelRoutes'
import type { ProviderAccount } from '@/data/settings'
import { Select } from '@/components/ui/Select'

/** 三个成功判据。顺序与扫描创建向导一致。 */
const GOALS = [
  { key: 'findVulns' },
  { key: 'captureFlag' },
  { key: 'verifyFix' },
] as const

/** 扫描技能路由的键前缀是 `skill:<id>`（v1.82）。 */
const SKILLS = [
  { key: 'skill:web' },
  { key: 'skill:api' },
  { key: 'skill:container' },
  { key: 'skill:cloud' },
  { key: 'skill:dependencies' },
  { key: 'skill:secrets' },
] as const

/**
 * 模型路由：不同成功判据用不同 provider。
 *
 * 未配置的判据回退平台默认。保存是覆盖式：空值会被后端丢弃。
 */
export function ModelRoutesPanel({ accounts }: { accounts: ProviderAccount[] }) {
  const { t } = useI18n()
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
      showToast({ tone: 'ok', message: t('settings.modelRoutes.saved') })
    } catch (err) {
      showToast({
        tone: 'risk',
        message: err instanceof Error ? err.message : t('settings.modelRoutes.saveFailed'),
      })
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsPanel
      id="model-routes"
      title={t('settings.modelRoutes.title')}
      description={t('settings.modelRoutes.description')}
    >
      {loading ? (
        <p className="text-sm text-ink-muted">
          <T k="settings.modelRoutes.loading" />
        </p>
      ) : (
        <div className="flex flex-col gap-panel">
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {GOALS.map((goal) => (
              <label key={goal.key} className="flex flex-col gap-1.5 text-sm">
                <span className="text-ink-muted">
                  {t(`settings.modelRoutes.goal.${goal.key}`)}
                </span>
                <Select
                  value={routes[goal.key] ?? ''}
                  options={[
                    { value: '', label: t('settings.modelRoutes.platformDefault') },
                    ...accounts.map((account) => ({
                      value: account.id,
                      label: `${account.label} · ${account.model}`,
                    })),
                  ]}
                  onChange={(value) => {
                    setRoutes((prev) => {
                      const next = { ...prev }
                      if (value) next[goal.key] = value
                      else delete next[goal.key]
                      return next
                    })
                  }}
                />
              </label>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {SKILLS.map((skill) => (
              <label key={skill.key} className="flex flex-col gap-1.5 text-sm">
                <span className="text-ink-muted">
                  <T
                    k="settings.modelRoutes.skillLabel"
                    vars={{ skill: t(`settings.modelRoutes.skill.${skill.key}`) }}
                  />
                </span>
                <Select
                  value={routes[skill.key] ?? ''}
                  options={[
                    { value: '', label: t('settings.modelRoutes.noRoute') },
                    ...accounts.map((account) => ({
                      value: account.id,
                      label: `${account.label} · ${account.model}`,
                    })),
                  ]}
                  onChange={(value) => {
                    setRoutes((prev) => {
                      const next = { ...prev }
                      if (value) next[skill.key] = value
                      else delete next[skill.key]
                      return next
                    })
                  }}
                />
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
              {saving ? (
                <T k="settings.modelRoutes.saving" />
              ) : (
                <T k="settings.modelRoutes.save" />
              )}
            </Button>
            {!dirty && (
              <span className="text-2xs text-ink-faint">
                <T k="settings.modelRoutes.noChanges" />
              </span>
            )}
          </div>
        </div>
      )}
    </SettingsPanel>
  )
}
