/**
 * 应急响应页的测试（v0.43）。
 *
 * ## 这个文件要守什么
 *
 * 页面的价值不在"能不能渲染出东西"，而在**几条判据**上。它们每一条
 * 都是这次改动里做过的决定，而且每一条都有一种"看起来正常、其实错了"
 * 的失败方式：
 *
 * 1. **只显示 `ir-*` 分类的题**。判错的表现不是报错，是把 CTF 题混进
 *    复盘列表 —— 而列表里躺着一道 pwn 题时，用户会以为这个页面坏了。
 * 2. **`unsolved` 对复盘任务有两种意思**。跑完的复盘状态也回到
 *    `unsolved`，直接套 CTF 的状态词会让**成功的复盘显示成「未开始」**。
 * 3. **建任务与开跑是两步**。中间那一步是留给用户核对证据包的；合并成
 *    一步的表现是"传错包也立刻烧掉一轮额度"。
 * 4. **`notice` 必须显示**。附件批次过期时后端照建不误，不说话就等于
 *    静默丢掉了用户的输入。
 *
 * ## 为什么断言打的是"看得见的字"，不是 class 名
 *
 * 与 `OverviewPage.test.tsx` 同一纪律：不 mock 子组件、断言用户能读到
 * 的东西。class 名重构一次就全红，而那些断言守不住任何真实契约。
 *
 * ⚠️ `vitest.config.ts` 关掉了 globals，`@testing-library/react` 的
 *    自动 cleanup **不会注册** —— 必须有 `afterEach(cleanup)`，
 *    否则同一文件里第二个用例会看到第一个用例留下的 DOM。
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import IrPage from '@/pages/IrPage'

// ── 被 mock 的外部依赖 ──────────────────────────────────
//
// ★ 只 mock"会发网络请求"的那几个，**不 mock 子组件**。
//   Dialog 与 ErrorState 按真实实现跑 —— 它们的文案本身就是要守的契约。
vi.mock('@/data/ctf2', async () => {
  const actual = await vi.importActual<typeof import('@/data/ctf2')>('@/data/ctf2')
  return {
    ...actual,
    useCtf2: vi.fn(),
    loadCtf2: vi.fn(),
    createIrTask: vi.fn(),
    solveChallenge: vi.fn(),
  }
})

vi.mock('@/data/scans-store', () => ({
  stageScanAttachments: vi.fn(),
}))

vi.mock('@/components/ui/Toast', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}))

import { createIrTask, loadCtf2, solveChallenge, useCtf2 } from '@/data/ctf2'
import { stageScanAttachments } from '@/data/scans-store'

const mockedUseCtf2 = vi.mocked(useCtf2)
const mockedLoadCtf2 = vi.mocked(loadCtf2)
const mockedCreateIrTask = vi.mocked(createIrTask)
const mockedSolveChallenge = vi.mocked(solveChallenge)
const mockedStage = vi.mocked(stageScanAttachments)

type Challenge = ReturnType<typeof useCtf2>['challenges'][number]

/** 造一道题。默认是一次 IR 复盘，字段形状与后端 `Ctf2ChallengeOut` 一致。 */
function makeChallenge(overrides: Partial<Challenge> = {}): Challenge {
  return {
    id: 'ctf2chal_test01',
    competitionId: 'ctf2comp_local',
    remoteId: 'local-abc',
    stageId: null,
    title: 'web-01 入侵事件',
    category: 'ir-root-cause',
    description: '想知道他是怎么进来的',
    attachments: [{ name: 'auth.log' }, { name: 'access.log' }],
    score: null,
    status: 'unsolved',
    candidateFlag: null,
    confirmedFlag: null,
    remainingWrongSubmissions: 0,
    wrongSubmissionCount: 0,
    maxWrongSubmissions: 0,
    candidateCount: 0,
    sandboxProfile: 'ctf2-sandbox-ir',
    scanId: null,
    solvedAt: null,
    lastError: null,
    environment: null,
    priority: 0,
    createdAt: '2026-10-21T10:00:00Z',
    updatedAt: '2026-10-21T10:00:00Z',
    ...overrides,
  } as Challenge
}

