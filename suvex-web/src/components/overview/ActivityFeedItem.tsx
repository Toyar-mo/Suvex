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

  return (
    <li className={cn('group', className)}>
      <Link
        to={to}
        className={cn(
          'flex gap-tight rounded-sm px-2 py-2.5',
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
          <p className="text-sm leading-snug text-ink">{event.summary}</p>

          <div className="mono-ui mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-ink-faint">
            <span className="text-ink-muted">{event.agentId}</span>
            <span aria-hidden="true">·</span>
            <span>{PHASE_LABEL[event.agentPhase]}</span>
            <span aria-hidden="true">·</span>
            <time dateTime={event.at} className="tabular">
              {formatRelativeTime(event.at)}
            </time>
            {event.subject && (
              <>
                <span aria-hidden="true">·</span>
                {/* 端点/参数：等宽，超长时省略而不是换行 —— 换行会把每条挤成三行 */}
                <span className="truncate text-ink-faint" title={event.subject}>
                  {event.subject}
                </span>
              </>
            )}
          </div>
        </div>

        {/*
          事件类型的可见文字标签。图标+颜色对色觉障碍用户不够 —— 这里的文字
          正是那个替代通道，所以必须可读：用 ink-muted(8.05:1) 而不是
          ink-ghost(2.17:1)。它既然是信息，就不能待在装饰色里。
          只在宽屏显示，因为窄屏宽度要留给 summary。
        */}
        <span className="mono-ui mt-0.5 hidden shrink-0 text-ink-faint xl:inline">
          {meta.label}
        </span>
      </Link>
    </li>
  )
}

export default ActivityFeedItem
