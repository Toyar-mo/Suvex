import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  ActivityEvent as WireActivityEvent,
  EventSourceHandle,
  StreamStatus,
} from '@/data/activity'
import type { ActivityEvent } from '@/data/overview'

import { useOverviewLiveActivity } from './useOverviewLiveActivity'
import { createSseEventSource } from '@/data/activity-stream'

/*
  ══════════════════════════════════════════════════════════════
  useOverviewLiveActivity 的行为测试
  ══════════════════════════════════════════════════════════════

  这个 hook 把两种数据源拼在一起（/overview 的种子 + SSE 的增量），
  而拼错的后果全都**看起来正常**：

    · 实时事件没去重 → 轮询后同一条显示两遍；
    · `reasoning` 没过掉 → 总览活动流变成过程日志；
    · 种子被当成"新事件" → 一进页面满屏同时播入场动画。

  所以这里钉的是合并规则，不是渲染。
*/

vi.mock('@/data/activity-stream', () => ({
  createSseEventSource: vi.fn(),
}))

const mockedCreate = vi.mocked(createSseEventSource)

function seedEvent(id: string, at: string): ActivityEvent {
  return {
    id,
    scanId: 'scan_seed',
    agentId: 'agent-seed',
    agentPhase: 'scan',
    type: 'tool_call',
    summary: `种子事件 ${id}`,
    subject: null,
    at,
  }
}

/** 完整事件（SSE 形状）。前端类型没声明 `agentPhase`，运行时才有。 */
function wireEvent(
  id: string,
  overrides: Partial<WireActivityEvent> & Record<string, unknown> = {},
): WireActivityEvent {
  return {
    id,
    scanId: 'scan_live',
    agentId: 'agent-live',
    type: 'tool_call',
    summary: `实时事件 ${id}`,
    detail: null,
    at: '2026-11-09T10:00:00.000Z',
    ...overrides,
  } as WireActivityEvent
}

/** 可控的事件源替身 —— 与 `useActivityStream.test.ts` 同一套做法。 */
function makeSource() {
  const listeners = new Set<(event: WireActivityEvent) => void>()
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
      // 契约：注册时立刻回一次当前值
      listener(status)
      return () => statusListeners.delete(listener)
    },
  }

  return {
    source,
    calls,
    push: (event: WireActivityEvent) => {
      for (const listener of listeners) listener(event)
    },
    setStatus: (next: StreamStatus) => {
      status = next
      for (const listener of statusListeners) listener(next)
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  /*
    jsdom 没有 EventSource。hook 用它来判断"这个环境支不支持实时流" ——
    不补上的话所有用例都会走降级分支，测不到合并逻辑。
  */
  vi.stubGlobal('EventSource', class {})
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useOverviewLiveActivity', () => {
  it('没有活跃扫描时不连实时流，只返回种子', () => {
    const seed = [seedEvent('seed-1', '2026-11-09T09:00:00.000Z')]
    const { result } = renderHook(() => useOverviewLiveActivity(seed, false))

    expect(mockedCreate).not.toHaveBeenCalled()
    expect(result.current.events.map((event) => event.id)).toEqual(['seed-1'])
    expect(result.current.connection).toBeNull()
    expect(result.current.latestBatchIds.size).toBe(0)
  })

  it('有活跃扫描时连上，并把增量事件合并到种子前面', () => {
    const fake = makeSource()
    mockedCreate.mockReturnValue(fake.source)

    const seed = [seedEvent('seed-1', '2026-11-09T09:00:00.000Z')]
    const { result } = renderHook(() => useOverviewLiveActivity(seed, true))

    expect(mockedCreate).toHaveBeenCalledTimes(1)
    expect(fake.calls.start).toBe(1)

    act(() => {
      fake.push(
        wireEvent('live-1', {
          type: 'tool_call',
          detail: {
            kind: 'tool_call',
            tool: 'nmap',
            command: 'nmap -sV 10.0.0.5',
            exitCode: 0,
            durationMs: null,
          },
          at: '2026-11-09T10:00:00.000Z',
          agentPhase: 'scan',
        }),
      )
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    expect(result.current.events.map((event) => event.id)).toEqual(['live-1', 'seed-1'])
    expect(result.current.events[0].subject).toBe('nmap -sV 10.0.0.5')
    expect(result.current.latestBatchIds.has('live-1')).toBe(true)
    // 种子不是"新到"的 —— 首次渲染不播动画
    expect(result.current.latestBatchIds.has('seed-1')).toBe(false)
  })

  it('过滤掉 reasoning / failed —— 总览不是过程日志', () => {
    const fake = makeSource()
    mockedCreate.mockReturnValue(fake.source)

    const { result } = renderHook(() => useOverviewLiveActivity([], true))

    act(() => {
      fake.push(
        wireEvent('r-1', {
          type: 'reasoning',
          detail: { kind: 'reasoning', text: '想一想', intent: '想' },
        }),
      )
      fake.push(
        wireEvent('f-1', {
          type: 'failed',
          detail: { kind: 'failed', attempted: 'x', error: 'y' },
        }),
      )
      fake.push(
        wireEvent('d-1', {
          type: 'discovery',
          detail: {
            kind: 'discovery',
            subject: '/api/v1/users',
            evidence: '200',
            confidence: 'high',
          },
          agentPhase: 'scan',
        }),
      )
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })

    expect(result.current.events.map((event) => event.id)).toEqual(['d-1'])
    expect(result.current.events[0].subject).toBe('/api/v1/users')
  })

  it('轮询把同一条事件再发一次时不重复、也不再当新事件', () => {
    const fake = makeSource()
    mockedCreate.mockReturnValue(fake.source)

    const { result, rerender } = renderHook(({ seed }) => useOverviewLiveActivity(seed, true), {
      initialProps: { seed: [] as ActivityEvent[] },
    })

    act(() => {
      fake.push(wireEvent('same-1', { agentPhase: 'verify', at: '2026-11-09T10:00:00.000Z' }))
    })
    act(() => {
      vi.advanceTimersByTime(150)
    })
    expect(result.current.events).toHaveLength(1)

    // 下一轮 /overview 把同一条作为种子发回来
    rerender({ seed: [seedEvent('same-1', '2026-11-09T10:00:00.000Z')] })

    expect(result.current.events).toHaveLength(1)
    expect(result.current.events[0].id).toBe('same-1')
  })

  it('连接状态如实上报，断线时界面才能说"显示的是断线前的事件"', () => {
    const fake = makeSource()
    mockedCreate.mockReturnValue(fake.source)

    const { result } = renderHook(() => useOverviewLiveActivity([], true))
    expect(result.current.connection).toBe('connecting')

    act(() => fake.setStatus('live'))
    expect(result.current.connection).toBe('live')

    act(() => fake.setStatus('disconnected'))
    expect(result.current.connection).toBe('disconnected')
  })

  it('环境不支持 EventSource 时不连、也不冒充实时', () => {
    vi.unstubAllGlobals()
    const { result } = renderHook(() => useOverviewLiveActivity([], true))

    expect(mockedCreate).not.toHaveBeenCalled()
    expect(result.current.connection).toBeNull()
  })
})
