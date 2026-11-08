import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChallengeBoard } from './ChallengeBoard'
import type { KernelChallenge } from '@/data/kernel'

/*
  ══════════════════════════════════════════════════════════════
  ChallengeBoard：一行一道题
  ══════════════════════════════════════════════════════════════

  ## 为什么换掉卡片并测它

  比赛现场第一件事是「扫一遍：哪些还没解、哪些快出来了」。卡片网格
  一屏只放得下 6–9 道，而真正要扫的两列（状态、分值）被埋在中间。
  看板把这两列拉到同一行，代价是**次要操作要展开才看得见** ——
  所以下面同时钉住两件事：

    · 行内只留主操作（开始 / 确认 / 取消），一屏能扫；
    · 展开行里的入口**判据不变**（题解看 `scanId`、工单看 `category`），
      换版面不该改规则。

  ## ⚠️ 三个必须自己处理的坑（与 ErrorState.test.tsx 记录的一致）

  1. **必须手动 `cleanup`** —— 本项目没开 vitest `globals`，
     testing-library 的自动清理不会注册。不清理会让 DOM 在用例间累积，
     第二个用例开始 `getByRole` 就报 "Found multiple elements"。
  2. **不能用 `toHaveTextContent`** —— 那来自 `jest-dom`，本项目没装。
     一律用原生 `textContent` + `toContain`。
  3. `Link` 需要 Router 上下文，所以用 `MemoryRouter` 包一层。
*/
afterEach(cleanup)

/** 造一道够渲染的题。只写这个用例关心与组件必读的字段 */
function makeChallenge(over: Partial<KernelChallenge> = {}): KernelChallenge {
  return {
    id: 'chal_1',
    competitionId: 'comp_1',
    remoteId: 'r-1',
    stageId: null,
    title: '示例题',
    category: 'web',
    description: null,
    attachments: [],
    score: 100,
    status: 'unsolved',
    candidateFlag: null,
    confirmedFlag: null,
    remainingWrongSubmissions: 3,
    wrongSubmissionCount: 0,
    maxWrongSubmissions: 3,
    candidateCount: 0,
    sandboxProfile: 'suvex-sandbox',
    scanId: null,
    solvedAt: null,
    lastError: null,
    environment: null,
    priority: 0,
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    canonicalCategory: 'web',
    platformSlug: 'ctf2',
    platformName: 'CTF2（内置）',
    ...over,
  }
}

const noop = () => {}

function renderBoard(
  challenges: KernelChallenge[],
  handlers: {
    onSolve?: (c: KernelChallenge, resume?: boolean) => void
    onSubmit?: (c: KernelChallenge) => void
    onCancel?: (c: KernelChallenge) => void
    onWriteup?: (c: KernelChallenge) => void
    onWorkOrder?: (c: KernelChallenge) => void
    onOpen?: (c: KernelChallenge) => void
    onBatch?: (action: 'solve' | 'cancel', ids: string[]) => void
    queue?: { running: number; limit: number; queued: number; challenges: string[] }
  } = {},
) {
  return render(
    <MemoryRouter>
      <ChallengeBoard
        challenges={challenges}
        onSolve={handlers.onSolve ?? noop}
        onSubmit={handlers.onSubmit ?? noop}
        onCancel={handlers.onCancel ?? noop}
        onRelease={noop}
        onSetPriority={noop}
        onWriteup={handlers.onWriteup ?? noop}
        onWorkOrder={handlers.onWorkOrder}
        onOpen={handlers.onOpen}
        onBatch={handlers.onBatch}
        queue={handlers.queue}
      />
    </MemoryRouter>,
  )
}

/**
 * 展开一道题（详情渲染在下一行，用 `screen` 查即可）。
 *
 * ⚠️ 用 `fireEvent.click` 而不是原生 `.click()`：后者不在 `act()` 里，
 *    React 19 的状态更新要等一个微任务才落地，紧接着的断言会看到
 *    还没展开的 DOM。`fireEvent` 会把这次点击连同更新一起冲干净。
 */
function expand(title: string) {
  fireEvent.click(screen.getByRole('button', { name: title }))
}

