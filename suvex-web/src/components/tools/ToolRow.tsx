import { cn } from '@/lib/cn'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  CircleAlert,
  Plug,
  Power,
} from 'lucide-react'
import { useState } from 'react'

import type { ToolHealth } from '@/data/tools'
import { useI18n } from '@/i18n'

/**
 * 工具清单的一行。
 *
 * ## 为什么这一行要显示**四**种状态而不是两种
 *
 * "可用 / 不可用"是不够的 —— 因为用户面对每种状态该做的事不同：
 *
 * | 状态 | 含义 | 用户该做什么 |
 * | --- | --- | --- |
 * | `ready` | 开着且依赖就绪 | 什么都不用做 |
 * | `disabled` | 被关掉了 | 去开关里打开 |
 * | `unconfigured` | 开着，但依赖的 MCP 连接没配 | 去「MCP 连接」加一条 |
 * | `misconfigured` | 开着、依赖也配了，但仍不可用（如白名单挡住） | 去看那条连接的详情 |
 *
 * 合成一个灰色"不可用"，用户就不知道该去哪修 —— 而"知道去哪修"
 * 正是这个页面存在的理由。
 *
 * ## 为什么状态用**文字**而不是只用颜色
 *
 * 颜色单独承载状态在色觉障碍下会失效（本项目 design-system §2 也要求
 * 语义不能只靠色相表达）。所以每个状态都有中文标签 + 图标 + 色调三重冗余。
 */

const HEALTH_META: Record<
  ToolHealth,
  { label: string; icon: typeof Check; className: string; dot: string }
> = {
  ready: {
    label: 'tools.health.ready',
    icon: Check,
    // 用 ink-muted 而不是 signal-ok：这一页大部分工具本来就该是"可用"，
    // 全绿会让真正需要注意的那几行淹掉（design-system §2：语义色少量使用）
    className: 'text-ink-muted',
    dot: 'bg-signal-ok',
  },
  disabled: {
    label: 'tools.health.disabled',
    icon: Power,
    className: 'text-ink-faint',
    dot: 'bg-ink-faint',
  },
  unconfigured: {
    label: 'tools.health.unconfigured',
    icon: Plug,
    className: 'text-signal-warn',
    dot: 'bg-signal-warn',
  },
  misconfigured: {
    label: 'tools.health.misconfigured',
    icon: CircleAlert,
    className: 'text-signal-risk',
    dot: 'bg-signal-risk',
  },
}

export interface ToolRowProps {
  name: string
  label: string
  summary: string
  params?: string
  health: ToolHealth
  /** 关掉它会明显废掉主力能力（后端 `core`）。 */
  core: boolean
  /** 依赖的 MCP 连接名（空串 = 无依赖）。 */
  requiresConnection: string
  /** 未就绪的原因（中文，后端给）。 */
  reason: string
  /**
   * ★ v0.99：默认参数下它做不到什么（后端 `defaultLimitation`）。
   *
   * 空串 = 默认就是全功能（`run_shell` 就是这样）。
   * 与 `reason` 是**两件事**：`reason` 是"它现在坏了"，这条是"它能用但会漏"。
   */
  defaultLimitation?: string
  enabled: boolean
  /** 最近 2 万条工具调用事件里的次数。 */
  calls?: number
  lastCallAt?: string | null
  onToggle: (next: boolean) => void
}

