import type { ActivityEvent } from './activity'

/**
 * 时间线视图的纯逻辑：把一条按到达顺序排列的事件流，换算成一条按**真实时间**
 * 排列的轴。
 *
 * ## 为什么需要另一个视图（列表已经能看事件了）
 *
 * 列表回答的是"发生了什么"，时间线回答的是"**多久没发生什么**"。
 *
 * 这两件事在列表里看不出来：事件之间是一行行紧挨着的，中间那 32 分钟
 * 静默和 200ms 的间隔长得一模一样。而线上真实的故障恰恰是这个形态 ——
 * 一次扫描跑了 45 分钟，其中 32 分钟零产出，agent 在空转，用户在盯着
 * 一个"看起来还在滚"的事件流。列表承载不了这个信息，时间轴能。
 *
 * 所以本模块的核心产物不是那些点，而是 `gaps`：**静默窗口**。
 *
 * ## 为什么静默窗口要区分"可疑"与"正常"
 *
 * 扫描结束后当然没有事件 —— 那不是静默，那是结束了。把所有间隔都标红，
 * 时间线就会变成一条永远在报警的轴，用户立刻学会忽略它。
 * 所以 `gapIsSuspicious` 只在"扫描还在跑"的窗口内成立。
 */

/**
 * 值得单独标出来的静默门槛：2 分钟。
 *
 * 为什么不是 30 秒：一次 `nmap -sV` 或 nuclei 全量模板跑几分钟很正常，
 * 单次工具调用期间本来就没有事件。按 30 秒标，正常的深扫会被涂满。
 *
 * 为什么不是 10 分钟：32 分钟零产出那次事故，前 10 分钟用户已经明显觉得不对了。
 * 等到 10 分钟才提示，等于把"发现问题"的时间推迟到用户自己起疑之后。
 */
export const SILENCE_THRESHOLD_MS = 2 * 60 * 1000

/** 时间轴上一个事件点 */
export interface TimelinePoint {
  event: ActivityEvent
  /** 距扫描起点的偏移（毫秒），恒 >= 0 */
  offsetMs: number
  /** 在轴上的位置百分比 0..100。轴长度为 0 时恒为 0 */
  pct: number
}

/** 一段静默 */
export interface TimelineGap {
  /** 前一条事件的偏移 */
  startMs: number
  /** 后一条事件的偏移 */
  endMs: number
  /** 静默时长（毫秒），恒 > 0 */
  durationMs: number
  /** 位置百分比 */
  startPct: number
  widthPct: number
  /** 这段静默里**没有**事件的下标区间：events[afterIndex-1] 到 events[afterIndex] 之间 */
  afterIndex: number
}

export interface TimelineModel {
  points: TimelinePoint[]
  gaps: TimelineGap[]
  /** 轴起点（最早事件时刻） */
  startMs: number
  /** 轴终点（最晚事件时刻，或外部传入的"现在"） */
  endMs: number
  durationMs: number
  /** 时间刻度 */
  ticks: TimelineTick[]
}

export interface TimelineTick {
  offsetMs: number
  pct: number
  label: string
}

export interface BuildTimelineOptions {
  /**
   * 轴的终点。
   *
   * ⚠️ 扫描还在跑时必须传"现在"，不能只取最后一条事件的时间。
   *    否则一次已经静默 20 分钟的扫描，时间轴会缩到最后一个事件那一瞬间 ——
   *    静默段在图上宽度为 0，正好把要表达的东西抹掉。
   */
  now?: number
  /** 静默门槛，默认 `SILENCE_THRESHOLD_MS` */
  silenceThresholdMs?: number
  /** 刻度数量上限，默认自适应 */
  maxTicks?: number
}

