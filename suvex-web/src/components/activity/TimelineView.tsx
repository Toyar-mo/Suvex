import { cn } from '@/lib/cn'
import { useMemo } from 'react'
import type { ActivityEvent } from '@/data/activity'
import { EVENT_TYPE_TONE } from '@/data/activity'
import {
  buildTimeline,
  formatGap,
  gapIsSuspicious,
  timelineEmptyKind,
  type TimelineGap,
  type TimelineModel,
} from '@/data/timeline'
import { AlertTriangle } from 'lucide-react'

/**
 * 时间线视图 —— 一条按**真实时间**摆开的事件轴。
 *
 * ## 它回答的问题，事件列表回答不了
 *
 * 列表按到达顺序一行行紧挨着排，「32 分钟没有任何事件」和「200 毫秒间隔」
 * 在列表上长得一模一样。而线上真实事故恰恰是这个形态：一次扫描跑了 45 分钟，
 * 中间 32 分钟零产出，agent 在空转。用户盯着的是一条看起来还在滚的事件流。
 *
 * 这张图把时间当成横轴，那段静默就是**一条很宽的空白带** —— 眼睛不需要
 * 任何解释就能看出来。所以本组件的重点不是那些点，而是 `gaps`。
 *
 * ## 为什么不是"每分钟一列"的柱状图
 *
 * 柱状图会让人去比较柱子的高度，而这里没有"每小时多少事件"这个量有意义 ——
 * 事件密度取决于 agent 干了什么，不是一个可比较的指标。
 * 点 + 空白带表达的是"什么时候有动静、什么时候完全没有"，这才是要看的。
 *
 * ## 克制
 *
 * 与事件流一致：只有三种颜色（中性 / accent / risk），且 risk 只用在
 * **可疑静默**上。如果给每类事件都上色，这条轴上会挤满八种颜色，
 * 而用户唯一需要一眼看见的"这段明显不对"就淹没了。
 */

/**
 * 一段静默两侧的边界事件。
 *
 * ★ 为什么由本组件算好再交出去，而不是让父级自己从 `events` 里找：
 *
 *   `buildTimeline` 内部按 `at` **重排过**（SSE 重连补发的到达顺序不等于
 *   发生顺序），下标是重排后的。父级拿原始 `events` 用 `gap.afterIndex`
 *   去取，静默前后两条事件就会错位 —— 而且错得很隐蔽：多数扫描的事件
 *   本来就是顺序到达的，只有重连补发那几次才对不上，正好是最需要看准的时候。
 *
 *   同一个 `model` 只算一次，边界就不可能和轴上的点不一致。
 */
export interface TimelineGapBoundary {
  /** 静默开始前最后一条事件（无则 null） */
  before: ActivityEvent | null
  /** 静默结束后第一条事件（无则 null —— 表示静默一直持续到现在） */
  after: ActivityEvent | null
}

export interface TimelineViewProps {
  events: readonly ActivityEvent[]
  /** 当前扫描的状态 —— 决定静默段是否算"可疑" */
  scanStatus: string | null | undefined
  /** 是否指定了某次扫描（决定空态措辞） */
  scanMarked: boolean
  /** 轴终点。扫描在跑时传 now，否则不传（用最后一条事件） */
  now?: number
  /**
   * 点某段静默时的回调。
   *
   * 第二参数是该段静默两侧的事件，见 `TimelineGapBoundary` 的说明。
   */
  onSelectGap?: (gap: TimelineGap, boundary: TimelineGapBoundary) => void
  className?: string
}

