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
      // v0.95：待处置口径。刻意给成与 `severityBreakdown` **不同**的值 ——
      // 两者若在夹具里恒等，任何把风险分错接成全量口径的 bug 都测不出来。
      unresolvedBreakdown: [
        { severity: 'critical', count: 1 },
        { severity: 'high', count: 2 },
        { severity: 'medium', count: 0 },
        { severity: 'low', count: 0 },
        { severity: 'info', count: 0 },
      ],
      unresolvedTotal: 3,
      riskScore: 20,
      riskLevel: 'high',
    },
    activity: [],
    severityBreakdown: [],
    hasAnyScan,
    attention: [],
    // v0.96：可用能力三格。刻意给三种 tone 各一条 ——
    // `muted` 与 `attention` 的视觉差别（"读不到"vs"待配置"）如果
    // 在夹具里混成同一种，两个分支谁接错了都测不出来。
    resources: [
      { kind: 'mcp_tool', label: '可用工具', count: 21, detail: '本机内置', tone: 'ok' },
      { kind: 'mcp_server', label: '外部 MCP', count: 0, detail: '还没配', tone: 'attention' },
      { kind: 'skill', label: '技能卡', count: 31, detail: '', tone: 'muted' },
    ],
  }
}

function setSnapshot(snapshot: {
  data: OverviewData | null
  status: 'loading' | 'ready' | 'error'
  error: null
  lastLoadedAt?: number | null
}) {
  // `error` 恒为 null：这三个用例都不走"带 ApiError 的失败"那条更细的分支，
  // 真 ApiError 的区分能力由 ErrorState 自己的测试负责。
  //
  // `lastLoadedAt` 默认给 `Date.now()`：绝大多数用例与新鲜度无关，
  // 而**缺省成 null 会让提示条永远不出现**——那么"提示条该出现时出现了"
  // 就永远不会被测到。需要测提示条的用例自己传一个旧时间戳。
  mockedUseOverviewState.mockReturnValue({
    lastLoadedAt: Date.now(),
    ...snapshot,
  } as unknown as ReturnType<typeof useOverviewState>)
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

/**
 * v0.95：风险分、该做什么、数据新鲜度。
 *
 * 这三块各自引一个"看起来在工作但什么都没说"的失效模式：
 *   · 风险分接错口径（用全量而非待处置）→ 修完漏洞不降分；
 *   · 该做什么永远为空（判据写错）→ 一块永远说"没事"的区块；
 *   · 静默刷新失败不留痕 → 屏幕上是几分钟前的数字，用户不知道。
 */
describe('OverviewPage · 待处置风险（v0.95）', () => {
  it('风险分卡显示后端给的分与等级，不自己重新分档', () => {
    setSnapshot({ data: makeData(true), status: 'ready', error: null })

    renderPage()

    // 夹具：riskScore 20 / level high / unresolvedTotal 3
    expect(screen.getByText('待处置风险')).toBeTruthy()
    expect(screen.getByText('3 项')).toBeTruthy()
    expect(screen.getByText('高')).toBeTruthy()
  })

  it('构成条列出非零档，不列 0 值档', () => {
    setSnapshot({ data: makeData(true), status: 'ready', error: null })

    renderPage()

    // 夹具里 critical 1 / high 2，medium 及以下都是 0
    expect(screen.getByText('严重')).toBeTruthy()
    expect(screen.getByText('高危')).toBeTruthy()
    expect(screen.queryByText('中危')).toBeNull()
  })

  it('该做什么：给建议时按后端顺序渲染，并带上数量', () => {
    const data = makeData(true)
    data.attention = [
      {
        kind: 'unresolved_critical',
        level: 'urgent',
        title: '修复 2 个待处置的严重漏洞',
        detail: '严重等级的漏洞应优先处置。',
        count: 2,
      },
      {
        kind: 'failing_schedule',
        level: 'warning',
        title: '检查 1 个连续失败的定时复查',
        detail: '',
        count: 1,
      },
    ]
    setSnapshot({ data, status: 'ready', error: null })

    renderPage()

    expect(screen.getByText('该做什么')).toBeTruthy()
    expect(screen.getByText('修复 2 个待处置的严重漏洞')).toBeTruthy()
    expect(screen.getByText('检查 1 个连续失败的定时复查')).toBeTruthy()
    // 等级文字是第三条信息通道（颜色之外的）—— 必须在
    expect(screen.getByText('紧急')).toBeTruthy()
    expect(screen.getByText('需留意')).toBeTruthy()
  })

  it('该做什么：空列表时说清"查过哪几项"，而不是只说"没有"', () => {
    setSnapshot({ data: makeData(true), status: 'ready', error: null })

    renderPage()

    // ★ 关键：不能只是"没有待办"——那与"这块坏了"长得一样
    expect(screen.getByText(/没有需要你处理的事/)).toBeTruthy()
    expect(screen.getByText(/连续失败的定时复查/)).toBeTruthy()
  })

  it('认不出的建议 kind 被忽略，而不是让整块崩掉', () => {
    const data = makeData(true)
    data.attention = [
      {
        kind: 'some_future_kind_the_backend_added',
        level: 'info',
        title: '这条旧前端不认识',
        detail: '',
        count: 1,
      },
      {
        kind: 'unresolved_critical',
        level: 'urgent',
        title: '修复 1 个待处置的严重漏洞',
        detail: '',
        count: 1,
      },
    ]
    setSnapshot({ data, status: 'ready', error: null })

    renderPage()

    expect(screen.queryByText('这条旧前端不认识')).toBeNull()
    // 同一个列表里认识的那条照常渲染 —— 少显示一条是可接受的降级，白屏不是
    expect(screen.getByText('修复 1 个待处置的严重漏洞')).toBeTruthy()
  })

  it('数据超过 5 分钟没更新时显式标出来', () => {
    setSnapshot({
      data: makeData(true),
      status: 'ready',
      error: null,
      lastLoadedAt: Date.now() - 7 * 60 * 1000,
    })

    renderPage()

    expect(screen.getByText(/这份数据是 7 分钟前取的/)).toBeTruthy()
    expect(screen.getByText(/数字可能已经过时/)).toBeTruthy()
  })

  it('数据新鲜时不显示任何提示（不给正常状态加噪音）', () => {
    setSnapshot({
      data: makeData(true),
      status: 'ready',
      error: null,
      lastLoadedAt: Date.now() - 10 * 1000,
    })

    renderPage()

    expect(screen.queryByText(/这份数据是/)).toBeNull()
    expect(screen.queryByText(/正在自动刷新/)).toBeNull()
  })

  it('时钟回拨（lastLoadedAt 在未来）时不显示荒谬的负数时间', () => {
    setSnapshot({
      data: makeData(true),
      status: 'ready',
      error: null,
      lastLoadedAt: Date.now() + 10 * 60 * 1000,
    })

    renderPage()

    // 块级断言而不是 `queryByText(/-/)`：页面别处有连字符，那种断言会永远通过
    expect(screen.queryByText(/分钟前/)).toBeNull()
  })
})
