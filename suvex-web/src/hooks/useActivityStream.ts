import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ActivityEvent, Agent, AgentRole, EventSourceHandle, StreamStatus } from '@/data/activity'

/**
 * 事件流的消费端状态。
 *
 * 三件必须做对的事：
 *
 * 1. **上限截断。** 事件只增不减，长时间挂着会积累几万条，
 *    内存和渲染都会垮。保留最近 MAX_EVENTS 条，超出的丢掉 ——
 *    丢最老的，因为实时流页面的价值在"最近发生了什么"。
 *
 * 2. **暂停时不丢事件。** 用户往上翻看历史时，新事件**照常收进缓冲**，
 *    只是不自动滚动。如果暂停就停止接收，用户回到最新时会发现中间空了一段，
 *    那比打断阅读更糟。代价是要记一个「暂停期间新增了多少条」给"回到最新"用。
 *
 * 3. **不按到达顺序排序，按 at 排序。** 真实 SSE 里事件可能乱序到达
 *    （重连补发、多路合并）。按时间戳排序才是正确顺序。
 *
 * 渲染性能：事件先入 ref 缓冲，按 FLUSH_INTERVAL_MS 合并成一次 setState。
 * 突发 100 条事件时只触发一次渲染，而不是 100 次。
 */

/** 上限。按每条事件约 1KB 估算，5000 条约 5MB —— 可接受的上限 */
const MAX_EVENTS = 5000

/** 批量合并窗口：这段时间内到达的事件合成一次 setState */
const FLUSH_INTERVAL_MS = 120

// `StreamStatus` 现在定义在 `data/activity`（它原来在这里和 activity-mock
// 里各写了一遍）。re-export 一下，既有的导入路径继续可用。
export type { StreamStatus }

/** 数据源的四态 → UI 的三态。`paused` 是用户主动暂停，不属于"连接"问题 */
function toConnection(status: StreamStatus): 'connecting' | 'live' | 'disconnected' {
  if (status === 'live') return 'live'
  if (status === 'disconnected') return 'disconnected'
  return 'connecting'
}

export interface UseActivityStreamResult {
  events: ActivityEvent[]
  /** 名册上的 agent（含从事件流派生的实时统计） */
  agents: Agent[]
  /** 数据源状态（连接/断开），与用户主动暂停无关 */
  connection: 'connecting' | 'live' | 'disconnected'
  /** 用户是否暂停了自动滚动 */
  isPaused: boolean
  setPaused: (paused: boolean) => void
  /** 暂停期间新增的事件数 —— "回到最新"按钮上的角标 */
  newWhilePaused: number
  /**
   * 最近一批**新到达**的事件 id。用于只给新条目播一次入场动画。
   *
   * 为什么由 hook 提供而不是让消费组件自己 diff：
   * 组件要判断"哪些是新的"就得记住上一批的 id，而渲染期读 ref 不安全、
   * 用 effect 同步 state 又会触发级联渲染（React 明确反对）。
   * 而**这里本来就处在"事件到达"的事件处理路径上** —— 在 flush 时顺手
   * 记下这批 id 是自然的，不需要任何额外机制。
   *
   * 首批历史事件（seed）不在此列：一进页面十几条一起淡入会很吵。
   */
  latestBatchIds: ReadonlySet<string>
}

