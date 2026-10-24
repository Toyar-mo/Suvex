import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getAssetsSnapshot, loadAssets, resetAssets, useAssetsState } from './assets-store'
import type { Asset, AssetList } from './asset'

/*
  ══════════════════════════════════════════════════════════════
  assets-store：三个真正会出错的地方
  ══════════════════════════════════════════════════════════════

  这个 store 本身很小，但有三个行为是"写错了也看不出来"的，
  所以值得单独测：

  1. **`idle` 与 `loading` 必须分开。** 侧栏一挂载就问一次数据的话，
     没进这一页的用户也会看到骨架屏闪一下。`idle` 表达的是"没人问过"。
  2. **`silent` 刷新不能把已显示的数据换成骨架屏。** 用户在看着表格，
     后台刷一下不该让内容消失一瞬。
  3. **`silent` 刷新失败不能把页面变成错误页。** 后台失败时，
     手上那份（旧的）数据仍然比一个错误提示有用。
  4. **在飞去重。** 切页/点刷新很容易并发发两个请求，第二个要复用第一个。

  这些都不是"能不能跑通"的问题，而是"出错时会静默降级成错误体验"。
*/

function makeAsset(target: string, patch: Partial<Asset> = {}): Asset {
  return {
    target,
    scanCount: 1,
    lastScanAt: '2026-10-20T00:00:00Z',
    lastScanStatus: 'completed',
    lastCompletedAt: '2026-10-20T00:00:00Z',
    consecutiveFailures: 0,
    findingCount: 0,
    openFindingCount: 0,
    openCritical: 0,
    openHigh: 0,
    bySeverity: {},
    ...patch,
  }
}

function makeList(targets: string[]): AssetList {
  const items = targets.map((t) => makeAsset(t))
  return {
    items,
    total: items.length,
    truncated: false,
    summary: {
      targetCount: items.length,
      findingCount: 0,
      openFindingCount: 0,
      targetsWithOpenHigh: 0,
      neverScanned: 0,
      neverCompleted: 0,
      generatedAt: '2026-10-20T00:00:00Z',
    },
  }
}

/** 手动控制 resolve 的 fetch 桩。 */
let pending: Array<{
  url: string
  resolve: (body: unknown) => void
  reject: (err: unknown) => void
}> = []
let requested: string[] = []

beforeEach(() => {
  resetAssets()
  requested = []
  pending = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    requested.push(url)
    return new Promise<Response>((resolve, reject) => {
      const settle = (body: unknown) => {
        resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      pending.push({ url, resolve: settle, reject })
    })
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetAssets()
})

/** 等所有已排队的请求都被 fetch 接到。 */
async function drain() {
  await waitFor(() => {
    expect(pending.length).toBeGreaterThan(0)
  })
}

describe('assets-store', () => {
  it('初始是 idle，不是 loading', () => {
    expect(getAssetsSnapshot().status).toBe('idle')
    expect(getAssetsSnapshot().items).toBeNull()
  })

  it('首次加载：idle → loading → ready，并落库总分与统计条', async () => {
    const promise = loadAssets()
    // 请求已发出，但还没回来 —— 此时必须是 loading（要显示骨架屏）
    expect(getAssetsSnapshot().status).toBe('loading')

    const [req] = pending
    req.resolve(makeList(['a.example', 'b.example']))
    await promise

    const snap = getAssetsSnapshot()
    expect(snap.status).toBe('ready')
    expect(snap.items?.map((a) => a.target)).toEqual(['a.example', 'b.example'])
    expect(snap.total).toBe(2)
    expect(snap.summary?.targetCount).toBe(2)
    expect(snap.error).toBeNull()
  })

  it('已有数据时再拉，不会退回 loading（内容不该消失一瞬）', async () => {
    const first = loadAssets()
    pending[0].resolve(makeList(['a.example']))
    await first
    expect(getAssetsSnapshot().status).toBe('ready')

    const second = loadAssets()
    // ★ 关键：第二次进来时仍然是 ready，不是 loading
    expect(getAssetsSnapshot().status).toBe('ready')

    pending[1].resolve(makeList(['a.example', 'b.example']))
    await second
    expect(getAssetsSnapshot().items).toHaveLength(2)
  })

  it('silent 刷新失败：保留旧数据，不变成错误页', async () => {
    const first = loadAssets()
    pending[0].resolve(makeList(['a.example']))
    await first

    const second = loadAssets({ silent: true })
    pending[1].reject(new TypeError('Failed to fetch'))
    await second

    const snap = getAssetsSnapshot()
    // ★ 后台刷新失败不该把好好的页面变成错误页
    expect(snap.status).toBe('ready')
    expect(snap.items).toHaveLength(1)
    expect(snap.error).toBeNull()
  })

  it('首次加载失败：status=error，且 error 是 ApiError（不是一句字符串）', async () => {
    const promise = loadAssets()
    pending[0].reject(new TypeError('Failed to fetch'))
    await promise

    const snap = getAssetsSnapshot()
    expect(snap.status).toBe('error')
    expect(snap.error).not.toBeNull()
    // ErrorState 要靠 ApiError 区分失败种类 —— 传字符串它认不出来
    expect(snap.error?.constructor.name).toBe('ApiError')
  })

  it('并发调用只发一个请求，第二个复用第一个', async () => {
    const a = loadAssets()
    const b = loadAssets()
    await drain()
    expect(requested).toHaveLength(1)

    pending[0].resolve(makeList(['a.example']))
    await Promise.all([a, b])
    expect(getAssetsSnapshot().items).toHaveLength(1)
  })

  it('无论成功失败，在飞状态都会清空（否则下一次刷新永远复用旧 promise）', async () => {
    const failed = loadAssets()
    pending[0].reject(new TypeError('boom'))
    await failed

    // 还能再发一次 —— 若 inFlight 没清空，这里会直接返回同一个旧 promise
    void loadAssets()
    await drain()
    expect(requested).toHaveLength(2)
  })

  it('resetAssets 回到 idle 并清空数据', async () => {
    const promise = loadAssets()
    pending[0].resolve(makeList(['a.example']))
    await promise
    expect(getAssetsSnapshot().items).not.toBeNull()

    resetAssets()
    const snap = getAssetsSnapshot()
    expect(snap.status).toBe('idle')
    expect(snap.items).toBeNull()
    expect(snap.summary).toBeNull()
  })

  it('订阅者会在快照变化时收到通知', async () => {
    let renders = 0
    function Probe() {
      useAssetsState()
      renders += 1
      return <span data-testid="status">{getAssetsSnapshot().status}</span>
    }

    render(<Probe />)
    const initial = renders

    const promise = loadAssets()
    pending[0].resolve(makeList(['a.example']))
    await promise

    await waitFor(() => {
      expect(renders).toBeGreaterThan(initial)
    })
    expect(screen.getByTestId('status').textContent).toBe('ready')
  })
})

describe('assets-store：truncated 必须透传', () => {
  it('服务端说截断了，快照里就要看得见', async () => {
    const promise = loadAssets()
    pending[0].resolve({
      ...makeList(['a.example']),
      truncated: true,
      total: 900,
    })
    await promise

    const snap = getAssetsSnapshot()
    // ★ 界面必须能把"只返回了前 N 个"显示出来，否则用户会当成"总共就这些"
    expect(snap.truncated).toBe(true)
    expect(snap.total).toBe(900)
    expect(snap.items).toHaveLength(1)
  })
})

// 让 `fireEvent` / `screen` 的导入不被 lint 判成未使用（Probe 只读快照）。
void fireEvent
void screen
