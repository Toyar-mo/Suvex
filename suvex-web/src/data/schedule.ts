/**
 * 定时扫描（schedule）类型与展示元数据。
 *
 * 字段名与后端 `app/schedules/schemas.py` 的 `ScheduleOut` 逐字一致（camelCase）。
 *
 * ## 为什么这一页不是「扫描页加一个开关」
 *
 * 扫描页回答的是「我发起了什么、跑得怎么样」—— 每一行都对应**一次已经发生的动作**。
 * 定时计划回答的是完全不同的问题：「**以后**什么时候会自动发生动作」。
 *
 * 这个区别不是概念洁癖，它决定了两个页面各自最该显眼的东西：
 *
 *   · 扫描页的失败是**历史**：那次没跑完，重跑一次就行。
 *   · 定时计划的失败是**未来**：这条计划每次到点都在失败，而用户以为
 *     「它一直在帮我复查」。后者没人盯着会一直错下去。
 *
 * 所以这一页把「连续失败」和「已停用」排在最前面 —— 见 `compareSchedules`。
 *
 * ## ★ `nextRunAt` / `lastRunAt` / `lastScanId` 三个字段必须分开看
 *
 * 后端刻意把它们分成三列（`models.py` 的说明）。前端也不能合并：
 *
 *   · `nextRunAt === null` + `enabled === true`  → **不该出现**（数据库 CHECK 拦住）。
 *     真出现了说明数据被手改过，界面必须能看出来，不能静默当成"停用"。
 *   · `lastRunAt === null`                      → 建好之后还一次没到点。
 *     这和「跑过但失败了」是两件事，文案不能都是「—」。
 *   · `lastScanId` 有值但 `lastRunAt` 为空      → 同样是不该出现的组合。
 */

import type { TargetKind } from '@/data/scan'

/** 一条定时计划。与后端 `ScheduleOut` 逐字一致。 */
export interface Schedule {
  id: string
  name: string
  target: string
  targetKind: string
  goal: string
  /** 间隔分钟数。后端约束 5 分钟 ~ 365 天。 */
  intervalMinutes: number
  enabled: boolean
  /** 下次到点时刻；停用时为 null（与数据库 CHECK 约束一致）。 */
  nextRunAt: string | null
  /** 上一轮建出的扫描 id。不是外键 —— 扫描被清理后这里仍留着。 */
  lastScanId: string | null
  lastRunAt: string | null
  /** 连续失败次数。**不会**自动停用计划。 */
  failureCount: number
  lastFailureReason: string | null
  providerId: string
  providerLabel: string | null
  skills: string[]
  brief: string | null
  createdAt: string
  updatedAt: string
}

export interface ScheduleList {
  items: Schedule[]
  total: number
  /** 已到点但还没派发出去的计划数 —— 界面用它提示"积压"。 */
  dueCount: number
}

/** 新建计划的请求体。字段名与 `ScheduleCreateIn` 逐字一致。 */
export interface CreateScheduleInput {
  name: string
  target: string
  targetKind: string
  goal: string
  intervalMinutes: number
  providerId: string
  skills: string[]
  brief: string | null
  authorizationStatement: string
}

/**
 * 一条计划此刻的**处境** —— 列表里那一列"状态"。
 *
 * 为什么需要这个而不直接看 `enabled` + `failureCount`：
 * 用户要判断的是「这条计划还靠得住吗」。四个来源：
 *
 *   · `broken`    —— 数据自相矛盾（启用却没有下次时间）。几乎不可能出现，
 *                    但出现时必须显式报出来，而不是当成"停用"糊过去。
 *   · `failing`   —— 连续失败。它还在到点，但每次都没建出扫描来。
 *   · `paused`    —— 停用。可能是有意停的，也可能是三个月前随手关掉忘了。
 *   · `never-run` —— 建好了，还一次没到点。
 *   · `due`       —— 已经到点了还没派发（调度器可能没在跑）。
 *   · `ok`        —— 下次时间在未来，历史没连续失败。
 */
export type ScheduleState = 'broken' | 'failing' | 'paused' | 'never-run' | 'due' | 'ok'

/**
 * 判读一条计划的处境。
 *
 * 判定顺序是**有意的**：从"最不能相信"到"最正常"，每一档只在上一档
 * 不成立时才问。反过来排会让「启用却没有下次时间」一路落到 `ok`。
 */