export function useActivityStream(
  source: EventSourceHandle,
  roster: Agent[],
): UseActivityStreamResult {
  const [events, setEvents] = useState<ActivityEvent[]>([])
  const [connection, setConnection] = useState<'connecting' | 'live' | 'disconnected'>('connecting')
  const [isPaused, setIsPaused] = useState(false)
  const [newWhilePaused, setNewWhilePaused] = useState(0)
  const [latestBatchIds, setLatestBatchIds] = useState<ReadonlySet<string>>(() => new Set())

  const pendingRef = useRef<ActivityEvent[]>([])
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 暂停态用 ref 读：放进 subscribe 的闭包依赖会导致重订阅、丢掉缓冲
  const pausedRef = useRef(false)
  // 首批（seed）不播动画 —— 用 ref 是因为它只在事件路径上读写，与渲染无关
  const hasSeededRef = useRef(false)

  const flush = useCallback(() => {
    flushTimerRef.current = null
    const batch = pendingRef.current
    if (batch.length === 0) return
    pendingRef.current = []

    setEvents((prev) => {
      /*
        ★★ 按 id 去重（2026-10-10 修）。

        ## 为什么需要它

        `prev` 里可能**已经有**这一批里的某些事件。真实的路径是切换扫描：

          `?scan=A` → `?scan=B` → 回 `?scan=A`

        每次 `scanFilter` 变化，`ActivityPage` 都会 `createSseEventSource`
        建一个**新实例**，而 `emittedIds`（防重复的那道防线）是**实例级闭包
        变量** —— 新实例的它是空的，于是 seed 会把同一批历史事件**再 emit
        一遍**。而 `events` 在 source 变化时**没有被清空**，两者一撞就是
        重复条目，`key={event.id}` 也会撞车（React 会在控制台报重复 key）。

        ## 与 `activity-stream.ts` 里那道防线的关系

        那边防的是**同一个实例**内的重复（StrictMode 双挂载 —— 见它
        `epoch` 的注释，那个 bug 已经修过了）。**跨实例**它管不着，
        因为实例一换，它记住的 id 就全没了。

        两道防线各管一层，缺一不可：
          · 实例内：`emittedIds`（那边的 `epoch` 保证 seed 不重复跑）；
          · 跨实例：这里按 id 去重（`prev` 是唯一知道"已经显示过什么"的地方）。

        ## 为什么用 Set 而不是每次 filter 一遍

        批次是增量的（通常几条），而 `prev` 可能到 5000 条。
        建一个 Set 是 O(prev)，逐条 `some()` 是 O(batch × prev) ——
        在 5000 条 × 每 120ms 一次 flush 的量级上，这个差别是实打实的。
      */
      const seen = new Set(prev.map((e) => e.id))
      const fresh = batch.filter((e) => !seen.has(e.id))
      if (fresh.length === 0) return prev // ★ 没有新东西就返回原引用，省一次渲染

      const merged = [...prev, ...fresh]
      // 按时间升序。localeCompare 对 ISO 串等价于字符串比较，
      // 稳定排序保证同一毫秒内的事件保持到达顺序
      merged.sort((a, b) => a.at.localeCompare(b.at))
      return merged.length > MAX_EVENTS ? merged.slice(merged.length - MAX_EVENTS) : merged
    })

    // 首批是历史回填，不播动画；之后的每批都要播
    if (hasSeededRef.current) {
      setLatestBatchIds(new Set(batch.map((e) => e.id)))
    } else {
      hasSeededRef.current = true
    }

    if (pausedRef.current) {
      setNewWhilePaused((n) => n + batch.length)
    }
  }, [])

  useEffect(() => {
    const unsubscribe = source.subscribe((event) => {
      pendingRef.current.push(event)
      if (flushTimerRef.current === null) {
        flushTimerRef.current = setTimeout(flush, FLUSH_INTERVAL_MS)
      }
    })

    /*
      ★ 状态订阅。

      真实 SSE 的连接状态**会自己变**：`onopen` → live、`onerror` → disconnected。
      不订阅的话界面会永远停在"连接中"，用户根本看不出到底连上没有。

      `subscribeStatus` 在**注册时会立即回调一次当前值**（接口约定），
      所以这里订阅完就知道初始状态 —— 不需要另外再读一次，
      也没有"订阅之后、第一次变化之前"那段空窗。
    */
    const unsubscribeStatus = source.subscribeStatus((next) => {
      setConnection(toConnection(next))
    })

    source.start()

    return () => {
      unsubscribe()
      unsubscribeStatus()
      source.stop()
      if (flushTimerRef.current !== null) {
        clearTimeout(flushTimerRef.current)
        flushTimerRef.current = null
      }
      pendingRef.current = []

      /*
        ★★ 清空已显示的事件（2026-10-10 修）。

        ## 为什么必须在这里清

        这个 effect 的依赖是 `[source, flush]`，而 `source` 在
        **切换扫描**时会变成新实例（`ActivityPage` 用 `useMemo` 按
        `scanFilter` 重建）。新实例的 `emittedIds` 是空的 ——
        它会把同一批历史事件**再 emit 一遍**。

        而 `events` 是 `useState`，它**不随 source 变化而重置**。
        于是旧事件还在、新 seed 又进来 → 重复条目 + `key={event.id}` 撞车。

        ## 为什么"清空"是对的，而不是"保留着更好"

        因为 `source` 换了就意味着**看的已经不是同一条流了**
        （从全站流切到某次扫描、或从 A 切到 B）。留着上一条流的事件
        在语义上就是错的 —— 它们不属于当前视图。

        同理清掉另外三个派生状态：暂停计数（属于旧流）、
        入场动画批次（旧 id 在新流里没有意义）、seed 标记
        （新流要重新经历"首批不播动画"）。

        ## 为什么不在渲染期或 useMemo 里做

        在 effect 的清理函数里做是**唯一安全**的位置：
          · 渲染期 setState 会被 React 拒绝（或触发级联渲染）；
          · `useMemo` 里做副作用是反模式（StrictMode 下会跑两次）；
          · 而这里本来就是"这一轮订阅结束"的时机，天然对齐。

        ⚠️ 代价：切换扫描时列表会**闪一下空**（下一个 tick 就填回来）。
            这是正确的取舍 —— 短暂空白 vs 显示错误的数据，
            而后者会让用户以为"这条扫描有两份重复的事件"。
      */
      setEvents([])
      setLatestBatchIds(new Set())
      setNewWhilePaused(0)
      hasSeededRef.current = false
    }
  }, [source, flush])

  const setPaused = useCallback((paused: boolean) => {
    pausedRef.current = paused
    setIsPaused(paused)
    // 恢复时清零角标 —— 用户已经回到最新，未读提示没有意义了
    if (!paused) setNewWhilePaused(0)
  }, [])

  /**
   * 名册统计从事件流派生，而不是让事件源自己维护。
   * 单一时序来源：不会出现「左侧列表说 12 条、右侧却只有 11 条」。
   *
   * ★ 但**基准必须是"事件流 ∪ 名册"，不能是名册**（任务 11 修复）。
   *
   * 原来写的是 `return roster.map(...)` —— 事件统计出来了，却拿名册当权威
   * 名单去筛。于是**没进名册的 agent 一律不显示**，而名册只在"选中某次扫描"
   * 时才请求（`GET /scans/{id}/agents`）。结果是**在「全部活动」（全局流）下
   * 名册为空 → `agents` 恒为空 → 左侧一直显示 `0/0`**，尽管右侧明明有
   * 若干 agent 在刷事件。
   *
   * 那个 bug 最刺眼的地方在于：`ActivityPage` 早在注释里承诺过
   * 「全局流时……那种情况下 agent 统计**仍能从事件流派生**」——
   * 实现没能兑现它。所以这里改成的不是"加个兜底"，而是**把基准换成并集**，
   * 让那句承诺成为事实。
   *
   * 两个方向都要覆盖：
   *   · 名册里有、事件里还没有 → 保留（刚派出去、还没干活，也要看得见）
   *   · 事件里有、名册里没有 → **派生出来**（全局流无名册、或动态产生的子 agent）
   */
  const agents = useMemo(() => {
    const stats = new Map<string, { count: number; lastAt: string; scanId: string }>()
    for (const event of events) {
      const current = stats.get(event.agentId)
      if (!current) {
        stats.set(event.agentId, { count: 1, lastAt: event.at, scanId: event.scanId })
      } else {
        current.count += 1
        if (event.at > current.lastAt) current.lastAt = event.at
      }
    }

    const byId = new Map(roster.map((agent) => [agent.id, agent]))

    const fromRoster = roster.map((agent) => {
      const s = stats.get(agent.id)
      if (!s) return agent
      // 有事件进来的 agent 就是活跃的 —— 覆盖初始名册里的静态状态
      return { ...agent, eventCount: s.count, lastActiveAt: s.lastAt, status: 'running' as const }
    })

    /*
      派生出来的排在名册之后，且按**最近活跃**倒序 ——
      全局流下名册为空，这个顺序就是唯一的顺序，而"谁刚动过"正是要看的信息。
    */
    const derived = [...stats.keys()]
      .filter((id) => !byId.has(id))
      .sort((a, b) => stats.get(b)!.lastAt.localeCompare(stats.get(a)!.lastAt))
      .map((id) => {
        const s = stats.get(id)!
        return {
          id,
          /*
            角色在全局流下**无从得知** —— 它只存在于 `GET /scans/{id}/agents`
            的响应里，而事件载荷（`ActivityEvent`）根本不带 role 字段。
            所以如实标成"未分类"，而不是猜一个：猜错会让人以为某个 agent
            真的在做漏洞利用，而那是会改变处置优先级的判断。
          */
          role: 'unknown' as AgentRole,
          status: 'running' as const,
          // 同理，目标也只有一个来源（名册）。事件里没有它。
          target: '',
          scanId: s.scanId,
          eventCount: s.count,
          lastActiveAt: s.lastAt,
        }
      })

    return [...fromRoster, ...derived]
  }, [events, roster])

  return { events, agents, connection, isPaused, setPaused, newWhilePaused, latestBatchIds }
}

export const ACTIVITY_MAX_EVENTS = MAX_EVENTS
