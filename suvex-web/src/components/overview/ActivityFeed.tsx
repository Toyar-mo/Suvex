import { cn } from '@/lib/cn'
import type { ActivityEvent } from '@/data/overview'
import { ArrowRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import ActivityFeedItem from './ActivityFeedItem'

/**
 * 实时活动流（精简版）。
 *
 * 这是 Overview 上唯一「活」的东西 —— 它证明系统正在跑，而不是一份报表。
 * 因此：
 *   - 顶部带一个呼吸的状态点，只在真有 agent 在跑时才呼吸；
 *   - 「View all」指向 Agent Activity 全量流；
 *   - 列表本身不滚动（页面滚动即可）。嵌套滚动区会吃掉鼠标滚轮，
 *     而且用户想往下看更多时会被困在一个小盒子里。
 */

export interface ActivityFeedProps {
  events: ActivityEvent[]
  /** 是否有 agent 正在运行 —— 决定标题旁的状态点是"进行中"还是"静止" */
  isLive: boolean
  /** 最多显示几条 */
  limit?: number
  title?: string
  className?: string
}

export function ActivityFeed({
  events,
  isLive,
  limit = 6,
  title = '实时活动',
  className,
}: ActivityFeedProps) {
  const visible = events.slice(0, limit)
  const hasMore = events.length > limit

  return (
    <section
      aria-labelledby="activity-feed-title"
      className={cn('flex flex-col rounded-md border border-line bg-surface', className)}
    >
      <div className="flex items-center justify-between gap-tight border-b border-line px-panel py-3">
        <div className="flex items-center gap-tight">
          <span
            aria-hidden="true"
            className={cn(
              'h-1.5 w-1.5 rounded-pill',
              isLive ? 'animate-breath bg-accent' : 'bg-ink-ghost',
            )}
          />
          <h2 id="activity-feed-title" className="text-sm font-medium text-ink">
            {title}
          </h2>
        </div>

        <Link
          to="/activity"
          className="group inline-flex items-center gap-1 text-xs text-ink-muted transition-ui duration-fast hover:text-accent-strong"
        >
          查看全部
          <ArrowRight
            size={13}
            strokeWidth={1.75}
            className="transition-transform duration-fast group-hover:translate-x-0.5"
            aria-hidden="true"
          />
        </Link>
      </div>

      {visible.length === 0 ? (
        <p className="px-panel py-bay text-xs text-ink-faint">
          暂无智能体活动。扫描一开始，事件就会出现在这里。
        </p>
      ) : (
        <ul className="flex flex-col p-1">
          {visible.map((event) => (
            <ActivityFeedItem key={event.id} event={event} />
          ))}
        </ul>
      )}

      {hasMore && (
        <div className="border-t border-line px-panel py-2">
          <Link
            to="/activity"
            className="text-xs text-ink-faint transition-ui duration-fast hover:text-ink-muted"
          >
            实时流中还有 {events.length - limit} 条
          </Link>
        </div>
      )}
    </section>
  )
}

export default ActivityFeed
