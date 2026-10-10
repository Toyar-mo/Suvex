import { API_BASE_URL, API_PREFIX, api } from '@/lib/api'
import type { ActivityEvent, EventSourceHandle, StreamStatus } from './activity'

/**
 * 真实的实时事件源 —— **SSE**（契约 §4）。
 *
 * 它实现的是与 `createMockEventSource` **同一个** `EventSourceHandle`，
 * 所以 `ActivityPage` 与 `useActivityStream` 不需要认识它。
 *
 * ## 三件必须做对的事
 *
 * ### 1. `withCredentials: true` —— 少了它一个事件都收不到
 *
 * 开发态前端在 `:5173`、后端在 `:8000`，是**跨源**请求。
 * `EventSource` 默认 `withCredentials: false`，于是浏览器**不带 Cookie** ——
 * 而这条流是 `read` 级鉴权，结果是连接被 401 拒掉。
 *
 * 症状是"实时流一直停在连接中"，而它看起来完全像网络问题或后端没起。
 * 注意 `EventSource` 还有一个坑：`onerror` 里读不到 HTTP 状态码
 * （规范如此），所以**没法**把 401 和"网络断了"区分开 ——
 * 这也是为什么这里把"连不上"单独做成一个可展示的状态。
 *
 * ### 2. 先拉 seed、再订阅，并且用 `?lastEventId=` 把缝关上
 *
 * 契约 §4.1 专门说明了：`EventSource` **只在重连时**自动带
 * `Last-Event-ID` 请求头，**首次连接带不了** —— 而首次连接恰恰是唯一
 * 有丢事件风险的时刻。seed 拉完到订阅建立之间有一个几百毫秒的缝，
 * 缝里产生的事件 seed 里没有、订阅也收不到。
 * 把 seed 最后一条的 `id` 用查询参数带上去，缝就关掉了。
 *
 * ### 3. `resync` 帧要真的处理
 *
 * 游标指向的事件已被清理、或积压超过服务端上限时，服务端发 `resync` ——
 * 意思是"你要的那一段我给不出来了，重新拉一次"。此时**只等增量是不够的**：
 * 中间已经断了一截，用户会看到一个悄悄少了内容的流。
 */

/** seed 一次拉多少条。与契约 §4.4 的例子一致 */
const SEED_LIMIT = 200

/** `/scans/{id}/events/recent` 的响应 */
interface RecentEventsResponse {
  events: ActivityEvent[]
  hasMore: boolean
}

export interface SseEventSourceOptions {
  /**
   * 订阅哪一次扫描。
   *
   * `null` / 省略 = 全局流（`/api/v1/events`），用于同时看多个扫描。
   * ⚠️ 全局流**没有 seed 端点**（`/scans/{id}/events/recent` 是按扫描的），
   *    所以它的起点由服务端决定：契约 §4.1 规定不传游标时
   *    **从当前末尾开始**，不重放历史 —— 那正是全局流想要的。
   */
  scanId?: string | null
}