describe('ChallengeBoard：看板本身', () => {
  it('一行一道题（30 道题 = 30 行，不是 30 张卡）', () => {
    const challenges = Array.from({ length: 30 }, (_, i) =>
      makeChallenge({ id: `chal_${i}`, title: `题目 ${i}` }),
    )
    renderBoard(challenges)

    // 表头 1 行 + 30 行正文
    expect(screen.getAllByRole('row')).toHaveLength(31)
    expect(screen.getByRole('button', { name: '题目 29' })).not.toBeNull()
  })

  it('状态与分值直接可读（不用展开）', () => {
    renderBoard([makeChallenge({ status: 'solved', score: 500 })])

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    expect(row.textContent).toContain('已解出')
    expect(row.textContent).toContain('500')
  })

  it('附件没下载时行内就有警告（模型看不到题面文件）', () => {
    renderBoard([makeChallenge({ attachments: [{ name: 'a.zip' }], attachmentsReady: false })])

    expand('示例题')
    expect(screen.getByText(/还没下载到服务器/)).not.toBeNull()
  })
})

describe('ChallengeBoard：行内主操作', () => {
  it('未开始的题有「开始」，点了把这道题交给调用方', () => {
    const onSolve = vi.fn()
    const challenge = makeChallenge()
    renderBoard([challenge], { onSolve })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    within(row).getByRole('button', { name: '开始' }).click()

    expect(onSolve).toHaveBeenCalledWith(challenge, false)
  })

  it('★ 回合用尽的失败题显示「接着解」，带 resume=true', () => {
    const onSolve = vi.fn()
    const challenge = makeChallenge({
      status: 'failed',
      lastError: '回合用尽（40 轮）仍未给出结论。',
    })
    renderBoard([challenge], { onSolve })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    within(row).getByRole('button', { name: '接着解' }).click()

    expect(onSolve).toHaveBeenCalledWith(challenge, true)
  })

  it('「确认」只**打开弹窗**，不直接提交', () => {
    const onSubmit = vi.fn()
    const challenge = makeChallenge({
      status: 'candidate_ready',
      candidateFlag: 'flag{maybe}',
    })
    renderBoard([challenge], { onSubmit })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    within(row).getByRole('button', { name: '确认' }).click()

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith(challenge)
  })

  it('可以取消的题有「取消」', () => {
    const onCancel = vi.fn()
    const challenge = makeChallenge({ status: 'solving' })
    renderBoard([challenge], { onCancel })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    within(row).getByRole('button', { name: '取消' }).click()

    expect(onCancel).toHaveBeenCalledWith(challenge)
  })
})

describe('ChallengeBoard：「查看」打开这道题的对话', () => {
  it('点了把**这道题**交给调用方（由它去 find-or-create 会话并跳转）', () => {
    const onOpen = vi.fn()
    const challenge = makeChallenge({ status: 'solved', confirmedFlag: 'flag{x}' })
    renderBoard([challenge], { onOpen })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    within(row).getByRole('button', { name: '查看' }).click()

    expect(onOpen).toHaveBeenCalledWith(challenge)
  })

  it('★ 已解出的题也有 —— 回看过程正是解出之后最常做的事', () => {
    renderBoard([makeChallenge({ status: 'solved', confirmedFlag: 'flag{x}' })], { onOpen: noop })

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    expect(within(row).queryByRole('button', { name: '查看' })).not.toBeNull()
  })

  it('调用方不传 onOpen 时整项不渲染（按需能力）', () => {
    renderBoard([makeChallenge()])

    const row = screen.getByRole('button', { name: '示例题' }).closest('tr')!
    expect(within(row).queryByRole('button', { name: '查看' })).toBeNull()
  })
})

