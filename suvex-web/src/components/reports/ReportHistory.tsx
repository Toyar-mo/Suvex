import { cn } from '@/lib/cn'
import { SEVERITY_META, SEVERITY_ORDER } from '@/data/findings'
import { formatDateTime, type Report } from '@/data/report'
import { formatRelativeTime } from '@/lib/datetime'
import { FileCode2, FileText, Trash2, ArrowRight } from 'lucide-react'
import { useState } from 'react'

/**
 * 历史报告列表。
 *
 * 每条一行的横向布局，窄屏改卡片堆叠 —— 与 ScanList 的响应式策略一致。
 *
 * 交互上最重要的一条：**打开和下载是两个独立动作。**
 *   - 点整行 = 打开预览（用户想"看看这份报告里写了什么"）；
 *   - 下载按钮 = 按这份报告**当时**的格式重新导出。
 * 合成一个"点击即下载"会让人无法确认内容 —— 报告是要发出去的，
 * 发之前总得看一眼。
 */
export interface ReportHistoryProps {
  reports: Report[]
  onOpen: (report: Report) => void
  onDownload: (report: Report) => void
  onDelete: (report: Report) => void
  /** 正在下载中的报告 id —— 期间禁用按钮，避免重复触发 */
  downloadingId: string | null
  /**
   * 范围的人话描述。
   *
   * 由页面注入，而不是这里直接调 describeScope(report.scope)：
   * 扫描类范围需要把 scanId 解析成扫描**名**，而那需要 scans 数据 ——
   * 列表组件不该自己去拉。早先这里没传，于是列表显示
   * 「扫描任务「scan_01J8W9」」而预览显示「扫描任务「acme-web」」——
   * 同一份报告在列表和预览里是两种说法。
   */
  describeScopeOf: (report: Report) => string
  emptyState: React.ReactNode
}

export function ReportHistory({
  reports,
  onOpen,
  onDownload,
  onDelete,
  downloadingId,
  describeScopeOf,
  emptyState,
}: ReportHistoryProps) {
  if (reports.length === 0) {
    return <div className="px-panel py-bay">{emptyState}</div>
  }

  return (
    <ul className="flex flex-col">
      {/* 表头仅宽屏。没有表头的话，右侧几个宽度固定的列不知道是什么 */}
      <li
        aria-hidden="true"
        className="hidden items-center gap-panel border-b border-line px-panel py-2 text-2xs text-ink-faint lg:flex"
      >
        <span className="min-w-0 flex-1">报告</span>
        <span className="w-44 shrink-0">发现</span>
        <span className="w-36 shrink-0">生成于</span>
        <span className="w-40 shrink-0 text-right">操作</span>
      </li>

      {reports.map((report) => (
        <ReportRow
          key={report.id}
          report={report}
          onOpen={onOpen}
          onDownload={onDownload}
          onDelete={onDelete}
          downloading={downloadingId === report.id}
          scopeText={describeScopeOf(report)}
        />
      ))}
    </ul>
  )
}

