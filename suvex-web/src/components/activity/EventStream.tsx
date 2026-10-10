import { cn } from '@/lib/cn'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import type { ActivityEvent } from '@/data/activity'
import { ArrowDown, Pause, Play } from 'lucide-react'
import EventRow from './EventRow'

/**
 * 事件流容器。
 *
 * 动效策略（brief 明确要求克制，这里是全部动效的清单）：
 *   - **只有新到达的事件**播放一次 `stream-in`（淡入 + 上移 4px，240ms）。
 *   - 已有事件在列表里位置变化时**不做动画**（不做 FLIP），否则滚动时整列都在动。
 *   - 不用 AnimatePresence 之类的库：那需要给每个元素维护存在状态，
 *     在高频追加下开销明显，而这里的效果用一条 CSS animation 就够了。
 *
 * "新"的判定：记住上一批事件的 id 集合，本批里不在其中的即为新。
 * 用 ref 存集合，避免把它放进渲染依赖。
 */

export interface EventStreamProps {
  events: ActivityEvent[]
  /**
   * 本批新到达的事件 id（由 useActivityStream 提供）。
   * 只有这些条目会播一次入场动画。
   */
  latestBatchIds: ReadonlySet<string>
  /** 是否按 agent 过滤掉了内容 —— 决定是否显示 agent 列 */
  filtered: boolean
  /** 用户是否暂停（暂停时显示"回到最新"） */
  isPaused: boolean
  /** 暂停期间新增的条数 */
  newWhilePaused: number
  onPauseChange: (paused: boolean) => void
  /** 空态内容 */
  emptyState?: React.ReactNode
  className?: string
}

export function EventStream({
  events,
  latestBatchIds,
  filtered,
  isPaused,
  newWhilePaused,
  onPauseChange,
  emptyState,
  className,
}: EventStreamProps) {
  const { containerRef, isAtBottom, scrollToLatest } = useAutoScroll({
    follow: !isPaused,
    contentKey: events.length,
    // 用户往上翻 → 暂停；滚回底部 → 恢复。双向，不用手动点两次
    onUserAway: () => onPauseChange(true),
    onUserBack: () => onPauseChange(false),
  })

  /**
   * 哪些事件是"这一批新到的" —— 由 hook 在事件到达时给出（见 useActivityStream
   * 的 latestBatchIds 注释）。组件不需要自己 diff，也就不会踩
   * 「渲染期读 ref 不安全」和「effect 里 setState 级联渲染」这两个坑。
   *
   * 首批历史回填不在其中，所以刚进页面时十几条不会一起淡入。
   */
  const showJumpButton = isPaused && !isAtBottom

  return (
    <div className={cn('relative flex min-h-0 flex-1 flex-col', className)}>
      {/* ── 滚动区 ── */}
      <div
        ref={containerRef}
        data-stream-scroll=""
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        // 让读屏器知道这是实时更新的区域，但不逐条打断播报
        aria-live="off"
      >
        {events.length === 0 ? (
          <div className="flex h-full items-center justify-center p-bay">{emptyState}</div>
        ) : (
          <ul className="flex flex-col">
            {events.map((event) => {
              // 新到的事件挂一次入场动画类；已存在的事件完全不动。
              // 动画本身在 tailwind.config 的 stream-in：240ms / 4px。
              const isNew = latestBatchIds.has(event.id)
              return (
                <EventRow
                  key={event.id}
                  event={event}
                  showAgent={!filtered}
                  className={isNew ? 'animate-stream-in' : undefined}
                />
              )
            })}
          </ul>
        )}
      </div>

      {/*
        底部渐变遮罩 + "回到最新"按钮。
        遮罩是功能性的（提示内容被裁切了），不是装饰 —— 所以它是渐隐而不是半透明色块。
      */}
      {events.length > 0 && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-canvas to-transparent"
        />
      )}

      {showJumpButton && (
        <button
          type="button"
          onClick={() => {
            onPauseChange(false)
            scrollToLatest()
          }}
          className={cn(
            'absolute bottom-4 left-1/2 -translate-x-1/2',
            'inline-flex items-center gap-tight rounded-pill border border-line bg-surface-overlay px-3 py-1.5',
            'text-xs text-ink shadow-lift',
            'transition-ui duration-fast hover:border-line-strong hover:text-accent-strong',
          )}
        >
          <ArrowDown size={13} strokeWidth={2} aria-hidden="true" />
          回到最新
          {newWhilePaused > 0 && (
            <span className="tabular text-accent-strong" data-numeric>
              +{newWhilePaused}
            </span>
          )}
        </button>
      )}
    </div>
  )
}