export function ToolRow({
  name,
  label,
  summary,
  params,
  health,
  core,
  requiresConnection,
  reason,
  defaultLimitation,
  enabled,
  calls = 0,
  lastCallAt,
  onToggle,
}: ToolRowProps) {
  const { t } = useI18n()
  const meta = HEALTH_META[health]
  const StatusIcon = meta.icon
  const [open, setOpen] = useState(false)
  const hasDetail = Boolean(params) || Boolean(defaultLimitation)

  return (
    <li className="border-b border-line/60 last:border-b-0">
      <div className="flex items-start gap-tight px-3 py-2.5">
        {/*
          开关放在最左：这一列是**动作**，名称是**对象**。
          动作在左让人扫一眼就知道"这一栏我能改什么"。
        */}
        <label className="mt-0.5 flex shrink-0 cursor-pointer items-center gap-tight">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => onToggle(event.target.checked)}
            className="size-3.5 accent-accent"
            aria-label={t('tools.enableAria', { label })}
          />
        </label>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-tight gap-y-1">
            {/*
              ★ 标识符用等宽字体原样显示 —— 模型在 ACTION 里写的就是它。
                翻译它会让人对不上日志与提示词（见 `data/tools.ts` 的类型注释）。
            */}
            <code className="font-mono text-sm text-ink">{name}</code>
            <span className="text-sm text-ink-muted">{label}</span>
            {core && (
              <span
                className="rounded-sm border border-line px-1 py-px text-2xs text-ink-faint"
                title={t('tools.coreTitle')}
              >
                {t('tools.coreBadge')}
              </span>
            )}
            {/*
              ★ v1.79：局限折叠后仍要有**可见标记**。
                全文收进展开区是为了让 13 行能一屏扫完；但把"默认会漏"
                一起藏起来，用户就不会去展开它 —— 而那正是这个字段
                存在的理由（见下方展开区）。
            */}
            <span
              className="ml-auto shrink-0 text-2xs text-ink-faint"
              title={
                lastCallAt
                  ? t('tools.recentCallsLast', {
                      time: new Date(lastCallAt).toLocaleString('zh-CN', { hour12: false }),
                    })
                  : t('tools.recentCalls')
              }
            >
              {t('tools.calls', { n: calls })}
            </span>
            {defaultLimitation && (
              <span
                className="rounded-sm border border-signal-warn/40 px-1 py-px text-2xs text-signal-warn"
                title={t('tools.limitationTitle')}
              >
                {t('tools.limitationBadge')}
              </span>
            )}
          </div>

          <p className="mt-1 text-sm leading-relaxed text-ink-muted">{t(`tools.summary.${name}`, undefined, summary)}</p>

          {/*
            ★ 未就绪时**必须显示原因**，而且要说"去哪修"。
              `reason` 是后端算的（与运行期判据同源），不要在前端重新拼 ——
              两处各拼一次，界面上说的与模型实际遇到的就会不一致。
          */}
          {reason && health !== 'ready' && (
            <p className={cn('mt-1 text-xs leading-relaxed', meta.className)}>
              {reason}
              {requiresConnection && health === 'unconfigured' && (
                <span className="text-ink-faint">
                  {' '}
                  {t('tools.requires.before')}{' '}
                  <code className="font-mono">{requiresConnection}</code>
                  {t('tools.requires.after')}
                </span>
              )}
            </p>
          )}

          {open && (
            <div className="mt-2 flex flex-col gap-2 rounded-sm border border-line/60 bg-surface-raised/20 px-2.5 py-2">
              {params && (
                <p className="break-words font-mono text-xs text-ink-faint [overflow-wrap:anywhere]">
                  {t('tools.params', { params })}
                </p>
              )}
              <span
              className="ml-auto shrink-0 text-2xs text-ink-faint"
              title={
                lastCallAt
                  ? t('tools.recentCallsLast', {
                      time: new Date(lastCallAt).toLocaleString('zh-CN', { hour12: false }),
                    })
                  : t('tools.recentCalls')
              }
            >
              {t('tools.calls', { n: calls })}
            </span>
            {defaultLimitation && (
                <div className="flex items-start gap-tight">
                  <AlertTriangle
                    className="mt-0.5 size-3.5 shrink-0 text-signal-warn"
                    strokeWidth={1.75}
                    aria-hidden="true"
                  />
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-signal-warn">
                      {t('tools.limitationHeading')}
                    </p>
                    <p className="mt-0.5 whitespace-pre-line text-xs leading-relaxed text-ink-muted">
                      {defaultLimitation}
                    </p>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {hasDetail && (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              aria-label={open ? t('tools.collapseAria', { label }) : t('tools.expandAria', { label })}
              className="inline-flex size-6 items-center justify-center rounded-sm text-ink-faint transition-ui duration-fast hover:bg-surface-overlay hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              {open ? (
                <ChevronDown className="size-3.5" strokeWidth={2} aria-hidden="true" />
              ) : (
                <ChevronRight className="size-3.5" strokeWidth={2} aria-hidden="true" />
              )}
            </button>
          )}
          <span
            className={cn('flex items-center gap-1 text-xs', meta.className)}
            title={t(meta.label)}
          >
            <span className={cn('size-1.5 rounded-full', meta.dot)} aria-hidden />
            <StatusIcon className="size-3" aria-hidden strokeWidth={1.75} />
            {t(meta.label)}
          </span>
        </div>
      </div>
    </li>
  )
}

/** 空状态：清单没拿到（后端不可达或返回空）。 */
export function ToolListEmpty({ reason }: { reason?: string }) {
  const { t } = useI18n()
  return (
    <div className="flex items-start gap-tight px-3 py-6 text-sm text-ink-muted">
      <CircleSlash className="mt-0.5 size-4 shrink-0 text-ink-faint" strokeWidth={1.75} />
      <div>
        <p>{t('tools.empty.title')}</p>
        {reason && <p className="mt-1 text-xs text-ink-faint">{reason}</p>}
      </div>
    </div>
  )
}

export default ToolRow