export function scheduleState(schedule: Schedule, now: number = Date.now()): ScheduleState {
  // 启用却没有下次时间 —— 数据库 CHECK 本不该放它进来，真出现说明被手改过。
  if (schedule.enabled && schedule.nextRunAt === null) return 'broken'
  if (!schedule.enabled) return 'paused'
  if (schedule.failureCount > 0) return 'failing'
  if (schedule.lastRunAt === null) return 'never-run'

  const next = schedule.nextRunAt === null ? NaN : new Date(schedule.nextRunAt).getTime()
  if (!Number.isFinite(next)) return 'broken'
  if (next <= now) return 'due'

  return 'ok'
}

/** 处境的展示文案与配色。 */
export const SCHEDULE_STATE_META: Record<
  ScheduleState,
  { label: string; hint: string; className: string; dot: string }
> = {
  broken: {
    label: '数据异常',
    hint: '这条计划是启用状态，却没有"下次执行时间" —— 正常流程不会产生这种组合',
    className: 'text-signal-risk',
    dot: 'bg-signal-risk',
  },
  failing: {
    label: '连续失败',
    hint: '每次到点都尝试建扫描，但都没成功 —— 它并没有在帮你复查',
    className: 'text-signal-risk',
    dot: 'bg-signal-risk',
  },
  paused: {
    label: '已停用',
    hint: '停用中，不会自动发起扫描',
    className: 'text-ink-muted',
    dot: 'bg-line',
  },
  'never-run': {
    label: '待首次执行',
    hint: '计划已建好，还没到第一次执行时间',
    className: 'text-ink-muted',
    dot: 'bg-signal-info',
  },
  due: {
    label: '已到点待执行',
    hint: '已经过了下次执行时间但还没派发出去 —— 调度器可能没在运行',
    className: 'text-signal-warn',
    dot: 'bg-signal-warn',
  },
  ok: {
    label: '正常',
    hint: '下次执行时间在未来，历史上没有连续失败',
    className: 'text-signal-ok',
    dot: 'bg-signal-ok',
  },
}

/** 后端允许的间隔范围（`models.py` 的 `MIN_/MAX_INTERVAL_MINUTES`）。 */
export const MIN_INTERVAL_MINUTES = 5
export const MAX_INTERVAL_MINUTES = 365 * 24 * 60

/**
 * 定时计划**能**用的目标形态 —— 「题目附件」不在其中。
 *
 * ## 为什么不是直接复用 `TARGET_KIND_LABEL` 的全部键
 *
 * 因为 `artifact` 在定时计划里不是"暂时不支持"，而是**立不起来**：
 *
 *   · 它的合法 `target` 是空串（附件名由建扫描时自己填），而后端
 *     `ScheduleCreateIn.target` 是 `min_length=1` —— 空串直接 422；
 *   · 随便填个非空值又能过 schema，但 `targets.normalize_and_validate`
 *     对任何非空输入都回「这类题目不需要目标地址」—— 400。
 *
 * 两条路都走不通。而**即使放行也没意义**：附件是提交时一次性暂存的，
 * `ScanSchedule` 没有存它的地方，派发时 `attachmentStagingId=None`，
 * 于是每一轮到点都会建出一条没有素材的扫描然后必然失败，
 * `failureCount` 每轮 +1 —— 用户会看到一条**永远修不好**的计划。
 *
 * 所以在选项层面就不给。后端 `POST /schedules` 也做了同样的拒绝
 * （`app/schedules/router.py`），两处口径一致：这里少一个按钮，
 * 那里多一条可读的报错，绕过前端也拿不到一条注定失败的计划。
 *
 * ## ★ 顺序就是按钮的显示顺序
 *
 * 与 `TARGET_KIND_LABEL` 保持同样的相对次序（`artifact` 本来在最后，
 * 去掉它不影响其余的位置记忆）。新增形态时**追加到末尾**，不要插中间。
 */
export const SCHEDULE_TARGET_KINDS: ReadonlyArray<TargetKind> = ['url', 'repo', 'path', 'service']

/**
 * 常用间隔选项。
 *
 * 给选项而不是让用户填数字：后端存的是"间隔分钟数"，而人脑子里想的是
 * "每小时""每天 3 点前后"这类说法。让人自己把"每天"翻译成 1440 是
 * 一次不必要的换算，而且 1440 和 24 都"看起来对"。
 *
 * 刻意**没有**"每 5 分钟"以外的更密选项 —— 再密就不是定期复查了。
 */