/**
 * 流控工具条。放在流的上方而不是下方 —— 它是"控制这个区域"的工具，
 * 应该跟着区域的头走。
 */
export interface StreamToolbarProps {
  isPaused: boolean
  onPauseChange: (paused: boolean) => void
  eventCount: number
  connection: 'connecting' | 'live' | 'disconnected'
  /** 右侧插槽（筛选器等） */
  children?: React.ReactNode
}

export function StreamToolbar({
  isPaused,
  onPauseChange,
  eventCount,
  connection,
  children,
}: StreamToolbarProps) {
  const statusLabel = isPaused
    ? '已暂停'
    : connection === 'live'
      ? '实时'
      : connection === 'connecting'
        ? '连接中'
        : '已断开'

  const dotClass = isPaused
    ? 'bg-ink-ghost'
    : connection === 'live'
      ? 'animate-breath bg-accent'
      : connection === 'connecting'
        ? 'animate-breath bg-signal-warn'
        : 'bg-signal-risk'

  return (
    /*
      工具条高度：py-1 而不是 py-2。
      按钮已经有 44px 触摸高度，再叠 8px×2 的内距会让整条涨到 60px，
      从事件流里抢走一行。py-1 之后整条约 52px —— 按钮仍是 44px 可点，
      而多出来的高度只是内距，不是内容。
    */
    <div className="flex flex-wrap items-center gap-tight border-b border-line px-panel py-1">
      <span aria-hidden="true" className={cn('h-1.5 w-1.5 shrink-0 rounded-pill', dotClass)} />
      <span className="text-xs text-ink-muted">{statusLabel}</span>

      {/*
        ★ 这个中点不能省（任务 11）。

        原来这两个 span 只靠 flex 的 gap 隔开，视觉上连成「连接中 6 条」——
        读起来像"有 6 个连接正在进行中"。而它其实是**两个互不相干的信息**：
        ① SSE 的连接状态；② 本次会话已收到的事件条数。

        加一个中点，读起来就是两项并列；再给计数挂个 title，把
        "和连接状态无关"这件事说死。
      */}
      <span aria-hidden="true" className="text-ink-ghost">
        ·
      </span>

      <span
        className="mono-ui tabular text-ink-faint"
        data-numeric
        title="本次会话已接收的事件条数（与连接状态无关）"
      >
        {eventCount} 条
      </span>

      <div className="flex-1" />

      {children}

      {/*
        触摸目标：实测 58×30px，高度不达 44px。用 min-h-11 把它撑起来。
        工具条本身 h 固定，所以这里不会把工具条撑高 —— flex 会让它居中。
      */}
      <button
        type="button"
        onClick={() => onPauseChange(!isPaused)}
        aria-pressed={isPaused}
        className={cn(
          // h-11 = 44px：达到 WCAG 2.5.8 的最小触摸目标。
          // 工具条用 items-center，所以这里不会把工具条撑高。
          'inline-flex h-11 items-center gap-1 rounded-sm border px-2.5 text-xs',
          'transition-ui duration-fast',
          isPaused
            ? 'border-accent/40 text-accent-strong hover:border-accent'
            : 'border-line text-ink-muted hover:border-line-strong hover:text-ink',
        )}
      >
        {isPaused ? (
          <>
            <Play size={12} strokeWidth={2} aria-hidden="true" />
            继续
          </>
        ) : (
          <>
            <Pause size={12} strokeWidth={2} aria-hidden="true" />
            暂停
          </>
        )}
      </button>
    </div>
  )
}

export default EventStream
