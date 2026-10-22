import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChallengeCard } from './ChallengeCard'
import type { KernelChallenge } from '@/data/kernel'

/*
  ══════════════════════════════════════════════════════════════
  ChallengeCard 的「题解」入口（v0.36）
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  入口的**有无**本身就是产品决定，判据是 `scanId`（= 至少开始过一次
  解题），**不是** `status`。这个区别是被真实数据打出来的：

    `[ACTF2020 新生赛]Upload` 跑满 78 轮、未解出，收尾回到 `unsolved`
    —— 而它有题解。按状态判断会把这道题的入口藏掉（见
    `data/kernel.ts` 里 `canHaveWriteup` 的说明）。

  两条必须钉住的口径：

    · **未解出但跑过**的题**必须**有入口（真实回归，最容易错的一条）；
    · **从没开始过**的题**必须不**有 —— 点了必然 409，而"入口本身
      就是一句承诺"（与 ScanList 只给夺旗任务显示题解同一条）。

  这两条都是"少一个/多一个按钮"，不会让任何测试变红，也不会抛异常。

  ## ⚠️ 三个必须自己处理的坑（与 ErrorState.test.tsx 记录的一致）

  1. **必须手动 `cleanup`** —— 本项目没开 vitest `globals`，
     testing-library 的自动清理不会注册。不清理会让 DOM 在用例间累积，
     第二个用例开始 `getByRole` 就报 "Found multiple elements"。
  2. **不能用 `toHaveTextContent`** —— 那来自 `jest-dom`，本项目没装。
     一律用原生 `textContent` + `toContain`。
  3. `Link` 需要 Router 上下文，所以用 `MemoryRouter` 包一层。

  ⚠️ 这里**不**断言按钮的样式类名：那是易碎断言，改了 class 就会红，
     而它并不承载语义。
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
    sandboxProfile: 'ctf2-sandbox',
    scanId: null,
    solvedAt: null,
    lastError: null,
    environment: null,
    priority: 0,
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    ...over,
  }
}

const noop = () => {}

/**
 * 渲染一张卡片。
 *
 * ⚠️ `onWriteup` 默认**给一个**（`noop`）—— 否则"有按钮"那几条用例
 *    会因为"没传回调"而假失败，而那与它们要验的可见性规则无关。
 *    要验"不传就没有"请用下面的 `renderCardWithoutWriteup`。
 */
function renderCard(
  challenge: KernelChallenge,
  handlers: { onWriteup?: (c: KernelChallenge) => void } = { onWriteup: noop },
) {
  return render(
    <MemoryRouter>
      <ChallengeCard
        challenge={challenge}
        onSubmit={noop}
        onSolve={noop}
        onCancel={noop}
        onRelease={noop}
        onSetPriority={noop}
        onWriteup={handlers.onWriteup}
      />
    </MemoryRouter>,
  )
}

/** 不传 `onWriteup` 的版本（验"按需能力"那条约定） */
function renderCardWithoutWriteup(challenge: KernelChallenge) {
  return render(
    <MemoryRouter>
      <ChallengeCard
        challenge={challenge}
        onSubmit={noop}
        onSolve={noop}
        onCancel={noop}
        onRelease={noop}
        onSetPriority={noop}
      />
    </MemoryRouter>,
  )
}

/** 「题解」按钮存在吗。用可访问名精确定位，避免撞上别处的同名文字 */
function writeupButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: '题解' })
}

describe('ChallengeCard：题解入口的可见性', () => {
  it('★★ 真实回归：未解出但跑过一轮的题**有**「题解」按钮', () => {
    /*
      这条钉住的是 2026-10-15 用真实数据抓到的 bug：判据曾经是状态
      白名单，而这道真实题收尾状态是 `unsolved`、`solved=false`，
      却被判成"没有题解" —— 于是入口不显示，用户永远看不到那份题解。

      字段照真实 payload 抄（status/scanId/candidateFlag/confirmedFlag/
      lastError 全部来自 `/kernel/snapshot` 的实际响应）。
    */
    renderCard(
      makeChallenge({
        id: 'ctf2chal_01M4QRAT254DE91N89338Z7P4M',
        title: '[ACTF2020 新生赛]Upload',
        status: 'unsolved',
        scanId: 'scan_01M4RD51TJSMXS4ZQF97QK636N',
        candidateFlag: null,
        confirmedFlag: null,
        candidateCount: 0,
        lastError: '本次未产出候选 flag。',
      }),
    )

    expect(writeupButton()).not.toBeNull()
  })

  it('★ solved 的题有「题解」按钮', () => {
    renderCard(makeChallenge({ status: 'solved', scanId: 'scan_1', confirmedFlag: 'flag{x}' }))

    expect(writeupButton()).not.toBeNull()
  })

  it('★ failed 的题也有 —— 题解记的是"我们试过什么"，没解出同样有价值', () => {
    renderCard(
      makeChallenge({
        status: 'failed',
        scanId: 'scan_1',
        lastError: '回合用尽（40 轮）仍未给出结论。',
      }),
    )

    expect(writeupButton()).not.toBeNull()
  })

  it('★ candidate_ready 也有（safe 模式跑完之后就在这个状态）', () => {
    renderCard(
      makeChallenge({ status: 'candidate_ready', scanId: 'scan_1', candidateFlag: 'flag{guess}' }),
    )

    expect(writeupButton()).not.toBeNull()
  })

  it('★ 还在跑的题也有 —— `scanId` 已非空，后端那句话现在是准确的', () => {
    for (const status of ['solving', 'environment_ready', 'submitting'] as const) {
      cleanup()
      renderCard(makeChallenge({ status, scanId: 'scan_1' }))
      expect(writeupButton(), `${status} 应该有题解入口（scanId 非空）`).not.toBeNull()
    }
  })

  it('★★ 从没开始过的题**没有**这个按钮（点了必然 409）', () => {
    // 判据是 scanId 为空 —— 注意这里 status 也是 unsolved，
    // 但真正决定的是 scanId（上一组用例里 unsolved+scanId 是有按钮的）
    renderCard(makeChallenge({ status: 'unsolved', scanId: null }))

    expect(writeupButton()).toBeNull()
  })

  it('调用方不传 onWriteup 时整项不渲染（按需能力）', () => {
    renderCardWithoutWriteup(
      makeChallenge({ status: 'solved', scanId: 'scan_1', confirmedFlag: 'flag{x}' }),
    )

    expect(writeupButton()).toBeNull()
  })

  it('★ 点击把**这道题**交给调用方', () => {
    const onWriteup = vi.fn()
    const challenge = makeChallenge({ status: 'solved', scanId: 'scan_1', confirmedFlag: 'flag{x}' })
    renderCard(challenge, { onWriteup })

    writeupButton()!.click()

    expect(onWriteup).toHaveBeenCalledTimes(1)
    expect(onWriteup).toHaveBeenCalledWith(challenge)
  })
})

