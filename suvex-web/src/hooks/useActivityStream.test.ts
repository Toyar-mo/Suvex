import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ActivityEvent, EventSourceHandle, StreamStatus } from '@/data/activity'
import { ACTIVITY_MAX_EVENTS, useActivityStream } from './useActivityStream'

/*
  ══════════════════════════════════════════════════════════════
  useActivityStream 的行为测试
  ══════════════════════════════════════════════════════════════

  ## 为什么先测这个文件

  它是前端唯一有状态机味道的地方：批量合并、按 id 去重、上限截断、
  暂停缓冲、状态映射，全部在这里。而 2026-10-10 修过的两个 bug
  之一（切换扫描时事件重复）就出在这个文件 —— 那次是**跨实例**
  去重缺失，`emittedIds` 是实例级闭包变量，新实例记不住旧 id。

  当时 `tsc` 与 `eslint` 全绿，`verify:*` 也全绿 —— 它们验不了行为。

  ## 替身事件源

  真的 `EventSourceHandle` 由 `activity-stream.ts` 实现（连 SSE）。
  这里用**手写替身**而不是 mock 库：接口只有四个方法，替身能精确
  控制"什么时候推事件、推什么"，比 mock 的调用断言更贴行为。
  而且替身与真实实现共享同一个 TS 接口 —— 接口改了这里会编译报错，
  mock 则不会。
*/

/** 造一条事件。只填测试关心的字段，其余给稳定默认值。 */
function makeEvent(id: string, overrides: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id,
    scanId: 'scan_a',
    agentId: 'agent-01',
    // ⚠️ 必须是 `ActivityEventType` 里的取值。第一版我写了 `narration`
    //    —— 那是**事件渲染**里的叫法，不是类型里的。类型检查当场报错，
    //    而这条错误只在测试文件被 tsconfig 覆盖后才看得见（见下）。
    type: 'reasoning',
    summary: `事件 ${id}`,
    detail: null,
    at: '2026-10-10T10:00:00.000Z',
    ...overrides,
  }
}

/**
 * 可控的事件源替身。
 *
 * `push` 手动推事件（模拟 SSE 到达），`setStatus` 手动改连接状态，
 * 并记录 `start` / `stop` 被调了几次 —— 切换扫描时的清理行为要靠它验证。
 */
function makeSource() {
  const listeners = new Set<(event: ActivityEvent) => void>()
  const statusListeners = new Set<(status: StreamStatus) => void>()
  let status: StreamStatus = 'connecting'
  const calls = { start: 0, stop: 0 }

  const source: EventSourceHandle = {
    start: () => {
      calls.start += 1
    },
    stop: () => {
      calls.stop += 1
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    subscribeStatus: (listener) => {
      statusListeners.add(listener)
      // 与真实实现一致：注册时立即回一次当前值
      listener(status)
      return () => statusListeners.delete(listener)
    },
  }

  return {
    source,
    calls,
    push: (...events: ActivityEvent[]) => {
      for (const event of events) for (const listener of listeners) listener(event)
    },
    setStatus: (next: StreamStatus) => {
      status = next
      for (const listener of statusListeners) listener(next)
    },
  }
}

// ══════════════════════════════════════════════════════════════
// 一、状态映射
// ══════════════════════════════════════════════════════════════

describe('连接状态映射', () => {
  it('初始为 connecting —— 还没有收到任何状态回调时的默认值', () => {
    const { source } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    expect(result.current.connection).toBe('connecting')
  })

  it('live 映射为 live，disconnected 映射为 disconnected', () => {
    const { source, setStatus } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => setStatus('live'))
    expect(result.current.connection).toBe('live')

    act(() => setStatus('disconnected'))
    expect(result.current.connection).toBe('disconnected')
  })

  it('★ paused 不映射成 disconnected —— 用户暂停不是连接问题', () => {
    /*
      这一条防的是"把两种不同的东西混成一个词"：
      `paused` 是**用户主动**暂停自动滚动，连接好得很；
      `disconnected` 是流断了。

      若把 paused 也显示成"已断开"，用户会以为实时流坏了，
      然后去刷新页面 —— 而他其实只是往上翻了一下历史。
    */
    const { source, setStatus } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => setStatus('live'))
    act(() => setStatus('paused'))

    expect(result.current.connection).not.toBe('disconnected')
    expect(result.current.connection).toBe('connecting')
  })

  it('订阅时立即拿到当前状态，不需要等下一次变化', () => {
    // `subscribeStatus` 的接口约定：注册时回一次当前值。
    // 少了它，界面在"订阅之后、第一次变化之前"那段空窗里不知道显示什么。
    const { source, setStatus } = makeSource()
    setStatus('live')

    const { result } = renderHook(() => useActivityStream(source, []))

    expect(result.current.connection).toBe('live')
  })
})

