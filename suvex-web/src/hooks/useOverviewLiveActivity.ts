import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { createSseEventSource } from '@/data/activity-stream'
import type { ActivityEvent as WireEvent, EventDetail } from '@/data/activity'
import type { ActivityEvent, ActivityEventType, AgentPhase } from '@/data/overview'

/**
 * 总览页的「真·实时」活动流（v1.81）。
 *
 * ## 为什么总览页也要连 SSE
 *
 * 在这之前，总览页的「实时活动」其实来自 `GET /overview` 的 60 秒轮询。
 * 也就是说：标题写着「实时活动」，内容最多可以晚一分钟。而这一页的
 * 第一屏正是产品承诺（"看它每一步在想什么"）—— 一个 60 秒才动一次的
 * 演示，动效再好看也只是装饰。
 *
 * 现在：`GET /overview` 的 `activity` 作为**种子**，页面挂载后按需接上
 * 全局 SSE（`/api/v1/events`），事件到达就合并进来。动效因此是
 * **真实事件驱动的**，不是定时器演出来的。
 *
 * ## 三条纪律
 *
 * 1. **只在真的有扫描在跑时连。** 没有活跃扫描时挂一条长连接是纯浪费，
 *    而且后端每条 SSE 占一个线程（见 `app/events/stream.py`）。
 * 2. **连接失败不冒充实时。** 断线时把 `connection` 如实交给界面，
 *    由界面说明"下面是最近收到的事件"；静默退回轮询而标题还写「实时」
 *    是这一页最不能犯的错。
 * 3. **种子不播动画。** 首批 `latestBatchIds` 为空集，只有订阅之后
 *    真正新到的事件才会被标成"新"。
 *
 * ## 与 Activity 页的关系
 *
 * 两者消费同一个 SSE 端点，但互不影响：路由一次只挂一个页面，
 * 所以同一时刻最多一条连接。这个 hook 不保存完整事件（不需要详情），
 * 只保留 Overview 投影所需的 7 个字段。
 */

/** 与后端 `PROJECTED_TYPES` 对齐：总览投影只取这 6 种。 */
const PROJECTED_TYPES: ReadonlySet<string> = new Set([
  'discovery',
  'tool_call',
  'poc_generated',
  'verified',
  'blocked',
  'completed',
])

const PHASES: ReadonlySet<string> = new Set(['recon', 'scan', 'exploit', 'verify', 'report'])

/** 合并后最多保留多少条。总览只显示 8 条，留 40 条是为了阶段统计有上下文。 */
const MAX_EVENTS = 40

/** 已见事件 id 的上限。与 Activity 页的 `EMITTED_MEMORY` 同一思路。 */
const SEEN_LIMIT = 2000

/** SSE 帧的合并窗口。与 `useActivityStream` 一致 —— 突发时一次渲染而不是每条一次。 */
const FLUSH_INTERVAL_MS = 120

/** 空集常量：`useState` 的初值必须是稳定引用，否则每次渲染都会触发"批次变了"的判断。 */
const EMPTY_BATCH: ReadonlySet<string> = new Set()

export type LiveConnection = 'connecting' | 'live' | 'disconnected'

export interface OverviewLiveActivity {
  /** 种子 ∪ 实时事件，按 `at` 倒序，最多 40 条 */
  events: ActivityEvent[]
  /** 最近一批新到事件的 id —— 只给这批播入场 / 阶段脉冲 */
  latestBatchIds: ReadonlySet<string>
  /** SSE 连接状态。`null` = 这次没有连（没有活跃扫描，或环境不支持 EventSource） */
  connection: LiveConnection | null
}

/** SSE 帧比前端类型多一个 `agentPhase`（契约 §2.5 的例外字段）。 */
type WireActivityEvent = WireEvent & { agentPhase?: string }

/**
 * `detail` → 一行摘要里的 `subject`。
 *
 * 与后端 `overview/service.py:_subject_of` 保持同一套取值规则：
 * 后端投影时不解释 detail，前端合并实时事件时必须复刻这条规则，
 * 否则同一条事件"刚到时"和"下一轮轮询后"会显示成两种样子。
 */
function subjectOf(detail: EventDetail | null): string | null {
  if (!detail) return null

  let value: string | null
  switch (detail.kind) {
    case 'discovery':
      value = detail.subject
      break
    case 'tool_call':
      // 命令比工具名信息量大 —— 与后端同一条取舍
      value = detail.command || detail.tool
      break
    case 'poc_generated':
      value = detail.target
      break
    case 'blocked':
      value = detail.attempted
      break
    default:
      // verified / completed / reasoning / failed 没有"对象"这个概念
      value = null
  }

  if (value === null) return null
  const text = value.trim()
  return text === '' ? null : text
}

/**
 * 完整事件 → Overview 投影。
 *
 * 返回 `null` 表示这条事件不该进总览（`reasoning` 是过程噪音，
 * `failed` 属于诊断信息 —— 与后端 `PROJECTED_TYPES` 完全一致）。
 */
