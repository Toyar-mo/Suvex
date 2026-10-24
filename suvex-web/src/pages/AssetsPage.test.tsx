// ⚠️ 必须手动 import `cleanup`：本仓库没开 vitest 的 `globals: true`，
//    RTL 自动清理只在 globals 模式下注册。漏了它的症状是「Found multiple
//    elements」——上一个用例的 DOM 留在 document 里。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AssetsPage from './AssetsPage'
import { resetAssets } from '@/data/assets-store'
import type { Asset, AssetList } from '@/data/asset'

/*
  ══════════════════════════════════════════════════════════════
  AssetsPage：这一页的判据是「能不能把不能信的目标顶上来」
  ══════════════════════════════════════════════════════════════

  这一页最容易写成"一个能跑的表格"—— 数据渲出来了，看着挺正常，
  但它存在**唯一理由**是把「看起来干净但其实没被检查过」的目标和
  「真的检查过、确实没问题」分开。写不出这个区分，表格再漂亮也没用。

  所以测试集中在三件事：
  ① 排序把「从未跑完 / 连续失败」顶到前面，而不是字母序；
  ② 「已修复」不参与提权 —— 一个修干净的目标不该排在带病的上面；
  ③ 截断时必须说出来，否则用户会当成"总共就这些"。
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

function makeList(items: Asset[], patch: Partial<AssetList> = {}): AssetList {
  const openFindingCount = items.reduce((sum, a) => sum + a.openFindingCount, 0)
  return {
    items,
    total: items.length,
    truncated: false,
    summary: {
      targetCount: items.length,
      findingCount: items.reduce((sum, a) => sum + a.findingCount, 0),
      openFindingCount,
      targetsWithOpenHigh: items.filter((a) => a.openHigh > 0 || a.openCritical > 0).length,
      neverScanned: items.filter((a) => a.scanCount === 0).length,
      neverCompleted: items.filter((a) => a.scanCount > 0 && a.lastCompletedAt === null).length,
      generatedAt: '2026-10-20T00:00:00Z',
    },
    ...patch,
  }
}

let response: AssetList | null = null
let failWith: unknown = null

beforeEach(() => {
  resetAssets()
  response = null
  failWith = null
  vi.stubGlobal('fetch', () => {
    if (failWith) return Promise.reject(failWith)
    return Promise.resolve(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  resetAssets()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <AssetsPage />
    </MemoryRouter>,
  )
}

/** 表格里所有数据行的目标名，按渲染顺序。 */
function rowTargets(): string[] {
  const table = screen.getByRole('table')
  const rows = within(table).getAllByRole('row').slice(1) // 去掉表头
  return rows.map((row) => within(row).getAllByRole('cell')[0].textContent ?? '')
}

describe('AssetsPage', () => {
  it('把「从未跑完」排在「正常」前面 —— 而不是字母序', async () => {
    response = makeList([
      makeAsset('a-normal.example'),
      makeAsset('z-never-completed.example', {
        lastScanStatus: 'failed',
        lastCompletedAt: null,
        consecutiveFailures: 3,
      }),
    ])
    renderPage()

    await screen.findByRole('table')

    // ★ 字母序会把 a-normal 放前面；风险序必须反过来
    expect(rowTargets()[0]).toContain('z-never-completed')
  })

  it('带开着的高危排最前，即使它刚扫完 && 最近有结论', async () => {
    response = makeList([
      makeAsset('a-failing.example', { lastCompletedAt: null, consecutiveFailures: 4 }),
      makeAsset('z-bleeding.example', { openHigh: 2, openFindingCount: 2, findingCount: 2 }),
    ])
    renderPage()

    await screen.findByRole('table')

    // ★ 规则：已经"在出血"的优先于"不知道情况"的
    expect(rowTargets()[0]).toContain('z-bleeding')
  })

  it('「已修复」不提权：修干净的排正常档，不该排到从未跑完之前', async () => {
    response = makeList([
      // 曾经是高危，但全修了；最近扫完、结论新鲜
      makeAsset('z-fixed.example', {
        findingCount: 3,
        openFindingCount: 0,
        openHigh: 0,
        openCritical: 0,
      }),
      // 扫过但从未跑完 —— 「看起来干净」其实没检查过
      makeAsset('a-unchecked.example', { lastCompletedAt: null, lastScanStatus: 'failed' }),
    ])
    renderPage()

    await screen.findByRole('table')

    // ★ 这是这一页存在的核心理由。
    //   ⚠️ 两个目标的名字故意倒过来取：排序在前面的判据已经把它排好了，
    //      字母序恰好相反，所以"按字母序"和"按风险序"给出的是不同答案。
    //      早先版本把字母序恰好排对了，导致「按 openFindingCount 提权」
    //      这种改坏也能跑绿 —— 那条断言其实没在测它声称的规则。
    expect(rowTargets()[0]).toContain('a-unchecked')
  })

  it('状态列把「从未跑完」和「正常」显示成不同文案（不能都是绿点）', async () => {
    response = makeList([
      makeAsset('ok.example'),
      makeAsset('never-done.example', { lastCompletedAt: null, lastScanStatus: 'failed' }),
    ])
    renderPage()

    const table = await screen.findByRole('table')
    expect(within(table).getByText('从未跑完')).toBeTruthy()
    expect(within(table).getByText('正常')).toBeTruthy()
  })

  it('truncated 时必须明说「只显示了前 N 个」', async () => {
    response = makeList([makeAsset('a.example')], { truncated: true, total: 900 })
    renderPage()

    // ★ 不说的话用户会当成"总共就这些"
    await screen.findByRole('status')
    const notice = screen.getByRole('status')
    expect(notice.textContent).toContain('900')
    expect(notice.textContent).toContain('截断')
  })

  it('搜索框按目标子串过滤', async () => {
    response = makeList([
      makeAsset('api.example.com'),
      makeAsset('db.example.com'),
      makeAsset('other.test'),
    ])
    renderPage()
    await screen.findByRole('table')

    // 项目里没装 @testing-library/user-event，用 fireEvent 直接派发 input 事件
    fireEvent.change(screen.getByRole('searchbox', { name: /按目标筛选资产/ }), {
      target: { value: 'example.com' },
    })

    await waitFor(() => {
      expect(rowTargets()).toHaveLength(2)
    })
    expect(rowTargets().join(' ')).not.toContain('other.test')
  })

  it('健康度筛选只留下那一档', async () => {
    response = makeList([
      makeAsset('ok.example'),
      makeAsset('never.example', { scanCount: 0, lastScanAt: null, lastCompletedAt: null }),
    ])
    renderPage()
    await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: '从未扫描' }))

    await waitFor(() => {
      expect(rowTargets()).toHaveLength(1)
    })
    expect(rowTargets()[0]).toContain('never.example')
  })

  it('空库时给的是"还没有任何目标"，不装一个空表格', async () => {
    response = makeList([])
    renderPage()

    expect(await screen.findByText('还没有任何目标')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('加载失败时给 ErrorState 与可用的重试，不是一片白', async () => {
    failWith = new TypeError('Failed to fetch')
    renderPage()

    expect(await screen.findByText(/资产列表加载失败/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /重试|重新加载/ })).toBeTruthy()
  })
})