/** 把 store 的返回值摆成指定形状。 */
function setStore(overrides: Partial<ReturnType<typeof useCtf2>> = {}) {
  mockedUseCtf2.mockReturnValue({
    account: null,
    challenges: [],
    settings: null,
    queue: null,
    status: 'ready',
    error: null,
    notice: null,
    settingsError: null,
    ...overrides,
  } as ReturnType<typeof useCtf2>)
}

function renderPage() {
  // ⚠️ 必须包 MemoryRouter：页面里有 <Link>，裸渲染会报
  //    `Cannot destructure property 'basename' of useContext(...)`。
  return render(
    <MemoryRouter>
      <IrPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedLoadCtf2.mockResolvedValue(undefined as never)
  setStore()
})

afterEach(cleanup)

describe('IrPage：只列应急响应任务', () => {
  it('三个 ir-* 分类都显示出来', () => {
    setStore({
      challenges: [
        makeChallenge({ id: 'a', category: 'ir-root-cause', title: '根因复盘' }),
        makeChallenge({ id: 'b', category: 'ir-timeline', title: '时间线复盘' }),
        makeChallenge({ id: 'c', category: 'ir-scope', title: '范围复盘' }),
      ],
    })

    renderPage()

    expect(screen.getByText('根因复盘')).toBeTruthy()
    expect(screen.getByText('时间线复盘')).toBeTruthy()
    expect(screen.getByText('范围复盘')).toBeTruthy()
  })

  it('★ CTF 题不显示 —— 混一道 pwn 进来会让这个页面看起来坏了', () => {
    setStore({
      challenges: [
        makeChallenge({ id: 'a', category: 'ir-root-cause', title: '这是一次复盘' }),
        makeChallenge({ id: 'b', category: 'pwn', title: '这是一道 pwn 题' }),
        makeChallenge({ id: 'c', category: 'agentsec-prompt-injection', title: '这是一次评测' }),
      ],
    })

    renderPage()

    expect(screen.getByText('这是一次复盘')).toBeTruthy()
    expect(screen.queryByText('这是一道 pwn 题')).toBeNull()
    expect(screen.queryByText('这是一次评测')).toBeNull()
  })

  it('分类大小写与空白不影响判定', () => {
    setStore({
      challenges: [
        makeChallenge({ id: 'a', category: '  IR-Timeline  ', title: '大写带空白' }),
      ],
    })

    renderPage()

    expect(screen.getByText('大写带空白')).toBeTruthy()
  })

  it('只有 ir 前缀不算 —— 裸 `ir` 与 `irx-` 都排除', () => {
    setStore({
      challenges: [
        makeChallenge({ id: 'a', category: 'ir', title: '裸 ir' }),
        makeChallenge({ id: 'b', category: 'irx-foo', title: 'irx 前缀' }),
      ],
    })

    renderPage()

    expect(screen.queryByText('裸 ir')).toBeNull()
    expect(screen.queryByText('irx 前缀')).toBeNull()
  })
})

describe('IrPage：状态措辞', () => {
  it('★ 跑过的复盘不显示「未开始」—— 状态也是 unsolved，但产物证明它跑过', () => {
    setStore({
      challenges: [
        makeChallenge({
          id: 'a',
          title: '跑过一次的复盘',
          status: 'unsolved',
          createdAt: '2026-10-21T10:00:00Z',
          // updatedAt != createdAt 是"跑过"的乐观判据
          updatedAt: '2026-10-21T10:30:00Z',
        }),
      ],
    })

    renderPage()

    expect(screen.getByText(/已复盘（可重跑）/)).toBeTruthy()
    expect(screen.queryByText(/待复盘/)).toBeNull()
  })

  it('没跑过的显示「待复盘」', () => {
    setStore({ challenges: [makeChallenge({ status: 'unsolved' })] })

    renderPage()

    expect(screen.getByText(/待复盘/)).toBeTruthy()
  })

  it('正在跑显示「复盘中」', () => {
    setStore({ challenges: [makeChallenge({ status: 'solving' })] })

    renderPage()

    expect(screen.getByText(/复盘中/)).toBeTruthy()
  })

  it('failed 显示「上一次复盘没跑完」—— 不说「失败」，因为它没有 flag 可失', () => {
    setStore({ challenges: [makeChallenge({ status: 'failed' })] })

    renderPage()

    expect(screen.getByText(/上一次复盘没跑完/)).toBeTruthy()
  })

  it('显示证据包份数', () => {
    setStore({ challenges: [makeChallenge({ attachments: [{ name: 'a' }, { name: 'b' }] })] })

    renderPage()

    expect(screen.getByText(/证据包 2 份/)).toBeTruthy()
  })
})

