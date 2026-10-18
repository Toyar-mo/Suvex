/**
 * `OverviewPage` 的渲染 smoke test。
 *
 * 为什么补这一层：`src/pages/` 此前**零测试文件**。组件级测试已经覆盖了
 * Button / ErrorState / ToggleChip / ChallengeCard 等叶子组件，但它们
 * 保证不了"页面把 store 接错了、三态写反了"。渲染期崩掉的那类问题
 * （取 undefined 上的字段、条件分支打架）只有真挂载一次才暴露，
 * 而在此之前只能靠人工点开页面。
 *
 * 这里**不 mock 子组件**：`StatCard` / `ActivityFeed` / `SeverityChart`
 * 都按真实实现跑，所以它们自身炸了这里也会红。代价是要喂一份形状
 * 完整的 `OverviewData`（见 `makeData`），收益是这层测的确实是页面。
 *
 * ⚠️ 三态的顺序是本页的**核心契约**，用例 1 专门钉它：
 *    `status === 'loading'` 必须先于空状态判定，否则新用户首屏会看到
 *    "还没有扫描记录"——而真相是数据还在路上。源码里那条注释说的
 *    "首屏闪一下假空状态是最刺眼的"就是这个。
 *
 * ⚠️ `vitest.config.ts` 关掉了 `globals`，`@testing-library/react` 的
 *    自动 cleanup **不会注册**，所以每个文件都得手动 `cleanup()`。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFleetStatus } from '@/data/fleet'
import type { OverviewData } from '@/data/overview'
import { loadOverview, useOverviewState } from '@/data/overview-store'

import OverviewPage from './OverviewPage'

vi.mock('@/data/fleet', () => ({ useFleetStatus: vi.fn() }))
vi.mock('@/data/overview-store', () => ({
  useOverviewState: vi.fn(),
  loadOverview: vi.fn(),
}))

const mockedUseFleetStatus = vi.mocked(useFleetStatus)
const mockedUseOverviewState = vi.mocked(useOverviewState)
const mockedLoadOverview = vi.mocked(loadOverview)

/** 形状完整的一份总览数据 —— 字段缺一个就会在渲染期炸。 */
function makeData(hasAnyScan: boolean): OverviewData {
  return {
    metrics: {
      activeScans: 2,
      newHighSeverityThisWeek: 3,
      newHighSeverityDelta: 1,
      verifiedPocCount: 5,
      avgScanDurationSec: 90,
      falsePositiveCount: 1,
      ctf2LlmCalls: 10,
      ctf2RecordedCalls: 8,
      ctf2TotalTokens: 12345,
      ctf2UsageSince: '2026-10-01T00:00:00Z',
    },
    activity: [],
    severityBreakdown: [],
    hasAnyScan,
  }
}

function setSnapshot(snapshot: {
  data: OverviewData | null
  status: 'loading' | 'ready' | 'error'
  error: null
}) {
  // `error` 恒为 null：这三个用例都不走"带 ApiError 的失败"那条更细的分支，
  // 真 ApiError 的区分能力由 ErrorState 自己的测试负责。
  mockedUseOverviewState.mockReturnValue(
    snapshot as unknown as ReturnType<typeof useOverviewState>,
  )
}

/**
 * 页面里的 `PageHeader`（面包屑/导航）和 `NoScansYet` 都会渲染 `<Link>`，
 * 而 `react-router` 的 `<Link>` 从 context 里取 `basename` —— 没有 Router
 * 包一层就报 `Cannot destructure property 'basename' of 'useContext(...)' as it is null`。
 */
function renderPage() {
  return render(
    <MemoryRouter>
      <OverviewPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseFleetStatus.mockReturnValue({
    agents: { running: 0, queued: 0 },
  } as unknown as ReturnType<typeof useFleetStatus>)
})

afterEach(cleanup)

describe('OverviewPage 三态', () => {
  it('loading 时不显示"还没有扫描记录"——空状态不能抢在数据前面', () => {
    setSnapshot({ data: null, status: 'loading', error: null })

    renderPage()

    // 这一条就是本页的核心契约：loading 阶段绝不能出现假空态。
    expect(screen.queryByText('还没有扫描记录')).toBeNull()
  })

  it('失败时给出可读的错误块，而不是空白页', () => {
    setSnapshot({ data: null, status: 'error', error: null })

    renderPage()

    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByText('总览数据加载失败')).toBeTruthy()
    // 失败态同样不能把"还没有扫描记录"顶上来 —— 那是两回事。
    expect(screen.queryByText('还没有扫描记录')).toBeNull()
  })

  it('一次都没扫过时走空状态，而不是渲染一排 0 值卡片', () => {
    setSnapshot({ data: makeData(false), status: 'ready', error: null })

    renderPage()

    expect(screen.getByText('还没有扫描记录')).toBeTruthy()
    // 空状态整块替换，所以正文区的关键指标不该出现。
    expect(screen.queryByLabelText('关键指标')).toBeNull()
  })

  it('有数据时渲染关键指标区，并把 mock 的智能体数带进脚注', () => {
    mockedUseFleetStatus.mockReturnValue({
      agents: { running: 3, queued: 1 },
    } as unknown as ReturnType<typeof useFleetStatus>)
    setSnapshot({ data: makeData(true), status: 'ready', error: null })

    renderPage()

    expect(screen.getByLabelText('关键指标')).toBeTruthy()
    expect(screen.getByText('进行中的扫描')).toBeTruthy()
    // 脚注里拼的是 agents 的两个数 —— 顺便证明 useFleetStatus 被真的用上了。
    expect(screen.getByText(/1 个排队 · 3 个智能体在跑/)).toBeTruthy()
    expect(screen.queryByText('还没有扫描记录')).toBeNull()
  })

  it('挂载时拉一次总览（且只拉一次）', () => {
    setSnapshot({ data: makeData(true), status: 'ready', error: null })

    renderPage()

    expect(mockedLoadOverview).toHaveBeenCalledTimes(1)
  })
})