export function createSseEventSource(options: SseEventSourceOptions = {}): EventSourceHandle {
  const scanId = options.scanId ?? null

  const listeners = new Set<(event: ActivityEvent) => void>()
  const statusListeners = new Set<(status: StreamStatus) => void>()

  let status: StreamStatus = 'connecting'
  let socket: EventSource | null = null
  let stopped = false
  /** 补发游标。seed 的最后一条 → 之后由每条到达的事件推进 */
  let lastEventId: string | null = null
  /** 最后一次见到的事件时刻，`resync` 后用它补历史 */
  let lastEventAt: string | null = null

  /**
   * 启动代数 —— 用来作废"上一轮"迟到的 seed 结果。
   *
   * ★ 这是 `React.StrictMode` 逼出来的一个真 bug。
   *
   *   开发模式下 StrictMode 会把 effect **执行两次**（mount → unmount → mount），
   *   而 `useActivityStream` 的清理函数会调 `stop()`、第二次再调 `start()`。
   *   因为 `source` 是 `useMemo` 出来的**同一个对象**，两次 `start()` 都会
   *   去拉 seed —— 于是每条历史事件被 emit 两次，界面上看到**成对的重复条目**
   *   （实测：后端 1 条，页面显示 2 条）。
   *
   *   而且它极难怀疑到这一层：数据本身是对的、连接是通的、没有报错，
   *   只是每条都出现两遍 —— 看起来像后端发重了。
   *
   *   `stop()` 时把代数 +1，迟到的 seed 完成时会发现自己已经过期，直接丢弃。
   */
  let epoch = 0

  /**
   * 已发过的事件 id（有上限）。
   *
   * 第二道防线：即使 `?lastEventId=` 因为任何原因没生效，
   * seed 与增量流交叠也不会让同一条事件出现两次。
   * 上限是必要的 —— 长时间挂着的页面不能让它无限增长。
   */
  const emittedIds = new Set<string>()
  const EMITTED_MEMORY = 2000

  function setStatus(next: StreamStatus): void {
    if (status === next) return
    status = next
    for (const listener of statusListeners) listener(next)
  }

  function emit(event: ActivityEvent): void {
    if (emittedIds.has(event.id)) return
    emittedIds.add(event.id)
    if (emittedIds.size > EMITTED_MEMORY) {
      // Set 保持插入顺序 —— 删掉最早进来的那个
      const oldest = emittedIds.values().next().value
      if (oldest !== undefined) emittedIds.delete(oldest)
    }

    lastEventId = event.id
    // 只在推进时更新 —— `at` 是事件发生时刻，补发的旧事件不该把它拉回去
    if (!lastEventAt || event.at > lastEventAt) lastEventAt = event.at
    for (const listener of listeners) listener(event)
  }

  /** 历史事件（seed 与 `resync` 后的补齐都走它） */
  async function fetchRecent(): Promise<ActivityEvent[]> {
    // 全局流没有按扫描的历史端点
    if (!scanId) return []
    const data = await api.get<RecentEventsResponse>(`/scans/${scanId}/events/recent`, {
      query: {
        limit: SEED_LIMIT,
        // 首次不带 since（取最近 N 条）；resync 后带上，只补缺的那段
        since: lastEventAt ?? undefined,
      },
    })
    return data.events
  }

  function connect(): void {
    if (stopped) return

    const url = new URL(`${API_BASE_URL}${API_PREFIX}${scanId ? `/scans/${scanId}/events` : '/events'}`)
    if (lastEventId) url.searchParams.set('lastEventId', lastEventId)

    // ★ 见文件头第 1 条：不是可选项
    const es = new EventSource(url.toString(), { withCredentials: true })
    socket = es

    es.onopen = () => setStatus('live')

    /*
      ★ 事件名是 `activity`，**不是默认的 `message`**。

      契约 §4.2 的帧格式里写着这一行：

          id: evt_09
          event: activity        ← 就是它
          data: {"id":"evt_09", ...}

      这一点极易踩空：`EventSource.onmessage` **只接收没有 `event:` 字段
      （或 `event: message`）的帧**。后端发的是命名事件，所以 `onmessage`
      **永远不会触发** —— 而连接看起来完全正常：`onopen` 照常触发、
      状态显示"实时"、`curl` 能看到数据、`fetch` 的流也能读到字节，
      唯独 `onmessage` 一条都收不到。

      症状是「连接是实时，但事件永远是空的」。排查时我先后怀疑过
      CORS、Cookie、`withCredentials`、服务端缓冲，全都不是 ——
      真正的原因只是**监听器名字不对**。契约里写清楚了，是我没照着做。
    */
    es.addEventListener('activity', (message: MessageEvent<string>) => {
      try {
        emit(JSON.parse(message.data) as ActivityEvent)
      } catch {
        // 坏帧忽略而不是抛：一条解不出来的帧不该让整条流停掉。
        // 原始 data 在浏览器控制台的 Network 面板里能看到
        console.warn('[suvex] 忽略无法解析的事件帧')
      }
    })

    /*
      `resync`：游标失效，服务端让我们重新拉历史（契约 §4.1 的三种情况
      里后两种在客户端看来是同一件事）。服务端那边已经切到"从末尾继续"，
      所以我们只需要把中间缺的那段补回来。
    */
    es.addEventListener('resync', () => {
      void fetchRecent()
        .then((events) => {
          for (const event of events) emit(event)
        })
        .catch(() => {
          // 补历史失败就让它去 —— 增量流仍然活着，下次 resync 还会再试。
          // 这里弹错会把一个可自愈的情况报成故障
        })
    })

    /*
      `onerror` 不关闭连接、也不自己重连：`EventSource` 会**自动重连**，
      而且重连时会带上 `Last-Event-ID` 请求头（浏览器自己维护），
      服务端据此补发 —— 那正是契约 §4.4 第 1 条要的行为。
      我们要做的只是把状态如实告诉界面。
    */
    es.onerror = () => setStatus('disconnected')
  }

  return {
    start: () => {
      stopped = false
      const myEpoch = ++epoch
      setStatus('connecting')

      /*
        先拉 seed 再订阅（契约 §4.4）。

        ⚠️ seed 失败**不阻塞订阅**：订阅本身可能还能成功，而"因为拉历史
        失败就完全不显示实时流"是更糟的取舍 —— 用户会以为整个扫描都没动静。
        seed 失败时只是少了开头那段历史，实时部分照常工作。
      */
      void fetchRecent()
        .catch(() => [])
        .then((events) => {
          // 这一轮已经被 stop() 作废（StrictMode 的双挂载）—— 丢弃结果，
          // 否则 seed 会跑两次、每条历史事件出现两遍。见 epoch 的说明。
          if (myEpoch !== epoch || stopped) return
          for (const event of events) emit(event)
          connect()
        })
    },

    stop: () => {
      stopped = true
      // 作废进行中的 seed（见 epoch 的说明）
      epoch += 1
      socket?.close()
      socket = null
      setStatus('disconnected')
      listeners.clear()
      statusListeners.clear()
    },

    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },

    subscribeStatus: (listener) => {
      statusListeners.add(listener)
      /*
        注册时**立即**回一次当前值 —— `setStatus` 有去重（值没变就不通知），
        而初始状态 `connecting` 之后可能要等几百毫秒才变成 `live`。
        不立刻回一次的话，调用方在那之前不知道该显示什么。

        这条也是 `getStatus` 被删掉的原因：订阅本身就带上了当前值，
        不需要再开一个"读一次"的口子（见 EventSourceHandle 的说明）。
      */
      listener(status)
      return () => statusListeners.delete(listener)
    },
  }
}