/*
  ─────────────────────────────────────────────────────────────
  「处置工单」入口（v0.41，应急响应复盘产物）
  ─────────────────────────────────────────────────────────────

  ## ★ 判据与上面那组**不同**

  题解判 `scanId`（跑过就有）；工单判 `category` 前缀 `ir-`
  （这类任务才有）。两者在同一道 pwn 题上会给出相反答案 ——
  而拿 `scanId` 来判工单的后果是每道跑过的 pwn 题都露出一个
  点开必然 409 的按钮。

  ## 为什么也要测"不传就整项不渲染"

  与 `onWriteup` 同一条约定：能力由调用方按需给。少测这一条的
  后果是有人把按钮改成常驻，而页面上多出一个永远点不出东西的入口。
*/
function renderCardWithWorkOrder(
  challenge: KernelChallenge,
  onWorkOrder: (c: KernelChallenge) => void,
) {
  return render(
    <MemoryRouter>
      <ChallengeCard
        challenge={challenge}
        onSubmit={noop}
        onSolve={noop}
        onCancel={noop}
        onRelease={noop}
        onSetPriority={noop}
        onWriteup={noop}
        onWorkOrder={onWorkOrder}
      />
    </MemoryRouter>,
  )
}

/** 「处置工单」按钮存在吗。用可访问名精确定位，避免撞上标题里的"工单" */
function workOrderButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: '处置工单' })
}

describe('ChallengeCard：处置工单入口的可见性', () => {
  it('★★ 应急响应任务有「处置工单」按钮', () => {
    renderCardWithWorkOrder(
      makeChallenge({ category: 'ir-root-cause', status: 'solved', scanId: 'scan_1' }),
      noop,
    )

    expect(workOrderButton()).not.toBeNull()
  })

  it('★★ 跑过的 pwn 题**没有**这个按钮（scanId 非空也不行）', () => {
    /*
      这是与题解分歧的那一条。同一张卡上：
        · 「题解」在（scanId 非空）
        · 「处置工单」不在（不是 ir-* 分类）
    */
    renderCardWithWorkOrder(
      makeChallenge({ category: 'pwn', status: 'solved', scanId: 'scan_1' }),
      noop,
    )

    expect(workOrderButton()).toBeNull()
    // 而题解按钮确实在 —— 证明不是整排都没渲染
    expect(writeupButton()).not.toBeNull()
  })

  it('★ 调用方不传 onWorkOrder 时整项不渲染（按需能力）', () => {
    // renderCard 默认只给 onWriteup，正好是"没传 onWorkOrder"的形状
    renderCard(makeChallenge({ category: 'ir-root-cause', status: 'solved', scanId: 'scan_1' }))

    expect(workOrderButton()).toBeNull()
    expect(writeupButton()).not.toBeNull()
  })

  it('★ 点击把**这道题**交给调用方', () => {
    const onWorkOrder = vi.fn()
    const challenge = makeChallenge({ category: 'ir-timeline', status: 'solved', scanId: 'scan_1' })
    renderCardWithWorkOrder(challenge, onWorkOrder)

    workOrderButton()!.click()

    expect(onWorkOrder).toHaveBeenCalledTimes(1)
    expect(onWorkOrder).toHaveBeenCalledWith(challenge)
  })

  it('★ 还没跑完的应急响应题也有入口 —— 点了会拿到"等复盘跑完"那句', () => {
    /*
      ★ 与题解相反的一处：题解在没跑过时**藏起来**（点了必然 409 且
        没有任何补救动作）。工单这里不藏，因为后端那句话是有用的
        —— 它告诉用户"这是应急响应题，等它跑完"。
        判据是分类而不是进度，所以进度不影响入口。
    */
    renderCardWithWorkOrder(
      makeChallenge({ category: 'ir-scope', status: 'unsolved', scanId: null }),
      noop,
    )

    expect(workOrderButton()).not.toBeNull()
  })
})
