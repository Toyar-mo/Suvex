import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { SettingsPanel } from './SaveBar'
import { showToast } from '@/data/toast-store'
import { api } from '@/lib/api'
import { useI18n } from '@/i18n'

interface RetryPolicy {
  connectRetries: number
  connectIntervalMs: number
  emptyRetries: number
  emptyIntervalMs: number
  streamRetries: number
  streamIntervalMs: number
  breakerFailures: number
  breakerIntervalMs: number
  breakerCooldownS: number
}

const FIELDS: { key: keyof RetryPolicy; label: string; hint: string }[] = [
  { key: 'connectRetries', label: '建连重试次数', hint: '拿到响应前的连接失败/429/5xx' },
  { key: 'connectIntervalMs', label: '建连间隔 ms', hint: '0 = 立即重试；指数退避' },
  { key: 'emptyRetries', label: '空响应重试次数', hint: '2xx 但没有任何内容' },
  { key: 'emptyIntervalMs', label: '空响应间隔 ms', hint: '0 = 立即重试' },
  { key: 'streamRetries', label: '流式建连重试', hint: '流式请求建连失败的重试次数' },
  { key: 'streamIntervalMs', label: '流式建连间隔 ms', hint: '0 = 立即重试' },
  { key: 'breakerFailures', label: '熔断阈值', hint: '连续失败多少次后冷却' },
  { key: 'breakerCooldownS', label: '冷却秒数', hint: '熔断后跳过多久' },
]

/**
 * 模型调用重试策略（v1.83）。
 *
 * 分层的语义对齐 ARTEX，但只暴露本平台真实读取的参数；不把“意图重跑”
 * 等没有实现的层画成可用开关。
 */
export function RetryPolicyPanel() {
  const { t } = useI18n()
  const [policy, setPolicy] = useState<RetryPolicy | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    void api
      .get<RetryPolicy>('/settings/llm-retry')
      .then((value) => {
        if (alive) setPolicy(value)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  async function save() {
    if (!policy) return
    setSaving(true)
    try {
      const saved = await api.put<RetryPolicy>('/settings/llm-retry', policy)
      setPolicy(saved)
      showToast({ tone: 'ok', message: '重试策略已保存' })
    } catch (err) {
      showToast({ tone: 'risk', message: err instanceof Error ? err.message : t('settings.retry.saveFailed', undefined, '保存失败') })
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsPanel
      id="llm-retry"
      title={t('settings.retry.title', undefined, '重试与退避')}
      description={t('settings.retry.desc', undefined, '模型调用失败时的重试次数与间隔。留空/0 表示立即重试；参数范围由后端校验。')}
    >
      {!policy ? (
        <p className="text-sm text-ink-muted">{t('settings.retry.reading', undefined, '读取中…')}</p>
      ) : (
        <div className="flex flex-col gap-panel">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {FIELDS.map((field) => (
              <label key={field.key} className="flex flex-col gap-1 text-sm">
                <span className="text-ink-muted">{t(`settings.retry.field.${field.key}.label`, undefined, field.label)}</span>
                <input
                  type="number"
                  min={0}
                  value={policy[field.key]}
                  onChange={(event) =>
                    setPolicy({ ...policy, [field.key]: Number(event.target.value) || 0 })
                  }
                  className="h-9 w-full rounded-sm border border-line bg-surface px-2 text-sm text-ink"
                />
                <span className="text-2xs text-ink-faint">{t(`settings.retry.field.${field.key}.hint`, undefined, field.hint)}</span>
              </label>
            ))}
          </div>
          <p className="text-2xs text-ink-faint">
            安全窗口（首帧前断流）仍由“未推送则回退非流式”兜底；意图重跑未接入。
          </p>
          <div>
            <Button variant="primary" size="sm" onClick={() => void save()} disabled={saving}>
              {saving ? '保存中…' : '保存'}
            </Button>
          </div>
        </div>
      )}
    </SettingsPanel>
  )
}
