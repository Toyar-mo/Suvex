import { cn } from '@/lib/cn'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useVirtualRows } from '@/hooks/useVirtualRows'
import type { ActivityEvent } from '@/data/activity'
import { ArrowDown, Pause, Play } from 'lucide-react'
import { useCallback, useEffect, useMemo } from 'react'
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
  /**
   * 需要滚过去并短暂强调的事件 id（从时间线的静默段跳过来时用）。
   *
   * ⚠️ 只表达"要跳到哪"，**不**表达"跳到了" —— 组件自己不知道什么时候
   *    跳完了（平滑滚动是异步的）。父级如果要清掉高亮，按自己的计时器清，
   *    不要等这个组件回调。
   */
  focusEventId?: string | null
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
  focusEventId = null,
  emptyState,
  className,
}: EventStreamProps) {
  const { attachContainer: attachAutoScroll, isAtBottom, scrollToLatest } = useAutoScroll({
    follow: !isPaused,
    contentKey: events.length,
    // 用户往上翻 → 暂停；滚回底部 → 恢复。双向，不用手动点两次
    onUserAway: () => onPauseChange(true),
    onUserBack: () => onPauseChange(false),
  })

  /*
    ── 虚拟滚动 ──

    为什么这一页必须虚拟化：一次 45 分钟的扫描有 330 条事件，折叠态下
    每条 6 个 DOM 节点，约 2000 个；而 `useActivityStream` 的上限是 5000 条，
    极端情况下 3 万个节点。展开/折叠一次会让浏览器重排整列，肉眼可见地掉帧。

    不设"超过 N 条才虚拟化"的阈值：那个阈值在本页没有对应的真实边界
    （330 条就已经难受了，而虚拟化的代价只在滚动时体现）。
  */
  const keys = useMemo(() => events.map((e) => e.id), [events])
  const virtual = useVirtualRows({ count: events.length, keys })

  /*
    ★ 两个 hook 要挂在**同一个**滚动元素上：
    `useAutoScroll` 要监听它判断"用户往回翻了"，`useVirtualRows` 要读它的
    scrollTop 算区间。

    两个 hook 现在都**只**导出回调 ref、不导出 ref 对象 —— 于是组合方式就是
    依次调用两个回调。组件不碰任何 `.current`，也就没有"改别的 hook 内部
    状态"这回事（那正是 `react-hooks/immutability` 之前拦的点）。

    ⚠️ 身份必须稳定：身份一变，React 会先用 null 调旧的、再用新节点调新的，
      等于让容器卸载又挂载一次 —— 滚动位置归零、observer 重新挂。
      两个 `attachContainer` 各自是空依赖 `useCallback`，这个 memo 因此也稳定。
  */
  const { attachContainer: attachVirtual } = virtual
  const setContainer = useCallback(
    (node: HTMLDivElement | null) => {
      attachAutoScroll(node)
      attachVirtual(node)
    },
    [attachAutoScroll, attachVirtual],
  )

  /**
   * 哪些事件是"这一批新到的" —— 由 hook 在事件到达时给出（见 useActivityStream
   * 的 latestBatchIds 注释）。组件不需要自己 diff，也就不会踩
   * 「渲染期读 ref 不安全」和「effect 里 setState 级联渲染」这两个坑。
   *
   * 首批历史回填不在其中，所以刚进页面时十几条不会一起淡入。
   */
  const showJumpButton = isPaused && !isAtBottom

  const slice = events.slice(virtual.startIndex, virtual.endIndex)

  /*
    ── 从时间线跳过来的落点 ──

    为什么在 EventStream 里做，而不是让 ActivityPage 直接调 `scrollToIndex`：
    虚拟滚动的偏移表只存在于这个 hook 内部，页面拿不到下标→像素的映射。
    页面能给的只有"哪条事件"，翻译成像素这一步只能在这里发生。

    ⚠️ `behavior: 'smooth'` 是异步的（几帧到几百毫秒），所以这里**不报告
       "跳完了"**。父级的高亮靠自己的计时器清掉，不依赖本组件的回调 ——
       否则一旦浏览器因为"减少动态效果"把平滑滚动降级成瞬时，回调的时机
       就变了，高亮会跟着忽长忽短。

    ⚠️ 找不到就不跳：`focusEventId` 指向的事件可能被 agent 筛选掉了
       （时间线画的是全部事件，事件流可能只显示一个 agent 的）。
       这时静默地什么都不做比跳到错误位置好 —— 跳错位置比不跳更难发现。
  */
  useEffect(() => {
    if (focusEventId === null) return
    const index = events.findIndex((e) => e.id === focusEventId)
    if (index < 0) return
    virtual.scrollToIndex(index, 'center')
    // 只在"要跳的目标变化"时跳一次。`events` 不进依赖：跳到某条之后
    // 新事件到达不该把用户又拽回去 —— 那时 `focusEventId` 已由父级清掉了，
    // 即使没清，同一个 id 也不该二次跳动。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusEventId])

  return (
    <div className={cn('relative flex min-h-0 flex-1 flex-col', className)}>
      {/* ── 滚动区 ── */}
      <div
        ref={setContainer}
        data-stream-scroll=""
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        // 让读屏器知道这是实时更新的区域，但不逐条打断播报
        aria-live="off"
      >
        {events.length === 0 ? (
          <div className="flex h-full items-center justify-center p-bay">{emptyState}</div>
        ) : (
          <ul className="flex flex-col">
            {/*
              上方未渲染行占位。用 `aria-hidden` —— 它没有内容，只是把
              已渲染部分推到正确位置；给读屏器看到一堆空 li 只会更糟。
            */}
            {virtual.padTop > 0 && (
              <li aria-hidden="true" style={{ height: virtual.padTop }} className="shrink-0" />
            )}

            {slice.map((event) => {
              // 新到的事件挂一次入场动画类；已存在的事件完全不动。
              // 动画本身在 tailwind.config 的 stream-in：240ms / 4px。
              const isNew = latestBatchIds.has(event.id)
              return (
                <EventRow
                  key={event.id}
                  event={event}
                  showAgent={!filtered}
                  className={isNew ? 'animate-stream-in' : undefined}
                  measureRef={virtual.measureRef}
                  highlighted={event.id === focusEventId}
                />
              )
            })}

            {virtual.padBottom > 0 && (
              <li aria-hidden="true" style={{ height: virtual.padBottom }} className="shrink-0" />
            )}
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
