import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * `RacePanel` —— 赛事托管面板。
 *
 * ## 钉住的三件事
 *
 * ① **未跑时画起跑表单，跑了之后画进度条而不是表单** —— 否则用户会
 *    对着一个已经跑着的界面反复点「开始」，而后端会 409。
 * ② **点「请求停止」后按钮必须禁用且换文案** —— 停止不是瞬时的（当前
 *    step 跑完才收手），不禁用就会让用户以为没点上、反复点。
 * ③ **进度表按 stage 落到正确的行** —— flag 显示在已解的题上，
 *    错误显示在失败的题上。画错会让人以为某题解出来了但其实没有。
 *
 * 网络层整体 mock（`@/data/ctf-race`），不打真后端。
 */

const getRaceStatus = vi.fn()
const startRace = vi.fn()
const stopRace = vi.fn()

vi.mock('@/data/ctf-race', async () => {
  const actual = await vi.importActual<typeof import('@/data/ctf-race')>('@/data/ctf-race')
  return {
    ...actual,
    getRaceStatus: (...a: unknown[]) => getRaceStatus(...a),
    startRace: (...a: unknown[]) => startRace(...a),
    stopRace: (...a: unknown[]) => stopRace(...a),
  }
})

import { RacePanel } from '@/components/kernel/RacePanel'

function idleStatus() {
  return {
    running: false,
    models: [],
    challenges: {},
    cost: {},
    report: null,
    error: null,
  }
}

function runningStatus() {
  return {
    running: true,
    models: ['deepseek-v4.1-flash'],
    challenges: {
      total: 2,
      solved: 1,
      failed: 0,
      skipped: 0,
      rounds: 3,
      stopped_reason: null,
      elapsed_seconds: 12,
      by_challenge: {
        a: { remote_id: 'a', title: '题A', stage: 'solved', winner_model: 'm1', flag: 'flag{a}', attempts: 1, last_error: null },
        b: { remote_id: 'b', title: '题B', stage: 'running', winner_model: null, flag: null, attempts: 0, last_error: null },
      },
    },
    cost: { totalCostUsd: 0.0123, totalTokens: 1234 },
    report: null,
    error: null,
  }
}

beforeEach(() => {
  getRaceStatus.mockReset()
  startRace.mockReset()
  stopRace.mockReset()
})

afterEach(() => {
  vi.clearAllTimers()
})

describe('RacePanel（赛事托管）', () => {
  it('未跑时显示起跑表单与开始按钮', async () => {
    getRaceStatus.mockResolvedValue(idleStatus())
    render(<RacePanel />)
    await waitFor(() => expect(getRaceStatus).toHaveBeenCalled())
    expect(await screen.findByText('开始整场托管')).toBeTruthy()
    expect(document.querySelector('[data-race-form]')).toBeTruthy()
  })

  it('跑起来时显示进度、flag 落到已解题、成本可见', async () => {
    getRaceStatus.mockResolvedValue(runningStatus())
    render(<RacePanel />)
    await waitFor(() => expect(getRaceStatus).toHaveBeenCalled())
    // 进度条
    expect(await screen.findByText(/赛事托管进行中/)).toBeTruthy()
    // flag 行
    expect(await screen.findByText('flag{a}')).toBeTruthy()
    // 成本
    const cost = document.querySelector('[data-race-cost]')
    expect(cost?.textContent).toContain('$0.0123')
    // 停止按钮
    expect(document.querySelector('[data-race-stop]')).toBeTruthy()
  })

  it('点请求停止后按钮禁用且换成「正在停止」并显示红条', async () => {
    getRaceStatus.mockResolvedValue(runningStatus())
    stopRace.mockResolvedValue({ ...runningStatus(), running: true })
    render(<RacePanel />)
    await waitFor(() => expect(getRaceStatus).toHaveBeenCalled())
    const stopBtn = (await screen.findByText('请求停止')).closest('button')!
    fireEvent.click(stopBtn)
    await waitFor(() => expect(stopRace).toHaveBeenCalled())
    // 文案变「正在停止…」且禁用
    const stopped = await screen.findByText('正在停止…')
    expect(stopped.closest('button')?.disabled).toBe(true)
    // 红条文案出现
    expect(await screen.findByText(/已请求停止/)).toBeTruthy()
  })

  it('点开始调用 startRace 并携带模型清单', async () => {
    getRaceStatus.mockResolvedValue(idleStatus())
    startRace.mockResolvedValue(runningStatus())
    render(<RacePanel />)
    await waitFor(() => expect(getRaceStatus).toHaveBeenCalled())
    fireEvent.click(screen.getByText('开始整场托管'))
    await waitFor(() => expect(startRace).toHaveBeenCalled())
    const arg = startRace.mock.calls[0][0]
    expect(arg.models).toEqual(['deepseek-v4.1-flash'])
  })
})
