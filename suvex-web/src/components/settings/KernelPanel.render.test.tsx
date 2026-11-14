/**
 * `KernelPanel` 墙钟预算字段的**渲染**验证（v1.61）。
 *
 * ## 与 `kernel.test.ts` 的分工
 *
 * `kernel.test.ts` 断言的是 `saveKernelSettings` **发出去的请求体**有没有
 * `wallClockSeconds`。它证明不了"用户在界面上**看得到**这个字段、能改、改完
 * 值对得上" —— 那正是本文件补的另一半：
 *
 *   1. 字段真的渲染出来（`#ctf2-wall-clock` 存在、label/help 文案对）；
 *   2. `null` 显示为空串（不是 `"null"`、不是 `0`），placeholder 是自动推导值；
 *   3. 显式值原样显示；
 *   4. 越界时**不出红**（校验只在该字段填了非法值时红，这里只验渲染层
 *      的 value/placeholder 绑对）。
 *
 * ⚠️ `vitest.config.ts` 关了 `globals`，自动 cleanup 不注册，手动 `cleanup()`。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { KernelPanel } from './KernelPanel'
import { loadKernel, __resetKernelForTest } from '@/data/kernel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  __resetKernelForTest()
})

/** 一组控件契约形状的 settings（真的会经 `lib/api` → fetch 解析成 KernelSettings） */
function baseSettings(over: Record<string, unknown> = {}) {
  return {
    submitMode: 'safe',
    maxWrongSubmissions: 3,
    defaultSandboxProfile: 'suvex-sandbox',
    maxConcurrentSolves: 3,
    solvePollSeconds: 5,
    autoSyncSeconds: 300,
    autoPriorityNew: true,
    autoStartNew: false,
    maxTurns: 80,
    wallClockSeconds: null,
    fallbackProviderId: null,
    enableAgentsecScenarios: false,
    enableIrScenarios: false,
    enableMultiAgent: false,
    updatedAt: '2026-11-03T15:00:00Z',
    ...over,
  }
}

/**
 * 把 `loadKernel()` 会打的三路（snapshot / settings / queue）分别回好，
 * 再把 `useProviders` 的 `GET /providers` 回空数组。
 * 用路径判断而不是"所有请求回同一个 body"—— 三条路形状不同。
 */
function stubKernelFetch(settings: Record<string, unknown>) {
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    let payload: unknown = {}
    if (url.includes('/kernel/settings')) payload = settings
    else if (url.includes('/kernel/snapshot')) payload = { account: null, challenges: [] }
    else if (url.includes('/kernel/queue')) payload = { items: [] }
    else if (url.includes('/providers')) payload = []
    return Promise.resolve(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
}

describe('KernelPanel 墙钟预算字段渲染（v1.61）', () => {
  it('★ 字段渲染出来：label「单次任务墙钟预算（秒）」+ help 点破「留空 = 自动」', async () => {
    stubKernelFetch(baseSettings())
    await loadKernel()
    render(<KernelPanel />)

    // label 真的渲染（getByLabelText 会经 htmlFor↔id 关联到 input）
    const input = screen.getByLabelText('单次任务墙钟预算（秒）') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.id).toBe('ctf2-wall-clock')

    // help 文案必须点破"留空 = 自动"——否则用户会以为空着是"不限时"
    expect(screen.getByText(/留空 = 按回合预算自动推导/)).toBeTruthy()
  })

  it('★★ 自动（null）显示为空串，placeholder 是「自动（回合×45）」', async () => {
    stubKernelFetch(baseSettings({ maxTurns: 80, wallClockSeconds: null }))
    await loadKernel()
    render(<KernelPanel />)

    const input = screen.getByLabelText('单次任务墙钟预算（秒）') as HTMLInputElement
    // null 必须是空串，绝不能渲染成 "null" 或 "0"
    expect(input.value).toBe('')
    // placeholder 提示自动推导值：80×45=3600
    expect(input.placeholder).toBe('自动（3600s）')
  })

  it('★ 显式值原样显示在输入框里', async () => {
    stubKernelFetch(baseSettings({ wallClockSeconds: 1800 }))
    await loadKernel()
    render(<KernelPanel />)

    const input = screen.getByLabelText('单次任务墙钟预算（秒）') as HTMLInputElement
    expect(input.value).toBe('1800')
  })

  it('★ 换回合数时 placeholder 里的自动值跟着变（80→200 轮：3600→9000）', async () => {
    stubKernelFetch(baseSettings({ maxTurns: 200, wallClockSeconds: null }))
    await loadKernel()
    render(<KernelPanel />)

    const input = screen.getByLabelText('单次任务墙钟预算（秒）') as HTMLInputElement
    expect(input.placeholder).toBe('自动（9000s）')
  })
})