// ══════════════════════════════════════════════════════════════
// 二、事件去重（2026-10-10 修的那个 bug）
// ══════════════════════════════════════════════════════════════

describe('按 id 去重', () => {
  it('同一条事件推两次只显示一条', async () => {
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    const event = makeEvent('evt_01')
    act(() => push(event, event))

    await waitFor(() => expect(result.current.events).toHaveLength(1))
    expect(result.current.events[0].id).toBe('evt_01')
  })

  it('★ 切换事件源后，旧事件不会被新源重复推一遍', async () => {
    /*
      这是那次真实 bug 的直接复现。

      路径：`?scan=A` → `?scan=B` → 回 `?scan=A`。
      每次 `scanFilter` 变化都会建**新实例**，而实例级的 `emittedIds`
      在新实例里是空的 —— 于是 seed 会把同一批历史事件再 emit 一遍，
      而 `events` 是 useState，它**不随 source 变化而重置**。

      两者一撞：重复条目 + `key={event.id}` 撞车（React 会报重复 key）。
      修法是两道防线：实例内 `emittedIds`、跨实例按 id 去重（本测试盯后者）。
    */
    const first = makeSource()
    const { result, rerender } = renderHook(
      ({ source }) => useActivityStream(source, []),
      { initialProps: { source: first.source } },
    )

    const event = makeEvent('evt_same')
    act(() => first.push(event))
    await waitFor(() => expect(result.current.events).toHaveLength(1))

    // 换一个新源（模拟切换扫描），把**同一条**事件再推一次
    const second = makeSource()
    rerender({ source: second.source })
    act(() => second.push(event))

    // 仍然只有一条 —— 不能变成两条
    await waitFor(() => expect(result.current.events).toHaveLength(1))
  })

  it('★ 换源后新事件正常显示（去重不能误伤）', async () => {
    /*
      去重不能误伤：换源之后**新的**事件必须照常显示，
      否则"修好了重复"就变成了"什么都看不见"。

      ⚠️ 我第一版把这条写成"追加到 2 条"，**假设是错的**：
      换源时 effect 清理会 `setEvents([])`（见"切换事件源"那一组），
      所以换源后列表从 0 开始，新事件进来是 **1 条**。

      这不是测试写错就算了的细节 —— 它正是设计意图：
      换源 = 换了看的目标，旧目标的事件不该留在列表里。
      所以断言改成"新事件在，且只有它"。
    */
    const first = makeSource()
    const { result, rerender } = renderHook(
      ({ source }) => useActivityStream(source, []),
      { initialProps: { source: first.source } },
    )

    act(() => first.push(makeEvent('evt_old')))
    await waitFor(() => expect(result.current.events).toHaveLength(1))

    const second = makeSource()
    rerender({ source: second.source })
    // 换源后旧事件被清掉（这是刻意的，见上面那条说明）
    await waitFor(() => expect(result.current.events).toHaveLength(0))

    act(() => second.push(makeEvent('evt_new')))

    await waitFor(() => expect(result.current.events).toHaveLength(1))
    expect(result.current.events[0].id).toBe('evt_new')
  })
})

// ══════════════════════════════════════════════════════════════
// 三、切换事件源时的状态清理
// ══════════════════════════════════════════════════════════════

describe('切换事件源', () => {
  it('★ 换源时清空已显示的事件', async () => {
    /*
      与上面那条去重是配套的：清了之后，新源的 seed 才能正常显示。

      为什么必须清：新源的 `emittedIds` 是空的，它会重新 emit 一遍
      历史。如果不清，那些历史会与已有的撞 id 被去重掉 ——
      看起来"没重复"，但**新扫描自己的历史永远显示不出来**
      （因为旧扫描的同 id 事件占着位置）。清空才是正解。
    */
    const first = makeSource()
    const { result, rerender } = renderHook(
      ({ source }) => useActivityStream(source, []),
      { initialProps: { source: first.source } },
    )

    act(() => first.push(makeEvent('evt_a1'), makeEvent('evt_a2')))
    await waitFor(() => expect(result.current.events).toHaveLength(2))

    rerender({ source: makeSource().source })

    await waitFor(() => expect(result.current.events).toHaveLength(0))
  })

  it('换源时旧源被 stop、新源被 start', async () => {
    const first = makeSource()
    const { rerender } = renderHook(
      ({ source }) => useActivityStream(source, []),
      { initialProps: { source: first.source } },
    )

    expect(first.calls.start).toBe(1)

    const second = makeSource()
    rerender({ source: second.source })

    await waitFor(() => expect(first.calls.stop).toBeGreaterThan(0))
    expect(second.calls.start).toBe(1)
  })

  it('换源时暂停计数清零 —— 它属于旧流', async () => {
    const first = makeSource()
    const { result, rerender } = renderHook(
      ({ source }) => useActivityStream(source, []),
      { initialProps: { source: first.source } },
    )

    act(() => result.current.setPaused(true))
    act(() => first.push(makeEvent('evt_p1'), makeEvent('evt_p2')))
    await waitFor(() => expect(result.current.newWhilePaused).toBe(2))

    rerender({ source: makeSource().source })

    await waitFor(() => expect(result.current.newWhilePaused).toBe(0))
  })
})

