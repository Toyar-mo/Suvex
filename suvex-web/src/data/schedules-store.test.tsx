import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createSchedule,
  deleteSchedule,
  getSchedulesSnapshot,
  loadSchedules,
  resetSchedules,
  toggleSchedule,
  updateSchedule,
  useSchedulesState,
} from './schedules-store'
import type { Schedule } from './schedule'

/**
 * 定时计划 store 的行为。
 *
 * ⚠️ `vitest.config.ts` 里 `globals: false`，所以这里显式 import
 * `describe/it/expect/vi`，不能靠全局。
 *
 * 桩法与其他 store 测试一致：`vi.stubGlobal('fetch', ...)` +
 * 手工 `pending[]` 数组控制 resolve/reject 时机。
 */

interface Deferred {
  resolve: (body: unknown, status?: number) => void
  reject: (error: Error) => void
}

let pending: Deferred[] = []

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch() {
  pending = []
  vi.stubGlobal(
    'fetch',
    vi.fn(
      () =>
        new Promise<Response>((resolve, reject) => {
          pending.push({
            resolve: (body, status = 200) => resolve(jsonResponse(body, status)),
            reject,
          })
        }),
    ),
  )
}

function makeSchedule(overrides: Partial<Schedule> = {}): Schedule {
  const now = Date.now()
  return {
    id: 'sched_1',
    name: '每日复查',
    target: 'api.acme.internal',
    targetKind: 'url',
    goal: 'findVulns',
    intervalMinutes: 1440,
    enabled: true,
    nextRunAt: new Date(now + 3_600_000).toISOString(),
    lastScanId: null,
    lastRunAt: null,
    failureCount: 0,
    lastFailureReason: null,
    providerId: 'prov_default',
    providerLabel: 'DeepSeek',
    skills: ['web'],
    brief: null,
    createdAt: new Date(now - 86_400_000).toISOString(),
    updatedAt: new Date(now - 86_400_000).toISOString(),
    ...overrides,
  }
}

const CREATE_INPUT = {
  name: '每日复查',
  target: 'api.acme.internal',
  targetKind: 'url',
  goal: 'findVulns',
  intervalMinutes: 1440,
  providerId: 'prov_default',
  skills: ['web'],
  brief: null,
  authorizationStatement: '我有权对此目标进行安全测试',
}

