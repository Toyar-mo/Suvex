import { cn } from '@/lib/cn'
import { formatRelativeTime } from '@/lib/datetime'
import type { ActivityEvent, ActivityEventType } from '@/data/overview'
import {
  CheckCircle2,
  FlaskConical,
  Search,
  ShieldAlert,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import { Link } from 'react-router-dom'

/**
 * 实时活动流的单条。
 *
 * 可复用契约：吃一个 ActivityEvent + 跳转目标，不认识页面。
 * 点击跳转到 Agent Activity 页并带上 scanId —— 用 query 而不是路径参数，
 * 因为任务 3 还没定路由形状；query 在那边改成 param 时这里只改一处。
 *
 * 信息层级（扫视顺序）：
 *   1. 动作类型图标（左侧，一眼分类）
 *   2. summary（主句，唯一的长文本）
 *   3. agentId + 时间（右侧/次行，等宽，不抢主语）
 *   4. subject（涉及的端点/参数，等宽、可省略，给"想深究的人"）
 *
 * 为什么不用彩色左边框表示状态：design-system §7 明确禁止 >1px 的彩色竖条
 * （那是 AI 生成界面最明显的特征之一）。这里改用**图标颜色 + 图标形状**表达，
 * 颜色只是辅助。
 */

const EVENT_META: Record<ActivityEventType, { icon: LucideIcon; tone: string; label: string }> = {
  discovery: { icon: Search, tone: 'text-signal-warn', label: '发现' },
  tool_call: { icon: Wrench, tone: 'text-ink-faint', label: '工具调用' },
  poc_generated: { icon: FlaskConical, tone: 'text-accent-strong', label: '生成 PoC' },
  verified: { icon: CheckCircle2, tone: 'text-signal-ok', label: '已验证' },
  blocked: { icon: ShieldAlert, tone: 'text-signal-risk', label: '被策略拦截' },
  completed: { icon: CheckCircle2, tone: 'text-signal-ok', label: '已完成' },
}

/** 阶段的人话名 —— 直接显示 'exploit' 这种词会让非 Suvex 用户卡住 */
const PHASE_LABEL: Record<ActivityEvent['agentPhase'], string> = {
  recon: '信息收集',
  scan: '扫描中',
  exploit: '利用中',
  verify: '验证中',
  report: '出报告中',
}

export interface ActivityFeedItemProps {
  event: ActivityEvent
  /** 跳转目标构造函数，默认带 scanId query */
  href?: string
  className?: string
}

export function ActivityFeedItem({ event, href, className }: ActivityFeedItemProps) {
  const meta = EVENT_META[event.type]
  const Icon = meta.icon
  const to =
    href ??
    `/activity?scan=${encodeURIComponent(event.scanId)}&agent=${encodeURIComponent(event.agentId)}`
  const { toolName, body: summaryBody } = splitToolSummary(event)
  const title =
    event.subject && !event.summary.includes(event.subject)
      ? `${event.summary}\n${event.subject}`
      : event.summary

  return (
    <li className={cn('group', className)}>
      <Link
        to={to}
        className={cn(
          'flex gap-tight rounded-sm px-2 py-2',
          'transition-ui duration-fast hover:bg-surface-raised',
        )}
      >
        <Icon
          size={16}
          strokeWidth={1.75}
          className={cn('mt-0.5 shrink-0', meta.tone)}
          aria-hidden="true"
        />

        <div className="min-w-0 flex-1">
          {/*
            工具名单独一列，后面才是参数。

            ★ 2026-11-07 用户反馈「最近活动很乱」。乱的根源不是字多，是
              每条都在同一行里塞两种东西：`run_shell B=http://...` 是
              一条没有层级的长文本，扫视时看不出"它到底调了什么"。
              拆开之后：动作名（等宽、固定位置）→ 参数（可截断）。

            ★ 摘要限两行：活动流是**扫视**用的，不是读的。完整内容放
              `title`，要深究可以悬停/进活动页。限行也让每条高度稳定，
              列表不会因为一条超长命令就高低不齐。
          */}
          <p
            className={cn(
              'break-words text-sm leading-snug [overflow-wrap:anywhere]',
              // 工具调用压成**一行**：参数是命令，给它两行就会把整块撑成一堵墙。
              // 其余类型是给人读的一句话，留两行更完整。
              toolName ? 'line-clamp-1' : 'line-clamp-2',
            )}
            title={title}
          >
            {toolName && (
              <span className="mono-ui mr-1.5 font-medium text-ink">{toolName}</span>
            )}
            <span className={toolName ? 'text-ink-muted' : 'text-ink'}>{summaryBody}</span>
          </p>

          <div className="mono-ui mt-1 flex min-w-0 items-center gap-x-2 gap-y-0.5 text-ink-faint">
            <span className="shrink-0 text-ink-muted">{shortAgentId(event.agentId)}</span>
            <span aria-hidden="true" className="shrink-0">·</span>
            <span className="shrink-0">{PHASE_LABEL[event.agentPhase]}</span>
            <span aria-hidden="true" className="shrink-0">·</span>
            <time dateTime={event.at} className="shrink-0 tabular">
              {formatRelativeTime(event.at)}
            </time>
            <span className="ml-auto shrink-0">{meta.label}</span>
          </div>
        </div>
      </Link>
    </li>
  )
}

/**
 * 工具调用摘要拆成「动作名 + 参数」。
 *
 * `run_shell B=http://…` → `run_shell` + `B=http://…`。
 * 只对 tool_call 拆：别的类型（扫描完成、发现）本来就是一句人话。
 */
function splitToolSummary(event: ActivityEvent): { toolName: string | null; body: string } {
  if (event.type !== 'tool_call') return { toolName: null, body: event.summary }
  const trimmed = event.summary.trim()
  const space = trimmed.indexOf(' ')
  if (space <= 0) return { toolName: null, body: trimmed }
  return { toolName: trimmed.slice(0, space), body: trimmed.slice(space + 1) }
}

/**
 * agent id 只留尾部。
 *
 * `agent_01M6QS0K57C8KWBBH0HV3T0EC` 有 32 个字符，整段摆出来会把
 * 「阶段 · 时间」挤掉，而人真正用来对照的只是尾部几位。
 */
function shortAgentId(id: string): string {
  return id.length <= 16 ? id : `…${id.slice(-10)}`
}

export default ActivityFeedItem