// ══════════════════════════════════════════════════════════════
// 四、排序与上限
// ══════════════════════════════════════════════════════════════

describe('排序与上限', () => {
  it('★ 按 at 排序，而不是按到达顺序', async () => {
    /*
      真实 SSE 里事件可能乱序到达（重连补发、多路合并）。
      按到达顺序显示会让时间线前后颠倒 —— 而用户读的正是"先发生了什么"。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() =>
      push(
        makeEvent('evt_late', { at: '2026-10-10T10:00:09.000Z' }),
        makeEvent('evt_early', { at: '2026-10-10T10:00:01.000Z' }),
        makeEvent('evt_mid', { at: '2026-10-10T10:00:05.000Z' }),
      ),
    )

    await waitFor(() => expect(result.current.events).toHaveLength(3))
    expect(result.current.events.map((e) => e.id)).toEqual([
      'evt_early',
      'evt_mid',
      'evt_late',
    ])
  })

  it('超过上限时丢**最老的**，保留最近的', async () => {
    /*
      实时流页面的价值在"最近发生了什么"，所以截断要丢老的那一头。
      丢错方向的话，用户打开页面看到的是一小时前的事件，
      而最新的被挤掉了 —— 那比不截断更糟。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    const total = ACTIVITY_MAX_EVENTS + 5
    const events = Array.from({ length: total }, (_, i) =>
      makeEvent(`evt_${String(i).padStart(5, '0')}`, {
        // 递增时间戳，保证顺序确定
        at: new Date(Date.UTC(2026, 9, 10, 10, 0, 0, i)).toISOString(),
      }),
    )
    act(() => push(...events))

    await waitFor(() => expect(result.current.events).toHaveLength(ACTIVITY_MAX_EVENTS))
    // 最老的那 5 条应当被丢掉
    expect(result.current.events[0].id).toBe('evt_00005')
    expect(result.current.events.at(-1)?.id).toBe(`evt_${String(total - 1).padStart(5, '0')}`)
  })
})

// ══════════════════════════════════════════════════════════════
// 五、暂停时不丢事件
// ══════════════════════════════════════════════════════════════

describe('暂停', () => {
  it('★ 暂停期间事件照常收进列表，只是计数增加', async () => {
    /*
      暂停的语义是"别自动滚动"，**不是**"别收事件"。
      若暂停就停止接收，用户回到最新时会发现中间空了一段 ——
      那比打断阅读更糟。

      所以这里断言两件事：事件**确实进来了**，且计数**确实涨了**。
      只断言计数会漏掉"计数涨了但事件没进来"这种半吊子实现。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => result.current.setPaused(true))
    act(() => push(makeEvent('evt_1'), makeEvent('evt_2'), makeEvent('evt_3')))

    await waitFor(() => expect(result.current.events).toHaveLength(3))
    expect(result.current.newWhilePaused).toBe(3)
    expect(result.current.isPaused).toBe(true)
  })

  it('恢复时清零角标', async () => {
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => result.current.setPaused(true))
    act(() => push(makeEvent('evt_1')))
    await waitFor(() => expect(result.current.newWhilePaused).toBe(1))

    act(() => result.current.setPaused(false))

    expect(result.current.newWhilePaused).toBe(0)
    expect(result.current.isPaused).toBe(false)
  })
})

// ══════════════════════════════════════════════════════════════
// 六、agent 派生（任务 11 修过的 bug）
// ══════════════════════════════════════════════════════════════

describe('agent 统计', () => {
  it('★ 名册为空时仍从事件流派生 agent', () => {
    /*
      这是任务 11 修的那个 bug：原来 `return roster.map(...)` 拿名册当
      权威名单去筛，于是**没进名册的 agent 一律不显示**。

      而名册只在"选中某次扫描"时才请求，全局流下它恒为空 ——
      结果是左侧一直显示 `0/0`，尽管右侧明明有 agent 在刷事件。

      修法是把基准换成"事件流 ∪ 名册"。这条测试盯的就是那个并集。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() =>
      push(
        makeEvent('e1', { agentId: 'agent-07' }),
        makeEvent('e2', { agentId: 'agent-07' }),
        makeEvent('e3', { agentId: 'agent-09' }),
      ),
    )

    return waitFor(() => {
      expect(result.current.agents.map((a) => a.id).sort()).toEqual(['agent-07', 'agent-09'])
      const seven = result.current.agents.find((a) => a.id === 'agent-07')
      expect(seven?.eventCount).toBe(2)
    })
  })

  it('名册里有、事件里还没有的 agent 要保留', () => {
    // 刚派出去、还没干活，也要看得见 —— 否则用户以为它没被派出去。
    const { source } = makeSource()
    const roster = [
      {
        id: 'agent-idle',
        role: 'recon' as const,
        status: 'idle' as const,
        target: 'example.com',
        scanId: 'scan_a',
        eventCount: 0,
        lastActiveAt: '2026-10-10T09:00:00.000Z',
      },
    ]
    const { result } = renderHook(() => useActivityStream(source, roster))

    expect(result.current.agents.map((a) => a.id)).toEqual(['agent-idle'])
  })

  it('名册里的 agent 收到事件后状态变为 running', async () => {
    const { source, push } = makeSource()
    const roster = [
      {
        id: 'agent-01',
        role: 'recon' as const,
        status: 'idle' as const,
        target: 'example.com',
        scanId: 'scan_a',
        eventCount: 0,
        lastActiveAt: '2026-10-10T09:00:00.000Z',
      },
    ]
    const { result } = renderHook(() => useActivityStream(source, roster))

    act(() => push(makeEvent('e1', { agentId: 'agent-01' })))

    await waitFor(() => expect(result.current.agents[0].status).toBe('running'))
    expect(result.current.agents[0].eventCount).toBe(1)
  })

  it('★ 派生出的 agent 角色是 unknown，不猜', () => {
    /*
      role 只存在于 `GET /scans/{id}/agents` 的响应里，
      `ActivityEvent` 根本不带这个字段。

      猜一个的后果：让人以为某个 agent 真的在做漏洞利用，
      而那是会改变处置优先级的判断。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => push(makeEvent('e1', { agentId: 'agent-99' })))

    return waitFor(() => {
      const derived = result.current.agents.find((a) => a.id === 'agent-99')
      expect(derived?.role).toBe('unknown')
    })
  })
})

// ══════════════════════════════════════════════════════════════
// 七、批量合并（性能相关的行为）
// ══════════════════════════════════════════════════════════════

describe('批量合并', () => {
  it('突发多条事件只触发一次渲染批次', async () => {
    /*
      flush 的窗口是 120ms。同一 tick 里推 50 条应当合并成一次 setState，
      而不是 50 次 —— 后者在真实流里会让页面卡顿。

      这里用"事件最终都在"来间接证明合并没丢东西；
      `latestBatchIds` 记录的是**这一批**的 id，用它验证确实只分了一批。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    const batch = Array.from({ length: 50 }, (_, i) => makeEvent(`evt_${i}`))
    act(() => push(...batch))

    await waitFor(() => expect(result.current.events).toHaveLength(50))
  })

  it('首批（seed）不播入场动画', async () => {
    /*
      一进页面十几条一起淡入会很吵。`hasSeededRef` 保证只有**之后**
      到达的批次才记入 `latestBatchIds`。

      这里断言首批之后 `latestBatchIds` 为空。
    */
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => push(makeEvent('seed_1'), makeEvent('seed_2')))
    await waitFor(() => expect(result.current.events).toHaveLength(2))

    expect(result.current.latestBatchIds.size).toBe(0)
  })

  it('首批之后的新事件记入 latestBatchIds', async () => {
    const { source, push } = makeSource()
    const { result } = renderHook(() => useActivityStream(source, []))

    act(() => push(makeEvent('seed_1')))
    await waitFor(() => expect(result.current.events).toHaveLength(1))

    act(() => push(makeEvent('live_1')))
    await waitFor(() => expect(result.current.latestBatchIds.has('live_1')).toBe(true))
  })
})

// ══════════════════════════════════════════════════════════════
// 八、导出常量与实现一致
// ══════════════════════════════════════════════════════════════

describe('导出常量', () => {
  it('ACTIVITY_MAX_EVENTS 与内部上限一致', () => {
    // 这个常量是给测试与文档用的。它若与内部值漂移，
    // 所有基于它的断言都会失去意义（而测试仍然"通过"）。
    expect(ACTIVITY_MAX_EVENTS).toBe(5000)
  })
})
