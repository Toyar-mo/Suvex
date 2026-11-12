import { cn } from '@/lib/cn'
import { useI18n } from '@/i18n'
import type { ActivityEvent } from '@/data/overview'
import { ArrowRight, WifiOff } from 'lucide-react'
import { Link } from 'react-router-dom'
import ActivityFeedItem from './ActivityFeedItem'
import ActivityFlowStage from './ActivityFlowStage'

/** 稳定空集：作为可选 prop 的默认值，不能每次渲染新建一个 Set。 */
const EMPTY_BATCH: ReadonlySet<string> = new Set()

/**
 * 实时活动流（精简版）。
 *
 * 这是 Overview 上唯一「活」的东西 —— 它证明系统正在跑，而不是一份报表。
 * 因此：
 *   - 顶部带一个呼吸的状态点，只在真有 agent 在跑时才呼吸；
 *   - v1.82 起，列表上方是一块**实时流程舞台**（`ActivityFlowStage`）：
 *     粒子沿阶段流线持续流动，新事件会从左侧飞向目标节点并炸开；
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
  /**
   * 最近一批新到事件的 id（v1.81）。
   *
   * 只有它里面的行播入场动画；首批历史（种子）不播 ——
   * 一进页面八条一起淡入会很吵，这条规矩沿用 Activity 页。
   */
  latestBatchIds?: ReadonlySet<string>
  /**
   * SSE 连接状态（v1.81）。`null` = 这次没有连实时流
   * （没有活跃扫描，或环境不支持 EventSource）。
   *
   * ★ 断线时**必须**在界面里说出来：标题写着「实时活动」、内容是旧的，
   *   比不显示更糟 —— 用户会拿它当此刻的事实。
   */
  connection?: 'connecting' | 'live' | 'disconnected' | null
}

export function ActivityFeed({
  events,
  isLive,
  limit = 6,
  title = '实时活动',
  className,
  latestBatchIds = EMPTY_BATCH,
  connection = null,
}: ActivityFeedProps) {
  const { t } = useI18n()
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
          {t('overview.activity.viewAll', undefined, '查看全部')}
          <ArrowRight
            size={13}
            strokeWidth={1.75}
            className="transition-transform duration-fast group-hover:translate-x-0.5"
            aria-hidden="true"
          />
        </Link>
      </div>

      {/*
        ── 实时流程舞台（v1.82）──

        ★ 放在活动列表**上方**，因为它回答的是比"哪一条"更上一层的问题：
          「事情走到哪一步了」。列表是细节，舞台是过程。

        ★ 没有事件也不隐藏（只要还在实时）：扫描刚启动的那一两分钟
          正是用户最需要看到"流程长什么样、现在卡在哪一站"的时候。
      */}
      {visible.length > 0 || isLive ? (
        <div className="border-b border-line">
          <ActivityFlowStage
            events={events}
            latestBatchIds={latestBatchIds}
            flowing={connection === 'live'}
          />
        </div>
      ) : null}

      {/*
        ★ 断线提示（v1.81）。只在"本该实时"时出现 ——
          没开实时流时 Connection 是 null，这里不会渲染。
          措辞不断言"正在重连"：EventSource 会自己重试，但我们不知道
          它第几次、什么时候成功；说"正在重连"比说"已断线"更接近事实，
          又不承诺时间。
      */}
      {connection === 'disconnected' && (
        <p
          role="status"
          className="flex items-center gap-1.5 border-b border-line px-panel py-1.5 text-2xs text-signal-warn"
        >
          <WifiOff size={12} strokeWidth={1.75} aria-hidden="true" />
          实时连接中断，正在重连。下面是断线前收到的事件。
        </p>
      )}

      {visible.length === 0 ? (
        <p className="px-panel py-bay text-xs text-ink-faint">
          暂无智能体活动。扫描一开始，事件就会出现在这里。
        </p>
      ) : (
        <ul className="flex flex-col p-1">
          {visible.map((event) => (
            <ActivityFeedItem key={event.id} event={event} isNew={latestBatchIds.has(event.id)} />
          ))}
        </ul>
      )}

      {hasMore && (
        <div className="border-t border-line px-panel py-2">
          <Link
            to="/activity"
            className="text-xs text-ink-faint transition-ui duration-fast hover:text-ink-muted"
          >
            最近还有 {events.length - limit} 条
          </Link>
        </div>
      )}
    </section>
  )
}

export default ActivityFeed