function projectEvent(event: WireActivityEvent): ActivityEvent | null {
  if (!PROJECTED_TYPES.has(event.type)) return null

  /*
    `agentPhase` 契约要求必填，但它是 SSE 帧里的字段、前端 `ActivityEvent`
    类型里没有声明。这里按契约直接采用；万一收到畸形帧（没有该字段），
    退回 `scan` 是**有损的**，但总比丢掉整条事件好 —— 丢掉会让活动流
    看起来"漏事件"，而那是比阶段标错更难发现的问题。
  */
  const phase = event.agentPhase
  return {
    id: event.id,
    scanId: event.scanId,
    agentId: event.agentId,
    agentPhase: (phase && PHASES.has(phase) ? phase : 'scan') as AgentPhase,
    type: event.type as ActivityEventType,
    summary: event.summary,
    subject: subjectOf(event.detail),
    at: event.at,
  }
}

/**
 * @param seed     `GET /overview` 给的活动流（每次轮询会换一份）
 * @param enabled  是否有扫描在跑。false 时不连、不合并
 */
export function useOverviewLiveActivity(
  seed: ActivityEvent[],
  enabled: boolean,
): OverviewLiveActivity {
  const [liveEvents, setLiveEvents] = useState<ActivityEvent[]>([])
  const [latestBatchIds, setLatestBatchIds] = useState<ReadonlySet<string>>(EMPTY_BATCH)
  const [connection, setConnection] = useState<LiveConnection>('connecting')

  /** 已经见过的事件 id（种子 + 实时）。用来防止轮询与 SSE 交叠时重复播动画。 */
  const seenRef = useRef<Set<string>>(new Set())
  const pendingRef = useRef<WireActivityEvent[]>([])
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  /**
   * 环境没有 `EventSource` 时不连。
   *
   * 这不只是测试环境的便利：它让"不支持 SSE 的客户端"退化成
   * **60 秒轮询**，而不是抛错白屏。此时 `connection` 保持 `null`，
   * 界面不会假装自己是实时的。
   */
  const supported = typeof window !== 'undefined' && typeof window.EventSource !== 'undefined'

  const source = useMemo(
    () => (enabled && supported ? createSseEventSource({ scanId: null }) : null),
    [enabled, supported],
  )

  const remember = useCallback((id: string) => {
    const seen = seenRef.current
    if (seen.size >= SEEN_LIMIT) {
      // Set 保持插入顺序 —— 删最早进来的那个
      const oldest = seen.values().next().value
      if (oldest !== undefined) seen.delete(oldest)
    }
    seen.add(id)
  }, [])

  const flush = useCallback(() => {
    timerRef.current = null
    const batch = pendingRef.current
    pendingRef.current = []
    if (batch.length === 0) return

    const fresh: ActivityEvent[] = []
    for (const raw of batch) {
      const projected = projectEvent(raw)
      if (!projected) continue
      if (seenRef.current.has(projected.id)) continue
      remember(projected.id)
      fresh.push(projected)
    }
    if (fresh.length === 0) return

    setLiveEvents((prev) => [...fresh, ...prev].slice(0, MAX_EVENTS))
    setLatestBatchIds(new Set(fresh.map((event) => event.id)))
  }, [remember])

  /*
    ★ 先登记种子 id，再订阅（effect 的声明顺序就是执行顺序）。

    不登记的话，轮询刷新与 SSE 交叠的那一条会被当成"新事件"再播一次
    入场动画 —— 它已经在屏幕上了，再动一下读起来像重复。
  */
  useEffect(() => {
    for (const event of seed) remember(event.id)
  }, [seed, remember])

  useEffect(() => {
    if (!source) return

    const unsubscribe = source.subscribe((event) => {
      pendingRef.current.push(event as WireActivityEvent)
      if (timerRef.current === null) {
        timerRef.current = setTimeout(flush, FLUSH_INTERVAL_MS)
      }
    })

    /*
      `subscribeStatus` 注册时会**立刻回调一次当前值**（接口约定），
      所以这里既拿到了初始的 `connecting`，也能收到之后的 live / disconnected，
      不需要在 effect body 里另外 setState。
    */
    const unsubscribeStatus = source.subscribeStatus((status) => {
      setConnection(
        status === 'live' ? 'live' : status === 'disconnected' ? 'disconnected' : 'connecting',
      )
    })

    source.start()

    return () => {
      unsubscribe()
      unsubscribeStatus()
      source.stop()
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
      pendingRef.current = []
    }
  }, [source, flush])

  /*
    合并：种子 ∪ 实时，按 `at` 倒序。

    ⚠️ 用 id 去重而不是整条替换：同一条事件可能先由 SSE 到达、
      下一轮 `/overview` 又把它作为种子发回来。两者内容相同，
      但引用不同 —— 不去重的话 React 会看到两个相同 key。
  */
  const events = useMemo(() => {
    const byId = new Map<string, ActivityEvent>()
    for (const event of seed) byId.set(event.id, event)
    for (const event of liveEvents) byId.set(event.id, event)

    return [...byId.values()].sort((a, b) => b.at.localeCompare(a.at)).slice(0, MAX_EVENTS)
  }, [seed, liveEvents])

  return {
    events,
    latestBatchIds,
    connection: source ? connection : null,
  }
}