export const INTERVAL_PRESETS: ReadonlyArray<{ minutes: number; label: string }> = [
  { minutes: 15, label: '每 15 分钟' },
  { minutes: 30, label: '每 30 分钟' },
  { minutes: 60, label: '每小时' },
  { minutes: 180, label: '每 3 小时' },
  { minutes: 360, label: '每 6 小时' },
  { minutes: 720, label: '每 12 小时' },
  { minutes: 1440, label: '每天' },
  { minutes: 10080, label: '每周' },
  { minutes: 43200, label: '每 30 天' },
]

/**
 * 把间隔分钟数变成人话。
 *
 * 优先用能整除的最大单位 —— 「每 2 小时」比「每 120 分钟」好读，
 * 但 90 分钟**不**该写成「每 1.5 小时」：带小数的间隔会让人怀疑
 * 自己看到的是不是真的。
 */
export function formatInterval(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return '—'
  if (minutes % 1440 === 0) return `每 ${minutes / 1440} 天`
  if (minutes % 60 === 0) return `每 ${minutes / 60} 小时`
  return `每 ${minutes} 分钟`
}

/**
 * 相对时间：「3 天后」「2 小时前」。
 *
 * 与资产页的 `relativeTime` 分开写，因为**方向不同**：
 * 那里只显示过去（"上次扫描"），这里 `nextRunAt` 是未来。
 * 用一个函数靠符号分支会让调用点每次都要想"我传的是过去还是未来"。
 */
export function formatRelative(iso: string | null, now: number = Date.now()): string {
  if (iso === null) return '—'
  const ts = new Date(iso).getTime()
  if (!Number.isFinite(ts)) return '—'

  const diff = ts - now
  const future = diff > 0
  const abs = Math.abs(diff)

  const minutes = Math.floor(abs / 60_000)
  let text: string
  if (minutes < 1) {
    // 一分钟以内说"刚刚"比"0 分钟后"诚实
    return '刚刚'
  } else if (minutes < 60) {
    text = `${minutes} 分钟`
  } else if (minutes < 1440) {
    text = `${Math.floor(minutes / 60)} 小时`
  } else if (minutes < 43_200) {
    text = `${Math.floor(minutes / 1440)} 天`
  } else {
    text = `${Math.floor(minutes / 43_200)} 个月`
  }

  return future ? `${text}后` : `${text}前`
}

/** 搜索框用它做大小写不敏感的子串匹配（名字与目标都算）。 */
export function scheduleMatchesQuery(schedule: Schedule, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return (
    schedule.name.toLowerCase().includes(q) || schedule.target.toLowerCase().includes(q)
  )
}

/**
 * 列表排序 —— 把「不能信」的排前面。
 *
 * 顺序：数据异常 → 连续失败 → 已到点 → 待首次执行 → 正常 → 已停用。
 *
 * 两处刻意的选择：
 *
 * 1. **停用排最后**。停用是"用户主动做的决定"，不该占着视线；
 *    但也不能隐藏 —— 忘了自己三个月前关掉过一条计划，是这类功能
 *    最常见的失效方式。
 * 2. **同档内按"下次执行时间"升序**，不是按名字。正常档里最近要跑的
 *    排前面，因为那是用户下一步最可能关心的事。
 */
const STATE_RANK: Record<ScheduleState, number> = {
  broken: 0,
  failing: 1,
  due: 2,
  'never-run': 3,
  ok: 4,
  paused: 5,
}

function nextRunTs(schedule: Schedule): number {
  if (schedule.nextRunAt === null) return Number.POSITIVE_INFINITY
  const ts = new Date(schedule.nextRunAt).getTime()
  return Number.isFinite(ts) ? ts : Number.POSITIVE_INFINITY
}

export function compareSchedules(a: Schedule, b: Schedule, now: number = Date.now()): number {
  const aRank = STATE_RANK[scheduleState(a, now)]
  const bRank = STATE_RANK[scheduleState(b, now)]
  if (aRank !== bRank) return aRank - bRank

  const aNext = nextRunTs(a)
  const bNext = nextRunTs(b)
  if (aNext !== bNext) return aNext - bNext

  // 确定性 tie-break —— 没有它，同样两条数据每次刷新可能换位置
  return a.name.localeCompare(b.name) || a.id.localeCompare(b.id)
}
