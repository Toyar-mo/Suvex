import { useState } from 'react'
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  Radar,
  Terminal,
  Wrench,
} from 'lucide-react'
import { useRevealOnExpand } from '@/hooks/useRevealOnExpand'
import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import { showToast } from '@/data/toast-store'
import { copyText } from '@/lib/clipboard'
import type { ChatMessage } from '@/data/chat'
import { digestToolArgs } from './toolCardDigest'
import { setToolExpansion, useToolExpansion } from './toolExpansion'

/**
 * 工具卡：一次调用到底做了什么。
 *
 * ## v1.92：对齐 omp 的工具卡
 *
 * 参考 omp 的 TUI 转录：每张卡是**一行标题 + 一块等宽输出**，标题里有
 * 工具名、人话摘要、状态与耗时；输出默认露出来（短输出）或折起（长输出），
 * 展开后限高滚动，并给出「共 N 行 / 复制」。
 *
 * 与旧版的差别（旧版是一行灰字 + 点开一整块纯文本）：
 *   1. **默认看得见结果**：短输出（≤ 6 行）直接展开。旧版折叠到底，长任务里
 *      用户要点开每一次调用才知道它做了什么。
 *   2. **状态与耗时在标题行**：成功/失败不再只靠颜色，失败会带文字 + 图标，
 *      耗时来自后端真实计时（`durationMs`），不是按消息时间差猜的。
 *   3. **输出是「输出」，不是段落**：等宽字体、保留空白、限高滚动、可复制；
 *      正文 Markdown 的排版规则不该套到终端输出上。
 *   4. **失败必须看得出来**：失败卡片红边 + 红标题；展开后不会被当成成功。
 *
 * ## 为什么不用气泡
 *
 * 工具结果是**证据**，不是对话。给它一个和用户消息同级的圆角气泡，会让
 * 转录变成一堆等权重的块；omp / Claude Code 都用「卡片」把它们压在
 * 视觉层级下方，主线仍是模型说的话。
 */

export function ToolCard({ message, compact = false }: { message: ChatMessage; compact?: boolean }) {
  const { t } = useI18n()
  const failed = message.toolFailed === true
  const content = message.content ?? ''
  const lines = content.split('\n')
  const lineCount = lines.length
  const [localOpen, setLocalOpen] = useState(true)
  const expansion = useToolExpansion()
  // Ctrl+O 的全局覆盖优先于单卡状态；点任意一张卡会回到 auto。
  const open = expansion === 'expanded' ? true : expansion === 'collapsed' ? false : localOpen
  const [copied, setCopied] = useState(false)
  const bodyRef = useRevealOnExpand<HTMLDivElement>(open)
  const argDigest = digestToolArgs(message.toolArgs)

  const copy = async () => {
    if (await copyText(content)) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
      return
    }
    showToast({
      message: t('tools.card.copyFailed', undefined, '复制失败，请手动选中后按 Ctrl+C'),
      tone: 'risk',
    })
  }

  const Duration = message.durationMs != null ? formatDuration(message.durationMs) : ''

  return (
    <div
      data-message-row="tool"
      data-seq={message.seq}
      data-tool-name={message.toolName ?? ''}
      data-tool-collapsed={open ? 'false' : 'true'}
      className={cn(
        'min-w-0 overflow-hidden rounded-md border transition-colors duration-150',
        failed
          ? 'border-signal-risk/35 bg-signal-risk/[0.03]'
          : 'border-line/70 bg-surface-raised/15 hover:border-line-strong',
        compact && 'py-0',
      )}
    >
      <button
        type="button"
        data-tool-toggle="true"
        onClick={() => {
          if (expansion !== 'auto') setToolExpansion('auto')
          setLocalOpen((value) => !value)
        }}
        aria-expanded={open}
        className={cn(
          'group flex w-full min-w-0 items-center gap-2 px-2.5 py-1.5 text-left',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
          failed ? 'hover:bg-signal-risk/[0.05]' : 'hover:bg-surface-raised/35',
        )}
      >
        <span className="shrink-0 text-ink-faint">
          {open ? (
            <ChevronDown size={12} strokeWidth={2} aria-hidden="true" />
          ) : (
            <ChevronRight size={12} strokeWidth={2} aria-hidden="true" />
          )}
        </span>
        <ToolIcon name={message.toolName} failed={failed} />
        <span
          className={cn(
            'shrink-0 font-mono text-[12px] font-medium tracking-tight',
            failed ? 'text-signal-risk' : 'text-ink-muted',
          )}
        >
          {message.toolName ?? 'tool'}
        </span>
        {argDigest && (
          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-ink-faint">
            {argDigest}
          </span>
        )}
        {!argDigest && <span className="flex-1" />}

        <span
          className={cn(
            'flex shrink-0 items-center gap-1 text-2xs',
            failed ? 'text-signal-risk' : 'text-ink-faint',
          )}
        >
          {failed ? (
            <AlertCircle size={11} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Check size={11} strokeWidth={2.25} className="text-signal-ok/80" aria-hidden="true" />
          )}
          {failed ? t('tools.card.failed', undefined, '失败') : t('tools.card.done', undefined, '完成')}
          {Duration && <span className="tabular-nums text-ink-faint">· {Duration}</span>}
        </span>
        <span className="shrink-0 text-2xs tabular-nums text-ink-faint">
          {t('tools.card.lines', { n: lineCount }, `${lineCount} 行`)}
        </span>
      </button>

      {open && (
        <div
          ref={bodyRef}
          data-tool-output
          className={cn(
            'border-t',
            failed ? 'border-signal-risk/30' : 'border-line/60',
          )}
        >
          <div className="flex items-center gap-2 bg-surface-sunken/50 px-2.5 py-1">
            <span className="min-w-0 flex-1 truncate text-[10.5px] uppercase tracking-wide text-ink-faint/80">
              {t('tools.card.output', undefined, '输出')}
            </span>
            <button
              type="button"
              onClick={() => void copy()}
              className="flex shrink-0 items-center gap-1 rounded-sm px-1 py-0.5 text-2xs text-ink-faint transition-ui duration-fast hover:text-ink"
            >
              {copied ? (
                <Check size={11} strokeWidth={2.25} aria-hidden="true" />
              ) : (
                <Copy size={11} strokeWidth={2} aria-hidden="true" />
              )}
              {copied
                ? t('tools.card.copied', undefined, '已复制')
                : t('tools.card.copy', undefined, '复制')}
            </button>
          </div>
          {content.trim() ? (
            <pre className="max-h-[380px] overflow-auto whitespace-pre-wrap break-words bg-surface-sunken/30 px-3 py-2 font-mono text-[12px] leading-[1.7] text-ink-muted">
              {content}
            </pre>
          ) : (
            <p className="px-3 py-2 text-xs text-ink-faint">
              {t('tools.card.empty', undefined, '（无输出）')}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function ToolIcon({ name, failed }: { name?: string | null; failed: boolean }) {
  const cls = cn('shrink-0', failed ? 'text-signal-risk' : 'text-ink-faint')
  const props = { size: 12, strokeWidth: 2, 'aria-hidden': true } as const
  switch (name) {
    case 'run_shell':
    case 'bash':
      return <Terminal {...props} className={cls} />
    case 'http_request':
      return <Globe {...props} className={cls} />
    case 'recon_scan':
    case 'port_scan':
      return <Radar {...props} className={cls} />
    case 'read_file':
    case 'write_file':
      return <FileText {...props} className={cls} />
    default:
      return <Wrench {...props} className={cls} />
  }
}

/** 450ms / 1.2s / 1m 03s —— 只保留对"这次快不快"有用的精度。 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}