function ReportRow({
  report,
  onOpen,
  onDownload,
  onDelete,
  downloading,
  scopeText,
}: {
  report: Report
  onOpen: (report: Report) => void
  onDownload: (report: Report) => void
  onDelete: (report: Report) => void
  downloading: boolean
  scopeText: string
}) {
  const [confirming, setConfirming] = useState(false)

  const total = report.findingCount
  const FormatIcon = report.format === 'pdf' ? FileText : FileCode2

  return (
    <li className="border-b border-line/60 last:border-b-0">
      <div
        className={cn(
          'group flex flex-col gap-tight px-panel py-3 transition-ui duration-fast',
          'hover:bg-surface-raised/50',
          'lg:flex-row lg:items-center lg:gap-panel lg:py-2.5',
        )}
      >
        {/* 报告名 —— 主信息，也是打开预览的入口 */}
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={() => onOpen(report)}
            className="block max-w-full truncate text-left text-sm text-ink transition-ui duration-fast hover:text-accent-strong"
            title={`打开「${report.title}」的预览`}
          >
            {report.title}
          </button>

          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-ink-faint">
            {/* 报告编号用等宽：客户引用它时逐字符读，等宽更好认。
                编号由**后端分配**（`report.code`），前端不再派生 —— 见 data/report.ts */}
            <span className="mono-ui">{report.code}</span>
            <span aria-hidden="true">·</span>
            <span>{scopeText}</span>
            <span aria-hidden="true">·</span>
            {/* 格式如实标注 —— "重新下载"会按它导出，不能只在按钮上体现 */}
            <span className="inline-flex items-center gap-1">
              <FormatIcon size={10} strokeWidth={2} aria-hidden="true" />
              {report.format === 'pdf' ? 'PDF' : 'Markdown'}
            </span>
          </span>
        </div>

        {/* 发现摘要 */}
        <div className="flex shrink-0 flex-wrap items-center gap-1.5 lg:w-44">
          {total === 0 ? (
            /*
              "0 条"有两种截然不同的含义，列表必须区分开：
                · 范围内本来就没有发现 → "未发现问题"
                · 有发现但被选项筛掉了 → "0 条（排除 N 条）"
              混成一句"未包含条目"，会让读者以为这次扫描什么都没查到。
            */
            <span className="text-2xs text-ink-faint">
              {report.excludedCount > 0 ? `0 条（排除 ${report.excludedCount} 条）` : '未发现问题'}
            </span>
          ) : (
            <>
              {SEVERITY_ORDER.filter((sev) => (report.severityCounts[sev] ?? 0) > 0).map((sev) => (
                <span
                  key={sev}
                  className="flex items-center gap-1 text-2xs"
                  title={`${SEVERITY_META[sev].label} ${report.severityCounts[sev]}`}
                >
                  <span
                    aria-hidden="true"
                    className={cn('h-1.5 w-1.5 rounded-pill', SEVERITY_META[sev].bg)}
                  />
                  <span className={cn('tabular', SEVERITY_META[sev].text)} data-numeric>
                    {report.severityCounts[sev]}
                  </span>
                </span>
              ))}
              {/* 有排除时补一句，避免"列表 3 条、报告正文说命中 5 条"的困惑 */}
              {report.excludedCount > 0 && (
                <span className="text-2xs text-ink-faint">排除 {report.excludedCount} 条</span>
              )}
            </>
          )}
        </div>

        {/* 生成时间 */}
        <time
          dateTime={report.createdAt}
          className="tabular shrink-0 text-2xs text-ink-faint lg:w-36 lg:text-right"
          title={formatDateTime(report.createdAt)}
        >
          {formatRelativeTime(report.createdAt)}
        </time>

        {/* 操作 */}
        <div className="flex shrink-0 items-center gap-1 lg:w-40 lg:justify-end">
          <button
            type="button"
            onClick={() => onDownload(report)}
            disabled={downloading}
            className={cn(
              'inline-flex h-8 items-center gap-1 rounded-sm px-2 text-2xs transition-ui duration-fast',
              downloading
                ? 'cursor-wait text-ink-faint'
                : 'text-ink-muted hover:bg-surface-overlay hover:text-accent-strong',
            )}
          >
            {downloading ? '生成中…' : '下载'}
          </button>

          <button
            type="button"
            onClick={() => onOpen(report)}
            className="inline-flex h-8 items-center gap-1 rounded-sm px-2 text-2xs text-ink-muted transition-ui duration-fast hover:bg-surface-overlay hover:text-accent-strong"
          >
            打开
            <ArrowRight size={11} strokeWidth={2} aria-hidden="true" />
          </button>

          {/*
            删除做成两步确认。
            报告是交付物，重建它需要一个"当初的选项组合"——
            虽然理论上能再生成一份，但生成的未必和当初那份一模一样
            （漏洞状态可能变了）。所以误删的代价比列表项看起来的大。
            这里用就地确认而不是弹窗：报告列表本身不复杂，
            再叠一层模态只会打断。
          */}
          {confirming ? (
            <span className="inline-flex items-center gap-1">
              <button
                type="button"
                onClick={() => onDelete(report)}
                className="inline-flex h-8 items-center rounded-sm bg-signal-risk/15 px-2 text-2xs text-signal-risk transition-ui duration-fast hover:bg-signal-risk/25"
              >
                确认删除
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="inline-flex h-8 items-center rounded-sm px-2 text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
              >
                取消
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              aria-label={`删除「${report.title}」`}
              className="inline-flex h-8 w-8 items-center justify-center rounded-sm text-ink-faint transition-ui duration-fast hover:bg-surface-overlay hover:text-signal-risk"
            >
              <Trash2 size={13} strokeWidth={1.75} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </li>
  )
}

export default ReportHistory
