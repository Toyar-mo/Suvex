import { cn } from '@/lib/cn'
import { Check, CircleSlash, CircleAlert, Plug, Power } from 'lucide-react'

import type { ToolHealth } from '@/data/tools'

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
    label: '可用',
    icon: Check,
    // 用 ink-muted 而不是 signal-ok：这一页大部分工具本来就该是"可用"，
    // 全绿会让真正需要注意的那几行淹掉（design-system §2：语义色少量使用）
    className: 'text-ink-muted',
    dot: 'bg-signal-ok',
  },
  disabled: {
    label: '已关闭',
    icon: Power,
    className: 'text-ink-faint',
    dot: 'bg-ink-faint',
  },
  unconfigured: {
    label: '待配置',
    icon: Plug,
    className: 'text-signal-warn',
    dot: 'bg-signal-warn',
  },
  misconfigured: {
    label: '配置有误',
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
  enabled: boolean
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
  enabled,
  onToggle,
}: ToolRowProps) {
  const meta = HEALTH_META[health]
  const StatusIcon = meta.icon

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
            aria-label={`启用 ${label}`}
          />
        </label>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-tight gap-y-1">
            {/*
              ★ 标识符用等宽字体原样显示 —— 模型在 ACTION 里写的就是它。
                翻译它会让人对不上日志与提示词（见 `data/tools.ts` 的类型注释）。
            */}
            <code className="font-mono text-[13px] text-ink">{name}</code>
            <span className="text-sm text-ink-muted">{label}</span>
            {core && (
              <span
                className="rounded-sm border border-line px-1 py-px text-[11px] text-ink-faint"
                title="关掉它会让绝大多数任务基本做不了"
              >
                主力
              </span>
            )}
          </div>

          <p className="mt-1 text-sm leading-relaxed text-ink-muted">{summary}</p>

          {params && (
            <p className="mt-1 font-mono text-[12px] text-ink-faint">参数：{params}</p>
          )}

          {/*
            ★ 未就绪时**必须显示原因**，而且要说"去哪修"。
              `reason` 是后端算的（与运行期判据同源），不要在前端重新拼 ——
              两处各拼一次，界面上说的与模型实际遇到的就会不一致。
          */}
          {reason && health !== 'ready' && (
            <p className={cn('mt-1 text-[12px] leading-relaxed', meta.className)}>
              {reason}
              {requiresConnection && health === 'unconfigured' && (
                <span className="text-ink-faint">
                  {' '}
                  （在「MCP 连接」里加一条名为 <code className="font-mono">{requiresConnection}</code> 的连接）
                </span>
              )}
            </p>
          )}
        </div>

        <span
          className={cn('mt-0.5 flex shrink-0 items-center gap-1 text-[12px]', meta.className)}
          title={meta.label}
        >
          <span className={cn('size-1.5 rounded-full', meta.dot)} aria-hidden />
          <StatusIcon className="size-3" aria-hidden strokeWidth={1.75} />
          {meta.label}
        </span>
      </div>
    </li>
  )
}

/** 空状态：清单没拿到（后端不可达或返回空）。 */
export function ToolListEmpty({ reason }: { reason?: string }) {
  return (
    <div className="flex items-start gap-tight px-3 py-6 text-sm text-ink-muted">
      <CircleSlash className="mt-0.5 size-4 shrink-0 text-ink-faint" strokeWidth={1.75} />
      <div>
        <p>没拿到工具清单。</p>
        {reason && <p className="mt-1 text-[12px] text-ink-faint">{reason}</p>}
      </div>
    </div>
  )
}

export default ToolRow