export function TimelineView({
  events,
  scanStatus,
  scanMarked,
  now,
  onSelectGap,
  className,
}: TimelineViewProps) {
  const model = useMemo(
    () => buildTimeline(events, now === undefined ? {} : { now }),
    [events, now],
  )

  const emptyKind = timelineEmptyKind(events.length, scanMarked, scanStatus)

  /*
    ★ 边界事件在这里算一次，轴和明细列表共用。

    两处都能点同一段静默，如果各自去 `points` 里取下标，就多了一份
    "哪两条算边界"的知识 —— 而这份知识本来只属于 `model.points` 的排列。
  */
  const selectGap = useMemo(() => {
    if (!onSelectGap) return undefined
    return (gap: TimelineGap) => {
      onSelectGap(gap, {
        before: model.points[gap.afterIndex - 1]?.event ?? null,
        after: model.points[gap.afterIndex]?.event ?? null,
      })
    }
  }, [onSelectGap, model])

  if (emptyKind !== 'none') {
    return (
      <div
        className={cn(
          'flex h-full items-center justify-center px-panel py-bay text-center',
          className,
        )}
      >
        <p className="max-w-prose text-sm text-ink-muted">
          {emptyKind === 'no-scan'
            ? '没有指定扫描 —— 时间线要对着一次扫描的时间跨度画，先在上面选一次。'
            : emptyKind === 'starting'
              ? '扫描刚开始，还没有事件。时间线会随着事件到达自己长出来。'
              : '这次扫描没有任何事件产出。这不是"还在等"——它已经结束了，而整段时间是空的。'}
        </p>
      </div>
    )
  }

  const suspicious = model.gaps.filter((g) => gapIsSuspicious(g.durationMs, scanStatus))
  const longest = suspicious.reduce<TimelineGap | null>(
    (acc, g) => (acc === null || g.durationMs > acc.durationMs ? g : acc),
    null,
  )

  return (
    <div className={cn('flex min-h-0 flex-col gap-3 overflow-y-auto px-panel py-3', className)}>
      {/*
        ★ 顶部结论行。这一行是整张图的 tl;dr ——
        用户不需要读轴就知道"这次到底有没有卡住"。
        没有这段静默时也明说一句，因为"没有"本身是个结论。
      */}
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {suspicious.length > 0 && longest ? (
          <>
            <span className="inline-flex items-center gap-1 text-sm text-signal-risk">
              <AlertTriangle size={13} strokeWidth={2.25} aria-hidden="true" />
              {suspicious.length === 1
                ? `有一段 ${formatGap(longest.durationMs)} 没有产出`
                : `${suspicious.length} 段静默，最长 ${formatGap(longest.durationMs)}`}
            </span>
            <span className="text-xs text-ink-muted">
              而扫描当时在跑 —— 判断一下是深扫工具在跑，还是 agent 卡住了。
            </span>
          </>
        ) : (
          <span className="text-sm text-ink-muted">
            没有超过 {formatGap(SILENCE_HINT_MS)} 的静默
            {scanStatus === 'running' ? '' : '（扫描已结束，之后的安静是正常的）'}
          </span>
        )}
        <div className="flex-1" />
        <span className="mono-ui tabular text-2xs text-ink-faint" data-numeric>
          共 {events.length} 条 · {formatGap(model.durationMs)}
        </span>
      </div>

      <TimelineAxis model={model} scanStatus={scanStatus} onSelectGap={selectGap} />

      {/*
        静默明细列表。轴上的宽度给人"多久"的直觉，但精确数字要能读出来 ——
        尤其当静默段很窄时（占屏 3% 也意味着十几分钟）。
        按时间倒序：最近的静默最可能是当前还没解决的问题。
      */}
      {model.gaps.length > 0 && (
        <ul className="flex flex-col gap-1">
          {[...model.gaps].reverse().map((gap) => {
            const isSuspicious = gapIsSuspicious(gap.durationMs, scanStatus)
            return (
              <li key={`${gap.startMs}-${gap.endMs}`}>
                <button
                  type="button"
                  onClick={() => selectGap?.(gap)}
                  disabled={!selectGap}
                  className={cn(
                    'flex w-full items-baseline gap-2 rounded-xs px-1.5 py-1 text-left text-xs',
                    selectGap && 'transition-ui duration-fast hover:bg-surface-overlay',
                    isSuspicious ? 'text-signal-risk' : 'text-ink-muted',
                  )}
                >
                  <span className="mono-ui tabular shrink-0" data-numeric>
                    {formatGap(gap.startMs)} → {formatGap(gap.endMs)}
                  </span>
                  <span className="shrink-0">
                    {isSuspicious ? '静默' : '间隔'} {formatGap(gap.durationMs)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-ink-faint">
                    {gap.afterIndex >= model.points.length
                      ? '最后一条事件之后，一直到现在'
                      : `之后的第一条：${model.points[gap.afterIndex]?.event.summary ?? ''}`}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/**
 * 与 `data/timeline.ts` 的 `SILENCE_THRESHOLD_MS` 同值。
 * 这里单独写一份是为了在 JSX 里当文案用；两处漂移的风险由测试兜住
 * （`timeline.test.ts` 断言了门槛常量本身）。
 */
const SILENCE_HINT_MS = 2 * 60 * 1000

interface TimelineAxisProps {
  model: TimelineModel
  scanStatus: string | null | undefined
  onSelectGap?: (gap: TimelineGap) => void
}

function TimelineAxis({ model, scanStatus, onSelectGap }: TimelineAxisProps) {
  return (
    <div className="relative">
      {/*
        ── 轴本体 ──
        高度 56px：足够放下点与静默带，又不至于占掉半屏事件流。
      */}
      <div className="relative h-14 w-full">
        {/* 基线 */}
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-line" aria-hidden="true" />

        {/* 静默带 —— 铺在基线上方，是这张图的主角 */}
        {model.gaps.map((gap) => {
          const isSuspicious = gapIsSuspicious(gap.durationMs, scanStatus)
          return (
            <button
              key={`gap-${gap.startMs}-${gap.endMs}`}
              type="button"
              onClick={() => onSelectGap?.(gap)}
              disabled={!onSelectGap}
              title={`${formatGap(gap.startMs)} → ${formatGap(gap.endMs)}，${isSuspicious ? '静默' : '间隔'} ${formatGap(gap.durationMs)}`}
              aria-label={`${isSuspicious ? '静默' : '间隔'} ${formatGap(gap.durationMs)}`}
              className={cn(
                'absolute top-1/2 h-6 -translate-y-1/2 rounded-xs border',
                isSuspicious
                  ? 'border-signal-risk/40 bg-signal-risk/15'
                  : 'border-line/60 bg-surface-overlay/40',
                onSelectGap && 'cursor-pointer transition-ui duration-fast hover:brightness-125',
                // 太窄的带子给一个最小宽度，否则 2 分钟的静默在 45 分钟的轴上
                // 只有 7% 宽，而它在窄屏上会缩到 1px —— 看不见就等于没画
                gap.widthPct < 1 && 'min-w-[2px]',
              )}
              style={{ left: `${gap.startPct}%`, width: `${gap.widthPct}%` }}
            />
          )
        })}

        {/* 事件点 */}
        {model.points.map((point) => {
          const tone = EVENT_TYPE_TONE[point.event.type]
          return (
            <span
              key={point.event.id}
              title={`${formatGap(point.offsetMs)} · ${point.event.summary}`}
              className={cn(
                'absolute top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-pill',
                tone === 'neutral' && 'bg-ink-faint',
                tone === 'accent' && 'bg-accent',
                tone === 'ok' && 'bg-signal-ok',
                tone === 'risk' && 'bg-signal-risk',
              )}
              style={{ left: `${point.pct}%` }}
            />
          )
        })}
      </div>

      {/* ── 刻度 ── */}
      <div className="relative mt-1 h-4" aria-hidden="true">
        {model.ticks.map((tick) => (
          <span
            key={tick.offsetMs}
            className="mono-ui tabular absolute -translate-x-1/2 text-2xs text-ink-faint"
            style={{ left: `${tick.pct}%` }}
          >
            {tick.label}
          </span>
        ))}
      </div>

      {/* ── 图例 ── */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-ink-faint">
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-pill bg-accent" aria-hidden="true" />
          有价值产出
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-pill bg-ink-faint" aria-hidden="true" />
          日常动作
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-pill bg-signal-risk" aria-hidden="true" />
          告警 / 失败
        </span>
        <span className="inline-flex items-center gap-1">
          <span
            className="h-2 w-3 rounded-xs border border-signal-risk/40 bg-signal-risk/15"
            aria-hidden="true"
          />
          运行中的静默
        </span>
      </div>
    </div>
  )
}

export default TimelineView