describe('IrPage：建任务', () => {
  it('空标题时不发请求，而是说清楚要先做什么', async () => {
    renderPage()

    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => {
      expect(screen.getByText(/先给这次复盘起个名字/)).toBeTruthy()
    })
    expect(mockedCreateIrTask).not.toHaveBeenCalled()
  })

  it('有标题 + 有文件：先暂存再建任务，顺序不能反', async () => {
    mockedStage.mockResolvedValue({
      stagingId: 'stage_abc',
      files: [{ name: 'auth.log', sizeBytes: 100 }],
    })
    mockedCreateIrTask.mockResolvedValue({
      challenge: makeChallenge(),
      attachments: [{ name: 'auth.log', sizeBytes: 100 }],
      notice: null,
    })

    const { container } = renderPage()

    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '我的复盘' },
    })

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'auth.log', { type: 'text/plain' })
    fireEvent.change(fileInput, { target: { files: [file] } })

    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => expect(mockedCreateIrTask).toHaveBeenCalled())

    // ★ 暂存必须先发生，且 stagingId 被带进建任务请求
    expect(mockedStage).toHaveBeenCalledWith([file])
    expect(mockedCreateIrTask).toHaveBeenCalledWith({
      title: '我的复盘',
      brief: '',
      attachmentStagingId: 'stage_abc',
    })
    // 暂存调用的顺序在建任务之前
    expect(mockedStage.mock.invocationCallOrder[0]).toBeLessThan(
      mockedCreateIrTask.mock.invocationCallOrder[0],
    )
  })

  it('★ 没选文件时不调暂存 —— 传一个空 FormData 上去是没意义的往返', async () => {
    mockedCreateIrTask.mockResolvedValue({
      challenge: makeChallenge(),
      attachments: [],
      notice: null,
    })

    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '没带证据的复盘' },
    })
    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => expect(mockedCreateIrTask).toHaveBeenCalled())
    expect(mockedStage).not.toHaveBeenCalled()
    expect(mockedCreateIrTask).toHaveBeenCalledWith({
      title: '没带证据的复盘',
      brief: '',
      attachmentStagingId: null,
    })
  })

  it('★ 后端给的 notice 必须显示（附件批次过期时它照建不误但会说话）', async () => {
    mockedCreateIrTask.mockResolvedValue({
      challenge: makeChallenge(),
      attachments: [],
      notice: '附件暂存批次不存在或已过期（暂存保留 2 小时），本次任务没有证据包。',
    })

    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '过期场景' },
    })
    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => {
      expect(screen.getByText(/附件暂存批次不存在或已过期/)).toBeTruthy()
    })
  })

  it('没有证据包时要说明"只能给方法建议，不能给结论"', async () => {
    mockedCreateIrTask.mockResolvedValue({
      challenge: makeChallenge(),
      attachments: [],
      notice: null,
    })

    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '空包' },
    })
    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => {
      expect(screen.getByText(/没有证据的复盘只能给出方法建议，不能给出结论/)).toBeTruthy()
    })
  })

  it('建成功后列出认领到的文件名', async () => {
    mockedCreateIrTask.mockResolvedValue({
      challenge: makeChallenge(),
      attachments: [
        { name: 'auth.log', sizeBytes: 100 },
        { name: 'iptables.txt', sizeBytes: 200 },
      ],
      notice: null,
    })

    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '带证据' },
    })
    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => {
      expect(screen.getByText(/证据包 2 份：auth\.log、iptables\.txt/)).toBeTruthy()
    })
  })

  it('建任务失败时显示后端原话', async () => {
    mockedCreateIrTask.mockRejectedValue(new Error('还没有配置 LLM provider'))

    renderPage()
    fireEvent.change(screen.getByPlaceholderText(/web-01 入侵事件/), {
      target: { value: '会失败' },
    })
    fireEvent.click(screen.getByRole('button', { name: '建复盘任务' }))

    await waitFor(() => {
      expect(screen.getByText(/还没有配置 LLM provider/)).toBeTruthy()
    })
  })
})

