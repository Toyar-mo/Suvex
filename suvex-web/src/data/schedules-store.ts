import { ApiError, api, toApiError } from '@/lib/api'
import { useSyncExternalStore } from 'react'
import type { CreateScheduleInput, Schedule, ScheduleList } from './schedule'

/**
 * 定时计划列表的运行时事实来源。
 *
 * 与 scans-store / assets-store / findings-store 同一套模式
 * （`useSyncExternalStore` + 整体替换快照）。
 *
 * ## 为什么要一个 store，而不是页面里 `useState`
 *
 * 定时计划是**横向能力**，不是一页的私事：
 *
 *   · 资产页要知道「这个目标有没有在定期复查」（否则"结论陈旧"的提示
 *     没法给出下一步动作）；
 *   · 扫描页新建完扫描后，用户会想"要不排一条定期的"；
 *   · 侧栏要能显示"有几条计划在失败"。
 *
 * 三处各存一份必然漂移 —— 而且漂移的症状是"资产页说没复查，
 * 计划页明明有一条"，那是最难查的一类不一致。
 *
 * ## 状态口径与资产页一致
 *
 * `idle`（还没人问过）≠ `loading`（正在问）。混成 `loading` 会让侧栏
 * 刚挂载就闪一下骨架屏，而用户根本没进这一页。
 *
 * ## ★ 为什么这里**要**轮询，而资产列表不轮询
 *
 * 资产是台账，变化来自"有人发起了一次扫描"，那件事在扫描页自己会推。
 * 而定时计划是**活的**：`nextRunAt` 到点后会被调度器推进，
 * `failureCount` 会在后台涨，`dueCount` 会随调度器是否在运行而变化。
 * 这些都不经过用户的操作 —— 不轮询就只能靠用户手动刷新，
 * 而"页面上的下次执行时间已经过去了半小时"是会让用户误判调度器挂了的。
 *
 * 轮询间隔取 30 秒：`nextRunAt` 精度到分钟就够，30 秒能把"刚过点"
 * 反映出来的同时不至于让一个开着的标签页持续烧请求。
 */

type Listener = () => void

export type SchedulesStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface SchedulesSnapshot {
  /** 全部计划。`null` = 还没加载过。 */
  items: Schedule[] | null
  total: number
  /** 已到点但还没派发的条数。 */
  dueCount: number
  status: SchedulesStatus
  /** ★ 真的 `ApiError`，不是一句文案 —— `ErrorState` 要靠它区分失败种类。 */
  error: ApiError | null
}

const EMPTY: SchedulesSnapshot = {
  items: null,
  total: 0,
  dueCount: 0,
  status: 'idle',
  error: null,
}

const listeners = new Set<Listener>()

let snapshot: SchedulesSnapshot = { ...EMPTY }

function emit() {
  for (const listener of listeners) listener()
}

function setSnapshot(patch: Partial<SchedulesSnapshot>) {
  snapshot = { ...snapshot, ...patch }
  emit()
}

/** 在飞的请求 —— 同一时刻只发一个。 */
let inFlight: Promise<void> | null = null

/**
 * 拉取定时计划列表。
 *
 * `silent` 用于后台轮询：不切到 `loading`（否则会把已经显示的表格
 * 换成骨架屏），失败时也不覆盖已有的 `error` —— 轮询失败不该把
 * 页面上好好的数据变成一个错误页。
 */
export async function loadSchedules(options: { silent?: boolean } = {}): Promise<void> {
  if (inFlight) return inFlight

  const silent = options.silent === true
  if (!silent && snapshot.items === null) {
    setSnapshot({ status: 'loading', error: null })
  }

  inFlight = (async () => {
    try {
      const data = await api.get<ScheduleList>('/schedules')
      setSnapshot({
        items: data.items,
        total: data.total,
        dueCount: data.dueCount,
        status: 'ready',
        error: null,
      })
    } catch (error) {
      const apiError = toApiError(error)
      if (!silent) {
        setSnapshot({ status: 'error', error: apiError })
      }
    } finally {
      inFlight = null
    }
  })()

  return inFlight
}

/**
 * 把一条刚建/刚改的计划放进快照。
 *
 * 不重新拉整个列表：`POST /schedules` 已经返回了完整的那一条，
 * 再拉一次只会让刚建好的行闪一下（列表重排 + 骨架屏）。
 * 而**排序**由页面自己做（`compareSchedules`），store 只管"有哪些"。
 */
function upsert(schedule: Schedule) {
  const items = snapshot.items
  if (items === null) return
  const index = items.findIndex((item) => item.id === schedule.id)
  const next = index === -1 ? [schedule, ...items] : items.map((item) => (item.id === schedule.id ? schedule : item))
  setSnapshot({ items: next, total: Math.max(next.length, snapshot.total) })
}

function remove(id: string) {
  const items = snapshot.items
  if (items === null) return
  const next = items.filter((item) => item.id !== id)
  setSnapshot({ items: next, total: Math.max(0, snapshot.total - 1) })
}

/** 新建一条计划。 */
export async function createSchedule(input: CreateScheduleInput): Promise<Schedule> {
  const schedule = await api.post<Schedule>('/schedules', input)
  upsert(schedule)
  return schedule
}

/** 改名字 / 改间隔。 */
export async function updateSchedule(
  id: string,
  patch: { name?: string; intervalMinutes?: number },
): Promise<Schedule> {
  const schedule = await api.patch<Schedule>(`/schedules/${encodeURIComponent(id)}`, patch)
  upsert(schedule)
  return schedule
}

/**
 * 启停一条计划。
 *
 * ⚠️ 单独一个端点（`POST /schedules/{id}/enabled`）而不是 `PATCH`
 * 的一个字段：启用时服务端要**重算** `nextRunAt`（从旧值按整数倍推到
 * 未来，保住相位），那是"改一个字段"表达不了的行为。
 */
export async function toggleSchedule(id: string, enabled: boolean): Promise<Schedule> {
  const schedule = await api.post<Schedule>(`/schedules/${encodeURIComponent(id)}/enabled`, {
    enabled,
  })
  upsert(schedule)
  return schedule
}

/** 删一条计划。 */
export async function deleteSchedule(id: string): Promise<void> {
  await api.del<void>(`/schedules/${encodeURIComponent(id)}`)
  remove(id)
}

/** 清空到 idle（登出、切库时用）。 */
export function resetSchedules(): void {
  inFlight = null
  snapshot = { ...EMPTY }
  emit()
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function getSnapshot(): SchedulesSnapshot {
  return snapshot
}

export function useSchedulesState(): SchedulesSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** 供测试用：直接读当前快照，不经过 React。 */
export function getSchedulesSnapshot(): SchedulesSnapshot {
  return snapshot
}