describe('ChallengeBoard：批量开始 / 停止（第 3 步）', () => {
  it('★ 只把**能开始的**那些交给批量开始（已解出的不计入）', () => {
    const onBatch = vi.fn()
    renderBoard(
      [
        makeChallenge({ id: 'a', title: '甲' }),
        makeChallenge({ id: 'b', title: '乙' }),
        makeChallenge({ id: 'c', title: '丙', status: 'solved', confirmedFlag: 'flag{x}' }),
      ],
      { onBatch },
    )

    fireEvent.click(screen.getByRole('checkbox', { name: '选择当前列表里的全部题目' }))
    // 3 道都勾上了，但只有 2 道可以开始
    expect(screen.getByText(/已选 3 道/)).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /开始所选/ }))

    expect(onBatch).toHaveBeenCalledWith('solve', ['a', 'b'])
  })

  it('★ 只把**在跑的**交给批量停止', () => {
    const onBatch = vi.fn()
    renderBoard(
      [
        makeChallenge({ id: 'a', title: '甲', status: 'solving' }),
        makeChallenge({ id: 'b', title: '乙', status: 'unsolved' }),
      ],
      { onBatch },
    )

    fireEvent.click(screen.getByRole('checkbox', { name: '选择当前列表里的全部题目' }))
    fireEvent.click(screen.getByRole('button', { name: /停止所选/ }))

    expect(onBatch).toHaveBeenCalledWith('cancel', ['a'])
  })

  it('★ 操作完清空勾选 —— 否则下一次点击会带上上一批的题', () => {
    const onBatch = vi.fn()
    renderBoard([makeChallenge({ id: 'a', title: '甲' })], { onBatch })

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 甲' }))
    fireEvent.click(screen.getByRole('button', { name: /开始所选/ }))

    expect(screen.queryByText(/已选/)).toBeNull()
  })

  it('全选按钮再点一次取消全选', () => {
    renderBoard(
      [makeChallenge({ id: 'a', title: '甲' }), makeChallenge({ id: 'b', title: '乙' })],
      { onBatch: noop },
    )

    const all = screen.getByRole('checkbox', { name: '选择当前列表里的全部题目' })
    fireEvent.click(all)
    expect(screen.getByText(/已选 2 道/)).not.toBeNull()
    fireEvent.click(all)
    expect(screen.queryByText(/已选/)).toBeNull()
  })

  it('并发进度常驻：在跑 / 上限 / 排队', () => {
    renderBoard([makeChallenge()], {
      queue: { running: 3, limit: 8, queued: 5, challenges: [] },
    })

    expect(screen.getByText('3/8')).not.toBeNull()
    expect(screen.getByText('排队 5')).not.toBeNull()
  })

  it('调用方不传 onBatch 时没有勾选框与批量栏（按需能力）', () => {
    renderBoard([makeChallenge({ title: '甲' })])

    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByText(/已选/)).toBeNull()
  })
})

describe('ChallengeBoard：展开行的入口判据（与旧卡片一致）', () => {
  it('★★ 未解出但跑过一轮的题**有**「题解」', () => {
    renderBoard([makeChallenge({ status: 'unsolved', scanId: 'scan_1' })])

    expand('示例题')
    expect(screen.queryByRole('button', { name: '题解' })).not.toBeNull()
  })

  it('★★ 从没开始过的题**没有**「题解」（点了必然 409）', () => {
    renderBoard([makeChallenge({ scanId: null })])

    expand('示例题')
    expect(screen.queryByRole('button', { name: '题解' })).toBeNull()
  })

  it('★★ 跑过的 pwn 题题解在、处置工单不在（判据不同）', () => {
    renderBoard([makeChallenge({ category: 'pwn', status: 'solved', scanId: 'scan_1' })])

    expand('示例题')
    expect(screen.queryByRole('button', { name: '题解' })).not.toBeNull()
    expect(screen.queryByRole('button', { name: '处置工单' })).toBeNull()
  })

  it('★ 应急响应题有「处置工单」—— 没跑完也有（判据是分类）', () => {
    // 传回调：这组用例验的是**按钮可见性**，不是"按需能力"那条约定
    renderBoard([makeChallenge({ category: 'ir-root-cause', status: 'unsolved' })], {
      onWorkOrder: noop,
    })

    expand('示例题')
    expect(screen.queryByRole('button', { name: '处置工单' })).not.toBeNull()
  })

  it('★ 调用方不传 onWorkOrder 时整项不渲染（按需能力）', () => {
    // renderBoard 默认只给 onWriteup，正好是"没传 onWorkOrder"的形状
    renderBoard([makeChallenge({ category: 'ir-root-cause', status: 'solved', scanId: 's' })])

    expand('示例题')
    expect(screen.queryByRole('button', { name: '处置工单' })).toBeNull()
  })

  it('排队中的题才有「提前 / 延后」', () => {
    renderBoard([makeChallenge({ status: 'solving' })])
    expand('示例题')
    expect(screen.queryByRole('button', { name: '提前' })).not.toBeNull()

    cleanup()
    renderBoard([makeChallenge({ status: 'solved', confirmedFlag: 'flag{x}' })])
    expand('示例题')
    expect(screen.queryByRole('button', { name: '提前' })).toBeNull()
  })
})