describe('IrPage：开跑与产物', () => {
  it('★ 建完之后按钮是「开始复盘」而不是自动开跑', () => {
    setStore({ challenges: [makeChallenge({ status: 'unsolved' })] })

    renderPage()

    expect(screen.getByRole('button', { name: '开始复盘' })).toBeTruthy()
  })

  it('跑过的显示「重新复盘」', () => {
    setStore({
      challenges: [
        makeChallenge({
          status: 'unsolved',
          createdAt: '2026-10-21T10:00:00Z',
          updatedAt: '2026-10-21T10:30:00Z',
        }),
      ],
    })

    renderPage()

    expect(screen.getByRole('button', { name: '重新复盘' })).toBeTruthy()
  })

  it('点「开始复盘」调 solveChallenge', async () => {
    mockedSolveChallenge.mockResolvedValue({} as never)
    setStore({ challenges: [makeChallenge({ id: 'chal_1' })] })

    renderPage()
    fireEvent.click(screen.getByRole('button', { name: '开始复盘' }))

    await waitFor(() => expect(mockedSolveChallenge).toHaveBeenCalledWith('chal_1'))
  })

  it('正在跑的条目按钮禁用', () => {
    setStore({ challenges: [makeChallenge({ status: 'solving' })] })

    renderPage()

    const button = screen.getByRole('button', { name: /启动|重新复盘|开始复盘/ }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('ir-* 分类都有「处置工单」入口', () => {
    setStore({ challenges: [makeChallenge({ category: 'ir-timeline' })] })

    renderPage()

    expect(screen.getByRole('button', { name: /处置工单/ })).toBeTruthy()
  })

  it('★ 有题解才有「复盘报告」入口 —— canHaveWriteup 判的是 scanId', () => {
    setStore({ challenges: [makeChallenge({ scanId: null })] })

    renderPage()

    expect(screen.queryByRole('button', { name: /复盘报告/ })).toBeNull()
  })

  it('有 scanId 时露出「复盘报告」', () => {
    setStore({ challenges: [makeChallenge({ scanId: 'scan_abc' })] })

    renderPage()

    expect(screen.getByRole('button', { name: /复盘报告/ })).toBeTruthy()
  })

  it('lastError 显示出来 —— 上一次为什么没跑完是用户最需要的信息', () => {
    setStore({
      challenges: [makeChallenge({ status: 'failed', lastError: '回合用尽（80 轮）仍未给出结论' })],
    })

    renderPage()

    expect(screen.getByText(/回合用尽（80 轮）仍未给出结论/)).toBeTruthy()
  })
})

describe('IrPage：空态与错误', () => {
  it('★ 空态要说清「复盘与扫描是两件事」', () => {
    setStore({ challenges: [] })

    renderPage()

    expect(screen.getByText('还没有复盘任务')).toBeTruthy()
    expect(screen.getByText(/扫描回答「这个目标有没有漏洞」/)).toBeTruthy()
    expect(screen.getByText(/复盘回答「已经发生了什么」/)).toBeTruthy()
  })

  it('加载中不显示空态（否则用户会以为真的没有）', () => {
    setStore({ status: 'loading', challenges: [] })

    renderPage()

    expect(screen.queryByText('还没有复盘任务')).toBeNull()
  })

  it('加载失败给 role=alert 的提示与重试', () => {
    setStore({ status: 'error', error: new Error('网络断了'), challenges: [] })

    renderPage()

    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByText(/复盘任务列表加载失败/)).toBeTruthy()
  })

  it('★ 共享 store 已是 ready 时不重复拉 —— 从 CTF2 页切过来不该再打一次', () => {
    setStore({ status: 'ready', challenges: [] })

    renderPage()

    expect(mockedLoadCtf2).not.toHaveBeenCalled()
  })

  it('store 还 idle 时拉一次，且只拉一次（重渲染不重复发）', () => {
    setStore({ status: 'idle', challenges: [] })

    const { rerender } = renderPage()
    rerender(
      <MemoryRouter>
        <IrPage />
      </MemoryRouter>,
    )

    expect(mockedLoadCtf2).toHaveBeenCalledTimes(1)
  })
})
