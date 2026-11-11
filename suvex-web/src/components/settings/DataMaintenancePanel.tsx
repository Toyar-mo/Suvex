import { Download, ShieldAlert, Trash2 } from 'lucide-react'

import { SettingsPanel } from './SaveBar'
import { Button } from '@/components/ui/Button'
import { useSettings } from '@/data/settings-store'
import { useI18n } from '@/i18n'

/**
 * 数据维护（v1.90，UI 移植阶段 4）。
 *
 * ## 这里为什么只有一件"真动作"
 *
 * 截图里那三张卡（导出数据库 / 清空业务记录 / 恢复初始配置）在后端**没有对应
 * 端点**。给它们接上按钮的代价不是写前端，而是先设计"导出什么、清空的范围、
 * 谁能执行、怎么回滚"—— 那是独立的一次任务，不该夹在一次 UI 移植里顺手做完。
 *
 * 所以这里只提供**已经成立**的导出（设置快照 JSON，就是这一页读到的数据），
 * 其余两张写成禁用 + 原因。一个点了没反应的按钮，比一个明确写着"还没做"的
 * 按钮伤害大得多 —— 后者至少不骗人。
 */
export function DataMaintenancePanel() {
  const { t } = useI18n()
  const settings = useSettings()

  const exportSnapshot = () => {
    const payload = {
      exportedAt: new Date().toISOString(),
      source: 'suvex settings snapshot',
      data: {
        sandbox: settings.sandbox,
        notices: settings.notices,
        tokens: settings.tokens,
      },
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `suvex-settings-${new Date().toISOString().slice(0, 10)}.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <SettingsPanel
      id="data-maintenance"
      title={t('data.title')}
      description={t('data.desc')}
    >
      <ul className="flex flex-col divide-y divide-line">
        <li className="flex flex-wrap items-center justify-between gap-2 py-2 first:pt-0">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-xs text-ink">
              <Download size={13} strokeWidth={1.8} aria-hidden="true" />
              {t('data.export')}
            </p>
            <p className="mt-0.5 text-2xs text-ink-faint">
              {t('data.exportHint')}
            </p>
          </div>
          <Button variant="secondary" size="sm" onClick={exportSnapshot}>
            {t('data.exportAction')}
          </Button>
        </li>

        <li className="flex flex-wrap items-center justify-between gap-2 py-2">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-xs text-ink-muted">
              <Trash2 size={13} strokeWidth={1.8} aria-hidden="true" />
              {t('data.purge')}
            </p>
            <p className="mt-0.5 text-2xs text-ink-faint">
              {t('data.purgeHint')}
            </p>
          </div>
          <Button variant="secondary" size="sm" disabled title={t('data.unavailableHint')}>
            {t('data.unavailable')}
          </Button>
        </li>

        <li className="flex flex-wrap items-center justify-between gap-2 py-2 last:pb-0">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-xs text-ink-muted">
              <ShieldAlert size={13} strokeWidth={1.8} aria-hidden="true" />
              {t('data.reset')}
            </p>
            <p className="mt-0.5 text-2xs text-ink-faint">
              {t('data.resetHint')}
            </p>
          </div>
          <Button variant="secondary" size="sm" disabled title={t('data.unavailableHint')}>
            {t('data.unavailable')}
          </Button>
        </li>
      </ul>
    </SettingsPanel>
  )
}

export default DataMaintenancePanel
