import { Link } from 'react-router-dom'

import { SCAN_STATUS_TONE, totalFindings, type Scan } from '@/data/scan'
import { formatDuration } from '@/lib/datetime'
import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { ExternalLink, MousePointerClick } from 'lucide-react'

/**
 * 任务页右栏的详情预览（v1.90，UI 移植阶段 2）。
 *
 * ## 为什么先做"预览"而不是完整详情
 *
 * 截图里的右栏是完整的执行详情（步骤、日志、证据）。Suvex 的这些内容已经在
 * `/activity?scan=…` 上有成熟实现，**再写一份只会分叉**。这里先给出
 * 「这一条是什么、跑到哪、有多少发现、下一步点哪」—— 四件事能回答，
 * 用户就知道要不要点进详情；等右栏承载更多交互时再嵌活动流。
 *
 * ## 空态为什么必须占位
 *
 * 只画有选中的时候，右栏会突然出现/消失，左侧列表宽度跟着跳 ——
 * 那是最典型的"布局抖动"。空态固定占同一块宽度，选中只是换内容。
 */
export function ScanPreviewPane({
  scan,
  className,
}: {
  scan: Scan | null
  className?: string
}) {
  const { t } = useI18n()
  return (
    <aside
      aria-label={t('scanPreview.aria')}
      className={cn(
        'flex min-h-[22rem] flex-col rounded-md border border-line bg-surface p-panel',
        className,
      )}
      data-scan-preview=""
    >
      {scan === null ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
          <MousePointerClick size={20} strokeWidth={1.5} className="text-ink-ghost" aria-hidden="true" />
          <p className="text-sm text-ink-muted">{t('scanPreview.empty')}</p>
          <p className="max-w-[26ch] text-xs text-ink-faint">
            {t('scanPreview.emptyHint')}
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-sm text-ink">{scan.name || scan.target}</p>
              <p className="mono-ui mt-0.5 truncate text-2xs text-ink-faint">{scan.target}</p>
            </div>
            <span
              className={cn(
                'shrink-0 rounded-xs px-1.5 py-0.5 text-2xs',
                SCAN_STATUS_TONE[scan.status],
              )}
            >
              {t(`scanStatus.${scan.status}`)}
            </span>
          </div>

          <dl className="mt-panel flex flex-col gap-1.5 text-xs">
            <div className="flex items-center justify-between gap-2">
              <dt className="text-ink-faint">{t('scanPreview.findings')}</dt>
              <dd className="mono-ui tabular text-ink" data-numeric>
                {totalFindings(scan.findings)}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-2">
              <dt className="text-ink-faint">{t('scanPreview.duration')}</dt>
              <dd className="mono-ui tabular text-ink-muted">
                {scan.durationSec === null || scan.durationSec === undefined
                  ? '—'
                  : formatDuration(scan.durationSec)}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-2">
              <dt className="text-ink-faint">{t('scanPreview.kind')}</dt>
              <dd className="text-ink-muted">{scan.targetKind}</dd>
            </div>
          </dl>

          <div className="mt-auto pt-panel">
            <Link
              to={`/activity?scan=${encodeURIComponent(scan.id)}`}
              className="inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-sm border border-line px-2 text-xs text-ink-muted transition-colors hover:bg-surface-sunken hover:text-ink"
            >
              {t('scanPreview.open')}
              <ExternalLink size={12} strokeWidth={2} aria-hidden="true" />
            </Link>
          </div>
        </>
      )}
    </aside>
  )
}

export default ScanPreviewPane