/** 秒 → "6 分 20 秒"，与 `lib/datetime` 的 `formatDuration` 同口径但**不做小时**展开 */
export function formatGap(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  const total = Math.round(ms / 1000)
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  if (minutes < 60) return seconds === 0 ? `${minutes} 分` : `${minutes} 分 ${seconds} 秒`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`
}

/**
 * 把事件流换算成时间线模型。
 *
 * 三件事：
 *   1. 按 `at` 排序（到达顺序不等于发生顺序 —— SSE 重连补发会乱）；
 *   2. 算出每个点的偏移与百分比；
 *   3. 找出超过门槛的静默窗口。
 */
export function buildTimeline(
  events: readonly ActivityEvent[],
  options: BuildTimelineOptions = {},
): TimelineModel {
  const threshold = normaliseThreshold(options.silenceThresholdMs)

  /*
    ★ 时间戳非法的事件被**丢弃**，而不是当成 0。

    当成 0 会让它跑到轴的最左端，看起来像"这条事件发生在扫描刚开始时" ——
    一个看起来完全合理的假事实。它其实是数据缺失，不该被画成一个位置。
  */
  const stampeds: { event: ActivityEvent; ms: number }[] = []
  for (const event of events) {
    const ms = Date.parse(event.at)
    if (Number.isFinite(ms)) stampeds.push({ event, ms })
  }

  if (stampeds.length === 0) {
    return { points: [], gaps: [], startMs: 0, endMs: 0, durationMs: 0, ticks: [] }
  }

  // 稳定排序：同一毫秒的多条事件保持原来的相对顺序（Array.prototype.sort 在
  // 现代引擎里是稳定的，但显式用下标兜底，免得依赖实现细节）
  const ordered = stampeds
    .map((item, index) => ({ ...item, index }))
    .sort((a, b) => a.ms - b.ms || a.index - b.index)

  const startMs = ordered[0].ms
  let lastEventMs = startMs
  for (const item of ordered) if (item.ms > lastEventMs) lastEventMs = item.ms

  const now = Number.isFinite(options.now) ? (options.now as number) : lastEventMs
  // 轴终点不早于最后一条事件：`now` 早于事件时间说明时钟漂移，
  // 用它当终点会让所有点挤在最右端之外
  const endMs = Math.max(now, lastEventMs)
  const durationMs = endMs - startMs

  const toPct = (ms: number): number => {
    if (durationMs <= 0) return 0
    const raw = ((ms - startMs) / durationMs) * 100
    return raw < 0 ? 0 : raw > 100 ? 100 : raw
  }

  const points: TimelinePoint[] = ordered.map((item) => ({
    event: item.event,
    offsetMs: item.ms - startMs,
    pct: toPct(item.ms),
  }))

  const gaps: TimelineGap[] = []
  for (let i = 1; i < ordered.length; i += 1) {
    const gapMs = ordered[i].ms - ordered[i - 1].ms
    if (gapMs >= threshold) {
      const startPct = toPct(ordered[i - 1].ms)
      gaps.push({
        startMs: ordered[i - 1].ms - startMs,
        endMs: ordered[i].ms - startMs,
        durationMs: gapMs,
        startPct,
        widthPct: Math.max(0, toPct(ordered[i].ms) - startPct),
        afterIndex: i,
      })
    }
  }

  /*
    ★ 扫描还在跑时，**最后一条事件到 now 之间**也是一段静默。

    这一段最容易被漏掉，而它恰恰是最要紧的那一段：32 分钟零产出那次，
    事件流在事故发生时并没有"两条事件之间的间隔"——它就是停在最后一条上不动了。
    只算相邻事件的间隔，这段时间在图上完全不存在。
  */
  const tailMs = endMs - lastEventMs
  if (tailMs >= threshold && tailMs > 0) {
    const startPct = toPct(lastEventMs)
    gaps.push({
      startMs: lastEventMs - startMs,
      endMs: durationMs,
      durationMs: tailMs,
      startPct,
      widthPct: Math.max(0, 100 - startPct),
      afterIndex: ordered.length,
    })
  }

  return {
    points,
    gaps,
    startMs,
    endMs,
    durationMs,
    ticks: buildTicks(durationMs, options.maxTicks),
  }
}

function normaliseThreshold(value: number | undefined): number {
  if (Number.isFinite(value) && (value as number) > 0) return value as number
  return SILENCE_THRESHOLD_MS
}

/**
 * 自适应刻度。
 *
 * 逐级选一个"整齐"的步长（1s/5s/15s/30s/1m/5m/15m/30m/1h/3h/6h/12h/1d），
 * 使刻度数落在 3..6 之间。给太多刻度在窄屏上会叠成一团，太少又读不出速度。
 */
const TICK_STEPS_MS = [
  1_000, 5_000, 15_000, 30_000, 60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000, 60 * 60_000,
  3 * 60 * 60_000, 6 * 60 * 60_000, 12 * 60 * 60_000, 24 * 60 * 60_000,
]

export function buildTicks(durationMs: number, maxTicks = 6): TimelineTick[] {
  if (!Number.isFinite(durationMs) || durationMs <= 0) {
    // 零长度轴：给一个 0 刻度，让轴有个起点标签，而不是完全空白
    return [{ offsetMs: 0, pct: 0, label: '0 秒' }]
  }

  const cap = Number.isFinite(maxTicks) && maxTicks >= 2 ? Math.floor(maxTicks) : 6

  // 选一个步长，使刻度数 <= cap
  let step = TICK_STEPS_MS[TICK_STEPS_MS.length - 1]
  for (const candidate of TICK_STEPS_MS) {
    if (durationMs / candidate <= cap - 1) {
      step = candidate
      break
    }
  }

  const ticks: TimelineTick[] = []
  for (let offset = 0; offset <= durationMs; offset += step) {
    ticks.push({ offsetMs: offset, pct: (offset / durationMs) * 100, label: formatGap(offset) })
  }
  // 末尾补一个总时长刻度：它与前一个刻度的间隔不整齐，但用户最想知道"一共多久"
  const last = ticks[ticks.length - 1]
  if (durationMs - last.offsetMs > step * 0.25) {
    ticks.push({ offsetMs: durationMs, pct: 100, label: formatGap(durationMs) })
  }
  return ticks
}

/**
 * 这段静默值不值得提示。
 *
 * 判据不是"有多长"，而是"**这段时间里扫描该不该有产出**"：
 *   - 扫描已结束 / 失败 / 排队中 → 不提示（结束之后当然安静）
 *   - 扫描在跑，但静默没超过门槛 → 不提示
 *   - 扫描在跑且静默超过门槛 → 提示
 *
 * ⚠️ 这是本模块唯一读扫描状态的地方，刻意做成独立函数而不是塞进 `buildTimeline`：
 *    时间线的数据与"要不要报警"是两件事，混在一起之后就没法单独测后者。
 */
export function gapIsSuspicious(durationMs: number, scanStatus: string | null | undefined): boolean {
  if (!Number.isFinite(durationMs) || durationMs < SILENCE_THRESHOLD_MS) return false
  return scanStatus === 'running'
}

/**
 * 时间线的空态判据。
 *
 * 三句话对应三种完全不同的处境，措辞不能合并：
 *   - 没有事件、扫描在跑 → "刚启动"（该等）
 *   - 没有事件、扫描结束 → "这次没有任何产出"（是结论，不是等待）
 *   - 没有事件、没有扫描 → "还没有扫描"
 */
export type TimelineEmptyKind = 'starting' | 'produced-nothing' | 'no-scan' | 'none'

export function timelineEmptyKind(
  eventCount: number,
  scanMarked: boolean,
  scanStatus: string | null | undefined,
): TimelineEmptyKind {
  if (eventCount > 0) return 'none'
  if (!scanMarked) return 'no-scan'
  return scanStatus === 'running' || scanStatus === 'queued' ? 'starting' : 'produced-nothing'
}