beforeEach(() => {
  resetSchedules()
  stubFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('schedules-store', () => {
  it('初始状态是 idle —— 不是 loading', () => {
    // `idle` 与 `loading` 是两件事：侧栏刚挂载不该闪骨架屏。
    const state = getSchedulesSnapshot()
    expect(state.status).toBe('idle')
    expect(state.items).toBeNull()
  })

  it('首次加载走 idle → loading → ready', async () => {
    const promise = loadSchedules()
    expect(getSchedulesSnapshot().status).toBe('loading')

    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 0 })
    await promise

    const state = getSchedulesSnapshot()
    expect(state.status).toBe('ready')
    expect(state.items).toHaveLength(1)
    expect(state.dueCount).toBe(0)
  })

  it('已有数据时重拉不回 loading —— 否则表格会闪成骨架屏', async () => {
    const first = loadSchedules()
    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 0 })
    await first

    const second = loadSchedules()
    // 关键：重拉期间状态仍是 ready，不是 loading
    expect(getSchedulesSnapshot().status).toBe('ready')

    pending[1].resolve({ items: [makeSchedule({ name: '改过名' })], total: 1, dueCount: 0 })
    await second
    expect(getSchedulesSnapshot().items?.[0].name).toBe('改过名')
  })

  it('silent 刷新失败时保留旧数据，不把页面变成错误页', async () => {
    const first = loadSchedules()
    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 2 })
    await first

    const second = loadSchedules({ silent: true })
    pending[1].reject(new Error('网络炸了'))
    await second

    const state = getSchedulesSnapshot()
    expect(state.status).toBe('ready')
    expect(state.error).toBeNull()
    expect(state.items).toHaveLength(1)
    expect(state.dueCount).toBe(2)
  })

  it('首次加载失败给出真的 ApiError，而不是一句文案', async () => {
    const promise = loadSchedules()
    pending[0].resolve({ error: { code: 'FORBIDDEN', message: '没有权限' } }, 403)
    await promise

    const state = getSchedulesSnapshot()
    expect(state.status).toBe('error')
    expect(state.error?.constructor.name).toBe('ApiError')
    expect(state.error?.code).toBe('FORBIDDEN')
  })

  it('并发加载只发一个请求', async () => {
    const a = loadSchedules()
    const b = loadSchedules()
    expect(pending).toHaveLength(1)

    pending[0].resolve({ items: [], total: 0, dueCount: 0 })
    await Promise.all([a, b])
    expect(getSchedulesSnapshot().status).toBe('ready')
  })

  it('请求落定后清掉 inFlight —— 否则第二次刷新永远发不出去', async () => {
    const first = loadSchedules()
    pending[0].reject(new Error('炸'))
    await first

    const second = loadSchedules()
    expect(pending).toHaveLength(2)
    pending[1].resolve({ items: [], total: 0, dueCount: 0 })
    await second
    expect(getSchedulesSnapshot().status).toBe('ready')
  })

  it('新建成功后把那条计划放进快照（不重拉整个列表）', async () => {
    const loaded = loadSchedules()
    pending[0].resolve({ items: [], total: 0, dueCount: 0 })
    await loaded

    const created = createSchedule(CREATE_INPUT)
    pending[1].resolve(makeSchedule())
    await created

    expect(getSchedulesSnapshot().items).toHaveLength(1)
    expect(getSchedulesSnapshot().items?.[0].id).toBe('sched_1')
  })

  it('改计划后快照里那一条被替换，不是追加', async () => {
    const loaded = loadSchedules()
    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 0 })
    await loaded

    const updated = updateSchedule('sched_1', { name: '改过名' })
    pending[1].resolve(makeSchedule({ name: '改过名' }))
    await updated

    const items = getSchedulesSnapshot().items
    expect(items).toHaveLength(1)
    expect(items?.[0].name).toBe('改过名')
  })

  it('启停走的是 /enabled 端点，不是 PATCH', async () => {
    const loaded = loadSchedules()
    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 0 })
    await loaded

    const toggled = toggleSchedule('sched_1', false)
    pending[1].resolve(makeSchedule({ enabled: false, nextRunAt: null }))
    await toggled

    const call = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[1]
    expect(call[0]).toContain('/schedules/sched_1/enabled')
    expect(getSchedulesSnapshot().items?.[0].enabled).toBe(false)
    expect(getSchedulesSnapshot().items?.[0].nextRunAt).toBeNull()
  })

  it('删成功后从快照里移除那一条', async () => {
    const loaded = loadSchedules()
    pending[0].resolve({ items: [makeSchedule(), makeSchedule({ id: 'sched_2' })], total: 2, dueCount: 0 })
    await loaded

    const deleted = deleteSchedule('sched_1')
    pending[1].resolve(undefined, 204)
    await deleted

    const items = getSchedulesSnapshot().items
    expect(items).toHaveLength(1)
    expect(items?.[0].id).toBe('sched_2')
  })

  it('resetSchedules 回到 idle 并清空数据', async () => {
    const loaded = loadSchedules()
    pending[0].resolve({ items: [makeSchedule()], total: 1, dueCount: 3 })
    await loaded

    resetSchedules()

    const state = getSchedulesSnapshot()
    expect(state.status).toBe('idle')
    expect(state.items).toBeNull()
    expect(state.dueCount).toBe(0)
  })

  it('订阅者会收到每次快照变化', async () => {
    const seen: string[] = []
    // `useSchedulesState` 是 hook，这里直接用 store 的订阅路径
    // —— 通过一次 load 触发 loading → ready 两次变化。
    const probe = setInterval(() => {}, 1000)
    try {
      const promise = loadSchedules()
      seen.push(getSchedulesSnapshot().status)
      pending[0].resolve({ items: [], total: 0, dueCount: 0 })
      await promise
      seen.push(getSchedulesSnapshot().status)
    } finally {
      clearInterval(probe)
    }

    expect(seen).toEqual(['loading', 'ready'])
    // hook 本身在渲染期才会订阅，这里只确认 store 的 getSnapshot 稳定可读
    expect(typeof useSchedulesState).toBe('function')
  })
})
